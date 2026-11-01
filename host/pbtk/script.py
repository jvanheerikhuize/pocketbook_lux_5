"""Replayable input scripts, run over the agent.

One command per line; `#` starts a comment:

    tap X Y            long X Y            swipe X1 Y1 X2 Y2 [MS]
    key NAME           wait MS             shot [NAME]
    expect-log TEXT    (waits up to 10 s for TEXT to appear in the app log)

The GUI can save the current session as such a script (`/api/script`), so a
manual session becomes a regression test.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from . import PbtkError
from .device import Device


def to_script(entries: list[dict]) -> str:
    """Turn activity entries into a script, with waits from their timing."""
    out = ["# recorded by pbtk"]
    last_ts = None
    for e in entries:
        if not e.get("ok", True):
            continue
        a, args = e.get("action"), e.get("args", {})
        line = None
        if a == "tap":
            line = f"tap {args['x']} {args['y']}"
        elif a == "long":
            line = f"long {args['x']} {args['y']}"
        elif a == "swipe":
            line = f"swipe {args['x1']} {args['y1']} {args['x2']} {args['y2']} {args.get('ms', 300)}"
        elif a == "key":
            line = f"key {args['name']}"
        elif a == "screenshot":
            line = "shot"
        if line is None:
            continue
        if last_ts is not None:
            gap = int((e["ts"] - last_ts) * 1000)
            if gap > 150:
                out.append(f"wait {min(gap, 30000)}")
        out.append(line)
        last_ts = e["ts"]
    return "\n".join(out) + "\n"


def run(dev: Device, text: str, out_dir: Path | None = None, echo=print) -> list[Path]:
    shots: list[Path] = []
    # expect-log only looks at log lines written after the script started.
    try:
        _, log_offset = dev.agent.log(-1)
    except PbtkError:
        log_offset = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = shlex.split(line)
        cmd, args = parts[0], parts[1:]
        echo(f"{n:>3}: {line}")
        try:
            if cmd == "tap":
                dev.tap(int(args[0]), int(args[1]))
            elif cmd == "long":
                dev.long_press(int(args[0]), int(args[1]))
            elif cmd == "swipe":
                dev.swipe(*(int(a) for a in args[:4]), ms=int(args[4]) if len(args) > 4 else 300)
            elif cmd == "key":
                dev.key(args[0])
            elif cmd == "wait":
                time.sleep(int(args[0]) / 1000)
            elif cmd == "shot":
                name = (args[0] if args else f"shot-{n}") + ".png"
                target = (out_dir / name) if out_dir else None
                if target:
                    out_dir.mkdir(parents=True, exist_ok=True)
                _, path = dev.screenshot(out=target)
                shots.append(path)
                echo(f"     saved {path}")
            elif cmd == "expect-log":
                needle = " ".join(args)
                deadline = time.monotonic() + 10
                while True:
                    data, _ = dev.agent.log(log_offset)
                    if needle.encode() in data:
                        break
                    if time.monotonic() > deadline:
                        raise PbtkError(f"line {n}: log never showed {needle!r}")
                    time.sleep(0.3)
            else:
                raise PbtkError(f"line {n}: unknown command {cmd!r}")
        except (IndexError, ValueError) as e:
            raise PbtkError(f"line {n}: bad arguments for {cmd}: {e}") from e
    return shots
