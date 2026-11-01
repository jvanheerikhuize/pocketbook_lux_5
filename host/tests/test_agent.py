"""End to end: pbtk's client against the real libpbagent (demo app on the stub)."""

import shutil
import socket
import time
import unittest
import zlib

from pbtk import PbtkError
from pbtk.agent import AgentClient
from pbtk.device import Device
from pbtk.project import Project
from pbtk import activity

from support import BUTTON1, BUTTON2, FB_H, FB_W, TOKEN, DemoApp, IsolatedState, fb_pixel


def inside(rect, dx=40, dy=30):
    x, y, _, _ = rect
    return x + dx, y + dy


class AgentTest(IsolatedState):
    def setUp(self):
        super().setUp()
        self.app = DemoApp(self.tmp)
        self.client = AgentClient("127.0.0.1", self.app.port, TOKEN, timeout=5)

    def tearDown(self):
        self.client.close()
        self.app.stop()
        super().tearDown()

    def test_rejects_wrong_token(self):
        bad = AgentClient("127.0.0.1", self.app.port, "wrong-token-wrong-token", timeout=5)
        with self.assertRaisesRegex(PbtkError, "refused the token"):
            bad.ping()

    def test_rejects_commands_before_auth(self):
        with socket.create_connection(("127.0.0.1", self.app.port), timeout=3) as s:
            s.sendall(b"shot\n")
            self.assertTrue(s.recv(100).startswith(b"err auth required"))

    def test_info(self):
        info = self.client.info()
        self.assertEqual(info["app"], "pbtk-demo")
        self.assertEqual((info["width"], info["height"], info["depth"]), ("758", "1024", "8"))
        self.assertEqual(info["model"], "host-stub")
        # The demo turns the firmware status panel off, so canvas == panel.
        self.assertEqual((info["panel_type"], info["panel_fb_offset"]), ("0", "0"))

    def test_no_panel_warning_for_demo(self):
        time.sleep(3.5)
        self.client.ping()
        time.sleep(0.5)
        self.assertNotIn(b"status panel is on", self.app.log.read_bytes())

    def test_shot_tap_changes_pixels(self):
        x, y = inside(BUTTON1)
        before = self.client.shot()
        self.assertEqual((before.width, before.height), (758, 1024))
        self.assertEqual(before.at(x, y), 255)
        r = self.client.tap(*inside(BUTTON1, 150, 120))
        self.assertEqual(r.get("dispatched"), "1")
        after = self.client.shot()
        self.assertEqual(after.at(x, y), 0, "button 1 should be filled black after a tap")
        self.assertEqual(after.at(*inside(BUTTON2)), 255, "button 2 untouched")

    def test_keys_select_and_toggle(self):
        self.client.key("next")  # select button 2
        self.client.key("ok")    # toggle it
        img = self.client.shot()
        self.assertEqual(img.at(*inside(BUTTON2)), 0)
        self.assertEqual(img.at(*inside(BUTTON1)), 255)

    def test_fbshot_rotations(self):
        raw = self.client.fbshot("raw")
        self.assertEqual((raw.width, raw.height), (FB_W, FB_H))
        self.assertEqual(raw.pixels, bytes(fb_pixel(x, y) for y in range(FB_H) for x in range(FB_W)))
        self.assertNotIn(0xEE, raw.pixels, "stride padding must be skipped")

        cw = self.client.fbshot("cw")
        self.assertEqual((cw.width, cw.height), (FB_H, FB_W))
        for ny in range(FB_W):
            for nx in range(FB_H):
                self.assertEqual(cw.at(nx, ny), fb_pixel(ny, FB_H - 1 - nx))
        # Same convention as the host-side rotation used by `pbtk probe collect`.
        self.assertEqual(cw.pixels, raw.rotate_cw().pixels)

        ccw = self.client.fbshot("ccw")
        for ny in range(FB_W):
            for nx in range(FB_H):
                self.assertEqual(ccw.at(nx, ny), fb_pixel(FB_W - 1 - ny, nx))

        self.assertEqual(self.client.fbshot().pixels, cw.pixels, "auto = cw for a landscape buffer")
        with self.assertRaisesRegex(PbtkError, "rotate must be"):
            self.client.fbshot("sideways")
        with self.assertRaisesRegex(PbtkError, "fbshot mode"):
            self.client.request("fbshot sideways")

    def test_fbshot_follows_pan(self):
        # The visible window starts at the pan offset and wraps round the buffer.
        (self.app.fb_sys / "pan").write_text("2,1\n")
        info = self.client.fbinfo()
        self.assertEqual((info["source"], info["xoffset"], info["yoffset"]), ("sysfs", "2", "1"))
        raw = self.client.fbshot("raw")
        for y in range(FB_H):
            for x in range(FB_W):
                self.assertEqual(raw.at(x, y), fb_pixel((x + 2) % FB_W, (y + 1) % FB_H))

    def test_fbinfo_unpanned(self):
        info = self.client.fbinfo()
        self.assertEqual(info["xres"], str(FB_W))
        self.assertEqual(info["line_length"], "8")
        self.assertEqual(info["xoffset"], "0")

    def test_calibrate_pattern_and_restore(self):
        self.assertEqual(self.client.calibrate().get("dispatched"), "1")
        img = self.client.shot()
        h = img.height
        self.assertEqual([img.at(300, y) for y in (0, 49, 50)], [0, 0, 255], "top bar is rows 0-49")
        self.assertEqual([img.at(300, y) for y in (h - 51, h - 50, h - 1)], [255, 0, 0], "bottom bar")
        self.assertEqual(img.at(0, 500), 0, "left edge")
        # 3 ruler blocks at y=300: three black runs starting at x=20.
        row = [img.at(x, 300) for x in range(10, 120)]
        runs = sum(1 for i in range(1, len(row)) if row[i] == 0 and row[i - 1] != 0)
        self.assertEqual(runs, 3)
        self.client.calibrate(False)
        self.assertEqual(self.client.shot().at(300, 10), 255, "app repainted")

    def test_fbshot_missing_device(self):
        self.app.fb.unlink()
        with self.assertRaisesRegex(PbtkError, "read .*fb0"):
            self.client.fbshot()
        self.client.ping()  # connection still fine

    def test_unknown_key_and_command(self):
        with self.assertRaisesRegex(PbtkError, "unknown key"):
            self.client.key("nope")
        with self.assertRaisesRegex(PbtkError, "unknown command"):
            self.client.request("frobnicate")

    def test_log_offsets(self):
        self.client.tap(*inside(BUTTON1, 150, 120))
        data, nxt = self.client.log(0)
        self.assertIn(b"demo: tap 1 -> on", data)
        self.assertIn(b"pbagent: listening", data)
        more, nxt2 = self.client.log(nxt)
        self.assertEqual(more, b"")
        self.assertEqual(nxt2, nxt)
        tail, _ = self.client.log(-10)
        self.assertEqual(len(tail), 10)

    def test_long_press_and_swipe(self):
        self.client.long_press(*inside(BUTTON2, 150, 120))
        data, _ = self.client.log(0)
        self.assertIn(b"long press on 2", data)
        r = self.client.swipe(100, 900, 600, 900, 200)
        self.assertEqual(r.get("dispatched"), "1")

    def test_injected_events_are_not_reported_as_user_input(self):
        self.client.tap(10, 10)
        events, nxt = self.client.events(0)
        self.assertEqual([e for e in events if e["type"].startswith("pointer")], [])
        self.assertGreaterEqual(nxt, 0)

    def test_files_inside_bundle_only(self):
        p = str(self.app.dir / "note.txt")
        self.client.put(p, b"hello\n")
        self.assertEqual(self.client.get(p), b"hello\n")
        names = [e["name"] for e in self.client.ls(str(self.app.dir))]
        self.assertIn("note.txt", names)
        with self.assertRaisesRegex(PbtkError, "path not allowed"):
            self.client.get("/etc/passwd")
        with self.assertRaisesRegex(PbtkError, "path not allowed"):
            self.client.get(str(self.app.dir) + "/../../etc/passwd")
        # The connection is still usable after a rejected put (payload was consumed).
        with self.assertRaisesRegex(PbtkError, "path not allowed"):
            self.client.put("/etc/pbtk-test", b"x" * 1000)
        self.client.ping()


    def test_sum_matches_zlib(self):
        data = bytes(range(256)) * 37
        p = str(self.app.dir / "blob.bin")
        self.client.put(p, data)
        self.assertEqual(self.client.sum(p), (zlib.crc32(data), len(data)))
        self.assertIsNone(self.client.sum(str(self.app.dir / "nope.bin")))

    def test_put_creates_parent_folders(self):
        p = self.app.dir / "a" / "b" / "c.txt"
        self.client.put(str(p), b"deep\n")
        self.assertEqual(p.read_bytes(), b"deep\n")

    def test_restart_keeps_pid_and_changes_start(self):
        before = self.client.info()
        self.assertIn("build", before)
        self.client.restart()
        after = self.client.wait_restarted(before["started"], timeout=10)
        self.assertEqual(after["pid"], before["pid"])
        self.assertNotEqual(after["started"], before["started"])
        self.assertIsNone(self.app.proc.poll())
        self.client.ping()

    def test_deploy_wifi_copies_changes_and_restarts(self):
        # A project whose dist mirrors the running bundle: <apps_root>/bundle/...
        root = self.tmp / "proj"
        dist = root / "dist"
        (dist / "bundle").mkdir(parents=True)
        shutil.copy2(self.app.exe, dist / "bundle" / "pbtk-demo")  # same as running: unchanged
        (dist / "bundle" / "assets").mkdir()
        (dist / "bundle" / "assets" / "level1.txt").write_bytes(b"new level\n")
        proj = Project(root=root, name="pbtk-demo", dist=dist, log=None, build=None)
        d = Device(source="cli", agent_addr=self.app.address, discover_timeout=0)
        d.apps_root = str(self.tmp)
        try:
            r = d.deploy_wifi(proj, restart=True)
            self.assertEqual(r["changed"], [f"{self.tmp}/bundle/assets/level1.txt"])
            self.assertEqual(r["unchanged"], 1)
            self.assertTrue(r["restarted"])
            self.assertTrue(r["pid_kept"])
            self.assertEqual((self.app.dir / "assets" / "level1.txt").read_bytes(), b"new level\n")
            # Second run: nothing to send.
            r = d.deploy_wifi(proj)
            self.assertEqual((r["changed"], r["unchanged"], r["restarted"]), ([], 2, False))
            # Restart refuses to replace a different app.
            other = Project(root=root, name="mmiv", dist=dist, log=None, build=None)
            with self.assertRaisesRegex(PbtkError, "running app is"):
                d.deploy_wifi(other, restart=True)
        finally:
            d.close()

    def test_several_clients(self):
        others = [AgentClient("127.0.0.1", self.app.port, TOKEN, timeout=5) for _ in range(3)]
        try:
            for o in others:
                o.ping()
            self.client.ping()
        finally:
            for o in others:
                o.close()

    def test_exit_closes_app(self):
        self.client.exit_app()
        self.app.proc.wait(5)
        self.assertEqual(self.app.proc.returncode, 0)
        self.assertIn(b"demo: exit", self.app.log.read_bytes())

    def test_device_facade_records_activity_and_shot(self):
        d = Device(source="mcp", agent_addr=self.app.address, discover_timeout=0)
        _, img, path = d.tap(*inside(BUTTON1, 150, 120), shot_after=True, settle_ms=50)
        self.assertTrue(path.exists())
        self.assertEqual(img.at(*inside(BUTTON1)), 0)
        entries, _ = activity.read(0)
        self.assertEqual(entries[-1]["source"], "mcp")
        self.assertEqual(entries[-1]["action"], "tap")
        self.assertEqual(entries[-1]["shot"], path.name)
        d.close()

    def test_hold_blocks_non_gui_sources(self):
        activity.set_hold(True)
        try:
            d = Device(source="mcp", agent_addr=self.app.address, discover_timeout=0)
            with self.assertRaisesRegex(PbtkError, "taken control"):
                d.tap(1, 1)
            g = Device(source="gui", agent_addr=self.app.address, discover_timeout=0)
            g.tap(1, 1)
            d.close()
            g.close()
        finally:
            activity.set_hold(False)


class BeaconTest(IsolatedState):
    def test_discovery_hears_beacon(self):
        from pbtk.agent import discover

        app = DemoApp(self.tmp)
        try:
            # Beacons go to the broadcast address on the agent's port.
            found = discover(timeout=4.0, port=app.port)
        finally:
            app.stop()
        if not found:
            self.skipTest("no broadcast delivery in this environment")
        self.assertEqual(found[0].app, "pbtk-demo")
        self.assertEqual(found[0].port, app.port)


if __name__ == "__main__":
    unittest.main()
