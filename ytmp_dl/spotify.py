"""Spotify Web API client (optional metadata fallback).
Requires a free app's client_id/client_secret (client-credentials flow)."""
from __future__ import annotations

import threading
import time
from typing import Any

import requests

from .util import log

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"


class Spotify:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.enabled = bool(client_id and client_secret)
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()

    def _get_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            r = self.session.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + data.get("expires_in", 3600)
            return self._token

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        if not self.enabled:
            return None
        try:
            r = self.session.get(
                f"{API}{path}", params=params,
                headers={"Authorization": f"Bearer {self._get_token()}"},
                timeout=20,
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.debug("spotify request failed: %s", e)
            return None

    def search_artist(self, name: str) -> dict | None:
        data = self._get("/search", {"q": name, "type": "artist", "limit": 5})
        items = ((data or {}).get("artists") or {}).get("items") or []
        return items[0] if items else None

    def artist_albums(self, artist_id: str) -> list[dict]:
        """Albums + singles for an artist (paginated)."""
        out, offset = [], 0
        while True:
            data = self._get(f"/artists/{artist_id}/albums",
                             {"include_groups": "album,single", "limit": 50,
                              "market": "US", "offset": offset})
            if not data:
                break
            items = data.get("items", [])
            out.extend(items)
            if len(out) >= data.get("total", 0) or not items:
                break
            offset += len(items)
            if offset > 500:
                break
        return out

    def album_tracks(self, album_id: str) -> list[dict]:
        out, offset = [], 0
        while True:
            data = self._get(f"/albums/{album_id}/tracks",
                             {"limit": 50, "offset": offset, "market": "US"})
            if not data:
                break
            items = data.get("items", [])
            out.extend(items)
            if len(out) >= data.get("total", 0) or not items:
                break
            offset += len(items)
            if offset > 1000:
                break
        return out

    def search_track(self, query: str) -> list[dict]:
        data = self._get("/search", {"q": query, "type": "track", "limit": 5})
        return ((data or {}).get("tracks") or {}).get("items") or []

    @staticmethod
    def image_url(images: list | None, size: str = "large") -> str | None:
        if not images:
            return None
        pref = {"small": 0, "medium": 1, "large": 2}.get(size, 2)
        idx = min(pref, len(images) - 1)
        return images[idx].get("url")
