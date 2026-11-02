/*
 * Host stand-in for the subset of InkView that pbtk's device code uses.
 *
 * It lets libpbagent and app templates build and run on the PC, so the agent
 * protocol is tested end to end without the reader. Values match the SDK
 * header (SDK-B288, 6.5) for every name defined here.
 */
#ifndef PBTK_HOST_INKVIEW_H
#define PBTK_HOST_INKVIEW_H

#ifdef __cplusplus
extern "C" {
#endif

#define EVT_INIT 21
#define EVT_EXIT 22
#define EVT_SHOW 23
#define EVT_KEYDOWN 25
#define EVT_KEYPRESS 25
#define EVT_KEYUP 26
#define EVT_KEYRELEASE 26
#define EVT_POINTERUP 29
#define EVT_POINTERDOWN 30
#define EVT_POINTERMOVE 31
#define EVT_POINTERLONG 34
#define EVT_POINTERHOLD 35

#define IV_KEY_OK 0x0a
#define IV_KEY_UP 0x11
#define IV_KEY_DOWN 0x12
#define IV_KEY_LEFT 0x13
#define IV_KEY_RIGHT 0x14
#define IV_KEY_MENU 0x17
#define IV_KEY_PREV 0x18
#define IV_KEY_NEXT 0x19
#define IV_KEY_HOME 0x1a
#define IV_KEY_BACK 0x1b
#define IV_KEY_PREV2 0x1c
#define IV_KEY_NEXT2 0x1d

#define BLACK 0x000000
#define DGRAY 0x555555
#define LGRAY 0xaaaaaa
#define WHITE 0xffffff

#define DEFAULTFONT "stub"
#define DEFAULTFONTB "stub-bold"

typedef int (*iv_handler)(int type, int par1, int par2);
typedef void (*iv_timerproc)();

typedef struct icanvas_s {
    int width;
    int height;
    int scanline;
    int depth;
    int clipx1, clipx2;
    int clipy1, clipy2;
    unsigned char *addr;
} icanvas;

typedef struct ifont_s {
    char *name;
    char *family;
    int size;
    unsigned char aa;
    unsigned char isbold;
    unsigned char isitalic;
    unsigned char _r1;
    unsigned short charset;
    unsigned short _r2;
    int color;
    int height;
    int linespacing;
    int baseline;
    void *fdata;
} ifont;

void InkViewMain(iv_handler h);
void CloseApp(void);
iv_handler SetEventHandler(iv_handler h);
iv_handler GetEventHandler(void);
void SendEvent(iv_handler h, int type, int par1, int par2);
void SetHardTimer(const char *name, iv_timerproc proc, int ms);
void SetWeakTimer(const char *name, iv_timerproc proc, int ms);
void ClearTimer(iv_timerproc proc);

icanvas *GetCanvas(void);
int ScreenWidth(void);
int ScreenHeight(void);
int GetOrientation(void);
void SetOrientation(int n);
char *GetDeviceModel(void);
char *GetSoftwareVersion(void);
int NetConnect(const char *name);
/* No sleep for `sec` seconds (the stub logs it to stderr). */
void BanSleep(int sec);

/* Firmware status panel. On the reader it is on unless the app disables it,
 * and while on it shifts the app's picture on the panel by its height with
 * wrap-around (confirmed on PB628 FW 6.9: 75 px). */
#define PANEL_DISABLED 0
#define PANEL_ENABLED (1 << 1)
void SetPanelType(int type);
int GetPanelType(void);
int PanelHeight(void);
int PanelHeightFBOffset(void);

void ClearScreen(void);
void FillArea(int x, int y, int w, int h, int color);
void DrawRect(int x, int y, int w, int h, int color);
void InvertArea(int x, int y, int w, int h);
void FullUpdate(void);
void PartialUpdate(int x, int y, int w, int h);
void SoftUpdate(void);

ifont *OpenFont(const char *name, int size, int aa);
void CloseFont(ifont *f);
void SetFont(const ifont *font, int color);
void DrawString(int x, int y, const char *s);
int StringWidth(const char *s);

#ifdef __cplusplus
}
#endif
#endif
