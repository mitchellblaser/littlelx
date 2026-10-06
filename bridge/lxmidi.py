"""MIDI controllers (APC Mini, Midi Fighter Spectra, ...) as extra littlelx hardware.

A device definition (midi/<name>.json, or your own in ~/littlelx-profiles/midi/)
says what the device has and how its lights work:

    {"name": "APC Mini", "match": ["APC MINI"], "exclude": ["mk2"],
     "controls": [{"group": "pads", "type": "button", "msg": "note", "channel": 1,
                   "numbers": [0, 1, ...], "led": "pad"},
                  {"group": "faders", "type": "fader", "msg": "cc", "channel": 1,
                   "numbers": [48, ...]}],
     "leds": {"pad": {"msg": "note", "channel": 1, "off": 0,
                      "palette": [[1, "#00ff00"], [3, "#ff0000"]] | "midifighter",
                      "idle": "off" | {"color": "sequence", "default": "#ffff00", "channel": 2},
                      "on": {"color": "sequence", "blink": 1} | {"velocity": 1}}},
     "map": {"pads": [{"exec": 101}, ...], "faders": [{"exec": 201}, ...]}}

What each control does lives in the profile ("midi"), in the same form as the
controller's own keys and faders; a group the profile leaves out uses the
device's "map":

    "midi": [{"device": "APC Mini", "map": {"faders": [{"exec": 201}, ...]}},
             {"device": "Midi Fighter Spectra", "enabled": false}]

A known device that is plugged in but not in the profile works with its
defaults. Lights: "idle" = something is assigned (for an executor: it has a
sequence), "on" = running / active; "color": "sequence" takes the sequence's
colour from MA (nearest in the palette).
"""
import colorsys
import json
import os
import re
import threading
import time

try:
    import rtmidi  # python-rtmidi
except Exception:  # not installed, or no MIDI system (e.g. a container)
    rtmidi = None


# The computer's own virtual MIDI ports, not devices: not worth listing
VIRTUAL = ("through", "iac driver", "network session", "microsoft gs wavetable", "loopmidi")


def rgb(hexstr):
    h = str(hexstr or "").lstrip("#")
    if len(h) >= 6:
        try:
            return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))
        except ValueError:
            pass
    return None


def midifighter_palette():
    """Midi Fighter colour velocities: 1..126 go round the colour wheel from blue."""
    out = []
    for v in range(1, 127):
        hue = ((240 - (v - 1) * 360 / 126) % 360) / 360
        r, g, b = colorsys.hsv_to_rgb(hue, 1, 1)
        out.append([v, "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))])
    return out


def nearest(palette, color, dim=False):
    """The palette velocity closest to color (hue first: a dim sequence colour
    should still pick the right hue). Entries are [velocity, colour] or
    [velocity, colour, its dim velocity]; dim picks the latter when there is one."""
    c = rgb(color)
    if not c or not palette:
        return palette[0][0] if palette else 1
    h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in c))
    best, bd = palette[0], None
    for entry in palette:
        hx = entry[1]
        p = rgb(hx)
        if not p:
            continue
        ph, ps, pv = colorsys.rgb_to_hsv(*(x / 255 for x in p))
        if s < 0.2 or ps < 0.2:  # white / grey: compare saturation instead of hue
            d = abs(s - ps) * 2 + (0 if (s < 0.2) == (ps < 0.2) else 1)
        else:
            dh = abs(h - ph)
            d = min(dh, 1 - dh) * 4
        if bd is None or d < bd:
            best, bd = entry, d
    return best[2] if dim and len(best) > 2 else best[0]


