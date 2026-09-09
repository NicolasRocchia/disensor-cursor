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
import shutil
import sys
from pathlib import Path

from . import __version__, disensor, gitio, state, templates

USER_HOOKS = Path.home() / ".cursor" / "hooks.json"


def _resolve_python() -> str:
    """The interpreter running this install, absolute.

    Not `shutil.which("disensor-cursor")`: on Windows with Python 3.10 and
    3.11 that search looks at the current directory first, and the hook runs
    with the repository as its cwd, so a repository shipping a same-named
    script would choose what executes. `sys.executable` is already absolute and
    is the interpreter that has this package importable.
    """
    return os.path.abspath(sys.executable)


def _resolve_disensor() -> tuple[str, str]:
    found = shutil.which("disensor")
    if not found:
        return "", "disensor is not on PATH. Install it first: pip install disensor"
    path = os.path.abspath(found)
    ok, detail = disensor.probe(Path(path))
    if not ok:
        return path, f"found at {path}, but it does not answer as expected: {detail}"
    return path, ""


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"{path} is a symlink: refusing to write through it")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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

    generated = templates.hooks_config(python, disensor_path, base)
    merged = templates.merge_hooks(_read_json(target), generated)
    _write_json(target, merged)
    report.append(f"wrote {target}")

    if args.project and repo is not None:
        rule = repo / ".cursor" / "rules" / "disensor.mdc"
        rule.parent.mkdir(parents=True, exist_ok=True)
        rule.write_text(templates.RULE, encoding="utf-8")
        report.append(f"wrote {rule.relative_to(repo)}")
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


def main_doctor(args) -> int:
    print(f"disensor-cursor {__version__}")
    print(f"  python:   {_resolve_python()}")
    path, problem = _resolve_disensor()
    print(f"  disensor: {path or 'NOT FOUND'}")
    if problem:
        print(f"            {problem}")

    for label, target in (("user", USER_HOOKS),):
        data = _read_json(target)
        events = [
            event for event, entries in (data.get("hooks") or {}).items()
            if isinstance(entries, list)
            and any(templates.MARKER in str(e.get("command", "")) for e in entries if isinstance(e, dict))
        ]
        print(f"  hooks ({label}): {target}")
        print(f"            {', '.join(sorted(events)) if events else 'none installed'}")

    repo = gitio.repo_root(Path.cwd())
    if repo is None:
        print("  repo:     not inside a git repository")
        return 0
    print(f"  repo:     {repo}")
    print(f"  adopted:  {'yes' if (repo / 'disensor.config.json').is_file() else 'no (the hook stays quiet)'}")
    base = gitio.default_base(repo)
    print(f"  base:     {base or 'unresolved (origin/main, origin/master)'}")
    dirty = gitio.is_dirty(repo)
    print(f"  tree:     {'unknown' if dirty is None else ('dirty' if dirty else 'clean')}")
    project_hooks = repo / ".cursor" / "hooks.json"
    if project_hooks.exists():
        print(f"  hooks (project): {project_hooks}")
    return 0 if not problem else 1


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
        print(f"  edits:    {len(edits)} file(s) touched")
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

    doctor = sub.add_parser("doctor", help="What is installed and whether it can work here.")
    doctor.set_defaults(func=main_doctor)

    status = sub.add_parser("status", help="What the hook decided, and what it could not.")
    status.set_defaults(func=main_status)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args))
