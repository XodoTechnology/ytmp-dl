"""YouTube Music client: wraps ytmusicapi with throttling, retries and the
custom discography browse that current ytmusicapi can't parse."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .util import log, parse_duration

try:
    from ytmusicapi import YTMusic
except ImportError:  # pragma: no cover
    YTMusic = None


class YTMError(Exception):
    pass


def _find_all(obj: Any, key: str, out: list):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            _find_all(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_all(v, key, out)


def _first(obj: Any, key: str):
    found: list = []
    _find_all(obj, key, found)
    return found[0] if found else None


def _nav(obj: Any, path: list):
    cur = obj
    for p in path:
        if isinstance(p, int):
            if not isinstance(cur, list) or p >= len(cur):
                return None
            cur = cur[p]
        elif isinstance(cur, dict):
            cur = cur.get(p)
            if cur is None:
                return None
        else:
            return None
    return cur


def _runs_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    runs = node.get("runs")
    if runs:
        return "".join(r.get("text", "") for r in runs)
    return node.get("simpleText") or ""


def _thumbnails_max(thumbs: list | None) -> str | None:
    if not thumbs:
        return None
    best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    return best.get("url")


class YTM:
    def __init__(self, auth_path: str | Path | None = None, min_interval: float = 0.35):
        if YTMusic is None:
            raise YTMError("ytmusicapi not installed")
        self.yt = YTMusic(auth=str(auth_path) if auth_path else None)
        self.min_interval = min_interval
        self._last = 0.0

    # ---------- plumbing ----------
    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    _RETRYABLE = ("429", "500", "502", "503", "504", "timed out", "timeout",
                  "connection", "temporarily", "try again", "eof", "chunked")

    def _call(self, fn, *args, **kwargs):
        last_exc = None
        for attempt in range(4):
            self._throttle()
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - YT is flaky, retry selectively
                last_exc = e
                msg = str(e).lower()
                if attempt == 3 or not any(s in msg for s in self._RETRYABLE):
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise YTMError(f"ytmusicapi call failed: {last_exc}") from last_exc

    @property
    def authed(self) -> bool:
        return self.yt.auth_type.name != "UNAUTHORIZED"

    # ---------- auth'd library ----------
    def account_info(self) -> dict:
        return self._call(self.yt.get_account_info)

    def liked_songs(self) -> dict:
        return self._call(self.yt.get_playlist, "LM", None)

    def history(self) -> list[dict]:
        return self._call(self.yt.get_history)

    def library_playlists(self) -> list[dict]:
        return self._call(self.yt.get_library_playlists, None)

    def library_songs(self) -> list[dict]:
        return self._call(self.yt.get_library_songs, None)

    def library_albums(self) -> list[dict]:
        return self._call(self.yt.get_library_albums, None)

    def playlist(self, playlist_id: str) -> dict:
        return self._call(self.yt.get_playlist, playlist_id, None)

    # ---------- public catalogue ----------
    def search_artist(self, name: str) -> dict | None:
        results = self._call(self.yt.search, name, "artists", None, 5)
        if not results:
            return None
        return results[0]

    def get_artist(self, channel_id: str) -> dict:
        try:
            return self._call(self.yt.get_artist, channel_id)
        except Exception as e:
            # YouTube serves a different header renderer for plain channels
            # (auto-generated/topic channels) — ytmusicapi hard-indexes
            # musicImmersiveHeaderRenderer and dies. Parse the raw browse
            # ourselves; we only need section browseIds + thumbnails.
            log.info("get_artist parse failed for %s (%s) — raw fallback",
                     channel_id, e)
            return self._get_artist_fallback(channel_id)

    def _get_artist_fallback(self, channel_id: str) -> dict:
        resp = self._browse(channel_id)
        out: dict[str, Any] = {"name": None, "thumbnails": [],
                               "songs": {"browseId": None}}

        header = _nav(resp, ["header"]) or {}
        for key in ("musicImmersiveHeaderRenderer", "musicVisualHeaderRenderer",
                    "musicDetailHeaderRenderer", "musicResponsiveHeaderRenderer"):
            h = header.get(key)
            if not h:
                continue
            out["name"] = out["name"] or _runs_text(_nav(h, ["title"]))
            thumbs = _nav(h, ["thumbnail", "musicThumbnailRenderer",
                              "thumbnail", "thumbnails"])
            if thumbs:
                out["thumbnails"] = thumbs
            ml = _nav(h, ["monthlyListenerCount", "runs", 0, "text"])
            if ml:
                out["monthlyListeners"] = ml
            break

        results = (
            _nav(resp, ["contents", "singleColumnBrowseResultsRenderer",
                        "tabs", 0, "tabRenderer", "content",
                        "sectionListRenderer", "contents"])
            or _nav(resp, ["contents", "twoColumnBrowseResultsRenderer",
                           "tabs", 0, "tabRenderer", "content",
                           "sectionListRenderer", "contents"])
            or [])

        cat_names = {
            "albums": "albums", "singles & eps": "singles", "singles": "singles",
            "shows": "shows", "episodes": "episodes", "videos": "videos",
            "playlists": "playlists", "podcasts": "podcasts",
            "related": "related",
        }
        for item in results:
            if not isinstance(item, dict):
                continue
            shelf = item.get("musicCarouselShelfRenderer")
            if shelf:
                head = _nav(shelf, ["header", "musicCarouselShelfBasicHeaderRenderer"])
                title_text = _runs_text(_nav(head, ["title"])).strip().lower()
                cat = cat_names.get(title_text)
                if not cat:
                    continue
                be = (_nav(head, ["title", "runs", 0,
                                  "navigationEndpoint", "browseEndpoint"])
                      or _nav(head, ["title", "navigationEndpoint",
                                     "browseEndpoint"]) or {})
                entry = out.setdefault(cat, {"browseId": None, "params": None,
                                             "results": []})
                entry["browseId"] = be.get("browseId") or entry["browseId"]
                entry["params"] = be.get("params") or entry["params"]
                continue
            mshelf = item.get("musicShelfRenderer")
            if mshelf:
                t = _runs_text(_nav(mshelf, ["title"])).strip().lower()
                if t in ("songs", "top songs"):
                    bid = _nav(mshelf, ["title", "runs", 0,
                                        "navigationEndpoint", "browseEndpoint",
                                        "browseId"])
                    if bid:
                        out["songs"] = {"browseId": bid}
        return out

    def get_album(self, browse_id: str) -> dict:
        return self._call(self.yt.get_album, browse_id)

    def get_song(self, video_id: str) -> dict:
        return self._call(self.yt.get_song, video_id)

    def _browse(self, browse_id: str, params: str | None = None) -> dict:
        body = {"browseId": browse_id}
        if params:
            body["params"] = params
        return self._call(self.yt._send_request, "browse", body)

    # ---------- discography ----------
    def discography(self, channel_id: str) -> dict:
        """Return {'releases': [{title,year,browse_id,kind}], 'songs_browse_id'}.

        Uses the artist 'Music' tab: Albums and Singles & EPs chips each map to
        a browse endpoint whose gridRenderer lists every release.
        """
        artist = self.get_artist(channel_id)
        out = {"releases": [], "songs_browse_id": None, "artist": artist}
        seen = set()
        for key, kind in (("albums", "album"), ("singles", "single"),
                          ("shows", "show"), ("episodes", "episode")):
            sec = artist.get(key) or {}
            bid, params = sec.get("browseId"), sec.get("params")
            if not bid or not params:
                continue
            for item in self._grid_items(bid, params):
                if item["browse_id"] in seen:
                    continue
                seen.add(item["browse_id"])
                item["kind"] = kind
                out["releases"].append(item)
        songs = artist.get("songs") or {}
        out["songs_browse_id"] = songs.get("browseId")
        return out

    def _grid_items(self, browse_id: str, params: str) -> list[dict]:
        """Parse musicTwoRowItemRenderer entries from a browse response."""
        resp = self._browse(browse_id, params)
        items: list[dict] = []
        renderers: list = []
        _find_all(resp, "musicTwoRowItemRenderer", renderers)
        for r in renderers:
            title = _runs_text(_nav(r, ["title"]))
            endpoint = _nav(r, ["navigationEndpoint", "browseEndpoint"]) or {}
            bid = endpoint.get("browseId")
            if not title or not bid:
                continue
            subtitle = _runs_text(_nav(r, ["subtitle"]))
            year = None
            m = re.search(r"\b(19|20)\d{2}\b", subtitle or "")
            if m:
                year = m.group(0)
            page_type = (
                endpoint.get("browseEndpointContextSupportedConfigs", {})
                .get("browseEndpointContextMusicConfig", {})
                .get("pageType", "")
            )
            thumbs = _nav(r, ["thumbnailRenderer", "musicThumbnailRenderer",
                              "thumbnail", "thumbnails"]) or []
            items.append({
                "title": title,
                "year": year,
                "browse_id": bid,
                "page_type": page_type,
                "thumbnail": _thumbnails_max(thumbs),
                "subtitle": subtitle,
            })
        return items

    # ---------- track normalisation ----------
    @staticmethod
    def norm_playlist_track(item: dict, source: str,
                            album_override: str | None = None,
                            album_browse: str | None = None,
                            track_no: int | None = None,
                            disc_no: int | None = None) -> dict | None:
        """Normalise a ytmusicapi playlist/album track dict."""
        vid = item.get("videoId")
        if not vid:
            return None
        artists = []
        for a in item.get("artists") or []:
            name = a.get("name") if isinstance(a, dict) else str(a)
            if name:
                artists.append({"name": name, "id": a.get("id") if isinstance(a, dict) else None})
        album = album_override
        abid = album_browse
        if album is None and isinstance(item.get("album"), dict):
            album = item["album"].get("name")
            abid = abid or item["album"].get("id")
        elif album is None and isinstance(item.get("album"), str):
            album = item["album"]
        return {
            "video_id": vid,
            "title": item.get("title") or "",
            "artists": artists,
            "album": album,
            "album_browse_id": abid,
            "ytm_track_number": track_no if track_no is not None else item.get("trackNumber"),
            "ytm_disc_number": disc_no if disc_no is not None else item.get("discNumber"),
            "duration_sec": item.get("duration_seconds") or parse_duration(item.get("duration")),
            "explicit": 1 if item.get("isExplicit") else 0,
            "is_available": item.get("isAvailable", True),
            "thumb": _thumbnails_max(item.get("thumbnails")),
            "source": source,
        }
