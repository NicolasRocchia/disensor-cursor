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
