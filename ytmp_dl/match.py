"""Metadata matching: map a (title, artists, duration) track onto canonical
MusicBrainz / Spotify release metadata for tagging and folder placement."""
from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path
from typing import Any

from .mb import MusicBrainz
from .spotify import Spotify
from .util import log, norm_title_variants, norm_text, parse_duration


# ---------------------------------------------------------------- title sim
def title_sim(a: str, b: str) -> float:
    va = norm_title_variants(a)
    vb = norm_title_variants(b)
    best = 0.0
    for x in va:
        for y in vb:
            if not x or not y:
                continue
            r = difflib.SequenceMatcher(None, x, y).ratio()
            # token-sort ratio helps word-order differences
            tx, ty = " ".join(sorted(x.split())), " ".join(sorted(y.split()))
            r = max(r, difflib.SequenceMatcher(None, tx, ty).ratio())
            best = max(best, r)
    return best


def _dur_score(sec_a: float | None, sec_b: float | None) -> float:
    if not sec_a or not sec_b:
        return 0.5
    diff = abs(sec_a - sec_b)
    return max(0.0, 1.0 - diff / 8.0)


def _first_isrc(rec: dict) -> str | None:
    isrcs = rec.get("isrcs")
    if isinstance(isrcs, list) and isrcs:
        return isrcs[0]
    if isinstance(isrcs, str):
        return isrcs
    return None


def _join_names(credit: list | dict | None) -> list[str]:
    """MusicBrainz artist-credit -> display names."""
    if isinstance(credit, dict):
        credit = [credit]
    names = []
    for c in credit or []:
        a = (c or {}).get("artist") or {}
        n = a.get("name")
        if n:
            names.append(n)
    return names


_STOP_TOKENS = {"the", "a", "an", "of", "and", "in", "on", "at", "to",
                "for", "with", "by", "from"}


def _release_rank(rel: dict) -> int:
    """Higher = more 'canonical' release for folder placement."""
    rg = rel.get("release-group") or {}
    pt = rg.get("primary-type")
    st = set(rg.get("secondary-types") or [])
    rank = 0
    if pt in ("Album", "Single", "EP"):
        rank += 3
    if rel.get("status") == "Official":
        rank += 1
    ca = rel.get("cover-art-archive") or {}
    if ca.get("front"):
        rank += 1
    if "Live" in st or "Demo" in st or "Soundtrack" in st or "Compilation" in st:
        rank -= 1
    return rank


