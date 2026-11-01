/*
 * libpbagent — see pbagent.h for the integration contract and docs/protocol.md
 * for the wire protocol.
 *
 * Threads:
 *   - acceptor thread: accepts TCP clients, broadcasts the UDP beacon;
 *   - one thread per client: parses commands, reads the canvas, moves files;
 *   - InkView main thread: a hard timer drains the injection queue and calls
 *     SendEvent() there, because InkView handlers are not thread-safe.
 */
#define _GNU_SOURCE
#include "pbagent.h"

#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inkview.h>
#include <limits.h>
#include <linux/fb.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define MAX_CLIENTS 4
#define QUEUE_LEN 128
#define OBSERVED_LEN 256
#define LINE_MAX_LEN 1024
#define BEACON_MS 3000
#define TICK_ACTIVE_MS 30
#define TICK_IDLE_MS 250
#define DISPATCH_WAIT_MS 5000

/* Pseudo event types the main thread handles itself. */
#define Q_FULLUPDATE (-1)
#define Q_CLOSEAPP (-2)
#define Q_CALIBRATE (-3)

/* Calibration pattern: shows on the panel itself where the canvas edges land.
 * Black bars along the top and bottom (CAL_BAR px), thin black edges left and
 * right, and ruler blocks every 100 px down the left side whose width grows
 * with the hundred (1 block at 100, 2 at 200, ...), so offsets can be read
 * off both the panel and a screenshot. */
#define CAL_BAR 50

static void draw_calibration(void) {
    int w = ScreenWidth(), h = ScreenHeight();
    FillArea(0, 0, w, h, WHITE);
    FillArea(0, 0, w, CAL_BAR, BLACK);
    FillArea(0, h - CAL_BAR, w, CAL_BAR, BLACK);
    FillArea(0, 0, 6, h, BLACK);
    FillArea(w - 6, 0, 6, h, BLACK);
    for (int y = 100; y < h - CAL_BAR; y += 100) {
        FillArea(6, y, w - 12, 2, LGRAY);     /* faint rule across */
        FillArea(20, y - 10, 24 * (y / 100), 20, BLACK); /* n blocks at n*100 */
        for (int k = 1; k < y / 100; k++) FillArea(20 + 24 * k - 3, y - 10, 3, 20, WHITE);
    }
    for (int y = 25; y < h - CAL_BAR; y += 25) FillArea(w - 40, y, 34, 2, BLACK); /* ticks */
    FullUpdate();
}

typedef struct {
    long due_ms;
    long id;
    int type, p1, p2;
} qitem;

typedef struct {
    long seq;
    long ms;
    int type, p1, p2;
} observed;

static struct {
    int running;
    char app_name[64];
    char log_path[PATH_MAX];
    char token[128];
    char bundle[PATH_MAX];
    char model[64];
    char firmware[64];
    int port;
    int no_beacon;
    int listen_fd;
    int beacon_fd;
    pthread_t acceptor;
    int clients;
    int client_fds[MAX_CLIENTS];

    pthread_mutex_t qlock;
    pthread_cond_t qdone;
    qitem queue[QUEUE_LEN];
    int qcount;
    long next_id;
    long done_id;

    observed obs[OBSERVED_LEN];
    long obs_seq;
    int dispatching;
} A = {
    .listen_fd = -1,
    .beacon_fd = -1,
    .qlock = PTHREAD_MUTEX_INITIALIZER,
    .qdone = PTHREAD_COND_INITIALIZER,
};

static pthread_mutex_t log_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t client_lock = PTHREAD_MUTEX_INITIALIZER;

static long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

/* ---------------------------------------------------------------- paths */

const char *pbagent_bundle_dir(void) {
    if (!A.bundle[0]) {
        char exe[PATH_MAX];
        ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
        if (n > 0) {
            exe[n] = 0;
            char *slash = strrchr(exe, '/');
            if (slash) *slash = 0;
            snprintf(A.bundle, sizeof A.bundle, "%s", exe);
        } else {
            snprintf(A.bundle, sizeof A.bundle, ".");
        }
    }
    return A.bundle;
}

static int path_allowed(const char *p) {
    if (!p || p[0] != '/' || strstr(p, "/../") || strstr(p, "/./")) return 0;
    size_t n = strlen(p);
    if (n >= 3 && strcmp(p + n - 3, "/..") == 0) return 0;
    const char *roots[] = {"/mnt/ext1", "/mnt/ext2", "/tmp", pbagent_bundle_dir()};
    for (size_t i = 0; i < sizeof roots / sizeof roots[0]; i++) {
        size_t r = strlen(roots[i]);
        if (strncmp(p, roots[i], r) == 0 && (p[r] == 0 || p[r] == '/')) return 1;
    }
    return 0;
}

/* ------------------------------------------------------------------ log */

void pbagent_log(const char *fmt, ...) {
    char line[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);

    pthread_mutex_lock(&log_lock);
    char fallback[PATH_MAX];
    const char *path = A.log_path;
    if (!path[0]) {
        snprintf(fallback, sizeof fallback, "%s/pbagent.log", pbagent_bundle_dir());
        path = fallback;
    }
    FILE *f = fopen(path, "a");
    if (f) {
        fprintf(f, "%ld %s\n", now_ms(), line);
        fclose(f);
    }
    pthread_mutex_unlock(&log_lock);
}

/* ------------------------------------------------------- main-thread side */

static void tick(void);

static void arm_timer(void) {
    int active;
    pthread_mutex_lock(&A.qlock);
    active = A.qcount > 0 || A.clients > 0;
    pthread_mutex_unlock(&A.qlock);
    SetHardTimer("pbagent", tick, active ? TICK_ACTIVE_MS : TICK_IDLE_MS);
}

