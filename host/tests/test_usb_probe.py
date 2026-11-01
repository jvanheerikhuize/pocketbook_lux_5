"""USB storage transport, deploy, token, probe install/collect — on a fake mount."""

import json
import os
import struct
import time
import unittest
import zlib
from pathlib import Path

from pbtk import PbtkError, paths, probe, project
from pbtk.device import Device
from pbtk.image import Gray, from_framebuffer, parse_pgm, png_size
from pbtk.usb import UsbStorage, find_mount

from support import ROOT, TOKEN, IsolatedState


class ImageTest(unittest.TestCase):
    def test_pgm_png_roundtrip(self):
        pgm = b"P5\n3 2\n255\n" + bytes([0, 128, 255, 10, 20, 30])
        g = parse_pgm(pgm)
        self.assertEqual((g.width, g.height, g.at(1, 0), g.at(2, 1)), (3, 2, 128, 30))
        png = g.png()
        self.assertEqual(png_size(png), (3, 2))
        # IDAT decompresses to filter byte + row per line.
        idat = png[png.index(b"IDAT") + 4 : png.index(b"IEND") - 8]
        self.assertEqual(zlib.decompress(idat), b"\x00\x00\x80\xff\x00\x0a\x14\x1e")

    def test_rotate_cw(self):
        # 3x2:  a b c      rotated:  d a
        #       d e f                e b
        #                            f c
        g = Gray(3, 2, b"abcdef").rotate_cw()
        self.assertEqual((g.width, g.height, g.pixels), (2, 3, b"daebfc"))

    def test_framebuffer_formats(self):
        self.assertEqual(from_framebuffer(bytes([1, 2, 0, 3, 4, 0]), 2, 2, 8, 3).pixels, bytes([1, 2, 3, 4]))
        white32 = bytes([255, 255, 255, 0]) * 2
        self.assertEqual(from_framebuffer(white32, 2, 1, 32, 8).pixels, bytes([255, 255]))
        rgb565_white = struct.pack("<H", 0xFFFF)
        # 0xFFFF → r=248 g=252 b=248 → luma 250
        self.assertEqual(from_framebuffer(rgb565_white, 1, 1, 16, 2).pixels[0], 250)


class UsbTest(IsolatedState):
    def test_find_mount_and_paths(self):
        m = self.fake_mount()
        self.assertEqual(find_mount(), m)
        u = UsbStorage(m)
        self.assertEqual(u.local("/mnt/ext1/applications"), (m / "applications").resolve())
        with self.assertRaises(PbtkError):
            u.local("/mnt/ext1/../../etc/passwd")

    def test_no_mount(self):
        with self.assertRaisesRegex(PbtkError, "not mounted"):
            UsbStorage.connect()

    def test_deploy_is_incremental_and_installs_token(self):
        m = self.fake_mount()
        proj_dir = self.tmp / "proj"
        dist = proj_dir / "dist" / "applications"
        (dist / "demo").mkdir(parents=True)
        (dist / "demo" / "demo").write_bytes(b"\x7fELF...")
        (dist / "demo.app").write_text("#!/bin/sh\n")
        (proj_dir / "pbtk.toml").write_text('[app]\nname = "demo"\nlog = "demo/demo.log"\n')
        proj = project.load(proj_dir)
        self.assertEqual(proj.device_log, "/mnt/ext1/applications/demo/demo.log")

        d = Device(source="cli", discover_timeout=0)
        changed = d.deploy(proj)
        self.assertEqual(sorted(changed), ["/mnt/ext1/applications/demo.app", "/mnt/ext1/applications/demo/demo"])
        self.assertEqual((m / "applications" / "pbagent.token").read_text().strip(), TOKEN)
        self.assertEqual(d.deploy(proj), [], "second deploy copies nothing")
        time.sleep(0.01)
        (dist / "demo.app").write_text("#!/bin/sh\necho changed\n")
        self.assertEqual(d.deploy(proj), ["/mnt/ext1/applications/demo.app"])

        (m / "applications" / "demo" / "demo.log").write_text("\n".join(f"line {i}" for i in range(100)))
        text, where = d.logs(5, proj.device_log)
        self.assertEqual(text.splitlines(), [f"line {i}" for i in range(95, 100)])
        self.assertTrue(where.startswith("usb"))

    def test_deploy_keeps_saves_on_the_reader(self):
        m = self.fake_mount()
        proj_dir = self.tmp / "proj"
        dist = proj_dir / "dist"
        (dist / "game" / "saves").mkdir(parents=True)
        (dist / "game" / "saves" / "slot.001").write_bytes(b"pc save")
        (dist / "game" / "saves" / "slot.002").write_bytes(b"pc only")
        (dist / "game" / "bin").write_bytes(b"new bin")
        (proj_dir / "pbtk.toml").write_text(
            '[app]\nname = "game"\ndist = "dist"\nkeep = ["game/saves/*"]\n')
        proj = project.load(proj_dir)
        on_reader = m / "applications" / "game" / "saves"
        on_reader.mkdir(parents=True)
        (on_reader / "slot.001").write_bytes(b"reader save, newer")
        changed = Device(source="cli", discover_timeout=0).deploy(proj)
        self.assertEqual(sorted(changed), ["/mnt/ext1/applications/game/bin",
                                           "/mnt/ext1/applications/game/saves/slot.002"])
        self.assertEqual((on_reader / "slot.001").read_bytes(), b"reader save, newer")
        self.assertEqual((on_reader / "slot.002").read_bytes(), b"pc only")

    def test_deploy_without_build_output(self):
        self.fake_mount()
        (self.tmp / "p").mkdir()
        (self.tmp / "p" / "pbtk.toml").write_text('[app]\nname = "x"\n')
        with self.assertRaisesRegex(PbtkError, "build first"):
            Device(discover_timeout=0).deploy(project.load(self.tmp / "p"))


