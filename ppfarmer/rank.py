"""Ranking maps: popularity inside the band, and real pp gain."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .ppcalc import median_pp, pp_gain
from .store import (Store, difficulty_mods, mods_allowed, score_age_years,
                    split_mods)


def speed_multiplier(mods: str) -> float:
    """Speed factor imposed by the mods, which changes the real length.

    DT and NC speed up by 50%, HT and DC slow down by 25%. A 3-minute map
    farmed with DT only costs 2 minutes of play.

    The lookup runs on split acronyms, not on a substring: "HD" followed by
    "TD" gives "HDTD", where a naive test would find a "DT" that is not
    there.
    """
    acr = set(split_mods(mods))
    if acr & {"DT", "NC"}:
        return 1.5
    if acr & {"HT", "DC"}:
        return 0.75
    return 1.0


def parse_duration(value: str | int | float | None) -> int | None:
    """Read a length written in several shapes, return seconds.

    Accepts "90" (seconds), "1m30", "1m", "1:30", "90s". Writing a four-minute
    map's length in raw seconds is not natural.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace(" ", "")
    if not text:
        return None
    match = re.fullmatch(r"(?:(\d+)[m:])?(\d+)?s?", text)
    if not match or not any(match.groups()):
        raise ValueError(
            f"length not understood: {value!r} (expected 90, 90s, 1m30, 1m or 1:30)"
        )
    minutes, seconds = match.groups()
    if minutes is None:
        return int(seconds)
    return int(minutes) * 60 + int(seconds or 0)


