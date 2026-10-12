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


# --- identidad de nuestras entradas ---------------------------------------

@pytest.mark.parametrize("command", [
    templates.command_for("stop", r"C:\Python314\python.exe", r"C:\bin\disensor.exe", "origin/main"),
    templates.command_for("afterFileEdit", r"C:\Program Files\Python\python.exe",
                          r"C:\Program Files\disensor\disensor.exe", None),
    templates.command_for("stop", r"C:\Python314\python.exe", "", None,
                          location=r"C:\Users\Nicolás\My Apps\site-packages"),
    # Lo que escribio la 0.1.0: sin -E ni -P y sin comillas donde no habia espacios.
    r"C:\Python314\python.exe -m disensor_cursor.hook --event stop --disensor "
    r"C:\Users\x\disensor.EXE --base origin/main --generated-by 0.1.0",
    r'"C:\Program Files\Python\python.exe" -m disensor_cursor.hook --event afterFileEdit '
    r"--generated-by 0.1.0",
    # Los borradores de 0.2.0, que llegaron a escribirse en maquinas de prueba.
    r'"C:\Python314\python.exe" -E -P -m disensor_cursor.hook --event stop --generated-by 0.2.0',
    '"C:\\Python310\\python.exe" -E -c "' + templates._SAFE_ENTRY_310 + '" --event stop '
    "--generated-by 0.2.0",
    # La de 0.2.x: sin -S, y con el programa que no miraba de donde salia el modulo.
    '"C:\\Python314\\python.exe" -I -c "' + templates._PROGRAM_022 + '" "C:\\x\\site-packages" '
    "--event stop --generated-by 0.2.2",
])
def test_what_init_writes_is_recognised_as_ours(command):
    assert templates.is_ours({"command": command})


@pytest.mark.parametrize("command", [
    "python audit.py --label disensor_cursor.hook",
    "otra-herramienta --stop",
    "evil.exe & rem disensor_cursor.hook --event stop --generated-by 1",
    # Codex, segunda ronda: con comodines alrededor del import, esto pasaba
    # por nuestro aunque solo imprime el texto.
    "\"python\" -c \"print('from disensor_cursor.hook import main')\" --event stop --generated-by 0.2.0",
    r"C:\Python314\python.exe -m disensor_cursor.hookx --event stop --generated-by 0.2.0",
    # Codex, quinta ronda: sin mirar el primer token, esto pasaba por nuestro
    # y init lo borraba, aunque solo imprime.
    "echo -m disensor_cursor.hook --event stop",
    r'"C:\Windows\System32\cmd.exe" -m disensor_cursor.hook --event stop',
    f'"C:\\herramientas\\otra.exe" -I -S -c "{templates.PROGRAM}" "x" --event stop',
])
def test_an_entry_that_only_mentions_the_module_is_somebody_elses(command):
    assert not templates.is_ours({"command": command})


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


@pytest.mark.parametrize("command", [
    r"C:\Python314\python.exe -m disensor_cursor.hook --event stop",
    r"C:\Python314\python.exe -m disensor_cursor.hook --event inventado --generated-by 0.2.0",
    r"C:\Python314\python.exe -m disensor_cursor.hook --otro valor --event stop --generated-by x",
    f'"python" -I -S -c "{templates.PROGRAM}" --event stop --generated-by 0.2.0',
    templates.command_for("stop", r"C:\Python314\python.exe", "", None)
    + " --config a.json --config b.json",
])
def test_this_hook_with_arguments_nobody_expected_is_still_ours(command):
    """The plain `-m` form above is the one that imports from the current
    directory. Left unrecognised, init installed the safe command next to it
    and Cursor kept running both."""
    assert templates.is_ours({"command": command})
    merged = templates.merge_hooks(
        {"version": 1, "hooks": {"stop": [{"command": command}]}},
        templates.hooks_config(r"C:\Python314\python.exe", "", None),
    )
    assert len(merged["hooks"]["stop"]) == 1
    assert " -I -S -c " in merged["hooks"]["stop"][0]["command"]


_OURS = templates.command_for("stop", r"C:\Python314\python.exe", "", None)


