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
* Keys/encoders: switch to GND (the Mega uses internal pull-ups). Faders: wipers
  on A0–A15. Any pin except 0, 1, 14 and 15 can be used. Nothing else needs to be
  known; the learn step works out which pin is which.

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

*Optional, for feedback (executor names/levels and the "MA3 online" indicator):*
add an OSC line with **Send** = Yes pointing at the bridge computer on port `9000`.

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
| Encoder 1 | `Attribute "Dimmer" At ± 2`; push = Clear |
| Encoder 2 | Page −/+; push = Keypad screen |
| Touchscreen | Fader levels and names (draggable), 12 command buttons, a full command keypad |

Actions are one of `{"exec": 201}`, `{"cmd": "Go+"}`, `{"page": 1}` or
`{"screen": "keypad"}`. Encoder commands use `{d}` for the step. Set `"pickup": true`
for soft takeover when MA3 reports fader values (useful when paging). Set
`"fader_type": "f"` if your MA3 version wants float fader values.

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
  header of `pi/app/littlelx.c`, including the `UPD` update commands
* Speeds: USB 500000 baud, Mega ⇄ Pi 500000 baud
