#!/usr/bin/env python3
"""Multiseat Control Panel - GTK3 GUI for multiseat-mint.sh"""

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf

import subprocess
import threading
import os
import glob
import re
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
MULTISEAT_SCRIPT = SCRIPT_DIR / "multiseat-mint.sh"
CONF_DIR = Path("/etc/multiseat")
TMP_CONF = Path("/tmp/multiseat.conf")
DLM_RUN = Path("/var/local/run/drm-lease-manager")

CSS = b"""
.card {
    border: 2px solid #3584e4;
    border-radius: 8px;
    padding: 12px;
    margin: 6px;
}
.card-good  { border-color: #26a269; }
.card-warn  { border-color: #e5a50a; }
.card-error { border-color: #c01c28; }
.card-value { font-size: 28px; font-weight: bold; }
.card-label { font-size: 12px; color: #5e5c64; }
.section-title { font-weight: bold; font-size: 14px; margin-bottom: 6px; }
.infobar-error   background { background-color: #c01c28; color: white; }
.infobar-warning background { background-color: #e5a50a; }
.infobar-info    background { background-color: #1c71d8; color: white; }
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_cmd(args, timeout=10, input_text=None):
    """Run a command and return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
        return result.stdout, result.stderr, result.returncode
    except FileNotFoundError:
        return "", f"Command not found: {args[0]}", -1
    except subprocess.TimeoutExpired:
        return "", "Command timed out", -1
    except Exception as exc:
        return "", str(exc), -1


def run_privileged_bg(args, callback=None):
    """Run a privileged command (via pkexec) in a background thread.
    callback(stdout, stderr, returncode) is called on the GLib main loop."""
    def _worker():
        cmd = ["pkexec"] + [str(a) for a in args]
        out, err, rc = run_cmd(cmd, timeout=60)
        if callback:
            GLib.idle_add(callback, out, err, rc)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def detect_config_file():
    """Return the active multiseat config path or None."""
    if TMP_CONF.exists():
        return TMP_CONF
    if CONF_DIR.exists():
        confs = sorted(CONF_DIR.glob("_*.conf"))
        if confs:
            return confs[0]
    return None


