"""osu!api v2 client: client_credentials OAuth plus rate limiting.

The ppy/osu-api wiki sets the hard limit at 1200 req/min but asks callers to
stay under 60. We default to 60.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

TOKEN_URL = "https://osu.ppy.sh/oauth/token"
API_BASE = "https://osu.ppy.sh/api/v2"

# The ppy/osu-api wiki tolerates 1200 req/min but asks callers to stay
# under 60. Not a setting: going slower only wastes time, and going
# faster is discourteous for no gain worth having.
REQUESTS_PER_MINUTE = 60

# osu-web: Model::PER_PAGE = 50, RankingController::MAX_RESULTS = 10000
PAGE_SIZE = 50
MAX_RANKING_PAGES = 200


class OsuApiError(RuntimeError):
    pass


class RateLimiter:
    """Simple token bucket: at most `rpm` requests per minute, spread out."""

    def __init__(self, rpm: int) -> None:
        self.interval = 60.0 / max(1, rpm)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self.interval


class OsuClient:
    def __init__(self, client_id: str, client_secret: str,
                 rpm: int = REQUESTS_PER_MINUTE) -> None:
        if not client_id or not client_secret:
            raise OsuApiError(
                "No osu! API credentials. Create an OAuth client at "
                "https://osu.ppy.sh/home/account/edit and enter its ID and "
                "secret when the app asks for them."
            )
        self._id = client_id
        self._secret = client_secret
        self._limiter = RateLimiter(rpm)
        self._token: str | None = None
        self._expires_at = 0.0
        self._http = httpx.Client(timeout=30.0, headers={"User-Agent": "ppfarmer/0.1"})
        self.request_count = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OsuClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth ----------------------------------------------------------------

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        resp = self._http.post(
            TOKEN_URL,
            json={
                "client_id": self._id,
                "client_secret": self._secret,
                "grant_type": "client_credentials",
                "scope": "public",
            },
        )
        if resp.status_code != 200:
            raise OsuApiError(
                f"osu! rejected those credentials (HTTP {resp.status_code}). "
                "Check the client ID and secret, both copied from the same "
                "OAuth application."
            )
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 86400)
        return self._token

    # -- requests ------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error: str = ""
        for attempt in range(5):
            token = self._ensure_token()
            self._limiter.acquire()
            self.request_count += 1
            resp = self._http.get(
                f"{API_BASE}{path}",
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "x-api-version": "20220705",
                },
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                self._token = None
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(2 ** attempt)
                continue
            raise OsuApiError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        raise OsuApiError(f"GET {path} failed after 5 attempts ({last_error}).")

    # -- endpoints -----------------------------------------------------------

    def country_stats(self, mode: str = "osu", pages: int = 1) -> list[dict[str, Any]]:
        """Country ranking, in the order of osu.ppy.sh/rankings/osu/country.

        That order is by total performance, not by player count. One page holds
        50, so `pages=1` returns exactly the 50 countries shown on the site's
        first page.
        """
        out: list[dict[str, Any]] = []
        for page in range(1, min(pages, MAX_RANKING_PAGES) + 1):
            data = self.get(f"/rankings/{mode}/country", {"page": page})
            chunk = (data or {}).get("ranking", [])
            if not chunk:
                break
            out.extend(chunk)
            if len(chunk) < PAGE_SIZE:
                break
        return out

    def rankings_page(
        self, mode: str = "osu", country: str | None = None, page: int = 1
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"page": page}
        if country:
            params["country"] = country
        data = self.get(f"/rankings/{mode}/global", params)
        return (data or {}).get("ranking", [])

    def user(self, username_or_id: str, mode: str = "osu") -> dict[str, Any] | None:
        params = {}
        if not str(username_or_id).isdigit():
            params["key"] = "username"
        return self.get(f"/users/{username_or_id}/{mode}", params)

    def user_best(self, user_id: int, mode: str = "osu", limit: int = 50) -> list[dict[str, Any]]:
        """Top scores. The server clamps limit to 100 (UsersController::MAX_RESULTS)."""
        data = self.get(
            f"/users/{user_id}/scores/best",
            {"mode": mode, "limit": min(limit, 100)},
        )
        return data or []
