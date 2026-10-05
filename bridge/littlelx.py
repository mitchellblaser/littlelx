#!/usr/bin/env python3
"""
littlelx bridge: Arduino Mega (USB) <-> grandMA3 (OSC). Mac, Windows, Linux.

The Mega reports raw pin changes and relays the Raspberry Pi touchscreen.
This script turns faders/keys/encoders into OSC for MA3 and draws the screen.

  littlelx.py               run the bridge
  littlelx.py --learn       teach it which pin is which control
  littlelx.py --calibrate   calibrate the touchscreen
  littlelx.py --faders      calibrate fader bottom/top (after --learn)
  littlelx.py --monitor     print everything the Mega sends
  littlelx.py --probe       show how key-matrix pins connect (hold a key)
  littlelx.py --test-faders find the fader message format your MA3 accepts
  littlelx.py --update-pi littlelx-pi-update.zip
                            update the touchscreen's firmware over USB
  littlelx.py --port COM5   use a specific serial port

Needs pyserial on Windows (pip install pyserial); on Mac/Linux it works with
plain Python 3 too. Settings live in ~/.littlelx.json (created on first run,
safe to hand-edit while the bridge is stopped).
"""
import argparse
import copy
import glob
import json
import os
import queue
import re
import socket
import struct
import sys
import base64
import zipfile
import zlib
import threading
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

CONFIG_PATH = os.path.expanduser("~/.littlelx.json")

# ---------------------------------------------------------------- defaults
#
# Key / touch-button actions:
#   {"exec": 201}                 press/release executor key (follows page)
#   {"key": "Store"}              a console key: typed into MA's command line
#                                 ("Please", "Clear" and "<-" act on the line)
#   {"cmd": "Go+"}                run a command right away
#   {"page": 1} / {"page": -1}    page up / down (MA's page follows)
#   {"screen": "keypad"}          switch touchscreen page
# Touch buttons can also have "state": "highlight"|"lowlight"|"solo"|"blind"
# to light up while that is active in MA (executor buttons light by themselves).
# Encoder actions: {"cmd": "... {d} ..."} with {d} = signed step, or {"page": 1}

CONFIG_VERSION = 2

DEFAULTS = {
    "config_version": CONFIG_VERSION,
    "osc": {
        "host": "127.0.0.1",      # MA3 machine (127.0.0.1 = MA3 onPC on this computer)
        "port": 8000,             # MA3: Menu > In & Out > OSC > Port
        "prefix": "/gma3",        # MA3 OSC line "Prefix" (gma3), "" if none
        "listen_port": 9000,      # MA3 "Send" destination port, for feedback
        "fader_type": "i",        # how faders are sent; --test-faders picks it (see FADER_FORMATS)
    },
    "ma3": {
        "auto_install": True,     # put the littlelx code into MA3 over OSC
        "osc_line": 2,            # MA3 OSC line that SENDS to this computer (port 9000)
        "tick": 0.1,              # how often MA reports (s)
    },
    "serial_port": "",            # "" = auto-detect, or e.g. "COM5" / "/dev/cu.usbmodem1101"
    "page": 1,
    "pickup": True,               # soft takeover: a fader acts once it reaches MA's level
    "faders": [{"exec": 201 + i, "name": ""} for i in range(5)],
    "keys": (
        [{"exec": 201 + i} for i in range(5)]          # under the faders
        + [{"exec": 101 + i} for i in range(10)]       # button executors
        + [{"page": -1}, {"page": 1}, {"key": "Clear"}, {"cmd": "Go+"}, {"cmd": "Oops"}]
    ),
    "encoders": [
        {"cmd": "Attribute \"Dimmer\" At + {d}", "step": 2, "push": {"key": "Clear"}},
        {"page": 1, "push": {"screen": "keypad"}},
    ],
    "touch_buttons": [
        {"label": "Page -", "page": -1},
        {"label": "Page +", "page": 1},
        {"label": "Clear", "key": "Clear"},
        {"label": "Oops", "cmd": "Oops"},
        {"label": "Keypad", "screen": "keypad"},
        {"label": "Go -", "cmd": "Go-"},
        {"label": "Pause", "cmd": "Pause"},
        {"label": "Go +", "cmd": "Go+", "color": "1f7a3a"},
        {"label": "Highlight", "cmd": "Highlight", "state": "highlight"},
        {"label": "Blind", "cmd": "Blind", "state": "blind"},
        {"label": "Last", "cmd": "Previous"},
        {"label": "Next", "cmd": "Next"},
    ],
    # Filled in by --learn
    "hw": {
        "faders": [None] * 5,      # {"ch": 0..15, "lo": 0, "hi": 1023}
        "keys": [None] * 20,       # {"pin": 22} to GND, or {"pair": [22, 31]} matrix
        "encoders": [None] * 2,    # {"a": 2, "b": 3, "push": 4 or [22, 31], "div": 4}
        "pin_modes": {},           # {"54": 2, ...} sent to the Mega on connect
    },
    "touch_cal": None,
}


def load_config():
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        if user.get("config_version", 1) < CONFIG_VERSION:
            migrate(cfg)
            cfg["config_version"] = CONFIG_VERSION
            save_config(cfg)
    else:
        save_config(cfg)
    return cfg


def migrate(cfg):
    """Bring a config saved by an older version up to date (runs once)."""
    labels = [b.get("label") for b in cfg.get("touch_buttons", [])]
    if "Blackout" in labels or "Freeze" in labels:
        cfg["touch_buttons"] = copy.deepcopy(DEFAULTS["touch_buttons"])
        print("Touch buttons updated: Blackout/Freeze replaced by Last/Next; Highlight/Blind light up.")
    for k in cfg.get("keys", []):
        if k == {"cmd": "Clear"}:  # Clear now acts on MA's command line first
            k.pop("cmd")
            k["key"] = "Clear"
    for e in cfg.get("encoders", []):
        if e and e.get("push") == {"cmd": "Clear"}:
            e["push"] = {"key": "Clear"}
    cfg.pop("sync_page_to_ma", None)
    cfg["pickup"] = True  # old default was off; soft takeover is now on


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------- OSC

def _pad(b):
    return b + b"\0" * (4 - len(b) % 4)


def osc_message(addr, *args):
    tags, data = ",", b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            data += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            data += struct.pack(">f", a)
        else:
            tags += "s"
            data += _pad(str(a).encode())
    return _pad(addr.encode()) + _pad(tags.encode()) + data


def osc_parse(data):
    """Yield (address, [args]) from an OSC packet or bundle."""
    def string(d, i):
        e = d.index(b"\0", i)
        return d[i:e].decode(errors="replace"), (e + 4) & ~3

    if data.startswith(b"#bundle\0"):
        i = 16
        while i + 4 <= len(data):
            (n,) = struct.unpack(">i", data[i:i + 4])
            yield from osc_parse(data[i + 4:i + 4 + n])
            i += 4 + n
        return
    try:
        addr, i = string(data, 0)
        tags, i = string(data, i)
        args = []
        for t in tags[1:]:
            if t == "i":
                args.append(struct.unpack(">i", data[i:i + 4])[0]); i += 4
            elif t == "f":
                args.append(struct.unpack(">f", data[i:i + 4])[0]); i += 4
            elif t == "s":
                s, i = string(data, i); args.append(s)
            elif t in "TF":
                args.append(t == "T")
        yield addr, args
    except (ValueError, struct.error):
        return


# How a fader move is sent to MA3. MA3 versions/setups differ, so
# --test-faders tries each on a real executor and stores the one that works.
FADER_FORMATS = {
    "i":     "OSC /PageN/FaderE  int 0..100",
    "f":     "OSC /PageN/FaderE  float 0..100",
    "f1":    "OSC /PageN/FaderE  float 0..1",
    "cmd":   "command  Page N.E At V",
    "cmdex": "command  Executor N.E At V",
}


def fader_message(fmt, page, ex, value):
    """-> (address, arg) for Bridge.osc(); value is 0..100."""
    if fmt == "f":
        return f"/Page{page}/Fader{ex}", float(round(value, 1))
    if fmt == "f1":
        return f"/Page{page}/Fader{ex}", float(round(value / 100.0, 4))
    if fmt == "cmd":
        return "/cmd", f"Page {page}.{ex} At {round(value)}"
    if fmt == "cmdex":
        return "/cmd", f"Executor {page}.{ex} At {round(value)}"
    return f"/Page{page}/Fader{ex}", int(round(value))