# ------------------------------------------------------------------ catalog
class MBReleaseCatalog:
    """Flattened recording index for one MusicBrainz artist."""

    def __init__(self, mb: MusicBrainz, artist_mbid: str, artist_name: str):
        self.mb = mb
        self.artist_mbid = artist_mbid
        self.artist_name = artist_name
        self.entries: list[dict] = []
        self._index: dict[str, list[int]] = {}
        self._built = False

    def build(self):
        if self._built:
            return self
        releases = self.mb.artist_releases(self.artist_mbid)
        # rank releases so ties resolve toward canonical pressings; every
        # (recording, release) pair is kept — the same recording on a single
        # and an album are distinct placements the album hint can prefer
        releases = sorted(releases, key=lambda r: (-_release_rank(r),
                                                   r.get("date") or ""))
        seen_pair: set[tuple] = set()
        for rel in releases:
            rg = rel.get("release-group") or {}
            rel_artists = _join_names(rel.get("artist-credit"))
            rel_date = (rel.get("date") or "")[:10]
            rel_year = rel_date[:4] or None
            rel_genres = [g.get("name") for g in (rg.get("genres") or rel.get("genres") or [])]
            media = rel.get("media") or []
            total_discs = len(media)
            for disc_idx, medium in enumerate(media, start=1):
                tracks = medium.get("tracks") or []
                total_tracks = len(tracks) or medium.get("track-count")
                for t in tracks:
                    rec = t.get("recording") or {}
                    rec_id = rec.get("id")
                    pos = t.get("position") or t.get("number")
                    if (not rec or not rec_id
                            or (rec_id, rel.get("id"), disc_idx, pos) in seen_pair):
                        continue
                    seen_pair.add((rec_id, rel.get("id"), disc_idx, pos))
                    length_ms = rec.get("length") or t.get("length")
                    title = rec.get("title") or t.get("title") or ""
                    variants = norm_title_variants(title)
                    tokens = set()
                    for v in variants:
                        tokens.update(tk for tk in v.split() if tk not in _STOP_TOKENS)
                    entry = {
                        "recording_id": rec_id,
                        "title": title,
                        "_variants": variants,
                        "_tokens": tokens,
                        "length_sec": (length_ms / 1000.0) if length_ms else None,
                        "artists": _join_names(rec.get("artist-credit")) or rel_artists,
                        "isrc": _first_isrc(rec),
                        "release_id": rel.get("id"),
                        "release_group_id": rg.get("id"),
                        "release_title": rel.get("title") or rg.get("title"),
                        "_rel_variants": norm_title_variants(
                            rel.get("title") or rg.get("title")),
                        "release_date": rel_date,
                        "release_year": rel_year,
                        "release_type": rg.get("primary-type"),
                        "album_artists": rel_artists,
                        "track_number": t.get("position") or t.get("number"),
                        "disc_number": disc_idx if total_discs > 1 else None,
                        "total_discs": total_discs if total_discs > 1 else None,
                        "total_tracks": total_tracks,
                        "genre": rel_genres[0] if rel_genres else None,
                        "_secondary": set(rg.get("secondary-types") or []),
                        "_artist_mbid": self.artist_mbid,
                    }
                    idx = len(self.entries)
                    self.entries.append(entry)
                    for tok in tokens:
                        self._index.setdefault(tok, []).append(idx)
        self._built = True
        log.debug("MB catalog for %s: %d release-track entries", self.artist_name,
                  len(self.entries))
        return self

    def _candidates(self, title: str) -> list[dict]:
        qtokens = set()
        for v in norm_title_variants(title):
            qtokens.update(tk for tk in v.split() if tk not in _STOP_TOKENS)
        if not qtokens:
            return self.entries
        cand_idx: set[int] = set()
        for tok in qtokens:
            cand_idx.update(self._index.get(tok, ()))
        if not cand_idx:
            return []
        return [self.entries[i] for i in cand_idx]

    def match(self, title: str, artists: list[str], duration_sec: int | None,
              threshold: float = 0.72, album_hint: str | None = None) -> dict | None:
        if not self.entries:
            return None
        hint_variants = norm_title_variants(album_hint) if album_hint else []
        # per-variant token sets of the query for the subset pre-filter
        qtok_variants = []
        for v in norm_title_variants(title):
            qt = {t for t in v.split() if t not in _STOP_TOKENS}
            if qt:
                qtok_variants.append(qt)
        best, best_score = None, 0.0
        track_artists_norm = {norm_text(a) for a in artists if a}
        for e in self._candidates(title):
            # cheap prune: some query variant's tokens must all appear
            # in the entry's title tokens (entries may carry extra words)
            et = e["_tokens"]
            if qtok_variants and not any(qv <= et for qv in qtok_variants):
                continue
            ts = 0.0
            for x in norm_title_variants(title):
                for y in e["_variants"]:
                    if not x or not y:
                        continue
                    r = difflib.SequenceMatcher(None, x, y).ratio()
                    tx = " ".join(sorted(x.split()))
                    ty = " ".join(sorted(y.split()))
                    r = max(r, difflib.SequenceMatcher(None, tx, ty).ratio())
                    ts = max(ts, r)
            if ts < 0.5:
                continue
            ds = _dur_score(duration_sec, e["length_sec"])
            # hard-ish duration gate for close title matches
            if duration_sec and e["length_sec"] and abs(duration_sec - e["length_sec"]) > 12:
                continue
            score = 0.62 * ts + 0.38 * ds
            # prefer main-disc versions: bonus/live discs of box sets hold
            # alternate recordings of the same songs
            score -= 0.02 * max(0, (e.get("disc_number") or 1) - 1)
            # artist-credit agreement is a strong signal for correct release
            rec_art = {norm_text(a) for a in e["artists"]}
            alb_art = {norm_text(a) for a in (e.get("album_artists") or [])}
            if track_artists_norm & (rec_art | alb_art):
                score += 0.05
            elif rec_art or alb_art:
                score -= 0.04
            # prefer studio releases over compilations/live for equal matches
            if e["_secondary"] & {"Compilation", "Live", "Soundtrack", "Demo"}:
                score -= 0.04
            # YTM album context breaks ties toward the right release;
            # scaled by how closely the hint matches the release title
            if hint_variants and e.get("release_title"):
                best_hint = 0.0
                for hv in hint_variants:
                    for rv in e["_rel_variants"]:
                        if hv and rv:
                            best_hint = max(best_hint,
                                            difflib.SequenceMatcher(None, hv, rv).ratio())
                if best_hint > 0.9:
                    score += 0.15
                elif best_hint > 0.8:
                    score += 0.08
            if score > best_score:
                best, best_score = e, score
        if best and best_score >= threshold:
            res = {k: v for k, v in best.items() if not k.startswith("_")}
            res["score"] = round(best_score, 3)
            res["match_source"] = "musicbrainz"
            return res
        return None


