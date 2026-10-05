/*
 * littlelx - Raspberry Pi touchscreen front panel.
 *
 * Runs as /init on a RAM-only (initramfs) system. It is a dumb, retained-mode
 * "terminal": the computer (via the Mega's UART) defines widgets, this program
 * draws them on the SPI panel and reports touches back. Nothing is ever
 * written to the SD card.
 *
 * Wire protocol (text lines, 115200 8N1 on /dev/ttyAMA0):
 *
 *   computer -> panel
 *     ?                                   reply with HELLO
 *     PING                                keep-alive (panel shows splash if quiet)
 *     CLR                                 delete all widgets
 *     BG rrggbb                           screen background
 *     W id kind x y w h bg fg ac font align value text...
 *        kind : L label, B button, V vertical bar, H horizontal bar
 *               (lowercase v/h = bar the user can drag)
 *        font : 0 small, 1 medium, 2 large      align: 0 centre, 1 left, 2 right
 *        value: 0..1000 (bars) / 0|1 lit (buttons)   text: rest of line, "\n" = newline
 *     V id value                          set value
 *     T id text...                        set text
 *     C id bg fg ac                       set colours
 *     M id value                          marker line on a bar (0..1000, -1 none),
 *                                         e.g. where the physical fader is
 *   Any line may end in "*hh": XOR of all bytes before the '*', as two hex
 *   digits. Lines whose checksum doesn't match are dropped.
 *     D id                                delete widget
 *     K a b c d e f                       touch calibration: x=a*rx+b*ry+c, y=d*rx+e*ry+f
 *     CAL                                 run touch calibration on the panel
 *     UPD ...                             firmware update, see "update" below
 *
 *   panel -> computer
 *     HELLO littlelx-pi 3 <w> <h> <version>   (3 = protocol version)
 *     INFO fb=<ok|missing> touch=<ok|missing>   (right after each HELLO)
 *     P id / R id                         button press / release
 *     S id value                          bar dragged to value
 *     CALD a b c d e f                    calibration result (store it, send back with K)
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <linux/watchdog.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include "font.h"

static void klog(const char *msg);
static void send_hello(void);

#ifndef LLX_VERSION
#define LLX_VERSION "dev"
#endif

#define MAXW 160
#define TEXTMAX 96

typedef struct { int x0, y0, x1, y1; } rect_t;

typedef struct {
	int used;
	char kind;
	int x, y, w, h;
	uint32_t bg, fg, ac;
	int font, align, value;
	int marker; /* -1 = none, else 0..1000 along the bar */
	int pressed;
	char text[TEXTMAX];
} widget_t;

static int W = 480, H = 320;
static uint16_t *back, *shadow, *fbmem;
static int fb_stride; /* in pixels */
static widget_t wd[MAXW];
static uint32_t screen_bg = 0x000000;
static rect_t dirty[32];
static int ndirty;

static int tty = -1, touch = -1, wdog = -1;
static int online;
static long long last_rx, last_hello;

static double cal[6];
static int cal_valid;
static int raw_min_x, raw_max_x, raw_min_y, raw_max_y;

static int calibrating, cal_step;
static int cal_raw[3][2];

static int touch_id = -1; /* widget being touched */
static int touch_down, raw_x, raw_y, have_x, have_y;

static struct {
	int active;
	char cur, spare;
	long cfg_off;
	long total, done;
	int files_ok;
	int fd;
	char path[160];
	long size, got;
	uint32_t want, crc;
	long long last_rx;
	int pct;
} upd = { .fd = -1 };

static const font_t *fonts[3] = { &font_small, &font_medium, &font_large };

static long long now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000LL + ts.tv_nsec / 1000000;
}

/* ------------------------------------------------------------------ output */

static void send_line(const char *fmt, ...)
{
	char buf[256];
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(buf, sizeof(buf) - 1, fmt, ap);
	va_end(ap);
	if (n < 0)
		return;
	if (n > (int)sizeof(buf) - 2)
		n = sizeof(buf) - 2;
	buf[n++] = '\n';
	if (tty >= 0) {
		int off = 0;
		while (off < n) {
			int r = write(tty, buf + off, n - off);
			if (r <= 0)
				break;
			off += r;
		}
	}
}

/* ---------------------------------------------------------------- drawing */

static inline uint16_t rgb565(uint32_t c)
{
	return ((c >> 8) & 0xf800) | ((c >> 5) & 0x07e0) | ((c >> 3) & 0x001f);
}

static uint32_t mix(uint32_t a, uint32_t b, int t /* 0..256 */)
{
	int r = (((a >> 16) & 255) * (256 - t) + ((b >> 16) & 255) * t) >> 8;
	int g = (((a >> 8) & 255) * (256 - t) + ((b >> 8) & 255) * t) >> 8;
	int bl = ((a & 255) * (256 - t) + (b & 255) * t) >> 8;
	return (r << 16) | (g << 8) | bl;
}

static rect_t clip;

static void fill(int x, int y, int w, int h, uint32_t c)
{
	int x0 = x > clip.x0 ? x : clip.x0, y0 = y > clip.y0 ? y : clip.y0;
	int x1 = x + w < clip.x1 ? x + w : clip.x1, y1 = y + h < clip.y1 ? y + h : clip.y1;
	uint16_t p = rgb565(c);
	for (int j = y0; j < y1; j++) {
		uint16_t *row = back + j * W;
		for (int i = x0; i < x1; i++)
			row[i] = p;
	}
}

