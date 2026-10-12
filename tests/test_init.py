"""`disensor-cursor init`: where it writes, and what it refuses to destroy."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from disensor_cursor import cli, templates


REAL_PACKAGE_PROBLEM = cli._package_problem


@pytest.fixture(autouse=True)
def installed_outside_a_checkout(monkeypatch):
    """The suite imports the package from this checkout, which init refuses on
    purpose (see the tests of `--allow-checkout`). Every other test installs
    the way a user would."""
    monkeypatch.setattr(cli, "_package_problem", lambda: "")


def run(argv, monkeypatch, tmp_path, *, disensor_ok=True):
    monkeypatch.setattr(cli, "USER_HOOKS", tmp_path / "casa" / ".cursor" / "hooks.json")
    monkeypatch.setattr(cli, "CURSOR_PLUGINS", tmp_path / "casa" / ".cursor" / "plugins")
    monkeypatch.setattr(
        cli, "_resolve_disensor",
        lambda: (str(tmp_path / "bin" / "disensor"), "" if disensor_ok else "no anda"),
    )
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_global_install_touches_no_repository(monkeypatch, tmp_path, capsys):
    assert run(["init"], monkeypatch, tmp_path) == 0
    data = read(cli.USER_HOOKS)
    assert set(data["hooks"]) == {"sessionStart", "afterFileEdit", "stop"}
    salida = capsys.readouterr().out
    assert "disensor.config.json" in salida, "tiene que decir cual es el guard"
    assert "pilot" in salida and "no stability promise" in salida


def test_the_stop_hook_is_advisory_and_bounded(monkeypatch, tmp_path, capsys):
    run(["init"], monkeypatch, tmp_path)
    stop = read(cli.USER_HOOKS)["hooks"]["stop"][0]
    assert stop["failClosed"] is False
    assert stop["loop_limit"] == 1
    assert templates.MARKER in stop["command"]


def test_init_is_idempotent(monkeypatch, tmp_path, capsys):
    run(["init"], monkeypatch, tmp_path)
    primero = cli.USER_HOOKS.read_text(encoding="utf-8")
    run(["init"], monkeypatch, tmp_path)
    assert cli.USER_HOOKS.read_text(encoding="utf-8") == primero
    assert len(read(cli.USER_HOOKS)["hooks"]["stop"]) == 1, "no se duplica"


def test_hooks_installed_by_somebody_else_are_preserved(monkeypatch, tmp_path, capsys):
    """This file lives in the user's home and other tools write to it too.
    Overwriting it would delete their work; appending would duplicate ours."""
    monkeypatch.setattr(cli, "USER_HOOKS", tmp_path / "casa" / ".cursor" / "hooks.json")
    cli.USER_HOOKS.parent.mkdir(parents=True)
    cli.USER_HOOKS.write_text(json.dumps({
        "version": 1,
        "hooks": {
            "stop": [{"command": "otra-herramienta --stop"}],
            "beforeSubmitPrompt": [{"command": "algo-mio"}],
        },
    }), encoding="utf-8")

    run(["init"], monkeypatch, tmp_path)
    hooks = read(cli.USER_HOOKS)["hooks"]
    comandos = [e["command"] for e in hooks["stop"]]
    assert "otra-herramienta --stop" in comandos
    assert any(templates.MARKER in c for c in comandos)
    assert hooks["beforeSubmitPrompt"] == [{"command": "algo-mio"}]


def test_reinstalling_replaces_only_our_entry(monkeypatch, tmp_path, capsys):
    run(["init"], monkeypatch, tmp_path)
    data = read(cli.USER_HOOKS)
    data["hooks"]["stop"].insert(0, {"command": "ajeno"})
    cli.USER_HOOKS.write_text(json.dumps(data), encoding="utf-8")

    run(["init"], monkeypatch, tmp_path)
    comandos = [e["command"] for e in read(cli.USER_HOOKS)["hooks"]["stop"]]
    assert comandos.count("ajeno") == 1
    assert sum(1 for c in comandos if templates.MARKER in c) == 1


def test_a_disensor_that_does_not_answer_stops_the_install(monkeypatch, tmp_path, capsys):
    assert run(["init"], monkeypatch, tmp_path, disensor_ok=False) == 1
    assert not cli.USER_HOOKS.exists(), "una invocacion rechazada no deja nada escrito"
    assert "--force" in capsys.readouterr().out


def test_force_writes_anyway_and_says_so(monkeypatch, tmp_path, capsys):
    assert run(["init", "--force"], monkeypatch, tmp_path, disensor_ok=False) == 0
    assert cli.USER_HOOKS.exists()
    assert "WARNING" in capsys.readouterr().out


def test_the_project_install_says_what_it_costs(monkeypatch, tmp_path, capsys, repo):
    monkeypatch.chdir(repo)
    assert run(["init", "--project"], monkeypatch, tmp_path) == 0
    assert (repo / ".cursor" / "hooks.json").exists()
    regla = (repo / ".cursor" / "rules" / "disensor.mdc").read_text(encoding="utf-8")
    assert "alwaysApply: true" in regla
    assert "disensor guide" in regla, "la regla apunta a la fuente, no la copia"

    salida = capsys.readouterr().out
    assert "executable code, versioned in this repository" in salida
    assert "A pull request can change what it runs" in salida
    assert "--global" in salida
    # El archivo se versiona y lleva rutas absolutas de esta maquina: clonado
    # en otra, no resuelven. Callarlo dejaria hooks muertos sin explicacion.
    assert "absolute paths from THIS machine" in salida
    assert ".gitignore" in salida


def test_the_project_hook_pins_the_target_branch(monkeypatch, tmp_path, capsys, repo):
    """At user level the hook serves many repositories and resolves the base
    per run; pinned to one project, it can be fixed at install time."""
    monkeypatch.chdir(repo)
    run(["init", "--project"], monkeypatch, tmp_path)
    stop = read(repo / ".cursor" / "hooks.json")["hooks"]["stop"][0]
    assert "--base origin/main" in stop["command"]


def test_the_rule_is_only_written_for_a_project_install(monkeypatch, tmp_path, capsys, repo):
    """Cursor defines user rules through its UI, not through a file, so a
    global install has no .mdc to write. sessionStart carries that text."""
    monkeypatch.chdir(repo)
    run(["init"], monkeypatch, tmp_path)
    assert not (repo / ".cursor").exists()


# --- la regla del proyecto ------------------------------------------------

def test_an_edited_rule_is_never_overwritten(monkeypatch, tmp_path, capsys, repo):
    """Same doctrine as disensor's own init: what it cannot recognise as its
    own is left alone and reported, never silently replaced."""
    monkeypatch.chdir(repo)
    regla = repo / ".cursor" / "rules" / "disensor.mdc"
    regla.parent.mkdir(parents=True)
    regla.write_bytes(b"---\nalwaysApply: true\n---\n\nMi propia regla.\n")
    run(["init", "--project"], monkeypatch, tmp_path)
    assert regla.read_bytes() == b"---\nalwaysApply: true\n---\n\nMi propia regla.\n"
    assert "edited: left as it is" in capsys.readouterr().out


def test_a_rule_from_an_older_version_is_upgraded(monkeypatch, tmp_path, capsys, repo):
    monkeypatch.chdir(repo)
    regla = repo / ".cursor" / "rules" / "disensor.mdc"
    regla.parent.mkdir(parents=True)
    vieja = templates.RULE.replace(f"v{templates.__version__} -->", "v0.0.1 -->")
    assert vieja != templates.RULE
    regla.write_bytes(vieja.encode("utf-8"))
    run(["init", "--project"], monkeypatch, tmp_path)
    assert regla.read_bytes().decode("utf-8") == templates.RULE


# La regla que escribio init de 0.1.0 a 0.2.2, tal cual salio de sus versiones.
RULE_022 = """---
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

