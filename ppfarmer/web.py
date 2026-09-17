"""Local server backing the browser interface.

Deliberately standard-library only: a handful of routes does not justify
another dependency. The whole dataset is sent once and all filtering happens
in the browser, which is what makes the controls feel instant.

A local process is unavoidable. The osu! API sends no CORS headers, so a page
cannot call it directly, and the client_credentials grant needs a secret that
has no business living in a browser.
"""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from .api import OsuApiError, OsuClient
from .crawler import band_for, crawl, plan_band, resolve_user
from .discovery import COUNTRY_POOL
from .maxpp import MaxPpCalculator, cached_ceilings, get_or_compute
from .paths import default_db, resource_dir
from .rank import rank_beatmaps
from .store import Store, parse_profile, user_score_index

STATIC = resource_dir() / "static"

# Floor for the dataset sent to the page. Splitting rows per mod combination
# multiplies them: without a floor, a depth of 100 produced 37,000 rows and
# 24 MB. Below five players a median means nothing anyway, so nothing useful
# is lost.
PAYLOAD_MIN_PLAYERS = 5


class Context:
    """Shared state: the player's profile, their scores, and the SQLite cache.

    The profile is resolved lazily, because the browser is what supplies it.
    Until then the page shows its profile dialog and every data route answers
    that it has nothing to work with.
    """

    def __init__(self, db: str, mode: str = "osu") -> None:
        # Kept for source checkouts; a packaged build simply has no .env.
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        self.db = db
        self.mode = mode
        self.me: dict | None = None
        self.user_pps: list[float] = []
        self.user_scores: dict = {}
        self.low = self.high = 0
        self.spread = 0
        self._lock = threading.Lock()
        self._cache: dict[tuple, dict] = {}
        saved = None
        with Store(db) as store:
            saved = store.profile()
        if saved:
            try:
                self.set_profile(saved, persist=False)
            except OsuApiError:
                # A saved profile that no longer resolves must not block the
                # launch: the page will simply ask for another one.
                self.me = None

    def ready(self) -> bool:
        return self.me is not None

    def set_profile(self, text: str, persist: bool = True) -> dict:
        """Resolve a profile URL, id or username, and load what depends on it."""
        who = parse_profile(text)
        if not who:
            raise OsuApiError(
                "Could not read a profile from that. Paste a link such as "
                "https://osu.ppy.sh/users/4721909"
            )
        with Store(self.db) as store:
            client_id, secret = store.credentials()
        with OsuClient(client_id, secret) as client:
            me = resolve_user(client, who, self.mode)
            top = client.user_best(me["user_id"], self.mode, limit=100)

        with Store(self.db) as store:
            if persist:
                store.set_meta("profile", str(me["user_id"]))
            saved_band = store.last_band()

        self.me = me
        self.user_pps = [s["pp"] for s in top if s.get("pp") is not None]
        self.user_scores = user_score_index(top)
        # Without an explicit band, reuse the one the last crawl covered:
        # recomputing from a default spread would only show a fraction of it.
        self.low, self.high = saved_band or band_for(me["global_rank"], 1000)
        self.spread = self.high - self.low + 1
        with self._lock:
            self._cache.clear()
        return me

    def payload(self, top_n: int, scoring: str = "all",
                year_min: int | None = None, year_max: int | None = None) -> dict:
        if not self.ready():
            with Store(self.db) as store:
                has_creds = all(store.credentials())
            return {"needs_profile": True, "needs_credentials": not has_creds}
        # The year filter applies before grouping, so it belongs in the cache
        # key just like depth and scoring version.
        key = (top_n, scoring, year_min, year_max)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            with Store(self.db) as store:
                maps, panel = rank_beatmaps(
                    store, self.low, self.high, top_n=top_n,
                    limit=100000, min_players=PAYLOAD_MIN_PLAYERS,
                    user_pps=self.user_pps, user_scores=self.user_scores,
                    scoring=scoring, year_min=year_min, year_max=year_max,
                )
                # Depth actually available: no point offering a slider up to
                # 100 when the crawl only fetched 50 scores per player.
                depth = store.conn.execute(
                    """SELECT MAX(s.position) FROM scores s
                       JOIN players p ON p.user_id = s.user_id
                       WHERE p.global_rank BETWEEN ? AND ?""",
                    (self.low, self.high),
                ).fetchone()[0]
                bounds = store.conn.execute(
                    """SELECT MIN(substr(ended_at,1,4)), MAX(substr(ended_at,1,4))
                       FROM scores WHERE ended_at IS NOT NULL""").fetchone()
                known = cached_ceilings(
                    store.conn, [(m["beatmap_id"], m["mods"]) for m in maps])
            for m in maps:
                m["max_pp"] = known.get(f"{m['beatmap_id']}:{m['mods']}")
            data = {
                "needs_profile": False,
                "needs_credentials": False,
                "user": {
                    "name": self.me["username"],
                    "id": self.me["user_id"],
                    "rank": self.me["global_rank"],
                    "pp": self.me["pp"],
                    "country": self.me["country"],
                },
                "band": {"low": self.low, "high": self.high, "spread": self.spread},
                "panel": panel,
                "cutoff": min(self.user_pps) if len(self.user_pps) >= 100 else 0,
                "top_n": top_n,
                "scoring": scoring,
                "year_filter": {"min": year_min, "max": year_max},
                "years": {"min": int(bounds[0]) if bounds[0] else 2007,
                          "max": int(bounds[1]) if bounds[1] else 2026},
                "max_depth": depth or 100,
                "min_players_floor": PAYLOAD_MIN_PLAYERS,
                "maps": maps,
            }
            self._cache[key] = data
            return data


