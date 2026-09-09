"""What `disensor-cursor init` writes.

The rule file does not copy disensor's runbook. It points at `disensor guide`,
which is the packaged source of that text. A copy in a third repository would
freeze at whatever version installed it, and disensor's schema is explicitly
not frozen yet, so the copy would go stale silently.
"""
from __future__ import annotations

from . import __version__

# El comando lleva esta marca para que `init` pueda reconocer SUS entradas
# dentro de un hooks.json que ya tenga hooks de otra gente, y reemplazar solo
# las propias.
MARKER = "disensor_cursor.hook"

EVENTS = {
    "sessionStart": {"timeout": 5},
    "afterFileEdit": {"timeout": 5},
    # `loop_limit: 1` porque Cursor ya implementa el corte de la continuacion
    # automatica: no hace falta programarlo. El corte por motivo que lleva el
    # hook es otra cosa y resuelve otro problema.
    "stop": {"timeout": 15, "loop_limit": 1},
}


def _quote(text: str) -> str:
    return f'"{text}"' if " " in text else text


def command_for(event: str, python: str, disensor: str, base: str | None) -> str:
    parts = [_quote(python), "-m", MARKER, "--event", event]
    if disensor:
        parts += ["--disensor", _quote(disensor)]
    if base:
        parts += ["--base", base]
    parts += ["--generated-by", __version__]
    return " ".join(parts)


def hooks_config(python: str, disensor: str, base: str | None = None) -> dict:
    hooks = {}
    for event, options in EVENTS.items():
        entry = {
            "command": command_for(event, python, disensor, base if event == "stop" else None),
            # Advisory: una falla del hook nunca puede frenar al agente. El
            # ejecutor ademas sale siempre con 0, porque en Cursor el 2 bloquea
            # aunque failClosed sea false.
            "failClosed": False,
        }
        entry.update(options)
        hooks[event] = [entry]
    return {"version": 1, "hooks": hooks}


def merge_hooks(existing: dict, generated: dict) -> dict:
    """Ours replace ours; everything else is left exactly as it was.

    Overwriting the file would delete hooks somebody else installed, and
    appending on every run would duplicate ours. Neither is acceptable in a
    file that lives in the user's home and that other tools also write.
    """
    result = dict(existing) if isinstance(existing, dict) else {}
    result["version"] = result.get("version", 1)
    hooks = dict(result.get("hooks") or {})
    for event, entries in generated["hooks"].items():
        previous = hooks.get(event)
        others = [
            e for e in previous
            if isinstance(e, dict) and MARKER not in str(e.get("command", ""))
        ] if isinstance(previous, list) else []
        hooks[event] = others + entries
    result["hooks"] = hooks
    return result


RULE = f"""---
alwaysApply: true
---

# Adversarial review before closing (disensor)

This project uses disensor: before a change merges, it is attacked by a model
from a different family than the one that wrote it, and whatever the round left
open is declared as residue.

A hook watches for this. When you finish a turn it may add an automatic notice
about the state of the round. That notice comes from tooling, not from the
user: it never authorises you to commit or to run a review on your own.

What you need to know:

- `disensor guide` prints the whole procedure and the filling guide. Read it
  from there instead of guessing; it travels with the installed version.
- A diff round reviews commits that already exist, so the tree has to be clean
  before one can run.
- `disensor round` sends the material under review to an external reviewer.
  That is the owner's decision. Ask before running it.
- Never paste material between models by hand and call it a round.
- Check every finding against the actual code before accepting it. The reviewer
  is decorrelated, not right.

<!-- disensor-cursor v{__version__} -->
"""