<!-- disensor-cursor v0.2.2 -->
"""


def test_the_rule_0_2_2_wrote_is_upgraded(monkeypatch, tmp_path, capsys, repo):
    """Codex, round on the 0.3.0 diff: the consent line changed the body, and
    the rule 0.2.2 wrote read as edited, so init kept it as it was."""
    monkeypatch.chdir(repo)
    regla = repo / ".cursor" / "rules" / "disensor.mdc"
    regla.parent.mkdir(parents=True)
    regla.write_bytes(RULE_022.encode("utf-8"))
    run(["init", "--project"], monkeypatch, tmp_path)
    assert regla.read_bytes().decode("utf-8") == templates.RULE


def test_the_rule_0_2_2_wrote_is_uninstalled(monkeypatch, tmp_path, capsys, repo):
    monkeypatch.chdir(repo)
    regla = repo / ".cursor" / "rules" / "disensor.mdc"
    regla.parent.mkdir(parents=True)
    regla.write_bytes(RULE_022.encode("utf-8"))
    run(["uninstall", "--project"], monkeypatch, tmp_path)
    assert not regla.exists()


@pytest.mark.parametrize("encoding", ["bom", "bom-crlf"])
def test_a_rule_an_editor_saved_with_a_bom_is_still_ours(monkeypatch, tmp_path, capsys, repo, encoding):
    """Codex, second round on the 0.3.0 diff: a BOM is an encoding, not an
    edit, and an editor adds it on saving without touching anything."""
    monkeypatch.chdir(repo)
    regla = repo / ".cursor" / "rules" / "disensor.mdc"
    regla.parent.mkdir(parents=True)
    texto = RULE_022.replace("\n", "\r\n") if encoding == "bom-crlf" else RULE_022
    regla.write_bytes(b"\xef\xbb\xbf" + texto.encode("utf-8"))
    run(["init", "--project"], monkeypatch, tmp_path)
    assert regla.read_bytes().decode("utf-8") == templates.RULE
    run(["uninstall", "--project"], monkeypatch, tmp_path)
    assert not regla.exists()


def test_an_edited_rule_of_0_2_2_is_still_left_alone(monkeypatch, tmp_path, capsys, repo):
    monkeypatch.chdir(repo)
    regla = repo / ".cursor" / "rules" / "disensor.mdc"
    regla.parent.mkdir(parents=True)
    editada = RULE_022.replace("Never paste", "Never, ever paste")
    regla.write_bytes(editada.encode("utf-8"))
    run(["init", "--project"], monkeypatch, tmp_path)
    assert regla.read_bytes().decode("utf-8") == editada
    assert "edited: left as it is" in capsys.readouterr().out


def test_the_rule_is_written_with_the_line_endings_it_was_given(monkeypatch, tmp_path, capsys, repo):
    """write_text would turn every newline into CRLF on Windows, inside
    somebody else's repository."""
    monkeypatch.chdir(repo)
    run(["init", "--project"], monkeypatch, tmp_path)
    assert b"\r\n" not in (repo / ".cursor" / "rules" / "disensor.mdc").read_bytes()