/* Rectangle with 3px cut corners - cheap "rounded" look. */
static void fill_round(int x, int y, int w, int h, uint32_t c)
{
	static const int cut[3] = { 3, 1, 1 };
	if (w < 8 || h < 8) {
		fill(x, y, w, h, c);
		return;
	}
	for (int k = 0; k < 3; k++) {
		fill(x + cut[k], y + k, w - 2 * cut[k], 1, c);
		fill(x + cut[k], y + h - 1 - k, w - 2 * cut[k], 1, c);
	}
	fill(x, y + 3, w, h - 6, c);
}

static int text_width(const font_t *f, const char *s, int n)
{
	int w = 0;
	for (int i = 0; i < n && s[i]; i++) {
		unsigned char ch = s[i];
		if (ch < 32 || ch > 126)
			ch = '?';
		w += f->g[ch - 32].adv;
	}
	return w;
}

static void draw_glyph(const font_t *f, int x, int y, unsigned char ch, uint32_t c)
{
	const glyph_t *g = &f->g[ch - 32];
	const uint8_t *bits = f->bits + g->off;
	int gx = x + g->xo, gy = y + g->yo;
	int c565 = rgb565(c);
	for (int j = 0; j < g->h; j++) {
		int py = gy + j;
		if (py < clip.y0 || py >= clip.y1)
			continue;
		for (int i = 0; i < g->w; i++) {
			int px = gx + i, k = j * g->w + i;
			int a = (k & 1) ? bits[k >> 1] & 15 : bits[k >> 1] >> 4;
			if (!a || px < clip.x0 || px >= clip.x1)
				continue;
			uint16_t *d = back + py * W + px;
			if (a == 15) {
				*d = c565;
				continue;
			}
			uint16_t o = *d;
			int r = (o >> 11) & 31, gg = (o >> 5) & 63, b = o & 31;
			int cr = (c >> 19) & 31, cg = (c >> 10) & 63, cb = (c >> 3) & 31;
			r += (cr - r) * a / 15;
			gg += (cg - gg) * a / 15;
			b += (cb - b) * a / 15;
			*d = (r << 11) | (gg << 5) | b;
		}
	}
}

/* Draw (possibly multi-line) text inside a box. */
static void draw_text_box(int x, int y, int w, int h, int fi, int align, uint32_t c, const char *s)
{
	const font_t *f = fonts[fi < 0 || fi > 2 ? 0 : fi];
	int lines = 1;
	for (const char *p = s; *p; p++)
		lines += *p == '\n';
	int ty = y + (h - lines * f->height) / 2;
	const char *p = s;
	rect_t saved = clip;
	if (clip.x0 < x) clip.x0 = x;
	if (clip.y0 < y) clip.y0 = y;
	if (clip.x1 > x + w) clip.x1 = x + w;
	if (clip.y1 > y + h) clip.y1 = y + h;
	while (1) {
		const char *e = strchr(p, '\n');
		int n = e ? (int)(e - p) : (int)strlen(p);
		int tw = text_width(f, p, n);
		int tx = align == 1 ? x + 4 : align == 2 ? x + w - 4 - tw : x + (w - tw) / 2;
		for (int i = 0; i < n; i++) {
			unsigned char ch = p[i];
			if (ch < 32 || ch > 126)
				ch = '?';
			draw_glyph(f, tx, ty, ch, c);
			tx += f->g[ch - 32].adv;
		}
		ty += f->height;
		if (!e)
			break;
		p = e + 1;
	}
	clip = saved;
}

static void draw_widget(widget_t *w)
{
	uint32_t bg = w->bg;
	switch (w->kind) {
	case 'L':
		fill(w->x, w->y, w->w, w->h, bg);
		draw_text_box(w->x, w->y, w->w, w->h, w->font, w->align, w->fg, w->text);
		break;
	case 'B':
		if (w->value)
			bg = w->ac;
		if (w->pressed)
			bg = mix(bg, 0xffffff, 110);
		fill_round(w->x, w->y, w->w, w->h, bg);
		draw_text_box(w->x, w->y, w->w, w->h, w->font, w->align, w->fg, w->text);
		break;
	case 'V': case 'v': {
		int fh = w->h * w->value / 1000;
		fill_round(w->x, w->y, w->w, w->h, bg);
		rect_t saved = clip;
		if (clip.y0 < w->y + w->h - fh)
			clip.y0 = w->y + w->h - fh;
		fill_round(w->x, w->y, w->w, w->h, w->pressed ? mix(w->ac, 0xffffff, 80) : w->ac);
		clip = saved;
		draw_text_box(w->x, w->y, w->w, w->h, w->font, w->align, w->fg, w->text);
		if (w->marker >= 0) { /* e.g. the physical fader position */
			int my = w->y + w->h - w->h * w->marker / 1000;
			if (my > w->y + w->h - 3)
				my = w->y + w->h - 3;
			fill(w->x, my - 1, w->w, 4, 0x000000);
			fill(w->x + 2, my, w->w - 4, 2, w->fg);
		}
		break;
	}
	case 'H': case 'h': {
		int fw = w->w * w->value / 1000;
		fill_round(w->x, w->y, w->w, w->h, bg);
		rect_t saved = clip;
		if (clip.x1 > w->x + fw)
			clip.x1 = w->x + fw;
		fill_round(w->x, w->y, w->w, w->h, w->pressed ? mix(w->ac, 0xffffff, 80) : w->ac);
		clip = saved;
		draw_text_box(w->x, w->y, w->w, w->h, w->font, w->align, w->fg, w->text);
		if (w->marker >= 0) {
			int mx = w->x + w->w * w->marker / 1000;
			if (mx > w->x + w->w - 3)
				mx = w->x + w->w - 3;
			fill(mx - 1, w->y, 4, w->h, 0x000000);
			fill(mx, w->y + 2, 2, w->h - 4, w->fg);
		}
		break;
	}
	}
}

