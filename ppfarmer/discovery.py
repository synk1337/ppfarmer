"""Discovering the players inside a given global-rank band.

Why this is needed: /rankings/osu/global is capped at 10,000 results
(RankingController::MAX_RESULTS), so rank 71,000 cannot be reached directly.

The way around it: the same endpoint filtered by country (?country=XX) is also
capped at 10,000, but *per country*, and every entry carries the player's real
global_rank (UserStatistics::globalRank() returns rank_score_index, a
precomputed column with no depth limit).

A country exposes global rank G within its first 10,000 entries as long as it
holds less than 10000/G of ranked players. For G = 72,000 the threshold is
14.2%: only the very largest countries exceed it. The US, measured at 15.4%,
yields nothing past rank 51,256; every other country goes deep enough, and
sweeping them reconstructs the band.
"""

from __future__ import annotations

from typing import Any, Callable

from .api import MAX_RANKING_PAGES, PAGE_SIZE, OsuClient

# The countries swept are exactly the 50 on osu.ppy.sh/rankings/osu/country,
# the first page of the country ranking. Deliberately fixed: making it an
# option only made results less comparable between crawls.
COUNTRY_POOL = 50


def entry_rank(entry: dict[str, Any]) -> int | None:
    rank = entry.get("global_rank")
    if rank is None:
        rank = (entry.get("user") or {}).get("statistics", {}).get("global_rank")
    return rank


def entry_to_player(entry: dict[str, Any]) -> dict[str, Any] | None:
    user = entry.get("user") or {}
    uid = user.get("id")
    rank = entry_rank(entry)
    if uid is None or rank is None:
        return None
    return {
        "user_id": uid,
        "username": user.get("username"),
        "country": user.get("country_code"),
        "global_rank": rank,
        "pp": entry.get("pp"),
    }


class CountryScanner:
    """Walks a country ranking to extract a slice of global ranks."""

    def __init__(self, client: OsuClient, country: str, mode: str = "osu") -> None:
        self.client = client
        self.country = country
        self.mode = mode
        self._cache: dict[int, list[dict[str, Any]]] = {}

    def page(self, n: int) -> list[dict[str, Any]]:
        if n not in self._cache:
            self._cache[n] = self.client.rankings_page(self.mode, self.country, n)
        return self._cache[n]

    def _first_page_reaching(self, low: int) -> int | None:
        """Smallest page whose last entry already reaches rank `low`.

        Pages are sorted by pp descending, hence by global_rank ascending, so a
        binary search is valid.
        """
        lo, hi = 1, MAX_RANKING_PAGES
        found: int | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            entries = self.page(mid)
            if not entries:
                # The country has fewer players than this page: go back up.
                hi = mid - 1
                continue
            last = entry_rank(entries[-1])
            if last is not None and last >= low:
                found = mid
                hi = mid - 1
            else:
                lo = mid + 1
        return found

    def collect(self, low: int, high: int) -> list[dict[str, Any]]:
        start = self._first_page_reaching(low)
        if start is None:
            return []
        out: list[dict[str, Any]] = []
        for n in range(start, MAX_RANKING_PAGES + 1):
            entries = self.page(n)
            if not entries:
                break
            stop = False
            for entry in entries:
                rank = entry_rank(entry)
                if rank is None:
                    continue
                if rank > high:
                    stop = True
                    break
                if rank >= low:
                    player = entry_to_player(entry)
                    if player:
                        out.append(player)
            if stop:
                break
        return out


def discover_band(
    client: OsuClient,
    low: int,
    high: int,
    mode: str = "osu",
    target: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    countries: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collects the players whose global rank falls inside [low, high].

    The countries swept are the 50 on the first page of the country ranking, in
    the site's own order (total performance descending).

    Also returns a coverage report. A country too populous to reach the band
    within its first 10,000 entries has its players out of reach, and the
    share of population lost is counted.
    """
    # One page = 50 countries, in the site's order. We do not re-sort: this is
    # the order shown on osu.ppy.sh/rankings/osu/country.
    countries = (countries or client.country_stats(mode))[:COUNTRY_POOL]
    total_active = sum(c.get("active_users") or 0 for c in countries) or 1

    seen: set[int] = set()
    found: list[dict[str, Any]] = []
    unreachable: list[tuple[str, float]] = []
    scanned = 0

    stopped = False
    for country in countries:
        # Cancellation is checked between countries: stopping mid binary
        # search would only save a few seconds.
        if should_stop and should_stop():
            stopped = True
            break
        code = country.get("code")
        if not code:
            continue
        active = country.get("active_users") or 0
        scanner = CountryScanner(client, code, mode)
        players = scanner.collect(low, high)
        scanned += 1
        if not players and active > total_active * 0.01:
            # A large country with nobody in the band: its ranking stops
            # short, so its players at this level are out of reach.
            unreachable.append((code, active / total_active))
        for player in players:
            if player["user_id"] not in seen:
                seen.add(player["user_id"])
                found.append(player)
        if progress:
            progress(code, len(found), active)
        if target and len(found) >= target:
            break

    found.sort(key=lambda p: p["global_rank"])
    report = {
        "stopped": stopped,
        "countries_scanned": scanned,
        "unreachable": unreachable,
        "lost_share": sum(share for _, share in unreachable),
    }
    return found, report
