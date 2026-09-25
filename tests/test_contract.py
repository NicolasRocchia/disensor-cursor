"""The contract with the disensor CLI that is actually installed.

The rest of the suite replaces disensor with doubles, which is right for the
adapter's own logic and says nothing about whether the adapter still speaks
the language of the disensor a user has. A renamed flag, a new family or a
reworded line would leave every other test green. This file runs the real
executable against real repositories.

Point it at a build with DISENSOR_EXE; without it, whatever `disensor` is on
PATH. When there is none, the module is skipped, and the skip says so.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import git
from disensor_cursor import disensor, families, hook


def _executable() -> Path | None:
    explicit = os.environ.get("DISENSOR_EXE")
    if explicit:
        return Path(explicit)
    found = shutil.which("disensor")
    return Path(found) if found else None


EXE = _executable()

pytestmark = pytest.mark.skipif(
    EXE is None or not EXE.is_file(),
    reason="no disensor executable to test the contract against (set DISENSOR_EXE)",
)


@pytest.fixture(scope="module")
def round_help() -> str:
    done = subprocess.run([str(EXE), "round", "--help"], capture_output=True, check=False)
    return (done.stdout or b"").decode("utf-8", "replace")


def make_repo(tmp_path: Path, config: dict) -> Path:
    """A repository whose target branch already carries `config` as its policy.

    The gate reads the policy from the tip of the target, not from the head,
    so the policy has to be committed before `origin/main` is pointed at it.
    """
    root = tmp_path / "contrato"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    git(root.parent, "init", "-q", str(root))
    git(root, "config", "user.email", "contrato@example.test")
    git(root, "config", "user.name", "contrato")
    (root / "disensor.config.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "docs" / "nota.md").write_text("nota\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "inicial")
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))
    return root


def commit(root: Path, changes: dict[str, str]) -> str:
    for relative, content in changes.items():
        (root / relative).write_text(content, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "cambio")
    return git(root, "rev-parse", "HEAD")


def ask(root: Path) -> disensor.Check:
    base = git(root, "rev-parse", "origin/main")
    head = git(root, "rev-parse", "HEAD")
    return disensor.check_requirement(EXE, root, base, head)


BASIC = {"criticality_level": "B", "level_A_enabled": False}


# --- la superficie que el adaptador usa ------------------------------------

def test_this_disensor_answers_the_probe():
    ok, detail = disensor.probe(EXE)
    assert ok, detail


def test_the_family_enum_is_the_one_disensor_accepts(round_help):
    """A family disensor does not know would be rejected after the round ran,
    and a family it added would sit unmapped here without anybody noticing."""
    match = re.search(r"--generator-family \{([^}]*)\}", round_help)
    assert match, "round --help no longer lists the generator families"
    accepted = set(match.group(1).split(","))
    assert accepted == set(families._table()["families"])


def test_every_flag_the_notice_suggests_exists(round_help):
    """The notice hands the agent a command to propose. A flag disensor
    renamed would turn it into a command that fails in front of the user."""
    flags = families.generator_flags("claude-4.5-sonnet", None)
    messages = [
        hook._required_message("origin/main", "a" * 40, "b" * 40, flags, "caveat"),
        hook._required_message("origin/main", "a" * 40, "b" * 40, [], "caveat"),
        hook._required_message("origin/main", "a" * 40, "b" * 40, [], "caveat", gates=("plan",)),
    ]
    suggested = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", "\n".join(messages)))
    assert suggested, "the notice suggests no flags at all"
    missing = sorted(f for f in suggested if f not in round_help)
    assert not missing, f"round --help does not know {missing}"


# --- los veredictos de `round --check` -----------------------------------

def test_an_empty_range_needs_no_round(tmp_path):
    root = make_repo(tmp_path, BASIC)
    assert ask(root).verdict == disensor.NOT_NEEDED


def test_a_code_change_needs_a_diff_round(tmp_path):
    """Also pins the wording `parse_gates` reads. That line is not part of the
    surface disensor promises, so a change has to fail here, loudly, before it
    turns into a wrong suggestion."""
    root = make_repo(tmp_path, BASIC)
    commit(root, {"src/app.py": "x = 2\n"})
    check = ask(root)
    assert check.verdict == disensor.REQUIRED
    assert check.gates == ("diff",), f"could not read the gates from {check.detail!r}"


def test_a_policy_that_accepts_only_plan_is_read_as_such(tmp_path):
    root = make_repo(tmp_path, {**BASIC, "gate": {"scope": [{"paths": ["**"], "accepts": ["plan"]}]}})
    commit(root, {"src/app.py": "x = 2\n"})
    check = ask(root)
    assert check.verdict == disensor.REQUIRED
    assert check.gates == ("plan",)


def test_paths_without_a_common_gate_are_blocked(tmp_path):
    root = make_repo(tmp_path, {**BASIC, "gate": {"scope": [
        {"paths": ["src/**"], "accepts": ["diff"]},
        {"paths": ["docs/**"], "accepts": ["plan"]},
        {"paths": ["**"], "accepts": ["diff"]},
    ]}})
    commit(root, {"src/app.py": "x = 2\n", "docs/nota.md": "otra\n"})
    assert ask(root).verdict == disensor.BLOCKED


def test_coverage_turned_off_needs_no_round(tmp_path):
    root = make_repo(tmp_path, {**BASIC, "gate": {"required": False}})
    commit(root, {"src/app.py": "x = 2\n"})
    assert ask(root).verdict == disensor.NOT_NEEDED


def test_an_unknown_base_is_not_a_verdict(tmp_path):
    """Could not ask is not the same as no round needed: the hook logs it and
    stays quiet, instead of reading it as permission."""
    root = make_repo(tmp_path, BASIC)
    head = commit(root, {"src/app.py": "x = 2\n"})
    check = disensor.check_requirement(EXE, root, "0" * 40, head)
    assert check.verdict == disensor.UNAVAILABLE


def test_the_check_runs_on_a_dirty_tree_and_touches_nothing(tmp_path):
    """The hook asks while the agent's work may still be uncommitted, so
    `--check` has to answer on a dirty tree and leave it exactly as it was."""
    root = make_repo(tmp_path, BASIC)
    commit(root, {"src/app.py": "x = 2\n"})
    (root / "src" / "app.py").write_text("x = 3\n", encoding="utf-8")
    (root / "nuevo.txt").write_text("sin trackear\n", encoding="utf-8")
    before = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    assert ask(root).verdict == disensor.REQUIRED
    assert git(root, "status", "--porcelain=v1", "--untracked-files=all") == before


# --- lo que el hook le pasa a disensor no puede apuntar al repositorio -----

def _plant_disensor(root: Path, mark: Path) -> None:
    """A `disensor` package in the checkout that leaves a mark when imported."""
    paquete = root / "disensor"
    paquete.mkdir()
    (paquete / "__init__.py").write_text(f"open(r'{mark}', 'w').close()\n", encoding="utf-8")
    (paquete / "cli.py").write_text("def main():\n    raise SystemExit(0)\n", encoding="utf-8")


def test_without_protection_a_relative_pythonpath_runs_the_checkouts_disensor(tmp_path):
    """Codex, third round: the hook's own interpreter ignores PYTHONPATH, but
    disensor's launcher does not, and it used to inherit it. Proven here first."""
    root = make_repo(tmp_path, BASIC)
    commit(root, {"src/app.py": "x = 2\n"})
    mark = tmp_path / "importado.txt"
    _plant_disensor(root, mark)
    subprocess.run([str(EXE), "round", "--help"], cwd=root, capture_output=True, check=False,
                   env={**os.environ, "PYTHONPATH": "."})
    assert mark.exists()


