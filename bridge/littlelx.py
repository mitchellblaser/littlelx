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

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:  # loaded from elsewhere (tests)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lxmidi  # noqa: E402

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

CONFIG_PATH = os.path.expanduser("~/.littlelx.json")


def bridge_version():
    """git version of this checkout, or the one stamped into a built app."""
    import subprocess
    if getattr(sys, "_MEIPASS", None):  # a built .exe / .app: VERSION is bundled
        try:
            with open(os.path.join(sys._MEIPASS, "VERSION")) as f:
                return f.read().strip() or "unknown"
        except OSError:
            return "unknown"
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run(["git", "-C", here, "describe", "--always", "--dirty"],
                              capture_output=True, text=True, timeout=3).stdout.strip() or "unknown"
    except Exception:
        return "unknown"

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
# Encoder actions:
#   {"attribute": "Dimmer", "step": 1}   % per click at MA's Coarse resolution;
#                                        follows MA's Coarse/Fine for that attribute
#   {"cmd": "... {d} ..."}               {d} = step per click, or {"page": 1}
# Push actions also allow {"resolution": "Dimmer"}: toggle MA's Coarse/Fine.

CONFIG_VERSION = 10

DEFAULTS = {
    "config_version": CONFIG_VERSION,
    "osc": {
        "host": "127.0.0.1",      # MA3 machine (127.0.0.1 = MA3 onPC on this computer)
        "port": 8000,             # MA3: Menu > In & Out > OSC > Port
        "prefix": "/gma3",        # MA3 OSC line "Prefix" (gma3), "" if none
        "listen_port": 9000,      # MA3 "Send" destination port, for feedback
        "fader_interval": 0.025,  # s between messages per fader (MA lags if flooded)
        "fader_jump": 5,          # % moved that is sent at once (fast throws stay smooth)
        "fader_type": "i",        # how faders are sent; --test-faders picks it (see FADER_FORMATS)
        "type": "ma3",            # "ma3", or "generic": plain OSC to anything else, at these:
        "generic": {"fader": "/fader/{n}", "key": "/key/{n}", "encoder": "/encoder/{n}",
                    "push": "/encoder/{n}/push", "button": "/button/{n}",
                    "page": "/page", "command": "/command", "set": "/encoder/{n}/set"},
        # generic: the values sent (and fader feedback read the same way)
        "values": {"fader": [0, 1],     # fader range (bottom, top)
                   "key": [1, 0],       # keys: pressed, released
                   "button": [1, 0],    # touchscreen buttons
                   "push": [1, 0],      # encoder pushes
                   "encoder": 1,        # per click (x direction)
                   "integer": False},   # whole numbers (e.g. MIDI-style 0..127)
    },
    # The active profile's name: everything the controller does (connection,
    # faders, keys, encoders, screen) as one JSON file in PROFILE_DIR, also
    # kept on the controller (see profile_from_cfg)
    "profile": "",
    "ma3": {
        "auto_install": True,     # put the littlelx code into MA3 over OSC
        "osc_line": 2,            # MA3 OSC line that SENDS to this computer (port 9000)
        "tick": 0.1,              # how often MA reports (s)
    },
    "serial_port": "",            # "" = auto-detect, or e.g. "COM5" / "/dev/cu.usbmodem1101"
    "screen_bgr": True,           # panel takes blue-green-red: swap (false if red/blue look swapped)
    "page": 1,
    "pickup": True,               # soft takeover: a fader acts once it reaches MA's level
    "faders": [{"exec": 201 + i, "name": ""} for i in range(5)],
    "keys": (  # also set from the touchscreen: Setup
        [{"page": -1}, {"page": 1}, {"key": "Clear"}, {"cmd": "Oops"}, {"key": "Please"}]
        + [{"exec": 301 + i} for i in range(5)]
        + [{"exec": 201 + i} for i in range(5)]
        + [{"exec": 101 + i} for i in range(5)]
    ),
    "encoders": [
        # "follow": turn MA's encoders (the selected feature's attributes, a pair
        # at a time); the rest is used when MA has none (nothing selected)
        {"follow": True, "attribute": "Dimmer", "step": 1},  # click: type a value
        {"follow": True, "page": 1, "push": {"screen": "keypad"}},
    ],
    "touch_buttons": [
        {"label": "Page -", "page": -1},
        {"label": "Page +", "page": 1},
        {"label": "Clear", "key": "Clear"},
        {"label": "Highlight", "cmd": "Highlight", "state": "highlight"},
        {"label": "Last", "cmd": "Previous"},
        {"label": "Next", "cmd": "Next"},
        {"label": "Blind", "cmd": "Blind", "state": "blind"},
        {"label": "Keypad", "screen": "keypad"},
        {"label": "Encoders", "screen": "encoders"},
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
            cfg["config_version"] = user.get("config_version", 1)
            migrate(cfg)
            cfg["config_version"] = CONFIG_VERSION
            save_config(cfg)
    else:
        save_config(cfg)
    cfg.pop("profiles", None)  # an earlier draft's list
    if not cfg.get("profile"):  # the setup so far becomes the first profile
        cfg["profile"] = "grandMA3" if cfg["osc"].get("type", "ma3") == "ma3" else "Default"
        save_config(cfg)
    install_example_profiles()
    if not os.path.exists(profile_path(cfg["profile"])):
        save_profile(profile_from_cfg(cfg))
    return cfg


def migrate(cfg):
    """Bring a config saved by an older version up to date (runs once)."""
    cfg.pop("encoder_attributes", None)  # the encoders follow MA's now
    labels = [b.get("label") for b in cfg.get("touch_buttons", [])]
    old_defaults = (["Page -", "Page +", "Clear", "Oops", "Keypad", "Go -", "Pause", "Go +",
                     "Highlight", "Blind", "Last", "Next"],
                    ["Page -", "Page +", "Clear", "Oops", "Keypad", "Go -", "Pause", "Go +",
                     "Highlight", "Blind", "Blackout", "Freeze"])
    if "Blackout" in labels or "Freeze" in labels or labels in old_defaults:
        cfg["touch_buttons"] = copy.deepcopy(DEFAULTS["touch_buttons"])
        print("Touch buttons updated: Page -/+, Clear, Highlight, Last, Next, Blind, Keypad, Encoders.")
    for k in cfg.get("keys", []):
        if k == {"cmd": "Clear"}:  # Clear now acts on MA's command line first
            k.pop("cmd")
            k["key"] = "Clear"
    for e in cfg.get("encoders", []):
        if e and e.get("push") == {"cmd": "Clear"}:
            e["push"] = {"key": "Clear"}
    if cfg.get("config_version", 1) < 2:
        cfg.pop("sync_page_to_ma", None)
        cfg["pickup"] = True  # old default was off; soft takeover is now on
    old_keys = ([{"exec": 201 + i} for i in range(5)] + [{"exec": 101 + i} for i in range(10)]
                + [{"page": -1}, {"page": 1}, {"key": "Clear"}, {"cmd": "Go+"}, {"cmd": "Oops"}])
    if cfg.get("keys") == old_keys:  # v7: the old default layout -> the new one
        cfg["keys"] = copy.deepcopy(DEFAULTS["keys"])
        print("Keys updated: 1-5 Page -, Page +, Clear, Oops, Please; 6-10 exec 301-305;"
              " 11-15 exec 201-205; 16-20 exec 101-105 (change them on the touchscreen: Setup).")
    if cfg.get("config_version", 1) < 8:  # v8: an encoder click types a value, not Coarse/Fine
        for e in cfg.get("encoders", []):
            if e and isinstance(e.get("push"), dict) and "resolution" in e["push"]:
                e.pop("push")
    if cfg.get("config_version", 1) < 6:  # encoders follow MA's encoders
        for e in cfg.get("encoders", []):
            if e is not None:
                e.setdefault("follow", True)
    if cfg.get("osc", {}).get("fader_interval") == 0.04:  # v4: faster default
        cfg["osc"]["fader_interval"] = 0.025
    for e in cfg.get("encoders", []):  # v3: attribute encoders follow MA's resolution
        m = re.match(r'Attribute "([^"]+)" At \+ \{d\}$', (e or {}).get("cmd", ""))
        if m:
            e.pop("cmd")
            e["attribute"] = m.group(1)
            e["step"] = 1
            if e.get("push") in ({"key": "Clear"}, {"cmd": "Clear"}):
                e["push"] = {"resolution": m.group(1)}


# ---- profiles: everything the controller does, as one JSON file
#
# {"name": "...",
#  "connection": {"type": "ma3" | "generic", "host", "port", "prefix",
#                 "listen_port", "fader_type", "generic": {addresses}},
#  "faders": [{"exec": 201, "name": "", "osc": "/addr" (generic)}, ...5],
#  "keys": [20 actions], "encoders": [2], "touch_buttons": [main page],
#  "keypad": [5 rows of 5 labels] (optional),
#  "midi": [{"device": "APC Mini", "map": {"pads": [actions], "faders": [faders]}}]
#          (MIDI controllers: see bridge/lxmidi.py; same actions / faders as above)}
#
# Actions: see the top of this file; also {"osc": "/addr"} sends 1 / 0.
# The active one is also stored on the controller (setup_blob).

PROFILE_DIR = os.path.join(os.path.expanduser("~"), "littlelx-profiles")
CONN_KEYS = ("type", "host", "port", "prefix", "listen_port", "fader_type", "generic", "values",
             "fader_interval", "fader_jump")
PROFILE_FIELDS = ("faders", "keys", "encoders", "touch_buttons", "keypad", "pickup",
                  "screen", "feedback", "screens", "midi")

# What the touchscreen shows, per connection type; a profile's "screen" overrides
# any of it. Texts may use {page} and {profile}.
#   left / middle / status: the top bar. middle: "cmdline" (MA's command line),
#     "sent" (the last OSC message sent), "feedback" (text from OSC, see below)
#     or any text. status: "ma" (MA3 linked/online), "osc" (where to + whether
#     anything comes back), "feedback", or any text.
#   encoders: "ma" (follow MA's encoder bar) or "simple" (label + value each)
#   encoder_strip: the small encoder line on the main and keypad pages
#   columns: the main page's button grid (default: what suits the number of buttons)
SCREEN_DEFAULTS = {
    "ma3": {"left": "Page {page}", "middle": "cmdline", "status": "ma", "encoders": "ma",
            "encoder_strip": True, "encoders_title": "Encoders", "columns": None},
    "generic": {"left": "{profile}", "middle": "sent", "status": "osc", "encoders": "simple",
                "encoder_strip": True, "encoders_title": "Encoders", "columns": None},
}
# Generic profiles: OSC coming back (to listen_port) that drives the screen.
# A profile's "feedback" replaces this; items can also have their own:
# faders "feedback", touch buttons "lit", encoders "value" (exact addresses).
#   fader: level 0..1 (bar + catch-up arrows)  fader_name / fader_color ("rrggbb")
#   button: lit when > 0 / "on" / true       button_label
#   encoder_label / encoder_value             text (top bar)  status  page
FEEDBACK_DEFAULT = {
    "fader": "/fader/{n}", "fader_name": "/fader/{n}/name", "fader_color": "/fader/{n}/color",
    "button": "/button/{n}/state", "button_label": "/button/{n}/label",
    "encoder_label": "/encoder/{n}/label", "encoder_value": "/encoder/{n}/value",
    "text": "/text", "status": "/status", "page": "/page",
}


def profile_from_cfg(cfg):
    p = {"name": cfg.get("profile") or "Default",
         "connection": {k: copy.deepcopy(cfg["osc"][k]) for k in CONN_KEYS if k in cfg["osc"]}}
    for k in PROFILE_FIELDS:
        if k in cfg:
            p[k] = copy.deepcopy(cfg[k])
    return p


def apply_profile(cfg, prof):
    """Make a profile the active one (fields it leaves out keep their defaults)."""
    conn = prof.get("connection", {})
    for k in CONN_KEYS:
        cfg["osc"][k] = copy.deepcopy(conn[k]) if k in conn else copy.deepcopy(DEFAULTS["osc"].get(k))
    for k in PROFILE_FIELDS:
        if k in prof:
            cfg[k] = copy.deepcopy(prof[k])
        elif k in DEFAULTS:
            cfg[k] = copy.deepcopy(DEFAULTS[k])
        else:
            cfg.pop(k, None)
    cfg["keys"] = (list(cfg.get("keys") or []) + [None] * 20)[:20]
    cfg["profile"] = prof.get("name") or "Default"


def profile_path(name):
    safe = re.sub(r"[^\w\- .]", "_", name).strip() or "profile"
    return os.path.join(PROFILE_DIR, safe + ".json")


def list_profiles():
    try:
        files = sorted(f for f in os.listdir(PROFILE_DIR) if f.endswith(".json"))
    except OSError:
        return []
    out = []
    for f in files:
        try:
            with open(os.path.join(PROFILE_DIR, f)) as fh:
                out.append(json.load(fh).get("name") or f[:-5])
        except (OSError, ValueError, AttributeError):
            pass
    return out


def load_profile(name):
    with open(profile_path(name)) as f:
        return json.load(f)


def example_profiles():
    """The example profiles shipped with the bridge (profiles/ in the repo)."""
    src = resource("profiles")
    return [os.path.join(src, f) for f in sorted(os.listdir(src))
            if f.endswith(".json")] if os.path.isdir(src) else []


def install_example_profiles(overwrite=False):
    """Put the example profiles in PROFILE_DIR: the missing ones, or (overwrite)
    all of them again - e.g. newer versions after an update."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    done = []
    for path in example_profiles():
        dest = os.path.join(PROFILE_DIR, os.path.basename(path))
        if overwrite or not os.path.exists(dest):
            with open(path) as a, open(dest, "w") as b:
                b.write(a.read())
            done.append(os.path.basename(path)[:-5])
    return done


def save_profile(prof):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    tmp = profile_path(prof["name"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(prof, f, indent=2)
    os.replace(tmp, profile_path(prof["name"]))


def save_active(cfg, ser=None, keep=False):
    """The active profile changed: save it everywhere (config, its file, and
    the controller when connected)."""
    save_config(cfg)
    save_profile(profile_from_cfg(cfg))
    if ser:
        save_to_mega(cfg, ser, keep=keep)


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


class Pacer:
    """Keep a byte rate: the Mega relays at exactly the rate data arrives, so
    full-speed bursts overflow its 64-byte buffer and drop bytes."""

    def __init__(self, rate):
        self.rate, self.free = rate, 0.0

    def wait(self, nbytes):
        now = time.time()
        self.free = max(now, self.free) + nbytes / self.rate
        if self.free - now > 0.001:
            time.sleep(self.free - now - 0.001)


def checksummed(line):
    x = 0
    for c in line.encode(errors="replace"):
        x ^= c
    return f"{line}*{x:02X}"


PI_CK = re.compile(r"^>(.*)\*([0-9A-F]{2})$")
PI_RATE = 16000   # bytes/s to the touchscreen: ~2/3 of the 250k Mega<->Pi link
PI_PIECE = 32     # bytes per write, so the Mega's 64-byte buffers never fill


class Port:
    """Serial port to the Mega; a reader thread posts ("mega", port, line)."""

    def __init__(self, path):
        self.path = path
        self.closed = False
        if serial:
            try:  # exclusive: a second program on the port would garble everything
                self.s = serial.Serial(path, BAUD, timeout=0.1, exclusive=True)
            except (ValueError, TypeError):  # exclusive not supported (Windows is exclusive anyway)
                self.s = serial.Serial(path, BAUD, timeout=0.1)
        else:  # stdlib fallback for Mac/Linux
            import termios
            self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
            import fcntl
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self.fd)
                raise OSError("port in use - is the bridge (or another littlelx tool) already running?")
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
        self.line_lock = threading.Lock()  # a Pi line's pieces must not be split by another line
        self.outq = queue.Queue()  # touchscreen lines for the writer thread (see post)
        self.writer = None
        self.pi_proto = 0        # touchscreen protocol, from its HELLO
        self.pi_bad = 0          # lines from the touchscreen with a bad checksum
        self.pacer = Pacer(PI_RATE)
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
                    l = l.strip(b"\r").decode(errors="replace")
                    if l.startswith(">"):  # from the touchscreen: verify + strip checksum
                        m = PI_CK.match(l)
                        if m:
                            x = 0
                            for c in m.group(1).encode(errors="replace"):
                                x ^= c
                            if x != int(m.group(2), 16):
                                self.pi_bad += 1
                                continue
                            l = ">" + m.group(1)
                        if l.startswith(">HELLO littlelx-pi"):
                            p = l.split()
                            self.pi_proto = int(p[2]) if len(p) > 2 and p[2].isdigit() else 1
                    if l:
                        events.put(("mega", self, l))
        except Exception as e:  # unplugged
            if not self.closed:
                events.put(("lost", self, str(e)))

    def send(self, line):
        """Lines starting with '>' go to the touchscreen: checksummed (if its
        firmware understands it) and paced in small pieces. Everything that
        talks to the Pi goes through here, so nothing can skip either."""
        with self.line_lock:
            if line.startswith(">"):
                body = line[1:]
                if self.pi_proto >= 3:
                    body = checksummed(body)
                data = (">" + body + "\n").encode(errors="replace")
                for off in range(0, len(data), PI_PIECE):
                    self.pacer.wait(PI_PIECE)
                    self._write(data[off:off + PI_PIECE])
                return
            self._write((line + "\n").encode(errors="replace"))

    def post(self, line):
        """Like send(), but returns at once: a writer thread does the paced
        sending, so the bridge never waits on the touchscreen link (faders
        and keys must not queue up behind screen drawing)."""
        if self.writer is None:
            self.writer = start_thread(self._writer)
        self.outq.put(line)

    @property
    def backlog(self):
        """Touchscreen lines still waiting to be sent."""
        return self.outq.qsize()

    def _writer(self):
        while not self.closed:
            try:
                line = self.outq.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.send(line)
            except Exception:
                pass  # the reader thread reports the disconnect

    def _write(self, data):
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


def connect(cfg, verbose=True, stop=None, idle=None):
    """Open the Mega and wait for it to boot (opening the port resets it).
    stop: returns True to give up (-> None), e.g. the app pausing the bridge.
    idle(kind, src, data): gets the other events meanwhile (and None ones
    regularly), so MIDI devices and MA keep working without the controller."""
    while True:
        if stop and stop():
            return None
        path = find_port(cfg)
        if path:
            try:
                p = Port(path)
                start, asked = time.time(), False
                while time.time() - start < 4:
                    try:
                        kind, src, line = events.get(timeout=0.02 if idle else 0.2)
                    except queue.Empty:
                        kind = src = line = None
                    if kind == "call":
                        line()  # the app's request (see Bridge.call)
                    elif idle and kind != "mega":
                        idle(kind, src, line)
                    if kind == "mega" and src is p and line.startswith("HELLO littlelx-mega"):
                        parts = line.split()
                        p.mega_version = parts[3] if len(parts) > 3 else "old"  # before versions
                        print(f"Mega connected on {path} (firmware {p.mega_version})")
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
        end = time.time() + 2
        while time.time() < end and not (stop and stop()):
            try:
                kind, src, data = events.get(timeout=0.02 if idle else 0.2)
            except queue.Empty:
                kind = src = data = None
            if kind == "call":
                data()
            elif idle:
                idle(kind, src, data)


# ------------------------------------------------- setup stored on the Mega
#
# The learned wiring + calibrations live in the Mega's EEPROM, so they travel
# with the controller to any computer. Layout: 0-1 "LX", 2-71 pin modes (the
# Mega applies them at power-up), 128+ "CF" len16 crc32 zlib(JSON).

EE_MODES, EE_BLOB, EE_SIZE = 0, 128, 4096


def wait_line(ser, pred, timeout, keep=False):
    """Wait for a Mega line matching pred. Other events are dropped, or with
    keep=True (the running bridge) put back, in order, for the main loop."""
    end = time.time() + timeout
    kept = []
    try:
        while time.time() < end:
            try:
                ev = events.get(timeout=0.1)
            except queue.Empty:
                continue
            kind, src, line = ev
            if kind == "lost" and src is ser:
                raise OSError("controller disconnected")
            if kind == "mega" and src is ser and pred(line):
                return line
            if keep:
                kept.append(ev)
        return None
    finally:
        if kept:
            with events.mutex:
                events.queue.extendleft(reversed(kept))


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


def mega_write(ser, addr, data, keep=False):
    for off in range(0, len(data), 32):
        a = addr + off
        for _ in range(3):
            ser.send(f"W{a} {data[off:off + 32].hex()}")
            if wait_line(ser, lambda l: l == f"W{a} OK", 2.0, keep):
                break
        else:
            raise TimeoutError("EEPROM write not confirmed")


def matrix_modes(cfg):
    """Mark the learned key-matrix lines as mode 4: the Mega then scans only
    those, the whole matrix every loop (fast). Returns True if anything changed."""
    hw = cfg["hw"]
    pairs = [k["pair"] for k in hw["keys"] if k and "pair" in k]
    pairs += [e["push"] for e in hw["encoders"] if e and isinstance(e.get("push"), list)]
    changed = False
    for pin in {p for pair in pairs for p in pair}:
        if hw["pin_modes"].get(str(pin)) != 4:
            hw["pin_modes"][str(pin)] = 4
            changed = True
    return changed


def learned(cfg):
    hw = cfg["hw"]
    return any(hw["faders"] + hw["keys"] + hw["encoders"])


def setup_blob(cfg):
    """What the controller carries to any computer: wiring, touch calibration
    and the active profile."""
    return {"hw": cfg["hw"], "touch_cal": cfg.get("touch_cal"), "profile": profile_from_cfg(cfg)}


def save_to_mega(cfg, ser, keep=False):
    blob = setup_blob(cfg)
    pack = lambda b: zlib.compress(json.dumps(b, separators=(",", ":")).encode(), 9)  # noqa: E731
    data = pack(blob)
    if len(data) > EE_SIZE - EE_BLOB - 8 and blob["profile"].get("midi"):
        # MIDI devices plug into this computer anyway: their layouts can stay here
        blob["profile"].pop("midi")
        blob["profile"]["midi_here"] = True
        data = pack(blob)
        print("(The profile's MIDI layouts are too big for the controller: kept on this computer.)")
    if len(data) > EE_SIZE - EE_BLOB - 8:
        print("Setup too big for the controller's memory; kept on this computer only.")
        return
    modes = bytearray([0xff] * 70)
    for pin, m in cfg["hw"]["pin_modes"].items():
        modes[int(pin)] = int(m)
    try:
        mega_write(ser, EE_BLOB + 8, data, keep)  # data first, header (with CRC) last
        mega_write(ser, EE_BLOB, b"CF" + struct.pack("<HI", len(data), zlib.crc32(data)), keep)
        mega_write(ser, EE_MODES, b"LX" + bytes(modes), keep)
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
        changed = False  # the controller's copy wins
        if blob["hw"] != cfg["hw"] or blob.get("touch_cal") != cfg.get("touch_cal"):
            cfg["hw"], cfg["touch_cal"] = blob["hw"], blob.get("touch_cal")
            changed = True
            print("Loaded the learned setup from the controller.")
        prof = blob.get("profile")
        if prof and prof.pop("midi_here", False):  # its MIDI layouts stayed on the computer
            here = cfg.get("midi") if prof.get("name") == cfg.get("profile") else None
            if here is None:
                try:
                    here = load_profile(prof.get("name", "")).get("midi")
                except (OSError, ValueError):
                    here = None
            if here:
                prof["midi"] = here
        if prof and prof != profile_from_cfg(cfg):
            apply_profile(cfg, prof)
            changed = True
            print(f"Using the controller's profile '{cfg['profile']}'.")
            save_profile(profile_from_cfg(cfg))  # so it's in the profiles folder here too
        elif not prof and blob.get("keys") and blob["keys"] != cfg.get("keys"):  # older bridge: keys only
            cfg["keys"] = blob["keys"]
            changed = True
        if changed:
            save_config(cfg)
        if not prof:  # saved by an older bridge: give it the profile
            save_to_mega(cfg, ser)
        return changed
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


def theme_line(line, swap):
    """Recolour a screen command: default colours -> the profile's theme."""
    p = line.split(" ")
    idx = range(7, 10) if p[0] == "W" and len(p) > 10 else range(2, 5) if p[0] == "C" and len(p) >= 5 \
        else range(1, 2) if p[0] == "BG" and len(p) == 2 else ()
    for k in idx:
        p[k] = swap.get(p[k], p[k])
    return " ".join(p) if idx else line


def bgr(color):
    """'rrggbb' -> 'bbggrr'."""
    return color[4:6] + color[2:4] + color[:2] if len(color) == 6 else color


def bgr_line(line):
    """Swap red and blue in a screen command's colours: these panels take BGR."""
    p = line.split(" ")
    if p[0] == "W" and len(p) > 10:      # W id kind x y w h bg fg ac ...
        p[7:10] = [bgr(c) for c in p[7:10]]
    elif p[0] == "C" and len(p) >= 5:    # C id bg fg ac
        p[2:5] = [bgr(c) for c in p[2:5]]
    elif p[0] == "BG" and len(p) == 2:
        p[1] = bgr(p[1])
    else:
        return line
    return " ".join(p)


def hexcolor(v, default):
    """A profile/feedback colour ("rrggbb" or "#rrggbb") or the default."""
    v = str(v or "").lstrip("#").lower()
    return v if re.fullmatch(r"[0-9a-f]{6}", v) else default


def shade(color, f):
    """'rrggbb' scaled towards black (f < 1)."""
    try:
        v = int(color, 16)
    except (TypeError, ValueError):
        return None
    return "".join(f"{min(255, int(((v >> sh) & 255) * f)):02x}" for sh in (16, 8, 0))


RES_LABELS = ((0.5, "Coarse"), (0.05, "Fine"), (0, "Ultra"))  # MA's step factor -> name


def res_label(f):
    return next(name for lim, name in RES_LABELS if f >= lim)


C_PROG = "ff4b3e"  # programmer values: red, like MA
C_VALUE = "a4acb8"  # values not in the programmer: grey, like MA
C_STRIP = "161d28"  # main page: the encoder strip under the status bar

# Theme names for the screen's colours: a profile's "screen": {"colors": {...}}
# replaces any of them (hex "rrggbb").
THEME = {"background": C_BG, "panel": C_PANEL, "text": C_TEXT, "dim": C_DIM, "button": C_BTN,
         "button_lit": C_BTN_ON, "key": C_KEY2, "bar": C_BAR, "bar_background": C_BAR_BG,
         "command": C_CMD, "ok": C_OK, "waiting": C_WAIT, "programmer": C_PROG, "value": C_VALUE,
         "strip": C_STRIP}


FEATURE_COLORS = {  # encoder headers, by MA feature group
    "dimmer": "d8b020", "position": "3a7bd5", "gobo": "3aa655", "color": "b03ab8",
    "beam": "d07a2a", "focus": "2aa8a8", "control": "7a8590", "shapers": "b84848", "video": "5a5ac8",
}


class Screen:
    """Builds the touchscreen pages out of Pi widgets. Adapts to portrait or
    landscape using the size the panel reports in its HELLO.

    Every widget's current state is kept here, so the whole screen can be
    re-sent bit by bit in the background: anything lost on the serial link
    reappears within a few seconds."""
    HDR_PAGE, HDR_MID, HDR_STATUS, HDR_SETUP, HDR_ENC1, HDR_ENC2 = 0, 1, 2, 3, 4, 5
    FADER0 = 10
    BTN0 = 20
    CMDLINE = 39
    KEY0 = 40
    ENC0 = 66      # encoders page: 10 ids per encoder
    ENC_FEAT, ENC_PAGE = 86, 87
    ENC_TAB0, TABS_SHOWN = 88, 4   # feature tabs (the last one pages when there are more)
    BACK = 120
    SET_TITLE, SET_RESET, SET_MORE, SET_PROBE = 121, 122, 123, 124
    SET0 = 125     # setup grids (keys, functions, digits); named values from SET0 + 20
    # (the Pi has 160 widget ids: keep everything below that)

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
        self.dirty_faders = set()
        self.enc_mode = False  # encoders page drawn for MA's encoders (else the set ones)
        self.keymap_hdr = {}
        self.exec_entry = ""
        self.reset_armed = 0.0
        self.value_entry = ""
        self.sets_page = 0
        self.sets_rows = 3
        self.sets_sel = -1     # highlighted named value (encoder turns move it)
        self.enc_tabs = False  # encoders page drawn with a tab row
        self.enc_simple = False  # encoders page: label + value (generic), not MA's
        self.tab_page = 0
        self.tab_paged = False  # tabs paged by hand: don't jump back to the current one
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
        colors = (self.b.cfg.get("screen") or {}).get("colors")
        if colors:
            swap = {THEME[k]: str(v).lstrip("#").lower() for k, v in colors.items()
                    if k in THEME and re.fullmatch(r"#?[0-9a-fA-F]{6}", str(v))}
            line = theme_line(line, swap)
        if self.b.cfg.get("screen_bgr", True):
            line = bgr_line(line)
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
    ENC_STRIP_H = 20  # the two encoders, small, under the status bar
    STRIP_SCREENS = ("main", "keypad")

    def strip_on(self):
        return self.name in self.STRIP_SCREENS and bool(self.b.screen_opt("encoder_strip"))

    def fmt(self, text):
        b = self.b
        try:
            return str(text).format(page=b.page, profile=b.cfg.get("profile", ""))
        except (KeyError, IndexError, ValueError):
            return str(text)

    def header_h(self):
        strip = self.ENC_STRIP_H if self.strip_on() else 0
        return (54 if self.portrait else 30) + strip

    def mid_text(self):
        b = self.b
        if self.cmdline:  # the line being built here
            return "> " + self.cmdline
        mode = b.screen_opt("middle")
        if mode == "cmdline":
            if b.ma_linked():  # whatever is on MA's own line (typed at the desk)
                line = b.ma["busy"] or b.ma["cmdline"]
                return "> " + line if line else ">"
            return b.last_cmd
        if mode == "sent":
            return b.last_sent
        if mode == "feedback":
            return b.fb["text"] or ""
        return self.fmt(mode)

    def header(self):
        w = self.w
        page = self.fmt(self.b.screen_opt("left"))
        strip = self.strip_on()
        sy = 30  # the encoder strip sits right under the status bar
        if strip:
            half = w // 2
            for k, wid in enumerate((self.HDR_ENC1, self.HDR_ENC2)):
                self.widget(wid, "L", k * half, sy, half if k == 0 else w - half, self.ENC_STRIP_H, C_STRIP, C_VALUE,
                            C_STRIP, 0, 1, 0, "")
        cy = 30 + (self.ENC_STRIP_H if strip else 0)
        if self.portrait:
            self.widget(self.HDR_PAGE, "L", 0, 0, 100, 30, C_PANEL, C_TEXT, C_PANEL, 1, 1, 0, page)
            self.widget(self.HDR_MID, "L", 0, cy, w, 24, C_PANEL, C_CMD, C_PANEL, 0, 1, 0, self.mid_text())
            x = 100
        else:
            self.widget(self.HDR_PAGE, "L", 0, 0, 120, 30, C_PANEL, C_TEXT, C_PANEL, 1, 1, 0, page)
            self.widget(self.HDR_MID, "L", 120, 0, w - 340, 30, C_PANEL, C_CMD, C_PANEL, 0, 0, 0, self.mid_text())
            x = w - 220
        self.widget(self.HDR_STATUS, "L", x, 0, w - 80 - x, 30, C_PANEL, C_DIM, C_PANEL, 0, 2, 0, "")
        self.keymap_hdr = {self.HDR_SETUP: {"setup": True}}
        self.widget(self.HDR_SETUP, "B", w - 76, 2, 72, 26, C_KEY2, C_TEXT, C_BTN_ON, 0, 0,
                    1 if self.in_setup() else 0, "Setup")
        self.update_header()
        self.update_enc_strip()

    def update_enc_strip(self):
        """Main page: what the two encoders control and their values, small."""
        if self.HDR_ENC1 not in self.state:
            return
        b = self.b
        for i, wid in enumerate((self.HDR_ENC1, self.HDR_ENC2)):
            if b.screen_opt("encoders") == "simple":
                label, value = self.simple_encoder(i)
                text, fg = f" {i + 1}  {label}  {value}", C_VALUE
            elif b.ma_encoders_on():
                e = b.ma_encoder(i)
                text = f" {i + 1}  {e['pretty']}  {e['value'] or '-'}" if e else f" {i + 1}  -"
                fg = C_PROG if e and e["prog"] else C_VALUE
            else:
                what, _ = self.encoder_info(i)
                text, fg = f" {i + 1}  {what}", C_VALUE
            self.setw(wid, text=text[:26], colors=(C_STRIP, fg, C_STRIP))

    def update_header(self):
        b = self.b
        mode = b.screen_opt("status")
        if mode == "ma":
            if b.ma_linked():
                text, color = "MA3 linked", C_OK
            elif b.ma_seen and time.time() - b.ma_seen < 5:
                text, color = "MA3 online", C_OK
            else:
                text, color = f"OSC > {b.cfg['osc']['host']}", C_DIM
        elif mode == "osc":  # where to, green while anything comes back
            alive = b.ma_seen and time.time() - b.ma_seen < 5  # any OSC coming back
            text, color = f"OSC > {b.cfg['osc']['host']}:{b.cfg['osc']['port']}", C_OK if alive else C_DIM
        elif mode == "feedback":
            text, color = b.fb["status"] or "", C_DIM
        else:
            text, color = self.fmt(mode), C_DIM
        self.setw(self.HDR_STATUS, text=text, colors=(C_PANEL, color, C_PANEL))
        self.setw(self.HDR_PAGE, text=self.fmt(b.screen_opt("left")))
        self.setw(self.HDR_MID, text=self.mid_text())

    def draw(self):
        self.state = {}
        self.refresh_ids = []
        self.send("CLR")
        self.send(f"BG {C_BG}")
        self.header()
        spec = self.custom_screen(self.name)
        if spec is not None:  # a profile's own screen
            if spec.get("type") == "keypad":
                self.draw_keypad(spec)
            else:
                self.draw_custom(spec)
        elif self.name == "keypad":
            self.draw_keypad()
        elif self.name == "encoders":
            self.draw_encoders()
        elif self.name == "setup":
            self.draw_setup()
        elif self.name.startswith("key"):
            self.draw_key_editor(int(self.name[3:]))
        elif self.name.startswith("exec"):
            self.draw_exec_entry(int(self.name[4:]))
        elif self.name.startswith("entry"):
            self.draw_value_entry(int(self.name[5:]))
        elif self.name == "fgroups":
            self.draw_fgroups()
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
        btns = [b for b in self.b.cfg["touch_buttons"] if isinstance(b, dict)][:18]
        cols = self.grid_columns(len(btns), self.portrait, self.b.screen_opt("columns"))
        by = top + fh + 6
        self.keymap = {}
        self.button_grid(btns, 4, by, w - 4, h - by - 2, cols, self.BTN0)

    # Keypad keys: a text is typed into the command line ("Please", "Clear", "<-"
    # and "Back" do what they say); an action dict does that action, e.g.
    # {"label": "Enter", "cmdline": "please"}, {"label": "Go", "osc": "/go"}.
    CMDLINE_KEYS = {"please": "Please", "clear": "Clear", "backspace": "<-"}

    def draw_keypad(self, spec=None):
        w, h = self.w, self.h
        top = self.header_h() + 4
        self.keymap = {}
        self.widget(self.CMDLINE, "L", 4, top, w - 8, 40, "000000", C_CMD, "000000", 1, 1, 0,
                    self.keypad_text())
        ky = top + 44
        rows = ((spec or {}).get("keys") or self.b.cfg.get("keypad") or self.KEYPAD)[:5]
        cols = max(1, min(5, max(len(r) for r in rows) if rows else 5))
        bw = (w - 4) // cols
        bh = (h - ky - 2) // max(1, len(rows))
        for r, row in enumerate(rows):
            for c, key in enumerate(row[:cols]):
                wid = self.KEY0 + r * 5 + c
                if isinstance(key, dict):  # an action
                    label = str(key.get("label", "?"))
                    self.keymap[wid] = key
                    color = C_BTN_ON if key.get("cmdline") == "please" else C_KEY2
                else:
                    label = str(key)
                    self.keymap[wid] = {"keypad": label}
                    digit = label.isdigit() or label == "."
                    color = C_BTN_ON if label == "Please" else C_BTN if digit else C_KEY2
                font = 0 if self.portrait and len(label) > 3 else 1
                bg = hexcolor(key.get("color"), color) if isinstance(key, dict) else color
                self.widget(wid, "B", 4 + c * bw, ky + r * bh, bw - 3, bh - 4, bg, C_TEXT, C_BTN_ON,
                            font, 0, 0, label)

    # ---- a profile's own screens: "screens": {"name": {"title", "columns",
    # "buttons": [actions with "label", "lit", colours], "back": "main"}}; open
    # one with {"screen": "name"}. {"type": "keypad", "keys": [...]} makes a
    # keypad screen (the built-in one is "keypad").
    def custom_screen(self, name):
        spec = (self.b.cfg.get("screens") or {}).get(name)
        return spec if isinstance(spec, dict) else None

    @staticmethod
    def grid_columns(n, portrait, given=None):
        """Columns for n buttons: as given, else what suits the count."""
        if given:
            return max(1, int(given))
        if portrait:
            return 1 if n <= 1 else 2 if n <= 4 else 3 if n <= 9 else 4
        return max(1, n) if n <= 3 else 4 if n <= 8 else 5

    def button_grid(self, btns, x, y, w, h, cols, wid0, font=1):
        rows = max(1, -(-len(btns) // cols))
        bw, bh = w // cols, h // rows
        for n, btn in enumerate(btns):
            r, c = divmod(n, cols)
            wid = wid0 + n
            self.keymap[wid] = btn
            self.widget(wid, "B", x + c * bw, y + r * bh, bw - 3, bh - 6, hexcolor(btn.get("color"), C_BTN),
                        hexcolor(btn.get("text_color"), C_TEXT), hexcolor(btn.get("lit_color"), C_BTN_ON),
                        0 if self.portrait and cols > 3 else font, 0, self.button_lit(btn), self.button_label(btn))

    def draw_custom(self, spec):
        w, h = self.w, self.h
        top = self.header_h() + 4
        self.keymap = {}
        if spec.get("title"):
            self.widget(self.CMDLINE, "L", 4, top, w - 8, 36, C_PANEL, C_TEXT, C_PANEL, 1, 0, 0,
                        self.fmt(spec["title"]))
            top += 40
        btns = [b for b in (spec.get("buttons") or []) if isinstance(b, dict)][:25]
        back = spec.get("back", "main")
        bottom = h - (62 if back else 4)
        cols = self.grid_columns(len(btns), self.portrait, spec.get("columns"))
        self.button_grid(btns, 4, top, w - 4, bottom - top, cols, self.KEY0)
        if back:
            self.keymap[self.BACK] = {"back": back}
            self.widget(self.BACK, "B", 4, h - 58, w - 8, 54, C_KEY2, C_TEXT, C_BTN_ON, 1, 0, 0, "Back")

    def back_button(self):
        self.keymap[self.BACK] = {"back": True}
        self.widget(self.BACK, "B", 4, self.h - 58, self.w - 8, 54, C_KEY2, C_TEXT, C_BTN_ON, 1, 0, 0, "Back")

    def simple_encoder(self, i):
        """'simple' encoders page: (label, value) from feedback or the profile."""
        b = self.b
        encs = b.cfg["encoders"]
        e = encs[i] if i < len(encs) and encs[i] else {}
        label = b.fb["encoder_label"].get(i) or e.get("label") or f"Encoder {i + 1}"
        return label, b.fb["encoder_value"].get(i) or "-"

    def encoder_info(self, i):
        """-> (what it controls, resolution label or None if it has none),
        for an encoder that isn't following MA's encoders."""
        encs = self.b.cfg["encoders"]
        act = encs[i] if i < len(encs) and encs[i] else {}
        if "attribute" in act:
            attr = act["attribute"]
            if not self.b.ma_linked():
                return attr, "No MA link"
            return attr, res_label(self.b.ma["res"].get(attr, 1.0))  # not reported: MA's default
        if "page" in act:
            return "Page", None
        if "cmd" in act:
            return act["cmd"].replace("{d}", "n"), None
        return "-", None

    def draw_encoders(self):
        """The two encoders, stacked like the hardware. Following MA: the
        selected feature, a 1/2 button to swap pairs, and for each encoder the
        attribute, its value (red = in the programmer, like MA) and Coarse/Fine.
        Otherwise (nothing selected in MA) what each encoder is set to."""
        w = self.w
        b = self.b
        self.enc_simple = b.screen_opt("encoders") == "simple"
        self.enc_mode = b.ma_encoders_on() and not self.enc_simple
        top = self.header_h() + 4
        self.keymap = {}
        if self.enc_mode:  # tap the bank's name: pick another bank (Dimmer, Position, ...)
            self.keymap[self.ENC_FEAT] = {"fgroups": True}
        self.widget(self.ENC_FEAT, "B", 4, top, w - 100, 40, C_PANEL, C_TEXT, C_PANEL, 1, 1, 0, "")
        self.keymap[self.ENC_PAGE] = {"encpage": 1}
        self.widget(self.ENC_PAGE, "B", w - 92, top, 88, 40, C_KEY2, C_TEXT, C_BTN_ON, 1, 0, 0, "")
        top += 46
        # the feature group's tabs (Gobo2, Gobo2Pos, ...), like MA's encoder bar
        self.enc_tabs = self.enc_mode and len(b.ma["tabs"]) > 1
        if self.enc_tabs:
            tw = (w - 4) // self.TABS_SHOWN
            for n in range(self.TABS_SHOWN):
                self.widget(self.ENC_TAB0 + n, "B", 4 + n * tw, top, tw - 3, 34, C_BTN, C_TEXT, C_BTN_ON, 0, 0, 0, "")
            top += 38
        ph = (self.h - 62 - top) // 2
        hw = b.cfg["hw"]["encoders"]
        for i in range(2):
            y, base = top + i * ph, self.ENC0 + i * 10
            self.widget(base, "L", 4, y, w - 8, ph - 6, C_PANEL, C_DIM, C_PANEL, 0, 1, 0, "")
            title = f"Encoder {i + 1}" + ("" if i < len(hw) and hw[i] else "  (not learnt)")
            self.widget(base + 4, "L", 4, y, w - 8, 26, C_PANEL, C_DIM, C_PANEL, 0 if not self.enc_mode else 1,
                        1, 0, title)
            vh = ph - 26 - 62
            if b.enc_click(i) == "pad":
                self.keymap[base + 1] = {"entry": i}  # tap the value to set one
            self.widget(base + 1, "B", 8, y + 28, w - 16, vh, C_PANEL, C_TEXT, C_PANEL, 2, 0, 0, "")
            by, bh = y + 30 + vh, ph - 30 - vh - 10
            self.widget(base + 2, "B", 8, by, w - 16, bh, C_BTN, C_TEXT, C_BTN_ON, 1, 0, 0, "")
        self.back_button()
        self.update_encoders()

    def draw_fgroups(self):
        """The encoder banks (MA's feature groups) the selected fixture has:
        tap one to put the encoders on it."""
        w = self.w
        top = self.header_h() + 4
        self.keymap = {}
        self.widget(self.SET_TITLE, "L", 4, top, w - 8, 36, C_PANEL, C_TEXT, C_PANEL, 1, 0, 0, "Encoder bank")
        groups = self.b.ma["groups"][:24]
        cur = self.b.ma["feat"][1]
        cols = 2 if len(groups) <= 8 else 3
        y = top + 40
        rows = max(1, -(-len(groups) // cols))
        bw, bh = (w - 4) // cols, min(70, (self.h - 62 - y) // rows)
        for n, g in enumerate(groups):
            r, c = divmod(n, cols)
            color = FEATURE_COLORS.get(g.lower(), C_KEY2)
            wid = self.SET0 + n
            self.keymap[wid] = {"fgroup": g}
            self.widget(wid, "B", 4 + c * bw, y + r * bh, bw - 3, bh - 4, shade(color, 0.45), C_TEXT,
                        shade(color, 0.9), 1, 0, 1 if g == cur else 0, g)
        if not groups:
            self.widget(self.SET0, "L", 4, y, w - 8, 60, C_BG, C_DIM, C_BG, 0, 0, 0, "Select fixtures in MA")
        self.keymap[self.BACK] = {"back": "encoders"}
        self.widget(self.BACK, "B", 4, self.h - 58, w - 8, 54, C_KEY2, C_TEXT, C_BTN_ON, 1, 0, 0, "Back")

    def show_tabs(self):
        """Fill the tab buttons: the current tab lit; with more tabs than fit, the
        last button pages through the rest."""
        tabs, cur = self.b.ma["tabs"], self.b.ma["feat"][0]
        n_btn = self.TABS_SHOWN
        paged = len(tabs) > n_btn
        per = n_btn - 1 if paged else n_btn
        pages = max(1, -(-len(tabs) // per))
        if cur in tabs and not self.tab_paged:  # show the page with the current tab
            self.tab_page = tabs.index(cur) // per
        self.tab_page %= pages
        shown = tabs[self.tab_page * per:(self.tab_page + 1) * per]
        for n in range(n_btn):
            wid = self.ENC_TAB0 + n
            if paged and n == n_btn - 1:
                self.keymap[wid] = {"tabpage": 1}
                self.setw(wid, value=0, text=f"more {self.tab_page + 1}/{pages}", colors=(C_KEY2, C_TEXT, C_BTN_ON))
            elif n < len(shown):
                self.keymap[wid] = {"tab": shown[n]}
                self.setw(wid, value=1 if shown[n] == cur else 0, text=shown[n][:12], colors=(C_BTN, C_TEXT, C_BTN_ON))
            else:
                self.keymap.pop(wid, None)
                self.setw(wid, value=0, text="", colors=(C_PANEL, C_DIM, C_PANEL))

    def update_encoders(self):
        self.update_enc_strip()
        if self.name.startswith("entry"):  # typing a value: keep MA's current one fresh
            self.setw(self.SET_TITLE, text=self.value_title(int(self.name[5:])))
            return
        if self.name != "encoders":
            return
        b = self.b
        if self.enc_simple:  # label + value per encoder, from the profile / feedback
            self.setw(self.ENC_FEAT, text=" " + self.fmt(b.screen_opt("encoders_title")),
                      colors=(C_PANEL, C_TEXT, C_PANEL))
            self.keymap.pop(self.ENC_PAGE, None)
            self.setw(self.ENC_PAGE, text="", colors=(C_PANEL, C_DIM, C_PANEL))
            for i in range(2):
                base = self.ENC0 + i * 10
                label, value = self.simple_encoder(i)
                self.setw(base + 4, text=f" {i + 1}: {label}", colors=(C_PANEL, C_DIM, C_PANEL))
                self.setw(base + 1, text=value, colors=(C_PANEL, C_TEXT, C_PANEL))
                self.keymap.pop(base + 2, None)
                self.setw(base + 2, value=0, text="", colors=(C_PANEL, C_PANEL, C_PANEL))
            return
        if (b.ma_encoders_on() != self.enc_mode or  # MA started/stopped giving encoders: new layout
                (b.ma_encoders_on() and (len(b.ma["tabs"]) > 1) != self.enc_tabs)):
            self.draw_encoders()
            return
        if self.enc_tabs:
            self.show_tabs()
        ma = b.ma
        fname, group, has_sel = ma["feat"]
        gcolor = FEATURE_COLORS.get(group.lower(), FEATURE_COLORS.get(fname.lower(), C_KEY2))
        pages = b.enc_pages()
        if self.enc_mode:
            title = group if self.enc_tabs and group else fname
            more = "  v" if len(b.ma["groups"]) > 1 else ""  # tap for the other banks
            self.setw(self.ENC_FEAT, text=" " + title + more, colors=(shade(gcolor, 0.55), C_TEXT, C_PANEL))
            self.setw(self.ENC_PAGE, text=f"{b.enc_page + 1}/{pages}",
                      colors=(C_KEY2, C_TEXT if pages > 1 else C_DIM, C_BTN_ON))
        else:
            hint = ("Select fixtures in MA" if b.ma_linked() and fname else
                    "Encoders" if b.ma_linked() else "Encoders (no MA link)")
            self.setw(self.ENC_FEAT, text=" " + hint, colors=(C_PANEL, C_TEXT, C_PANEL))
            self.setw(self.ENC_PAGE, text="1/1", colors=(C_KEY2, C_DIM, C_BTN_ON))
        for i in range(2):
            base = self.ENC0 + i * 10
            if self.enc_mode:
                e = b.ma_encoder(i)
                if not e:
                    self.setw(base + 4, text="", colors=(C_PANEL, C_DIM, C_PANEL))
                    self.setw(base + 1, text="-", colors=(C_PANEL, C_DIM, C_PANEL))
                    self.keymap.pop(base + 2, None)
                    self.setw(base + 2, value=0, text="", colors=(C_PANEL, C_DIM, C_PANEL))
                    continue
                self.setw(base + 4, text=f" {i + 1}: {e['pretty']}", colors=(shade(gcolor, 0.4), C_TEXT, C_PANEL))
                self.setw(base + 1, text=e["value"] or "-",  # like MA: red in the programmer, else grey
                          colors=(C_PANEL, C_PROG if e["prog"] else C_VALUE, C_PANEL))
                res = res_label(e["res"])
                self.keymap[base + 2] = {"resolution": e["attr"]}
                self.setw(base + 2, value=1 if res != "Coarse" else 0, text=res, colors=(C_BTN, C_TEXT, C_BTN_ON))
                continue
            what, res = self.encoder_info(i)
            self.setw(base + 1, text=what, colors=(C_PANEL, C_TEXT, C_PANEL))
            if res is None:
                self.keymap.pop(base + 2, None)
                self.setw(base + 2, value=0, text="-", colors=(C_PANEL, C_DIM, C_PANEL))
            else:
                act = b.cfg["encoders"][i]
                self.keymap[base + 2] = {"resolution": act["attribute"]}
                self.setw(base + 2, value=1 if res in ("Fine", "Ultra") else 0, text=res,
                          colors=(C_BTN, C_TEXT, C_BTN_ON))

    # ---- typing a value for an encoder's attribute (encoder click)
    def value_title(self, i):
        a = self.b.encoder_attr(i)
        if not a:
            return "-"
        now = f"   (now {a[2]})" if a[2] else ""
        return f"{a[1]}{now}\n{self.value_entry or ''}_"

    def show_sets(self, i):
        """Fill the named-value buttons from the current scroll row."""
        sets = self.b.choices(i)
        rows = self.sets_rows
        last_row = max(0, -(-len(sets) // 3) - rows)
        self.sets_page = max(0, min(self.sets_page, last_row))  # first row shown
        first = self.sets_page * 3
        for n in range(rows * 3):
            wid = self.SET0 + 20 + n
            if wid not in self.state:
                continue
            k = first + n
            if k < len(sets):
                self.keymap[wid] = {"setv": k + 1, "enc": i}
                self.setw(wid, value=1 if k == self.sets_sel else 0, text=sets[k][:14],
                          colors=(C_KEY2, C_TEXT, C_BTN_ON))
            else:
                self.keymap.pop(wid, None)
                self.setw(wid, text="", colors=(C_BG, C_DIM, C_BG))
        if self.SET_MORE in self.state:
            self.setw(self.SET_MORE, text=f"turn the encoder to choose, click to use   "
                                           f"{first + 1}-{min(first + rows * 3, len(sets))} of {len(sets)}")

    def entry_sets(self):
        """Named values on the open value page, or []."""
        if not self.name.startswith("entry"):
            return []
        return self.b.choices(int(self.name[5:]))

    def scroll_sets(self, d):
        """Encoder turned on the value page: move the highlight through the named
        values (the list follows it); clicking the encoder then applies it."""
        sets = self.entry_sets()
        if not sets or self.SET0 + 20 not in self.state:
            return False
        self.sets_sel = max(0, min(len(sets) - 1, (self.sets_sel if self.sets_sel >= 0 else -1) + d))
        row = self.sets_sel // 3
        if row < self.sets_page:
            self.sets_page = row
        elif row >= self.sets_page + self.sets_rows:
            self.sets_page = row - self.sets_rows + 1
        self.show_sets(int(self.name[5:]))
        return True

    def encoder_click(self):
        """Encoder clicked on the value page: apply the highlighted named value,
        or Set the typed number; otherwise just close."""
        i = int(self.name[5:])
        sets = self.entry_sets()
        if 0 <= self.sets_sel < len(sets):
            self.b.apply_choice(i, self.sets_sel + 1)
        elif self.value_entry not in ("", "-", "."):
            self.b.apply_number(i, self.value_entry)
        self.set_screen("encoders")

    def draw_value_entry(self, i, keep=False):
        """Number pad for the encoder's attribute, and above it the selected
        fixture's named values (gobos, colour slots...) when it has any.
        keep: redraw (named values arrived, next page of them), keep the typing."""
        w = self.w
        top = self.header_h() + 4
        b = self.b
        a = b.encoder_attr(i)
        if keep:
            self.state = {}
            self.refresh_ids = []
            self.send("CLR")
            self.send(f"BG {C_BG}")
            self.header()
        else:
            self.value_entry = ""
            self.sets_page = 0
            ma_choices = b.enc_cfg(i).get("choices", "ma") == "ma" and not b.generic()
            if a and b.ma_linked() and ma_choices:
                b.ma["sets"].pop(a[0].lower(), None)
                b.ma_plugin(f"sets {a[0]}")  # the answer redraws this page
        self.keymap = {}
        self.widget(self.SET_TITLE, "L", 4, top, w - 8, 56, "000000", C_CMD, "000000", 1, 0, 0, self.value_title(i))
        y = top + 60
        sets = b.choices(i)
        pad = b.enc_pad(i)
        if not keep:
            self.sets_sel = -1
        if sets and self.sets_sel < 0:  # highlight the one it is on now ("0 Closed" -> Closed)
            now = (a[2] or "").lower()
            for k, name in enumerate(sets):
                if now == name.lower() or now.endswith(" " + name.lower()):
                    self.sets_sel = k
                    self.sets_page = max(0, k // 3 - 1)
                    break
        if sets:  # named values (above the number pad, or the whole page without one)
            sh = 40 if pad else 48
            fit = 3 if pad else max(1, (self.h - 62 - 34 - y) // sh)
            rows = min(fit, -(-len(sets) // 3))
            self.sets_rows = rows
            sw = (w - 4) // 3
            for n in range(rows * 3):
                r, c = divmod(n, 3)
                self.widget(self.SET0 + 20 + n, "B", 4 + c * sw, y + r * sh, sw - 3, sh - 4, C_KEY2, C_TEXT,
                            C_BTN_ON, 0, 0, 0, "")
            y += rows * sh
            if len(sets) > rows * 3:  # more than fit: the encoder (or this button) scrolls
                self.keymap[self.SET_MORE] = {"setpage": 1, "enc": i}
                self.widget(self.SET_MORE, "B", 4, y, w - 8, 30, C_PANEL, C_DIM, C_BTN_ON, 0, 0, 0, "")
                y += 34
            self.show_sets(i)
        if not pad:  # choices only
            self.back_button()
            return
        bw, bh = (w - 4) // 3, (self.h - 4 - y) // 5
        keys = ["7", "8", "9", "4", "5", "6", "1", "2", "3", ".", "0", "<-"]
        for n, k in enumerate(keys):
            r, c = divmod(n, 3)
            wid = self.SET0 + n
            self.keymap[wid] = {"vkey": k, "enc": i}
            self.widget(wid, "B", 4 + c * bw, y + r * bh, bw - 3, bh - 4, C_BTN if k.isdigit() else C_KEY2,
                        C_TEXT, C_BTN_ON, 1, 0, 0, k)
        y += 4 * bh
        self.keymap[self.SET0 + 12] = {"vkey": "-", "enc": i}
        self.widget(self.SET0 + 12, "B", 4, y, bw - 3, bh - 4, C_KEY2, C_TEXT, C_BTN_ON, 1, 0, 0, "+/-")
        self.keymap[self.BACK] = {"back": True}
        self.widget(self.BACK, "B", 4 + bw, y, bw - 3, bh - 4, C_KEY2, C_TEXT, C_BTN_ON, 1, 0, 0, "Back")
        self.keymap[self.SET0 + 13] = {"vkey": "Set", "enc": i}
        self.widget(self.SET0 + 13, "B", 4 + 2 * bw, y, bw - 3, bh - 4, C_BTN_ON, C_TEXT, C_BTN_ON, 1, 0, 0, "Set")

    # ---- Setup: what each hardware key does
    KEY_CHOICES = [
        ("Executor...", "exec"), ("Page -", {"page": -1}), ("Page +", {"page": 1}), ("Clear", {"key": "Clear"}),
        ("Oops", {"cmd": "Oops"}), ("Please", {"key": "Please"}), ("Go +", {"cmd": "Go+"}), ("Go -", {"cmd": "Go-"}),
        ("Pause", {"cmd": "Pause"}), ("Highlight", {"cmd": "Highlight"}), ("Blind", {"cmd": "Blind"}),
        ("Last", {"cmd": "Previous"}), ("Next", {"cmd": "Next"}), ("Store", {"key": "Store"}),
        ("Update", {"key": "Update"}), ("Keypad", {"screen": "keypad"}), ("Encoders", {"screen": "encoders"}),
        ("Enc. 1/2", {"encpage": 1}), ("Nothing", {}),
    ]

    GENERIC_KEY_CHOICES = [  # generic profiles: no MA functions
        ("Default OSC", None), ("Page -", {"page": -1}), ("Page +", {"page": 1}),
        ("Keypad", {"screen": "keypad"}), ("Encoders", {"screen": "encoders"}), ("Enc. 1/2", {"encpage": 1}),
    ]

    def key_choices(self):
        base = self.GENERIC_KEY_CHOICES if self.b.generic() else self.KEY_CHOICES
        own = [(str(spec.get("title") or name)[:12], {"screen": name})
               for name, spec in (self.b.cfg.get("screens") or {}).items()
               if isinstance(spec, dict) and name not in ("main", "keypad", "encoders")]  # those are listed
        return (base + own)[:24]

    def key_label(self, k):
        act = self.key_act(k)
        if self.b.generic() and not act:  # sends the profile's key address
            addrs = self.b.cfg["osc"].get("generic") or DEFAULTS["osc"]["generic"]
            return addrs.get("key", "/key/{n}").replace("{n}", str(k + 1))
        return self.describe(act)

    def in_setup(self):
        # setup, key<n> (editing a key), exec<n> (its executor number) - not "keypad"
        if self.custom_screen(self.name) is not None:
            return False
        return self.name == "setup" or re.fullmatch(r"(key|exec)\d+", self.name) is not None

    @classmethod
    def describe(cls, act):
        if not act:
            return "-"
        if "exec" in act:
            return f"Exec {act['exec']}"
        if "osc" in act:
            return act["osc"]
        if "label" in act and ("cmdline" in act or "screen" in act):
            return act["label"]
        for label, a in cls.KEY_CHOICES:
            if a == act:
                return label
        if "cmd" in act:
            return act["cmd"]
        if "key" in act:
            return act["key"]
        if "label" in act:
            return act["label"]
        if "screen" in act:
            return str(act["screen"]).capitalize()
        return "?"

    def key_act(self, i):
        keys = self.b.cfg["keys"]
        return keys[i] if i < len(keys) else None

    def set_key(self, i, act):
        keys = self.b.cfg["keys"]
        while len(keys) <= i:
            keys.append(None)
        keys[i] = act or None
        save_active(self.b.cfg, self.b.ser, keep=True)
        print(f"Key {i + 1} now: {self.describe(act)}")
        self.set_screen("setup")

    def setup_title(self, y, text):
        self.widget(self.SET_TITLE, "L", 4, y, self.w - 8, 40, C_PANEL, C_TEXT, C_PANEL, 0, 0, 0, text)

    def draw_setup(self):
        """All 20 keys and what they do. Press a key on the controller (or tap
        it here) to change it."""
        w = self.w
        top = self.header_h() + 4
        self.keymap = {}
        self.setup_title(top, "Press a key on the controller\n(or tap it here) to change it")
        hw = self.b.cfg["hw"]["keys"]
        n = max(20, len(hw))
        cols = 5  # like the hardware: rows of five, lined up with the faders
        rows = -(-n // cols)
        gy = top + 44
        bw, bh = (w - 4) // cols, (self.h - 62 - 44 - gy) // rows
        for k in range(n):
            r, c = divmod(k, cols)
            wid = self.SET0 + k
            learnt = k < len(hw) and hw[k]
            self.keymap[wid] = {"edit": k}
            what = self.key_label(k)
            if self.portrait and what.startswith("Exec "):
                what = "Ex " + what[5:]  # 5 across is narrow
            self.widget(wid, "B", 4 + c * bw, gy + r * bh, bw - 3, bh - 4, C_BTN if learnt else C_PANEL,
                        C_TEXT if learnt else C_DIM, C_BTN_ON, 0, 0, 0, f"{k + 1}\n{what}")
        ma = not self.b.generic()
        self.keymap[self.SET_RESET] = {"reset_keys": True}
        self.widget(self.SET_RESET, "B", 4, self.h - 104, (w - 112) if ma else (w - 8), 40, C_PANEL, C_DIM,
                    C_BTN_ON, 0, 0, 0, "Set all keys back to defaults")
        if ma:  # MA diagnostics
            self.keymap[self.SET_PROBE] = {"probe": True}
            self.widget(self.SET_PROBE, "B", w - 104, self.h - 104, 100, 40, C_PANEL, C_DIM, C_BTN_ON, 0, 0, 0,
                        "MA probe")
        self.back_button()

    def draw_key_editor(self, i):
        w = self.w
        top = self.header_h() + 4
        self.keymap = {}
        self.setup_title(top, f"Key {i + 1}: {self.key_label(i)}\nchoose what it does")
        choices = self.key_choices()
        cols = 4 if self.portrait else 5
        rows = -(-len(choices) // cols)
        gy = top + 44
        bw, bh = (w - 4) // cols, min(70, (self.h - 62 - gy) // rows)
        cur = self.key_act(i) or {}
        for n, (label, act) in enumerate(choices):
            r, c = divmod(n, cols)
            wid = self.SET0 + n
            self.keymap[wid] = {"assign": i, "choice": act}
            on = (act or {}) == cur or (act == "exec" and "exec" in cur)
            self.widget(wid, "B", 4 + c * bw, gy + r * bh, bw - 3, bh - 4, C_KEY2 if act == "exec" else C_BTN,
                        C_TEXT, C_BTN_ON, 0, 0, 1 if on else 0, label)
        self.back_button()

    def exec_title(self, i):
        return f"Key {i + 1}: executor number\n{self.exec_entry or '_'}"

    def draw_exec_entry(self, i):
        w = self.w
        top = self.header_h() + 4
        self.keymap = {}
        self.widget(self.SET_TITLE, "L", 4, top, w - 8, 60, "000000", C_CMD, "000000", 1, 0, 0, self.exec_title(i))
        gy = top + 66
        bw, bh = (w - 4) // 3, (self.h - 62 - gy) // 4
        for n, d in enumerate(["7", "8", "9", "4", "5", "6", "1", "2", "3", "<-", "0", "OK"]):
            r, c = divmod(n, 3)
            wid = self.SET0 + n
            self.keymap[wid] = {"digit": d, "key": i}
            self.widget(wid, "B", 4 + c * bw, gy + r * bh, bw - 3, bh - 4,
                        C_BTN_ON if d == "OK" else C_BTN if d.isdigit() else C_KEY2, C_TEXT, C_BTN_ON, 1, 0, 0, d)
        self.back_button()

    # ---- live updates
    def button_index(self, btn):
        buttons = self.b.cfg["touch_buttons"]
        return buttons.index(btn) if btn in buttons else None

    def button_label(self, btn):
        k = self.button_index(btn)
        return self.b.fb["button_label"].get(k) or btn.get("label", "?")

    def button_lit(self, btn):
        if btn.get("lit") in self.b.fb["lit"]:  # its own feedback address
            return 1 if self.b.fb["lit"][btn["lit"]] else 0
        k = self.button_index(btn)
        if k is not None and k in self.b.fb["button"]:  # feedback says
            return 1 if self.b.fb["button"][k] else 0
        ma = self.b.ma
        if "state" in btn:
            return 1 if ma["master"].get(btn["state"].lower()) else 0
        if "exec" in btn:
            return 1 if ma["run"].get(btn["exec"]) else 0
        return 0

    def update_buttons(self):
        if self.name != "main" and self.custom_screen(self.name) is None:
            return
        for wid, btn in self.keymap.items():
            if self.button_index(btn) is not None:
                self.setw(wid, value=self.button_lit(btn), text=self.button_label(btn))
            else:
                self.setw(wid, value=self.button_lit(btn))

    def update_fader(self, i):
        """Redraw fader i soon: faders change far faster than the screen link
        can show, so only the latest state is drawn (see flush_faders)."""
        self.dirty_faders.add(i)

    def flush_faders(self):
        ser = self.b.ser
        if not self.dirty_faders or (ser and ser.backlog > 4):  # link busy: draw newer values later
            return
        for i in sorted(self.dirty_faders):
            self.draw_fader(i)
        self.dirty_faders.clear()

    def draw_fader(self, i):
        """Bar = MA's real level (when known), marker = the physical fader."""
        if self.name != "main" or (self.FADER0 + i) not in self.state:
            return
        b = self.b
        f = b.cfg["faders"][i]
        ma = b.ma_fader(i)
        pos = b.fader_pos[i]
        shown = ma if ma is not None else pos
        if b.picked[i] and pos is not None and time.time() - b.moved_at[i] < b.HANDS_ON:
            shown = pos  # in your hand: MA's echo lags, show the fader itself
        ex = f.get("exec")
        name = b.fb["fader_name"].get(i) or (b.ma["name"].get(ex) if b.ma_linked() and ex else None)
        name = name or f.get("name") or ((str(ex) if self.portrait else f"Exec {ex}") if ex else f"F{i + 1}")
        if self.portrait:
            name = name[:7]
        text = f"{name}\n" + ("--" if shown is None else f"{round(shown)}%")
        waiting = ma is not None and pos is not None and not b.picked[i]
        if waiting:
            text += "\n" + ("^ ^ ^" if pos < ma else "v v v")
        marker = int(pos * 10) if (ma is not None and pos is not None) else -1
        # its colour: from feedback, MA's sequence, or the profile; dimmed behind,
        # brighter for the level
        color = (hexcolor(b.fb["fader_color"].get(i), None)
                 or (b.ma["color"].get(ex) if b.ma_linked() and ex else None)
                 or hexcolor(f.get("color"), None))
        bg, bar = (shade(color, 0.4), shade(color, 0.85)) if color else (C_BAR_BG, C_BAR)
        bg, bar = bg or C_BAR_BG, bar or C_BAR
        self.setw(self.FADER0 + i, value=int((shown or 0) * 10), text=text, marker=marker,
                  colors=(bg, C_CMD if waiting else C_TEXT, C_WAIT if waiting else bar))

    def keypad_text(self):
        if not self.cmdline and self.b.ma_linked():
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
        act = self.keymap.get(wid) or self.keymap_hdr.get(wid)
        if not act:
            return
        if "setup" in act:
            self.set_screen("main" if self.in_setup() else "setup")
            return
        if "edit" in act:
            self.set_screen(f"key{act['edit']}")
            return
        if "assign" in act:
            i, choice = act["assign"], act["choice"]
            if choice == "exec":
                self.exec_entry = ""
                self.set_screen(f"exec{i}")
                return
            self.set_key(i, choice)
            return
        if "digit" in act:
            d = act["digit"]
            if d == "<-":
                self.exec_entry = self.exec_entry[:-1]
            elif d == "OK":
                if self.exec_entry:
                    self.set_key(act["key"], {"exec": int(self.exec_entry)})
                return
            elif len(self.exec_entry) < 4:
                self.exec_entry = (self.exec_entry + d).lstrip("0")
            self.setw(self.SET_TITLE, text=self.exec_title(act["key"]))
            return
        if "entry" in act:
            if self.b.encoder_attr(act["entry"]):
                self.set_screen(f"entry{act['entry']}")
            return
        if "setv" in act:
            self.b.apply_choice(act["enc"], act["setv"])
            self.set_screen("encoders")
            return
        if "setpage" in act:  # tap: next rows, then back to the top
            n = len(self.b.choices(act["enc"]))
            rows = self.sets_rows
            self.sets_page = self.sets_page + rows if (self.sets_page + rows) * 3 < n else 0
            self.show_sets(act["enc"])
            return
        if "vkey" in act:
            k, i = act["vkey"], act["enc"]
            a = self.b.encoder_attr(i)
            if k == "Set":
                if a and self.value_entry not in ("", "-", "."):
                    self.b.apply_number(i, self.value_entry)
                self.set_screen("encoders")
                return
            if k == "<-":
                self.value_entry = self.value_entry[:-1]
            elif k == "-":
                self.value_entry = self.value_entry[1:] if self.value_entry.startswith("-") else "-" + self.value_entry
            elif k == "." and "." in self.value_entry:
                return
            elif len(self.value_entry) < 8:
                self.value_entry += k
            self.setw(self.SET_TITLE, text=self.value_title(i))
            return
        if "probe" in act:
            if self.b.ma_linked():
                self.b.probe_lines = []
                self.b.ma_plugin("probe")
                self.setw(self.SET_TITLE, text="Asking MA...\n(saved to a file on the computer)")
            else:
                self.setw(self.SET_TITLE, text="MA isn't linked:\nno probe possible")
            return
        if "reset_keys" in act:
            if time.time() - self.reset_armed > 4:  # first tap: ask for a second one
                self.reset_armed = time.time()
                self.setw(self.SET_RESET, text="Tap again to reset all 20 keys", colors=(C_WAIT, C_CMD, C_BTN_ON))
                return
            self.reset_armed = 0.0
            self.b.cfg["keys"] = copy.deepcopy(DEFAULTS["keys"])
            save_active(self.b.cfg, self.b.ser, keep=True)
            print("Keys set back to the defaults.")
            self.draw()
            return
        if "keypad" in act:
            if act["keypad"] == "Back":
                self.set_screen("main")
            else:
                self.b.ma_key(act["keypad"])
        elif "back" in act:
            self.set_screen(act["back"] if isinstance(act["back"], str) else
                            "setup" if re.fullmatch(r"key\d+", self.name) else
                            "encoders" if self.name.startswith("entry") else
                            f"key{self.name[4:]}" if self.name.startswith("exec") else "main")
        elif "encpage" in act:
            self.b.next_enc_page()
        elif "fgroups" in act:
            if self.b.ma["groups"]:
                self.set_screen("fgroups")
        elif "fgroup" in act:
            self.b.ma_plugin(f"group {act['fgroup']}")  # MA's report switches the encoders
            self.b.enc_page = 0
            self.tab_paged = False
            self.set_screen("encoders")
        elif "tab" in act:
            self.b.ma_plugin(f"tab {act['tab']}")  # MA's report switches the encoders
            self.b.enc_page = 0
            self.tab_paged = False
        elif "tabpage" in act:
            self.tab_page += 1
            self.tab_paged = True
            self.show_tabs()
        elif not self.generic_button(act, 1):
            self.b.do_action(act, True)

    def generic_button(self, act, value):
        """Generic OSC profile: a touch button sends /button/<n> (except the ones
        that switch the controller's own screens and pages)."""
        buttons = self.b.cfg["touch_buttons"]
        if (not self.b.generic() or act not in buttons or "screen" in act or "page" in act
                or "osc" in act):  # its own address: do_action sends it
            return False
        self.b.generic_send("button", buttons.index(act) + 1, self.b.onoff("button", bool(value), act))
        return True

    def on_release(self, wid):
        act = self.keymap.get(wid)
        if act and not any(k in act for k in ("keypad", "back", "encpage", "tab", "tabpage", "fgroups", "fgroup", "edit", "assign",
                                              "digit", "reset_keys", "entry", "vkey", "setv", "setpage", "probe")):
            if not self.generic_button(act, 0):
                self.b.do_action(act, False)

    def local_key(self, k):
        """The command line, kept here and sent to MA on Please."""
        if k == "<-":  # a whole keyword ("Fixture "), else one character ("12" -> "1")
            if self.cmdline.endswith(" "):
                self.cmdline = self.cmdline.rstrip()
                self.cmdline = self.cmdline[:self.cmdline.rfind(" ") + 1] if " " in self.cmdline else ""
            else:
                self.cmdline = self.cmdline[:-1]
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
        self.net = 0             # movement since the click-size check last looked
        self.div = max(1, div)

    def rests(self):
        """States the knob sits in between clicks (A/B pulled up: 3 = both open)."""
        return {3} if self.div >= 4 else {0, 3} if self.div >= 2 else {0, 1, 2, 3}

    def update(self, a, b):
        """-> clicks to act on. A click only counts once the knob settles into a
        detent, so wiggling (or contact bounce) that comes back adds up to 0."""
        cur = (a << 1) | b
        step = self.TABLE[(self.state << 2) | cur]
        self.acc += step
        self.net += step
        self.state = cur
        if cur not in self.rests():
            return 0
        detents = 0
        if abs(self.acc) * 2 >= self.div:  # most of a click (tolerates a missed edge)
            detents = round(self.acc / self.div) or (1 if self.acc > 0 else -1)
        self.acc = 0
        return detents


def resource(*parts):
    """A file shipped with the bridge (also inside the Windows .exe)."""
    base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(base, *parts)


class Bridge:
    ECHO_WINDOW = 3.0   # s: MA's report of our own fader move can arrive this late
    HANDS_ON = 1.5      # s after the last physical move the fader stays ours
    def __init__(self, cfg):
        self.cfg = cfg
        self.page = cfg.get("page", 1)
        self.ser = None
        self.screen = Screen(self)
        self.fader_pos = [None] * 5          # physical position 0..100
        self.fader_sent = [None] * 5
        self.picked = [True] * 5             # physical fader is in control (soft takeover)
        self.sent_hist = [[] for _ in range(5)]  # (time, value) we sent recently: MA echoes these back late
        self.moved_at = [0.0] * 5            # last physical movement
        self.fader_cmd_at = [0.0] * 5
        self.fader_cmd_pending = [None] * 5
        self.fader_arg = [None] * 5          # generic: the value last sent, in the profile's range
        self.verbose = False
        self.ma = dict(alive=0.0, page=None, fader={}, run={}, name={}, master={}, cmdline="",
                       busy="", busy_at=0.0, res={}, color={},
                       feat=("", "", False), encn=0, encs={}, ver=None, linked_at=0.0,
                       sets={},  # attribute -> named values of the selected fixture
                       tabs=[],  # the feature group's features the fixture has
                       groups=[])  # the feature groups (encoder banks) it has  # MA's encoders (see report_encoders)
        self.keys_down = set()               # hardware keys whose press was acted on
        self.probe_lines = []                # MA probe answer being received
        self.enc_page = 0                    # which pair of MA's encoders the hardware ones follow
        self.enc_edge_at = [0.0, 0.0]        # encoder click-size auto-detection
        self.enc_checked = [0.0, 0.0]
        self.enc_rest = [{}, {}]              # settled state -> times seen
        self.ma_values = {}                  # (page, exec) -> 0..100 from plain OSC feedback
        self.ma_seen = 0
        self.ma_installed_at = 0.0
        self.last_cmd = ""
        self.pins = {}
        self.encs = []
        self.pi_ready = False
        self.pi_heard = 0.0
        self.pi_proto = 0
        self.pi_version = ""
        self.last_port = None                # where the controller was (Mega flashing)
        self.mega_version = None             # its firmware, from its HELLO
        self.raw_analog = {}                 # Mega analog channel -> last raw reading
        self.last_sent = ""                  # the last OSC message sent (top bar, generic)
        self.fb = dict(fader={}, fader_name={}, fader_color={}, button={}, button_label={}, lit={},
                       encoder_label={}, encoder_value={}, text=None, status=None, seen=0.0)
        self.fb_rules = []
        self.hold = threading.Event()        # the app wants the port (set) ...
        self.held = threading.Event()        # ... and has it (set by run)
        self.quit = threading.Event()
        self.started = time.time()
        self.midi = lxmidi.Midi(self, user_dir=os.path.join(PROFILE_DIR, "midi"))
        self.build_maps()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx = None
        self.open_osc()
        self.build_feedback()

    def open_osc(self):
        """(Re)open OSC from cfg["osc"]: where to send, and the feedback port."""
        o = self.cfg["osc"]
        self.dest = (o["host"], int(o["port"]))
        self.prefix = o["prefix"].rstrip("/")
        port = int(o["listen_port"])
        if self.rx and self.rx.getsockname()[1] == port:
            return
        if self.rx:
            self.rx.close()  # its reader thread ends
            self.rx = None
        try:
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.bind(("0.0.0.0", port))
            self.rx = rx
            start_thread(self.osc_reader, rx)
        except OSError as e:
            print(f"Can't listen for MA feedback on port {port}: {e}")
            if getattr(e, "errno", None) in (48, 98, 10048):  # address in use (Mac, Linux, Windows)
                print("  Another bridge is probably still running (e.g. the auto-start one):"
                      " it sends to MA too. Stop it first.")

    def generic(self):
        """Talking plain OSC to something that isn't MA (a connection profile)."""
        return self.cfg["osc"].get("type", "ma3") == "generic"

    def screen_opt(self, key):
        """What the touchscreen shows: the profile's "screen", else the defaults
        for its connection type (MA things only for MA)."""
        own = self.cfg.get("screen") or {}
        if key in own:
            return own[key]
        return SCREEN_DEFAULTS["generic" if self.generic() else "ma3"][key]

    def build_feedback(self):
        """The profile's feedback addresses as patterns ({n} = the number)."""
        fb = self.cfg.get("feedback")
        if fb is None:
            fb = FEEDBACK_DEFAULT if self.generic() else {}
        rules = []

        def add(addr, kind, n=None):
            if not isinstance(addr, str) or not addr.startswith("/"):
                return
            rx = re.escape(addr).replace(r"\{n\}", r"(\d+)")
            rules.append((re.compile(rx + "$"), kind, n))
        for i, f in enumerate(self.cfg.get("faders", [])):  # items' own addresses first
            if f and f.get("feedback"):
                add(f["feedback"], "fader", i + 1)
        lit = [b for b in self.cfg.get("touch_buttons", []) if isinstance(b, dict)]
        for spec in (self.cfg.get("screens") or {}).values():
            if isinstance(spec, dict):
                lit += [b for b in spec.get("buttons") or [] if isinstance(b, dict)]
                lit += [k for row in spec.get("keys") or [] for k in row if isinstance(k, dict)]
        for btn in lit:
            if btn.get("lit"):
                add(btn["lit"], "lit", btn["lit"])
        for i, e in enumerate(self.cfg.get("encoders", [])):
            if e and e.get("value"):
                add(e["value"], "encoder_value", i + 1)
        for kind, addr in fb.items():
            add(addr, kind)
        self.fb_rules = rules

    def on_feedback(self, addr, args):
        """OSC from a generic target (or extra feedback in any profile)."""
        for rx, kind, fixed in self.fb_rules:
            m = rx.match(addr)
            if not m:
                continue
            if kind == "lit":  # a button's own address
                val = args[0] if args else None
                on = val > 0 if isinstance(val, (int, float)) else str(val).lower() in ("1", "on", "true", "yes")
                self.fb["lit"][fixed] = on
                self.fb["seen"] = time.time()
                self.midi.dirty = True
                if self.pi_ready:
                    self.screen.update_buttons()
                return True
            n = fixed or (int(m.group(1)) if m.groups() else None)
            val = args[0] if args else None
            self.fb["seen"] = time.time()
            self.midi.dirty = True
            scr = self.screen if self.pi_ready else None
            if kind == "fader" and n and isinstance(val, (int, float)):
                f = self.cfg["faders"][n - 1] if n - 1 < len(self.cfg["faders"]) else {}
                level = self.fader_level(val, (f or {}).get("range"))
                self.fb["fader"][n - 1] = level
                if n - 1 < len(self.fader_pos):
                    self.fader_feedback(n - 1, level)
            elif kind in ("fader_name", "fader_color") and n:
                self.fb[kind][n - 1] = str(val or "").lstrip("#")
                if scr:
                    scr.update_fader(n - 1)
            elif kind == "button" and n:
                on = val > 0 if isinstance(val, (int, float)) else str(val).lower() in ("1", "on", "true", "yes")
                self.fb["button"][n - 1] = on
                if scr:
                    scr.update_buttons()
            elif kind == "button_label" and n:
                self.fb["button_label"][n - 1] = str(val or "")
                if scr:
                    scr.update_buttons()
            elif kind in ("encoder_label", "encoder_value") and n:
                self.fb[kind][n - 1] = str(val if val is not None else "")
                if scr:
                    scr.update_encoders()
            elif kind in ("text", "status"):
                self.fb[kind] = str(val if val is not None else "")
                if scr:
                    scr.update_header()
            elif kind == "page" and isinstance(val, (int, float)):
                self.set_page(int(val), from_ma=True)
            return True
        return False

    def generic_send(self, what, n, value):
        """Generic OSC: e.g. /fader/3 0.5, /key/7 1 (addresses from the profile)."""
        addrs = self.cfg["osc"].get("generic") or DEFAULTS["osc"]["generic"]
        self.osc(addrs.get(what, DEFAULTS["osc"]["generic"][what]).replace("{n}", str(n)), value)

    def values(self, what):
        v = self.cfg["osc"].get("values") or {}
        return v.get(what, DEFAULTS["osc"]["values"][what])

    def as_number(self, x):
        """Whole numbers when the profile asks for them (e.g. 0..127)."""
        return int(round(x)) if self.values("integer") else float(round(x, 4))

    def fader_value(self, level, rng=None):
        """0..100 -> the profile's fader range."""
        lo, hi = rng or self.values("fader")
        return self.as_number(lo + (hi - lo) * level / 100.0)

    def fader_level(self, v, rng=None):
        """Fader feedback in the profile's range -> 0..100."""
        lo, hi = rng or self.values("fader")
        return max(0.0, min(100.0, (float(v) - lo) * 100.0 / ((hi - lo) or 1)))

    def onoff(self, what, down, act=None):
        """Pressed / released value: the item's own "on"/"off", else the profile's."""
        on, off = self.values(what)
        if act:
            on, off = act.get("on", on), act.get("off", off)
        return on if down else off

    @staticmethod
    def osc_reader(rx):
        while True:
            try:
                data, _ = rx.recvfrom(65536)
            except OSError:
                if rx.fileno() < 0:
                    return  # closed: the feedback port changed
                continue  # Windows reports ICMP errors here; ignore
            for msg in osc_parse(data):
                events.put(("osc", None, msg))

    def build_maps(self):
        self.build_feedback()
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
        if any(e and e.get("div", 4) <= 2 for e in hw["encoders"]):
            for e in hw["encoders"]:  # one controller's encoders are the same model:
                if e and e.get("div", 4) != 2:  # a click size found on one fits the other
                    e["div"] = 2  # (also repairs a 1 from an earlier, over-eager detection)
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

    # ---- following MA's encoders: hardware encoder i = MA encoder 2*page + i
    def ma_encoders_on(self):
        return self.ma_linked() and self.ma["encn"] > 0 and any(
            e and e.get("follow") for e in self.cfg["encoders"])

    def enc_pages(self):
        return max(1, -(-self.ma["encn"] // 2))

    def ma_encoder(self, i):
        """MA encoder that hardware encoder i follows now, or None."""
        encs = self.cfg["encoders"]
        if not (self.ma_encoders_on() and i < len(encs) and encs[i] and encs[i].get("follow")):
            return None
        k = self.enc_page * 2 + i + 1
        return self.ma["encs"].get(k) if k <= self.ma["encn"] else None

    def following(self, i):
        """True when hardware encoder i follows MA (even onto an empty slot)."""
        encs = self.cfg["encoders"]
        return self.ma_encoders_on() and i < len(encs) and bool(encs[i] and encs[i].get("follow"))

    def next_enc_page(self):
        self.enc_page = (self.enc_page + 1) % self.enc_pages()
        if self.pi_ready:
            self.screen.update_encoders()

    # ---- what clicking an encoder does (profile: per encoder)
    #   "click": "pad" (value page), "push" (its push action / OSC) or "none"
    #   "pad": show the number pad; "choices": "ma" (MA's named values of the
    #   attribute) or a list [{"label": "Open", "value": 0}, ...]; "set": where a
    #   generic profile sends the value (default the "set" address, {n})
    def enc_cfg(self, i):
        encs = self.cfg["encoders"]
        return encs[i] if i < len(encs) and isinstance(encs[i], dict) else {}

    def enc_click(self, i):
        e = self.enc_cfg(i)
        if "click" in e:
            return e["click"]
        if self.generic():
            return "pad" if e.get("set") or isinstance(e.get("choices"), list) else "push"
        return "pad" if (self.following(i) or "attribute" in e) else "push"

    def enc_pad(self, i):
        e = self.enc_cfg(i)
        return bool(e.get("pad", True if not self.generic() else bool(e.get("set"))))

    def choices(self, i):
        """Names offered on encoder i's value page."""
        src = self.enc_cfg(i).get("choices", None if self.generic() else "ma")
        if isinstance(src, list):
            return [str(c.get("label", c.get("value", "?"))) if isinstance(c, dict) else str(c) for c in src]
        if src == "ma" and not self.generic():
            a = self.encoder_attr(i)
            return self.ma["sets"].get(a[0].lower(), []) if a else []
        return []

    def apply_choice(self, i, k):
        """The k-th (from 1) choice on encoder i's value page."""
        src = self.enc_cfg(i).get("choices", None if self.generic() else "ma")
        if isinstance(src, list):
            if 0 < k <= len(src):
                c = src[k - 1]
                if isinstance(c, dict) and "osc" in c:
                    self.osc_raw(c["osc"], c.get("value", 1))
                else:
                    self.send_set(i, c.get("value", c.get("label")) if isinstance(c, dict) else c)
            return
        a = self.encoder_attr(i)
        if a:
            self.ma_plugin(f"setv {a[0]} {k}")

    def apply_number(self, i, text):
        """A number typed on encoder i's value page."""
        if not self.generic():
            a = self.encoder_attr(i)
            if a:
                self.set_value(a[0], text)
            return
        try:
            v = float(text)
        except ValueError:
            return
        self.send_set(i, int(v) if v.is_integer() and self.values("integer") else v)

    def send_set(self, i, value):
        own = self.enc_cfg(i).get("set")
        if own:
            self.osc_raw(own, value)
        else:
            self.generic_send("set", i + 1, value)

    def encoder_attr(self, i):
        """-> (attribute, display name, MA's value text) encoder i controls, or None."""
        if self.generic():  # a value page of its own: label + feedback value
            if self.enc_click(i) != "pad":
                return None
            label, value = self.screen.simple_encoder(i)
            return (f"enc{i + 1}", label, "" if value == "-" else value)
        if self.following(i):
            e = self.ma_encoder(i)
            return (e["attr"], e["pretty"], e["value"]) if e else None
        encs = self.cfg["encoders"]
        act = encs[i] if i < len(encs) and encs[i] else {}
        return (act["attribute"], act["attribute"], "") if "attribute" in act else None

    def encoder_push(self, i, down):
        """Click: the value page for the encoder's attribute, its push action, or
        nothing - as the profile says (enc_click)."""
        click = self.enc_click(i)
        if click == "none":
            return
        if click == "pad" and self.pi_ready and self.screen.name.startswith("entry"):
            if down:
                self.screen.encoder_click()
            return
        if click == "pad" and self.encoder_attr(i):
            if down and self.pi_ready:
                self.screen.set_screen(f"entry{i}")
            return
        if self.generic():
            self.generic_send("push", i + 1, self.onoff("push", down))
            return
        if click == "pad" and self.following(i):
            return  # following MA onto an empty slot: nothing to set
        act = self.cfg["encoders"][i] if i < len(self.cfg["encoders"]) else {}
        if act and act.get("push"):
            self.do_action(act["push"], down)

    def set_value(self, attr, text):
        self.ma_cmd(f'Attribute "{attr}" At {text}')

    def ma_fader(self, i):
        """The target's real level for fader i (MA, or generic feedback), if known."""
        if i in self.fb["fader"]:
            return self.fb["fader"][i]
        if self.generic():
            return None
        ex = self.cfg["faders"][i].get("exec")
        if ex is None:
            return None
        if self.ma_linked():
            return self.ma["fader"].get(ex)
        return self.ma_values.get((self.page, ex))

    # ---- output
    def osc(self, addr, *args):
        self.last_sent = " ".join([self.prefix + addr] + [str(a) for a in args])
        try:
            self.sock.sendto(osc_message(self.prefix + addr, *args), self.dest)
        except OSError as e:
            print(f"OSC send failed: {e}")

    def ma_cmd(self, cmd):
        if self.generic():  # command line / cmd actions: to the profile's command address
            self.generic_send("command", 0, cmd)
        else:
            self.osc("/cmd", cmd)
        self.last_cmd = cmd
        print(f"cmd: {cmd}")
        if self.pi_ready:
            self.screen.update_cmdline()

    # The littlelx code lives in MA as hex pieces in global variables
    # llx_1..llx_N (each prefixed with 'x' so MA never mistakes one for a
    # number; MA drops empty strings and may limit long ones). This Lua
    # rebuilds it and calls it with an argument.
    MA_RUN = ("local g=GlobalVars() local n=GetVar(g,'llx_n') "
              "if type(n)=='string' then n=tonumber(n) end "  # MA's tonumber only takes strings
              "if n then local t={} for i=1,n do local h=tostring(GetVar(g,'llx_'..i)) "
              "for k=2,#h-1,2 do local a,b=h:byte(k,k+1) "
              "t[#t+1]=string.char(((a>96 and a-87 or a-48)<<4)|(b>96 and b-87 or b-48)) end end "
              "load(table.concat(t))()(nil,'{arg}') end")

    def ma_plugin(self, arg):
        """Run an action in the littlelx code installed in MA (see ma3/littlelx.lua)."""
        arg = re.sub(r"[^A-Za-z0-9 .+\-_<>/]", "", arg)
        self.osc("/cmd", 'Lua "' + self.MA_RUN.replace("{arg}", arg) + '"')
        if self.verbose:
            print(f"MA: {arg}")

    def ma_key(self, k):
        """A console key. The command line is kept here and sent to MA whole on
        Please: typing keystrokes into MA (as before) hit its keyboard shortcuts
        and whatever had focus on the onPC. MA's own command line shows it too
        when its version allows (the plugin sets the text, no keystrokes)."""
        self.screen.local_key(k)
        if self.ma_linked():
            self.ma_plugin("show " + self.screen.cmdline.strip())

    def ma_code_version(self):
        """Short fingerprint of the MA code this bridge would install, and of the
        executors it reports (a new MIDI mapping restarts it with the new ones)."""
        if not hasattr(self, "_ma_ver"):
            try:
                self._ma_ver = f"{zlib.crc32(open(resource('ma3', 'littlelx.lua'), 'rb').read()):08x}"
            except OSError:
                self._ma_ver = ""
        return f"{self._ma_ver}-{zlib.crc32(self.watch_spec().encode()) & 0xffff:04x}"

    def watch_spec(self):
        """The executors MA reports (names, colours, running, fader levels):
        101-115 and 201-215, and whatever the profile and MIDI devices use."""
        nums = set(range(101, 116)) | set(range(201, 216))
        for item in list(self.cfg.get("faders") or []) + list(self.cfg.get("keys") or []) \
                + list(self.cfg.get("touch_buttons") or []):
            if isinstance(item, dict) and isinstance(item.get("exec"), int):
                nums.add(item["exec"])
        nums |= self.midi.executors()
        return lxmidi.ranges(n for n in nums if 0 < n < 10000)

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
        h = src.hex()
        pieces = [h[off:off + 240] for off in range(0, len(h), 240)]
        attrs = ",".join(sorted({e["attribute"] for e in self.cfg["encoders"] if e and e.get("attribute")}
                                | {e["push"]["resolution"] for e in self.cfg["encoders"]
                                   if e and isinstance(e.get("push"), dict) and "resolution" in e["push"]})) or "Dimmer"
        attrs = re.sub(r"[^A-Za-z0-9,_]", "", attrs)
        start = 'Lua "' + self.MA_RUN.replace(
            "{arg}", f"__start {line} {tick} {attrs} {self.ma_code_version()} {self.watch_spec()}") + '"'

        def send_all():  # spaced out for MA, on its own thread so faders never wait
            for i, piece in enumerate(pieces, 1):
                self.osc("/cmd", f"Lua \"SetVar(GlobalVars(),'llx_{i}','x{piece}')\"")
                time.sleep(0.01)
            self.osc("/cmd", f"Lua \"SetVar(GlobalVars(),'llx_n',{len(pieces)})\"")
            time.sleep(0.05)
            self.osc("/cmd", start)
        start_thread(send_all)

    def to_pi(self, line):
        if not self.ser:
            return
        self.ser.post(">" + line)  # checksummed and paced on the port's writer thread

    def send_fader(self, i, value):
        fmt = self.cfg["osc"].get("fader_type", "i")
        # One message per fader_interval, always the newest position: MA can't
        # take one per percent of a long move (it queues them and trails behind).
        # A fast throw would then be only a few steps, so a move of fader_jump %
        # since the last message goes out at once (but never closer than 4 ms).
        now = time.time()
        gap = now - self.fader_cmd_at[i]
        jump = float(self.cfg["osc"].get("fader_jump", 5))
        big = (not fmt.startswith("cmd") and self.fader_sent[i] is not None
               and abs(value - self.fader_sent[i]) >= jump)
        if gap < self.fader_interval(fmt) and not (big and gap >= 0.004):
            self.fader_cmd_pending[i] = value
            return
        self.fader_cmd_at[i] = now
        self.fader_cmd_pending[i] = None
        self.fader_sent[i] = value
        now = time.time()
        hist = self.sent_hist[i]
        hist.append((now, value))
        while hist and now - hist[0][0] > self.ECHO_WINDOW:
            hist.pop(0)
        f = self.cfg["faders"][i] if i < len(self.cfg["faders"]) else {}
        if f.get("osc") or self.generic():
            arg = self.fader_value(value, f.get("range") if f.get("osc") else None)
            if arg == self.fader_arg[i]:
                return  # a whole-number range: this move rounds to what was sent
            self.fader_arg[i] = arg
        if f.get("osc"):  # a profile's own address for this fader (its own "range" if given)
            addr = f["osc"]
            self.osc_raw(addr, arg)
        elif self.generic():
            addr = "fader"
            self.generic_send("fader", i + 1, arg)
        else:
            addr, arg = fader_message(fmt, self.page, self.cfg["faders"][i].get("exec", 201 + i), value)
            self.osc(addr, arg)
        if self.verbose:
            print(f"{time.strftime('%H:%M:%S')}.{int(now * 1000) % 1000:03d}  fader {i + 1}: {addr} {arg}")

    def fader_interval(self, fmt):
        iv = float(self.cfg["osc"].get("fader_interval", 0.025))
        return max(iv, 0.05) if fmt.startswith("cmd") else iv  # the command line is slower

    def flush_faders(self):
        fmt = self.cfg["osc"].get("fader_type", "i")
        for i, v in enumerate(self.fader_cmd_pending):
            if v is not None and time.time() - self.fader_cmd_at[i] >= self.fader_interval(fmt):
                self.send_fader(i, v)

    def key_event(self, i, down):
        if down and self.pi_ready and self.screen.in_setup():  # Setup: pressing a key picks it
            self.screen.set_screen(f"key{i}")
            return
        if not down and i not in self.keys_down:
            return  # its press went to Setup: MA never saw it
        act = self.cfg["keys"][i] if i < len(self.cfg["keys"]) else None
        local = ("osc", "page", "goto_page", "screen", "encpage")  # its own address, or the controller's own pages/screens
        if self.generic() and not (act and any(k in act for k in local)):
            self.keys_down.add(i) if down else self.keys_down.discard(i)
            self.generic_send("key", i + 1, self.onoff("key", down))
            return
        if down:
            self.keys_down.add(i)
        else:
            self.keys_down.discard(i)
        act = self.cfg["keys"][i] if i < len(self.cfg["keys"]) else None
        if act:
            self.do_action(act, down)

    def osc_raw(self, addr, *args):
        """An OSC message exactly as given (no prefix): profile "osc" actions."""
        self.last_sent = " ".join([addr] + [str(a) for a in args])
        if self.pi_ready and self.screen_opt("middle") == "sent":
            self.screen.update_cmdline()
        try:
            self.sock.sendto(osc_message(addr, *args), self.dest)
        except OSError as e:
            print(f"OSC send failed: {e}")

    def do_action(self, act, down):
        if "cmdline" in act:  # keypad specials: {"cmdline": "please" | "clear" | "backspace"}
            if down and act["cmdline"] in Screen.CMDLINE_KEYS:
                self.ma_key(Screen.CMDLINE_KEYS[act["cmdline"]])
            return
        if "osc" in act:  # {"osc": "/addr"}: on / off values on press / release (any connection)
            self.osc_raw(act["osc"], self.onoff("key", down, act))
            return
        if "exec" in act:
            self.osc(f"/Page{self.page}/Key{act['exec']}", 1 if down else 0)
        elif not down:
            return
        elif "key" in act:
            self.ma_key(act["key"])
        elif "resolution" in act:
            if self.ma_linked():
                self.ma_plugin(f"res {act['resolution']}")
            else:
                print("Coarse/fine needs the MA3 link (second OSC line).")
        elif "cmd" in act:
            self.ma_cmd(act["cmd"])
        elif "page" in act:
            self.set_page(self.page + int(act["page"]))
        elif "goto_page" in act:
            self.set_page(int(act["goto_page"]))
        elif "screen" in act:
            self.screen.set_screen(act["screen"] if self.screen.name != act["screen"] else "main")
        elif "encpage" in act:
            self.next_enc_page()

    def send_item_fader(self, item, value):
        """A fader that isn't one of the controller's own (MIDI, later modules),
        set up like them: {"exec": 201} | {"osc": "/addr", "range": [0, 1]} |
        {"cmd": "Master 2.1 At {v}"}; value 0..100."""
        if item.get("osc"):
            self.osc_raw(item["osc"], self.fader_value(value, item.get("range")))
        elif "cmd" in item:
            self.ma_cmd_quiet(str(item["cmd"]).replace("{v}", str(round(value))))
        elif "exec" in item and not self.generic():
            addr, arg = fader_message(self.cfg["osc"].get("fader_type", "i"), self.page, item["exec"], value)
            self.osc(addr, arg)

    def ma_cmd_quiet(self, cmd):
        """A command from a moving fader: not echoed to the log and screen."""
        if self.generic():
            self.generic_send("command", 0, cmd)
        else:
            self.osc("/cmd", cmd)

    def item_state(self, act):
        """For lights (MIDI devices): -> ("off" | "idle" | "on", sequence colour or None).
        idle = something is assigned; on = it runs / is active."""
        if not act:
            return "off", None
        ma = self.ma
        if act.get("lit") in self.fb["lit"]:
            return ("on" if self.fb["lit"][act["lit"]] else "idle"), act.get("color")
        if "exec" in act and not self.generic():
            ex = act["exec"]
            if not self.ma_linked():
                return "idle", None
            color = ma["color"].get(ex) or None
            if not ma["name"].get(ex) and not ma["run"].get(ex):
                return "off", None  # no sequence there
            return ("on" if ma["run"].get(ex) else "idle"), color
        if "state" in act:
            return ("on" if ma["master"].get(str(act["state"]).lower()) else "idle"), act.get("color")
        if "goto_page" in act:
            return ("on" if self.page == int(act["goto_page"]) else "idle"), act.get("color")
        return "idle", act.get("color")

    def set_page(self, page, from_ma=False):
        page = max(1, page)
        if not from_ma:
            if self.generic():
                self.generic_send("page", page, page)
            else:
                self.osc("/cmd", f"Page {page}")  # MA follows; the plugin confirms
        if page == self.page:
            return
        self.page = page
        self.cfg["page"] = page
        self.ma["fader"], self.ma["run"], self.ma["name"], self.ma["color"] = {}, {}, {}, {}
        for i in range(5):  # new page: catch each fader again before it takes over
            self.picked[i] = not self.cfg.get("pickup", True)
        self.midi.page_changed()
        if self.pi_ready:
            self.screen.update_header()
            self.screen.update_buttons()
            for i in range(5):
                self.screen.update_fader(i)

    # ---- input from the Mega
    def on_fader(self, i, value):
        prev = self.fader_pos[i]
        self.fader_pos[i] = value
        if prev is not None:
            self.moved_at[i] = time.time()
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
        sent = self.fader_sent[i]
        if self.cfg["osc"].get("fader_type", "i") in ("f", "f1"):
            changed = sent is None or abs(value - sent) >= 0.25  # decimal formats: fine steps
        else:
            changed = sent is None or round(value) != round(sent)
        if self.picked[i] and changed:
            self.send_fader(i, value)
        if self.pi_ready:
            self.screen.update_fader(i)

    def on_mega(self, line):
        if line.startswith(">"):
            self.pi_heard = time.time()
            self.on_pi(line[1:])
            return
        k = re.match(r"K(\d+) (\d+) ([01])$", line)
        if k:  # matrix key
            what = self.matrix.get((int(k.group(1)), int(k.group(2))))
            down = k.group(3) == "1"
            if what and what[0] == "key":
                self.key_event(what[1], down)
            elif what:
                self.encoder_push(what[1], down)
            return
        m = re.match(r"([DA])(\d+) (\d+)$", line)
        if not m:
            if line.startswith("PI "):
                print("Touchscreen on Serial" + line[3:] if line[3:] != "0" else "Touchscreen not found yet")
            return
        kind, n, v = m.group(1), int(m.group(2)), int(m.group(3))
        if kind == "A":
            self.raw_analog[n] = v
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
            self.key_event(what[1], v == 0)
        else:
            i, part = what[1], what[2]
            e = self.cfg["hw"]["encoders"][i]
            act = self.cfg["encoders"][i] if i < len(self.cfg["encoders"]) else {}
            if part == "push":
                self.encoder_push(i, v == 0)
                return
            self.enc_edge_at[i] = time.time()
            d = self.encs[i].update(self.pins.get(e["a"], 1), self.pins.get(e["b"], 1))
            if d and e.get("reverse"):
                d = -d
            if d and self.generic():
                self.generic_send("encoder", i + 1, self.as_number(d * self.values("encoder")))
            elif d and self.pi_ready and self.screen.scroll_sets(d):
                pass  # scrolled the named values on the screen
            elif d and self.following(i):
                e = self.ma_encoder(i)
                if e:
                    self.on_encoder({"attribute": e["attr"], "step": act.get("step", 1), "res": e["res"]}, d)
            elif d:
                self.on_encoder(act, d)

    def on_encoder(self, act, d):
        if "attribute" in act:
            attr = act["attribute"]
            factor = act["res"] if "res" in act else self.ma["res"].get(attr, 1.0) if self.ma_linked() else 1.0
            step = d * float(act.get("step", 1)) * factor
            self.ma_cmd(f'Attribute "{attr}" At {"+" if step > 0 else "-"} {abs(step):g}')
        elif "page" in act:
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
            self.pi_version = ver
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
            if errs.get("bad", 0) > getattr(self, "last_bad", 0) and self.ser:
                # damaged lines are increasing: slow the link down (the screen
                # is re-sent in the background, so it heals by itself)
                self.ser.pacer.rate = max(4000, self.ser.pacer.rate * 0.7)
                print(f"  slowing the link to the touchscreen to {self.ser.pacer.rate / 1000:.1f} KB/s")
            self.last_bad = errs.get("bad", 0)
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
            cal = [float(x) for x in parts[1:7]]
            self.to_pi("K " + " ".join(str(x) for x in cal))  # ack: stops the resends
            if cal != self.cfg.get("touch_cal"):
                self.cfg["touch_cal"] = cal
                save_config(self.cfg)
                print("Touch calibration saved")
                save_to_mega(self.cfg, self.ser, keep=True)
                self.screen.draw()

    # ---- MA feedback
    def on_osc(self, addr, args):
        self.ma_seen = time.time()
        if "/littlelx/" in addr:
            self.on_littlelx(addr.split("/littlelx/", 1)[1], args)
            return
        if self.on_feedback(addr, args):
            return
        m = re.search(r"/Page(\d+)/Fader(\d+)$", addr)  # plain MA OSC feedback
        if not m:
            return
        page, ex = int(m.group(1)), int(m.group(2))
        nums = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
        if nums:
            self.ma_values[(page, ex)] = float(nums[-1])
        if page == self.page:
            for i, f in enumerate(self.cfg["faders"]):
                if f.get("exec") == ex:
                    self.check_pickup(i)
                    if self.pi_ready:
                        self.screen.update_fader(i)

    def fader_feedback(self, i, v):
        """The target reports fader i at v (0..100): MA, or generic feedback."""
        # Moved there by someone else? Then the physical fader must catch it
        # again. Not if it's just the target reporting one of our own recent
        # moves late, or the fader is in our hand now.
        now = time.time()
        ours = any(abs(v - sv) <= 1.5 for t, sv in self.sent_hist[i] if now - t <= self.ECHO_WINDOW)
        hands_on = now - self.moved_at[i] < self.HANDS_ON
        if self.picked[i] and not ours and not hands_on and self.fader_pos[i] is not None \
                and abs(v - self.fader_pos[i]) > 3:
            self.picked[i] = not self.cfg.get("pickup", True)
        self.check_pickup(i)
        if self.pi_ready:
            self.screen.update_fader(i)

    def check_pickup(self, i):
        """A fader already sitting at MA's level (e.g. 0 % on both after a page
        change) has nothing to catch: it is in control straight away."""
        ma, pos = self.ma_fader(i), self.fader_pos[i]
        if not self.picked[i] and ma is not None and pos is not None and abs(pos - ma) <= 2:
            self.picked[i] = True

    def on_littlelx(self, what, args):
        """State reported by the code running inside MA3."""
        ma = self.ma
        val = args[0] if args else None
        was_linked = self.ma_linked()
        ma["alive"] = time.time()
        if not was_linked:
            print("MA3 linked: page, faders, key states and command line follow MA.")
            ma["ver"], ma["linked_at"] = None, time.time()
            # its code may have been running all along and only reports changes:
            # ask for everything (names, colours...) once
            self.ma_plugin("resync")
        scr = self.screen if self.pi_ready else None
        if what.startswith(("run/", "name/", "color/", "master/", "fader/")):
            self.midi.dirty = True  # MIDI lights follow
        if what == "page" and isinstance(val, int):
            self.set_page(val, from_ma=True)
        elif what.startswith(("fader/", "run/", "name/", "color/")):
            kind, ex = what.split("/", 1)
            ex = int(ex)
            if kind == "fader":
                v = float(val)
                ma["fader"][ex] = v
                for i, f in enumerate(self.cfg["faders"]):
                    if f.get("exec") == ex:
                        self.fader_feedback(i, v)
            elif kind == "run":
                ma["run"][ex] = int(val or 0)
                if scr:
                    scr.update_buttons()
            else:
                ma[kind][ex] = str(val or "")
                for i, f in enumerate(self.cfg["faders"]):
                    if f.get("exec") == ex and scr:
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
        elif what == "feat":
            p = (str(val or "") + "||").split("|")
            if p[0] != ma["feat"][0]:
                self.enc_page = 0  # another feature (tab): start at its first pair
            ma["feat"] = (p[0], p[1], p[2] == "1")
            if scr:
                scr.update_encoders()
        elif what == "groups":  # the encoder banks the selected fixture has
            ma["groups"] = [g for g in str(val or "").split("|") if g]
            if scr and scr.name == "fgroups":
                scr.draw()
        elif what == "tabs":
            ma["tabs"] = [t for t in str(val or "").split("|") if t]
            if scr:
                scr.tab_paged = False
                scr.update_encoders()
        elif what == "encn":
            ma["encn"] = int(val or 0)
            if self.enc_page >= self.enc_pages():
                self.enc_page = 0
            if scr:
                scr.update_encoders()
        elif what.startswith("enc/"):
            p = (str(val or "") + "||||").split("|")
            try:
                res = float(p[4] or 1)
            except ValueError:
                res = 1.0
            ma["encs"][int(what[4:])] = dict(attr=p[0], pretty=p[1] or p[0], value=p[2], prog=p[3] == "p", res=res)
            if scr:
                scr.update_encoders()
        elif what == "sets":
            p = str(val or "").split("|")
            ma["sets"][p[0].lower()] = [n for n in p[1:] if n]
            if scr and scr.name.startswith("entry"):
                a = self.encoder_attr(int(scr.name[5:]))
                if a and a[0].lower() == p[0].lower():
                    scr.draw_value_entry(int(scr.name[5:]), keep=True)
        elif what == "ver":
            ma["ver"] = str(val or "")
        elif what == "probe":
            self.probe_lines.append(str(val or ""))
        elif what == "probe_end":
            path = save_probe(self.probe_lines)
            self.probe_lines = []
            print(f"MA probe saved to {path} - open it there and copy everything.")
            if scr and scr.name == "setup":
                scr.setw(scr.SET_TITLE, text=f"MA probe saved on the computer:\n{path}")
        elif what.startswith("res/"):
            ma["res"][what.split("/", 1)[1]] = float(val or 1)
            if scr:
                scr.update_encoders()
        elif what == "started":
            print(f"MA3 code running (update every {val} s).")
        if scr and not was_linked:
            scr.update_header()

    def check_encoder_clicks(self, now):
        """Learn whether one click is 4 or 2 signal changes: a 4-change encoder
        always settles on the same state, a 2-change one alternates between two.
        Only settled positions after a real turn count (not wiggles, not a knob
        held between clicks), and it takes repeated evidence to change."""
        for i, e in enumerate(self.cfg["hw"]["encoders"]):
            enc = self.encs[i] if i < len(self.encs) else None
            if not e or not enc or self.enc_edge_at[i] <= self.enc_checked[i] or now - self.enc_edge_at[i] < 0.8:
                continue
            self.enc_checked[i] = now
            turned, enc.net = abs(enc.net) >= 2, 0
            if not turned or e.get("div", 4) <= 2:
                continue
            seen = self.enc_rest[i]
            seen[enc.state] = seen.get(enc.state, 0) + 1
            if seen.get(0, 0) >= 2 and seen.get(3, 0) >= 2:
                for k, other in enumerate(self.cfg["hw"]["encoders"]):  # same model: all of them
                    if other and k < len(self.encs) and self.encs[k]:
                        other["div"] = self.encs[k].div = 2
                        self.encs[k].acc = 0
                print("Encoders: 2 signal changes per click detected, adjusted.")
                save_config(self.cfg)
                if self.ser:
                    save_to_mega(self.cfg, self.ser, keep=True)

    # ---- main loop
    def call(self, fn):
        """Run fn on the bridge's thread (from the app's): safe with the port."""
        events.put(("call", None, fn))

    def devices(self):
        """Everything plugged in, for the app: the controller, its touchscreen,
        (later) its modules, and MIDI devices. Safe from another thread."""
        ser = self.ser
        rows = [
            {"kind": "controller", "part": "main", "name": "Controller (main module)",
             "connected": bool(ser), "port": getattr(ser, "path", None) if ser else None,
             "firmware": self.mega_version if ser else None},
            {"kind": "touchscreen", "part": "touchscreen", "name": "Touchscreen",
             "connected": bool(ser and self.pi_ready), "port": None,
             "firmware": self.pi_version if ser and self.pi_ready else None},
        ]
        return rows + self.midi.status()

    def post_midi(self, dev, msg):
        """From a MIDI device's own thread: into the bridge's loop."""
        events.put(("midi", dev, msg))

    def idle(self, kind, src, data):
        """While no controller is plugged in: MIDI devices and MA still work."""
        if kind == "osc":
            self.on_osc(*data)
        elif kind == "midi":
            self.midi.on_message(src, data)
        self.background(time.time())

    def background(self, now):
        """Housekeeping with or without the controller."""
        self.midi.tick(now)
        if (self.cfg.get("ma3", {}).get("auto_install", True) and not self.generic()
                and now - self.ma_installed_at > 30):
            if not self.ma_linked() and now - self.started > 4:
                self.install_ma3()
            elif (self.ma_linked() and self.ma["ver"] != self.ma_code_version()
                  and now - self.ma["linked_at"] > 5):  # older code (or another executor list) in MA
                print("Updating the littlelx code in MA3...")
                self.install_ma3()

    def run(self):
        last_ping = last_status = last_refresh = 0
        while not self.quit.is_set():
            if self.hold.is_set():  # the app has the port (touchscreen update)
                self.held.set()
                time.sleep(0.2)
                continue
            self.held.clear()
            self.ser = connect(self.cfg, stop=lambda: self.hold.is_set() or self.quit.is_set(),
                               idle=self.idle)
            if self.ser is None:
                continue
            self.last_port = self.ser.path
            self.mega_version = getattr(self.ser, "mega_version", None)
            self.pi_ready = False
            self.pi_heard = 0.0
            try:
                if sync_setup(self.cfg, self.ser):
                    self.build_maps()
            except OSError:
                continue
            if not learned(self.cfg):
                print("No wiring learnt yet: run with --learn.")
            if matrix_modes(self.cfg):  # setups learned before fast matrix scanning
                print("Switching the key matrix to fast scanning...")
                save_config(self.cfg)
                save_to_mega(self.cfg, self.ser, keep=True)
            for pin, mode in self.cfg["hw"]["pin_modes"].items():
                self.ser.send(f"M{pin} {mode}")
            self.ser.send("?")
            self.to_pi("?")
            while True:
                if self.hold.is_set() or self.quit.is_set():
                    self.ser.close()
                    self.ser = None
                    self.pi_ready = False
                    break
                try:
                    kind, src, data = events.get(timeout=0.01)  # short: held fader values go out on time
                except queue.Empty:
                    kind = None
                if kind == "call":
                    data()
                elif kind == "midi":
                    self.midi.on_message(src, data)
                elif kind == "mega" and src is self.ser:
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
                self.check_encoder_clicks(now)
                self.background(now)
                if self.ma["busy"] and now - self.ma["busy_at"] > 3:
                    self.ma["busy"] = ""
                    if self.pi_ready:
                        self.screen.update_cmdline()
                if self.pi_ready and now - self.pi_heard > 25:  # it reports every 10 s
                    print("Touchscreen went quiet, looking for it again...")
                    self.pi_ready = False
                if now - last_ping > 1:
                    last_ping = now
                    # Until the screen has answered, ask who's there: a plain PING
                    # tells it a computer is listening, so it stops announcing
                    # itself and (plugged in after the bridge started) would stay
                    # black forever.
                    self.to_pi("PING" if self.pi_ready else "?")
                if self.pi_ready and now - last_status > 1:
                    last_status = now
                    self.screen.update_header()
                if self.pi_ready:
                    self.screen.flush_faders()
                if self.pi_ready and now - last_refresh > 0.1 and self.ser and self.ser.backlog < 4:
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
    matrix_modes(cfg)
    for pin, mode in hw["pin_modes"].items():
        ser.send(f"M{pin} {mode}")
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
    find_screen(ser)

    def send(line):  # screen colours, swapped for BGR panels like the bridge's
        ser.send(">" + bgr_line(line[1:]) if cfg.get("screen_bgr", True) else line)

    def screen(title, button):
        send(">CLR")
        send(">BG 101418")
        send(f">W 0 L 0 0 320 70 1c2430 ffffff 1c2430 1 0 0 {esc(title)}")
        for n, (i, f) in enumerate(faders):
            send(f">W {10 + n} V {6 + n * 63} 80 57 300 232b38 ffffff 2f7de1 0 0 0 F{i + 1}")
        if button:
            send(f">W {NEXT} B 60 396 200 70 c08a1e ffffff c08a1e 1 0 0 {button}")

    def step(title):
        print(title.replace("\\n", " ") + ", then press Enter here or tap Next on the screen.")
        screen(title, "Next")
        ser.send("?")  # Mega replies with every current value
        ping = time.time()
        while True:
            if time.time() - ping > 1:  # the screen drops lines once it thinks we've gone
                ser.send(">PING")
                ping = time.time()
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


def find_screen(ser, required=False):
    """Say hello to the touchscreen. Its HELLO also tells the serial layer to
    use checksums, without which the screen ignores what we draw."""
    print("Looking for the touchscreen...")
    for _ in range(8):
        ser.send(">?")
        if wait_line(ser, lambda l: l.startswith(">HELLO"), 2):
            return True
    if required:
        sys.exit("The touchscreen isn't answering. Is it showing 'waiting for computer'?")
    print("The touchscreen isn't answering; carrying on without it.")
    return False


def calibrate(cfg):
    ser = connect(cfg)
    sync_setup(cfg, ser)
    find_screen(ser, required=True)
    for _ in range(6):
        ser.send(">CAL")
        if wait_line(ser, lambda l: l == ">CALSTART", 1.5):
            break
    else:
        sys.exit("The touchscreen didn't start calibrating (old firmware? run --update-pi).")
    print("Tap the three crosses on the touchscreen...")
    line, end = None, time.time() + 120
    while not line and time.time() < end:
        ser.send(">PING")  # keep the screen listening while you tap
        line = wait_line(ser, lambda l: l.startswith(">CALD "), 1)
    if not line:
        sys.exit("No calibration received.")
    cal = [float(x) for x in line.split()[1:7]]
    cfg["touch_cal"] = cal
    save_config(cfg)
    ser.send(">K " + " ".join(str(x) for x in cal))  # tells the screen it's saved
    save_to_mega(cfg, ser)
    print("Saved.")


PROBE_PATH = os.path.join(os.path.expanduser("~"), "littlelx-probe.txt")


def save_probe(lines):
    """Save MA's probe answer as a plain text file (easy to copy from)."""
    with open(PROBE_PATH, "w") as f:
        f.write(f"littlelx MA probe, {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("\n".join(lines) + "\n")
    return PROBE_PATH


def firmware_package():
    """The firmware package (.lxfw: every part) bundled with this build, if any."""
    path = resource("firmware", "littlelx-firmware.lxfw")
    return path if os.path.exists(path) else None


def mega_firmware():
    """The Mega firmware bundled with this build (from its package), if any."""
    path = resource("firmware", "littlelx_mega.hex")
    if os.path.exists(path):
        return path
    if firmware_package():
        import fwpack
        part = fwpack.Package(firmware_package()).parts.get("main")
        return part and part["path"]
    return None


def installed_versions(cfg):
    """Ask the connected controller and touchscreen for their firmware versions."""
    ser = connect(cfg)
    found = {"main": getattr(ser, "mega_version", None), "touchscreen": None}
    for _ in range(4):
        ser.send(">?")
        line = wait_line(ser, lambda l: l.startswith(">HELLO littlelx-pi"), 1.5)
        if line:
            parts = line.split()
            found["touchscreen"] = parts[5] if len(parts) > 5 else "old"
            break
    found["port"] = ser.path
    ser.close()
    time.sleep(0.5)
    return found


def install_cli(cfg, path, force):
    import fwpack
    path = path or firmware_package()
    if not path:
        sys.exit("No firmware package given, and none is bundled with this bridge "
                 "(download littlelx-firmware from the GitHub build).")
    try:
        pkg = fwpack.Package(path)
    except fwpack.PackageError as e:
        sys.exit(str(e))
    print(f"Firmware package {pkg.version}")
    have = installed_versions(cfg)
    for part, inst, new, action in fwpack.plan(pkg, have, force):
        print(f"  {fwpack.NAMES[part]:<20} {inst or '-':<24} -> {new or '-':<24} {action}")
    shown = {}

    def progress(part, p):
        if shown.get(part) != p:
            shown[part] = p
            print(f"\r  {p:3d}%", end="\n" if p >= 100 else "", flush=True)
    problems = fwpack.install(sys.modules[__name__], cfg, pkg, have, have["port"], force, progress)
    pkg.close()
    if problems:
        sys.exit(1)


def flash_mega(cfg, path=None, port=None, progress=None):
    """Write the Mega firmware (a .hex; default: the bundled one) over USB."""
    import megaflash
    path = path or mega_firmware()
    if not path:
        raise megaflash.FlashError("No firmware file given, and none is bundled with this bridge "
                                   "(download littlelx-mega-hex from the GitHub build).")
    port = port or find_port(cfg)
    if not port:
        raise megaflash.FlashError("The controller isn't plugged in (no Arduino found).")
    megaflash.flash(port, path, progress)


def flash_mega_cli(cfg, path):
    import megaflash
    try:
        shown = [-1]

        def progress(p):
            if p != shown[0]:
                shown[0] = p
                print(f"\r  {p:3d}%", end="\n" if p >= 100 else "", flush=True)
        flash_mega(cfg, path or None, progress=progress)
    except megaflash.FlashError as e:
        sys.exit(f"\nFlashing failed: {e}\nThe old firmware may be half-written: just flash again.")


def show_midi():
    defs = lxmidi.load_definitions(os.path.join(PROFILE_DIR, "midi"))
    print("Known MIDI devices: " + (", ".join(sorted(defs)) or "none"))
    b = lxmidi.Backend()
    if not b.ok:
        print(f"MIDI is off on this computer: {b.error}")
        return
    ins = b.inputs()
    print("MIDI inputs found:" + ("" if ins else " none"))
    for name in ins:
        spec = next((s for s in defs.values() if lxmidi.matches(s, name)), None)
        print(f"  {name}  ->  {spec['name'] if spec else 'not known (no definition)'}")


def show_profiles(cfg):
    print(f"Profiles in {PROFILE_DIR}:")
    for name in list_profiles():
        print(("  * " if name == cfg.get("profile") else "    ") + name)
    print("(* = active. Edit the files, then: --use-profile NAME)")


def use_profile_cli(cfg, name):
    try:
        prof = load_profile(name)
    except OSError:
        sys.exit(f"No profile '{name}' in {PROFILE_DIR} (see --profiles).")
    except ValueError as e:
        sys.exit(f"{profile_path(name)} isn't valid JSON: {e}")
    prof["name"] = prof.get("name") or name
    apply_profile(cfg, prof)
    save_config(cfg)
    print(f"Profile '{cfg['profile']}' is active. Storing it on the controller (stop the bridge first)...")
    ser = connect(cfg)
    save_active(cfg, ser)
    ser.close()
    print("Restart the bridge (if it is running) to use it.")


def ma_probe(cfg):
    """Ask the littlelx code in MA what its Lua offers (encoder diagnostics)."""
    o = cfg["osc"]
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rx.bind(("0.0.0.0", int(o["listen_port"])))
    except OSError as e:
        sys.exit(f"Can't listen on port {o['listen_port']} ({e}): stop the bridge first.")
    rx.settimeout(0.5)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd = 'Lua "' + Bridge.MA_RUN.replace("{arg}", "probe") + '"'
    tx.sendto(osc_message(o["prefix"].rstrip("/") + "/cmd", cmd), (o["host"], int(o["port"])))
    print("Asked MA (needs the bridge to have installed its code at least once)...")
    lines, end = [], time.time() + 8
    while time.time() < end:
        try:
            data, _ = rx.recvfrom(65536)
        except socket.timeout:
            continue
        for addr, args in osc_parse(data):
            if addr.endswith("/littlelx/probe") and args:
                lines.append(str(args[0]))
                end = time.time() + 3  # more coming
            elif addr.endswith("/littlelx/probe_end"):
                end = 0
    if not lines:
        sys.exit("No answer. Is MA's OSC line that sends to this computer set up? (The touchscreen's"
                 " Setup > MA probe works while the bridge runs.)")
    print("\n".join(lines))
    print(f"\nSaved to {save_probe(lines)} - open it there and copy everything.")


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


class UpdateFailed(Exception):
    pass


def update_pi(cfg, path, progress=None, patience=16):
    """Send a littlelx-pi-update.zip to the touchscreen over USB.
    patience: seconds to wait for the touchscreen to answer (longer right
    after the controller restarted: the touchscreen has to find it again)."""
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
    files = {n: z.read(n) for n in names}

    ser = None
    try:  # always let go of the controller: the app's bridge takes it back after

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
                        raise UpdateFailed("touchscreen reported " + line[10:])
                    for p in prefixes:
                        if line.startswith(p):
                            return line
            return None

        def hello(patience=16):
            end = time.time() + patience
            while time.time() < end:
                ser.send(">?")
                h = wait([">HELLO"], 2)
                if h:
                    return h
            return None

        # Right after the controller was flashed, the touchscreen can stay
        # silent on the first connection however long we wait, yet answers at
        # once after the port is closed and opened again (which restarts the
        # controller) - so don't wait long on one connection: reconnect.
        deadline = time.time() + patience
        h = None
        while True:
            ser = connect(cfg)
            print("Looking for the touchscreen...")
            h = hello(min(6, max(2, deadline - time.time())))
            if h or time.time() >= deadline:
                break
            print("  no answer yet: reconnecting to the controller...")
            ser.close()
            ser = None
            time.sleep(1)
        if not h:
            sys.exit("The touchscreen isn't answering. Is it powered and showing 'waiting for computer'?")
        parts = h.split()
        old_ver = parts[5] if len(parts) > 5 else "old"
        if len(parts) <= 5 or parts[2] == "1":
            sys.exit("This touchscreen firmware is too old to update over USB; flash the SD card once instead.")
        proto = int(parts[2]) if parts[2].isdigit() else 2
        print(f"Touchscreen firmware {old_ver} -> {new_ver}")
        rate = PI_RATE * 0.72  # payload rate after base64 and line overhead

        def send(line):
            ser.send(">" + line)  # Port.send checksums (protocol 3+) and paces

        def attempt():
            total = sum(len(d) for d in files.values())
            send(f"UPD BEGIN {total}")
            if not wait([">UPD READY"], 20):
                raise UpdateFailed("no answer to BEGIN")
            CHUNK = 192
            done = 0
            for n, data in files.items():
                send(f"UPD FILE {n} {len(data)} {zlib.crc32(data) & 0xffffffff:08x}")
                r = wait([">UPD HAVE", ">UPD SEND"], 30)
                if not r:
                    raise UpdateFailed(f"no answer for {n}")
                if r.startswith(">UPD SEND"):
                    print(f"\r  sending {n} ({len(data) / 1024:.0f} KB, about {len(data) / rate:.0f} s)" + " " * 10)
                for again in range(4):
                    if not r.startswith(">UPD SEND"):
                        break
                    if again:
                        print(f"\r  {n} arrived damaged, sending it again" + " " * 20)
                    if proto >= 3:
                        # go-back-N: chunks carry their offset (and a CRC-32 on
                        # protocol 4); the Pi acks how much it has and anything
                        # lost or damaged is simply sent again
                        pos = nxt = 0
                        rewound, tries = False, 0
                        while pos < len(data):
                            while nxt < len(data) and nxt - pos < 4 * CHUNK:
                                chunk = data[nxt:nxt + CHUNK]
                                crc = f"{zlib.crc32(chunk) & 0xffffffff:08x} " if proto >= 4 else ""
                                send(f"UPD DAT {nxt} {crc}" + base64.b64encode(chunk).decode())
                                nxt = min(nxt + CHUNK, len(data))
                            r2 = wait([">UPD ACK"], 2)
                            a = int(r2.split()[2]) if r2 else None
                            if a is None or (a == pos and not rewound):
                                tries += 1
                                if tries > 40:
                                    raise UpdateFailed("the link keeps failing")
                                nxt, rewound = pos, True  # resend from what the Pi has
                            elif a > pos:
                                pos, rewound, tries = a, False, 0
                                print(f"\r  {(done + pos) * 100 // total:3d}%  {n:<28}", end="", flush=True)
                                if progress:
                                    progress((done + pos) * 100 // total)
                    else:
                        # older firmware: no repair possible, small window
                        pos, inflight = 0, []
                        while pos < len(data) or inflight:
                            while pos < len(data) and len(inflight) < 2:
                                chunk = data[pos:pos + CHUNK]
                                send("UPD DATA " + base64.b64encode(chunk).decode())
                                inflight.append(len(chunk))
                                pos += len(chunk)
                            if wait([">UPD ACK"], 15) is None:
                                raise UpdateFailed("transfer stalled")
                            inflight.pop(0)
                            print(f"\r  {(done + pos - sum(inflight)) * 100 // total:3d}%  {n:<28}",
                                  end="", flush=True)
                    # the new firmware answers a file that failed its CRC with SEND again
                    r = wait([">UPD OK", ">UPD SEND"], 30)
                    if not r:
                        raise UpdateFailed(f"{n} wasn't confirmed")
                if r.startswith(">UPD SEND"):
                    raise UpdateFailed(f"{n} kept arriving damaged")
                done += len(data)
            print(f"\r  100%  {'all files sent':<28}")
            send(f"UPD COMMIT {len(files)}")
            if not wait([">UPD DONE"], 60):
                raise UpdateFailed("the switch wasn't confirmed")

        for tryno in range(1, 6):
            try:
                attempt()
                break
            except UpdateFailed as e:
                send("UPD ABORT")
                print(f"\n  Attempt {tryno} failed ({e}); nothing was changed.")
                if tryno == 5:
                    sys.exit("Giving up after 5 attempts: the serial link to the Pi is too unreliable. "
                             "Check the wiring, or flash the SD card instead.")
                print("  Trying again...")
                time.sleep(2)
                if not hello():
                    sys.exit("The touchscreen stopped answering.")
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
    finally:
        if ser:
            ser.close()
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--learn", action="store_true", help="learn the wiring")
    ap.add_argument("--calibrate", action="store_true", help="calibrate the touchscreen")
    ap.add_argument("--faders", action="store_true", help="calibrate fader bottom/top")
    ap.add_argument("--monitor", action="store_true", help="print raw events")
    ap.add_argument("--probe", action="store_true", help="key-matrix wiring diagnostics")
    ap.add_argument("--test-faders", action="store_true", help="find the fader format MA3 accepts")
    ap.add_argument("--ma-probe", action="store_true", help="show what MA's Lua offers (encoder diagnostics)")
    ap.add_argument("--profiles", action="store_true", help=f"list the profiles (files in {PROFILE_DIR})")
    ap.add_argument("--midi", action="store_true", help="list the MIDI devices littlelx knows and finds")
    ap.add_argument("--install", metavar="FILE.lxfw", nargs="?", const="",
                    help="install the firmware package: controller, touchscreen (only what's out of date; "
                         "default: the one bundled with this build)")
    ap.add_argument("--reinstall", action="store_true", help="with --install: install every part again")
    ap.add_argument("--flash-mega", metavar="FILE.hex", nargs="?", const="",
                    help="flash the Arduino Mega firmware (default: the one bundled with this build)")
    ap.add_argument("--use-profile", metavar="NAME", help="make a profile active and store it on the controller")
    ap.add_argument("--verbose", action="store_true", help="print every fader message sent")
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--update-pi", metavar="ZIP", help="update the touchscreen firmware")
    args = ap.parse_args()
    print(f"littlelx bridge {bridge_version()}")
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
        elif args.ma_probe:
            ma_probe(cfg)
        elif args.install is not None:
            install_cli(cfg, args.install, args.reinstall)
        elif args.flash_mega is not None:
            flash_mega_cli(cfg, args.flash_mega)
        elif args.profiles:
            show_profiles(cfg)
        elif args.midi:
            show_midi()
        elif args.use_profile:
            use_profile_cli(cfg, args.use_profile)
        else:
            print(f"Sending OSC to {cfg['osc']['host']}:{cfg['osc']['port']} prefix '{cfg['osc']['prefix']}'")
            b = Bridge(cfg)
            b.verbose = args.verbose
            b.run()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
