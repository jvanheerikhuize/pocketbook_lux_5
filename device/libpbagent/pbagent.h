/*
 * libpbagent — remote control for an InkView app (pbtk tier T1).
 *
 * Link this into an app to let pbtk (CLI, MCP server, GUI) watch and drive it
 * over Wi-Fi while it runs: screenshots of the app canvas, injected taps and
 * keys, a live log, observed user input, and file access.
 *
 * Integration (three calls, all from the InkView main thread):
 *
 *     static int handler(int type, int p1, int p2) {
 *         pbagent_observe(type, p1, p2);          // optional: report real input
 *         if (type == EVT_INIT) {
 *             pbagent_config cfg = { .app_name = "myapp" };
 *             pbagent_start(&cfg);
 *         }
 *         if (type == EVT_EXIT) pbagent_stop();
 *         ...
 *     }
 *
 * Security: the agent only listens when it finds a token (see pbagent_config).
 * pbtk writes that token over USB (`pbtk token install`); every TCP client must
 * send it first. Without a token the agent logs why and stays off.
 *
 * Protocol (TCP, default port 8628), one request line per command:
 *     request:  <command> [args...]\n
 *     response: ok <payload-bytes> [key=value ...]\n<payload>   or   err <message>\n
 * The full command list is in docs/protocol.md.
 */
#ifndef PBAGENT_H
#define PBAGENT_H

#ifdef __cplusplus
extern "C" {
#endif

#define PBAGENT_VERSION "1"
#define PBAGENT_DEFAULT_PORT 8628

typedef struct pbagent_config {
    const char *app_name;  /* shown by pbtk; required */
    const char *log_path;  /* app log served by `log`; default <bundle>/<app>.log */
    const char *token;     /* default: $PBAGENT_TOKEN, <bundle>/pbagent.token,
                              then /mnt/ext1/applications/pbagent.token */
    int port;              /* 0 → PBAGENT_DEFAULT_PORT; $PBAGENT_PORT overrides */
    int connect_wifi;      /* 1 → call NetConnect(NULL) once on start (blocks briefly) */
    int no_beacon;         /* 1 → do not broadcast the UDP discovery beacon */
    const char *restart_exec; /* `restart` runs this script with /bin/sh (e.g. the
                              app's .app launcher, which must `exec` the binary);
                              default: the current executable with the same argv */
} pbagent_config;

/* Start the agent. Main thread, usually on EVT_INIT. Returns 0 when listening,
 * -1 when disabled (no token, socket error); the reason is in the log. */
int pbagent_start(const pbagent_config *cfg);

/* Stop the agent and close all connections. Main thread, on EVT_EXIT. */
void pbagent_stop(void);

/* Report an event the app received, so pbtk can show the user's own taps and
 * keys on its timeline. Injected events are not reported twice. Cheap; safe to
 * call for every event. */
void pbagent_observe(int type, int par1, int par2);

/* Append a line to the app log (timestamped, thread-safe). Also usable when
 * the agent is not running. */
void pbagent_log(const char *fmt, ...)
#if defined(__GNUC__)
    __attribute__((format(printf, 1, 2)))
#endif
    ;

/* Path of the folder the running executable lives in (the app bundle). */
const char *pbagent_bundle_dir(void);

#ifdef __cplusplus
}
#endif
#endif