# --------------------------------------------------------------- spotify cat
class SpotifyCatalog:
    def __init__(self, sp: Spotify, artist_id: str, artist_name: str):
        self.sp = sp
        self.artist_id = artist_id
        self.artist_name = artist_name
        self.entries: list[dict] = []
        self._built = False

    def build(self):
        if self._built:
            return self
        albums = self.sp.artist_albums(self.artist_id)
        for alb in albums:
            alb_id = alb.get("id")
            if not alb_id:
                continue
            tracks = self.sp.album_tracks(alb_id)
            images = alb.get("images") or []
            img = Spotify.image_url(images)
            alb_artists = [a.get("name", "") for a in alb.get("artists", [])]
            for t in tracks:
                self.entries.append({
                    "track_id": t.get("id"),
                    "title": t.get("name", ""),
                    "length_sec": (t.get("duration_ms") or 0) / 1000.0 or None,
                    "artists": [a.get("name", "") for a in t.get("artists", [])],
                    "album": alb.get("name"),
                    "album_id": alb_id,
                    "album_artists": alb_artists,
                    "track_number": t.get("track_number"),
                    "disc_number": t.get("disc_number"),
                    "total_tracks": alb.get("total_tracks"),
                    "date": (alb.get("release_date") or "")[:10],
                    "album_type": alb.get("album_group") or alb.get("album_type"),
                    "image_url": img,
                    "isrc": t.get("external_ids", {}).get("isrc"),
                })
        self._built = True
        log.debug("Spotify catalog for %s: %d tracks", self.artist_name, len(self.entries))
        return self

    def match(self, title: str, artists: list[str], duration_sec: int | None,
              threshold: float = 0.72) -> dict | None:
        if not self.entries:
            return None
        best, best_score = None, 0.0
        for e in self.entries:
            ts = title_sim(title, e["title"])
            if ts < 0.5:
                continue
            if duration_sec and e["length_sec"] and abs(duration_sec - e["length_sec"]) > 12:
                continue
            ds = _dur_score(duration_sec, e["length_sec"])
            score = 0.62 * ts + 0.38 * ds
            if score > best_score:
                best, best_score = e, score
        if best and best_score >= threshold:
            res = {k: v for k, v in best.items() if not k.startswith("_")}
            res["score"] = round(best_score, 3)
            res["match_source"] = "spotify"
            return res
        return None


