"""`disensor-cursor`: install the hooks, and tell the truth about their state.

Installing at USER level is the default, and it is not a convenience.

Cursor's project hooks are executable commands, versioned in the repository,
that run on their own in a trusted workspace. disensor's own reviewer registry
exists because of exactly that shape: "The commands live on the machine, never
in the repository. A reviewer entry is executable code: if a pull request could
add one, opening that pull request would run arbitrary commands on the machine
of whoever reviews it."

Writing to `~/.cursor/hooks.json` keeps that standard: the hook activates only
inside repositories that hold a `disensor.config.json`, the same guard disensor
uses for the global CLAUDE.md section it can install. `--project` stays
available, and says out loud what it costs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from . import __version__, disensor, executables, gitio, state, templates

USER_HOOKS = Path.home() / ".cursor" / "hooks.json"

_GENERATED_BY = re.compile(r"--generated-by\s+(\S+)")


def _resolve_python() -> str:
    """The interpreter running this install, absolute.

    Not `shutil.which("disensor-cursor")`: on Windows with Python 3.10 and
    3.11 that search looks at the current directory first, and the hook runs
    with the repository as its cwd, so a repository shipping a same-named
    script would choose what executes. `sys.executable` is already absolute and
    is the interpreter that has this package importable.
    """
    return os.path.abspath(sys.executable)


def _repository_content(path: Path) -> str:
    """Why a program found on PATH is repository content, or "" if it is not.

    What init resolves is written into hooks.json and runs in every later
    session, in every repository. One committed to the repository it lives in
    came from somebody else's history, and a PATH entry pointing at it would
    make that history run everywhere. A disensor in an untracked virtualenv
    inside some checkout is the user's own install, and is not refused. The
    repository init runs from is already excluded from PATH (see `main`).

    The git that answers comes from outside the repository being asked
    about, and only its two meaningful answers count: a git from inside it
    answered with an error, and the error read as "not committed".
    """
    repository = executables.enclosing_repository(path.parent)
    if repository is None:
        return ""
    try:
        code, _, err = gitio.run_git(
            ["ls-files", "--error-unmatch", "--", path.name], path.parent, exclude=(repository,),
        )
    except gitio.GitUnavailable as exc:
        return f"whether the repository at {repository} commits it could not be checked ({exc})"
    if code == 0:
        return "it is committed to the repository it lives in"
    if code == 1:
        return ""
    return f"whether the repository at {repository} commits it could not be checked (git exited {code})"


def _resolve_disensor() -> tuple[str, str]:
    # Desde el PATH y nunca el cwd: `init` se corre adentro de un repositorio,
    # y lo que resuelva aca queda escrito en hooks.json para cada corrida.
    found = executables.find("disensor")
    if not found:
        return "", "disensor is not on PATH. Install it first: pip install disensor"
    path = os.path.abspath(found)
    reason = _repository_content(Path(path))
    if reason:
        return path, (
            f"found at {path}, but {reason}: the hooks would run repository content in every "
            "session. Install disensor outside any repository (pip install --user disensor, or pipx)"
        )
    ok, detail = disensor.probe(Path(path))
    if not ok:
        return path, f"found at {path}, but it does not answer as expected: {detail}"
    return path, ""


def _write_json(path: Path, data: dict) -> None:
    """Atomic, and patient with Cursor: it may be reading hooks.json right now."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"{path} is a symlink: refusing to write through it")
    temporary = state.unique_temporary(path)
    temporary.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    state.replace_file(temporary, path)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _ours(entry) -> bool:
    return templates.is_ours(entry)


def _our_entries(data: dict) -> dict[str, list[dict]]:
    return {
        event: [e for e in entries if _ours(e)]
        for event, entries in (data.get("hooks") or {}).items()
        if isinstance(entries, list) and any(_ours(e) for e in entries)
    }


