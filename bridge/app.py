"""littlelx app: the bridge in the system tray (Windows) / menu bar (macOS).

Runs the bridge in the background and adds a window with its status, the
profiles and their Builder, touchscreen updates, calibration and the log.

    pip install pyside6 pyserial
    python3 bridge/app.py

(The command-line bridge, littlelx.py, still works on its own.)
"""
import collections
import copy
import io
import os
import re
import shutil
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QTimer, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSystemTrayIcon,
    QTabWidget, QVBoxLayout, QWidget,
)

import littlelx as lx  # noqa: E402
from app_builder import Builder  # noqa: E402

LOG_PATH = os.path.join(os.path.expanduser("~"), "littlelx.log")
LOG = None  # the app's Log (progress of updates)


class Log(io.TextIOBase):
    """Takes what the bridge prints: kept for the Log tab, written to
    ~/littlelx.log, and still shown in a terminal if there is one."""

    def __init__(self, echo):
        super().__init__()
        self.echo = echo
        self.lines = collections.deque(maxlen=3000)
        self.partial = ""
        self.count = 0  # lines ever added (the Log tab follows this)
        self.lock = threading.Lock()
        try:
            self.file = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
        except OSError:
            self.file = None
        self.progress = None  # last "NN%" seen (touchscreen update)

    def write(self, s):
        if self.echo:
            try:
                self.echo.write(s)
                self.echo.flush()
            except Exception:
                pass
        with self.lock:
            for ch in re.split(r"(\r|\n)", s):
                if ch == "\n":
                    self._add(self.partial)
                    self.partial = ""
                elif ch == "\r":
                    if self.partial.strip():
                        self._add(self.partial)
                    self.partial = ""
                else:
                    self.partial += ch
        return len(s)

    def _add(self, line):
        line = line.rstrip()
        if not line:
            return
        m = re.match(r"\s*(\d{1,3})%", line)
        if m:
            self.progress = int(m.group(1))
        self.lines.append(time.strftime("%H:%M:%S  ") + line)
        self.count += 1
        if self.file:
            self.file.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + line + "\n")

    def flush(self):
        pass

    def tail(self, since):
        """Lines added after `since` (a count), and the new count."""
        with self.lock:
            n = self.count - since
            return list(self.lines)[-n:] if 0 < n <= len(self.lines) else (list(self.lines) if n > 0 else []), \
                self.count


class Runner:
    """The bridge, on its own thread; the window asks it for things."""

    def __init__(self):
        self.cfg = lx.load_config()
        self.bridge = lx.Bridge(self.cfg)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.updating = False

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            self.bridge.run()
        except Exception:
            print("The bridge stopped:\n" + traceback.format_exc())

    def quit(self):
        self.bridge.quit.set()
        self.thread.join(2)

    def status(self):
        b = self.bridge
        ser = b.ser
        o = self.cfg["osc"]
        return {
            "controller": getattr(ser, "path", None) if ser else None,
            "touchscreen": b.pi_version if b.pi_ready else None,
            "generic": b.generic(),
            "ma": b.ma_linked(),
            "ma_seen": bool(b.ma_seen and time.time() - b.ma_seen < 5),
            "profile": self.cfg.get("profile", ""),
            "target": f"{o.get('host')}:{o.get('port')}",
            "page": b.page,
            "updating": self.updating,
        }

    def activate(self, prof):
        """Make a profile the active one: live, saved, and on the controller."""
        def do():
            lx.apply_profile(self.cfg, prof)
            self.bridge.open_osc()
            self.bridge.build_maps()
            lx.save_active(self.cfg, self.bridge.ser, keep=True)
            if self.bridge.pi_ready:
                self.bridge.screen.set_screen("main")
            print(f"Profile '{self.cfg['profile']}' is active.")
        self.bridge.call(do)

    def calibrate_touch(self):
        self.bridge.call(lambda: self.bridge.to_pi("CAL"))

    def save_fader_cal(self, lows, highs):
        """lows / highs: Mega channel -> raw reading. Returns problems (text)."""
        problems = []
        hw = self.cfg["hw"]
        new = copy.deepcopy(hw["faders"])
        for i, f in enumerate(new):
            if not f:
                continue
            lo, hi = lows.get(f["ch"]), highs.get(f["ch"])
            if lo is None or hi is None or abs(hi - lo) < 300:
                problems.append(f"Fader {i + 1}: didn't move far enough (bottom {lo}, top {hi}) - unchanged")
                continue
            margin = (hi - lo) * 0.015  # reach 0 / 100 % without slamming the ends
            f["lo"], f["hi"] = round(lo + margin), round(hi - margin)

        def do():
            hw["faders"] = new
            lx.save_config(self.cfg)
            if self.bridge.ser:
                lx.save_to_mega(self.cfg, self.bridge.ser, keep=True)
            print("Fader calibration saved.")
        self.bridge.call(do)
        return problems

    def flash_mega(self, path, done):
        """Mega firmware (path None = the bundled one): the bridge lets go of the
        port meanwhile, then finds the controller again."""
        def work():
            self.updating = True
            err = None
            b = self.bridge
            port = b.ser.path if b.ser else b.last_port
            b.hold.set()
            if not b.held.wait(15):
                err = "The bridge didn't let go of the controller."
            else:
                time.sleep(0.5)  # the port is really closed
                try:
                    lx.flash_mega(self.cfg, path, port, progress=lambda p: setattr(LOG, "progress", p))
                except Exception as e:
                    err = f"Flashing failed: {e}. Nothing is lost - just flash again."
                    print(err)
            b.hold.clear()
            self.updating = False
            done(err)
        threading.Thread(target=work, daemon=True).start()

    def update_pi(self, path, done):
        """Touchscreen update: the bridge lets go of the port meanwhile."""
        def work():
            self.updating = True
            err = None
            b = self.bridge
            b.hold.set()
            if not b.held.wait(15):
                err = "The bridge didn't let go of the controller."
            else:
                try:
                    lx.update_pi(self.cfg, path)
                except SystemExit as e:
                    err = str(e) if e.code not in (None, 0) else None
                except Exception as e:
                    err = f"{e}"
            b.hold.clear()
            self.updating = False
            done(err)
        threading.Thread(target=work, daemon=True).start()