# --- uninstall ------------------------------------------------------------

def test_uninstall_removes_a_file_that_only_held_our_hooks(monkeypatch, tmp_path, capsys):
    run(["init"], monkeypatch, tmp_path)
    assert run(["uninstall"], monkeypatch, tmp_path) == 0
    assert not cli.USER_HOOKS.exists()


def test_uninstall_keeps_what_somebody_else_installed(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "USER_HOOKS", tmp_path / "casa" / ".cursor" / "hooks.json")
    cli.USER_HOOKS.parent.mkdir(parents=True)
    ajenos = {"stop": [{"command": "otra-herramienta --stop"}]}
    cli.USER_HOOKS.write_text(json.dumps({"version": 1, "hooks": ajenos}), encoding="utf-8")
    run(["init"], monkeypatch, tmp_path)
    run(["uninstall"], monkeypatch, tmp_path)
    assert read(cli.USER_HOOKS)["hooks"] == ajenos


def test_uninstall_without_anything_installed_says_so(monkeypatch, tmp_path, capsys):
    assert run(["uninstall"], monkeypatch, tmp_path) == 0
    assert "no " in capsys.readouterr().out


def test_uninstall_of_a_project_leaves_an_edited_rule(monkeypatch, tmp_path, capsys, repo):
    monkeypatch.chdir(repo)
    run(["init", "--project"], monkeypatch, tmp_path)
    regla = repo / ".cursor" / "rules" / "disensor.mdc"
    regla.write_bytes(regla.read_bytes() + b"\nUna nota mia.\n")
    run(["uninstall", "--project"], monkeypatch, tmp_path)
    assert not (repo / ".cursor" / "hooks.json").exists()
    assert regla.exists()
    assert "edited" in capsys.readouterr().out


def test_uninstall_of_a_project_removes_its_own_rule(monkeypatch, tmp_path, capsys, repo):
    monkeypatch.chdir(repo)
    run(["init", "--project"], monkeypatch, tmp_path)
    run(["uninstall", "--project"], monkeypatch, tmp_path)
    assert not (repo / ".cursor" / "rules" / "disensor.mdc").exists()


def test_purge_deletes_the_session_state(monkeypatch, tmp_path, capsys, isolated_state):
    isolated_state.mkdir(parents=True)
    (isolated_state / "state-x.json").write_text("{}", encoding="utf-8")
    run(["uninstall", "--purge"], monkeypatch, tmp_path)
    assert not isolated_state.exists()


# --- doctor ---------------------------------------------------------------

@pytest.fixture
def importable(monkeypatch, tmp_path):
    """A home of its own for any child doctor starts, so nothing reaches the
    real one. Whether the package is installed matters only to the test that
    expects the smoke run to succeed; see `installed`."""
    hogar = tmp_path / "hogar-hijo"
    hogar.mkdir()
    monkeypatch.setenv("USERPROFILE", str(hogar))
    monkeypatch.setenv("HOME", str(hogar))


def test_doctor_runs_the_installed_command(monkeypatch, tmp_path, capsys, importable):
    """No `pip install` needed: the command names the directory the package
    was found in, so the smoke run works from this checkout too."""
    monkeypatch.chdir(tmp_path)
    run(["init"], monkeypatch, tmp_path)
    capsys.readouterr()
    assert run(["doctor"], monkeypatch, tmp_path) == 0
    assert "smoke run: ok" in capsys.readouterr().out


def test_a_hook_configured_as_the_readme_says_survives_init_and_passes_doctor(
    monkeypatch, tmp_path, capsys, importable,
):
    """Codex, third round, made the entry recognisable; it still was not
    runnable by doctor, and init dropped the settings."""
    monkeypatch.chdir(tmp_path)
    run(["init"], monkeypatch, tmp_path)
    data = read(cli.USER_HOOKS)
    for entries in data["hooks"].values():
        entries[0]["command"] += ' --config "mi politica.json" --directory evidencia'
    cli.USER_HOOKS.write_text(json.dumps(data), encoding="utf-8")

    run(["init"], monkeypatch, tmp_path)
    for entries in read(cli.USER_HOOKS)["hooks"].values():
        assert templates.kept_settings(entries[0]) == [
            ("--config", "mi politica.json"), ("--directory", "evidencia"),
        ]
    capsys.readouterr()
    assert run(["doctor"], monkeypatch, tmp_path) == 0
    assert "smoke run: ok" in capsys.readouterr().out