class Backend:
    """MIDI ports through python-rtmidi. Tests swap in a fake with the same methods."""

    def __init__(self):
        self.ok = rtmidi is not None
        self.error = None if self.ok else "python-rtmidi isn't installed"
        self._in = self._out = None
        if self.ok:
            try:
                self._in, self._out = rtmidi.MidiIn(), rtmidi.MidiOut()
            except Exception as e:  # no MIDI system
                self.ok, self.error = False, str(e)

    def inputs(self):
        return self._in.get_ports() if self.ok else []

    def outputs(self):
        return self._out.get_ports() if self.ok else []

    def open_input(self, name, callback):
        port = rtmidi.MidiIn()
        port.open_port(self.inputs().index(name))
        port.ignore_types(sysex=True, timing=True, active_sense=True)
        port.set_callback(lambda ev, _: callback(ev[0]))
        return port

    def open_output(self, name):
        port = rtmidi.MidiOut()
        port.open_port(self.outputs().index(name))
        return port


class PiBackend:
    """MIDI devices plugged into the controller's USB ports (the touchscreen
    Pi passes them through; see USB in pi/app/littlelx.c). Same methods as
    Backend; the bridge feeds it the Pi's USB / MI lines."""

    ok, error, where = True, None, "controller"

    def __init__(self, bridge):
        self.b = bridge
        self.ports = {}      # port name -> id
        self.parsers = {}    # id -> (callback, parser state)
        self.pending = {}    # id -> bytes waiting to go (flushed once per tick)

    @staticmethod
    def port_name(dev_id, name):
        return f"{name} (controller USB {dev_id})"

    def on_line(self, line):
        """A USB line from the Pi: "USB + 3 midi APC MINI", "USB - 3", "MI 3 90007f"."""
        p = line.split(" ", 4)
        if p[0] == "USB" and len(p) >= 3 and p[1] == "+" and p[2].isdigit():
            kind, name = (p[3] if len(p) > 3 else ""), (p[4] if len(p) > 4 else "USB device")
            if kind == "midi":
                self.ports[self.port_name(int(p[2]), name)] = int(p[2])
        elif p[0] == "USB" and len(p) >= 3 and p[1] == "-" and p[2].isdigit():
            gone = int(p[2])
            self.ports = {n: i for n, i in self.ports.items() if i != gone}
        elif p[0] == "MI" and len(p) >= 3 and p[1].isdigit():
            got = self.parsers.get(int(p[1]))
            if got:
                try:
                    data = bytes.fromhex(" ".join(p[2:]).replace(" ", ""))
                except ValueError:
                    return
                for msg in parse(got[1], data):
                    got[0](msg)

    def lost(self):
        """The controller (or its touchscreen) went away: so did these."""
        self.ports = {}

    def inputs(self):
        return list(self.ports)

    def outputs(self):
        return list(self.ports)

    def open_input(self, name, callback):
        dev_id = self.ports[name]
        self.parsers[dev_id] = (callback, {"status": 0, "data": [], "sysex": False})
        return _PiPort(self, dev_id)

    def open_output(self, name):
        return _PiPort(self, self.ports[name])

    def flush(self):
        for dev_id, data in list(self.pending.items()):
            del self.pending[dev_id]
            for off in range(0, len(data), 60):  # short lines for the link
                self.b.to_pi(f"MO {dev_id} {data[off:off + 60].hex()}")


class _PiPort:
    def __init__(self, backend, dev_id):
        self.backend, self.id = backend, dev_id

    def send_message(self, msg):
        self.backend.pending[self.id] = self.backend.pending.get(self.id, b"") + bytes(msg)

    def close_port(self):
        self.backend.parsers.pop(self.id, None)


def parse(state, data):
    """MIDI bytes (as they come, in pieces) -> complete messages; running status
    kept, SysEx and real-time bytes skipped."""
    out = []
    for byte in data:
        if byte >= 0xF8:          # real-time: one byte, anywhere
            continue
        if byte == 0xF0:
            state["sysex"] = True
            continue
        if state["sysex"]:
            if byte == 0xF7 or byte >= 0x80:
                state["sysex"] = False
            if byte == 0xF7 or byte < 0x80:
                continue
        if byte >= 0x80:
            state["status"], state["data"] = (byte if byte < 0xF0 else 0), []
            continue
        st = state["status"]
        if not st:
            continue
        state["data"].append(byte)
        need = 1 if (st & 0xF0) in (0xC0, 0xD0) else 2
        if len(state["data"]) == need:
            out.append([st] + state["data"])
            state["data"] = []
    return out


