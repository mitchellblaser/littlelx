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


def bridge_version():
    """git version of this checkout (or 'unknown', e.g. inside the .exe)."""
    import subprocess
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

CONFIG_VERSION = 8

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
    },
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
    # Choices on the touchscreen's Encoders page (MA3 attribute names)
    "encoder_attributes": ["Dimmer", "Pan", "Tilt", "Zoom", "Focus1", "Iris",
                           "Shutter1", "Gobo1", "Color1", "Prism1", "Frost1"],
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
    return cfg


def migrate(cfg):
    """Bring a config saved by an older version up to date (runs once)."""
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
    and what the keys do."""
    return {"hw": cfg["hw"], "touch_cal": cfg.get("touch_cal"), "keys": cfg.get("keys")}


def save_to_mega(cfg, ser, keep=False):
    data = zlib.compress(json.dumps(setup_blob(cfg), separators=(",", ":")).encode(), 9)
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
        mine = setup_blob(cfg)
        merged = dict(mine, **{k: v for k, v in blob.items() if k in mine})  # the controller's wins
        changed = merged != mine
        if changed:
            cfg.update(merged)
            save_config(cfg)
            print("Loaded the learned setup from the controller.")
        if any(k not in blob for k in mine):  # saved by an older bridge: add what it lacks (keys)
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
    HDR_PAGE, HDR_MID, HDR_STATUS, HDR_SETUP = 0, 1, 2, 3
    FADER0 = 10
    BTN0 = 20
    CMDLINE = 39
    KEY0 = 40
    ENC0 = 66      # encoders page: 10 ids per encoder
    ENC_FEAT, ENC_PAGE = 86, 87
    BACK = 120
    SET_TITLE, SET_RESET, SET_MORE = 121, 122, 123
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
            self.widget(self.HDR_PAGE, "L", 0, 0, 100, 30, C_PANEL, C_TEXT, C_PANEL, 1, 1, 0, page)
            self.widget(self.HDR_MID, "L", 0, 30, w, 24, C_PANEL, C_CMD, C_PANEL, 0, 1, 0, self.mid_text())
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
        btns = self.b.cfg["touch_buttons"][:cols * 4]
        rows = max(1, -(-len(btns) // cols))
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

    def back_button(self):
        self.keymap[self.BACK] = {"back": True}
        self.widget(self.BACK, "B", 4, self.h - 58, self.w - 8, 54, C_KEY2, C_TEXT, C_BTN_ON, 1, 0, 0, "Back")

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
        self.enc_mode = b.ma_encoders_on()
        top = self.header_h() + 4
        self.keymap = {}
        self.widget(self.ENC_FEAT, "L", 4, top, w - 100, 40, C_PANEL, C_TEXT, C_PANEL, 1, 1, 0, "")
        self.keymap[self.ENC_PAGE] = {"encpage": 1}
        self.widget(self.ENC_PAGE, "B", w - 92, top, 88, 40, C_KEY2, C_TEXT, C_BTN_ON, 1, 0, 0, "")
        top += 46
        ph = (self.h - 62 - top) // 2
        hw = b.cfg["hw"]["encoders"]
        for i in range(2):
            y, base = top + i * ph, self.ENC0 + i * 10
            self.widget(base, "L", 4, y, w - 8, ph - 6, C_PANEL, C_DIM, C_PANEL, 0, 1, 0, "")
            title = f"Encoder {i + 1}" + ("" if i < len(hw) and hw[i] else "  (not learnt)")
            self.widget(base + 4, "L", 4, y, w - 8, 26, C_PANEL, C_DIM, C_PANEL, 0 if not self.enc_mode else 1,
                        1, 0, title)
            vh = ph - 26 - 62
            self.keymap[base + 1] = {"entry": i}  # tap the value to type one
            self.widget(base + 1, "B", 8, y + 28, w - 16, vh, C_PANEL, C_TEXT, C_PANEL, 2, 0, 0, "")
            by, bh = y + 30 + vh, ph - 30 - vh - 10
            self.widget(base + 2, "B", 8, by, w - 16, bh, C_BTN, C_TEXT, C_BTN_ON, 1, 0, 0, "")
        self.back_button()
        self.update_encoders()

    def update_encoders(self):
        if self.name.startswith("entry"):  # typing a value: keep MA's current one fresh
            self.setw(self.SET_TITLE, text=self.value_title(int(self.name[5:])))
            return
        if self.name != "encoders":
            return
        b = self.b
        if b.ma_encoders_on() != self.enc_mode:  # MA started/stopped giving encoders: new layout
            self.draw_encoders()
            return
        ma = b.ma
        fname, group, has_sel = ma["feat"]
        gcolor = FEATURE_COLORS.get(group.lower(), FEATURE_COLORS.get(fname.lower(), C_KEY2))
        pages = b.enc_pages()
        if self.enc_mode:
            self.setw(self.ENC_FEAT, text=" " + fname, colors=(shade(gcolor, 0.55), C_TEXT, C_PANEL))
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
        a = self.b.encoder_attr(i)
        sets = self.b.ma["sets"].get(a[0].lower(), []) if a else []
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
                self.setw(wid, text=sets[k][:14], colors=(C_KEY2, C_TEXT, C_BTN_ON))
            else:
                self.keymap.pop(wid, None)
                self.setw(wid, text="", colors=(C_BG, C_DIM, C_BG))
        if self.SET_MORE in self.state:
            self.setw(self.SET_MORE, text=f"turn the encoder for more   {first + 1}-{min(first + rows * 3, len(sets))}"
                                           f" of {len(sets)}")

    def scroll_sets(self, d):
        """Encoder turned while typing a value: scroll the named values a row."""
        if not self.name.startswith("entry") or self.SET0 + 20 not in self.state:
            return False
        self.sets_page += d
        self.show_sets(int(self.name[5:]))
        return True

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
            if a and b.ma_linked():
                b.ma["sets"].pop(a[0].lower(), None)
                b.ma_plugin(f"sets {a[0]}")  # the answer redraws this page
        self.keymap = {}
        self.widget(self.SET_TITLE, "L", 4, top, w - 8, 56, "000000", C_CMD, "000000", 1, 0, 0, self.value_title(i))
        y = top + 60
        sets = b.ma["sets"].get(a[0].lower(), []) if a else []
        if sets:  # named values above the number pad
            sh = 40
            rows = min(3, -(-len(sets) // 3))
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

    def in_setup(self):
        return self.name == "setup" or self.name.startswith(("key", "exec"))

    @classmethod
    def describe(cls, act):
        if not act:
            return "-"
        if "exec" in act:
            return f"Exec {act['exec']}"
        for label, a in cls.KEY_CHOICES:
            if a == act:
                return label
        if "cmd" in act:
            return act["cmd"]
        if "key" in act:
            return act["key"]
        if "label" in act:
            return act["label"]
        return "?"

    def key_act(self, i):
        keys = self.b.cfg["keys"]
        return keys[i] if i < len(keys) else None

    def set_key(self, i, act):
        keys = self.b.cfg["keys"]
        while len(keys) <= i:
            keys.append(None)
        keys[i] = act or None
        save_config(self.b.cfg)
        if self.b.ser:
            save_to_mega(self.b.cfg, self.b.ser, keep=True)
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
            what = self.describe(self.key_act(k))
            if self.portrait and what.startswith("Exec "):
                what = "Ex " + what[5:]  # 5 across is narrow
            self.widget(wid, "B", 4 + c * bw, gy + r * bh, bw - 3, bh - 4, C_BTN if learnt else C_PANEL,
                        C_TEXT if learnt else C_DIM, C_BTN_ON, 0, 0, 0, f"{k + 1}\n{what}")
        self.keymap[self.SET_RESET] = {"reset_keys": True}
        self.widget(self.SET_RESET, "B", 4, self.h - 104, w - 8, 40, C_PANEL, C_DIM, C_BTN_ON, 0, 0, 0,
                    "Set all keys back to defaults")
        self.back_button()

    def draw_key_editor(self, i):
        w = self.w
        top = self.header_h() + 4
        self.keymap = {}
        self.setup_title(top, f"Key {i + 1}: {self.describe(self.key_act(i))}\nchoose what it does")
        cols = 4 if self.portrait else 5
        rows = -(-len(self.KEY_CHOICES) // cols)
        gy = top + 44
        bw, bh = (w - 4) // cols, min(70, (self.h - 62 - gy) // rows)
        cur = self.key_act(i) or {}
        for n, (label, act) in enumerate(self.KEY_CHOICES):
            r, c = divmod(n, cols)
            wid = self.SET0 + n
            self.keymap[wid] = {"assign": i, "choice": act}
            on = act == cur or (act == "exec" and "exec" in cur)
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
        name = b.ma["name"].get(f["exec"]) if b.ma_linked() else None
        name = name or f.get("name") or (str(f["exec"]) if self.portrait else f"Exec {f['exec']}")
        if self.portrait:
            name = name[:7]
        text = f"{name}\n" + ("--" if shown is None else f"{round(shown)}%")
        waiting = ma is not None and pos is not None and not b.picked[i]
        if waiting:
            text += "\n" + ("^ ^ ^" if pos < ma else "v v v")
        marker = int(pos * 10) if (ma is not None and pos is not None) else -1
        # the sequence's colour from MA: dimmed behind, brighter for the level
        color = b.ma["color"].get(f["exec"]) if b.ma_linked() else None
        bg, bar = (shade(color, 0.4), shade(color, 0.85)) if color else (C_BAR_BG, C_BAR)
        bg, bar = bg or C_BAR_BG, bar or C_BAR
        self.setw(self.FADER0 + i, value=int((shown or 0) * 10), text=text, marker=marker,
                  colors=(bg, C_CMD if waiting else C_TEXT, C_WAIT if waiting else bar))

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
            a = self.b.encoder_attr(act["enc"])
            if a:
                self.b.ma_plugin(f"setv {a[0]} {act['setv']}")
            self.set_screen("encoders")
            return
        if "setpage" in act:  # tap: next three rows, then back to the top
            a = self.b.encoder_attr(act["enc"])
            n = len(self.b.ma["sets"].get(a[0].lower(), [])) if a else 0
            rows = self.sets_rows
            self.sets_page = self.sets_page + rows if (self.sets_page + rows) * 3 < n else 0
            self.show_sets(act["enc"])
            return
        if "vkey" in act:
            k, i = act["vkey"], act["enc"]
            a = self.b.encoder_attr(i)
            if k == "Set":
                if a and self.value_entry not in ("", "-", "."):
                    self.b.set_value(a[0], self.value_entry)
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
        if "reset_keys" in act:
            if time.time() - self.reset_armed > 4:  # first tap: ask for a second one
                self.reset_armed = time.time()
                self.setw(self.SET_RESET, text="Tap again to reset all 20 keys", colors=(C_WAIT, C_CMD, C_BTN_ON))
                return
            self.reset_armed = 0.0
            self.b.cfg["keys"] = copy.deepcopy(DEFAULTS["keys"])
            save_config(self.b.cfg)
            if self.b.ser:
                save_to_mega(self.b.cfg, self.b.ser, keep=True)
            print("Keys set back to the defaults.")
            self.draw()
            return
        if "keypad" in act:
            if act["keypad"] == "Back":
                self.set_screen("main")
            else:
                self.b.ma_key(act["keypad"])
        elif "back" in act:
            self.set_screen("setup" if self.name.startswith("key") else
                            "encoders" if self.name.startswith("entry") else
                            f"key{self.name[4:]}" if self.name.startswith("exec") else "main")
        elif "encpage" in act:
            self.b.next_enc_page()
        else:
            self.b.do_action(act, True)

    def on_release(self, wid):
        act = self.keymap.get(wid)
        if act and not any(k in act for k in ("keypad", "back", "encpage", "edit", "assign",
                                              "digit", "reset_keys", "entry", "vkey", "setv", "setpage")):
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
        self.verbose = False
        self.ma = dict(alive=0.0, page=None, fader={}, run={}, name={}, master={}, cmdline="",
                       busy="", busy_at=0.0, res={}, color={},
                       feat=("", "", False), encn=0, encs={}, ver=None, linked_at=0.0,
                       sets={})  # attribute -> named values of the selected fixture  # MA's encoders (see report_encoders)
        self.keys_down = set()               # hardware keys whose press was acted on
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
            if getattr(e, "errno", None) in (48, 98, 10048):  # address in use (Mac, Linux, Windows)
                print("  Another bridge is probably still running (e.g. the auto-start one):"
                      " it sends to MA too. Stop it first.")

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
            if e and e.get("div", 4) < 2:  # set by an earlier, over-eager click detection
                e["div"] = 2
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

    def encoder_attr(self, i):
        """-> (attribute, display name, MA's value text) encoder i controls, or None."""
        if self.following(i):
            e = self.ma_encoder(i)
            return (e["attr"], e["pretty"], e["value"]) if e else None
        encs = self.cfg["encoders"]
        act = encs[i] if i < len(encs) and encs[i] else {}
        return (act["attribute"], act["attribute"], "") if "attribute" in act else None

    def encoder_push(self, i, down):
        """Click: type a value for the encoder's attribute (Coarse/Fine is on the
        screen). An encoder without an attribute (Page) does its push action."""
        if self.encoder_attr(i) or self.following(i):
            if down and self.pi_ready and self.encoder_attr(i):
                self.screen.set_screen("encoders" if self.screen.name == f"entry{i}" else f"entry{i}")
            return
        act = self.cfg["encoders"][i] if i < len(self.cfg["encoders"]) else {}
        if act and act.get("push"):
            self.do_action(act["push"], down)

    def set_value(self, attr, text):
        self.ma_cmd(f'Attribute "{attr}" At {text}')

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
        """A console key: typed into MA's real command line when linked."""
        if not self.ma_linked():
            self.screen.local_key(k)
            return
        verb = {"Please": "please", "Clear": "clear", "<-": "back"}.get(k)
        self.ma_plugin(verb or f"type {k}")
        # Show the expected result straight away; MA's real line follows.
        t = self.ma["cmdline"]
        if k in ("Please", "Clear"):
            t = ""
        elif k == "<-":
            t = re.sub(r"\s*\S+\s*$", "", t)
            t = t + " " if t else ""
        else:
            numeric = re.fullmatch(r"[\d.]+", k) is not None
            if t and not t.endswith(" ") and not numeric:
                t += " "
            t += k + ("" if numeric else " ")
        self.ma["cmdline"] = t
        if self.pi_ready:
            self.screen.update_cmdline()

    def ma_code_version(self):
        """Short fingerprint of the MA code this bridge would install."""
        if not hasattr(self, "_ma_ver"):
            try:
                self._ma_ver = f"{zlib.crc32(open(resource('ma3', 'littlelx.lua'), 'rb').read()):08x}"
            except OSError:
                self._ma_ver = ""
        return self._ma_ver

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
                                   if e and isinstance(e.get("push"), dict) and "resolution" in e["push"]}
                                | set(self.cfg.get("encoder_attributes", [])))) or "Dimmer"
        attrs = re.sub(r"[^A-Za-z0-9,_]", "", attrs)
        start = 'Lua "' + self.MA_RUN.replace("{arg}", f"__start {line} {tick} {attrs} {self.ma_code_version()}") + '"'

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
        addr, arg = fader_message(fmt, self.page, self.cfg["faders"][i]["exec"], value)
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
        if down:
            self.keys_down.add(i)
        else:
            self.keys_down.discard(i)
        act = self.cfg["keys"][i] if i < len(self.cfg["keys"]) else None
        if act:
            self.do_action(act, down)

    def do_action(self, act, down):
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
        elif "screen" in act:
            self.screen.set_screen(act["screen"] if self.screen.name != act["screen"] else "main")
        elif "encpage" in act:
            self.next_enc_page()

    def set_page(self, page, from_ma=False):
        page = max(1, page)
        if not from_ma:
            self.osc("/cmd", f"Page {page}")  # MA follows; the plugin confirms
        if page == self.page:
            return
        self.page = page
        self.cfg["page"] = page
        self.ma["fader"], self.ma["run"], self.ma["name"], self.ma["color"] = {}, {}, {}, {}
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
            if d and self.pi_ready and self.screen.scroll_sets(d):
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
        m = re.search(r"/Page(\d+)/Fader(\d+)$", addr)  # plain MA OSC feedback
        if not m:
            return
        page, ex = int(m.group(1)), int(m.group(2))
        nums = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
        if nums:
            self.ma_values[(page, ex)] = float(nums[-1])
        if page == self.page:
            for i, f in enumerate(self.cfg["faders"]):
                if f["exec"] == ex:
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
        scr = self.screen if self.pi_ready else None
        if what == "page" and isinstance(val, int):
            self.set_page(val, from_ma=True)
        elif what.startswith(("fader/", "run/", "name/", "color/")):
            kind, ex = what.split("/", 1)
            ex = int(ex)
            if kind == "fader":
                v = float(val)
                ma["fader"][ex] = v
                for i, f in enumerate(self.cfg["faders"]):
                    if f["exec"] == ex:
                        # Moved in MA by someone else? Then the physical fader must
                        # catch it again. Not if it's just MA reporting one of our
                        # own recent moves late, or the fader is in our hand now.
                        now = time.time()
                        ours = any(abs(v - sv) <= 1.5 for t, sv in self.sent_hist[i]
                                   if now - t <= self.ECHO_WINDOW)
                        hands_on = now - self.moved_at[i] < self.HANDS_ON
                        if self.picked[i] and not ours and not hands_on and self.fader_pos[i] is not None \
                                and abs(v - self.fader_pos[i]) > 3:
                            self.picked[i] = not self.cfg.get("pickup", True)
                        self.check_pickup(i)
                        if scr:
                            scr.update_fader(i)
            elif kind == "run":
                ma["run"][ex] = int(val or 0)
                if scr:
                    scr.update_buttons()
            else:
                ma[kind][ex] = str(val or "")
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
        elif what == "feat":
            p = (str(val or "") + "||").split("|")
            ma["feat"] = (p[0], p[1], p[2] == "1")
            if scr:
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
            print("MA probe:\n  " + str(val or "").replace(" / ", "\n  "))
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
                e["div"] = enc.div = 2
                enc.acc = 0
                print(f"Encoder {i + 1}: 2 signal changes per click detected, adjusted.")
                save_config(self.cfg)
                if self.ser:
                    save_to_mega(self.cfg, self.ser, keep=True)

    # ---- main loop
    def run(self):
        last_ping = last_status = last_refresh = 0
        started = time.time()
        while True:
            self.ser = connect(self.cfg)
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
                try:
                    kind, src, data = events.get(timeout=0.01)  # short: held fader values go out on time
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
                self.check_encoder_clicks(now)
                if self.cfg.get("ma3", {}).get("auto_install", True) and now - self.ma_installed_at > 30:
                    if not self.ma_linked() and now - started > 4:
                        self.install_ma3()
                    elif (self.ma_linked() and self.ma["ver"] != self.ma_code_version()
                          and now - self.ma["linked_at"] > 5):  # older code still running in MA
                        print("Updating the littlelx code in MA3...")
                        self.install_ma3()
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
    end = time.time() + 6
    while time.time() < end:
        try:
            data, _ = rx.recvfrom(65536)
        except socket.timeout:
            continue
        for addr, args in osc_parse(data):
            if addr.endswith("/littlelx/probe") and args:
                print("\n" + str(args[0]).replace(" / ", "\n"))
                print("\nThe same lines are in MA's System Monitor (littlelx probe: ...).")
                return
    print("No answer. The lines may still be in MA's System Monitor (littlelx probe: ...).")


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
    files = {n: z.read(n) for n in names}

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
                    raise UpdateFailed("touchscreen reported " + line[10:])
                for p in prefixes:
                    if line.startswith(p):
                        return line
        return None

    def hello():
        for _ in range(8):
            ser.send(">?")
            h = wait([">HELLO"], 2)
            if h:
                return h
        return None

    print("Looking for the touchscreen...")
    h = hello()
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--learn", action="store_true", help="learn the wiring")
    ap.add_argument("--calibrate", action="store_true", help="calibrate the touchscreen")
    ap.add_argument("--faders", action="store_true", help="calibrate fader bottom/top")
    ap.add_argument("--monitor", action="store_true", help="print raw events")
    ap.add_argument("--probe", action="store_true", help="key-matrix wiring diagnostics")
    ap.add_argument("--test-faders", action="store_true", help="find the fader format MA3 accepts")
    ap.add_argument("--ma-probe", action="store_true", help="show what MA's Lua offers (encoder diagnostics)")
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
        else:
            print(f"Sending OSC to {cfg['osc']['host']}:{cfg['osc']['port']} prefix '{cfg['osc']['prefix']}'")
            b = Bridge(cfg)
            b.verbose = args.verbose
            b.run()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
