"""What `disensor-cursor init` writes.

The rule file does not copy disensor's runbook. It points at `disensor guide`,
which is the packaged source of that text. A copy in a third repository would
freeze at whatever version installed it, and disensor's schema is explicitly
not frozen yet, so the copy would go stale silently.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import __version__

# El comando lleva esta marca para que `init` pueda reconocer SUS entradas
# dentro de un hooks.json que ya tenga hooks de otra gente, y reemplazar solo
# las propias.
MARKER = "disensor_cursor.hook"

EVENTS = {
    "sessionStart": {"timeout": 5},
    "afterFileEdit": {"timeout": 5},
    # `loop_limit: 1` porque Cursor ya implementa el corte de la continuacion
    # automatica: no hace falta programarlo. El corte por motivo que lleva el
    # hook es otra cosa y resuelve otro problema.
    "stop": {"timeout": 15, "loop_limit": 1},
}


# El programa que corre el interprete. Es siempre el mismo texto: la carpeta
# del paquete llega como primer argumento y no dentro del programa, asi que
# ninguna ruta tiene que escaparse como literal de Python.
PROGRAM = (
    "import sys; sys.path.append(sys.argv[1]); "
    f"from {MARKER} import main; sys.exit(main(sys.argv[2:]))"
)

# Lo que algun shell expande incluso entre comillas dobles: `%` y `!` en
# cmd.exe, `$` y el backtick en sh y en PowerShell. La comilla doble cierra.
_EXPANDED = frozenset('"%!$`')


def package_location() -> str:
    """The directory the running `disensor_cursor` package was imported from."""
    return str(Path(__file__).resolve().parent.parent)


def _quote(text: str) -> str:
    """Always quoted: a path without spaces can still hold `&` or `^`.

    Unquoted, cmd.exe splits the line at `&` and runs whatever follows. Quoted,
    some characters still mean something to some shell, and a path holding one
    cannot be written so that every shell reads it back as it is.
    """
    bad = sorted({ch for ch in text if ch in _EXPANDED or not ch.isprintable()})
    if bad:
        shown = " ".join(repr(ch) for ch in bad)
        raise ValueError(f"a path holding {shown} cannot go into a command line safely: {text}")
    if text.endswith("\\"):
        # Para el runtime de C que arma el argv en Windows, `\"` es una
        # comilla literal: "C:\" llega como `C:"` pegado a todo lo que sigue.
        raise ValueError(f"a path ending in a backslash cannot go into a command line safely: {text}")
    if "\\\\" in text:
        # sh convierte `\\` en `\` entre comillas dobles: una ruta UNC u otra
        # con la barra doblada le llegaba al hook con otro valor.
        raise ValueError(f"a path holding a doubled backslash cannot go into a command line safely: {text}")
    return f'"{text}"'


def is_interpreter(path: str) -> bool:
    """Whether a program path names a Python interpreter.

    What init writes first is `sys.executable`, and nothing else: it refuses
    to put any other program there.
    """
    name = re.split(r"[\\/]", path)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return re.fullmatch(r"python[0-9a-z._-]*", name) is not None


def _entry(python: str, location: str) -> list[str]:
    """How the interpreter reaches the hook without importing from the repository.

    Cursor runs project hooks from the repository root, and Python has three
    ways of putting a directory on sys.path on its own that can point there:

    - `-m` and `-c` put the current directory first. A repository holding its
      own `disensor_cursor/` package would run instead of the installed one,
      before any of the hook's protections.
    - PYTHONPATH, when it holds a relative entry such as the common `.`.
    - PYTHONUSERBASE, when relative: the user site-packages moves into the
      checkout, and site.py executes the `import` lines of any .pth file it
      finds there. `-E` does not stop this one, because site.py reads that
      variable directly.

    `-I` switches the three off. It also drops the user site-packages, where
    `pip install --user` puts this package, so the command carries the
    directory init found the package in, absolute, and the program adds
    exactly that directory. Appended and not prepended: in front, a
    site-packages directory would take precedence over the standard library.
    """
    interpreter = _quote(python)
    if not is_interpreter(python):
        raise ValueError(f"not a Python interpreter, by its name: {python}")
    return [interpreter, "-I", "-c", f'"{PROGRAM}"', _quote(location)]


def command_for(
    event: str, python: str, disensor: str, base: str | None, location: str | None = None,
) -> str:
    parts = [*_entry(python, location or package_location()), "--event", event]
    if disensor:
        parts += ["--disensor", _quote(disensor)]
    if base:
        parts += ["--base", base]
    parts += ["--generated-by", __version__]
    return " ".join(parts)


def hooks_config(
    python: str, disensor: str, base: str | None = None, location: str | None = None,
) -> dict:
    hooks = {}
    for event, options in EVENTS.items():
        entry = {
            "command": command_for(
                event, python, disensor, base if event == "stop" else None, location,
            ),
            # Advisory: una falla del hook nunca puede frenar al agente. El
            # ejecutor ademas sale siempre con 0, porque en Cursor el 2 bloquea
            # aunque failClosed sea false.
            "failClosed": False,
        }
        entry.update(options)
        hooks[event] = [entry]
    return {"version": 1, "hooks": hooks}


def merge_hooks(existing: dict, generated: dict) -> dict:
    """Ours replace ours; everything else is left exactly as it was.

    Overwriting the file would delete hooks somebody else installed, and
    appending on every run would duplicate ours. Neither is acceptable in a
    file that lives in the user's home and that other tools also write.
    """
    result = dict(existing) if isinstance(existing, dict) else {}
    result["version"] = result.get("version", 1)
    hooks = dict(result.get("hooks") or {})
    for event, entries in generated["hooks"].items():
        previous = hooks.get(event)
        others = [
            e for e in previous
            if isinstance(e, dict) and MARKER not in str(e.get("command", ""))
        ] if isinstance(previous, list) else []
        hooks[event] = others + entries
    result["hooks"] = hooks
    return result


RULE = f"""---
alwaysApply: true
---

# Adversarial review before closing (disensor)

This project uses disensor: before a change merges, it is attacked by a model
from a different family than the one that wrote it, and whatever the round left
open is declared as residue.

A hook watches for this. When you finish a turn it may add an automatic notice
about the state of the round. That notice comes from tooling, not from the
user: it never authorises you to commit or to run a review on your own.

What you need to know:

- `disensor guide` prints the whole procedure and the filling guide. Read it
  from there instead of guessing; it travels with the installed version.
- A diff round reviews commits that already exist, so the tree has to be clean
  before one can run.
- `disensor round` sends the material under review to an external reviewer.
  That is the owner's decision. Ask before running it.
- Never paste material between models by hand and call it a round.
- Check every finding against the actual code before accepting it. The reviewer
  is decorrelated, not right.

<!-- disensor-cursor v{__version__} -->
"""
