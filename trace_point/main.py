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
import shutil
import glob
import re
import socket
import threading
import json
import time
import math
import ctypes
import shlex
import urllib.request
import urllib.parse

SOCKET_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "trace_point.sock")

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
LAYER_SHELL_LIB = None
for _cand in [
    "/usr/lib64/libgtk4-layer-shell.so.0",
    "/usr/lib/x86_64-linux-gnu/libgtk4-layer-shell.so.0",
    "/usr/lib/libgtk4-layer-shell.so.0",
    "/usr/local/lib/libgtk4-layer-shell.so.0",
    os.path.expanduser("~/.local/lib/libgtk4-layer-shell.so.0"),
]:
    if os.path.exists(_cand):
        LAYER_SHELL_LIB = _cand
        break
else:
    LAYER_SHELL_LIB = "/usr/lib64/libgtk4-layer-shell.so.0"

# The GPU renderer only repaints the new glyph bounds. On a translucent
# surface, fast backspace leaves the previous letters behind. Forcing a
# full redraw clears them. Must be set before GTK creates its renderer,
# and before the layer-shell re-exec so the new process inherits it.
_gsk_debug = [part for part in os.environ.get("GSK_DEBUG", "").split(",") if part]
if "full-redraw" not in _gsk_debug:
    _gsk_debug.append("full-redraw")
os.environ["GSK_DEBUG"] = ",".join(_gsk_debug)

if os.path.exists(LAYER_SHELL_LIB) and LAYER_SHELL_LIB not in os.environ.get("LD_PRELOAD", ""):
    env = dict(os.environ)
    existing = env.get("LD_PRELOAD", "")
    env["LD_PRELOAD"] = f"{LAYER_SHELL_LIB}:{existing}" if existing else LAYER_SHELL_LIB
    os.execvpe(sys.executable, [sys.executable] + sys.argv, env)

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gtk, Gdk, Gtk4LayerShell, GLib, Gio, Pango

# Clean LD_PRELOAD from process environment so spawned applications (Electron, GTK3, etc.) do not crash
if "LD_PRELOAD" in os.environ:
    preloads = [p for p in os.environ["LD_PRELOAD"].split(":") if p and p != LAYER_SHELL_LIB]
    if preloads:
        os.environ["LD_PRELOAD"] = ":".join(preloads)
    else:
        os.environ.pop("LD_PRELOAD", None)


def _child_env(env=None):
    """Copy env and strip TracePoint-only LD_PRELOAD (gtk4-layer-shell)."""
    out = dict(env if env is not None else os.environ)
    if "LD_PRELOAD" in out:
        preloads = [
            p for p in out["LD_PRELOAD"].split(":")
            if p and p != LAYER_SHELL_LIB and "gtk4-layer-shell" not in p
        ]
        if preloads:
            out["LD_PRELOAD"] = ":".join(preloads)
        else:
            out.pop("LD_PRELOAD", None)
    return out


def spawn_detached(cmd, *, shell=False, env=None):
    """Launch an independent process that survives TracePoint daemon death.

    Prefer systemd-run --user --scope so the app lives outside the service
    cgroup; fall back to Popen(start_new_session=True). Always strip
    TracePoint-only LD_PRELOAD so Electron/GTK apps do not inherit
    libgtk4-layer-shell (avoids gdk_display_manager_get Abort).
    """
    child_env = _child_env(env)
    popen_kw = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=child_env,
        start_new_session=True,
    )
    systemd_run = shutil.which("systemd-run")
    try:
        if systemd_run:
            prefix = [systemd_run, "--user", "--scope", "--collect", "--"]
            if shell:
                shell_cmd = cmd if isinstance(cmd, str) else " ".join(cmd)
                argv = prefix + ["/bin/sh", "-c", shell_cmd]
            else:
                argv = prefix + ([cmd] if isinstance(cmd, str) else list(cmd))
            return subprocess.Popen(argv, **popen_kw)
        return subprocess.Popen(cmd, shell=shell, **popen_kw)
    except Exception:
        try:
            return subprocess.Popen(cmd, shell=shell, **popen_kw)
        except Exception:
            return None


_PKG_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def read_desktop_entry_keys(path, wanted):
    """Return the first value of each requested key in the main desktop group."""
    found = {}
    wanted = set(wanted)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            in_entry = False
            for line in handle:
                line = line.strip()
                if line == "[Desktop Entry]":
                    in_entry = True
                    continue
                if line.startswith("[") and in_entry:
                    break
                if not in_entry or "=" not in line or line.startswith("#"):
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in wanted and key not in found:
                    found[key] = value.strip()
                    if len(found) == len(wanted):
                        break
    except OSError:
        return {}
    return found


