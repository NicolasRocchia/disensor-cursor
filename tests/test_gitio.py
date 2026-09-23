"""git, read the way disensor reads it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import git
from disensor_cursor import gitio


def commit(repo: Path, relative: str, content: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", "--", relative)
    git(repo, "commit", "-qm", f"cambia {relative}")
    return git(repo, "rev-parse", "HEAD")


def declaration(repo: Path, reviewed: str | None, *, gate: str = "diff", base: str | None = None) -> str:
    """Only the fields the witness reads: the gate, what was reviewed, and the
    base a diff review names, which by default is the fixture's merge base."""
    event = {"gate": gate, "base_commit": base or git(repo, "rev-parse", "origin/main")}
    if reviewed is not None:
        event["head_commit"] = reviewed
    return json.dumps({"schema": "residue/v0.4", "event": event}) + "\n"


def coverage(repo: Path, accepted_gates=None) -> str:
    base = git(repo, "rev-parse", "origin/main")
    head = git(repo, "rev-parse", "HEAD")
    return gitio.declaration_coverage(base, head, repo, ".residue", accepted_gates)


# --- rutas sucias ---------------------------------------------------------

def test_dirty_paths_lists_every_kind_of_change(repo):
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "con espacios.txt").write_text("a\n", encoding="utf-8")
    (repo / "nueva" / "honda").mkdir(parents=True)
    (repo / "nueva" / "honda" / "f.txt").write_text("b\n", encoding="utf-8")
    assert gitio.dirty_paths(repo) == {"src/app.py", "con espacios.txt", "nueva/honda/f.txt"}


def test_a_rename_is_two_plain_paths_and_not_a_pair_to_guess(repo):
    git(repo, "mv", "src/app.py", "src/renombrado.py")
    assert gitio.dirty_paths(repo) == {"src/app.py", "src/renombrado.py"}


def test_a_clean_tree_has_no_dirty_paths(repo):
    assert gitio.dirty_paths(repo) == set()
    assert gitio.is_dirty(repo) is False


def test_outside_a_repository_git_does_not_answer(tmp_path):
    assert gitio.dirty_paths(tmp_path) is None
    assert gitio.is_dirty(tmp_path) is None


# --- cobertura: el testigo de G7 ------------------------------------------

def test_no_declaration_in_the_range(repo):
    commit(repo, "src/app.py", "x = 2\n")
    assert coverage(repo) == gitio.NO_DECLARATION


def test_a_declaration_of_the_final_code_covers(repo):
    revisado = commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/uno.json", declaration(repo, revisado))
    assert coverage(repo) == gitio.COVERED


def test_code_changed_after_the_reviewed_commit_makes_it_stale(repo):
    revisado = commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/uno.json", declaration(repo, revisado))
    commit(repo, "src/app.py", "x = 3\n")
    assert coverage(repo) == gitio.STALE


def test_a_second_declaration_after_the_fix_covers_again(repo):
    """The normal end of a round with findings: the last declaration is the
    witness, the earlier one simply stops mattering."""
    primero = commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/uno.json", declaration(repo, primero))
    arreglo = commit(repo, "src/app.py", "x = 3\n")
    commit(repo, ".residue/dos.json", declaration(repo, arreglo))
    assert coverage(repo) == gitio.COVERED


