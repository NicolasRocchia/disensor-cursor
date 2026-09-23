"""One deadline for one run of the hook.

Cursor kills a hook that outlives its timeout, and a hook killed mid-flight
never prints the empty object that keeps the agent moving, never logs why, and
never persists what it decided. Timeouts per call do not add up to that:
several git reads, one disensor call and a lock wait each stayed under their
own limit and together went past Cursor's. So the run gets one budget, set
below Cursor's timeout for its event, and every wait draws from it.
"""
from __future__ import annotations

import time

_deadline: float | None = None


def start(seconds: float) -> None:
    global _deadline
    _deadline = time.monotonic() + seconds


def reset() -> None:
    global _deadline
    _deadline = None


def left(cap: float) -> float:
    """Seconds a wait may take: its own cap, or what is left of the run."""
    if _deadline is None:
        return cap
    return max(0.0, min(cap, _deadline - time.monotonic()))
