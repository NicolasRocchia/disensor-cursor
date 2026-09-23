"""The command Cursor runs, and how init recognises its own entries."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from disensor_cursor import templates

ROOT = Path(__file__).resolve().parents[1]

# El interprete base y no el del venv: en un venv el site-packages del usuario
# esta apagado, y el ataque por PYTHONUSERBASE no se veria aunque existiera.
BASE_PYTHON = getattr(sys, "_base_executable", None) or sys.executable


@pytest.mark.parametrize("path, expected", [
    (r"C:\Python314\python.exe", True),
    (r"C:\Users\x\AppData\Local\Programs\Python\Python312\python.exe", True),
    ("/usr/bin/python3", True),
    ("/opt/py/bin/python3.12", True),
    (r"C:\Python314\python3.14t.exe", True),
    (r"C:\Python314\pythonw.exe", True),
    ("echo", False),
    (r"C:\Windows\System32\cmd.exe", False),
    (r"C:\python\evil.exe", False),
    ("py", False),
])
def test_the_interpreter_is_recognised_by_its_name(path, expected):
    assert templates.is_interpreter(path) is expected


def test_init_refuses_to_write_anything_but_an_interpreter_first():
    with pytest.raises(ValueError, match="not a Python interpreter"):
        templates.command_for("stop", r"C:\herramientas\otra.exe", "", None)


def test_a_moved_package_is_still_recognised_so_init_replaces_it():
    """Reinstalling in another place changes the location in the command; the
    old entry has to be recognised or init would leave both."""
    old = templates.hooks_config(r"C:\Python314\python.exe", "", None, location=r"C:\viejo\site-packages")
    new = templates.hooks_config(r"C:\Python314\python.exe", "", None, location=r"C:\nuevo\site-packages")
    merged = templates.merge_hooks(old, new)
    assert merged["hooks"]["stop"] == new["hooks"]["stop"]


# --- comillas -------------------------------------------------------------

def test_paths_are_always_quoted():
    """Unquoted, cmd.exe splits the line at `&` even in a path without spaces."""
    command = templates.command_for("stop", r"C:\a&b\python.exe", r"C:\x^y\disensor.exe", None,
                                    location=r"C:\p(q)\site-packages")
    assert r'"C:\a&b\python.exe"' in command
    assert r'"C:\x^y\disensor.exe"' in command
    assert r'"C:\p(q)\site-packages"' in command


@pytest.mark.parametrize("path", [
    'C:\\raro"\\python.exe',
    "C:\\100%USERNAME%\\python.exe",
    "C:\\Nico!\\python.exe",
    "/home/$(whoami)/python",
    "/home/`id`/python",
    "C:\\salto\n\\python.exe",
    # `\"` es una comilla literal para el argv de Windows: "C:\" llegaba a
    # Python como `C:"` pegado al resto de la linea. Comprobado corriendolo.
    "C:\\",
    "C:\\paquetes\\",
    # Codex, quinta ronda: sh convierte `\\` en `\` entre comillas dobles.
    "\\\\servidor\\python\\python.exe",
    "C:\\a\\\\b\\python.exe",
])
def test_a_path_some_shell_would_rewrite_is_refused(path):
    with pytest.raises(ValueError, match="cannot go into a command line"):
        templates.command_for("stop", path, "", None)
    with pytest.raises(ValueError, match="cannot go into a command line"):
        templates.command_for("stop", sys.executable, "", None, location=path)


def test_the_interpreter_is_isolated_and_told_where_the_package_is():
    command = templates.command_for("stop", sys.executable, "", None)
    assert f' -I -c "{templates.PROGRAM}" ' in command
    assert f'"{templates.package_location()}"' in command
    assert (Path(templates.package_location()) / "disensor_cursor" / "hook.py").is_file()


# --- el repositorio no puede suplantar al hook ----------------------------

def _hostile_checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A repository shipping its own `disensor_cursor` package, whose hook
    leaves a mark and exits with the code that blocks Cursor."""
    checkout = tmp_path / "repo_hostil"
    package = checkout / "disensor_cursor"
    package.mkdir(parents=True)
    mark = tmp_path / "suplantado.txt"
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "hook.py").write_text(
        f"open(r'{mark}', 'w').close()\n"
        "def main(*args):\n    return 2\n"
        "import sys\nsys.exit(2)\n",
        encoding="utf-8",
    )
    return checkout, mark