@pytest.mark.parametrize("command", [
    # Codex, cuarta ronda: para un split ingenuo `0.2.0&whoami` era un valor,
    # y para cmd.exe son dos comandos. Reconocida como nuestra, init y
    # uninstall borraban el segundo junto con la entrada.
    _OURS.replace(f"--generated-by {templates.__version__}", "--generated-by 0.2.0&whoami"),
    _OURS + " & whoami",
    _OURS + " | tee copia.txt",
    _OURS + "; rm -rf x",
    _OURS + " > salida.txt",
    _OURS + " ^& whoami",
    _OURS + ' --config "$(whoami).json"',
    _OURS + ' --config "%USERNAME%.json"',
    _OURS + ' --config "politica`whoami`.json"',
    _OURS + ' --config "a!b.json"',
    _OURS + ' --config "sin cerrar',
    # Los que solo el chequeo de caracteres atrapa: la estructura esta bien,
    # una bandera conocida con un valor, y el valor es media linea de cmd.exe.
    _OURS + " --directory evidencia&whoami",
    _OURS + " --config politica.json|more",
    # La 0.1.0 escribia el interprete sin comillas cuando no tenia espacios.
    r"C:\Py&whoami\python.exe -m disensor_cursor.hook --event stop --generated-by 0.1.0",
    # Para sh la comilla escapada no abre nada, y el `&` queda afuera.
    _OURS + r' --config a\" & whoami ; echo \"',
])
def test_a_command_that_does_more_than_ours_is_not_ours(command):
    assert not templates.is_ours({"command": command})


@pytest.mark.parametrize("location", ["", ".", "--event", r"C:\otra\parte"])
def test_our_program_with_a_broken_directory_is_ours_so_init_replaces_it(location):
    """`""` or `.` there would import the hook from the current directory.
    Not recognised, init would leave that entry installed next to its own."""
    command = (f'"C:\\Python314\\python.exe" -I -S -c "{templates.PROGRAM}" "{location}" '
               f"--event stop --generated-by 0.2.0")
    assert templates.is_ours({"command": command})
    merged = templates.merge_hooks(
        {"version": 1, "hooks": {"stop": [{"command": command}]}},
        templates.hooks_config(r"C:\Python314\python.exe", "", None),
    )
    assert [e["command"] for e in merged["hooks"]["stop"]] == [
        templates.command_for("stop", r"C:\Python314\python.exe", "", None)
    ]


def test_only_objects_can_be_ours():
    assert not templates.is_ours("disensor_cursor.hook")
    assert not templates.is_ours(None)


def test_a_typographic_apostrophe_goes_through_and_stays_ours():
    """Codex, fourth round on doctor: the single typographic quotes do not
    close a double-quoted string in PowerShell, and refusing them left out a
    user folder like O'Brien with its curly apostrophe, and stopped init and
    uninstall from recognising a hook already installed there."""
    python = "C:\\Users\\O\u2019Brien\\python.exe"
    command = templates.command_for("stop", python, "", None)
    assert templates._tokens(command)[0] == python
    old = templates.hooks_config(python, "", None, location=r"C:\viejo\site-packages")
    new = templates.hooks_config(python, "", None, location=r"C:\nuevo\site-packages")
    assert templates.merge_hooks(old, new)["hooks"]["stop"] == new["hooks"]["stop"]


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
    assert templates.is_ours({"command": command})


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
    # Codex, tercera ronda sobre doctor: PowerShell tambien cierra una cadena
    # doble en las comillas dobles tipograficas, y el parser de Windows
    # PowerShell 5.1 rechazaba el comando que init escribia con una de estas.
    "C:\\\u201ccitas\u201d\\python.exe",
    "C:\\baja\u201e\\python.exe",
])
def test_a_path_some_shell_would_rewrite_is_refused(path):
    with pytest.raises(ValueError, match="cannot go into a command line"):
        templates.command_for("stop", path, "", None)
    with pytest.raises(ValueError, match="cannot go into a command line"):
        templates.command_for("stop", sys.executable, "", None, location=path)


def test_the_interpreter_is_isolated_and_told_where_the_package_is():
    command = templates.command_for("stop", sys.executable, "", None)
    assert f' -I -S -c "{templates.PROGRAM}" ' in command
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


# --- de donde sale el paquete ---------------------------------------------