static void dispatch(const qitem *it) {
    if (it->type == Q_FULLUPDATE) {
        FullUpdate();
        return;
    }
    if (it->type == Q_CLOSEAPP) {
        CloseApp();
        return;
    }
    if (it->type == Q_CALIBRATE) {
        draw_calibration();
        return;
    }
    iv_handler h = GetEventHandler();
    if (!h) return;
    A.dispatching = 1;
    SendEvent(h, it->type, it->p1, it->p2);
    A.dispatching = 0;
}

static long started_ms;

/* Once, a few seconds in: the status panel silently moves the picture. */
static void check_panel(void) {
    static int checked;
    if (checked || now_ms() - started_ms < 3000) return;
    checked = 1;
    if (GetPanelType() != PANEL_DISABLED && PanelHeightFBOffset() > 0)
        pbagent_log("pbagent: WARNING the firmware status panel is on (type %d): the panel shows "
                    "the canvas moved down %d px with wrap-around. Call SetPanelType(PANEL_DISABLED) "
                    "on EVT_SHOW, or use `pbtk shot --whole` to see the real panel.",
                    GetPanelType(), PanelHeightFBOffset());
}

static void tick(void) {
    if (!A.running) return;
    check_panel();
    for (;;) {
        qitem it;
        int found = 0;
        long t = now_ms();
        pthread_mutex_lock(&A.qlock);
        /* The queue is ordered by due time; take the head when it is due. */
        if (A.qcount > 0 && A.queue[0].due_ms <= t) {
            it = A.queue[0];
            memmove(&A.queue[0], &A.queue[1], (size_t)(A.qcount - 1) * sizeof(qitem));
            A.qcount--;
            found = 1;
        }
        pthread_mutex_unlock(&A.qlock);
        if (!found) break;
        dispatch(&it);
        pthread_mutex_lock(&A.qlock);
        if (it.id > A.done_id) A.done_id = it.id;
        pthread_cond_broadcast(&A.qdone);
        pthread_mutex_unlock(&A.qlock);
        if (it.type == Q_CLOSEAPP) return;
    }
    arm_timer();
}

void pbagent_observe(int type, int par1, int par2) {
    if (A.dispatching) return;
    switch (type) {
    case EVT_POINTERDOWN: case EVT_POINTERUP: case EVT_POINTERMOVE:
    case EVT_POINTERLONG: case EVT_KEYPRESS: case EVT_KEYRELEASE:
    case EVT_SHOW: case EVT_EXIT:
        break;
    default:
        return;
    }
    /* Pointer moves are frequent; keep only every one that is far enough apart in time. */
    pthread_mutex_lock(&A.qlock);
    long t = now_ms();
    if (type == EVT_POINTERMOVE && A.obs_seq > 0) {
        observed *last = &A.obs[(A.obs_seq - 1) % OBSERVED_LEN];
        if (last->type == EVT_POINTERMOVE && t - last->ms < 50) {
            pthread_mutex_unlock(&A.qlock);
            return;
        }
    }
    observed *o = &A.obs[A.obs_seq % OBSERVED_LEN];
    o->seq = A.obs_seq++;
    o->ms = t;
    o->type = type;
    o->p1 = par1;
    o->p2 = par2;
    pthread_mutex_unlock(&A.qlock);
}

/* Queue events (sorted by due time). Returns the id of the last one, or -1. */
static long enqueue(const qitem *items, int n) {
    long last = -1;
    pthread_mutex_lock(&A.qlock);
    if (A.qcount + n > QUEUE_LEN) {
        pthread_mutex_unlock(&A.qlock);
        return -1;
    }
    for (int k = 0; k < n; k++) {
        qitem it = items[k];
        it.id = ++A.next_id;
        int i = A.qcount;
        while (i > 0 && A.queue[i - 1].due_ms > it.due_ms) {
            A.queue[i] = A.queue[i - 1];
            i--;
        }
        A.queue[i] = it;
        A.qcount++;
        last = it.id;
    }
    pthread_mutex_unlock(&A.qlock);
    return last;
}

/* Wait until the main thread has dispatched event `id`. Returns 1 when done. */
static int wait_done(long id, long extra_ms) {
    long deadline = now_ms() + DISPATCH_WAIT_MS + extra_ms;
    int done = 0;
    pthread_mutex_lock(&A.qlock);
    while (A.done_id < id && A.running) {
        long left = deadline - now_ms();
        if (left <= 0) break;
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        long ms = left > 100 ? 100 : left;
        ts.tv_nsec += ms * 1000000L;
        ts.tv_sec += ts.tv_nsec / 1000000000L;
        ts.tv_nsec %= 1000000000L;
        pthread_cond_timedwait(&A.qdone, &A.qlock, &ts);
    }
    done = A.done_id >= id;
    pthread_mutex_unlock(&A.qlock);
    return done;
}

/* --------------------------------------------------------- socket helpers */