def test_the_check_never_imports_the_checkouts_disensor(tmp_path, monkeypatch):
    root = make_repo(tmp_path, BASIC)
    commit(root, {"src/app.py": "x = 2\n"})
    mark = tmp_path / "importado.txt"
    _plant_disensor(root, mark)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "planta un disensor")
    monkeypatch.setenv("PYTHONPATH", ".")
    assert ask(root).verdict == disensor.REQUIRED
    assert not mark.exists(), "disensor imported the package planted in the checkout"


def test_the_check_never_runs_the_checkouts_git(tmp_path, monkeypatch):
    """Codex, third round: a `.` in PATH survives NoDefaultCurrentDirectory,
    and disensor calls git by bare name. The planted git.exe is a copy of
    whoami, so if disensor ran it the check could not answer."""
    import sys

    if sys.platform != "win32":
        pytest.skip("the planted .exe is a Windows scenario")
    whoami = shutil.which("whoami", path=os.path.join(os.environ["SystemRoot"], "System32"))
    if not whoami:
        pytest.skip("no whoami.exe to plant")
    root = make_repo(tmp_path, BASIC)
    commit(root, {"src/app.py": "x = 2\n"})
    # Los OIDs antes de plantar: los helpers de este test llaman a git por
    # nombre pelado desde el repositorio, y despues ejecutarian el plantado.
    base = git(root, "rev-parse", "origin/main")
    head = git(root, "rev-parse", "HEAD")
    shutil.copy(whoami, root / "git.exe")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setenv("PATH", "." + os.pathsep + os.environ["PATH"])

    def unprotected():
        return subprocess.run(
            [str(EXE), "round", "--gate", "diff", "--generator-family", "other",
             "--base", base, "--head", head, "--check"],
            cwd=root, capture_output=True, check=False, env=dict(os.environ),
        )

    # Codex, cuarta ronda: que falle no prueba por si solo que corrio el
    # plantado. El control es la misma invocacion, mismo entorno, sin el
    # archivo: si esa responde, lo unico que cambio es el git.exe.
    sin_proteccion = unprotected()
    (root / "git.exe").unlink()
    control = unprotected()
    shutil.copy(whoami, root / "git.exe")
    assert control.returncode == disensor.OK, "the control: the same call answers without the plant"
    assert disensor.check_requirement(EXE, root, base, head).verdict == disensor.REQUIRED
    # disensor 0.11 resuelve git por su cuenta fuera del directorio de trabajo
    # (su #75), y entonces el plantado no corre ni sin la proteccion del
    # adaptador. La premisa solo se prueba contra un disensor que todavia lo corre.
    if sin_proteccion.returncode == disensor.OK:
        pytest.skip("this disensor no longer runs a git from the working directory: the premise is gone")
    assert sin_proteccion.returncode == disensor.ERROR, "the premise: disensor ran the planted git"