static void mark(int x, int y, int w, int h)
{
	rect_t r = { x < 0 ? 0 : x, y < 0 ? 0 : y, x + w > W ? W : x + w, y + h > H ? H : y + h };
	if (r.x1 <= r.x0 || r.y1 <= r.y0)
		return;
	for (int i = 0; i < ndirty; i++) {
		rect_t *d = &dirty[i];
		if (r.x0 >= d->x0 && r.y0 >= d->y0 && r.x1 <= d->x1 && r.y1 <= d->y1)
			return;
	}
	if (ndirty == 32) { /* too many: collapse into one */
		for (int i = 1; i < ndirty; i++) {
			if (dirty[i].x0 < dirty[0].x0) dirty[0].x0 = dirty[i].x0;
			if (dirty[i].y0 < dirty[0].y0) dirty[0].y0 = dirty[i].y0;
			if (dirty[i].x1 > dirty[0].x1) dirty[0].x1 = dirty[i].x1;
			if (dirty[i].y1 > dirty[0].y1) dirty[0].y1 = dirty[i].y1;
		}
		ndirty = 1;
	}
	dirty[ndirty++] = r;
}

static void mark_all(void) { ndirty = 0; mark(0, 0, W, H); }
static void mark_w(widget_t *w) { mark(w->x, w->y, w->w, w->h); }

static void draw_cross(int x, int y, uint32_t c)
{
	fill(x - 15, y, 31, 1, c);
	fill(x, y - 15, 1, 31, c);
	fill(x - 3, y - 3, 7, 7, c);
}

static const int cal_pts[3][2] = { { 10, 10 }, { 90, 50 }, { 50, 90 } }; /* percent */

static void paint_region(void)
{
	if (calibrating) {
		fill(0, 0, W, H, 0x000000);
		draw_text_box(0, H / 2 - 40, W, 30, 1, 0, 0xffffff, "Touch calibration");
		draw_text_box(0, H / 2 - 10, W, 24, 0, 0, 0xaaaaaa, "Tap the centre of each cross");
		draw_cross(cal_pts[cal_step][0] * W / 100, cal_pts[cal_step][1] * H / 100, 0xffcc00);
		return;
	}
	if (upd.active) {
		char msg[64];
		int bw = W - 60, fillw = bw * (upd.pct < 0 ? 0 : upd.pct) / 100;
		fill(0, 0, W, H, 0x101418);
		draw_text_box(0, H / 2 - 60, W, 30, 1, 0, 0xffffff, "Updating firmware");
		fill(30, H / 2 - 12, bw, 24, 0x2a3340);
		fill(30, H / 2 - 12, fillw, 24, 0x2f7de1);
		snprintf(msg, sizeof(msg), "%d%%   (unplugging now keeps the old version)", upd.pct < 0 ? 0 : upd.pct);
		draw_text_box(0, H / 2 + 20, W, 24, 0, 0, 0x8899aa, msg);
		return;
	}
	if (!online) {
		fill(0, 0, W, H, 0x101418);
		draw_text_box(0, H / 2 - 50, W, 40, 2, 0, 0xffffff, "littlelx");
		draw_text_box(0, H / 2, W, 24, 0, 0, 0x8899aa,
			      tty < 0 ? "no serial port" : "waiting for computer...");
		return;
	}
	fill(0, 0, W, H, screen_bg);
	for (int i = 0; i < MAXW; i++) {
		widget_t *w = &wd[i];
		if (!w->used || w->x >= clip.x1 || w->y >= clip.y1 ||
		    w->x + w->w <= clip.x0 || w->y + w->h <= clip.y0)
			continue;
		draw_widget(w);
	}
}

/* Repaint dirty regions into the back buffer, then copy only the rows that
 * actually changed to the framebuffer, so fbtft's deferred I/O pushes as few
 * lines over SPI as possible. */
static void flush(void)
{
	if (!ndirty)
		return;
	int y0 = H, y1 = 0;
	for (int i = 0; i < ndirty; i++) {
		clip = dirty[i];
		paint_region();
		if (dirty[i].y0 < y0) y0 = dirty[i].y0;
		if (dirty[i].y1 > y1) y1 = dirty[i].y1;
	}
	ndirty = 0;
	for (int y = y0; y < y1; y++) {
		uint16_t *b = back + y * W, *s = shadow + y * W;
		if (memcmp(b, s, W * 2)) {
			memcpy(s, b, W * 2);
			if (fbmem)
				memcpy(fbmem + y * fb_stride, b, W * 2);
		}
	}
}

/* -------------------------------------------------------------- protocol */

static uint32_t hex(const char *s) { return strtoul(s, NULL, 16) & 0xffffff; }

static void unescape(char *dst, const char *src, int max)
{
	int n = 0;
	while (*src && n < max - 1) {
		if (src[0] == '\\' && src[1] == 'n') {
			dst[n++] = '\n';
			src += 2;
		} else {
			dst[n++] = *src++;
		}
	}
	dst[n] = 0;
}

/* Split off the first n space-separated fields; return pointer to the rest. */
static char *fields(char *s, char **f, int n)
{
	for (int i = 0; i < n; i++) {
		while (*s == ' ')
			s++;
		f[i] = s;
		while (*s && *s != ' ')
			s++;
		if (*s)
			*s++ = 0;
	}
	if (*s == ' ')
		s++; /* exactly one separator before free text */
	return s;
}

static widget_t *get_w(const char *id)
{
	int i = atoi(id);
	return i >= 0 && i < MAXW ? &wd[i] : NULL;
}

static void start_calibration(void)
{
	calibrating = 1;
	cal_step = 0;
	touch_id = -1;
	mark_all();
}

static void set_online(int on)
{
	if (on != online) {
		online = on;
		mark_all();
	}
}

