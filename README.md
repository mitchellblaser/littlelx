# littlelx

A small hardware controller for **grandMA3**: 20 keys, 5 faders, 2 push-encoders
and a 3.5" touchscreen. It speaks **OSC** to MA3, and the computer only sees
**one USB cable**. Works on Mac and Windows.

```
 faders/keys/encoders ──► Arduino Mega ──USB──► bridge (Mac/Windows) ──OSC/UDP──► grandMA3
                              ▲   │
               Serial1/2/3    │   ▼   (500000 baud)
                         Raspberry Pi 3B + 3.5" SPI touchscreen
```

| Part | What it runs | Where |
|------|--------------|-------|
| Arduino Mega | Reads every pin and relays the Pi's serial over USB. It doesn't need to know the wiring. | `mega/littlelx_mega/` |
| Mac / Windows | Bridge: turns pin events into MA3 OSC, draws the touchscreen, learns the wiring | `bridge/littlelx.py` |
| Raspberry Pi | A tiny RAM-only Linux whose only program is the touchscreen terminal | `pi/` |

### Why is there a bridge program at all?

MA3 only takes OSC over the **network** (UDP). The Mega's USB port is a plain
serial port, and neither it nor a Pi 3B can pretend to be a network adapter
(the Pi 3B can't be a USB device at all). So something on the computer has to
turn serial into UDP, and that's the bridge. It is one small script with no
installer, or one `.exe` on Windows.

The only bridge-free option would be USB-MIDI: reflash the Mega's USB chip
(genuine 16U2 boards only, not CH340 clones) as a class-compliant MIDI device
and map it in MA3's MIDI remotes. That loses the touchscreen UI, the keypad and
the wiring learner, so it isn't the default.

## The Pi image: small, read-only, safe to unplug

* The **entire OS is two files on a single FAT partition**: a ~5 MB kernel and a
  ~200 KB initramfs that holds only the app. The app *is* `/init`. There is no
  shell, no BusyBox, no libraries and no other processes.
* Linux runs **entirely from RAM**. The SD card isn't even mounted in normal
  use, so nothing writes to it and it can't be corrupted. Pull the power
  whenever you like. The card is only written during an update you start
  (below), and that is power-cut-safe too.
* No modules, no networking, no USB, no Wi-Fi/Bluetooth, no HDMI console. The
  Pi 3's USB controller normally fires thousands of interrupts a second; with it
  gone the CPU is free for the UI.
* Boots straight into the panel. If the app ever crashes it is respawned. If it
  ever hangs, the hardware watchdog reboots the Pi.
* The screen is only redrawn where something changed, and only changed rows are
  pushed over SPI. This matters far more for snappiness than CPU speed.
* `config.txt` has fixed clocks and a mild overclock (1300 MHz, `force_turbo`).
  **However:** a bus-powered Pi 3 is almost certainly under-voltage, and the
  firmware then caps the CPU at 600 MHz no matter what. A proper 5 V / 2.5 A
  supply to the Pi is the real fix. If you get random reboots or lockups,
  remove the five overclock lines from `config.txt` (you can edit it on any
  computer; it's a normal FAT partition).

## Setup

### 1. Wiring check

The Pi talks to the Mega's **Serial3**:

```
Mega TX3 (pin 14) ──[ 1kΩ ]──┬──► Pi GPIO15 / RXD (header pin 10)
                             │
                           [ 2kΩ ]   (2.2kΩ is fine too)
                             │
Mega GND ────────────────────┴────── Pi GND (e.g. header pin 6)

Mega RX3 (pin 15) ◄──────────────── Pi GPIO14 / TXD (header pin 8)   (direct wire is fine)
```

* ⚠️ **The two resistors are not optional.** The Mega's TX is **5 V** and the
  Pi's pins are **3.3 V only**. Wired straight, the Mega slowly damages GPIO15
  (or the whole Pi), and it back-powers the Pi through that pin when the Pi is
  off. The divider brings it down to ~3.3 V. A BSS138-style level-shifter board
  also works. The other direction (Pi → Mega) needs nothing.
* Already ran it without resistors? If the screen gets stuck on "waiting for
  computer…" while the bridge says the Mega is connected, the Pi's RX pin is
  probably damaged.
* Keys: either straight to GND, or in **key matrices** (any number, any size,
  with or without diodes). The Mega scans for matrices without knowing their
  rows and columns: it pulls one pin low at a time and sees which pin follows.
  Encoders: A/B/push to GND. Faders: wipers on A0–A15. Any pin except 0, 1, 14
  and 15 can be used. Nothing else needs to be known; the learn step works out
  which pin is which.

### 2. Flash the Mega

Optional: back up the old firmware first, since it's being replaced:
`avrdude -p m2560 -c wiring -P /dev/cu.usbmodemXXXX -b 115200 -D -U flash:r:old-mega.hex:i`

Easiest: in the littlelx app, **Firmware > Install** (it carries the firmware
that matches it, and updates the touchscreen too). From a terminal:
`littlelx.py --install` (the built app / .exe), `littlelx.py --install
littlelx-firmware.lxfw` (the `littlelx-firmware` from the GitHub build), or
just the Mega: `littlelx.py --flash-mega littlelx_mega.ino.hex`. Only the program is replaced: the learned wiring and
the active profile stay on the controller. If it fails halfway, just flash
again - the bootloader is never touched.

Or open `mega/littlelx_mega/littlelx_mega.ino` in the Arduino IDE, select
**Arduino Mega or Mega 2560**, and upload.

### 3. Flash the Pi SD card

Get `littlelx-sdcard.img.gz` (from the GitHub Actions run, *build → Artifacts*,
or build it yourself, see below). Write it with Raspberry Pi Imager (*Use
custom*) or balenaEtcher. Any card size works; only 32 MB is used.

The default screen setting (`dtoverlay=littlelx35`) is for the common 3.5"
XPT2046 boards that goodtft's **LCD-show** (`LCD35-show`, `tft35a`) supports,
mounted **portrait**. It uses exactly the same display setup as LCD-show.
Genuine Waveshare 3.5" (A) boards need `piscreen` instead; both lines are in
`config.txt`, just move the `#`. If the picture is upside down, use
`rotate=180`. For landscape use 90/270; the bridge's layout adapts by itself.

**Screen stays white?** Look at the Pi's green LED:
* **Double-blinking heartbeat:** Linux is running, so it's the display setting.
  Try the other `dtoverlay=` line. The bridge also prints
  `Touchscreen status: fb=missing` in this case.
* **No blinking:** the Pi isn't booting. Remove the overclock lines from
  `config.txt` and check the power supply.

### 4. Get the bridge running

* **Mac:** nothing to install: `python3 bridge/littlelx.py`. (The first time, macOS may
  offer to install its command line developer tools, which include Python; accept.)
* **Windows:** download `littlelx.exe` (GitHub Actions run, *build → Artifacts →
  littlelx-windows*) and run it from a Command Prompt. Or, with Python installed:
  `py -m pip install pyserial` then `py bridge\littlelx.py`.
  A genuine Mega needs no driver; CH340 clones get theirs from Windows Update.

The serial port is found automatically; if not, add `--port COM5` (Windows) or
`--port /dev/cu.usbmodem1101` (Mac), or set `"serial_port"` in the config.

### 5. Teach it your wiring (once)

```sh
python3 bridge/littlelx.py --learn        # Windows: littlelx.exe --learn
```

Follow the prompts: move each fader, turn and press each encoder, and press each
key in the order you want them numbered. Press Enter to skip any control.

**You only do this once.** The learned wiring and the fader and touch calibrations
are stored **on the controller itself** (the Mega's EEPROM) as well as on the
computer. Plug it into any other Mac or PC running the bridge and it loads
everything from the controller. If you set it up before this feature existed,
the bridge copies your existing setup onto the controller the first time it
connects.
Everything is saved to `~/.littlelx.json` (Windows: `C:\Users\<you>\.littlelx.json`).

Then calibrate the faders' real bottom and top. Pull all faders down and tap
**Next** on the screen (or press Enter), then push them all up and tap **Next**
again. Each fader's live reading is shown while you do it:

```sh
python3 bridge/littlelx.py --faders       # Windows: littlelx.exe --faders
```

Re-run this whenever a fader doesn't quite reach 0 % or 100 %.

Then calibrate the touchscreen (tap three crosses):

```sh
python3 bridge/littlelx.py --calibrate    # Windows: littlelx.exe --calibrate
```

### 6. Set up MA3

*Menu → In & Out → OSC*: add a line, set **Enable Input** and **Receive** /
**Receive Command** to Yes, set the port to `8000`, and set the prefix to `gma3`. If MA3
runs on another machine, set `"host"` in `~/.littlelx.json` to its IP address
(on Windows, allow the bridge through the firewall when asked).

Then add a **second** OSC line so MA can talk back. Set **Destination IP** to
the bridge computer (`127.0.0.1` if MA3 onPC runs on the same computer), the
port to `9000`, and **Send** = Yes. If it isn't line **2**, set
`"ma3": {"osc_line": N}` in `~/.littlelx.json`.

That's all. **The bridge installs its own code into MA3 over OSC** (no plugin to
import) and starts it, and it reinstalls it by itself after MA restarts or loads
a show. The screen shows **MA3 linked** when it's running. (`ma3/littlelx.lua`
can also be imported as a normal plugin if you prefer; tap it to start or stop.)

### What the MA3 link gives you

* **Page follows MA.** Change page in MA and the controller follows. Page
  buttons and the encoder change MA's page.
* **Fader target / actual.** Each screen fader shows **MA's real level** as the
  bar and **your physical fader** as a line. Until your fader reaches MA's
  level, the bar turns grey and shows `^ ^ ^` / `v v v` (which way to move). It
  only takes control once it crosses MA's level (soft takeover), so paging, or
  someone moving the fader in MA, never makes levels jump. Set `"pickup": false`
  to turn soft takeover off.
* **Status keys.** Highlight and Blind light up while they're on in MA, and
  executor buttons light up while running. Any touch button can light up for
  `"state": "highlight" | "lowlight" | "solo" | "blind"`.
* **Command line.** The keypad and `{"key": …}` buttons build the command on
  the controller (shown on its screen, and in MA's own command line where MA
  allows it) and send it whole on Please. Nothing is typed into MA, so MA's
  keyboard shortcuts and whatever has focus on the onPC don't matter. With the
  line empty, the screen shows MA's own command line (typed at the desk).

### 7. Run it

```sh
python3 bridge/littlelx.py        # Windows: double-click littlelx.exe
```

Start at login: on Mac run `bridge/install-autostart.sh`. On Windows put a
shortcut to `littlelx.exe` in `shell:startup` (Win+R → `shell:startup`).

It reconnects by itself when the controller is unplugged and plugged back in.

## The littlelx app (tray / menu bar)

The bridge as an app: it lives in the **system tray** (Windows) or the **menu
bar** (macOS) and runs the bridge in the background - no terminal window. Its
menu shows whether the controller, touchscreen and MA (or your OSC target) are
there, switches profiles, and opens the littlelx window:

* **Status** - controller, touchscreen, profile, where it sends to, the link.
* **Profiles** - make one active (it's stored on the controller too), edit in
  the Builder, new, duplicate, rename, delete, import / export, open the folder,
  and **Restore examples** (writes the shipped example profiles again - e.g.
  after an update brings newer ones; missing examples are added by themselves).
* **Builder** - edit a profile with forms: connection, faders, the 20 keys
  (laid out like the controller), the touchscreen's buttons, encoders, your own
  screens and keypad, the look (top bar, colours) and feedback. The JSON tab
  shows the whole file for anything else. **Open** picks any profile to edit,
  **Save as copy** saves it under a new name, **Save & make active** uses it
  straight away.
* **Firmware** - one **Install** for everything: the app carries a firmware
  package (`.lxfw`) with the controller (Mega) and touchscreen firmware of its
  own version; Install shows what's installed and updates only what's out of
  date, the controller first (the touchscreen is updated through it). **Install
  from file** takes another `.lxfw`; single parts (a Mega .hex, a touchscreen
  .zip) are under Advanced. Modules will join the same package.
* **Calibration** - fader bottom / top (live readings) and the touchscreen.
* **Log** - what the bridge reports (also in `littlelx.log` in your home folder).

Closing the window keeps it running; **Quit** is in the tray / menu bar menu.

**Get it:** the GitHub build (Actions) makes `littlelx-app.exe` (Windows, in
`littlelx-windows`) and `littlelx.app` (macOS, `littlelx-mac.zip`). It isn't
signed with an Apple developer account, so macOS won't open it by itself the
first time: right-click it and choose Open, or after a refused open go to
System Settings > Privacy & Security and click "Open Anyway". If macOS calls it
"damaged", run `xattr -cr littlelx.app` in Terminal (in the folder it's in) and
open it again. From the source:

    pip install pyside6 pyserial
    python3 bridge/app.py

The command-line bridge (`littlelx.py`) still works on its own; don't run both
at once. Learning the wiring is still done with `littlelx.py --learn`.

## Updating the touchscreen over USB

No need to take the SD card out. Stop the bridge if it's running, then:

```sh
python3 bridge/littlelx.py --update-pi littlelx-pi-update.zip   # Windows: littlelx.exe --update-pi ...
```

`littlelx-pi-update.zip` comes from the same build as the SD image (GitHub
Actions artifact *littlelx-pi-update*, or `pi/out/` when building yourself).
Only changed files are sent: an app-only update takes a few seconds, a kernel
update a few minutes. The screen shows the progress, and the bridge prints the
old and new version.

How it stays safe: the card holds two copies of the OS, `a/` and `b/`.
`config.txt` picks one (`os_prefix=a/`). The update writes the *other* copy,
checks every file's CRC, and only then changes that single letter and reboots.
If the power or USB goes during an update, the old copy is untouched and still
boots. Just run the update again. If a new version itself turns out to be
broken, put the card in a computer and change `os_prefix=` back to the other
letter.

## Profiles: everything the controller does, as one JSON file

A **profile** describes the whole controller: where it talks to (grandMA3, or
plain OSC to anything else), what each fader, key and encoder does, the
touchscreen's main buttons and its keypad. Profiles are plain files in the
`littlelx-profiles` folder in your home folder - edit them in any text editor,
copy them, share them. Two examples are put there (when missing):
`grandMA3.json` (the default layout) and `Generic OSC.json`.

The **active** profile is also stored on the controller (next to the learned
wiring), so it travels with it: plug it into another computer and it brings
its profile along.

    python3 bridge/littlelx.py --profiles               # list them (* = active)
    python3 bridge/littlelx.py --use-profile "Generic OSC"   # activate + store on the controller

(stop the bridge first; it uses the new profile when it starts again). Changes
made on the touchscreen (Setup) are saved into the active profile's file and
on the controller.

```
{
  "name": "My show",
  "connection": {
    "type": "ma3",              // or "generic": plain OSC to anything else
    "host": "192.168.1.20", "port": 8000, "prefix": "/gma3", "listen_port": 9000,
    "fader_interval": 0.025,    // s between fader messages; "fader_jump": 5 (% sent at once)
    "values": {                 // generic: what is sent (fader feedback is read the same way)
      "fader": [0, 1],          //   bottom, top (e.g. [0, 127])
      "key": [1, 0], "button": [1, 0], "push": [1, 0],   // pressed, released
      "encoder": 1,             //   per click
      "integer": false          //   whole numbers
    },
    "generic": {                // generic: where things are sent ({n} = number)
      "fader": "/fader/{n}",    //   level 0..1
      "key": "/key/{n}",        //   1 pressed, 0 released
      "encoder": "/encoder/{n}",//   +clicks / -clicks
      "push": "/encoder/{n}/push",
      "button": "/button/{n}",  //   touchscreen buttons
      "page": "/page",          //   page number (Page -/+)
      "command": "/command",    //   the keypad's command line, on Please
      "set": "/encoder/{n}/set" //   a value set on an encoder's value page
    }
  },
  "faders": [{"exec": 201, "name": "Front", "color": "e0a020"},
             {"osc": "/my/level", "range": [0, 255], "feedback": "/my/level/state"}, ...],
  "keys": [{"page": -1}, {"exec": 301}, {"osc": "/go"}, ...],              // 20
  "encoders": [{"follow": true, "attribute": "Dimmer", "step": 1,         // ma3
                "click": "pad", "pad": true, "choices": "ma"},
               {"label": "Gobo", "value": "/gobo/display", "set": "/gobo/set",  // generic
                "click": "pad", "pad": false,
                "choices": [{"label": "Open", "value": 0}, {"label": "Star", "value": 1}]}],
  "touch_buttons": [{"label": "Go", "osc": "/go", "lit": "/go/running",
                     "color": "1f7a3a", "lit_color": "c03030", "text_color": "ffffff"},
                    {"label": "Keypad", "screen": "keypad"}, ...],
  "keypad": [["Fixture", "7", "8", "9", "Thru"], ...],                     // 5 rows of 5
  "screens": {                  // your own screens; open one with {"screen": "name"}
    "effects": {"title": "Effects", "columns": 2, "back": "main",
                "buttons": [{"label": "Strobe", "osc": "/fx/strobe", "lit": "/fx/strobe/on"}, ...]},
    "keypad": {"type": "keypad", "keys": [   // replaces the built-in keypad
      ["1", "2", "3", {"label": "Enter", "cmdline": "please"}],
      ["4", "5", "6", {"label": "Del", "cmdline": "backspace"}],
      [{"label": "Back", "screen": "main"}, "0", ".", {"label": "Go", "osc": "/go"}]]}
  },
  "screen": {                   // what the touchscreen shows (all optional)
    "left": "Page {page}",      //   top bar left: text, {page} / {profile}
    "middle": "cmdline",        //   "cmdline" (MA), "sent" (last OSC sent),
                                //   "feedback" (from the "text" address) or text
    "status": "ma",             //   "ma" (MA3 linked), "osc" (where to; green while
                                //   OSC comes back), "feedback" or text
    "encoders": "ma",           //   "ma" (follow MA's encoders) or "simple"
    "encoders_title": "Encoders",
    "encoder_strip": true,      //   the small encoder line on main / keypad
    "columns": null,            //   main page button grid (null: fits the number of buttons)
    "colors": {"background": "101418", "panel": "1c2430", "text": "ffffff",
               "dim": "8899aa", "button": "2c3546", "button_lit": "c08a1e",
               "key": "3a4458", "bar": "2f7de1", "bar_background": "232b38",
               "command": "ffd36b", "ok": "38c172", "waiting": "5a6578",
               "programmer": "ff4b3e", "value": "a4acb8", "strip": "161d28"}
  },
  "feedback": {                 // generic: OSC coming back (to listen_port)
    "fader": "/fader/{n}",      //   level 0..1: bar + catch-up arrows
    "fader_name": "/fader/{n}/name", "fader_color": "/fader/{n}/color",
    "button": "/button/{n}/state",   //   lit when > 0 / "on"
    "button_label": "/button/{n}/label",
    "encoder_label": "/encoder/{n}/label", "encoder_value": "/encoder/{n}/value",
    "text": "/text", "status": "/status", "page": "/page"
  }
}
```

Leave out what you don't need: a profile without `"screen"` gets the defaults
for its connection type (grandMA3: MA's command line, "MA3 linked", MA's
encoders; generic: the profile name, the last OSC sent, the OSC target, simple
encoders), and a generic profile without `"feedback"` listens on the addresses
above. With grandMA3 the plugin reports MA's state; a `"feedback"` section there
adds to it.

Actions (keys, touchscreen buttons, keypad keys, encoder pushes): `{"exec": 201}`
(executor button), `{"key": "Store"}` (command-line key), `{"cmd": "Go+"}`
(command right away; generic: to the command address), `{"page": 1}`,
`{"screen": "keypad"}` (any screen, also your own), `{"resolution": "Dimmer"}`,
`{"cmdline": "please" | "clear" | "backspace"}` (keypad special keys), and
`{"osc": "/any/address"}` (sends 1 on press, 0 on release, or the item's own
`"on"` / `"off"` - with any connection). Keypad keys that are plain text are
typed into the command line.

Encoder clicks: `"click"` is `"pad"` (a value page: number pad if `"pad"`, and
`"choices"`: `"ma"` = MA's named values of the attribute, or your own list),
`"push"` (its push action / push address) or `"none"`. Generic profiles send
the value to the encoder's `"set"` address. The main page's buttons arrange
themselves in a grid that suits how many there are (up to 18). A fader with `"osc"` sends its level 0..1
there. In a `generic` profile, keys and buttons without their own `"osc"` send
to the `generic` addresses (Page and screen buttons still work locally).
Colours: fader colours come from feedback, then MA's sequence colour, then the
profile's `"color"`.

## Default layout (all of it is in the active profile, below)

| Control | Does |
|---------|------|
| Faders 1–5 | Executor faders 201–205 on the current page |
| Keys 1–5 | Page − / Page + / Clear / Oops / Please |
| Keys 6–10 | Executor 301–305 buttons |
| Keys 11–15 | Executor 201–205 buttons |
| Keys 16–20 | Executor 101–105 buttons |
| Encoder 1 | Dimmer ± 1 per click (± 0.1 when MA's Dimmer encoder resolution is Fine); push = toggle Coarse/Fine in MA |
| Encoder 2 | Page −/+; push = Keypad screen |
| Touchscreen | Main page: the 5 faders in their MA sequence colours (MA level + physical position, names); Page −/+, Clear, Highlight, Last, Next, Blind, Keypad, Encoders. Keypad page: a full command keypad. Encoders page: follows MA's encoders (below) |

**Changing what a key does:** tap **Setup** at the top of the touchscreen, press the
key on the controller (or tap it in the list) and pick a function, or
**Executor...** to type an executor number. Saved straight away, on the
controller too, so the keys travel with it to any computer.

Actions are one of `{"exec": 201}`, `{"key": "Store"}` (a console key on the command
line; `"Please"`, `"Clear"` and `"<-"` act on the line), `{"cmd": "Go+"}`
(a command run right away), `{"page": 1}`, `{"screen": "keypad"}` or
`{"resolution": "Dimmer"}` (toggle MA's Coarse/Fine for that attribute). Encoders
take `{"attribute": "Pan", "step": 1}` (follows MA's Coarse/Fine), `{"page": 1}` or
a `{"cmd": ...}` using `{d}` for the step.

**Encoders follow MA's encoders**: the selected feature's attributes for the
selected fixture, two at a time (the **1/2** button on the Encoders page swaps
pairs). The page shows each attribute's value in MA's colours (red = in the
programmer) and its Coarse/Fine (tap to toggle). **Click an encoder** (or tap its
value) to set its attribute: a number pad (then Set) and, for attributes with
named values (gobos, colour slots...), above it a list of them read from the
selected fixture's type: tap one to use it, or turn the encoder to highlight
one and click it to use it. On the number pad, clicking the encoder is Set. Values inside a
named range show as its name, like MA's encoder bar. With
nothing selected in MA they fall back to what's set in the config (Dimmer and
Page). If the page stays on
"Select fixtures in MA" although fixtures are selected (or values look wrong),
tap **Setup > MA probe** on the touchscreen (or run `littlelx.py --ma-probe` with
the bridge stopped): it saves what MA's Lua offers to `littlelx-probe.txt` in
your home folder - send that file.

Encoders are counted on the computer, and a click only counts once the knob
settles into its detent (wiggling does nothing). If an encoder needs two clicks
per step, the bridge notices within a few clicks, fixes it and saves it on the
controller.

Faders send at most `"fader_interval"` apart (0.025 s = 40 updates a second per
fader, in the `osc` section): slow moves still go out every 1 %, faster moves
in bigger steps, always ending on the exact final position. A move of
`"fader_jump"` % (5) goes out at once, so even a fast throw is about 20 steps. MA falls behind when
flooded with one message per percent.

**Faders not moving in MA3?** MA3 versions differ in the fader message they
accept. Run `littlelx.py --test-faders`: it moves executor 201 using each known
format in turn, asks you which one worked, and remembers it. To see every
fader message the bridge sends, run it with `--verbose`.

`littlelx.py --monitor` prints every raw event from the Mega and the Pi.

## Building the Pi image yourself

On Debian or Ubuntu (or let GitHub Actions do it, see `.github/workflows/build.yml`):

```sh
sudo apt install gcc-arm-linux-gnueabihf libc6-dev-armhf-cross make flex bison bc \
                 libssl-dev dosfstools mtools fdisk git curl
./pi/build.sh        # -> pi/out/littlelx-sdcard.img(.gz) + littlelx-pi-update.zip
```

This builds the Raspberry Pi kernel (`rpi-6.12.y`) with `pi/kernel.fragment`,
packs the statically linked `pi/app` as `/init` into `littlelx.cpio.gz`, and
adds the Pi firmware (cut-down `start_cd.elf`) plus `pi/boot/config.txt`.

The panel app also builds and runs on a normal Linux PC for testing:
`cc -O2 -o llx pi/app/littlelx.c && ./llx --render out.ppm 320x480 < script.txt`
renders a list of protocol lines to an image. To boot-test the image in QEMU
(no panel there, but you'll see the app start and greet the Mega on the UART):
`qemu-system-arm -M raspi2b -kernel zImage -dtb bcm2709-rpi-2-b.dtb -append LLX_NOWDOG=1 -serial stdio`.
`LLX_NOWDOG=1` is needed because QEMU resets the moment the watchdog is armed.

## Protocols

* Mega ⇄ bridge: see the header of `mega/littlelx_mega/littlelx_mega.ino`
* Pi ⇄ bridge (tunnelled through the Mega as lines starting with `>`): see the
  header of `pi/app/littlelx.c`, including the `UPD` update commands. Lines
  carry a checksum, and the bridge re-sends the whole screen in the
  background, so a damaged line can't leave a gap on the screen.
* MA3 → bridge: `/littlelx/...` OSC messages, see `ma3/littlelx.lua`
* Speeds: USB 500000 baud, Mega ⇄ Pi 500000 baud
