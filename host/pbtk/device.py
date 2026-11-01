"""One facade over all transports. CLI, MCP server and GUI all go through it,
so every action lands on the same activity timeline."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import PbtkError, activity, agent as agent_mod, paths
from .image import Gray
from .project import Project
from .usb import UsbStorage, find_mount


class Device:
    def __init__(self, source: str = "cli", agent_addr: str | None = None,
                 discover_timeout: float = 4.0):
        self.source = source
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
    def screenshot(self, record: bool = True, out: Path | None = None) -> tuple[Gray, Path]:
        img = self.agent.shot()
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.png"
        path = Path(out) if out else paths.shots_dir() / name
        path.write_bytes(img.png())
        if record:
            activity.record(self.source, "screenshot", {}, shot=path.name if not out else None,
                            result=f"{img.width}x{img.height}")
        return img, path

    # -------------------------------------------------------------- input
    def tap(self, x: int, y: int, shot_after: bool = False, settle_ms: int = 400):
        return self._act("tap", {"x": x, "y": y}, lambda: self.agent.tap(x, y), shot_after, settle_ms)

    def long_press(self, x: int, y: int, shot_after: bool = False, settle_ms: int = 400):
        return self._act("long", {"x": x, "y": y}, lambda: self.agent.long_press(x, y),
                         shot_after, settle_ms)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300, shot_after: bool = False,
              settle_ms: int = 400):
        return self._act("swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms},
                         lambda: self.agent.swipe(x1, y1, x2, y2, ms), shot_after, settle_ms)

    def key(self, name: str, shot_after: bool = False, settle_ms: int = 400):
        name = name.lower()
        if name not in agent_mod.KEYS and not name.startswith("0x"):
            raise PbtkError(f"unknown key {name!r}; one of {', '.join(agent_mod.KEYS)}")
        return self._act("key", {"name": name}, lambda: self.agent.key(name), shot_after, settle_ms)

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

    def read_file(self, path: str) -> bytes:
        u = self.usb_or_none()
        if u:
            return u.read(path)
        return self.agent.get(path)

    # ------------------------------------------------------------- deploy
    def deploy(self, project: Project, build: bool = False, eject: bool = False) -> list[str]:
        def run():
            if build:
                if not project.build:
                    raise PbtkError("pbtk.toml has no [app] build command")
                r = subprocess.run(project.build, shell=True, cwd=project.root,
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    raise PbtkError(f"build failed:\n{(r.stdout + r.stderr)[-3000:]}")
            u = self.usb
            u.install_token(paths.token())
            changed = u.deploy(project.dist)
            if eject:
                u.eject()
            return changed

        changed, _, _ = self._act("deploy", {"app": project.name, "build": build, "eject": eject},
                                  run, False, 0, guard=False)
        return changed

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
        return " ".join(f"{k}={val}" for k, val in v.items()) or None
    s = str(v)
    return s if len(s) < 300 else s[:300] + "…"
