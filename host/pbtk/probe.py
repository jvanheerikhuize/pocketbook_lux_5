"""Phase 1: install the probe app over USB, then collect and interpret its facts."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import PbtkError, paths
from .image import from_framebuffer
from .usb import UsbStorage

APP_DIR = "/mnt/ext1/applications/pbtk-probe"
TOUCH_HINT = re.compile(r"touch|_tp|tp_|cyttsp|elan|goodix|zforce|ft5|tsc|ilitek|gt9", re.I)
KEY_HINT = re.compile(r"key|gpio|button|pwr|power", re.I)


def install(usb: UsbStorage) -> list[str]:
    src = paths.device_dir() / "probe"
    if not (src / "pbtk-probe.app").exists():
        raise PbtkError(f"probe files not found under {src} (set PBTK_ROOT to the toolkit checkout)")
    changed = usb.push(src / "pbtk-probe", APP_DIR)
    changed += usb.push(src / "pbtk-probe.app", "/mnt/ext1/applications/pbtk-probe.app")
    usb.sync()
    return changed


def parse_facts(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def collect(usb: UsbStorage) -> dict:
    try:
        facts = parse_facts(usb.read(APP_DIR + "/facts.txt").decode("utf-8", "replace"))
    except PbtkError:
        raise PbtkError(
            "No probe results yet. Eject (`pbtk eject`), open 'pbtk-probe' from the reader's "
            "applications list once, wait ~40 s, reconnect USB, then run this again."
        )
    if facts.get("probe.done") != "1":
        raise PbtkError("facts.txt is incomplete; the probe did not finish. Run it again.")

    out_dir = paths.state_dir() / "probe"
    out_dir.mkdir(exist_ok=True)
    for name in ("facts.txt", "probe.log"):
        try:
            (out_dir / name).write_bytes(usb.read(f"{APP_DIR}/{name}"))
        except PbtkError:
            pass

    report = interpret(facts)

    # Did the detached child outlive the launcher?
    try:
        alive = usb.read(APP_DIR + "/bg-alive.txt").decode().strip()
        report["background_survives"] = True
        report["background_note"] = alive
    except PbtkError:
        report["background_survives"] = False

    # Framebuffer dump → PNG, to compare with what the panel showed during the probe.
    try:
        raw = usb.read(APP_DIR + "/fb0.raw")
        w, h = (int(v) for v in facts.get("fb.virtual_size", "0,0").split(","))
        bpp = int(facts.get("fb.bits_per_pixel") or 8)
        stride = int(facts.get("fb.stride") or w * max(bpp // 8, 1))
        if w and h:
            visible_h = min(h, len(raw) // stride) if stride else 0
            img = from_framebuffer(raw, w, visible_h, bpp, stride)
            png = out_dir / "fb0.png"
            png.write_bytes(img.png())
            report["framebuffer_png"] = str(png)
    except (PbtkError, ValueError):
        pass

    result = {"facts": facts, "report": report}
    (paths.state_dir() / "facts.json").write_text(json.dumps(result, indent=2))
    return result


def interpret(f: dict[str, str]) -> dict:
    r: dict = {}
    r["runs_as_root"] = f.get("user.uid") == "0"
    r["firmware"] = f.get("sys.firmware") or "?"
    r["kernel"] = f.get("sys.kernel")
    r["framebuffer"] = {
        "readable": f.get("fb.readable") == "1",
        "writable": f.get("fb.writable") == "1",
        "size": f.get("fb.virtual_size"),
        "bpp": f.get("fb.bits_per_pixel"),
        "stride": f.get("fb.stride"),
    }
    inputs = []
    for k, v in f.items():
        m = re.fullmatch(r"input\.event(\d+)\.name", k)
        if m:
            n = m.group(1)
            inputs.append({
                "node": f"/dev/input/event{n}",
                "name": v,
                "role": "touch" if TOUCH_HINT.search(v) else "keys" if KEY_HINT.search(v) else "?",
                "writable": f.get(f"input.event{n}.writable") == "1",
            })
    r["inputs"] = sorted(inputs, key=lambda i: int(i["node"].rsplit("event", 1)[1]))
    r["uinput_writable"] = f.get("input.uinput_writable") == "1"
    r["ipv4"] = f.get("net.ipv4") or None
    r["pbjb_installed"] = f.get("root.pbjb") == "1"
    r["dropbear_running"] = f.get("proc.dropbear_running") == "1"
    r["softfloat_only"] = f.get("libc.softfloat_loader") == "1" and f.get("libc.hardfloat_loader") != "1"

    can_inject = any(i["writable"] for i in inputs) or r["uinput_writable"]
    r["tiers"] = {
        "T0_usb": True,
        "T1_in_app_agent": True,
        "system_wide_screenshots_without_root": r["framebuffer"]["readable"],
        "system_wide_input_without_root": can_inject,
        "T2_root_ssh": r["pbjb_installed"] or r["dropbear_running"],
    }
    return r


def summary(result: dict) -> str:
    r = result["report"]
    yes = lambda b: "yes" if b else "no"  # noqa: E731
    lines = [
        f"firmware          {r['firmware']}  (kernel {r.get('kernel')})",
        f"apps run as root  {yes(r['runs_as_root'])}",
        f"/dev/fb0          readable={yes(r['framebuffer']['readable'])} "
        f"writable={yes(r['framebuffer']['writable'])} size={r['framebuffer']['size']} "
        f"bpp={r['framebuffer']['bpp']} stride={r['framebuffer']['stride']}",
        f"uinput writable   {yes(r['uinput_writable'])}",
        f"Wi-Fi IPv4        {r['ipv4'] or '(off during probe)'}",
        f"detached child    {'survived the app' if r.get('background_survives') else 'did not survive (or not yet written)'}",
        f"PBJB / dropbear   {yes(r['pbjb_installed'])} / {yes(r['dropbear_running'])}",
        "input devices:",
    ]
    for i in r["inputs"]:
        lines.append(f"  {i['node']:<20} {i['role']:<6} writable={yes(i['writable'])}  {i['name']}")
    if r.get("framebuffer_png"):
        lines.append(f"framebuffer dump  {r['framebuffer_png']}  (compare with what the screen showed)")
    t = r["tiers"]
    lines.append("")
    lines.append("what this means:")
    if t["system_wide_screenshots_without_root"] and t["system_wide_input_without_root"]:
        lines.append("  apps can already see the whole screen and inject input: a daemon started from an")
        lines.append("  .app could control the reader outside apps too, without PBJB.")
    else:
        lines.append("  without root, control is limited to apps that link libpbagent (T1).")
    if not t["T2_root_ssh"]:
        lines.append("  full remote control (launching apps, firmware menus) needs PBJB (T2).")
    return "\n".join(lines)