def _venv_with_decoy(tmp_path: Path) -> tuple[str, Path, Path]:
    """A virtualenv holding another copy of the package in its site-packages,
    which leaves one mark when it is imported and another when its hook runs.

    Codex, round on the plugin plan: `-I` keeps site-packages, ahead of the
    directory the command appends, so an older copy installed there ran
    instead of the one init found.
    """
    import venv

    folder = tmp_path / "entorno"
    venv.create(folder, with_pip=False)
    python = str(folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    done = subprocess.run(
        [python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        env=_environment(tmp_path), capture_output=True, text=True, check=True,
    )
    package = Path(done.stdout.strip()) / "disensor_cursor"
    package.mkdir(parents=True)
    imported, ran = tmp_path / "senuelo-importado.txt", tmp_path / "senuelo-corrido.txt"
    (package / "__init__.py").write_text(f"open(r'{imported}', 'w').close()\n", encoding="utf-8")
    (package / "hook.py").write_text(
        f"def main(*args):\n    open(r'{ran}', 'w').close()\n    print('{{\"decoy\": 1}}')\n    return 0\n",
        encoding="utf-8",
    )
    return python, imported, ran


def test_without_S_a_copy_in_site_packages_runs_instead(tmp_path):
    """The problem is real: the 0.2.x command, `-I` and the program that
    appended the directory, ran the copy site-packages had."""
    python, _, ran = _venv_with_decoy(tmp_path)
    done = subprocess.run(
        [python, "-I", "-c", templates._PROGRAM_022, templates.package_location(),
         "--event", "afterFileEdit"],
        env=_environment(tmp_path), input=b"{}", capture_output=True, timeout=60, check=False,
    )
    assert ran.exists(), done.stderr
    assert json.loads(done.stdout) == {"decoy": 1}


def test_the_installed_command_never_imports_a_copy_from_site_packages(tmp_path):
    python, imported, ran = _venv_with_decoy(tmp_path)
    done = _run(templates.command_for("afterFileEdit", python, "", None), tmp_path,
                _environment(tmp_path))
    assert not imported.exists(), "-S should leave site-packages out"
    assert not ran.exists()
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {}


def test_the_program_does_not_run_a_package_from_anywhere_else(tmp_path):
    """The second layer, for a runtime whose `._pth` file puts its own paths
    first even with -S: the program looks at where the module came from and
    answers `{}` without running it."""
    python, imported, ran = _venv_with_decoy(tmp_path)
    done = subprocess.run(
        [python, "-I", "-c", templates.PROGRAM, templates.package_location(),
         "--event", "afterFileEdit"],
        env=_environment(tmp_path), input=b"{}", capture_output=True, timeout=60, check=False,
    )
    assert imported.exists(), "without -S the decoy comes first, as it would with a ._pth"
    assert not ran.exists()
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {}


@pytest.mark.parametrize("command", [
    # Lo que el README le dice al usuario que agregue, con y sin comillas, en
    # cualquier orden, incluso despues de --generated-by.
    _OURS + ' --config "policy files/p.json" --directory "review evidence"',
    _OURS + " --directory evidencia",
    r'"C:\Python314\python.exe" -E -P -m disensor_cursor.hook --config politica.json --event stop '
    r"--generated-by 0.2.0",
])
def test_a_hook_the_user_configured_is_still_ours(command):
    """Codex, third round: the README says to add --config or --directory, and
    once added the hook stopped being recognised: doctor said nothing was
    installed and init installed a second copy."""
    assert templates.is_ours({"command": command})


def _reinstalled(previous: str) -> str:
    merged = templates.merge_hooks(
        {"version": 1, "hooks": {"stop": [{"command": previous}]}},
        templates.hooks_config(r"C:\Python314\python.exe", "", None),
    )
    [entry] = merged["hooks"]["stop"]
    return entry["command"]


def test_reinstalling_keeps_the_settings_the_readme_says_to_add():
    """doctor tells you to re-run init after an upgrade, and init used to drop
    --config and --directory without a word: the hook then asked disensor with
    a policy that was not the repository's."""
    previous = _OURS + ' --config "policy files/p.json" --directory evidencia'
    command = _reinstalled(previous)
    assert command.endswith(' --config "policy files/p.json" --directory "evidencia"')
    assert templates.kept_settings({"command": command}) == [
        ("--config", "policy files/p.json"), ("--directory", "evidencia"),
    ]


def test_only_those_settings_are_kept_and_the_last_one_wins():
    previous = (_OURS + " --config a.json --otro valor --config b.json --base release"
                + " --directory --config")
    command = _reinstalled(previous)
    assert templates.kept_settings({"command": command}) == [("--config", "b.json")]
    assert "--otro" not in command and "release" not in command


def test_an_entry_without_settings_is_regenerated_as_init_writes_it():
    assert _reinstalled(_OURS) == _OURS


def test_a_setting_that_cannot_be_written_back_stops_init_and_says_which():
    """Dropping it would be the silent loss this carry-over exists to avoid.
    Unquoted, since a quoted value ending in a backslash holds `\\"` and is
    not recognised as ours at all."""
    with pytest.raises(ValueError, match="stop hook carries a setting init cannot keep"):
        _reinstalled(_OURS + " --directory evidencia\\")


def test_what_init_writes_now_is_recognised_as_is():
    for event in templates.EVENTS:
        for base in (None, "origin/main"):
            command = templates.command_for(event, r"C:\Program Files\Py\python.exe",
                                            r"C:\Program Files\d\disensor.exe", base)
            assert templates.is_ours({"command": command}), command