class CrawlRunner:
    """Runs a crawl on its own thread and publishes its progress.

    One crawl at a time: two would fight over the request budget and the
    database. The context cache is cleared at the end so the page reflects the
    new data.
    """

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = "idle"    # idle | running | done | cancelled | error
        self.phase = ""
        self.countries_done = 0
        self.countries_total = 0
        self.players_done = 0
        self.players_total = 0
        self.message = ""
        self.report: dict = {}
        self._cancel = threading.Event()

    def cancel(self) -> tuple[bool, str]:
        if not self.running():
            return False, "No crawl is running."
        self._cancel.set()
        self.message = "Cancelling, will stop after the current step..."
        return True, self.message

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {
            "state": self.state, "phase": self.phase,
            "countries_done": self.countries_done,
            "countries_total": self.countries_total,
            "players_done": self.players_done,
            "players_total": self.players_total,
            "message": self.message, "report": self.report,
        }

    def start(self, params: dict) -> tuple[bool, str]:
        with self._lock:
            if self.running():
                return False, "A crawl is already running."
            if not self.ctx.ready():
                return False, "Set your osu! profile first."
            self.state = "running"
            self.phase = "discovery"
            self.countries_done = self.players_done = 0
            self.countries_total = COUNTRY_POOL
            self.players_total = 0
            self.message = ""
            self.report = {}
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run, args=(params,), daemon=True)
            self._thread.start()
            return True, "Crawl started."

    def _run(self, p: dict) -> None:
        try:
            rank = self.ctx.me["global_rank"]
            countries = None
            with Store(self.ctx.db) as creds:
                client_id, secret = creds.credentials()
            if p.get("players"):
                with OsuClient(client_id, secret) as probe:
                    low, high, countries = plan_band(
                        probe, rank, p["players"], self.ctx.mode)
            else:
                low, high = band_for(rank, p["spread"])

            def on_country(code: str, found: int, active: int) -> None:
                self.countries_done += 1
                self.message = f"{code} - {found} players found"

            def on_player(i: int, total: int) -> None:
                self.phase = "fetching"
                self.players_done, self.players_total = i, total

            with OsuClient(client_id, secret) as client, \
                    Store(self.ctx.db) as store:
                report = crawl(
                    client, store, low, high, self.ctx.mode,
                    top_n=p["fetch_n"],
                    target=p["target"],
                    on_country=on_country, on_player=on_player,
                    should_stop=self._cancel.is_set,
                    target_players=p.get("players"), countries=countries,
                )
            # The band may have tightened if a player count was targeted: take
            # it before building the report, otherwise the report and the
            # message disagree about which band was covered.
            low, high = report.get("band", (low, high))
            self.report = {
                "discovered": report["discovered"],
                "crawled": report["crawled"],
                "pending": report.get("pending", 0),
                "skipped": report.get("skipped", 0),
                "band": [low, high],
                "stopped": report.get("stopped", False),
                "lost_share": report.get("lost_share", 0),
                "unreachable": [c for c, _ in report.get("unreachable", [])],
            }
            self.ctx.low, self.ctx.high = low, high
            self.ctx.spread = high - low + 1
            with self.ctx._lock:
                self.ctx._cache.clear()
            if report.get("stopped"):
                self.state = "cancelled"
                self.phase = "cancelled"
                self.message = (
                    f"Cancelled. Kept {report['discovered']} players discovered "
                    f"and {report['crawled']} tops fetched"
                    + (f", {report['pending']} still to do."
                       if report.get("pending") else ".")
                )
            else:
                skipped = report.get("skipped", 0)
                self.state = "done"
                self.phase = "done"
                self.message = (
                    f"#{low:,}-#{high:,}: {report['discovered']} players "
                    f"discovered, {report['crawled']} tops fetched"
                    + (f", {skipped} already up to date." if skipped else ".")
                )
        except Exception as exc:                          # noqa: BLE001
            self.state = "error"
            self.phase = ""
            self.message = str(exc)


