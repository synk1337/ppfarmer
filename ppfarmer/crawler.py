"""Orchestration: resolve the player, discover their band, fetch the tops."""

from __future__ import annotations

from typing import Any, Callable

from .api import OsuApiError, OsuClient
from .discovery import COUNTRY_POOL, discover_band
from .store import CACHE_MAX_AGE_DAYS, Store


def resolve_user(client: OsuClient, who: str, mode: str = "osu") -> dict[str, Any]:
    data = client.user(who, mode)
    if not data:
        raise OsuApiError(f"Player not found: {who}")
    stats = data.get("statistics") or {}
    rank = stats.get("global_rank")
    if rank is None:
        raise OsuApiError(
            f"{data.get('username')} has no global rank in {mode} "
            "(inactive account, or no ranked score)."
        )
    return {
        "user_id": data["id"],
        "username": data.get("username"),
        "country": data.get("country_code"),
        "global_rank": rank,
        "pp": stats.get("pp"),
    }


# A country's ranking is capped at 10,000 entries, so it only exposes global
# rank G while it holds less than 10000/G of ranked players. The deeper the
# band, the more countries fall out: nobody is missing above rank 30,000, the
# US alone costs 16% around rank 71,000, and the US plus Russia cost 29% past
# rank 100,000.
COUNTRY_CAP = 10_000

# Of the players a reachable country does expose, some are inactive or
# restricted and never come back. Measured at 76.4% harvested for 83.5%
# reachable, which is this ratio.
ACTIVE_RATIO = 0.91

# Used when the country shares are not known yet. Matches the rate measured
# around rank 71,000.
HARVEST_RATE = 0.70


def reachable_share(countries: list[dict], deepest_rank: int) -> float:
    """Share of players the country sweep can still see at that depth.

    Each country is weighted by its active players, and dropped once it is too
    populous to expose `deepest_rank` within its 10,000-entry cap.
    """
    total = sum(c.get("active_users") or 0 for c in countries)
    if not total or deepest_rank <= 0:
        return 1.0
    ceiling = COUNTRY_CAP / deepest_rank
    kept = sum((c.get("active_users") or 0) for c in countries
               if (c.get("active_users") or 0) / total <= ceiling)
    return kept / total


def window_for_players(target: int, deepest_rank: int | None = None,
                       countries: list[dict] | None = None) -> int:
    """How many ranks to sweep to expect `target` players.

    With the country list and the depth, the estimate follows what is actually
    reachable there. A fixed rate would badly undershoot deep bands: around
    rank 300,000 half the population is out of reach, so the same window
    yields half the players.
    """
    rate = HARVEST_RATE
    if countries and deepest_rank:
        rate = reachable_share(countries, deepest_rank) * ACTIVE_RATIO
        rate = max(0.15, min(0.95, rate))
    return max(target, int(target / rate) + 50)


def band_for(rank: int, spread: int) -> tuple[int, int]:
    """The `spread` ranks sitting just above the player.

    Above means better, so a smaller rank number. The upper bound excludes the
    player: their own top teaches the ranking nothing, and including it would
    bias the signal towards what they already play.
    """
    return max(1, rank - spread), max(1, rank - 1)


def plan_band(client: OsuClient, rank: int, target_players: int,
              mode: str = "osu") -> tuple[int, int, list[dict[str, Any]]]:
    """Band to sweep to end up with `target_players`, at this depth.

    Costs one request for the country list, which is handed back so the crawl
    does not fetch it twice.
    """
    countries = client.country_stats(mode)[:COUNTRY_POOL]
    window = window_for_players(target_players, rank, countries)
    low, high = band_for(rank, window)
    return low, high, countries


def crawl(
    client: OsuClient,
    store: Store,
    low: int,
    high: int,
    mode: str = "osu",
    top_n: int = 50,
    max_age_days: float = CACHE_MAX_AGE_DAYS,
    target: int | None = None,
    on_country: Callable[[str, int, int], None] | None = None,
    on_player: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    target_players: int | None = None,
    countries: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    players, report = discover_band(
        client, low, high, mode, target=target, progress=on_country,
        should_stop=should_stop, countries=countries,
    )
    # Discovered players are kept even on cancellation: that is paid-for work
    # a later crawl will not have to redo.
    store.upsert_players(players)
    if report.get("stopped"):
        return {"discovered": len(players), "crawled": 0, "pending": 0, **report}

    # Asked in players: tighten the band onto the N closest *as the database
    # knows them*. Limiting to players discovered just now would miss those a
    # previous crawl already found, and tightening before writing made the
    # crawl fetch more tops than asked for.
    if target_players:
        known = store.players_in_band(low, high)
        if len(known) > target_players:
            closest = sorted(known, key=lambda r: r["global_rank"],
                             reverse=True)[:target_players]
            low = min(r["global_rank"] for r in closest)
            high = max(r["global_rank"] for r in closest)
            report["trimmed_to"] = target_players

    # The band actually covered is remembered. Storing a spread is not enough:
    # the player's rank moves, and recomputing would shift the window towards
    # ranks with no data.
    store.set_meta("last_low", str(low))
    store.set_meta("last_high", str(high))
    report["band"] = (low, high)

    todo = store.players_needing_scores(low, high, max_age_days)
    # Players already up to date are skipped. Unless that is reported, a crawl
    # re-run right after the previous one looks like it did nothing.
    in_band = len(store.players_in_band(low, high))
    report["skipped"] = in_band - len(todo)
    done = 0
    for i, uid in enumerate(todo, start=1):
        # Each top is saved as it arrives: cancelling here only loses the
        # request in flight, everything already done is kept.
        if should_stop and should_stop():
            report["stopped"] = True
            break
        try:
            scores = client.user_best(uid, mode, limit=top_n)
        except OsuApiError:
            continue
        store.save_scores(uid, scores)
        done = i
        if on_player:
            on_player(i, len(todo))

    return {"discovered": len(players), "crawled": done,
            "pending": len(todo) - done, **report}
