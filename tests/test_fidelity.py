"""The coverage witness, checked against disensor's gate itself.

Reimplementing G7 from reading gate.py is a claim. Running both on the same
repositories is evidence. This file imports the gate as an oracle, which the
adapter never does at runtime, and runs only where disensor is importable.

Declarations here are not schema-valid on purpose: validation is the gate's
job and the adapter says it does not do it. What is compared is exactly what
the adapter reimplements, the range checks of G5 and the coverage of G6 and G7.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import git
from disensor_cursor import gitio

gate = pytest.importorskip("disensor.gate")
gitctx = pytest.importorskip("disensor.gitctx")


def commit(repo: Path, changes: dict[str, str], message: str = "cambio") -> str:
    for relative, content in changes.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(repo, "add", "--", relative)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def declare(repo: Path, reviewed: str, name: str = "uno", *, gate_name: str = "diff",
            base: str | None = None) -> str:
    event = {"gate": gate_name, "head_commit": reviewed,
             "base_commit": base or git(repo, "rev-parse", "origin/main")}
    return commit(repo, {f".residue/{name}.json": json.dumps({"event": event}) + "\n"}, "declara")


def gate_says_covered(repo: Path) -> bool:
    """G5 per declaration, then G1, G6 and G7, with the gate's own functions."""
    ctx = gate.resolve_context(".residue", "disensor.config.json", "origin/main", "HEAD", repo)
    artifacts = []
    for path in ctx.new_paths:
        data = json.loads(gitctx.show_text(ctx.head_oid, path, repo))
        artifact = gate.Artifact(path=path, data=data, errors=[])
        gate.check_range_membership(artifact, ctx.merge_base, ctx.head_oid, repo)
        if artifact.range_reason:
            artifact.errors.append(artifact.range_reason)
        artifacts.append(artifact)
    errors, _, demanding = gate.evaluate_coverage(
        artifacts, ctx.ordinary, ctx.config, ctx.evidence_root, ctx.config_path,
    )
    assert demanding, "every scenario here changes code: the gate has to demand a review"
    return not errors


def adapter_says_covered(repo: Path) -> bool:
    verdict = gitio.declaration_coverage(
        git(repo, "rev-parse", "origin/main"), git(repo, "rev-parse", "HEAD"),
        repo, ".residue", ("diff",),
    )
    return verdict == gitio.COVERED


def scenario_final_code_reviewed(repo):
    declare(repo, commit(repo, {"src/app.py": "x = 2\n"}))
    return True


def scenario_fix_after_the_declaration(repo):
    declare(repo, commit(repo, {"src/app.py": "x = 2\n"}))
    commit(repo, {"src/app.py": "x = 3\n"})
    return False


def scenario_second_round_after_the_fix(repo):
    declare(repo, commit(repo, {"src/app.py": "x = 2\n"}), "uno")
    declare(repo, commit(repo, {"src/app.py": "x = 3\n"}), "dos")
    return True


def scenario_nested_file_after_the_declaration(repo):
    """Codex, first round: the adapter skipped everything under the root."""
    declare(repo, commit(repo, {"src/app.py": "x = 2\n"}))
    commit(repo, {".residue/anidada/payload.py": "print('codigo')\n"})
    return False


def scenario_file_put_back_to_its_base_content(repo):
    """Codex, first round: the adapter counted any later change."""
    commit(repo, {"src/otro.py": "y = 1\n"}, "otro en la base")
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    revisado = commit(repo, {"src/app.py": "x = 2\n", "src/otro.py": "y = 2\n"})
    declare(repo, revisado)
    commit(repo, {"src/otro.py": "y = 1\n"})
    return True


def scenario_review_from_before_a_rebase(repo):
    git(repo, "checkout", "-q", "-b", "antes-del-rebase")
    revisado = commit(repo, {"src/app.py": "x = 2\n"}, "antes")
    git(repo, "checkout", "-q", "-")
    commit(repo, {"src/app.py": "x = 2\n"}, "despues del rebase")
    declare(repo, revisado)
    return False


def scenario_diff_review_naming_another_base(repo):
    otro = commit(repo, {"src/app.py": "x = 2\n"})
    declare(repo, commit(repo, {"src/app.py": "x = 3\n"}), base=otro)
    return False


def scenario_plan_declaration_for_a_diff_policy(repo):
    declare(repo, commit(repo, {"src/app.py": "x = 2\n"}), gate_name="plan")
    return False


def scenario_review_from_before_the_branch_point(repo):
    anterior = git(repo, "rev-parse", "HEAD")
    commit(repo, {"src/base.py": "b = 1\n"})
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    commit(repo, {"src/app.py": "x = 2\n"})
    declare(repo, anterior)
    return False


def scenario_exempt_path_changed_after_the_review(repo):
    """G7 intersects with every ordinary path, exempt ones included: a docs
    change after the review leaves no witness even though docs demand nothing."""
    config = {"criticality_level": "B", "level_A_enabled": False, "gate": {"scope": [
        {"paths": ["docs/**"], "accepts": []},
        {"paths": ["**"], "accepts": ["diff"]},
    ]}}
    commit(repo, {"disensor.config.json": json.dumps(config) + "\n", "docs/n.md": "a\n"}, "politica")
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    declare(repo, commit(repo, {"src/app.py": "x = 2\n", "docs/n.md": "b\n"}))
    commit(repo, {"docs/n.md": "c\n"})
    return False


def scenario_directory_replaced_by_a_declaration(repo):
    """Codex, second round: the adapter listed only blobs, so a directory named
    like a declaration looked absent at the merge base."""
    nombre = ".residue/0b1c2d3e.json"
    commit(repo, {f"{nombre}/archivo.txt": "adentro\n"}, "directorio con nombre de declaracion")
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    revisado = commit(repo, {"src/app.py": "x = 2\n"})
    git(repo, "rm", "-rq", nombre)
    event = {"gate": "diff", "head_commit": revisado, "base_commit": git(repo, "rev-parse", "origin/main")}
    commit(repo, {nombre: json.dumps({"event": event}) + "\n"}, "lo reemplaza")
    return False


SCENARIOS = [
    scenario_directory_replaced_by_a_declaration,
    scenario_final_code_reviewed,
    scenario_fix_after_the_declaration,
    scenario_second_round_after_the_fix,
    scenario_nested_file_after_the_declaration,
    scenario_file_put_back_to_its_base_content,
    scenario_review_from_before_a_rebase,
    scenario_diff_review_naming_another_base,
    scenario_plan_declaration_for_a_diff_policy,
    scenario_review_from_before_the_branch_point,
    scenario_exempt_path_changed_after_the_review,
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__[len("scenario_"):])
def test_the_adapter_agrees_with_the_gate(repo, scenario):
    expected = scenario(repo)
    assert gate_says_covered(repo) is expected, "the scenario does not describe what the gate does"
    assert adapter_says_covered(repo) is expected
