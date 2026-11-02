"""One facade over all transports. CLI, MCP server and GUI all go through it,
so every action lands on the same activity timeline."""

from __future__ import annotations

import os
import subprocess
import time
import zlib
from pathlib import Path

from . import PbtkError, activity, agent as agent_mod, paths, project as project_mod
from .image import Gray
from .project import Project
from .usb import UsbStorage, find_mount


class Device:
    #: where bundles live on the reader (a test points this at a temp dir)
    apps_root = "/mnt/ext1/applications"

    def __init__(self, source: str = "cli", agent_addr: str | None = None,
                 discover_timeout: float = 4.0, view: str | None = None):
        self.source = source
        self._view = view
        self._canvas: tuple[int, int] | None = None
        self._agent_addr = agent_addr
        self._discover_timeout = discover_timeout
        self._agent: agent_mod.AgentClient | None = None

    # ------------------------------------------------------- transports
    @property
    def usb(self) -> UsbStorage:
        return UsbStorage.connect()

    def usb_or_none(self) -> UsbStorage | None:
        m = find_mount()
        return UsbStorage(m) if m else None

    @property
    def agent(self) -> agent_mod.AgentClient:
        if self._agent is None:
            host, port = agent_mod.resolve_address(self._agent_addr, self._discover_timeout)
            self._agent = agent_mod.AgentClient(host, port)
        return self._agent

    def agent_or_none(self) -> agent_mod.AgentClient | None:
        try:
            a = self.agent
            a.ping()
            return a
        except PbtkError:
            self._agent = None
            return None

    def close(self) -> None:
        if self._agent:
            self._agent.close()

    # --------------------------------------------------------------- view
    @property
    def view(self) -> str:
        """How app screenshots are turned: none, cw or ccw. A landscape app on the
        portrait panel (mmIV) reads upright with one; taps, long presses and
        swipes take coordinates in the same turned picture. From the argument,
        $PBTK_VIEW, or `view` in the nearest pbtk.toml."""
        if self._view is None:
            v = os.environ.get("PBTK_VIEW")
            if not v:
                try:
                    v = project_mod.load().view
                except PbtkError:
                    v = "none"
            self._view = project_mod._view(v, "PBTK_VIEW")
        return self._view

    def _canvas_size(self) -> tuple[int, int]:
        if self._canvas is None:
            info = self.agent.info()
            self._canvas = (int(info["width"]), int(info["height"]))
        return self._canvas

    def to_canvas(self, x: int, y: int) -> tuple[int, int]:
        """A point in the (turned) screenshot → the app canvas the agent taps."""
        if self.view == "none":
            return x, y
        w, h = self._canvas_size()
        if self.view == "ccw":
            return w - 1 - y, x
        return y, h - 1 - x  # cw

    # ------------------------------------------------------------ helpers
    def _guard(self) -> None:
        if self.source != "gui" and activity.held():
            raise PbtkError("The user has taken control in the pbtk GUI (hold is on); wait or ask them.")

    def _act(self, action: str, args: dict, fn, shot_after: bool, settle_ms: int,
             guard: bool = True):
        if guard:
            self._guard()
        try:
            result = fn()
        except PbtkError as e:
            activity.record(self.source, action, args, ok=False, result=str(e))
            raise
        shot_path = None
        img = None
        if shot_after:
            time.sleep(settle_ms / 1000)
            try:
                img, shot_path = self.screenshot(record=False)
            except PbtkError:
                pass
        activity.record(self.source, action, args, result=_short(result),
                        shot=shot_path.name if shot_path else None)
        return result, img, shot_path

    # ------------------------------------------------------------- screen
    def screenshot(self, record: bool = True, out: Path | None = None, whole: bool = False,
                   rotate: str | None = None) -> tuple[Gray, Path]:
        """The app canvas, or with whole=True the panel itself (/dev/fb0)."""
        img = self.agent.fbshot(rotate or "auto") if whole else self.agent.shot()
        if not whole or rotate is None:
            img = img.turned(self.view)
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.png"
        path = Path(out) if out else paths.shots_dir() / name
        path.write_bytes(img.png())
        if record:
            activity.record(self.source, "screenshot", {"whole": True} if whole else {},
                            shot=path.name if not out else None,
                            result=f"{img.width}x{img.height}")
        return img, path

    # -------------------------------------------------------------- input
    def tap(self, x: int, y: int, shot_after: bool = False, settle_ms: int = 400):
        return self._act("tap", {"x": x, "y": y}, lambda: self.agent.tap(*self.to_canvas(x, y)),
                         shot_after, settle_ms)

    def long_press(self, x: int, y: int, shot_after: bool = False, settle_ms: int = 400):
        return self._act("long", {"x": x, "y": y}, lambda: self.agent.long_press(*self.to_canvas(x, y)),
                         shot_after, settle_ms)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300, shot_after: bool = False,
              settle_ms: int = 400):
        return self._act("swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms},
                         lambda: self.agent.swipe(*self.to_canvas(x1, y1), *self.to_canvas(x2, y2), ms),
                         shot_after, settle_ms)

    def key(self, name: str, shot_after: bool = False, settle_ms: int = 400, hold_ms: int = 80):
        name = name.lower()
        if name not in agent_mod.KEYS and not name.startswith("0x"):
            raise PbtkError(f"unknown key {name!r}; one of {', '.join(agent_mod.KEYS)}")
        args = {"name": name} if hold_ms == 80 else {"name": name, "hold_ms": hold_ms}
        return self._act("key", args, lambda: self.agent.key(name, hold_ms), shot_after, settle_ms)

    def write_text(self, path: str, text: str):
        """A small text file on the reader (app settings for a scenario)."""
        return self._act("write", {"path": path, "text": text.strip()[:80]},
                         lambda: self.agent.put(path, text.encode()), False, 0)

    def refresh(self):
        return self._act("refresh", {}, self.agent.refresh, False, 0)

    def exit_app(self):
        return self._act("exit-app", {}, self.agent.exit_app, False, 0)

    # --------------------------------------------------------------- logs
    def logs(self, lines: int = 50, device_log: str | None = None) -> tuple[str, str]:
        """(text, where): live from the agent when an app runs, else from USB storage."""
        a = self.agent_or_none()
        if a:
            data, _ = a.log(-64 * 1024)
            text = "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])
            return text, f"agent {a.address}"
        u = self.usb_or_none()
        if u and device_log:
            return u.tail(device_log, lines), f"usb {device_log}"
        if u:
            raise PbtkError("which log? pass --log /mnt/ext1/applications/<app>/<app>.log or run "
                            "inside a project with pbtk.toml")
        raise PbtkError("neither an agent nor USB storage is reachable")

    # -------------------------------------------------------------- files
    def ls(self, path: str = "/mnt/ext1/applications") -> tuple[list[dict], str]:
        u = self.usb_or_none()
        if u:
            return u.ls(path), "usb"
        a = self.agent_or_none()
        if a:
            return a.ls(path), "agent"
        raise PbtkError("neither USB storage nor an agent is reachable")

    def push(self, local: Path, device_path: str) -> tuple[list[str], str]:
        """Copy a file or folder to the reader: USB storage when mounted, else the
        agent (files written as they are). Returns (device paths written, via)."""
        local = Path(local)
        if not local.exists():
            raise PbtkError(f"{local} does not exist")

        def run():
            u = self.usb_or_none()
            if u:
                changed = u.push(local, device_path)
                u.sync()
                return changed, "usb"
            a = self.agent
            if local.is_file():
                a.put(device_path, local.read_bytes())
                return [device_path], "agent"
            written = []
            for f in sorted(p for p in local.rglob("*") if p.is_file()):
                dst = device_path.rstrip("/") + "/" + f.relative_to(local).as_posix()
                a.put(dst, f.read_bytes())
                written.append(dst)
            return written, "agent"

        result, _, _ = self._act("push", {"to": device_path}, run, False, 0)
        return result

    def calibrate(self, on: bool = True) -> None:
        self._act("calibrate", {"on": on}, lambda: self.agent.calibrate(on), False, 0)

    def read_file(self, path: str) -> bytes:
        u = self.usb_or_none()
        if u:
            return u.read(path)
        return self.agent.get(path)

    # ------------------------------------------------------------- deploy
    def deploy(self, project: Project, build: bool = False, eject: bool = False) -> list[str]:
        def run():
            if build:
                self._build(project)
            u = self.usb
            u.install_token(paths.token())
            changed = u.deploy(project.dist, project.kept)
            if eject:
                u.eject()
            return changed

        changed, _, _ = self._act("deploy", {"app": project.name, "build": build, "eject": eject},
                                  run, False, 0, guard=False)
        return changed

    def deploy_wifi(self, project: Project, build: bool = False, restart: bool = False) -> dict:
        """Copy changed files of the bundle through the running app's agent, and
        optionally restart that app in place. No USB, no taps on the reader."""
        def run():
            if build:
                self._build(project)
            if not project.dist.is_dir():
                raise PbtkError(f"nothing to deploy: {project.dist} does not exist (build first)")
            a = self.agent
            info = a.info()
            if restart and info.get("app") != project.name:
                raise PbtkError(f"the running app is {info.get('app')!r}, not {project.name!r}: "
                                "restart only replaces the app that is running")
            if restart and "started" not in info:
                raise PbtkError("the running app's agent is too old to restart itself: deploy without "
                                "--restart, then reopen the app once on the reader")
            has_sum = "started" in info  # sum and restart arrived together
            changed, unchanged, kept = [], 0, 0
            for f in sorted(p for p in project.dist.rglob("*") if p.is_file()):
                rel = f.relative_to(project.dist).as_posix()
                dev_path = f"{self.apps_root}/{rel}"
                data = f.read_bytes()
                want = (zlib.crc32(data), len(data))
                have = a.sum(dev_path) if has_sum else None
                if have == want:
                    unchanged += 1
                    continue
                # keep: never overwrite (e.g. saves); without `sum` we cannot tell, so skip.
                if project.kept(rel) and (have is not None or not has_sum):
                    kept += 1
                    continue
                a.put(dev_path, data)
                if has_sum and a.sum(dev_path) != want:
                    raise PbtkError(f"verification failed after writing {dev_path}")
                changed.append(dev_path)
            result = {"changed": changed, "unchanged": unchanged, "kept": kept, "restarted": False}
            if restart:
                a.restart()
                back = a.wait_restarted(info.get("started", ""))
                result.update(restarted=True, pid_kept=back.get("pid") == info.get("pid"),
                              build=back.get("build"))
            return result

        result, _, _ = self._act("deploy", {"app": project.name, "via": "wifi", "build": build,
                                            "restart": restart}, run, False, 0)
        return result

    def restart_app(self) -> dict:
        def run():
            a = self.agent
            before = a.info()
            a.restart()
            back = a.wait_restarted(before.get("started", ""))
            return {"pid_kept": back.get("pid") == before.get("pid"), "build": back.get("build")}

        result, _, _ = self._act("restart", {}, run, False, 0)
        return result

    def _build(self, project: Project) -> None:
        if not project.build:
            raise PbtkError("pbtk.toml has no [app] build command")
        r = subprocess.run(project.build, shell=True, cwd=project.root, capture_output=True, text=True)
        if r.returncode != 0:
            raise PbtkError(f"build failed:\n{(r.stdout + r.stderr)[-3000:]}")

    def eject(self) -> str:
        dev, _, _ = self._act("eject", {}, lambda: self.usb.eject(), False, 0, guard=False)
        return dev

    # ------------------------------------------------------------- status
    def status(self) -> dict:
        out: dict = {"usb": None, "agent": None, "hold": activity.held()}
        u = self.usb_or_none()
        if u:
            out["usb"] = {"mount": str(u.mount), "block": u.block_device()}
        a = self.agent_or_none()
        if a:
            try:
                out["agent"] = {"address": a.address, **a.info()}
            except PbtkError as e:
                out["agent"] = {"address": a.address, "error": str(e)}
        facts = paths.state_dir() / "facts.json"
        if facts.exists():
            out["facts_file"] = str(facts)
        return out


def _short(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return f"{len(v)} item(s)"
    if isinstance(v, dict):
        return " ".join(f"{k}={len(val) if isinstance(val, list) else val}"
                        for k, val in v.items()) or None
    s = str(v)
    return s if len(s) < 300 else s[:300] + "…"