def test_an_unreadable_declaration_covers_nothing(repo):
    commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/rota.json", "{esto no es json\n")
    assert coverage(repo) == gitio.STALE


def test_a_reviewed_commit_that_does_not_exist_covers_nothing(repo):
    commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/uno.json", declaration(repo, "0" * 40))
    assert coverage(repo) == gitio.STALE


def test_a_review_from_before_a_rebase_covers_nothing(repo):
    """Same content, another commit: the review happened, then a rebase gave
    the code new ids. The gate rejects it (G5: the reviewed commit has to be
    an ancestor of the head), even though a plain diff between the two trees
    is empty. The first version of this test used a lateral commit with other
    content, and passed for that reason alone."""
    git(repo, "checkout", "-q", "-b", "antes-del-rebase")
    revisado = commit(repo, "src/app.py", "x = 2\n")
    git(repo, "checkout", "-q", "-")
    # Otro mensaje a proposito: mismo padre, arbol, autor y mensaje en el mismo
    # segundo darian el MISMO commit, y el test dependeria del reloj.
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    git(repo, "add", "--", "src/app.py")
    git(repo, "commit", "-qm", "el mismo cambio, reescrito por el rebase")
    reescrito = git(repo, "rev-parse", "HEAD")
    assert revisado != reescrito
    assert git(repo, "diff", "--name-only", revisado, reescrito) == ""
    commit(repo, ".residue/uno.json", declaration(repo, revisado))
    assert coverage(repo) == gitio.STALE


def test_a_nested_file_under_the_evidence_root_is_not_a_declaration(repo):
    """The gate only reads immediate .json files there; anything nested is
    ordinary content. Reading it as evidence would let it cover code."""
    revisado = commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/anidada/uno.json", declaration(repo, revisado))
    assert coverage(repo) == gitio.NO_DECLARATION


def test_a_fresh_declaration_only_needs_evidence_changes_after_it(repo):
    """Adding the declaration itself changes a path, under the evidence root.
    That change must not count as code changed after the review."""
    revisado = commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/uno.json", declaration(repo, revisado))
    commit(repo, ".residue/dos.json", declaration(repo, revisado))
    assert coverage(repo) == gitio.COVERED


def test_a_nested_file_added_after_the_declaration_is_code(repo):
    """The gate classifies anything under the evidence root that is not an
    immediate .json as ordinary content (classify_changes), so it fails G7
    here. A first version skipped everything under the root and stayed quiet:
    the dangerous direction."""
    revisado = commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/uno.json", declaration(repo, revisado))
    commit(repo, ".residue/anidada/payload.py", "print('codigo')\n")
    assert coverage(repo) == gitio.STALE


def test_a_file_put_back_to_its_base_content_does_not_make_it_stale(repo):
    """G7 intersects what changed after the review with the PR's ordinary
    paths. A file that went back to its base content is not in the PR any
    more, so changing it after the review is not a reason to review again. A
    first version counted any later change and announced a rejection that the
    gate would not make."""
    (repo / "src" / "otro.py").write_text("y = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "otro en la base")
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "src" / "otro.py").write_text("y = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "cambia los dos")
    revisado = git(repo, "rev-parse", "HEAD")
    commit(repo, ".residue/uno.json", declaration(repo, revisado))
    commit(repo, "src/otro.py", "y = 1\n")
    assert coverage(repo) == gitio.COVERED


def test_a_declaration_of_a_gate_the_policy_does_not_accept_is_no_witness(repo):
    revisado = commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/uno.json", declaration(repo, revisado, gate="plan"))
    assert coverage(repo, accepted_gates=("diff",)) == gitio.STALE
    assert coverage(repo, accepted_gates=("plan",)) == gitio.COVERED


def test_a_diff_review_has_to_name_the_merge_base(repo):
    """G5: a diff review identifies the pair, not a loose head."""
    otro = commit(repo, "src/app.py", "x = 2\n")
    revisado = commit(repo, "src/app.py", "x = 3\n")
    commit(repo, ".residue/uno.json", declaration(repo, revisado, base=otro))
    assert coverage(repo) == gitio.STALE


def test_a_review_from_before_the_branch_point_is_no_witness(repo):
    """G5: the reviewed commit has to belong to the PR, descending from the
    merge base. An older commit reviewed some other change."""
    anterior = git(repo, "rev-parse", "HEAD")
    commit(repo, "src/base.py", "b = 1\n")
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/uno.json", declaration(repo, anterior))
    assert coverage(repo) == gitio.STALE


def test_the_range_is_measured_from_the_merge_base(repo):
    """When the target also made the same change after the branch point, the
    tip of the target no longer differs from the head on that file. The gate
    measures from the merge base and still sees it; so has to this."""
    rama = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    inicial = git(repo, "rev-parse", "HEAD")
    commit(repo, "src/app.py", "x = 2\n")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "destino", inicial)
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "el destino hace el mismo cambio")
    destino = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", rama)
    assert gitio.changed_paths(destino, head, repo) == set(), "desde la punta no se ve"
    rng = gitio.pr_range(destino, head, repo, ".residue")
    assert rng is not None and rng.merge_base == inicial
    assert "src/app.py" in rng.changed


