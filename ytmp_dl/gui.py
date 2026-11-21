"""Local web GUI: job runner, status/log APIs, single-page UI.

Binds to 127.0.0.1 only — cookies and library data never leave the machine.
"""
from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .config import Config, find_root
from .util import log

STATIC = Path(__file__).parent / "static"
MAX_LOG_LINES = 4000

DISCOVER_OPTS = ("include_liked", "include_history",
                 "include_library_playlists", "include_library_songs",
                 "include_library_albums")
DISCOVERY_MODES = ("selected", "library", "all", "off")


# ------------------------------------------------------------ log capture
class _ListHandler(logging.Handler):
    """Logging handler storing formatted lines for the UI."""

    def __init__(self, buf: deque, counter: list):
        super().__init__()
        self.buf = buf
        self.counter = counter

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self.counter[0] += 1
        self.buf.append((self.counter[0], record.levelname, msg))


class LogCapture:
    def __init__(self):
        self.buf: deque = deque(maxlen=MAX_LOG_LINES)
        self.counter = [0]
        self.handler = _ListHandler(self.buf, self.counter)
        self.handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        self.attached = False

    def attach(self):
        if not self.attached:
            log.addHandler(self.handler)
            self.attached = True


# ------------------------------------------------------------ job runner
class Runner:
    STEPS = ("auth", "discover", "artists", "discography", "download",
             "retry", "remux")

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.q: queue.Queue = queue.Queue()
        self.state = {
            "running": False, "step": None, "queued": [],
            "error": None, "results": [],
        }
        self._lock = threading.Lock()
        self._pipeline = None
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ytmp-dl-jobs")
        self._thread.start()

    # ----- pipeline access -----
    def pipeline(self):
        if self._pipeline is None:
            from .pipeline import Pipeline
            self._pipeline = Pipeline(self.cfg)
        return self._pipeline

    def reset_clients(self):
        """Force YTM/MB/matcher re-creation (e.g. after new cookies.txt)."""
        p = self._pipeline
        if p is not None:
            p._ytm = None
            p._matcher = None
            p._mb = None

    # ----- queueing -----
    def enqueue(self, label: str, fn) -> bool:
        with self._lock:
            if self.state["running"] or not self.q.empty():
                self.state["queued"].append(label)
            self.q.put((label, fn))
            return True

    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.state, default=str))

    # ----- worker -----
    def _loop(self):
        while True:
            label, fn = self.q.get()
            with self._lock:
                if label in self.state["queued"]:
                    self.state["queued"].remove(label)
            self._set(running=True, step=label, error=None)
            try:
                res = fn()
                self._record(label, "ok", res)
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", label)
                self._set(error=f"{label}: {type(e).__name__}: {e}")
                self._record(label, "error", str(e))
            finally:
                self._set(running=False, step=None)

    def _record(self, label, status, res):
        entry = {"t": time.time(), "step": label, "status": status}
        if isinstance(res, dict):
            entry["result"] = {k: v for k, v in res.items()
                               if isinstance(v, (int, float, str, bool))}
        with self._lock:
            self.state["results"].append(entry)
            self.state["results"] = self.state["results"][-20:]

    # ----- step registry -----
    def run_steps(self, steps: list[str]) -> list[str]:
        p = self.pipeline()
        fns = {
            "auth": lambda: (p.ytm(require_auth=True), {"ok": True})[1],
            "discover": p.discover,
            "artists": p.resolve_artists,
            "discography": p.discography,
            "download": p.download,
            "retry": lambda: (p.reset_failed(), p.download())[1],
            "remux": p.remux_containers,
        }
        queued = []
        for s in steps:
            s = (s or "").strip().lower()
            if s == "all":
                queued += [x for x in ("discover", "artists", "discography",
                                       "download")]
            elif s in fns:
                queued.append(s)
        for s in queued:
            self.enqueue(s, fns[s])
        return queued


