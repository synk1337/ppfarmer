"""Weighted pp arithmetic.

A player's total pp is the weighted sum of their 100 best scores, sorted by pp
descending, with a weight of 0.95^i (i starting at 0). A new score therefore
only earns the difference it makes once inserted into that curve: a score below
your 100th earns nothing at all.

We compute that delta exactly rather than approximating it with constants.
"""

from __future__ import annotations

from statistics import median

DECAY = 0.95
TOP_COUNT = 100


def weighted_total(pps: list[float]) -> float:
    """Weighted sum of the 100 best scores."""
    ordered = sorted((p for p in pps if p is not None), reverse=True)[:TOP_COUNT]
    return sum(pp * DECAY**i for i, pp in enumerate(ordered))


def pp_gain(
    user_pps: list[float],
    new_pp: float | None,
    replacing: float | None = None,
) -> float:
    """Total pp gained if the player scored `new_pp` on a map.

    `replacing` is their current score on that map, if any. osu! only keeps the
    best score per map, so a new score does not add to the top, it replaces the
    old one. Treating a replay as an addition would badly overstate the gain.
    """
    if new_pp is None:
        return 0.0
    before = weighted_total(user_pps)
    pool = list(user_pps)
    if replacing is not None:
        if new_pp <= replacing:
            return 0.0  # replaying worse earns nothing
        # Remove the existing score (closest value: the floats come from the
        # same API call but may have travelled through SQLite).
        if pool:
            closest = min(range(len(pool)), key=lambda i: abs(pool[i] - replacing))
            if abs(pool[closest] - replacing) < 0.01:
                pool.pop(closest)
    after = weighted_total(pool + [new_pp])
    return max(0.0, after - before)


def median_pp(values: list[float]) -> float | None:
    """Median, more robust than the mean against outlier scores."""
    clean = [v for v in values if v is not None]
    return median(clean) if clean else None
