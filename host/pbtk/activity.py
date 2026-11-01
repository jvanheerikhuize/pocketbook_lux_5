"""The shared activity timeline.

Every action taken through pbtk (CLI, MCP server i.e. Claude, GUI i.e. you) and
every input observed on the reader is appended to one JSON-lines file. The GUI
tails it, which is how you watch what Claude does and Claude can see what you did.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import paths

_lock = threading.Lock()

SOURCES = ("cli", "mcp", "gui", "device", "script")


def log_path() -> Path:
    return paths.state_dir() / "activity.jsonl"


def hold_path() -> Path:
    return paths.state_dir() / "hold"


def record(source: str, action: str, args: dict | None = None, ok: bool = True,
           result: str | None = None, shot: str | None = None) -> dict:
    entry = {
        "ts": round(time.time(), 3),
        "source": source,
        "action": action,
        "args": args or {},
        "ok": ok,
    }
    if result:
        entry["result"] = result[:2000]
    if shot:
        entry["shot"] = shot
    line = json.dumps(entry, separators=(",", ":"))
    with _lock:
        with open(log_path(), "a") as f:
            f.write(line + "\n")
    return entry


def read(since: int = 0, limit: int = 500) -> tuple[list[dict], int]:
    """Entries after byte offset `since`; returns (entries, next offset)."""
    p = log_path()
    if not p.exists():
        return [], 0
    size = p.stat().st_size
    if since > size:
        since = 0
    with open(p, "rb") as f:
        f.seek(since)
        data = f.read()
    # Only hand out complete lines.
    end = data.rfind(b"\n") + 1
    out = []
    for line in data[:end].splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[-limit:], since + end


def tail_offset(entries: int = 200) -> int:
    """Byte offset that leaves roughly the last `entries` lines."""
    p = log_path()
    if not p.exists():
        return 0
    size = p.stat().st_size
    with open(p, "rb") as f:
        f.seek(max(size - entries * 400, 0))
        chunk = f.read()
    if size <= entries * 400:
        return 0
    nl = chunk.find(b"\n")
    return size - len(chunk) + nl + 1


# --- hold: the GUI's "I'm driving" switch. While set, only the GUI may act.
def set_hold(on: bool) -> None:
    if on:
        hold_path().write_text(str(time.time()))
    else:
        try:
            os.unlink(hold_path())
        except FileNotFoundError:
            pass


def held() -> bool:
    return hold_path().exists()
