# pbtk — PocketBook toolkit

Build for, deploy to, inspect and **drive** a PocketBook reader (Touch Lux 5,
PB628, firmware 6.x) from the PC — for people and for Claude. Other repos
(mmIV, inkquest, …) use it instead of each growing their own scripts.

```
           you ──► pbtk gui (browser) ─┐
                                       ├─► Device ─► USB storage (deploy, logs, probe)      T0
   Claude Code ──► pbtk mcp (stdio) ───┤     │
                                       │     └────► libpbagent in the running app (Wi-Fi) T1
   scripts/CI ──► pbtk <command> ──────┘            screenshots · taps · keys · log · files
                                   all actions → one activity timeline (shown in the GUI)
```

See [PLAN.md](PLAN.md) for the reasoning and the tiers, [docs/protocol.md](docs/protocol.md)
for the agent protocol.

## Install

```sh
make install          # uv tool install --editable ./host  → `pbtk` on PATH
make test             # host test suite (C agent runs against an InkView stub)
```

Needs Python ≥ 3.11 (stdlib only). Device builds need the SDK-B288 toolchain at
`~/opt/pocketbook-sdk/SDK_6.3.0-6.5/SDK-B288` (override `PB_SDK=`).

## Try it without the reader

```sh
pbtk sim &            # demo app + agent on this PC (InkView stub)
pbtk gui              # http://127.0.0.1:8629
pbtk tap 200 300 --shot ; pbtk key next ; pbtk logs
```

## First time with the reader (phase 1: what does this firmware allow?)

```sh
pbtk probe install    # reader on USB (PC link)
pbtk eject            # then on the reader: Applications → pbtk-probe (returns at once)
                      # wait ~40 s, plug USB back in, choose PC link
pbtk probe collect    # summary + ~/.local/state/pbtk/facts.json + fb0.png
```

## Daily loop for an app

```sh
cd ~/Repos/mmIV       # has a pbtk.toml
pbtk deploy --build --eject     # build, copy, write the agent token, eject
# open the app on the reader (Wi-Fi on)
pbtk shot             # → PNG path;  pbtk tap X Y --shot;  pbtk key back
pbtk shot --whole     # the whole panel (/dev/fb0): firmware menus, dialogs, keyboard
pbtk logs -f          # live app log
pbtk run session.pbtk # replay a recorded session (Timeline → "Save as script")
```

The GUI shows the reader's screen live (click = tap, drag = swipe, hold = long
press), hardware keys, the **timeline** of what Claude (orange), you (green)
and the reader itself (purple, your fingers on the device) did, the app log,
a file browser and deploy/eject. **I'm driving** pauses Claude's input until
you switch it off.

## Using pbtk from another repo

1. **Build settings** — in the app's Makefile:
   ```make
   PBTK ?= $(HOME)/Repos/pocketbook_lux_5
   include $(PBTK)/sdk/pocketbook.mk
   app: main.c
   	$(PB_CC) $(PB_CFLAGS) $(PBAGENT_CFLAGS) -o $@ main.c $(PBAGENT_SRC) $(PB_LDLIBS)
   ```
   C++ apps (ScummVM/mmIV): compile `pbagent.c` with `$(PB_CC)` and link it in;
   the header is `extern "C"`.
2. **Agent hooks** — three calls in the InkView handler (main thread):
   ```c
   pbagent_observe(type, p1, p2);                     // first line of the handler
   if (type == EVT_INIT) { pbagent_config c = {.app_name = "mmiv"}; pbagent_start(&c); }
   if (type == EVT_EXIT) pbagent_stop();
   ```
   `pbagent_log("...")` writes to `<bundle>/<app>.log`, which `pbtk logs` and the
   GUI stream. Wi-Fi must be on; set `.connect_wifi = 1` to have the agent call
   `NetConnect(NULL)` on start.
3. **pbtk.toml** at the repo root:
   ```toml
   [app]
   name  = "mmiv"
   dist  = "dist/applications"      # what goes into applications/ on the reader
   log   = "mmiv/mmiv.log"          # relative to applications/
   build = "make pocketbook"
   ```
4. **Claude Code** — `.mcp.json` in that repo:
   ```json
   {"mcpServers": {"pocketbook": {"command": "pbtk", "args": ["mcp"]}}}
   ```
   Tools: `pb_screenshot`, `pb_tap`, `pb_long_press`, `pb_swipe`, `pb_key`,
   `pb_logs`, `pb_user_events`, `pb_deploy`, `pb_eject`, `pb_ls`, `pb_read_file`,
   `pb_probe_collect`, `pb_status`, `pb_activity`, `pb_refresh`, `pb_exit_app`.

`templates/app/` is a complete starter app with all of the above.

## Layout

| Path | What |
|---|---|
| `device/probe/` | phase-1 probe app (POSIX sh, no compiler) |
| `device/libpbagent/` | the in-app agent (C, one file) |
| `device/host-stub/` | InkView stand-in to run device code on the PC |
| `sdk/pocketbook.mk` | shared toolchain/flags for PB628 firmware 6.x |
| `templates/app/` | starter app = demo used by tests and `pbtk sim` |
| `host/pbtk/` | CLI, transports, MCP server, GUI |
| `host/tests/` | end-to-end tests against the real agent |

State lives in `~/.local/state/pbtk/` (activity log, screenshots, token, facts);
`PBTK_STATE`, `PBTK_MOUNT`, `PBTK_AGENT`, `PBAGENT_TOKEN` override.

## Security

The agent listens only with a token, which travels over USB only. Paths are
allow-listed. The GUI binds to 127.0.0.1 and refuses cross-origin POSTs. The
UDP beacon carries no secret.
