"""T1 transport: a TCP client for libpbagent (see docs/protocol.md), plus discovery."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass

from . import PbtkError, paths
from .image import Gray, parse_pgm

DEFAULT_PORT = 8628
KEYS = ["prev", "next", "home", "back", "menu", "ok", "up", "down", "left", "right", "prev2", "next2"]

# InkView event numbers (inkview.h), for decoding `events`.
EVENT_NAMES = {21: "init", 22: "exit", 23: "show", 25: "keypress", 26: "keyrelease",
               29: "pointerup", 30: "pointerdown", 31: "pointermove", 34: "pointerlong"}
KEY_NAMES = {0x0A: "ok", 0x11: "up", 0x12: "down", 0x13: "left", 0x14: "right", 0x17: "menu",
             0x18: "prev", 0x19: "next", 0x1A: "home", 0x1B: "back", 0x1C: "prev2", 0x1D: "next2"}


@dataclass
class Beacon:
    host: str
    port: int
    app: str


def discover(timeout: float = 4.0, port: int = DEFAULT_PORT) -> list[Beacon]:
    """Listen for agent beacons (UDP broadcast every 3 s)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        s.bind(("", port))
    except OSError as e:
        s.close()
        raise PbtkError(f"cannot listen for beacons on UDP {port}: {e}") from e
    found: dict[tuple[str, int], Beacon] = {}
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            s.settimeout(max(deadline - time.monotonic(), 0.05))
            try:
                data, (host, _) = s.recvfrom(512)
            except socket.timeout:
                break
            parts = data.decode("utf-8", "replace").split()
            if len(parts) < 2 or parts[0] != "pbagent":
                continue
            kv = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
            b = Beacon(host, int(kv.get("port", port)), kv.get("app", "?"))
            found[(b.host, b.port)] = b
    finally:
        s.close()
    return list(found.values())


def _cache_path():
    return paths.state_dir() / "agent.json"


def remember(host: str, port: int) -> None:
    _cache_path().write_text(json.dumps({"host": host, "port": port}))


def resolve_address(addr: str | None = None, discover_timeout: float = 4.0) -> tuple[str, int]:
    """--agent / $PBTK_AGENT / last known / discovery, in that order."""
    addr = addr or os.environ.get("PBTK_AGENT")
    if addr:
        host, _, port = addr.partition(":")
        return host, int(port or DEFAULT_PORT)
    try:
        cached = json.loads(_cache_path().read_text())
        host, port = cached["host"], int(cached["port"])
        if _reachable(host, port):
            return host, port
    except (OSError, ValueError, KeyError):
        pass
    if discover_timeout > 0:
        beacons = discover(discover_timeout)
        if beacons:
            b = beacons[0]
            remember(b.host, b.port)
            return b.host, b.port
    raise PbtkError(
        "No agent found. Open an app built with libpbagent on the reader, with Wi-Fi on and USB "
        "storage not mounted (apps cannot run while the reader is in PC-link mode). "
        "Or pass --agent HOST[:PORT]."
    )