def test_doctor_says_why_the_tree_cannot_be_checked(monkeypatch, tmp_path, capsys, repo):
    """When the tree cannot be read the hook stays quiet there, and that is
    what doctor is for."""
    from disensor_cursor import gitio

    def unreadable(repo_dir):
        raise gitio.GitUnavailable("could not read the repository's git configuration (bad config)")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(gitio, "dirty_paths", unreadable)
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    salida = capsys.readouterr().out
    assert "not checked" in salida and "git configuration" in salida
    assert "stays quiet in this repository" in salida


def _planted_disensor(directory, mark):
    """A disensor.cmd that leaves a mark if anything runs it, the probe included."""
    directory.mkdir(parents=True, exist_ok=True)
    plantado = directory / "disensor.cmd"
    plantado.write_bytes(f'@echo off\r\ntype nul > "{mark}"\r\necho --check\r\n'.encode("utf-8"))
    return plantado


def _main(monkeypatch, *argv) -> int:
    """The command as it is really run, through `main` and its exit code."""
    import sys

    monkeypatch.setattr(sys, "argv", ["disensor-cursor", *argv])
    with pytest.raises(SystemExit) as salida:
        cli.main()
    return salida.value.code


def test_init_never_writes_a_disensor_from_the_repository(monkeypatch, tmp_path, capsys, repo):
    """Codex, fifth round: init resolved disensor through a PATH entry inside
    the repository it ran from, probed it, and wrote it into the hooks of every
    session. Run from there, the command now leaves the repository out of
    PATH, as the hook does."""
    import os
    import sys
    from pathlib import Path

    from disensor_cursor import executables

    if sys.platform != "win32":
        pytest.skip("the planted program is a .cmd script")
    marca = tmp_path / "ejecutado.txt"
    plantado = _planted_disensor(repo / "bin", marca)
    monkeypatch.setenv("PATH", os.pathsep.join([str(repo / "bin"), os.environ["PATH"]]))
    monkeypatch.chdir(repo)
    assert Path(executables.find("disensor")) == plantado, "the premise: it is first on PATH"

    monkeypatch.setattr(cli, "USER_HOOKS", tmp_path / "casa" / ".cursor" / "hooks.json")
    _main(monkeypatch, "init", "--force")
    assert not marca.exists(), "not even probed"
    if cli.USER_HOOKS.exists():
        assert str(plantado) not in cli.USER_HOOKS.read_text(encoding="utf-8")


def test_init_never_writes_a_disensor_committed_to_another_repository(monkeypatch, tmp_path, capsys, repo):
    """And the git that checks it comes from outside that repository. Codex,
    sixth round: a `git.cmd` next to the planted disensor answered with an
    error, the error read as "not committed", and both ran."""
    import os
    import sys

    from conftest import git

    if sys.platform != "win32":
        pytest.skip("the planted programs are .cmd scripts")
    marca = tmp_path / "ejecutado.txt"
    _planted_disensor(repo / "bin", marca)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "planta un disensor")
    (repo / "bin" / "git.cmd").write_bytes(f'@echo off\r\ntype nul > "{marca}"\r\nexit /b 1\r\n'.encode())
    monkeypatch.setenv("PATH", os.pathsep.join([str(repo / "bin"), os.environ["PATH"]]))
    monkeypatch.chdir(tmp_path)
    path, problem = cli._resolve_disensor()
    assert "committed to the repository" in problem
    assert not marca.exists(), "neither the planted git nor the planted disensor ran"


def test_a_commit_check_that_cannot_answer_is_not_read_as_not_committed(tmp_path):
    """Only exit 0 (committed) and 1 (not committed) are answers. A `.git`
    that git does not take for a repository makes it exit 128, and that is
    "could not check", which refuses."""
    from pathlib import Path

    falso = tmp_path / "falso"
    (falso / ".git").mkdir(parents=True)
    (falso / "bin").mkdir()
    programa = falso / "bin" / "disensor.cmd"
    programa.write_bytes(b"@echo off\r\n")
    assert "could not be checked" in cli._repository_content(Path(programa))


def test_doctor_without_git_explains_instead_of_crashing(monkeypatch, tmp_path, capsys):
    from disensor_cursor import executables

    real = executables.find
    monkeypatch.setattr(executables, "find",
                        lambda name, exclude=(): None if name == "git" else real(name, exclude))
    monkeypatch.chdir(tmp_path)
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    assert "not on PATH" in capsys.readouterr().out


def test_doctor_never_runs_a_command_it_did_not_write(monkeypatch, tmp_path, capsys, importable):
    """A project hooks.json is written by whoever wrote the repository, and
    doctor is what you run on a repository you do not trust yet. The entry
    below has exactly the shape init writes, so it passes for ours, but its
    interpreter is a script that leaves a mark. That it really does is proven
    first; then doctor must show it and not run it."""
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("the stand-in interpreter is a .cmd script")
    monkeypatch.chdir(tmp_path)
    marca = tmp_path / "ejecutado.txt"
    falso = tmp_path / "python-falso.cmd"
    falso.write_bytes(f'@echo off\r\ntype nul > "{marca}"\r\necho {{}}\r\n'.encode("utf-8"))
    hostil = templates.command_for("afterFileEdit", str(falso), "", None)
    assert templates.is_ours({"command": hostil}), "the test needs an entry that passes for ours"

    subprocess.run(hostil, shell=True, input=b"{}", capture_output=True, check=False)
    assert marca.exists(), "the stand-in has to really run for this test to mean anything"
    marca.unlink()

    monkeypatch.setattr(cli, "USER_HOOKS", tmp_path / "casa" / ".cursor" / "hooks.json")
    cli.USER_HOOKS.parent.mkdir(parents=True)
    cli.USER_HOOKS.write_text(json.dumps({"version": 1, "hooks": {
        "afterFileEdit": [{"command": hostil, "failClosed": False}],
    }}), encoding="utf-8")
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    salida = capsys.readouterr().out
    assert "NOT RUN" in salida
    assert str(falso) in salida, "what is not run has to be shown"
    assert not marca.exists(), "doctor executed a command read from a file"


def test_the_smoke_run_starts_the_hook_without_a_shell(monkeypatch, tmp_path):
    """A shell drags in its own startup, profiles, module paths, startup
    files, and each was a way for code from the project to run: two Codex
    rounds found five of them. doctor runs the exact command init writes, so
    it knows its arguments and starts the interpreter directly."""
    import subprocess
    import sys

    comando = templates.command_for("afterFileEdit", sys.executable, "", None)
    visto = {}

    def run(argv, **kwargs):
        visto.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, b"{}\n", b"")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._smoke(comando, tmp_path) == (True, "answered, exit 0")
    assert visto["argv"] == templates._tokens(comando)
    assert visto["argv"][0] == sys.executable
    assert not visto.get("shell")
    assert visto["input"].startswith(b"\xef\xbb\xbf"), "the payload carries the BOM Cursor's does"


def _on_path(monkeypatch, tmp_path, *names):
    """Directories on PATH, in order, each holding the files named."""
    entradas = []
    for index, group in enumerate(names):
        directory = tmp_path / f"path{index}"
        directory.mkdir()
        for name in group:
            (directory / name).write_bytes(b"")
        entradas.append(str(directory))
    monkeypatch.setenv("PATH", os.pathsep.join(entradas))
    return entradas


def test_cursors_shell_is_found_the_way_cursor_finds_it(monkeypatch, tmp_path):
    """Read from Cursor's `zr`: each extension over the whole PATH before the
    next one, so a pwsh.exe late on PATH wins over a pwsh.cmd early on it."""
    temprano, tarde = _on_path(monkeypatch, tmp_path, ["pwsh.cmd"], ["pwsh.exe"])
    assert cli._cursor_shell() == (os.path.join(tarde, "pwsh.exe"), "")


@pytest.mark.parametrize("wrapper", ["pwsh.cmd", "pwsh.bat", "powershell.cmd"])
def test_a_batch_file_named_like_powershell_means_no_hook_would_run(monkeypatch, tmp_path, wrapper):
    """Codex, the round on doctor: Cursor starts cmd.exe with PowerShell's
    arguments. Verified: cmd.exe then opens its banner, reads the end of its
    input and exits 0 having run nothing, so every hook looks fine and none runs."""
    _on_path(monkeypatch, tmp_path, [wrapper])
    shell, problem = cli._cursor_shell()
    assert shell is None and "batch file" in problem


def test_a_directory_named_like_powershell_is_what_cursor_picks(monkeypatch, tmp_path):
    """Codex, third round on doctor: Cursor's resolver takes anything that
    exists, a directory too, and then fails to start it."""
    carpeta = tmp_path / "path0"
    (carpeta / "pwsh.exe").mkdir(parents=True)
    monkeypatch.setenv("PATH", str(carpeta))
    shell, problem = cli._cursor_shell()
    assert shell is None and "not a file" in problem


def test_path_entries_are_taken_as_they_come(monkeypatch, tmp_path):
    """Cursor does not strip the quotes of a PATH entry, so a quoted one holds
    nothing it can find."""
    (entrada,) = _on_path(monkeypatch, tmp_path, ["pwsh.exe"])
    monkeypatch.setenv("PATH", f'"{entrada}"')
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "sin-sistema"))
    assert cli._cursor_shell()[0] is None


def _system_root(monkeypatch, tmp_path, *, with_powershell=True):
    root = tmp_path / "sistema"
    carpeta = root / "System32" / "WindowsPowerShell" / "v1.0"
    carpeta.mkdir(parents=True)
    if with_powershell:
        (carpeta / "powershell.exe").write_bytes(b"")
    monkeypatch.setenv("SYSTEMROOT", str(root))
    return str(carpeta / "powershell.exe")


def test_a_script_named_pwsh_means_the_systems_windows_powershell(monkeypatch, tmp_path):
    _on_path(monkeypatch, tmp_path, ["pwsh.ps1"])
    esperado = _system_root(monkeypatch, tmp_path)
    assert cli._cursor_shell() == (esperado, "")