def make_icon(dark=False):
    """Five little fader bars (no image files needed)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    col = QColor("#000000" if dark else "#2f7de1")
    for n, h in enumerate((40, 26, 50, 32, 44)):
        p.setBrush(col)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4 + n * 12, 60 - h, 8, h, 3, 3)
    p.end()
    icon = QIcon(pm)
    if dark:
        icon.setIsMask(True)  # macOS menu bar: follows light / dark mode
    return icon


# ------------------------------------------------------------------ the window

class StatusTab(QWidget):
    def __init__(self, runner):
        super().__init__()
        self.runner = runner
        f = QFormLayout(self)
        big = QFont()
        big.setPointSize(big.pointSize() + 2)
        self.labels = {}
        for k, name in (("version", "Bridge"), ("controller", "Controller"), ("touchscreen", "Touchscreen"),
                        ("profile", "Profile"), ("target", "Sends to"), ("link", "Link"), ("page", "Page")):
            lab = QLabel()
            lab.setFont(big)
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            f.addRow(name, lab)
            self.labels[k] = lab
        self.labels["version"].setText(lx.bridge_version())

    def refresh(self):
        s = self.runner.status()
        ok = lambda t: f"<span style='color:#2a9d4a'>{t}</span>"  # noqa: E731
        bad = lambda t: f"<span style='color:#c0392b'>{t}</span>"  # noqa: E731
        self.labels["controller"].setText(ok(f"connected ({s['controller']})") if s["controller"]
                                          else bad("not connected"))
        self.labels["touchscreen"].setText(ok(f"connected, firmware {s['touchscreen']}") if s["touchscreen"]
                                           else bad("not found"))
        self.labels["profile"].setText(s["profile"])
        self.labels["target"].setText(s["target"])
        if s["generic"]:
            self.labels["link"].setText(ok("OSC coming back") if s["ma_seen"] else "no OSC coming back")
        else:
            self.labels["link"].setText(ok("MA3 linked") if s["ma"] else ok("MA3 online") if s["ma_seen"]
                                        else bad("MA3 not answering"))
        self.labels["page"].setText(str(s["page"]))


class ProfilesTab(QWidget):
    def __init__(self, runner, edit):
        super().__init__()
        self.runner, self.edit = runner, edit
        lay = QHBoxLayout(self)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.do_edit())
        lay.addWidget(self.list, 1)
        col = QVBoxLayout()
        for name, fn in (("Make active", self.activate), ("Edit in Builder", self.do_edit), ("New", self.new),
                         ("Duplicate", self.duplicate), ("Rename", self.rename), ("Delete", self.delete),
                         ("Import...", self.import_), ("Export...", self.export), ("Open folder", self.folder),
                         ("Restore examples...", self.examples)):
            b = QPushButton(name)
            b.clicked.connect(fn)
            col.addWidget(b)
        col.addStretch()
        note = QLabel("The active profile is also stored on the controller,\nso it travels with it.")
        note.setStyleSheet("color: gray")
        col.addWidget(note)
        lay.addLayout(col)
        self.refresh()

    def refresh(self):
        cur = self.selected()
        self.list.clear()
        active = self.runner.cfg.get("profile")
        for name in lx.list_profiles():
            self.list.addItem(("●  " if name == active else "    ") + name)
        names = lx.list_profiles()
        if cur in names:
            self.list.setCurrentRow(names.index(cur))

    def selected(self):
        item = self.list.currentItem()
        return item.text()[3:] if item else None

    def load(self, name):
        try:
            return lx.load_profile(name)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "littlelx", f"Can't read the profile '{name}':\n{e}")
            return None

    def activate(self):
        name = self.selected()
        p = name and self.load(name)
        if p:
            p["name"] = p.get("name") or name
            self.runner.activate(p)
            QTimer.singleShot(400, self.refresh)

    def do_edit(self):
        name = self.selected()
        p = name and self.load(name)
        if p:
            self.edit(p)

    def ask(self, title, default=""):
        name, ok = QInputDialog.getText(self, "littlelx", title, text=default)
        name = name.strip()
        if ok and name and name in lx.list_profiles():
            QMessageBox.information(self, "littlelx", f"There is already a profile '{name}'.")
            return None
        return name if ok and name else None

    def new(self):
        name = self.ask("Name of the new profile")
        if name:
            cfg = copy.deepcopy(lx.DEFAULTS)
            cfg["profile"] = name
            lx.save_profile(lx.profile_from_cfg(cfg))
            self.refresh()

    def duplicate(self):
        src = self.selected()
        p = src and self.load(src)
        if p:
            name = self.ask("Name of the copy", src + " copy")
            if name:
                p["name"] = name
                lx.save_profile(p)
                self.refresh()

    def rename(self):
        src = self.selected()
        p = src and self.load(src)
        if p:
            name = self.ask("New name", src)
            if name:
                p["name"] = name
                lx.save_profile(p)
                os.remove(lx.profile_path(src))
                if self.runner.cfg.get("profile") == src:
                    self.runner.activate(p)
                QTimer.singleShot(400, self.refresh)

    def delete(self):
        name = self.selected()
        if not name:
            return
        if name == self.runner.cfg.get("profile"):
            QMessageBox.information(self, "littlelx", "That's the active profile: make another one active first.")
            return
        if QMessageBox.question(self, "littlelx", f"Delete the profile '{name}'?") == QMessageBox.Yes:
            os.remove(lx.profile_path(name))
            self.refresh()

    def import_(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import a profile", "", "Profiles (*.json)")
        if not path:
            return
        try:
            import json
            with open(path) as f:
                p = json.load(f)
            if not isinstance(p, dict):
                raise ValueError("not a profile")
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "littlelx", f"Can't import {path}:\n{e}")
            return
        p["name"] = p.get("name") or os.path.splitext(os.path.basename(path))[0]
        lx.save_profile(p)
        self.refresh()

    def export(self):
        name = self.selected()
        if not name:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export the profile", name + ".json", "Profiles (*.json)")
        if path:
            shutil.copyfile(lx.profile_path(name), path)

    def examples(self):
        names = [os.path.basename(p)[:-5] for p in lx.example_profiles()]
        if QMessageBox.question(self, "littlelx", "Write the example profiles (" + ", ".join(names) +
                                ") again? Your own changes to those files are replaced.") != QMessageBox.Yes:
            return
        lx.install_example_profiles(overwrite=True)
        if self.runner.cfg.get("profile") in names:  # the active one: use the restored version
            self.runner.activate(lx.load_profile(self.runner.cfg["profile"]))
        QTimer.singleShot(400, self.refresh)

    def folder(self):
        os.makedirs(lx.PROFILE_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(lx.PROFILE_DIR))


class FirmwareTab(QWidget):
    def __init__(self, runner, log):
        super().__init__()
        self.runner, self.log = runner, log
        lay = QVBoxLayout(self)
        box = QGroupBox("Touchscreen (Raspberry Pi)")
        b = QVBoxLayout(box)
        self.version = QLabel()
        b.addWidget(self.version)
        b.addWidget(QLabel("Update it over USB with a littlelx-pi-update.zip (from the GitHub build). "
                           "The old version stays until the new one is complete, so it's safe to unplug."))
        row = QHBoxLayout()
        self.button = QPushButton("Update from file...")
        self.button.clicked.connect(self.update)
        row.addWidget(self.button)
        row.addStretch()
        b.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        b.addWidget(self.bar)
        self.msg = QLabel()
        b.addWidget(self.msg)
        lay.addWidget(box)
        mega = QGroupBox("Controller (Arduino Mega)")
        m = QVBoxLayout(mega)
        bundled = lx.mega_firmware()
        info = QLabel("Flash its firmware over USB. Only the program is replaced: the learned wiring and "
                      "the active profile stay on the controller. "
                      + ("This app carries the firmware that matches it." if bundled else
                         "This copy has no firmware built in: use a littlelx_mega .hex from the GitHub build."))
        info.setWordWrap(True)
        m.addWidget(info)
        mrow = QHBoxLayout()
        self.mega_builtin = QPushButton("Flash the built-in firmware")
        self.mega_builtin.setEnabled(bool(bundled))
        self.mega_builtin.clicked.connect(lambda: self.flash(None))
        self.mega_file = QPushButton("Flash from file...")
        self.mega_file.clicked.connect(self.flash_file)
        mrow.addWidget(self.mega_builtin)
        mrow.addWidget(self.mega_file)
        mrow.addStretch()
        m.addLayout(mrow)
        self.mbar = QProgressBar()
        self.mbar.setVisible(False)
        m.addWidget(self.mbar)
        self.mmsg = QLabel()
        self.mmsg.setWordWrap(True)
        m.addWidget(self.mmsg)
        lay.addWidget(mega)
        ma = QGroupBox("grandMA3 plugin")
        a = QVBoxLayout(ma)
        a.addWidget(QLabel(f"The bridge puts its code into MA by itself (version {runner.bridge.ma_code_version()})."))
        again = QPushButton("Install into MA again")
        again.clicked.connect(lambda: runner.bridge.call(runner.bridge.install_ma3))
        a.addWidget(again, 0, Qt.AlignLeft)
        lay.addWidget(ma)
        lay.addStretch()

    def refresh(self):
        s = self.runner.status()
        self.version.setText(f"Firmware: {s['touchscreen'] or 'not connected'}")
        if self.runner.updating:
            self.bar.setValue(self.log.progress or 0)
            self.mbar.setValue(self.log.progress or 0)

    def flash_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Mega firmware", "", "Arduino firmware (*.hex)")
        if path:
            self.flash(path)

    def flash(self, path):
        if self.runner.updating:
            return
        if QMessageBox.question(self, "littlelx", "Flash the controller's firmware now? Keep it plugged in "
                                "until it's done (about 10 seconds).") != QMessageBox.Yes:
            return
        for b in (self.mega_builtin, self.mega_file, self.button):
            b.setEnabled(False)
        self.mbar.setVisible(True)
        self.mbar.setValue(0)
        self.log.progress = 0
        self.mmsg.setText("Flashing...")

        def done(err):
            QTimer.singleShot(0, lambda: self.flashed(err))
        self.runner.flash_mega(path, done)

    def flashed(self, err):
        self.mega_builtin.setEnabled(bool(lx.mega_firmware()))
        self.mega_file.setEnabled(True)
        self.button.setEnabled(True)
        self.mbar.setVisible(False)
        self.mmsg.setText(f"<span style='color:#c0392b'>{err}</span>" if err else
                          "Flashed and verified. The controller restarts and the bridge reconnects.")

    def update(self):
        path, _ = QFileDialog.getOpenFileName(self, "Touchscreen update", "", "littlelx update (*.zip)")
        if not path:
            return
        self.button.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.log.progress = 0
        self.msg.setText("Updating... (keep the controller plugged in)")

        def done(err):
            QTimer.singleShot(0, lambda: self.finished(err))
        self.runner.update_pi(path, done)

    def finished(self, err):
        self.button.setEnabled(True)
        self.bar.setVisible(False)
        self.msg.setText(f"<span style='color:#c0392b'>{err}</span>" if err else "Updated.")


class CalibrationTab(QWidget):
    def __init__(self, runner):
        super().__init__()
        self.runner = runner
        self.lows, self.highs = {}, {}
        lay = QVBoxLayout(self)
        box = QGroupBox("Faders: the real bottom and top of each")
        g = QGridLayout(box)
        self.bars = []
        for i in range(5):
            g.addWidget(QLabel(f"Fader {i + 1}"), i, 0)
            bar = QProgressBar()
            bar.setRange(0, 1023)
            bar.setFormat("%v")
            g.addWidget(bar, i, 1)
            self.bars.append(bar)
        steps = QHBoxLayout()
        for name, fn in (("1. All faders down: record bottom", self.bottom),
                         ("2. All faders up: record top", self.top), ("3. Save", self.save)):
            b = QPushButton(name)
            b.clicked.connect(fn)
            steps.addWidget(b)
        g.addLayout(steps, 5, 0, 1, 2)
        self.fmsg = QLabel()
        g.addWidget(self.fmsg, 6, 0, 1, 2)
        lay.addWidget(box)
        tbox = QGroupBox("Touchscreen")
        t = QHBoxLayout(tbox)
        cal = QPushButton("Calibrate the touchscreen")
        cal.clicked.connect(self.touch)
        t.addWidget(cal)
        t.addWidget(QLabel("then tap the three crosses on the touchscreen"))
        t.addStretch()
        lay.addWidget(tbox)
        lay.addWidget(QLabel("Learning the wiring is still done in a terminal:  littlelx.py --learn"))
        lay.addStretch()

    def channels(self):
        return [f["ch"] if f else None for f in self.runner.cfg["hw"]["faders"]]

    def refresh(self):
        raw = self.runner.bridge.raw_analog
        for i, ch in enumerate(self.channels()):
            self.bars[i].setValue(raw.get(ch, 0) if ch is not None else 0)
            self.bars[i].setEnabled(ch is not None)

    def bottom(self):
        self.lows = dict(self.runner.bridge.raw_analog)
        self.fmsg.setText("Bottom recorded. Now push all faders fully up and record the top.")

    def top(self):
        self.highs = dict(self.runner.bridge.raw_analog)
        self.fmsg.setText("Top recorded. Save to use it.")

    def save(self):
        if not self.lows or not self.highs:
            self.fmsg.setText("Record the bottom and the top first.")
            return
        problems = self.runner.save_fader_cal(self.lows, self.highs)
        self.fmsg.setText("\n".join(problems) if problems else "Saved (also on the controller).")

    def touch(self):
        self.runner.calibrate_touch()


class LogTab(QWidget):
    def __init__(self, log):
        super().__init__()
        self.log, self.seen = log, 0
        lay = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(3000)
        mono = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.text.setFont(mono)
        lay.addWidget(self.text)
        row = QHBoxLayout()
        verbose = QPushButton("Log every fader message")
        verbose.setCheckable(True)
        verbose.toggled.connect(self.set_verbose)
        self.verbose_btn = verbose
        row.addWidget(verbose)
        row.addStretch()
        open_file = QPushButton("Open the log file")
        open_file.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(LOG_PATH)))
        row.addWidget(open_file)
        lay.addLayout(row)
        self.bridge = None

    def set_verbose(self, on):
        if self.bridge:
            self.bridge.verbose = on

    def refresh(self):
        lines, self.seen = self.log.tail(self.seen)
        for line in lines:
            self.text.appendPlainText(line)


class Window(QMainWindow):
    def __init__(self, runner, log):
        super().__init__()
        self.runner = runner
        self.setWindowTitle("littlelx")
        self.setWindowIcon(make_icon())
        self.resize(900, 640)
        self.tabs = QTabWidget()
        self.status = StatusTab(runner)
        self.builder = Builder(self.saved)
        self.profiles = ProfilesTab(runner, self.edit)
        self.firmware = FirmwareTab(runner, log)
        self.calibration = CalibrationTab(runner)
        self.logtab = LogTab(log)
        self.logtab.bridge = runner.bridge
        for name, w in (("Status", self.status), ("Profiles", self.profiles), ("Builder", self.builder),
                        ("Firmware", self.firmware), ("Calibration", self.calibration), ("Log", self.logtab)):
            self.tabs.addTab(w, name)
        self.setCentralWidget(self.tabs)
        try:
            self.builder.load(lx.load_profile(runner.cfg.get("profile")))
        except (OSError, ValueError):
            self.builder.load(lx.profile_from_cfg(runner.cfg))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)

    def refresh(self):
        w = self.tabs.currentWidget()
        self.logtab.refresh()  # keep collecting even when not shown
        if w is self.status:
            self.status.refresh()
        elif w is self.firmware:
            self.firmware.refresh()
        elif w is self.calibration:
            self.calibration.refresh()

    def edit(self, prof):
        self.builder.load(prof)
        self.tabs.setCurrentWidget(self.builder)

    def saved(self, prof, activate):
        old = self.builder.prof.get("name")
        lx.save_profile(prof)
        if old and old != prof["name"] and os.path.exists(lx.profile_path(old)) and \
                QMessageBox.question(self, "littlelx", f"Renamed: delete the old profile file '{old}'?") == \
                QMessageBox.Yes and old != self.runner.cfg.get("profile"):
            os.remove(lx.profile_path(old))
        self.builder.mark_saved(prof)
        if activate or prof["name"] == self.runner.cfg.get("profile"):
            self.runner.activate(prof)  # the active one changed: use it now
        self.statusBar().showMessage(f"Saved '{prof['name']}'" + (" and made it active" if activate else ""), 4000)
        QTimer.singleShot(400, self.profiles.refresh)

    def show_tab(self, name):
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == name:
                self.tabs.setCurrentIndex(i)
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, ev):  # closing the window keeps the bridge running in the tray
        if QSystemTrayIcon.isSystemTrayAvailable():
            ev.ignore()
            self.hide()


class Tray(QSystemTrayIcon):
    def __init__(self, runner, window):
        super().__init__(make_icon(dark=sys.platform == "darwin"))
        self.runner, self.window = runner, window
        self.menu = QMenu()
        self.lines = [self.menu.addAction("") for _ in range(3)]
        for a in self.lines:
            a.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction("Open littlelx...", lambda: window.show_tab("Status"))
        self.menu.addAction("Builder...", lambda: window.show_tab("Builder"))
        self.profiles = self.menu.addMenu("Profile")
        self.profiles.aboutToShow.connect(self.fill_profiles)
        self.menu.addSeparator()
        self.menu.addAction("Quit littlelx", QApplication.quit)
        self.setContextMenu(self.menu)
        self.activated.connect(self.clicked)
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def clicked(self, reason):
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger) and sys.platform != "darwin":
            self.window.show_tab("Status")

    def fill_profiles(self):
        self.profiles.clear()
        group = QActionGroup(self.profiles)
        for name in lx.list_profiles():
            a = QAction(name, self.profiles, checkable=True)
            a.setChecked(name == self.runner.cfg.get("profile"))
            a.triggered.connect(lambda _=False, n=name: self.use(n))
            group.addAction(a)
            self.profiles.addAction(a)

    def use(self, name):
        try:
            p = lx.load_profile(name)
        except (OSError, ValueError):
            return
        p["name"] = p.get("name") or name
        self.runner.activate(p)

    def refresh(self):
        s = self.runner.status()
        link = ("OSC coming back" if s["ma_seen"] else "no OSC coming back") if s["generic"] else \
            ("MA3 linked" if s["ma"] else "MA3 not answering")
        texts = [f"Controller: {'connected' if s['controller'] else 'not connected'}",
                 f"Touchscreen: {'connected' if s['touchscreen'] else 'not found'}",
                 f"{s['profile']}: {link}"]
        for a, t in zip(self.lines, texts):
            a.setText(t)
        self.setToolTip("littlelx\n" + "\n".join(texts))


def hide_dock_icon():
    """macOS from source: no Dock icon (the packaged .app sets LSUIElement)."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication  # pyobjc, if installed
        NSApplication.sharedApplication().setActivationPolicy_(1)  # accessory
    except Exception:
        pass


def main():
    global LOG
    log = LOG = Log(sys.__stdout__ if sys.__stdout__ and sys.__stdout__.isatty() else None)
    sys.stdout = sys.stderr = log
    print(f"littlelx app {lx.bridge_version()}")
    app = QApplication(sys.argv)
    app.setApplicationName("littlelx")
    app.setQuitOnLastWindowClosed(False)
    hide_dock_icon()
    runner = Runner()
    runner.start()
    window = Window(runner, log)
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = Tray(runner, window)
        tray.show()
        if not lx.learned(runner.cfg):  # first run: show the window
            window.show_tab("Status")
    else:
        window.show()
    app.aboutToQuit.connect(runner.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
