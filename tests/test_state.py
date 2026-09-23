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


def test_the_state_write_refuses_a_symlinked_file(isolated_state, monkeypatch):
    """Only the file is a link here; the directory is real. A linked directory
    is refused earlier, and that has its own test."""
    from pathlib import Path

    key = state.session_key("conv", "/uno")
    isolated_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name.startswith("state-"))
    assert state.usable()
    with pytest.raises(OSError, match="symlink"):
        state.write_state(key, {"eligibility": state.ADMITTED})


def test_a_notice_is_identified_by_its_reason_and_its_head():
    assert state.token(state.ROUND_REQUIRED, "a" * 40) != state.token(state.ROUND_REQUIRED, "b" * 40)
    assert state.token(state.ROUND_REQUIRED, "a" * 40) != state.token(state.DIRTY_TREE, "a" * 40)


def test_old_sessions_are_purged_and_nothing_else_is(isolated_state):
    """Without an expiry this directory kept, forever, the list of files every
    conversation touched."""
    import os
    import time

    isolated_state.mkdir(parents=True, exist_ok=True)
    viejo = time.time() - 40 * 86400
    old = [
        isolated_state / "state-aaa.json",
        isolated_state / "edits-aaa.jsonl",
        isolated_state / "state-aaa.json.lock",
    ]
    fresh = [isolated_state / "state-bbb.json", isolated_state / "edits-bbb.jsonl"]
    foreign = isolated_state / "notas-mias.txt"
    for path in old + fresh + [foreign]:
        path.write_text("x", encoding="utf-8")
    for path in old + [foreign]:
        os.utime(path, (viejo, viejo))

    assert state.purge_stale() == len(old)
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in fresh)
    assert foreign.exists(), "only our own names are ever deleted"


def test_the_purge_is_bounded(isolated_state):
    import os
    import time

    isolated_state.mkdir(parents=True, exist_ok=True)
    viejo = time.time() - 40 * 86400
    for i in range(7):
        path = isolated_state / f"state-{i}.json"
        path.write_text("x", encoding="utf-8")
        os.utime(path, (viejo, viejo))
    assert state.purge_stale(limit=3) == 3


def test_updates_merge_instead_of_replacing(isolated_state):
    """Sequential, and named for what it checks. An earlier version called
    itself concurrent and ran one write after the other."""
    key = state.session_key("conv", "/uno")
    state.update_state(key, {"uno": 1})
    state.update_state(key, {"otro": 2})
    data = json.loads(state.state_path(key).read_text(encoding="utf-8"))
    assert data == {"uno": 1, "otro": 2}


def test_concurrent_writers_do_not_lose_each_others_notices(tmp_path):
    """Real processes, at the same time: two stop hooks can race.

    This test found a real bug: on Windows a rename onto a file fails for a
    moment while another process holds it, and every writer shared one
    temporary name. What it does not claim is more than the design gives: a
    writer that waits past the lock's timeout proceeds without it, and the
    worst that can do is lose a mark, which means one repeated notice. Under
    heavy load that can happen, so the contention here is realistic, a few
    processes, not a stress test of a guarantee the lock never made."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "hogar"
    home.mkdir()
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "USERPROFILE": str(home),
           "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    key = "k" * 64
    code = f"import sys; from disensor_cursor import state; state.mark_reported('{key}', sys.argv[1])"
    procesos = [subprocess.Popen([sys.executable, "-c", code, f"t{i}"], env=env) for i in range(6)]
    assert all(p.wait(timeout=120) == 0 for p in procesos)
    data = json.loads((home / ".disensor-cursor" / f"state-{key}.json").read_text(encoding="utf-8"))
    assert set(data["reported"]) == {f"t{i}" for i in range(6)}


def test_the_first_writer_takes_the_lock_before_the_directory_exists(isolated_state):
    """The lock was tried before the state directory existed. On the very
    first write the O_EXCL failed with FileNotFoundError and the writer went
    on without the lock, so the concurrent test above lost a notice about one
    run in eight. Found while splitting the commits."""
    assert not isolated_state.exists()
    with state._Lock(state.state_path("k" * 64)) as lock:
        assert lock.held


def test_a_lock_left_by_a_dead_process_is_reclaimed(isolated_state):
    """Without this, every hook waited the full timeout for thirty days after
    one crash: exactly what Cursor gives sessionStart."""
    import os
    import time

    key = state.session_key("conv", "/uno")
    isolated_state.mkdir(parents=True, exist_ok=True)
    lock = state.state_path(key).with_suffix(".json.lock")
    lock.write_text("", encoding="utf-8")
    viejo = time.time() - 60
    os.utime(lock, (viejo, viejo))
    antes = time.monotonic()
    state.update_state(key, {"a": 1})
    assert time.monotonic() - antes < 1.0, "a stale lock made the writer wait"
    assert not lock.exists()
    assert state.read_state(key) == {"a": 1}


def test_a_live_lock_is_waited_for_and_never_stolen(isolated_state, monkeypatch):
    import time

    monkeypatch.setattr(state, "LOCK_TIMEOUT", 0.3)
    key = state.session_key("conv", "/uno")
    isolated_state.mkdir(parents=True, exist_ok=True)
    lock = state.state_path(key).with_suffix(".json.lock")
    lock.write_text("", encoding="utf-8")
    antes = time.monotonic()
    state.update_state(key, {"a": 1})
    assert time.monotonic() - antes >= 0.25
    assert lock.exists(), "a lock somebody may still hold was removed"


def _junction(link, target) -> bool:
    import subprocess

    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, check=False)
    return done.returncode == 0


def test_the_purge_does_not_walk_through_a_junction(tmp_path, monkeypatch):
    """`is_symlink()` is False for a Windows junction, so an earlier check let
    the purge glob the junction's target and delete files there."""
    import os
    import sys
    import time

    if sys.platform != "win32":
        pytest.skip("junctions are a Windows thing")
    destino = tmp_path / "destino"
    destino.mkdir()
    ajeno = destino / "state-backup.json"
    ajeno.write_text("{}", encoding="utf-8")
    viejo = time.time() - 40 * 86400
    os.utime(ajeno, (viejo, viejo))
    enlace = tmp_path / "estado"
    if not _junction(enlace, destino):
        pytest.skip("could not create a junction here")
    assert not enlace.is_symlink(), "the premise: Python does not call it a symlink"
    monkeypatch.setattr(state, "STATE_DIR", enlace)
    assert state.is_link(enlace)
    assert state.purge_stale() == 0
    assert ajeno.exists()