def test_a_directory_named_pwsh_ps1_still_means_the_systems_windows_powershell(monkeypatch, tmp_path):
    """Codex, fourth round on doctor: Cursor turns any .ps1 it finds into the
    system's Windows PowerShell, a directory too, and the hooks run. doctor
    said none would."""
    carpeta = tmp_path / "path0"
    (carpeta / "pwsh.ps1").mkdir(parents=True)
    monkeypatch.setenv("PATH", str(carpeta))
    esperado = _system_root(monkeypatch, tmp_path)
    assert cli._cursor_shell() == (esperado, "")


def test_a_script_named_pwsh_without_windows_powershell_runs_nothing(monkeypatch, tmp_path):
    _on_path(monkeypatch, tmp_path, ["pwsh.ps1"])
    _system_root(monkeypatch, tmp_path, with_powershell=False)
    shell, problem = cli._cursor_shell()
    assert shell is None and "not there" in problem


def test_a_shell_cursor_would_take_from_the_project_is_reported(monkeypatch, tmp_path):
    """Codex, second round on doctor: the adapter searched only the safe part
    of PATH, so when Cursor's pick came from the project doctor quietly
    described another shell. Now it names the one Cursor would start."""
    (entrada,) = _on_path(monkeypatch, tmp_path, ["pwsh.exe"])
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(tmp_path))
    shell, problem = cli._cursor_shell()
    assert shell is None and "inside the project" in problem


@pytest.mark.parametrize("variables, expected", [
    ({"TEMP": r"C:\t1", "TMP": r"C:\t2"}, r"C:\t1"),
    ({"TMP": "C:\\t2\\"}, r"C:\t2"),
    ({"SystemRoot": r"C:\Win"}, r"C:\Win\temp"),
    ({"TEMP": "C:\\"}, "C:\\"),
])
def test_cursors_temporary_directory_is_nodes(monkeypatch, variables, expected):
    """Codex, second round on doctor: Python and Node pick the temporary
    directory in different orders (TMPDIR first for Python, TEMP for Node),
    so doctor judged a directory Cursor does not use. And tempfile writes a
    probe file to find it; this reads the environment and writes nothing."""
    for name in ("TEMP", "TMP", "SystemRoot", "windir"):
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    assert cli._cursor_tmpdir() == expected


def test_doctor_says_how_cursor_runs_hooks(monkeypatch, capsys):
    import sys

    assert cli._describe_cursor() is True
    salida = capsys.readouterr().out
    assert "cursor:   hooks run through" in salida
    if sys.platform != "win32":
        assert "-c, payload on stdin" in salida


