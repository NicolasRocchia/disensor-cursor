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