# ------------------------------------------------------------------ events
#
# Everything (serial lines, OSC packets, keyboard input) arrives on one queue,
# fed by background threads. This works the same on Windows, Mac and Linux.

events = queue.Queue()


def start_thread(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


def stdin_reader():
    for line in sys.stdin:
        events.put(("stdin", None, line))


# ------------------------------------------------------------------ serial

BAUD = 500000
USB_IDS = {0x2341, 0x2A03, 0x1A86, 0x0403, 0x10C4}  # Arduino, CH340, FTDI, CP210x


def find_port(cfg):
    if cfg.get("serial_port"):
        return cfg["serial_port"]
    if serial:
        ports = list(serial.tools.list_ports.comports())
        for p in ports:
            if p.vid in USB_IDS or "arduino" in (p.description or "").lower():
                return p.device
    for pat in ["/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*",
                "/dev/ttyACM*", "/dev/ttyUSB*"]:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


class Port:
    """Serial port to the Mega; a reader thread posts ("mega", port, line)."""

    def __init__(self, path):
        self.path = path
        self.closed = False
        if serial:
            self.s = serial.Serial(path, BAUD, timeout=0.1)
        else:  # stdlib fallback for Mac/Linux
            import termios
            self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
            a = termios.tcgetattr(self.fd)
            a[0] = a[1] = a[3] = 0
            a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            a[4] = a[5] = getattr(termios, "B%d" % BAUD, termios.B9600)
            a[6][termios.VMIN] = 0
            a[6][termios.VTIME] = 1
            termios.tcsetattr(self.fd, termios.TCSANOW, a)
            if sys.platform == "darwin":  # macOS: non-standard rates need IOSSIOSPEED
                import fcntl
                fcntl.ioctl(self.fd, 0x80085402, struct.pack("L", BAUD))
        self.lock = threading.Lock()
        start_thread(self._reader)

    def _read(self):
        if serial:
            return self.s.read(max(1, self.s.in_waiting))
        import select
        r, _, _ = select.select([self.fd], [], [], 0.2)
        if not r:
            return b""
        data = os.read(self.fd, 4096)
        if not data:
            raise OSError("device gone")
        return data

    def _reader(self):
        buf = b""
        try:
            while not self.closed:
                buf += self._read()
                *done, buf = buf.split(b"\n")
                for l in done:
                    l = l.strip(b"\r")
                    if l:
                        events.put(("mega", self, l.decode(errors="replace")))
        except Exception as e:  # unplugged
            if not self.closed:
                events.put(("lost", self, str(e)))

    def send(self, line):
        data = (line + "\n").encode(errors="replace")
        with self.lock:
            if serial:
                self.s.write(data)
            else:
                while data:
                    n = os.write(self.fd, data)
                    data = data[n:]

    def close(self):
        self.closed = True
        try:
            self.s.close() if serial else os.close(self.fd)
        except Exception:
            pass


def connect(cfg, verbose=True):
    """Open the Mega and wait for it to boot (opening the port resets it)."""
    while True:
        path = find_port(cfg)
        if path:
            try:
                p = Port(path)
                start, asked = time.time(), False
                while time.time() - start < 4:
                    try:
                        kind, src, line = events.get(timeout=0.2)
                    except queue.Empty:
                        kind = None
                    if kind == "mega" and src is p and line.startswith("HELLO littlelx-mega"):
                        print(f"Mega connected on {path}")
                        return p
                    if not asked and time.time() - start > 2:
                        p.send("?")  # in case it did not reset
                        asked = True
                p.close()
                if verbose:
                    print(f"{path}: no littlelx firmware answered (flash mega/littlelx_mega first)")
            except Exception as e:
                if verbose:
                    print(f"{path}: {e}")
        elif verbose:
            print("Waiting for the controller to be plugged in...")
        verbose = False
        time.sleep(2)


# ------------------------------------------------- setup stored on the Mega
#
# The learned wiring + calibrations live in the Mega's EEPROM, so they travel
# with the controller to any computer. Layout: 0-1 "LX", 2-71 pin modes (the
# Mega applies them at power-up), 128+ "CF" len16 crc32 zlib(JSON).

EE_MODES, EE_BLOB, EE_SIZE = 0, 128, 4096


def wait_line(ser, pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            kind, src, line = events.get(timeout=0.1)
        except queue.Empty:
            continue
        if kind == "lost" and src is ser:
            raise OSError("controller disconnected")
        if kind == "mega" and src is ser and pred(line):
            return line
    return None


def mega_read(ser, addr, n):
    out = b""
    while len(out) < n:
        a, k = addr + len(out), min(32, n - len(out))
        for _ in range(3):
            ser.send(f"R{a} {k}")
            line = wait_line(ser, lambda l: l.startswith(f"E{a} "), 1.0)
            if line:
                break
        else:
            raise TimeoutError("no EEPROM answer (old Mega firmware?)")
        out += bytes.fromhex(line.split(" ", 1)[1])
    return out


def mega_write(ser, addr, data):
    for off in range(0, len(data), 32):
        a = addr + off
        for _ in range(3):
            ser.send(f"W{a} {data[off:off + 32].hex()}")
            if wait_line(ser, lambda l: l == f"W{a} OK", 2.0):
                break
        else:
            raise TimeoutError("EEPROM write not confirmed")


def learned(cfg):
    hw = cfg["hw"]
    return any(hw["faders"] + hw["keys"] + hw["encoders"])


def setup_blob(cfg):
    return {"hw": cfg["hw"], "touch_cal": cfg.get("touch_cal")}


def save_to_mega(cfg, ser):
    data = zlib.compress(json.dumps(setup_blob(cfg), separators=(",", ":")).encode(), 9)
    if len(data) > EE_SIZE - EE_BLOB - 8:
        print("Setup too big for the controller's memory; kept on this computer only.")
        return
    modes = bytearray([0xff] * 70)
    for pin, m in cfg["hw"]["pin_modes"].items():
        modes[int(pin)] = int(m)
    try:
        mega_write(ser, EE_BLOB + 8, data)  # data first, header (with CRC) last
        mega_write(ser, EE_BLOB, b"CF" + struct.pack("<HI", len(data), zlib.crc32(data)))
        mega_write(ser, EE_MODES, b"LX" + bytes(modes))
        print("Setup saved on the controller (it travels with it to any computer).")
    except (TimeoutError, OSError) as e:
        print(f"Couldn't save the setup on the controller ({e}); it is saved on this computer.")


def load_from_mega(ser):
    hdr = mega_read(ser, EE_BLOB, 8)
    if hdr[:2] != b"CF":
        return None
    n, crc = struct.unpack("<HI", hdr[2:])
    if not 0 < n <= EE_SIZE - EE_BLOB - 8:
        return None
    data = mega_read(ser, EE_BLOB + 8, n)
    if zlib.crc32(data) != crc:
        return None
    return json.loads(zlib.decompress(data))


def sync_setup(cfg, ser):
    """Controller copy wins; if it has none, give it ours. Returns True if cfg changed."""
    try:
        blob = load_from_mega(ser)
    except TimeoutError:
        print("This Mega firmware can't store the setup yet: re-flash mega/littlelx_mega.")
        return False
    except (OSError, ValueError, zlib.error):
        blob = None
    if blob and blob.get("hw") and any(blob["hw"].get(k) for k in ("faders", "keys", "encoders")):
        if blob != setup_blob(cfg):
            cfg["hw"] = blob["hw"]
            cfg["touch_cal"] = blob.get("touch_cal")
            save_config(cfg)
            print("Loaded the learned setup from the controller.")
            return True
        return False
    if learned(cfg):
        print("Copying this computer's learned setup onto the controller...")
        save_to_mega(cfg, ser)
    return False


# -------------------------------------------------------------- touchscreen

C_BG, C_PANEL, C_TEXT, C_DIM = "101418", "1c2430", "ffffff", "8899aa"
C_BTN, C_BTN_ON, C_BAR, C_BAR_BG = "2c3546", "c08a1e", "2f7de1", "232b38"
C_KEY2, C_OK, C_WAIT, C_CMD = "3a4458", "38c172", "5a6578", "ffd36b"


def esc(text):
    return str(text).replace("\n", "\\n")


class Screen:
    """Builds the touchscreen pages out of Pi widgets. Adapts to portrait or
    landscape using the size the panel reports in its HELLO.

    Every widget's current state is kept here, so the whole screen can be
    re-sent bit by bit in the background: anything lost on the serial link
    reappears within a few seconds."""
    HDR_PAGE, HDR_MID, HDR_STATUS = 0, 1, 2
    FADER0 = 10
    BTN0 = 20
    CMDLINE = 39
    KEY0 = 40

    KEYPAD = [
        ["Fixture", "7", "8", "9", "Thru"],
        ["Group", "4", "5", "6", "+"],
        ["Preset", "1", "2", "3", "-"],
        ["Cue", "0", ".", "At", "Full"],
        ["Back", "Clear", "Exec", "<-", "Please"],
    ]

    def __init__(self, bridge):
        self.b = bridge
        self.name = "main"
        self.cmdline = ""      # local command line (used when MA isn't linked)
        self.keymap = {}
        self.w, self.h = 320, 480
        self.state = {}        # wid -> widget fields
        self.refresh_ids = []
        self.refresh_i = 0

    @property
    def portrait(self):
        return self.h > self.w

    def send(self, line):
        self.b.to_pi(line)

    # ---- widget state
    @staticmethod
    def wline(wid, s):
        return (f"W {wid} {s['kind']} {s['x']} {s['y']} {s['w']} {s['h']} {s['bg']} {s['fg']} {s['ac']} "
                f"{s['font']} {s['align']} {s['value']} {esc(s['text'])}")

    def widget(self, wid, kind, x, y, w, h, bg, fg, ac, font, align, value, text, marker=-1):
        s = dict(kind=kind, x=x, y=y, w=w, h=h, bg=bg, fg=fg, ac=ac, font=font, align=align,
                 value=value, text=text, marker=marker)
        self.state[wid] = s
        self.send(self.wline(wid, s))
        if marker >= 0:
            self.send(f"M {wid} {marker}")

    def setw(self, wid, value=None, text=None, colors=None, marker=None):
        s = self.state.get(wid)
        if not s:
            return
        if value is not None and value != s["value"]:
            s["value"] = value
            self.send(f"V {wid} {value}")
        if text is not None and text != s["text"]:
            s["text"] = text
            self.send(f"T {wid} {esc(text)}")
        if colors is not None and tuple(colors) != (s["bg"], s["fg"], s["ac"]):
            s["bg"], s["fg"], s["ac"] = colors
            self.send(f"C {wid} {s['bg']} {s['fg']} {s['ac']}")
        if marker is not None and marker != s["marker"]:
            s["marker"] = marker
            self.send(f"M {wid} {marker}")

    def refresh_step(self):
        """Re-send one widget (round robin). Identical redraws cost nothing on the Pi."""
        if not self.refresh_ids:
            self.refresh_ids = sorted(self.state)
            self.refresh_i = 0
        if not self.refresh_ids:
            return
        wid = self.refresh_ids[self.refresh_i % len(self.refresh_ids)]
        self.refresh_i += 1
        if self.refresh_i >= len(self.refresh_ids):
            self.refresh_ids = []
        s = self.state.get(wid)
        if s:
            self.send(self.wline(wid, s))
            if s["marker"] >= 0:
                self.send(f"M {wid} {s['marker']}")

    # ---- layout
    def header_h(self):
        return 54 if self.portrait else 30

    def mid_text(self):
        b = self.b
        if b.ma_linked():
            line = b.ma["busy"] or b.ma["cmdline"]
            return "> " + line if line else ">"
        return b.last_cmd

    def header(self):
        w = self.w
        page = f"Page {self.b.page}"
        if self.portrait:
            self.widget(self.HDR_PAGE, "L", 0, 0, w // 2, 30, C_PANEL, C_TEXT, C_PANEL, 1, 1, 0, page)
            self.widget(self.HDR_MID, "L", 0, 30, w, 24, C_PANEL, C_CMD, C_PANEL, 0, 1, 0, self.mid_text())
        else:
            self.widget(self.HDR_PAGE, "L", 0, 0, 120, 30, C_PANEL, C_TEXT, C_PANEL, 1, 1, 0, page)
            self.widget(self.HDR_MID, "L", 120, 0, w - 260, 30, C_PANEL, C_CMD, C_PANEL, 0, 0, 0, self.mid_text())
        x = self.w // 2 if self.portrait else self.w - 140
        self.widget(self.HDR_STATUS, "L", x, 0, self.w - x, 30, C_PANEL, C_DIM, C_PANEL, 0, 2, 0, "")
        self.update_header()

    def update_header(self):
        b = self.b
        if b.ma_linked():
            text, color = "MA3 linked", C_OK
        elif b.ma_seen and time.time() - b.ma_seen < 5:
            text, color = "MA3 online", C_OK
        else:
            text, color = f"OSC > {b.cfg['osc']['host']}", C_DIM
        self.setw(self.HDR_STATUS, text=text, colors=(C_PANEL, color, C_PANEL))
        self.setw(self.HDR_PAGE, text=f"Page {b.page}")
        self.setw(self.HDR_MID, text=self.mid_text())

    def draw(self):
        self.state = {}
        self.refresh_ids = []
        self.send("CLR")
        self.send(f"BG {C_BG}")
        self.header()
        if self.name == "keypad":
            self.draw_keypad()
        else:
            self.draw_main()

    def draw_main(self):
        w, h = self.w, self.h
        top = self.header_h() + 6
        fh = 180 if self.portrait else 150
        fw = (w - 4) // 5
        for i in range(5):
            self.widget(self.FADER0 + i, "v", 4 + i * fw, top, fw - 3, fh, C_BAR_BG, C_TEXT, C_BAR,
                        0, 0, 0, "")
            self.update_fader(i)
        cols = 3 if self.portrait else 5
        rows = 4 if self.portrait else 2
        btns = self.b.cfg["touch_buttons"][:cols * rows]
        by = top + fh + 6
        bw = (w - 4) // cols
        bh = (h - by - 2) // rows
        self.keymap = {}
        for n, btn in enumerate(btns):
            r, c = divmod(n, cols)
            wid = self.BTN0 + n
            self.keymap[wid] = btn
            self.widget(wid, "B", 4 + c * bw, by + r * bh, bw - 3, bh - 6, btn.get("color", C_BTN),
                        C_TEXT, C_BTN_ON, 1, 0, self.button_lit(btn), btn.get("label", "?"))

    def draw_keypad(self):
        w, h = self.w, self.h
        top = self.header_h() + 4
        self.keymap = {}
        self.widget(self.CMDLINE, "L", 4, top, w - 8, 40, "000000", C_CMD, "000000", 1, 1, 0,
                    self.keypad_text())
        ky = top + 44
        bw = (w - 4) // 5
        bh = (h - ky - 2) // 5
        for r, row in enumerate(self.KEYPAD):
            for c, label in enumerate(row):
                wid = self.KEY0 + r * 5 + c
                self.keymap[wid] = {"keypad": label}
                digit = label.isdigit() or label == "."
                color = C_BTN_ON if label == "Please" else C_BTN if digit else C_KEY2
                font = 0 if self.portrait and len(label) > 3 else 1
                self.widget(wid, "B", 4 + c * bw, ky + r * bh, bw - 3, bh - 4, color, C_TEXT, C_BTN_ON,
                            font, 0, 0, label)

    # ---- live updates
    def button_lit(self, btn):
        ma = self.b.ma
        if "state" in btn:
            return 1 if ma["master"].get(btn["state"].lower()) else 0
        if "exec" in btn:
            return 1 if ma["run"].get(btn["exec"]) else 0
        return 0

    def update_buttons(self):
        if self.name != "main":
            return
        for wid, btn in self.keymap.items():
            self.setw(wid, value=self.button_lit(btn))

    def update_fader(self, i):
        """Bar = MA's real level (when known), marker = the physical fader."""
        if self.name != "main" or (self.FADER0 + i) not in self.state:
            return
        b = self.b
        f = b.cfg["faders"][i]
        ma = b.ma_fader(i)
        pos = b.fader_pos[i]
        shown = ma if ma is not None else pos
        name = b.ma["name"].get(f["exec"]) if b.ma_linked() else None
        name = name or f.get("name") or (str(f["exec"]) if self.portrait else f"Exec {f['exec']}")
        if self.portrait:
            name = name[:7]
        text = f"{name}\n" + ("--" if shown is None else f"{round(shown)}%")
        waiting = ma is not None and pos is not None and not b.picked[i]
        if waiting:
            text += "\n" + ("^ ^ ^" if pos < ma else "v v v")
        marker = int(pos * 10) if (ma is not None and pos is not None) else -1
        self.setw(self.FADER0 + i, value=int((shown or 0) * 10), text=text, marker=marker,
                  colors=(C_BAR_BG, C_CMD if waiting else C_TEXT, C_WAIT if waiting else C_BAR))

    def keypad_text(self):
        if self.b.ma_linked():
            return (self.b.ma["busy"] or self.b.ma["cmdline"]) + "_"
        return self.cmdline + "_"

    def update_cmdline(self):
        self.setw(self.HDR_MID, text=self.mid_text())
        if self.name == "keypad":
            self.setw(self.CMDLINE, text=self.keypad_text())

    def set_screen(self, name):
        self.name = name
        self.draw()

    def on_press(self, wid):
        act = self.keymap.get(wid)
        if not act:
            return
        if "keypad" in act:
            if act["keypad"] == "Back":
                self.set_screen("main")
            else:
                self.b.ma_key(act["keypad"])
        else:
            self.b.do_action(act, True)

    def on_release(self, wid):
        act = self.keymap.get(wid)
        if act and "keypad" not in act:
            self.b.do_action(act, False)

    def local_key(self, k):
        """Command line kept here when MA isn't linked (sent on Please)."""
        if k == "<-":
            self.cmdline = self.cmdline.rstrip()
            self.cmdline = self.cmdline[:self.cmdline.rfind(" ") + 1] if " " in self.cmdline else ""
        elif k == "Clear":
            if self.cmdline:
                self.cmdline = ""
            else:
                self.b.ma_cmd("Clear")
        elif k == "Please":
            if self.cmdline.strip():
                self.b.ma_cmd(self.cmdline.strip())
            self.cmdline = ""
        elif k.isdigit() or k == ".":
            self.cmdline += k
        else:
            if self.cmdline and not self.cmdline.endswith(" "):
                self.cmdline += " "
            self.cmdline += k + " "
        self.update_cmdline()


# ------------------------------------------------------------------ bridge

class Encoder:
    # quadrature transition table: (prev<<2 | cur) -> step
    TABLE = [0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0]

    def __init__(self, div):
        self.state = 3
        self.acc = 0
        self.div = max(1, div)

    def update(self, a, b):
        cur = (a << 1) | b
        self.acc += self.TABLE[(self.state << 2) | cur]
        self.state = cur
        detents = int(self.acc / self.div)
        self.acc -= detents * self.div
        return detents


def resource(*parts):
    """A file shipped with the bridge (also inside the Windows .exe)."""
    base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(base, *parts)


class Bridge:
    PI_TX_RATE = 20000   # bytes/s to the touchscreen, well under what the Mega relays

    def __init__(self, cfg):
        self.cfg = cfg
        self.page = cfg.get("page", 1)
        self.ser = None
        self.screen = Screen(self)
        self.fader_pos = [None] * 5          # physical position 0..100
        self.fader_sent = [None] * 5
        self.picked = [True] * 5             # physical fader is in control (soft takeover)
        self.fader_cmd_at = [0.0] * 5
        self.fader_cmd_pending = [None] * 5
        self.verbose = False
        self.ma = dict(alive=0.0, page=None, fader={}, run={}, name={}, master={}, cmdline="",
                       busy="", busy_at=0.0)
        self.ma_values = {}                  # (page, exec) -> 0..100 from plain OSC feedback
        self.ma_seen = 0
        self.ma_installed_at = 0.0
        self.last_cmd = ""
        self.pins = {}
        self.encs = []
        self.pi_ready = False
        self.pi_proto = 0
        self.tx_free_at = 0.0
        self.build_maps()

        o = cfg["osc"]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dest = (o["host"], int(o["port"]))
        self.prefix = o["prefix"].rstrip("/")
        try:
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.bind(("0.0.0.0", int(o["listen_port"])))
            start_thread(self.osc_reader, rx)
        except OSError as e:
            print(f"Can't listen for MA feedback on port {o['listen_port']}: {e}")

    @staticmethod
    def osc_reader(rx):
        while True:
            try:
                data, _ = rx.recvfrom(65536)
            except OSError:
                continue  # Windows reports ICMP errors here; ignore
            for msg in osc_parse(data):
                events.put(("osc", None, msg))

    def build_maps(self):
        hw = self.cfg["hw"]
        self.digital = {}   # pin -> ("key", i) | ("enc", i, "a"/"b"/"push")
        self.analog = {}    # ch -> fader index
        for i, f in enumerate(hw["faders"]):
            if f:
                self.analog[f["ch"]] = i
        self.matrix = {}    # (a, b) -> ("key", i) | ("enc", i, "push")
        for i, k in enumerate(hw["keys"]):
            if k and "pair" in k:
                self.matrix[tuple(k["pair"])] = ("key", i)
            elif k:
                self.digital[k["pin"]] = ("key", i)
        self.encs = []
        for i, e in enumerate(hw["encoders"]):
            self.encs.append(Encoder(e.get("div", 4)) if e else None)
            if e:
                self.digital[e["a"]] = ("enc", i, "a")
                self.digital[e["b"]] = ("enc", i, "b")
                if isinstance(e.get("push"), list):
                    self.matrix[tuple(e["push"])] = ("enc", i, "push")
                elif e.get("push") is not None:
                    self.digital[e["push"]] = ("enc", i, "push")

    # ---- MA state
    def ma_linked(self):
        return time.time() - self.ma["alive"] < 6

    def ma_fader(self, i):
        """MA's real level for fader i on the current page, if known."""
        ex = self.cfg["faders"][i]["exec"]
        if self.ma_linked():
            return self.ma["fader"].get(ex)
        return self.ma_values.get((self.page, ex))

    # ---- output
    def osc(self, addr, *args):
        try:
            self.sock.sendto(osc_message(self.prefix + addr, *args), self.dest)
        except OSError as e:
            print(f"OSC send failed: {e}")

    def ma_cmd(self, cmd):
        self.osc("/cmd", cmd)
        self.last_cmd = cmd
        print(f"cmd: {cmd}")
        if self.pi_ready:
            self.screen.update_cmdline()

    def ma_plugin(self, arg):
        """Run an action in the littlelx code installed in MA (see ma3/littlelx.lua)."""
        arg = re.sub(r"[^A-Za-z0-9 .+\-_<>/]", "", arg)
        self.osc("/cmd", "Lua \"local c=GetVar(GlobalVars(),'llx_code') "
                         f"if c then load(c)()(nil,'{arg}') end\"")
        if self.verbose:
            print(f"MA: {arg}")

    def ma_key(self, k):
        """A console key: typed into MA's real command line when linked."""
        if not self.ma_linked():
            self.screen.local_key(k)
            return
        verb = {"Please": "please", "Clear": "clear", "<-": "back"}.get(k)
        self.ma_plugin(verb or f"type {k}")

    def install_ma3(self):
        """Put the littlelx code into MA3 over OSC and start it (no import needed)."""
        try:
            src = open(resource("ma3", "littlelx.lua"), "rb").read()
        except OSError as e:
            print(f"Can't find the MA3 code to install ({e})")
            return
        conf = self.cfg.get("ma3", {})
        line, tick = int(conf.get("osc_line", 2)), float(conf.get("tick", 0.1))
        print(f"Installing littlelx into MA3 (it reports back on OSC line {line})...")
        self.ma_installed_at = time.time()
        put = "Lua \"SetVar(GlobalVars(),'llx_src',GetVar(GlobalVars(),'llx_src')..'{}')\""
        self.osc("/cmd", "Lua \"SetVar(GlobalVars(),'llx_src','')\"")
        h = src.hex()
        for off in range(0, len(h), 240):
            time.sleep(0.01)
            self.osc("/cmd", put.format(h[off:off + 240]))
        time.sleep(0.05)
        self.osc("/cmd", "Lua \"local h=GetVar(GlobalVars(),'llx_src') "
                         "local c=h:gsub('..',function(x) return string.char(tonumber(x,16)) end) "
                         "SetVar(GlobalVars(),'llx_code',c) "
                         f"load(c)()(nil,'__start {line} {tick}')\"")

    def to_pi(self, line):
        if not self.ser:
            return
        if self.pi_proto >= 3:  # checksum: the Pi drops lines damaged on the way
            x = 0
            for c in line.encode(errors="replace"):
                x ^= c
            line = f"{line}*{x:02X}"
        # pace the output so the Mega's small serial buffers can't overflow
        now = time.time()
        self.tx_free_at = max(now, self.tx_free_at) + (len(line) + 2) / self.PI_TX_RATE
        if self.tx_free_at - now > 0.05:
            time.sleep(self.tx_free_at - now - 0.05)
        try:
            self.ser.send(">" + line)
        except Exception:
            pass  # reader thread reports the disconnect

    def send_fader(self, i, value):
        fmt = self.cfg["osc"].get("fader_type", "i")
        if fmt.startswith("cmd"):  # command line: max ~25/s per fader, last value always sent
            now = time.time()
            if now - self.fader_cmd_at[i] < 0.04:
                self.fader_cmd_pending[i] = value
                return
            self.fader_cmd_at[i] = now
            self.fader_cmd_pending[i] = None
        self.fader_sent[i] = value
        addr, arg = fader_message(fmt, self.page, self.cfg["faders"][i]["exec"], value)
        self.osc(addr, arg)
        if self.verbose:
            print(f"fader {i + 1}: {round(value)}%  ->  {addr} {arg}")

    def flush_faders(self):
        for i, v in enumerate(self.fader_cmd_pending):
            if v is not None and time.time() - self.fader_cmd_at[i] >= 0.04:
                self.send_fader(i, v)

    def do_action(self, act, down):
        if "exec" in act:
            self.osc(f"/Page{self.page}/Key{act['exec']}", 1 if down else 0)
        elif not down:
            return
        elif "key" in act:
            self.ma_key(act["key"])
        elif "cmd" in act:
            self.ma_cmd(act["cmd"])
        elif "page" in act:
            self.set_page(self.page + int(act["page"]))
        elif "screen" in act:
            self.screen.set_screen(act["screen"] if self.screen.name != act["screen"] else "main")

    def set_page(self, page, from_ma=False):
        page = max(1, page)
        if not from_ma:
            self.osc("/cmd", f"Page {page}")  # MA follows; the plugin confirms
        if page == self.page:
            return
        self.page = page
        self.cfg["page"] = page
        self.ma["fader"], self.ma["run"], self.ma["name"] = {}, {}, {}
        for i in range(5):  # new page: catch each fader again before it takes over
            self.picked[i] = not self.cfg.get("pickup", True)
        if self.pi_ready:
            self.screen.update_header()
            self.screen.update_buttons()
            for i in range(5):
                self.screen.update_fader(i)

    # ---- input from the Mega
    def on_fader(self, i, value):
        prev = self.fader_pos[i]
        self.fader_pos[i] = value
        if prev is None:
            # First reading after connecting: only note where the fader is.
            # Sending it would yank MA's executor to wherever the knob sits.
            if self.pi_ready:
                self.screen.update_fader(i)
            return
        ma = self.ma_fader(i)
        if not self.picked[i]:
            if ma is None or not self.cfg.get("pickup", True):
                self.picked[i] = True
            else:  # soft takeover: take control once the fader reaches MA's level
                crossed = prev is not None and (prev - ma) * (value - ma) <= 0
                self.picked[i] = crossed or abs(value - ma) <= 2
        if self.picked[i] and (self.fader_sent[i] is None or round(value) != round(self.fader_sent[i])):
            self.send_fader(i, value)
        if self.pi_ready:
            self.screen.update_fader(i)

    def on_mega(self, line):
        if line.startswith(">"):
            self.on_pi(line[1:])
            return
        k = re.match(r"K(\d+) (\d+) ([01])$", line)
        if k:  # matrix key
            what = self.matrix.get((int(k.group(1)), int(k.group(2))))
            down = k.group(3) == "1"
            if what and what[0] == "key":
                act = self.cfg["keys"][what[1]] if what[1] < len(self.cfg["keys"]) else None
                if act:
                    self.do_action(act, down)
            elif what:
                act = self.cfg["encoders"][what[1]] if what[1] < len(self.cfg["encoders"]) else {}
                if act.get("push"):
                    self.do_action(act["push"], down)
            return
        m = re.match(r"([DA])(\d+) (\d+)$", line)
        if not m:
            if line.startswith("PI "):
                print("Touchscreen on Serial" + line[3:] if line[3:] != "0" else "Touchscreen not found yet")
            return
        kind, n, v = m.group(1), int(m.group(2)), int(m.group(3))
        if kind == "A":
            i = self.analog.get(n)
            if i is not None:
                f = self.cfg["hw"]["faders"][i]
                lo, hi = f.get("lo", 0), f.get("hi", 1023)
                pct = (v - lo) * 100.0 / ((hi - lo) or 1)
                self.on_fader(i, min(100.0, max(0.0, pct)))
            return
        self.pins[n] = v
        what = self.digital.get(n)
        if not what:
            return
        if what[0] == "key":
            i = what[1]
            act = self.cfg["keys"][i] if i < len(self.cfg["keys"]) else None
            if act:
                self.do_action(act, v == 0)
        else:
            i, part = what[1], what[2]
            e = self.cfg["hw"]["encoders"][i]
            act = self.cfg["encoders"][i] if i < len(self.cfg["encoders"]) else {}
            if part == "push":
                if act.get("push"):
                    self.do_action(act["push"], v == 0)
                return
            d = self.encs[i].update(self.pins.get(e["a"], 1), self.pins.get(e["b"], 1))
            if d and e.get("reverse"):
                d = -d
            if d:
                self.on_encoder(act, d)

    def on_encoder(self, act, d):
        if "page" in act:
            self.set_page(self.page + d * int(act["page"]))
        elif "cmd" in act:
            step = d * act.get("step", 1)
            cmd = act["cmd"].replace("{d}", f"{abs(step)}")
            if step < 0:
                cmd = cmd.replace("+ ", "- ", 1)
            self.ma_cmd(cmd)

    def on_pi(self, line):
        parts = line.split()
        if not parts:
            return
        if parts[0] == "HELLO":
            if len(parts) >= 5:
                self.screen.w, self.screen.h = int(parts[3]), int(parts[4])
            self.pi_proto = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
            ver = parts[5] if len(parts) > 5 else "old"
            print(f"Touchscreen connected ({self.screen.w}x{self.screen.h}, firmware {ver})")
            if self.pi_proto < 3:
                print("  Touchscreen firmware is out of date (no fader markers, unchecked link):"
                      " update it with --update-pi")
            self.pi_ready = True
            cal = self.cfg.get("touch_cal")
            if cal:
                self.to_pi("K " + " ".join(str(x) for x in cal))
            self.screen.draw()
        elif parts[0] == "STAT":
            st = dict(kv.split("=", 1) for kv in parts[1:] if "=" in kv)
            errs = {k: int(v) for k, v in st.items() if k != "lines" and v.isdigit() and int(v)}
            if errs and errs != getattr(self, "last_stat", None):
                self.last_stat = errs
                lines = int(st.get("lines", "0") or 0)
                bad = errs.get("bad", 0)
                print(f"Link to touchscreen: {bad} of {lines} lines damaged"
                      f" ({100.0 * bad / max(lines, 1):.2f}%), UART frame errors {errs.get('frame', 0)},"
                      f" overruns {errs.get('overrun', 0)}, buffer losses {errs.get('lost', 0)}")
                if errs.get("frame"):
                    print("  frame errors = electrical: check the Mega TX3 -> divider -> Pi RX wire and GND")
                if errs.get("overrun") or errs.get("lost"):
                    print("  overruns = the Pi was too busy to keep up (usually under-voltage throttling)")
        elif parts[0] == "INFO":
            print("Touchscreen status: " + " ".join(parts[1:]))
            if "fb=missing" in parts:
                print("  -> no display found: check the dtoverlay= line in the SD card's config.txt")
        elif parts[0] == "P" and len(parts) > 1:
            self.screen.on_press(int(parts[1]))
        elif parts[0] == "R" and len(parts) > 1:
            self.screen.on_release(int(parts[1]))
        elif parts[0] == "S" and len(parts) > 2:
            i = int(parts[1]) - Screen.FADER0
            if 0 <= i < 5:  # dragging a fader on the screen sets MA directly
                self.send_fader(i, int(parts[2]) / 10.0)
                self.picked[i] = False  # the physical fader catches it again
                self.screen.update_fader(i)
        elif parts[0] == "CALD":
            self.cfg["touch_cal"] = [float(x) for x in parts[1:7]]
            save_config(self.cfg)
            print("Touch calibration saved")
            save_to_mega(self.cfg, self.ser)
            self.screen.draw()

    # ---- MA feedback
    def on_osc(self, addr, args):
        self.ma_seen = time.time()
        if "/littlelx/" in addr:
            self.on_littlelx(addr.split("/littlelx/", 1)[1], args)
            return
        m = re.search(r"/Page(\d+)/Fader(\d+)$", addr)  # plain MA OSC feedback
        if not m:
            return
        page, ex = int(m.group(1)), int(m.group(2))
        nums = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
        if nums:
            self.ma_values[(page, ex)] = float(nums[-1])
        if page == self.page and self.pi_ready:
            for i, f in enumerate(self.cfg["faders"]):
                if f["exec"] == ex:
                    self.screen.update_fader(i)

    def on_littlelx(self, what, args):
        """State reported by the code running inside MA3."""
        ma = self.ma
        val = args[0] if args else None
        was_linked = self.ma_linked()
        ma["alive"] = time.time()
        if not was_linked:
            print("MA3 linked: page, faders, key states and command line follow MA.")
        scr = self.screen if self.pi_ready else None
        if what == "page" and isinstance(val, int):
            self.set_page(val, from_ma=True)
        elif what.startswith(("fader/", "run/", "name/")):
            kind, ex = what.split("/", 1)
            ex = int(ex)
            if kind == "fader":
                v = float(val)
                ma["fader"][ex] = v
                for i, f in enumerate(self.cfg["faders"]):
                    if f["exec"] == ex:
                        sent = self.fader_sent[i]
                        # moved in MA by someone else: the physical fader must catch it
                        if self.picked[i] and self.fader_pos[i] is not None and abs(v - self.fader_pos[i]) > 3 \
                                and (sent is None or abs(v - sent) > 1.5):
                            self.picked[i] = not self.cfg.get("pickup", True)
                        if scr:
                            scr.update_fader(i)
            elif kind == "run":
                ma["run"][ex] = int(val or 0)
                if scr:
                    scr.update_buttons()
            else:
                ma["name"][ex] = str(val or "")
                for i, f in enumerate(self.cfg["faders"]):
                    if f["exec"] == ex and scr:
                        scr.update_fader(i)
        elif what.startswith("master/"):
            ma["master"][what.split("/", 1)[1].lower()] = int(val or 0)
            if scr:
                scr.update_buttons()
        elif what == "cmdline":
            ma["cmdline"] = str(val or "")
            ma["busy"] = ""
            if scr:
                scr.update_cmdline()
        elif what == "busy":
            ma["busy"], ma["busy_at"] = str(val or ""), time.time()
            if scr:
                scr.update_cmdline()
        elif what == "started":
            print(f"MA3 code running (update every {val} s).")
        if scr and not was_linked:
            scr.update_header()

    # ---- main loop
    def run(self):
        last_ping = last_status = last_refresh = 0
        started = time.time()
        while True:
            self.ser = connect(self.cfg)
            self.pi_ready = False
            try:
                if sync_setup(self.cfg, self.ser):
                    self.build_maps()
            except OSError:
                continue
            if not learned(self.cfg):
                print("No wiring learnt yet: run with --learn.")
            for pin, mode in self.cfg["hw"]["pin_modes"].items():
                self.ser.send(f"M{pin} {mode}")
            self.ser.send("?")
            self.to_pi("?")
            while True:
                try:
                    kind, src, data = events.get(timeout=0.05)
                except queue.Empty:
                    kind = None
                if kind == "mega" and src is self.ser:
                    self.on_mega(data)
                elif kind == "osc":
                    self.on_osc(*data)
                elif kind == "lost" and src is self.ser:
                    print(f"Controller disconnected ({data}), reconnecting...")
                    self.ser.close()
                    self.ser = None
                    time.sleep(1)
                    break
                now = time.time()
                self.flush_faders()
                if (self.cfg.get("ma3", {}).get("auto_install", True) and not self.ma_linked()
                        and now - started > 4 and now - self.ma_installed_at > 30):
                    self.install_ma3()
                if self.ma["busy"] and now - self.ma["busy_at"] > 3:
                    self.ma["busy"] = ""
                    if self.pi_ready:
                        self.screen.update_cmdline()
                if now - last_ping > 1:
                    last_ping = now
                    self.to_pi("PING")
                if self.pi_ready and now - last_status > 1:
                    last_status = now
                    self.screen.update_header()
                if self.pi_ready and now - last_refresh > 0.1:
                    last_refresh = now
                    self.screen.refresh_step()


# ------------------------------------------------------------------- tools

def wait_event(ser, accept, timeout=None):
    """Wait for a Mega line accepted by accept(); Enter on the keyboard returns None."""
    end = time.time() + timeout if timeout else None
    while True:
        try:
            kind, src, data = events.get(timeout=0.1)
        except queue.Empty:
            kind = None
        if kind == "stdin":
            return None
        if kind == "lost" and src is ser:
            sys.exit("Controller disconnected.")
        if kind == "mega" and src is ser:
            res = accept(data)
            if res is not None:
                return res
        if end and time.time() > end:
            return None


def drain():
    while True:
        try:
            events.get_nowait()
        except queue.Empty:
            return


def learn(cfg):
    ser = connect(cfg)
    start_thread(stdin_reader)
    hw = cfg["hw"]
    pin_modes = {}
    print("\nlittlelx learn mode. Press Enter at any prompt to skip that control.\n")
    for p in range(2, 54):
        ser.send(f"M{p} 2")
    for ch in range(16):
        ser.send(f"M{54 + ch} 3")
    time.sleep(0.3)
    drain()

    # ---- faders
    used_ch = set()
    for i in range(5):
        print(f"Fader {i + 1}: move it all the way down, then all the way up, then press Enter.")
        seen = {}

        def acc(line):
            m = re.match(r"A(\d+) (\d+)$", line)
            if m:
                ch, v = int(m.group(1)), int(m.group(2))
                if ch not in used_ch:
                    seen.setdefault(ch, []).append(v)
            return None
        wait_event(ser, acc)
        # a real fader travels (nearly) end to end; noise and crosstalk don't
        best = max(seen.items(), key=lambda kv: max(kv[1]) - min(kv[1]), default=None)
        if not best or max(best[1]) - min(best[1]) < 600:
            print("  no full fader movement seen (bottom to top), skipped")
            hw["faders"][i] = None
            continue
        ch, vals = best
        first, last = vals[0], vals[-1]
        lo, hi = min(vals), max(vals)
        if last < first:  # moved up but value went down: wired backwards
            lo, hi = hi, lo
        hw["faders"][i] = {"ch": ch, "lo": lo, "hi": hi}
        used_ch.add(ch)
        print(f"  -> A{ch} ({lo}..{hi})")

    # spare analog pins can be buttons
    for ch in range(16):
        mode = 3 if ch in used_ch else 2
        pin_modes[str(54 + ch)] = mode
        ser.send(f"M{54 + ch} {mode}")
    time.sleep(0.3)
    drain()

    used_pins = set()
    used_pairs = set()

    def press(line):
        """A direct button (pin to GND) -> pin, or a matrix key -> [a, b]."""
        m = re.match(r"D(\d+) 0$", line)
        if m and int(m.group(1)) not in used_pins:
            return int(m.group(1))
        m = re.match(r"K(\d+) (\d+) 1$", line)
        if m and (int(m.group(1)), int(m.group(2))) not in used_pairs:
            return [int(m.group(1)), int(m.group(2))]
        return None

    def use(p):
        if isinstance(p, list):
            used_pairs.add(tuple(p))
            return f"matrix pins {p[0]}+{p[1]}"
        used_pins.add(p)
        return f"pin {p}"

    # ---- encoders first (their pins chatter while turning)
    for i in range(2):
        print(f"Encoder {i + 1}: turn it slowly clockwise a few clicks, then press Enter.")
        seq = []

        def acc(line):
            m = re.match(r"D(\d+) ([01])$", line)
            if m and int(m.group(1)) not in used_pins:
                seq.append((int(m.group(1)), int(m.group(2))))
            return None
        wait_event(ser, acc)
        counts = {}
        for p, _ in seq:
            counts[p] = counts.get(p, 0) + 1
        top = sorted(counts, key=counts.get, reverse=True)[:2]
        if len(top) < 2:
            print("  no encoder seen, skipped")
            hw["encoders"][i] = None
            continue
        a, b = sorted(top)
        enc, state, total = Encoder(1), {a: 1, b: 1}, 0
        for p, v in seq:
            if p in state:
                state[p] = v
                total += enc.update(state[a], state[b])
        e = {"a": a, "b": b, "push": None, "div": 4, "reverse": total < 0}
        used_pins.update((a, b))
        for p in (a, b):
            pin_modes[str(p)] = 10  # pullup, no debounce
            ser.send(f"M{p} 10")
        print(f"  -> pins {a}/{b}" + (" (reversed)" if total < 0 else ""))
        print(f"Encoder {i + 1}: now press it (push button), or Enter if it has none.")
        p = wait_event(ser, press)
        if p is not None:
            e["push"] = p
            print(f"  -> push on {use(p)}")
        hw["encoders"][i] = e

    # ---- keys
    for i in range(20):
        print(f"Key {i + 1}: press it (Enter to skip).")
        p = wait_event(ser, press)
        if p is None:
            hw["keys"][i] = None
            print("  skipped")
            continue
        hw["keys"][i] = {"pair": p} if isinstance(p, list) else {"pin": p}
        print(f"  -> {use(p)}")
        time.sleep(0.15)
        drain()

    hw["pin_modes"] = pin_modes
    save_config(cfg)
    save_to_mega(cfg, ser)
    print(f"\nSaved to {CONFIG_PATH}.")
    print("Next: run with --faders to calibrate the fader ends, then without options to start.")


def calibrate_faders(cfg):
    """Record each fader's real bottom and top reading (all faders at once)."""
    ser = connect(cfg)
    sync_setup(cfg, ser)
    hw = cfg["hw"]
    faders = [(i, f) for i, f in enumerate(hw["faders"]) if f]
    if not faders:
        sys.exit("No faders learnt yet: run with --learn first.")
    start_thread(stdin_reader)
    for pin, mode in hw["pin_modes"].items():  # quiet the unused analog pins
        ser.send(f"M{pin} {mode}")
    by_ch = {f["ch"]: i for i, f in faders}
    latest = {}
    NEXT = 1

    def screen(title, button):
        ser.send(">CLR")
        ser.send(">BG 101418")
        ser.send(f">W 0 L 0 0 320 70 1c2430 ffffff 1c2430 1 0 0 {esc(title)}")
        for n, (i, f) in enumerate(faders):
            ser.send(f">W {10 + n} V {6 + n * 63} 80 57 300 232b38 ffffff 2f7de1 0 0 0 F{i + 1}")
        if button:
            ser.send(f">W {NEXT} B 60 396 200 70 c08a1e ffffff c08a1e 1 0 0 {button}")

    def step(title):
        print(title.replace("\\n", " ") + ", then press Enter here or tap Next on the screen.")
        screen(title, "Next")
        ser.send("?")  # Mega replies with every current value
        while True:
            try:
                kind, src, data = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "stdin":
                return
            if kind == "lost" and src is ser:
                sys.exit("Controller disconnected.")
            if kind != "mega" or src is not ser:
                continue
            if data == f">P {NEXT}":
                return
            m = re.match(r"A(\d+) (\d+)$", data)
            if m and int(m.group(1)) in by_ch:
                ch, v = int(m.group(1)), int(m.group(2))
                latest[ch] = v
                n = [i for i, _ in faders].index(by_ch[ch])
                ser.send(f">V {10 + n} {v * 1000 // 1023}")
                ser.send(f">T {10 + n} F{by_ch[ch] + 1}\\n{v}")

    step("Pull ALL faders\\nfully DOWN")
    lows = dict(latest)
    step("Push ALL faders\\nfully UP")
    highs = dict(latest)

    print()
    for i, f in faders:
        lo, hi = lows.get(f["ch"]), highs.get(f["ch"])
        if lo is None or hi is None or abs(hi - lo) < 300:
            print(f"  Fader {i + 1} (A{f['ch']}): didn't move far enough (bottom {lo}, top {hi}), unchanged")
            continue
        margin = (hi - lo) * 0.015  # reach 0 % / 100 % without slamming the ends
        f["lo"], f["hi"] = round(lo + margin), round(hi - margin)
        note = "  (wired upside down - handled)" if hi < lo else ""
        print(f"  Fader {i + 1} (A{f['ch']}): bottom {lo}, top {hi}{note}")
    save_config(cfg)
    save_to_mega(cfg, ser)
    screen("Faders calibrated", None)
    print(f"Saved to {CONFIG_PATH}.")


def calibrate(cfg):
    ser = connect(cfg)
    sync_setup(cfg, ser)
    ser.send(">CAL")
    print("Tap the three crosses on the touchscreen...")

    def acc(line):
        if line.startswith(">CALD "):
            return [float(x) for x in line.split()[1:7]]
        if line.startswith(">HELLO"):
            ser.send(">CAL")
        return None
    cal = wait_event(ser, acc, timeout=120)
    if cal:
        cfg["touch_cal"] = cal
        save_config(cfg)
        save_to_mega(cfg, ser)
    else:
        print("No calibration received.")


def test_faders(cfg):
    """Try each fader message format on a real executor; keep the one that moves it."""
    o = cfg["osc"]
    page, ex = cfg.get("page", 1), cfg["faders"][0]["exec"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    prefix = o["prefix"].rstrip("/")

    def send(fmt, v):
        addr, arg = fader_message(fmt, page, ex, v)
        sock.sendto(osc_message(prefix + addr, arg), (o["host"], int(o["port"])))

    print(f"Testing fader formats on Page {page}, executor {ex} (MA3 at {o['host']}:{o['port']}).")
    print(f"Make sure executor {ex} on page {page} has something on it (e.g. a sequence),")
    print("and watch its fader in MA3. Each test moves it up to 100 %, then back down.\n")
    for fmt, desc in FADER_FORMATS.items():
        input(f"[{fmt}] {desc} - press Enter to try it... ")
        for v in list(range(0, 101, 10)) + list(range(100, -1, -10)):
            send(fmt, v)
            time.sleep(0.08)
        if input("    Did the fader move? [y/N] ").strip().lower().startswith("y"):
            cfg["osc"]["fader_type"] = fmt
            save_config(cfg)
            print(f"\nSaved: faders will use '{fmt}'. Restart the bridge.")
            return
    print("\nNone of them moved it. Check that executor", ex, "has a sequence on page", page,
          "and that the OSC line in MA3 has Receive = Yes (commands working means it does).")


def probe(cfg):
    """Show, live, which pins connect when keys are held (wiring diagnostics)."""
    ser = connect(cfg)
    ser.send("?")
    info = {}
    end = time.time() + 2
    while time.time() < end:
        l = wait_line(ser, lambda l: l.startswith(("MX", "AN")), 0.5)
        if l:
            info[l[:2]] = l[2:].strip()
    print("Pins scanned for key matrices:", info.get("MX", "?"))
    print("Analog inputs (faders):      ", info.get("AN", "?"))
    print("\nHold a key (or several). Ctrl-C to stop.\n")
    last = None
    while True:
        ser.send("X")
        rest, links = "", []
        while True:
            l = wait_line(ser, lambda l: l.startswith("X"), 2)
            if l is None or l == "XE":
                break
            if l.startswith("XR"):
                rest = l[2:].strip()
            else:
                p, *qs = l[1:].split()
                links.append(f"{p}->{','.join(qs)}")
        now = (rest, tuple(links))
        if now != last:
            last = now
            print(time.strftime("%H:%M:%S"),
                  "| low at rest:", rest or "-",
                  "| pulling a pin low also pulls:", " ".join(links) or "nothing")
        time.sleep(0.3)


def monitor(cfg):
    ser = connect(cfg)
    ser.send("?")
    ser.send(">?")
    while True:
        try:
            kind, src, data = events.get(timeout=1)
        except queue.Empty:
            continue
        if kind == "mega":
            print(data)
        elif kind == "lost":
            sys.exit("Controller disconnected.")


class Pacer:
    """Keep a byte rate: the Mega relays at exactly the rate data arrives, so
    full-speed bursts overflow its 64-byte buffer and drop bytes."""

    def __init__(self, rate):
        self.rate, self.free = rate, 0.0

    def wait(self, nbytes):
        now = time.time()
        self.free = max(now, self.free) + nbytes / self.rate
        if self.free - now > 0.02:
            time.sleep(self.free - now - 0.02)


def checksummed(line):
    x = 0
    for c in line.encode(errors="replace"):
        x ^= c
    return f"{line}*{x:02X}"


def update_pi(cfg, path):
    """Send a littlelx-pi-update.zip to the touchscreen over USB."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        sys.exit(f"Can't open {path}: {e}")
    inner = [n for n in z.namelist() if n.endswith(".zip")]
    if len(z.namelist()) == 1 and inner:  # GitHub artifact download: zip in a zip
        import io
        z = zipfile.ZipFile(io.BytesIO(z.read(inner[0])))
    names = [n for n in z.namelist() if not n.endswith("/")]
    if "zImage" not in names or "littlelx.cpio.gz" not in names:
        sys.exit(f"{path} doesn't look like a littlelx-pi-update.zip")
    new_ver = z.read("VERSION").decode().strip() if "VERSION" in names else "?"
    total = sum(z.getinfo(n).file_size for n in names)

    ser = connect(cfg)

    def wait(prefixes, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                kind, src, line = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "lost" and src is ser:
                sys.exit("Controller disconnected during the update. The Pi still runs the old version; try again.")
            if kind == "mega" and src is ser:
                if line.startswith(">UPD FAIL"):
                    ser.send(">UPD ABORT")
                    sys.exit("Touchscreen reported an error: " + line[10:] + " (nothing was changed)")
                for p in prefixes:
                    if line.startswith(p):
                        return line
        return None

    print("Looking for the touchscreen...")
    hello = None
    for _ in range(8):
        ser.send(">?")
        hello = wait([">HELLO"], 2)
        if hello:
            break
    if not hello:
        sys.exit("The touchscreen isn't answering. Is it powered and showing 'waiting for computer'?")
    parts = hello.split()
    old_ver = parts[5] if len(parts) > 5 else "old"
    if len(parts) <= 5 or parts[2] == "1":
        sys.exit("This touchscreen firmware is too old to update over USB; flash the SD card once instead.")
    print(f"Touchscreen firmware {old_ver} -> {new_ver}")
    proto = int(parts[2]) if parts[2].isdigit() else 2
    # protocol 3+: checksummed lines and self-repairing chunks; older firmware
    # fails on any damaged byte, so go slower and gentler with it
    pacer = Pacer(20000 if proto >= 3 else 10000)

    def send(line):
        if proto >= 3:
            line = checksummed(line)
        pacer.wait(len(line) + 2)
        ser.send(">" + line)

    def wait_ack(timeout):
        r = wait([">UPD ACK"], timeout)
        return int(r.split()[2]) if r else None

    send(f"UPD BEGIN {total}")
    r = wait([">UPD READY"], 20)
    if not r:
        sys.exit("No answer from the touchscreen (nothing was changed).")
    done = 0
    shown = [-1]
    CHUNK = 192

    def progress(n, nbytes):
        pct = (done + nbytes) * 100 // total
        if pct != shown[0]:
            shown[0] = pct
            print(f"\r  {pct:3d}%  {n:<28}", end="", flush=True)

    for n in names:
        data = z.read(n)
        send(f"UPD FILE {n} {len(data)} {zlib.crc32(data) & 0xffffffff:08x}")
        r = wait([">UPD HAVE", ">UPD SEND"], 30)
        if not r:
            sys.exit("Touchscreen stopped answering (nothing was changed).")
        if r.startswith(">UPD SEND") and proto >= 3:
            # go-back-N: chunks carry their offset; the Pi acks how much it
            # has, and anything lost or damaged is simply sent again
            pos = nxt = 0
            rewound, tries = False, 0
            while pos < len(data):
                while nxt < len(data) and nxt - pos < 4 * CHUNK:
                    send(f"UPD DAT {nxt} " + base64.b64encode(data[nxt:nxt + CHUNK]).decode())
                    nxt = min(nxt + CHUNK, len(data))
                a = wait_ack(2)
                if a is None or (a == pos and not rewound):
                    tries += 1
                    if tries > 30:
                        send("UPD ABORT")
                        sys.exit("\nThe link keeps failing (nothing was changed). Check the Pi wiring.")
                    nxt, rewound = pos, True  # resend from what the Pi has
                elif a > pos:
                    pos, rewound, tries = a, False, 0
                    progress(n, pos)
        elif r.startswith(">UPD SEND"):
            # older Pi firmware: one damaged byte fails the update, so small
            # window, slow pace
            pos, inflight = 0, []
            while pos < len(data) or inflight:
                while pos < len(data) and len(inflight) < 2:
                    chunk = data[pos:pos + CHUNK]
                    send("UPD DATA " + base64.b64encode(chunk).decode())
                    inflight.append(len(chunk))
                    pos += len(chunk)
                if wait_ack(15) is None:
                    send("UPD ABORT")
                    sys.exit("\nTransfer stalled (nothing was changed). Try again.")
                inflight.pop(0)
                progress(n, pos - sum(inflight))
        if r.startswith(">UPD SEND") and not wait([">UPD OK"], 30):
            sys.exit("\nFile wasn't confirmed (nothing was changed).")
        done += len(data)
        progress(n, 0)
    print(f"\r  100%  {'all files sent':<28}")
    send(f"UPD COMMIT {len(names)}")
    if not wait([">UPD DONE"], 60):
        sys.exit("The touchscreen didn't confirm the switch. It will keep running the old version.")
    print("Installed. The touchscreen is restarting...")
    end = time.time() + 90
    while time.time() < end:
        ser.send(">?")  # the new firmware decides its own protocol
        h = wait([">HELLO"], 3)
        if h:
            v = h.split()[5] if len(h.split()) > 5 else "?"
            print(f"Touchscreen is back, running {v}.")
            return
    print("The touchscreen hasn't come back yet. If it stays blank, see 'Pi updates' in the README.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--learn", action="store_true", help="learn the wiring")
    ap.add_argument("--calibrate", action="store_true", help="calibrate the touchscreen")
    ap.add_argument("--faders", action="store_true", help="calibrate fader bottom/top")
    ap.add_argument("--monitor", action="store_true", help="print raw events")
    ap.add_argument("--probe", action="store_true", help="key-matrix wiring diagnostics")
    ap.add_argument("--test-faders", action="store_true", help="find the fader format MA3 accepts")
    ap.add_argument("--verbose", action="store_true", help="print every fader message sent")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--update-pi", metavar="ZIP", help="update the touchscreen firmware")
    args = ap.parse_args()
    cfg = load_config()
    if args.port:
        cfg["serial_port"] = args.port
    elif os.environ.get("LITTLELX_PORT"):
        cfg["serial_port"] = os.environ["LITTLELX_PORT"]
    if os.name == "nt" and not serial:
        sys.exit("On Windows this needs pyserial:  py -m pip install pyserial")
    try:
        if args.update_pi:
            update_pi(cfg, args.update_pi)
        elif args.learn:
            learn(cfg)
        elif args.faders:
            calibrate_faders(cfg)
        elif args.calibrate:
            calibrate(cfg)
        elif args.monitor:
            monitor(cfg)
        elif args.probe:
            probe(cfg)
        elif args.test_faders:
            test_faders(cfg)
        else:
            print(f"Sending OSC to {cfg['osc']['host']}:{cfg['osc']['port']} prefix '{cfg['osc']['prefix']}'")
            b = Bridge(cfg)
            b.verbose = args.verbose
            b.run()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
