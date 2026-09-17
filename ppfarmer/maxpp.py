"""A map's pp ceiling: what a perfect SS would earn on it.

Computed on demand, one map at a time, on click. Precomputing everything cost
21,000 downloads for maps nobody looks at; computing a single map is instant,
and the result is kept.

The exact pp comes from rosu-pp, the Rust port of the official calculator,
applied to the map's .osu file. Checked on [Shiawase!!]: 259.8pp for a nomod
SS under stable scoring, 274.0pp under lazer.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from .paths import beatmap_cache

try:
    import rosu_pp_py as rosu
except ImportError:  # pragma: no cover - the page then shows a clear error
    rosu = None

OSU_FILE_URL = "https://osu.ppy.sh/osu/{beatmap_id}"

MAXPP_SCHEMA = """
CREATE TABLE IF NOT EXISTS max_pp (
    beatmap_id INTEGER NOT NULL,
    mods       TEXT    NOT NULL,
    pp         REAL,
    PRIMARY KEY (beatmap_id, mods)
);
"""


class MaxPpCalculator:
    """Fetches the .osu if needed and computes the pp of a perfect SS."""

    def __init__(self, cache_dir: str | Path | None = None, rpm: int = 60) -> None:
        self.dir = Path(cache_dir) if cache_dir else beatmap_cache()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._http = httpx.Client(timeout=30.0,
                                  headers={"User-Agent": "ppfarmer/0.1"})
        self._lock = threading.Lock()
        self._interval = 60.0 / max(1, rpm)
        self._next = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MaxPpCalculator":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def beatmap_file(self, beatmap_id: int) -> Path | None:
        path = self.dir / f"{beatmap_id}.osu"
        if path.exists() and path.stat().st_size > 0:
            return path
        with self._lock:
            wait = self._next - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next = time.monotonic() + self._interval
        try:
            resp = self._http.get(OSU_FILE_URL.format(beatmap_id=beatmap_id))
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        path.write_bytes(resp.content)
        return path

    def max_pp(self, beatmap_id: int, mods: str) -> float | None:
        """pp of a 100% SS with maximum combo, for these mods.

        `mods` is a difficulty combination (DTHD, HR...), without CL, so the
        computation runs under lazer scoring, the current game. The same SS is
        worth about 5% less under stable.
        """
        if rosu is None:
            return None
        path = self.beatmap_file(beatmap_id)
        if path is None:
            return None
        from .store import split_mods
        try:
            beatmap = rosu.Beatmap(path=str(path))
            perf = rosu.Performance(mods=split_mods(mods), accuracy=100.0, lazer=True)
            return float(perf.calculate(beatmap).pp)
        except Exception:                                  # noqa: BLE001
            # Truncated file, unconvertible mode, rejected mods: give up on
            # this map rather than propagating the error to the page.
            return None


def ensure_schema(conn) -> None:
    conn.executescript(MAXPP_SCHEMA)


def get_or_compute(conn, beatmap_id: int, mods: str,
                   calculator: MaxPpCalculator) -> float | None:
    """This map's ceiling, from cache or computed then stored."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT pp FROM max_pp WHERE beatmap_id = ? AND mods = ?",
        (beatmap_id, mods),
    ).fetchone()
    if row is not None:
        return row["pp"]
    value = calculator.max_pp(beatmap_id, mods)
    conn.execute("INSERT OR REPLACE INTO max_pp VALUES (?,?,?)",
                 (beatmap_id, mods, value))
    conn.commit()
    return value


def cached_ceilings(conn, pairs: list[tuple[int, str]]) -> dict[str, float]:
    """Ceilings already known, so the button is not offered again for nothing."""
    if not pairs:
        return {}
    ensure_schema(conn)
    wanted = set(pairs)
    out: dict[str, float] = {}
    for row in conn.execute("SELECT beatmap_id, mods, pp FROM max_pp"):
        key = (row["beatmap_id"], row["mods"])
        if key in wanted and row["pp"] is not None:
            out[f"{row['beatmap_id']}:{row['mods']}"] = row["pp"]
    return out
