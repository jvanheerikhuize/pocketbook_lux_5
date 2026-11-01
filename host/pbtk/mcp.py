"""MCP server on stdio, so Claude Code can see and drive the reader as tools.

Register it in a consuming repo's .mcp.json:

    {"mcpServers": {"pocketbook": {"command": "pbtk", "args": ["mcp"]}}}

Everything goes through Device(source="mcp"), so each call shows on the GUI
timeline, and the GUI's hold switch blocks input while the user is driving.
Stdlib only: newline-delimited JSON-RPC 2.0, protocol "2025-06-18".
"""

from __future__ import annotations

import base64
import json
import sys
import traceback

from . import PbtkError, __version__, activity, probe, project
from .device import Device

PROTOCOL = "2025-06-18"

_XY = {"x": {"type": "integer", "description": "screen x in pixels (0 = left)"},
       "y": {"type": "integer", "description": "screen y in pixels (0 = top)"}}
_SHOT = {"screenshot": {"type": "boolean", "default": True,
                        "description": "return a screenshot taken after the action"}}


def _tool(name, description, props=None, required=None):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": required or []}}


TOOLS = [
    _tool("pb_status", "What is reachable: USB storage (deploy/logs) and the in-app agent (screen/input). "
          "Also says whether the user has taken control in the GUI."),
    _tool("pb_screenshot", "Screenshot of the app running on the PocketBook (8-bit gray PNG, canvas coordinates "
          "match pb_tap)."),
    _tool("pb_info", "Running app, screen size, orientation, firmware, agent version."),
    _tool("pb_tap", "Tap at a screen point in the running app.", {**_XY, **_SHOT}, ["x", "y"]),
    _tool("pb_long_press", "Long-press at a screen point.", {**_XY, **_SHOT}, ["x", "y"]),
    _tool("pb_swipe", "Swipe from (x1,y1) to (x2,y2).",
          {"x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"},
           "y2": {"type": "integer"}, "ms": {"type": "integer", "default": 300}, **_SHOT},
          ["x1", "y1", "x2", "y2"]),
    _tool("pb_key", "Press a hardware/navigation key: prev, next, home, back, menu, ok, up, down, left, right.",
          {"name": {"type": "string"}, **_SHOT}, ["name"]),
    _tool("pb_refresh", "Ask the app for a full e-ink refresh (clears ghosting)."),
    _tool("pb_exit_app", "Close the running app (the reader returns to its library)."),
    _tool("pb_logs", "Tail of the app log: live from the agent when an app runs, else from USB storage.",
          {"lines": {"type": "integer", "default": 80},
           "log": {"type": "string", "description": "device path of the log; default from pbtk.toml"}}),
    _tool("pb_user_events", "Input the user gave directly on the reader since `since` (taps, keys).",
          {"since": {"type": "integer", "default": 0}}),
    _tool("pb_ls", "List a folder on the reader (/mnt/ext1/...).",
          {"path": {"type": "string", "default": "/mnt/ext1/applications"}}),
    _tool("pb_read_file", "Read a text file from the reader (first 200 kB).",
          {"path": {"type": "string"}}, ["path"]),
    _tool("pb_deploy", "Copy a project's built bundle (pbtk.toml) to the reader over USB; optionally build "
          "first and eject so the user can open the app.",
          {"project": {"type": "string", "description": "dir containing pbtk.toml"},
           "build": {"type": "boolean", "default": False}, "eject": {"type": "boolean", "default": False}},
          ["project"]),
    _tool("pb_eject", "Unmount the reader so it leaves PC-link mode and apps can run."),
    _tool("pb_probe_collect", "Read the probe app's device facts from USB storage (after the user ran it)."),
    _tool("pb_activity", "Recent entries of the shared timeline (Claude, the user in the GUI, and the device).",
          {"n": {"type": "integer", "default": 30}}),
]


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def _image(png: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"}


class Server:
    def __init__(self, agent_addr: str | None = None):
        self.dev = Device(source="mcp", agent_addr=agent_addr, discover_timeout=3.0)

    def _act(self, result, want_shot: bool, label: str) -> list[dict]:
        fields, img, path = result
        out = [_text(f"{label}: ok {json.dumps(fields) if fields else ''}".strip())]
        if want_shot and img is not None:
            out.append(_text(f"screenshot {img.width}x{img.height} ({path})"))
            out.append(_image(img.png()))
        return out

    def call(self, name: str, a: dict) -> list[dict]:
        d = self.dev
        shot = a.get("screenshot", True)
        if name == "pb_status":
            return [_text(json.dumps(d.status(), indent=2))]
        if name == "pb_screenshot":
            img, path = d.screenshot()
            return [_text(f"{img.width}x{img.height} ({path})"), _image(img.png())]
        if name == "pb_info":
            return [_text(json.dumps(d.agent.info(), indent=2))]
        if name == "pb_tap":
            return self._act(d.tap(a["x"], a["y"], shot_after=shot), shot, f"tap {a['x']},{a['y']}")
        if name == "pb_long_press":
            return self._act(d.long_press(a["x"], a["y"], shot_after=shot), shot, "long press")
        if name == "pb_swipe":
            return self._act(d.swipe(a["x1"], a["y1"], a["x2"], a["y2"], a.get("ms", 300), shot_after=shot),
                             shot, "swipe")
        if name == "pb_key":
            return self._act(d.key(a["name"], shot_after=shot), shot, f"key {a['name']}")
        if name == "pb_refresh":
            d.refresh()
            return [_text("full refresh requested")]
        if name == "pb_exit_app":
            d.exit_app()
            return [_text("app asked to close")]
        if name == "pb_logs":
            log = a.get("log")
            if not log:
                try:
                    log = project.load().device_log
                except PbtkError:
                    pass
            text, where = d.logs(a.get("lines", 80), log)
            return [_text(f"# {where}\n{text}")]
        if name == "pb_user_events":
            events, nxt = d.agent.events(a.get("since", 0))
            return [_text(json.dumps({"events": events, "next": nxt}, indent=1))]
        if name == "pb_ls":
            entries, via = d.ls(a.get("path", "/mnt/ext1/applications"))
            lines = [f"{'d' if e['dir'] else '-'} {e['size']:>10}  {e['name']}" for e in entries]
            return [_text(f"# via {via}\n" + "\n".join(lines))]
        if name == "pb_read_file":
            data = d.read_file(a["path"])[:200_000]
            return [_text(data.decode("utf-8", "replace"))]
        if name == "pb_deploy":
            proj = project.load(a["project"])
            changed = d.deploy(proj, build=a.get("build", False), eject=a.get("eject", False))
            msg = f"deployed {proj.name}: {len(changed)} file(s) changed"
            if a.get("eject"):
                msg += "; ejected — ask the user to open the app on the reader"
            return [_text(msg + "\n" + "\n".join(changed[:50]))]
        if name == "pb_eject":
            return [_text(f"ejected {d.eject()}")]
        if name == "pb_probe_collect":
            result = probe.collect(d.usb)
            return [_text(probe.summary(result))]
        if name == "pb_activity":
            entries, _ = activity.read(activity.tail_offset(a.get("n", 30)), limit=a.get("n", 30))
            return [_text("\n".join(json.dumps(e) for e in entries))]
        raise PbtkError(f"unknown tool {name}")

    def handle(self, msg: dict) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                client_proto = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
                result = {
                    "protocolVersion": client_proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "pbtk", "version": __version__},
                    "instructions": (
                        "Tools for a PocketBook e-reader. Screen and input work only while an app built "
                        "with libpbagent is open on the reader (Wi-Fi on, USB storage not mounted). "
                        "Deploy/logs/probe use USB storage. Coordinates are canvas pixels as in "
                        "pb_screenshot. If a tool says the user has taken control, stop and wait."
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                p = msg.get("params") or {}
                try:
                    content = self.call(p.get("name", ""), p.get("arguments") or {})
                    result = {"content": content, "isError": False}
                except PbtkError as e:
                    result = {"content": [_text(str(e))], "isError": True}
                except (KeyError, TypeError, ValueError) as e:
                    result = {"content": [_text(f"bad arguments: {e}")], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": f"method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:  # noqa: BLE001 — never kill the server
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def serve(agent_addr: str | None = None, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server(agent_addr)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
