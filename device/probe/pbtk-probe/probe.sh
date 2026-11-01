#!/bin/sh
# pbtk probe: records what the firmware offers, so the host can decide which
# control tier (T1 in-app agent, T2 root daemon) is possible on this device.
#
# Runs from the launcher (applications/pbtk-probe.app). It writes only inside its
# own folder and reads everything else. Output:
#   facts.txt       key=value lines, parsed by `pbtk probe collect`
#   probe.log       raw command output, for a human
#   fb0.raw         one raw framebuffer dump (does /dev/fb0 match the screen?)
#   bg-*.txt        written by a detached child, to see whether it outlives the app
#
# Busybox on the device may lack commands; every check degrades instead of failing.

DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
[ -n "$DIR" ] || DIR=/mnt/ext1/applications/pbtk-probe
FACTS="$DIR/facts.txt"
LOG="$DIR/probe.log"

have() { command -v "$1" >/dev/null 2>&1; }
fact() { printf '%s=%s\n' "$1" "$2" >>"$FACTS"; }
section() { printf '\n== %s ==\n' "$1" >>"$LOG"; }
run() { printf '$ %s\n' "$*" >>"$LOG"; "$@" >>"$LOG" 2>&1; }
first_line() { head -n 1 "$1" 2>/dev/null | tr -d '\r'; }

rm -f "$FACTS" "$LOG" "$DIR/fb0.raw"
: >"$FACTS"

count=0
[ -f "$DIR/launch-count" ] && count="$(cat "$DIR/launch-count" 2>/dev/null)"
case "$count" in ''|*[!0-9]*) count=0 ;; esac
count=$((count + 1))
echo "$count" >"$DIR/launch-count"

fact probe.version 1
fact probe.launch "$count"
fact probe.dir "$DIR"
fact probe.time "$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null)"
fact probe.uptime_s "$(cut -d. -f1 /proc/uptime 2>/dev/null)"

# --- who apps run as (decides whether T1 can already reach root-only devices)
section identity
run id
fact user.uid "$(id -u 2>/dev/null)"
fact user.name "$(id -un 2>/dev/null)"
fact user.groups "$(id -Gn 2>/dev/null)"

# --- system
section system
run uname -a
fact sys.kernel "$(uname -r 2>/dev/null)"
fact sys.arch "$(uname -m 2>/dev/null)"
for f in /ebrmain/config/version /etc/version /etc/os-release /etc/issue; do
  [ -f "$f" ] && { echo "--- $f" >>"$LOG"; cat "$f" >>"$LOG" 2>&1; }
done
[ -f /ebrmain/config/version ] && fact sys.firmware "$(first_line /ebrmain/config/version)"
fact sys.mem_total_kb "$(awk '/^MemTotal/ {print $2}' /proc/meminfo 2>/dev/null)"
fact sys.cpus "$(grep -c '^processor' /proc/cpuinfo 2>/dev/null)"
fact sys.busybox "$(busybox 2>&1 | head -n 1)"
for t in setsid nohup timeout nc ifconfig ip udhcpc dropbear sshd telnetd utelnetd tcpsvd httpd fbgrab; do
  if have "$t"; then fact "tool.$t" "$(command -v "$t")"; else fact "tool.$t" ""; fi
done