_VERSION_MARK = re.compile(r"^<!-- disensor-cursor v[^>]*-->\s*$", re.MULTILINE)


def _rule_is_ours(text: str) -> bool:
    """Whether a rule file is exactly what some version of init wrote.

    The version marker is ignored so an upgrade can recognise what an older
    version wrote. Anything else that differs is an edit, and an edit is left
    alone: same doctrine as disensor's own init, which never overwrites a
    block it cannot recognise as its own.
    """
    def normal(s: str) -> str:
        return _VERSION_MARK.sub("", s.replace("\r\n", "\n")).strip()
    return normal(text) == normal(templates.RULE)


def main_init(args) -> int:
    report: list[str] = []
    python = _resolve_python()
    disensor_path, problem = _resolve_disensor()
    if problem and not args.force:
        print(f"init: {problem}")
        print("Fix that first, or re-run with --force to write the hooks anyway.")
        return 1
    if problem:
        report.append(f"WARNING {problem}")

    if args.project:
        repo = gitio.repo_root(Path.cwd())
        if repo is None:
            print("init: --project needs to run inside a git repository")
            return 1
        target = repo / ".cursor" / "hooks.json"
        base = gitio.default_base(repo)
        if not base:
            report.append(
                "WARNING no origin/main or origin/master resolved, so the hook has no target "
                "branch and will stay quiet until there is one"
            )
    else:
        target = USER_HOOKS
        # A nivel usuario el hook sirve a muchos repositorios, asi que la rama
        # destino se resuelve por repositorio en cada corrida y no se fija aca.
        repo, base = None, None

    try:
        generated = templates.hooks_config(python, disensor_path, base)
        merged = templates.merge_hooks(_read_json(target), generated)
    except ValueError as exc:
        print(f"init: {exc}")
        return 1
    _write_json(target, merged)
    report.append(f"wrote {target}")

    if args.project and repo is not None:
        rule = repo / ".cursor" / "rules" / "disensor.mdc"
        label = rule.relative_to(repo).as_posix()
        if rule.exists() and not _rule_is_ours(rule.read_bytes().decode("utf-8", "replace")):
            report.append(f"kept    {label} (edited: left as it is)")
        else:
            rule.parent.mkdir(parents=True, exist_ok=True)
            # En bytes: write_text traduce los saltos a CRLF en Windows y el
            # archivo entra al repositorio de otra persona con otros finales
            # de linea que los suyos.
            rule.write_bytes(templates.RULE.encode("utf-8"))
            report.append(f"wrote {label}")
        report.append("")
        report.append(
            "WARNING: .cursor/hooks.json is executable code, versioned in this repository, that "
            "Cursor runs on its own in a trusted workspace. A pull request can change what it "
            "runs. That is why the default of this tool is --global, which installs the same "
            "hooks in your home and activates them only inside repositories that hold a "
            "disensor.config.json."
        )
        report.append("")
        report.append(
            f"WARNING: the commands hold absolute paths from THIS machine ({python}). Committed "
            "and cloned elsewhere they will not resolve, and the hooks will do nothing there "
            "until each person runs `disensor-cursor init` for themselves. Consider adding "
            ".cursor/hooks.json to .gitignore instead of committing it."
        )
    else:
        report.append(
            "The hooks activate only inside repositories whose root holds a "
            "disensor.config.json. Everywhere else they do nothing."
        )

    report.append("")
    report.append(
        f"disensor-cursor {__version__} is a pilot. It carries no stability promise: flags and "
        "behaviour can change between commits."
    )
    for line in report:
        print(line)
    return 0