static int write_all(int fd, const void *buf, size_t n) {
    const char *p = buf;
    while (n > 0) {
        ssize_t w = send(fd, p, n, MSG_NOSIGNAL);
        if (w < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += w;
        n -= (size_t)w;
    }
    return 0;
}

static int read_exact(int fd, void *buf, size_t n) {
    char *p = buf;
    while (n > 0) {
        ssize_t r = recv(fd, p, n, 0);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) return -1;
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

/* Reads one line (without \n). Returns length, or -1 on EOF/error. */
static int read_line(int fd, char *buf, int max) {
    int n = 0;
    for (;;) {
        char c;
        ssize_t r = recv(fd, &c, 1, 0);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) return -1;
        if (c == '\n') break;
        if (c == '\r') continue;
        if (n < max - 1) buf[n++] = c;
    }
    buf[n] = 0;
    return n;
}

static int reply_ok(int fd, const void *payload, size_t len, const char *extra) {
    char head[256];
    int h = snprintf(head, sizeof head, "ok %zu%s%s\n", len, extra && *extra ? " " : "",
                     extra ? extra : "");
    if (write_all(fd, head, (size_t)h) < 0) return -1;
    return len ? write_all(fd, payload, len) : 0;
}

static int reply_err(int fd, const char *fmt, ...) {
    char msg[256], line[300];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    int n = snprintf(line, sizeof line, "err %s\n", msg);
    return write_all(fd, line, (size_t)n);
}

/* ------------------------------------------------------------- commands */

/* Convert one row of `w` pixels at `depth` bits to 8-bit gray. -1 if unsupported. */
static int gray_row(const unsigned char *src, int depth, int w, unsigned char *row) {
    if (depth == 8) {
        memcpy(row, src, (size_t)w);
    } else if (depth == 4) {
        for (int x = 0; x < w; x++) {
            unsigned char b = src[x / 2];
            unsigned char v = (x & 1) ? (b & 0x0f) : (b >> 4);
            row[x] = (unsigned char)(v * 17);
        }
    } else if (depth == 16) {
        for (int x = 0; x < w; x++) {
            unsigned v = src[2 * x] | (src[2 * x + 1] << 8);
            int r = (v >> 11) << 3, g = ((v >> 5) & 63) << 2, b = (v & 31) << 3;
            row[x] = (unsigned char)((r * 299 + g * 587 + b * 114) / 1000);
        }
    } else if (depth >= 24) {
        int bpp = depth / 8;
        for (int x = 0; x < w; x++) {
            const unsigned char *p = src + x * bpp;
            row[x] = (unsigned char)((p[2] * 299 + p[1] * 587 + p[0] * 114) / 1000);
        }
    } else {
        return -1;
    }
    return 0;
}

/* Allocate a PGM with room for w*h pixels; returns the pixel pointer. */
static unsigned char *pgm_alloc(int w, int h, char **out, size_t *len) {
    char head[64];
    int hl = snprintf(head, sizeof head, "P5\n%d %d\n255\n", w, h);
    size_t total = (size_t)hl + (size_t)w * (size_t)h;
    char *buf = malloc(total);
    if (!buf) return NULL;
    memcpy(buf, head, (size_t)hl);
    *out = buf;
    *len = total;
    return (unsigned char *)buf + hl;
}

static int canvas_pgm(char **out, size_t *len, int *w_out, int *h_out) {
    icanvas *c = GetCanvas();
    if (!c || !c->addr || c->width <= 0 || c->height <= 0) return -1;
    int w = c->width, h = c->height;
    unsigned char *dst = pgm_alloc(w, h, out, len);
    if (!dst) return -1;
    for (int y = 0; y < h; y++) {
        if (gray_row(c->addr + (size_t)y * (size_t)c->scanline, c->depth, w,
                     dst + (size_t)y * (size_t)w) != 0) {
            free(*out);
            return -1;
        }
    }
    if (w_out) *w_out = w;
    if (h_out) *h_out = h;
    return 0;
}

/* ---- whole-screen capture from the framebuffer (fbshot) ----
 * On the PB628 /dev/fb0 is readable by apps (uid reader) and holds what the
 * panel shows, including firmware UI, as an 8-bit 1024x758 landscape buffer
 * for the portrait panel; turning it 90° clockwise gives the panel as held
 * (docs/FACTS.md). $PBAGENT_FB and $PBAGENT_FB_SYS override the device and
 * its sysfs directory (used by host tests). */

static int read_sys(const char *dir, const char *name, char *buf, size_t n) {
    char path[PATH_MAX];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    int ok = fgets(buf, (int)n, f) != NULL;
    fclose(f);
    if (!ok) return -1;
    buf[strcspn(buf, "\r\n")] = 0;
    return 0;
}

typedef struct {
    int xres, yres;   /* visible */
    int vxres, vyres; /* virtual (allocated) */
    int xoff, yoff;   /* pan: where the visible window starts in the virtual buffer */
    int bpp, line;    /* bits per pixel, bytes per line */
    int rotate;       /* driver rotation hint (informational) */
    const char *source;
} fbgeo;

static const char *fb_dev(void) {
    const char *d = getenv("PBAGENT_FB");
    return d ? d : "/dev/fb0";
}

static const char *fb_sysdir(void) {
    const char *s = getenv("PBAGENT_FB_SYS");
    return s ? s : "/sys/class/graphics/fb0";
}

/* Geometry from the driver (ioctl) when possible, else from sysfs. */
static int fb_geometry(int fd, fbgeo *g, char *err, size_t errn) {
    memset(g, 0, sizeof *g);
    struct fb_var_screeninfo var;
    struct fb_fix_screeninfo fix;
    if (fd >= 0 && ioctl(fd, FBIOGET_VSCREENINFO, &var) == 0 &&
        ioctl(fd, FBIOGET_FSCREENINFO, &fix) == 0) {
        g->xres = (int)var.xres;
        g->yres = (int)var.yres;
        g->vxres = (int)var.xres_virtual;
        g->vyres = (int)var.yres_virtual;
        g->xoff = (int)var.xoffset;
        g->yoff = (int)var.yoffset;
        g->bpp = (int)var.bits_per_pixel;
        g->line = (int)fix.line_length;
        g->rotate = (int)var.rotate;
        g->source = "ioctl";
    } else {
        const char *sys = fb_sysdir();
        char v[64], b[32], s[32], p[64], r[32];
        if (read_sys(sys, "virtual_size", v, sizeof v) != 0 ||
            sscanf(v, "%d,%d", &g->vxres, &g->vyres) != 2 ||
            read_sys(sys, "bits_per_pixel", b, sizeof b) != 0 || (g->bpp = atoi(b)) <= 0) {
            snprintf(err, errn, "framebuffer geometry unavailable (no ioctl, no %s)", sys);
            return -1;
        }
        g->xres = g->vxres;
        g->yres = g->vyres;
        g->line = read_sys(sys, "stride", s, sizeof s) == 0 ? atoi(s) : 0;
        if (read_sys(sys, "pan", p, sizeof p) != 0 || sscanf(p, "%d,%d", &g->xoff, &g->yoff) != 2)
            g->xoff = g->yoff = 0;
        g->rotate = read_sys(sys, "rotate", r, sizeof r) == 0 ? atoi(r) : 0;
        g->source = "sysfs";
    }
    if (g->vxres < g->xres) g->vxres = g->xres;
    if (g->vyres < g->yres) g->vyres = g->yres;
    if (g->line <= 0) g->line = g->vxres * (g->bpp >= 8 ? g->bpp / 8 : 1);
    if (g->xres <= 0 || g->yres <= 0 || g->vxres > 8192 || g->vyres > 16384) {
        snprintf(err, errn, "bad framebuffer size %dx%d (virtual %dx%d)", g->xres, g->yres,
                 g->vxres, g->vyres);
        return -1;
    }
    return 0;
}

static int cmd_fbinfo(int fd) {
    char err[160] = "", buf[512];
    fbgeo g;
    int dfd = open(fb_dev(), O_RDONLY | O_CLOEXEC);
    int rc = fb_geometry(dfd, &g, err, sizeof err);
    if (dfd >= 0) close(dfd);
    if (rc != 0) return reply_err(fd, "%s", err);
    int n = snprintf(buf, sizeof buf,
                     "source=%s\ndevice=%s\nxres=%d\nyres=%d\nxres_virtual=%d\nyres_virtual=%d\n"
                     "xoffset=%d\nyoffset=%d\nbits_per_pixel=%d\nline_length=%d\nrotate=%d\n",
                     g.source, fb_dev(), g.xres, g.yres, g.vxres, g.vyres, g.xoff, g.yoff, g.bpp,
                     g.line, g.rotate);
    return reply_ok(fd, buf, (size_t)n, NULL);
}

/* rotate: 0 = as stored, 1 = 90° clockwise, 3 = 90° counter-clockwise,
 *         -1 = auto (clockwise when the buffer is landscape).
 * `g_out` receives the geometry used, for the reply fields. */
static int fb_pgm(int rotate, char **out, size_t *len, int *w_out, int *h_out, fbgeo *g_out,
                  char *err, size_t errn) {
    const char *dev = fb_dev();
    int fd = open(dev, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        snprintf(err, errn, "read %s: %s", dev, strerror(errno));
        return -1;
    }
    fbgeo g;
    if (fb_geometry(fd, &g, err, errn) != 0) {
        close(fd);
        return -1;
    }
    *g_out = g;
    /* Read the whole virtual buffer, then cut out the visible (panned) window. */
    size_t raw_len = (size_t)g.line * (size_t)g.vyres;
    unsigned char *raw = malloc(raw_len);
    unsigned char *vrow = malloc((size_t)g.vxres);
    int fw = g.xres, fh = g.yres;
    unsigned char *gray = malloc((size_t)fw * (size_t)fh);
    if (!raw || !vrow || !gray) {
        free(raw);
        free(vrow);
        free(gray);
        close(fd);
        snprintf(err, errn, "oom");
        return -1;
    }
    size_t got = 0;
    while (got < raw_len) {
        ssize_t r = read(fd, raw + got, raw_len - got);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) break;
        got += (size_t)r;
    }
    close(fd);
    /* At least the visible lines must be there; pans wrap within what was read. */
    size_t need = (size_t)g.line * (size_t)g.yres;
    if (got < need) {
        snprintf(err, errn, "read %s: short read (%zu of %zu bytes)", dev, got, need);
        free(raw);
        free(vrow);
        free(gray);
        return -1;
    }
    int rows_avail = (int)(got / (size_t)g.line);
    for (int y = 0; y < fh; y++) {
        int vy = (y + g.yoff) % rows_avail;
        if (gray_row(raw + (size_t)vy * (size_t)g.line, g.bpp, g.vxres, vrow) != 0) {
            snprintf(err, errn, "unsupported depth %d", g.bpp);
            free(raw);
            free(vrow);
            free(gray);
            return -1;
        }
        unsigned char *dst = gray + (size_t)y * (size_t)fw;
        for (int x = 0; x < fw; x++) dst[x] = vrow[(x + g.xoff) % g.vxres];
    }
    free(raw);
    free(vrow);

    if (rotate < 0) rotate = fw > fh ? 1 : 0;
    int w = (rotate == 1 || rotate == 3) ? fh : fw;
    int h = (rotate == 1 || rotate == 3) ? fw : fh;
    unsigned char *dst = pgm_alloc(w, h, out, len);
    if (!dst) {
        free(gray);
        snprintf(err, errn, "oom");
        return -1;
    }
    for (int ny = 0; ny < h; ny++) {
        unsigned char *row = dst + (size_t)ny * (size_t)w;
        for (int nx = 0; nx < w; nx++) {
            int sx, sy;
            if (rotate == 1) {        /* clockwise */
                sx = ny;
                sy = fh - 1 - nx;
            } else if (rotate == 3) { /* counter-clockwise */
                sx = fw - 1 - ny;
                sy = nx;
            } else {
                sx = nx;
                sy = ny;
            }
            row[nx] = gray[(size_t)sy * (size_t)fw + (size_t)sx];
        }
    }
    free(gray);
    if (w_out) *w_out = w;
    if (h_out) *h_out = h;
    return 0;
}

