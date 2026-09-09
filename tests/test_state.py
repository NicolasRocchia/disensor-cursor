"""State on disk: the filename is derived, never received.

`conversation_id` arrives inside the hook payload. Building a path out of it
directly puts a value like `../reviewers` on top of disensor's own reviewer
registry, which is the file that decides which executables get to receive
private code. disensor already defends this shape for reviewer ids; the
adapter cannot be the one that lowers it.
"""
from __future__ import annotations

import json

import pytest

from disensor_cursor import state

HOSTILE = [
    "../reviewers",
    "../../.disensor/reviewers",
    "..\\..\\reviewers",
    "/etc/passwd",
    "C:\\Windows\\System32\\config",
    "CON",
    "NUL",
    "algo:flujo-alternativo",
    "con espacios y / barras",
    "",
]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_a_hostile_conversation_id_cannot_escape_the_state_directory(hostile, isolated_state):
    key = state.session_key(hostile, "/un/worktree")
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    for path in (state.state_path(key), state.edits_path(key)):
        assert path.parent == isolated_state, f"{hostile!r} salio del directorio de estado"


def test_the_same_conversation_in_two_worktrees_is_two_situations():
    """One must not silence the other: they are different trees."""
    assert state.session_key("conv", "/uno") != state.session_key("conv", "/otro")


def test_the_key_is_stable():
    assert state.session_key("conv", "/uno") == state.session_key("conv", "/uno")


def test_an_unreadable_state_degrades_towards_silence(isolated_state):
    """The default is not to engage, so losing the state has to lose the
    permission, never grant one."""
    key = state.session_key("conv", "/uno")
    isolated_state.mkdir(parents=True, exist_ok=True)
    state.state_path(key).write_text("{esto no es json", encoding="utf-8")
    assert state.read_state(key) == {}
    assert state.eligibility(key) == state.UNKNOWN


def test_eligibility_is_ternary_and_unknown_is_the_default(isolated_state):
    key = state.session_key("conv", "/uno")
    assert state.eligibility(key) == state.UNKNOWN
    state.update_state(key, {"eligibility": state.REJECTED})
    assert state.eligibility(key) == state.REJECTED
    state.update_state(key, {"eligibility": state.ADMITTED})
    assert state.eligibility(key) == state.ADMITTED
    state.update_state(key, {"eligibility": "cualquier-cosa"})
    assert state.eligibility(key) == state.UNKNOWN, "un valor raro no puede admitir"


def test_reasons_are_tracked_one_by_one(isolated_state):
    key = state.session_key("conv", "/uno")
    assert not state.already_reported(key, state.DIRTY_TREE)
    state.mark_reported(key, state.DIRTY_TREE)
    assert state.already_reported(key, state.DIRTY_TREE)
    assert not state.already_reported(key, state.ROUND_REQUIRED), (
        "un motivo no puede silenciar a otro"
    )


def test_edits_survive_a_truncated_line(isolated_state):
    """The file is appended to by a process that can be killed at any moment."""
    key = state.session_key("conv", "/uno")
    state.append_edit(key, {"path": "a.py"})
    with open(state.edits_path(key), "a", encoding="utf-8") as handle:
        handle.write('{"path": "b.py"\n')
    state.append_edit(key, {"path": "c.py"})
    assert [r["path"] for r in state.read_edits(key)] == ["a.py", "c.py"]


def test_the_state_write_refuses_a_symlink(isolated_state, monkeypatch):
    from pathlib import Path

    key = state.session_key("conv", "/uno")
    isolated_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)
    with pytest.raises(OSError, match="symlink"):
        state.write_state(key, {"eligibility": state.ADMITTED})


def test_a_concurrent_update_does_not_lose_the_other_writer(isolated_state):
    key = state.session_key("conv", "/uno")
    state.update_state(key, {"uno": 1})
    state.update_state(key, {"otro": 2})
    data = json.loads(state.state_path(key).read_text(encoding="utf-8"))
    assert data == {"uno": 1, "otro": 2}