def main_uninstall(args) -> int:
    """Remove exactly what init added, and nothing somebody else wrote."""
    report: list[str] = []
    if args.project:
        repo = gitio.repo_root(Path.cwd())
        if repo is None:
            print("uninstall: --project needs to run inside a git repository")
            return 1
        target = repo / ".cursor" / "hooks.json"
    else:
        repo, target = None, USER_HOOKS

    if target.exists():
        data = _read_json(target)
        hooks = data.get("hooks") or {}
        kept_hooks: dict = {}
        removed = 0
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                kept_hooks[event] = entries
                continue
            kept = [e for e in entries if not _ours(e)]
            removed += len(entries) - len(kept)
            if kept:
                kept_hooks[event] = kept
        if not removed:
            report.append(f"nothing of disensor-cursor in {target}")
        elif not kept_hooks and set(data) <= {"version", "hooks"}:
            target.unlink()
            report.append(f"removed {target} (it only held disensor-cursor hooks)")
        else:
            data["hooks"] = kept_hooks
            _write_json(target, data)
            report.append(f"removed {removed} disensor-cursor entries from {target}; kept the rest")
    else:
        report.append(f"no {target}")

    if args.project and repo is not None:
        rule = repo / ".cursor" / "rules" / "disensor.mdc"
        label = rule.relative_to(repo).as_posix()
        if rule.exists():
            if _rule_is_ours(rule.read_bytes().decode("utf-8", "replace")):
                rule.unlink()
                report.append(f"removed {label}")
            else:
                report.append(f"kept    {label} (edited: remove it yourself if you want it gone)")

    if args.purge:
        directory = state.STATE_DIR
        # is_link y no is_symlink: una junction de Windows no es un symlink
        # para Python, y rmtree sobre ella fallaba con un traceback.
        if state.is_link(directory):
            report.append(f"kept    {directory} (a link or junction: refusing to delete through it)")
        elif directory.exists():
            try:
                shutil.rmtree(directory)
                report.append(f"removed {directory}")
            except OSError as exc:
                report.append(f"could not remove {directory}: {exc}")
        else:
            report.append(f"no {directory}")

    for line in report:
        print(line)
    return 0


# Como arranca Cursor 3.21 un hook, leido de su codigo (`executeHookDirect`,
# `kL` y `zr` en el host de extensiones) y visto en vivo el 2026-09-24. En
# Windows no es cmd.exe sino PowerShell: el primero de pwsh y powershell que
# encuentra probando .exe, .bat, .cmd y .ps1 sobre todo el PATH, con el payload
# en un archivo del temporal de Node que PowerShell manda por un pipe. En el
# resto, `$SHELL -c` con el payload por stdin. doctor no lo reproduce: correr
# una shell arrastra todo su arranque (perfiles, rutas de modulos, archivos de
# inicio), y cada una de esas vias era una forma de que corriera codigo del
# proyecto. Lo describe, y dice cuando la maquina no dejaria correr ningun hook.
_TYPOGRAPHIC_QUOTES = "\u2018\u2019\u201a\u201b"