static int key_code(const char *name) {
    static const struct { const char *n; int c; } keys[] = {
        {"ok", IV_KEY_OK},       {"up", IV_KEY_UP},       {"down", IV_KEY_DOWN},
        {"left", IV_KEY_LEFT},   {"right", IV_KEY_RIGHT}, {"menu", IV_KEY_MENU},
        {"prev", IV_KEY_PREV},   {"next", IV_KEY_NEXT},   {"home", IV_KEY_HOME},
        {"back", IV_KEY_BACK},   {"prev2", IV_KEY_PREV2}, {"next2", IV_KEY_NEXT2},
    };
    for (size_t i = 0; i < sizeof keys / sizeof keys[0]; i++)
        if (strcasecmp(name, keys[i].n) == 0) return keys[i].c;
    char *end;
    long v = strtol(name, &end, 0);
    return (*end == 0 && v > 0 && v < 0x10000) ? (int)v : -1;
}

/* Queue the items, wait for them, and reply. */
static int inject(int fd, qitem *items, int n) {
    long base = now_ms(), span = 0;
    for (int i = 0; i < n; i++) {
        if (items[i].due_ms > span) span = items[i].due_ms;
        items[i].due_ms += base;
    }
    long id = enqueue(items, n);
    if (id < 0) return reply_err(fd, "queue full");
    int done = wait_done(id, span);
    return reply_ok(fd, NULL, 0, done ? "dispatched=1" : "dispatched=0");
}

static int cmd_info(int fd) {
    char buf[1024];
    icanvas *c = GetCanvas();
    /* panel_*: the firmware status panel. While it is on, the panel shows the
     * canvas moved by panel_fb_offset rows with wrap-around, so `shot` (canvas)
     * and what the person sees differ; `fbshot` is then the faithful one. */
    int n = snprintf(buf, sizeof buf,
                     "agent=%s\napp=%s\npid=%d\nport=%d\nwidth=%d\nheight=%d\ndepth=%d\n"
                     "orientation=%d\nmodel=%s\nfirmware=%s\nbundle=%s\nlog=%s\n"
                     "panel_type=%d\npanel_height=%d\npanel_fb_offset=%d\n",
                     PBAGENT_VERSION, A.app_name, (int)getpid(), A.port, c ? c->width : 0,
                     c ? c->height : 0, c ? c->depth : 0, GetOrientation(), A.model,
                     A.firmware, pbagent_bundle_dir(), A.log_path, GetPanelType(), PanelHeight(),
                     PanelHeightFBOffset());
    return reply_ok(fd, buf, (size_t)n, NULL);
}

static int cmd_events(int fd, long since) {
    size_t cap = OBSERVED_LEN * 48 + 1, len = 0;
    char *buf = malloc(cap);
    if (!buf) return reply_err(fd, "oom");
    pthread_mutex_lock(&A.qlock);
    long first = A.obs_seq - OBSERVED_LEN;
    if (first < 0) first = 0;
    if (since < first) since = first;
    for (long s = since; s < A.obs_seq; s++) {
        observed *o = &A.obs[s % OBSERVED_LEN];
        len += (size_t)snprintf(buf + len, cap - len, "%ld %ld %d %d %d\n", o->seq, o->ms,
                                o->type, o->p1, o->p2);
    }
    long next = A.obs_seq;
    pthread_mutex_unlock(&A.qlock);
    char extra[64];
    snprintf(extra, sizeof extra, "next=%ld now=%ld", next, now_ms());
    int rc = reply_ok(fd, buf, len, extra);
    free(buf);
    return rc;
}

static int cmd_log(int fd, long offset, long max) {
    struct stat st;
    if (!A.log_path[0] || stat(A.log_path, &st) != 0) return reply_ok(fd, NULL, 0, "size=0");
    long size = (long)st.st_size;
    if (offset < 0) offset = size + offset < 0 ? 0 : size + offset; /* negative = tail */
    if (offset > size) offset = 0;                                  /* log was truncated */
    if (max <= 0 || max > 1 << 20) max = 1 << 20;
    long n = size - offset < max ? size - offset : max;
    char extra[64];
    snprintf(extra, sizeof extra, "size=%ld offset=%ld", size, offset);
    if (n <= 0) return reply_ok(fd, NULL, 0, extra);
    char *buf = malloc((size_t)n);
    FILE *f = fopen(A.log_path, "rb");
    long got = 0;
    if (buf && f && fseek(f, offset, SEEK_SET) == 0) got = (long)fread(buf, 1, (size_t)n, f);
    if (f) fclose(f);
    int rc = reply_ok(fd, buf, (size_t)(got > 0 ? got : 0), extra);
    free(buf);
    return rc;
}

static int cmd_ls(int fd, const char *path) {
    if (!path_allowed(path)) return reply_err(fd, "path not allowed");
    DIR *d = opendir(path);
    if (!d) return reply_err(fd, "%s", strerror(errno));
    size_t cap = 4096, len = 0;
    char *buf = malloc(cap);
    struct dirent *e;
    while (buf && (e = readdir(d))) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
        char full[PATH_MAX];
        struct stat st;
        snprintf(full, sizeof full, "%s/%s", path, e->d_name);
        if (stat(full, &st) != 0) continue;
        size_t need = strlen(e->d_name) + 64;
        if (len + need > cap) {
            cap = cap * 2 + need;
            char *nb = realloc(buf, cap);
            if (!nb) break;
            buf = nb;
        }
        len += (size_t)snprintf(buf + len, cap - len, "%c %lld %lld %s\n",
                                S_ISDIR(st.st_mode) ? 'd' : 'f', (long long)st.st_size,
                                (long long)st.st_mtime, e->d_name);
    }
    closedir(d);
    int rc = reply_ok(fd, buf, len, NULL);
    free(buf);
    return rc;
}

static int cmd_get(int fd, const char *path) {
    if (!path_allowed(path)) return reply_err(fd, "path not allowed");
    FILE *f = fopen(path, "rb");
    if (!f) return reply_err(fd, "%s", strerror(errno));
    struct stat st;
    if (fstat(fileno(f), &st) != 0 || !S_ISREG(st.st_mode) || st.st_size > (64 << 20)) {
        fclose(f);
        return reply_err(fd, "not a regular file under 64 MB");
    }
    size_t n = (size_t)st.st_size;
    char *buf = malloc(n ? n : 1);
    size_t got = buf ? fread(buf, 1, n, f) : 0;
    fclose(f);
    int rc = buf ? reply_ok(fd, buf, got, NULL) : reply_err(fd, "oom");
    free(buf);
    return rc;
}

static int cmd_put(int fd, const char *path, long len) {
    if (len < 0 || len > (64L << 20)) return reply_err(fd, "bad length");
    char *buf = malloc((size_t)(len ? len : 1));
    if (!buf) return -1;
    if (read_exact(fd, buf, (size_t)len) < 0) {
        free(buf);
        return -1;
    }
    if (!path_allowed(path)) {
        free(buf);
        return reply_err(fd, "path not allowed");
    }
    char tmp[PATH_MAX];
    snprintf(tmp, sizeof tmp, "%s.pbtk-tmp", path);
    FILE *f = fopen(tmp, "wb");
    int ok = f && fwrite(buf, 1, (size_t)len, f) == (size_t)len;
    if (f) ok = (fclose(f) == 0) && ok;
    free(buf);
    if (!ok || rename(tmp, path) != 0) {
        unlink(tmp);
        return reply_err(fd, "write failed: %s", strerror(errno));
    }
    return reply_ok(fd, NULL, 0, NULL);
}

static int handle(int fd, char *line) {
    char *argv[8];
    int argc = 0;
    char *save = NULL;
    /* `put` keeps spaces in its path out of scope: paths with spaces are rejected by design. */
    for (char *tok = strtok_r(line, " ", &save); tok && argc < 8; tok = strtok_r(NULL, " ", &save))
        argv[argc++] = tok;
    if (argc == 0) return 0;
    const char *cmd = argv[0];
#define ARGI(i, def) (argc > (i) ? atoi(argv[i]) : (def))

    if (!strcmp(cmd, "ping")) return reply_ok(fd, NULL, 0, NULL);
    if (!strcmp(cmd, "info") || !strcmp(cmd, "hello")) return cmd_info(fd);
    if (!strcmp(cmd, "shot")) {
        char *pgm;
        size_t len;
        int w, h;
        if (canvas_pgm(&pgm, &len, &w, &h) != 0) return reply_err(fd, "no canvas");
        char extra[48];
        snprintf(extra, sizeof extra, "width=%d height=%d", w, h);
        int rc = reply_ok(fd, pgm, len, extra);
        free(pgm);
        return rc;
    }
    if (!strcmp(cmd, "fbinfo")) return cmd_fbinfo(fd);
    if (!strcmp(cmd, "fbshot")) {
        const char *mode = argc > 1 ? argv[1] : "auto";
        int rot = !strcmp(mode, "auto") ? -1 : !strcmp(mode, "raw") ? 0
                  : !strcmp(mode, "cw") ? 1 : !strcmp(mode, "ccw") ? 3 : -2;
        if (rot == -2) return reply_err(fd, "fbshot mode must be auto, raw, cw or ccw");
        char *pgm, err[160] = "";
        size_t len;
        int w, h;
        fbgeo g;
        if (fb_pgm(rot, &pgm, &len, &w, &h, &g, err, sizeof err) != 0)
            return reply_err(fd, "%s", err);
        char extra[160];
        snprintf(extra, sizeof extra, "width=%d height=%d source=fb geo=%s xoffset=%d yoffset=%d",
                 w, h, g.source, g.xoff, g.yoff);
        int rc = reply_ok(fd, pgm, len, extra);
        free(pgm);
        return rc;
    }
    if (!strcmp(cmd, "tap") && argc >= 3) {
        int x = ARGI(1, 0), y = ARGI(2, 0), hold = ARGI(3, 60);
        qitem q[2] = {{0, 0, EVT_POINTERDOWN, x, y}, {hold, 0, EVT_POINTERUP, x, y}};
        return inject(fd, q, 2);
    }
    if (!strcmp(cmd, "long") && argc >= 3) {
        int x = ARGI(1, 0), y = ARGI(2, 0);
        qitem q[3] = {{0, 0, EVT_POINTERDOWN, x, y},
                      {600, 0, EVT_POINTERLONG, x, y},
                      {900, 0, EVT_POINTERUP, x, y}};
        return inject(fd, q, 3);
    }
    if ((!strcmp(cmd, "down") || !strcmp(cmd, "move") || !strcmp(cmd, "up")) && argc >= 3) {
        int t = !strcmp(cmd, "down") ? EVT_POINTERDOWN
                : !strcmp(cmd, "up") ? EVT_POINTERUP : EVT_POINTERMOVE;
        qitem q[1] = {{0, 0, t, ARGI(1, 0), ARGI(2, 0)}};
        return inject(fd, q, 1);
    }
    if (!strcmp(cmd, "swipe") && argc >= 5) {
        int x1 = ARGI(1, 0), y1 = ARGI(2, 0), x2 = ARGI(3, 0), y2 = ARGI(4, 0);
        int ms = ARGI(5, 300), steps = 8;
        qitem q[10];
        q[0] = (qitem){0, 0, EVT_POINTERDOWN, x1, y1};
        for (int i = 1; i <= steps; i++)
            q[i] = (qitem){(long)ms * i / (steps + 1), 0, EVT_POINTERMOVE,
                           x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps};
        q[steps + 1] = (qitem){ms, 0, EVT_POINTERUP, x2, y2};
        return inject(fd, q, steps + 2);
    }
    if (!strcmp(cmd, "key") && argc >= 2) {
        int code = key_code(argv[1]), hold = ARGI(2, 80);
        if (code < 0) return reply_err(fd, "unknown key %s", argv[1]);
        qitem q[2] = {{0, 0, EVT_KEYPRESS, code, 0}, {hold, 0, EVT_KEYRELEASE, code, 0}};
        return inject(fd, q, 2);
    }
    if (!strcmp(cmd, "event") && argc >= 2) {
        qitem q[1] = {{0, 0, ARGI(1, 0), ARGI(2, 0), ARGI(3, 0)}};
        if (q[0].type < 0) return reply_err(fd, "bad event type");
        return inject(fd, q, 1);
    }
    if (!strcmp(cmd, "calibrate")) {
        /* `calibrate` draws the pattern; `calibrate off` asks the app to repaint (EVT_SHOW). */
        int off = argc > 1 && !strcmp(argv[1], "off");
        qitem q[1] = {{0, 0, off ? EVT_SHOW : Q_CALIBRATE, 0, 0}};
        return inject(fd, q, 1);
    }
    if (!strcmp(cmd, "refresh")) {
        qitem q[1] = {{0, 0, Q_FULLUPDATE, 0, 0}};
        return inject(fd, q, 1);
    }
    if (!strcmp(cmd, "exit")) {
        qitem q[1] = {{0, 0, Q_CLOSEAPP, 0, 0}};
        long base = now_ms();
        q[0].due_ms = base + 100; /* let the reply leave first */
        if (enqueue(q, 1) < 0) return reply_err(fd, "queue full");
        return reply_ok(fd, NULL, 0, NULL);
    }
    if (!strcmp(cmd, "events")) return cmd_events(fd, argc > 1 ? atol(argv[1]) : 0);
    if (!strcmp(cmd, "log"))
        return cmd_log(fd, argc > 1 ? atol(argv[1]) : 0, argc > 2 ? atol(argv[2]) : 0);
    if (!strcmp(cmd, "ls") && argc >= 2) return cmd_ls(fd, argv[1]);
    if (!strcmp(cmd, "get") && argc >= 2) return cmd_get(fd, argv[1]);
    if (!strcmp(cmd, "put") && argc >= 3) return cmd_put(fd, argv[1], atol(argv[2]));
    return reply_err(fd, "unknown command %s", cmd);
#undef ARGI
}

/* --------------------------------------------------------------- threads */

static void *client_thread(void *arg) {
    int fd = (int)(long)arg;
    char line[LINE_MAX_LEN];
    int authed = 0;
    while (A.running && read_line(fd, line, sizeof line) >= 0) {
        if (!authed) {
            if (strncmp(line, "auth ", 5) == 0 && strcmp(line + 5, A.token) == 0) {
                authed = 1;
                if (reply_ok(fd, NULL, 0, "agent=" PBAGENT_VERSION) < 0) break;
            } else {
                reply_err(fd, "auth required");
                pbagent_log("pbagent: rejected a client without a valid token");
                break;
            }
            continue;
        }
        if (handle(fd, line) < 0) break;
    }
    close(fd);
    pthread_mutex_lock(&client_lock);
    for (int i = 0; i < MAX_CLIENTS; i++)
        if (A.client_fds[i] == fd) A.client_fds[i] = -1;
    pthread_mutex_unlock(&client_lock);
    pthread_mutex_lock(&A.qlock);
    A.clients--;
    pthread_mutex_unlock(&A.qlock);
    return NULL;
}

static void send_beacon(void) {
    if (A.beacon_fd < 0) return;
    char msg[128];
    int n = snprintf(msg, sizeof msg, "pbagent %s app=%s port=%d", PBAGENT_VERSION, A.app_name,
                     A.port);
    struct sockaddr_in to = {0};
    to.sin_family = AF_INET;
    to.sin_port = htons((unsigned short)A.port);
    to.sin_addr.s_addr = htonl(INADDR_BROADCAST);
    sendto(A.beacon_fd, msg, (size_t)n, 0, (struct sockaddr *)&to, sizeof to);
}

static void *acceptor_thread(void *arg) {
    (void)arg;
    long last_beacon = 0;
    while (A.running) {
        long t = now_ms();
        if (t - last_beacon >= BEACON_MS) {
            send_beacon();
            last_beacon = t;
        }
        struct pollfd p = {A.listen_fd, POLLIN, 0};
        int r = poll(&p, 1, 500);
        if (r <= 0 || !A.running) continue;
        int fd = accept(A.listen_fd, NULL, NULL);
        if (fd < 0) continue;
        pthread_mutex_lock(&client_lock);
        int slot = -1;
        for (int i = 0; i < MAX_CLIENTS; i++)
            if (A.client_fds[i] < 0) {
                slot = i;
                break;
            }
        if (slot >= 0) A.client_fds[slot] = fd;
        pthread_mutex_unlock(&client_lock);
        if (slot < 0) {
            reply_err(fd, "too many clients");
            close(fd);
            continue;
        }
        pthread_mutex_lock(&A.qlock);
        A.clients++;
        pthread_mutex_unlock(&A.qlock);
        pthread_t th;
        if (pthread_create(&th, NULL, client_thread, (void *)(long)fd) == 0) {
            pthread_detach(th);
        } else {
            close(fd);
            pthread_mutex_lock(&client_lock);
            A.client_fds[slot] = -1;
            pthread_mutex_unlock(&client_lock);
            pthread_mutex_lock(&A.qlock);
            A.clients--;
            pthread_mutex_unlock(&A.qlock);
        }
    }
    return NULL;
}

/* ----------------------------------------------------------- start/stop */

static int read_token_file(const char *path, char *out, size_t n) {
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char buf[256] = {0};
    if (!fgets(buf, sizeof buf, f)) buf[0] = 0;
    fclose(f);
    buf[strcspn(buf, "\r\n \t")] = 0;
    if (strlen(buf) < 16) return 0;
    snprintf(out, n, "%s", buf);
    return 1;
}

static int find_token(const pbagent_config *cfg) {
    if (cfg->token && strlen(cfg->token) >= 16) {
        snprintf(A.token, sizeof A.token, "%s", cfg->token);
        return 1;
    }
    const char *env = getenv("PBAGENT_TOKEN");
    if (env && strlen(env) >= 16) {
        snprintf(A.token, sizeof A.token, "%s", env);
        return 1;
    }
    char path[PATH_MAX];
    snprintf(path, sizeof path, "%s/pbagent.token", pbagent_bundle_dir());
    if (read_token_file(path, A.token, sizeof A.token)) return 1;
    snprintf(path, sizeof path, "%s/../pbagent.token", pbagent_bundle_dir());
    if (read_token_file(path, A.token, sizeof A.token)) return 1;
    return read_token_file("/mnt/ext1/applications/pbagent.token", A.token, sizeof A.token);
}

int pbagent_start(const pbagent_config *cfg) {
    if (A.running) return 0;
    if (!cfg || !cfg->app_name) return -1;
    snprintf(A.app_name, sizeof A.app_name, "%s", cfg->app_name);
    if (cfg->log_path)
        snprintf(A.log_path, sizeof A.log_path, "%s", cfg->log_path);
    else
        snprintf(A.log_path, sizeof A.log_path, "%s/%s.log", pbagent_bundle_dir(), A.app_name);
    A.port = cfg->port > 0 ? cfg->port : PBAGENT_DEFAULT_PORT;
    const char *port_env = getenv("PBAGENT_PORT");
    if (port_env && atoi(port_env) > 0) A.port = atoi(port_env);
    A.no_beacon = cfg->no_beacon;
    const char *m = GetDeviceModel();
    const char *fw = GetSoftwareVersion();
    snprintf(A.model, sizeof A.model, "%s", m ? m : "");
    snprintf(A.firmware, sizeof A.firmware, "%s", fw ? fw : "");
    for (int i = 0; i < MAX_CLIENTS; i++) A.client_fds[i] = -1;

    if (!find_token(cfg)) {
        pbagent_log("pbagent: off — no token (run `pbtk token install` over USB)");
        return -1;
    }
    if (cfg->connect_wifi) {
        long t0 = now_ms();
        int rc = NetConnect(NULL);
        pbagent_log("pbagent: NetConnect -> %d (%ld ms)", rc, now_ms() - t0);
    }

    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        pbagent_log("pbagent: socket: %s", strerror(errno));
        return -1;
    }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)A.port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) != 0 || listen(fd, 4) != 0) {
        pbagent_log("pbagent: bind/listen on %d: %s", A.port, strerror(errno));
        close(fd);
        return -1;
    }
    A.listen_fd = fd;
    if (!A.no_beacon) {
        A.beacon_fd = socket(AF_INET, SOCK_DGRAM, 0);
        if (A.beacon_fd >= 0) {
            setsockopt(A.beacon_fd, SOL_SOCKET, SO_BROADCAST, &one, sizeof one);
            fcntl(A.beacon_fd, F_SETFD, FD_CLOEXEC);
        }
    }
    A.running = 1;
    if (pthread_create(&A.acceptor, NULL, acceptor_thread, NULL) != 0) {
        A.running = 0;
        close(fd);
        A.listen_fd = -1;
        pbagent_log("pbagent: thread: %s", strerror(errno));
        return -1;
    }
    started_ms = now_ms();
    SetHardTimer("pbagent", tick, TICK_IDLE_MS);
    pbagent_log("pbagent: listening on port %d (app %s)", A.port, A.app_name);
    return 0;
}

void pbagent_stop(void) {
    if (!A.running) return;
    A.running = 0;
    ClearTimer(tick);
    pthread_mutex_lock(&client_lock);
    for (int i = 0; i < MAX_CLIENTS; i++)
        if (A.client_fds[i] >= 0) shutdown(A.client_fds[i], SHUT_RDWR);
    pthread_mutex_unlock(&client_lock);
    pthread_mutex_lock(&A.qlock);
    pthread_cond_broadcast(&A.qdone);
    pthread_mutex_unlock(&A.qlock);
    pthread_join(A.acceptor, NULL);
    if (A.listen_fd >= 0) close(A.listen_fd);
    if (A.beacon_fd >= 0) close(A.beacon_fd);
    A.listen_fd = A.beacon_fd = -1;
    pbagent_log("pbagent: stopped");
}
