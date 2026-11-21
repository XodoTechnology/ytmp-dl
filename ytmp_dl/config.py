"""Configuration loading: config.toml with sane defaults."""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "general": {
        "output_dir": "./music",
        "state_db": "./ytmp_dl.db",
        "cookies_file": "./cookies.txt",
        "cache_dir": "./cache",
    },
    "discover": {
        "include_liked": True,
        "include_history": True,
        "include_library_playlists": True,
        "include_library_songs": False,
        "include_library_albums": False,
        "skip_playlist_patterns": [
            "mix", "discover", "release radar", "repeat rewind",
            "your mix", "on repeat", "new releases", "daily drive",
            "deeper dive", "repeatable", "revisit",
        ],
        "manual_playlists": [],
        "manual_artists": [],
    },
    "download": {
        "format": "bestaudio/best",
        "workers": 2,
        "sleep_requests": 0.5,
        "sleep_interval": 0.2,
        "max_retries": 3,
        "concurrent_fragments": 4,
        "include_discography": False,  # download album:/songs bulk tracks
    },
    "discography": {
        "mode": "selected",  # selected | library | all | off
    },
    "metadata": {
        "musicbrainz": True,
        "spotify": True,
        "match_threshold": 0.72,
        "mb_rate_interval": 1.1,
    },
    "spotify": {
        "client_id": "",
        "client_secret": "",
    },
    "artwork": {
        "size": "500",
    },
}


class Config:
    def __init__(self, data: dict, root: Path):
        self._data = data
        self.root = root

    def get(self, section: str, key: str, default=None):
        return self._data.get(section, {}).get(key, default)

    def section(self, name: str) -> dict:
        return self._data.get(name, {})

    def path(self, key: str) -> Path:
        """Resolve a path-valued general.* setting relative to project root."""
        raw = self.get("general", key)
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p)

    @classmethod
    def load(cls, root: Path | None = None, config_file: str | Path | None = None) -> "Config":
        root = root or Path.cwd()
        cfg_path = Path(config_file) if config_file else root / "config.toml"
        data = copy.deepcopy(DEFAULTS)
        if cfg_path.is_file():
            with open(cfg_path, "rb") as f:
                user = tomllib.load(f)
            for section, values in user.items():
                if isinstance(values, dict) and section in data:
                    data[section].update(values)
                else:
                    data[section] = values
        return cls(data, root)


def find_root(start: Path | None = None) -> Path:
    """Walk up from cwd looking for config.toml or the package marker.
    Frozen (PyInstaller) builds anchor to the exe's directory instead."""
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    p = (start or Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / "config.toml").is_file() or (cand / "ytmp_dl").is_dir():
            return cand
    return p