def builtin_dir():
    import sys
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    return os.path.join(base, "midi")


def load_definitions(user_dir=None):
    """Device definitions by name: the built-in ones, then the user's own (which win)."""
    defs = {}
    for d in (builtin_dir(), user_dir):
        if not d or not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(d, f)) as fh:
                        spec = json.load(fh)
                    defs[spec.get("name") or f[:-5]] = spec
                except (OSError, ValueError) as e:
                    print(f"MIDI device file {f}: {e}")
    return defs


def matches(spec, port_name):
    p = port_name.lower()
    return (any(m.lower() in p for m in spec.get("match") or [spec.get("name", "")])
            and not any(x.lower() in p for x in spec.get("exclude") or []))


class Device:
    """One plugged-in MIDI device: its controls, what they do, its lights."""

    def __init__(self, spec, entry, in_name, out_name):
        self.spec, self.entry = spec, entry or {}
        self.name = spec.get("name", "MIDI device")
        self.in_name, self.out_name = in_name, out_name
        self.inp = self.out = None
        self.backend = None     # where it is plugged in (the computer / the controller)
        self.leds = {}          # (status, number) -> velocity last sent
        self.lookup = {}        # (kind, channel 0-15, number) -> (group, index)
        self.loose = {}         # (kind, number) -> (group, index): any channel
        self.channels = {}      # group -> the channel it really uses (set up differently)
        self.told = set()       # unmatched messages already logged
        for c in spec.get("controls", []):
            kind = "cc" if c.get("msg") == "cc" else "note"
            ch = int(c.get("channel", 1)) - 1
            for i, n in enumerate(c.get("numbers", [])):
                self.lookup[(kind, ch, int(n))] = (c["group"], i)
                self.loose.setdefault((kind, int(n)), (c["group"], i))
        self.faders = {}        # (group, index) -> dict(pos, sent, picked, at, pending)
        self.down = set()

    def control(self, group):
        for c in self.spec.get("controls", []):
            if c["group"] == group:
                return c
        return None

    def item(self, group, i):
        """What control i of group does: the profile's map, else the device's."""
        m = (self.entry.get("map") or {}).get(group)
        if m is None:
            m = (self.spec.get("map") or {}).get(group) or []
        return (m[i] if i < len(m) else None) or {}

    def palette(self, led):
        p = led.get("palette") or []
        return midifighter_palette() if p == "midifighter" else p

    def send(self, data):
        if self.out:
            try:
                self.out.send_message(list(data))
            except Exception:
                pass

    def light(self, group, i, state, color):
        """state: "off" | "idle" | "on"; color: the sequence colour (hex) or None."""
        c = self.control(group)
        led = (self.spec.get("leds") or {}).get(c.get("led")) if c else None
        if not led:
            return
        number = int(c["numbers"][i])
        how = led.get(state) if state != "off" else "off"
        ch = int(led.get("channel", c.get("channel", 1))) - 1
        if group in self.channels and int(led.get("channel", c.get("channel", 1))) == int(c.get("channel", 1)):
            ch = self.channels[group]  # the device sends on another channel: light it there too
        vel = int(led.get("off", 0))
        if isinstance(how, dict):
            ch = int(how.get("channel", ch + 1)) - 1
            if "velocity" in how:
                vel = int(how["velocity"])
            else:
                col = color if how.get("color") == "sequence" and rgb(color) and rgb(color) != (0, 0, 0) \
                    else how.get("default", "#ffffff") if how.get("color") == "sequence" else how.get("color")
                vel = nearest(self.palette(led), col, how.get("dim")) + int(how.get("blink", 0))
        status = (0x90 if led.get("msg", "note") == "note" else 0xB0) | (ch & 15)
        key = (number,)
        if self.leds.get(key) == (status, vel):
            return
        if self.leds.get(key) and (self.leds[key][0] & 15) != (status & 15):
            self.send((0x80 | (self.leds[key][0] & 15), number, 0))  # other channel: drop the old one
        self.leds[key] = (status, vel)
        self.send((status, number, vel))

    def all_off(self):
        for c in self.spec.get("controls", []):
            if c.get("led"):
                for i in range(len(c.get("numbers", []))):
                    self.light(c["group"], i, "off", None)

    def reset_channel(self, group, ch, value=0):
        """Hand group's lights on channel ch (and its animation channel, if the
        device has one: e.g. the Spectra's flash / pulse) back to the device."""
        c = self.control(group)
        led = (self.spec.get("leds") or {}).get(c.get("led")) if c else None
        if not led:
            return
        chans = [ch] + ([ch + int(led["animation_channel_offset"])] if "animation_channel_offset" in led else [])
        for n in c.get("numbers", []):
            for k in chans:
                self.send((0x90 | (k & 15), int(n), value))

    def start(self):
        """Just plugged in: no animations left over, everything dark."""
        for c in self.spec.get("controls", []):
            led = (self.spec.get("leds") or {}).get(c.get("led"))
            if led and "animation_channel_offset" in led:
                ch = self.channels.get(c["group"], int(led.get("channel", c.get("channel", 1))) - 1)
                k = ch + int(led["animation_channel_offset"])
                for n in c.get("numbers", []):
                    self.send((0x90 | (k & 15), int(n), 0))
        self.all_off()

    def close(self):
        for c in self.spec.get("controls", []):  # give the lights back to the device
            led = (self.spec.get("leds") or {}).get(c.get("led"))
            if led and "release" in led:
                ch = self.channels.get(c["group"], int(led.get("channel", c.get("channel", 1))) - 1)
                self.reset_channel(c["group"], ch, int(led["release"]))
                for n in c.get("numbers", []):
                    self.leds[(int(n),)] = None
        if not any((self.spec.get("leds") or {}).get(c.get("led"), {}).get("release") is not None
                   for c in self.spec.get("controls", [])):
            self.all_off()
        for p in (self.inp, self.out):
            try:
                p and p.close_port()
            except Exception:
                pass
        self.inp = self.out = None


