"""Git configuration that names a program, and what the adapter's git runs of it.

Git trusts the configuration of a repository the user owns, and some of it
names commands git runs on its own. A repository that arrives with its `.git`
directory, from an archive or a shared folder, brings that configuration with
it. Every test here first proves the command really runs under a plain git
invocation, the adapter's flags included, and only then that the adapter's
own call does not run it.
"""
from __future__ import annotations

import itertools
import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import git
from disensor_cursor import executables, gitio


def _marker(tmp_path: Path, name: str, *, passthrough: bool = False) -> tuple[Path, Path]:
    """A shell script that leaves a line in a mark file every time it runs.

    `passthrough` copies stdin to stdout, which a clean filter has to do for
    git to accept its output.
    """
    mark = tmp_path / f"{name}.log"
    script = tmp_path / f"{name}.sh"
    body = f'#!/bin/sh\necho "{name} $*" >> "{mark.as_posix()}"\n' + ("cat\n" if passthrough else "")
    script.write_bytes(body.encode("utf-8"))
    script.chmod(0o755)
    return script, mark


def _plain_status(repo: Path, *extra: str) -> None:
    """The status the adapter runs, without the adapter's environment."""
    subprocess.run(
        ["git", "--no-optional-locks", *gitio.STATUS_ARGS, *extra], cwd=repo,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False,
    )


_HOURS_BACK = itertools.count(1)


def _stat_dirty(path: Path) -> None:
    """Change the timestamps and nothing else, so git has to read the content.

    A different hour each call: git compares whole seconds, and two calls in
    the same second left the second one looking unchanged to it. That made a
    test pass whether or not the index got written.
    """
    moment = time.time() - 3600 * next(_HOURS_BACK)
    os.utime(path, (moment, moment))


# --- fsmonitor ------------------------------------------------------------

def test_an_fsmonitor_command_in_the_repository_config_never_runs(repo, tmp_path):
    script, mark = _marker(tmp_path, "fsmonitor")
    git(repo, "config", "core.fsmonitor", script.as_posix())
    _plain_status(repo)
    assert mark.exists(), "the premise: git status runs the configured fsmonitor"
    mark.unlink()

    assert gitio.dirty_paths(repo) is not None
    assert not mark.exists()


