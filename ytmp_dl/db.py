"""SQLite state store: artists, playlists, releases, tracks."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .util import log, norm_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT,
    ytm_channel_id TEXT,
    mb_artist_id TEXT,
    mb_artist_name TEXT,
    thumb_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT,
    created REAL,
    updated REAL,
    UNIQUE(name_key)
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    playlist_id TEXT NOT NULL UNIQUE,
    title TEXT,
    track_count INTEGER,
    kind TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created REAL
);
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,
    artist_id INTEGER REFERENCES artists(id),
    browse_id TEXT NOT NULL UNIQUE,
    kind TEXT,
    title TEXT,
    year TEXT,
    track_count INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT,
    created REAL,
    updated REAL
);
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL UNIQUE,
    title TEXT,
    artists TEXT,
    artist_key TEXT,
    album TEXT,
    album_browse_id TEXT,
    ytm_track_number INTEGER,
    ytm_disc_number INTEGER,
    duration_sec INTEGER,
    explicit INTEGER,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    mb_recording_id TEXT,
    mb_release_id TEXT,
    mb_release_group_id TEXT,
    mb_album TEXT,
    mb_album_artist TEXT,
    mb_track_number INTEGER,
    mb_disc_number INTEGER,
    mb_total_tracks INTEGER,
    mb_date TEXT,
    isrc TEXT,
    genre TEXT,
    artwork_url TEXT,
    meta TEXT,
    file_path TEXT,
    file_ext TEXT,
    file_size INTEGER,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created REAL,
    updated REAL
);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist_key);
CREATE INDEX IF NOT EXISTS idx_tracks_source ON tracks(source);
CREATE INDEX IF NOT EXISTS idx_releases_artist ON releases(artist_id);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

TRACK_COLS = [
    "video_id", "title", "artists", "artist_key", "album", "album_browse_id",
    "ytm_track_number", "ytm_disc_number", "duration_sec", "explicit",
    "source", "status", "mb_recording_id", "mb_release_id", "mb_release_group_id",
    "mb_album", "mb_album_artist", "mb_track_number", "mb_disc_number",
    "mb_total_tracks", "mb_date", "isrc", "genre", "artwork_url", "meta",
    "file_path", "file_ext", "file_size", "error", "attempts",
    "created", "updated",
]


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # timeout=30 -> SQLITE_BUSY waits instead of instant 'database is
        # locked' when another ytmp-dl process/AV briefly holds the DB
        self.conn = sqlite3.connect(str(path), check_same_thread=False,
                                    timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        # init writes need locks; if another ytmp-dl.exe instance (or a CLI
        # run) holds the DB this can take a while — retry instead of dying,
        # and skip the journal_mode switch when already WAL (it needs an
        # exclusive lock momentarily)
        last_err = None
        for attempt in range(1, 6):
            try:
                mode = self.conn.execute("PRAGMA journal_mode").fetchone()
                if (mode[0] or "").lower() != "wal":
                    self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA foreign_keys=ON")
                self.conn.executescript(SCHEMA)
                self.conn.commit()
                break
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" not in str(e).lower():
                    raise
                if attempt == 5:
                    raise sqlite3.OperationalError(
                        f"database is locked — is another ytmp-dl.exe or "
                        f"CLI command still running? Close it and retry. "
                        f"({e})")
                log.warning("db locked during init (attempt %d/5), retrying",
                            attempt)
                time.sleep(2)
        self._lock = threading.RLock()

    # ---------- locked helpers (safe for worker threads) ----------
    def q(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def q1(self, sql: str, args: tuple = ()):
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def close(self):
        self.conn.close()

    # ---------- artists ----------
    def upsert_artist(self, name: str, name_key: str | None = None, **fields) -> int:
        name_key = name_key or norm_text(name)
        now = time.time()
        row = self.conn.execute(
            "SELECT id, status FROM artists WHERE name_key=?", (name_key,)
        ).fetchone()
        if row:
            items = [(k, v) for k, v in fields.items() if v is not None]
            sets = ", ".join(f"{k}=?" for k, _ in items)
            vals = [v for _, v in items]
            if items:
                self.conn.execute(
                    f"UPDATE artists SET {sets}, updated=? WHERE id=?",
                    [*vals, now, row["id"]],
                )
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO artists(name, name_key, created, updated) VALUES(?,?,?,?)",
            (name, name_key, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_artist(self, artist_id: int, **fields):
        items = [(k, v) for k, v in fields.items() if k in
                 {"name", "ytm_channel_id", "mb_artist_id", "mb_artist_name",
                  "thumb_url", "status", "detail"} and v is not None]
        if not items:
            return
        sets = ", ".join(f"{k}=?" for k, _ in items)
        self.conn.execute(
            f"UPDATE artists SET {sets}, updated=? WHERE id=?",
            [v for _, v in items] + [time.time(), artist_id],
        )
        self.conn.commit()

    def artists(self, status: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM artists"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY name_key"
        return self.conn.execute(q, args).fetchall()

    def pending_artists(self) -> list[sqlite3.Row]:
        return self.artists("pending")

    # ---------- playlists ----------
    def upsert_playlist(self, playlist_id: str, title: str | None = None,
                        track_count: int | None = None, kind: str | None = None):
        self.conn.execute(
            """INSERT INTO playlists(playlist_id, title, track_count, kind, created)
               VALUES(?,?,?,?,?)
               ON CONFLICT(playlist_id) DO UPDATE SET
                 title=COALESCE(excluded.title, playlists.title),
                 track_count=COALESCE(excluded.track_count, playlists.track_count),
                 kind=COALESCE(excluded.kind, playlists.kind)""",
            (playlist_id, title, track_count, kind, time.time()),
        )
        self.conn.commit()

    def playlists(self, status: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM playlists"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        return self.conn.execute(q + " ORDER BY title", args).fetchall()

    # ---------- releases ----------
    def upsert_release(self, browse_id: str, artist_id: int | None = None,
                       kind: str | None = None, title: str | None = None,
                       year: str | None = None, track_count: int | None = None):
        now = time.time()
        self.conn.execute(
            """INSERT INTO releases(browse_id, artist_id, kind, title, year,
                                    track_count, created, updated)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(browse_id) DO UPDATE SET
                 title=COALESCE(excluded.title, releases.title),
                 year=COALESCE(excluded.year, releases.year),
                 track_count=COALESCE(excluded.track_count, releases.track_count),
                 updated=excluded.updated""",
            (browse_id, artist_id, kind, title, year, track_count, now, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM releases WHERE browse_id=?", (browse_id,)
        ).fetchone()
        return row["id"] if row else None

    def pending_releases(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM releases WHERE status='pending' ORDER BY title"
        ).fetchall()

    def set_release(self, release_id: int, **fields):
        items = [(k, v) for k, v in fields.items()
                 if k in {"status", "detail", "track_count"} and v is not None]
        if not items:
            return
        sets = ", ".join(f"{k}=?" for k, _ in items)
        self.conn.execute(
            f"UPDATE releases SET {sets}, updated=? WHERE id=?",
            [v for _, v in items] + [time.time(), release_id],
        )
        self.conn.commit()

    def releases_for_artist(self, artist_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM releases WHERE artist_id=? ORDER BY year, title",
            (artist_id,),
        ).fetchall()

    # ---------- tracks ----------
    def upsert_track(self, video_id: str, source: str, **fields) -> bool:
        """Insert or enrich a track. Returns True if a new row was created."""
        now = time.time()
        row = self.conn.execute(
            "SELECT * FROM tracks WHERE video_id=?", (video_id,)
        ).fetchone()
        if row is None:
            data = {c: fields[c] for c in TRACK_COLS
                    if c != "video_id" and c in fields and fields[c] is not None}
            data["video_id"] = video_id
            data.setdefault("source", source)
            data["status"] = fields.get("status") or "pending"
            data.setdefault("attempts", 0)
            data["created"] = now
            data["updated"] = now
            cols = ", ".join(data.keys())
            ph = ", ".join("?" for _ in data)
            self.conn.execute(
                f"INSERT INTO tracks({cols}) VALUES({ph})",
                list(data.values()),
            )
            self.conn.commit()
            return True

        # enrich existing: fill empty fields; album-context sources upgrade info
        updates: dict = {}
        for k, v in fields.items():
            if v is None or k in {"status", "error", "attempts", "file_path",
                                  "file_ext", "file_size"}:
                continue
            cur = row[k] if k in row.keys() else None
            if cur in (None, ""):
                updates[k] = v
        # album/songs sources carry better metadata than liked/history
        rank = {"liked": 0, "history": 1, "playlist": 2, "songs": 3,
                "album": 4, "manual": 2}
        def rank_of(src):
            return rank.get((src or "").split(":")[0], 1)
        if rank_of(source) > rank_of(row["source"]) and fields.get("album_browse_id"):
            for k in ("album", "album_browse_id", "ytm_track_number",
                      "ytm_disc_number", "title", "artists", "artist_key",
                      "duration_sec"):
                v = fields.get(k)
                if v is not None:
                    updates[k] = v
            updates["source"] = source
        if updates:
            sets = ", ".join(f"{k}=?" for k in updates)
            self.conn.execute(
                f"UPDATE tracks SET {sets}, updated=? WHERE id=?",
                [*updates.values(), now, row["id"]],
            )
            self.conn.commit()
        return False

    def set_track(self, track_id: int, **fields):
        allowed = set(TRACK_COLS) | {"id"}
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return
        sets = ", ".join(f"{k}=?" for k, _ in items)
        with self._lock:
            self.conn.execute(
                f"UPDATE tracks SET {sets}, updated=? WHERE id=?",
                [v for _, v in items] + [time.time(), track_id],
            )
            self.conn.commit()

    def set_track_by_video(self, video_id: str, **fields):
        row = self.q1("SELECT id FROM tracks WHERE video_id=?", (video_id,))
        if row:
            self.set_track(row["id"], **fields)

    def get_track(self, video_id: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM tracks WHERE video_id=?", (video_id,))

    def tracks(self, status: str | None = None, artist: str | None = None,
               source: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM tracks"
        cond, args = [], []
        if status:
            cond.append("status=?")
            args.append(status)
        if artist:
            cond.append("artist_key LIKE ?")
            args.append(f"%{artist.strip().lower()}%")
        if source:
            cond.append("source LIKE ?")
            args.append(f"{source}%")
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY artist_key, album, ytm_track_number, title"
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.conn.execute(q, args).fetchall()

    def pending_tracks(self, artist: str | None = None,
                       source: str | None = None,
                       limit: int | None = None) -> list[sqlite3.Row]:
        return self.tracks("pending", artist=artist, source=source, limit=limit)

    def counts(self) -> dict:
        out = {}
        for row in self.conn.execute(
            "SELECT status, COUNT(*) c FROM tracks GROUP BY status"
        ):
            out[row["status"]] = row["c"]
        return out

    def artist_track_counts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT artist_key, COUNT(*) total,
                      SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done,
                      SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending
               FROM tracks GROUP BY artist_key ORDER BY total DESC"""
        ).fetchall()

    def stats(self) -> dict:
        c = self.counts()
        artists = self.conn.execute("SELECT COUNT(*) c FROM artists").fetchone()["c"]
        releases = self.conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
        playlists = self.conn.execute("SELECT COUNT(*) c FROM playlists").fetchone()["c"]
        done_size = self.conn.execute(
            "SELECT COALESCE(SUM(file_size),0) s FROM tracks WHERE status='done'"
        ).fetchone()["s"]
        return {
            "artists": artists, "releases": releases, "playlists": playlists,
            "tracks_total": sum(c.values()), "done": c.get("done", 0),
            "pending": c.get("pending", 0), "failed": c.get("failed", 0),
            "skipped": c.get("skipped", 0), "done_gb": round(done_size / 1e9, 2),
        }

    def kv_get(self, key: str):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row["v"]) if row else None

    def kv_set(self, key: str, value):
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, json.dumps(value)),
            )
            self.conn.commit()
