"""MusicBrainz WS2 client: artist search, full release discography with
recordings/ISRCs, and cover art lookups. Serial, rate-limited (1 req/sec)."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import requests

from .util import log

BASE = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
UA = "ytmp-dl/0.1 (personal music library archiver)"


class MBError(Exception):
    pass


class MusicBrainz:
    def __init__(self, cache_dir: Path, rate_interval: float = 1.1, enabled: bool = True):
        self.enabled = enabled
        self.rate_interval = rate_interval
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self._lock = threading.Lock()
        self._last = 0.0

    # ---------- http ----------
    def _get(self, url: str, params: dict | None = None, raw: bool = False,
             cache_key: str | None = None, ttl: float = 86400 * 7) -> Any:
        if cache_key:
            cf = self.cache_dir / f"{cache_key}.json"
            if cf.is_file() and (time.time() - cf.stat().st_mtime) < ttl:
                try:
                    return json.loads(cf.read_text())
                except Exception:
                    pass
        with self._lock:
            wait = self.rate_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        for attempt in range(4):
            try:
                r = self.session.get(url, params=params, timeout=30)
                if r.status_code == 404:
                    return None
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                data = r.content if raw else r.json()
                if cache_key:
                    try:
                        (self.cache_dir / f"{cache_key}.json").write_text(
                            data.decode("utf-8", "replace") if raw else json.dumps(data))
                    except Exception:
                        pass
                return data
            except requests.RequestException as e:
                if attempt == 3:
                    raise MBError(f"musicbrainz request failed: {e}") from e
                time.sleep(1.5 * (attempt + 1))
        raise MBError("musicbrainz request failed")

    def _ws(self, endpoint: str, params: dict, cache_key: str | None = None) -> dict | None:
        params = dict(params)
        params["fmt"] = "json"
        return self._get(f"{BASE}/{endpoint}/", params=params, cache_key=cache_key)

    # ---------- artist ----------
    def search_artist(self, name: str) -> dict | None:
        """Best-guess artist MBID for a name (ranked by name similarity)."""
        import difflib
        data = self._ws("artist", {"query": name, "limit": 10},
                        cache_key=f"artist-search-{hashlib.md5(name.encode()).hexdigest()[:12]}")
        artists = (data or {}).get("artists", [])
        if not artists:
            return None
        q = name.strip().lower()

        def score(a):
            n = (a.get("name") or "").strip().lower()
            if n == q:
                return 2.0
            return difflib.SequenceMatcher(None, q, n).ratio()

        return max(artists, key=score)

    def artist(self, mbid: str) -> dict | None:
        return self._ws("artist", {"mbid": mbid}, cache_key=f"artist-{mbid}")

    # ---------- releases / discography ----------
    def artist_releases(self, artist_mbid: str) -> list[dict]:
        """All releases credited to the artist, with full tracklists.

        NOTE: heavy inc makes MB size-cap pages well below `limit`, so
        pagination must follow release-count/release-offset, not batch size.
        """
        releases: list[dict] = []
        seen: set[str] = set()
        offset = 0
        limit = 100
        total: int | None = None
        while True:
            data = self._ws(
                "release",
                {
                    "artist": artist_mbid,
                    "inc": "recordings+isrcs+media+artist-credits+release-groups+genres",
                    "limit": limit,
                    "offset": offset,
                },
                cache_key=f"releases-{artist_mbid}-{offset}",
            )
            if not data:
                break
            if total is None:
                total = data.get("release-count")
            batch = data.get("releases", [])
            fresh = [r for r in batch if r.get("id") not in seen]
            for r in fresh:
                seen.add(r.get("id"))
            releases.extend(fresh)
            if not batch:
                break
            offset += len(batch)
            if total is not None and offset >= total:
                break
            if total is None and len(batch) < limit:
                break
            if offset > 5000:  # safety
                break
        return releases

    def release(self, mbid: str) -> dict | None:
        return self._get(f"{BASE}/release/{mbid}",
                         params={"inc": "recordings+media+artist-credits", "fmt": "json"},
                         cache_key=f"release-{mbid}")

    def search_recording(self, query: str) -> list[dict]:
        data = self._ws("recording",
                        {"query": query, "limit": 5,
                         "inc": "releases+artist-credits+isrcs"},
                        cache_key=f"rec-search-{hashlib.md5(query.encode()).hexdigest()[:12]}")
        return (data or {}).get("recordings", [])

    # ---------- cover art ----------
    def cover_art(self, release_mbid: str | None = None,
                  release_group_id: str | None = None,
                  size: str = "500") -> bytes | None:
        """Fetch cover image bytes (front). CAA supports release + release-group."""
        urls = []
        if release_mbid:
            urls += [f"{CAA}/release/{release_mbid}/front-{size}",
                     f"{CAA}/release/{release_mbid}/front"]
        if release_group_id:
            urls += [f"{CAA}/release-group/{release_group_id}/front-{size}",
                     f"{CAA}/release-group/{release_group_id}/front"]
        for url in urls:
            try:
                time.sleep(0.3)
                r = self.session.get(url, timeout=30, allow_redirects=True)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                    return r.content
            except requests.RequestException:
                continue
        return None

    @staticmethod
    def mb_credit_names(credit: list | None) -> list[str]:
        names = []
        for c in credit or []:
            a = c.get("artist") or {}
            if a.get("name"):
                names.append(a["name"])
        return names
