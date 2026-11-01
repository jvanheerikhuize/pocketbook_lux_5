"""GUI HTTP API, MCP server and scripts, against the demo app's real agent."""

import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pbtk import activity, script
from pbtk.device import Device
from pbtk.gui.server import make_server
from pbtk.image import png_size
from pbtk.mcp import Server, serve

from support import BUTTON1, ROOT, DemoApp, IsolatedState


class GuiTest(IsolatedState):
    def setUp(self):
        super().setUp()
        self.app = DemoApp(self.tmp)
        self.httpd = make_server("127.0.0.1", 0, agent_addr=self.app.address)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.app.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.stop()
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.headers, r.read()

    def post(self, path, body, origin=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_page_status_screen(self):
        code, _, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"pbtk console", body)
        _, _, st = self.get("/api/status")
        self.assertEqual(json.loads(st)["agent"]["app"], "pbtk-demo")
        code, headers, png = self.get("/api/screen.png")
        self.assertEqual(png_size(png), (758, 1024))
        self.assertEqual(headers["X-Width"], "758")

    def test_tap_records_gui_activity(self):
        x, y, _, _ = BUTTON1
        code, body = self.post("/api/tap", {"x": x + 150, "y": y + 120})
        self.assertEqual((code, body), (200, {"ok": True}))
        _, _, raw = self.get("/api/activity?since=0")
        entries = json.loads(raw)["entries"]
        self.assertEqual(entries[-1]["source"], "gui")
        self.assertIn(b"tap 1 -> on", self.app.log.read_bytes())
        _, _, text = self.get("/api/script?sources=gui")
        self.assertIn(f"tap {x + 150} {y + 120}", text.decode())

    def test_cross_origin_post_refused(self):
        code, body = self.post("/api/tap", {"x": 1, "y": 1}, origin="http://evil.example")
        self.assertEqual(code, 403)

    def test_hold_and_log_and_files(self):
        self.assertEqual(self.post("/api/hold", {"on": True})[0], 200)
        self.assertTrue(activity.held())
        self.post("/api/hold", {"on": False})
        self.assertFalse(activity.held())
        _, _, raw = self.get("/api/log?offset=0")
        log = json.loads(raw)
        self.assertTrue(log["live"])
        self.assertIn("pbagent: listening", log["text"])
        _, _, raw = self.get(f"/api/files?path={self.app.dir}")
        self.assertIn("pbtk-demo", [e["name"] for e in json.loads(raw)["entries"]])

    def test_stream_delivers_new_entries(self):
        offset = activity.tail_offset(0)
        got = []

        def reader():
            with urllib.request.urlopen(f"{self.base}/api/stream?since={offset}", timeout=10) as r:
                for line in r:
                    if line.startswith(b"data: "):
                        got.append(json.loads(line[6:]))
                        return

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.3)
        activity.record("mcp", "tap", {"x": 5, "y": 6})
        t.join(5)
        self.assertEqual(got and got[0]["source"], "mcp")

    def test_observed_device_input_lands_on_timeline(self):
        self.get("/api/status")  # connect; the poller then takes its "now" baseline
        time.sleep(2.3)
        self.post("/api/tap", {"x": 10, "y": 10})  # injected: must not show as device input
        self.app.user_input("tap 300 400\nkey 0x19\n")  # the person touching the reader
        deadline = time.monotonic() + 5
        device = []
        while time.monotonic() < deadline and len(device) < 2:
            time.sleep(0.3)
            device = [e for e in activity.read(0)[0] if e["source"] == "device"]
        self.assertEqual([(e["action"], e["args"]) for e in device],
                         [("pointerup", {"x": 300, "y": 400}), ("keypress", {"key": "next"})])


class McpTest(IsolatedState):
    def setUp(self):
        super().setUp()
        self.app = DemoApp(self.tmp)
        self.server = Server(agent_addr=self.app.address)

    def tearDown(self):
        self.server.dev.close()
        self.app.stop()
        super().tearDown()

    def rpc(self, method, params=None, id_=1):
        return self.server.handle({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})

    def test_handshake_and_tools(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "pbtk")
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertTrue({"pb_screenshot", "pb_tap", "pb_key", "pb_logs", "pb_deploy"} <= names)

    def test_screenshot_and_tap_return_images(self):
        r = self.rpc("tools/call", {"name": "pb_screenshot", "arguments": {}})["result"]
        img = [c for c in r["content"] if c["type"] == "image"][0]
        self.assertEqual(png_size(base64.b64decode(img["data"])), (758, 1024))
        x, y, _, _ = BUTTON1
        r = self.rpc("tools/call", {"name": "pb_tap", "arguments": {"x": x + 150, "y": y + 120}})["result"]
        self.assertFalse(r["isError"])
        self.assertTrue(any(c["type"] == "image" for c in r["content"]))
        entries, _ = activity.read(0)
        self.assertEqual(entries[-1]["source"], "mcp")

    def test_errors_are_tool_errors(self):
        r = self.rpc("tools/call", {"name": "pb_key", "arguments": {"name": "bogus"}})["result"]
        self.assertTrue(r["isError"])
        r = self.rpc("tools/call", {"name": "pb_tap", "arguments": {}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("error", self.rpc("nope/method"))

    def test_stdio_loop(self):
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n\nnot json\n")
        stdout = io.StringIO()
        serve(agent_addr=self.app.address, stdin=stdin, stdout=stdout)
        self.assertEqual(json.loads(stdout.getvalue()), {"jsonrpc": "2.0", "id": 7, "result": {}})

    def test_cli_entrypoint(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "host"))
        r = subprocess.run([sys.executable, "-m", "pbtk", "--agent", self.app.address, "info"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pbtk-demo", r.stdout)


class ScriptTest(IsolatedState):
    def test_record_and_replay(self):
        app = DemoApp(self.tmp)
        try:
            d = Device(source="gui", agent_addr=app.address, discover_timeout=0)
            x, y, _, _ = BUTTON1
            d.tap(x + 150, y + 120)
            d.key("next")
            entries, _ = activity.read(0)
            text = script.to_script(entries)
            self.assertIn(f"tap {x + 150} {y + 120}", text)
            self.assertIn("key next", text)
            text += "expect-log key 0x19\nshot final\n"
            runner = Device(source="script", agent_addr=app.address, discover_timeout=0)
            shots = script.run(runner, text, self.tmp / "shots", echo=lambda *_: None)
            self.assertEqual([p.name for p in shots], ["final.png"])
            runner.close()
            d.close()
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
