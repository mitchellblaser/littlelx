"""littlelx app: the profile Builder (part of app.py).

Edits a profile (see "Profiles" in littlelx.py / the README) with forms; the
JSON tab shows and edits the whole file for anything the forms don't cover.
Every editor works the same way: load(profile) fills it, collect(profile)
writes it back.
"""
import copy
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

import littlelx as lx

# ---------------------------------------------------------------- helpers


def num_or_text(t):
    """'127' -> 127, '0.5' -> 0.5, 'go' -> 'go' (OSC values in forms)."""
    t = t.strip()
    try:
        return int(t)
    except ValueError:
        try:
            return float(t)
        except ValueError:
            return t


def note(text):
    """A wrapping help text."""
    lab = QLabel(text)
    lab.setWordWrap(True)
    return lab


def text_of(v):
    return "" if v is None else str(v)


def put(d, key, value, empty=("", None)):
    """d[key] = value, or remove it when empty (keeps profiles tidy)."""
    if value in empty:
        d.pop(key, None)
    else:
        d[key] = value


def describe(act):
    """Short text for an action (lists, key grid)."""
    if not act:
        return "-"
    for k, f in (("exec", "Exec {}"), ("osc", "OSC {}"), ("cmd", "Cmd {}"), ("key", "Key {}"),
                 ("screen", "Screen {}"), ("cmdline", "Keypad {}"), ("resolution", "Coarse/Fine {}")):
        if k in act:
            return f.format(act[k])
    if "page" in act:
        return "Page +" if int(act["page"]) > 0 else "Page -"
    if "encpage" in act:
        return "Encoder pair 1/2"
    return "-"


