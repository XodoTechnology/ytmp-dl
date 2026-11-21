"""ytmp-dl: archive your YouTube Music library into a Plex-ready folder tree."""

try:
    from ._buildinfo import BUILD_STAMP
except Exception:  # pragma: no cover - file generated at build time
    BUILD_STAMP = "source"

__version__ = "0.1.0"