/* ---------------------------------------------------------------- update
 *
 * The SD card holds two complete copies of the OS, in a/ and b/, and
 * config.txt says which one the firmware boots ("os_prefix=a/"). An update
 * writes the other slot, checks every file, then flips that single byte and
 * reboots. A power cut at any point leaves the running slot untouched.
 * The card is only mounted while this runs.
 *
 *   UPD BEGIN <bytes>             mount, wipe the spare slot   -> UPD READY
 *   UPD FILE <path> <size> <crc>  -> UPD HAVE <path> (copied from the running
 *                                    slot, identical) or UPD SEND <path>
 *   UPD DATA <base64>             -> UPD ACK <bytes so far>, then UPD OK <path>
 *   UPD COMMIT <files>            switch slots, reboot         -> UPD DONE
 *   UPD ABORT                     give up, nothing changes     -> UPD ABORTED
 *   errors: UPD FAIL <reason>
 */
static const char *boot = "/boot"; /* LLX_BOOTDIR=dir: use a plain directory (tests) */


static uint32_t crc_table[256];

static uint32_t crc32_update(uint32_t crc, const uint8_t *p, size_t n)
{
	if (!crc_table[1])
		for (uint32_t i = 0; i < 256; i++) {
			uint32_t c = i;
			for (int k = 0; k < 8; k++)
				c = c & 1 ? 0xedb88320 ^ (c >> 1) : c >> 1;
			crc_table[i] = c;
		}
	crc = ~crc;
	while (n--)
		crc = crc_table[(crc ^ *p++) & 255] ^ (crc >> 8);
	return ~crc;
}

static int b64_decode(const char *in, uint8_t *out)
{
	int n = 0, bits = 0;
	uint32_t acc = 0;
	for (; *in && *in != '='; in++) {
		int v;
		char c = *in;
		if (c >= 'A' && c <= 'Z') v = c - 'A';
		else if (c >= 'a' && c <= 'z') v = c - 'a' + 26;
		else if (c >= '0' && c <= '9') v = c - '0' + 52;
		else if (c == '+') v = 62;
		else if (c == '/') v = 63;
		else return -1;
		acc = (acc << 6) | v;
		bits += 6;
		if (bits >= 8) {
			bits -= 8;
			out[n++] = acc >> bits;
		}
	}
	return n;
}

static void slot_path(char *dst, size_t len, char slot, const char *rel)
{
	snprintf(dst, len, "%s/%c/%s", boot, slot, rel);
}

static void mkdirs(const char *path) /* create parent directories of path */
{
	char tmp[200];
	snprintf(tmp, sizeof(tmp), "%s", path);
	for (char *p = tmp + 1; *p; p++)
		if (*p == '/') {
			*p = 0;
			mkdir(tmp, 0755);
			*p = '/';
		}
}

static int rm_entry(const char *path, const struct stat *st, int flag, struct FTW *ftw)
{
	(void)st; (void)flag; (void)ftw;
	return remove(path);
}

static void upd_repaint(void)
{
	int pct = upd.total ? (int)(upd.done * 100 / upd.total) : 0;
	if (pct != upd.pct) {
		upd.pct = pct;
		mark_all();
	}
}

static void upd_close(void)
{
	if (upd.fd >= 0)
		close(upd.fd);
	upd.fd = -1;
	sync();
	if (!getenv("LLX_BOOTDIR"))
		umount(boot);
	upd.active = 0;
	mark_all();
}

static void upd_fail(const char *why)
{
	send_line("UPD FAIL %s", why);
	klog(why);
	if (upd.active)
		upd_close();
}

static void upd_begin(long total)
{
	if (upd.active)
		upd_close();
	memset(&upd, 0, sizeof(upd));
	upd.fd = -1;
	upd.pct = -1;
	int ok = -1;
	if (getenv("LLX_BOOTDIR")) {
		boot = getenv("LLX_BOOTDIR");
		ok = 0;
	} else {
		mkdir(boot, 0755);
	}
	for (int i = 0; i < 50 && ok; i++) { /* SD card may still be probing */
		ok = mount("/dev/mmcblk0p1", boot, "vfat", MS_NOATIME, "");
		if (ok)
			usleep(100000);
	}
	if (ok) {
		upd_fail("cannot-mount-sd-card");
		return;
	}
	upd.active = 1;
	char cfg[200];
	snprintf(cfg, sizeof(cfg), "%s/config.txt", boot);
	FILE *f = fopen(cfg, "r");
	char line[256];
	long off = 0;
	upd.cfg_off = -1;
	while (f && fgets(line, sizeof(line), f)) {
		if (!strncmp(line, "os_prefix=", 10) && (line[10] == 'a' || line[10] == 'b') && line[11] == '/') {
			upd.cfg_off = off + 10;
			upd.cur = line[10];
		}
		off += strlen(line);
	}
	if (f)
		fclose(f);
	if (upd.cfg_off < 0) {
		upd_fail("no-os_prefix-in-config.txt");
		return;
	}
	upd.spare = upd.cur == 'a' ? 'b' : 'a';
	char dir[40];
	snprintf(dir, sizeof(dir), "%.24s/%c", boot, upd.spare);
	nftw(dir, rm_entry, 16, FTW_DEPTH | FTW_PHYS);
	mkdir(dir, 0755);
	upd.total = total > 0 ? total : 1;
	upd.last_rx = now_ms();
	upd_repaint();
	send_line("UPD READY %c", upd.spare);
}

static int valid_rel(const char *p)
{
	if (!*p || *p == '/' || strstr(p, ".."))
		return 0;
	for (; *p; p++)
		if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') ||
		      *p == '.' || *p == '_' || *p == '-' || *p == '/'))
			return 0;
	return 1;
}

