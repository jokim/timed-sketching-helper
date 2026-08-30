"""Turning a stored list into a randomized practice session.

The backend holds no session state. It picks a random subset to show plus the
leftover ids as a "reroll pool"; the browser drives the timer and swaps in a
pool id whenever the user rerolls.
"""

from __future__ import annotations

import random


def build_session(
    source_ids: list[str],
    count: int,
    *,
    rng: random.Random | None = None,
) -> tuple[list[str], list[str]]:
    rng = rng or random.Random()
    shuffled = source_ids[:]
    rng.shuffle(shuffled)
    count = max(0, min(count, len(shuffled)))
    return shuffled[:count], shuffled[count:]
