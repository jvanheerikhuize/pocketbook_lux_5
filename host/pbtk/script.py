"""Replayable input scripts and scenarios, run over the agent.

One command per line; `#` starts a comment:

    tap X Y            long X Y            swipe X1 Y1 X2 Y2 [MS]
    key NAME [HOLD_MS] wait MS             shot [NAME]
    expect-log TEXT    (waits up to 10 s for TEXT to appear in the app log)
    expect-change [MS] (waits up to MS, default 4000, for the panel to change
                        after the last input; the time it took is reported)
    expect-screen REF [MAX_PCT] [X Y W H]
                       (the screen, or that box of it, matches REF: a `shot`
                        name from this run or a PNG file; at most MAX_PCT,
                        default 2, of the pixels differ)
    expect-differs REF [MIN_PCT] [X Y W H]
                       (at least MIN_PCT, default 10, of the pixels differ)
    note TEXT          (a line on the shared timeline, e.g. the scenario step)
    write PATH TEXT    (write a small text file on the reader, e.g. app settings)
    restart            (restart the app in place and wait for its agent)

With stop_on_user, the run stops before the next input as soon as the player
touches the reader or presses a key (needs an app that calls pbagent_observe).
run() returns the screenshots; pass `report` (a list) to get one entry per
command with its result and, for expect-change, the latency.

The GUI can save the current session as such a script (`/api/script`), so a
manual session becomes a regression test.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from . import PbtkError, activity
from .device import Device
from .image import diff_pct, read_png_gray


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
            line = f"key {args['name']}" + (f" {args['hold_ms']}" if args.get("hold_ms") else "")
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


INPUTS = ("tap", "long", "swipe", "key")


def _user_input(dev: Device, since: int) -> tuple[list[dict], int]:
    events, nxt = dev.agent.events(since)
    return [e for e in events if e.get("type") in ("pointerdown", "keypress")], nxt


def run(dev: Device, text: str, out_dir: Path | None = None, echo=print,
        stop_on_user: bool = False, report: list | None = None) -> list[Path]:
    shots: list[Path] = []
    steps = report if report is not None else []
    lines = text.splitlines()
    watch_panel = any(l.split("#", 1)[0].strip().startswith("expect-change") for l in lines)
    # expect-log only looks at log lines written after the script started.
    try:
        _, log_offset = dev.agent.log(-1)
    except PbtkError:
        log_offset = 0
    since = _user_input(dev, 0)[1] if stop_on_user else 0
    before = None  # the panel before the last input (for expect-change)
    sent = 0.0
    for n, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = shlex.split(line)
        cmd, args = parts[0], parts[1:]
        echo(f"{n:>3}: {line}")
        step = {"line": n, "cmd": line, "ok": True}
        steps.append(step)
        try:
            if cmd in INPUTS:
                if stop_on_user:
                    user, since = _user_input(dev, since)
                    if user:
                        e = user[0]
                        raise PbtkError(f"line {n}: stopped, the player used the reader "
                                        f"({e['type']} {e.get('key') or (e.get('p1'), e.get('p2'))})")
                if watch_panel:
                    before = dev.agent.fbshot("raw").pixels
            if cmd == "tap":
                dev.tap(int(args[0]), int(args[1]))
            elif cmd == "long":
                dev.long_press(int(args[0]), int(args[1]))
            elif cmd == "swipe":
                dev.swipe(*(int(a) for a in args[:4]), ms=int(args[4]) if len(args) > 4 else 300)
            elif cmd == "key":
                dev.key(args[0], hold_ms=int(args[1]) if len(args) > 1 else 80)
            elif cmd == "wait":
                time.sleep(int(args[0]) / 1000)
            elif cmd == "note":
                activity.record(dev.source, "note", {"text": " ".join(args)})
            elif cmd == "write":
                dev.write_text(args[0], " ".join(args[1:]) + "\n")
            elif cmd == "restart":
                dev.restart_app()
                since = _user_input(dev, 0)[1] if stop_on_user else 0
                before = None
            elif cmd == "expect-change":
                if before is None:
                    raise PbtkError(f"line {n}: expect-change needs an input before it")
                limit = int(args[0]) if args else 4000
                deadline = sent + limit / 1000
                while dev.agent.fbshot("raw").pixels == before:
                    if time.monotonic() > deadline:
                        raise PbtkError(f"line {n}: the panel did not change within {limit} ms")
                    time.sleep(0.05)
                step["ms"] = round((time.monotonic() - sent) * 1000)
                echo(f"     panel changed after {step['ms']} ms")
            elif cmd in ("expect-screen", "expect-differs"):
                ref_path = Path(args[0]) if args[0].endswith(".png") else (out_dir or Path(".")) / f"{args[0]}.png"
                if not ref_path.exists():
                    raise PbtkError(f"line {n}: no reference screen {ref_path}")
                ref = read_png_gray(ref_path.read_bytes())
                same = cmd == "expect-screen"
                limit = float(args[1]) if len(args) > 1 else (2.0 if same else 10.0)
                box = tuple(int(a) for a in args[2:6]) if len(args) >= 6 else None
                pct = diff_pct(dev.agent.shot().turned(dev.view), ref, box)
                step["diff_pct"] = round(pct, 2)
                echo(f"     {pct:.2f}% differs from {ref_path.name}")
                if (pct > limit) if same else (pct < limit):
                    raise PbtkError(f"line {n}: screen {'differs from' if same else 'matches'} {ref_path.name} "
                                    f"({pct:.2f}% of pixels differ, {'max' if same else 'min'} {limit}%)")
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
            if cmd in INPUTS:
                sent = time.monotonic()
        except (IndexError, ValueError) as e:
            step.update(ok=False, error=f"bad arguments: {e}")
            raise PbtkError(f"line {n}: bad arguments for {cmd}: {e}") from e
        except PbtkError as e:
            step.update(ok=False, error=str(e))
            raise
    return shots
