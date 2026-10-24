#!/usr/bin/env python3
"""
Multiseat Control Panel  (GTK3)  -  a point-and-click front end for multiseat-mint.sh

Workflow (like ASTER on Windows):
  1. Home   : install/build components, see status, start/stop, enable at boot
  2. Seats  : every connected monitor is a seat card. Press "Identify" on a card,
              then press a key / move the mouse of the devices that belong to it.
  3. Logs   : journal viewer
  4. Advanced: every command-line flag as a button, recovery tools, raw config

The shell script stays the engine, and every flag still works from a terminal
(recovery mode, Ctrl+Alt+F3 ...).  This GUI only writes the same config file the
script reads, and runs the same commands.

Run:  multiseat-gui          (re-launches itself as root through pkexec)
"""

import glob
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
GUI_PATH_FILE = "/etc/multiseat-gui.path"
DLM_RUN = "/var/local/run/drm-lease-manager"
SCRIPT_NAMES = ("multiseat-mint.sh", "multiseat.sh")
DEVICE_ICON = {"usbk": "⌨", "ps2k": "⌨", "usbm": "🖱", "ps2m": "🖱", "usbd": "🔌"}
KIND_ORDER = {"usbk": 0, "ps2k": 0, "usbm": 1, "ps2m": 1, "usbd": 2}
COMPONENTS = [
    ("tomlc99", "lib", "libtoml*", "TOML parser"),
    ("drm-lease-manager", "bin", "drm-lease-manager", "splits one GPU into per-seat leases"),
    ("wlroots (patched)", "lib", "libwlroots*", "compositor library with lease support"),
    ("labwc", "bin", "labwc", "the compositor each seat runs"),
    ("sfwbar", "bin", "sfwbar", "panel"),
    ("wlopm", "bin", "wlopm", "monitor power management"),
]
BUILD_IDS = ["0  tomlc99", "1  drm-lease-manager", "2  wlroots", "3  labwc", "4  sfwbar", "5  wlopm"]

CSS = b"""
.seatcard { border: 1px solid alpha(currentColor, 0.28); border-radius: 8px; }
.seatcard.off { opacity: 0.55; }
.dim { opacity: 0.65; }
.overlaywin { background: #101820; }
.overlaylbl { color: white; font-size: 72pt; font-weight: bold; }
"""


def esc(s):
    return GLib.markup_escape_text(str(s))


def rd(path, default=""):
    try:
        return Path(path).read_text(errors="replace").strip()
    except Exception:
        return default


