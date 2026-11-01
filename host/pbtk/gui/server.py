"""pbtk GUI: a local web app to watch and drive the reader.

    pbtk gui            → http://127.0.0.1:8629

Live mirror of the running app (click = tap, drag = swipe, hold = long press),
hardware keys, the shared activity timeline (Claude's MCP calls, your clicks,
taps made on the reader itself), app log, file browser, deploy/eject, and a
"hold" switch that blocks Claude's input while you drive.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import PbtkError, activity, paths, project, script
from ..device import Device

STATIC = Path(__file__).parent / "static"
OBSERVED = {"pointerup", "pointerlong", "keypress"}


class App:
    def __init__(self, agent_addr: str | None = None):
        self.dev = Device(source="gui", agent_addr=agent_addr, discover_timeout=0)
        self.agent_addr = agent_addr
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.events_seq: int | None = None
        self._last_discover = 0.0
        threading.Thread(target=self._observe_loop, daemon=True).start()

    def agent(self, discover: bool = False):
        """The agent (explicit or last-known address). With discover=True, also
        listen for beacons — at most every 10 s, since that blocks for ~3 s."""
        a = self.dev.agent_or_none()
        if a is None and discover and time.monotonic() - self._last_discover > 10:
            self._last_discover = time.monotonic()
            self.dev._discover_timeout = 3.5
            a = self.dev.agent_or_none()
            self.dev._discover_timeout = 0
        return a

    def _observe_loop(self) -> None:
        """Copy the user's own input on the reader onto the timeline."""
        while not self.stop.wait(1.0):
            try:
                a = self.dev._agent  # only poll while connected; do not trigger discovery here
                if a is None:
                    self.events_seq = None
                    continue
                if self.events_seq is None:
                    _, self.events_seq = a.events(1 << 40)  # start from "now"
                    continue
                events, self.events_seq = a.events(self.events_seq)
                for e in events:
                    if e["type"] not in OBSERVED:
                        continue
                    args = {"key": e.get("key")} if "key" in e else {"x": e["p1"], "y": e["p2"]}
                    activity.record("device", e["type"], args)
            except PbtkError:
                self.events_seq = None
            except Exception:  # noqa: BLE001 — the poller must survive anything
                self.events_seq = None


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pbtk-gui"

        def log_message(self, fmt, *args):  # quiet
            pass

        # ----------------------------------------------------- responses
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _error(self, e: Exception, code: int = 409):
            self._json({"error": str(e)}, code)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return {}

        def _same_origin(self) -> bool:
            """Reject cross-site POSTs (a web page must not drive the reader)."""
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            return origin is None or urlparse(origin).netloc == host

        # ---------------------------------------------------------- GET
        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    return self._static("index.html")
                if u.path.startswith("/static/"):
                    return self._static(u.path[len("/static/"):])
                if u.path == "/api/status":
                    app.agent(discover=True)  # (re)discover when the agent is gone
                    return self._json(app.dev.status())
                if u.path == "/api/screen.png":
                    a = app.agent()
                    if not a:
                        return self._json({"error": "no agent"}, 503)
                    img = a.fbshot() if q.get("whole") == "1" else a.shot()
                    return self._send(200, img.png(), "image/png",
                                      {"X-Width": str(img.width), "X-Height": str(img.height)})
                if u.path.startswith("/api/shots/"):
                    name = Path(u.path).name
                    p = paths.shots_dir() / name
                    if not p.is_file():
                        return self._json({"error": "not found"}, 404)
                    return self._send(200, p.read_bytes(), "image/png")
                if u.path == "/api/activity":
                    since = int(q.get("since", -1))
                    if since < 0:
                        since = activity.tail_offset(200)
                    entries, nxt = activity.read(since)
                    return self._json({"entries": entries, "next": nxt})
                if u.path == "/api/stream":
                    return self._stream(int(q.get("since", -1)))
                if u.path == "/api/log":
                    return self._log(q)
                if u.path == "/api/files":
                    entries, via = app.dev.ls(q.get("path", "/mnt/ext1/applications"))
                    return self._json({"entries": entries, "via": via})
                if u.path == "/api/file":
                    data = app.dev.read_file(q["path"])
                    return self._send(200, data, "application/octet-stream",
                                      {"Content-Disposition": f'attachment; filename="{Path(q["path"]).name}"'})
                if u.path == "/api/script":
                    entries, _ = activity.read(activity.tail_offset(int(q.get("n", 200))))
                    srcs = set((q.get("sources") or "gui,mcp,device,cli,script").split(","))
                    text = script.to_script([e for e in entries if e["source"] in srcs])
                    return self._send(200, text.encode(), "text/plain; charset=utf-8")
                return self._json({"error": "not found"}, 404)
            except PbtkError as e:
                return self._error(e)
            except (BrokenPipeError, ConnectionResetError):
                return None

        def _static(self, name: str):
            p = (STATIC / name).resolve()
            if STATIC.resolve() not in p.parents or not p.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            return self._send(200, p.read_bytes(), ctype)

        def _log(self, q: dict):
            a = app.agent() if not q.get("path") else None
            if a:
                off = int(q.get("offset", -16384))
                data, nxt = a.log(off)
                return self._json({"text": data.decode("utf-8", "replace"), "next": nxt,
                                   "where": f"agent {a.address}", "live": True})
            log = q.get("path")
            if not log and q.get("project"):
                log = project.load(q["project"]).device_log
            text, where = app.dev.logs(200, log)
            return self._json({"text": text, "next": None, "where": where, "live": False})

        def _stream(self, since: int):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if since < 0:
                since = activity.tail_offset(0)
            last_beat = time.monotonic()
            try:
                while not app.stop.is_set():
                    entries, since = activity.read(since)
                    for e in entries:
                        self.wfile.write(f"data: {json.dumps(e)}\n\n".encode())
                    if entries:
                        self.wfile.flush()
                    if time.monotonic() - last_beat > 15:
                        self.wfile.write(b": beat\n\n")
                        self.wfile.flush()
                        last_beat = time.monotonic()
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError):
                return

        # --------------------------------------------------------- POST
        def do_POST(self):
            if not self._same_origin():
                return self._json({"error": "cross-origin request refused"}, 403)
            u = urlparse(self.path)
            b = self._body()
            d = app.dev
            try:
                if u.path in ("/api/tap", "/api/long", "/api/swipe", "/api/key"):
                    if not app.agent():
                        return self._json({"error": "no agent: open an app with libpbagent on the reader"}, 503)
                if u.path == "/api/tap":
                    d.tap(int(b["x"]), int(b["y"]))
                elif u.path == "/api/long":
                    d.long_press(int(b["x"]), int(b["y"]))
                elif u.path == "/api/swipe":
                    d.swipe(int(b["x1"]), int(b["y1"]), int(b["x2"]), int(b["y2"]), int(b.get("ms", 300)))
                elif u.path == "/api/key":
                    d.key(str(b["name"]))
                elif u.path == "/api/refresh":
                    d.refresh()
                elif u.path == "/api/exit":
                    d.exit_app()
                elif u.path == "/api/hold":
                    activity.set_hold(bool(b.get("on")))
                    activity.record("gui", "hold", {"on": bool(b.get("on"))})
                elif u.path == "/api/screenshot":
                    _, path = d.screenshot(whole=bool(b.get("whole")))
                    return self._json({"ok": True, "shot": path.name})
                elif u.path == "/api/deploy":
                    proj = project.load(b.get("project") or None)
                    if b.get("wifi"):
                        r = d.deploy_wifi(proj, build=bool(b.get("build")), restart=bool(b.get("restart")))
                        return self._json({"ok": True, "app": proj.name, "via": "wifi", **r})
                    changed = d.deploy(proj, build=bool(b.get("build")), eject=bool(b.get("eject")))
                    return self._json({"ok": True, "changed": changed, "app": proj.name, "via": "usb"})
                elif u.path == "/api/restart":
                    return self._json({"ok": True, **d.restart_app()})
                elif u.path == "/api/eject":
                    return self._json({"ok": True, "device": d.eject()})
                elif u.path == "/api/connect":
                    addr = (b.get("address") or "").strip()
                    if addr:
                        from .. import agent as agent_mod

                        host, _, port = addr.partition(":")
                        agent_mod.remember(host, int(port or 8628))
                    d.close()
                    d._agent = None
                    d._agent_addr = addr or app.agent_addr
                    app._last_discover = 0
                    return self._json({"ok": bool(app.agent(discover=True))})
                else:
                    return self._json({"error": "not found"}, 404)
                return self._json({"ok": True})
            except PbtkError as e:
                return self._error(e)
            except (KeyError, ValueError, TypeError) as e:
                return self._json({"error": f"bad request: {e}"}, 400)

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 8629, agent_addr: str | None = None):
    app = App(agent_addr)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8629, agent_addr: str | None = None,
          open_browser: bool = True) -> None:
    httpd = make_server(host, port, agent_addr)
    url = f"http://{host}:{port}/"
    print(f"pbtk gui on {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.app.stop.set()  # type: ignore[attr-defined]
        httpd.server_close()
