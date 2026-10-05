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

Open `mega/littlelx_mega/littlelx_mega.ino` in the Arduino IDE, select
**Arduino Mega or Mega 2560**, and upload. (CI also builds a `.hex`.)

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
* **MA's real command line.** The keypad and `{"key": …}` buttons type into MA's
  own command line, exactly like pressing the console keys, and the screen shows
  MA's actual command line back. What you see is always MA's syntax. (MA won't
  accept typing while a popup is open on its screen; the controller says so.)
* Without the link (e.g. no second OSC line), everything still works as
  before: commands are built on the controller and sent on Please.

### 7. Run it

```sh
python3 bridge/littlelx.py        # Windows: double-click littlelx.exe
```

Start at login: on Mac run `bridge/install-autostart.sh`. On Windows put a
shortcut to `littlelx.exe` in `shell:startup` (Win+R → `shell:startup`).

It reconnects by itself when the controller is unplugged and plugged back in.

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

## Default layout (all of it is editable in `~/.littlelx.json`)

| Control | Does |
|---------|------|
| Faders 1–5 | Executor faders 201–205 on the current page |
| Keys 1–5 | Executor 201–205 buttons |
| Keys 6–15 | Executor 101–110 buttons |
| Keys 16 / 17 | Page − / Page + |
| Keys 18 / 19 / 20 | Clear / Go+ / Oops |
| Encoder 1 | Dimmer ± 1 per click (± 0.1 when MA's Dimmer encoder resolution is Fine); push = toggle Coarse/Fine in MA |
| Encoder 2 | Page −/+; push = Keypad screen |
| Touchscreen | MA fader levels + physical position, names; Page −/+, Clear, Oops, Keypad, Go −, Pause, Go +, Highlight, Blind, Last, Next; a full command keypad |

Actions are one of `{"exec": 201}`, `{"key": "Store"}` (a console key typed into
MA's command line; `"Please"`, `"Clear"` and `"<-"` act on the line), `{"cmd": "Go+"}`
(a command run right away), `{"page": 1}`, `{"screen": "keypad"}` or
`{"resolution": "Dimmer"}` (toggle MA's Coarse/Fine for that attribute). Encoders
take `{"attribute": "Pan", "step": 1}` (follows MA's Coarse/Fine), `{"page": 1}` or
a `{"cmd": ...}` using `{d}` for the step.

Encoders are counted on the computer, and a click only counts once the knob
settles into its detent (wiggling does nothing). If an encoder needs two clicks
per step, the bridge notices within a few clicks, fixes it and saves it on the
controller.

Faders send at most `"fader_interval"` apart (0.025 s = 40 updates a second per
fader, in the `osc` section): slow moves still go out every 1 %, fast moves in
bigger steps, always ending on the exact final position. MA falls behind when
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
