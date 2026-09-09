"""Shared fixtures.

The state directory is redirected for every test without exception: this
package writes into the user's home, and a suite that touched the real one
would corrupt the state of whoever runs it.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from disensor_cursor import state  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    directory = tmp_path / "estado"
    monkeypatch.setattr(state, "STATE_DIR", directory)
    return directory


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository that already adopted disensor, with a resolvable target.

    `origin/main` is written with update-ref instead of a real remote: the
    adapter only ever reads that ref, and a real clone would make the suite
    slower without testing anything more.
    """
    root = tmp_path / "proyecto"
    (root / "src").mkdir(parents=True)
    git(root.parent, "init", "-q", str(root))
    git(root, "config", "user.email", "suite@example.test")
    git(root, "config", "user.name", "suite")
    (root / "disensor.config.json").write_text(
        json.dumps({"criticality_level": "B", "level_A_enabled": False}) + "\n", encoding="utf-8"
    )
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "inicial")
    git(root, "update-ref", "refs/remotes/origin/main", git(root, "rev-parse", "HEAD"))
    return root


@pytest.fixture
def stdin_payload(monkeypatch):
    """Feed a hook payload the way Cursor does: JSON bytes on stdin."""

    def feed(payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")

        class Fake:
            buffer = io.BytesIO(raw)

        monkeypatch.setattr(sys, "stdin", Fake())

    return feed


def emitted(capsys) -> dict:
    """What the hook wrote to stdout, parsed. Anything else is a failure."""
    out = capsys.readouterr().out.strip()
    assert out, "the hook has to print an object, always"
    return json.loads(out)
