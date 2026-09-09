"""The hook: what it says, what it refuses to say, and how it fails.

Every test here asserts the same two invariants underneath whatever it is
actually checking: the hook prints an object, and it exits 0. Cursor reads
exit code 2 as "block the action" regardless of failClosed, so an advisory
integration that can exit 2 is not advisory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import emitted, git
from disensor_cursor import disensor, hook, state


def run(hook_event, payload, repo, monkeypatch, capsys, stdin_payload, **flags):
    stdin_payload(payload)
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(repo))
    argv = ["--event", hook_event]
    for name, value in flags.items():
        argv += [f"--{name}", value]
    assert hook.main(argv) == 0, "the hook never exits with anything but 0"
    return emitted(capsys)


def start(repo, monkeypatch, capsys, stdin_payload, *, mode="agent", background=False,
          conversation="conv-1", model="claude-4.5-sonnet"):
    return run("sessionStart", {
        "conversation_id": conversation,
        "session_id": "ses-1",
        "composer_mode": mode,
        "is_background_agent": background,
        "model": model,
    }, repo, monkeypatch, capsys, stdin_payload)


def stop(repo, monkeypatch, capsys, stdin_payload, *, status="completed", conversation="conv-1"):
    return run("stop", {
        "conversation_id": conversation,
        "status": status,
        "loop_count": 0,
    }, repo, monkeypatch, capsys, stdin_payload)


def edit(repo, monkeypatch, capsys, stdin_payload, relative, *, conversation="conv-1",
         model="claude-4.5-sonnet"):
    return run("afterFileEdit", {
        "conversation_id": conversation,
        "file_path": str(repo / relative),
        "model": model,
        "edits": [{"old_string": "SECRETO", "new_string": "OTRO SECRETO"}],
    }, repo, monkeypatch, capsys, stdin_payload)


def commit_change(repo: Path, relative: str, content: str) -> None:
    (repo / relative).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"cambia {relative}")


@pytest.fixture
def required(monkeypatch):
    """A disensor that answers 'this range needs a round'."""
    monkeypatch.setattr(disensor, "locate", lambda hint, repo=None: Path("disensor-falso"))
    monkeypatch.setattr(
        disensor, "check_requirement", lambda *a, **k: (disensor.REQUIRED, "gates diff")
    )


# --- elegibilidad ---------------------------------------------------------

def test_an_agent_session_is_admitted(repo, monkeypatch, capsys, stdin_payload):
    salida = start(repo, monkeypatch, capsys, stdin_payload)
    assert "disensor" in salida["additional_context"]
    key = state.session_key("conv-1", str(repo))
    assert state.eligibility(key) == state.ADMITTED


@pytest.mark.parametrize("mode", ["ask", "edit"])
def test_a_non_agent_session_is_rejected(repo, monkeypatch, capsys, stdin_payload, mode):
    assert start(repo, monkeypatch, capsys, stdin_payload, mode=mode) == {}
    key = state.session_key("conv-1", str(repo))
    assert state.eligibility(key) == state.REJECTED


def test_a_background_agent_is_rejected_and_never_gets_a_followup(
    repo, monkeypatch, capsys, stdin_payload, required
):
    """The worst possible failure of this integration is a background agent
    stuck on a follow-up with nobody watching."""
    start(repo, monkeypatch, capsys, stdin_payload, background=True)
    commit_change(repo, "src/app.py", "x = 2\n")
    assert stop(repo, monkeypatch, capsys, stdin_payload) == {}


def test_without_a_session_start_nothing_happens(repo, monkeypatch, capsys, stdin_payload, required):
    """Cursor does not support sessionStart on cloud agents, so absence of the
    mark is the normal state there and it has to be the quiet one."""
    key = state.session_key("conv-1", str(repo))
    assert state.eligibility(key) == state.UNKNOWN
    commit_change(repo, "src/app.py", "x = 2\n")
    assert stop(repo, monkeypatch, capsys, stdin_payload) == {}


def test_a_repository_that_never_adopted_disensor_is_left_alone(
    tmp_path, monkeypatch, capsys, stdin_payload
):
    """Without a configuration the gate applies strict defaults, which is right
    for a gate and would be nagging here."""
    plain = tmp_path / "ajeno"
    plain.mkdir()
    git(plain.parent, "init", "-q", str(plain))
    git(plain, "config", "user.email", "s@e.test")
    git(plain, "config", "user.name", "s")
    (plain / "a.txt").write_text("hola\n", encoding="utf-8")
    git(plain, "add", "-A")
    git(plain, "commit", "-qm", "inicial")
    start(plain, monkeypatch, capsys, stdin_payload)
    assert state.eligibility(state.session_key("conv-1", str(plain))) == state.REJECTED


# --- el hook stop ---------------------------------------------------------

@pytest.mark.parametrize("status", ["aborted", "error"])
def test_an_interrupted_turn_is_not_nagged(repo, monkeypatch, capsys, stdin_payload, status):
    start(repo, monkeypatch, capsys, stdin_payload)
    assert stop(repo, monkeypatch, capsys, stdin_payload, status=status) == {}


def test_a_dirty_tree_asks_the_user_instead_of_ordering_a_commit(
    repo, monkeypatch, capsys, stdin_payload
):
    """Cursor injects the follow-up AS THE USER. An agent can read an order as
    authorisation, and committing was never authorised."""
    start(repo, monkeypatch, capsys, stdin_payload)
    (repo / "sin_commitear.txt").write_text("pendiente\n", encoding="utf-8")
    mensaje = stop(repo, monkeypatch, capsys, stdin_payload)["followup_message"]
    assert "Automatic notice" in mensaje
    assert "not a request from the user" in mensaje
    assert "Do not commit on your own initiative" in mensaje
    assert "Ask the user" in mensaje


def test_no_review_required_says_nothing(repo, monkeypatch, capsys, stdin_payload):
    monkeypatch.setattr(disensor, "locate", lambda hint, repo=None: Path("x"))
    monkeypatch.setattr(disensor, "check_requirement", lambda *a, **k: (disensor.NOT_NEEDED, ""))
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    assert stop(repo, monkeypatch, capsys, stdin_payload) == {}


def test_an_undecidable_range_is_reported_not_waved_through(
    repo, monkeypatch, capsys, stdin_payload
):
    monkeypatch.setattr(disensor, "locate", lambda hint, repo=None: Path("x"))
    monkeypatch.setattr(
        disensor, "check_requirement", lambda *a, **k: (disensor.BLOCKED, "no common gate")
    )
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py")
    mensaje = stop(repo, monkeypatch, capsys, stdin_payload)["followup_message"]
    assert "no common gate" in mensaje
    assert "not treat it as permission to skip" in mensaje


def test_a_missing_disensor_stays_quiet_and_leaves_a_reason(
    repo, monkeypatch, capsys, stdin_payload
):
    monkeypatch.setattr(disensor, "locate", lambda hint, repo=None: None)
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    assert stop(repo, monkeypatch, capsys, stdin_payload) == {}
    assert "disensor not found" in state.log_path().read_text(encoding="utf-8")


def test_the_followup_carries_oids_and_not_moving_references(
    repo, monkeypatch, capsys, stdin_payload, required
):
    """Two composers share one tree. `HEAD` at the time the notice is read is
    not necessarily what the notice was about."""
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py")
    mensaje = stop(repo, monkeypatch, capsys, stdin_payload)["followup_message"]
    head = git(repo, "rev-parse", "HEAD")
    assert f"--head {head}" in mensaje
    assert "--head HEAD" not in mensaje
    assert "origin/main" not in mensaje


def test_it_asks_before_sending_material_to_a_reviewer(
    repo, monkeypatch, capsys, stdin_payload, required
):
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py")
    mensaje = stop(repo, monkeypatch, capsys, stdin_payload)["followup_message"]
    assert "owner's decision and not yours" in mensaje
    assert "Ask before running anything" in mensaje


# --- terminacion y ruido --------------------------------------------------

def test_the_same_reason_is_reported_once(repo, monkeypatch, capsys, stdin_payload, required):
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py")
    assert "followup_message" in stop(repo, monkeypatch, capsys, stdin_payload)
    assert stop(repo, monkeypatch, capsys, stdin_payload) == {}


def test_a_new_reason_is_still_reported(repo, monkeypatch, capsys, stdin_payload, required):
    """A single counter for the whole conversation gets spent by the first
    notice, and then the dirty tree silences the round that mattered."""
    start(repo, monkeypatch, capsys, stdin_payload)
    (repo / "pendiente.txt").write_text("algo\n", encoding="utf-8")
    primero = stop(repo, monkeypatch, capsys, stdin_payload)["followup_message"]
    assert "uncommitted changes" in primero

    commit_change(repo, "src/app.py", "x = 2\n")
    (repo / "pendiente.txt").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "limpia")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py")
    segundo = stop(repo, monkeypatch, capsys, stdin_payload)["followup_message"]
    assert "asks for an adversarial review" in segundo


def test_work_this_session_did_not_touch_is_not_nagged_about(
    repo, monkeypatch, capsys, stdin_payload, required
):
    """`round --check` measures the whole branch, not the session. Without this
    filter a branch carrying old undeclared work would nag on every session."""
    commit_change(repo, "src/viejo.py", "y = 1\n")
    start(repo, monkeypatch, capsys, stdin_payload)
    assert stop(repo, monkeypatch, capsys, stdin_payload) == {}


def test_a_declaration_already_in_the_range_stops_the_nagging(
    repo, monkeypatch, capsys, stdin_payload, required
):
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    (repo / ".residue").mkdir()
    commit_change(repo, ".residue/algo.json", '{"schema": "residue/v0.4"}\n')
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py")
    assert stop(repo, monkeypatch, capsys, stdin_payload) == {}


# --- prellenado del generador ---------------------------------------------

def test_a_known_model_prefills_family_and_model_together(
    repo, monkeypatch, capsys, stdin_payload, required
):
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py", model="claude-4.5-sonnet")
    mensaje = stop(repo, monkeypatch, capsys, stdin_payload)["followup_message"]
    assert "--generator-family anthropic" in mensaje
    assert "--generator-model claude-4.5-sonnet" in mensaje


def test_an_unknown_slug_prefills_nothing(repo, monkeypatch, capsys, stdin_payload, required):
    """Guessing a family would make R4 certify a decorrelation that never was."""
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py", model="composer-2.5")
    mensaje = stop(repo, monkeypatch, capsys, stdin_payload)["followup_message"]
    assert "--generator-family" not in mensaje.split("The command is incomplete")[0]
    assert "not in the family table" in mensaje
    assert "The command is incomplete" in mensaje


def test_two_models_in_one_session_prefill_nothing(
    repo, monkeypatch, capsys, stdin_payload, required
):
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py", model="claude-4.5-sonnet")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py", model="gpt-5.4")
    mensaje = stop(repo, monkeypatch, capsys, stdin_payload)["followup_message"]
    assert "More than one model" in mensaje


# --- evidencia ------------------------------------------------------------

def test_the_edit_log_keeps_metadata_and_never_the_code(
    repo, monkeypatch, capsys, stdin_payload
):
    """old_string and new_string would retain source, and any secret in it,
    outside the repository with no expiry and no purpose."""
    start(repo, monkeypatch, capsys, stdin_payload)
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py")
    key = state.session_key("conv-1", str(repo))
    crudo = state.edits_path(key).read_text(encoding="utf-8")
    assert "SECRETO" not in crudo
    registro = state.read_edits(key)[0]
    assert registro["path"] == "src/app.py"
    assert set(registro) == {"ts", "path", "model", "model_id"}


# --- robustez -------------------------------------------------------------

def test_an_exception_inside_the_hook_is_invisible_to_cursor(
    repo, monkeypatch, capsys, stdin_payload
):
    def explode(*a, **k):
        raise RuntimeError("algo se rompio adentro")

    monkeypatch.setitem(hook.HANDLERS, "stop", explode)
    stdin_payload({"conversation_id": "conv-1", "status": "completed"})
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(repo))
    assert hook.main(["--event", "stop"]) == 0
    assert emitted(capsys) == {}
    assert "algo se rompio adentro" in state.log_path().read_text(encoding="utf-8")


@pytest.mark.parametrize("argv", [
    [],
    ["--event"],
    ["--event", "inventado"],
    ["--flag-que-no-existe", "valor"],
    ["-x"],
])
def test_bad_arguments_never_produce_the_exit_code_that_blocks(
    argv, monkeypatch, capsys, stdin_payload
):
    """argparse exits with 2 on an unknown flag, and Cursor reads 2 as block."""
    stdin_payload({})
    assert hook.main(argv) == 0
    assert emitted(capsys) == {}


def test_a_payload_with_a_byte_order_mark_is_read(repo, monkeypatch, capsys):
    """Cursor sends the payload WITH a BOM on Windows, and json.loads rejects
    it. Found by running it, not by reading the documentation, which does not
    mention it. Before the fix every hook silently did nothing."""
    import io
    import sys as system

    crudo = json.dumps({
        "conversation_id": "conv-bom",
        "session_id": "s",
        "composer_mode": "agent",
        "is_background_agent": False,
    }).encode("utf-8-sig")
    assert crudo.startswith(b"\xef\xbb\xbf"), "el vector tiene que llevar BOM"

    class Fake:
        buffer = io.BytesIO(crudo)

    monkeypatch.setattr(system, "stdin", Fake())
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(repo))
    assert hook.main(["--event", "sessionStart"]) == 0
    assert "additional_context" in emitted(capsys)
    assert state.eligibility(state.session_key("conv-bom", str(repo))) == state.ADMITTED


def test_a_payload_that_is_not_json_is_survivable_and_leaves_a_reason(monkeypatch, capsys):
    """Quiet is right; quiet for an unexplained reason is not. From outside,
    `{}` because the JSON was malformed looks like `{}` because there was
    nothing to say."""
    import io
    import sys as system

    class Fake:
        buffer = io.BytesIO(b"esto no es json")

    monkeypatch.setattr(system, "stdin", Fake())
    assert hook.main(["--event", "stop"]) == 0
    assert emitted(capsys) == {}
    assert "not valid JSON" in state.log_path().read_text(encoding="utf-8")


def test_a_payload_that_is_not_an_object_is_recorded(monkeypatch, capsys):
    import io
    import sys as system

    class Fake:
        buffer = io.BytesIO(b'["una", "lista"]')

    monkeypatch.setattr(system, "stdin", Fake())
    assert hook.main(["--event", "stop"]) == 0
    assert emitted(capsys) == {}
    assert "not an object" in state.log_path().read_text(encoding="utf-8")


def test_the_notice_is_marked_only_after_it_is_emitted(
    repo, monkeypatch, capsys, stdin_payload, required
):
    """Marking first would let a process killed in between burn a notice
    nobody read. Repeating one is the harmless failure."""
    start(repo, monkeypatch, capsys, stdin_payload)
    commit_change(repo, "src/app.py", "x = 2\n")
    edit(repo, monkeypatch, capsys, stdin_payload, "src/app.py")
    key = state.session_key("conv-1", str(repo))

    stdin_payload({"conversation_id": "conv-1", "status": "completed"})
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(repo))
    resultado, commit = hook._stop(
        {"conversation_id": "conv-1", "status": "completed"}, {}
    )
    assert "followup_message" in resultado
    assert not state.already_reported(key, state.ROUND_REQUIRED), (
        "todavia no se emitio: marcarlo aca perderia el aviso si el proceso muere"
    )
    commit()
    assert state.already_reported(key, state.ROUND_REQUIRED)
