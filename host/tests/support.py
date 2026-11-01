"""Shared fixtures: an isolated state dir, a fake USB mount, and the demo app
running on the host against the InkView stub (the real C agent)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "test-token-0123456789abcdef"

# Demo layout for the stub's 758x1024 screen (templates/app/main.c button_rect).
BUTTON1 = (63, 170, 300, 211)
BUTTON2 = (394, 170, 300, 211)


FB_W, FB_H, FB_STRIDE = 6, 4, 8


def fb_pixel(x: int, y: int) -> int:
    return y * FB_W + x + 1


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class IsolatedState(unittest.TestCase):
    """PBTK_STATE in a temp dir, a fixed token, no discovery side effects."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pbtk-test-"))
        self._env = {k: os.environ.get(k) for k in ("PBTK_STATE", "PBAGENT_TOKEN", "PBTK_AGENT", "PBTK_MOUNT")}
        os.environ["PBTK_STATE"] = str(self.tmp / "state")
        os.environ["PBAGENT_TOKEN"] = TOKEN
        os.environ.pop("PBTK_AGENT", None)
        os.environ["PBTK_MOUNT"] = str(self.tmp / "no-mount")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_mount(self) -> Path:
        m = self.tmp / "PB628"
        (m / "system").mkdir(parents=True)
        (m / "system" / ".pocketbook").touch()
        (m / "applications").mkdir()
        os.environ["PBTK_MOUNT"] = str(m)
        return m


_built = False


def build_demo() -> Path:
    global _built
    exe = ROOT / "templates" / "app" / "build" / "pbtk-demo-host"
    if not _built:
        r = subprocess.run(["make", "-s", "-C", str(ROOT / "templates" / "app"), "host"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise unittest.SkipTest(f"cannot build the demo for the host: {r.stderr}")
        _built = True
    return exe


class DemoApp:
    """The demo app with its agent, on a free localhost port."""

    def __init__(self, workdir: Path, token: str = TOKEN, env: dict | None = None):
        exe = build_demo()
        self.dir = workdir / "bundle"
        self.dir.mkdir(parents=True, exist_ok=True)
        # Run a copy so the bundle dir (and its log) is private to the test.
        self.exe = self.dir / "pbtk-demo"
        shutil.copy2(exe, self.exe)
        self.port = free_port()
        self.input_file = self.dir / "user-input.txt"
        # A fake landscape framebuffer like the PB628's (FB_W x FB_H, 8 bpp,
        # stride padded with 0xEE), for `fbshot`.
        self.fb = workdir / "fb0"
        self.fb_sys = workdir / "fb0-sys"
        self.fb_sys.mkdir(exist_ok=True)
        (self.fb_sys / "virtual_size").write_text(f"{FB_W},{FB_H}\n")
        (self.fb_sys / "bits_per_pixel").write_text("8\n")
        (self.fb_sys / "stride").write_text(f"{FB_STRIDE}\n")
        self.fb.write_bytes(b"".join(
            bytes(fb_pixel(x, y) for x in range(FB_W)) + b"\xee" * (FB_STRIDE - FB_W)
            for y in range(FB_H)))
        env = dict(os.environ, PBAGENT_TOKEN=token, PBAGENT_PORT=str(self.port),
                   PBTK_STUB_INPUT=str(self.input_file),
                   PBAGENT_FB=str(self.fb), PBAGENT_FB_SYS=str(self.fb_sys), **(env or {}))
        self.proc = subprocess.Popen([str(self.exe)], env=env, cwd=self.dir,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("demo agent did not start")

    def user_input(self, text: str) -> None:
        """Simulate the person touching the reader (bypasses the agent)."""
        tmp = self.input_file.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, self.input_file)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def log(self) -> Path:
        return self.dir / "pbtk-demo.log"

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