static void upd_finish_file(void)
{
	char part[220], dst[200];
	fsync(upd.fd);
	close(upd.fd);
	upd.fd = -1;
	slot_path(dst, sizeof(dst), upd.spare, upd.path);
	snprintf(part, sizeof(part), "%s.part", dst);
	if (upd.crc != upd.want) {
		upd_fail("checksum-mismatch");
		return;
	}
	if (rename(part, dst)) {
		upd_fail("rename-failed");
		return;
	}
	upd.files_ok++;
	send_line("UPD OK %s", upd.path);
}

/* Copy an identical file from the running slot instead of sending it. */
static int upd_copy_same(const char *rel, long size, uint32_t want)
{
	char src[200], dst[200];
	uint8_t buf[8192];
	struct stat st;
	slot_path(src, sizeof(src), upd.cur, rel);
	slot_path(dst, sizeof(dst), upd.spare, rel);
	if (stat(src, &st) || st.st_size != size)
		return 0;
	int in = open(src, O_RDONLY);
	if (in < 0)
		return 0;
	uint32_t crc = 0;
	int n;
	while ((n = read(in, buf, sizeof(buf))) > 0)
		crc = crc32_update(crc, buf, n);
	if (crc != want) {
		close(in);
		return 0;
	}
	lseek(in, 0, SEEK_SET);
	mkdirs(dst);
	int out = open(dst, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (out < 0) {
		close(in);
		return 0;
	}
	crc = 0;
	while ((n = read(in, buf, sizeof(buf))) > 0) {
		if (write(out, buf, n) != n)
			break;
		crc = crc32_update(crc, buf, n);
	}
	fsync(out);
	close(out);
	close(in);
	return crc == want;
}

static void upd_file(const char *rel, long size, uint32_t want)
{
	char part[200];
	if (!upd.active || upd.fd >= 0) {
		upd_fail("unexpected-FILE");
		return;
	}
	if (!valid_rel(rel) || strlen(rel) >= sizeof(upd.path)) {
		upd_fail("bad-path");
		return;
	}
	if (upd_copy_same(rel, size, want)) {
		upd.files_ok++;
		send_line("UPD HAVE %s", rel);
		return;
	}
	strcpy(upd.path, rel);
	upd.size = size;
	upd.got = 0;
	upd.want = want;
	upd.crc = 0;
	slot_path(part, sizeof(part), upd.spare, rel);
	strcat(part, ".part");
	mkdirs(part);
	upd.fd = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (upd.fd < 0) {
		upd_fail("cannot-create-file");
		return;
	}
	send_line("UPD SEND %s", rel);
	if (size == 0)
		upd_finish_file();
}

static void upd_data(const char *b64)
{
	uint8_t buf[256];
	if (!upd.active || upd.fd < 0) {
		upd_fail("unexpected-DATA");
		return;
	}
	int n = strlen(b64) <= 340 ? b64_decode(b64, buf) : -1;
	if (n < 0 || upd.got + n > upd.size) {
		upd_fail("bad-data");
		return;
	}
	if (write(upd.fd, buf, n) != n) {
		upd_fail("write-failed-card-full");
		return;
	}
	upd.crc = crc32_update(upd.crc, buf, n);
	upd.got += n;
	upd.done += n;
	send_line("UPD ACK %ld", upd.got);
	upd_repaint();
	if (upd.got == upd.size)
		upd_finish_file();
}

static void upd_commit(int files)
{
	if (!upd.active || upd.fd >= 0 || files != upd.files_ok || files < 1) {
		upd_fail("incomplete-update");
		return;
	}
	char cfg[200];
	sync();
	snprintf(cfg, sizeof(cfg), "%s/config.txt", boot);
	int fd = open(cfg, O_RDWR);
	if (fd < 0 || pwrite(fd, &upd.spare, 1, upd.cfg_off) != 1) {
		if (fd >= 0)
			close(fd);
		upd_fail("cannot-switch-slot");
		return;
	}
	fsync(fd);
	close(fd);
	upd.done = upd.total;
	send_line("UPD DONE %c", upd.spare);
	klog("update installed, rebooting");
	sync();
	if (getenv("LLX_BOOTDIR")) { /* test mode: don't reboot the PC */
		upd.active = 0;
		mark_all();
		return;
	}
	umount(boot);
	sync();
	tcdrain(tty);
	reboot(RB_AUTOBOOT);
}

static void upd_line(char *args)
{
	char *f[3];
	upd.last_rx = now_ms();
	if (!strncmp(args, "BEGIN", 5)) {
		upd_begin(atol(args + 5));
	} else if (!strncmp(args, "FILE ", 5)) {
		char *rest = fields(args + 5, f, 3);
		(void)rest;
		upd_file(f[0], atol(f[1]), (uint32_t)strtoul(f[2], NULL, 16));
	} else if (!strncmp(args, "DATA ", 5)) {
		upd_data(args + 5);
	} else if (!strncmp(args, "COMMIT", 6)) {
		upd_commit(atoi(args + 6));
	} else if (!strncmp(args, "ABORT", 5)) {
		if (upd.active)
			upd_close();
		send_line("UPD ABORTED");
	}
}

/* Lines may end in "*hh" (XOR of the bytes before '*'). Returns 0 = drop. */
static int checksum_ok(char *line)
{
	size_t n = strlen(line);
	if (n < 3 || line[n - 3] != '*')
		return 1; /* no checksum (older bridge): accept */
	char *end;
	unsigned long want = strtoul(line + n - 2, &end, 16);
	if (*end)
		return 1;
	uint8_t x = 0;
	for (size_t i = 0; i < n - 3; i++)
		x ^= (uint8_t)line[i];
	line[n - 3] = 0;
	return x == want;
}

static void handle_line(char *line)
{
	char *f[12];
	widget_t *w;

	if (!checksum_ok(line))
		return; /* damaged on the way: the bridge re-sends everything regularly */

	last_rx = now_ms();
	set_online(1);

	if (!strcmp(line, "?")) {
		send_hello();
	} else if (!strcmp(line, "PING")) {
		/* keep-alive only */
	} else if (!strcmp(line, "CLR")) {
		memset(wd, 0, sizeof(wd));
		touch_id = -1;
		mark_all();
	} else if (!strncmp(line, "BG ", 3)) {
		screen_bg = hex(line + 3);
		mark_all();
	} else if (!strncmp(line, "UPD ", 4)) {
		upd_line(line + 4);
	} else if (!strcmp(line, "CAL")) {
		start_calibration();
	} else if (line[0] == 'W' && line[1] == ' ') {
		char *rest = fields(line + 2, f, 12);
		if (!(w = get_w(f[0])))
			return;
		if (w->used)
			mark_w(w);
		else
			w->marker = -1;
		w->used = 1;
		w->kind = f[1][0];
		w->x = atoi(f[2]); w->y = atoi(f[3]); w->w = atoi(f[4]); w->h = atoi(f[5]);
		w->bg = hex(f[6]); w->fg = hex(f[7]); w->ac = hex(f[8]);
		w->font = atoi(f[9]); w->align = atoi(f[10]); w->value = atoi(f[11]);
		unescape(w->text, rest, TEXTMAX);
		mark_w(w);
	} else if (line[0] == 'V' && line[1] == ' ') {
		fields(line + 2, f, 2);
		if ((w = get_w(f[0])) && w->used) {
			int v = atoi(f[1]);
			if (v != w->value) {
				w->value = v;
				mark_w(w);
			}
		}
	} else if (line[0] == 'T' && line[1] == ' ') {
		char *rest = fields(line + 2, f, 1);
		if ((w = get_w(f[0])) && w->used) {
			char t[TEXTMAX];
			unescape(t, rest, TEXTMAX);
			if (strcmp(t, w->text)) {
				strcpy(w->text, t);
				mark_w(w);
			}
		}
	} else if (line[0] == 'C' && line[1] == ' ') {
		fields(line + 2, f, 4);
		if ((w = get_w(f[0])) && w->used) {
			w->bg = hex(f[1]); w->fg = hex(f[2]); w->ac = hex(f[3]);
			mark_w(w);
		}
	} else if (line[0] == 'M' && line[1] == ' ') {
		fields(line + 2, f, 2);
		if ((w = get_w(f[0])) && w->used) {
			int m = atoi(f[1]);
			if (m != w->marker) {
				w->marker = m;
				mark_w(w);
			}
		}
	} else if (line[0] == 'D' && line[1] == ' ') {
		if ((w = get_w(line + 2)) && w->used) {
			mark_w(w);
			w->used = 0;
			if (touch_id == w - wd)
				touch_id = -1;
		}
	} else if (line[0] == 'K' && line[1] == ' ') {
		if (sscanf(line + 2, "%lf %lf %lf %lf %lf %lf", &cal[0], &cal[1], &cal[2],
			   &cal[3], &cal[4], &cal[5]) == 6)
			cal_valid = 1;
	}
}

static void read_tty(void)
{
	static char buf[512];
	static int len;
	char tmp[256];
	int n = read(tty, tmp, sizeof(tmp));
	if (n <= 0)
		return;
	for (int i = 0; i < n; i++) {
		char c = tmp[i];
		if (c == '\r')
			continue;
		if (c == '\n') {
			buf[len] = 0;
			if (len)
				handle_line(buf);
			len = 0;
		} else if (len < (int)sizeof(buf) - 1) {
			buf[len++] = c;
		}
	}
}

/* ------------------------------------------------------------------ touch */

static void raw_to_screen(int rx, int ry, int *sx, int *sy)
{
	if (cal_valid) {
		*sx = (int)(cal[0] * rx + cal[1] * ry + cal[2]);
		*sy = (int)(cal[3] * rx + cal[4] * ry + cal[5]);
	} else {
		int dx = raw_max_x - raw_min_x, dy = raw_max_y - raw_min_y;
		*sx = dx ? (rx - raw_min_x) * W / dx : rx;
		*sy = dy ? (ry - raw_min_y) * H / dy : ry;
	}
}

/* Solve the 3-point affine fit for one screen axis. */
static void solve_axis(const double s[3], double out[3])
{
	double x[3], y[3];
	for (int i = 0; i < 3; i++) {
		x[i] = cal_raw[i][0];
		y[i] = cal_raw[i][1];
	}
	double det = x[0] * (y[1] - y[2]) - y[0] * (x[1] - x[2]) + (x[1] * y[2] - x[2] * y[1]);
	if (det == 0) {
		out[0] = out[1] = out[2] = 0;
		return;
	}
	out[0] = (s[0] * (y[1] - y[2]) - y[0] * (s[1] - s[2]) + (s[1] * y[2] - s[2] * y[1])) / det;
	out[1] = (x[0] * (s[1] - s[2]) - s[0] * (x[1] - x[2]) + (x[1] * s[2] - x[2] * s[1])) / det;
	out[2] = (x[0] * (y[1] * s[2] - y[2] * s[1]) - y[0] * (x[1] * s[2] - x[2] * s[1]) +
		  s[0] * (x[1] * y[2] - x[2] * y[1])) / det;
}

static void calibration_tap(int rx, int ry)
{
	cal_raw[cal_step][0] = rx;
	cal_raw[cal_step][1] = ry;
	if (++cal_step < 3) {
		mark_all();
		return;
	}
	double sx[3], sy[3];
	for (int i = 0; i < 3; i++) {
		sx[i] = cal_pts[i][0] * W / 100.0;
		sy[i] = cal_pts[i][1] * H / 100.0;
	}
	solve_axis(sx, cal);
	solve_axis(sy, cal + 3);
	cal_valid = 1;
	calibrating = 0;
	send_line("CALD %.6f %.6f %.3f %.6f %.6f %.3f", cal[0], cal[1], cal[2], cal[3], cal[4], cal[5]);
	mark_all();
}

static int bar_value(widget_t *w, int sx, int sy)
{
	int v = (w->kind == 'v') ? (w->y + w->h - sy) * 1000 / (w->h ? w->h : 1)
				 : (sx - w->x) * 1000 / (w->w ? w->w : 1);
	return v < 0 ? 0 : v > 1000 ? 1000 : v;
}

static void touch_event(int down)
{
	int sx, sy;
	raw_to_screen(raw_x, raw_y, &sx, &sy);

	if (calibrating) {
		if (!down && touch_id == -2)
			touch_id = -1;
		else if (down && touch_id == -1) {
			touch_id = -2; /* wait for lift before the next point */
			calibration_tap(raw_x, raw_y);
		}
		return;
	}
	if (!online)
		return;

	if (down && touch_id < 0) {
		for (int i = MAXW - 1; i >= 0; i--) { /* topmost first */
			widget_t *w = &wd[i];
			if (!w->used || (w->kind != 'B' && w->kind != 'v' && w->kind != 'h'))
				continue;
			if (sx < w->x || sx >= w->x + w->w || sy < w->y || sy >= w->y + w->h)
				continue;
			touch_id = i;
			w->pressed = 1;
			mark_w(w);
			if (w->kind == 'B')
				send_line("P %d", i);
			break;
		}
	}
	if (touch_id >= 0) {
		widget_t *w = &wd[touch_id];
		if (down && (w->kind == 'v' || w->kind == 'h')) {
			int v = bar_value(w, sx, sy);
			if (v != w->value) {
				w->value = v;
				mark_w(w);
				send_line("S %d %d", touch_id, v);
			}
		}
		if (!down) {
			w->pressed = 0;
			mark_w(w);
			if (w->kind == 'B')
				send_line("R %d", touch_id);
			touch_id = -1;
		}
	}
}

static void read_touch(void)
{
	struct input_event ev[32];
	int n = read(touch, ev, sizeof(ev));
	if (n <= 0) {
		if (n < 0 && errno == ENODEV) {
			close(touch);
			touch = -1;
		}
		return;
	}
	for (int i = 0; i < n / (int)sizeof(ev[0]); i++) {
		struct input_event *e = &ev[i];
		if (e->type == EV_ABS && e->code == ABS_X) {
			raw_x = e->value;
			have_x = 1;
		} else if (e->type == EV_ABS && e->code == ABS_Y) {
			raw_y = e->value;
			have_y = 1;
		} else if (e->type == EV_KEY && e->code == BTN_TOUCH) {
			touch_down = e->value;
		} else if (e->type == EV_SYN && e->code == SYN_REPORT) {
			if (have_x && have_y)
				touch_event(touch_down);
		}
	}
}

/* ------------------------------------------------------------------ setup */

static int open_touch(void)
{
	DIR *d = opendir("/dev/input");
	struct dirent *e;
	int fd = -1;
	if (!d)
		return -1;
	while ((e = readdir(d)) && fd < 0) {
		char path[300];
		unsigned long absbits = 0;
		if (strncmp(e->d_name, "event", 5))
			continue;
		snprintf(path, sizeof(path), "/dev/input/%s", e->d_name);
		fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0)
			continue;
		if (ioctl(fd, EVIOCGBIT(EV_ABS, sizeof(absbits)), &absbits) < 0 ||
		    !(absbits & (1UL << ABS_X)) || !(absbits & (1UL << ABS_Y))) {
			close(fd);
			fd = -1;
			continue;
		}
		struct input_absinfo ax, ay;
		ioctl(fd, EVIOCGABS(ABS_X), &ax);
		ioctl(fd, EVIOCGABS(ABS_Y), &ay);
		raw_min_x = ax.minimum; raw_max_x = ax.maximum;
		raw_min_y = ay.minimum; raw_max_y = ay.maximum;
	}
	closedir(d);
	return fd;
}

