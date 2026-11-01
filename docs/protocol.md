# libpbagent wire protocol (v1)

TCP, default port **8628** (`PBAGENT_PORT` overrides). One request line per
command; replies are

```
ok <payload-bytes> [key=value ...]\n<payload>
err <message>\n
```

The first line of every connection must be `auth <token>`; anything else gets
`err auth required` and the connection is closed. The token comes from
`$PBAGENT_TOKEN`, `<bundle>/pbagent.token`, `<bundle>/../pbagent.token` or
`/mnt/ext1/applications/pbagent.token` (written by `pbtk token install` /
`pbtk deploy`). With no token the agent does not listen at all.

| Command | Reply payload / fields | Notes |
|---|---|---|
| `ping` | — | |
| `info` | `key=value` lines: agent, app, pid, port, width, height, depth, orientation, model, firmware, bundle, log | |
| `shot` | binary PGM (P5, 8-bit), fields `width height` | the app's canvas (what InkView draws), not `/dev/fb0` |
| `tap X Y [HOLD_MS=60]` | field `dispatched=1/0` | `EVT_POINTERDOWN`, then `EVT_POINTERUP` after HOLD_MS |
| `long X Y` | `dispatched` | down, `EVT_POINTERLONG` at 600 ms, up at 900 ms |
| `down/move/up X Y` | `dispatched` | single pointer events |
| `swipe X1 Y1 X2 Y2 [MS=300]` | `dispatched` | down, 8 moves, up |
| `key NAME|CODE [HOLD_MS=80]` | `dispatched` | names: ok up down left right menu prev next home back prev2 next2 |
| `event TYPE P1 P2` | `dispatched` | raw InkView event (escape hatch) |
| `refresh` | `dispatched` | `FullUpdate()` on the main thread |
| `exit` | — | `CloseApp()` 100 ms later |
| `events SINCE` | lines `seq ms type p1 p2`; fields `next now` | input the *user* gave (via `pbagent_observe`); injected events are excluded |
| `log OFFSET [MAX]` | log bytes; fields `size offset` | negative OFFSET = from the end; offset past the end restarts at 0 (log truncated) |
| `ls PATH` | lines `d|f size mtime name` | |
| `get PATH` | file bytes (≤ 64 MB) | |
| `put PATH LEN` + LEN bytes | — | written to `PATH.pbtk-tmp`, then renamed |

Paths are limited to `/mnt/ext1`, `/mnt/ext2`, `/tmp` and the app bundle, may
not contain `..` segments or spaces.

**Threading.** Injection commands are queued and executed by an InkView hard
timer on the main thread (30 ms while a client is connected or events are
pending, 250 ms otherwise), because InkView handlers are not thread-safe. The
reply is sent once the last event was dispatched (`dispatched=1`) or after 5 s
plus the gesture's duration (`dispatched=0`, e.g. while the app is in the
background). `shot` and file commands run on the client's thread.

**Discovery.** Every 3 s the agent broadcasts a UDP datagram to
`255.255.255.255:<port>`: `pbagent 1 app=<name> port=<port>`. It carries no
secret. `pbtk discover` listens for it.

**Limits.** 4 concurrent clients; 128 queued events; 256 observed events.