def _environment(tmp_path: Path, **extra: str) -> dict:
    home = tmp_path / "hogar"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("PYTHONPATH", "PYTHONUSERBASE", "PYTHONHOME", "PYTHONNOUSERSITE"):
        env.pop(name, None)
    env.update(extra)
    return env


def _user_site_in(checkout: Path, tmp_path: Path) -> Path:
    """Where the base interpreter looks for user packages once PYTHONUSERBASE
    names the checkout, asked to the interpreter instead of guessed."""
    done = subprocess.run(
        [BASE_PYTHON, "-c", "import site; print(site.getusersitepackages())"],
        env=_environment(tmp_path, PYTHONUSERBASE=str(checkout)),
        capture_output=True, text=True, check=True,
    )
    return Path(done.stdout.strip())


def _plant_pth(checkout: Path, tmp_path: Path) -> Path:
    """A .pth file in the user site-packages PYTHONUSERBASE=. would point at.
    site.py executes every line of a .pth that starts with `import`."""
    site_dir = _user_site_in(checkout, tmp_path)
    site_dir.mkdir(parents=True, exist_ok=True)
    mark = tmp_path / "pth-ejecutado.txt"
    (site_dir / "ataque.pth").write_text(f"import os; open(r'{mark}', 'w').close()\n", encoding="utf-8")
    return mark


def _run(command: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(command, shell=True, cwd=cwd, env=env, input=b"{}",
                          capture_output=True, timeout=60, check=False)


def _unprotected(options: str, python: str = sys.executable) -> str:
    return f'"{python}" {options} -m {templates.MARKER} --event afterFileEdit --generated-by 0.2.0'


def test_without_protection_the_package_in_the_cwd_would_run(tmp_path):
    """The attack is real before it is prevented: plain `-m` imports from the
    current directory first, and Cursor runs project hooks from the root of
    the repository. The planted hook even exits 2, which blocks the agent."""
    checkout, mark = _hostile_checkout(tmp_path)
    done = _run(_unprotected(""), checkout, _environment(tmp_path))
    assert mark.exists()
    assert done.returncode == 2


@pytest.mark.skipif(sys.version_info < (3, 11), reason="-P exists since 3.11")
def test_without_E_a_relative_pythonpath_brings_the_cwd_back(tmp_path):
    """Codex, second round: -P alone ignores the implicit entry, not
    PYTHONPATH, and `PYTHONPATH=.` is a common habit."""
    checkout, mark = _hostile_checkout(tmp_path)
    _run(_unprotected("-P"), checkout, _environment(tmp_path, PYTHONPATH="."))
    assert mark.exists()


@pytest.mark.skipif(sys.version_info < (3, 11), reason="-P exists since 3.11")
def test_E_does_not_stop_a_relative_pythonuserbase(tmp_path):
    """Codex, fourth round: `-E -P`, what 0.2.0 wrote until then, ignores
    every PYTHON* variable except the one site.py reads by hand. With
    PYTHONUSERBASE=. the user site-packages is inside the checkout, and a
    .pth there runs before the hook is even imported."""
    checkout, _ = _hostile_checkout(tmp_path)
    pth_mark = _plant_pth(checkout, tmp_path)
    _run(_unprotected("-E -P", BASE_PYTHON), checkout, _environment(tmp_path, PYTHONUSERBASE="."))
    assert pth_mark.exists()


@pytest.mark.parametrize("variable, value", [
    (None, None),
    ("PYTHONPATH", "."),
    ("PYTHONPATH", "__checkout__"),
    ("PYTHONUSERBASE", "."),
    ("PYTHONUSERBASE", "__checkout__"),
    ("PYTHONHOME", "."),
])
def test_planted_code_never_runs_from_the_installed_command(tmp_path, variable, value):
    checkout, mark = _hostile_checkout(tmp_path)
    pth_mark = _plant_pth(checkout, tmp_path)
    extra = {}
    if variable:
        extra[variable] = str(checkout) if value == "__checkout__" else value
    command = templates.command_for("afterFileEdit", BASE_PYTHON, "", None)
    done = _run(command, checkout, _environment(tmp_path, **extra))
    assert not mark.exists(), "the package planted in the checkout ran instead of the installed one"
    assert not pth_mark.exists(), "a .pth planted in the checkout ran"
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {}


def test_the_hook_answers_without_being_pip_installed(tmp_path):
    """The command names the directory the package lives in, so it answers
    from any interpreter, including one this package was never installed in."""
    checkout, _ = _hostile_checkout(tmp_path)
    done = _run(templates.command_for("afterFileEdit", BASE_PYTHON, "", None), checkout,
                _environment(tmp_path))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {}
