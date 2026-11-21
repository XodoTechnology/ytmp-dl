"""Pipeline orchestration: discover -> resolve artists -> discography -> download."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .auth import ensure_auth
from .db import DB
from .grab import DownloadError, Grabber, fetch_image
from .match import Matcher
from .mb import MusicBrainz
from .spotify import Spotify
from .util import join_artists, log, norm_text
from .ytm import YTM, YTMError


class Pipeline:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.root
        self.db = DB(cfg.path("state_db"))
        self.cache_dir = cfg.path("cache_dir")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._ytm: YTM | None = None
        self._mb: MusicBrainz | None = None
        self._sp: Spotify | None = None
        self._matcher: Matcher | None = None
        self._grab: Grabber | None = None

    # ---------------------------------------------------------- lazy clients
    def ytm(self, require_auth: bool = False) -> YTM:
        if self._ytm is None:
            auth_path = self.cfg.path("state_db").with_suffix(".auth.json")
            if auth_path.is_file():
                self._ytm = YTM(auth_path=auth_path)
            elif (self.cfg.path("cookies_file")).is_file():
                ensure_auth(self.cfg)
                self._ytm = YTM(auth_path=auth_path)
            else:
                self._ytm = YTM()
        if require_auth and not self._ytm.authed:
            raise YTMError(
                "This step needs your YouTube Music login. Export cookies.txt "
                "from your browser into the project root (see README), then run "
                "`ytmp-dl auth`."
            )
        return self._ytm

    def mb(self) -> MusicBrainz:
        if self._mb is None:
            self._mb = MusicBrainz(
                self.cache_dir / "mb",
                rate_interval=float(self.cfg.get("metadata", "mb_rate_interval", 1.1)),
                enabled=bool(self.cfg.get("metadata", "musicbrainz", True)),
            )
        return self._mb

    def spotify(self) -> Spotify | None:
        if self._sp is None:
            self._sp = Spotify(
                self.cfg.get("spotify", "client_id", ""),
                self.cfg.get("spotify", "client_secret", ""),
            )
        return self._sp

    def matcher(self) -> Matcher:
        if self._matcher is None:
            self._matcher = Matcher(
                self.mb(), self.spotify(),
                threshold=float(self.cfg.get("metadata", "match_threshold", 0.72)),
            )
        return self._matcher

    def grab(self) -> Grabber:
        if self._grab is None:
            self._grab = Grabber(self.cfg, self.db)
        return self._grab

    def _opt(self, section: str, key: str, default=None):
        """GUI-persisted override (kv) wins over config.toml."""
        v = self.db.kv_get(f"opt.{section}.{key}")
        return default if v is None else v

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _artist_names(track_artists: list) -> list[str]:
        names = []
        for a in track_artists or []:
            n = a.get("name") if isinstance(a, dict) else str(a)
            if n:
                names.append(n)
        return names

    @staticmethod
    def _track_fields(norm: dict) -> dict:
        artists = norm.get("artists") or []
        names = Pipeline._artist_names(artists)
        primary = names[0] if names else "Unknown Artist"
        return {
            "title": norm.get("title"),
            "artists": json.dumps(artists),
            "artist_key": norm_text(primary),
            "album": norm.get("album"),
            "album_browse_id": norm.get("album_browse_id"),
            "ytm_track_number": norm.get("ytm_track_number"),
            "ytm_disc_number": norm.get("ytm_disc_number"),
            "duration_sec": norm.get("duration_sec"),
            "explicit": norm.get("explicit"),
            "artwork_url": norm.get("thumb"),
        }

    def _register_track(self, norm: dict, source: str) -> bool:
        fields = self._track_fields(norm)
        is_new = self.db.upsert_track(norm["video_id"], source, **fields)
        if is_new:
            primary = self._artist_names(
                json.loads(fields["artists"]))[0] \
                if fields["artists"] != "[]" else None
            if primary and primary != "Unknown Artist":
                aid = self.db.upsert_artist(primary)
                artists = json.loads(fields["artists"])
                pid = artists[0].get("id") if artists and isinstance(artists[0], dict) else None
                if pid:
                    self.db.set_artist(aid, ytm_channel_id=pid)
        return is_new

    # ------------------------------------------------------------- discover
    def discover(self) -> dict:
        ytm = self.ytm(require_auth=True)
        dcfg = self.cfg.section("discover")
        stats = {"liked": 0, "history": 0, "playlists": 0, "tracks_new": 0,
                 "tracks_seen": 0}

        def ingest(items, source, album=None, album_browse=None):
            n = 0
            for it in items or []:
                norm = YTM.norm_playlist_track(it, source, album, album_browse)
                if not norm:
                    continue
                if norm.get("is_available") is False:
                    fields = self._track_fields(norm)
                    fields["status"] = "skipped"
                    self.db.upsert_track(norm["video_id"], source, **fields)
                    continue
                if self._register_track(norm, source):
                    stats["tracks_new"] += 1
                stats["tracks_seen"] += 1
                n += 1
            return n

        # liked songs
        if self._opt("discover", "include_liked", True):
            try:
                pl = ytm.liked_songs()
                n = ingest((pl or {}).get("tracks"), "liked")
                stats["liked"] = n
                log.info("liked songs: %d tracks", n)
            except Exception as e:
                log.warning("liked songs failed: %s", e)

        # history
        if self._opt("discover", "include_history", True):
            try:
                hist = ytm.history()
                n = ingest(hist, "history")
                stats["history"] = n
                log.info("history: %d tracks", n)
            except Exception as e:
                log.warning("history failed: %s", e)

        # library songs / albums (optional)
        if self._opt("discover", "include_library_songs", False):
            try:
                for it in ytm.library_songs() or []:
                    vid = it.get("videoId")
                    if vid:
                        self._register_track(
                            {"video_id": vid, "title": it.get("title"),
                             "artists": it.get("artists"), "album": it.get("album", {}).get("name") if isinstance(it.get("album"), dict) else it.get("album"),
                             "duration_sec": it.get("duration_seconds")},
                            "library_song")
            except Exception as e:
                log.warning("library songs failed: %s", e)
        if self._opt("discover", "include_library_albums", False):
            try:
                for it in ytm.library_albums() or []:
                    bid = it.get("browseId") or (it.get("album") or {}).get("id")
                    if bid:
                        self.db.upsert_release(bid, kind="album",
                                               title=it.get("title") or (it.get("album") or {}).get("name"))
            except Exception as e:
                log.warning("library albums failed: %s", e)

        # library playlists
        if self._opt("discover", "include_library_playlists", True):
            skip = [p.lower() for p in dcfg.get("skip_playlist_patterns", [])]
            try:
                pls = ytm.library_playlists() or []
            except Exception as e:
                log.warning("library playlists failed: %s", e)
                pls = []
            for p in pls:
                pid = p.get("id") or p.get("playlistId")
                title = p.get("title") or p.get("name") or ""
                if not pid or pid == "LM":
                    continue
                if any(s in title.lower() for s in skip):
                    continue
                try:
                    pl = ytm.playlist(pid)
                    tracks = (pl or {}).get("tracks") or []
                    self.db.upsert_playlist(pid, title=title,
                                            track_count=len(tracks), kind="library")
                    n = ingest(tracks, f"playlist:{pid}")
                    stats["playlists"] += 1
                    log.info("playlist '%s': %d tracks", title, n)
                except Exception as e:
                    log.warning("playlist %s failed: %s", pid, e)

        # manual playlists from config
        for raw in dcfg.get("manual_playlists", []) or []:
            raw = str(raw).strip()
            pid = self._extract_playlist_id(raw) or (
                raw if self._PLAYLIST_PREFIX.fullmatch(raw) else None)
            if not pid:
                log.warning("skipping unrecognised playlist entry: %r", raw)
                continue
            try:
                pl = ytm.playlist(pid)
                self.db.upsert_playlist(pid, title=(pl or {}).get("title"),
                                        track_count=len((pl or {}).get("tracks") or []),
                                        kind="manual")
                ingest((pl or {}).get("tracks"), f"playlist:{pid}")
                log.info("manual playlist %s: done", pid)
            except Exception as e:
                log.warning("manual playlist %s failed: %s", pid, e)

        # manual artists -> straight into artists table for discography
        for name in dcfg.get("manual_artists", []) or []:
            self.db.upsert_artist(name.strip())

        return stats

    # ------------------------------------------------------- artist resolve
    def resolve_artists(self) -> dict:
        ytm = self.ytm(require_auth=False)
        # retry 'failed' too — earlier failures were often transient parser
        # errors that a later run can fix
        artists = self.db.q(
            "SELECT * FROM artists WHERE status IN ('pending','failed') "
            "ORDER BY name_key")
        done = failed = 0
        for a in artists:
            name = a["name"]
            try:
                channel_id = a["ytm_channel_id"]
                if not channel_id:
                    # try to find channel id from known tracks' artist ids
                    channel_id = self._channel_id_from_tracks(a["name_key"])
                if not channel_id:
                    res = ytm.search_artist(name)
                    channel_id = (res or {}).get("browseId")
                if not channel_id:
                    self.db.set_artist(a["id"], status="failed",
                                       detail="no YTM artist page found")
                    failed += 1
                    continue
                info = ytm.get_artist(channel_id)
                thumb = None
                thumbs = info.get("thumbnails") or []
                if thumbs:
                    thumb = max(thumbs, key=lambda t: t.get("width", 0)).get("url")
                self.db.set_artist(
                    a["id"], status="resolved", ytm_channel_id=channel_id,
                    thumb_url=thumb,
                    detail=f"{info.get('monthlyListeners') or ''} listeners")
                done += 1
            except Exception as e:
                self.db.set_artist(a["id"], status="failed", detail=str(e)[:300])
                failed += 1
                log.warning("artist %r failed: %s", name, e)
        log.info("artists resolved: %d ok, %d failed", done, failed)
        return {"resolved": done, "failed": failed}

    def _channel_id_from_tracks(self, name_key: str) -> str | None:
        rows = self.db.q(
            "SELECT artists FROM tracks WHERE artist_key=? LIMIT 100",
            (name_key,),
        )
        want = name_key
        for r in rows:
            try:
                for a in json.loads(r["artists"] or "[]"):
                    if isinstance(a, dict) and norm_text(a.get("name")) == want and a.get("id"):
                        return a["id"]
            except Exception:
                continue
        return None

    # ------------------------------------------------------------ discography
    def _library_artist_ids(self) -> set[int]:
        """Artists that have >=1 track from a library source (liked,
        history, playlists, manual adds) — i.e. not discography bulk."""
        rows = self.db.q(
            """SELECT DISTINCT a.id FROM artists a
               JOIN tracks t ON t.artist_key = a.name_key
               WHERE t.source NOT LIKE 'album:%' AND t.source != 'songs'""")
        return {r["id"] for r in rows}

    def _selected_artist_ids(self) -> list[int]:
        v = self.db.kv_get("discography.selected") or []
        return [int(x) for x in v]

    def set_artist_selected(self, ids, selected: bool) -> int:
        cur = set(self._selected_artist_ids())
        ids = [int(i) for i in ids]
        if selected:
            cur.update(ids)
        else:
            cur.difference_update(ids)
        self.db.kv_set("discography.selected", sorted(cur))
        return len(cur)

    def discography(self) -> dict:
        stats = {"artists": 0, "releases": 0, "tracks_new": 0}
        mode = str(self._opt("discography", "mode", "selected")).lower()
        if mode == "off":
            log.info("discography expansion is off — check artists in the "
                     "Artists tab or change the mode in Options")
            return {**stats, "mode": mode}
        ytm = self.ytm(require_auth=False)
        artists = [a for a in self.db.artists("resolved")]
        # dedupe by channel id
        seen_ch, unique = set(), []
        for a in artists:
            ch = a["ytm_channel_id"]
            if ch and ch in seen_ch:
                continue
            seen_ch.add(ch)
            unique.append(a)
        if mode == "selected":
            sel = set(self._selected_artist_ids())
            unique = [a for a in unique if a["id"] in sel]
            if not unique:
                log.info("discography mode=selected but no artists are "
                         "checked — tick artists in the Artists tab first")
        elif mode == "library":
            lib = self._library_artist_ids()
            unique = [a for a in unique if a["id"] in lib]
            log.info("discography mode=library: %d of %d artists",
                     len(unique), len(artists))
        # mode "all" expands every resolved artist
        for a in unique:
            try:
                stats["artists"] += 1
                self._discography_for(ytm, a, stats)
            except Exception as e:
                log.warning("discography for %r failed: %s", a["name"], e)
                self.db.set_artist(a["id"], detail=f"discography error: {str(e)[:200]}")
        return {**stats, "mode": mode}

    def _discography_for(self, ytm: YTM, artist_row, stats: dict):
        aid = artist_row["id"]
        ch = artist_row["ytm_channel_id"]
        disco = ytm.discography(ch)

        # releases (albums + singles/eps)
        for rel in disco.get("releases") or []:
            bid = rel["browse_id"]
            rid = self.db.upsert_release(
                bid, artist_id=aid, kind=rel.get("kind"),
                title=rel.get("title"), year=rel.get("year"))
            stats["releases"] += 1
            # expand release -> tracks (once)
            row = self.db.conn.execute(
                "SELECT status FROM releases WHERE id=?", (rid,)
            ).fetchone()
            if row and row["status"] == "done":
                continue
            try:
                album = ytm.get_album(bid)
            except Exception as e:
                self.db.set_release(rid, status="failed", detail=str(e)[:200])
                continue
            tracks = album.get("tracks") or []
            album_title = album.get("title") or rel.get("title")
            for t in tracks:
                norm = YTM.norm_playlist_track(
                    t, f"album:{bid}", album_override=album_title,
                    album_browse=bid)
                if norm and self._register_track(norm, f"album:{bid}"):
                    stats["tracks_new"] += 1
            self.db.set_release(rid, status="done", track_count=len(tracks))

        # 'songs' tab -> loose tracks not on any release
        sbid = disco.get("songs_browse_id")
        if sbid:
            try:
                pl = ytm.playlist(sbid)
                for it in (pl or {}).get("tracks") or []:
                    norm = YTM.norm_playlist_track(it, "songs")
                    if norm and self._register_track(norm, "songs"):
                        stats["tracks_new"] += 1
            except Exception as e:
                log.debug("songs tab for %r failed: %s", artist_row["name"], e)

    # -------------------------------------------------------------- download
    def _fallback_meta(self, track) -> dict:
        artists = []
        try:
            artists = [a.get("name", "") for a in json.loads(track["artists"] or "[]")
                       if isinstance(a, dict)]
        except Exception:
            pass
        primary = artists[0] if artists else (track["artist_key"] or "Unknown Artist").title()
        album = track["album"]
        year = None
        if track["album_browse_id"]:
            row = self.db.q1(
                "SELECT title, year FROM releases WHERE browse_id=?",
                (track["album_browse_id"],))
            if row:
                album = album or row["title"]
                year = row["year"]
        return {
            "source": "fallback",
            "title": track["title"],
            "artists": artists,
            "artist": join_artists(artists),
            "album_artist": primary,
            "album": album or "Unknown Album",
            "track_number": track["ytm_track_number"],
            "disc_number": track["ytm_disc_number"],
            "total_tracks": None, "total_discs": None,
            "date": None, "year": year,
            "isrc": None, "genre": None,
            "image_url": None, "release_mbid": None,
            "recording_id": None, "score": None,
        }

    def _meta_for(self, track, matcher: Matcher) -> dict:
        # cached match?
        if track["meta"]:
            try:
                return json.loads(track["meta"])
            except Exception:
                pass
        artists = []
        try:
            artists = [a.get("name", "") for a in json.loads(track["artists"] or "[]")
                       if isinstance(a, dict)]
        except Exception:
            pass
        try:
            hit = matcher.match_track({
                "title": track["title"],
                "artists": artists,
                "duration_sec": track["duration_sec"],
                "album": track["album"],
            })
        except Exception as e:
            # a transient MusicBrainz failure must not kill the download —
            # tag with fallback metadata and move on
            log.warning("match failed for %r: %s — using fallback meta",
                        track["title"], e)
            hit = None
        if hit:
            meta = dict(hit)
            meta["artist"] = join_artists(hit.get("artists") or artists)
            meta.setdefault("album_artist",
                            join_artists(hit.get("artists") or artists))
            # persist match fields
            fields = {
                "mb_recording_id": hit.get("recording_id"),
                "mb_release_id": hit.get("release_mbid"),
                "mb_release_group_id": hit.get("release_group_id"),
                "mb_album": hit.get("album"),
                "mb_album_artist": hit.get("album_artist"),
                "mb_track_number": hit.get("track_number"),
                "mb_disc_number": hit.get("disc_number"),
                "mb_total_tracks": hit.get("total_tracks"),
                "mb_date": hit.get("date"),
                "isrc": hit.get("isrc"),
                "genre": hit.get("genre"),
                "artwork_url": hit.get("image_url") or track["artwork_url"],
                "meta": json.dumps(meta),
            }
            self.db.set_track(track["id"], **fields)
            return meta
        meta = self._fallback_meta(track)
        self.db.set_track(track["id"], meta=json.dumps(meta))
        return meta

    def _artwork(self, track, meta: dict, matcher: Matcher) -> bytes | None:
        # 1. remote url (spotify/ytm thumb)
        url = meta.get("image_url") or track["artwork_url"]
        key = None
        if url:
            key = "url-" + hashlib.md5(url.encode()).hexdigest()[:16]
        elif meta.get("release_mbid"):
            key = f"mb-{meta['release_mbid']}"
        if key:
            cached = self.cache_dir / "art" / f"{key}.jpg"
            if cached.is_file():
                try:
                    return cached.read_bytes()
                except OSError:
                    pass
        img = None
        if url:
            img = fetch_image(url)
        if not img and self.cfg.get("metadata", "musicbrainz", True) and (
                meta.get("release_mbid") or meta.get("release_group_id")):
            try:
                img = self.mb().cover_art(
                    release_mbid=meta.get("release_mbid"),
                    release_group_id=meta.get("release_group_id"),
                    size=str(self.cfg.get("artwork", "size", "500")))
            except Exception as e:
                log.debug("cover art fetch failed: %s", e)
        if not img and track["artwork_url"]:
            img = fetch_image(track["artwork_url"])
        if img and key:
            adir = self.cache_dir / "art"
            adir.mkdir(parents=True, exist_ok=True)
            try:
                (adir / f"{key}.jpg").write_bytes(img)
            except OSError:
                pass
        return img

    def _process_one(self, track, matcher, grab) -> str:
        video_id = track["video_id"]
        try:
            meta = self._meta_for(track, matcher)
            meta = dict(meta)
            # same canonical recording already downloaded via another video?
            rid = meta.get("recording_id")
            if rid:
                dup = self.db.q1(
                    "SELECT video_id FROM tracks WHERE mb_recording_id=? "
                    "AND status='done' AND id != ?", (rid, track["id"]))
                if dup:
                    self.db.set_track(
                        track["id"], status="skipped",
                        error=f"duplicate recording of {dup['video_id']}")
                    log.info("skip dup: %s = %s", video_id, dup["video_id"])
                    return "skipped"
            meta["_image_bytes"] = self._artwork(track, meta, matcher)
            path = grab.download_track(track, meta)
            self.db.set_track(
                track["id"], status="done", file_path=str(path),
                file_ext=path.suffix.lstrip("."), file_size=path.stat().st_size,
                error=None)
            log.info("done: %s — %s", track["artist_key"], path.name)
            return "done"
        except DownloadError as e:
            self._fail(track, str(e))
            return "failed"
        except Exception as e:  # noqa: BLE001
            self._fail(track, f"{type(e).__name__}: {e}")
            return "failed"

    def remux_containers(self) -> dict:
        """Re-box .webm/.mkv downloads to .ogg in place (ffmpeg -c copy),
        retag from stored metadata + sibling cover.jpg, update the DB.
        For files grabbed before ffmpeg was reachable."""
        from .grab import remux_to_ogg, tag_file, audio_codec
        grab = self.grab()
        stats = {"remuxed": 0, "failed": 0, "skipped": 0}
        if not grab._ffmpeg_ok:
            log.error("ffmpeg/ffprobe not found — cannot remux")
            return stats
        outdir = self.cfg.path("output_dir")
        if not outdir.is_dir():
            return stats
        for src in sorted(outdir.rglob("*")):
            if not src.is_file() or src.suffix.lower() not in (".webm", ".mkv"):
                continue
            dst = src.with_suffix(".ogg")
            if dst.exists():
                stats["skipped"] += 1
                continue
            if not remux_to_ogg(src, dst):
                stats["failed"] += 1
                log.warning("remux failed: %s", src)
                continue
            row = self.db.q1(
                "SELECT * FROM tracks WHERE file_path=?", (str(src),))
            meta = {}
            if row and row["meta"]:
                try:
                    meta = json.loads(row["meta"])
                except Exception:
                    pass
            image = None
            for cname in ("cover.jpg", "cover.png"):
                c = src.parent / cname
                if c.is_file():
                    try:
                        image = c.read_bytes()
                    except OSError:
                        pass
                    break
            try:
                tag_file(dst, meta, image, codec=audio_codec(dst))
            except Exception as e:
                log.warning("retag failed for %s: %s", dst.name, e)
            try:
                src.unlink()
            except OSError:
                pass
            if row:
                self.db.set_track(
                    row["id"], file_path=str(dst), file_ext="ogg",
                    file_size=dst.stat().st_size)
            stats["remuxed"] += 1
            if stats["remuxed"] % 100 == 0:
                log.info("remuxed %d files...", stats["remuxed"])
        log.info("remux done: %s", stats)
        return stats

    def _fail(self, track, err: str):
        attempts = (track["attempts"] or 0) + 1
        max_retries = int(self.cfg.get("download", "max_retries", 3))
        status = "failed" if attempts >= max_retries else "pending"
        self.db.set_track(track["id"], status=status, attempts=attempts,
                          error=err[:500])
        log.warning("track %s failed (%d): %s", track["video_id"], attempts, err[:160])

    def download(self, limit: int | None = None, artist: str | None = None,
                 source: str | None = None, workers: int | None = None) -> dict:
        workers = workers or int(self.cfg.get("download", "workers", 2))
        tracks = self.db.pending_tracks(artist=artist, source=source, limit=limit)
        # discography bulk (album:/songs sources) is opt-in — library
        # tracks download first unless the user enabled it in Options
        if source is None and not self._opt("download", "include_discography",
                                            False):
            kept = [t for t in tracks
                    if (t["source"] or "") != "songs"
                    and not (t["source"] or "").startswith("album:")]
            skipped = len(tracks) - len(kept)
            if skipped:
                log.info("skipping %d discography tracks "
                         "(include_discography off)", skipped)
            tracks = kept
        if not tracks:
            log.info("nothing to download")
            return {"done": 0, "failed": 0}
        # library tracks (liked/history/playlists) before discography bulk —
        # the stuff you actually listen to lands first; stable sort keeps
        # the artist_key clustering so MB catalogs still build once each
        tracks = sorted(
            tracks,
            key=lambda t: 1 if (t["source"] or "").startswith("album:")
            or t["source"] == "songs" else 0)
        # metadata is matched lazily inside each worker — downloads start
        # immediately instead of after a serial MusicBrainz pass over the
        # whole queue (which took hours on large discographies). MB calls
        # stay rate-limited by the shared client's lock; duplicate
        # recordings are still deduped per-track in _process_one.
        log.info("downloading %d tracks (%d workers, match-per-track)",
                 len(tracks), workers)
        grab = self.grab()
        matcher = self.matcher()
        sleep_s = float(self.cfg.get("download", "sleep_interval", 0.2))
        results = {"done": 0, "failed": 0, "skipped": 0}

        if workers <= 1:
            for t in tracks:
                results[self._process_one(t, matcher, grab)] += 1
                if sleep_s:
                    time.sleep(sleep_s)
        else:
            # sqlite conn shared: guard writes
            def worker(t):
                r = self._process_one(t, matcher, grab)
                if sleep_s:
                    time.sleep(sleep_s)
                return r
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(worker, t): t for t in tracks}
                for fut in as_completed(futs):
                    try:
                        results[fut.result()] += 1
                    except Exception as e:
                        results["failed"] += 1
                        log.warning("worker error: %s", e)
        return results

    # ------------------------------------------------------------------ add
    _PLAYLIST_PREFIX = re.compile(r"^(PL|RD|UU|OL|LM|SE|FL|VL)[A-Za-z0-9_-]{2,}$")

    def add(self, target: str) -> dict:
        """Add a youtube/ytm url, video id, playlist url/id, or artist name."""
        target = target.strip()
        ytm = self.ytm(require_auth=False)

        pid = self._extract_playlist_id(target)
        if not pid and ("list=" in target or "playlist" in target.lower()):
            pid = self._extract_playlist_id(target)
        if not pid and self._PLAYLIST_PREFIX.fullmatch(target):
            pid = target
        if pid:
            pl = ytm.playlist(pid)
            self.db.upsert_playlist(pid, title=(pl or {}).get("title"),
                                    track_count=len((pl or {}).get("tracks") or []),
                                    kind="manual")
            n = 0
            for it in (pl or {}).get("tracks") or []:
                norm = YTM.norm_playlist_track(it, f"playlist:{pid}")
                if norm and self._register_track(norm, f"playlist:{pid}"):
                    n += 1
            return {"type": "playlist", "id": pid, "new_tracks": n}

        vid = self._extract_video_id(target)
        if vid:
            try:
                song = ytm.get_song(vid)
            except Exception:
                song = {}
            item = {
                "videoId": vid,
                "title": song.get("title") or target,
                "artists": song.get("artists") or [],
                "album": song.get("album"),
                "duration": song.get("duration"),
                "duration_seconds": song.get("duration_seconds"),
                "thumbnails": song.get("thumbnails"),
                "isAvailable": song.get("isAvailable", True),
            }
            norm = YTM.norm_playlist_track(item, "manual")
            if norm:
                self._register_track(norm, "manual")
            return {"type": "track", "video_id": vid}

        # otherwise treat as artist name -> resolve + discography
        self.db.upsert_artist(target)
        self.resolve_artists()
        row = self.db.q1(
            "SELECT * FROM artists WHERE name_key=?", (norm_text(target),)
        )
        if row and row["status"] == "resolved":
            stats = {"artists": 0, "releases": 0, "tracks_new": 0}
            self._discography_for(ytm, row, stats)
            return {"type": "artist", "name": target, **stats}
        return {"type": "artist", "name": target, "error": "could not resolve artist"}

    # ------------------------------------------------------------- misc
    @staticmethod
    def _extract_video_id(s: str) -> str | None:
        m = re.search(r"(?:v=|youtu\.be/|shorts/|watch\?v=)([A-Za-z0-9_-]{11})", s)
        if m:
            return m.group(1)
        s = s.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
            return s
        return None

    @staticmethod
    def _extract_playlist_id(s: str) -> str | None:
        m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", s)
        return m.group(1) if m else None

    def reset_failed(self):
        self.db.conn.execute(
            "UPDATE tracks SET status='pending', error=NULL WHERE status='failed'")
        self.db.conn.commit()

    def close(self):
        self.db.close()
