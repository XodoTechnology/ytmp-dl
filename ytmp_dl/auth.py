"""Turn a browser cookies.txt export into credentials for ytmusicapi + yt-dlp."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from http.cookies import SimpleCookie
from pathlib import Path

import requests

from .util import log

YTM_ORIGIN = "https://music.youtube.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# cookie domains worth sending to YouTube/Google innertube endpoints
_COOKIE_DOMAIN_SUFFIXES = (".youtube.com", "youtube.com", ".google.com", "google.com")


class AuthError(Exception):
    pass


def parse_cookies_text(text: str) -> dict[str, str]:
    """Parse Netscape-format cookies text into {name: value} for YT/Google."""
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, _expires, name, value = parts[:7]
        if not any(domain.endswith(s) or domain == s.lstrip(".")
                   for s in _COOKIE_DOMAIN_SUFFIXES):
            continue
        cookies.setdefault(name, value)
    return cookies


def parse_cookies_txt(path: Path) -> dict[str, str]:
    """Parse a Netscape-format cookies.txt into {name: value} for YT/Google."""
    if not path.is_file():
        raise AuthError(f"cookies file not found: {path}")
    return parse_cookies_text(path.read_text(errors="replace"))


def save_json_cookies(text: str, out_path: Path) -> int:
    """Accept a JSON cookie export (EditThisCookie/Cookie-Editor style),
    keep YouTube/Google entries, write Netscape cookies.txt. Returns count."""
    import json as _json
    data = _json.loads(text)
    if isinstance(data, dict):
        data = data.get("cookies") or []
    if not isinstance(data, list):
        raise AuthError("unrecognised cookie JSON format")

    yt_domains = ("youtube.com", "youtu.be", "google.com",
                  "googlevideo.com", "ggpht.com")
    lines: list[str] = []
    for c in data:
        if not isinstance(c, dict):
            continue
        name, value = c.get("name"), c.get("value")
        domain = (c.get("domain") or "").lstrip(".")
        if not name or value is None:
            continue
        if not any(domain == d or domain.endswith("." + d) for d in yt_domains):
            continue
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        host_only = "FALSE" if str(c.get("domain", "")).startswith(".") else "TRUE"
        expires = int(c.get("expirationDate") or c.get("expires") or 0)
        lines.append("\t".join([domain, host_only, path, secure,
                                str(expires), str(name), str(value)]))
    if not lines:
        raise AuthError("no YouTube/Google cookies found in that JSON export")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n")
    return len(lines)


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def sapisid_hash(sapisid: str, origin: str, timestamp: int | None = None) -> str:
    ts = timestamp or int(time.time())
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def build_auth_dict(cookies: dict[str, str], user_agent: str | None = None) -> dict:
    """Build the headers dict ytmusicapi expects for browser-style auth."""
    header = cookie_header(cookies)
    if "__Secure-3PAPISID" not in cookies:
        raise AuthError(
            "cookies.txt is missing __Secure-3PAPISID — export cookies while "
            "logged into YouTube in a desktop browser (all youtube.com cookies)."
        )
    auth = {
        "cookie": header,
        "user-agent": user_agent or DEFAULT_UA,
        "origin": YTM_ORIGIN,
        "x-origin": YTM_ORIGIN,
        "x-goog-authuser": "0",
        "accept": "*/*",
        "content-type": "application/json",
    }
    # ytmusicapi >=1.12 needs an authorization header containing SAPISIDHASH
    # to classify browser auth; it recomputes the real value per request.
    auth["authorization"] = sapisid_hash(cookies["__Secure-3PAPISID"], YTM_ORIGIN)
    return auth


def ensure_auth(cfg) -> Path:
    """Write auth.json next to the db; returns its path."""
    cookies_path = cfg.path("cookies_file")
    auth_path = cfg.path("state_db").with_suffix(".auth.json")
    cookies = parse_cookies_txt(cookies_path)
    auth = build_auth_dict(cookies)
    auth_path.write_text(json.dumps(auth, indent=2))
    try:
        auth_path.chmod(0o600)
    except OSError:
        pass
    log.info("wrote %s (%d cookies)", auth_path, len(cookies))
    return auth_path


# ------------------------------------------------------- browser extraction
def detect_browsers() -> list[dict]:
    """Find browsers with a cookie store, with their profile dirs.

    Returns [{browser, profile}] — chromium 'profile' is the dir name under
    'User Data'; firefox 'profile' is the profile dir name.
    """
    import os
    import sys
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))

    chromium_bases = {
        "chrome": local / "Google/Chrome/User Data",
        "edge": local / "Microsoft/Edge/User Data",
        "brave": local / "BraveSoftware/Brave-Browser/User Data",
        "chromium": local / "Chromium/User Data",
        "vivaldi": local / "Vivaldi/User Data",
        # yt-dlp only knows 'opera' — Opera GX shares its extractor, and we
        # pass the profile as an absolute path so the right dir is read
        "opera": roaming / "Opera Software/Opera Stable",
        "opera_gx": roaming / "Opera Software/Opera GX Stable",
    }
    out = []
    for browser, base in chromium_bases.items():
        if not base.is_dir():
            continue
        ytdlp_name = "opera" if browser.startswith("opera") else browser
        if ytdlp_name == "opera":
            # yt-dlp treats opera as profile-less: the 'profile' value is
            # used as browser_dir itself, so it must be the user-data root
            # (holds Local State + Default/Network/Cookies). Passing a
            # profile subdir breaks key lookup -> decryption fails.
            out.append({"browser": ytdlp_name, "profile": str(base),
                        "label": f"{browser} — {base.name}"})
            continue
        profiles = []
        for child in sorted(base.iterdir()):
            if child.is_dir() and ((child / "Cookies").is_file()
                                   or (child / "Network/Cookies").is_file()):
                profiles.append(child)
        if not profiles and ((base / "Cookies").is_file()
                             or (base / "Network/Cookies").is_file()):
            profiles = [base]
        for p in profiles or [base / "Default"]:
            out.append({"browser": ytdlp_name,
                        "profile": str(p),
                        "label": f"{browser} — {p.name}"})

    # firefox: %APPDATA%/Mozilla/Firefox/Profiles/<name>.default-release
    ff = roaming / "Mozilla/Firefox/Profiles"
    if ff.is_dir():
        for prof in sorted(ff.iterdir()):
            if prof.is_dir() and (prof / "cookies.sqlite").is_file():
                out.append({"browser": "firefox", "profile": prof.name,
                            "label": f"firefox — {prof.name}"})
    return out


class _YDLLogAdapter:
    """Route yt-dlp's cookie-extraction log calls into our logger so
    failures are visible in the GUI log instead of a black box."""

    def __init__(self, logger):
        self._l = logger

    def debug(self, msg, *a, **k):
        self._l.debug(str(msg))

    def info(self, msg, *a, **k):
        self._l.info(str(msg))

    def warning(self, msg, *a, **k):
        self._l.warning(str(msg))

    def error(self, msg, *a, **k):
        self._l.error(str(msg))


def _copy_locked_db(database_path: str, dest: str) -> bool:
    """Copy a Chromium Cookies DB that the running browser has locked
    (yt-dlp issue #7271). Tries sqlite's backup API, then esentutl's raw
    copy. Returns True if dest was written."""
    import sqlite3
    import subprocess

    # 1) sqlite backup API — reads through sqlite's own locking, which
    #    tolerates the browser's WAL/journal locks in most cases
    try:
        from pathlib import Path as _P
        from urllib.parse import quote
        uri = "file:" + quote(_P(database_path).as_posix(), safe="/:") \
            + "?mode=ro"
        src = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            dst = sqlite3.connect(dest)
            src.backup(dst)
            dst.close()
        finally:
            src.close()
        if Path(dest).stat().st_size > 0:
            return True
    except Exception as e:
        log.debug("sqlite backup copy failed: %s", e)

    # 2) esentutl /y — Windows built-in raw copy that can read files
    #    other processes hold open
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["esentutl", "/y", database_path, "/d", dest, "/o"],
                capture_output=True, timeout=60)
            if r.returncode == 0 and Path(dest).exists() \
                    and Path(dest).stat().st_size > 0:
                return True
            log.debug("esentutl copy failed rc=%s", r.returncode)
        except Exception as e:
            log.debug("esentutl copy failed: %s", e)
    return False


def _open_database_copy_robust(database_path, tmpdir):
    """Drop-in for yt_dlp.cookies._open_database_copy that survives the
    browser holding the Cookies DB open."""
    import os
    import shutil
    import sqlite3

    copy_path = os.path.join(tmpdir, "temporary.sqlite")
    try:
        shutil.copy(database_path, copy_path)
    except PermissionError:
        if not _copy_locked_db(database_path, copy_path):
            raise
    conn = sqlite3.connect(copy_path)
    return conn.cursor()


_BROWSER_EXES = {
    "chrome": ["chrome.exe"], "chromium": ["chrome.exe", "chromium.exe"],
    "edge": ["msedge.exe"], "brave": ["brave.exe"],
    "opera": ["opera.exe", "opera_gx.exe"],
    "vivaldi": ["vivaldi.exe"], "whale": ["whale.exe"],
    "firefox": ["firefox.exe"], "safari": [],
}


def _browser_running_hint(browser: str) -> str:
    """If the browser process is still alive, say exactly what to kill."""
    if sys.platform != "win32":
        return ""
    import subprocess
    for exe in _BROWSER_EXES.get(browser, []):
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                capture_output=True, text=True, timeout=15)
            if exe.lower() in r.stdout.lower():
                return (f" — {exe} is still running; fully quit it "
                        f"(check the system tray) and retry")
        except Exception:
            pass
    return ""


def cookies_from_browser(browser: str, profile: str | None,
                         out_path: Path) -> int:
    """Pull cookies straight from an installed browser via yt-dlp's
    decryptor, keep only YouTube/Google ones, write Netscape cookies.txt.
    Returns number of cookies written."""
    from http.cookiejar import MozillaCookieJar
    import yt_dlp.cookies as ydc
    from yt_dlp.cookies import extract_cookies_from_browser

    log.info("extracting cookies from %s (profile=%s)", browser, profile)
    orig_copy = ydc._open_database_copy
    ydc._open_database_copy = _open_database_copy_robust
    try:
        jar = extract_cookies_from_browser(
            browser, profile=profile or None, logger=_YDLLogAdapter(log))
    except Exception as e:
        hint = _browser_running_hint(browser)
        if hint:
            log.warning("browser still running%s", hint)
        raise
    finally:
        ydc._open_database_copy = orig_copy
    # drop non-YT/Google cookies to keep the file small
    for c in list(jar):
        d = (c.domain or "").lstrip(".")
        if not any(d == s or d.endswith("." + s) for s in
                   ("youtube.com", "google.com", "youtu.be", "googlevideo.com")):
            try:
                jar.clear(c.domain, c.path, c.name)
            except Exception:
                pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(str(out_path), ignore_discard=True, ignore_expires=True)
    return len(list(jar))


def validate_auth(auth_path: Path) -> dict:
    """Check the auth actually works; returns account info.

    YouTube periodically reshapes the account-menu response, which breaks
    get_account_info()'s parser even for valid sessions — so on parse
    failure we fall back to an authenticated-only library call to tell
    'bad cookies' apart from 'YT changed their layout'."""
    from ytmusicapi import YTMusic

    yt = YTMusic(auth=str(auth_path))
    try:
        return yt.get_account_info()
    except Exception:
        pass
    try:
        liked = yt.get_liked_songs(limit=1)
    except Exception as e:
        raise AuthError(
            "YouTube rejected these cookies — they may be expired, "
            "revoked, or the session was signed out. Export fresh cookies "
            f"(or re-run browser sign-in): {type(e).__name__}: {e}"
        ) from e
    return {"accountName": "signed in (account info unavailable)",
            "channelId": None,
            "_liked_sample": liked[0].get("title") if liked else None}


def cookie_smoke_test(cookies_path: Path) -> bool:
    """Cheap check that the cookies still authenticate to YouTube."""
    cookies = parse_cookies_txt(cookies_path)
    header = cookie_header(cookies)
    try:
        r = requests.get(
            "https://www.youtube.com/account",
            headers={"Cookie": header, "User-Agent": DEFAULT_UA},
            timeout=15,
            allow_redirects=True,
        )
        return "Sign in" not in r.text[:200000] or "signed in" in r.text.lower()
    except requests.RequestException:
        return False


def sapisid_from_header(cookie_hdr: str) -> str | None:
    jar = SimpleCookie()
    try:
        jar.load(cookie_hdr)
    except Exception:
        return None
    m = jar.get("__Secure-3PAPISID")
    return m.value if m else None
