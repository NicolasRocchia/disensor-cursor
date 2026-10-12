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

# El nombre del modulo que Cursor ejecuta. Aparece en cada comando nuestro,
# pero NO alcanza para reconocerlo: una entrada ajena puede contener este texto
# (`python audit.py --label disensor_cursor.hook`) y uninstall la borraba.
MARKER = "disensor_cursor.hook"

EVENTS = {
    "sessionStart": {"timeout": 5},
    "afterFileEdit": {"timeout": 5},
    # `loop_limit: 1` porque Cursor ya implementa el corte de la continuacion
    # automatica: no hace falta programarlo. El corte por motivo y commit que
    # lleva el hook es otra cosa y resuelve otro problema.
    "stop": {"timeout": 15, "loop_limit": 1},
}

# El programa que corre el interprete. Es siempre el mismo texto: la carpeta
# del paquete llega como primer argumento y no dentro del programa, asi que
# ninguna ruta tiene que escaparse como literal de Python. Antes de correr el
# hook mira de donde salio el modulo: si no es de esa carpeta, contesta `{}` y
# sale 0 sin ejecutarlo (ver `_entry`).
PROGRAM = (
    "import os, sys; sys.path.append(sys.argv[1]); "
    f"import {MARKER} as h; "
    "here = os.path.normcase(os.path.realpath(sys.argv[1])); "
    "sys.exit(h.main(sys.argv[2:]) if os.path.normcase(os.path.dirname(os.path.dirname("
    "os.path.realpath(h.__file__)))) == here else print({}))"
)

# El programa de 0.2.x, que se sigue reconociendo para reemplazarlo y
# desinstalarlo.
_PROGRAM_022 = (
    "import sys; sys.path.append(sys.argv[1]); "
    f"from {MARKER} import main; sys.exit(main(sys.argv[2:]))"
)

# Lo que algun shell expande incluso entre comillas dobles: `%` y `!` en
# cmd.exe, `$` y el backtick en sh y en PowerShell. La comilla doble cierra, y
# PowerShell, que es la shell con que Cursor corre los hooks en Windows, cierra
# una cadena entre comillas dobles tambien con las dobles tipograficas: una
# ruta con una de esas la cortaba. Las simples tipograficas no cierran una
# cadena doble, y rechazarlas dejaba afuera rutas validas como `O'Brien` con
# su apostrofo curvo.
_EXPANDED = frozenset('"%!$`' "\u201c\u201d\u201e")