def parse_config_sections(conf_path):
    """Parse an INI-style config file and return a dict of section→{key:value}."""
    sections = {}
    current = None
    try:
        with open(conf_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^\[(.+)\]$", line)
                if m:
                    current = m.group(1)
                    sections[current] = {}
                elif current and "=" in line:
                    k, _, v = line.partition("=")
                    sections[current][k.strip()] = v.strip()
    except Exception:
        pass
    return sections


def get_drm_lease_count():
    try:
        files = [f for f in DLM_RUN.iterdir()
                 if not f.suffix == ".lock"] if DLM_RUN.exists() else []
        return len(files)
    except Exception:
        return 0


def get_active_seats():
    out, _, _ = run_cmd(["loginctl", "list-seats"])
    count = 0
    for line in out.splitlines():
        seat = line.strip()
        if seat and seat != "seat0" and not seat.startswith("SEAT") and seat != "":
            count += 1
    return count


def get_running_compositors():
    out, _, _ = run_cmd(
        ["systemctl", "list-units", "--state=active", "--no-legend",
         "--no-pager", "multiseat-*.service"]
    )
    return len([l for l in out.splitlines() if l.strip()])


def get_system_target():
    out, _, _ = run_cmd(["systemctl", "get-default"])
    return out.strip() or "unknown"


def get_journal_tail(lines=30):
    out, _, _ = run_cmd(
        ["journalctl", "-b", "--no-pager", "-n", str(lines),
         "-g", "multiseat|labwc|seat"],
        timeout=15,
    )
    return out


# ---------------------------------------------------------------------------
# Status Card widget
# ---------------------------------------------------------------------------

class StatusCard(Gtk.Frame):
    """A colored status card showing a value and a label."""

    def __init__(self, label_text):
        super().__init__()
        self.get_style_context().add_class("card")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        self.add(box)

        self._value_lbl = Gtk.Label(label="—")
        self._value_lbl.get_style_context().add_class("card-value")
        self._value_lbl.set_halign(Gtk.Align.CENTER)
        box.pack_start(self._value_lbl, False, False, 0)

        desc = Gtk.Label(label=label_text)
        desc.get_style_context().add_class("card-label")
        desc.set_halign(Gtk.Align.CENTER)
        box.pack_start(desc, False, False, 0)

        self._classes = ["card-good", "card-warn", "card-error"]

    def set_value(self, text, state="good"):
        """state: 'good', 'warn', 'error', or 'neutral'."""
        self._value_lbl.set_text(str(text))
        ctx = self.get_style_context()
        for cls in self._classes:
            ctx.remove_class(cls)
        if state in ("good", "warn", "error"):
            ctx.add_class(f"card-{state}")


# ---------------------------------------------------------------------------
# Dashboard Tab
# ---------------------------------------------------------------------------

class DashboardTab(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.app = app
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        # ── Status Cards ──────────────────────────────────────────────────
        cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        cards_box.set_homogeneous(True)
        self.pack_start(cards_box, False, False, 0)

        self.card_leases = StatusCard("DRM Leases Active")
        self.card_seats  = StatusCard("Active Seats")
        self.card_comps  = StatusCard("Running Compositors")
        self.card_target = StatusCard("System Target")
        for c in (self.card_leases, self.card_seats,
                  self.card_comps, self.card_target):
            cards_box.pack_start(c, True, True, 0)

        # ── Toolbar ───────────────────────────────────────────────────────
        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(tb, False, False, 4)

        self._make_btn(tb, "🟢  Start All",  "#26a269", self._on_start_all)
        self._make_btn(tb, "🔴  Stop All",   "#c01c28", self._on_stop_all)
        self._make_btn(tb, "🔄  Refresh",    "#3584e4", self._on_refresh)
        self._make_btn(tb, "📋  View Logs",  "#9141ac", self._on_view_logs)

        # ── Journal View ──────────────────────────────────────────────────
        lbl = Gtk.Label()
        lbl.set_markup('<span weight="bold" size="large">Recent Journal (multiseat / labwc / seat)</span>')
        lbl.set_halign(Gtk.Align.START)
        self.pack_start(lbl, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(200)
        self.pack_start(sw, True, True, 0)

        self.journal_view = Gtk.TextView()
        self.journal_view.set_editable(False)
        self.journal_view.set_monospace(True)
        self.journal_view.set_cursor_visible(False)
        self.journal_buf = self.journal_view.get_buffer()
        sw.add(self.journal_view)

        # Auto-refresh
        self._refresh_data()
        GLib.timeout_add_seconds(5, self._auto_refresh)

    # ── helpers ───────────────────────────────────────────────────────────

    def _make_btn(self, box, label, color, cb):
        btn = Gtk.Button(label=label)
        btn.connect("clicked", cb)
        box.pack_start(btn, False, False, 0)
        return btn

    def _auto_refresh(self):
        self._refresh_data()
        return True  # keep firing

    def _on_refresh(self, _btn):
        self._refresh_data()

    def _refresh_data(self):
        threading.Thread(target=self._fetch_and_update, daemon=True).start()

    def _fetch_and_update(self):
        leases  = get_drm_lease_count()
        seats   = get_active_seats()
        comps   = get_running_compositors()
        target  = get_system_target()
        journal = get_journal_tail(30)
        GLib.idle_add(self._update_ui, leases, seats, comps, target, journal)

    def _update_ui(self, leases, seats, comps, target, journal):
        self.card_leases.set_value(leases,  "good" if leases > 0 else "warn")
        self.card_seats.set_value(seats,    "good" if seats > 0  else "warn")
        self.card_comps.set_value(comps,    "good" if comps > 0  else "warn")
        state = "good" if "graphical" in target else "warn"
        self.card_target.set_value(target, state)

        self.journal_buf.set_text(journal if journal else "(no journal entries found)")
        # Scroll to end
        end = self.journal_buf.get_end_iter()
        self.journal_view.scroll_to_iter(end, 0.0, False, 0.0, 1.0)

    def _on_start_all(self, _btn):
        dlg = Gtk.MessageDialog(
            transient_for=self.app,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Start all multiseat services?",
        )
        dlg.format_secondary_text(
            f"This will run:\n  pkexec {MULTISEAT_SCRIPT} -s"
        )
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.YES:
            run_privileged_bg(
                [str(MULTISEAT_SCRIPT), "-s"],
                callback=lambda o, e, rc: self.app.show_info(
                    "Start All completed." if rc == 0 else f"Error: {e}", rc
                ),
            )

    def _on_stop_all(self, _btn):
        dlg = Gtk.MessageDialog(
            transient_for=self.app,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Stop all multiseat services?",
        )
        dlg.format_secondary_text(
            f"This will run:\n  pkexec {MULTISEAT_SCRIPT} -q"
        )
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.YES:
            run_privileged_bg(
                [str(MULTISEAT_SCRIPT), "-q"],
                callback=lambda o, e, rc: self.app.show_info(
                    "Stop All completed." if rc == 0 else f"Error: {e}", rc
                ),
            )

    def _on_view_logs(self, _btn):
        try:
            subprocess.Popen(
                ["xterm", "-e",
                 'journalctl -b | grep -e multiseat -e labwc -e seat | less'],
                start_new_session=True,
            )
        except Exception as exc:
            self.app.show_info(f"Could not open xterm: {exc}", 1)


# ---------------------------------------------------------------------------
# Seats Tab
# ---------------------------------------------------------------------------

class SeatsTab(Gtk.Box):
    """Tab 2: Per-seat management."""

    # Columns
    COL_SEAT   = 0
    COL_CARD   = 1
    COL_CONN   = 2
    COL_USER   = 3
    COL_PID    = 4
    COL_STATUS = 5

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.app = app
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        # Top bar
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(top, False, False, 0)

        lbl = Gtk.Label()
        lbl.set_markup('<b>Seat Management</b>')
        top.pack_start(lbl, False, False, 0)

        top.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                       False, False, 4)

        btn_detect = Gtk.Button(label="🔍  Detect Seats")
        btn_detect.connect("clicked", lambda _: self.refresh())
        top.pack_start(btn_detect, False, False, 0)

        # Info bar for "no config"
        self.no_conf_lbl = Gtk.Label(
            label="⚠  No config found. Run 'Auto-Detect' in the Config tab first."
        )
        self.no_conf_lbl.set_no_show_all(True)
        self.pack_start(self.no_conf_lbl, False, False, 0)

        # TreeView
        self.store = Gtk.ListStore(str, str, str, str, str, str)
        self.tv = Gtk.TreeView(model=self.store)
        self.tv.set_headers_visible(True)

        for idx, title in enumerate(
            ["Seat", "Card", "Connector", "User", "Compositor PID", "Status"]
        ):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, renderer, text=idx)
            col.set_resizable(True)
            col.set_min_width(80)
            self.tv.append_column(col)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tv)
        self.pack_start(sw, True, True, 0)

        # Action buttons
        act = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(act, False, False, 0)

        lbl2 = Gtk.Label(label="Selected seat:")
        act.pack_start(lbl2, False, False, 0)

        self.btn_restart = Gtk.Button(label="🔄  Restart")
        self.btn_restart.connect("clicked", self._on_restart)
        act.pack_start(self.btn_restart, False, False, 0)

        self.btn_kill = Gtk.Button(label="⛔  Kill")
        self.btn_kill.connect("clicked", self._on_kill)
        act.pack_start(self.btn_kill, False, False, 0)

        self.btn_term = Gtk.Button(label="🖥  Open Terminal")
        self.btn_term.connect("clicked", self._on_open_terminal)
        act.pack_start(self.btn_term, False, False, 0)

        self.refresh()

    # ── data ─────────────────────────────────────────────────────────────

    def refresh(self):
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        conf = detect_config_file()
        if conf is None:
            GLib.idle_add(self._show_no_conf)
            return

        sections = parse_config_sections(conf)
        rows = []
        for section, vals in sections.items():
            if not section.startswith("card"):
                continue
            card = section
            connector = vals.get("connector", "")
            seat_name = f"seat-{card}"

            # compositor status
            out, _, _ = run_cmd(
                ["systemctl", "is-active", f"multiseat-{card}.service"]
            )
            status = out.strip()

            # user
            out2, _, _ = run_cmd(
                ["loginctl", "seat-status", seat_name]
            )
            user = ""
            for line in out2.splitlines():
                m = re.search(r"(\w+)\s+logged in", line)
                if m:
                    user = m.group(1)
                    break

            # PID of compositor
            out3, _, _ = run_cmd(
                ["systemctl", "show", f"multiseat-{card}.service",
                 "--property=MainPID", "--value"]
            )
            pid = out3.strip()
            if pid == "0":
                pid = ""

            rows.append((seat_name, card, connector, user, pid, status))

        GLib.idle_add(self._populate, rows)

    def _show_no_conf(self):
        self.no_conf_lbl.show()
        self.store.clear()

    def _populate(self, rows):
        self.no_conf_lbl.hide()
        self.store.clear()
        for row in rows:
            self.store.append(row)

    # ── actions ───────────────────────────────────────────────────────────

    def _selected_card(self):
        sel = self.tv.get_selection()
        model, it = sel.get_selected()
        if it is None:
            return None, None
        seat = model[it][self.COL_SEAT]
        card = model[it][self.COL_CARD]
        return seat, card

    def _on_restart(self, _btn):
        seat, card = self._selected_card()
        if not card:
            self.app.show_info("Select a seat row first.", 1)
            return
        run_privileged_bg(
            [str(MULTISEAT_SCRIPT), "-r", card],
            callback=lambda o, e, rc: (
                self.app.show_info(
                    f"Restart {card}: {'OK' if rc == 0 else e}", rc
                ),
                self.refresh(),
            ),
        )

    def _on_kill(self, _btn):
        seat, card = self._selected_card()
        if not card:
            self.app.show_info("Select a seat row first.", 1)
            return
        run_privileged_bg(
            ["systemctl", "stop", f"multiseat-{card}.service"],
            callback=lambda o, e, rc: (
                self.app.show_info(
                    f"Stop {card}: {'OK' if rc == 0 else e}", rc
                ),
                self.refresh(),
            ),
        )

    def _on_open_terminal(self, _btn):
        seat, card = self._selected_card()
        if not seat:
            self.app.show_info("Select a seat row first.", 1)
            return
        try:
            subprocess.Popen(
                ["xterm", "-T", f"Seat {seat}", "-e",
                 f"XDG_SEAT={seat} bash"],
                start_new_session=True,
            )
        except Exception as exc:
            self.app.show_info(f"Could not open xterm: {exc}", 1)


