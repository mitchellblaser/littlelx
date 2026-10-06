"""littlelx firmware package (.lxfw): every part's firmware in one file.

A zip with manifest.json and the parts:

    {"format": 1, "version": "<package version>",
     "parts": {"main":        {"file": "main/littlelx_mega.hex", "version": "..."},
               "touchscreen": {"file": "touchscreen/littlelx-pi-update.zip", "version": "..."}}}

Modules (fader module, keypad) will be more parts here. Installing compares
each part's version with what's connected and only installs what differs:
the main controller first (the touchscreen is updated through it), then the
touchscreen.

    python3 fwpack.py make OUT.lxfw VERSION --main X.hex --touchscreen Y.zip
"""
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

FORMAT = 1
PARTS = ("main", "touchscreen")
NAMES = {"main": "Controller (Mega)", "touchscreen": "Touchscreen (Pi)"}


class PackageError(Exception):
    pass


def touchscreen_version(update_zip):
    """The version inside a littlelx-pi-update.zip (its VERSION file)."""
    with zipfile.ZipFile(update_zip) as z:
        for name in z.namelist():
            if name.rstrip("/").split("/")[-1] == "VERSION":
                return z.read(name).decode().strip()
    return "unknown"


def make(out, version, main=None, touchscreen=None, main_version=None):
    parts = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if main:
            z.write(main, "main/littlelx_mega.hex")
            parts["main"] = {"file": "main/littlelx_mega.hex", "version": main_version or version}
        if touchscreen:
            z.write(touchscreen, "touchscreen/littlelx-pi-update.zip", zipfile.ZIP_STORED)
            parts["touchscreen"] = {"file": "touchscreen/littlelx-pi-update.zip",
                                    "version": touchscreen_version(touchscreen)}
        z.writestr("manifest.json", json.dumps({"format": FORMAT, "version": version, "parts": parts}, indent=2))
    return parts


class Package:
    """An opened .lxfw: its manifest, parts extracted to a temporary folder."""

    def __init__(self, path):
        self.path = path
        try:
            z = zipfile.ZipFile(path)
            self.manifest = json.loads(z.read("manifest.json"))
        except (OSError, zipfile.BadZipFile, KeyError, ValueError) as e:
            raise PackageError(f"{os.path.basename(path)} isn't a littlelx firmware package ({e})")
        if self.manifest.get("format", 0) > FORMAT:
            raise PackageError("This firmware package is newer than this bridge: update the app first.")
        self.dir = tempfile.mkdtemp(prefix="littlelx-fw-")
        self.parts = {}
        for name, part in self.manifest.get("parts", {}).items():
            dest = os.path.join(self.dir, part["file"])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(part["file"]) as a, open(dest, "wb") as b:
                shutil.copyfileobj(a, b)
            self.parts[name] = dict(part, path=dest)
        z.close()

    @property
    def version(self):
        return self.manifest.get("version", "?")

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def plan(pkg, installed, force=False):
    """-> [(part, installed version, package version, action)]; action is
    "install", "up to date", "not connected" or "not in package"."""
    rows = []
    for part in PARTS:
        have = installed.get(part)
        p = pkg.parts.get(part)
        if not p:
            rows.append((part, have, None, "not in package"))
        elif part == "touchscreen" and have is None:
            rows.append((part, None, p["version"], "not connected"))
        elif force or have != p["version"]:
            rows.append((part, have, p["version"], "install"))
        else:
            rows.append((part, have, p["version"], "up to date"))
    return rows


def install(lx, cfg, pkg, installed, port=None, force=False, progress=None, log=print):
    """Install what differs. lx: the littlelx module. Returns problems (text)."""
    problems = []
    todo = {part for part, _, _, action in plan(pkg, installed, force) if action == "install"}
    if not todo:
        log("Everything is up to date.")
        return problems
    if "main" in todo:
        log(f"== {NAMES['main']}: {installed.get('main') or 'unknown'} -> {pkg.parts['main']['version']}")
        try:
            lx.flash_mega(cfg, pkg.parts["main"]["path"], port,
                          progress=(lambda p: progress("main", p)) if progress else None)
            time.sleep(2)  # it restarts with the new firmware
        except Exception as e:
            problems.append(f"{NAMES['main']}: {e}")
            log(f"  failed: {e}")
            if "touchscreen" in todo:  # it goes through the controller: not now
                problems.append(f"{NAMES['touchscreen']}: not updated (the controller comes first)")
            return problems
    if "touchscreen" in todo:
        log(f"== {NAMES['touchscreen']}: {installed.get('touchscreen') or 'unknown'} -> "
            f"{pkg.parts['touchscreen']['version']}")
        if progress:
            progress("touchscreen", 0)
        try:
            lx.update_pi(cfg, pkg.parts["touchscreen"]["path"])
        except SystemExit as e:
            if e.code not in (None, 0):
                problems.append(f"{NAMES['touchscreen']}: {e}")
        except Exception as e:
            problems.append(f"{NAMES['touchscreen']}: {e}")
    log("Done." if not problems else "Finished with problems:\n  " + "\n  ".join(problems))
    return problems


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="make a littlelx firmware package")
    ap.add_argument("cmd", choices=["make", "show"])
    ap.add_argument("file")
    ap.add_argument("version", nargs="?")
    ap.add_argument("--main")
    ap.add_argument("--main-version")
    ap.add_argument("--touchscreen")
    a = ap.parse_args()
    if a.cmd == "make":
        if not a.version:
            sys.exit("make needs a version")
        for k, v in make(a.file, a.version, a.main, a.touchscreen, a.main_version).items():
            print(f"{k}: {v['version']}")
    else:
        pk = Package(a.file)
        print(json.dumps(pk.manifest, indent=2))
        pk.close()