# ------------------------------------------------------------ flask app
def create_app(root: Path | None = None) -> Flask:
    root = root or find_root()
    cfg = Config.load(root)
    capture = LogCapture()
    capture.attach()
    runner = Runner(cfg)
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    # shared-secret gate: env override wins, else [gui] token in config.toml
    import os
    token = (os.environ.get("YTMP_GUI_TOKEN")
             or str(cfg.get("gui", "token", "") or ""))
    app.config["EFFECTIVE_GUI_TOKEN"] = token or None

    @app.before_request
    def _auth():
        if not token:
            return None
        from flask import g, redirect
        arg_key = request.args.get("key") or ""
        other_key = (request.cookies.get("gui_key")
                     or request.headers.get("X-Gui-Key") or "")
        if arg_key == token:
            g._set_gui_cookie = True
            return None
        if other_key == token:
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect("/?key=" + token)

    @app.after_request
    def _cookie(resp):
        from flask import g
        if getattr(g, "_set_gui_cookie", False):
            resp.set_cookie("gui_key", token, httponly=True, samesite="Lax")
        return resp

    def db():
        return runner.pipeline().db

    # ---------------- pages ----------------
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/logo.svg")
    def logo():
        return send_from_directory(STATIC, "logo.svg")

    # ---------------- state / status ----------------
    @app.get("/api/state")
    def api_state():
        return jsonify(runner.snapshot())

    @app.get("/api/status")
    def api_status():
        try:
            stats = db().stats()
        except Exception as e:
            stats = {"error": str(e)}
        try:
            per_artist = [
                {"artist": r["artist_key"], "total": r["total"],
                 "done": r["done"], "pending": r["pending"]}
                for r in db().artist_track_counts()[:60]
            ]
        except Exception:
            per_artist = []
        from . import BUILD_STAMP
        return jsonify({"stats": stats, "per_artist": per_artist,
                        "job": runner.snapshot(), "build": BUILD_STAMP})

    @app.get("/api/artists")
    def api_artists():
        try:
            p = runner.pipeline()
            counts = {r["artist_key"]: dict(r)
                      for r in p.db.artist_track_counts()}
            artist_rows = p.db.artists()
            sel = set(p._selected_artist_ids())
            lib = p._library_artist_ids()
        except Exception as e:
            return jsonify({"error": str(e)}), 503
        out = []
        for a in artist_rows:
            c = counts.get(a["name_key"]) or {}
            out.append({
                "id": a["id"], "name": a["name"], "key": a["name_key"],
                "status": a["status"],
                "channel": a["ytm_channel_id"],
                "mb_id": a["mb_artist_id"],
                "total": c.get("total", 0), "done": c.get("done", 0),
                "pending": c.get("pending", 0),
                "selected": a["id"] in sel,
                "library": a["id"] in lib,
            })
        return jsonify(out)

    @app.post("/api/artists/select")
    def api_artists_select():
        body = request.get_json(force=True, silent=True) or {}
        ids = body.get("ids")
        if ids is None and body.get("id") is not None:
            ids = [body["id"]]
        try:
            ids = [int(i) for i in (ids or [])]
        except (TypeError, ValueError):
            return jsonify({"error": "bad ids"}), 400
        n = runner.pipeline().set_artist_selected(
            ids, bool(body.get("selected", True)))
        return jsonify({"ok": True, "selected": n})

    @app.get("/api/settings")
    def api_settings_get():
        p = runner.pipeline()
        return jsonify({
            "discover": {k: bool(p._opt("discover", k))
                         for k in DISCOVER_OPTS},
            "discography": {"mode": p._opt("discography", "mode")},
            "download": {"include_discography":
                         bool(p._opt("download", "include_discography"))},
            "selected_artists": len(p._selected_artist_ids()),
        })

    @app.post("/api/settings")
    def api_settings_post():
        body = request.get_json(force=True, silent=True) or {}
        db_ = runner.pipeline().db
        for k, v in (body.get("discover") or {}).items():
            if k in DISCOVER_OPTS:
                db_.kv_set(f"opt.discover.{k}", bool(v))
        mode = (body.get("discography") or {}).get("mode")
        if mode in DISCOVERY_MODES:
            db_.kv_set("opt.discography.mode", mode)
        inc = (body.get("download") or {}).get("include_discography")
        if inc is not None:
            db_.kv_set("opt.download.include_discography", bool(inc))
        return jsonify({"ok": True})

    @app.get("/api/tracks")
    def api_tracks():
        status = request.args.get("status") or None
        artist = request.args.get("artist") or None
        q = (request.args.get("q") or "").strip()
        limit = min(int(request.args.get("limit", 200)), 1000)
        offset = int(request.args.get("offset", 0))
        clauses, args = [], []
        if status:
            clauses.append("status=?")
            args.append(status)
        if artist:
            clauses.append("artist_key=?")
            args.append(artist)
        if q:
            clauses.append("(title LIKE ? OR album LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            total = db().q1(
                f"SELECT COUNT(*) c FROM tracks {where}",
                tuple(args))["c"]
            rows = db().q(
                f"""SELECT id, video_id, title, artists, album, duration_sec,
                           status, mb_album, mb_track_number, file_path, error
                    FROM tracks {where}
                    ORDER BY artist_key, mb_album, mb_track_number, title
                    LIMIT ? OFFSET ?""",
                tuple(args) + (limit, offset))
        except Exception as e:
            return jsonify({"error": str(e), "total": 0, "rows": []}), 503
        out = []
        for r in rows:
            d = dict(r)
            try:
                arts = json.loads(d.get("artists") or "[]")
                d["artist_names"] = [a.get("name") for a in arts
                                     if isinstance(a, dict)][:3]
            except Exception:
                d["artist_names"] = []
            d.pop("artists", None)
            out.append(d)
        return jsonify({"total": total, "rows": out})

    @app.get("/api/logs")
    def api_logs():
        since = int(request.args.get("since", 0))
        lines = [l for l in capture.buf if l[0] > since]
        nxt = lines[-1][0] if lines else since
        return jsonify({"lines": lines, "next": nxt})

    # ---------------- actions ----------------
    @app.post("/api/run")
    def api_run():
        body = request.get_json(force=True, silent=True) or {}
        steps = body.get("steps") or []
        if isinstance(steps, str):
            steps = [s.strip() for s in steps.split(",") if s.strip()]
        valid = [s for s in steps
                 if s in Runner.STEPS or s == "all"]
        if not valid:
            return jsonify({"error": "no valid steps"}), 400
        queued = runner.run_steps(valid)
        return jsonify({"queued": queued})

    @app.post("/api/add")
    def api_add():
        body = request.get_json(force=True, silent=True) or {}
        target = (body.get("target") or "").strip()
        if not target:
            return jsonify({"error": "target required"}), 400
        runner.enqueue(f"add {target[:60]}", lambda: runner.pipeline().add(target))
        return jsonify({"ok": True})

    @app.post("/api/reconnect")
    def api_reconnect():
        runner.reset_clients()
        runner.enqueue("auth", lambda: (
            runner.pipeline().ytm(require_auth=True), {"ok": True})[1])
        return jsonify({"ok": True})

    # ---------------- auth / sign-in ----------------
    def _auth_info() -> dict:
        from . import auth as authmod
        cfg = runner.cfg
        cookies = cfg.path("cookies_file")
        auth_json = cfg.path("state_db").with_suffix(".auth.json")
        info = {"cookies_file": cookies.is_file(),
                "auth_json": auth_json.is_file(),
                "account": None, "error": None}
        st = runner.state.get("auth_check") or {}
        info.update(st)
        return info

    @app.get("/api/auth/status")
    def api_auth_status():
        return jsonify(_auth_info())

    @app.get("/api/auth/browsers")
    def api_auth_browsers():
        from . import auth as authmod
        try:
            return jsonify(authmod.detect_browsers())
        except Exception as e:
            return jsonify({"error": str(e)})

    def _do_auth():
        from . import auth as authmod
        runner.reset_clients()
        p = runner.pipeline()
        authmod.ensure_auth(runner.cfg)
        acct = authmod.validate_auth(
            runner.cfg.path("state_db").with_suffix(".auth.json"))
        name = acct.get("accountName") or acct.get("email") or "signed in"
        runner.state["auth_check"] = {"account": name, "error": None}
        return {"ok": True, "account": name}

    @app.post("/api/auth/browser")
    def api_auth_browser():
        from . import auth as authmod
        body = request.get_json(force=True, silent=True) or {}
        browser = body.get("browser")
        profile = body.get("profile")
        if not browser:
            return jsonify({"error": "browser required"}), 400
        try:
            n = authmod.cookies_from_browser(
                browser, profile, runner.cfg.path("cookies_file"))
            if n < 5:
                return jsonify({"error": "too few YouTube cookies — "
                                "are you logged into YouTube Music?"}), 400
            return jsonify(_do_auth())
        except Exception as e:
            log.warning("browser auth failed: %s", e)
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 400

    @app.post("/api/auth/cookies")
    def api_auth_cookies():
        from . import auth as authmod
        body = request.get_json(force=True, silent=True) or {}
        text = body.get("text") or ""
        cookies_path = runner.cfg.path("cookies_file")
        try:
            stripped = text.lstrip()
            if stripped.startswith("[") or stripped.startswith("{"):
                n = authmod.save_json_cookies(text, cookies_path)
                log.info("imported %d cookies from JSON paste", n)
            else:
                cookies = authmod.parse_cookies_text(text)
                if "__Secure-3PAPISID" not in cookies:
                    return jsonify({"error": "missing __Secure-3PAPISID — "
                                    "export ALL cookies for "
                                    "music.youtube.com while logged in"}), 400
                cookies_path.write_text(text)
            return jsonify(_do_auth())
        except authmod.AuthError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            log.warning("cookie auth failed: %s", e)
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 400

    return app


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ytmp-dl-gui")
    ap.add_argument("--port", type=int, default=8377)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--root", default=None)
    args = ap.parse_args(argv)

    from .util import setup_logging
    setup_logging()
    url = f"http://{args.host}:{args.port}"
    exposed = args.host not in ("127.0.0.1", "localhost", "::1")
    import os
    root = Path(args.root) if args.root else find_root()
    cfg_token = Config.load(root).get("gui", "token", "")
    token = os.environ.get("YTMP_GUI_TOKEN") or str(cfg_token or "")
    if not token and exposed:
        import secrets
        token = secrets.token_urlsafe(18)
        os.environ["YTMP_GUI_TOKEN"] = token
        print("WARNING: no [gui] token configured — generated an "
              "ephemeral one for this session (set [gui] token in "
              "config.toml for a permanent URL)", file=sys.stderr)
    app = create_app(root)
    if token:
        url += f"/?key={token}"
    print(f"ytmp-dl GUI → {url}  (ctrl-c to stop)", file=sys.stderr)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=args.host, port=args.port, threaded=True,
            use_reloader=False)
    return 0