# ------------------------------------------------------------------ matcher
class Matcher:
    """Orchestrates MB/Spotify lookups and per-artist catalogs."""

    def __init__(self, mb: MusicBrainz, spotify: Spotify | None,
                 threshold: float = 0.72):
        self.mb = mb
        self.sp = spotify
        self.threshold = threshold
        self._mb_artists: dict[str, dict | None] = {}   # name_key -> {mbid,name}
        self._mb_cats: dict[str, MBReleaseCatalog] = {}
        self._sp_artists: dict[str, dict | None] = {}
        self._sp_cats: dict[str, SpotifyCatalog] = {}

    # ----- artist resolution -----
    def mb_artist(self, name: str) -> dict | None:
        key = norm_text(name)
        if key in self._mb_artists:
            return self._mb_artists[key]
        artist = self.mb.search_artist(name) if self.mb.enabled else None
        self._mb_artists[key] = artist
        return artist

    def spotify_artist(self, name: str) -> dict | None:
        if not self.sp or not self.sp.enabled:
            return None
        key = norm_text(name)
        if key in self._sp_artists:
            return self._sp_artists[key]
        artist = self.sp.search_artist(name)
        self._sp_artists[key] = artist
        return artist

    def _candidate_names(self, artists: list[str]) -> list[str]:
        """Artist names to try, primary first."""
        out = []
        for a in artists:
            a = (a or "").strip()
            if a and a not in out:
                out.append(a)
        return out

    # ----- main entry -----
    def match_track(self, track: dict) -> dict | None:
        """track: {title, artists:[names], duration_sec, album?}. Returns meta."""
        title = track.get("title") or ""
        artists = self._candidate_names(track.get("artists") or [])
        dur = track.get("duration_sec")
        album_hint = track.get("album")

        # 1) MusicBrainz catalog of each credited artist
        for name in artists:
            art = self.mb_artist(name)
            if not art or not art.get("id"):
                continue
            cat = self._mb_cats.get(art["id"])
            if cat is None:
                cat = MBReleaseCatalog(self.mb, art["id"], art.get("name") or name).build()
                self._mb_cats[art["id"]] = cat
            hit = cat.match(title, artists, dur, self.threshold, album_hint)
            if hit:
                return self._mb_result(hit)

        # 2) MB free recording search (features/soundtrack edge cases)
        if self.mb.enabled and artists:
            q = f'recording:"{title}" AND artist:"{artists[0]}"'
            recs = self.mb.search_recording(q)
            for rec in recs[:3]:
                ts = title_sim(title, rec.get("title", ""))
                rl = (rec.get("length") or 0) / 1000.0
                if ts >= 0.8 and (not dur or not rl or abs(dur - rl) <= 10):
                    rels = rec.get("releases") or []
                    rel = rels[0] if rels else {}
                    return self._mb_result({
                        "recording_id": rec.get("id"),
                        "title": rec.get("title"),
                        "length_sec": rl or None,
                        "artists": _join_names(rec.get("artist-credit")),
                        "isrc": _first_isrc(rec),
                        "release_id": rel.get("id"),
                        "release_title": rel.get("title"),
                        "release_date": (rel.get("date") or "")[:10],
                        "release_year": (rel.get("date") or "")[:4] or None,
                        "album_artists": _join_names(rel.get("artist-credit")),
                        "track_number": None, "disc_number": None,
                        "total_tracks": None, "genre": None,
                        "score": ts, "match_source": "musicbrainz",
                    })

        # 3) Spotify catalog fallback
        if self.sp and self.sp.enabled:
            for name in artists:
                art = self.spotify_artist(name)
                if not art or not art.get("id"):
                    continue
                cat = self._sp_cats.get(art["id"])
                if cat is None:
                    cat = SpotifyCatalog(self.sp, art["id"], art.get("name") or name).build()
                    self._sp_cats[art["id"]] = cat
                hit = cat.match(title, artists, dur, self.threshold)
                if hit:
                    return self._spotify_result(hit)
            # spotify track search last resort
            q = " ".join([title] + artists[:1])
            for t in (self.sp.search_track(q) or [])[:3]:
                ts = title_sim(title, t.get("name", ""))
                dl = (t.get("duration_ms") or 0) / 1000.0
                if ts >= 0.85 and (not dur or not dl or abs(dur - dl) <= 10):
                    alb = t.get("album") or {}
                    return self._spotify_result({
                        "track_id": t.get("id"), "title": t.get("name"),
                        "length_sec": dl or None,
                        "artists": [a.get("name", "") for a in t.get("artists", [])],
                        "album": alb.get("name"), "album_id": alb.get("id"),
                        "album_artists": [a.get("name", "") for a in alb.get("artists", [])],
                        "track_number": t.get("track_number"),
                        "disc_number": t.get("disc_number"),
                        "total_tracks": alb.get("total_tracks"),
                        "date": (alb.get("release_date") or "")[:10],
                        "image_url": Spotify.image_url(alb.get("images")),
                        "isrc": (t.get("external_ids") or {}).get("isrc"),
                        "score": ts, "match_source": "spotify",
                    })
        return None

    # ----- result normalisation -----
    @staticmethod
    def _mb_result(e: dict) -> dict:
        album_artists = e.get("album_artists") or e.get("artists") or []
        return {
            "source": "musicbrainz",
            "title": e.get("title"),
            "artists": e.get("artists") or [],
            "album_artist": "; ".join(album_artists) if album_artists else None,
            "album": e.get("release_title"),
            "track_number": e.get("track_number"),
            "disc_number": e.get("disc_number"),
            "total_tracks": e.get("total_tracks"),
            "total_discs": e.get("total_discs"),
            "date": e.get("release_date") or (str(e.get("release_year")) if e.get("release_year") else None),
            "year": e.get("release_year"),
            "isrc": e.get("isrc"),
            "genre": e.get("genre"),
            "release_mbid": e.get("release_id"),
            "recording_id": e.get("recording_id"),
            "release_group_id": e.get("release_group_id"),
            "mb_artist_id": e.get("_artist_mbid"),
            "image_url": None,
            "score": e.get("score"),
        }

    @staticmethod
    def _spotify_result(e: dict) -> dict:
        album_artists = e.get("album_artists") or e.get("artists") or []
        return {
            "source": "spotify",
            "title": e.get("title"),
            "artists": e.get("artists") or [],
            "album_artist": "; ".join(album_artists) if album_artists else None,
            "album": e.get("album"),
            "track_number": e.get("track_number"),
            "disc_number": e.get("disc_number"),
            "total_tracks": e.get("total_tracks"),
            "total_discs": None,
            "date": e.get("date"),
            "year": (e.get("date") or "")[:4] or None,
            "isrc": e.get("isrc"),
            "genre": None,
            "release_mbid": None,
            "recording_id": e.get("track_id"),
            "release_group_id": None,
            "album_id": e.get("album_id"),
            "mb_artist_id": None,
            "image_url": e.get("image_url"),
            "score": e.get("score"),
        }
