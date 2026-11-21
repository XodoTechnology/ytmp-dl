"""Command line interface for ytmp-dl."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, BUILD_STAMP
from .config import Config, find_root
from .util import log, setup_logging


def _cfg(args) -> Config:
    root = find_root(Path(args.root).resolve() if args.root else None)
    return Config.load(root=root, config_file=args.config)


def cmd_init(args):
    cfg = _cfg(args)
    from .db import DB
    from .auth import ensure_auth, AuthError
    db = DB(cfg.path("state_db"))
    cfg.path("output_dir").mkdir(parents=True, exist_ok=True)
    cfg.path("cache_dir").mkdir(parents=True, exist_ok=True)
    print(f"project root : {cfg.root}")
    print(f"output dir   : {cfg.path('output_dir')}")
    print(f"state db     : {cfg.path('state_db')}")
    print(f"cookies file : {cfg.path('cookies_file')}")
    if cfg.path("cookies_file").is_file():
        try:
            p = ensure_auth(cfg)
            print(f"auth json    : {p}")
        except AuthError as e:
            print(f"auth         : FAILED - {e}")
    else:
        print("auth         : no cookies.txt yet (needed for library discovery)")
    db.close()


def cmd_auth(args):
    cfg = _cfg(args)
    from .auth import ensure_auth, validate_auth, AuthError
    try:
        p = ensure_auth(cfg)
    except AuthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        info = validate_auth(p)
        name = info.get("accountName") or info.get("username") or "?"
        print(f"authenticated as: {name}")
        return 0
    except Exception as e:
        print(f"auth validation failed: {e}", file=sys.stderr)
        return 1


def cmd_discover(args):
    from .pipeline import Pipeline
    p = Pipeline(_cfg(args))
    try:
        stats = p.discover()
        print(json.dumps(stats, indent=2))
    finally:
        p.close()


def cmd_artists(args):
    from .pipeline import Pipeline
    p = Pipeline(_cfg(args))
    try:
        stats = p.resolve_artists()
        print(json.dumps(stats, indent=2))
    finally:
        p.close()


def cmd_discography(args):
    from .pipeline import Pipeline
    p = Pipeline(_cfg(args))
    try:
        stats = p.discography()
        print(json.dumps(stats, indent=2))
    finally:
        p.close()


def cmd_download(args):
    from .pipeline import Pipeline
    p = Pipeline(_cfg(args))
    try:
        stats = p.download(limit=args.limit, artist=args.artist,
                           source=args.source, workers=args.workers)
        print(json.dumps(stats, indent=2))
    finally:
        p.close()


def cmd_remux(args):
    from .pipeline import Pipeline
    p = Pipeline(_cfg(args))
    try:
        print(json.dumps(p.remux_containers(), indent=2))
    finally:
        p.close()


def cmd_retry(args):
    from .pipeline import Pipeline
    p = Pipeline(_cfg(args))
    try:
        p.reset_failed()
        print("failed tracks reset to pending")
    finally:
        p.close()


def cmd_add(args):
    from .pipeline import Pipeline
    p = Pipeline(_cfg(args))
    try:
        for t in args.targets:
            print(json.dumps({"target": t, **p.add(t)}, indent=2))
    finally:
        p.close()


def cmd_status(args):
    from .pipeline import Pipeline
    p = Pipeline(_cfg(args))
    try:
        print(json.dumps(p.db.stats(), indent=2))
        print("\nper-artist progress:")
        for r in p.db.artist_track_counts()[:40]:
            key = r["artist_key"] or "?"
            print(f"  {key:40s} {r['done']:>5}/{r['total']:<5} pending={r['pending']}")
        if args.verbose:
            print("\nartists:")
            for a in p.db.artists():
                print(f"  [{a['status']:8s}] {a['name']}  ({a['ytm_channel_id'] or '-'})")
    finally:
        p.close()


def cmd_all(args):
    from .pipeline import Pipeline
    cfg = _cfg(args)
    p = Pipeline(cfg)
    try:
        print("== discover ==")
        print(json.dumps(p.discover(), indent=2))
        print("== resolve artists ==")
        print(json.dumps(p.resolve_artists(), indent=2))
        print("== discography ==")
        print(json.dumps(p.discography(), indent=2))
        print("== download ==")
        print(json.dumps(p.download(workers=args.workers), indent=2))
    finally:
        p.close()


def cmd_gui(args):
    from .gui import main as gui_main
    return gui_main(["--root", str(_cfg(args).root),
                     "--port", str(args.port),
                     "--host", args.host]
                    + (["--no-browser"] if args.no_browser else []))


def cmd_update(args):
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "--quiet", "--upgrade",
                           "yt-dlp", "ytmusicapi", "mutagen", "requests"])
    print("dependencies updated")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ytmp-dl",
        description="Archive your YouTube Music library into a Plex-ready folder tree.")
    ap.add_argument("--version", action="version",
                    version=f"ytmp-dl {__version__} (build {BUILD_STAMP})")
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("--root", help="project root (auto-detected)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="initialise dirs/db/auth").set_defaults(f=cmd_init)
    sub.add_parser("auth", help="build+validate auth from cookies.txt").set_defaults(f=cmd_auth)
    sub.add_parser("discover", help="pull liked songs/history/playlists").set_defaults(f=cmd_discover)
    sub.add_parser("artists", help="resolve artist channel ids").set_defaults(f=cmd_artists)
    sub.add_parser("discography", help="expand artists into releases+tracks").set_defaults(f=cmd_discography)

    d = sub.add_parser("download", help="download pending tracks")
    d.add_argument("--limit", type=int)
    d.add_argument("--artist")
    d.add_argument("--source", help="e.g. liked, playlist, album, songs, manual")
    d.add_argument("--workers", type=int)
    d.set_defaults(f=cmd_download)

    sub.add_parser("retry", help="reset failed tracks to pending").set_defaults(f=cmd_retry)
    sub.add_parser("remux", help="re-box .webm/.mkv downloads to .ogg in place").set_defaults(f=cmd_remux)

    a = sub.add_parser("add", help="add url / video id / playlist / artist name")
    a.add_argument("targets", nargs="+")
    a.set_defaults(f=cmd_add)

    s = sub.add_parser("status", help="show progress")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(f=cmd_status)

    w = sub.add_parser("all", help="discover+artists+discography+download")
    w.add_argument("--workers", type=int)
    w.set_defaults(f=cmd_all)

    sub.add_parser("update", help="upgrade python deps").set_defaults(f=cmd_update)

    g = sub.add_parser("gui", help="launch the local web UI")
    g.add_argument("--port", type=int, default=8377)
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--no-browser", action="store_true")
    g.set_defaults(f=cmd_gui)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=getattr(args, "verbose", False),
                  quiet=getattr(args, "quiet", False))
    try:
        return args.f(args) or 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as e:
        log.exception("fatal: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
