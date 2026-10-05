#!/usr/bin/env python3
"""
littlelx bridge: Arduino Mega (USB) <-> grandMA3 (OSC). Mac, Windows, Linux.

The Mega reports raw pin changes and relays the Raspberry Pi touchscreen.
This script turns faders/keys/encoders into OSC for MA3 and draws the screen.

  littlelx.py               run the bridge
  littlelx.py --learn       teach it which pin is which control
  littlelx.py --calibrate   calibrate the touchscreen
  littlelx.py --monitor     print everything the Mega sends
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
#   {"cmd": "Go+"}                send a command-line command on press
#   {"page": 1} / {"page": -1}    page up / down
#   {"screen": "keypad"}          switch touchscreen page
# Encoder actions: {"cmd": "... {d} ..."} with {d} = signed step, or {"page": 1}

DEFAULTS = {
    "osc": {
        "host": "127.0.0.1",      # MA3 machine (127.0.0.1 = MA3 onPC on this computer)
        "port": 8000,             # MA3: Menu > In & Out > OSC > Port
        "prefix": "/gma3",        # MA3 OSC line "Prefix" (gma3), "" if none
        "listen_port": 9000,      # MA3 "Send" destination port, for feedback
        "fader_type": "i",        # "i" (0..100 int) or "f" (0..100 float)
    },
    "serial_port": "",            # "" = auto-detect, or e.g. "COM5" / "/dev/cu.usbmodem1101"
    "page": 1,
    "sync_page_to_ma": False,     # also send "Page N" to MA when paging
    "pickup": False,              # soft takeover when MA reports fader values
    "faders": [{"exec": 201 + i, "name": ""} for i in range(5)],
    "keys": (
        [{"exec": 201 + i} for i in range(5)]          # under the faders
        + [{"exec": 101 + i} for i in range(10)]       # button executors
        + [{"page": -1}, {"page": 1}, {"cmd": "Clear"}, {"cmd": "Go+"}, {"cmd": "Oops"}]
    ),
    "encoders": [
        {"cmd": "Attribute \"Dimmer\" At + {d}", "step": 2, "push": {"cmd": "Clear"}},
        {"page": 1, "push": {"screen": "keypad"}},
    ],
    "touch_buttons": [
        {"label": "Page -", "page": -1},
        {"label": "Page +", "page": 1},
        {"label": "Clear", "cmd": "Clear"},
        {"label": "Oops", "cmd": "Oops"},
        {"label": "Keypad", "screen": "keypad"},
        {"label": "Go -", "cmd": "Go-"},
        {"label": "Pause", "cmd": "Pause"},
        {"label": "Go +", "cmd": "Go+", "color": "1f7a3a"},
        {"label": "Highlight", "cmd": "Highlight"},
        {"label": "Blackout", "cmd": "Blackout", "color": "7a1f2a"},
        {"label": "Blind", "cmd": "Blind"},
        {"label": "Freeze", "cmd": "Freeze"},
    ],
    # Filled in by --learn
    "hw": {
        "faders": [None] * 5,      # {"ch": 0..15, "lo": 0, "hi": 1023}
        "keys": [None] * 20,       # {"pin": 22} digital, active low
        "encoders": [None] * 2,    # {"a": 2, "b": 3, "push": 4, "div": 4}
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
    else:
        save_config(cfg)
    return cfg


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


# -------------------------------------------------------------- touchscreen

C_BG, C_PANEL, C_TEXT, C_DIM = "101418", "1c2430", "ffffff", "8899aa"
C_BTN, C_BTN_ON, C_BAR, C_BAR_BG = "2c3546", "c08a1e", "2f7de1", "232b38"
C_KEY2, C_OK = "3a4458", "38c172"


def esc(text):
    return str(text).replace("\n", "\\n")


class Screen:
    """Builds the touchscreen pages out of Pi widgets. Adapts to portrait or
    landscape using the size the panel reports in its HELLO."""
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
        self.cmdline = ""
        self.keymap = {}
        self.w, self.h = 320, 480

    @property
    def portrait(self):
        return self.h > self.w

    def send(self, line):
        self.b.to_pi(line)

    def widget(self, wid, kind, x, y, w, h, bg, fg, ac, font, align, value, text):
        self.send(f"W {wid} {kind} {x} {y} {w} {h} {bg} {fg} {ac} {font} {align} {value} {esc(text)}")

    def header_h(self):
        return 54 if self.portrait else 30

    def header(self):
        w = self.w
        if self.portrait:
            self.widget(self.HDR_PAGE, "L", 0, 0, w // 2, 30, C_PANEL, C_TEXT, C_PANEL, 1, 1, 0,
                        f"Page {self.b.page}")
            self.widget(self.HDR_MID, "L", 0, 30, w, 24, C_PANEL, C_DIM, C_PANEL, 0, 1, 0, self.b.last_cmd)
        else:
            self.widget(self.HDR_PAGE, "L", 0, 0, 120, 30, C_PANEL, C_TEXT, C_PANEL, 1, 1, 0,
                        f"Page {self.b.page}")
            self.widget(self.HDR_MID, "L", 120, 0, w - 260, 30, C_PANEL, C_DIM, C_PANEL, 0, 0, 0,
                        self.b.last_cmd)
        self.status()

    def status(self):
        ok = self.b.ma_seen and time.time() - self.b.ma_seen < 5
        x = self.w // 2 if self.portrait else self.w - 140
        self.widget(self.HDR_STATUS, "L", x, 0, self.w - x, 30, C_PANEL, C_OK if ok else C_DIM, C_PANEL,
                    0, 2, 0, "MA3 online" if ok else f"OSC > {self.b.cfg['osc']['host']}")

    def draw(self):
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
                        0, 0, self.fader_value(i), self.fader_text(i))
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
                        C_TEXT, C_BTN_ON, 1, 0, 0, btn.get("label", "?"))

    def draw_keypad(self):
        w, h = self.w, self.h
        top = self.header_h() + 4
        self.keymap = {}
        self.widget(self.CMDLINE, "L", 4, top, w - 8, 40, "000000", "ffd36b", "000000", 1, 1, 0,
                    self.cmdline + "_")
        ky = top + 44
        bw = (w - 4) // 5
        bh = (h - ky - 2) // 5
        for r, row in enumerate(self.KEYPAD):
            for c, label in enumerate(row):
                wid = self.KEY0 + r * 5 + c
                self.keymap[wid] = {"key": label}
                digit = label.isdigit() or label == "."
                color = C_BTN_ON if label == "Please" else C_BTN if digit else C_KEY2
                font = 0 if self.portrait and len(label) > 3 else 1
                self.widget(wid, "B", 4 + c * bw, ky + r * bh, bw - 3, bh - 4, color, C_TEXT, C_BTN_ON,
                            font, 0, 0, label)

    def fader_value(self, i):
        v = self.b.fader_pos[i]
        return int(v * 10) if v is not None else 0

    def fader_text(self, i):
        f = self.b.cfg["faders"][i]
        default = str(f["exec"]) if self.portrait else f"Exec {f['exec']}"
        name = self.b.ma_names.get((self.b.page, f["exec"])) or f.get("name") or default
        if self.portrait:
            name = name[:7]
        v = self.b.fader_pos[i]
        pct = "--" if v is None else f"{round(v)}%"
        hint = ""
        if self.b.pickup_pending[i] is not None:
            hint = " ^" if self.b.pickup_pending[i] > (v or 0) else " v"
        return f"{name}\n{pct}{hint}"

    def update_fader(self, i):
        if self.name == "main":
            self.send(f"V {self.FADER0 + i} {self.fader_value(i)}")
            self.send(f"T {self.FADER0 + i} {esc(self.fader_text(i))}")

    def update_cmdline(self):
        if self.name == "keypad":
            self.send(f"T {self.CMDLINE} {esc(self.cmdline + '_')}")

    def set_screen(self, name):
        self.name = name
        self.draw()

    def on_press(self, wid):
        act = self.keymap.get(wid)
        if not act:
            return
        if "key" in act:
            self.keypad(act["key"])
        else:
            self.b.do_action(act, True)

    def on_release(self, wid):
        act = self.keymap.get(wid)
        if act and "key" not in act:
            self.b.do_action(act, False)

    def keypad(self, k):
        if k == "Back":
            self.set_screen("main")
            return
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


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.page = cfg.get("page", 1)
        self.ser = None
        self.screen = Screen(self)
        self.fader_pos = [None] * 5          # physical position 0..100
        self.fader_sent = [None] * 5
        self.pickup_pending = [None] * 5     # MA value we wait to cross
        self.ma_values = {}                  # (page, exec) -> 0..100
        self.ma_names = {}
        self.ma_seen = 0
        self.last_cmd = ""
        self.pins = {}
        self.encs = []
        self.pi_ready = False
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
        for i, k in enumerate(hw["keys"]):
            if k:
                self.digital[k["pin"]] = ("key", i)
        self.encs = []
        for i, e in enumerate(hw["encoders"]):
            self.encs.append(Encoder(e.get("div", 4)) if e else None)
            if e:
                self.digital[e["a"]] = ("enc", i, "a")
                self.digital[e["b"]] = ("enc", i, "b")
                if e.get("push") is not None:
                    self.digital[e["push"]] = ("enc", i, "push")

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
            self.to_pi(f"T {Screen.HDR_MID} {esc(cmd)}")

    def to_pi(self, line):
        if self.ser:
            try:
                self.ser.send(">" + line)
            except Exception:
                pass  # reader thread reports the disconnect

    def send_fader(self, i, value):
        ex = self.cfg["faders"][i]["exec"]
        v = round(value) if self.cfg["osc"]["fader_type"] == "i" else float(round(value, 1))
        self.osc(f"/Page{self.page}/Fader{ex}", v)

    def do_action(self, act, down):
        if "exec" in act:
            self.osc(f"/Page{self.page}/Key{act['exec']}", 1 if down else 0)
        elif not down:
            return
        elif "cmd" in act:
            self.ma_cmd(act["cmd"])
        elif "page" in act:
            self.set_page(self.page + int(act["page"]))
        elif "screen" in act:
            self.screen.set_screen(act["screen"] if self.screen.name != act["screen"] else "main")

    def set_page(self, page):
        self.page = max(1, page)
        self.cfg["page"] = self.page
        if self.cfg.get("sync_page_to_ma"):
            self.ma_cmd(f"Page {self.page}")
        for i in range(5):
            self.arm_pickup(i)
        if self.pi_ready:
            self.screen.header()
            for i in range(5):
                self.screen.update_fader(i)

    def arm_pickup(self, i):
        ma = self.ma_values.get((self.page, self.cfg["faders"][i]["exec"]))
        pos = self.fader_pos[i]
        if self.cfg.get("pickup") and ma is not None and pos is not None and abs(ma - pos) > 3:
            self.pickup_pending[i] = ma
        else:
            self.pickup_pending[i] = None

    # ---- input from the Mega
    def on_fader(self, i, value):
        prev = self.fader_pos[i]
        self.fader_pos[i] = value
        target = self.pickup_pending[i]
        if target is not None:
            crossed = prev is not None and (prev - target) * (value - target) <= 0
            if crossed or abs(value - target) <= 2:
                self.pickup_pending[i] = None
        if self.pickup_pending[i] is None and (self.fader_sent[i] is None or round(value) != round(self.fader_sent[i])):
            self.fader_sent[i] = value
            self.send_fader(i, value)
        if self.pi_ready:
            self.screen.update_fader(i)

    def on_mega(self, line):
        if line.startswith(">"):
            self.on_pi(line[1:])
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
            ver = parts[5] if len(parts) > 5 else "old"
            print(f"Touchscreen connected ({self.screen.w}x{self.screen.h}, firmware {ver})")
            self.pi_ready = True
            cal = self.cfg.get("touch_cal")
            if cal:
                self.to_pi("K " + " ".join(str(x) for x in cal))
            self.screen.draw()
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
            if 0 <= i < 5:
                v = int(parts[2]) / 10.0
                self.fader_sent[i] = v
                self.send_fader(i, v)
                self.fader_pos[i] = v
                self.screen.update_fader(i)
        elif parts[0] == "CALD":
            self.cfg["touch_cal"] = [float(x) for x in parts[1:7]]
            save_config(self.cfg)
            print("Touch calibration saved")
            self.screen.draw()

    # ---- MA feedback
    def on_osc(self, addr, args):
        self.ma_seen = time.time()
        m = re.search(r"/Page(\d+)/Fader(\d+)$", addr)
        if not m:
            return
        page, ex = int(m.group(1)), int(m.group(2))
        nums = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
        strs = [a for a in args if isinstance(a, str)]
        if nums:
            self.ma_values[(page, ex)] = float(nums[-1])
        names = [s for s in strs if s not in ("FaderMaster", "FaderX", "FaderTemp")]
        if names:
            self.ma_names[(page, ex)] = names[0][:12]
        if page == self.page and self.pi_ready:
            for i, f in enumerate(self.cfg["faders"]):
                if f["exec"] == ex:
                    self.screen.update_fader(i)

    # ---- main loop
    def run(self):
        last_ping = last_status = 0
        while True:
            self.ser = connect(self.cfg)
            self.pi_ready = False
            for pin, mode in self.cfg["hw"]["pin_modes"].items():
                self.ser.send(f"M{pin} {mode}")
            self.ser.send("?")
            self.to_pi("?")
            while True:
                try:
                    kind, src, data = events.get(timeout=0.1)
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
                if now - last_ping > 1:
                    last_ping = now
                    self.to_pi("PING")
                if now - last_status > 2 and self.pi_ready:
                    last_status = now
                    self.screen.status()


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
        best = max(seen.items(), key=lambda kv: max(kv[1]) - min(kv[1]), default=None)
        if not best or max(best[1]) - min(best[1]) < 300:
            print("  no fader movement seen, skipped")
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

    def press(line):
        m = re.match(r"D(\d+) 0$", line)
        if m and int(m.group(1)) not in used_pins:
            return int(m.group(1))
        return None

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
            used_pins.add(p)
            print(f"  -> push on pin {p}")
        hw["encoders"][i] = e

    # ---- keys
    for i in range(20):
        print(f"Key {i + 1}: press it (Enter to skip).")
        p = wait_event(ser, press)
        if p is None:
            hw["keys"][i] = None
            print("  skipped")
            continue
        hw["keys"][i] = {"pin": p}
        used_pins.add(p)
        print(f"  -> pin {p}")
        time.sleep(0.15)
        drain()

    hw["pin_modes"] = pin_modes
    save_config(cfg)
    print(f"\nSaved to {CONFIG_PATH}. Run without --learn to start the bridge.")


def calibrate(cfg):
    ser = connect(cfg)
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
        print("Saved.")
    else:
        print("No calibration received.")


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

    ser.send(f">UPD BEGIN {total}")
    r = wait([">UPD READY"], 20)
    if not r:
        sys.exit("No answer from the touchscreen (nothing was changed).")
    sent = 0
    shown = -1
    CHUNK, WINDOW = 192, 4
    for n in names:
        data = z.read(n)
        ser.send(f">UPD FILE {n} {len(data)} {zlib.crc32(data) & 0xffffffff:08x}")
        r = wait([">UPD HAVE", ">UPD SEND"], 30)
        if not r:
            sys.exit("Touchscreen stopped answering (nothing was changed).")
        if r.startswith(">UPD HAVE"):
            sent += len(data)
            continue
        chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
        acked = 0
        i = 0
        while acked < len(chunks):
            while i < len(chunks) and i - acked < WINDOW:
                ser.send(">UPD DATA " + base64.b64encode(chunks[i]).decode())
                i += 1
            if not wait([">UPD ACK"], 15):
                ser.send(">UPD ABORT")
                sys.exit("Transfer stalled (nothing was changed). Try again.")
            acked += 1
            sent += len(chunks[acked - 1])
            if sent * 100 // total != shown:
                shown = sent * 100 // total
                print(f"\r  {shown:3d}%  {n:<28}", end="", flush=True)
        if not wait([">UPD OK"], 30):
            sys.exit("\nFile wasn't confirmed (nothing was changed).")
    print(f"\r  100%  {'all files sent':<28}")
    ser.send(f">UPD COMMIT {len(names)}")
    if not wait([">UPD DONE"], 60):
        sys.exit("The touchscreen didn't confirm the switch. It will keep running the old version.")
    print("Installed. The touchscreen is restarting...")
    end = time.time() + 90
    while time.time() < end:
        ser.send(">?")
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
    ap.add_argument("--monitor", action="store_true", help="print raw events")
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
        elif args.calibrate:
            calibrate(cfg)
        elif args.monitor:
            monitor(cfg)
        else:
            if not any(cfg["hw"]["faders"] + cfg["hw"]["keys"]):
                print("No wiring learnt yet: run with --learn first.")
            print(f"Sending OSC to {cfg['osc']['host']}:{cfg['osc']['port']} prefix '{cfg['osc']['prefix']}'")
            Bridge(cfg).run()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