def rank_beatmaps(
    store: Store,
    low: int,
    high: int,
    top_n: int = 10,
    exclude: set[int] | None = None,
    limit: int = 30,
    min_stars: float | None = None,
    max_stars: float | None = None,
    user_pps: list[float] | None = None,
    sort: str = "players",
    min_players: int | None = None,
    user_scores: dict[int, dict[str, Any]] | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    mods_filter: set[str] | None = None,
    scoring: str = "all",
    year_min: int | None = None,
    year_max: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """For each map, count the players in the band who have it in their top
    `top_n`, and estimate what it would earn you.

    A player counts once per map and mod combination, even if they passed it
    several times.
    """
    exclude = exclude or set()
    user_pps = user_pps or []
    user_scores = user_scores or {}

    panel = store.conn.execute(
        """SELECT COUNT(*) FROM players
           WHERE global_rank BETWEEN ? AND ? AND scores_at IS NOT NULL""",
        (low, high),
    ).fetchone()[0]

    if min_players is None:
        # A fixed threshold means nothing: 5 players out of a 20-strong panel
        # is a strong signal, 5 out of 1544 is noise. So we ask for 3%.
        min_players = max(5, round(panel * 0.03))

    rows = store.conn.execute(
        """SELECT s.beatmap_id,
                  GROUP_CONCAT(s.user_id || '|' || s.mods || '|'
                               || COALESCE(s.pp, '') || '|' || s.position
                               || '|' || COALESCE(s.ended_at, '')) AS blob,
                  b.artist, b.title, b.version, b.stars, b.bpm, b.length
           FROM scores s
           JOIN players  p ON p.user_id = s.user_id
           LEFT JOIN beatmaps b ON b.beatmap_id = s.beatmap_id
           WHERE p.global_rank BETWEEN ? AND ?
             AND s.position <= ?
           GROUP BY s.beatmap_id""",
        (low, high, top_n),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        bid = row["beatmap_id"]
        if bid in exclude:
            continue
        stars = row["stars"]
        if min_stars is not None and (stars is None or stars < min_stars):
            continue
        if max_stars is not None and (stars is None or stars > max_stars):
            continue

        # Every field travels in a single GROUP_CONCAT: separate concatenations
        # have no guaranteed order between them, and nothing would ensure a pp
        # matches its own mods.
        groups: dict[str, dict[str, Any]] = {}
        for record in (row["blob"] or "").split(","):
            parts = record.split("|")
            if len(parts) != 5:
                continue
            raw_user, brut, raw_pp, raw_pos, date = parts
            # Grouped on difficulty mods only: the same play submitted from
            # stable or from lazer is still the same play.
            mod = difficulty_mods(brut)
            # The year reads straight out of the ISO date, no parsing needed.
            year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
            if year_min is not None and (year is None or year < year_min):
                continue
            if year_max is not None and (year is None or year > year_max):
                continue

            # CL (Classic) marks a score submitted from stable. We keep it out
            # of the grouping but remember who had it, so it can be filtered
            # on or simply displayed.
            from_stable = "CL" in split_mods(brut)
            if scoring == "stable" and not from_stable:
                continue
            if scoring == "lazer" and from_stable:
                continue
            g = groups.setdefault(mod, {"users": set(), "pps": [], "ages": [],
                                        "positions": [], "stable": set(),
                                        "lazer": set(), "years": Counter()})
            g["users"].add(raw_user)
            (g["stable"] if from_stable else g["lazer"]).add(raw_user)
            if year is not None:
                g["years"][year] += 1
            age = score_age_years(date)
            if age is not None:
                g["ages"].append(age)
            try:
                g["positions"].append(int(raw_pos))
            except ValueError:
                pass
            try:
                g["pps"].append(float(raw_pp))
            except ValueError:
                pass

        for mod, g in groups.items():
            # The threshold applies per combination: a map played by 300 people
            # in NM and 6 in HR gives one solid row and one anecdotal row, not
            # an average of the two.
            players = len(g["users"])
            if players < min_players:
                continue
            if not mods_allowed(mod, mods_filter):
                continue

            pps = g["pps"]
            if not pps:
                continue
            # Median by default: a lone outstanding score must not drag the
            # value up. The mean stays exposed next to it, for comparison.
            typical = median_pp(pps)
            mean = sum(pps) / len(pps)
            ages = sorted(g["ages"])
            median_age = ages[len(ages) // 2] if ages else None
            # Median year: more telling than an age in years when judging
            # whether a map belongs to the current meta or to a past era.
            all_years = sorted(a for a, n in g["years"].items() for _ in range(n))
            median_year = all_years[len(all_years) // 2] if all_years else None
            avg_position = (sum(g["positions"]) / len(g["positions"])
                            if g["positions"] else None)

            # Time actually spent on the map, speed mods included.
            raw_length = row["length"]
            played_length = (raw_length / speed_multiplier(mod)
                             if raw_length else None)
            pp_per_min = (typical / (played_length / 60)
                          if typical and played_length else None)

            # The filter applies to the played length: that is the time the map
            # will cost you, not the one shown on the website.
            if played_length is not None:
                if min_length is not None and played_length < min_length:
                    continue
                if max_length is not None and played_length > max_length:
                    continue
            elif min_length is not None or max_length is not None:
                continue

            # Your best score on the map, whatever mods: osu! keeps only one,
            # so that is the one you would be replacing.
            mine = user_scores.get(bid)
            my_pp = mine.get("pp") if mine else None

            out.append({
                "beatmap_id": bid,
                "row_id": f"{bid}:{mod}",
                "players": players,
                "share": players / panel if panel else 0.0,
                "typical_pp": typical,
                "mean_pp": mean,
                "median_age": median_age,
                "median_year": median_year,
                "years": dict(sorted(g["years"].items())),
                "gain": pp_gain(user_pps, typical, replacing=my_pp) if user_pps else None,
                "played": mine is not None,
                "my_pp": my_pp,
                "my_accuracy": mine.get("accuracy") if mine else None,
                "my_fc": mine.get("fc") if mine else None,
                "my_rank": mine.get("rank") if mine else None,
                "my_mods": (difficulty_mods(mine["mods"])
                            if mine and mine.get("mods") else None),
                "avg_position": avg_position,
                "mods": mod,
                "mods_players": players,
                "stable_players": len(g["stable"]),
                "lazer_players": len(g["lazer"]),
                "artist": row["artist"],
                "title": row["title"],
                "version": row["version"],
                "stars": stars,
                "bpm": row["bpm"],
                "length": raw_length,
                "played_length": played_length,
                "pp_per_min": pp_per_min,
                "url": f"https://osu.ppy.sh/b/{bid}",
            })

    if sort == "efficiency":
        out.sort(key=lambda m: (m["pp_per_min"] or 0.0, m["players"]), reverse=True)
    elif sort == "gain" and user_pps:
        out.sort(key=lambda m: (m["gain"] or 0.0, m["players"]), reverse=True)
    elif sort == "pp":
        out.sort(key=lambda m: (m["typical_pp"] or 0.0, m["players"]), reverse=True)
    else:
        out.sort(key=lambda m: (m["players"], m["typical_pp"] or 0.0), reverse=True)

    return out[:limit], panel
