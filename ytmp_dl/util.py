"""Small shared helpers: logging, text normalisation, filename safety."""
from __future__ import annotations

import gettext as _gettext
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path

# ytmusicapi calls gettext.translation() with no fallback; if its .mo
# locale files are missing (incomplete frozen build) every YTMusic()
# call crashes with "No translation file found for domain 'base'".
# Force fallback=True so a missing catalogue degrades to English
# msgids instead of an exception.
_orig_gettext_translation = _gettext.translation


def _gettext_translation_fallback(*args, **kwargs):
    kwargs.setdefault("fallback", True)
    return _orig_gettext_translation(*args, **kwargs)


_gettext.translation = _gettext_translation_fallback

log = logging.getLogger("ytmp_dl")


def setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(level)
    # silence noisy libraries
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_duration(text: str | int | float | None) -> int | None:
    """'4:12' / '1:02:03' / 252 -> seconds."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    text = str(text).strip()
    if not text:
        return None
    if text.replace(":", "").isdigit() is False and ":" not in text:
        try:
            return int(float(text))
        except ValueError:
            return None
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return nums[-1] if nums else None


_WS = re.compile(r"\s+")
_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FEAT = re.compile(
    r"\b(?:feat\.?|ft\.?|featuring)\b.*$", re.IGNORECASE
)
_VERSION_WORDS = re.compile(
    r"\b(?:remaster(?:ed)?|mono|stereo|radio edit|album version|single version|"
    r"deluxe|explicit|clean|edit|version|mix|remix|live|acoustic|demo|"
    r"instrumental|karaoke|bonus|reprise| reprise)\b",
    re.IGNORECASE,
)


def norm_text(s: str | None) -> str:
    """Aggressive lowercase normalisation for fuzzy matching."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _PAREN.sub(" ", s)
    s = _FEAT.sub("", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = _WS.sub(" ", s).strip()
    return s


def norm_title_variants(s: str | None) -> list[str]:
    """Return [strict_norm, core_norm] title variants for matching."""
    if not s:
        return [""]
    strict = norm_text(s)
    core = _VERSION_WORDS.sub(" ", strict)
    core = _WS.sub(" ", core).strip()
    out = [strict]
    if core and core != strict:
        out.append(core)
    return out


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, max_len: int = 200) -> str:
    """Make a string safe for use as a file/folder name."""
    name = _ILLEGAL.sub(" ", name or "")
    name = _WS.sub(" ", name).strip().strip(".")
    # windows reserved names
    if name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "untitled"


def join_artists(artists: list[str], sep: str = "; ") -> str:
    seen: list[str] = []
    for a in artists:
        a = (a or "").strip()
        if a and a not in seen:
            seen.append(a)
    return sep.join(seen)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def now_ts() -> float:
    return time.time()
