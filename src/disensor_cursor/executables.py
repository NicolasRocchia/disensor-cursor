"""Where the programs this adapter starts come from, and what they inherit.

On Windows, a bare name given to CreateProcess is searched for in the current
directory of the process that starts it before PATH is consulted. Cursor runs
project hooks from the repository root, so a `git.exe` or a `disensor.cmd`
committed to a repository would be the program that runs. `shutil.which` does
not settle it: it looks at the current directory first too, unless
`NoDefaultCurrentDirectoryInExePath` is set, which it is not by default.

Protecting this process is not enough either: every child inherits the
environment. disensor calls git by bare name, and its launcher starts Python
without -E, so a `.` in PATH or a PYTHONPATH pointing at the checkout would
run the repository's code inside the check, after the hook's own guards.

So programs are resolved from absolute PATH entries only, never from one that
lies inside the project, and children get a sanitized environment: the same
PATH filtering, no PYTHONPATH, no Python location variable that points into
the checkout, `PYTHONSAFEPATH`, `NoDefaultCurrentDirectoryInExePath`, and git
told not to run the fsmonitor command or to fetch missing objects.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Lo que ninguna variable de entorno le puede agregar a un hijo: codigo que se
# importa o se ejecuta desde el repositorio antes que lo instalado.
_STRIPPED = {"PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT"}

# Variables que mueven de donde lee Python: la biblioteca estandar, el
# site-packages del usuario (y sus .pth, que se ejecutan) y los .pyc. El
# usuario puede tenerlas por buenas razones, y disensor puede estar instalado
# justamente ahi, asi que no se borran siempre: se borran cuando son
# relativas, que es decir el directorio actual, o cuando apuntan adentro del
# proyecto.
_RELOCATING = ("PYTHONHOME", "PYTHONUSERBASE", "PYTHONPYCACHEPREFIX")

# Lo que se le agrega a la configuracion de git de cada hijo. `core.fsmonitor`
# vacio lo apaga en todas las versiones: con un valor, git lo ejecuta en cada
# `status`, y la configuracion local del repositorio lo puede definir. Va por
# GIT_CONFIG_PARAMETERS, el canal de `git -c`, que tiene la mayor precedencia
# y que git les pasa a sus propios hijos, submodulos incluidos.
_GIT_OVERRIDES = "'core.fsmonitor='"

# Las carpetas que el hook declaro como parte del proyecto durante esta
# corrida, ademas de las que Cursor pone en el entorno.
_declared: list[Path] = []


def _extensions(name: str) -> list[str]:
    if sys.platform != "win32" or Path(name).suffix:
        return [""]
    pathext = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [ext.lower() for ext in pathext.split(";") if ext]


def _resolved(raw) -> Path | None:
    if not raw or not isinstance(raw, (str, os.PathLike)):
        return None
    try:
        return Path(raw).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def declare_project(*paths) -> None:
    """Directories that belong to the project for the rest of this run.

    Cursor usually says which project it is working on through
    CURSOR_PROJECT_DIR, and those are always excluded. When it does not, the
    hook still finds the project, through the payload or the current
    directory, and it has to declare it here before running git: otherwise a
    PATH entry inside that project was a place a repository could plant the
    `git.exe` the hook ran.
    """
    for raw in paths:
        resolved = _resolved(raw)
        if resolved is not None and resolved not in _declared:
            _declared.append(resolved)


def forget_projects() -> None:
    _declared.clear()


def enclosing_repository(path) -> Path | None:
    """The nearest directory at or above `path` holding a `.git`, found
    without running git.

    Declaring only the folder Cursor opened was not enough when that folder is
    a subdirectory: the first git call, the one that finds the repository
    root, ran before the root was known, and a PATH entry elsewhere in the
    same repository could supply that git. Git itself stops at the nearest
    `.git` too, so this is the root it reports, unless the repository's own
    configuration moves the working tree with `core.worktree`. That case is
    not chased: telling it apart means parsing git's configuration the way
    git does, and a text reading of it switched the hook off in ordinary
    repositories. The README states it as a limit.
    """
    start = _resolved(path)
    if start is None:
        return None
    for directory in (start, *start.parents):
        try:
            if (directory / ".git").exists():
                return directory
        except OSError:
            return None
    return None


def inside_project(path) -> bool:
    """Whether a path lies inside the project, as PATH entries are judged."""
    return _points_inside(str(path), _project_roots())


def _project_roots() -> list[Path]:
    """The project, which nothing may run from."""
    roots = []
    for raw in (os.environ.get("CURSOR_PROJECT_DIR"), os.environ.get("CLAUDE_PROJECT_DIR")):
        resolved = _resolved(raw)
        if resolved is not None:
            roots.append(resolved)
    return roots + [p for p in _declared if p not in roots]


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _points_inside(directory: str, roots: list[Path]) -> bool:
    """Whether a directory is relative, or absolute inside one of the roots."""
    directory = directory.strip().strip('"')
    if not directory or not os.path.isabs(directory):
        return True
    resolved = _resolved(directory)
    return resolved is None or any(_within(resolved, root) for root in roots)


def safe_path_entries(exclude=()) -> list[str]:
    """PATH without empty, relative or in-project entries.

    A relative entry, `.` included, resolves against the current directory,
    which is the repository. An absolute entry inside the project is the same
    thing written differently. `exclude` adds directories for one lookup.
    """
    roots = _project_roots() + [r for r in map(_resolved, exclude) if r is not None]
    return [
        directory.strip().strip('"')
        for directory in os.environ.get("PATH", "").split(os.pathsep)
        if not _points_inside(directory, roots)
    ]


def find(name: str, exclude=()) -> str | None:
    """Absolute path of `name` on the safe part of PATH, or None."""
    for directory in safe_path_entries(exclude):
        for extension in _extensions(name):
            candidate = os.path.join(directory, name + extension)
            if os.path.isfile(candidate) and (sys.platform == "win32" or os.access(candidate, os.X_OK)):
                return os.path.abspath(candidate)
    return None


def child_environment() -> dict[str, str]:
    """The environment for a child, with nothing that points back at the repo."""
    roots = _project_roots()
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        name = key.upper()
        if name in _STRIPPED:
            continue
        if name in _RELOCATING and any(
            _points_inside(part, roots) for part in value.split(os.pathsep)
        ):
            continue
        env[key] = value
    env["PATH"] = os.pathsep.join(safe_path_entries())
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    # El equivalente de -P para el Python de disensor (3.11 y posteriores): no
    # antepone el directorio del script ni el actual a sys.path.
    env["PYTHONSAFEPATH"] = "1"
    # Al final, para que gane sobre lo que el usuario ya tuviera ahi.
    previous = env.get("GIT_CONFIG_PARAMETERS", "").strip()
    env["GIT_CONFIG_PARAMETERS"] = f"{previous} {_GIT_OVERRIDES}".strip()
    # Sin red: en un clon parcial, leer un objeto que falta dispara una
    # descarga, y la hace el transporte que diga la configuracion local.
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env