SAMPLE_FACTS = """probe.version=1
probe.launch=1
user.uid=0
user.name=root
sys.firmware=U628.6.9.988
sys.kernel=3.10.65
fb.exists=1
fb.readable=1
fb.writable=1
fb.virtual_size=4,2
fb.bits_per_pixel=8
fb.stride=4
input.event0.name=sunxi-keyboard
input.event0.readable=1
input.event0.writable=1
input.event1.name=cyttsp5_mt
input.event1.readable=1
input.event1.writable=1
input.uinput_writable=0
net.ipv4=192.168.1.40
root.pbjb=0
proc.dropbear_running=0
libc.softfloat_loader=1
libc.hardfloat_loader=0
probe.done=1
"""


class ProbeTest(IsolatedState):
    def test_install_copies_launcher_and_script(self):
        m = self.fake_mount()
        os.environ["PBTK_ROOT"] = str(ROOT)
        try:
            probe.install(UsbStorage(m))
        finally:
            os.environ.pop("PBTK_ROOT")
        self.assertTrue((m / "applications" / "pbtk-probe.app").is_file())
        self.assertTrue((m / "applications" / "pbtk-probe" / "probe.sh").is_file())

    def test_collect_before_run(self):
        m = self.fake_mount()
        with self.assertRaisesRegex(PbtkError, "No probe results yet"):
            probe.collect(UsbStorage(m))

    def test_collect_interprets(self):
        m = self.fake_mount()
        d = m / "applications" / "pbtk-probe"
        d.mkdir()
        (d / "facts.txt").write_text(SAMPLE_FACTS)
        (d / "fb0.raw").write_bytes(bytes(range(0, 255, 32)))
        (d / "bg-alive.txt").write_text("alive uptime=100\n")
        result = probe.collect(UsbStorage(m))
        r = result["report"]
        self.assertTrue(r["runs_as_root"])
        self.assertTrue(r["background_survives"])
        self.assertEqual([i["role"] for i in r["inputs"]], ["keys", "touch"])
        self.assertTrue(r["tiers"]["system_wide_input_without_root"])
        self.assertTrue(Path(r["framebuffer_png"]).is_file())
        self.assertTrue((paths.state_dir() / "facts.json").is_file())
        self.assertIn("apps run as root  yes", probe.summary(result))
        json.dumps(result)


if __name__ == "__main__":
    unittest.main()