static int open_tty(const char *path)
{
	int fd = open(path, O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);
	if (fd < 0)
		return -1;
	struct termios t;
	tcgetattr(fd, &t);
	cfmakeraw(&t);
	cfsetispeed(&t, B500000);
	cfsetospeed(&t, B500000);
	t.c_cflag |= CLOCAL | CREAD;
	t.c_cflag &= ~CRTSCTS;
	tcsetattr(fd, TCSANOW, &t);
	tcflush(fd, TCIOFLUSH);
	return fd;
}

static int open_fb(const char *path)
{
	int fd = open(path, O_RDWR | O_CLOEXEC);
	if (fd < 0)
		return -1;
	struct fb_var_screeninfo v;
	struct fb_fix_screeninfo fx;
	if (ioctl(fd, FBIOGET_VSCREENINFO, &v) < 0 || ioctl(fd, FBIOGET_FSCREENINFO, &fx) < 0 ||
	    v.bits_per_pixel != 16) {
		close(fd);
		return -1;
	}
	W = v.xres;
	H = v.yres;
	fb_stride = fx.line_length / 2;
	fbmem = mmap(NULL, fx.line_length * v.yres, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (fbmem == MAP_FAILED) {
		fbmem = NULL;
		close(fd);
		return -1;
	}
	return fd;
}

/* Offline test: render a protocol script from stdin to a PPM image. */
static int test_render(const char *out)
{
	char line[512];
	back = calloc(W * H, 2);
	shadow = calloc(W * H, 2);
	mark_all();
	while (fgets(line, sizeof(line), stdin)) {
		line[strcspn(line, "\r\n")] = 0;
		if (!strncmp(line, "#touch ", 7)) { /* #touch x y down : screen coords */
			int down;
			sscanf(line + 7, "%d %d %d", &raw_x, &raw_y, &down);
			raw_min_x = raw_min_y = 0; raw_max_x = W; raw_max_y = H;
			have_x = have_y = 1;
			touch_event(down);
		} else if (line[0]) {
			handle_line(line);
		}
		flush();
	}
	FILE *f = fopen(out, "wb");
	if (!f)
		return 1;
	fprintf(f, "P6 %d %d 255\n", W, H);
	for (int i = 0; i < W * H; i++) {
		uint16_t p = back[i];
		fputc((p >> 8) & 0xf8, f);
		fputc((p >> 3) & 0xfc, f);
		fputc((p << 3) & 0xf8, f);
	}
	fclose(f);
	return 0;
}

static int app(void)
{
	const char *ttyp = getenv("LLX_TTY") ? getenv("LLX_TTY") : "/dev/ttyAMA0";
	const char *fbp = getenv("LLX_FB") ? getenv("LLX_FB") : "/dev/fb0";
	int fb = -1;

	klog("app starting");

	/* fbtft may probe a moment after we start */
	for (int i = 0; i < 100 && (fb = open_fb(fbp)) < 0; i++)
		usleep(100000);
	klog(fb >= 0 ? "framebuffer ok" : "no framebuffer");
	back = calloc(W * H, 2);
	shadow = calloc(W * H, 2);
	if (!back || !shadow)
		return 1;
	memset(shadow, 0x55, W * H * 2); /* force first full paint */

	/* LLX_NOWDOG=1 on the kernel command line skips it (QEMU resets at once) */
	wdog = getenv("LLX_NOWDOG") ? -1 : open("/dev/watchdog", O_WRONLY | O_CLOEXEC);
	if (wdog >= 0) {
		int t = 10;
		ioctl(wdog, WDIOC_SETTIMEOUT, &t);
	}
	tty = open_tty(ttyp);
	touch = open_touch();
	int kfd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC);
	if (kfd >= 0) {
		dprintf(kfd, "littlelx: %dx%d fb=%s tty=%s touch=%s watchdog=%s\n", W, H,
			fbmem ? "ok" : "MISSING", tty >= 0 ? "ok" : "MISSING",
			touch >= 0 ? "ok" : "MISSING", wdog >= 0 ? "ok" : "none");
		close(kfd);
	}
	mark_all();
	flush();

	for (;;) {
		struct pollfd p[2];
		int np = 0;
		if (tty >= 0)
			p[np++] = (struct pollfd){ .fd = tty, .events = POLLIN };
		if (touch >= 0)
			p[np++] = (struct pollfd){ .fd = touch, .events = POLLIN };
		poll(p, np, 250);
		for (int i = 0; i < np; i++) {
			if (!(p[i].revents & POLLIN))
				continue;
			if (p[i].fd == tty)
				read_tty();
			else
				read_touch();
		}

		long long t = now_ms();
		static long long last_retry;
		if (t - last_retry > 1000) { /* devices that went away or were late */
			last_retry = t;
			if (tty < 0)
				tty = open_tty(ttyp);
			if (touch < 0)
				touch = open_touch();
		}
		if (t - last_rx > 2500 && t - last_hello > 2000) {
			send_hello();
			last_hello = t;
		}
		if (upd.active && t - upd.last_rx > 30000) {
			klog("update timed out");
			upd_close();
		}
		if (online && t - last_rx > 6000) {
			set_online(0);
		}
		flush();
		if (wdog >= 0)
			ioctl(wdog, WDIOC_KEEPALIVE, 0);
	}
}