# ---------------------------------------------------------------------------
# Devices Tab
# ---------------------------------------------------------------------------

class DevicesTab(Gtk.Box):
    """Tab 3: Hardware discovery and assignment."""

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.app = app
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        # Top flush button
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(top, False, False, 0)

        lbl = Gtk.Label()
        lbl.set_markup('<b>Device Discovery</b>')
        top.pack_start(lbl, False, False, 0)
        top.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                       False, False, 4)

        btn_refresh = Gtk.Button(label="🔄  Refresh Devices")
        btn_refresh.connect("clicked", lambda _: self.refresh())
        top.pack_start(btn_refresh, False, False, 0)

        btn_flush = Gtk.Button(label="🗑  Flush Assignments (loginctl)")
        btn_flush.connect("clicked", self._on_flush)
        top.pack_end(btn_flush, False, False, 0)

        # Notebook for sub-sections
        nb = Gtk.Notebook()
        self.pack_start(nb, True, True, 0)

        # ── Displays ──────────────────────────────────────────────────────
        disp_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        disp_box.set_margin_top(6)
        disp_box.set_margin_start(6)
        disp_box.set_margin_end(6)

        self.disp_store = Gtk.ListStore(str, str, str, str)
        self.disp_tv = self._make_tv(
            self.disp_store,
            ["Connector", "Status", "Leased", "Assigned Seat"],
        )

        sw1 = Gtk.ScrolledWindow()
        sw1.add(self.disp_tv)
        disp_box.pack_start(sw1, True, True, 0)

        btn_assign_disp = Gtk.Button(label="📺  Assign Selected to Seat…")
        btn_assign_disp.connect("clicked", self._on_assign_display)
        disp_box.pack_start(btn_assign_disp, False, False, 0)
        nb.append_page(disp_box, Gtk.Label(label="Displays"))

        # ── Input Devices ─────────────────────────────────────────────────
        inp_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inp_box.set_margin_top(6)
        inp_box.set_margin_start(6)
        inp_box.set_margin_end(6)

        self.inp_store = Gtk.ListStore(str, str, str, str)
        self.inp_tv = self._make_tv(
            self.inp_store,
            ["Device Name", "Type", "Sys Path", "Assigned Seat"],
        )

        sw2 = Gtk.ScrolledWindow()
        sw2.add(self.inp_tv)
        inp_box.pack_start(sw2, True, True, 0)
        nb.append_page(inp_box, Gtk.Label(label="Input Devices"))

        # ── USB Devices ───────────────────────────────────────────────────
        usb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        usb_box.set_margin_top(6)
        usb_box.set_margin_start(6)
        usb_box.set_margin_end(6)

        self.usb_store = Gtk.ListStore(str, str, str, str)
        self.usb_tv = self._make_tv(
            self.usb_store,
            ["USB Path", "Description", "Type", "Assigned Seat"],
        )

        sw3 = Gtk.ScrolledWindow()
        sw3.add(self.usb_tv)
        usb_box.pack_start(sw3, True, True, 0)
        nb.append_page(usb_box, Gtk.Label(label="USB Devices"))

        self.refresh()

    # ── helpers ───────────────────────────────────────────────────────────

    def _make_tv(self, store, cols):
        tv = Gtk.TreeView(model=store)
        tv.set_headers_visible(True)
        for idx, title in enumerate(cols):
            r = Gtk.CellRendererText()
            c = Gtk.TreeViewColumn(title, r, text=idx)
            c.set_resizable(True)
            c.set_min_width(100)
            tv.append_column(c)
        return tv

    def refresh(self):
        threading.Thread(target=self._fetch_all, daemon=True).start()

    def _fetch_all(self):
        displays = self._fetch_displays()
        inputs   = self._fetch_inputs()
        usbs     = self._fetch_usb()
        GLib.idle_add(self._populate_all, displays, inputs, usbs)

    # Displays
    def _fetch_displays(self):
        rows = []
        pattern = "/sys/devices/pci*/*/*/drm/card*/card*-*"
        leased_names = set()
        try:
            if DLM_RUN.exists():
                leased_names = {
                    f.name for f in DLM_RUN.iterdir()
                    if not f.suffix == ".lock"
                }
        except Exception:
            pass

        try:
            paths = glob.glob(pattern, recursive=False)
            for path in sorted(paths):
                connector = Path(path).name
                status_file = Path(path) / "status"
                try:
                    status = status_file.read_text().strip()
                except Exception:
                    status = "unknown"
                leased = "yes" if connector in leased_names else "no"
                rows.append((connector, status, leased, ""))
        except Exception:
            pass
        return rows

    # Input devices
    def _fetch_inputs(self):
        rows = []
        try:
            for inp_dir in sorted(Path("/sys/class/input").iterdir()):
                if not inp_dir.name.startswith("input"):
                    continue
                name_file = inp_dir / "name"
                phys_file = inp_dir / "phys"
                cap_file  = inp_dir / "capabilities" / "key"
                try:
                    name = name_file.read_text().strip()
                    phys = phys_file.read_text().strip() if phys_file.exists() else ""
                except Exception:
                    name = inp_dir.name
                    phys = ""

                # Classify
                if cap_file.exists():
                    try:
                        caps = int(cap_file.read_text().strip().replace(" ", ""), 16)
                        # keyboard: bit 1 (KEY_1) set; mouse: relative axes
                        dev_type = "keyboard" if (caps & 0x2) else "mouse/other"
                    except Exception:
                        dev_type = "unknown"
                else:
                    dev_type = "unknown"

                # Skip devices with no name
                if not name:
                    continue

                rows.append((name, dev_type, str(inp_dir.resolve()), ""))
        except Exception:
            pass
        return rows

    # USB devices
    def _fetch_usb(self):
        rows = []
        try:
            out, _, _ = run_cmd(["lsusb"], timeout=10)
            lsusb_map = {}
            for line in out.splitlines():
                m = re.match(
                    r"Bus (\d+) Device (\d+).*ID ([0-9a-f]{4}:[0-9a-f]{4})\s*(.*)",
                    line, re.I,
                )
                if m:
                    bus, dev, vid_pid, desc = m.groups()
                    lsusb_map[f"{int(bus):03d}/{int(dev):03d}"] = (vid_pid, desc.strip())

            usb_root = Path("/sys/bus/usb/devices")
            for dev_dir in sorted(usb_root.iterdir()):
                # Only real devices (e.g. 1-1, 1-1.2, not 1-0:1.0)
                if ":" in dev_dir.name:
                    continue
                vid_f = dev_dir / "idVendor"
                pid_f = dev_dir / "idProduct"
                if not vid_f.exists():
                    continue
                try:
                    vid = vid_f.read_text().strip()
                    pid = pid_f.read_text().strip()
                except Exception:
                    continue

                # busnum / devnum for lsusb lookup
                try:
                    bus_num = (dev_dir / "busnum").read_text().strip()
                    dev_num = (dev_dir / "devnum").read_text().strip()
                    key = f"{int(bus_num):03d}/{int(dev_num):03d}"
                except Exception:
                    key = ""

                vid_pid, desc = lsusb_map.get(key, (f"{vid}:{pid}", ""))
                if not desc:
                    try:
                        mfr = (dev_dir / "manufacturer").read_text().strip()
                        prod = (dev_dir / "product").read_text().strip()
                        desc = f"{mfr} {prod}".strip()
                    except Exception:
                        desc = vid_pid

                # Skip hubs
                if "hub" in desc.lower():
                    continue

                # Type heuristic
                try:
                    cls = (dev_dir / "bDeviceClass").read_text().strip()
                except Exception:
                    cls = ""
                if cls == "03":
                    dev_type = "HID"
                elif cls == "09":
                    continue  # hub
                elif cls == "00":
                    dev_type = "composite"
                else:
                    dev_type = f"class {cls}"

                rows.append((dev_dir.name, desc, dev_type, ""))
        except Exception:
            pass
        return rows

    def _populate_all(self, displays, inputs, usbs):
        self.disp_store.clear()
        for r in displays:
            self.disp_store.append(r)
        self.inp_store.clear()
        for r in inputs:
            self.inp_store.append(r)
        self.usb_store.clear()
        for r in usbs:
            self.usb_store.append(r)

    def _on_flush(self, _btn):
        run_privileged_bg(
            ["loginctl", "flush-devices"],
            callback=lambda o, e, rc: self.app.show_info(
                "Flush devices: OK" if rc == 0 else f"Flush error: {e}", rc
            ),
        )

    def _on_assign_display(self, _btn):
        sel = self.disp_tv.get_selection()
        model, it = sel.get_selected()
        if it is None:
            self.app.show_info("Select a display row first.", 1)
            return
        connector = model[it][0]

        dlg = Gtk.Dialog(title=f"Assign {connector} to Seat",
                         transient_for=self.app, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Assign", Gtk.ResponseType.OK)
        content = dlg.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        content.add(Gtk.Label(label=f"Assign connector: {connector}"))
        combo = Gtk.ComboBoxText()
        combo.append_text("seat0")
        # Pull seats from loginctl
        out, _, _ = run_cmd(["loginctl", "list-seats"])
        for line in out.splitlines():
            s = line.strip()
            if s and not s.startswith("SEAT") and s != "seat0" and s != "":
                combo.append_text(s)
        combo.set_active(0)
        content.add(combo)
        dlg.show_all()

        resp = dlg.run()
        seat = combo.get_active_text()
        dlg.destroy()

        if resp == Gtk.ResponseType.OK and seat:
            # loginctl attach <seat> <device-path>
            # We can't attach a DRM connector directly via loginctl;
            # inform user this requires udev rule editing.
            self.app.show_info(
                f"To assign {connector} to {seat}, add a udev rule or "
                "use 'loginctl attach' with the sysfs device path. "
                "See /etc/udev/rules.d/ for current rules.",
                0,
            )


# ---------------------------------------------------------------------------
# Config Tab
# ---------------------------------------------------------------------------

class ConfigTab(Gtk.Box):
    """Tab 4: Configuration file editor."""

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.app = app
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._current_path = None

        # Toolbar
        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(tb, False, False, 0)

        self.btn_save = Gtk.Button(label="💾  Save")
        self.btn_save.connect("clicked", self._on_save)
        tb.pack_start(self.btn_save, False, False, 0)

        btn_detect = Gtk.Button(label="🔍  Auto-Detect")
        btn_detect.connect("clicked", self._on_auto_detect)
        tb.pack_start(btn_detect, False, False, 0)

        btn_nano = Gtk.Button(label="📝  Open in Nano")
        btn_nano.connect("clicked", self._on_open_nano)
        tb.pack_start(btn_nano, False, False, 0)

        btn_validate = Gtk.Button(label="✅  Validate")
        btn_validate.connect("clicked", self._on_validate)
        tb.pack_start(btn_validate, False, False, 0)

        btn_refresh = Gtk.Button(label="🔄  Refresh")
        btn_refresh.connect("clicked", lambda _: self.refresh())
        tb.pack_start(btn_refresh, False, False, 0)

        # Paned
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(260)
        self.pack_start(paned, True, True, 0)

        # Left: file list
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl = Gtk.Label()
        lbl.set_markup('<b>Config Files</b>')
        lbl.set_halign(Gtk.Align.START)
        left.pack_start(lbl, False, False, 0)

        self.file_store = Gtk.ListStore(str, str, str)  # name, path, symlink_status
        self.file_tv = Gtk.TreeView(model=self.file_store)
        self.file_tv.set_headers_visible(True)
        for idx, title in enumerate(["File", "Status"]):
            r = Gtk.CellRendererText()
            c = Gtk.TreeViewColumn(title, r, text=idx if idx == 0 else 2)
            c.set_resizable(True)
            self.file_tv.append_column(c)
        self.file_tv.get_selection().connect("changed", self._on_file_selected)

        sw_left = Gtk.ScrolledWindow()
        sw_left.add(self.file_tv)
        left.pack_start(sw_left, True, True, 0)
        paned.pack1(left, False, False)

        # Right: editor
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.path_lbl = Gtk.Label(label="No file selected")
        self.path_lbl.set_halign(Gtk.Align.START)
        self.path_lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        right.pack_start(self.path_lbl, False, False, 0)

        sw_right = Gtk.ScrolledWindow()
        sw_right.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.editor_buf = Gtk.TextBuffer()
        self.editor_view = Gtk.TextView(buffer=self.editor_buf)
        self.editor_view.set_monospace(True)
        sw_right.add(self.editor_view)
        right.pack_start(sw_right, True, True, 0)
        paned.pack2(right, True, False)

        self.refresh()

    # ── data ─────────────────────────────────────────────────────────────

    def refresh(self):
        self.file_store.clear()
        tmp_symlink_target = None
        if TMP_CONF.exists():
            try:
                tmp_symlink_target = str(TMP_CONF.resolve())
            except Exception:
                pass
            self.file_store.append([TMP_CONF.name, str(TMP_CONF), "active (tmp)"])

        if CONF_DIR.exists():
            for f in sorted(CONF_DIR.glob("_*.conf")):
                try:
                    resolved = str(f.resolve())
                except Exception:
                    resolved = ""
                if tmp_symlink_target and resolved == tmp_symlink_target:
                    status = "active (symlinked)"
                elif f.is_symlink():
                    status = f"symlink → {os.readlink(f)}"
                else:
                    status = "file"
                self.file_store.append([f.name, str(f), status])
        else:
            self.file_store.append(
                ["(no /etc/multiseat)", "", "Run Auto-Detect first"]
            )

    def _on_file_selected(self, sel):
        model, it = sel.get_selected()
        if it is None:
            return
        path = model[it][1]
        if not path:
            return
        self._current_path = Path(path)
        self.path_lbl.set_text(str(self._current_path))
        try:
            content = self._current_path.read_text()
        except PermissionError:
            content = "(Permission denied — try opening in Nano as root)"
        except Exception as exc:
            content = f"(Error reading file: {exc})"
        self.editor_buf.set_text(content)

    # ── actions ───────────────────────────────────────────────────────────

    def _on_save(self, _btn):
        if self._current_path is None:
            self.app.show_info("No file selected.", 1)
            return
        start = self.editor_buf.get_start_iter()
        end   = self.editor_buf.get_end_iter()
        text  = self.editor_buf.get_text(start, end, True)

        # Write to a temp file, then pkexec copy
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".conf", delete=False
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name

        run_privileged_bg(
            ["cp", tmp_path, str(self._current_path)],
            callback=lambda o, e, rc: (
                self.app.show_info(
                    f"Saved to {self._current_path}" if rc == 0 else f"Save error: {e}",
                    rc,
                ),
                os.unlink(tmp_path) if os.path.exists(tmp_path) else None,
            ),
        )

    def _on_auto_detect(self, _btn):
        self.app.show_info("Running Auto-Detect (pkexec multiseat-mint.sh -C)…", 0)
        run_privileged_bg(
            [str(MULTISEAT_SCRIPT), "-C"],
            callback=lambda o, e, rc: (
                self.app.show_info(
                    "Auto-Detect complete." if rc == 0 else f"Auto-Detect error: {e}",
                    rc,
                ),
                GLib.idle_add(self.refresh),
            ),
        )

    def _on_open_nano(self, _btn):
        path = str(self._current_path) if self._current_path else "/etc/multiseat/"
        try:
            subprocess.Popen(
                ["xterm", "-e", f"sudo nano {path}"],
                start_new_session=True,
            )
        except Exception as exc:
            self.app.show_info(f"Could not open xterm: {exc}", 1)

    def _on_validate(self, _btn):
        if self._current_path is None:
            self.app.show_info("No file selected.", 1)
            return
        sections = parse_config_sections(self._current_path)
        issues = []
        for section, vals in sections.items():
            if not section.startswith("card"):
                continue
            has_keyboard   = any("keyboard" in k.lower() or "kb" in k.lower()
                                  for k in vals)
            has_display    = any(k in ("connector", "display", "output")
                                  for k in vals)
            if not has_keyboard:
                issues.append(f"[{section}] missing keyboard entry")
            if not has_display:
                issues.append(f"[{section}] missing connector/display entry")

        if issues:
            msg = "Validation issues found:\n• " + "\n• ".join(issues)
            self.app.show_info(msg, 1)
        else:
            self.app.show_info("Config validation passed ✓", 0)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MultiseatControlPanel(Gtk.ApplicationWindow):
    def __init__(self):
        super().__init__(title="Multiseat Control Panel")
        self.set_default_size(900, 600)

        # ── CSS ───────────────────────────────────────────────────────────
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # ── HeaderBar ─────────────────────────────────────────────────────
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title("Multiseat Control Panel")
        conf = detect_config_file()
        hb.set_subtitle(str(conf) if conf else "No config detected")
        self.set_titlebar(hb)

        # ── InfoBar (notification strip) ──────────────────────────────────
        self._infobar_timer = None
        self.infobar = Gtk.InfoBar()
        self.infobar.set_show_close_button(True)
        self.infobar.set_no_show_all(True)
        self.infobar_lbl = Gtk.Label(label="")
        self.infobar_lbl.set_line_wrap(True)
        self.infobar.get_content_area().add(self.infobar_lbl)
        self.infobar.connect("response", lambda ib, _: ib.hide())

        # ── Root layout ───────────────────────────────────────────────────
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)
        root.pack_start(self.infobar, False, False, 0)

        # ── Notebook ──────────────────────────────────────────────────────
        nb = Gtk.Notebook()
        nb.set_tab_pos(Gtk.PositionType.TOP)
        root.pack_start(nb, True, True, 0)

        self.dashboard_tab = DashboardTab(self)
        nb.append_page(self.dashboard_tab, Gtk.Label(label="📊  Dashboard"))

        self.seats_tab = SeatsTab(self)
        nb.append_page(self.seats_tab, Gtk.Label(label="🪑  Seats"))

        self.devices_tab = DevicesTab(self)
        nb.append_page(self.devices_tab, Gtk.Label(label="🖥  Devices"))

        self.config_tab = ConfigTab(self)
        nb.append_page(self.config_tab, Gtk.Label(label="⚙  Config"))

        # Setup wizard check
        if not CONF_DIR.exists() and not TMP_CONF.exists():
            GLib.idle_add(self._show_setup_wizard)

    # ── InfoBar helper ────────────────────────────────────────────────────

    def show_info(self, message, rc=0):
        """Show message in the InfoBar. rc=0 → info, rc!=0 → error."""
        if not isinstance(message, str):
            return
        msg_type = Gtk.MessageType.INFO if rc == 0 else Gtk.MessageType.ERROR
        self.infobar.set_message_type(msg_type)
        self.infobar_lbl.set_text(message)
        self.infobar.show()
        self.infobar_lbl.show()

        # Auto-hide after 5 s
        if self._infobar_timer:
            GLib.source_remove(self._infobar_timer)
        self._infobar_timer = GLib.timeout_add_seconds(
            5, self._hide_infobar
        )

    def _hide_infobar(self):
        self.infobar.hide()
        self._infobar_timer = None
        return False  # one-shot

    # ── Setup wizard ──────────────────────────────────────────────────────

    def _show_setup_wizard(self):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.NONE,
            text="Welcome to Multiseat Control Panel",
        )
        dlg.format_secondary_text(
            "No multiseat configuration was detected.\n\n"
            "• /etc/multiseat/ does not exist\n"
            "• /tmp/multiseat.conf not present\n\n"
            "Would you like to run Auto-Detect now "
            f"({MULTISEAT_SCRIPT} -C)?"
        )
        dlg.add_buttons(
            "Not Now", Gtk.ResponseType.CANCEL,
            "Run Auto-Detect", Gtk.ResponseType.OK,
        )
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.OK:
            self.config_tab._on_auto_detect(None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    win = MultiseatControlPanel()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
