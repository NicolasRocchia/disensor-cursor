"""The disensor client, without disensor: what it sends and how it reads."""
from __future__ import annotations

from pathlib import Path

import pytest

from disensor_cursor import clock, disensor


@pytest.mark.parametrize("line, gates", [
    ("round: review required, gates diff", ("diff",)),
    ("round: review required, gates diff, plan", ("diff", "plan")),
    ("round: review required, gates architecture, diff, plan", ("architecture", "diff", "plan")),
    ("round: review required", None),
    ("", None),
    ("round: review required, gates sideways", None),
    ("round: review required, gates diff, sideways", None),
])
def test_the_accepted_gates_are_read_or_admitted_unknown(line, gates):
    """None is "could not read", never "no gate": one unknown word and the
    whole line is distrusted."""
    assert disensor.parse_gates(line) == gates


@pytest.fixture
def sent(monkeypatch):
    seen = {}

    def fake(executable, args, cwd, timeout):
        seen["args"] = list(args)
        return seen.get("code", 0), "", seen.get("err", "round: review required, gates diff")

    monkeypatch.setattr(disensor, "_run", fake)
    return seen


def test_only_what_the_user_configured_is_sent(sent):
    """Neither --directory nor --config is documented for `round`, so neither
    is promised: left unset, disensor applies its own defaults."""
    disensor.check_requirement(Path("x"), Path("."), "a" * 40, "b" * 40)
    assert "--directory" not in sent["args"] and "--config" not in sent["args"]
    assert sent["args"][-1] == "--check"

    disensor.check_requirement(Path("x"), Path("."), "a" * 40, "b" * 40,
                               directory="evidencia", config="politica.json")
    args = sent["args"]
    assert args[args.index("--directory") + 1] == "evidencia"
    assert args[args.index("--config") + 1] == "politica.json"


@pytest.mark.parametrize("code, verdict", [
    (0, disensor.REQUIRED),
    (3, disensor.NOT_NEEDED),
    (6, disensor.BLOCKED),
    (1, disensor.UNAVAILABLE),
    (2, disensor.UNAVAILABLE),
    (4, disensor.UNAVAILABLE),
    (-1, disensor.UNAVAILABLE),
])
def test_every_exit_code_has_one_meaning(sent, code, verdict):
    """2 is what an older disensor's argparse answers to an unknown flag: it
    means "could not ask", never a verdict."""
    sent["code"] = code
    assert disensor.check_requirement(Path("x"), Path("."), "a" * 40, "b" * 40).verdict == verdict


def test_without_time_left_disensor_is_not_even_started():
    clock.start(0.1)
    try:
        code, _, err = disensor._run(Path("no-importa"), ["round"], Path("."), 8.0)
    finally:
        clock.reset()
    assert code == -1 and "no time left" in err
