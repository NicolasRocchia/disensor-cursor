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
import time
from pathlib import Path

STATE_DIR = Path.home() / ".disensor-cursor"

# Elegibilidad ternaria y no un booleano: un booleano no sabe revocar. Una
# sesion reanudada, o una que cambia de modo sin otro sessionStart, arrastraria
# un permiso viejo. `unknown` es el default y no engancha.
ADMITTED = "admitted"
REJECTED = "rejected"
UNKNOWN = "unknown"

# Los motivos por los que el hook puede avisar. El corte es POR MOTIVO y no por
# conversacion: un contador global se consume con el primer aviso, y entonces
# "el arbol esta sucio" silenciaria "falta la ronda", que es el aviso que
# importaba.
DIRTY_TREE = "dirty_tree"
ROUND_REQUIRED = "round_required"
BLOCKED = "blocked"

LOCK_TIMEOUT = 5.0
LOCK_POLL = 0.05


def session_key(conversation_id: str, worktree: str) -> str:
    """Opaque, filesystem-safe identity of a session in a worktree.

    The worktree is part of the key on purpose: the same conversation opened
    against two checkouts is two situations, and one must not silence the other.
    """
    material = json.dumps([str(conversation_id or ""), str(worktree or "")], ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _ensure_dir() -> Path:
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

    A stale lock cannot deadlock the agent loop: past the timeout the caller
    proceeds anyway. Losing the race means at worst a repeated reminder, which
    is the benign failure; blocking Cursor is not.
    """

    def __init__(self, target: Path) -> None:
        self.path = target.with_suffix(target.suffix + ".lock")
        self.held = False

    def __enter__(self) -> "_Lock":
        limit = time.monotonic() + LOCK_TIMEOUT
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


def write_state(key: str, data: dict) -> None:
    """Atomic replace, refusing a symlink, mirroring disensor's own registry."""
    _ensure_dir()
    path = state_path(key)
    if path.is_symlink():
        raise OSError(f"{path} is a symlink: refusing to write through it")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(temporary, path)


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


def already_reported(key: str, reason: str) -> bool:
    return reason in set(read_state(key).get("reported", []))


def mark_reported(key: str, reason: str) -> None:
    with _Lock(state_path(key)):
        data = read_state(key)
        reported = set(data.get("reported", []))
        reported.add(reason)
        data["reported"] = sorted(reported)
        write_state(key, data)


def append_edit(key: str, record: dict) -> None:
    """One line per edit, metadata only.

    `old_string` and `new_string` never land here. Keeping them would retain
    source code, and whatever secret it carried, outside the repository, with
    no expiry and no purpose: to decide whether to remind, the path is enough.
    """
    _ensure_dir()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(edits_path(key), "a", encoding="utf-8") as handle:
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
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(log_path(), "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass
