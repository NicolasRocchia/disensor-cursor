"""`disensor-cursor init`: where it writes, and what it refuses to destroy."""
from __future__ import annotations

import json

import pytest

from disensor_cursor import cli, templates


def run(argv, monkeypatch, tmp_path, *, disensor_ok=True):
    monkeypatch.setattr(cli, "USER_HOOKS", tmp_path / "casa" / ".cursor" / "hooks.json")
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