def test_doctor_fails_where_cursor_would_run_no_hook(monkeypatch, tmp_path, capsys, importable):
    import sys

    if sys.platform != "win32":
        pytest.skip("the shell Cursor starts is chosen this way only on Windows")
    monkeypatch.chdir(tmp_path)
    run(["init"], monkeypatch, tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(cli, "_cursor_shell", lambda: (None, "the reason"))
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    assert "NO HOOK WOULD RUN: the reason" in capsys.readouterr().out


def test_doctor_names_a_typographic_quote_where_cursor_writes_payloads(monkeypatch, capsys):
    """Codex, the round on doctor: PowerShell also closes a quoted string at
    ‘ ’ ‚ ‛, and Cursor escapes only the ASCII quote when it puts the payload's
    path in its script. With one in that directory no hook gets its payload."""
    import sys

    if sys.platform != "win32":
        pytest.skip("the payload goes through a file only on Windows")
    monkeypatch.setattr(cli, "_cursor_shell", lambda: (r"C:\ps\pwsh.exe", ""))
    monkeypatch.setenv("TEMP", "C:\\Users\\O\u2019Brien\\AppData\\Local\\Temp")
    assert cli._describe_cursor() is False
    assert "NO HOOK WOULD RUN" in capsys.readouterr().out


def test_doctor_reports_an_installed_command_that_fails(monkeypatch, tmp_path, capsys, importable):
    """A broken interpreter path fails in silence inside Cursor: the hook just
    never answers. doctor is where that has to become visible."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_resolve_python", lambda: str(tmp_path / "no-existe" / "python.exe"))
    run(["init"], monkeypatch, tmp_path)
    capsys.readouterr()
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    assert "smoke run: FAILED" in capsys.readouterr().out


def test_doctor_notices_hooks_written_by_another_version(monkeypatch, tmp_path, capsys, importable):
    """`--generated-by` travels in every command. Written and never read, it
    was decoration; this is where it is read."""
    monkeypatch.chdir(tmp_path)
    run(["init"], monkeypatch, tmp_path)
    viejo = cli.USER_HOOKS.read_text(encoding="utf-8").replace(
        f"--generated-by {templates.__version__}", "--generated-by 0.0.1"
    )
    cli.USER_HOOKS.write_text(viejo, encoding="utf-8")
    capsys.readouterr()
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    assert "generated by 0.0.1" in capsys.readouterr().out


def test_purge_refuses_to_delete_through_a_junction(monkeypatch, tmp_path, capsys):
    """rmtree refuses a junction on its own, but with a traceback. Refused on
    purpose, it says why and leaves the target alone."""
    import subprocess
    import sys

    from disensor_cursor import state

    if sys.platform != "win32":
        pytest.skip("junctions are a Windows thing")
    destino = tmp_path / "destino"
    destino.mkdir()
    (destino / "importante.txt").write_text("x", encoding="utf-8")
    enlace = tmp_path / "estado"
    hecho = subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(destino)],
                           capture_output=True, check=False)
    if hecho.returncode != 0:
        pytest.skip("could not create a junction here")
    monkeypatch.setattr(state, "STATE_DIR", enlace)
    assert run(["uninstall", "--purge"], monkeypatch, tmp_path) == 0
    assert "refusing" in capsys.readouterr().out
    assert (destino / "importante.txt").exists()


def test_doctor_says_when_the_state_directory_is_a_link(monkeypatch, tmp_path, capsys):
    """Otherwise the hook goes inactive and nothing anywhere says why."""
    import subprocess
    import sys

    from disensor_cursor import state

    if sys.platform != "win32":
        pytest.skip("junctions are a Windows thing")
    destino = tmp_path / "destino"
    destino.mkdir()
    enlace = tmp_path / "estado"
    hecho = subprocess.run(["cmd", "/c", "mklink", "/J", str(enlace), str(destino)],
                           capture_output=True, check=False)
    if hecho.returncode != 0:
        pytest.skip("could not create a junction here")
    monkeypatch.setattr(state, "STATE_DIR", enlace)
    monkeypatch.chdir(tmp_path)
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    assert "link or junction" in capsys.readouterr().out


# --- de donde se importa el paquete ---------------------------------------

def _in_a_git_checkout() -> bool:
    return (Path(templates.package_location()).parent / ".git").exists()


@pytest.mark.skipif(not _in_a_git_checkout(), reason="the suite has to run from a git checkout")
def test_init_refuses_a_package_imported_from_a_checkout(monkeypatch, tmp_path, capsys):
    """Codex, round on the plugin plan: after `pip install -e` the hooks import
    the checkout, and every later edit to it, an agent's included, runs inside
    them. The suite itself runs from exactly that: this checkout."""
    monkeypatch.setattr(cli, "_package_problem", REAL_PACKAGE_PROBLEM)
    assert run(["init"], monkeypatch, tmp_path) == 1
    salida = capsys.readouterr().out
    assert "committed to the repository it lives in" in salida
    assert "--allow-checkout" in salida
    assert not cli.USER_HOOKS.exists()


@pytest.mark.skipif(not _in_a_git_checkout(), reason="the suite has to run from a git checkout")
def test_allow_checkout_installs_and_says_what_it_costs(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_package_problem", REAL_PACKAGE_PROBLEM)
    assert run(["init", "--allow-checkout"], monkeypatch, tmp_path) == 0
    assert "WARNING this disensor-cursor is imported from" in capsys.readouterr().out
    assert cli.USER_HOOKS.exists()


def test_a_virtualenv_inside_a_checkout_is_the_users_own_install(monkeypatch, repo):
    """Same doctrine as for disensor: what is refused is committed content."""
    site = repo / ".venv" / "Lib" / "site-packages"
    (site / "disensor_cursor").mkdir(parents=True)
    (site / "disensor_cursor" / "hook.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(templates, "package_location", lambda: str(site))
    assert REAL_PACKAGE_PROBLEM() == ""


def test_doctor_says_where_the_hook_imports_the_package_from(monkeypatch, tmp_path, capsys, importable):
    monkeypatch.chdir(tmp_path)
    run(["init"], monkeypatch, tmp_path)
    capsys.readouterr()
    assert run(["doctor"], monkeypatch, tmp_path) == 0
    esperado = Path(templates.package_location()) / "disensor_cursor" / "hook.py"
    assert f"module:    {esperado} ({templates.__version__})" in capsys.readouterr().out


def test_another_version_where_the_hook_imports_from_is_named(tmp_path):
    otra = tmp_path / "otra"
    (otra / "disensor_cursor").mkdir(parents=True)
    (otra / "disensor_cursor" / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    (otra / "disensor_cursor" / "hook.py").write_text("", encoding="utf-8")
    ok, detalle = cli._provenance(sys.executable, str(otra), tmp_path)
    assert not ok
    assert "version 0.0.1 there" in detalle


def test_doctor_looks_at_the_module_the_program_checks(tmp_path):
    """Codex, round on the 0.3.0 diff: with the package's `__init__` where the
    command says and its `hook` found somewhere else, the program answers `{}`
    without running anything, and a doctor that looked at the `__init__` said
    all was well."""
    import subprocess

    esperada, otra = tmp_path / "esperada", tmp_path / "otra"
    (esperada / "disensor_cursor").mkdir(parents=True)
    (otra / "disensor_cursor").mkdir(parents=True)
    (esperada / "disensor_cursor" / "__init__.py").write_text(
        f"__version__ = {templates.__version__!r}\n"
        f"__path__.append({str(otra / 'disensor_cursor')!r})\n",
        encoding="utf-8",
    )
    corrido = tmp_path / "corrido.txt"
    (otra / "disensor_cursor" / "hook.py").write_text(
        f"def main(*args):\n    open({str(corrido)!r}, 'w').close()\n    return 0\n", encoding="utf-8",
    )
    hecho = subprocess.run(
        [sys.executable, "-I", "-S", "-c", templates.PROGRAM, str(esperada), "--event", "stop"],
        input=b"{}", capture_output=True, timeout=60, check=False,
    )
    assert hecho.stdout.decode().strip() == "{}" and not corrido.exists(), "the program refuses it"
    ok, detalle = cli._provenance(sys.executable, str(esperada), tmp_path)
    assert not ok
    assert f"imported from {otra}" in detalle


def test_doctor_fails_when_the_module_comes_from_elsewhere(monkeypatch, tmp_path, capsys, importable):
    monkeypatch.chdir(tmp_path)
    run(["init"], monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_provenance", lambda *a: (False, "imported from X, not from Y"))
    capsys.readouterr()
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    assert "module:    WRONG: imported from X, not from Y" in capsys.readouterr().out


def test_doctor_warns_about_hooks_imported_from_a_checkout(monkeypatch, tmp_path, capsys, importable):
    monkeypatch.chdir(tmp_path)
    run(["init"], monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_package_problem", lambda: "this disensor-cursor is imported from X")
    capsys.readouterr()
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    assert "WARNING this disensor-cursor is imported from X" in capsys.readouterr().out


# --- el plugin ------------------------------------------------------------

def _plugin(folder: Path, name: str = "disensor-cursor", hooks: bool = False) -> Path:
    (folder / ".cursor-plugin").mkdir(parents=True)
    (folder / ".cursor-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    if hooks:
        (folder / "hooks").mkdir()
        (folder / "hooks" / "hooks.json").write_text('{"version": 1, "hooks": {}}', encoding="utf-8")
    return folder


def test_doctor_lists_every_copy_of_the_plugin_it_finds(monkeypatch, tmp_path, capsys):
    plugins = tmp_path / "plugins"
    local = _plugin(plugins / "local" / "disensor-cursor")
    cached = _plugin(plugins / "cache" / "cursor-public" / "disensor-cursor" / "abc123")
    _plugin(plugins / "local" / "otro", name="otro")
    monkeypatch.setattr(cli, "CURSOR_PLUGINS", plugins)
    assert cli._describe_plugin() is True
    salida = capsys.readouterr().out
    assert str(local) in salida and str(cached) in salida
    assert "otro" not in salida
    assert "cannot be read from here" in salida


def test_a_copy_of_the_plugin_with_hooks_of_its_own_is_reported(monkeypatch, tmp_path, capsys):
    """The plugin only brings the setup skill. A copy with hooks next to
    init's would run every event twice: Cursor runs all of them in parallel."""
    plugins = tmp_path / "plugins"
    _plugin(plugins / "local" / "disensor-cursor", hooks=True)
    monkeypatch.setattr(cli, "CURSOR_PLUGINS", plugins)
    assert cli._describe_plugin() is False
    assert "every event may run twice" in capsys.readouterr().out


def test_without_the_plugin_doctor_says_the_hooks_work_anyway(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "CURSOR_PLUGINS", tmp_path / "vacio")
    assert cli._describe_plugin() is True
    assert "not found (the hooks work without it)" in capsys.readouterr().out


def test_doctor_notices_the_hooks_installed_twice(monkeypatch, tmp_path, capsys, repo, importable):
    """Cursor runs the entries of every source in parallel: installed in the
    home and in the repository, each event here runs twice."""
    monkeypatch.chdir(repo)
    run(["init"], monkeypatch, tmp_path)
    run(["init", "--project"], monkeypatch, tmp_path)
    capsys.readouterr()
    assert run(["doctor"], monkeypatch, tmp_path) == 1
    assert "afterFileEdit, sessionStart, stop installed in your home and in this repository" in (
        capsys.readouterr().out
    )


def _keep_only(path, event):
    data = read(path)
    data["hooks"] = {event: data["hooks"][event]}
    path.write_text(json.dumps(data), encoding="utf-8")


def test_doctor_names_only_the_events_installed_twice(monkeypatch, tmp_path, capsys, repo, importable):
    monkeypatch.chdir(repo)
    run(["init"], monkeypatch, tmp_path)
    run(["init", "--project"], monkeypatch, tmp_path)
    _keep_only(repo / ".cursor" / "hooks.json", "stop")
    capsys.readouterr()
    run(["doctor"], monkeypatch, tmp_path)
    salida = capsys.readouterr().out
    assert "  twice:    stop installed in your home" in salida


def test_events_split_between_home_and_repository_are_not_twice(monkeypatch, tmp_path, capsys, repo, importable):
    """Codex, second round on the 0.3.0 diff: with sessionStart in the home and
    stop in the repository nothing runs twice, and doctor told the user to
    uninstall half of a working setup."""
    monkeypatch.chdir(repo)
    run(["init"], monkeypatch, tmp_path)
    run(["init", "--project"], monkeypatch, tmp_path)
    _keep_only(cli.USER_HOOKS, "sessionStart")
    _keep_only(repo / ".cursor" / "hooks.json", "stop")
    capsys.readouterr()
    run(["doctor"], monkeypatch, tmp_path)
    assert "twice" not in capsys.readouterr().out