def _system_powershell() -> str:
    system = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return str(Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _cursor_shell() -> tuple[str | None, str]:
    """The shell Cursor would start for a hook on Windows, or what stops it.

    Found the way Cursor's resolver finds it (3.21 and 3.22), without running
    anything: each extension over the whole PATH before the next one, so a
    pwsh.exe late on PATH wins over a pwsh.cmd early on it, and anything that
    exists counts, a directory too, with PATH entries taken as they are. A
    .bat or .cmd makes Cursor start cmd.exe with PowerShell's arguments, which
    opens its banner, reads the end of its input and exits 0 having run
    nothing. A .ps1 makes it start the system's Windows PowerShell. One step
    stays out of sight: before PATH, Cursor tries each name in its own working
    directory, which only Cursor knows.
    """
    entries = os.environ.get("PATH", "").split(os.pathsep)
    for name in ("pwsh", "powershell"):
        for extension in (".exe", ".bat", ".cmd", ".ps1"):
            for entry in entries:
                candidate = os.path.join(entry, name + extension) if entry else ""
                if not candidate or not os.path.exists(candidate):
                    continue
                # Primero lo que Cursor hace con la extension: cambia un .ps1
                # por el PowerShell del sistema y un .bat o .cmd por cmd.exe,
                # y en ninguno de los dos casos corre lo que encontro, sea un
                # archivo o un directorio.
                if extension == ".ps1":
                    system = _system_powershell()
                    if os.path.isfile(system):
                        return system, ""
                    return None, f"{candidate} makes Cursor start {system}, which is not there"
                if extension in (".bat", ".cmd"):
                    return None, (
                        f"{candidate} is a batch file: Cursor would start cmd.exe with PowerShell's "
                        "arguments, and no hook would run"
                    )
                if executables.inside_project(entry) or executables.inside_project(os.path.realpath(candidate)):
                    return None, f"Cursor would start {candidate}, which is relative to or inside the project"
                if not os.path.isfile(candidate):
                    return None, f"Cursor would try to start {candidate}, which is not a file, and no hook would run"
                return candidate, ""
    system = _system_powershell()
    if os.path.isfile(system):
        return system, ""
    return None, "no PowerShell found, and Cursor runs every hook through one"


def _cursor_tmpdir() -> str:
    """Where Cursor writes each payload: Node's `os.tmpdir()` on Windows."""
    path = (
        os.environ.get("TEMP") or os.environ.get("TMP")
        or f"{os.environ.get('SystemRoot') or os.environ.get('windir')}\\temp"
    )
    if len(path) > 1 and path.endswith("\\") and not path.endswith(":\\"):
        return path[:-1]
    return path


def _describe_cursor() -> bool:
    """How Cursor would start the hooks here. False when it would start none."""
    if sys.platform != "win32":
        print(f"  cursor:   hooks run through {os.environ.get('SHELL') or '/bin/sh'} -c, payload on stdin")
        return True
    shell, problem = _cursor_shell()
    if shell is None:
        print(f"  cursor:   NO HOOK WOULD RUN: {problem}")
        return False
    print(f"  cursor:   hooks run through {shell}")
    tmpdir = _cursor_tmpdir()
    quotes = sorted({ch for ch in tmpdir if ch in _TYPOGRAPHIC_QUOTES})
    if quotes:
        print(
            f"            NO HOOK WOULD RUN: the directory Cursor writes payloads to ({tmpdir}) "
            f"holds {' '.join(quotes)}, which PowerShell reads as the end of the payload's path"
        )
        return False
    if Path(shell).stem.lower() == "powershell" and not str(Path.cwd()).isascii():
        print(
            "            Windows PowerShell re-encodes the payload's non-ASCII characters, and this "
            "path has some: the hook recovers the edited paths it can. PowerShell 7 (pwsh) on "
            "PATH delivers them as written."
        )
    return True


def _smoke(command: str, cwd: Path) -> tuple[bool, str]:
    """Run an installed hook command once, and read the answer.

    Directly, without a shell: doctor only gets here with the exact command
    init writes, so its arguments are known, and a shell would add nothing but
    its own startup. How Cursor starts it is described apart, without running
    anything (see `_describe_cursor`). The payload carries a BOM because
    Cursor's does, and an unknown conversation, so the hook answers without
    writing any state.
    """
    argv = templates._tokens(command)
    if not argv:
        return False, "the installed command does not split into arguments"
    payload = json.dumps({
        "conversation_id": f"doctor-{uuid.uuid4().hex}",
        "hook_event_name": "afterFileEdit",
        "file_path": str(cwd / "doctor-smoke.txt"),
    }).encode("utf-8-sig")
    environment = {**executables.child_environment(), "CURSOR_PROJECT_DIR": str(cwd)}
    try:
        done = subprocess.run(
            argv, cwd=str(cwd), input=payload,
            capture_output=True, timeout=20, check=False, env=environment,
        )
    except subprocess.TimeoutExpired:
        return False, "no answer within 20s"
    except OSError as exc:
        return False, f"could not run: {exc}"
    out = (done.stdout or b"").decode("utf-8", "replace").strip()
    if done.returncode != 0:
        err = (done.stderr or b"").decode("utf-8", "replace").strip()
        return False, f"exit {done.returncode}: {(err or out)[:200]}"
    try:
        answer = json.loads(out)
    except json.JSONDecodeError:
        return False, f"answered something that is not JSON: {out[:120]!r}"
    if not isinstance(answer, dict):
        return False, f"answered a {type(answer).__name__}, not an object"
    return True, "answered, exit 0"


def _describe_hooks(label: str, target: Path, cwd: Path, disensor_path: str) -> bool:
    """Print what is installed in one hooks.json. False when something is off.

    It never executes a command because a file says so. A project hooks.json
    is written by whoever wrote the repository, and a command that merely
    contains the marker (`evil & rem disensor_cursor.hook`) would pass for
    ours: doctor, of all commands the one you run on a repository you do not
    trust yet, would be the one running it. So the command init would write
    today is rebuilt, and the installed one is run only if it is that exact
    string. Anything else is shown, not run.
    """
    data = _read_json(target)
    ours = _our_entries(data)
    print(f"  hooks ({label}): {target}")
    if not ours:
        print("            none installed")
        return True
    print(f"            {', '.join(sorted(ours))}")
    healthy = True
    versions = {
        m.group(1)
        for entries in ours.values() for e in entries
        for m in [_GENERATED_BY.search(str(e.get("command", "")))] if m
    }
    if versions and versions != {__version__}:
        healthy = False
        print(
            f"            generated by {', '.join(sorted(versions))}, running {__version__}: "
            "re-run `disensor-cursor init` to regenerate them"
        )
    probe = (ours.get("afterFileEdit") or [None])[0]
    if probe:
        installed = str(probe.get("command", ""))
        try:
            # Con los ajustes que el usuario agrego y que init conserva: sin
            # ellos, un hook configurado como dice el README pasaba por ajeno.
            expected = templates.command_for(
                "afterFileEdit", _resolve_python(), disensor_path, None,
                kept=templates.kept_settings(probe),
            )
        except ValueError as exc:
            print(f"            smoke run: NOT RUN ({exc})")
            return False
        if installed != expected:
            healthy = False
            print("            smoke run: NOT RUN (the installed command is not the one init writes)")
            print(f"            installed: {installed}")
            print(f"            init now:  {expected}")
        else:
            ok, detail = _smoke(installed, cwd)
            print(f"            smoke run: {'ok' if ok else 'FAILED'} ({detail})")
            healthy = healthy and ok
    return healthy


def main_doctor(args) -> int:
    print(f"disensor-cursor {__version__}")
    print(f"  python:   {_resolve_python()}")
    path, problem = _resolve_disensor()
    print(f"  disensor: {path or 'NOT FOUND'}")
    if problem:
        print(f"            {problem}")

    healthy = True
    if not state.usable():
        healthy = False
        print(f"  state:    {state.STATE_DIR} is a link or junction: the hook writes nothing there")
        print("            and stays inactive. Make it a real directory to enable it.")

    cwd = Path.cwd()
    healthy = _describe_cursor() and healthy
    healthy = _describe_hooks("user", USER_HOOKS, cwd, path) and healthy
    try:
        healthy = _describe_repository(cwd, path) and healthy
    except gitio.GitUnavailable as exc:
        # Lo mismo que hace el hook: git que no contesta no es un traceback.
        print(f"  repo:     not checked ({exc})")
        healthy = False
    return 0 if (healthy and not problem) else 1


def _describe_repository(cwd: Path, disensor_path: str) -> bool:
    repo = gitio.repo_root(cwd)
    if repo is None:
        print("  repo:     not inside a git repository")
        return True
    print(f"  repo:     {repo}")
    print(f"  adopted:  {'yes' if (repo / 'disensor.config.json').is_file() else 'no (the hook stays quiet)'}")
    base = gitio.default_base(repo)
    print(f"  base:     {base or 'unresolved (origin/main, origin/master)'}")
    healthy = True
    try:
        dirty = gitio.is_dirty(repo)
        print(f"  tree:     {'unknown' if dirty is None else ('dirty' if dirty else 'clean')}")
    except gitio.GitUnavailable as exc:
        # El hook tampoco lo va a poder mirar, y sin eso no avisa nada en este
        # repositorio: es exactamente lo que doctor tiene que decir.
        print(f"  tree:     not checked: {exc}")
        print("            the hook stays quiet in this repository for the same reason")
        healthy = False
    project_hooks = repo / ".cursor" / "hooks.json"
    if project_hooks.exists():
        healthy = _describe_hooks("project", project_hooks, repo, disensor_path) and healthy
    return healthy


def main_status(args) -> int:
    """What the hook decided, including what it could not decide.

    A hook that fails open prints an empty object, which is indistinguishable
    from "nothing to say". Whatever it could not evaluate has to be readable
    somewhere, or fail-open turns into fail-silent.
    """
    directory = state.STATE_DIR
    if not directory.exists():
        print(f"no state yet ({directory})")
        return 0
    sessions = sorted(directory.glob("state-*.json"))
    if not sessions:
        print(f"no sessions recorded ({directory})")
    for path in sessions:
        key = path.stem[len("state-"):]
        data = state.read_state(key)
        edits = state.read_edits(key)
        print(f"{key[:12]}  {data.get('eligibility', 'unknown')}")
        print(f"  worktree: {data.get('worktree', '?')}")
        print(f"  mode:     {data.get('composer_mode', '?')}"
              f"{' (background)' if data.get('is_background_agent') else ''}")
        print(f"  reported: {', '.join(data.get('reported') or []) or 'nothing'}")
        print(f"  edits:    {len(edits)} recorded")
    log = state.log_path()
    if log.exists():
        print(f"\nnot evaluated, and why ({log}):")
        try:
            lines = log.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            lines = []
        for line in lines[-10:]:
            print(f"  {line}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="disensor-cursor",
        description=(
            "Cursor hooks that remind the agent to close the adversarial round. The adapter "
            "runs no models and handles no API keys: it asks the disensor CLI and reports."
        ),
    )
    parser.add_argument("--version", action="version", version=f"disensor-cursor {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Install the hooks.")
    where = init.add_mutually_exclusive_group()
    where.add_argument(
        "--global", dest="global_", action="store_true", default=True,
        help="Install in ~/.cursor/hooks.json (default). Nothing is added to any repository.",
    )
    where.add_argument(
        "--project", action="store_true",
        help="Install in .cursor/ of this repository. Versioned executable code: see the warning.",
    )
    init.add_argument(
        "--force", action="store_true",
        help="Write the hooks even if disensor cannot be found or does not answer.",
    )
    init.set_defaults(func=main_init)

    uninstall = sub.add_parser(
        "uninstall", help="Remove the hooks init added, keeping everything else.",
    )
    uninstall.add_argument(
        "--project", action="store_true", help="Remove them from .cursor/ of this repository.",
    )
    uninstall.add_argument(
        "--purge", action="store_true",
        help=f"Also delete the session state in {state.STATE_DIR}.",
    )
    uninstall.set_defaults(func=main_uninstall)

    doctor = sub.add_parser(
        "doctor", help="What is installed, whether it runs, and whether it can work here.",
    )
    doctor.set_defaults(func=main_doctor)

    status = sub.add_parser("status", help="What the hook decided, and what it could not.")
    status.set_defaults(func=main_status)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    # Lo mismo que hace el hook: los programas no salen del repositorio desde
    # el que se corre el comando. `init` resuelve ahi el disensor que despues
    # corre en cada sesion.
    executables.declare_project(executables.enclosing_repository(Path.cwd()))
    sys.exit(args.func(args))