def make_handler(ctx: Context, runner: CrawlRunner):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # no per-request logging
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:
            route = urlparse(self.path)
            if route.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
                return
            if route.path == "/api/crawl":
                self._json(200, runner.status())
                return
            if route.path == "/api/data":
                params = parse_qs(route.query)
                try:
                    top_n = int(params.get("top_n", ["10"])[0])
                except ValueError:
                    top_n = 10
                # Capped at 100, osu!'s own limit on /scores/best.
                top_n = max(1, min(100, top_n))
                scoring = params.get("scoring", ["all"])[0]
                if scoring not in ("all", "stable", "lazer"):
                    scoring = "all"

                def year(name):
                    try:
                        return max(2007, min(2100, int(params[name][0])))
                    except (KeyError, IndexError, ValueError):
                        return None

                self._json(200, ctx.payload(top_n, scoring,
                                            year("year_min"), year("year_max")))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                body = {}

            if path == "/api/credentials":
                client_id = str(body.get("client_id", "")).strip()
                secret = str(body.get("client_secret", "")).strip()
                if not client_id or not secret:
                    self._json(400, {"ok": False,
                                     "message": "Both the client ID and the secret are required."})
                    return
                try:
                    # Validated straight away: a wrong secret must be caught
                    # here, not at the first crawl.
                    with OsuClient(client_id, secret) as probe:
                        probe.get("/rankings/osu/country", {"page": 1})
                except OsuApiError as exc:
                    self._json(400, {"ok": False, "message": str(exc)})
                    return
                with Store(ctx.db) as store:
                    store.set_credentials(client_id, secret)
                self._json(200, {"ok": True})
                return

            if path == "/api/profile":
                try:
                    me = ctx.set_profile(str(body.get("url", "")))
                except OsuApiError as exc:
                    self._json(400, {"ok": False, "message": str(exc)})
                    return
                self._json(200, {"ok": True, "user": {
                    "name": me["username"], "id": me["user_id"],
                    "rank": me["global_rank"], "pp": me["pp"],
                    "country": me["country"]}})
                return

            if path == "/api/ceiling":
                # One map at a time, on click: no bulk computation.
                try:
                    bid = int(body["beatmap_id"])
                    mods = str(body["mods"])
                except (KeyError, TypeError, ValueError):
                    self._json(400, {"error": "invalid request"})
                    return
                with Store(ctx.db) as store, MaxPpCalculator() as calc:
                    value = get_or_compute(store.conn, bid, mods, calc)
                with ctx._lock:
                    for payload in ctx._cache.values():
                        for m in payload.get("maps", []):
                            if m["beatmap_id"] == bid and m["mods"] == mods:
                                m["max_pp"] = value
                self._json(200, {"beatmap_id": bid, "mods": mods, "pp": value})
                return

            if path == "/api/crawl/stop":
                ok, message = runner.cancel()
                self._json(200 if ok else 409, {"ok": ok, "message": message})
                return

            if path != "/api/crawl":
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return

            def clamp(name, default, lo, hi, cast=int):
                try:
                    return max(lo, min(hi, cast(body.get(name, default))))
                except (TypeError, ValueError):
                    return default

            target = clamp("players", 0, 0, 20000) or None
            params = {
                "players": target,
                # In player mode the window is worked out inside the crawl,
                # where the country list says how deep the sweep still reaches.
                "spread": (None if target
                           else clamp("spread", ctx.spread or 1000, 50, 20000)),
                "fetch_n": clamp("fetch_n", 50, 10, 100),
                "target": clamp("target", 0, 0, 100000) or None,
            }
            ok, message = runner.start(params)
            self._json(200 if ok else 409,
                       {"ok": ok, "message": message, "params": params})

    return Handler


def serve(db: str | None = None, mode: str = "osu", port: int = 8000,
          open_browser: bool = True) -> None:
    db = db or default_db()
    ctx = Context(db, mode)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(ctx, CrawlRunner(ctx)))
    url = f"http://127.0.0.1:{port}/"
    print(f"ppfarmer running at {url}  (Ctrl+C to stop)")
    if not ctx.ready():
        print("Not configured yet - the page will ask for what it needs.")
    print(f"Data stored in {Path(db).parent}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()