def _reachable(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class AgentClient:
    """One authenticated connection. Thread-safe: requests are serialised."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, token: str | None = None,
                 timeout: float = 10.0):
        self.host, self.port = host, port
        self.token = token or paths.token()
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    # ------------------------------------------------------------ wire
    def _connect(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise PbtkError(f"agent at {self.address} unreachable: {e}") from e
        self._buf = b""
        self._send(f"auth {self.token}\n".encode())
        head = self._line()
        if not head.startswith("ok"):
            self.close()
            raise PbtkError(
                f"agent at {self.address} refused the token ({head}). "
                "Run `pbtk token install` with the reader on USB."
            )

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _send(self, data: bytes) -> None:
        assert self._sock
        self._sock.sendall(data)

    def _fill(self) -> None:
        assert self._sock
        chunk = self._sock.recv(65536)
        if not chunk:
            raise ConnectionError("agent closed the connection")
        self._buf += chunk

    def _line(self) -> str:
        while b"\n" not in self._buf:
            self._fill()
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("utf-8", "replace")

    def _exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def request(self, line: str, payload: bytes | None = None) -> tuple[dict, bytes]:
        """Send one command; returns (header fields, payload)."""
        if "\n" in line:
            raise ValueError("command must be one line")
        with self._lock:
            for attempt in (0, 1):
                try:
                    if not self._sock:
                        self._connect()
                    self._send(line.encode() + b"\n" + (payload or b""))
                    head = self._line()
                    break
                except (OSError, ConnectionError) as e:
                    self.close()
                    if attempt == 1:
                        raise PbtkError(f"agent at {self.address}: {e}") from e
            if head.startswith("err"):
                raise PbtkError(f"agent: {head[4:]}")
            parts = head.split()
            if not parts or parts[0] != "ok":
                self.close()
                raise PbtkError(f"agent: bad reply {head!r}")
            n = int(parts[1]) if len(parts) > 1 else 0
            fields = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
            return fields, self._exact(n)

    # -------------------------------------------------------- commands
    def ping(self) -> None:
        self.request("ping")

    def info(self) -> dict:
        _, body = self.request("info")
        return dict(l.split("=", 1) for l in body.decode().splitlines() if "=" in l)

    def shot(self) -> Gray:
        _, body = self.request("shot")
        return parse_pgm(body)

    def tap(self, x: int, y: int, hold_ms: int = 60) -> dict:
        return self.request(f"tap {int(x)} {int(y)} {int(hold_ms)}")[0]

    def long_press(self, x: int, y: int) -> dict:
        return self.request(f"long {int(x)} {int(y)}")[0]

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> dict:
        return self.request(f"swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")[0]

    def key(self, name: str, hold_ms: int = 80) -> dict:
        if " " in name:
            raise PbtkError("bad key name")
        return self.request(f"key {name} {int(hold_ms)}")[0]

    def event(self, type_: int, p1: int = 0, p2: int = 0) -> dict:
        return self.request(f"event {int(type_)} {int(p1)} {int(p2)}")[0]

    def refresh(self) -> None:
        self.request("refresh")

    def exit_app(self) -> None:
        self.request("exit")
        self.close()

    def events(self, since: int = 0) -> tuple[list[dict], int]:
        fields, body = self.request(f"events {int(since)}")
        now = int(fields.get("now", 0))
        out = []
        for l in body.decode().splitlines():
            seq, ms, t, p1, p2 = (int(v) for v in l.split())
            e = {"seq": seq, "ago_ms": now - ms, "type": EVENT_NAMES.get(t, str(t)), "p1": p1, "p2": p2}
            if t in (25, 26):
                e["key"] = KEY_NAMES.get(p1, hex(p1))
            out.append(e)
        return out, int(fields.get("next", since))

    def log(self, offset: int = 0, max_bytes: int = 0) -> tuple[bytes, int]:
        """Bytes of the app log from offset (negative = from the end); returns (data, next offset)."""
        fields, body = self.request(f"log {int(offset)} {int(max_bytes)}")
        start = int(fields.get("offset", max(offset, 0)))
        return body, start + len(body)

    def ls(self, path: str) -> list[dict]:
        _check_path(path)
        _, body = self.request(f"ls {path}")
        out = []
        for l in body.decode("utf-8", "replace").splitlines():
            kind, size, mtime, name = l.split(" ", 3)
            out.append({"name": name, "dir": kind == "d", "size": int(size), "mtime": int(mtime)})
        return sorted(out, key=lambda e: (not e["dir"], e["name"].lower()))

    def get(self, path: str) -> bytes:
        _check_path(path)
        return self.request(f"get {path}")[1]

    def put(self, path: str, data: bytes) -> None:
        _check_path(path)
        self.request(f"put {path} {len(data)}", data)


def _check_path(path: str) -> None:
    if " " in path or "\n" in path:
        raise PbtkError("agent paths cannot contain spaces")