class Midi:
    """All MIDI devices for one bridge: finds them (plugged in at any time),
    turns their messages into the bridge's actions and fader moves, and keeps
    their lights following MA (or the generic target's feedback)."""

    SCAN = 2.0      # s between looks for plugged / unplugged devices
    FADER_GAP = 0.025

    def __init__(self, bridge, backend=None, user_dir=None):
        self.b = bridge
        self.pi = PiBackend(bridge)
        self.backends = [backend or Backend(), self.pi]
        self.user_dir = user_dir
        self.defs = load_definitions(user_dir)
        self.devices = {}       # input port name -> Device
        self.unknown = []       # MIDI inputs we have no definition for
        self.scanned = 0.0
        self.dirty = True
        self.lock = threading.Lock()
        self.learned = self.load_learned()  # device -> {group: channel} it really uses

    def learned_path(self):
        return os.path.join(self.user_dir, "channels.json") if self.user_dir else None

    def load_learned(self):
        try:
            with open(self.learned_path()) as f:
                return json.load(f)
        except (OSError, TypeError, ValueError):
            return {}

    def save_learned(self):
        try:
            os.makedirs(self.user_dir, exist_ok=True)
            with open(self.learned_path(), "w") as f:
                json.dump(self.learned, f)
        except (OSError, TypeError):
            pass

    # ---- which devices
    def entries(self):
        return [e for e in (self.b.cfg.get("midi") or []) if isinstance(e, dict)]

    def entry_for(self, spec, port):
        for e in self.entries():
            if e.get("device") == spec.get("name") and (not e.get("port") or e["port"].lower() in port.lower()):
                return e
        return None

    def reload(self):
        """Profile changed: re-read the definitions and the mappings."""
        self.defs = load_definitions(self.user_dir)
        for d in list(self.devices.values()):
            d.close()
        self.devices = {}
        self.scanned = 0.0
        self.update_status()

    @property
    def backend(self):  # the computer's own MIDI
        return self.backends[0]

    @backend.setter
    def backend(self, b):
        self.backends[0] = b

    def scan(self):
        self.unknown = []
        for backend in self.backends:
            self.scan_backend(backend)
        self.update_status()

    def scan_backend(self, backend):
        if not backend.ok:
            if not getattr(self, "said", False):
                self.said = True
                print(f"MIDI devices on this computer: off ({backend.error})")
            return
        try:
            ins, outs = backend.inputs(), backend.outputs()
        except Exception:
            return
        for name in list(self.devices):
            if self.devices[name].backend is backend and name not in ins:
                print(f"MIDI: {self.devices[name].name} unplugged")
                self.devices.pop(name).close()
        for name in ins:
            if name in self.devices:
                continue
            spec = next((s for s in self.defs.values() if matches(s, name)), None)
            if not spec:
                if not any(v in name.lower() for v in VIRTUAL):
                    self.unknown.append(name)
                continue
            entry = self.entry_for(spec, name)
            if entry and entry.get("enabled") is False:
                continue
            out = next((o for o in outs if o == name), None) or next((o for o in outs if matches(spec, o)), None)
            dev = Device(spec, entry, name, out)
            dev.backend = backend
            dev.channels = dict(self.learned.get(dev.name, {}))  # as found last time
            try:
                dev.inp = backend.open_input(name, lambda msg, d=dev: self.b.post_midi(d, msg))
                dev.out = backend.open_output(out) if out else None
            except Exception as e:
                print(f"MIDI: can't open {name}: {e}")
                dev.close()
                continue
            self.devices[name] = dev
            print(f"MIDI: {dev.name} connected ({name})")
            dev.start()
            self.dirty = True

    # ---- what's connected (the app reads this from its own thread: a snapshot)
    def update_status(self):
        rows = [{"kind": "midi", "name": d.name, "port": d.in_name, "connected": True,
                 "where": getattr(d.backend, "where", "computer")}
                for d in self.devices.values()]
        rows += [{"kind": "midi", "name": n, "port": n, "connected": True, "unknown": True} for n in self.unknown]
        self.snapshot = rows

    def status(self):
        return list(getattr(self, "snapshot", []))

    # ---- input
    def find(self, dev, kind, ch, number):
        """The control a message is for: exact, else the same note / CC on another
        channel (a device set up with another channel, e.g. in its own utility)."""
        hit = dev.lookup.get((kind, ch, number))
        if not hit:
            hit = dev.loose.get((kind, number))
            if hit and dev.channels.get(hit[0]) != ch:
                c = dev.control(hit[0])
                old = dev.channels.get(hit[0], int(c.get("channel", 1)) - 1)
                dev.reset_channel(hit[0], old)  # whatever went to the wrong channel: undone
                dev.channels[hit[0]] = ch
                print(f"MIDI: {dev.name} sends on channel {ch + 1}: using that")
                self.learned.setdefault(dev.name, {})[hit[0]] = ch
                self.save_learned()
                dev.leds = {}
                dev.start()
                self.dirty = True  # its lights on that channel too
        if not hit and (kind, ch, number) not in dev.told and len(dev.told) < 20:
            dev.told.add((kind, ch, number))
            print(f"MIDI: {dev.name} sent {kind} {number} on channel {ch + 1}: not one of its controls")
        return hit

    def on_message(self, dev, msg):
        if not msg:
            return
        st, kind = msg[0], msg[0] & 0xF0
        ch = st & 15
        if kind in (0x90, 0x80) and len(msg) >= 3:
            hit = self.find(dev, "note", ch, msg[1])
            if hit:
                self.button(dev, hit[0], hit[1], kind == 0x90 and msg[2] > 0)
        elif kind == 0xB0 and len(msg) >= 3:
            hit = self.find(dev, "cc", ch, msg[1])
            if hit:
                c = dev.control(hit[0])
                if c.get("type") == "fader":
                    self.fader(dev, hit[0], hit[1], msg[2] * 100.0 / 127)
                else:
                    self.button(dev, hit[0], hit[1], msg[2] >= 64)

    def button(self, dev, group, i, down):
        key = (group, i)
        if not down and key not in dev.down:
            return
        dev.down.add(key) if down else dev.down.discard(key)
        act = dev.item(group, i)
        if act:
            self.b.do_action(act, down)
        self.dirty = True

    def fader(self, dev, group, i, value):
        f = dev.faders.setdefault((group, i), dict(pos=None, sent=None, picked=False, at=0.0, pending=None))
        prev, f["pos"] = f["pos"], value
        item = dev.item(group, i)
        target = self.target_level(item)
        if not f["picked"]:  # soft takeover, like the controller's own faders
            if target is None or not self.b.cfg.get("pickup", True) or prev is None and abs(value - target) <= 3:
                f["picked"] = True
            elif prev is not None and ((prev - target) * (value - target) <= 0 or abs(value - target) <= 2):
                f["picked"] = True
        if f["picked"]:
            self.send_fader(item, f, value)

    def target_level(self, item):
        ex = item.get("exec")
        if ex is not None and not self.b.generic() and self.b.ma_linked():
            return self.b.ma["fader"].get(ex)
        return None

    def send_fader(self, item, f, value, force=False):
        now = time.time()
        if not force and now - f["at"] < self.FADER_GAP:
            f["pending"] = value
            return
        f["at"], f["pending"] = now, None
        if f["sent"] is not None and abs(f["sent"] - value) < 0.4:
            return
        f["sent"] = value
        self.b.send_item_fader(item, value)

    # ---- the bridge's loop
    def tick(self, now):
        if now - self.scanned > self.SCAN:
            self.scanned = now
            self.scan()
        for dev in self.devices.values():
            for (g, i), f in dev.faders.items():
                if f["pending"] is not None and now - f["at"] >= self.FADER_GAP:
                    self.send_fader(dev.item(g, i), f, f["pending"], force=True)
        if self.dirty:
            self.dirty = False
            self.refresh()
        self.pi.flush()

    def page_changed(self):
        for dev in self.devices.values():  # new page: catch each fader again
            for f in dev.faders.values():
                f["picked"] = not self.b.cfg.get("pickup", True)
        self.dirty = True

    def refresh(self):
        for dev in self.devices.values():
            for c in dev.spec.get("controls", []):
                if not c.get("led"):
                    continue
                for i in range(len(c.get("numbers", []))):
                    state, color = self.b.item_state(dev.item(c["group"], i))
                    dev.light(c["group"], i, state, color)

    def close(self):
        for d in self.devices.values():
            d.close()
        self.devices = {}

    def executors(self):
        """Executor numbers the MIDI devices use (MA reports these)."""
        out = set()
        for spec in self.defs.values():
            entry = next((e for e in self.entries() if e.get("device") == spec.get("name")), {}) or {}
            if entry.get("enabled") is False:
                continue
            groups = dict(spec.get("map") or {}, **(entry.get("map") or {}))
            for items in groups.values():
                for it in items or []:
                    if isinstance(it, dict) and isinstance(it.get("exec"), int):
                        out.add(it["exec"])
        return out


def ranges(numbers):
    """{101, 102, 103, 201} -> "101-103,201"."""
    out, run = [], []
    for n in sorted(numbers):
        if run and n == run[-1] + 1:
            run.append(n)
        else:
            if run:
                out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = [n]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ",".join(out)


def _selftest():
    assert ranges({101, 102, 103, 201, 205, 206}) == "101-103,201,205-206"
    p = midifighter_palette()
    assert nearest(p, "#0000ff") == 1 and nearest([[1, "#00ff00"], [3, "#ff0000"], [5, "#ffff00"]], "#ff2000") == 3
    assert re.match(r"#[0-9a-f]{6}", p[40][1])
    print("ok")


if __name__ == "__main__":
    _selftest()
