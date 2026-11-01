# pbtk — plan

A toolkit other repos (mmIV, inkquest, …) use to build for, deploy to, inspect
and drive the PocketBook Touch Lux 5 (PB628, firmware U628.6.9.988), with a GUI
so a person can watch Claude work on the device or take over.

## Problem

- While the reader is mounted as USB storage it shows "connected to PC" and no
  app runs. Every test is copy → eject → a person taps the app → reconnect →
  read the log.
- Claude cannot tap the reader. Without help it can only plant files and wait.

## Tiers (same interface for each; later tiers add, nothing is thrown away)

| Tier | Needs | Gives | Status |
|---|---|---|---|
| **T0 USB storage** | nothing | deploy, logs, files, probe | done (`pbtk deploy/logs/ls/pull/push/eject/probe`) |
| **T1 in-app agent** | Wi-Fi, an app linked with `libpbagent` | live screenshots of the app, injected taps/keys/swipes, live log, the user's own input, files while the app runs | done (`device/libpbagent`, `pbtk shot/tap/key/…`) |
| **T2 root (PBJB)** | the owner choosing to jailbreak | SSH, launching apps, whole-screen capture, input outside apps | not started — decide after the probe |

Research behind T2: [PBJB](https://github.com/ezdiy/pbjb) puts scripts in
`/mnt/secure/runonce/` that the stock firmware runs at boot (no firmware files
changed) and gives root SSH (USB-net `169.254.0.1` or Wi-Fi; needs
`-oHostKeyAlgorithms=+ssh-rsa`). Firmware updates fail unless its runonce
scripts lose their immutable flag first. Reported working on FW 6.x; the PB628
on 6.9 is unconfirmed.

### Probe results (2026-11-01, [docs/FACTS.md](docs/FACTS.md))

- Apps run as `reader`, not root; input devices are not writable → driving
  the reader outside an app still needs PBJB (T2).
- `/dev/fb0` **is** readable by apps and shows the whole firmware UI (landscape
  buffer, turn 90° CW) → whole-screen *watching* works without root. Done:
  the agent's `fbshot` command (`pbtk shot --whole`, MCP `whole: true`, GUI
  "whole screen") includes InkView dialogs, the on-screen keyboard and
  firmware overlays, not only the app canvas.
- The first `fbshot` of the demo was 75 px off from `shot`. `pbtk calibrate`
  and a look at the panel showed `fbshot` was right: the firmware status panel
  (on unless the app disables it) moves the picture with wrap-around. Fixed in
  the template (`SetPanelType(PANEL_DISABLED)` on `EVT_SHOW`); the agent now
  reports `panel_*` in `info` and warns in the log.
- `/sbin/dropbear` ships with the firmware → a user-level SSH shell on a high
  port from an `.app` is worth a test before reaching for PBJB.

## Phases

1. **Facts** — `device/probe`: a shell-only app (no compiler) recording who apps
   run as, `/dev/fb0` geometry plus one dump, input devices and whether they are
   writable, uinput, Wi-Fi address, PBJB traces, loaders, and whether a detached
   child outlives the app. `pbtk probe install` / `pbtk probe collect`.
   *Needs one tap on the reader.* The answers decide whether a non-root daemon
   started from an `.app` could already do T2's job.
2. **CLI over USB** — `pbtk` (Python, stdlib only): `deploy` (incremental, FAT
   mtime-aware, writes the agent token), `logs`, `ls/pull/push`, `eject`
   (udisksctl), `pbtk.toml` per consuming repo, shared `sdk/pocketbook.mk`.
3. **libpbagent (T1)** — C, one file: TCP server with token auth, UDP beacon,
   canvas → PGM, injection queue drained on the InkView main thread, observed
   user input, log/file access. Host InkView stub so it runs and is tested on
   the PC; ARM build verified with the SDK-B288 toolchain (soft-float loader).
4. **GUI + MCP** — `pbtk gui` (local web app) and `pbtk mcp` (stdio MCP server).
   Both go through one `Device` facade that writes a shared activity timeline,
   so the GUI shows Claude's actions (orange), yours (green) and taps made on
   the reader itself (purple). "I'm driving" blocks Claude's input. Sessions
   can be saved as replayable scripts (`pbtk run`).
5. **Wi-Fi deploy** — `pbtk deploy --wifi [--restart]`: CRC-32 compare (`sum`), `put`
   only changed files, then `restart` re-execs the app in place (`/proc/self/exe`,
   same pid). After the first USB install no taps on the reader are needed.
   Also in the GUI (Deploy tab) and MCP (`pb_deploy wifi/restart`, `pb_restart`).
6. **T2** (optional, after the probe) — PBJB install guide, `pbagentd` system
   daemon (fb0 + input devices), SSH transport, remote app launch.
7. **Adoption** — mmIV links libpbagent in its ScummVM backend and gets a
   `pbtk.toml`; inkquest moves to `sdk/pocketbook.mk`.

## Risks

| Risk | Fallback |
|---|---|
| `/dev/fb0` does not match the panel on this firmware | T1 screenshots come from the InkView canvas, which works regardless |
| Writing to `/dev/input/*` needs root | T1 injects through InkView's `SendEvent` inside the app |
| PBJB fails on FW 6.9 | T1 still covers app development; only launching apps remotely is lost |
| Open port on the LAN | token required (written over USB only), path allow-list, GUI binds to localhost and refuses cross-origin POSTs |