def test_the_fsmonitor_override_keeps_what_the_user_had(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'user.name=Alguien'")
    env = executables.child_environment()
    assert env["GIT_CONFIG_PARAMETERS"].startswith("'user.name=Alguien'")
    assert env["GIT_CONFIG_PARAMETERS"].endswith("'core.fsmonitor='"), "last, so it wins"


# --- filtros --------------------------------------------------------------

def _filtered(repo: Path, tmp_path: Path, driver: str = "ataque") -> tuple[Path, Path]:
    """A repository whose `.gitattributes` assigns a filter driver to a
    tracked file; where the driver's command comes from is up to each test."""
    script, mark = _marker(tmp_path, "clean", passthrough=True)
    (repo / ".gitattributes").write_text(f"*.txt filter={driver}\n", encoding="utf-8")
    tracked = repo / "datos.txt"
    tracked.write_text("hola\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "con atributos")
    return script, mark


def _never_runs(repo: Path, mark: Path) -> None:
    """The premise with a plain status, then the adapter's check: it answers,
    and the filter did not run."""
    _stat_dirty(repo / "datos.txt")
    _plain_status(repo)
    assert mark.exists(), "the premise: git status runs the clean filter of a touched file"
    mark.unlink()
    _stat_dirty(repo / "datos.txt")
    assert gitio.dirty_paths(repo) is not None, "the check still answers"
    assert not mark.exists()


def test_a_clean_filter_in_the_repository_config_never_runs(repo, tmp_path):
    script, mark = _filtered(repo, tmp_path)
    git(repo, "config", "filter.ataque.clean", f"sh '{script.as_posix()}'")
    _never_runs(repo, mark)


def test_a_filter_included_from_the_working_tree_never_runs(repo, tmp_path):
    """`.git/config` can include a file the repository ships."""
    script, mark = _filtered(repo, tmp_path)
    (repo / "incluido.cfg").write_text(
        f'[filter "ataque"]\n\tclean = sh \'{script.as_posix()}\'\n', encoding="utf-8",
    )
    git(repo, "config", "include.path", "../incluido.cfg")
    _never_runs(repo, mark)


def test_a_filter_in_the_worktree_config_never_runs(repo, tmp_path):
    script, mark = _filtered(repo, tmp_path)
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "filter.ataque.clean", f"sh '{script.as_posix()}'")
    _never_runs(repo, mark)


def test_a_filter_from_the_user_config_is_switched_off_too(repo, tmp_path, monkeypatch):
    """Codex, fifth round: a filter the user installed can run what the
    repository configures, so being the user's is no reason to let it run.
    The check still answers with every driver switched off."""
    script, mark = _filtered(repo, tmp_path, driver="mio")
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        f'[filter "mio"]\n\tclean = sh \'{script.as_posix()}\'\n\trequired = true\n', encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    _never_runs(repo, mark)


def test_a_driver_name_that_git_c_cannot_carry_is_switched_off_too(repo, tmp_path):
    """`git -c filter.a=b.clean=` splits at the first `=`; the override goes
    through GIT_CONFIG_COUNT, where key and value travel apart."""
    script, mark = _filtered(repo, tmp_path, driver="a=b")
    git(repo, "config", "filter.a=b.clean", f"sh '{script.as_posix()}'")
    _never_runs(repo, mark)


def test_a_driver_name_that_is_not_utf8_leaves_the_tree_unchecked(repo, tmp_path):
    """Codex, sixth round: decoded with replacement characters, `driver\\xff`
    became another name, and switching that one off left the real driver
    running. It cannot be named back, so status does not run at all."""
    script, mark = _filtered(repo, tmp_path, driver="x")
    (repo / ".gitattributes").write_bytes(b"*.txt filter=driver\xff\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "atributos con un byte suelto")
    with open(repo / ".git" / "config", "ab") as config:
        config.write(b'[filter "driver\xff"]\n\tclean = sh \'' + script.as_posix().encode() + b"'\n")
    _stat_dirty(repo / "datos.txt")
    _plain_status(repo)
    assert mark.exists(), "the premise: git runs the driver named with that byte"
    mark.unlink()

    _stat_dirty(repo / "datos.txt")
    with pytest.raises(gitio.GitUnavailable, match="not UTF-8"):
        gitio.dirty_paths(repo)
    assert not mark.exists()


def test_a_value_that_is_not_utf8_does_not_stop_the_check(repo, tmp_path):
    """Codex, seventh round: decoding the whole output strictly turned a stray
    byte in any value, a description for instance, into an unchecked tree.
    Only the names have to survive the trip, and they are the only thing
    decoded that strictly."""
    script, mark = _filtered(repo, tmp_path)
    git(repo, "config", "filter.ataque.clean", f"sh '{script.as_posix()}'")
    with open(repo / ".git" / "config", "ab") as config:
        config.write(b'[filter "ataque"]\n\tdescripcion = caf\xe9\n')
    _never_runs(repo, mark)


def test_a_driver_name_beyond_ascii_is_switched_off_by_its_real_name(repo, tmp_path):
    """Valid UTF-8 survives the trip through the environment on Windows."""
    script, mark = _filtered(repo, tmp_path, driver="filtroñ")
    git(repo, "config", "filter.filtroñ.clean", f"sh '{script.as_posix()}'")
    _never_runs(repo, mark)


def test_an_lfs_extension_in_the_repository_config_never_runs(repo, tmp_path, monkeypatch):
    """Codex, fifth round: git-lfs, installed by the user, runs the
    `lfs.extension.*.clean` command of `.git/config` on every clean. Checking
    only the repository's own filter drivers missed it."""
    if subprocess.run(["git", "lfs", "version"], capture_output=True, check=False).returncode != 0:
        pytest.skip("git-lfs is not installed")
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tsmudge = git-lfs smudge -- %f\n'
        "\tprocess = git-lfs filter-process\n\trequired = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    script, mark = _marker(tmp_path, "extension", passthrough=True)
    (repo / ".gitattributes").write_text("*.dat filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
    (repo / "a.dat").write_text("binario\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "con lfs")
    for variable, value in (("clean", f"sh '{script.as_posix()}' %f"),
                            ("smudge", f"sh '{script.as_posix()}' %f"), ("priority", "0")):
        git(repo, "config", f"lfs.extension.ataque.{variable}", value)

    _stat_dirty(repo / "a.dat")
    subprocess.run(["git", "--no-optional-locks", *gitio.STATUS_ARGS], cwd=repo,
                   env=executables.child_environment(), stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=60, check=False)
    assert mark.exists(), "the premise: with fsmonitor off, lfs still runs the extension"
    mark.unlink()
    _stat_dirty(repo / "a.dat")
    assert gitio.dirty_paths(repo) is not None
    assert not mark.exists()


def test_what_the_user_passes_through_git_config_count_is_kept(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Alguien")
    env = gitio._with_config(executables.child_environment(), [("filter.x.clean", "")])
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("user.name", "Alguien")
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("filter.x.clean", "")


def test_a_broken_config_leaves_the_tree_unchecked_and_says_why(repo):
    with open(repo / ".git" / "config", "a", encoding="utf-8") as config:
        config.write("[esto no es config\n")
    with pytest.raises(gitio.GitUnavailable, match="git configuration"):
        gitio.dirty_paths(repo)


# --- submodulos -----------------------------------------------------------

def test_a_submodule_config_never_runs_through_the_superproject(tmp_path):
    """With `--ignore-submodules=none`, the flag disensor uses, git runs a
    status inside each submodule under the submodule's own config, which the
    superproject's config does not show."""
    origin = tmp_path / "sub-origen"
    git(tmp_path, "init", "-q", str(origin))
    git(origin, "config", "user.email", "suite@example.test")
    git(origin, "config", "user.name", "suite")
    (origin / ".gitattributes").write_text("*.txt filter=ataque\n", encoding="utf-8")
    (origin / "b.txt").write_text("x\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "sub")

    superproject = tmp_path / "super"
    git(tmp_path, "init", "-q", str(superproject))
    git(superproject, "config", "user.email", "suite@example.test")
    git(superproject, "config", "user.name", "suite")
    (superproject / "c.txt").write_text("y\n", encoding="utf-8")
    git(superproject, "add", "-A")
    git(superproject, "commit", "-qm", "super")
    git(superproject, "-c", "protocol.file.allow=always", "submodule", "add", "-q", origin.as_posix(), "sub")
    git(superproject, "commit", "-qm", "con submodulo")

    script, mark = _marker(tmp_path, "clean-sub", passthrough=True)
    git(superproject / "sub", "config", "filter.ataque.clean", f"sh '{script.as_posix()}'")
    _stat_dirty(superproject / "sub" / "b.txt")
    _plain_status(superproject, "--ignore-submodules=none")
    assert mark.exists(), "the premise: the superproject's status runs the submodule's filter"
    mark.unlink()

    assert "ataque" not in gitio.configured_filters(superproject)
    assert gitio.dirty_paths(superproject) is not None
    assert not mark.exists()


# --- hooks del indice -----------------------------------------------------

def test_status_never_writes_the_index_so_no_hook_runs(repo, tmp_path):
    """A status that takes the index lock writes the refreshed index, and
    writing it runs `post-index-change`, from whatever `core.hooksPath` says:
    a directory the repository ships, for instance."""
    hooks = repo / "ganchos"
    hooks.mkdir()
    mark = tmp_path / "gancho.log"
    hook = hooks / "post-index-change"
    hook.write_bytes(f'#!/bin/sh\necho "gancho $*" >> "{mark.as_posix()}"\n'.encode("utf-8"))
    hook.chmod(0o755)
    git(repo, "config", "core.hooksPath", "ganchos")
    _stat_dirty(repo / "src" / "app.py")
    subprocess.run(["git", "status", "--porcelain"], cwd=repo, stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=60, check=False)
    assert mark.exists(), "the premise: a status with the lock runs the index hook"
    mark.unlink()

    _stat_dirty(repo / "src" / "app.py")
    assert gitio.dirty_paths(repo) is not None
    assert not mark.exists()


# --- descargas perezosas --------------------------------------------------

def test_a_missing_object_is_never_fetched(tmp_path):
    """In a partial clone, reading an object that is not there fetches it with
    whatever transport the config names, and `ext::` runs a command."""
    origin = tmp_path / "origen"
    git(tmp_path, "init", "-q", str(origin))
    git(origin, "config", "user.email", "suite@example.test")
    git(origin, "config", "user.name", "suite")
    (origin / "grande.txt").write_text("uno\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "uno")
    (origin / "grande.txt").write_text("dos\n", encoding="utf-8")
    git(origin, "commit", "-qam", "dos")
    git(origin, "config", "uploadpack.allowFilter", "true")

    clone = tmp_path / "parcial"
    git(tmp_path, "clone", "-q", "--no-local", "--no-checkout", "--filter=blob:none",
        f"file://{origin.as_posix()}", str(clone))
    old_blob = git(clone, "rev-parse", "HEAD~1:grande.txt")
    script, mark = _marker(tmp_path, "transporte")
    git(clone, "config", "remote.origin.url", f"ext::sh {script.as_posix()} %S")
    git(clone, "config", "protocol.ext.allow", "always")

    subprocess.run(["git", "show", old_blob], cwd=clone, stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=60, check=False)
    assert mark.exists(), "the premise: reading a missing object runs the transport"
    mark.unlink()

    code, _, _ = gitio.run_git(["show", old_blob], clone)
    assert code != 0, "the object is missing and has to stay missing"
    assert not mark.exists()
