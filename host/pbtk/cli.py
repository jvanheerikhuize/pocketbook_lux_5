"""pbtk command line. `pbtk --help` lists everything; docs/usage.md explains the workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import PbtkError, __version__, activity, agent as agent_mod, paths, probe, project, script
from .device import Device


def _dev(args, source="cli") -> Device:
    return Device(source=source, agent_addr=args.agent, discover_timeout=args.discover,
                  view=getattr(args, "view", None))


def cmd_status(args):
    d = _dev(args)
    print(json.dumps(d.status(), indent=2))


def cmd_token(args):
    tok = paths.token()
    if args.action == "show":
        print(tok)
        return
    d = _dev(args)
    path = d.usb.install_token(tok)
    activity.record("cli", "token-install", {"path": path})
    print(f"token written to {path}; apps with libpbagent will accept pbtk from now on")


def cmd_probe(args):
    d = _dev(args)
    u = d.usb
    if args.action == "install":
        u.install_token(paths.token())
        changed = probe.install(u)
        activity.record("cli", "probe-install", {"files": len(changed)})
        print(f"installed the probe ({len(changed)} file(s) changed).")
        print("Next: `pbtk eject`, open 'pbtk-probe' from the reader's applications once,")
        print("wait ~40 s, plug USB back in (PC link), then `pbtk probe collect`.")
    else:
        result = probe.collect(u)
        activity.record("cli", "probe-collect", {}, result=json.dumps(result["report"]["tiers"]))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(probe.summary(result))
            print(f"\nfull facts: {paths.state_dir() / 'facts.json'}")


def cmd_deploy(args):
    proj = project.load(args.project)
    d = _dev(args)
    if args.restart and not args.wifi:
        raise PbtkError("--restart needs --wifi (over USB the app cannot run)")
    if args.wifi:
        r = d.deploy_wifi(proj, build=args.build, restart=args.restart)
        print(f"deployed {proj.name} over Wi-Fi: {len(r['changed'])} file(s) changed, "
              f"{r['unchanged']} unchanged" + (f", {r['kept']} kept (on the reader already)" if r['kept'] else ""))
        for c in r["changed"]:
            print(f"  {c}")
        if r["restarted"]:
            print(f"restarted in place (pid kept: {'yes' if r['pid_kept'] else 'no'}, build {r['build']})")
        return
    changed = d.deploy(proj, build=args.build, eject=args.eject)
    print(f"deployed {proj.name}: {len(changed)} file(s) changed")
    for c in changed[:50]:
        print(f"  {c}")
    if args.eject:
        print("ejected: open the app on the reader")


def cmd_restart(args):
    r = _dev(args).restart_app()
    print(f"restarted in place (pid kept: {'yes' if r['pid_kept'] else 'no'}, build {r['build']})")


def cmd_eject(args):
    print(f"ejected {_dev(args).eject()}")


def cmd_logs(args):
    d = _dev(args)
    log = args.log
    if not log:
        try:
            log = project.load(args.project).device_log
        except PbtkError:
            log = None
    if args.follow:
        a = d.agent
        data, off = a.log(-4096)
        sys.stdout.write(data.decode("utf-8", "replace"))
        while True:
            time.sleep(0.5)
            data, off = a.log(off)
            if data:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
    text, where = d.logs(args.lines, log)
    print(f"# {where}", file=sys.stderr)
    print(text)


def cmd_ls(args):
    entries, via = _dev(args).ls(args.path)
    print(f"# {args.path} via {via}", file=sys.stderr)
    for e in entries:
        print(f"{'d' if e['dir'] else '-'} {e['size']:>10}  {e['name']}")


def cmd_pull(args):
    d = _dev(args)
    u = d.usb_or_none()
    if u:
        for p in u.pull(args.device_path, Path(args.local)):
            print(p)
        return
    data = d.agent.get(args.device_path)
    out = Path(args.local)
    if out.is_dir():
        out = out / Path(args.device_path).name
    out.write_bytes(data)
    print(out)


def cmd_push(args):
    d = _dev(args)
    u = d.usb_or_none()
    if u:
        changed = u.push(Path(args.local), args.device_path)
        u.sync()
        print(f"{len(changed)} file(s) changed")
    else:
        d.agent.put(args.device_path, Path(args.local).read_bytes())
        print("written via agent")
    activity.record("cli", "push", {"to": args.device_path})


def cmd_discover(args):
    beacons = agent_mod.discover(args.timeout)
    if not beacons:
        print("no agents heard (is an app with libpbagent open, Wi-Fi on, same network?)")
        return 1
    for b in beacons:
        print(f"{b.host}:{b.port}  app={b.app}")
    agent_mod.remember(beacons[0].host, beacons[0].port)


def cmd_info(args):
    a = _dev(args).agent
    for k, v in a.info().items():
        print(f"{k:<12} {v}")


def cmd_fbinfo(args):
    for k, v in _dev(args).agent.fbinfo().items():
        print(f"{k:<15} {v}")


def cmd_calibrate(args):
    d = _dev(args)
    d._guard()
    d.agent.calibrate(not args.off)
    activity.record("cli", "calibrate", {"on": not args.off})
    if not args.off:
        print("calibration pattern on the panel: black bars along the canvas top and bottom (50 px),")
        print("ruler blocks every 100 px (n blocks at n*100). `pbtk calibrate --off` restores the app.")


def cmd_shot(args):
    img, path = _dev(args).screenshot(out=Path(args.output) if args.output else None,
                                      whole=args.whole, rotate=args.rotate)
    print(path)


def _after(args, result):
    _, _, shot = result
    if shot:
        print(shot)


def cmd_tap(args):
    _after(args, _dev(args).tap(args.x, args.y, shot_after=args.shot))


def cmd_long(args):
    _after(args, _dev(args).long_press(args.x, args.y, shot_after=args.shot))


def cmd_swipe(args):
    _after(args, _dev(args).swipe(args.x1, args.y1, args.x2, args.y2, args.ms, shot_after=args.shot))


def cmd_key(args):
    _after(args, _dev(args).key(args.name, shot_after=args.shot))


def cmd_events(args):
    a = _dev(args).agent
    events, nxt = a.events(args.since)
    for e in events:
        print(json.dumps(e))
    print(f"# next={nxt}", file=sys.stderr)


def cmd_refresh(args):
    _dev(args).refresh()


def cmd_exit_app(args):
    _dev(args).exit_app()


def cmd_run(args):
    d = _dev(args, source="script")
    shots = script.run(d, Path(args.file).read_text(), Path(args.out) if args.out else None)
    print(f"done; {len(shots)} screenshot(s)")


def cmd_activity(args):
    entries, _ = activity.read(activity.tail_offset(args.n), limit=args.n)
    for e in entries:
        t = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        print(f"{t} {e['source']:<6} {e['action']:<14} {json.dumps(e.get('args', {}))}"
              f"{'' if e.get('ok', True) else '  FAILED: ' + e.get('result', '')}")


def cmd_gui(args):
    from .gui.server import serve

    serve(args.host, args.port, agent_addr=args.agent, open_browser=not args.no_browser)


def cmd_mcp(args):
    from .mcp import serve

    serve(agent_addr=args.agent)


def cmd_sim(args):
    """Build and run the demo app on this PC against the InkView stub."""
    tmpl = paths.toolkit_root() / "templates" / "app"
    r = subprocess.run(["make", "-s", "-C", str(tmpl), "host"], capture_output=True, text=True)
    if r.returncode != 0:
        raise PbtkError(f"building the simulator failed:\n{r.stdout}{r.stderr}")
    exe = tmpl / "build" / "pbtk-demo-host"
    env = dict(os.environ, PBAGENT_TOKEN=paths.token(), PBAGENT_PORT=str(args.port))
    agent_mod.remember("127.0.0.1", args.port)
    print(f"simulator: {exe} on 127.0.0.1:{args.port} (Ctrl-C to stop)")
    print("try: pbtk shot | pbtk tap 200 300 --shot | pbtk gui")
    try:
        subprocess.run([str(exe)], env=env, cwd=exe.parent)
    except KeyboardInterrupt:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pbtk", description="PocketBook toolkit")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--agent", help="agent HOST[:PORT] (default: $PBTK_AGENT, last seen, or discovery)")
    p.add_argument("--view", choices=["none", "cw", "ccw"],
                   help="turn app screenshots; taps use the same coordinates "
                        "(default: $PBTK_VIEW or `view` in pbtk.toml)")
    p.add_argument("--discover", type=float, default=4.0, help="seconds to listen for agents (0 = never)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("status", cmd_status, "what is reachable (USB storage, agent)")
    sp = add("token", cmd_token, "show the agent token or write it to the reader")
    sp.add_argument("action", choices=["install", "show"])
    sp = add("probe", cmd_probe, "phase 1: install the probe app / collect its facts")
    sp.add_argument("action", choices=["install", "collect"])
    sp.add_argument("--json", action="store_true")
    sp = add("deploy", cmd_deploy, "copy a project's built bundle to the reader (USB, or --wifi)")
    sp.add_argument("--project", help="dir with pbtk.toml (default: search upwards)")
    sp.add_argument("--build", action="store_true", help="run the project's build command first")
    sp.add_argument("--eject", action="store_true", help="eject afterwards so you can open the app")
    sp.add_argument("--wifi", action="store_true",
                    help="copy changed files through the running app's agent instead of USB")
    sp.add_argument("--restart", action="store_true",
                    help="with --wifi: restart the running app in place to load the new build")
    add("eject", cmd_eject, "unmount the reader so it leaves PC-link mode")
    add("restart", cmd_restart, "restart the running app in place (loads the binary on disk)")
    sp = add("logs", cmd_logs, "app log: live via agent, else from USB")
    sp.add_argument("-n", "--lines", type=int, default=50)
    sp.add_argument("--log", help="device path of the log (default: from pbtk.toml)")
    sp.add_argument("--project")
    sp.add_argument("-f", "--follow", action="store_true", help="stream (agent only)")
    sp = add("ls", cmd_ls, "list a folder on the reader")
    sp.add_argument("path", nargs="?", default="/mnt/ext1/applications")
    sp = add("pull", cmd_pull, "copy from the reader")
    sp.add_argument("device_path")
    sp.add_argument("local", nargs="?", default=".")
    sp = add("push", cmd_push, "copy to the reader")
    sp.add_argument("local")
    sp.add_argument("device_path")
    sp = add("discover", cmd_discover, "listen for agent beacons on the LAN")
    sp.add_argument("--timeout", type=float, default=6.0)
    add("info", cmd_info, "running app and screen, via agent")
    add("fbinfo", cmd_fbinfo, "framebuffer geometry from the driver (for `shot --whole`)")
    sp = add("calibrate", cmd_calibrate, "draw an edge/ruler pattern on the panel to check alignment")
    sp.add_argument("--off", action="store_true", help="let the app repaint itself")
    sp = add("shot", cmd_shot, "screenshot of the running app (PNG)")
    sp.add_argument("-o", "--output")
    sp.add_argument("-w", "--whole", action="store_true",
                    help="whole panel from /dev/fb0 (firmware UI, dialogs, keyboard), not just the app canvas")
    sp.add_argument("--rotate", choices=["auto", "raw", "cw", "ccw"], default="auto",
                    help="with --whole: auto = portrait as held (default)")
    for name, fn, coords in (("tap", cmd_tap, ["x", "y"]), ("long", cmd_long, ["x", "y"])):
        sp = add(name, fn, f"{name} at screen coordinates")
        for c in coords:
            sp.add_argument(c, type=int)
        sp.add_argument("--shot", action="store_true", help="screenshot afterwards")
    sp = add("swipe", cmd_swipe, "swipe between two points")
    for c in ("x1", "y1", "x2", "y2"):
        sp.add_argument(c, type=int)
    sp.add_argument("--ms", type=int, default=300)
    sp.add_argument("--shot", action="store_true")
    sp = add("key", cmd_key, f"press a key: {', '.join(agent_mod.KEYS)}")
    sp.add_argument("name")
    sp.add_argument("--shot", action="store_true")
    sp = add("events", cmd_events, "input the user gave on the reader (needs pbagent_observe)")
    sp.add_argument("--since", type=int, default=0)
    add("refresh", cmd_refresh, "full e-ink refresh in the app")
    add("exit-app", cmd_exit_app, "close the running app")
    sp = add("run", cmd_run, "run an input script (see pbtk/script.py)")
    sp.add_argument("file")
    sp.add_argument("--out", help="folder for `shot NAME` screenshots")
    sp = add("activity", cmd_activity, "recent actions on the shared timeline")
    sp.add_argument("-n", type=int, default=30)
    sp = add("gui", cmd_gui, "web GUI: live screen, controls, timeline, logs, files")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8629)
    sp.add_argument("--no-browser", action="store_true")
    add("mcp", cmd_mcp, "MCP server on stdio (for Claude Code)")
    sp = add("sim", cmd_sim, "run the demo app on this PC (InkView stub) for trying pbtk")
    sp.add_argument("--port", type=int, default=8628)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rc = args.fn(args)
        return rc or 0
    except PbtkError as e:
        print(f"pbtk: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