class ColorEdit(QWidget):
    """'rrggbb' text with a swatch that opens a colour picker; empty = default."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("default")
        self.edit.setMaxLength(7)
        self.edit.setFixedWidth(90)
        self.swatch = QPushButton()
        self.swatch.setFixedSize(28, 22)
        self.swatch.clicked.connect(self.pick)
        lay.addWidget(self.edit)
        lay.addWidget(self.swatch)
        lay.addStretch()
        self.edit.textChanged.connect(self.show_swatch)
        self.show_swatch()

    def pick(self):
        c = QColorDialog.getColor(QColor("#" + (self.value() or "808080")), self, "Colour")
        if c.isValid():
            self.edit.setText(c.name()[1:])

    def show_swatch(self):
        v = self.value()
        self.swatch.setStyleSheet(f"background:#{v};" if v else "")
        self.changed.emit()

    def value(self):
        v = self.edit.text().strip().lstrip("#").lower()
        return v if len(v) == 6 and all(c in "0123456789abcdef" for c in v) else ""

    def set(self, v):
        self.edit.setText(text_of(v).lstrip("#"))


# ---------------------------------------------------------------- actions

ACTION_TYPES = [
    ("Executor button", "exec"), ("Command-line key", "key"), ("Command", "cmd"), ("OSC message", "osc"),
    ("Page - / Page +", "page"), ("Open a screen", "screen"), ("Keypad special key", "cmdline"),
    ("MA Coarse/Fine", "resolution"), ("Encoder pair 1/2", "encpage"), ("Nothing", None),
]
CMD_KEYS = ["Please", "Clear", "<-", "Store", "Update", "Fixture", "Group", "Preset", "Cue", "Sequence",
            "Executor", "At", "Thru", "Full", "Oops", "Delete", "Copy", "Move", "Edit", "Label"]


class ActionDialog(QDialog):
    """What a key / button / push does; buttons also get label, colours, lit."""

    def __init__(self, parent, act, button=False, screens=()):
        super().__init__(parent)
        self.setWindowTitle("Button" if button else "Action")
        self.button = button
        act = dict(act or {})
        form = QFormLayout(self)
        if button:
            self.label = QLineEdit(text_of(act.get("label")))
            form.addRow("Label", self.label)
        self.type = QComboBox()
        for name, key in ACTION_TYPES:
            self.type.addItem(name, key)
        form.addRow("Does", self.type)
        self.stack = QStackedWidget()
        form.addRow("", self.stack)
        pages = {}

        def page(key, rows):
            w = QWidget()
            f = QFormLayout(w)
            f.setContentsMargins(0, 0, 0, 0)
            for label, widget in rows:
                f.addRow(label, widget)
            pages[key] = self.stack.addWidget(w)

        self.exec_no = QSpinBox()
        self.exec_no.setRange(1, 9999)
        self.exec_no.setValue(int(act.get("exec", 101)))
        page("exec", [("Executor", self.exec_no)])
        self.key = QComboBox()
        self.key.setEditable(True)
        self.key.addItems(CMD_KEYS)
        self.key.setCurrentText(text_of(act.get("key", "Store")))
        page("key", [("Key", self.key)])
        self.cmd = QLineEdit(text_of(act.get("cmd")))
        self.cmd.setPlaceholderText("e.g. Go+  (generic: sent to the command address)")
        page("cmd", [("Command", self.cmd)])
        self.osc = QLineEdit(text_of(act.get("osc")))
        self.osc.setPlaceholderText("/address")
        self.on = QLineEdit(text_of(act.get("on")))
        self.on.setPlaceholderText("profile default (1)")
        self.off = QLineEdit(text_of(act.get("off")))
        self.off.setPlaceholderText("profile default (0)")
        page("osc", [("Address", self.osc), ("Pressed sends", self.on), ("Released sends", self.off)])
        self.page_dir = QComboBox()
        self.page_dir.addItem("Page -", -1)
        self.page_dir.addItem("Page +", 1)
        self.page_dir.setCurrentIndex(1 if int(act.get("page", -1)) > 0 else 0)
        page("page", [("", self.page_dir)])
        self.screen = QComboBox()
        self.screen.setEditable(True)
        self.screen.addItems(["main", "keypad", "encoders", "setup"] + [s for s in screens if s not in
                                                                       ("main", "keypad", "encoders")])
        self.screen.setCurrentText(text_of(act.get("screen", "keypad")))
        page("screen", [("Screen", self.screen)])
        self.cmdline = QComboBox()
        for k in ("please", "clear", "backspace"):
            self.cmdline.addItem(k)
        self.cmdline.setCurrentText(text_of(act.get("cmdline", "please")))
        page("cmdline", [("Key", self.cmdline)])
        self.res = QLineEdit(text_of(act.get("resolution", "Dimmer")))
        page("resolution", [("Attribute", self.res)])
        page("encpage", [("", QLabel("Swaps which pair of MA's encoders the hardware follows"))])
        page(None, [("", QLabel("Does nothing"))])
        self.pages = pages
        current = next((k for _, k in ACTION_TYPES if k and k in act), None)
        self.type.setCurrentIndex([k for _, k in ACTION_TYPES].index(current))
        self.type.currentIndexChanged.connect(lambda: self.stack.setCurrentIndex(pages[self.type.currentData()]))
        self.stack.setCurrentIndex(pages[current])
        if button:
            self.color, self.lit_color, self.text_color = ColorEdit(), ColorEdit(), ColorEdit()
            self.color.set(act.get("color"))
            self.lit_color.set(act.get("lit_color"))
            self.text_color.set(act.get("text_color"))
            form.addRow("Colour", self.color)
            form.addRow("Lit colour", self.lit_color)
            form.addRow("Text colour", self.text_color)
            self.lit = QLineEdit(text_of(act.get("lit")))
            self.lit.setPlaceholderText("OSC address that lights it (> 0 / on)")
            form.addRow("Lit by", self.lit)
            self.state = QComboBox()
            self.state.addItems(["", "highlight", "lowlight", "solo", "blind"])
            self.state.setCurrentText(text_of(act.get("state")))
            form.addRow("Lit by MA state", self.state)
        self.extra = {k: v for k, v in act.items() if k not in
                      ("label", "color", "lit_color", "text_color", "lit", "state", "on", "off")
                      and k not in [t for _, t in ACTION_TYPES if t]}
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def result_action(self):
        t = self.type.currentData()
        a = dict(self.extra)
        if t == "exec":
            a["exec"] = self.exec_no.value()
        elif t == "key":
            a["key"] = self.key.currentText().strip()
        elif t == "cmd":
            a["cmd"] = self.cmd.text().strip()
        elif t == "osc":
            a["osc"] = self.osc.text().strip() or "/"
            if self.on.text().strip():
                a["on"] = num_or_text(self.on.text())
            if self.off.text().strip():
                a["off"] = num_or_text(self.off.text())
        elif t == "page":
            a["page"] = self.page_dir.currentData()
        elif t == "screen":
            a["screen"] = self.screen.currentText().strip()
        elif t == "cmdline":
            a["cmdline"] = self.cmdline.currentText()
        elif t == "resolution":
            a["resolution"] = self.res.text().strip()
        elif t == "encpage":
            a["encpage"] = 1
        if self.button:
            b = {"label": self.label.text().strip() or "?"}
            b.update(a)
            put(b, "color", self.color.value())
            put(b, "lit_color", self.lit_color.value())
            put(b, "text_color", self.text_color.value())
            put(b, "lit", self.lit.text().strip())
            put(b, "state", self.state.currentText())
            return b
        return a or None


def edit_action(parent, act, button=False, screens=()):
    """-> the edited action, or the original when cancelled."""
    d = ActionDialog(parent, act, button, screens)
    return d.result_action() if d.exec() == QDialog.Accepted else act


class ButtonList(QWidget):
    """A list of buttons (touchscreen buttons, a screen's buttons)."""

    def __init__(self, screens_fn, limit=25):
        super().__init__()
        self.screens_fn, self.limit = screens_fn, limit
        self.items = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.edit())
        lay.addWidget(self.list, 1)
        col = QVBoxLayout()
        for name, fn in (("Add", self.add), ("Edit", self.edit), ("Remove", self.remove),
                         ("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1))):
            b = QPushButton(name)
            b.clicked.connect(fn)
            col.addWidget(b)
        col.addStretch()
        lay.addLayout(col)

    def refresh(self, sel=None):
        self.list.clear()
        for b in self.items:
            self.list.addItem(QListWidgetItem(f"{b.get('label', '?')}    -  {describe(b)}"))
        if sel is not None and 0 <= sel < len(self.items):
            self.list.setCurrentRow(sel)

    def set_items(self, items):
        self.items = [dict(b) for b in items if isinstance(b, dict)]
        self.refresh()

    def add(self):
        if len(self.items) >= self.limit:
            QMessageBox.information(self, "littlelx", f"At most {self.limit} buttons here.")
            return
        b = edit_action(self, {"label": "New"}, True, self.screens_fn())
        if b:
            self.items.append(b)
            self.refresh(len(self.items) - 1)

    def edit(self):
        i = self.list.currentRow()
        if 0 <= i < len(self.items):
            self.items[i] = edit_action(self, self.items[i], True, self.screens_fn()) or self.items[i]
            self.refresh(i)

    def remove(self):
        i = self.list.currentRow()
        if 0 <= i < len(self.items):
            del self.items[i]
            self.refresh(min(i, len(self.items) - 1))

    def move(self, d):
        i = self.list.currentRow()
        j = i + d
        if 0 <= i < len(self.items) and 0 <= j < len(self.items):
            self.items[i], self.items[j] = self.items[j], self.items[i]
            self.refresh(j)


# ---------------------------------------------------------------- sections

class ConnectionPage(QWidget):
    GEN = ("fader", "key", "encoder", "push", "button", "page", "command", "set")

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        top = QGroupBox("Talks to")
        f = QFormLayout(top)
        self.type = QComboBox()
        self.type.addItem("grandMA3 (with the MA plugin)", "ma3")
        self.type.addItem("Generic OSC (anything else)", "generic")
        self.host = QLineEdit()
        self.port, self.listen = QSpinBox(), QSpinBox()
        for s in (self.port, self.listen):
            s.setRange(1, 65535)
        self.prefix = QLineEdit()
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.005, 1)
        self.interval.setDecimals(3)
        self.interval.setSingleStep(0.005)
        self.jump = QDoubleSpinBox()
        self.jump.setRange(0.5, 50)
        f.addRow("Type", self.type)
        f.addRow("Send to (IP)", self.host)
        f.addRow("Port", self.port)
        f.addRow("Prefix", self.prefix)
        f.addRow("Listen for feedback on port", self.listen)
        f.addRow("Fader messages every (s)", self.interval)
        f.addRow("A move of this % goes out at once", self.jump)
        lay.addWidget(top)
        self.gen_box = QGroupBox("Generic OSC: where things are sent ({n} = number)")
        g = QFormLayout(self.gen_box)
        self.gen = {}
        for k in self.GEN:
            self.gen[k] = QLineEdit()
            g.addRow(k.capitalize(), self.gen[k])
        lay.addWidget(self.gen_box)
        self.val_box = QGroupBox("Generic OSC: values sent")
        v = QFormLayout(self.val_box)
        self.fader_lo, self.fader_hi, self.enc_step = QLineEdit(), QLineEdit(), QLineEdit()
        row = QHBoxLayout()
        row.addWidget(QLabel("bottom"))
        row.addWidget(self.fader_lo)
        row.addWidget(QLabel("top"))
        row.addWidget(self.fader_hi)
        v.addRow("Faders", row)
        self.onoff = {}
        for k in ("key", "button", "push"):
            on, off = QLineEdit(), QLineEdit()
            r = QHBoxLayout()
            r.addWidget(QLabel("pressed"))
            r.addWidget(on)
            r.addWidget(QLabel("released"))
            r.addWidget(off)
            v.addRow(k.capitalize() + "s", r)
            self.onoff[k] = (on, off)
        v.addRow("Encoder: per click", self.enc_step)
        self.integer = QCheckBox("Whole numbers")
        v.addRow("", self.integer)
        lay.addWidget(self.val_box)
        lay.addStretch()
        self.type.currentIndexChanged.connect(self.show_generic)

    def show_generic(self):
        g = self.type.currentData() == "generic"
        self.gen_box.setVisible(g)
        self.val_box.setVisible(g)

    def load(self, p):
        c = dict(lx.DEFAULTS["osc"], **p.get("connection", {}))
        self.type.setCurrentIndex(1 if c.get("type") == "generic" else 0)
        self.host.setText(text_of(c.get("host")))
        self.port.setValue(int(c.get("port", 8000)))
        self.prefix.setText(text_of(c.get("prefix")))
        self.listen.setValue(int(c.get("listen_port", 9000)))
        self.interval.setValue(float(c.get("fader_interval", 0.025)))
        self.jump.setValue(float(c.get("fader_jump", 5)))
        gen = dict(lx.DEFAULTS["osc"]["generic"], **(c.get("generic") or {}))
        for k, e in self.gen.items():
            e.setText(text_of(gen.get(k)))
        vals = dict(lx.DEFAULTS["osc"]["values"], **(c.get("values") or {}))
        self.fader_lo.setText(text_of(vals["fader"][0]))
        self.fader_hi.setText(text_of(vals["fader"][1]))
        for k, (on, off) in self.onoff.items():
            on.setText(text_of(vals[k][0]))
            off.setText(text_of(vals[k][1]))
        self.enc_step.setText(text_of(vals["encoder"]))
        self.integer.setChecked(bool(vals.get("integer")))
        self.show_generic()

    def collect(self, p):
        c = p.setdefault("connection", {})
        c["type"] = self.type.currentData()
        c["host"] = self.host.text().strip() or "127.0.0.1"
        c["port"] = self.port.value()
        c["prefix"] = self.prefix.text().strip()
        c["listen_port"] = self.listen.value()
        c["fader_interval"] = round(self.interval.value(), 3)
        c["fader_jump"] = round(self.jump.value(), 2)
        c["generic"] = {k: e.text().strip() for k, e in self.gen.items() if e.text().strip()}
        vals = {"fader": [num_or_text(self.fader_lo.text() or "0"), num_or_text(self.fader_hi.text() or "1")],
                "encoder": num_or_text(self.enc_step.text() or "1"), "integer": self.integer.isChecked()}
        for k, (on, off) in self.onoff.items():
            vals[k] = [num_or_text(on.text() or "1"), num_or_text(off.text() or "0")]
        c["values"] = vals


class FadersPage(QWidget):
    COLS = ["Executor", "Name", "Colour", "Own OSC address", "Range bottom", "Range top", "Feedback address"]

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addWidget(note("The five faders, left to right. grandMA3: the executor each one controls. "
                             "Generic: the profile's fader address, or each fader's own."))
        self.table = QTableWidget(5, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setVerticalHeaderLabels([f"Fader {i + 1}" for i in range(5)])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        lay.addWidget(self.table)
        lay.addStretch()

    def load(self, p):
        faders = (p.get("faders") or []) + [{}] * 5
        for i in range(5):
            f = faders[i] or {}
            rng = f.get("range") or ["", ""]
            for c, v in enumerate([f.get("exec"), f.get("name"), f.get("color"), f.get("osc"), rng[0], rng[1],
                                   f.get("feedback")]):
                self.table.setItem(i, c, QTableWidgetItem(text_of(v)))

    def collect(self, p):
        old = (p.get("faders") or []) + [{}] * 5
        out = []
        for i in range(5):
            cell = [(self.table.item(i, c).text().strip() if self.table.item(i, c) else "") for c in range(7)]
            f = {k: v for k, v in (old[i] or {}).items() if k not in ("exec", "name", "color", "osc", "range",
                                                                      "feedback")}
            if cell[0]:
                f["exec"] = int(num_or_text(cell[0])) if str(num_or_text(cell[0])).isdigit() else cell[0]
            put(f, "name", cell[1])
            put(f, "color", cell[2].lstrip("#"))
            put(f, "osc", cell[3])
            if cell[4] and cell[5]:
                f["range"] = [num_or_text(cell[4]), num_or_text(cell[5])]
            put(f, "feedback", cell[6])
            out.append(f)
        p["faders"] = out


class KeysPage(QWidget):
    """The 20 hardware keys, 5 across like the controller."""

    def __init__(self, screens_fn):
        super().__init__()
        self.screens_fn = screens_fn
        self.keys = [None] * 20
        lay = QVBoxLayout(self)
        lay.addWidget(note("Click a key to choose what it does (laid out like the controller). "
                             "Generic profiles: keys left on 'Nothing' send the key address."))
        grid = QGridLayout()
        self.btns = []
        for k in range(20):
            b = QPushButton()
            b.setMinimumSize(110, 56)
            b.clicked.connect(lambda _=False, k=k: self.edit(k))
            grid.addWidget(b, k // 5, k % 5)
            self.btns.append(b)
        lay.addLayout(grid)
        reset = QPushButton("Set all keys to the default layout")
        reset.clicked.connect(self.reset)
        lay.addWidget(reset, 0, Qt.AlignLeft)
        lay.addStretch()

    def show_keys(self):
        for k, b in enumerate(self.btns):
            b.setText(f"{k + 1}\n{describe(self.keys[k])}")

    def edit(self, k):
        self.keys[k] = edit_action(self, self.keys[k], False, self.screens_fn())
        self.show_keys()

    def reset(self):
        if QMessageBox.question(self, "littlelx", "Set all 20 keys to the default layout?") == QMessageBox.Yes:
            self.keys = copy.deepcopy(lx.DEFAULTS["keys"])
            self.show_keys()

    def load(self, p):
        self.keys = ((p.get("keys") or []) + [None] * 20)[:20]
        self.show_keys()

    def collect(self, p):
        p["keys"] = [k or None for k in self.keys]


class ButtonsPage(QWidget):
    def __init__(self, screens_fn):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addWidget(note("The touchscreen's main page buttons (under the faders). They arrange "
                             "themselves in a grid that suits how many there are."))
        self.list = ButtonList(screens_fn, 18)
        lay.addWidget(self.list)
        f = QFormLayout()
        self.columns = QSpinBox()
        self.columns.setRange(0, 6)
        self.columns.setSpecialValueText("automatic")
        f.addRow("Columns", self.columns)
        lay.addLayout(f)

    def load(self, p):
        self.list.set_items(p.get("touch_buttons") or [])
        self.columns.setValue(int((p.get("screen") or {}).get("columns") or 0))

    def collect(self, p):
        p["touch_buttons"] = self.list.items
        scr = p.setdefault("screen", {})
        put(scr, "columns", self.columns.value() or None)


class EncoderBox(QGroupBox):
    def __init__(self, i, screens_fn):
        super().__init__(f"Encoder {i + 1}")
        self.screens_fn = screens_fn
        self.push = None
        self.choices = []
        f = QFormLayout(self)
        self.follow = QCheckBox("Follow MA's encoders (grandMA3)")
        self.attribute = QLineEdit()
        self.attribute.setPlaceholderText("when not following / nothing selected, e.g. Dimmer")
        self.step = QDoubleSpinBox()
        self.step.setRange(0.01, 100)
        self.label = QLineEdit()
        self.value = QLineEdit()
        self.value.setPlaceholderText("generic: OSC address showing its value")
        self.click = QComboBox()
        for name, key in (("Opens the value page", "pad"), ("Sends its push", "push"), ("Nothing", "none"),
                          ("Default for the profile type", "")):
            self.click.addItem(name, key)
        self.pad = QCheckBox("Number pad on the value page")
        self.choice_src = QComboBox()
        for name, key in (("MA's named values (gobos...)", "ma"), ("This list", "list"), ("None", "none"),
                          ("Default for the profile type", "")):
            self.choice_src.addItem(name, key)
        self.choice_table = QTableWidget(0, 2)
        self.choice_table.setHorizontalHeaderLabels(["Label", "Value"])
        self.choice_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.choice_table.setMaximumHeight(130)
        rows = QHBoxLayout()
        add, rem = QPushButton("Add"), QPushButton("Remove")
        add.clicked.connect(lambda: self.choice_table.insertRow(self.choice_table.rowCount()))
        rem.clicked.connect(lambda: self.choice_table.removeRow(max(0, self.choice_table.currentRow())))
        rows.addWidget(add)
        rows.addWidget(rem)
        rows.addStretch()
        self.set_addr = QLineEdit()
        self.set_addr.setPlaceholderText("generic: where a chosen / typed value goes (default: set address)")
        self.push_btn = QPushButton()
        self.push_btn.clicked.connect(self.edit_push)
        f.addRow("", self.follow)
        f.addRow("Attribute", self.attribute)
        f.addRow("Step per click", self.step)
        f.addRow("Label", self.label)
        f.addRow("Value from", self.value)
        f.addRow("Click", self.click)
        f.addRow("", self.pad)
        f.addRow("Choices", self.choice_src)
        f.addRow("", self.choice_table)
        f.addRow("", rows)
        f.addRow("Value goes to", self.set_addr)
        f.addRow("Push action", self.push_btn)

    def edit_push(self):
        self.push = edit_action(self, self.push, False, self.screens_fn())
        self.push_btn.setText(describe(self.push))

    def load(self, e):
        e = e or {}
        self.extra = {k: v for k, v in e.items() if k not in ("follow", "attribute", "step", "label", "value",
                                                              "click", "pad", "choices", "set", "push")}
        self.follow.setChecked(bool(e.get("follow")))
        self.attribute.setText(text_of(e.get("attribute")))
        self.step.setValue(float(e.get("step", 1)))
        self.label.setText(text_of(e.get("label")))
        self.value.setText(text_of(e.get("value")))
        self.click.setCurrentIndex(self.click.findData(e.get("click", "")))
        self.pad.setChecked(bool(e.get("pad", True)))
        ch = e.get("choices", "")
        self.choice_src.setCurrentIndex(self.choice_src.findData("list" if isinstance(ch, list) else
                                                                 "none" if ch is False else ch or ""))
        self.choice_table.setRowCount(0)
        for c in ch if isinstance(ch, list) else []:
            r = self.choice_table.rowCount()
            self.choice_table.insertRow(r)
            c = c if isinstance(c, dict) else {"label": c}
            self.choice_table.setItem(r, 0, QTableWidgetItem(text_of(c.get("label"))))
            self.choice_table.setItem(r, 1, QTableWidgetItem(text_of(c.get("value"))))
        self.set_addr.setText(text_of(e.get("set")))
        self.push = e.get("push")
        self.push_btn.setText(describe(self.push))

    def collect(self):
        e = dict(self.extra)
        if self.follow.isChecked():
            e["follow"] = True
        put(e, "attribute", self.attribute.text().strip())
        if e.get("attribute"):
            e["step"] = round(self.step.value(), 3)
        put(e, "label", self.label.text().strip())
        put(e, "value", self.value.text().strip())
        put(e, "click", self.click.currentData())
        if not self.pad.isChecked():
            e["pad"] = False
        elif e.get("click") == "pad" or "pad" in self.extra:
            e["pad"] = True
        src = self.choice_src.currentData()
        if src == "list":
            items = []
            for r in range(self.choice_table.rowCount()):
                lab = self.choice_table.item(r, 0).text().strip() if self.choice_table.item(r, 0) else ""
                val = self.choice_table.item(r, 1).text() if self.choice_table.item(r, 1) else ""
                if lab or val.strip():
                    items.append({"label": lab or val.strip(), "value": num_or_text(val or lab)})
            e["choices"] = items
        elif src == "none":
            e["choices"] = False
        elif src == "ma":
            e["choices"] = "ma"
        put(e, "set", self.set_addr.text().strip())
        if self.push:
            e["push"] = self.push
        return e


class EncodersPage(QWidget):
    def __init__(self, screens_fn):
        super().__init__()
        lay = QHBoxLayout(self)
        self.boxes = [EncoderBox(i, screens_fn) for i in range(2)]
        for b in self.boxes:
            lay.addWidget(b)

    def load(self, p):
        encs = (p.get("encoders") or []) + [{}] * 2
        for i, b in enumerate(self.boxes):
            b.load(encs[i])

    def collect(self, p):
        p["encoders"] = [b.collect() for b in self.boxes]


class ScreensPage(QWidget):
    """The profile's own screens: button grids, or a keypad."""

    def __init__(self, screens_fn):
        super().__init__()
        self.screens = {}
        self.cur = None
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.names = QListWidget()
        self.names.currentTextChanged.connect(self.select)
        left.addWidget(self.names)
        for name, fn in (("New screen", self.new), ("New keypad", self.new_keypad), ("Rename", self.rename),
                         ("Delete", self.delete)):
            b = QPushButton(name)
            b.clicked.connect(fn)
            left.addWidget(b)
        lay.addLayout(left)
        self.right = QWidget()
        r = QVBoxLayout(self.right)
        f = QFormLayout()
        self.title = QLineEdit()
        self.columns = QSpinBox()
        self.columns.setRange(0, 6)
        self.columns.setSpecialValueText("automatic")
        self.back = QComboBox()
        self.back.setEditable(True)
        f.addRow("Title", self.title)
        f.addRow("Columns", self.columns)
        f.addRow("Back goes to", self.back)
        r.addLayout(f)
        self.kind = QStackedWidget()
        self.buttons = ButtonList(screens_fn, 25)
        self.kind.addWidget(self.buttons)
        kp = QWidget()
        kl = QVBoxLayout(kp)
        kl.addWidget(note("Keypad keys: up to 5 x 5. Text is typed into the command line; double-click "
                            "a key to make it an action (Enter / Delete / Clear, Back, OSC ...)."))
        self.keytable = QTableWidget(5, 5)
        self.keytable.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.keytable.horizontalHeader().setMinimumSectionSize(40)
        self.keytable.setMinimumWidth(0)
        self.keytable.cellDoubleClicked.connect(self.key_action)
        kl.addWidget(self.keytable)
        self.kind.addWidget(kp)
        r.addWidget(self.kind)
        lay.addWidget(self.right, 1)
        self.screens_fn = screens_fn
        self.key_actions = {}

    def select(self, name):
        self.store()
        self.cur = name if name in self.screens else None
        self.right.setEnabled(self.cur is not None)
        if not self.cur:
            return
        s = self.screens[name]
        self.title.setText(text_of(s.get("title")))
        self.columns.setValue(int(s.get("columns") or 0))
        self.back.clear()
        self.back.addItems(["main", "keypad", "encoders"] + list(self.screens))
        self.back.setCurrentText(text_of(s.get("back", "main")))
        if s.get("type") == "keypad":
            self.kind.setCurrentIndex(1)
            self.keytable.clearContents()
            self.key_actions = {}
            for rr, row in enumerate((s.get("keys") or [])[:5]):
                for c, k in enumerate(row[:5]):
                    if isinstance(k, dict):
                        self.key_actions[(rr, c)] = k
                        item = QTableWidgetItem("[" + text_of(k.get("label", describe(k))) + "]")
                    else:
                        item = QTableWidgetItem(text_of(k))
                    self.keytable.setItem(rr, c, item)
        else:
            self.kind.setCurrentIndex(0)
            self.buttons.set_items(s.get("buttons") or [])

    def key_action(self, r, c):
        cur = self.key_actions.get((r, c))
        if cur is None:
            item = self.keytable.item(r, c)
            cur = {"label": item.text() if item else ""}
        a = edit_action(self, cur, True, self.screens_fn())
        if a:
            self.key_actions[(r, c)] = a
            self.keytable.setItem(r, c, QTableWidgetItem("[" + text_of(a.get("label")) + "]"))

    def store(self):
        if not self.cur or self.cur not in self.screens:
            return
        s = self.screens[self.cur]
        put(s, "title", self.title.text().strip())
        put(s, "columns", self.columns.value() or None)
        s["back"] = self.back.currentText().strip() or "main"
        if s.get("type") == "keypad":
            rows = []
            for r in range(5):
                row = []
                for c in range(5):
                    item = self.keytable.item(r, c)
                    t = item.text().strip() if item else ""
                    if (r, c) in self.key_actions and t.startswith("["):
                        row.append(self.key_actions[(r, c)])
                    elif t:
                        row.append(t)
                if row:
                    rows.append(row)
            s["keys"] = rows
        else:
            s["buttons"] = self.buttons.items

    def ask_name(self, title, default=""):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "littlelx", title, text=default)
        name = name.strip()
        return name if ok and name else None

    def new(self):
        name = self.ask_name("Name of the new screen")
        if name and name not in self.screens:
            self.screens[name] = {"title": name.capitalize(), "buttons": [], "back": "main"}
            self.reload(name)

    def new_keypad(self):
        name = self.ask_name("Name (\"keypad\" replaces the built-in keypad)", "keypad")
        if name and name not in self.screens:
            self.screens[name] = {"type": "keypad", "keys": copy.deepcopy(lx.Screen.KEYPAD), "back": "main"}
            self.reload(name)

    def rename(self):
        if not self.cur:
            return
        name = self.ask_name("New name", self.cur)
        if name and name not in self.screens:
            self.store()
            self.screens[name] = self.screens.pop(self.cur)
            self.cur = None
            self.reload(name)

    def delete(self):
        if self.cur and QMessageBox.question(self, "littlelx", f"Delete the screen '{self.cur}'?") == \
                QMessageBox.Yes:
            del self.screens[self.cur]
            self.cur = None
            self.reload()

    def reload(self, sel=None):
        self.store()
        self.cur = None
        self.names.blockSignals(True)
        self.names.clear()
        self.names.addItems(list(self.screens))
        self.names.blockSignals(False)
        if sel:
            self.names.setCurrentRow(list(self.screens).index(sel))
        self.select(sel or "")

    def load(self, p):
        self.cur = None
        self.screens = copy.deepcopy(p.get("screens") or {})
        self.reload(next(iter(self.screens), None))

    def collect(self, p):
        self.store()
        put(p, "screens", copy.deepcopy(self.screens), empty=(None, {}))

    def names_list(self):
        return list(self.screens)


class LookPage(QWidget):
    OPTS = {
        "left": ["Page {page}", "{profile}"],
        "middle": ["cmdline", "sent", "feedback"],
        "status": ["ma", "osc", "feedback"],
        "encoders": ["ma", "simple"],
    }
    HELP = {"left": "top bar, left (text; {page} {profile})",
            "middle": "top bar, middle: cmdline / sent / feedback / text",
            "status": "top bar, right: ma / osc / feedback / text",
            "encoders": "Encoders page: ma (follow MA) / simple"}

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        box = QGroupBox("Screen (empty = the default for the profile type)")
        f = QFormLayout(box)
        self.fields = {}
        for k, opts in self.OPTS.items():
            c = QComboBox()
            c.setEditable(True)
            c.addItems([""] + opts)
            c.setToolTip(self.HELP[k])
            f.addRow(self.HELP[k], c)
            self.fields[k] = c
        self.title = QLineEdit()
        f.addRow("Encoders page title", self.title)
        self.strip = QComboBox()
        self.strip.addItems(["default", "show", "hide"])
        f.addRow("Encoder strip (main / keypad)", self.strip)
        outer.addWidget(box)
        cbox = QGroupBox("Colours (empty = default)")
        g = QGridLayout(cbox)
        self.colors = {}
        for n, name in enumerate(lx.THEME):
            e = ColorEdit()
            g.addWidget(QLabel(name.replace("_", " ")), n // 3, (n % 3) * 2)
            g.addWidget(e, n // 3, (n % 3) * 2 + 1)
            self.colors[name] = e
        outer.addWidget(cbox)
        outer.addStretch()

    def load(self, p):
        s = p.get("screen") or {}
        for k, c in self.fields.items():
            c.setCurrentText(text_of(s.get(k)))
        self.title.setText(text_of(s.get("encoders_title")))
        self.strip.setCurrentIndex(0 if "encoder_strip" not in s else 1 if s["encoder_strip"] else 2)
        cols = s.get("colors") or {}
        for k, e in self.colors.items():
            e.set(cols.get(k))

    def collect(self, p):
        s = p.setdefault("screen", {})
        for k, c in self.fields.items():
            put(s, k, c.currentText().strip())
        put(s, "encoders_title", self.title.text().strip())
        if self.strip.currentIndex() == 0:
            s.pop("encoder_strip", None)
        else:
            s["encoder_strip"] = self.strip.currentIndex() == 1
        cols = {k: e.value() for k, e in self.colors.items() if e.value()}
        put(s, "colors", cols, empty=(None, {}))
        if not s:
            p.pop("screen", None)


class FeedbackPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addWidget(note("Generic profiles: OSC coming back (to the listen port) that drives the screen. "
                             "{n} = the number. Leave everything empty for the defaults."))
        f = QFormLayout()
        self.fields = {}
        for k in lx.FEEDBACK_DEFAULT:
            e = QLineEdit()
            e.setPlaceholderText(lx.FEEDBACK_DEFAULT[k])
            f.addRow(k.replace("_", " ").capitalize(), e)
            self.fields[k] = e
        lay.addLayout(f)
        lay.addStretch()

    def load(self, p):
        fb = p.get("feedback") or {}
        for k, e in self.fields.items():
            e.setText(text_of(fb.get(k)))

    def collect(self, p):
        fb = {k: e.text().strip() for k, e in self.fields.items() if e.text().strip()}
        put(p, "feedback", fb, empty=(None, {}))


# ---------------------------------------------------------------- the Builder

class Builder(QWidget):
    """Edit a profile. save(activate) is wired by the app."""

    def __init__(self, on_save):
        super().__init__()
        self.on_save = on_save
        self.prof = {}
        self.saved_view = None
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Open"))
        self.picker = QComboBox()
        self.picker.setMinimumWidth(180)
        self.picker.activated.connect(self.open_picked)
        top.addWidget(self.picker)
        top.addSpacing(16)
        top.addWidget(QLabel("Name"))
        self.name = QLineEdit()
        top.addWidget(self.name, 1)
        copy_btn = QPushButton("Save as copy...")
        copy_btn.clicked.connect(self.save_copy)
        top.addWidget(copy_btn)
        lay.addLayout(top)
        self.tabs = QTabWidget()
        screens = lambda: self.screens.names_list()  # noqa: E731
        self.connection = ConnectionPage()
        self.faders = FadersPage()
        self.keys = KeysPage(screens)
        self.buttons = ButtonsPage(screens)
        self.encoders = EncodersPage(screens)
        self.screens = ScreensPage(screens)
        self.look = LookPage()
        self.feedback = FeedbackPage()
        self.sections = [("Connection", self.connection), ("Faders", self.faders), ("Keys", self.keys),
                         ("Screen buttons", self.buttons), ("Encoders", self.encoders),
                         ("Screens", self.screens), ("Look", self.look), ("Feedback", self.feedback)]
        for title, w in self.sections:
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # pages fit the window's width
            sc.setWidget(w)
            self.tabs.addTab(sc, title)
        self.json = QPlainTextEdit()
        self.json.setLineWrapMode(QPlainTextEdit.NoWrap)
        jw = QWidget()
        jl = QVBoxLayout(jw)
        jl.addWidget(note("The whole profile as JSON - for anything the other tabs don't cover. "
                            "Changes here are used when you save."))
        jl.addWidget(self.json)
        self.tabs.addTab(jw, "JSON")
        self.json_tab = self.tabs.count() - 1
        self.json_dirty = False
        self.json.textChanged.connect(lambda: setattr(self, "json_dirty", True))
        self.tabs.currentChanged.connect(self.tab_changed)
        self.last_tab = 0
        lay.addWidget(self.tabs, 1)
        bottom = QHBoxLayout()
        bottom.addStretch()
        save = QPushButton("Save")
        save.clicked.connect(lambda: self.save(False))
        act = QPushButton("Save && make active")
        act.setDefault(True)
        act.clicked.connect(lambda: self.save(True))
        bottom.addWidget(save)
        bottom.addWidget(act)
        lay.addLayout(bottom)

    def fill_picker(self):
        """The profile files, the one being edited selected."""
        self.picker.blockSignals(True)
        self.picker.clear()
        names = lx.list_profiles()
        self.picker.addItems(names)
        cur = self.prof.get("name")
        self.picker.setCurrentIndex(names.index(cur) if cur in names else -1)
        self.picker.blockSignals(False)

    def open_picked(self, i):
        name = self.picker.itemText(i)
        if not name or name == self.prof.get("name"):
            return
        if self.collect_forms() != self.saved_view and QMessageBox.question(
                self, "littlelx", f"Open '{name}'? Unsaved changes to '{self.prof.get('name')}' are lost.") \
                != QMessageBox.Yes:
            self.fill_picker()
            return
        try:
            p = lx.load_profile(name)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "littlelx", f"Can't read '{name}':\n{e}")
            self.fill_picker()
            return
        p["name"] = p.get("name") or name
        self.load(p)

    def save_copy(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "littlelx", "Name of the copy",
                                        text=(self.name.text().strip() or "Profile") + " copy")
        name = name.strip()
        if not ok or not name:
            return
        if name in lx.list_profiles():
            QMessageBox.information(self, "littlelx", f"There is already a profile '{name}'.")
            return
        p = self.collect()
        p["name"] = name
        self.prof = copy.deepcopy(p)  # from now on editing the copy
        self.name.setText(name)
        self.on_save(p, False)

    def mark_saved(self, prof):
        """What was just saved: for the 'unsaved changes' question."""
        self.prof = copy.deepcopy(prof)
        self.saved_view = self.collect_forms()
        self.fill_picker()

    def load(self, prof):
        self.prof = copy.deepcopy(prof)
        self.name.setText(text_of(prof.get("name")))
        for _, w in self.sections:
            w.load(self.prof)
        self.show_json()
        self.saved_view = self.collect_forms()
        self.fill_picker()

    def collect(self):
        """The profile as the forms (or the JSON tab, if edited there) have it."""
        if self.tabs.currentIndex() == self.json_tab and self.json_dirty:
            self.from_json()
        p = copy.deepcopy(self.prof)
        for _, w in self.sections:
            w.collect(p)
        p["name"] = self.name.text().strip() or "Profile"
        return p

    def show_json(self):
        self.json.blockSignals(True)
        self.json.setPlainText(json.dumps(self.collect_forms(), indent=2))
        self.json.blockSignals(False)
        self.json_dirty = False

    def collect_forms(self):
        p = copy.deepcopy(self.prof)
        for _, w in self.sections:
            w.collect(p)
        p["name"] = self.name.text().strip() or "Profile"
        return p

    def from_json(self):
        """Take the JSON tab's text into the forms; False if it isn't valid."""
        try:
            p = json.loads(self.json.toPlainText())
            if not isinstance(p, dict):
                raise ValueError("a profile is a JSON object { ... }")
        except ValueError as e:
            QMessageBox.warning(self, "littlelx", f"The JSON isn't valid:\n{e}")
            return False
        self.load(p)
        return True

    def tab_changed(self, i):
        if self.last_tab == self.json_tab and self.json_dirty:
            if not self.from_json():
                self.tabs.blockSignals(True)
                self.tabs.setCurrentIndex(self.json_tab)
                self.tabs.blockSignals(False)
                return
        if i == self.json_tab:
            self.show_json()
        self.last_tab = i

    def save(self, activate):
        if self.tabs.currentIndex() == self.json_tab and self.json_dirty and not self.from_json():
            return
        self.on_save(self.collect(), activate)