def test_a_write_waits_for_a_reader_that_holds_the_file(isolated_state):
    """Deterministic version of what the concurrent test found by chance: on
    Windows a rename onto a file fails while another process holds it open.
    Here the reader is this process, and it lets go after 300 ms."""
    import sys
    import threading

    if sys.platform != "win32":
        pytest.skip("the sharing violation is a Windows behaviour")
    key = state.session_key("conv", "/uno")
    state.write_state(key, {"a": 1})
    lector = open(state.state_path(key), "rb")
    threading.Timer(0.3, lector.close).start()
    state.write_state(key, {"a": 2})
    assert state.read_state(key) == {"a": 2}


def test_two_writers_never_share_a_temporary_file(isolated_state):
    ruta = state.state_path("x" * 64)
    assert state.unique_temporary(ruta) != state.unique_temporary(ruta)
    assert state.unique_temporary(ruta).name.startswith(ruta.name)


def test_nothing_is_written_or_deleted_through_a_linked_state_directory(tmp_path, monkeypatch):
    """Codex, second round: the purge refused a junction, and a new session
    then deleted through it anyway. Now nothing is written or deleted there,
    not even the lock, and the hook stays quiet."""
    import sys

    if sys.platform != "win32":
        pytest.skip("junctions are a Windows thing")
    destino = tmp_path / "destino"
    destino.mkdir()
    enlace = tmp_path / "estado"
    if not _junction(enlace, destino):
        pytest.skip("could not create a junction here")
    monkeypatch.setattr(state, "STATE_DIR", enlace)
    key = state.session_key("conv", "/uno")
    suyo = destino / f"edits-{key}.jsonl"
    suyo.write_text('{"path": "x"}\n', encoding="utf-8")

    assert not state.usable()
    with pytest.raises(state.StateUnavailable):
        state.write_state(key, {"a": 1})
    with pytest.raises(state.StateUnavailable):
        state.append_edit(key, {"path": "y"})
    state.clear_edits(key)
    assert suyo.read_text(encoding="utf-8") == '{"path": "x"}\n', "a delete went through the junction"
    assert sorted(p.name for p in destino.iterdir()) == [suyo.name], "something was written through it"


def test_a_linked_log_or_edit_file_is_never_written_through(isolated_state, monkeypatch):
    """Codex, third round: the directory was checked and its files were not. A
    hook.log linked into a repository received every diagnostic. The link is
    simulated: creating real symlinks on Windows needs a privilege."""
    key = state.session_key("conv", "/uno")
    isolated_state.mkdir(parents=True, exist_ok=True)
    real = state.is_link
    enlazados = {state.log_path().name, state.edits_path(key).name}
    monkeypatch.setattr(state, "is_link", lambda p: p.name in enlazados or real(p))

    state.note("esto no puede llegar al destino del enlace")
    assert not state.log_path().exists()
    with pytest.raises(state.StateUnavailable):
        state.append_edit(key, {"path": "a.py"})
    assert not state.edits_path(key).exists()
