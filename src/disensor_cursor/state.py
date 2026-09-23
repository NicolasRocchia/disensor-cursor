"""Per-session state, kept outside every repository.

Three decisions shape this file.

The state never lives inside a repository. The adapter asks for a clean tree
before it suggests a round, so writing its own bookkeeping into the tree would
dirty exactly what it just demanded and trigger itself forever.

The filename is DERIVED, never received. `conversation_id` arrives in the hook
payload, and a value like `../reviewers` would resolve to
`~/.disensor/reviewers.json` and corrupt disensor's own reviewer registry.
Hashing it closes that, and every Windows shape with it: drive letters,
reserved names, alternate data streams.

Edits and verdicts go to different files. `afterFileEdit` and `stop` run as
separate processes and a shared read-modify-write loses updates: the edit hook
reads counter=0, the stop hook saves counter=1, the edit hook saves its stale
copy back. Splitting by writer removes the race without a lock; the lock is
only for two `stop` hooks racing each other.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

from . import clock

STATE_DIR = Path.home() / ".disensor-cursor"

# Elegibilidad ternaria y no un booleano: un booleano no sabe revocar. Una
# sesion reanudada, o una que cambia de modo sin otro sessionStart, arrastraria
# un permiso viejo. `unknown` es el default y no engancha.
ADMITTED = "admitted"
REJECTED = "rejected"
UNKNOWN = "unknown"

# Los motivos por los que el hook puede avisar. Un aviso se marca como dado
# para un ESTADO, motivo y head, y no para la conversacion entera. Un contador
# global se gastaba con el primer aviso y "el arbol esta sucio" silenciaba
# "falta la ronda"; un corte por motivo a secas se gastaba igual un nivel mas
# abajo, porque despues de corregir un hallazgo hace falta otra ronda sobre un
# head nuevo, y ese es el aviso que mas importa.
DIRTY_TREE = "dirty_tree"
ROUND_REQUIRED = "round_required"
BLOCKED = "blocked"

LOCK_TIMEOUT = 2.0
LOCK_POLL = 0.05
# Un lock se sostiene lo que dura leer y escribir un JSON chico. Uno mas viejo
# que esto quedo de un proceso que murio con el lock tomado, y sin reclamarlo
# cada hook esperaba el timeout entero durante los treinta dias que tarda la
# purga en llevarselo: justo lo que Cursor le concede a sessionStart.
STALE_LOCK_SECONDS = 10.0

# Las sesiones viejas se borran solas. Sin vencimiento, este directorio crecia
# para siempre con la lista de archivos que cada conversacion toco.
MAX_AGE_DAYS = 30
PURGE_LIMIT = 500
PURGE_EXAMINE = 2000

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_link(path: Path) -> bool:
    """A symlink, or a Windows junction, which `is_symlink()` does not see.

    Destructive operations do not go through either. Verified on this
    machine: for a junction, `is_symlink()` is False on 3.12 and 3.14, and a
    glob under it walks the target.
    """
    try:
        if path.is_symlink():
            return True
        junction = getattr(path, "is_junction", None)
        if junction is not None:
            return bool(junction())
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & _REPARSE_POINT)
    except OSError:
        return False


def token(reason: str, head: str | None) -> str:
    """The identity of a notice: what it is about and at which commit."""
    return f"{reason}@{head or 'unknown'}"


def session_key(conversation_id: str, worktree: str) -> str:
    """Opaque, filesystem-safe identity of a session in a worktree.

    The worktree is part of the key on purpose: the same conversation opened
    against two checkouts is two situations, and one must not silence the other.
    """
    material = json.dumps([str(conversation_id or ""), str(worktree or "")], ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def usable() -> bool:
    """Whether the state directory is a real directory, or does not exist yet.

    A link or a junction there sends every write and every delete somewhere
    else, possibly into a repository. Refusing only the deletes, as a first
    version did, still let a new session delete through it. So nothing is
    written or deleted there at all: the hook stays inactive, which is the
    quiet side, and `doctor` says why.
    """
    return not is_link(STATE_DIR)


class StateUnavailable(OSError):
    """The state directory is a link: nothing is written or deleted there."""


def _ensure_dir() -> Path:
    if not usable():
        raise StateUnavailable(f"{STATE_DIR} is a link or junction: the hook stays inactive")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def state_path(key: str) -> Path:
    return STATE_DIR / f"state-{key}.json"


def edits_path(key: str) -> Path:
    return STATE_DIR / f"edits-{key}.jsonl"


def log_path() -> Path:
    return STATE_DIR / "hook.log"


class _Lock:
    """Cross-process lock built on O_EXCL, which is atomic on Windows too.

    It can never be what stalls the agent: the wait comes out of the run's
    budget, a lock left behind by a dead process is reclaimed, and past the
    wait the caller proceeds anyway. Losing a race means at worst a repeated
    reminder, which is the benign failure; blocking Cursor is not.
    """

    def __init__(self, target: Path) -> None:
        self.path = target.with_suffix(target.suffix + ".lock")
        self.held = False

    def _reclaim_if_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return
        if age > STALE_LOCK_SECONDS:
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "_Lock":
        # Ni el lock se crea a traves de un enlace: crearlo y borrarlo al salir
        # ya seria escribir y borrar donde el enlace apunte.
        if not usable():
            return self
        # El directorio antes que el lock. En la primera escritura no existia:
        # el O_EXCL fallaba con FileNotFoundError y cada escritor seguia sin
        # lock, justo en el momento en que varios podian escribir a la vez.
        try:
            _ensure_dir()
        except OSError:
            return self
        limit = time.monotonic() + clock.left(LOCK_TIMEOUT)
        self._reclaim_if_stale()
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                if time.monotonic() >= limit:
                    return self
                time.sleep(LOCK_POLL)
            except OSError:
                return self

    def __exit__(self, *exc) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


def read_state(key: str) -> dict:
    path = state_path(key)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Un estado ilegible se trata como ausente: el default es no enganchar,
        # asi que perderlo degrada hacia el silencio y nunca hacia un aviso.
        return {}
    return data if isinstance(data, dict) else {}


REPLACE_RETRY = 1.0


def replace_file(temporary: Path, path: Path) -> None:
    """os.replace, retried for a moment on Windows sharing violations.

    On Windows a rename onto a file fails while another process holds it open:
    a hook reading the state, an antivirus scanning what was just written, or
    Cursor itself reading hooks.json. Found by a test with twelve real
    processes, on Python 3.12. It lasts milliseconds, so a short retry within
    the run's budget is the fix; past that the temporary file is removed and
    the error is raised, which the hook logs instead of blocking on.
    """
    limit = time.monotonic() + clock.left(REPLACE_RETRY)
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if time.monotonic() >= limit:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.02)


def unique_temporary(path: Path) -> Path:
    """A temporary name no other writer can be using at the same time.

    With one fixed name, two writers that both missed the lock wrote the same
    temporary file, and each could rename the other's half-written bytes.
    """
    return path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")


def write_state(key: str, data: dict) -> None:
    """Atomic replace, refusing a symlink, mirroring disensor's own registry."""
    _ensure_dir()
    path = state_path(key)
    if is_link(path):
        raise StateUnavailable(f"{path} is a symlink: refusing to write through it")
    temporary = unique_temporary(path)
    temporary.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    replace_file(temporary, path)