def run(args, timeout=10):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "not found: %s" % args[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------

def adopt_user_display():
    """pkexec/sudo strip DISPLAY and XAUTHORITY: recover them from the calling user."""
    import pwd
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    user_home = None
    try:
        if uid:
            user_home = pwd.getpwuid(int(uid)).pw_dir
    except (KeyError, ValueError):
        pass
    if not os.environ.get("DISPLAY"):
        socks = sorted(glob.glob("/tmp/.X11-unix/X*"))
        if socks:
            os.environ["DISPLAY"] = ":" + os.path.basename(socks[0])[1:]
    if not os.environ.get("XAUTHORITY"):
        cands = []
        if user_home:
            cands.append(os.path.join(user_home, ".Xauthority"))
        if uid:
            cands += ["/run/user/%s/gdm/Xauthority" % uid, "/run/user/%s/.mutter-Xwaylandauth.*" % uid]
        for c in cands:
            hit = glob.glob(c)
            if hit:
                os.environ["XAUTHORITY"] = hit[0]
                break
    os.environ.setdefault("NO_AT_BRIDGE", "1")
    os.environ.setdefault("GSETTINGS_BACKEND", "memory")


def ensure_root():
    if os.geteuid() == 0:
        adopt_user_display()
        return
    env = ["%s=%s" % (k, os.environ[k]) for k in
           ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY") if k in os.environ]
    if "XAUTHORITY" not in os.environ:
        xa = os.path.expanduser("~/.Xauthority")
        if os.path.exists(xa):
            env.append("XAUTHORITY=" + xa)
    env += ["NO_AT_BRIDGE=1", "GSETTINGS_BACKEND=memory"]
    cmd = ["pkexec", "env"] + env + [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
    try:
        os.execvp("pkexec", cmd)
    except FileNotFoundError:
        print("pkexec not found. Run:  sudo python3 %s" % os.path.abspath(__file__))
        sys.exit(1)


def find_script():
    here = Path(__file__).resolve().parent
    cands = [os.environ.get("MULTISEAT_SCRIPT")]
    cands += [here / n for n in SCRIPT_NAMES]
    m = re.search(r"^ExecStart=(\S+)", rd("/etc/systemd/system/multiseat.service"), re.M)
    cands.append(m.group(1) if m else None)
    cands.append(rd(GUI_PATH_FILE) or None)
    cands += ["/usr/local/bin/" + n for n in SCRIPT_NAMES]
    cands += glob.glob("/home/*/*/multiseat*/multiseat-mint.sh")
    cands += glob.glob("/home/*/multiseat*/multiseat-mint.sh")
    for c in cands:
        if c and os.path.isfile(str(c)):
            return str(Path(c).resolve())
    return None


# ---------------------------------------------------------------------------
# Hardware detection (mirrors what the shell script's -c wizard does)
# ---------------------------------------------------------------------------

class Dev:
    def __init__(self, kind, ident, name="", present=True):
        self.kind, self.ident, self.name, self.present = kind, ident, name, present


class Display:
    def __init__(self, connector, label, mode):
        self.connector, self.label, self.mode = connector, label, mode


def edid_label(path):
    try:
        data = Path(path).read_bytes()
    except Exception:
        return ""
    if len(data) < 128:
        return ""
    for off in (54, 72, 90, 108):
        blk = data[off:off + 18]
        if blk[0:3] == b"\0\0\0" and blk[3] == 0xFC:
            return blk[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
    v = (data[8] << 8) | data[9]
    return "".join(chr(((v >> s) & 31) + 64) for s in (10, 5, 0)) + " monitor"


def detect_displays():
    out = []
    for p in sorted(glob.glob("/sys/class/drm/card*-*")):
        name = os.path.basename(p)
        if "Writeback" in name or rd(p + "/status") != "connected":
            continue
        modes = rd(p + "/modes").splitlines()
        out.append(Display(name, edid_label(p + "/edid") or "Monitor", modes[0] if modes else ""))
    return out


def usb_ident(real):
    """Same id the script writes: <pci>-<port>, e.g. 0000:00:1d.0-1.4"""
    port = os.path.basename(real)
    pci = os.path.basename(real.rsplit("/usb", 1)[0])
    return "%s-%s" % (pci, port.rsplit("-", 1)[-1])


def detect_inputs():
    devs = {}
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        real = os.path.realpath(dev)
        port = os.path.basename(real)
        if not re.match(r"^\d+-[\d.]+$", port):
            continue
        name = ("%s %s" % (rd(real + "/manufacturer"), rd(real + "/product"))).strip() or \
            "USB %s:%s" % (rd(real + "/idVendor"), rd(real + "/idProduct"))
        if rd(real + "/bDeviceClass") == "09" or "hub" in name.lower():
            continue
        kb = ms = False
        for ifc in glob.glob(real + "/*:*"):
            if rd(ifc + "/bInterfaceClass") == "03":
                pr = rd(ifc + "/bInterfaceProtocol")
                kb |= pr == "01"
                ms |= pr == "02"
        kind = "usbk" if kb else "usbm" if ms else "usbd"
        ident = usb_ident(real)
        devs[ident] = Dev(kind, ident, name + ("  (keyboard + mouse)" if kb and ms else ""))
    for inp in sorted(glob.glob("/sys/class/input/input*")):
        phys = rd(inp + "/phys")
        words = rd(inp + "/capabilities/key").split()
        if "serio" not in phys or not words:
            continue
        ident = phys.split("/")[-2]
        name = rd(inp + "/name") or ident
        if int(words[-1], 16) == 0xFFFFFFFFFFFFFFFE:
            devs[ident] = Dev("ps2k", ident, name)
        elif len(words) >= 5 and int(words[-5], 16) & 0x10000:
            devs[ident] = Dev("ps2m", ident, name)
    return devs


def conf_path():
    """Same file name the script computes from the DRM connector list."""
    rc, out, _ = run(["bash", "-c", 'shopt -s nullglob; find /sys/devices/pci* -type d '
                      '-path "*/drm/card*/card*-*" -prune'])
    names = [os.path.basename(x) for x in out.split()]
    if not names:
        return None
    s = "".join(re.sub(r"[^A-Za-z0-9]", "", n) for n in names).replace("card", "")
    return "/etc/multiseat/_%s.conf" % s


_ev_cache = {}


def describe_event_device(evpath):
    """/dev/input/eventN -> (config ident, device name) or None."""
    if evpath in _ev_cache:
        return _ev_cache[evpath]
    n = os.path.basename(evpath)
    base = "/sys/class/input/%s/device" % n
    res = None
    try:
        parts = os.path.realpath(base).split("/")
        name = rd(base + "/name") or n
        usb = [i for i, p in enumerate(parts) if re.match(r"^usb\d+$", p)]
        if usb:
            ports = [p for p in parts[usb[0] + 1:] if re.match(r"^\d+-[\d.]+$", p)]
            if ports:
                res = ("%s-%s" % (parts[usb[0] - 1], ports[-1].rsplit("-", 1)[-1]), name)
        else:
            phys = rd(base + "/phys")
            if "serio" in phys:
                res = (phys.split("/")[-2], name)
    except Exception:
        res = None
    _ev_cache[evpath] = res
    return res


class InputSniffer(threading.Thread):
    """Reports which physical device produces key presses / mouse movement."""
    FMT = "llHHi"
    SIZE = struct.calcsize(FMT)

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self.stop_ev = threading.Event()

    def run(self):
        fds = {}
        for p in glob.glob("/dev/input/event*"):
            try:
                fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = p
            except OSError:
                pass
        try:
            while not self.stop_ev.is_set() and fds:
                r, _, _ = select.select(list(fds), [], [], 0.2)
                for fd in r:
                    try:
                        data = os.read(fd, self.SIZE * 32)
                    except BlockingIOError:
                        continue
                    except OSError:
                        os.close(fd)
                        fds.pop(fd, None)
                        continue
                    for off in range(0, len(data) - self.SIZE + 1, self.SIZE):
                        _, _, t, c, v = struct.unpack_from(self.FMT, data, off)
                        kind = None
                        if t == 1 and v == 1:
                            kind = "kbd" if c < 0x100 else "mouse" if 0x110 <= c < 0x120 else None
                        elif t == 2:
                            kind = "mouse"
                        if kind:
                            info = describe_event_device(fds[fd])
                            if info:
                                GLib.idle_add(self.callback, kind, info[0], info[1])
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Config model (same text format the shell script parses)
# ---------------------------------------------------------------------------

class Seat:
    def __init__(self, connector, enabled=False):
        self.connector = connector
        self.enabled = enabled
        self.devs = []
        self.extra = {}
        self.display = None


class Config:
    DEV_RE = re.compile(r"^(usbk|usbm|usbd|ps2k|ps2m)\s+(\S+)(?:\s+#-\s*(.*))?$")
    SEAT_RE = re.compile(r"^(#?)\s*(card\d+-[^\s#]+)\s*(?:#.*)?$")
    EXTRA_RE = re.compile(r"^(comp|open|spkr)\s+(.*)$")

    def __init__(self):
        self.seats = []
        self.path = None

    @classmethod
    def load(cls, path):
        c = cls()
        c.path = path
        cur = None
        try:
            text = Path(path).read_text()
        except Exception:
            return c
        for raw in text.splitlines():
            line = raw.strip()
            m = cls.SEAT_RE.match(line)
            if m:
                cur = Seat(m.group(2), enabled=not m.group(1))
                c.seats.append(cur)
                continue
            if not line or line.startswith("#") or cur is None or not cur.enabled:
                continue
            m = cls.DEV_RE.match(line)
            if m:
                cur.devs.append(Dev(m.group(1), m.group(2), (m.group(3) or "").strip(), False))
                continue
            m = cls.EXTRA_RE.match(line)
            if m:
                cur.extra[m.group(1)] = re.sub(r"\s*#.*", "", m.group(2)).strip()
        return c

    def sync(self, displays, devices):
        by_conn = {s.connector: s for s in self.seats}
        for d in displays:
            if d.connector not in by_conn:
                s = Seat(d.connector)
                self.seats.append(s)
                by_conn[d.connector] = s
        disp = {d.connector: d for d in displays}
        for s in self.seats:
            s.display = disp.get(s.connector)
            for d in s.devs:
                hw = devices.get(d.ident)
                d.present = hw is not None
                if hw:
                    d.name = hw.name
                    d.kind = hw.kind if hw.kind != "usbd" or d.kind == "usbd" else d.kind

    def assigned(self):
        return {d.ident for s in self.seats for d in s.devs}

    def assign(self, seat, dev):
        for s in self.seats:
            s.devs = [d for d in s.devs if d.ident != dev.ident]
        seat.devs.append(dev)
        seat.devs.sort(key=lambda d: KIND_ORDER.get(d.kind, 9))
        seat.enabled = True

    def to_text(self):
        out = ["# Written by multiseat-gui. Hand edits are fine; the GUI re-reads this file.",
               "# One block per seat (= one monitor). A seat line starting with # is disabled.", ""]
        for s in self.seats:
            if not s.enabled:
                out += ["#" + s.connector, ""]
                continue
            out.append(s.connector)
            for d in s.devs:
                out.append("\t" + ("%s %s" % (d.kind, d.ident)).ljust(29) + " #- " +
                           (d.name or "").replace("\n", " "))
            for k in ("comp", "open", "spkr"):
                if s.extra.get(k):
                    out.append("\t%s %s" % (k, s.extra[k]))
            out.append("")
        return "\n".join(out)

    def save(self):
        if not self.path:
            raise RuntimeError("No GPU outputs found, so there is no config file name.")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            shutil.copy2(self.path, self.path + ".bak")
        tmp = self.path + ".tmp"
        Path(tmp).write_text(self.to_text())
        if os.path.exists(self.path):
            shutil.copymode(self.path, tmp)
        os.replace(tmp, self.path)


def match_gdk_monitor(connector, gdk_monitors):
    """Best-effort map of kernel connector (card0-HDMI-A-1) to an X11 output name."""
    rest = re.sub(r"^card\d+-", "", connector)
    typ = rest.split("-")[0].lower()
    idx = re.findall(r"\d+", rest)[-1:]
    same = [m for m in gdk_monitors if (m.get_model() or "").lower().startswith(typ)]
    if len(same) == 1:
        return same[0]
    for m in same:
        if re.findall(r"\d+", m.get_model() or "")[-1:] == idx:
            return m
    return None


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class IdentifyDialog(Gtk.Dialog):
    def __init__(self, parent, title, on_device):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.set_default_size(460, 320)
        self.on_device = on_device
        self.seen = set()
        self.add_button("Done", Gtk.ResponseType.OK)
        box = self.get_content_area()
        box.set_spacing(8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(14)
        head = Gtk.Label()
        head.set_markup("<b>%s</b>\n\nOn the keyboard and mouse that belong to this seat, press a key "
                        "and move or click the mouse. Each device is added as soon as it is "
                        "detected. Press <b>Done</b> when finished." % esc(title))
        head.set_line_wrap(True)
        head.set_xalign(0)
        box.pack_start(head, False, False, 0)
        self.lst = Gtk.ListBox()
        self.lst.set_selection_mode(Gtk.SelectionMode.NONE)
        sw = Gtk.ScrolledWindow()
        sw.add(self.lst)
        box.pack_start(sw, True, True, 0)
        self.sniffer = InputSniffer(self._event)
        self.sniffer.start()
        self.show_all()

    def _event(self, kind, ident, name):
        if (ident, kind) in self.seen:
            return False
        self.seen.add((ident, kind))
        row = Gtk.Label(label="%s  %s   (%s)" % ("⌨" if kind == "kbd" else "🖱", name, ident))
        row.set_xalign(0)
        self.lst.add(row)
        row.show()
        self.on_device(kind, ident, name)
        return False

    def finish(self):
        self.sniffer.stop_ev.set()


# ---------------------------------------------------------------------------
# Seats page
# ---------------------------------------------------------------------------

class SeatsPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.win = win
        self.cfg = Config()
        self.displays, self.devices = [], {}
        self.dirty = False
        for m in ("top", "bottom", "start", "end"):
            getattr(self, "set_margin_" + m)(10)

        bar = Gtk.Box(spacing=6)
        self.pack_start(bar, False, False, 0)
        for label, cb, tip in [
            ("🔄  Detect hardware", lambda *_: self.detect(), "Re-scan monitors and USB devices"),
            ("📺  Show seat numbers on monitors", lambda *_: self.identify_monitors(),
             "Briefly show a big number on each monitor"),
            ("✨  Auto-assign", lambda *_: self.auto_assign(),
             "Give each seat without a keyboard/mouse the next free one (a guess - verify with Identify)"),
        ]:
            b = Gtk.Button(label=label)
            b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            bar.pack_start(b, False, False, 0)
        self.save_btn = Gtk.Button(label="💾  Save configuration")
        self.save_btn.get_style_context().add_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self.save())
        bar.pack_end(self.save_btn, False, False, 0)
        self.state_lbl = Gtk.Label()
        bar.pack_end(self.state_lbl, False, False, 8)

        self.info = Gtk.Label(xalign=0)
        self.info.set_line_wrap(True)
        self.pack_start(self.info, False, False, 0)

        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_max_children_per_line(3)
        self.flow.set_min_children_per_line(1)
        self.flow.set_homogeneous(True)
        self.flow.set_row_spacing(10)
        self.flow.set_column_spacing(10)
        sw = Gtk.ScrolledWindow()
        sw.add(self.flow)
        self.pack_start(sw, True, True, 0)
        self.detect()

    # -- data ---------------------------------------------------------------
    def detect(self):
        self.displays = detect_displays()
        self.devices = detect_inputs()
        path = conf_path()
        if self.dirty and self.cfg.path == path:
            pass
        else:
            self.cfg = Config.load(path) if path else Config()
            self.cfg.path = path
            self.dirty = False
        self.cfg.sync(self.displays, self.devices)
        self.rebuild()

    def reload_from_file(self):
        path = self.cfg.path or conf_path()
        self.cfg = Config.load(path) if path else Config()
        self.cfg.path = path
        self.cfg.sync(self.displays, self.devices)
        self.dirty = False
        self.rebuild()

    def mark_dirty(self):
        self.dirty = True
        self.rebuild()

    def save(self):
        try:
            self.cfg.save()
        except Exception as e:
            self.win.message("Could not save", str(e), Gtk.MessageType.ERROR)
            return
        self.dirty = False
        self.rebuild()
        self.win.toast("Saved %s" % self.cfg.path)
        self.win.refresh_status()

    def seat_connectors(self):
        return [s.connector for s in self.cfg.seats if s.enabled]

    # -- actions ------------------------------------------------------------
    def assign_dev(self, seat, dev):
        self.cfg.assign(seat, Dev(dev.kind, dev.ident, dev.name, dev.present))
        self.mark_dirty()

    def unassign(self, seat, dev):
        seat.devs = [d for d in seat.devs if d is not dev]
        self.mark_dirty()

    def identify(self, seat, number):
        def on_dev(kind, ident, name):
            hw = self.devices.get(ident)
            k = hw.kind if hw and hw.kind != "usbd" else \
                ("ps2" if ident.startswith("serio") else "usb") + ("k" if kind == "kbd" else "m")
            self.cfg.assign(seat, Dev(k, ident, hw.name if hw else name, True))
            self.dirty = True
        dlg = IdentifyDialog(self.win, "Seat %d - %s" % (number, seat.connector), on_dev)
        dlg.run()
        dlg.finish()
        dlg.destroy()
        self.rebuild()

    def auto_assign(self):
        used = self.cfg.assigned()
        free = [d for d in self.devices.values() if d.ident not in used]
        kbs = [d for d in free if d.kind in ("usbk", "ps2k")]
        mice = [d for d in free if d.kind in ("usbm", "ps2m")]
        for s in self.cfg.seats:
            if s.display is None:
                continue
            if not any(d.kind in ("usbk", "ps2k") for d in s.devs) and kbs:
                self.cfg.assign(s, kbs.pop(0))
            if not any(d.kind in ("usbm", "ps2m") for d in s.devs) and mice:
                self.cfg.assign(s, mice.pop(0))
        self.mark_dirty()

    def identify_monitors(self):
        display = Gdk.Display.get_default()
        mons = [display.get_monitor(i) for i in range(display.get_n_monitors())]
        wins = []
        for i, mon in enumerate(mons):
            label = None
            for n, s in enumerate(self.cfg.seats):
                if s.display and match_gdk_monitor(s.connector, [mon]) is mon:
                    label = "Seat %d\n%s" % (n + 1, s.connector)
                    break
            if label is None:
                label = "?\n%s" % (mon.get_model() or "monitor %d" % i)
            w = Gtk.Window()
            w.set_decorated(False)
            w.get_style_context().add_class("overlaywin")
            lbl = Gtk.Label(label=label)
            lbl.set_justify(Gtk.Justification.CENTER)
            lbl.get_style_context().add_class("overlaylbl")
            w.add(lbl)
            geo = mon.get_geometry()
            w.move(geo.x, geo.y)
            w.set_default_size(geo.width, geo.height)
            w.show_all()
            w.fullscreen_on_monitor(display.get_default_screen(), i)
            wins.append(w)

        def close():
            for w in wins:
                w.destroy()
            return False
        GLib.timeout_add_seconds(4, close)

    # -- UI -----------------------------------------------------------------
    def rebuild(self):
        for c in self.flow.get_children():
            self.flow.remove(c)
        if not self.cfg.path:
            self.info.set_text("No GPU outputs were found, so there is nothing to configure yet.")
        else:
            self.info.set_markup(
                "<span alpha='70%%'>A seat is one monitor plus its own keyboard, mouse and USB devices. "
                "Use <b>Identify</b> on a seat and press a key on its keyboard.   Config: %s</span>"
                % esc(self.cfg.path))
        n_seats = 0
        for i, s in enumerate(self.cfg.seats):
            self.flow.add(self._card(i, s))
            n_seats += s.enabled
        self.flow.add(self._unassigned_card())
        self.flow.show_all()
        self.state_lbl.set_markup("<span foreground='#e5a50a'>● unsaved changes</span>" if self.dirty
                                  else "<span alpha='60%%'>%d seat(s) enabled</span>" % n_seats)
        self.win.seats_changed()

    def _card(self, idx, seat):
        frame = Gtk.Frame()
        ctx = frame.get_style_context()
        ctx.add_class("seatcard")
        if not seat.enabled:
            ctx.add_class("off")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(10)
        frame.add(box)

        head = Gtk.Box(spacing=8)
        t = Gtk.Label()
        t.set_markup("<span size='x-large' weight='bold'>Seat %d</span>" % (idx + 1))
        head.pack_start(t, False, False, 0)
        sw = Gtk.Switch()
        sw.set_active(seat.enabled)
        sw.set_valign(Gtk.Align.CENTER)

        def toggled(w, _p, s=seat):
            s.enabled = w.get_active()
            GLib.idle_add(self.mark_dirty)
        sw.connect("notify::active", toggled)
        head.pack_end(sw, False, False, 0)
        box.pack_start(head, False, False, 0)

        mon = Gtk.Label(xalign=0)
        if seat.display:
            mon.set_markup("📺  <b>%s</b> %s\n<span size='small' alpha='60%%'>%s</span>" %
                           (esc(seat.display.label), esc(seat.display.mode), esc(seat.connector)))
        else:
            mon.set_markup("📺  <span foreground='#e5a50a'>monitor not connected</span>\n"
                           "<span size='small' alpha='60%%'>%s</span>" % esc(seat.connector))
        box.pack_start(mon, False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 2)

        if not seat.devs:
            l = Gtk.Label(label="No devices assigned", xalign=0)
            l.get_style_context().add_class("dim")
            box.pack_start(l, False, False, 0)
        for d in seat.devs:
            box.pack_start(self._dev_row(seat, d), False, False, 0)
        if seat.enabled and not any(d.kind in ("usbk", "ps2k") for d in seat.devs):
            w = Gtk.Label(xalign=0)
            w.set_markup("<span foreground='#e5a50a' size='small'>⚠ no keyboard assigned</span>")
            box.pack_start(w, False, False, 0)

        btns = Gtk.Box(spacing=6)
        ib = Gtk.Button(label="🎯  Identify…")
        ib.set_tooltip_text("Press a key / move the mouse of the devices that belong to this seat")
        ib.connect("clicked", lambda *_, s=seat, n=idx + 1: self.identify(s, n))
        btns.pack_start(ib, True, True, 0)
        menu = Gtk.Menu()
        owner = {d.ident: i + 1 for i, s2 in enumerate(self.cfg.seats) for d in s2.devs}
        mine = {d.ident for d in seat.devs}
        n_items = 0
        for d in sorted(self.devices.values(), key=lambda x: KIND_ORDER.get(x.kind, 9)):
            if d.ident in mine:
                continue
            suffix = "" if d.ident not in owner else "   (move from Seat %d)" % owner[d.ident]
            it = Gtk.MenuItem(label="%s %s%s" % (DEVICE_ICON[d.kind], d.name, suffix))
            it.connect("activate", lambda _w, s=seat, dd=d: self.assign_dev(s, dd))
            menu.append(it)
            n_items += 1
        if not n_items:
            it = Gtk.MenuItem(label="(no other devices)")
            it.set_sensitive(False)
            menu.append(it)
        menu.show_all()
        mb = Gtk.MenuButton(label="➕  Add device")
        mb.set_popup(menu)
        btns.pack_start(mb, True, True, 0)
        box.pack_start(btns, False, False, 2)

        exp = Gtk.Expander(label="Advanced")
        g = Gtk.Grid(row_spacing=4, column_spacing=6)
        for r, (key, label, hint) in enumerate([
                ("comp", "Compositor", "default: labwc ; sfwbar ; pcmanfm-qt --desktop"),
                ("open", "Start app", "e.g. firefox   (separate several with ;)")]):
            g.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
            e = Gtk.Entry()
            e.set_text(seat.extra.get(key, ""))
            e.set_placeholder_text(hint)
            e.set_hexpand(True)

            def changed(w, s=seat, k=key):
                s.extra[k] = w.get_text().strip()
                self.dirty = True
                self.state_lbl.set_markup("<span foreground='#e5a50a'>● unsaved changes</span>")
            e.connect("changed", changed)
            g.attach(e, 1, r, 1, 1)
        exp.add(g)
        box.pack_start(exp, False, False, 0)
        return frame

    def _dev_row(self, seat, d):
        row = Gtk.Box(spacing=4)
        txt = "%s  %s  <span size='small' alpha='60%%'>%s</span>" % (
            DEVICE_ICON.get(d.kind, "?"), esc(d.name or d.ident), esc(d.ident))
        if not d.present:
            txt += "  <span foreground='#e5a50a' size='small'>(not connected)</span>"
        l = Gtk.Label(xalign=0)
        l.set_markup(txt)
        l.set_ellipsize(Pango.EllipsizeMode.END)
        row.pack_start(l, True, True, 0)
        b = Gtk.Button(label="✕")
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_tooltip_text("Remove from this seat")
        b.connect("clicked", lambda *_: self.unassign(seat, d))
        row.pack_end(b, False, False, 0)
        return row

    def _unassigned_card(self):
        frame = Gtk.Frame()
        frame.get_style_context().add_class("seatcard")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(10)
        frame.add(box)
        t = Gtk.Label()
        t.set_markup("<span size='large' weight='bold'>Not assigned</span>")
        t.set_xalign(0)
        box.pack_start(t, False, False, 0)
        used = self.cfg.assigned()
        free = [d for d in self.devices.values() if d.ident not in used]
        if not free:
            l = Gtk.Label(label="All detected devices are assigned.", xalign=0)
            l.get_style_context().add_class("dim")
            box.pack_start(l, False, False, 0)
        for d in free:
            l = Gtk.Label(xalign=0)
            l.set_markup("%s  %s  <span size='small' alpha='60%%'>%s</span>" %
                         (DEVICE_ICON[d.kind], esc(d.name), esc(d.ident)))
            l.set_ellipsize(Pango.EllipsizeMode.END)
            box.pack_start(l, False, False, 0)
        n = Gtk.Label(xalign=0)
        n.set_line_wrap(True)
        n.set_markup("<span size='small' alpha='60%'>Devices left here stay on the main seat (seat0). "
                     "🔌 = other USB device (e.g. a smartcard reader), passed to the seat's user.</span>")
        box.pack_start(n, False, False, 4)
        return frame


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Multiseat Control Panel")
        self.set_default_size(1040, 720)
        self.busy = False
        self.script = find_script()
        self.status = {}
        self.status_lock = False
        if self.script and not os.access(self.script, os.X_OK):
            try:
                os.chmod(self.script, os.stat(self.script).st_mode | 0o111)
            except OSError:
                pass
        self._fix_launcher()

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title("Multiseat Control Panel")
        self.spinner = Gtk.Spinner()
        hb.pack_end(self.spinner)
        self.set_titlebar(hb)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        self.banner = Gtk.InfoBar()
        self.banner.set_message_type(Gtk.MessageType.WARNING)
        self.banner_lbl = Gtk.Label(xalign=0)
        self.banner_lbl.set_line_wrap(True)
        self.banner.get_content_area().add(self.banner_lbl)
        self.banner.add_button("Locate script…", 1)
        self.banner.connect("response", lambda *_: self.locate_script())
        self.banner.set_no_show_all(True)
        root.pack_start(self.banner, False, False, 0)

        self.toast_bar = Gtk.InfoBar()
        self.toast_bar.set_message_type(Gtk.MessageType.INFO)
        self.toast_lbl = Gtk.Label()
        self.toast_bar.get_content_area().add(self.toast_lbl)
        self.toast_bar.set_no_show_all(True)
        root.pack_start(self.toast_bar, False, False, 0)
        self._toast_id = None

        self.nb = Gtk.Notebook()
        root.pack_start(self.nb, True, True, 0)

        self.seats_page = SeatsPage(self)
        self.nb.append_page(self._build_home(), Gtk.Label(label="🏠  Home"))
        self.nb.append_page(self.seats_page, Gtk.Label(label="🪑  Seats"))
        self.nb.append_page(self._build_logs(), Gtk.Label(label="📜  Logs"))
        self.nb.append_page(self._build_advanced(), Gtk.Label(label="🛠  Advanced / Recovery"))

        self.out_exp = Gtk.Expander(label="Command output")
        self.out_view = Gtk.TextView()
        self.out_view.set_editable(False)
        self.out_view.set_monospace(True)
        self.out_buf = self.out_view.get_buffer()
        self.out_mark = self.out_buf.create_mark("end", self.out_buf.get_end_iter(), False)
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(170)
        sw.add(self.out_view)
        self.out_exp.add(sw)
        root.pack_start(self.out_exp, False, False, 0)

        self.refresh_status()
        GLib.timeout_add_seconds(4, self._tick)

    # -- helpers ------------------------------------------------------------
    def _fix_launcher(self):
        p = "/usr/share/applications/multiseat-gui.desktop"
        t = rd(p)
        if "pkexec multiseat-gui" in t:
            try:
                Path(p).write_text(t.replace("pkexec multiseat-gui", "multiseat-gui"))
            except OSError:
                pass

    def toast(self, text):
        self.toast_lbl.set_text(text)
        self.toast_bar.show()
        self.toast_lbl.show()
        if self._toast_id:
            GLib.source_remove(self._toast_id)
        self._toast_id = GLib.timeout_add_seconds(5, self._hide_toast)

    def _hide_toast(self):
        self.toast_bar.hide()
        self._toast_id = None
        return False

    def message(self, title, text, mtype=Gtk.MessageType.INFO):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=mtype,
                              buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(text)
        d.run()
        d.destroy()

    def confirm(self, title, text, ok="Yes", mtype=Gtk.MessageType.WARNING):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=mtype,
                              buttons=Gtk.ButtonsType.NONE, text=title)
        d.format_secondary_text(text)
        d.add_button("Cancel", Gtk.ResponseType.CANCEL)
        d.add_button(ok, Gtk.ResponseType.OK)
        r = d.run()
        d.destroy()
        return r == Gtk.ResponseType.OK

    def locate_script(self):
        d = Gtk.FileChooserDialog(title="Select multiseat-mint.sh", transient_for=self,
                                  action=Gtk.FileChooserAction.OPEN)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
        if d.run() == Gtk.ResponseType.OK:
            self.script = d.get_filename()
            try:
                os.chmod(self.script, os.stat(self.script).st_mode | 0o111)
                Path(GUI_PATH_FILE).write_text(self.script + "\n")
            except OSError:
                pass
            self.refresh_status()
        d.destroy()

    def need_script(self):
        if not self.script:
            self.locate_script()
        return self.script

    def sc(self, *args):
        return [self.script] + list(args)

    def seats_changed(self):
        if hasattr(self, "home_seats_lbl"):
            self.update_home_summary()

    # -- command runner -------------------------------------------------------
    def log(self, text):
        self.out_buf.insert(self.out_buf.get_end_iter(), text)
        self.out_buf.move_mark(self.out_mark, self.out_buf.get_end_iter())
        self.out_view.scroll_mark_onscreen(self.out_mark)
        return False

    def run_cmds(self, title, cmds, after=None, stop_on_error=True):
        if self.busy:
            self.toast("Another task is still running…")
            return
        if any(c[0] == self.script for c in cmds) and not self.need_script():
            return
        self.busy = True
        self.spinner.start()
        self.out_exp.set_expanded(True)
        self.log("\n━━ %s ━━\n" % title)
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", TERM="dumb")
        cwd = os.path.dirname(self.script) if self.script else "/"

        def worker():
            ok = True
            for cmd in cmds:
                GLib.idle_add(self.log, "$ %s\n" % " ".join(cmd))
                try:
                    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                                         cwd=cwd, env=env, errors="replace")
                    for line in p.stdout:
                        GLib.idle_add(self.log, ANSI.sub("", line))
                    rc = p.wait()
                except Exception as e:  # noqa: BLE001
                    GLib.idle_add(self.log, "failed to start: %s\n" % e)
                    rc = 127
                if rc != 0:
                    ok = False
                    GLib.idle_add(self.log, "[exit code %d]\n" % rc)
                    if stop_on_error:
                        break
            GLib.idle_add(self._done, title, ok, after)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, title, ok, after):
        self.busy = False
        self.spinner.stop()
        self.log("━━ %s: %s ━━\n" % (title, "finished" if ok else "reported errors (see above)"))
        self.toast("%s: %s" % (title, "done" if ok else "had errors - see Command output"))
        self.refresh_status()
        if after:
            after(ok)
        return False

    # -- status ---------------------------------------------------------------
    def _tick(self):
        if not self.busy:
            self.refresh_status()
        return True

    def refresh_status(self):
        if self.status_lock:
            return
        self.status_lock = True
        seats = self.seats_page.seat_connectors()
        threading.Thread(target=self._gather, args=(seats,), daemon=True).start()

    def _gather(self, seats):
        st = {}
        st["comp"] = {}
        for name, kind, pat, _ in COMPONENTS:
            if kind == "bin":
                st["comp"][name] = os.path.exists("/usr/local/bin/" + pat)
            else:
                st["comp"][name] = bool(glob.glob("/usr/local/lib/" + pat) or
                                        glob.glob("/usr/local/lib/*-linux-gnu/" + pat))
        st["enabled"] = run(["systemctl", "is-enabled", "multiseat"])[1].strip() or "not installed"
        st["active"] = run(["systemctl", "is-active", "multiseat"])[1].strip()
        st["target"] = run(["systemctl", "get-default"])[1].strip()
        st["dm"] = run(["systemctl", "is-active", "display-manager"])[1].strip() == "active"
        st["running"] = [c for c in seats if
                         run(["systemctl", "is-active", "multiseat-%s.service" % c])[1].strip() == "active"]
        try:
            st["leases"] = len([f for f in os.listdir(DLM_RUN) if not f.endswith(".lock")])
        except OSError:
            st["leases"] = 0
        st["failsafe"] = self.failsafe_state()
        GLib.idle_add(self._apply_status, st)

    def _apply_status(self, st):
        self.status = st
        self.status_lock = False
        ok = "<span foreground='#26a269'>✔</span>"
        bad = "<span foreground='#c01c28'>✘</span>"
        for name, lbl in self.comp_lbls.items():
            lbl.set_markup("%s  %s" % (ok if st["comp"][name] else bad, esc(name)))
        miss = [n for n, v in st["comp"].items() if not v]
        self.install_btn.set_label("🔧  Rebuild everything" if not miss else "📦  Install / build components")
        self.comp_note.set_text("" if not miss else "Missing: " + ", ".join(miss))
        self.v_service.set_markup("%s  (%s)" % (esc(st["enabled"]), esc(st["active"])))
        self.v_target.set_text(st["target"] or "?")
        self.v_dm.set_markup("<span foreground='#e5a50a'>running</span>" if st["dm"] else "not running")
        n = len(self.seats_page.seat_connectors())
        self.v_seats.set_text("%d of %d running   (%d GPU lease(s))" % (len(st["running"]), n, st["leases"]))
        self.failsafe_sw.handler_block(self._fs_handler)
        self.failsafe_sw.set_sensitive(st["failsafe"] is not None)
        self.failsafe_sw.set_active(bool(st["failsafe"]))
        self.failsafe_sw.handler_unblock(self._fs_handler)

        if not self.script:
            self.banner_lbl.set_text("multiseat-mint.sh was not found. Locate it so the buttons can run it.")
            self.banner.show()
            self.banner_lbl.show()
        else:
            self.banner.hide()
        return False

    # -- failsafe toggle (the commented line at the top of the script) ---------
    FS_RE = re.compile(r'^([ \t]*)(#[ \t]*)?(\[\[ "-S" == "\$1" \]\] && systemctl disable multiseat)[ \t]*$', re.M)

    def failsafe_state(self):
        if not self.script:
            return None
        m = self.FS_RE.search(rd(self.script))
        return None if not m else m.group(2) is None

    def set_failsafe(self, on):
        text = Path(self.script).read_text()
        new = self.FS_RE.sub(lambda m: m.group(1) + ("" if on else "# ") + m.group(3), text, count=1)
        Path(self.script).write_text(new)

    # ------------------------------------------------------------------------
    # Home page
    # ------------------------------------------------------------------------
    def _frame(self, title):
        f = Gtk.Frame()
        f.set_label_widget(Gtk.Label(label=title))
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(b, "set_margin_" + m)(10)
        f.add(b)
        return f, b

    def _build_home(self):
        sw = Gtk.ScrolledWindow()
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(page, "set_margin_" + m)(14)
        sw.add(page)

        f, b = self._frame("  1 · Components  ")
        self.comp_lbls = {}
        grid = Gtk.Grid(column_spacing=24, row_spacing=2)
        for i, (name, _, _, desc) in enumerate(COMPONENTS):
            l = Gtk.Label(xalign=0)
            self.comp_lbls[name] = l
            grid.attach(l, i % 2, i // 2, 1, 1)
        b.pack_start(grid, False, False, 0)
        self.comp_note = Gtk.Label(xalign=0)
        b.pack_start(self.comp_note, False, False, 0)
        self.install_btn = Gtk.Button(label="📦  Install / build components")
        self.install_btn.set_halign(Gtk.Align.START)
        self.install_btn.connect("clicked", lambda *_: self.do_install())
        b.pack_start(self.install_btn, False, False, 0)
        hint = Gtk.Label(xalign=0)
        hint.set_line_wrap(True)
        hint.set_markup("<span size='small' alpha='60%'>Runs <tt>multiseat-mint.sh -b</tt> (apt packages, git clone, "
                        "patch, compile). Takes a while; progress shows in <i>Command output</i> below.</span>")
        b.pack_start(hint, False, False, 0)
        page.pack_start(f, False, False, 0)

        f, b = self._frame("  2 · Seats  ")
        self.home_seats_lbl = Gtk.Label(xalign=0)
        self.home_seats_lbl.set_line_wrap(True)
        b.pack_start(self.home_seats_lbl, False, False, 0)
        sb = Gtk.Button(label="🪑  Set up seats…")
        sb.set_halign(Gtk.Align.START)
        sb.connect("clicked", lambda *_: self.nb.set_current_page(1))
        b.pack_start(sb, False, False, 0)
        page.pack_start(f, False, False, 0)

        f, b = self._frame("  3 · Run  ")
        g = Gtk.Grid(column_spacing=14, row_spacing=3)
        self.v_service, self.v_target, self.v_dm, self.v_seats = (Gtk.Label(xalign=0) for _ in range(4))
        for r, (k, w) in enumerate([("Boot service (multiseat)", self.v_service),
                                    ("Default boot target", self.v_target),
                                    ("Login screen (display manager)", self.v_dm),
                                    ("Seats", self.v_seats)]):
            l = Gtk.Label(label=k, xalign=0)
            l.get_style_context().add_class("dim")
            g.attach(l, 0, r, 1, 1)
            g.attach(w, 1, r, 1, 1)
        b.pack_start(g, False, False, 0)
        row = Gtk.Box(spacing=8)
        for label, cb in [("▶  Start now", self.do_start), ("■  Stop", self.do_stop),
                          ("⏻  Enable at boot…", self.do_enable), ("↩  Back to normal login…", self.do_restore)]:
            bt = Gtk.Button(label=label)
            bt.connect("clicked", lambda *_, c=cb: c())
            row.pack_start(bt, False, False, 0)
        b.pack_start(row, False, False, 4)
        fs = Gtk.Box(spacing=8)
        self.failsafe_sw = Gtk.Switch()
        self._fs_handler = self.failsafe_sw.connect("notify::active", self._on_failsafe)
        fs.pack_start(self.failsafe_sw, False, False, 0)
        l = Gtk.Label(xalign=0)
        l.set_line_wrap(True)
        l.set_markup("<b>Boot test mode</b>  <span alpha='70%'>- the service disables itself every time it starts, "
                     "so a freeze at boot cannot repeat. Re-enable it before each boot test.</span>")
        fs.pack_start(l, True, True, 0)
        b.pack_start(fs, False, False, 0)
        page.pack_start(f, False, False, 0)
        return sw

    def update_home_summary(self):
        sp = self.seats_page
        en = [s for s in sp.cfg.seats if s.enabled]
        free = [d for d in sp.devices.values() if d.ident not in sp.cfg.assigned()]
        txt = "%d monitor(s) detected · %d seat(s) enabled · %d device(s) not assigned" % (
            len(sp.displays), len(en), len(free))
        if sp.dirty:
            txt += "  ·  <span foreground='#e5a50a'>unsaved changes on the Seats tab</span>"
        self.home_seats_lbl.set_markup(txt)

    def _on_failsafe(self, sw, _p):
        try:
            self.set_failsafe(sw.get_active())
            self.toast("Boot test mode %s" % ("on" if sw.get_active() else "off"))
        except Exception as e:  # noqa: BLE001
            self.message("Could not edit the script", str(e), Gtk.MessageType.ERROR)

    # -- home actions -----------------------------------------------------------
    def do_install(self):
        if not self.need_script():
            return

        def after(_ok):
            self._gather_sync_report()
        self.run_cmds("Install / build components", [self.sc("-b")], after)

    def _gather_sync_report(self):
        def check():
            miss = [n for n, kind, pat, _ in COMPONENTS
                    if not (os.path.exists("/usr/local/bin/" + pat) if kind == "bin" else
                            glob.glob("/usr/local/lib/" + pat) or glob.glob("/usr/local/lib/*-linux-gnu/" + pat))]
            if miss:
                self.message("Some components are still missing",
                             "Not found after the build: %s\n\nScroll through the command output for the first "
                             "error (often a missing -dev package or a failed meson step). You can rebuild one "
                             "component from Advanced → Build." % ", ".join(miss), Gtk.MessageType.WARNING)
            else:
                self.message("All components installed",
                             "Next: open the Seats tab, assign devices and save.", Gtk.MessageType.INFO)
            return False
        GLib.idle_add(check)

    def _save_first(self):
        sp = self.seats_page
        if sp.dirty:
            if self.confirm("Unsaved seat changes", "Save the seat configuration first?", "Save"):
                sp.save()
        if not sp.cfg.path or not os.path.exists(sp.cfg.path):
            self.message("No configuration yet", "Open the Seats tab, assign devices and press Save first.")
            return False
        if not sp.seat_connectors():
            self.message("No seats enabled", "Enable at least one seat on the Seats tab.")
            return False
        return True

    def do_start(self):
        if not self.need_script() or not self._save_first():
            return
        if self.status.get("dm"):
            if not self.confirm(
                    "A login screen is running on this GPU",
                    "multiseat needs exclusive access to the GPU. While lightdm (or another display manager) is "
                    "running, starting seats usually fails or freezes the screen.\n\nThe reliable way is "
                    "'Enable at boot…' and a reboot. Start anyway?", "Start anyway"):
                return
        self.run_cmds("Start multiseat", [self.sc("-S")])

    def do_stop(self):
        if not self.need_script():
            return
        flag = "-Q" if self.status.get("dm") else "-q"
        self.run_cmds("Stop multiseat", [self.sc(flag)])

    def do_enable(self):
        if not self.need_script() or not self._save_first():
            return
        d = Gtk.Dialog(title="Enable multiseat at boot", transient_for=self, modal=True)
        d.add_button("Cancel", Gtk.ResponseType.CANCEL)
        d.add_button("Enable", Gtk.ResponseType.OK)
        box = d.get_content_area()
        box.set_spacing(8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(14)
        l = Gtk.Label(xalign=0)
        l.set_line_wrap(True)
        l.set_text("This installs the multiseat service and enables it. Recommended options:")
        box.pack_start(l, False, False, 0)
        c1 = Gtk.CheckButton(label="Boot to text mode (multi-user.target) so the normal login screen does not "
                                   "compete for the GPU")
        c1.set_active(True)
        c2 = Gtk.CheckButton(label="First-boot safety: disable the service again as soon as it starts "
                                   "(a freeze can't repeat; re-enable here for each test)")
        c2.set_active(True)
        for c in (c1, c2):
            c.get_child().set_line_wrap(True)
            box.pack_start(c, False, False, 0)
        n = Gtk.Label(xalign=0)
        n.set_line_wrap(True)
        n.set_markup("<span size='small' alpha='70%'>If anything goes wrong: Ctrl+Alt+F3 or GRUB → recovery mode, "
                     "then run  <tt>multiseat-uninstall</tt>  or  <tt>systemctl set-default graphical.target</tt>.</span>")
        box.pack_start(n, False, False, 0)
        d.show_all()
        r = d.run()
        multi, safe = c1.get_active(), c2.get_active()
        d.destroy()
        if r != Gtk.ResponseType.OK:
            return
        try:
            if self.failsafe_state() is not None:
                self.set_failsafe(safe)
        except OSError as e:
            self.toast("Could not change boot test mode: %s" % e)
        cmds = [self.sc("-l"), ["systemctl", "enable", "multiseat"]]
        if multi:
            cmds.append(["systemctl", "set-default", "multi-user.target"])
        self.run_cmds("Enable at boot", cmds, lambda ok: ok and self.toast("Enabled. Reboot to test."))

    def do_restore(self):
        if not self.need_script():
            return
        if not self.confirm("Go back to the normal login screen?",
                            "Stops multiseat, disables it at boot and sets the default boot target to the "
                            "graphical login. Your seat configuration is kept.", "Restore"):
            return
        cmds = [self.sc("-Q"), ["systemctl", "disable", "multiseat"],
                ["systemctl", "set-default", "graphical.target"]]
        self.run_cmds("Restore normal login", cmds,
                      lambda ok: self.toast("Done. Reboot to get the normal login screen."), stop_on_error=False)

    # ------------------------------------------------------------------------
    # Logs page
    # ------------------------------------------------------------------------
    def _build_logs(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(10)
        bar = Gtk.Box(spacing=6)
        self.prev_boot = Gtk.CheckButton(label="Previous boot")
        self.log_all = Gtk.CheckButton(label="Show everything (no filter)")
        rb = Gtk.Button(label="🔄  Refresh")
        rb.connect("clicked", lambda *_: self.load_journal())
        for w in (rb, self.prev_boot, self.log_all):
            bar.pack_start(w, False, False, 0)
        box.pack_start(bar, False, False, 0)
        self.journal_view = Gtk.TextView()
        self.journal_view.set_editable(False)
        self.journal_view.set_monospace(True)
        sw = Gtk.ScrolledWindow()
        sw.add(self.journal_view)
        box.pack_start(sw, True, True, 0)
        self.nb.connect("switch-page", lambda nb, pg, n: n == 2 and self.load_journal())
        return box

    def load_journal(self):
        prev = self.prev_boot.get_active()
        allv = self.log_all.get_active()

        def work():
            rc, out, err = run(["journalctl", "--no-hostname", "--no-pager", "-b", "-1" if prev else "0",
                                "-n", "3000"], timeout=30)
            lines = (out or err).splitlines()
            if not allv:
                lines = [l for l in lines if re.search(r"multiseat|labwc|seat|dlm|drm-lease|plymouth", l, re.I)]
            GLib.idle_add(self._set_journal, "\n".join(lines[-600:]) or "(nothing found)")
        threading.Thread(target=work, daemon=True).start()

    def _set_journal(self, text):
        buf = self.journal_view.get_buffer()
        buf.set_text(ANSI.sub("", text))
        self.journal_view.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 0)
        return False

    # ------------------------------------------------------------------------
    # Advanced page
    # ------------------------------------------------------------------------
    def _build_advanced(self):
        sw = Gtk.ScrolledWindow()
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(page, "set_margin_" + m)(14)
        sw.add(page)

        f, b = self._frame("  Script commands  ")
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        cmds = [
            ("-l", "Create service + library links", "Re-creates multiseat.service and /usr symlinks"),
            ("-gp", "Install apt packages", "Only the package installation step"),
            ("-g", "Update source code (git)", "Clone/update repos, no compile"),
            ("-d", "Start DRM lease managers", "One drm-lease-manager per GPU"),
            ("-S", "Start multiseat (non-interactive)", "What the boot service runs"),
            ("-q", "Stop + restore tty1", "Stops seats and brings back the text console"),
            ("-Q", "Stop only", "Stops seats, leaves the console alone"),
            ("-f", "Flush seat assignments", "loginctl flush-devices - returns hardware to seat0"),
            ("-p", "Generate wlroots patch", "Developer option"),
        ]
        for i, (flag, label, tip) in enumerate(cmds):
            bt = Gtk.Button(label="%s   (%s)" % (label, flag))
            bt.set_tooltip_text(tip)
            bt.set_halign(Gtk.Align.FILL)
            bt.connect("clicked", lambda *_, f=flag, t=label: self.run_cmds(t, [self.sc(f)]) if self.need_script() else None)
            grid.attach(bt, i % 2, i // 2, 1, 1)
        b.pack_start(grid, False, False, 0)

        r = Gtk.Box(spacing=6)
        r.pack_start(Gtk.Label(label="Build one component:"), False, False, 0)
        self.build_combo = Gtk.ComboBoxText()
        for x in BUILD_IDS:
            self.build_combo.append_text(x)
        self.build_combo.set_active(1)
        r.pack_start(self.build_combo, False, False, 0)
        bb = Gtk.Button(label="Build (-b ID)")
        bb.connect("clicked", lambda *_: self.run_cmds("Build " + self.build_combo.get_active_text(),
                                                       [self.sc("-b", self.build_combo.get_active_text().split()[0])])
                   if self.need_script() else None)
        r.pack_start(bb, False, False, 0)
        b.pack_start(r, False, False, 0)

        r = Gtk.Box(spacing=6)
        r.pack_start(Gtk.Label(label="Restart one seat:"), False, False, 0)
        self.restart_combo = Gtk.ComboBoxText()
        r.pack_start(self.restart_combo, False, False, 0)
        rb = Gtk.Button(label="Restart (-r)")
        rb.connect("clicked", lambda *_: self._restart_seat())
        r.pack_start(rb, False, False, 0)
        b.pack_start(r, False, False, 0)
        self.nb.connect("switch-page", lambda nb, pg, n: n == 3 and self._fill_restart())
        page.pack_start(f, False, False, 0)

        f, b = self._frame("  Recovery  ")
        t = Gtk.Label(xalign=0)
        t.set_line_wrap(True)
        t.set_markup("If the screen freezes or login is broken, use a text console (Ctrl+Alt+F3, or GRUB → "
                     "Advanced options → recovery mode → root shell) and run any of these. "
                     "The GUI is not needed:")
        b.pack_start(t, False, False, 0)
        term = Gtk.Label(xalign=0, selectable=True)
        term.set_markup(
            "<tt>sudo ./multiseat-mint.sh -q        # stop everything, restore tty1\n"
            "sudo systemctl disable multiseat   # do not start at next boot\n"
            "sudo systemctl set-default graphical.target\n"
            "sudo multiseat-uninstall           # remove everything, restore single-seat\n"
            "sudo ./multiseat-mint.sh -j        # read the logs\n"
            "mount -o remount,rw /              # recovery mode: if / is read-only</tt>")
        b.pack_start(term, False, False, 4)
        row = Gtk.Box(spacing=8)
        ub = Gtk.Button(label="🗑  Uninstall everything (-u)…")
        ub.connect("clicked", lambda *_: self._uninstall())
        rb = Gtk.Button(label="⟳  Reboot…")
        rb.connect("clicked", lambda *_: self.confirm("Reboot now?", "The system will restart.", "Reboot") and
                   subprocess.Popen(["systemctl", "reboot"]))
        row.pack_start(ub, False, False, 0)
        row.pack_start(rb, False, False, 0)
        b.pack_start(row, False, False, 0)
        page.pack_start(f, False, False, 0)

        f, b = self._frame("  Raw config file  ")
        self.raw_path = Gtk.Label(xalign=0)
        b.pack_start(self.raw_path, False, False, 0)
        self.raw_buf = Gtk.TextBuffer()
        tv = Gtk.TextView(buffer=self.raw_buf)
        tv.set_monospace(True)
        s2 = Gtk.ScrolledWindow()
        s2.set_min_content_height(200)
        s2.add(tv)
        b.pack_start(s2, True, True, 0)
        row = Gtk.Box(spacing=8)
        for label, cb in [("Load", self._raw_load), ("Save", self._raw_save)]:
            bt = Gtk.Button(label=label)
            bt.connect("clicked", lambda *_, c=cb: c())
            row.pack_start(bt, False, False, 0)
        b.pack_start(row, False, False, 0)
        page.pack_start(f, False, False, 0)
        self.nb.connect("switch-page", lambda nb, pg, n: n == 3 and self._raw_load())
        return sw

    def _fill_restart(self):
        self.restart_combo.remove_all()
        for c in self.seats_page.seat_connectors():
            self.restart_combo.append_text(c)
        self.restart_combo.set_active(0)

    def _restart_seat(self):
        c = self.restart_combo.get_active_text()
        if c and self.need_script():
            self.run_cmds("Restart " + c, [self.sc("-r", c)])

    def _uninstall(self):
        if not self.need_script():
            return
        if self.confirm("Uninstall multiseat?",
                        "Removes the service, udev changes, auto-created seat users, /etc/multiseat "
                        "(your seat config) and restores the single-seat login.", "Uninstall"):
            self.run_cmds("Uninstall", [self.sc("-u")])

    def _raw_load(self):
        p = self.seats_page.cfg.path
        self.raw_path.set_text(p or "(no config path)")
        self.raw_buf.set_text(rd(p) if p else "")

    def _raw_save(self):
        p = self.seats_page.cfg.path
        if not p:
            return
        s, e = self.raw_buf.get_start_iter(), self.raw_buf.get_end_iter()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if os.path.exists(p):
                shutil.copy2(p, p + ".bak")
            Path(p).write_text(self.raw_buf.get_text(s, e, True))
        except OSError as ex:
            self.message("Could not save", str(ex), Gtk.MessageType.ERROR)
            return
        self.seats_page.reload_from_file()
        self.toast("Saved %s" % p)


def main():
    if os.environ.get("MULTISEAT_GUI_NOROOT") != "1":
        ensure_root()
    ok = Gtk.init_check()
    if not (ok[0] if isinstance(ok, tuple) else ok):
        print("Cannot open the display (DISPLAY/XAUTHORITY not set).")
        sys.exit(1)
    win = MainWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    win.banner.hide()
    win.toast_bar.hide()
    Gtk.main()


if __name__ == "__main__":
    main()