def rpm_package_name(path):
    """Package that owns this file, or None when rpm does not own it."""
    if not path or not os.path.lexists(path):
        return None
    try:
        out = subprocess.check_output(
            ["rpm", "-qf", "--queryformat", "%{NAME}", path],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=4,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    name = out.splitlines()[0].strip() if out else ""
    if not name or "not owned" in name.lower() or not _PKG_NAME_RE.fullmatch(name):
        return None
    return name


def _path_has(path, fragment):
    try:
        real = os.path.realpath(path)
    except OSError:
        real = path
    return fragment in os.path.abspath(path) or fragment in real


def _flatpak_id_from_exec(exec_cmd):
    if not exec_cmd or "flatpak" not in exec_cmd:
        return None
    try:
        tokens = shlex.split(exec_cmd)
    except ValueError:
        tokens = exec_cmd.split()
    if "run" not in tokens:
        return None
    index = tokens.index("run") + 1
    valued = {"--branch", "--arch", "--command", "--runtime", "--runtime-version", "--installation"}
    while index < len(tokens):
        token = tokens[index]
        if token in valued:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if _APP_ID_RE.fullmatch(token):
            return token
        index += 1
    return None


def _snap_name_from_exec(exec_cmd):
    if not exec_cmd or "snap" not in exec_cmd:
        return None
    try:
        tokens = shlex.split(exec_cmd)
    except ValueError:
        tokens = exec_cmd.split()
    if "run" not in tokens:
        return None
    index = tokens.index("run") + 1
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 1
    if index < len(tokens) and _APP_ID_RE.fullmatch(tokens[index]):
        return tokens[index]
    return None


def flatpak_is_installed(app_id, scope, flatpak_bin=None):
    flatpak_bin = flatpak_bin or shutil.which("flatpak")
    if not flatpak_bin or not app_id:
        return False
    flag = "--user" if scope == "user" else "--system"
    try:
        subprocess.run(
            [flatpak_bin, "info", flag, app_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def snap_is_installed(name, snap_bin=None):
    snap_bin = snap_bin or shutil.which("snap")
    if not snap_bin or not name:
        return False
    try:
        subprocess.run(
            [snap_bin, "list", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def classify_desktop_entry(
    path,
    application_dirs,
    rpm_name=rpm_package_name,
    command_exists=shutil.which,
    flatpak_installed=flatpak_is_installed,
    snap_installed=snap_is_installed,
):
    """Decide how a desktop entry can be removed.

    Packaged apps uninstall through dnf, Flatpak, or Snap. Anything else is a
    local launcher file. The returned command is never a shell string.
    """
    unknown = {"kind": "unknown", "package": None, "needs_auth": False, "argv": None, "local_path": None, "desktop_id": None}
    if not path or not str(path).endswith(".desktop") or not os.path.lexists(path):
        return unknown

    desktop_id = os.path.basename(path)
    abspath = os.path.abspath(path)
    try:
        real = os.path.realpath(path)
    except OSError:
        real = abspath

    def plan(**kwargs):
        result = {
            "kind": "unknown",
            "package": None,
            "needs_auth": False,
            "argv": None,
            "local_path": None,
            "desktop_id": desktop_id,
        }
        result.update(kwargs)
        return result

    keys = read_desktop_entry_keys(path, ("X-Flatpak", "X-SnapInstanceName", "Exec", "Name"))
    flatpak_bin = command_exists("flatpak")
    snap_bin = command_exists("snap")
    dnf_bin = command_exists("dnf") or command_exists("dnf5")
    pkexec_bin = command_exists("pkexec")

    app_id = keys.get("X-Flatpak") or _flatpak_id_from_exec(keys.get("Exec", ""))
    if not app_id and ("/flatpak/" in abspath or "/flatpak/" in real):
        stem = desktop_id[:-len(".desktop")]
        if _APP_ID_RE.fullmatch(stem):
            app_id = stem
    if flatpak_bin and app_id and _APP_ID_RE.fullmatch(app_id):
        if "/.local/share/flatpak/" in abspath:
            scopes = ("user", "system")
        elif "/flatpak/" in abspath or "/flatpak/" in real:
            scopes = ("system", "user")
        else:
            scopes = ("user", "system")
        for scope in scopes:
            if not flatpak_installed(app_id, scope):
                continue
            if scope == "user":
                return plan(
                    kind="flatpak",
                    package=app_id,
                    needs_auth=False,
                    argv=[flatpak_bin, "uninstall", "--user", "-y", "--noninteractive", app_id],
                )
            if pkexec_bin:
                return plan(
                    kind="flatpak",
                    package=app_id,
                    needs_auth=True,
                    argv=[pkexec_bin, flatpak_bin, "uninstall", "--system", "-y", "--noninteractive", app_id],
                )

    snap_name = keys.get("X-SnapInstanceName") or _snap_name_from_exec(keys.get("Exec", ""))
    if not snap_name and "/snapd/" in abspath:
        stem = desktop_id[:-len(".desktop")]
        if "_" in stem:
            snap_name = stem.split("_", 1)[0]
    if (
        snap_bin and pkexec_bin and snap_name and _APP_ID_RE.fullmatch(snap_name)
        and snap_installed(snap_name)
    ):
        return plan(
            kind="snap",
            package=snap_name,
            needs_auth=True,
            argv=[pkexec_bin, snap_bin, "remove", snap_name],
        )

    package = None
    if rpm_name is not None:
        package = rpm_name(path)
        if not package:
            for directory in application_dirs or []:
                candidate = os.path.join(directory, desktop_id)
                if os.path.abspath(candidate) == abspath or not os.path.lexists(candidate):
                    continue
                package = rpm_name(candidate) or rpm_name(os.path.realpath(candidate))
                if package:
                    break
    if package and dnf_bin and pkexec_bin and _PKG_NAME_RE.fullmatch(package):
        return plan(
            kind="dnf",
            package=package,
            needs_auth=True,
            argv=[pkexec_bin, dnf_bin, "remove", "-y", package],
        )

    parent_writable = os.access(os.path.dirname(path), os.W_OK)
    file_writable = os.access(path, os.W_OK) or (os.path.islink(path) and parent_writable)
    home = os.path.realpath(os.path.expanduser("~"))
    try:
        parent = os.path.realpath(os.path.dirname(path))
    except OSError:
        parent = ""
    in_home = parent == home or parent.startswith(home + os.sep)
    in_app_dir = any(parent == os.path.realpath(directory) for directory in (application_dirs or []))
    if file_writable and in_home and in_app_dir:
        return plan(kind="local", local_path=path)
    return plan()


def removal_prompt(plan, app_title):
    """Question shown before a desktop entry is deleted."""
    title = app_title or "this app"
    kind = plan.get("kind")
    package = plan.get("package")
    if kind == "dnf":
        return (
            f"Remove {title} from the system?\n"
            f"It was installed with dnf as “{package}”. "
            "Your password unlocks the package manager, which removes that package in the background."
        )
    if kind == "flatpak":
        where = "your user Flatpak install" if not plan.get("needs_auth") else "the system Flatpak install"
        password = " Your password is required." if plan.get("needs_auth") else ""
        return (
            f"Remove {title} from the system?\n"
            f"It was installed with Flatpak ({package}) in {where}.{password} "
            "Uninstall runs in the background."
        )
    if kind == "snap":
        return (
            f"Remove {title} from the system?\n"
            f"It was installed with Snap as “{package}”. "
            "Your password unlocks snap, which removes that package in the background."
        )
    if kind == "local":
        return (
            f"Delete the {title} launcher?\n"
            "This was not installed by dnf, Flatpak, or Snap, so only the local desktop file is deleted."
        )
    return (
        f"TracePoint can't tell how {title} was installed, and the file isn't a local launcher it can delete."
    )


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
    background-color: rgba(32, 36, 44, 0.46);
    border: 1px solid rgba(255, 255, 255, 0.30);
    border-radius: 9999px;
    padding-left: 20px;
    padding-right: 16px;
    min-height: 56px;
    min-width: 470px;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.22), 0 16px 36px rgba(0, 0, 0, 0.28);
    transition: border-color 200ms cubic-bezier(0.16, 1, 0.3, 1), box-shadow 200ms cubic-bezier(0.16, 1, 0.3, 1);
}

.spotlight-pill:focus-within {
    border-color: rgba(255, 255, 255, 0.28);
    box-shadow: 0 20px 44px rgba(0, 0, 0, 0.45), 0 4px 14px rgba(0, 0, 0, 0.16);
}

.spotlight-icon {
    margin-right: 12px;
    opacity: 0.95;
    transition: opacity 160ms ease, transform 160ms ease;
}

.spotlight-entry {
    background-color: transparent;
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

.spotlight-entry text,
.spotlight-entry undershoot,
.spotlight-entry overshoot {
    background-color: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
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
    background-color: rgba(32, 36, 44, 0.46);
    border: 1px solid rgba(255, 255, 255, 0.30);
    border-radius: 9999px;
    min-width: 56px;
    min-height: 56px;
    padding: 0;
    margin: 0;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.22), 0 16px 36px rgba(0, 0, 0, 0.28);
    transition: background-color 180ms cubic-bezier(0.16, 1, 0.3, 1), border-color 180ms cubic-bezier(0.16, 1, 0.3, 1), box-shadow 180ms cubic-bezier(0.16, 1, 0.3, 1), transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

button.spotlight-circle-btn:hover {
    background-color: rgba(58, 66, 80, 0.55);
    border-color: rgba(255, 255, 255, 0.42);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.28), 0 20px 44px rgba(0, 0, 0, 0.34);
    transform: scale(1.06);
}

button.spotlight-circle-btn:active {
    transform: scale(0.95);
}

button.spotlight-circle-btn.active {
    background: rgba(0, 122, 255, 0.35);
    border: 1px solid #007aff;
    box-shadow: 0 0 0 2px rgba(0, 122, 255, 0.50), 0 16px 36px rgba(0, 0, 0, 0.35);
    transform: scale(1.0);
}

/* ── Results Dropdown ── */
revealer {
    background: transparent;
}

.spotlight-results-panel {
    background-color: rgba(28, 32, 40, 0.42);
    border: 1px solid rgba(255, 255, 255, 0.24);
    border-radius: 28px;
    margin-top: 14px;
    padding: 10px;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.16), 0 24px 60px rgba(0, 0, 0, 0.32);
    transition: opacity 160ms cubic-bezier(0.16, 1, 0.3, 1), box-shadow 200ms cubic-bezier(0.16, 1, 0.3, 1);
}

.spotlight-results-panel scrolledwindow,
.spotlight-results-panel scrolledwindow > viewport,
.spotlight-results-panel viewport,
.spotlight-results-panel list,
.spotlight-results-panel list > row,
scrolledwindow,
viewport,
list,
listview,
.spotlight-scroll-content,
list.spotlight-scroll-content {
    background: transparent;
    background-color: transparent;
    border: none;
    box-shadow: none;
    outline: none;
}

.result-row,
list > row {
    background: transparent;
    background-color: transparent;
    border: none;
    outline: none;
    box-shadow: none;
    border-radius: 16px;
    padding: 8px 14px;
    margin: 2px 0px;
    transition: background-color 120ms cubic-bezier(0.16, 1, 0.3, 1), color 120ms ease, opacity 140ms cubic-bezier(0.16, 1, 0.3, 1);
}

.result-row.row-enter {
    opacity: 0;
}

.result-row:hover,
list > row:hover {
    background-color: rgba(255, 255, 255, 0.08);
}

.result-row:selected,
.result-row:focus,
list > row:selected,
list > row:focus {
    background-color: #007aff;
    color: #ffffff;
}

.result-title {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
    font-size: 15px;
    font-weight: 500;
    color: #f5f5f7;
}

.result-row:selected .result-title,
list > row:selected .result-title {
    color: #ffffff;
}

.result-subtitle {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
    font-size: 12px;
    color: rgba(255, 255, 255, 0.65);
}

.result-row:selected .result-subtitle,
list > row:selected .result-subtitle {
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

.result-row:selected .category-tag,
list > row:selected .category-tag {
    background-color: rgba(255, 255, 255, 0.28);
    color: #ffffff;
}

.category-tag.google {
    background-color: rgba(66, 133, 244, 0.22);
    color: #8ab4f8;
}

.result-row:selected .category-tag.google,
list > row:selected .category-tag.google {
    background-color: rgba(255, 255, 255, 0.30);
    color: #ffffff;
}

/* ── macOS-Style Sleek Scrollbar ── */
scrollbar {
    background: transparent;
    background-color: transparent;
    border: none;
    padding: 0;
    margin: 0;
    transition: opacity 320ms ease;
}

scrollbar trough {
    background: transparent;
    background-color: transparent;
    border: none;
}

scrollbar slider {
    background-color: rgba(255, 255, 255, 0.20);
    border-radius: 9999px;
    min-width: 5px;
    min-height: 24px;
    margin-right: 4px;
    transition: background-color 280ms ease, opacity 320ms ease;
    opacity: 0.75;
}

scrollbar slider:hover {
    background-color: rgba(255, 255, 255, 0.48);
    opacity: 1.0;
}

/* Opacity fade while typing. Transform stays immediate for the rubber-band. */
.spotlight-scroll-content {
    transition: opacity 140ms cubic-bezier(0.16, 1, 0.3, 1);
}

.spotlight-context-menu {
    background-color: rgba(28, 32, 40, 0.62);
    border: 1px solid rgba(255, 255, 255, 0.24);
    border-radius: 16px;
    padding: 6px;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.16), 0 16px 36px rgba(0, 0, 0, 0.35);
}

button.spotlight-context-btn {
    background: transparent;
    border: none;
    border-radius: 12px;
    padding: 8px 18px;
    color: #ffb4ab;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
    font-size: 14px;
    font-weight: 600;
}

button.spotlight-context-btn:hover {
    background-color: rgba(255, 90, 80, 0.22);
    color: #ffd7d2;
}

.spotlight-context-copy {
    color: rgba(255, 255, 255, 0.88);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
    font-size: 13px;
    font-weight: 450;
    padding: 8px 10px 4px 10px;
}

button.spotlight-context-cancel {
    background: transparent;
    border: none;
    border-radius: 12px;
    padding: 8px 14px;
    color: rgba(255, 255, 255, 0.88);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
    font-size: 14px;
    font-weight: 550;
}

button.spotlight-context-cancel:hover {
    background-color: rgba(255, 255, 255, 0.10);
}
"""

class SpringScroller:
    """macOS-style spring + rubber-band scroll physics (2026 Spotlight feel).

    One coherent helper for wheel/trackpad deltas, kinetic fling, and keyboard
    selection scrolls. Logical position may leave [0, max]; rubber-band maps
    overflow to a resisted visual offset (CSS translateY on list content).
    """

    # Asymptotic fling coast (closer to Apple than linear friction*0.90)
    FLING_DECEL = 3.8          # 1/s exponential velocity decay
    FLING_STOP_VEL = 28.0      # px/s — end coast / hand off to spring-back
    # Rubber-band (UIScrollView-like): y = (1 - 1/(x*c/d + 1)) * d
    RUBBER_C = 0.55
    # Critically-damped spring-back into bounds
    SPRING_OMEGA = 15.5        # rad/s — ~250–320ms settle
    SPRING_ZETA = 1.0
    # Keyboard selection: ease-out-expo + optional tiny edge overshoot
    SELECT_DURATION_MS = 240.0
    SELECT_OVERSHOOT_PX = 7.0
    # Fallback tick when no Gdk frame clock (~200Hz). Prefer vsync via add_tick_callback.
    FALLBACK_TICK_MS = 5
    DT_MIN_MS = 1.0
    DT_MAX_MS = 32.0

    def __init__(self, get_adj_fn, get_content_fn=None, get_tick_widget_fn=None):
        self.get_adj = get_adj_fn
        self.get_content = get_content_fn or (lambda: None)
        self.get_tick_widget = get_tick_widget_fn or (lambda: None)
        self.timer = None          # GLib.timeout_add id (fallback)
        self.tick_cb_id = None     # Gtk tick callback id (frame clock)
        self._last_frame_us = 0
        self.mode = "idle"  # idle | fling | spring | select
        self.position = 0.0
        self.velocity = 0.0
        self._overscroll_css = None
        self._visual_offset = 0.0
        # Selection animation state
        self._sel_start = 0.0
        self._sel_target = 0.0
        self._sel_overshoot = 0.0
        self._sel_start_ms = 0.0
        self._sel_duration_ms = self.SELECT_DURATION_MS
        self._edge_settle_id = None

    def _ensure_css_provider(self):
        if self._overscroll_css is not None:
            return
        self._overscroll_css = Gtk.CssProvider()
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._overscroll_css,
                Gtk.STYLE_PROVIDER_PRIORITY_USER + 1,
            )

    def _max_scroll(self, adj=None):
        adj = adj or self.get_adj()
        if not adj:
            return 0.0
        return max(0.0, adj.get_upper() - adj.get_page_size())

    def _page_size(self, adj=None):
        adj = adj or self.get_adj()
        if not adj:
            return 1.0
        return max(1.0, adj.get_page_size())

    @staticmethod
    def rubber_band(offset, dimension, c=RUBBER_C):
        """Map unconstrained past-edge distance to resisted rubber distance."""
        if offset <= 0.0 or dimension <= 0.0:
            return 0.0
        return (1.0 - (1.0 / ((offset * c / dimension) + 1.0))) * dimension

    @staticmethod
    def inv_rubber_band(visual, dimension, c=RUBBER_C):
        """Approximate inverse so consecutive deltas stay continuous at edges."""
        if visual <= 0.0 or dimension <= 0.0:
            return 0.0
        # visual = (1 - 1/(x*c/d + 1)) * d  =>  x = d/c * (visual/(d-visual))
        if visual >= dimension * 0.95:
            visual = dimension * 0.95
        return (dimension / c) * (visual / (dimension - visual))

    def sync_from_adj(self):
        adj = self.get_adj()
        if adj:
            self.position = adj.get_value()
            self.velocity = 0.0
            self._set_visual_offset(0.0)

    def stop(self):
        self._clear_tick_sources()
        self._cancel_edge_settle()
        self.mode = "idle"
        self.velocity = 0.0

    def stop_fling(self):
        """Compat alias used by SpotlightWindow."""
        self.stop()

    def _cancel_edge_settle(self):
        if getattr(self, "_edge_settle_id", None) is not None:
            GLib.source_remove(self._edge_settle_id)
            self._edge_settle_id = None

    def _arm_edge_settle(self):
        self._cancel_edge_settle()
        self._edge_settle_id = GLib.timeout_add(50, self._edge_settle_cb)

    def _edge_settle_cb(self):
        self._edge_settle_id = None
        adj = self.get_adj()
        max_s = self._max_scroll(adj)
        if self.position < -0.5 or self.position > max_s + 0.5:
            self.mode = "spring"
            self.velocity = 0.0
            self._start_timer()
        return GLib.SOURCE_REMOVE

    def _clear_tick_sources(self):
        if self.timer is not None:
            try:
                GLib.source_remove(self.timer)
            except Exception:
                pass
            self.timer = None
        if self.tick_cb_id is not None:
            widget = None
            try:
                widget = self.get_tick_widget()
            except Exception:
                widget = None
            if widget is not None:
                try:
                    widget.remove_tick_callback(self.tick_cb_id)
                except Exception:
                    pass
            self.tick_cb_id = None
        self._last_frame_us = 0

    def _start_timer(self):
        """Drive physics from Gdk frame clock (~display Hz) when possible."""
        if self.timer is not None or self.tick_cb_id is not None:
            return
        widget = None
        try:
            widget = self.get_tick_widget()
        except Exception:
            widget = None
        if widget is not None:
            self._last_frame_us = 0
            self.tick_cb_id = widget.add_tick_callback(self._on_frame_tick)
        else:
            self.timer = GLib.timeout_add(self.FALLBACK_TICK_MS, self._on_timeout_tick)

    def _on_frame_tick(self, widget, frame_clock):
        now_us = frame_clock.get_frame_time()
        if self._last_frame_us <= 0:
            dt = self.FALLBACK_TICK_MS / 1000.0
        else:
            dt_ms = (now_us - self._last_frame_us) / 1000.0
            dt_ms = max(self.DT_MIN_MS, min(self.DT_MAX_MS, dt_ms))
            dt = dt_ms / 1000.0
        self._last_frame_us = now_us
        if not self._step(dt):
            self.tick_cb_id = None
            self._last_frame_us = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _on_timeout_tick(self):
        dt = self.FALLBACK_TICK_MS / 1000.0
        if not self._step(dt):
            self.timer = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _set_visual_offset(self, offset_px):
        """offset > 0: past top (content pulled down); < 0: past bottom."""
        if abs(offset_px - self._visual_offset) < 0.05 and abs(offset_px) < 0.05 and abs(self._visual_offset) < 0.05:
            return
        self._visual_offset = offset_px
        self._ensure_css_provider()
        css = ".spotlight-scroll-content {{ transform: translateY({0:.2f}px); }}\n".format(offset_px)
        try:
            self._overscroll_css.load_from_data(css.encode("utf-8"))
        except Exception:
            pass

    def _apply_position(self):
        """Write clamped adj value + rubber-band visual for any overflow."""
        adj = self.get_adj()
        if not adj:
            return
        max_s = self._max_scroll(adj)
        page = self._page_size(adj)
        # Cap unconstrained overscroll so spring-back stays snappy
        limit = page * 1.15
        if self.position < -limit:
            self.position = -limit
            if self.velocity < 0:
                self.velocity = 0.0
        elif self.position > max_s + limit:
            self.position = max_s + limit
            if self.velocity > 0:
                self.velocity = 0.0
        pos = self.position

        if pos < 0.0:
            unconstrained = -pos
            visual = self.rubber_band(unconstrained, page)
            adj.set_value(0.0)
            self._set_visual_offset(visual)
        elif pos > max_s:
            unconstrained = pos - max_s
            visual = self.rubber_band(unconstrained, page)
            adj.set_value(max_s)
            self._set_visual_offset(-visual)
        else:
            adj.set_value(pos)
            self._set_visual_offset(0.0)

    def apply_delta(self, delta_px):
        """Wheel/trackpad scroll delta with continuous rubber-band at edges."""
        adj = self.get_adj()
        if not adj:
            return
        max_s = self._max_scroll(adj)
        if max_s <= 0 and abs(self.position) < 0.5 and abs(self._visual_offset) < 0.5:
            return

        # Interrupt fling/select/spring but keep current logical position
        if self.mode != "idle":
            was_pos = self.position
            self.stop()
            self.position = was_pos

        self.position = self.position + delta_px
        self.velocity = 0.0
        self._apply_position()

        # Debounced spring-back so consecutive trackpad deltas stay continuous;
        # cancelled by further deltas or start_fling.
        self._arm_edge_settle()

    def start_fling(self, vel_y):
        """Begin asymptotic kinetic coast; rubber-band + spring at ends."""
        self.stop()
        adj = self.get_adj()
        if adj:
            # Prefer live adj when we were clamped; keep overscroll if present
            if abs(self._visual_offset) < 0.5:
                self.position = adj.get_value()
        self.velocity = vel_y
        if abs(self.velocity) < 20:
            max_s = self._max_scroll(adj)
            if self.position < 0.0 or self.position > max_s:
                self.mode = "spring"
                self._start_timer()
            return
        self.mode = "fling"
        self._start_timer()

    def animate_to(self, adj, target, duration_ms=None, bounce=True):
        """Keyboard selection scroll: ease-out-expo (~240ms), subtle edge bounce."""
        if not adj:
            adj = self.get_adj()
        if not adj:
            return
        self.stop()
        max_s = self._max_scroll(adj)
        if abs(self._visual_offset) < 0.5:
            self.position = adj.get_value()
        self._sel_start = self.position
        clamped = max(0.0, min(max_s, target))
        self._sel_target = clamped
        if abs(clamped - self._sel_start) < 0.5:
            self.position = clamped
            self._apply_position()
            return

        duration = self.SELECT_DURATION_MS if duration_ms is None else float(duration_ms)
        self._sel_duration_ms = max(160.0, min(320.0, duration))
        # Tiny overshoot when destination is near an edge (Spotlight-like settle)
        self._sel_overshoot = 0.0
        if bounce:
            dist = clamped - self._sel_start
            near_edge = clamped <= 8.0 or clamped >= max_s - 8.0
            if near_edge and abs(dist) > 12.0:
                self._sel_overshoot = math.copysign(self.SELECT_OVERSHOOT_PX, dist)

        self._sel_start_ms = time.monotonic() * 1000.0
        self.mode = "select"
        self.velocity = 0.0
        self._start_timer()

    def _ease_out_expo(self, t):
        if t >= 1.0:
            return 1.0
        return 1.0 - math.pow(2.0, -10.0 * t)

    def _step(self, dt):
        """Advance scroll physics by dt seconds. Return True to keep ticking."""
        adj = self.get_adj()
        if not adj:
            self.mode = "idle"
            return False

        max_s = self._max_scroll(adj)

        if self.mode == "select":
            elapsed = (time.monotonic() * 1000.0) - self._sel_start_ms
            t = min(1.0, elapsed / self._sel_duration_ms)
            eased = self._ease_out_expo(t)
            # Overshoot envelope: peak near t≈0.72 then settle to target
            if self._sel_overshoot and t < 1.0:
                # smooth bump: 4t(1-t) scaled, peaks at t=0.5 — shift later with ease
                bump = math.sin(min(1.0, t / 0.85) * math.pi) * self._sel_overshoot
                value = self._sel_start + (self._sel_target - self._sel_start) * eased + bump * (1.0 - eased * 0.15)
            else:
                value = self._sel_start + (self._sel_target - self._sel_start) * eased
            self.position = value
            self._apply_position()
            if t >= 1.0:
                self.position = self._sel_target
                self._apply_position()
                # If somehow outside, spring; else idle
                if self.position < -0.5 or self.position > max_s + 0.5:
                    self.mode = "spring"
                    return True
                self.mode = "idle"
                return False
            return True

        if self.mode == "fling":
            # Asymptotic coast
            self.velocity *= math.exp(-self.FLING_DECEL * dt)
            self.position += self.velocity * dt

            # Soft rubber while flinging past ends (don't hard-clamp)
            if self.position < 0.0 or self.position > max_s:
                # Extra deceleration outside bounds (rubber resistance on velocity)
                self.velocity *= math.exp(-self.FLING_DECEL * 1.8 * dt)
            self._apply_position()

            out = self.position < 0.0 or self.position > max_s
            if abs(self.velocity) < self.FLING_STOP_VEL:
                if out:
                    self.velocity = 0.0
                    self.mode = "spring"
                    return True
                self.position = max(0.0, min(max_s, self.position))
                self._apply_position()
                self.mode = "idle"
                self.velocity = 0.0
                return False
            return True

        if self.mode == "spring":
            # Critically-damped spring toward nearest bound
            target = 0.0 if self.position < 0.0 else max_s
            if 0.0 <= self.position <= max_s and abs(self._visual_offset) < 0.5:
                self.position = max(0.0, min(max_s, self.position))
                self._apply_position()
                self.mode = "idle"
                self.velocity = 0.0
                return False

            # If somehow inside with leftover visual, clear
            if 0.0 <= self.position <= max_s:
                target = self.position

            omega = self.SPRING_OMEGA
            zeta = self.SPRING_ZETA
            disp = self.position - target
            # a = -ω²x - 2ζωv
            accel = (-omega * omega * disp) - (2.0 * zeta * omega * self.velocity)
            self.velocity += accel * dt
            self.position += self.velocity * dt
            self._apply_position()

            if abs(disp) < 0.4 and abs(self.velocity) < 12.0:
                self.position = target
                self.velocity = 0.0
                self._apply_position()
                self.mode = "idle"
                return False
            return True

        self.mode = "idle"
        return False

def rounded_rect_bands(x, y, w, h, radius):
    """Horizontal bands that follow a rounded rectangle, in surface pixels."""
    if w < 1 or h < 1:
        return []
    radius = max(0.0, min(float(radius), w / 2.0, h / 2.0))
    left = int(math.floor(x))
    top = int(math.floor(y))
    right = int(math.ceil(x + w))
    bottom = int(math.ceil(y + h))
    if radius < 1.0:
        width = right - left
        height = bottom - top
        return [(left, top, width, height)] if width > 0 and height > 0 else []

    bands = []
    for row in range(top, bottom):
        cy = row + 0.5
        inset = 0.0
        if cy < y + radius:
            dy = (y + radius) - cy
            if dy >= radius:
                continue
            inset = radius - math.sqrt(max(0.0, radius * radius - dy * dy))
        elif cy > y + h - radius:
            dy = cy - (y + h - radius)
            if dy >= radius:
                continue
            inset = radius - math.sqrt(max(0.0, radius * radius - dy * dy))
        band_left = int(math.floor(x + inset))
        band_right = int(math.ceil(x + w - inset))
        if band_right > band_left:
            bands.append((band_left, row, band_right - band_left, 1))

    merged = []
    for band in bands:
        if (
            merged
            and merged[-1][0] == band[0]
            and merged[-1][2] == band[2]
            and merged[-1][1] + merged[-1][3] == band[1]
        ):
            px, py, pw, ph = merged[-1]
            merged[-1] = (px, py, pw, ph + 1)
        else:
            merged.append(band)
    return merged


class _WlMessage(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("signature", ctypes.c_char_p),
        ("types", ctypes.c_void_p),
    ]


class _WlInterface(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("version", ctypes.c_int),
        ("method_count", ctypes.c_int),
        ("methods", ctypes.POINTER(_WlMessage)),
        ("event_count", ctypes.c_int),
        ("events", ctypes.POINTER(_WlMessage)),
    ]


def _wl_interface(name, version, methods, events):
    method_arr = (_WlMessage * len(methods))(*(_WlMessage(*item) for item in methods))
    if events:
        event_arr = (_WlMessage * len(events))(*(_WlMessage(*item) for item in events))
        event_count = len(events)
    else:
        event_arr = (_WlMessage * 1)()
        event_count = 0
    interface = _WlInterface(name, version, len(methods), method_arr, event_count, event_arr)
    interface._keep = (method_arr, event_arr)
    return interface


class BackgroundBlur:
    """Ask the compositor to blur behind the launcher's rounded shapes.

    Niri applies ext-background-effect regions as real backdrop blur. The
    launcher surface is fullscreen, so the region is only the pill, the round
    buttons, the results panel, and the context menu — not the whole screen.
    """

    _WL_MARSHAL_FLAG_DESTROY = 1

    def __init__(self):
        self.ready = False
        self.disabled = False
        self._wl = None
        self._gtk = None
        self._display = None
        self._compositor = None
        self._manager = None
        self._effect = None
        self._attached_surface = None
        self._keep = []
        self._logged = False

    def _log(self, message):
        if not self._logged:
            print(f"[TracePoint] {message}", flush=True)
            self._logged = True

    def _gobject_pointer(self, obj):
        capsule = obj.__gpointer__
        python_api = ctypes.PyDLL(None)
        python_api.PyCapsule_GetPointer.restype = ctypes.c_void_p
        python_api.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
        return python_api.PyCapsule_GetPointer(capsule, None)

    def _ptr(self, value):
        if not value:
            return None
        return ctypes.c_void_p(value)

    def setup(self, gdk_display):
        if self.ready or self.disabled:
            return self.ready
        try:
            wl = ctypes.CDLL("libwayland-client.so.0")
            gtk = ctypes.CDLL("libgtk-4.so.1")
            wl.wl_proxy_marshal_flags.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                ctypes.c_uint32, ctypes.c_uint32,
            ]
            wl.wl_proxy_marshal_flags.restype = ctypes.c_void_p
            wl.wl_proxy_add_listener.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            wl.wl_proxy_add_listener.restype = ctypes.c_int
            wl.wl_display_flush.argtypes = [ctypes.c_void_p]
            wl.wl_display_flush.restype = ctypes.c_int
            wl.wl_display_create_queue.argtypes = [ctypes.c_void_p]
            wl.wl_display_create_queue.restype = ctypes.c_void_p
            wl.wl_proxy_set_queue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            wl.wl_display_roundtrip_queue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            wl.wl_display_roundtrip_queue.restype = ctypes.c_int
            wl.wl_display_get_error.argtypes = [ctypes.c_void_p]
            wl.wl_display_get_error.restype = ctypes.c_int
            gtk.gdk_wayland_display_get_wl_display.argtypes = [ctypes.c_void_p]
            gtk.gdk_wayland_display_get_wl_display.restype = ctypes.c_void_p
            gtk.gdk_wayland_surface_get_wl_surface.argtypes = [ctypes.c_void_p]
            gtk.gdk_wayland_surface_get_wl_surface.restype = ctypes.c_void_p

            registry_iface = _wl_interface(
                b"wl_registry", 1,
                [(b"bind", b"usun", None)],
                [(b"global", b"usu", None), (b"global_remove", b"u", None)],
            )
            compositor_iface = _wl_interface(
                b"wl_compositor", 4,
                [(b"create_surface", b"n", None), (b"create_region", b"n", None)],
                [],
            )
            self._region_iface = _wl_interface(
                b"wl_region", 1,
                [(b"destroy", b"", None), (b"add", b"iiii", None), (b"subtract", b"iiii", None)],
                [],
            )
            manager_iface = _wl_interface(
                b"ext_background_effect_manager_v1", 1,
                [(b"destroy", b"", None), (b"get_background_effect", b"no", None)],
                [(b"capabilities", b"u", None)],
            )
            self._effect_iface = _wl_interface(
                b"ext_background_effect_surface_v1", 1,
                [(b"destroy", b"", None), (b"set_blur_region", b"?o", None)],
                [],
            )
            self._keep.extend([
                registry_iface, compositor_iface, self._region_iface,
                manager_iface, self._effect_iface,
            ])

            found = {}

            @ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32)
            def on_global(_data, _registry, name, interface, version):
                if interface:
                    found[interface.decode()] = (int(name), int(version))

            @ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
            def on_remove(_data, _registry, _name):
                return None

            @ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
            def on_capabilities(_data, _manager, _flags):
                return None

            self._keep.extend([on_global, on_remove, on_capabilities])
            registry_listener = (ctypes.c_void_p * 2)(
                ctypes.cast(on_global, ctypes.c_void_p),
                ctypes.cast(on_remove, ctypes.c_void_p),
            )
            capability_listener = (ctypes.c_void_p * 1)(
                ctypes.cast(on_capabilities, ctypes.c_void_p),
            )
            self._keep.extend([registry_listener, capability_listener])

            display = gtk.gdk_wayland_display_get_wl_display(self._gobject_pointer(gdk_display))
            if not display:
                self.disabled = True
                self._log("background blur unavailable (no Wayland display)")
                return False
            queue = wl.wl_display_create_queue(display)
            registry = wl.wl_proxy_marshal_flags(
                display, 1, ctypes.byref(registry_iface), 1, 0, None
            )
            wl.wl_proxy_set_queue(registry, queue)
            wl.wl_proxy_add_listener(registry, registry_listener, None)
            if wl.wl_display_roundtrip_queue(display, queue) < 0 or wl.wl_display_get_error(display):
                self.disabled = True
                self._log("background blur unavailable (registry roundtrip failed)")
                return False
            if "wl_compositor" not in found or "ext_background_effect_manager_v1" not in found:
                self.disabled = True
                self._log("background blur unavailable (compositor has no ext-background-effect)")
                return False

            compositor_name, _compositor_version = found["wl_compositor"]
            manager_name, _manager_version = found["ext_background_effect_manager_v1"]
            compositor = wl.wl_proxy_marshal_flags(
                registry, 0, ctypes.byref(compositor_iface), 4, 0,
                ctypes.c_uint32(compositor_name), b"wl_compositor", ctypes.c_uint32(4), None,
            )
            manager = wl.wl_proxy_marshal_flags(
                registry, 0, ctypes.byref(manager_iface), 1, 0,
                ctypes.c_uint32(manager_name), b"ext_background_effect_manager_v1", ctypes.c_uint32(1), None,
            )
            wl.wl_proxy_set_queue(manager, queue)
            wl.wl_proxy_add_listener(manager, capability_listener, None)
            if wl.wl_display_roundtrip_queue(display, queue) < 0 or wl.wl_display_get_error(display):
                self.disabled = True
                self._log("background blur unavailable (bind failed)")
                return False

            self._wl = wl
            self._gtk = gtk
            self._display = display
            self._compositor = compositor
            self._manager = manager
            self.ready = True
            self._log("background blur ready")
            return True
        except Exception as exc:
            self.disabled = True
            self._log(f"background blur unavailable ({exc})")
            return False

    def attach(self, gdk_surface):
        if not self.ready or self.disabled or gdk_surface is None:
            return False
        try:
            wl_surface = self._gtk.gdk_wayland_surface_get_wl_surface(self._gobject_pointer(gdk_surface))
        except Exception:
            return False
        if not wl_surface:
            return False
        if self._effect and self._attached_surface == wl_surface:
            return True
        effect = self._wl.wl_proxy_marshal_flags(
            self._manager, 1, ctypes.byref(self._effect_iface), 1, 0, None, self._ptr(wl_surface)
        )
        if not effect or self._wl.wl_display_get_error(self._display):
            self.disabled = True
            self._log("background blur disabled after a protocol error")
            return False
        self._effect = effect
        self._attached_surface = wl_surface
        return True

    def set_shapes(self, shapes):
        """shapes: iterable of (x, y, width, height, radius) in surface pixels."""
        if not self.ready or self.disabled or not self._effect:
            return
        bands = []
        for x, y, width, height, radius in shapes:
            bands.extend(rounded_rect_bands(x, y, width, height, radius))
        wl = self._wl
        region = wl.wl_proxy_marshal_flags(
            self._compositor, 1, ctypes.byref(self._region_iface), 1, 0, None
        )
        if not region:
            return
        for x, y, width, height in bands:
            x, y, width, height = int(x), int(y), int(width), int(height)
            if x < 0:
                width += x
                x = 0
            if y < 0:
                height += y
                y = 0
            if width <= 0 or height <= 0:
                continue
            wl.wl_proxy_marshal_flags(
                region, 1, None, 1, 0,
                ctypes.c_int(x), ctypes.c_int(y),
                ctypes.c_int(width), ctypes.c_int(height),
            )
        wl.wl_proxy_marshal_flags(self._effect, 1, None, 1, 0, self._ptr(region))
        wl.wl_proxy_marshal_flags(region, 0, None, 1, self._WL_MARSHAL_FLAG_DESTROY)
        wl.wl_display_flush(self._display)
        if wl.wl_display_get_error(self._display):
            self.disabled = True
            self._log("background blur disabled after a protocol error")

    def clear(self):
        if not self.ready or self.disabled or not self._effect:
            return
        self._wl.wl_proxy_marshal_flags(self._effect, 1, None, 1, 0, None)
        self._wl.wl_display_flush(self._display)


class SmoothScroller:
    """Back-compat wrapper — keyboard paths still call SmoothScroller.animate_to."""

    def __init__(self, spring):
        self._spring = spring

    def stop(self):
        self._spring.stop()

    def animate_to(self, adj, target, duration_ms=240.0):
        self._spring.animate_to(adj, target, duration_ms=duration_ms, bounce=True)

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

        # Unified spring + rubber-band scroller (fling, wheel, keyboard)
        self.spring_scroller = SpringScroller(
            lambda: self.scrolled.get_vadjustment() if hasattr(self, "scrolled") else None,
            lambda: self.list_box if hasattr(self, "list_box") else None,
            lambda: self.win if hasattr(self, "win") else None,
        )
        self.fling_scroller = self.spring_scroller  # compat alias
        self.smooth_scroller = SmoothScroller(self.spring_scroller)

        # Animation states for Spotlight-like open/close (opacity + settle)
        self.anim_state = "closed"
        self.anim_start_us = 0
        self.anim_duration_us = 200_000
        self.anim_open_duration_us = 210_000
        self.anim_close_duration_us = 150_000
        self.start_opacity = 0.0
        self.target_opacity = 1.0
        self.start_margin = 148
        self.target_margin = 160
        self.margin_rest = 160
        self.margin_hidden = 148
        self.tick_cb_id = None
        self.row_anim_generation = 0
        self._buttons_reveal_timer = None
        self._ctx_target = None
        self._ctx_plan = None
        self._ctx_anchor = (0, 0)
        self._blur_tick_id = None
        self._blur_signature = None
        self.blur = BackgroundBlur()
        self.RESULTS_RADIUS = 28
        self.MENU_RADIUS = 16

        self.setup_css()
        self.create_window()
        self.apps_dirs_mtimes = {}
        self.load_applications_cache()

    def setup_css(self):
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property("gtk-application-prefer-dark-theme", True)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS_DATA.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

    def create_window(self):
        self.win = Gtk.ApplicationWindow(application=self.app)
        self.win.set_title("TracePoint")

        # Wayland Layer Shell configuration - Fullscreen transparent overlay
        Gtk4LayerShell.init_for_window(self.win)
        Gtk4LayerShell.set_namespace(self.win, "trace_point")
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
        self.pill_box.set_overflow(Gtk.Overflow.HIDDEN)

        # Search Icon
        search_icon = Gtk.Image.new_from_file(os.path.join(ICONS_DIR, "search.svg"))
        search_icon.add_css_class("spotlight-icon")
        self.pill_box.append(search_icon)

        # Search Entry
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("TracePoint Search")
        self.entry.add_css_class("spotlight-entry")
        self.entry.set_hexpand(True)
        self.entry.set_overflow(Gtk.Overflow.HIDDEN)
        self.entry.set_has_frame(False)
        self.entry.connect("changed", self.on_search_changed)
        self.entry.connect("activate", self.on_entry_activate)
        self._entry_repaint_id = None
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
        self.buttons_revealer.set_transition_duration(200)

        self.buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        # 1. Apps
        self.btn_apps = Gtk.Button()
        self.btn_apps.add_css_class("spotlight-circle-btn")
        self.btn_apps.set_overflow(Gtk.Overflow.HIDDEN)
        self.btn_apps.set_child(Gtk.Image.new_from_file(os.path.join(ICONS_DIR, "apps.svg")))
        self.btn_apps.set_tooltip_text("Applications (⌘1)")
        self.btn_apps.connect("clicked", lambda b: self.toggle_filter("apps"))
        self.buttons_box.append(self.btn_apps)

        # 2. Folders
        self.btn_folders = Gtk.Button()
        self.btn_folders.add_css_class("spotlight-circle-btn")
        self.btn_folders.set_overflow(Gtk.Overflow.HIDDEN)
        self.btn_folders.set_child(Gtk.Image.new_from_file(os.path.join(ICONS_DIR, "folder.svg")))
        self.btn_folders.set_tooltip_text("Folders (⌘2)")
        self.btn_folders.connect("clicked", lambda b: self.toggle_filter("folders"))
        self.buttons_box.append(self.btn_folders)

        # 3. Windows / Actions
        self.btn_windows = Gtk.Button()
        self.btn_windows.add_css_class("spotlight-circle-btn")
        self.btn_windows.set_overflow(Gtk.Overflow.HIDDEN)
        self.btn_windows.set_child(Gtk.Image.new_from_file(os.path.join(ICONS_DIR, "windows.svg")))
        self.btn_windows.set_tooltip_text("Actions / Windows (⌘3)")
        self.btn_windows.connect("clicked", lambda b: self.toggle_filter("windows"))
        self.buttons_box.append(self.btn_windows)

        # 4. Documents / Clipboard
        self.btn_docs = Gtk.Button()
        self.btn_docs.add_css_class("spotlight-circle-btn")
        self.btn_docs.set_overflow(Gtk.Overflow.HIDDEN)
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
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.revealer.set_transition_duration(160)
        self.revealer.set_overflow(Gtk.Overflow.HIDDEN)

        self.results_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.results_panel.add_css_class("spotlight-results-panel")
        self.results_panel.set_size_request(750, -1)
        self.results_panel.set_halign(Gtk.Align.CENTER)
        self.results_panel.set_overflow(Gtk.Overflow.HIDDEN)

        # Scrolled window with kinetic & touchpad support
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_overflow(Gtk.Overflow.HIDDEN)
        self.scrolled.set_max_content_height(420)
        self.scrolled.set_propagate_natural_height(True)
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_kinetic_scrolling(True)
        self.scrolled.set_overlay_scrolling(True)
        self.scrolled.set_has_frame(False)

        vadj = self.scrolled.get_vadjustment()
        vadj.set_step_increment(48.0)
        vadj.set_page_increment(240.0)

        self.list_box = Gtk.ListBox()
        self.list_box.add_css_class("spotlight-scroll-content")
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.connect("row-activated", self.on_row_activated)
        self.scrolled.set_child(self.list_box)
        self.results_panel.append(self.scrolled)

        self.revealer.set_child(self.results_panel)
        self.revealer.set_reveal_child(False)
        self.revealer.connect("notify::child-revealed", lambda *_args: self._schedule_surface_repaint())
        self.revealer.connect("notify::reveal-child", lambda *_args: self._schedule_surface_repaint())
        self.center_box.append(self.revealer)

        self.root_box.append(self.center_box)

        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.root_box)
        self.ctx_menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.ctx_menu.add_css_class("spotlight-context-menu")
        self.ctx_menu.set_overflow(Gtk.Overflow.HIDDEN)
        self.ctx_menu.set_halign(Gtk.Align.START)
        self.ctx_menu.set_valign(Gtk.Align.START)
        self.ctx_menu.set_can_target(True)
        self.ctx_menu.set_visible(False)
        self.ctx_delete_btn = Gtk.Button(label="Delete")
        self.ctx_delete_btn.add_css_class("spotlight-context-btn")
        self.ctx_delete_btn.set_can_focus(False)
        self.ctx_delete_btn.connect("clicked", self.on_context_delete)
        self.ctx_menu.append(self.ctx_delete_btn)

        self.ctx_confirm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.ctx_confirm_box.set_visible(False)
        self.ctx_confirm_box.set_size_request(300, -1)
        self.ctx_message = Gtk.Label()
        self.ctx_message.add_css_class("spotlight-context-copy")
        self.ctx_message.set_wrap(True)
        self.ctx_message.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.ctx_message.set_max_width_chars(36)
        self.ctx_message.set_xalign(0)
        self.ctx_message.set_yalign(0)
        self.ctx_confirm_box.append(self.ctx_message)
        self.ctx_action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.ctx_action_row.set_halign(Gtk.Align.END)
        self.ctx_cancel_btn = Gtk.Button(label="Cancel")
        self.ctx_cancel_btn.add_css_class("spotlight-context-cancel")
        self.ctx_cancel_btn.set_can_focus(False)
        self.ctx_cancel_btn.connect("clicked", lambda _button: self.hide_context_menu())
        self.ctx_remove_btn = Gtk.Button(label="Remove")
        self.ctx_remove_btn.add_css_class("spotlight-context-btn")
        self.ctx_remove_btn.set_can_focus(False)
        self.ctx_remove_btn.connect("clicked", self.on_context_confirm)
        self.ctx_action_row.append(self.ctx_cancel_btn)
        self.ctx_action_row.append(self.ctx_remove_btn)
        self.ctx_confirm_box.append(self.ctx_action_row)
        self.ctx_menu.append(self.ctx_confirm_box)
        self.overlay.add_overlay(self.ctx_menu)
        self.win.set_child(self.overlay)

        self.win.connect("map", self._on_window_map)
        self.win.connect("unmap", self._on_window_unmap)

        # Capture phase so Escape closes the bar even while the entry has focus.
        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
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

        GLib.idle_add(self._setup_blur)

    def _setup_blur(self):
        display = Gdk.Display.get_default()
        if display is not None:
            self.blur.setup(display)
        return False

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
            self.hide_context_menu()
            self.hide()
            return

        in_pill = self.is_descendant_of(picked, self.pill_box)
        in_buttons = self.is_descendant_of(picked, self.buttons_revealer)
        in_results = (
            self.revealer.get_child_revealed() and
            self.is_descendant_of(picked, self.results_panel)
        )
        in_menu = self.ctx_menu.get_visible() and self.is_descendant_of(picked, self.ctx_menu)
        button = gesture.get_current_button()

        if button == Gdk.BUTTON_SECONDARY:
            if not (in_pill or in_buttons or in_results or in_menu):
                self.hide_context_menu()
                self.hide()
            elif not in_menu and not in_results:
                self.hide_context_menu()
            return

        if self.ctx_menu.get_visible() and not in_menu:
            self.hide_context_menu()
            if in_pill or in_buttons or in_results:
                return

        if not (in_pill or in_buttons or in_results or in_menu):
            self.hide()

    def hide_context_menu(self):
        self._ctx_target = None
        self._ctx_plan = None
        if hasattr(self, "ctx_menu"):
            self.ctx_delete_btn.set_visible(True)
            self.ctx_confirm_box.set_visible(False)
            self.ctx_menu.set_visible(False)

    def _place_context_menu(self, origin_x, origin_y, menu_w, menu_h):
        view_w = max(menu_w + 16, self.win.get_width())
        view_h = max(menu_h + 16, self.win.get_height())
        menu_x = min(max(8, int(origin_x)), max(8, view_w - menu_w - 8))
        menu_y = int(origin_y)
        if menu_y + menu_h > view_h - 8:
            menu_y = max(8, int(origin_y) - menu_h)
        self.ctx_menu.set_margin_start(menu_x)
        self.ctx_menu.set_margin_top(menu_y)

    def on_result_right_click(self, gesture, n_press, x, y, item):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        path = item.get("desktop_path")
        if not path:
            self.hide_context_menu()
            return
        row = gesture.get_widget()
        if isinstance(row, Gtk.ListBoxRow):
            self.list_box.select_row(row)
        origin_x, origin_y = x, y
        bounds = self._widget_rect(row, self.overlay)
        if bounds is not None:
            origin_x = bounds[0] + x
            origin_y = bounds[1] + y
        self._ctx_anchor = (origin_x, origin_y)
        self._ctx_plan = None
        self.ctx_delete_btn.set_visible(True)
        self.ctx_delete_btn.set_label("Delete")
        self.ctx_confirm_box.set_visible(False)
        self._place_context_menu(origin_x, origin_y, 168, 48)
        self._ctx_target = item
        self.ctx_menu.set_visible(True)
        self._blur_signature = None

    def on_context_delete(self, _button):
        item = self._ctx_target
        if not item:
            return
        path = item.get("desktop_path")
        if not path:
            self.hide_context_menu()
            return
        plan = classify_desktop_entry(path, self.get_application_dirs())
        self._ctx_plan = plan
        self.ctx_message.set_text(removal_prompt(plan, item.get("title")))
        self.ctx_delete_btn.set_visible(False)
        self.ctx_confirm_box.set_visible(True)
        if plan.get("kind") == "unknown":
            self.ctx_remove_btn.set_visible(False)
        else:
            self.ctx_remove_btn.set_visible(True)
            self.ctx_remove_btn.set_label("Delete" if plan.get("kind") == "local" else "Remove")
        origin_x, origin_y = self._ctx_anchor
        self._place_context_menu(origin_x, origin_y, 320, 168)
        self._blur_signature = None

    def on_context_confirm(self, _button):
        plan = self._ctx_plan
        item = self._ctx_target or {}
        self.hide_context_menu()
        if not plan or plan.get("kind") in (None, "unknown"):
            return
        title = item.get("title") or plan.get("package") or "Application"
        if plan.get("kind") == "local":
            if self._delete_local_desktop(plan.get("local_path")):
                desktop_id = os.path.basename(plan["local_path"])
                self.apps = [
                    app for app in self.apps
                    if os.path.basename(app.get("path", "")) != desktop_id
                ]
                self.on_search_changed(self.entry)
            self.entry.grab_focus()
            return
        if plan.get("argv"):
            Gtk4LayerShell.set_keyboard_mode(self.win, Gtk4LayerShell.KeyboardMode.NONE)
            self._start_package_removal(plan, title)
            self.hide()

    def _user_applications_dir(self):
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        if xdg_data_home:
            return os.path.join(xdg_data_home, "applications")
        return os.path.expanduser("~/.local/share/applications")

    def _delete_local_desktop(self, path):
        """Trash a launcher file that lives in the user's own applications directory."""
        if not path or not str(path).endswith(".desktop") or not os.path.lexists(path):
            return False
        home = os.path.realpath(os.path.expanduser("~"))
        parent = os.path.realpath(os.path.dirname(path))
        if parent != home and not parent.startswith(home + os.sep):
            return False
        allowed = False
        for directory in self.get_application_dirs():
            if parent == os.path.realpath(directory):
                allowed = True
                break
        if not allowed:
            return False
        if os.path.islink(path):
            try:
                os.remove(path)
                return True
            except OSError:
                return False
        try:
            Gio.File.new_for_path(path).trash(None)
            if not os.path.exists(path):
                return True
        except Exception:
            pass
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    def _notify(self, summary, body):
        if not shutil.which("notify-send"):
            return
        subprocess.Popen(
            ["notify-send", "-a", "TracePoint", summary, body],
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _start_package_removal(self, plan, title):
        argv = list(plan.get("argv") or [])
        if not argv:
            return

        def worker():
            try:
                proc = subprocess.run(
                    argv,
                    env=_child_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=1200,
                    start_new_session=True,
                )
                code = proc.returncode
                output = proc.stdout or ""
            except Exception as exc:
                code = 1
                output = str(exc)

            def finish(code=code, output=output):
                lowered = output.lower()
                if code == 0:
                    desktop_id = plan.get("desktop_id")
                    if desktop_id:
                        leftover = os.path.join(self._user_applications_dir(), desktop_id)
                        if os.path.abspath(leftover) != os.path.abspath(plan.get("local_path") or ""):
                            self._delete_local_desktop(leftover)
                    self.apps_dirs_mtimes = {}
                    try:
                        self.load_applications_cache()
                    except Exception:
                        pass
                    self._notify("Removed", f"{title} was uninstalled.")
                elif code in (126, 127) or "not authorized" in lowered or "dismissed" in lowered or "authentication" in lowered:
                    self._notify("Removal canceled", f"{title} is still installed.")
                else:
                    lines = [line.strip() for line in output.splitlines() if line.strip()]
                    detail = lines[-1][:180] if lines else "The package manager did not remove it."
                    self._notify(f"Couldn't remove {title}", detail)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _widget_rect(self, widget, target):
        if widget is None or not widget.get_mapped():
            return None
        try:
            result = widget.compute_bounds(target)
        except Exception:
            return None
        rect = None
        if isinstance(result, tuple):
            if not result or result[0] is False:
                return None
            rect = result[1] if len(result) > 1 else None
        else:
            rect = result
        if rect is None:
            return None
        width = rect.get_width()
        height = rect.get_height()
        if width < 2 or height < 2:
            return None
        return (rect.get_x(), rect.get_y(), width, height)

    def _blur_shapes(self):
        shapes = []
        pill = self._widget_rect(self.pill_box, self.win)
        if pill:
            shapes.append((*pill, pill[3] / 2.0))
        if self.buttons_revealer.get_child_revealed():
            for button in (self.btn_apps, self.btn_folders, self.btn_windows, self.btn_docs):
                rect = self._widget_rect(button, self.win)
                if rect:
                    shapes.append((*rect, min(rect[2], rect[3]) / 2.0))
        if self.revealer.get_child_revealed():
            panel = self._widget_rect(self.results_panel, self.win)
            if panel:
                shapes.append((*panel, self.RESULTS_RADIUS))
        if self.ctx_menu.get_visible():
            menu = self._widget_rect(self.ctx_menu, self.win)
            if menu:
                shapes.append((*menu, self.MENU_RADIUS))
        return shapes

    def _update_blur_regions(self):
        surface = self.win.get_surface()
        if surface is None or not self.blur.attach(surface):
            return
        shapes = self._blur_shapes()
        signature = tuple(
            (round(x), round(y), round(w), round(h), round(radius))
            for x, y, w, h, radius in shapes
        )
        if signature == self._blur_signature:
            return
        self._blur_signature = signature
        self.blur.set_shapes(shapes)
        self.win.queue_draw()

    def _on_blur_tick(self, _widget, _frame_clock):
        if not self.win.get_visible():
            self._blur_tick_id = None
            return GLib.SOURCE_REMOVE
        self._update_blur_regions()
        return GLib.SOURCE_CONTINUE

    def _on_window_map(self, _window):
        self._blur_signature = None
        if self._blur_tick_id is None:
            self._blur_tick_id = self.win.add_tick_callback(self._on_blur_tick)

    def _on_window_unmap(self, _window):
        if self._blur_tick_id is not None:
            try:
                self.win.remove_tick_callback(self._blur_tick_id)
            except Exception:
                pass
            self._blur_tick_id = None
        self.blur.clear()
        self._blur_signature = None

    def on_scroll(self, controller, dx, dy):
        if not self.revealer.get_child_revealed() or not self.results_data:
            return Gdk.EVENT_PROPAGATE

        adj = self.scrolled.get_vadjustment()
        max_scroll = max(0.0, adj.get_upper() - adj.get_page_size())
        # Allow deltas even at ends so rubber-band can engage
        if max_scroll <= 0 and abs(self.spring_scroller.position) < 0.5:
            return Gdk.EVENT_PROPAGATE

        step = 48.0
        self.spring_scroller.apply_delta(dy * step)
        return Gdk.EVENT_STOP

    def on_scroll_decelerate(self, controller, vel_x, vel_y):
        if not self.revealer.get_child_revealed() or not self.results_data:
            return
        adj = self.scrolled.get_vadjustment()
        max_scroll = max(0.0, adj.get_upper() - adj.get_page_size())
        if max_scroll <= 0 and abs(self.spring_scroller.position) < 0.5:
            return
        # Kinetic coast through spring physics (rubber + spring-back at ends)
        if abs(vel_y) > 10:
            self.spring_scroller.start_fling(vel_y)
        else:
            # No coast — settle if rubber-banded past an edge
            self.spring_scroller._cancel_edge_settle()
            if (self.spring_scroller.position < -0.5 or
                    self.spring_scroller.position > max_scroll + 0.5):
                self.spring_scroller.mode = "spring"
                self.spring_scroller.velocity = 0.0
                self.spring_scroller._start_timer()

    def get_application_dirs(self):
        dirs = [
            os.path.expanduser("~/.local/share/applications"),
            "/usr/local/share/applications",
            "/usr/share/applications",
            "/var/lib/flatpak/exports/share/applications",
            os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
            "/var/lib/snapd/desktop/applications",
        ]
        xdg_data_dirs = os.environ.get("XDG_DATA_DIRS", "").split(":")
        for d in xdg_data_dirs:
            if d:
                dirs.append(os.path.join(d, "applications"))
                dirs.append(d)
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        if xdg_data_home:
            dirs.append(os.path.join(xdg_data_home, "applications"))

        seen = set()
        unique_dirs = []
        for d in dirs:
            resolved = os.path.realpath(d)
            if resolved not in seen and os.path.isdir(resolved):
                seen.add(resolved)
                unique_dirs.append(resolved)
        return unique_dirs

    def check_and_reload_applications(self):
        dirs = self.get_application_dirs()
        current_mtimes = {}
        changed = False
        for d in dirs:
            try:
                mtime = os.path.getmtime(d)
                current_mtimes[d] = mtime
                if self.apps_dirs_mtimes.get(d) != mtime:
                    changed = True
            except OSError:
                continue

        if changed or set(current_mtimes.keys()) != set(self.apps_dirs_mtimes.keys()):
            self.apps_dirs_mtimes = current_mtimes
            self.load_applications_cache()

    def load_applications_cache(self):
        self.apps = []
        seen = set()
        dirs = self.get_application_dirs()
        for d in dirs:
            try:
                self.apps_dirs_mtimes[d] = os.path.getmtime(d)
            except OSError:
                pass
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
        hidden = False
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
                    elif k == "Hidden" and v.lower() == "true":
                        hidden = True
            if nodisplay or hidden:
                return None
            if name and exec_cmd:
                return {
                    "type": "app",
                    "name": name,
                    "comment": comment,
                    "icon": icon,
                    "exec": exec_cmd,
                    "path": path,
                    "nodisplay": nodisplay
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
                    "action": lambda text=clip: spawn_detached(["wl-copy", text])
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
            self.entry.set_placeholder_text("TracePoint Search")
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

    def _schedule_surface_repaint(self):
        """Redraw the whole overlay after text changes.

        The search field is translucent. GTK only damages the new glyphs, so a
        deleted letter stays in the window buffer and shows up under the pill.
        Damaging the whole surface clears it.
        """
        if self._entry_repaint_id is not None:
            return
        self._entry_repaint_id = GLib.idle_add(self._repaint_surface)

    def _repaint_surface(self):
        self._entry_repaint_id = None
        if not self.win.get_visible():
            return False
        self.entry.queue_draw()
        self.pill_box.queue_draw()
        self.center_box.queue_draw()
        self.win.queue_draw()
        return False

    def on_search_changed(self, entry):
        raw_text = entry.get_text()
        query = raw_text.strip()
        query_lower = query.lower()
        self.search_generation += 1
        gen = self.search_generation
        self._schedule_surface_repaint()

        # Cancel any pending debounce timer & previous search processes
        if self.debounce_timer_id is not None:
            GLib.source_remove(self.debounce_timer_id)
            self.debounce_timer_id = None
        self.cancel_active_searches()

        if not raw_text:
            if self.current_filter == "all":
                self.badge_box.set_visible(False)
                self.dismiss_results()
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
                self.dismiss_results()
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
                    "desktop_path": app.get("path"),
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
            ("Take Screenshot (Area)", "Capture selected region to ~/Pictures", "camera-photo", lambda: spawn_detached("grim -g \"$(slurp)\" ~/Pictures/Screenshot_$(date +%Y%m%d_%H%M%S).png", shell=True)),
            ("Take Screenshot (Screen)", "Capture entire screen to ~/Pictures", "camera-photo", lambda: spawn_detached("grim ~/Pictures/Screenshot_$(date +%Y%m%d_%H%M%S).png", shell=True)),
            ("Lock Screen", "Lock active user session", "system-lock-screen", lambda: spawn_detached(["loginctl", "lock-session"])),
            ("Sleep / Suspend", "Put computer into low power suspend mode", "system-suspend", lambda: spawn_detached(["systemctl", "suspend"])),
            ("Restart", "Reboot the computer", "system-reboot", lambda: spawn_detached(["systemctl", "reboot"])),
            ("Shut Down", "Power off the computer", "system-shutdown", lambda: spawn_detached(["systemctl", "poweroff"])),
            ("Log Out", "Exit current desktop session", "system-log-out", lambda: spawn_detached(["niri", "msg", "action", "quit"])),
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
        spawn_detached(["xdg-open", url])
        self.hide()

    def clear_results(self):
        self.spring_scroller.stop()
        self.list_box.remove_all()
        self.results_data = []
        self.spring_scroller.sync_from_adj()

    def dismiss_results(self):
        """Fade the current results away, then drop the rows.

        Clearing the list first made the revealer slide an empty panel up
        through the search pill.
        """
        self.results_data = []
        self.row_anim_generation += 1
        gen = self.row_anim_generation
        revealed = self.revealer.get_reveal_child() or self.revealer.get_child_revealed()
        self.revealer.set_reveal_child(False)
        if not revealed:
            self.list_box.remove_all()
            self.list_box.set_opacity(1.0)
            return

        def clear_rows(gen=gen):
            if gen != self.row_anim_generation:
                return False
            if self.revealer.get_reveal_child():
                return False
            if self.revealer.get_child_revealed():
                return True
            self.list_box.remove_all()
            self.list_box.set_opacity(1.0)
            return False

        GLib.timeout_add(30, clear_rows)

    def render_results(self, top_results):
        selected_row = self.list_box.get_selected_row()
        prev_idx = selected_row.get_index() if selected_row else 0
        was_revealed = self.revealer.get_reveal_child()

        self.results_data = top_results
        self.hide_context_menu()
        self.row_anim_generation += 1
        row_gen = self.row_anim_generation

        if not top_results:
            self.dismiss_results()
            return

        self.spring_scroller.stop()
        self.list_box.remove_all()
        self.spring_scroller.sync_from_adj()
        rows = []

        for item in top_results:
            row = Gtk.ListBoxRow()
            row.add_css_class("result-row")
            row.set_overflow(Gtk.Overflow.HIDDEN)
            if item.get("desktop_path"):
                right_click = Gtk.GestureClick.new()
                right_click.set_button(Gdk.BUTTON_SECONDARY)
                right_click.connect("pressed", self.on_result_right_click, item)
                row.add_controller(right_click)

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
            rows.append(row)

        self.revealer.set_reveal_child(True)
        target_idx = min(prev_idx, len(top_results) - 1) if top_results else 0
        target_row = self.list_box.get_row_at_index(target_idx)
        if target_row:
            self.list_box.select_row(target_row)

        if was_revealed:
            # Already open: fade the new rows in place. Don't slide the panel.
            self.list_box.set_opacity(0.45)
            for row in rows:
                row.set_opacity(1.0)

            def fade_list(gen=row_gen):
                if gen != self.row_anim_generation:
                    return False
                self.list_box.set_opacity(1.0)
                return False

            GLib.timeout_add(16, fade_list)
        else:
            self.list_box.set_opacity(1.0)
            for i, row in enumerate(rows):
                row.set_opacity(0.0)
                row.add_css_class("row-enter")
                delay = 20 + min(i, 8) * 16

                def reveal_row(r=row, gen=row_gen):
                    if gen != self.row_anim_generation:
                        return False
                    r.remove_css_class("row-enter")
                    r.set_opacity(1.0)
                    return False

                GLib.timeout_add(delay, reveal_row)

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
        spawn_detached(cmd, shell=True)
        self.hide()

    def open_file(self, path):
        spawn_detached(["xdg-open", path])
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

        # Navigation: Down / Next row (Down Arrow or Vim Ctrl+J)
        is_down = keyval == Gdk.KEY_Down or (is_ctrl and keyval in (Gdk.KEY_j, Gdk.KEY_J))
        # Navigation: Up / Previous row (Up Arrow or Vim Ctrl+K)
        is_up = keyval == Gdk.KEY_Up or (is_ctrl and keyval in (Gdk.KEY_k, Gdk.KEY_K))

        if is_down:
            row = self.list_box.get_selected_row()
            if row:
                next_row = self.list_box.get_row_at_index(row.get_index() + 1)
                if next_row:
                    self.list_box.select_row(next_row)
                    self.ensure_row_visible(next_row)
                    return True
            else:
                first_row = self.list_box.get_row_at_index(0)
                if first_row:
                    self.list_box.select_row(first_row)
                    self.ensure_row_visible(first_row)
                    return True
        elif is_up:
            row = self.list_box.get_selected_row()
            if row and row.get_index() > 0:
                prev_row = self.list_box.get_row_at_index(row.get_index() - 1)
                if prev_row:
                    self.list_box.select_row(prev_row)
                    self.ensure_row_visible(prev_row)
                    return True
        elif keyval == Gdk.KEY_Tab:
            # Cycle through filter buttons
            order = ["all", "apps", "folders", "windows", "documents"]
            curr_idx = order.index(self.current_filter) if self.current_filter in order else 0
            next_filter = order[(curr_idx + 1) % len(order)]
            self.toggle_filter(next_filter if next_filter != "all" else self.current_filter)
            return True
        return False

    def ensure_row_visible(self, row):
        """Smoothly scroll so the selected row stays in view."""
        if not row:
            return
        adj = self.scrolled.get_vadjustment()
        row_h = 48.0
        row_y = row.get_index() * row_h
        page = adj.get_page_size()
        current = adj.get_value()
        pad = 10.0
        if row_y + row_h > current + page:
            self.smooth_scroller.animate_to(adj, row_y + row_h - page + pad, duration_ms=240)
        elif row_y < current:
            self.smooth_scroller.animate_to(adj, max(0.0, row_y - pad), duration_ms=240)

    @staticmethod
    def _ease_out_expo(t):
        """Spotlight-like settle: fast start, soft landing."""
        if t >= 1.0:
            return 1.0
        return 1.0 - math.pow(2.0, -10.0 * t)

    @staticmethod
    def _ease_out_cubic(t):
        return 1.0 - math.pow(1.0 - t, 3)

    def _capture_anim_from_current(self):
        """Seed start_* from live widget state so reverse mid-flight stays smooth."""
        try:
            self.start_opacity = float(self.win.get_opacity())
        except Exception:
            self.start_opacity = 0.0
        self.start_margin = int(self.center_box.get_margin_top())

    def start_fade_in(self):
        self._capture_anim_from_current()
        self.target_opacity = 1.0
        self.target_margin = self.margin_rest
        self.anim_duration_us = self.anim_open_duration_us
        self.anim_state = "fade_in"
        self.anim_start_us = 0
        if self.tick_cb_id is None:
            self.tick_cb_id = self.win.add_tick_callback(self.on_anim_tick)

    def start_fade_out(self):
        if not self.win.get_visible():
            return
        self._capture_anim_from_current()
        self.target_opacity = 0.0
        self.target_margin = self.margin_hidden
        self.anim_duration_us = self.anim_close_duration_us
        self.anim_state = "fade_out"
        self.anim_start_us = 0
        if self.tick_cb_id is None:
            self.tick_cb_id = self.win.add_tick_callback(self.on_anim_tick)

    def on_anim_tick(self, widget, frame_clock):
        now_us = frame_clock.get_frame_time()
        if self.anim_start_us == 0:
            self.anim_start_us = now_us

        elapsed = now_us - self.anim_start_us
        progress = min(1.0, elapsed / max(1, self.anim_duration_us))

        if self.anim_state == "fade_in":
            eased = self._ease_out_expo(progress)
            opacity = self.start_opacity + (self.target_opacity - self.start_opacity) * eased
            margin = self.start_margin + (self.target_margin - self.start_margin) * eased
            self.win.set_opacity(max(0.0, min(1.0, opacity)))
            self.center_box.set_margin_top(int(round(margin)))
            # Subtle scale feel via center_box opacity lag (content settles after chrome)
            self.center_box.set_opacity(min(1.0, 0.55 + 0.45 * eased))
            if progress >= 1.0:
                self.win.set_opacity(1.0)
                self.center_box.set_opacity(1.0)
                self.center_box.set_margin_top(self.margin_rest)
                self.anim_state = "open"
                self.tick_cb_id = None
                return GLib.SOURCE_REMOVE

        elif self.anim_state == "fade_out":
            eased = self._ease_out_cubic(progress)
            opacity = self.start_opacity + (self.target_opacity - self.start_opacity) * eased
            margin = self.start_margin + (self.target_margin - self.start_margin) * eased
            self.win.set_opacity(max(0.0, min(1.0, opacity)))
            self.center_box.set_margin_top(int(round(margin)))
            self.center_box.set_opacity(max(0.0, 1.0 - eased * 0.35))
            if progress >= 1.0:
                self.win.set_opacity(0.0)
                self.center_box.set_opacity(1.0)
                self.center_box.set_margin_top(self.margin_rest)
                self.win.set_visible(False)
                self.anim_state = "closed"
                self.tick_cb_id = None
                return GLib.SOURCE_REMOVE

        return GLib.SOURCE_CONTINUE

    def show(self):
        self.check_and_reload_applications()
        self.search_generation += 1
        self.row_anim_generation += 1
        if self.debounce_timer_id is not None:
            GLib.source_remove(self.debounce_timer_id)
            self.debounce_timer_id = None
        if self._buttons_reveal_timer is not None:
            GLib.source_remove(self._buttons_reveal_timer)
            self._buttons_reveal_timer = None
        self.cancel_active_searches()
        self.fling_scroller.stop_fling()
        self.smooth_scroller.stop()
        self.entry.set_text("")
        self.current_filter = "all"
        self.entry.set_placeholder_text("TracePoint Search")
        self.badge_box.set_visible(False)
        for b in (self.btn_apps, self.btn_folders, self.btn_windows, self.btn_docs):
            b.remove_css_class("active")
        self.dismiss_results()

        # Reset scrolled position to top (clear any rubber-band offset)
        vadj = self.scrolled.get_vadjustment()
        vadj.set_value(0.0)
        self.spring_scroller.sync_from_adj()

        # Reset buttons revealer to unrevealed, then reveal for organic slide-out
        self.buttons_revealer.set_reveal_child(False)
        Gtk4LayerShell.set_keyboard_mode(self.win, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
        self.win.set_opacity(0.0)
        self.center_box.set_opacity(0.55)
        self.center_box.set_margin_top(self.margin_hidden)
        self.win.set_visible(True)
        self.win.present()
        self.entry.grab_focus()

        # Start fade in and slide out buttons slightly after chrome appears
        self.start_fade_in()

        def reveal_buttons():
            self._buttons_reveal_timer = None
            self.buttons_revealer.set_reveal_child(True)
            return False

        self._buttons_reveal_timer = GLib.timeout_add(40, reveal_buttons)

    def hide(self):
        if self.anim_state == "fade_out":
            return
        self.hide_context_menu()
        self.search_generation += 1
        self.row_anim_generation += 1
        if self.debounce_timer_id is not None:
            GLib.source_remove(self.debounce_timer_id)
            self.debounce_timer_id = None
        if self._buttons_reveal_timer is not None:
            GLib.source_remove(self._buttons_reveal_timer)
            self._buttons_reveal_timer = None
        self.cancel_active_searches()
        self.fling_scroller.stop_fling()
        self.smooth_scroller.stop()
        # Collapse panels first so close feels like Spotlight packing away
        self.buttons_revealer.set_transition_duration(140)
        self.revealer.set_transition_duration(120)
        self.buttons_revealer.set_reveal_child(False)
        self.dismiss_results()
        Gtk4LayerShell.set_keyboard_mode(self.win, Gtk4LayerShell.KeyboardMode.NONE)
        self.start_fade_out()
        # Restore revealer durations for next open
        GLib.timeout_add(180, self._restore_revealer_durations)

    def _restore_revealer_durations(self):
        self.buttons_revealer.set_transition_duration(200)
        self.revealer.set_transition_duration(160)
        return False

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
        application_id="org.tracepoint.daemon",
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
