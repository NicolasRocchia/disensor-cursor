"""Programs come from PATH, never from the current directory."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from disensor_cursor import executables, gitio


def test_relative_path_entries_are_skipped(tmp_path, monkeypatch):
    """A relative PATH entry, `.` included, resolves against the current
    directory, which in a project hook is the repository."""
    planted = tmp_path / "plantado"
    planted.mkdir()
    (planted / ("git.exe" if sys.platform == "win32" else "git")).write_bytes(b"")
    monkeypatch.chdir(planted)
    real = executables.find("git")
    monkeypatch.setenv("PATH", os.pathsep.join([".", "plantado", "", os.environ["PATH"]]))
    found = executables.find("git")
    assert found is not None
    assert Path(found).parent != planted
    assert found == real


def test_nothing_on_path_is_none(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert executables.find("git") is None


def test_children_skip_the_current_directory_in_their_own_searches():
    assert executables.child_environment()["NoDefaultCurrentDirectoryInExePath"] == "1"


def test_without_git_on_path_git_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(executables, "find", lambda name, exclude=(): None)
    with pytest.raises(gitio.GitUnavailable, match="not on PATH"):
        gitio.run_git(["--version"], tmp_path)


@pytest.fixture
def planted_repo(tmp_path, monkeypatch):
    """A checkout holding a `git.exe` and a `disensor.exe` that are copies of
    whoami, so what ran is visible in the output, entered as the current
    directory the way Cursor enters it for a project hook.

    `NoDefaultCurrentDirectoryInExePath` is removed on purpose. It is what
    switches the current-directory search off, it is not set by default on
    Windows, and the shell these tests were first run from happened to set it,
    which hid the attack: the premise failed until it was taken out.
    """
    if sys.platform != "win32":
        pytest.skip("the current-directory search is a Windows behaviour")
    whoami = shutil.which("whoami", path=os.path.join(os.environ["SystemRoot"], "System32"))
    if not whoami:
        pytest.skip("no whoami.exe to plant")
    planted = tmp_path / "repo"
    planted.mkdir()
    for name in ("git.exe", "disensor.exe"):
        shutil.copy(whoami, planted / name)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.chdir(planted)
    return planted


def _output(done) -> str:
    return (done.stdout or b"").decode("utf-8", "replace") + (done.stderr or b"").decode("utf-8", "replace")


def test_without_protection_the_planted_git_is_what_runs(planted_repo):
    """The attack, proven: a bare name, `shutil.which` and the shell all pick
    the copy in the current directory, on 3.12 and 3.14 alike."""
    assert "git version" not in _output(subprocess.run(["git", "--version"], capture_output=True))
    assert Path(shutil.which("git")).parent.resolve() == planted_repo.resolve()


def test_the_adapter_runs_the_real_git(planted_repo):
    code, out, _ = gitio.run_git(["--version"], planted_repo)
    assert code == 0 and out.startswith("git version")


def test_init_writes_the_real_disensor_into_the_hooks(planted_repo):
    """`init` is run inside a repository, and whatever it resolves is written
    into hooks.json for every later run."""
    from disensor_cursor import cli

    path, _ = cli._resolve_disensor()
    assert path and Path(path).parent.resolve() != planted_repo.resolve()


def test_an_absolute_entry_inside_the_project_is_skipped(tmp_path, monkeypatch):
    """Codex, third round: an absolute PATH entry pointing into the checkout is
    the current directory written differently."""
    proyecto = tmp_path / "proyecto"
    (proyecto / "bin").mkdir(parents=True)
    (proyecto / "bin" / ("git.exe" if sys.platform == "win32" else "git")).write_bytes(b"")
    real = executables.find("git")
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(proyecto))
    monkeypatch.setenv("PATH", os.pathsep.join([str(proyecto / "bin"), os.environ["PATH"]]))
    assert executables.find("git") == real
    assert str(proyecto / "bin") not in executables.safe_path_entries()


def test_a_declared_project_is_excluded_like_the_one_cursor_names(tmp_path, monkeypatch):
    """Codex, fourth round: without CURSOR_PROJECT_DIR the hook still finds the
    project, through the payload or the cwd, and a PATH entry inside it used
    to count."""
    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    proyecto = tmp_path / "proyecto"
    (proyecto / "bin").mkdir(parents=True)
    plantado = proyecto / "bin" / ("git.exe" if sys.platform == "win32" else "git")
    plantado.write_bytes(b"")
    plantado.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(proyecto / "bin"), os.environ["PATH"]]))
    assert Path(executables.find("git")) == plantado, "the premise: nothing excluded it"

    executables.declare_project(proyecto)
    assert Path(executables.find("git")).parent != proyecto / "bin"
    executables.forget_projects()
    assert Path(executables.find("git")) == plantado


def test_the_hook_declares_the_project_it_found_before_running_git(repo, monkeypatch):
    """The whole path, with the planted git a copy of whoami: if it ran, git
    could not answer and the hook would not find the repository at all."""
    if sys.platform != "win32":
        pytest.skip("the planted .exe is a Windows scenario")
    from disensor_cursor import hook

    whoami = shutil.which("whoami", path=os.path.join(os.environ["SystemRoot"], "System32"))
    if not whoami:
        pytest.skip("no whoami.exe to plant")
    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    (repo / "bin").mkdir()
    (repo / "anidada").mkdir()
    shutil.copy(whoami, repo / "bin" / "git.exe")
    monkeypatch.setenv("PATH", os.pathsep.join([str(repo / "bin"), os.environ["PATH"]]))
    assert gitio.repo_root(repo) is None, "the premise: the planted git is what answered"

    # Codex, quinta ronda: con la carpeta abierta en un subdirectorio, la raiz
    # se declaraba despues del primer git, y ese git ya era el plantado.
    for payload, cwd in (
        ({"workspace_roots": [str(repo)]}, None),
        ({}, repo),
        ({"workspace_roots": [str(repo / "anidada")]}, None),
    ):
        if cwd is not None:
            monkeypatch.chdir(cwd)
        found = hook._worktree(payload)
        assert found is not None and found.resolve() == repo.resolve()
        executables.forget_projects()


def test_the_repository_around_a_folder_is_found_without_git(tmp_path):
    raiz = tmp_path / "raiz"
    (raiz / ".git").mkdir(parents=True)
    (raiz / "a" / "b").mkdir(parents=True)
    assert executables.enclosing_repository(raiz / "a" / "b") == raiz.resolve()
    assert executables.enclosing_repository(raiz) == raiz.resolve()
    # Un submodulo o un worktree tienen un archivo .git, y ahi para git.
    (raiz / "a" / ".git").write_text("gitdir: ../.git/modules/a\n", encoding="utf-8")
    assert executables.enclosing_repository(raiz / "a" / "b") == (raiz / "a").resolve()


def test_disensor_is_never_run_from_inside_the_project(repo, monkeypatch):
    """Codex, fifth round: the hint init writes is absolute, and absolute is
    not the same as outside the repository."""
    from disensor_cursor import disensor

    (repo / "bin").mkdir()
    plantado = repo / "bin" / "disensor.cmd"
    plantado.write_bytes(b"@echo off\r\n")
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(repo))
    assert disensor.locate(str(plantado), repo) is None
    assert "inside the project" in disensor.why_not(str(plantado))
    afuera = repo.parent / "afuera" / "disensor.cmd"
    afuera.parent.mkdir()
    afuera.write_bytes(b"@echo off\r\n")
    assert disensor.locate(str(afuera), repo) == afuera


def test_a_run_of_the_hook_leaves_nothing_declared(repo, monkeypatch, stdin_payload, capsys):
    from disensor_cursor import hook

    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(repo))
    stdin_payload({"conversation_id": "c", "workspace_roots": [str(repo)]})
    assert hook.main(["--event", "afterFileEdit"]) == 0
    assert executables._declared == []


@pytest.mark.parametrize("variable", ["PYTHONUSERBASE", "PYTHONHOME", "PYTHONPYCACHEPREFIX"])
def test_a_python_location_that_points_into_the_repository_is_not_inherited(tmp_path, monkeypatch, variable):
    """Codex, fourth round: PYTHONUSERBASE=. moves the user site-packages into
    the checkout, and site.py runs the .pth files it finds there. The same
    variables can be the user's own, and disensor can live exactly there, so
    only the ones that point into the checkout go."""
    proyecto = tmp_path / "proyecto"
    proyecto.mkdir()
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(proyecto))
    for value, kept in ((".", False), ("relativo", False), (str(proyecto / "x"), False), (str(afuera), True)):
        monkeypatch.setenv(variable, value)
        env = executables.child_environment()
        assert (variable in {k.upper() for k in env}) is kept, value


def test_a_link_outside_the_project_to_a_program_inside_it_is_not_found(tmp_path, monkeypatch):
    """Codex, second round on doctor: find judged the PATH entry and not the
    file, so a link in an entry outside the project that points at a program
    inside it was taken. What runs is the target. The hook finds git and
    disensor this way."""
    import sys

    nombre = "herramienta.exe" if sys.platform == "win32" else "herramienta"
    proyecto = tmp_path / "proyecto"
    proyecto.mkdir()
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    en_bin = tmp_path / "bin"
    en_bin.mkdir()
    for destino in (proyecto / nombre, afuera / nombre):
        destino.write_bytes(b"")
        destino.chmod(0o755)
    try:
        (en_bin / nombre).symlink_to(proyecto / nombre)
    except OSError:
        pytest.skip("this machine does not let the test create a link")
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(proyecto))
    monkeypatch.setenv("PATH", str(en_bin))
    assert executables.find("herramienta") is None
    (en_bin / nombre).unlink()
    (en_bin / nombre).symlink_to(afuera / nombre)
    assert executables.find("herramienta") == str(en_bin / nombre), "a link to a program outside is fine"


def test_children_never_fetch_from_the_network():
    """A lazy fetch runs the transport the repository's config names."""
    assert executables.child_environment()["GIT_NO_LAZY_FETCH"] == "1"


def test_children_inherit_nothing_that_points_back_at_the_repository(tmp_path, monkeypatch):
    """-E protects the hook's own interpreter; a child used to get PYTHONPATH
    and PATH untouched, and disensor's launcher runs Python without -E."""
    proyecto = tmp_path / "proyecto"
    proyecto.mkdir()
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(proyecto))
    monkeypatch.setenv("PYTHONPATH", ".")
    monkeypatch.setenv("PYTHONSTARTUP", str(proyecto / "arranque.py"))
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(proyecto), os.environ["PATH"]]))
    env = executables.child_environment()
    assert "PYTHONPATH" not in {k.upper() for k in env}
    assert "PYTHONSTARTUP" not in {k.upper() for k in env}
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    entradas = env["PATH"].split(os.pathsep)
    assert "." not in entradas and "" not in entradas
    assert str(proyecto) not in entradas