# Lo que puede ir sin comillas y significa lo mismo en cmd.exe y en sh. Ni `,`
# ni `=`: cmd.exe los trata como separadores en algunos lugares, y sh lee un
# `X=y` al principio como una asignacion.
_PLAIN = frozenset("_.:/\\@+~-")


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

    What init writes first is `sys.executable`, and nothing else: an entry
    whose first word is `echo` or any other program is somebody else's, even
    when the rest of its line looks like ours.
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

    `-S` drops the rest of site-packages too. `-I` alone keeps the system's,
    ahead of the appended directory, and an older copy installed there ran
    instead of a newer `--user` install. Without site the package can only
    come from the directory the command names, save for a runtime whose
    `._pth` file adds its own paths and even loads site regardless of the
    flag. That is why the program checks where the module actually came from
    before running it, and answers `{}` when it is somewhere else.
    """
    interpreter = _quote(python)
    if not is_interpreter(python):
        raise ValueError(f"not a Python interpreter, by its name: {python}")
    return [interpreter, "-I", "-S", "-c", f'"{PROGRAM}"', _quote(location)]


def _with_settings(command: str, kept) -> str:
    return " ".join([command, *(f"{flag} {_quote(value)}" for flag, value in kept)])


def command_for(
    event: str, python: str, disensor: str, base: str | None,
    location: str | None = None, kept=(),
) -> str:
    """The command init writes for one event.

    `kept` are the settings the user added to a previous entry (see
    `kept_settings`), carried to the end of the new one.
    """
    parts = [*_entry(python, location or package_location()), "--event", event]
    if disensor:
        parts += ["--disensor", _quote(disensor)]
    if base:
        parts += ["--base", base]
    parts += ["--generated-by", __version__]
    return _with_settings(" ".join(parts), kept)


# Como arranca el interprete en cada forma que escribio alguna version: la de
# hoy, con la carpeta del paquete como argumento (`None` en el patron), la de
# 0.2.x sin -S, las de los borradores de 0.2.0 y la de 0.1.0, sin -E ni -P.
# Las formas -c exigen el programa EXACTO: con comodines alrededor del import,
# una entrada ajena que solo imprimia ese texto pasaba por nuestra y uninstall
# la borraba.
_SAFE_ENTRY_310 = (
    "import sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
    f"from {MARKER} import main; sys.exit(main())"
)
_ENTRIES = (
    ("-I", "-S", "-c", PROGRAM, None),
    ("-I", "-c", _PROGRAM_022, None),
    ("-E", "-P", "-m", MARKER),
    ("-P", "-m", MARKER),
    ("-E", "-m", MARKER),
    ("-m", MARKER),
    ("-E", "-c", _SAFE_ENTRY_310),
    ("-c", _SAFE_ENTRY_310),
)

# Lo que el README le dice al usuario que agregue a mano, y que init conserva
# al regenerar: descartarlo en silencio hacia que el hook le preguntara a
# disensor con otra politica que la del repositorio.
KEPT_SETTINGS = ("--config", "--directory")


def _tokens(command: str) -> list[str] | None:
    """Split a command line on spaces outside double quotes, quotes removed.

    None for anything init never writes: a quote left open, a character
    outside quotes that some shell treats as syntax (`&`, `|`, `;`, `(`...),
    or one inside quotes that some shell still expands. The first case matters
    most. `--generated-by 0.2.0&whoami` is a single token to a naive split and
    two commands to cmd.exe, and recognising the line as ours meant init and
    uninstall deleted the second command along with ours.
    """
    if '\\"' in command:
        # Una comilla despues de una barra invertida es literal para sh y para
        # el argv de Windows, y para cmd.exe abre o cierra. Con eso
        # `a\" & whoami ; echo \"` era un comando aca y dos para sh. init
        # nunca la escribe: ningun valor lleva comillas ni termina en barra.
        return None
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    started = False
    for ch in command:
        if ch == '"':
            quoted = not quoted
            started = True
        elif ch == " " and not quoted:
            if started:
                tokens.append("".join(current))
            current, started = [], False
        elif quoted and (ch in _EXPANDED or not ch.isprintable()):
            return None
        elif not quoted and not (ch.isalnum() or ch in _PLAIN):
            return None
        else:
            current.append(ch)
            started = True
    if quoted:
        return None
    if started:
        tokens.append("".join(current))
    return tokens


def _after_entry(rest: list[str]) -> list[str] | None:
    """What follows the interpreter's own options, or None if no known entry fits.

    The package directory matches any token, even an empty or a relative one.
    That is deliberate: an entry that runs this program with a broken
    directory runs the hook, and one with `""` or `.` there would import it
    from the current directory. Recognising it as ours is what gets it
    replaced by init and removed by uninstall; not recognising it would leave
    it installed next to the right one.
    """
    for shape in _ENTRIES:
        if len(rest) >= len(shape) and all(
            want is None or want == got for want, got in zip(shape, rest)
        ):
            return rest[len(shape):]
    return None


def _arguments(entry) -> list[str] | None:
    """What an entry of ours passes to the hook, or None if it is not ours."""
    if not isinstance(entry, dict):
        return None
    tokens = _tokens(str(entry.get("command", "")))
    if not tokens or len(tokens) < 2 or not is_interpreter(tokens[0]):
        return None
    return _after_entry(tokens[1:])


def is_ours(entry) -> bool:
    """Whether a hooks.json entry is a single command that runs this hook.

    Three conditions, and only three. The line is one command: nothing outside
    quotes that a shell reads as syntax, nothing inside them that a shell
    expands. Its first word is a Python interpreter, by name: `echo -m
    disensor_cursor.hook ...` only prints. And that interpreter starts one of
    the entries some version of init wrote, recognised token by token, so an
    entry that merely mentions the module name belongs to somebody else and is
    kept.

    What follows is not checked, on purpose. An entry that runs this hook with
    arguments nobody expected, or through the old `-m` form that imports from
    the current directory, is still this hook, and recognising it is what gets
    it replaced by init and removed by uninstall. Checking the arguments too
    left exactly those entries installed next to the right one.
    """
    return _arguments(entry) is not None


def kept_settings(entry) -> list[tuple[str, str]]:
    """The `--config` and `--directory` an entry of ours carries.

    Read the way the hook reads its flags: a flag followed by a value that is
    not itself a flag, the last occurrence winning.
    """
    rest = _arguments(entry) or []
    found: dict[str, str] = {}
    for flag, value in zip(rest, rest[1:]):
        if flag in KEPT_SETTINGS and not value.startswith("--"):
            found[flag] = value
    return list(found.items())


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
        previous = previous if isinstance(previous, list) else []
        kept = next((k for k in map(kept_settings, previous) if k), [])
        others = [e for e in previous if not is_ours(e)]
        try:
            fresh = [{**e, "command": _with_settings(e["command"], kept)} for e in entries]
        except ValueError as exc:
            raise ValueError(f"the installed {event} hook carries a setting init cannot keep: {exc}") from exc
        hooks[event] = others + fresh
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
