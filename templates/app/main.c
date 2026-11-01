/*
 * pbtk-demo — the starter InkView app with the pbtk agent built in.
 *
 * Copy this folder to start a new app. It shows the three agent hooks
 * (pbagent_observe / pbagent_start / pbagent_stop), logging, partial vs full
 * refresh, and touch + key handling. It is also the target pbtk's own tests
 * and the GUI's live mirror are tried against.
 *
 * Screen: a title, a 2×3 grid of buttons that toggle when tapped, a status
 * line. Prev/Next move the selection, OK toggles it, Back quits.
 */
#include <inkview.h>
#include <stdio.h>
#include <string.h>

#include "pbagent.h"

#define APP_NAME "pbtk-demo"
#define COLS 2
#define ROWS 3
#define N_BUTTONS (COLS * ROWS)

static ifont *font_title, *font_body;
static int on[N_BUTTONS];
static int selected;
static int taps;
static char status[128] = "tap a button";

typedef struct { int x, y, w, h; } rect;

static rect button_rect(int i) {
    int sw = ScreenWidth(), sh = ScreenHeight();
    int margin = sw / 12, top = sh / 6, gap = sw / 24;
    int bw = (sw - 2 * margin - (COLS - 1) * gap) / COLS;
    int bh = (sh - sh / 8 - top - gap - (ROWS - 1) * gap) / ROWS; /* stop above the status line */
    rect r = {margin + (i % COLS) * (bw + gap), top + (i / COLS) * (bh + gap), bw, bh};
    return r;
}

static void draw_button(int i) {
    rect r = button_rect(i);
    FillArea(r.x, r.y, r.w, r.h, on[i] ? BLACK : WHITE);
    DrawRect(r.x, r.y, r.w, r.h, BLACK);
    if (i == selected) DrawRect(r.x + 6, r.y + 6, r.w - 12, r.h - 12, on[i] ? WHITE : DGRAY);
    char label[12];
    snprintf(label, sizeof label, "%d", i + 1);
    SetFont(font_title, on[i] ? WHITE : BLACK);
    DrawString(r.x + (r.w - StringWidth(label)) / 2, r.y + r.h / 2 - 20, label);
}

static void draw_status(void) {
    int sw = ScreenWidth(), sh = ScreenHeight();
    int y = sh - sh / 8;
    FillArea(0, y, sw, sh / 8, WHITE);
    SetFont(font_body, BLACK);
    DrawString(sw / 12, y + 10, status);
    PartialUpdate(0, y, sw, sh / 8);
}

static void draw_all(void) {
    ClearScreen();
    SetFont(font_title, BLACK);
    DrawString(ScreenWidth() / 12, ScreenHeight() / 20, "pbtk demo");
    for (int i = 0; i < N_BUTTONS; i++) draw_button(i);
    draw_status();
    FullUpdate();
}

static void refresh_button(int i) {
    rect r = button_rect(i);
    draw_button(i);
    PartialUpdate(r.x, r.y, r.w, r.h);
}

static void toggle(int i, const char *how) {
    on[i] = !on[i];
    taps++;
    snprintf(status, sizeof status, "%s %d -> %s (%d)", how, i + 1, on[i] ? "on" : "off", taps);
    pbagent_log("demo: %s", status);
    refresh_button(i);
    draw_status();
}

static int hit(int x, int y) {
    for (int i = 0; i < N_BUTTONS; i++) {
        rect r = button_rect(i);
        if (x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h) return i;
    }
    return -1;
}

static void select_button(int i) {
    int old = selected;
    selected = (i + N_BUTTONS) % N_BUTTONS;
    refresh_button(old);
    refresh_button(selected);
}

static int handler(int type, int p1, int p2) {
    pbagent_observe(type, p1, p2);
    switch (type) {
    case EVT_INIT: {
        font_title = OpenFont(DEFAULTFONTB, 48, 1);
        font_body = OpenFont(DEFAULTFONT, 28, 1);
        /* connect_wifi: the reader often has Wi-Fi off when an app starts. */
        pbagent_config cfg = {.app_name = APP_NAME, .connect_wifi = 1};
        if (pbagent_start(&cfg) != 0)
            snprintf(status, sizeof status, "agent off: see %s.log", APP_NAME);
        pbagent_log("demo: started, screen %dx%d", ScreenWidth(), ScreenHeight());
        break;
    }
    case EVT_SHOW:
        draw_all();
        break;
    case EVT_POINTERUP: {
        int i = hit(p1, p2);
        if (i >= 0) toggle(i, "tap");
        break;
    }
    case EVT_POINTERLONG: {
        int i = hit(p1, p2);
        if (i >= 0) {
            snprintf(status, sizeof status, "long press on %d", i + 1);
            pbagent_log("demo: %s", status);
            draw_status();
        }
        break;
    }
    case EVT_KEYPRESS:
        if (p1 == IV_KEY_NEXT || p1 == IV_KEY_RIGHT || p1 == IV_KEY_DOWN) select_button(selected + 1);
        else if (p1 == IV_KEY_PREV || p1 == IV_KEY_LEFT || p1 == IV_KEY_UP) select_button(selected - 1);
        else if (p1 == IV_KEY_OK) toggle(selected, "key");
        else if (p1 == IV_KEY_BACK) CloseApp();
        pbagent_log("demo: key 0x%x", p1);
        break;
    case EVT_EXIT:
        pbagent_log("demo: exit after %d taps", taps);
        pbagent_stop();
        break;
    }
    return 0;
}

int main(void) {
    InkViewMain(handler);
    return 0;
}