static void send_hello(void)
{
	send_line("HELLO littlelx-pi 3 %d %d %s", W, H, LLX_VERSION);
	send_line("INFO fb=%s touch=%s", fbmem ? "ok" : "missing", touch >= 0 ? "ok" : "missing");
}

/* --------------------------------------------------------------- PID 1 */

static void klog(const char *msg)
{
	int fd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC);
	if (fd >= 0) {
		dprintf(fd, "littlelx: %s\n", msg);
		close(fd);
	}
}

static void pid1(void)
{
	mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
	klog("init started");
	mount("proc", "/proc", "proc", 0, NULL);
	mount("sysfs", "/sys", "sysfs", 0, NULL);
	mount("tmpfs", "/tmp", "tmpfs", 0, NULL);
	int fd = open("/dev/null", O_RDWR);
	if (fd >= 0) {
		dup2(fd, 0);
		dup2(fd, 1);
		dup2(fd, 2);
		if (fd > 2)
			close(fd);
	}
	signal(SIGINT, SIG_IGN); /* ctrl-alt-del */
	reboot(RB_DISABLE_CAD);

	for (;;) { /* supervise: respawn the app if it ever exits */
		pid_t pid = fork();
		if (pid == 0)
			_exit(app());
		for (;;) {
			pid_t r = wait(NULL);
			if (r == pid || (r < 0 && errno == ECHILD))
				break;
		}
		sleep(1);
	}
}

int main(int argc, char **argv)
{
	if (argc >= 3 && !strcmp(argv[1], "--render")) { /* --render out.ppm [WxH] */
		if (argc == 4)
			sscanf(argv[3], "%dx%d", &W, &H);
		return test_render(argv[2]);
	}
	if (getpid() == 1)
		pid1();
	return app();
}
