# Device facts — PocketBook Touch Lux 5 (PB628)

Confirmed by `pbtk probe` on 2026-11-01 (probe v1, launched from the
applications menu). Re-run the probe after a firmware update.

| Fact | Value | Consequence |
|---|---|---|
| User apps run as | `reader` (uid 101, groups 3003) — **not root**. Firmware apps run as `sreader`; system daemons as root | no access to root-only devices from an `.app` |
| Kernel / userland | Linux 3.10.65+ armv7l (built 2024-12-02), BusyBox 1.26.2 (2025-01-06), 2 CPUs, 512 MB RAM | |
| Loader / libc | only `/lib/ld-linux.so.3` → `ld-2.23.so` (soft-float, glibc 2.23) | build with SDK-B288 `arm-obreey-linux-gnueabi` (as in `sdk/pocketbook.mk`) |
| Firmware libs | `/ebrmain/lib`: libinkview, libcurl 4.7.0, libssl 1.0.0, libfreetype 6.13, libpng12, libjpeg 8 | |
| Firmware version file | `/ebrmain/config/version` absent | read it from InkView `GetSoftwareVersion()` (`pbtk info`) |
| `/dev/fb0` | **readable and writable by `reader`**; 8 bpp, virtual 1024×758, stride 1024, sysfs `rotate=2`; modes `758x1024`, `1024x758` | whole-screen screenshots **without root**; the buffer is landscape: turn 90° clockwise for the portrait panel |
| What fb0 shows | the full firmware UI (dump during the probe showed the applications menu, pixel-exact) | screenshots can include firmware menus, dialogs and the on-screen keyboard, not only an app's canvas |
| Input devices | event0 `sunxi-ths`, event1 `sunxi-keyboard`, event2 `axp22-powerkey`, event3 `gpio_keys`, event4 `elan-ktf2k` (touch), event5/6 unnamed — **none writable**; `/dev/uinput` not writable | no input injection outside an app without root; inside apps, libpbagent's `SendEvent` path is the way |
| Network | Wi-Fi interface is `eth0` (wpa_supplicant `-ieth0`, `netagent net on`); interface absent when Wi-Fi is off | apps must bring Wi-Fi up (`NetConnect`, libpbagent `.connect_wifi = 1`) |
| Shell tools | `setsid`, `nohup`, `timeout`, `nc`, `ifconfig`, `udhcpc`, **`/sbin/dropbear`**; no `ip`, `telnetd`, `httpd` | a user-level dropbear on a high port is a possible non-root shell (untested) |
| Root / jailbreak | `/mnt/secure` exists, not readable by `reader`; no PBJB, no running dropbear | T2 needs PBJB |
| Detached child outliving the app | not observed (no `bg-alive.txt`). Inconclusive: reconnecting USB within 30 s unmounts `/mnt/ext1` on the reader, which also stops the write | re-check over the agent: `pbtk ls /mnt/ext1/applications/pbtk-probe` while pbtk-demo runs |
