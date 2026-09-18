#!/usr/bin/env python3
"""
macOS-style Spotlight Search Bar for Niri / Wayland
Faithfully replicating the floating capsule search bar with circular action buttons.
Supports instant toggle daemon, keyboard navigation, and live search for
Applications, Open Windows, Folders, and Documents.
"""

import os
import sys
import subprocess
import glob
import re
import socket
import threading
import json
import time
import math
import urllib.request
import urllib.parse

SOCKET_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "spotlight.sock")

def client_command(cmd="toggle"):
    if not os.path.exists(SOCKET_PATH):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.4)
        s.connect(SOCKET_PATH)
        s.sendall(f"{cmd}\n".encode())
        res = s.recv(16)
        s.close()
        return True
    except Exception:
        return False

# Fast path for CLI commands without loading GTK or re-executing
if len(sys.argv) > 1:
    if sys.argv[1] == "query":
        cmd_str = "query " + " ".join(sys.argv[2:])
    else:
        cmd_str = " ".join(sys.argv[1:])
    if client_command(cmd_str):
        sys.exit(0)
    if sys.argv[1] == "hide":
        sys.exit(0)

# Auto-reexec with LD_PRELOAD for gtk4-layer-shell if needed
LAYER_SHELL_LIB = "/usr/lib/x86_64-linux-gnu/libgtk4-layer-shell.so.0"
if os.path.exists(LAYER_SHELL_LIB) and LAYER_SHELL_LIB not in os.environ.get("LD_PRELOAD", ""):
    env = dict(os.environ)
    existing = env.get("LD_PRELOAD", "")
    env["LD_PRELOAD"] = f"{LAYER_SHELL_LIB}:{existing}" if existing else LAYER_SHELL_LIB
    os.execvpe(sys.executable, [sys.executable] + sys.argv, env)

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gtk, Gdk, Gtk4LayerShell, GLib, Gio, Pango

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
ICONS_DIR = os.path.join(SCRIPT_DIR, "icons")

CSS_DATA = """
window {
    background: transparent;
}

.spotlight-backdrop {
    background: transparent;
}

.spotlight-container {
    background: transparent;
    padding: 0px;
}

/* ── Search Capsule Pill ── */
.spotlight-pill {
    background: linear-gradient(180deg, rgba(28, 42, 58, 0.88) 0%, rgba(18, 28, 40, 0.82) 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.18);
    border-radius: 9999px;
    padding-left: 20px;
    padding-right: 16px;
    min-height: 56px;
    min-width: 470px;
    box-shadow: 0 16px 36px rgba(0, 0, 0, 0.35), 0 3px 10px rgba(0, 0, 0, 0.12), inset 0 1px 1px rgba(255, 255, 255, 0.22);
    transition: all 180ms cubic-bezier(0.16, 1, 0.3, 1);
}

.spotlight-pill:focus-within {
    border-color: rgba(255, 255, 255, 0.35);
    box-shadow: 0 20px 44px rgba(0, 0, 0, 0.45), 0 4px 14px rgba(0, 0, 0, 0.16), inset 0 1px 1px rgba(255, 255, 255, 0.30);
}

.spotlight-icon {
    margin-right: 12px;
    opacity: 0.95;
    transition: opacity 160ms ease, transform 160ms ease;
}

.spotlight-entry {
    background: transparent;
    border: none;
    outline: none;
    box-shadow: none;
    color: #ffffff;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", "Segoe UI", sans-serif;
    font-size: 22px;
    font-weight: 450;
    caret-color: #007aff;
    min-width: 380px;
    padding: 0;
    margin: 0;
}

.spotlight-entry text {
    background: transparent;
    color: #ffffff;
    font-size: 22px;
    font-weight: 450;
}

.spotlight-entry placeholder {
    color: rgba(255, 255, 255, 0.70);
    font-size: 22px;
    font-weight: 450;
}

/* ── Badge Hint (⌘ 1, ⌘ 2, etc.) ── */
.spotlight-badge {
    background: rgba(255, 255, 255, 0.14);
    border: 1px solid rgba(255, 255, 255, 0.20);
    border-radius: 8px;
    padding: 2px 8px;
    margin-left: 8px;
    transition: all 160ms ease;
}

.badge-icon {
    color: rgba(255, 255, 255, 0.85);
    font-size: 13px;
    font-weight: 500;
    margin-right: 2px;
}

.badge-num {
    color: #ffffff;
    font-size: 13px;
    font-weight: 600;
}

/* ── 4 Circular Action Buttons ── */
button.spotlight-circle-btn {
    background: linear-gradient(180deg, rgba(28, 42, 58, 0.88) 0%, rgba(18, 28, 40, 0.82) 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.18);
    border-radius: 9999px;
    min-width: 56px;
    min-height: 56px;
    padding: 0;
    margin: 0;
    box-shadow: 0 16px 36px rgba(0, 0, 0, 0.35), 0 3px 10px rgba(0, 0, 0, 0.12), inset 0 1px 1px rgba(255, 255, 255, 0.22);
    transition: all 180ms cubic-bezier(0.16, 1, 0.3, 1);
}

button.spotlight-circle-btn:hover {
    background: linear-gradient(180deg, rgba(40, 60, 82, 0.95) 0%, rgba(26, 40, 58, 0.90) 100%);
    border-color: rgba(255, 255, 255, 0.38);
    box-shadow: 0 20px 44px rgba(0, 0, 0, 0.45), 0 4px 14px rgba(0, 0, 0, 0.18), inset 0 1px 1px rgba(255, 255, 255, 0.35);
    transform: scale(1.06);
}

button.spotlight-circle-btn:active {
    transform: scale(0.95);
}

button.spotlight-circle-btn.active {
    background: rgba(0, 122, 255, 0.35);
    border: 1.5px solid #007aff;
    box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.50), 0 16px 36px rgba(0, 0, 0, 0.35);
    transform: scale(1.0);
}

/* ── Results Dropdown ── */
.spotlight-results-panel {
    background: rgba(22, 32, 46, 0.92);
    border: 1.5px solid rgba(255, 255, 255, 0.16);
    border-radius: 22px;
    margin-top: 14px;
    padding: 8px;
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.50), 0 4px 16px rgba(0, 0, 0, 0.20);
    transition: all 180ms cubic-bezier(0.16, 1, 0.3, 1);
}

.result-row {
    border-radius: 12px;
    padding: 8px 14px;
    margin: 2px 0px;
    transition: background-color 100ms cubic-bezier(0.16, 1, 0.3, 1), color 100ms ease;
}

.result-row:hover {
    background-color: rgba(255, 255, 255, 0.08);
}

.result-row:selected, .result-row:focus {
    background-color: #007aff;
    color: #ffffff;
}

.result-title {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
    font-size: 15px;
    font-weight: 500;
    color: #f5f5f7;
}

.result-row:selected .result-title {
    color: #ffffff;
}

.result-subtitle {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
    font-size: 12px;
    color: rgba(255, 255, 255, 0.65);
}

.result-row:selected .result-subtitle {
    color: rgba(255, 255, 255, 0.90);
}

.category-tag {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 6px;
    background-color: rgba(255, 255, 255, 0.12);
    color: rgba(255, 255, 255, 0.85);
    transition: background-color 100ms ease, color 100ms ease;
}

.result-row:selected .category-tag {
    background-color: rgba(255, 255, 255, 0.28);
    color: #ffffff;
}

.category-tag.google {
    background-color: rgba(66, 133, 244, 0.22);
    color: #8ab4f8;
}

.result-row:selected .category-tag.google {
    background-color: rgba(255, 255, 255, 0.30);
    color: #ffffff;
}

/* ── macOS-Style Sleek Scrollbar ── */
scrollbar {
    background: transparent;
    border: none;
    padding: 0;
    margin: 0;
}

scrollbar slider {
    background-color: rgba(255, 255, 255, 0.22);
    border-radius: 9999px;
    min-width: 5px;
    min-height: 24px;
    margin-right: 4px;
    transition: background-color 150ms ease;
}

scrollbar slider:hover {
    background-color: rgba(255, 255, 255, 0.50);
}
"""