def test_rewritten_history_is_neither_code_nor_a_declaration(repo):
    """A declaration that already existed at the merge base and changes in the
    PR is a mutation of history (G8), which the gate reports on its own."""
    commit(repo, ".residue/vieja.json", '{"schema": "residue/v0.4"}\n')
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    commit(repo, ".residue/vieja.json", '{"schema": "residue/v0.4", "tocada": true}\n')
    rng = gitio.pr_range(git(repo, "rev-parse", "origin/main"), git(repo, "rev-parse", "HEAD"),
                         repo, ".residue")
    assert rng is not None
    assert ".residue/vieja.json" not in rng.ordinary
    assert ".residue/vieja.json" not in rng.declarations


@pytest.mark.parametrize("raw, canonical", [
    (".residue", ".residue"),
    ("./.residue", ".residue"),
    (".residue/", ".residue"),
    (".\\.residue", ".residue"),
    ("evidencia//interna/./", "evidencia/interna"),
    ("", None),
    (".", None),
    ("..", None),
    ("../afuera", None),
    ("/absoluta", None),
    ("C:\\evidencia", None),
    (":(top)evidencia", None),
])
def test_the_evidence_root_is_read_the_way_the_gate_reads_it(raw, canonical):
    """Same rules as gate.canonical_repo_path. Without them, `./.residue`
    matched no path git reports and every declaration looked absent."""
    assert gitio.canonical_root(raw) == canonical


@pytest.mark.parametrize("spelling", ["./.residue", ".residue/", ".\\.residue"])
def test_an_equivalent_spelling_of_the_root_finds_the_same_declarations(repo, spelling):
    revisado = commit(repo, "src/app.py", "x = 2\n")
    commit(repo, ".residue/uno.json", declaration(repo, revisado))
    base = git(repo, "rev-parse", "origin/main")
    head = git(repo, "rev-parse", "HEAD")
    assert gitio.declaration_coverage(base, head, repo, spelling) == gitio.COVERED


def test_a_directory_replaced_by_a_declaration_is_rewritten_history(repo):
    """Codex, second round: the gate asks whether the PATH existed at the
    merge base, and a directory named like a declaration existed as a tree.
    Listing only blobs made the replacement look like a new declaration."""
    nombre = ".residue/0b1c2d3e.json"
    commit(repo, f"{nombre}/archivo.txt", "adentro\n")
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    revisado = commit(repo, "src/app.py", "x = 2\n")
    git(repo, "rm", "-rq", nombre)
    # git rm se lleva .residue entero si queda vacio.
    (repo / nombre).parent.mkdir(parents=True, exist_ok=True)
    (repo / nombre).write_text(declaration(repo, revisado), encoding="utf-8")
    git(repo, "add", "--", nombre)
    git(repo, "commit", "-qm", "reemplaza el directorio por una declaracion")
    rng = gitio.pr_range(git(repo, "rev-parse", "origin/main"), git(repo, "rev-parse", "HEAD"),
                         repo, ".residue")
    assert rng is not None
    assert nombre not in rng.declarations
    assert coverage(repo) == gitio.NO_DECLARATION


# --- ancestros ------------------------------------------------------------

def test_ancestry(repo):
    uno = git(repo, "rev-parse", "HEAD")
    dos = commit(repo, "src/app.py", "x = 2\n")
    assert gitio.is_ancestor(uno, dos, repo)
    assert not gitio.is_ancestor(dos, uno, repo)