def _plant_pth(root: Path, mark: Path) -> None:
    """A .pth in every user site-packages PYTHONUSERBASE=. could point at.

    The interpreter behind the disensor launcher is not known from here, so
    every version this adapter supports gets one.
    """
    line = f"import os; open(r'{mark}', 'w').close()\n"
    for minor in range(10, 16):
        for relative in (f"Python3{minor}/site-packages", f"lib/python3.{minor}/site-packages"):
            directory = root / relative
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "ataque.pth").write_text(line, encoding="utf-8")


def test_the_check_never_runs_a_pth_from_the_checkout(tmp_path, monkeypatch):
    """Codex, fourth round: the launcher runs Python with its user site on, and
    PYTHONUSERBASE=. puts that site inside the checkout, where site.py runs
    the .pth files it finds before disensor is even imported."""
    root = make_repo(tmp_path, BASIC)
    commit(root, {"src/app.py": "x = 2\n"})
    mark = tmp_path / "pth.txt"
    _plant_pth(root, mark)
    subprocess.run([str(EXE), "round", "--help"], cwd=root, capture_output=True, check=False,
                   env={**os.environ, "PYTHONUSERBASE": "."})
    if not mark.exists():
        pytest.skip("this disensor runs without a user site-packages (from a venv): no attack to stop")
    mark.unlink()

    monkeypatch.setenv("PYTHONUSERBASE", ".")
    assert ask(root).verdict == disensor.REQUIRED
    assert not mark.exists(), "disensor's interpreter ran a .pth from the checkout"