class FlingScroller:
    """Provides kinetic momentum physics when flicking touchpad or mouse wheel."""
    def __init__(self, get_adj_fn):
        self.get_adj = get_adj_fn
        self.fling_timer = None
        self.velocity = 0.0

    def start_fling(self, vel_y):
        self.stop_fling()
        self.velocity = vel_y
        if abs(self.velocity) < 20:
            return
        self.fling_timer = GLib.timeout_add(16, self._fling_tick)

    def stop_fling(self):
        if self.fling_timer is not None:
            GLib.source_remove(self.fling_timer)
            self.fling_timer = None
        self.velocity = 0.0

    def _fling_tick(self):
        adj = self.get_adj()
        if not adj:
            self.fling_timer = None
            return GLib.SOURCE_REMOVE
        max_scroll = max(0.0, adj.get_upper() - adj.get_page_size())
        if max_scroll <= 0:
            self.fling_timer = None
            return GLib.SOURCE_REMOVE

        step = self.velocity * 0.016
        current = adj.get_value()
        target = max(0.0, min(max_scroll, current + step))
        adj.set_value(target)

        self.velocity *= 0.90
        if abs(self.velocity) < 8 or target == 0.0 or target == max_scroll:
            self.fling_timer = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

class SpotlightWindow:
    def __init__(self, app):
        self.app = app
        self.current_filter = "all"
        self.results_data = []
        self.current_instant_results = []
        self.search_generation = 0
        self.debounce_timer_id = None
        self.proc_lock = threading.Lock()
        self.active_processes = []

        # Kinetic physics fling scroller for touchpad gestures
        self.fling_scroller = FlingScroller(lambda: self.scrolled.get_vadjustment() if hasattr(self, 'scrolled') else None)

        # Animation states for smooth morph & fade
        self.anim_state = "closed"
        self.anim_start_us = 0
        self.anim_duration_us = 160_000
        self.start_opacity = 0.0
        self.target_opacity = 1.0
        self.start_margin = 145
        self.target_margin = 160
        self.tick_cb_id = None

        self.setup_css()
        self.create_window()
        self.load_applications_cache()

    def setup_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS_DATA.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def create_window(self):
        self.win = Gtk.ApplicationWindow(application=self.app)
        self.win.set_title("Spotlight")

        # Wayland Layer Shell configuration - Fullscreen transparent overlay
        Gtk4LayerShell.init_for_window(self.win)
        Gtk4LayerShell.set_namespace(self.win, "spotlight")
        Gtk4LayerShell.set_layer(self.win, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(self.win, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
        Gtk4LayerShell.set_anchor(self.win, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self.win, Gtk4LayerShell.Edge.BOTTOM, True)
        Gtk4LayerShell.set_anchor(self.win, Gtk4LayerShell.Edge.LEFT, True)
        Gtk4LayerShell.set_anchor(self.win, Gtk4LayerShell.Edge.RIGHT, True)
        self.win.set_opacity(0.0)

        # Fullscreen transparent root container
        self.root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.root_box.add_css_class("spotlight-backdrop")
        self.root_box.set_hexpand(True)
        self.root_box.set_vexpand(True)

        # Spotlight centered container (160px from top)
        self.center_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.center_box.add_css_class("spotlight-container")
        self.center_box.set_halign(Gtk.Align.CENTER)
        self.center_box.set_valign(Gtk.Align.START)
        self.center_box.set_margin_top(160)
        self.center_box.set_size_request(750, -1)

        # Horizontal Row containing Capsule + Revealer for 4 Buttons
        self.bar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.bar_row.set_halign(Gtk.Align.CENTER)

        # ── Search Capsule Pill ──
        self.pill_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.pill_box.add_css_class("spotlight-pill")
        self.pill_box.set_valign(Gtk.Align.CENTER)

        # Search Icon
        search_icon = Gtk.Image.new_from_file(os.path.join(ICONS_DIR, "search.svg"))
        search_icon.add_css_class("spotlight-icon")
        self.pill_box.append(search_icon)

        # Search Entry
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Spotlight Search")
        self.entry.add_css_class("spotlight-entry")
        self.entry.set_hexpand(True)
        self.entry.connect("changed", self.on_search_changed)
        self.entry.connect("activate", self.on_entry_activate)
        self.pill_box.append(self.entry)

        # Shortcut hint badge (⌘ 1, ⌘ 2, etc.)
        self.badge_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.badge_box.add_css_class("spotlight-badge")
        self.badge_box.set_valign(Gtk.Align.CENTER)
        self.badge_icon = Gtk.Label(label="⌘")
        self.badge_icon.add_css_class("badge-icon")
        self.badge_num = Gtk.Label(label="")
        self.badge_num.add_css_class("badge-num")
        self.badge_box.append(self.badge_icon)
        self.badge_box.append(self.badge_num)
        self.badge_box.set_visible(False)
        self.pill_box.append(self.badge_box)

        self.bar_row.append(self.pill_box)

        # ── 4 Circular Buttons inside Revealer (Morphs & Slides Out) ──
        self.buttons_revealer = Gtk.Revealer()
        self.buttons_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.buttons_revealer.set_transition_duration(220)

        self.buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        # 1. Apps
        self.btn_apps = Gtk.Button()
        self.btn_apps.add_css_class("spotlight-circle-btn")
        self.btn_apps.set_child(Gtk.Image.new_from_file(os.path.join(ICONS_DIR, "apps.svg")))
        self.btn_apps.set_tooltip_text("Applications (⌘1)")
        self.btn_apps.connect("clicked", lambda b: self.toggle_filter("apps"))
        self.buttons_box.append(self.btn_apps)

        # 2. Folders
        self.btn_folders = Gtk.Button()
        self.btn_folders.add_css_class("spotlight-circle-btn")
        self.btn_folders.set_child(Gtk.Image.new_from_file(os.path.join(ICONS_DIR, "folder.svg")))
        self.btn_folders.set_tooltip_text("Folders (⌘2)")
        self.btn_folders.connect("clicked", lambda b: self.toggle_filter("folders"))
        self.buttons_box.append(self.btn_folders)

        # 3. Windows / Actions
        self.btn_windows = Gtk.Button()
        self.btn_windows.add_css_class("spotlight-circle-btn")
        self.btn_windows.set_child(Gtk.Image.new_from_file(os.path.join(ICONS_DIR, "windows.svg")))
        self.btn_windows.set_tooltip_text("Actions / Windows (⌘3)")
        self.btn_windows.connect("clicked", lambda b: self.toggle_filter("windows"))
        self.buttons_box.append(self.btn_windows)

        # 4. Documents / Clipboard
        self.btn_docs = Gtk.Button()
        self.btn_docs.add_css_class("spotlight-circle-btn")
        self.btn_docs.set_child(Gtk.Image.new_from_file(os.path.join(ICONS_DIR, "documents.svg")))
        self.btn_docs.set_tooltip_text("Clipboard / Documents (⌘4)")
        self.btn_docs.connect("clicked", lambda b: self.toggle_filter("documents"))
        self.buttons_box.append(self.btn_docs)

        self.buttons_revealer.set_child(self.buttons_box)
        self.buttons_revealer.set_reveal_child(False)
        self.bar_row.append(self.buttons_revealer)

        self.center_box.append(self.bar_row)

        # ── Dropdown Results Container with Revealer Morphing ──
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.set_transition_duration(220)

        self.results_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.results_panel.add_css_class("spotlight-results-panel")
        self.results_panel.set_size_request(750, -1)
        self.results_panel.set_halign(Gtk.Align.CENTER)

        # Scrolled window with kinetic & touchpad support
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_max_content_height(420)
        self.scrolled.set_propagate_natural_height(True)
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_kinetic_scrolling(True)
        self.scrolled.set_overlay_scrolling(True)

        vadj = self.scrolled.get_vadjustment()
        vadj.set_step_increment(48.0)
        vadj.set_page_increment(240.0)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.connect("row-activated", self.on_row_activated)
        self.scrolled.set_child(self.list_box)
        self.results_panel.append(self.scrolled)

        self.revealer.set_child(self.results_panel)
        self.revealer.set_reveal_child(False)
        self.center_box.append(self.revealer)

        self.root_box.append(self.center_box)
        self.win.set_child(self.root_box)

        # Key controller for navigation
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self.on_key_pressed)
        self.win.add_controller(key_controller)

        # Click controller to dismiss when clicking outside Spotlight UI
        self.click_gesture = Gtk.GestureClick.new()
        self.click_gesture.set_button(0)
        self.click_gesture.connect("pressed", self.on_window_clicked)
        self.win.add_controller(self.click_gesture)

        # Scroll controller for touchpad and mouse wheel anywhere on Spotlight
        self.scroll_controller = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL |
            Gtk.EventControllerScrollFlags.DISCRETE |
            Gtk.EventControllerScrollFlags.KINETIC
        )
        self.scroll_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.scroll_controller.connect("scroll", self.on_scroll)
        self.scroll_controller.connect("decelerate", self.on_scroll_decelerate)
        self.win.add_controller(self.scroll_controller)

    def is_descendant_of(self, widget, ancestor):
        w = widget
        while w is not None:
            if w == ancestor:
                return True
            w = w.get_parent()
        return False

    def on_window_clicked(self, gesture, n_press, x, y):
        if not self.win.get_visible() or self.anim_state == "fade_out":
            return

        picked = self.win.pick(x, y, Gtk.PickFlags.DEFAULT)
        if not picked:
            self.hide()
            return

        in_pill = self.is_descendant_of(picked, self.pill_box)
        in_buttons = self.is_descendant_of(picked, self.buttons_revealer)
        in_results = (
            self.revealer.get_child_revealed() and
            self.is_descendant_of(picked, self.results_panel)
        )

        if not (in_pill or in_buttons or in_results):
            self.hide()

    def on_scroll(self, controller, dx, dy):
        if not self.revealer.get_child_revealed() or not self.results_data:
            return Gdk.EVENT_PROPAGATE

        adj = self.scrolled.get_vadjustment()
        upper = adj.get_upper()
        page_size = adj.get_page_size()
        max_scroll = max(0.0, upper - page_size)
        if max_scroll <= 0:
            return Gdk.EVENT_PROPAGATE

        self.fling_scroller.stop_fling()

        step = 48.0
        delta = dy * step
        current = adj.get_value()
        target = max(0.0, min(max_scroll, current + delta))
        adj.set_value(target)
        return Gdk.EVENT_STOP

    def on_scroll_decelerate(self, controller, vel_x, vel_y):
        if not self.revealer.get_child_revealed() or not self.results_data:
            return
        adj = self.scrolled.get_vadjustment()
        max_scroll = max(0.0, adj.get_upper() - adj.get_page_size())
        if max_scroll > 0 and abs(vel_y) > 10:
            self.fling_scroller.start_fling(vel_y)

    def load_applications_cache(self):
        self.apps = []
        seen = set()
        dirs = [
            "/usr/share/applications",
            "/usr/local/share/applications",
            os.path.expanduser("~/.local/share/applications")
        ]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for f in glob.glob(os.path.join(d, "*.desktop")):
                desktop_id = os.path.basename(f)
                if desktop_id in seen:
                    continue
                seen.add(desktop_id)
                app_info = self.parse_desktop_file(f)
                if app_info and not app_info.get("nodisplay", False):
                    self.apps.append(app_info)

    def parse_desktop_file(self, path):
        name = None
        comment = ""
        icon = "application-x-executable"
        exec_cmd = ""
        nodisplay = False
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                in_entry = False
                for line in f:
                    line = line.strip()
                    if line == "[Desktop Entry]":
                        in_entry = True
                        continue
                    elif line.startswith("[") and in_entry:
                        break
                    if not in_entry or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k == "Name" and not name:
                        name = v
                    elif k == "Comment":
                        comment = v
                    elif k == "Icon":
                        icon = v
                    elif k == "Exec":
                        exec_cmd = v
                    elif k == "NoDisplay" and v.lower() == "true":
                        nodisplay = True
            if name and exec_cmd:
                return {
                    "type": "app",
                    "name": name,
                    "comment": comment,
                    "icon": icon,
                    "exec": exec_cmd,
                    "path": path
                }
        except Exception:
            pass
        return None

    def cancel_active_searches(self):
        with self.proc_lock:
            for p in self.active_processes:
                try:
                    if p.poll() is None:
                        p.terminate()
                except Exception:
                    pass
            self.active_processes.clear()

    @staticmethod
    def get_file_icon(path, is_dir):
        if is_dir:
            return "folder"
        ext = os.path.splitext(path)[1].lower()
        if ext in ('.png', '.jpg', '.jpeg', '.webp', '.svg', '.gif', '.ico'):
            return "image-x-generic"
        elif ext in ('.mp4', '.mkv', '.mov', '.webm', '.avi'):
            return "video-x-generic"
        elif ext in ('.mp3', '.flac', '.wav', '.ogg', '.m4a'):
            return "audio-x-generic"
        elif ext in ('.pdf',):
            return "application-pdf"
        elif ext in ('.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar'):
            return "package-x-generic"
        return "text-x-generic"

    def get_common_folders(self):
        home = os.path.expanduser("~")
        common = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "Development", ".config"]
        results = []
        for c in common:
            p = os.path.join(home, c)
            if os.path.isdir(p):
                results.append((10, {
                    "category": "Folder",
                    "title": c,
                    "subtitle": p.replace(home, "~"),
                    "icon": "folder",
                    "action": lambda path=p: self.open_file(path)
                }))
        return results

    def get_common_documents(self):
        home = os.path.expanduser("~")
        results = []

        # Current clipboard preview (wl-paste)
        try:
            clip = subprocess.check_output(
                ["wl-paste", "--no-newline"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.25
            ).strip()
            if clip:
                first_line = clip.split("\n")[0][:50]
                results.append((60, {
                    "category": "Clipboard",
                    "title": first_line,
                    "subtitle": "Click to copy to clipboard",
                    "icon": "edit-paste",
                    "action": lambda text=clip: subprocess.Popen(["wl-copy", text])
                }))
        except Exception:
            pass

        dirs = [os.path.join(home, "Desktop"), os.path.join(home, "Documents"), os.path.join(home, "Downloads")]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            try:
                files = [os.path.join(d, f) for f in os.listdir(d) if not f.startswith(".")]
                files.sort(key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0, reverse=True)
                for p in files[:8]:
                    if os.path.isfile(p):
                        results.append((10, {
                            "category": "Document",
                            "title": os.path.basename(p),
                            "subtitle": p.replace(home, "~"),
                            "icon": self.get_file_icon(p, False),
                            "action": lambda path=p: self.open_file(path)
                        }))
            except Exception:
                pass
        return results

    def toggle_filter(self, filter_name):
        btns = {
            "apps": self.btn_apps,
            "folders": self.btn_folders,
            "windows": self.btn_windows,
            "documents": self.btn_docs
        }
        filter_meta = {
            "apps": ("Applications", "1"),
            "folders": ("Folders", "2"),
            "windows": ("Actions", "3"),
            "documents": ("Clipboard", "4")
        }

        if self.current_filter == filter_name:
            self.current_filter = "all"
            for b in btns.values():
                b.remove_css_class("active")
            self.entry.set_placeholder_text("Spotlight Search")
            self.badge_box.set_visible(False)
        else:
            self.current_filter = filter_name
            for k, b in btns.items():
                if k == filter_name:
                    b.add_css_class("active")
                else:
                    b.remove_css_class("active")
            meta = filter_meta.get(filter_name, (filter_name.capitalize(), ""))
            self.entry.set_placeholder_text(meta[0])
            self.badge_num.set_text(meta[1])
            self.badge_box.set_visible(True)

        self.on_search_changed(self.entry)

    def on_search_changed(self, entry):
        raw_text = entry.get_text()
        query = raw_text.strip()
        query_lower = query.lower()
        self.search_generation += 1
        gen = self.search_generation

        # Cancel any pending debounce timer & previous search processes
        if self.debounce_timer_id is not None:
            GLib.source_remove(self.debounce_timer_id)
            self.debounce_timer_id = None
        self.cancel_active_searches()

        if not raw_text:
            if self.current_filter == "all":
                self.revealer.set_reveal_child(False)
                self.results_data = []
                self.badge_box.set_visible(False)
                return
            else:
                if self.current_filter == "apps":
                    apps_results = self.get_app_results("")
                    self.render_results([r[1] for r in apps_results[:15]])
                elif self.current_filter == "windows":
                    wins = self.get_window_results("")
                    self.render_results([r[1] for r in wins[:15]])
                elif self.current_filter == "folders":
                    folders = self.get_common_folders()
                    self.render_results([r[1] for r in folders[:15]])
                elif self.current_filter == "documents":
                    docs = self.get_common_documents()
                    self.render_results([r[1] for r in docs[:15]])
                return

        # ── 1. Hot search term "fs:" for File Search ──
        if raw_text.lower().startswith("fs:") or raw_text.lower().startswith("fs "):
            self.badge_icon.set_text("fs:")
            self.badge_num.set_text("Files")
            self.badge_box.set_visible(True)

            file_query = raw_text[3:].strip().lower()
            if not file_query:
                folders = self.get_common_folders()
                docs = self.get_common_documents()
                self.render_results([r[1] for r in folders[:8] + docs[:7]])
                return

            self.debounce_timer_id = GLib.timeout_add(
                40,
                self.start_async_file_search,
                file_query,
                "all_files",
                gen
            )
            return

        # If not in fs: mode and current_filter is "all", hide badge
        if self.current_filter == "all":
            self.badge_box.set_visible(False)

        # ── 2. Explicit Category Filter Active (from ⌘1..4 or companion buttons) ──
        if self.current_filter != "all":
            if self.current_filter == "apps":
                results = self.get_app_results(query_lower)
                self.render_results([r[1] for r in results[:15]])
            elif self.current_filter == "windows":
                results = self.get_window_results(query_lower)
                self.render_results([r[1] for r in results[:15]])
            elif self.current_filter in ("folders", "documents"):
                self.debounce_timer_id = GLib.timeout_add(
                    60,
                    self.start_async_file_search,
                    query_lower,
                    self.current_filter,
                    gen
                )
            return

        # ── 3. Space entered -> Google Web Results with Autocompletions ──
        if " " in raw_text:
            web_query = query
            if not web_query:
                self.revealer.set_reveal_child(False)
                return

            google_icon = os.path.join(ICONS_DIR, "google.svg")
            immediate_results = [{
                "category": "Google",
                "title": web_query,
                "subtitle": f"Search Google for \"{web_query}\"",
                "icon_file": google_icon,
                "action": lambda q=web_query: self.open_web_search(q)
            }]
            self.render_results(immediate_results)

            self.debounce_timer_id = GLib.timeout_add(
                50,
                self.start_async_google_suggestions,
                web_query,
                gen
            )
            return

        # ── 4. Default Search (Single word, no prefix, no space) ──
        # By default, search ONLY Applications!
        app_results = self.get_app_results(query_lower)
        app_results.sort(key=lambda x: x[0], reverse=True)
        top_apps = [r[1] for r in app_results[:15]]
        self.render_results(top_apps)

    def get_app_results(self, query):
        results = []
        for app in self.apps:
            name_lower = app["name"].lower()
            comm_lower = app["comment"].lower()
            if not query or query in name_lower or query in comm_lower:
                score = 0
                if query:
                    if name_lower == query:
                        score = 50
                    elif name_lower.startswith(query):
                        score = 35
                    elif query in name_lower:
                        score = 20
                    elif query in comm_lower:
                        score = 10
                results.append((score, {
                    "category": "Application",
                    "title": app["name"],
                    "subtitle": app["comment"] or "Application",
                    "icon": app["icon"],
                    "action": lambda a=app: self.launch_app(a)
                }))
        return results

    def get_window_results(self, query):
        windows = self.get_open_windows()
        results = []
        for win in windows:
            t_lower = win["title"].lower()
            a_lower = win["app"].lower()
            if not query or query in t_lower or query in a_lower:
                score = 30 if query else 10
                if query:
                    if t_lower == query or a_lower == query:
                        score = 52
                    elif t_lower.startswith(query):
                        score = 42
                    elif query in t_lower:
                        score = 32
                results.append((score, {
                    "category": "Window",
                    "title": win["title"],
                    "subtitle": win["app"],
                    "icon": win["app"].lower(),
                    "action": lambda w=win: self.focus_window(w)
                }))

        # Quick System Actions
        system_actions = [
            ("Take Screenshot (Area)", "Capture selected region to ~/Pictures", "camera-photo", lambda: subprocess.Popen("grim -g \"$(slurp)\" ~/Pictures/Screenshot_$(date +%Y%m%d_%H%M%S).png", shell=True)),
            ("Take Screenshot (Screen)", "Capture entire screen to ~/Pictures", "camera-photo", lambda: subprocess.Popen("grim ~/Pictures/Screenshot_$(date +%Y%m%d_%H%M%S).png", shell=True)),
            ("Lock Screen", "Lock active user session", "system-lock-screen", lambda: subprocess.Popen(["loginctl", "lock-session"])),
            ("Sleep / Suspend", "Put computer into low power suspend mode", "system-suspend", lambda: subprocess.Popen(["systemctl", "suspend"])),
            ("Restart", "Reboot the computer", "system-reboot", lambda: subprocess.Popen(["systemctl", "reboot"])),
            ("Shut Down", "Power off the computer", "system-shutdown", lambda: subprocess.Popen(["systemctl", "poweroff"])),
            ("Log Out", "Exit current desktop session", "system-log-out", lambda: subprocess.Popen(["niri", "msg", "action", "quit"])),
        ]

        for act_title, act_sub, act_icon, act_fn in system_actions:
            title_lower = act_title.lower()
            if not query or query in title_lower:
                score = 6 if not query else (30 if title_lower.startswith(query) else 15)
                results.append((score, {
                    "category": "Action",
                    "title": act_title,
                    "subtitle": act_sub,
                    "icon": act_icon,
                    "action": lambda f=act_fn: (f(), self.hide())[1]
                }))

        return results

    def start_async_file_search(self, query, filter_mode, gen):
        self.debounce_timer_id = None
        if gen != self.search_generation:
            return False

        def worker():
            if gen != self.search_generation:
                return
            folders_only = (filter_mode == "folders")
            documents_only = (filter_mode == "documents")
            file_items = self.search_files(query, folders_only=folders_only, documents_only=documents_only, gen=gen)
            if gen == self.search_generation:
                GLib.idle_add(self.apply_file_results, gen, query, filter_mode, file_items)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def apply_file_results(self, gen, query, filter_mode, file_items):
        if gen != self.search_generation:
            return False

        file_results = []
        for item in file_items:
            path = item["path"].rstrip("/")
            is_dir = item["is_dir"]
            title_text = os.path.basename(path)
            if not title_text:
                title_text = path
            base = title_text.lower()
            score = 8
            if base == query:
                score = 48
            elif base.startswith(query):
                score = 28
            elif query in base:
                score = 15

            file_results.append((score, {
                "category": "Folder" if is_dir else "Document",
                "title": title_text,
                "subtitle": path.replace(os.path.expanduser("~"), "~"),
                "icon": self.get_file_icon(path, is_dir),
                "action": lambda p=path: self.open_file(p)
            }))

        if filter_mode in ("folders", "documents", "all_files"):
            all_results = file_results
        else:
            all_results = self.current_instant_results + file_results

        all_results.sort(key=lambda x: x[0], reverse=True)
        top_results = [r[1] for r in all_results[:15]]
        self.render_results(top_results)
        return False

    def start_async_google_suggestions(self, web_query, gen):
        self.debounce_timer_id = None
        if gen != self.search_generation:
            return False

        def worker():
            if gen != self.search_generation:
                return
            suggestions = self.fetch_google_suggestions(web_query)
            if gen == self.search_generation:
                GLib.idle_add(self.apply_google_suggestions, gen, web_query, suggestions)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def fetch_google_suggestions(self, query):
        url = "https://suggestqueries.google.com/complete/search?client=firefox&q=" + urllib.parse.quote_plus(query)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)'})
        try:
            with urllib.request.urlopen(req, timeout=1.2) as response:
                data = json.loads(response.read().decode('utf-8'))
                if len(data) > 1 and isinstance(data[1], list):
                    return data[1]
        except Exception:
            pass
        return []

    def apply_google_suggestions(self, gen, web_query, suggestions):
        if gen != self.search_generation:
            return False

        google_icon = os.path.join(ICONS_DIR, "google.svg")
        results = [{
            "category": "Google",
            "title": web_query,
            "subtitle": f"Search Google for \"{web_query}\"",
            "icon_file": google_icon,
            "action": lambda q=web_query: self.open_web_search(q)
        }]

        seen = {web_query.lower()}
        for s in suggestions:
            s_clean = s.strip()
            if s_clean.lower() in seen:
                continue
            seen.add(s_clean.lower())
            results.append({
                "category": "Google",
                "title": s_clean,
                "subtitle": "Google Search Suggestion",
                "icon_file": google_icon,
                "action": lambda q=s_clean: self.open_web_search(q)
            })

        # Also check if any installed application matches this query (e.g. "google chrome")
        app_matches = self.get_app_results(web_query.lower())
        if app_matches:
            for score, app_item in app_matches[:2]:
                results.append(app_item)

        self.render_results(results[:15])
        return False

    def open_web_search(self, query):
        url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception:
            subprocess.Popen(["xdg-open", url])
        self.hide()

    def clear_results(self):
        self.list_box.remove_all()
        self.results_data = []

    def render_results(self, top_results):
        selected_row = self.list_box.get_selected_row()
        prev_idx = selected_row.get_index() if selected_row else 0

        self.results_data = top_results

        if not top_results:
            self.revealer.set_reveal_child(False)
            return

        self.list_box.remove_all()

        for item in top_results:
            row = Gtk.ListBoxRow()
            row.add_css_class("result-row")

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

            # Icon
            if item.get("icon_file") and os.path.exists(item["icon_file"]):
                img = Gtk.Image.new_from_file(item["icon_file"])
            elif item.get("icon") and os.path.isabs(item["icon"]) and os.path.exists(item["icon"]):
                img = Gtk.Image.new_from_file(item["icon"])
            else:
                img = Gtk.Image.new_from_icon_name(item.get("icon", "application-x-executable"))
            img.set_pixel_size(32)
            hbox.append(img)

            # Text box (Title + Subtitle)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            vbox.set_hexpand(True)

            lbl_title = Gtk.Label(label=item["title"])
            lbl_title.add_css_class("result-title")
            lbl_title.set_xalign(0)
            lbl_title.set_ellipsize(Pango.EllipsizeMode.END)
            vbox.append(lbl_title)

            lbl_sub = Gtk.Label(label=item["subtitle"])
            lbl_sub.add_css_class("result-subtitle")
            lbl_sub.set_xalign(0)
            lbl_sub.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            vbox.append(lbl_sub)

            hbox.append(vbox)

            # Category pill tag
            tag = Gtk.Label(label=item["category"])
            tag.add_css_class("category-tag")
            if item.get("category") == "Google":
                tag.add_css_class("google")
            hbox.append(tag)

            row.set_child(hbox)
            self.list_box.append(row)

        self.revealer.set_reveal_child(True)
        target_idx = min(prev_idx, len(top_results) - 1) if top_results else 0
        target_row = self.list_box.get_row_at_index(target_idx)
        if target_row:
            self.list_box.select_row(target_row)

    def get_open_windows(self):
        try:
            out = subprocess.check_output(["niri", "msg", "-j", "windows"], text=True)
            data = json.loads(out)
            wins = []
            for w in data:
                wins.append({
                    "id": w.get("id"),
                    "title": w.get("title") or "Window",
                    "app": w.get("app_id") or "Application"
                })
            return wins
        except Exception:
            return []

    def focus_window(self, win):
        try:
            subprocess.run(["niri", "msg", "action", "focus-window", "--id", str(win["id"])])
        except Exception:
            pass
        self.hide()

    def search_files(self, query, folders_only=False, documents_only=False, gen=None):
        if not query:
            return []
        items = []
        home = os.path.expanduser("~")
        cfg = os.path.join(home, ".config")
        find_type = ["-t", "d"] if folders_only else (["-t", "f"] if documents_only else ["-t", "f", "-t", "d"])
        exclusions = [
            "--exclude", ".git",
            "--exclude", "node_modules",
            "--exclude", ".cache",
            "--exclude", ".var",
            "--exclude", ".gradle",
            "--exclude", "snap",
            "--exclude", ".local",
            "--exclude", "Android",
            "--exclude", "google-chrome",
            "--exclude", "chromium",
            "--exclude", "__pycache__",
            "--exclude", ".cargo",
            "--exclude", ".rustup",
            "--exclude", ".gemini",
            "--exclude", ".codex",
            "--exclude", ".grok",
            "--exclude", "target",
        ]
        cmd_find = ["fd"] + find_type + exclusions + [".", home, cfg]
        cmd_fzf = ["fzf", "--filter", query]

        try:
            p_find = subprocess.Popen(
                cmd_find,
                cwd=home,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True
            )
            p_fzf = subprocess.Popen(
                cmd_fzf,
                stdin=p_find.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True
            )
            p_find.stdout.close()

            with self.proc_lock:
                self.active_processes.extend([p_find, p_fzf])

            try:
                out, _ = p_fzf.communicate(timeout=0.8)
            except subprocess.TimeoutExpired:
                p_find.kill()
                p_fzf.kill()
                out, _ = p_fzf.communicate()
            finally:
                with self.proc_lock:
                    if p_find in self.active_processes:
                        self.active_processes.remove(p_find)
                    if p_fzf in self.active_processes:
                        self.active_processes.remove(p_fzf)

            if gen is not None and gen != self.search_generation:
                return []

            lines = [l.strip().rstrip("/") for l in out.strip().split("\n") if l.strip()][:15]
            for l in lines:
                full_path = l if os.path.isabs(l) else os.path.join(home, l)
                items.append({
                    "path": full_path,
                    "is_dir": os.path.isdir(full_path)
                })
        except Exception:
            pass
        return items

    def launch_app(self, app):
        cmd = app["exec"]
        cmd = re.sub(r'%[fFuUdDnNickvm]', '', cmd).strip()
        subprocess.Popen(cmd, shell=True)
        self.hide()

    def open_file(self, path):
        subprocess.Popen(["xdg-open", path])
        self.hide()

    def on_entry_activate(self, entry):
        row = self.list_box.get_selected_row()
        if row:
            self.on_row_activated(self.list_box, row)
        else:
            raw = entry.get_text().strip()
            if raw:
                if not (raw.lower().startswith("fs:") or raw.lower().startswith("fs ")):
                    self.open_web_search(raw)
            else:
                self.hide()

    def on_row_activated(self, list_box, row):
        idx = row.get_index()
        if 0 <= idx < len(self.results_data):
            self.results_data[idx]["action"]()

    def on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True

        # Check Ctrl+1..4 or Alt+1..4 or Super+1..4 for instant filter toggle
        is_ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        is_alt = bool(state & Gdk.ModifierType.ALT_MASK)
        is_super = bool(state & Gdk.ModifierType.SUPER_MASK)
        if is_ctrl or is_alt or is_super:
            if keyval in (Gdk.KEY_1, Gdk.KEY_KP_1):
                self.toggle_filter("apps")
                return True
            elif keyval in (Gdk.KEY_2, Gdk.KEY_KP_2):
                self.toggle_filter("folders")
                return True
            elif keyval in (Gdk.KEY_3, Gdk.KEY_KP_3):
                self.toggle_filter("windows")
                return True
            elif keyval in (Gdk.KEY_4, Gdk.KEY_KP_4):
                self.toggle_filter("documents")
                return True

        if keyval == Gdk.KEY_Down:
            row = self.list_box.get_selected_row()
            if row:
                next_row = self.list_box.get_row_at_index(row.get_index() + 1)
                if next_row:
                    self.list_box.select_row(next_row)
                    adj = self.scrolled.get_vadjustment()
                    row_y = next_row.get_index() * 48.0
                    row_h = 48.0
                    if row_y + row_h > adj.get_value() + adj.get_page_size():
                        max_s = max(0.0, adj.get_upper() - adj.get_page_size())
                        adj.set_value(min(max_s, row_y + row_h - adj.get_page_size() + 10))
                    return True
            else:
                first_row = self.list_box.get_row_at_index(0)
                if first_row:
                    self.list_box.select_row(first_row)
                    return True
        elif keyval == Gdk.KEY_Up:
            row = self.list_box.get_selected_row()
            if row and row.get_index() > 0:
                prev_row = self.list_box.get_row_at_index(row.get_index() - 1)
                if prev_row:
                    self.list_box.select_row(prev_row)
                    adj = self.scrolled.get_vadjustment()
                    row_y = prev_row.get_index() * 48.0
                    if row_y < adj.get_value():
                        adj.set_value(max(0.0, row_y - 10))
                    return True
        elif keyval == Gdk.KEY_Tab:
            # Cycle through filter buttons
            order = ["all", "apps", "folders", "windows", "documents"]
            curr_idx = order.index(self.current_filter) if self.current_filter in order else 0
            next_filter = order[(curr_idx + 1) % len(order)]
            self.toggle_filter(next_filter if next_filter != "all" else self.current_filter)
            return True
        return False

    def start_fade_in(self):
        self.anim_state = "fade_in"
        self.anim_start_us = 0
        self.win.set_opacity(0.0)
        if self.tick_cb_id is None:
            self.tick_cb_id = self.win.add_tick_callback(self.on_anim_tick)

    def start_fade_out(self):
        if not self.win.get_visible():
            return
        self.anim_state = "fade_out"
        self.anim_start_us = 0
        if self.tick_cb_id is None:
            self.tick_cb_id = self.win.add_tick_callback(self.on_anim_tick)

    def on_anim_tick(self, widget, frame_clock):
        now_us = frame_clock.get_frame_time()
        if self.anim_start_us == 0:
            self.anim_start_us = now_us

        elapsed = now_us - self.anim_start_us
        progress = min(1.0, elapsed / self.anim_duration_us)
        # Ease out cubic: 1 - (1 - t)^3
        eased = 1.0 - math.pow(1.0 - progress, 3)

        if self.anim_state == "fade_in":
            self.win.set_opacity(eased)
            cur_margin = int(148 + (160 - 148) * eased)
            self.center_box.set_margin_top(cur_margin)
            if progress >= 1.0:
                self.win.set_opacity(1.0)
                self.center_box.set_margin_top(160)
                self.anim_state = "open"
                self.tick_cb_id = None
                return GLib.SOURCE_REMOVE

        elif self.anim_state == "fade_out":
            self.win.set_opacity(max(0.0, 1.0 - eased))
            if progress >= 1.0:
                self.win.set_opacity(0.0)
                self.win.set_visible(False)
                self.anim_state = "closed"
                self.tick_cb_id = None
                return GLib.SOURCE_REMOVE

        return GLib.SOURCE_CONTINUE

    def show(self):
        self.search_generation += 1
        if self.debounce_timer_id is not None:
            GLib.source_remove(self.debounce_timer_id)
            self.debounce_timer_id = None
        self.cancel_active_searches()
        self.fling_scroller.stop_fling()
        self.entry.set_text("")
        self.current_filter = "all"
        self.entry.set_placeholder_text("Spotlight Search")
        self.badge_box.set_visible(False)
        for b in (self.btn_apps, self.btn_folders, self.btn_windows, self.btn_docs):
            b.remove_css_class("active")
        self.revealer.set_reveal_child(False)
        self.results_data = []

        # Reset scrolled position to top
        vadj = self.scrolled.get_vadjustment()
        vadj.set_value(0.0)

        # Reset buttons revealer to unrevealed, then reveal for organic slide-out
        self.buttons_revealer.set_reveal_child(False)
        Gtk4LayerShell.set_keyboard_mode(self.win, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
        self.win.set_opacity(0.0)
        self.center_box.set_margin_top(148)
        self.win.set_visible(True)
        self.win.present()
        self.entry.grab_focus()

        # Start fade in and slide out buttons
        self.start_fade_in()
        GLib.timeout_add(30, lambda: (self.buttons_revealer.set_reveal_child(True), False)[1])

    def hide(self):
        self.search_generation += 1
        if self.debounce_timer_id is not None:
            GLib.source_remove(self.debounce_timer_id)
            self.debounce_timer_id = None
        self.cancel_active_searches()
        self.fling_scroller.stop_fling()
        self.buttons_revealer.set_reveal_child(False)
        self.revealer.set_reveal_child(False)
        Gtk4LayerShell.set_keyboard_mode(self.win, Gtk4LayerShell.KeyboardMode.NONE)
        self.start_fade_out()

    def toggle(self):
        if self.win.get_visible() and self.anim_state != "fade_out":
            self.hide()
        else:
            self.show()

class DaemonServer:
    def __init__(self, window):
        self.window = window
        self.sock_path = SOCKET_PATH
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.sock_path)
        self.server.listen(5)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while True:
            try:
                conn, _ = self.server.accept()
                data = conn.recv(1024).decode()
                if data.endswith("\n"):
                    data = data[:-1]
                if data.endswith("\r"):
                    data = data[:-1]
                print(f"[Daemon] Received: {data!r}", flush=True)
                if data == "toggle":
                    GLib.idle_add(self.window.toggle)
                elif data == "show":
                    GLib.idle_add(self.window.show)
                elif data == "hide":
                    GLib.idle_add(self.window.hide)
                elif data.startswith("query "):
                    q = data[6:]
                    def set_q(text=q):
                        self.window.show()
                        self.window.entry.set_text(text)
                        self.window.entry.set_position(-1)
                    GLib.idle_add(set_q)
                elif data.startswith("filter "):
                    f = data[7:].strip()
                    def set_f(filt=f):
                        self.window.show()
                        self.window.toggle_filter(filt)
                    GLib.idle_add(set_f)
                conn.sendall(b"OK\n")
                conn.close()
            except Exception:
                break

def main():
    app = Gtk.Application(
        application_id="org.macos.spotlight.daemon",
        flags=Gio.ApplicationFlags.NON_UNIQUE
    )
    def on_activate(app):
        window = SpotlightWindow(app)
        DaemonServer(window)
        # If started with 'daemon', stay hidden until invoked
        if len(sys.argv) > 1 and sys.argv[1] == "daemon":
            pass
        else:
            window.show()
    app.connect("activate", on_activate)
    app.run([])

if __name__ == "__main__":
    main()