def update_state(key: str, changes: dict) -> dict:
    """Read, merge and write under the lock. For the writers that race."""
    with _Lock(state_path(key)):
        data = read_state(key)
        data.update(changes)
        write_state(key, data)
        return data


def eligibility(key: str) -> str:
    value = read_state(key).get("eligibility")
    return value if value in (ADMITTED, REJECTED) else UNKNOWN


def already_reported(key: str, notice: str) -> bool:
    return notice in set(read_state(key).get("reported", []))


def mark_reported(key: str, notice: str) -> None:
    with _Lock(state_path(key)):
        data = read_state(key)
        reported = set(data.get("reported", []))
        reported.add(notice)
        data["reported"] = sorted(reported)
        write_state(key, data)


def append_edit(key: str, record: dict) -> None:
    """One line per edit, metadata only.

    `old_string` and `new_string` never land here. Keeping them would retain
    source code, and whatever secret it carried, outside the repository, with
    no expiry and no purpose: to decide whether to remind, the path is enough.
    """
    _ensure_dir()
    path = edits_path(key)
    # Un archivo propio enlazado a otro lado se niega igual que el directorio:
    # abrirlo para agregar escribiria donde apunte el enlace.
    if is_link(path):
        raise StateUnavailable(f"{path} is a link: refusing to write through it")
    line = json.dumps(record, ensure_ascii=False) + "\n"
    # newline="" para que Windows no traduzca el salto a CRLF: el archivo es
    # nuestro y se lee linea por linea, igual que el resto del estado.
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write(line)


def read_edits(key: str) -> list[dict]:
    """Every edit this session recorded, oldest first.

    A truncated or half-written line is skipped rather than fatal: the file is
    append-only from a process that can be killed at any moment, and losing one
    record degrades towards staying quiet, which is the safe direction.
    """
    path = edits_path(key)
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return records
    return records


def note(message: str) -> None:
    """Diagnostics to a file, never to stdout: stdout is the hook protocol."""
    try:
        _ensure_dir()
        path = log_path()
        if is_link(path):
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8", newline="") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def clear_edits(key: str) -> None:
    """Forget the edits of a conversation's previous session.

    Kept across sessions, a new session inherited them: it was told about work
    it never did, and its generator was prefilled from models it never ran.
    """
    if not usable():
        return
    try:
        edits_path(key).unlink()
    except OSError:
        pass


def purge_stale(
    max_age_days: float = MAX_AGE_DAYS, limit: int = PURGE_LIMIT, examine: int = PURGE_EXAMINE,
) -> int:
    """Delete session files nobody touched in a while. Returns how many.

    Only our own names, only regular files, only directly inside the state
    directory, and a bounded amount of work: this runs inside sessionStart,
    and housekeeping must never be what makes a hook slow. And never through
    a link: if the state directory is a symlink or a junction, a glob under
    it walks somebody else's directory, so nothing is deleted at all.
    """
    if not usable() or not STATE_DIR.is_dir():
        return 0
    limit_time = time.time() - max_age_days * 86400
    removed = examined = 0
    for pattern in ("state-*.json", "edits-*.jsonl", "state-*.lock", "state-*.tmp"):
        for path in STATE_DIR.glob(pattern):
            examined += 1
            if removed >= limit or examined > examine or clock.left(1.0) < 0.2:
                return removed
            try:
                if is_link(path) or not path.is_file():
                    continue
                if path.stat().st_mtime < limit_time:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed
