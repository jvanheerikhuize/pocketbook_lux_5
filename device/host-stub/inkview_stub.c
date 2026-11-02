/*
 * Host stand-in for InkView: an 8-bit gray canvas in memory, a single-threaded
 * event loop with timers, and crude drawing (text is drawn as one block per
 * character). Good enough to run an app and its agent on the PC.
 *
 * Environment: PBTK_STUB_SIZE=WxH (default 758x1024, the PB628 panel).
 * SIGINT/SIGTERM close the app cleanly (EVT_EXIT is sent).
 */
#define _GNU_SOURCE
#include "inkview.h"

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_TIMERS 32

static icanvas canvas;
static iv_handler current;
static volatile sig_atomic_t closing;
static ifont stub_font = {.size = 24, .height = 24};
static const ifont *cur_font = &stub_font;
static int font_color = BLACK;

static struct {
    iv_timerproc proc;
    long due;
} timers[MAX_TIMERS];

static long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static unsigned char gray(int color) {
    int r = (color >> 16) & 255, g = (color >> 8) & 255, b = color & 255;
    return (unsigned char)((r * 299 + g * 587 + b * 114) / 1000);
}

static void ensure_canvas(void) {
    if (canvas.addr) return;
    int w = 758, h = 1024;
    const char *s = getenv("PBTK_STUB_SIZE");
    if (s) sscanf(s, "%dx%d", &w, &h);
    canvas.width = w;
    canvas.height = h;
    canvas.scanline = w;
    canvas.depth = 8;
    canvas.clipx2 = w - 1;
    canvas.clipy2 = h - 1;
    canvas.addr = malloc((size_t)w * (size_t)h);
    memset(canvas.addr, 255, (size_t)w * (size_t)h);
}

static void on_signal(int sig) {
    (void)sig;
    closing = 1;
}

/* Simulated physical input: when $PBTK_STUB_INPUT names a file, its lines
 * ("tap X Y" or "key CODE") are delivered to the handler like real touches
 * and key presses (i.e. not through the agent), then the file is removed. */
static void poll_user_input(void) {
    const char *path = getenv("PBTK_STUB_INPUT");
    if (!path) return;
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[128];
    while (fgets(line, sizeof line, f)) {
        int a, b;
        if (sscanf(line, "tap %d %d", &a, &b) == 2) {
            current(EVT_POINTERDOWN, a, b);
            current(EVT_POINTERUP, a, b);
        } else if (sscanf(line, "key %i", &a) == 1) {
            current(EVT_KEYPRESS, a, 0);
            current(EVT_KEYRELEASE, a, 0);
        }
    }
    fclose(f);
    unlink(path);
}

static void deliver_sent(void);

void InkViewMain(iv_handler h) {
    ensure_canvas();
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);
    current = h;
    h(EVT_INIT, 0, 0);
    h(EVT_SHOW, 0, 0);
    while (!closing) {
        long t = now_ms();
        for (int i = 0; i < MAX_TIMERS; i++) {
            if (timers[i].proc && timers[i].due <= t) {
                iv_timerproc p = timers[i].proc;
                timers[i].proc = NULL; /* one-shot, as in InkView */
                p();
                deliver_sent();
            }
        }
        poll_user_input();
        usleep(5000);
    }
    current(EVT_EXIT, 0, 0);
}

void CloseApp(void) { closing = 1; }

iv_handler SetEventHandler(iv_handler h) {
    iv_handler old = current;
    current = h;
    if (h) h(EVT_SHOW, 0, 0);
    return old;
}

iv_handler GetEventHandler(void) { return current; }

/* As on the reader, SendEvent() only queues: the handler runs from the main
 * loop after the current timer callback has returned. */
#define MAX_SENT 256
static struct {
    iv_handler h;
    int type, p1, p2;
} sent[MAX_SENT];
static int sent_count;

void SendEvent(iv_handler h, int type, int par1, int par2) {
    if (!h || sent_count == MAX_SENT) return;
    sent[sent_count].h = h;
    sent[sent_count].type = type;
    sent[sent_count].p1 = par1;
    sent[sent_count].p2 = par2;
    sent_count++;
}

static void deliver_sent(void) {
    for (int i = 0; i < sent_count; i++) sent[i].h(sent[i].type, sent[i].p1, sent[i].p2);
    sent_count = 0;
}

void SetHardTimer(const char *name, iv_timerproc proc, int ms) {
    (void)name;
    int free_slot = -1;
    for (int i = 0; i < MAX_TIMERS; i++) {
        if (timers[i].proc == proc) {
            timers[i].due = now_ms() + ms;
            return;
        }
        if (!timers[i].proc && free_slot < 0) free_slot = i;
    }
    if (free_slot >= 0) {
        timers[free_slot].proc = proc;
        timers[free_slot].due = now_ms() + ms;
    }
}

void SetWeakTimer(const char *name, iv_timerproc proc, int ms) { SetHardTimer(name, proc, ms); }

void ClearTimer(iv_timerproc proc) {
    for (int i = 0; i < MAX_TIMERS; i++)
        if (timers[i].proc == proc) timers[i].proc = NULL;
}

icanvas *GetCanvas(void) {
    ensure_canvas();
    return &canvas;
}
int ScreenWidth(void) { return GetCanvas()->width; }
int ScreenHeight(void) { return GetCanvas()->height; }
int GetOrientation(void) { return 0; }
void SetOrientation(int n) { (void)n; }
char *GetDeviceModel(void) { return "host-stub"; }
char *GetSoftwareVersion(void) { return "stub"; }
static int panel_type = PANEL_ENABLED; /* on by default, as on the reader */
void SetPanelType(int type) { panel_type = type; }
int GetPanelType(void) { return panel_type; }
int PanelHeight(void) { return panel_type ? 75 : 0; }
int PanelHeightFBOffset(void) { return panel_type ? 75 : 0; }

void BanSleep(int sec) { fprintf(stderr, "stub: BanSleep(%d)\n", sec); }

int NetConnect(const char *name) {
    (void)name;
    return 0;
}

void FillArea(int x, int y, int w, int h, int color) {
    ensure_canvas();
    unsigned char g = gray(color);
    for (int yy = y < 0 ? 0 : y; yy < y + h && yy < canvas.height; yy++)
        for (int xx = x < 0 ? 0 : x; xx < x + w && xx < canvas.width; xx++)
            canvas.addr[yy * canvas.scanline + xx] = g;
}

void ClearScreen(void) { FillArea(0, 0, canvas.width, canvas.height, WHITE); }

void DrawRect(int x, int y, int w, int h, int color) {
    FillArea(x, y, w, 1, color);
    FillArea(x, y + h - 1, w, 1, color);
    FillArea(x, y, 1, h, color);
    FillArea(x + w - 1, y, 1, h, color);
}

void InvertArea(int x, int y, int w, int h) {
    ensure_canvas();
    for (int yy = y < 0 ? 0 : y; yy < y + h && yy < canvas.height; yy++)
        for (int xx = x < 0 ? 0 : x; xx < x + w && xx < canvas.width; xx++)
            canvas.addr[yy * canvas.scanline + xx] ^= 0xff;
}

void FullUpdate(void) {}
void PartialUpdate(int x, int y, int w, int h) {
    (void)x, (void)y, (void)w, (void)h;
}
void SoftUpdate(void) {}

ifont *OpenFont(const char *name, int size, int aa) {
    (void)name, (void)aa;
    ifont *f = calloc(1, sizeof *f);
    f->size = size;
    f->height = size;
    return f;
}
void CloseFont(ifont *f) {
    if (f != &stub_font) free(f);
}
void SetFont(const ifont *font, int color) {
    cur_font = font ? font : &stub_font;
    font_color = color;
}
int StringWidth(const char *s) { return (int)strlen(s) * (cur_font->size * 6 / 10); }

void DrawString(int x, int y, const char *s) {
    int cw = cur_font->size * 6 / 10, h = cur_font->size * 7 / 10;
    for (int i = 0; s[i]; i++) {
        if (s[i] == ' ') continue;
        FillArea(x + i * cw + 1, y + (cur_font->size - h) / 2, cw - 2, h, font_color);
    }
}
