"""SQLite cache: discovered players, their top scores, and beatmap metadata.

Everything that costs an API request is persisted here, so a re-run only
fetches what is missing or stale.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,
    country      TEXT,
    global_rank  INTEGER,
    pp           REAL,
    discovered_at REAL,
    scores_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_players_rank ON players(global_rank);

CREATE TABLE IF NOT EXISTS scores (
    user_id    INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    beatmap_id INTEGER NOT NULL,
    pp         REAL,
    mods       TEXT,
    accuracy   REAL,
    rank       TEXT,
    ended_at   TEXT,
    PRIMARY KEY (user_id, position)
);
CREATE INDEX IF NOT EXISTS idx_scores_beatmap ON scores(beatmap_id);

CREATE TABLE IF NOT EXISTS beatmaps (
    beatmap_id INTEGER PRIMARY KEY,
    set_id     INTEGER,
    artist     TEXT,
    title      TEXT,
    version    TEXT,
    stars      REAL,
    bpm        REAL,
    length     INTEGER,
    status     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# Past this age a score says nothing about the current game: neither the pp
# formula nor the farming meta stayed still that long.
MAX_SCORE_AGE_YEARS = 10

# How long a fetched top stays usable. Ranks and tops drift slowly at this
# scale, so a shorter window would re-spend thousands of requests for
# almost the same ranking.
CACHE_MAX_AGE_DAYS = 30


def score_age_years(ended_at: str | None, now: dt.datetime | None = None) -> float | None:
    """Age of a score in years, from its ISO 8601 date."""
    if not ended_at:
        return None
    try:
        when = dt.datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - when).days / 365.25


def parse_profile(text: str | None) -> str | None:
    """Read an osu! profile URL, a numeric id or a username.

    Accepts the shapes people actually paste: the profile page with or
    without a ruleset suffix, with or without the scheme, or just the id.
    Returns what the API can look up, or None if nothing usable is found.
    """
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    match = re.search(r"osu\.ppy\.sh/(?:users|u)/([^/?#]+)", raw, re.IGNORECASE)
    if match:
        return match.group(1)
    # A bare URL with no recognisable user segment is a mistake, not a username.
    if "://" in raw or "osu.ppy.sh" in raw.lower():
        return None
    return raw


def normalize_mods(raw: Any) -> str:
    """The API returns either ['HD','DT'] (stable) or [{'acronym':'HD'}, ...] (lazer).

    No mod is dropped. CL (Classic) in particular has to survive: it marks a
    score submitted under stable scoring, which is not worth the same pp as a
    lazer one. Measured on [Shiawase!!]: the same 100% SS is 254pp with CL and
    268pp without. NF and SO carry a 0.9 multiplier of their own.
    """
    if not raw:
        return "NM"
    acronyms: list[str] = []
    for mod in raw:
        acr = mod.get("acronym") if isinstance(mod, dict) else str(mod)
        if acr:
            acronyms.append(acr)
    return "".join(sorted(acronyms)) or "NM"


def split_mods(mods: str) -> list[str]:
    """Split a concatenated mod string: every acronym is exactly two characters."""
    if not mods or mods == "NM":
        return []
    return [mods[i:i + 2] for i in range(0, len(mods), 2)]


# NC (Nightcore) is DT with a different sound, DC (Daycore) is HT: the pp is
# identical. Keeping them apart would split samples for nothing.
MOD_ALIASES = {"NC": "DT", "DC": "HT"}

# The combinations that shape farming. The rest is marginal.
COMMON_MODS = ("NM", "HD", "HR", "DT", "HDHR", "DTHD")


def canonical_mods(text: str) -> str:
    """Normalise a hand-written combination: "hddt" -> "DTHD"."""
    raw = "".join(str(text).split()).upper()
    if not raw or raw == "NM":
        return "NM"
    acr = {MOD_ALIASES.get(raw[i:i + 2], raw[i:i + 2])
           for i in range(0, len(raw), 2)} - {"CL"}
    return "".join(sorted(acr)) or "NM"


def parse_mod_filter(text: str | None) -> set[str] | None:
    """Read "HD,HR,HDDT" into a set of normalised combinations.

    The token "other" keeps everything outside COMMON_MODS.
    """
    if not text:
        return None
    out: set[str] = set()
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower() in {"other", "others", "autres", "autre"}:
            out.add("OTHER")
        else:
            out.add(canonical_mods(part))
    return out or None


def mods_allowed(mods: str, wanted: set[str] | None) -> bool:
    """Does this combination pass the filter?"""
    if not wanted:
        return True
    if mods in wanted:
        return True
    return "OTHER" in wanted and mods not in COMMON_MODS


def difficulty_mods(mods: str) -> str:
    """Mods that change difficulty, CL excluded.

    CL does not say how the map was played but which scoring system submitted
    it. Grouping on it would cut every map into two samples for nothing: we
    keep it out of the grouping and handle it separately, through the scoring
    filter.
    """
    acr = {MOD_ALIASES.get(m, m) for m in set(split_mods(mods)) - {"CL"}}
    return "".join(sorted(acr)) or "NM"


def is_full_combo(score: dict[str, Any]) -> bool | None:
    """Is this score an FC (no broken combo)?

    The API exposes the flag under two names depending on where the score came
    from: `is_perfect_combo` on lazer, `legacy_perfect` (or `perfect`) on stable.
    """
    for key in ("is_perfect_combo", "legacy_perfect", "perfect"):
        value = score.get(key)
        if value is not None:
            return bool(value)
    return None


def user_score_index(scores: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Index a player's best scores by beatmap, for comparison.

    The API returns the top sorted by pp descending, so the first score seen
    on a map is already the best one.
    """
    index: dict[int, dict[str, Any]] = {}
    for score in scores:
        bid = (score.get("beatmap") or {}).get("id")
        if bid is None or bid in index:
            continue
        index[bid] = {
            "pp": score.get("pp"),
            "accuracy": score.get("accuracy"),
            "fc": is_full_combo(score),
            "rank": score.get("rank"),
            "mods": normalize_mods(score.get("mods")),
        }
    return index


