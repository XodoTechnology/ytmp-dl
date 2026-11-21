"""Download, remux, tag and place audio files into the Plex-ready tree."""
from __future__ import annotations

import base64
import json
import re
import struct
import subprocess
from pathlib import Path

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, TPOS, TSRC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from .util import ensure_dir, file_size, join_artists, log, sanitize

YTM_WATCH = "https://music.youtube.com/watch?v={vid}"
YT_WATCH = "https://www.youtube.com/watch?v={vid}"


class DownloadError(Exception):
    pass


# ------------------------------------------------------------ image helpers
def image_size(data: bytes) -> tuple[int, int]:
    """Best-effort (w,h) for jpeg/png without PIL."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if data[:2] == b"\xff\xd8":  # jpeg: scan for SOF
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg
    except Exception:
        pass
    return 0, 0


def image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    return "image/jpeg"


def fetch_image(url: str | None, timeout: int = 20) -> bytes | None:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0",
        })
        if r.status_code == 200 and len(r.content) > 500:
            return r.content
    except requests.RequestException:
        pass
    return None


# ------------------------------------------------------------- tagging
def _flac_picture(img: bytes, mime: str) -> Picture:
    w, h = image_size(img)
    pic = Picture()
    pic.type = 3
    pic.mime = mime
    pic.desc = "Cover"
    pic.data = img
    pic.width, pic.height = w, h
    pic.depth = 24
    pic.colors = 0
    return pic


def _vorbis_comment_fields(meta: dict) -> dict[str, str]:
    m = {}
    if meta.get("title"):
        m["title"] = meta["title"]
    if meta.get("artist"):
        m["artist"] = meta["artist"]
    if meta.get("album_artist"):
        m["albumartist"] = meta["album_artist"]
    if meta.get("album"):
        m["album"] = meta["album"]
    if meta.get("track_number"):
        m["tracknumber"] = str(meta["track_number"])
    if meta.get("total_tracks"):
        m["totaltracks"] = str(meta["total_tracks"])
    if meta.get("disc_number"):
        m["discnumber"] = str(meta["disc_number"])
    if meta.get("total_discs"):
        m["totaldiscs"] = str(meta["total_discs"])
    if meta.get("date"):
        m["date"] = str(meta["date"])[:10]
    if meta.get("isrc"):
        m["isrc"] = meta["isrc"]
    if meta.get("genre"):
        m["genre"] = meta["genre"]
    return m


def tag_file(path: Path, meta: dict, image: bytes | None = None,
             codec: str | None = None) -> None:
    """Write tags + artwork into an audio file (mp3/m4a/ogg/opus/webm/flac)."""
    suffix = path.suffix.lower()
    mime = image_mime(image) if image else "image/jpeg"

    if suffix == ".mp3":
        audio = MP3(str(path))
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        def set_text(frame_cls, key, value):
            if value:
                tags.delall(key)
                tags.add(frame_cls(encoding=3, text=str(value)))
        set_text(TIT2, "TIT2", meta.get("title"))
        set_text(TPE1, "TPE1", meta.get("artist"))
        set_text(TPE2, "TPE2", meta.get("album_artist"))
        set_text(TALB, "TALB", meta.get("album"))
        tn, tt = meta.get("track_number"), meta.get("total_tracks")
        if tn:
            tags.delall("TRCK")
            tags.add(TRCK(encoding=3, text=f"{tn}/{tt}" if tt else str(tn)))
        dn, dt = meta.get("disc_number"), meta.get("total_discs")
        if dn:
            tags.delall("TPOS")
            tags.add(TPOS(encoding=3, text=f"{dn}/{dt}" if dt else str(dn)))
        set_text(TDRC, "TDRC", (meta.get("date") or "")[:10] or meta.get("year"))
        set_text(TCON, "TCON", meta.get("genre"))
        set_text(TSRC, "TSRC", meta.get("isrc"))
        if image:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image))
        audio.save()

    elif suffix in (".m4a", ".mp4", ".aac", ".alac", ".m4b"):
        f = MP4(str(path))

        def put(atom, value):
            if value:
                f[atom] = [str(value)]

        put("\xa9nam", meta.get("title"))
        put("\xa9ART", meta.get("artist"))
        put("aART", meta.get("album_artist"))
        put("\xa9alb", meta.get("album"))
        if meta.get("track_number"):
            f["trkn"] = [(int(meta["track_number"]), int(meta.get("total_tracks") or 0))]
        if meta.get("disc_number"):
            f["disk"] = [(int(meta["disc_number"]), int(meta.get("total_discs") or 0))]
        date = (meta.get("date") or "")[:10] or (str(meta.get("year")) if meta.get("year") else None)
        if date:
            f["\xa9day"] = [date]
        put("\xa9gen", meta.get("genre"))
        if meta.get("isrc"):
            f["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(meta["isrc"].encode())]
        if image:
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            f["covr"] = [MP4Cover(image, imageformat=fmt)]
        f.save()

    elif suffix in (".ogg", ".oga", ".opus", ".flac"):
        if suffix == ".flac":
            cls = FLAC
        else:
            codec = codec or audio_codec(path)
            cls = {"opus": OggOpus, "vorbis": OggVorbis}.get(codec, OggOpus)
        audio = cls(str(path))
        for k, v in _vorbis_comment_fields(meta).items():
            audio[k] = v
        if image:
            pic = _flac_picture(image, mime)
            audio["metadata_block_picture"] = [
                base64.b64encode(pic.write()).decode("ascii")
            ]
        audio.save()

    elif suffix == ".webm":
        try:
            from mutagen.webm import WebM
            audio = WebM(str(path))
            tags = audio.tags or {}
            audio.tags = tags
            for k, v in _vorbis_comment_fields(meta).items():
                tags[k.upper()] = v
            if image:
                pic = _flac_picture(image, mime)
                tags["METADATA_BLOCK_PICTURE"] = [
                    base64.b64encode(pic.write()).decode("ascii")
                ]
            audio.save()
        except Exception as e:  # webm tagging is best-effort
            log.debug("webm tagging failed for %s: %s", path, e)
    else:
        log.warning("no tagger for %s", path)


# ------------------------------------------------------------- ffmpeg bits
def _tool(name: str) -> str:
    """Resolve ffmpeg/ffprobe: bundled binary > PATH."""
    import sys
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / name)
        candidates.append(Path(meipass) / "bin" / name)
    candidates.append(Path(sys.executable).parent / name)
    candidates.append(Path(sys.executable).parent / "bin" / name)
    # exe in dist\ + ffmpeg in <root>\bin (install.bat layout)
    candidates.append(Path(sys.executable).parent.parent / "bin" / name)
    # source checkout: <repo>/bin
    candidates.append(Path(__file__).resolve().parent.parent / "bin" / name)
    for c in candidates:
        if c.is_file():
            return str(c)
    return name  # fall back to PATH


def _ffmpeg_ok() -> bool:
    import shutil
    ff, fp = _tool("ffmpeg"), _tool("ffprobe")
    return (bool(shutil.which(ff)) or Path(ff).is_file()) and \
           (bool(shutil.which(fp)) or Path(fp).is_file())


def audio_codec(path: Path) -> str | None:
    try:
        out = subprocess.run(
            [_tool("ffprobe"), "-v", "quiet", "-print_format", "json",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
        for s in data.get("streams", []):
            if s.get("codec_type") == "audio":
                return s.get("codec_name")
    except Exception:
        pass
    return None


def remux_to_ogg(src: Path, dst: Path) -> bool:
    try:
        subprocess.run(
            [_tool("ffmpeg"), "-y", "-v", "error", "-i", str(src),
             "-vn", "-c:a", "copy", str(dst)],
            check=True, timeout=120,
        )
        return dst.is_file() and dst.stat().st_size > 0
    except Exception as e:
        log.debug("remux failed %s -> %s: %s", src, dst, e)
        return False


# ------------------------------------------------------------- download
def _ydl_opts(cfg, staging_dir: Path, cookies: Path | None) -> dict:
    d = cfg.section("download")
    opts = {
        "format": d.get("format", "bestaudio/best"),
        "outtmpl": str(staging_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": int(d.get("max_retries", 3)),
        "fragment_retries": int(d.get("max_retries", 3)),
        "concurrent_fragment_downloads": int(d.get("concurrent_fragments", 4)),
        "sleep_interval_requests": float(d.get("sleep_requests", 0.5)),
        "max_sleep_interval": 5,
        "writethumbnail": False,
        "writesubtitles": False,
        "writeinfojson": False,
        "postprocessors": [],
        "ignoreerrors": False,
        "nocheckcertificate": False,
    }
    if cookies and cookies.is_file():
        opts["cookiefile"] = str(cookies)
    return opts


class Grabber:
    def __init__(self, cfg, db):
        import yt_dlp  # noqa: F401 - ensure present
        self.cfg = cfg
        self.db = db
        self.output_dir = cfg.path("output_dir")
        self.staging = cfg.path("cache_dir") / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.cookies = cfg.path("cookies_file")
        self._ffmpeg_ok = _ffmpeg_ok()
        if not self._ffmpeg_ok:
            log.warning("ffmpeg/ffprobe not found — .webm downloads won't "
                        "be remuxed to .ogg (put ffmpeg.exe + ffprobe.exe "
                        "next to the exe or in bin/)")
        self.ensure_output()

    def ensure_output(self):
        ensure_dir(self.output_dir)

    # ----- path building -----
    def build_path(self, meta: dict, video_id: str, ext: str = "") -> Path:
        artist = meta.get("album_artist") or meta.get("artist") or "Unknown Artist"
        album = meta.get("album") or "Unknown Album"
        year = meta.get("year") or (str(meta.get("date") or "")[:4] or "")
        album_name = sanitize(album + (f" ({year})" if year else ""))
        artist_dir = sanitize(artist)

        tn, tt = meta.get("track_number"), meta.get("total_tracks")
        dn, dt = meta.get("disc_number"), meta.get("total_discs")
        multi_disc = bool(dn and (dn > 1 or (dt and dt > 1)))
        title = sanitize(meta.get("title") or video_id)
        if multi_disc:
            num = f"{int(dn):02d}-{int(tn or 0):02d}"
            fname = f"{num} - {title}"
        elif tn:
            fname = f"{int(tn):02d} - {title}"
        else:
            fname = title
        if ext and not fname.lower().endswith(ext.lower()):
            fname += ext if ext.startswith(".") else f".{ext}"
        return self.output_dir / artist_dir / album_name / fname

    def _resolve_collision(self, base: Path, video_id: str) -> Path:
        """If path exists for a different video, suffix with short id."""
        if not base.exists():
            return base
        row = self.db.q1(
            "SELECT video_id FROM tracks WHERE file_path=?", (str(base),)
        )
        if row and row["video_id"] == video_id:
            return base
        return base.with_name(f"{base.stem} [{video_id[:8]}]{base.suffix}")

    # ----- main -----
    def download_track(self, track_row, meta: dict) -> Path:
        """Download + tag + move. Returns final path. Raises DownloadError."""
        import yt_dlp

        video_id = track_row["video_id"]
        staging_dir = self.staging / video_id
        ensure_dir(staging_dir)
        urls = [YTM_WATCH.format(vid=video_id), YT_WATCH.format(vid=video_id)]

        # public videos need no auth; cookies are a last resort so expired
        # cookies.txt can't break downloads days after discovery
        attempts = [(u, False) for u in urls]
        if self.cookies.is_file():
            attempts += [(u, True) for u in urls]

        info = None
        last_err = None
        for url, use_cookies in attempts:
            try:
                opts = _ydl_opts(self.cfg, staging_dir,
                                 self.cookies if use_cookies else None)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.debug("yt-dlp attempt failed (%s cookies=%s): %s",
                          url, use_cookies, str(e)[:120])
        if info is None:
            raise DownloadError(f"yt-dlp failed: {last_err}")

        # locate produced file
        ext = (info or {}).get("ext") or ""
        candidates = list(staging_dir.glob(f"{video_id}.*"))
        candidates = [c for c in candidates if c.suffix.lower() not in
                      (".json", ".txt", ".jpg", ".webp", ".part", ".ytdl")]
        if not candidates:
            raise DownloadError("no output file produced")
        src = max(candidates, key=file_size)
        if ext and src.suffix.lower() != f".{ext}":
            # trust actual file
            pass

        # normalise container: webm/mkv opus/vorbis -> ogg. Also try when
        # codec detection failed (ffprobe missing) — opus/vorbis are the
        # only audio codecs YouTube puts in webm anyway, and -c:a copy is
        # a no-op remux either way.
        codec = audio_codec(src)
        work = src
        if self._ffmpeg_ok and src.suffix.lower() in (".webm", ".mkv") and (
                codec in ("opus", "vorbis") or codec is None):
            ogg = src.with_suffix(".ogg")
            if remux_to_ogg(src, ogg):
                work = ogg

        # final path (extension known after remux decision)
        final = self.build_path(meta, video_id, ext=work.suffix or ".bin")
        final = self._resolve_collision(final, video_id)
        ensure_dir(final.parent)
        if work.resolve() != final.resolve():
            work.replace(final)

        # artwork
        image = meta.get("_image_bytes")
        try:
            tag_file(final, meta, image, codec=codec)
        except Exception as e:
            log.warning("tagging failed for %s: %s", final.name, e)

        # album folder cover.jpg (only write once)
        cover = final.parent / "cover.jpg"
        if image and not cover.exists():
            try:
                cover.write_bytes(image)
            except OSError:
                pass

        # cleanup staging leftovers
        for leftover in staging_dir.iterdir():
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            staging_dir.rmdir()
        except OSError:
            pass

        return final