# --- framebuffer
section framebuffer
for f in /sys/class/graphics/fb0/*; do
  [ -f "$f" ] || continue
  case "$f" in *uevent|*power*) continue ;; esac
  printf '%s: %s\n' "$f" "$(head -c 200 "$f" 2>/dev/null | tr '\n' ' ')" >>"$LOG"
done
fact fb.exists "$([ -e /dev/fb0 ] && echo 1 || echo 0)"
fact fb.readable "$([ -r /dev/fb0 ] && echo 1 || echo 0)"
fact fb.writable "$([ -w /dev/fb0 ] && echo 1 || echo 0)"
fact fb.virtual_size "$(first_line /sys/class/graphics/fb0/virtual_size)"
fact fb.bits_per_pixel "$(first_line /sys/class/graphics/fb0/bits_per_pixel)"
fact fb.stride "$(first_line /sys/class/graphics/fb0/stride)"
fact fb.rotate "$(first_line /sys/class/graphics/fb0/rotate)"
fact fb.name "$(first_line /sys/class/graphics/fb0/name)"
if [ -r /dev/fb0 ]; then
  # One screen's worth is enough to compare with what is on the panel right now.
  bytes=""
  stride="$(first_line /sys/class/graphics/fb0/stride)"
  vs="$(first_line /sys/class/graphics/fb0/virtual_size)"
  yres="${vs#*,}"
  case "$stride$yres" in *[!0-9]*|'') ;; *) bytes=$((stride * yres)) ;; esac
  if [ -n "$bytes" ] && [ "$bytes" -gt 0 ]; then
    dd if=/dev/fb0 of="$DIR/fb0.raw" bs="$stride" count="$yres" 2>>"$LOG"
  else
    dd if=/dev/fb0 of="$DIR/fb0.raw" bs=1024 count=4096 2>>"$LOG"
  fi
  fact fb.dump_bytes "$(wc -c <"$DIR/fb0.raw" 2>/dev/null | tr -d ' ')"
fi

# --- input devices (which node is touch, which is buttons, can we write to them)
section input
run cat /proc/bus/input/devices
i=0
for ev in /dev/input/event*; do
  [ -e "$ev" ] || continue
  n="${ev##*event}"
  name="$(first_line "/sys/class/input/event$n/device/name")"
  fact "input.event$n.name" "$name"
  fact "input.event$n.readable" "$([ -r "$ev" ] && echo 1 || echo 0)"
  fact "input.event$n.writable" "$([ -w "$ev" ] && echo 1 || echo 0)"
  i=$((i + 1))
done
fact input.count "$i"
fact input.uinput "$([ -e /dev/uinput ] && echo 1 || echo 0)"
fact input.uinput_writable "$([ -w /dev/uinput ] && echo 1 || echo 0)"

# --- network
section network
run ifconfig -a
run ip addr
run cat /proc/net/wireless
ipaddr=""
if have ip; then
  ipaddr="$(ip -4 addr show 2>/dev/null | awk '/inet / && $2 !~ /^127\./ {sub(/\/.*/, "", $2); print $2; exit}')"
fi
if [ -z "$ipaddr" ] && have ifconfig; then
  ipaddr="$(ifconfig 2>/dev/null | awk '/inet addr:/ {sub("addr:", "", $2); if ($2 !~ /^127\./) {print $2; exit}}')"
fi
fact net.ipv4 "$ipaddr"
fact net.wlan_present "$(grep -q 'wlan\|eth' /proc/net/dev 2>/dev/null && echo 1 || echo 0)"

# --- root / jailbreak routes (T2)
section root
run ls -la /mnt/secure
run ls -la /mnt/secure/runonce
fact root.secure_dir "$([ -d /mnt/secure ] && echo 1 || echo 0)"
fact root.secure_writable "$([ -w /mnt/secure ] && echo 1 || echo 0)"
fact root.runonce_dir "$([ -d /mnt/secure/runonce ] && echo 1 || echo 0)"
fact root.secure_su "$([ -x /mnt/secure/su ] && echo 1 || echo 0)"
fact root.pbjb "$(ls /mnt/secure/runonce 2>/dev/null | grep -qi 'pbjb\|jail' && echo 1 || echo 0)"
run ps
fact proc.dropbear_running "$(ps 2>/dev/null | grep -v grep | grep -q dropbear && echo 1 || echo 0)"

# --- InkView and libraries
section libraries
for d in /ebrmain/lib /usr/lib /lib; do
  [ -d "$d" ] && ls "$d" 2>/dev/null | grep -iE 'inkview|curl|ssl|png|jpeg|freetype' | sed "s|^|$d/|" >>"$LOG"
done
ls -l /lib/ld-linux*.so* >>"$LOG" 2>&1
fact libc.hardfloat_loader "$([ -e /lib/ld-linux-armhf.so.3 ] && echo 1 || echo 0)"
fact libc.softfloat_loader "$([ -e /lib/ld-linux.so.3 ] && echo 1 || echo 0)"

# --- does a detached child outlive the launcher? (decides whether a daemon
#     started from an .app keeps running after the app returns to the library)
section background
echo "started $(cut -d. -f1 /proc/uptime)" >"$DIR/bg-start.txt"
rm -f "$DIR/bg-alive.txt"
child='sleep 30; echo "alive uptime=$(cut -d. -f1 /proc/uptime)" > "$0/bg-alive.txt"'
if have setsid; then
  setsid sh -c "$child" "$DIR" </dev/null >/dev/null 2>&1 &
  fact bg.method setsid
elif have nohup; then
  nohup sh -c "$child" "$DIR" </dev/null >/dev/null 2>&1 &
  fact bg.method nohup
else
  sh -c "$child" "$DIR" </dev/null >/dev/null 2>&1 &
  fact bg.method plain
fi
fact bg.started_uptime "$(cut -d. -f1 /proc/uptime)"

fact probe.done 1
sync