class Store:
    def __init__(self, path: str | Path = "ppfarmer.db") -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after an existing database was created."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(scores)")}
        if "ended_at" not in have:
            self.conn.execute("ALTER TABLE scores ADD COLUMN ended_at TEXT")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.conn.commit()
        self.close()

    # -- players -------------------------------------------------------------

    def upsert_players(self, rows: Iterable[dict[str, Any]]) -> int:
        now = time.time()
        payload = [
            (r["user_id"], r.get("username"), r.get("country"),
             r.get("global_rank"), r.get("pp"), now)
            for r in rows
        ]
        if not payload:
            return 0
        self.conn.executemany(
            """INSERT INTO players (user_id, username, country, global_rank, pp, discovered_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 country=excluded.country,
                 global_rank=excluded.global_rank,
                 pp=excluded.pp""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def players_in_band(self, low: int, high: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM players WHERE global_rank BETWEEN ? AND ? ORDER BY global_rank",
            (low, high),
        ).fetchall()

    def players_needing_scores(self, low: int, high: int, max_age_days: float) -> list[int]:
        cutoff = time.time() - max_age_days * 86400
        rows = self.conn.execute(
            """SELECT user_id FROM players
               WHERE global_rank BETWEEN ? AND ?
                 AND (scores_at IS NULL OR scores_at < ?)
               ORDER BY global_rank""",
            (low, high, cutoff),
        ).fetchall()
        return [r["user_id"] for r in rows]

    # -- scores --------------------------------------------------------------

    def save_scores(self, user_id: int, scores: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM scores WHERE user_id = ?", (user_id,))
        rows = []
        maps: dict[int, tuple] = {}
        for pos, sc in enumerate(scores, start=1):
            bm = sc.get("beatmap") or {}
            bms = sc.get("beatmapset") or {}
            bid = bm.get("id")
            if bid is None:
                continue
            ended_at = sc.get("ended_at") or sc.get("created_at")
            age = score_age_years(ended_at)
            if age is not None and age > MAX_SCORE_AGE_YEARS:
                # Dropped, but without renumbering: the position reflects the
                # player's real ranking, and promoting the next score would
                # pull into the "top 10" something that never was there.
                continue
            rows.append((
                user_id, pos, bid, sc.get("pp"),
                normalize_mods(sc.get("mods")),
                sc.get("accuracy"), sc.get("rank"), ended_at,
            ))
            maps[bid] = (
                bid, bms.get("id"), bms.get("artist"), bms.get("title"),
                bm.get("version"), bm.get("difficulty_rating"), bm.get("bpm"),
                bm.get("total_length"), bm.get("status"),
            )
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?)", rows
            )
        if maps:
            self.conn.executemany(
                "INSERT OR REPLACE INTO beatmaps VALUES (?,?,?,?,?,?,?,?,?)",
                list(maps.values()),
            )
        self.conn.execute(
            "UPDATE players SET scores_at = ? WHERE user_id = ?", (time.time(), user_id)
        )
        self.conn.commit()

    # -- meta ----------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def last_band(self) -> tuple[int, int] | None:
        """Rank band covered by the last crawl, if known.

        Storing the bounds rather than a spread avoids shifting the window
        once the player's own rank has moved.
        """
        try:
            low = int(self.get_meta("last_low"))
            high = int(self.get_meta("last_high"))
        except (TypeError, ValueError):
            return None
        return (low, high) if low <= high else None

    def credentials(self) -> tuple[str, str]:
        """OAuth client id and secret.

        The environment wins, so a source checkout keeps working from .env.
        A packaged build has no .env, so the values come from the database
        after the user enters them once.
        """
        import os
        env_id = os.getenv("OSU_CLIENT_ID", "").strip()
        env_secret = os.getenv("OSU_CLIENT_SECRET", "").strip()
        if env_id and env_secret:
            return env_id, env_secret
        return (self.get_meta("osu_client_id") or "",
                self.get_meta("osu_client_secret") or "")

    def set_credentials(self, client_id: str, secret: str) -> None:
        self.set_meta("osu_client_id", client_id.strip())
        self.set_meta("osu_client_secret", secret.strip())

    def profile(self) -> str | None:
        """Saved osu! profile, as an id or username the API can resolve."""
        return self.get_meta("profile") or None

    def stats(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "players": q("SELECT COUNT(*) FROM players"),
            "with_scores": q("SELECT COUNT(*) FROM players WHERE scores_at IS NOT NULL"),
            "scores": q("SELECT COUNT(*) FROM scores"),
            "beatmaps": q("SELECT COUNT(*) FROM beatmaps"),
        }
