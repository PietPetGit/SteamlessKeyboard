"""Linux system-tray launcher for SteamlessKeyboard.

Mirrors the structure of the Windows tray (tray.py) but limited to the
features that currently make sense on Linux:

  * Tray icon with menu (pystray on AppIndicator/Xorg)
  * Open the on-screen keyboard from the menu
  * Steam+X chord watcher (passive) — same behavior as adusk_linux.py
  * Ctrl+Alt+K global hotkey (X11 only; silently no-ops on Wayland)
  * "Start at login" toggle (XDG autostart .desktop file)
  * "Pause / Exit when Steam is running" (mutually-exclusive submenu)
  * Settings persisted to settings.json next to the binary

Not yet ported (tracked separately): gamepad mode, auto gamepad mode,
ViGEm/uinput virtual gamepad, exclusive HID grab.
"""

import argparse
import ctypes
import json
import math
import os
import shutil
import signal
import sys
import tempfile
import threading
import time


# --- Resource / path helpers ------------------------------------------------

def _is_frozen():
    return getattr(sys, "frozen", False)


def _bundle_dir():
    """Directory containing read-only bundled resources (data/, icon)."""
    if _is_frozen():
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _exe_dir():
    """Directory we treat as the install location — used for the settings
    file and as the working directory for the autostart entry."""
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _exe_path():
    return os.path.abspath(sys.executable) if _is_frozen() else os.path.abspath(__file__)


# IMPORTANT: ADUSK_DATA must be set BEFORE importing adusk.* — adusk.resources
# captures it into a module-level tuple at import time.
os.environ.setdefault("ADUSK_DATA", os.path.join(_bundle_dir(), "data"))

# Force SDL onto the X11 backend (via XWayland) on Linux. Reasons:
#   - On a Wayland session SDL2 picks its native Wayland backend by
#     default. That backend creates an xdg_toplevel surface which the
#     compositor (KWin under Plasma 6) gives keyboard focus on map — the
#     OSK ends up stealing focus from whichever app the user was typing
#     in, so synthetic keystrokes land in the OSK and disappear.
#   - There is no portable Wayland "don't focus me" hint usable from
#     plain xdg-shell; the proper protocol (wlr_layer_shell_v1) isn't
#     bound by pysdl2. Routing through XWayland lets us reuse the X11
#     WM_HINTS.input=False + _NET_WM_WINDOW_TYPE_DOCK trick that KWin
#     does honor for XWayland clients (see adusk._make_window_no_focus_x11).
# Set before any sdl2 import so SDL_Init picks the right driver.
if sys.platform.startswith("linux"):
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")

# Force pystray's xorg backend. Its auto-detection picks AppIndicator
# whenever libayatana-appindicator is available, but pystray's AppIndicator
# backend points the SNI IconName at an absolute temp-file path which KDE
# Plasma 6 silently refuses to render. The xorg backend uses the legacy
# XEmbed protocol which KDE DOES accept; the icon image rendering has its
# own quirks (alpha is pasted onto an RGB background as solid black, no
# auto-scaling), but at least the icon is visible and right-clickable —
# we work around the alpha and sizing issues in _load_icon_image.


os.environ["PYSTRAY_BACKEND"] = "appindicator"
import pystray  # noqa: E402
from PIL import Image  # noqa: E402

import sdl3w as S  # noqa: E402
import steamcontroller.uinput as sui  # noqa: E402
from steamcontroller import SCButtons  # noqa: E402
from adusk import adusk as adusk_app  # noqa: E402
from adusk import inputsrc as adusk_inputsrc  # noqa: E402
from adusk import screen as adusk_screen  # noqa: E402
from adusk import skins as adusk_skins  # noqa: E402
from adusk import state as adusk_state  # noqa: E402


# --- Constants --------------------------------------------------------------

SETTINGS_FILENAME = "settings.json"
AUTOSTART_DESKTOP_NAME = "SteamlessKeyboard.desktop"
TRAY_TITLE = "SteamlessKeyboard"

DEFAULT_SETTINGS = {
    "start_at_login": True,
    "disable_while_steam_running": True,
    "exit_on_steam_launch": False,
    # Per-controller haptics — gates the OSK's UI click feedback (and rumble) for
    # that controller. Each controller's tray submenu has its own Vibration
    # toggle (no global switch). "sc" = Steam Controller, "switch" = Switch Pro.
    "rumble_enabled_sc": True,
    "rumble_enabled_switch": True,
    # Name of the selected Steam on-screen-keyboard skin (a .css under
    # data/skins/). Unlike the others this is a string, not a bool — see the
    # type-aware coercion in _load_settings. Applied when the OSK next opens.
    "skin": "DefaultTheme",
    # OSK transparency level (tray "Keyboard Skin → Transparent" submenu): one of
    # "off"/"low"/"medium"/"high". Renders the keyboard with no background and
    # translucent keys/text over the desktop, at three global-opacity levels.
    "osk_transparency": "off",
    # OSK window size (tray "Keyboard Skin → Size" submenu): "small" /
    # "medium" (the original 1286x369 size, default) / "full" (fills the
    # primary display's usable bounds edge-to-edge - good for touchscreens
    # like the Steam Deck). Each OSK open builds a fresh Screen(), which
    # picks this up automatically.
    "osk_size": "medium",
    # Steam Controller-only OSK settings (tray "Steam Controller" submenu, shown
    # only while an SC is connected). Left-stick OSK navigation on/off; and the
    # L2/R2 OSK actuation point: "default" (firmware full pull) / "low" / "lower".
    "sc_left_stick_nav": True,
    "sc_osk_trigger_actuation": "default",
    # Right-stick mouse pointer speed: "low" / "medium" (default) / "high".
    "sc_pointer_speed": "medium",
    # Switch Pro Controller submenu (shown only while a Switch Pro / SDL pad
    # is connected): same two settings as the SC, minus trigger actuation.
    "switch_left_stick_nav": True,
    "switch_pointer_speed": "medium",
    # Which controller most recently drove the on-screen keyboard: "sc" (Steam
    # Controller) or "sdl" (a generic SDL pad, e.g. the Switch Pro). Picks which
    # Shift/Enter trigger glyphs the OSK shows. A string, not a bool. Updated
    # live as each controller is used and persisted so the glyphs match the
    # last-used pad on the next open — even after a reboot.
    "last_osk_controller": "sc",
    # When False the Debug submenu is hidden; toggled via the "Debug menu"
    # item in the Startup submenu. Mirrors windows/tray.py.
    "debug_menu_unlocked": False,
    # "Block SteamInput Steam Controller grab" (Debug submenu): opens the
    # physical Steam Controller HID exclusively so Steam can't read it. See
    # [[block-steam-exclusive-hid]].
    "block_sc_hid": False,
}


# --- Settings ---------------------------------------------------------------

def _load_settings():
    path = os.path.join(_exe_dir(), SETTINGS_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        # Coerce each known key to the type of its default: bools stay bool
        # (legacy files stored 0/1), string settings (e.g. "skin") pass through.
        for k, val in data.items():
            if k not in DEFAULT_SETTINGS:
                continue
            merged[k] = bool(val) if isinstance(DEFAULT_SETTINGS[k], bool) else val
        # The two-level "low"(6000)/"lower"(3000) actuation collapsed to a single
        # "low" using the lighter 3000 pull — fold a saved "lower" into "low".
        if merged.get("sc_osk_trigger_actuation") == "lower":
            merged["sc_osk_trigger_actuation"] = "low"
        # The single global "rumble_enabled" split into per-controller toggles —
        # seed both from the old value so a saved preference carries over.
        if "rumble_enabled" in data:
            on = bool(data["rumble_enabled"])
            merged["rumble_enabled_sc"] = on
            merged["rumble_enabled_switch"] = on
        return merged
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def _save_settings(settings):
    path = os.path.join(_exe_dir(), SETTINGS_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        print(f"settings save failed: {e}")


# --- XDG autostart ----------------------------------------------------------

def _autostart_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart")


def _autostart_path():
    return os.path.join(_autostart_dir(), AUTOSTART_DESKTOP_NAME)


def _xdg_icon_path():
    """Persistent path for the app icon. ~/.local/share/icons is on the
    standard freedesktop icon search path, and absolute paths in .desktop
    Icon= fields are honored by KDE/GNOME, so referencing this file from
    autostart entries gives them the real app icon instead of the generic
    application fallback."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "icons", "SteamlessKeyboard.png")


def _install_xdg_icon():
    """Write the bundled app icon to the XDG icon dir if missing. Called
    at startup so the autostart entry's Icon= path resolves on the first
    launch after install.

    Uses the LARGEST embedded .ico frame (typically 256x256) so the desktop
    launcher / autostart icon stays crisp at any size KDE/GNOME renders it.
    `_open_app_icon()` picks a small tray-sized frame which would look blurry
    when scaled up to 48–96px desktop icon cells."""
    path = _xdg_icon_path()
    if os.path.exists(path):
        return path
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ico_path = os.path.join(_bundle_dir(), "data", "images", "app_icon.ico")
        if os.path.exists(ico_path):
            img = Image.open(ico_path)
            sizes = sorted(img.info.get("sizes", set()))
            if sizes:
                img.size = max(sizes)  # largest by width
                img.load()
            if img.mode != "RGBA":
                img = img.convert("RGBA")
        else:
            img = _open_app_icon()
            if img is None:
                return None
        img.save(path, "PNG")
        return path
    except Exception as e:
        print(f"xdg icon install failed: {e}")
        return None


def _apply_autostart(enabled):
    """Write or remove ~/.config/autostart/SteamlessKeyboard.desktop. The
    Exec line points at the frozen binary when bundled, or at `python
    tray_linux.py` when running from source — same convention as tray.py
    on Windows."""
    path = _autostart_path()
    if not enabled:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"autostart remove failed: {e}")
        return

    if _is_frozen():
        exec_line = _exe_path()
    else:
        exec_line = f"{sys.executable} {_exe_path()}"

    icon_path = _install_xdg_icon()
    icon_line = f"Icon={icon_path}\n" if icon_path else ""

    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={TRAY_TITLE}\n"
        f"Exec={exec_line}\n"
        f"{icon_line}"
        "X-GNOME-Autostart-enabled=true\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )
    try:
        os.makedirs(_autostart_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(contents)
        os.chmod(path, 0o644)
    except OSError as e:
        print(f"autostart write failed: {e}")


# --- Steam-running detection ------------------------------------------------

def _steam_running():
    """True iff a Steam client process is alive. Scans /proc for processes
    whose comm/cmdline matches the Linux Steam launcher. The official
    package launches via /usr/bin/steam (a shell wrapper) and `steam.sh`,
    and the native client binary is `steamwebhelper`/`steam`. We match the
    common names; the wrapper script normally stays alive as the parent of
    the running session, which is what we actually care about."""
    targets = ("steam", "steam.sh", "steamwebhelper")
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/comm") as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm in targets:
            return True
    return False


# --- Icon -------------------------------------------------------------------

TRAY_ICON_NAME = "SteamlessKeyboard"


def _open_app_icon():
    """Open the bundled app icon. Returns a PIL RGBA Image, or None if the
    icon file can't be loaded."""
    base = os.path.join(_bundle_dir(), "data", "images")
    for candidate in ("app_icon.ico", "glyphs/glyph_keyboard.png"):
        path = os.path.join(base, candidate)
        if not os.path.exists(path):
            continue
        try:
            img = Image.open(path)
        except Exception:
            continue
        # For .ico, pick the closest-to-tray-size frame to avoid the
        # 256x256 default getting downscaled by GTK (which sometimes drops
        # to a blurry blob). PIL's ICO plugin honors `size` setter to load
        # a specific embedded frame.
        if path.endswith(".ico"):
            try:
                sizes = sorted(img.info.get("sizes", set()))
                # 24/22px tray cells — prefer 24 then 32 then anything.
                pick = None
                for target in (24, 32, 22, 48, 16, 64):
                    for s in sizes:
                        if s[0] == target:
                            pick = s
                            break
                    if pick:
                        break
                if pick:
                    img.size = pick
                    img.load()
            except Exception:
                pass
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        return img
    return None


def _load_icon_image():
    """PIL image handed to pystray.Icon(). Required by the constructor even
    on the AppIndicator backend, which actually renders the icon via
    set_icon_full() against our temp theme path."""
    img = _open_app_icon()
    if img is None:
        # Last-ditch placeholder so pystray doesn't choke.
        return Image.new("RGB", (24, 24), (60, 90, 160))
    return img


def _install_tray_icon_theme():
    """Write the bundled app icon as PNG into a stable per-user temp dir
    and return (theme_dir, icon_name). The directory is then passed to
    AppIndicator via set_icon_theme_path(); KDE Plasma 6 will resolve the
    bare icon name against it.

    Layout: theme_dir/SteamlessKeyboard.png at the top level — flat layout
    is the simplest form GTK's icon-theme loader accepts as a search root,
    and it avoids us having to write index.theme/subdir indices."""
    theme_dir = os.path.join(
        tempfile.gettempdir(), f"SteamlessKeyboard-tray-{os.getuid()}")
    try:
        os.makedirs(theme_dir, exist_ok=True)
    except OSError as e:
        print(f"tray: theme dir create failed: {e}")
        return None, None

    icon_path = os.path.join(theme_dir, f"{TRAY_ICON_NAME}.png")
    img = _open_app_icon()
    if img is None:
        return None, None
    try:
        img.save(icon_path, "PNG")
    except Exception as e:
        print(f"tray: icon save failed: {e}")
        return None, None
    return theme_dir, TRAY_ICON_NAME


# --- X11 focused-window helpers --------------------------------------------
#
# Used by the Steam+B "force-kill foreground game" chord. Resolves the
# active window's owning process via _NET_ACTIVE_WINDOW + _NET_WM_PID on
# the X11 root. Works for any XWayland or native-X11 client; native
# Wayland-only apps don't have an X11 window so this gracefully returns
# None for them (KWin's killWindow D-Bus would be the Wayland-native
# path; not bothering until a user reports it's needed).

_libx11_cache = None


def _libx11():
    global _libx11_cache
    if _libx11_cache is not None:
        return _libx11_cache
    try:
        lib = ctypes.cdll.LoadLibrary("libX11.so.6")
    except OSError:
        return None
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p,            # display
        ctypes.c_ulong,             # window
        ctypes.c_ulong,             # property atom
        ctypes.c_long,              # long_offset
        ctypes.c_long,              # long_length
        ctypes.c_int,               # delete
        ctypes.c_ulong,             # req_type
        ctypes.POINTER(ctypes.c_ulong),  # actual_type_return
        ctypes.POINTER(ctypes.c_int),    # actual_format_return
        ctypes.POINTER(ctypes.c_ulong),  # nitems_return
        ctypes.POINTER(ctypes.c_ulong),  # bytes_after_return
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),  # prop_return
    ]
    lib.XGetWindowProperty.restype = ctypes.c_int
    lib.XFree.argtypes = [ctypes.c_void_p]
    _libx11_cache = lib
    return lib


def _x11_get_prop(lib, display, window, prop, expected_type, expected_format):
    """Read a single X11 property as a list of ints. Returns [] on any
    failure / mismatch."""
    actual_type = ctypes.c_ulong(0)
    actual_format = ctypes.c_int(0)
    nitems = ctypes.c_ulong(0)
    bytes_after = ctypes.c_ulong(0)
    prop_ret = ctypes.POINTER(ctypes.c_ubyte)()
    rc = lib.XGetWindowProperty(
        display, window, prop, 0, 1024, 0, expected_type,
        ctypes.byref(actual_type), ctypes.byref(actual_format),
        ctypes.byref(nitems), ctypes.byref(bytes_after),
        ctypes.byref(prop_ret),
    )
    if rc != 0 or not prop_ret:
        return []
    try:
        if actual_format.value != expected_format or actual_type.value != expected_type:
            return []
        if expected_format == 32:
            arr = ctypes.cast(prop_ret,
                              ctypes.POINTER(ctypes.c_ulong * nitems.value))
            return list(arr.contents)
        # 8/16-bit not used here
        return []
    finally:
        lib.XFree(prop_ret)


def _get_focused_window_pid():
    """PID of the process owning the currently-focused X11/XWayland
    window, or None. The Steam+B chord uses this to kill the foreground
    game on Linux."""
    lib = _libx11()
    if lib is None:
        return None
    display = lib.XOpenDisplay(None)
    if not display:
        return None
    try:
        root = lib.XDefaultRootWindow(display)
        atom_active = lib.XInternAtom(display, b"_NET_ACTIVE_WINDOW", 0)
        atom_pid = lib.XInternAtom(display, b"_NET_WM_PID", 0)
        XA_WINDOW = 33
        XA_CARDINAL = 6
        active = _x11_get_prop(lib, display, root, atom_active, XA_WINDOW, 32)
        if not active or not active[0]:
            return None
        win = active[0]
        pid_vals = _x11_get_prop(lib, display, win, atom_pid, XA_CARDINAL, 32)
        if not pid_vals:
            return None
        return int(pid_vals[0])
    finally:
        lib.XCloseDisplay(display)


def _kill_focused_window_process():
    """Steam+B: terminate the focused window's process. SIGTERM first
    (lets the app save / clean up); if it's still around 800 ms later,
    SIGKILL. Returns the pid we acted on (or None on failure)."""
    pid = _get_focused_window_pid()
    if pid is None or pid == os.getpid():
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except PermissionError:
        print(f"Steam+B: no permission to kill pid {pid}")
        return None

    def _followup():
        time.sleep(0.8)
        try:
            os.kill(pid, 0)  # alive?
        except ProcessLookupError:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    threading.Thread(target=_followup, daemon=True).start()
    return pid


# --- Plasma 6 KWin scripting fallback for Steam+B --------------------------
#
# Native Wayland clients (Konsole, Kate, etc. on Plasma 6) don't show up
# in the X11 _NET_ACTIVE_WINDOW atom, so the libX11 path above silently
# returns None for them. KWin's D-Bus scripting interface exposes the
# Wayland-aware `workspace.activeWindow`. We tried `w.kill()` directly
# from the script — the call returns silently without killing the
# process in Plasma 6.0/6.1 (Window.kill is a C++ slot, not a scriptable
# method on every version). Instead we have the script print the pid to
# journald with a per-call UUID marker, read it back via journalctl,
# and SIGTERM/SIGKILL the pid ourselves.

import subprocess
import uuid

_KWIN_PID_SCRIPT_TEMPLATE = """\
var w = workspace.activeWindow;
if (w !== null && w !== undefined) {{
    print("STEAMLESS-MARKER-{marker}: pid=" + w.pid);
}} else {{
    print("STEAMLESS-MARKER-{marker}: no active window");
}}
"""


def _get_focused_window_pid_via_kwin(timeout=0.6):
    """Plasma 6 path. Returns the focused window's pid, or None."""
    marker = uuid.uuid4().hex
    script_path = os.path.join(
        tempfile.gettempdir(), f"steamless-killwin-{os.getuid()}.js")
    try:
        with open(script_path, "w") as f:
            f.write(_KWIN_PID_SCRIPT_TEMPLATE.format(marker=marker))
    except OSError as e:
        print(f"Steam+B: KWin script write failed: {e}")
        return None

    plugin = "steamlesskeyboard-killwin"
    base = ["qdbus6", "org.kde.KWin", "/Scripting"]
    try:
        subprocess.run(base + ["org.kde.kwin.Scripting.unloadScript", plugin],
                       check=False, capture_output=True, timeout=2)
        subprocess.run(base + ["org.kde.kwin.Scripting.loadScript",
                               script_path, plugin],
                       check=False, capture_output=True, timeout=2)
        subprocess.run(base + ["org.kde.kwin.Scripting.start"],
                       check=False, capture_output=True, timeout=2)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"Steam+B: KWin script invoke failed: {e}")
        return None

    # Poll the user journal for the marker line. KWin's print() output
    # gets buffered through journald — usually <100 ms but allow more on
    # a busy box. We bound the wait so the chord doesn't hang.
    deadline = time.time() + timeout
    pid = None
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ["journalctl", "--user", "-n", "20",
                 "--since", "5 seconds ago", "--no-pager", "-o", "cat"],
                capture_output=True, timeout=1, text=True,
            ).stdout
        except Exception:
            break
        token = f"STEAMLESS-MARKER-{marker}: pid="
        for line in out.splitlines():
            i = line.find(token)
            if i >= 0:
                try:
                    pid = int(line[i + len(token):].split()[0])
                except (ValueError, IndexError):
                    pid = None
                break
        if pid is not None:
            break
        time.sleep(0.05)

    try:
        subprocess.run(base + ["org.kde.kwin.Scripting.unloadScript", plugin],
                       check=False, capture_output=True, timeout=2)
    except Exception:
        pass
    return pid


def _kill_pid_term_then_kill(pid):
    """SIGTERM, then SIGKILL 800 ms later if still alive."""
    if pid is None or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except PermissionError:
        print(f"Steam+B: no permission to kill pid {pid}")
        return False

    def _followup():
        time.sleep(0.8)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    threading.Thread(target=_followup, daemon=True).start()
    return True


def _kill_focused_window():
    """Combined entry point used by Steam+B. Tries the cheap X11 lookup
    first, falls back to the KWin scripting + journald pid round-trip
    for native Wayland windows. Returns a short status string."""
    pid = _kill_focused_window_process()
    if pid is not None:
        return f"x11 pid={pid}"
    pid = _get_focused_window_pid_via_kwin()
    if pid is None:
        return "no focused window found"
    if _kill_pid_term_then_kill(pid):
        return f"kwin pid={pid}"
    return f"kwin pid={pid} (kill failed)"


# --- Shared chord state -----------------------------------------------------

class _ChordState:
    """Held-modifier state for the desktop-mode chord watcher. Outlives
    individual SteamController instances so a mid-hold device rebuild
    doesn't strand Alt/Shift/Super pressed at the OS level.

    Mirrors tray.py's _ChordState on Windows."""

    def __init__(self):
        import steamcontroller.uinput as sui
        self.kb = sui.Keyboard()
        self.mouse = sui.Mouse()
        self.alt_held = False
        self.view_was_pressed = False
        self.shift_held = False
        self.win_held = False

    def release_alt(self):
        if self.alt_held:
            import steamcontroller.uinput as sui
            self.kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self.alt_held = False

    def release_shift(self):
        if self.shift_held:
            import steamcontroller.uinput as sui
            self.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self.shift_held = False

    def release_win(self):
        if self.win_held:
            import steamcontroller.uinput as sui
            self.kb.releaseEvent([sui.Keys.KEY_LEFTMETA])
            self.win_held = False

    def release_all_held(self):
        self.release_alt()
        self.release_shift()
        self.release_win()


class _SdlDesktopController:
    """Turns a non-Steam SDL pad (Switch Pro / Xbox / DualSense / ...) into a
    desktop mouse + keyboard — the SDL-pad equivalent of the Steam Controller's
    firmware lizard mode (which Linux disables), synthesized because those pads
    have no firmware desktop mode. Driven from sdl_gamepad_thread while the OSK
    is closed (mirrors the Windows _SdlDesktopController; Linux has no ViGEm /
    gamepad mode, so there's no game-feed or Home-hold mouse-only variant).

    Mapping: right stick = cursor, left stick = arrow keys, ZR (right trigger) =
    left click, ZL (left trigger) = right click, D-pad = arrow keys, Y = Space,
    L/R bumpers = Page Up / Page Down. (Physical Y opens the OSK, handled in
    sdl_gamepad_thread — so it is NOT a desktop key tap here.) Clicks/keys are
    suppressed while Home/"..." is held so the open-keyboard chord doesn't also
    fire desktop actions."""

    MOUSE_DEADZONE = 6000
    MOUSE_SPEED = 1400.0       # px/sec at full stick deflection
    MOUSE_EXPONENT = 1.6
    ARROW_DEADZONE = 14000
    # Match the Steam Controller's desktop arrow cadence on Linux (the
    # chord_watcher's ARROW_HOLD_DELAY / ARROW_REPEAT) so both controllers
    # scroll at one speed.
    ARROW_HOLD_DELAY = 0.35
    ARROW_REPEAT = 0.05
    _ARROW_KEYS = {
        "UP":    sui.Keys.KEY_UP,
        "DOWN":  sui.Keys.KEY_DOWN,
        "LEFT":  sui.Keys.KEY_LEFT,
        "RIGHT": sui.Keys.KEY_RIGHT,
    }
    _KEY_TAPS = (
        (SCButtons.DPAD_UP,    sui.Keys.KEY_UP),
        (SCButtons.DPAD_DOWN,  sui.Keys.KEY_DOWN),
        (SCButtons.DPAD_LEFT,  sui.Keys.KEY_LEFT),
        (SCButtons.DPAD_RIGHT, sui.Keys.KEY_RIGHT),
        (SCButtons.Y,  sui.Keys.KEY_SPACE),
        # A → Enter, B → Esc — matching the Steam Controller's desktop bindings.
        # SDL maps face buttons by POSITION: the Switch Pro's BOTTOM button
        # (physical "B") is SDL SOUTH = SCButtons.A → Enter, and its RIGHT button
        # (physical "A") is SDL EAST = SCButtons.B → Esc. So the same screen
        # position fires the same key as on the SC (physical B = Enter here).
        (SCButtons.A,  sui.Keys.KEY_ENTER),
        (SCButtons.B,  sui.Keys.KEY_ESC),
        # X (Switch Pro physical Y) opens the OSK, so it's NOT a desktop key tap
        # — otherwise opening would also fire a Backspace.
        # L / R (bumpers) are handled separately in update() as tab-switching
        # (Ctrl+Shift+Tab / Ctrl+Tab), not simple key taps.
    )
    # Triggers (ZR/ZL) as mouse buttons — the digital LT/RT bit engages at the
    # trigger threshold, so a light pull clicks. ZR = primary (left).
    _CLICKS = ((SCButtons.RT, "left"), (SCButtons.LT, "right"))
    # Home(Steam)-button chords, mirroring the Steam Controller's: Home + left
    # stick = volume (up/down, ramps while held) / track (left/right), at the
    # SC's media-chord cadence.
    MEDIA_DEADZONE = 14000
    MEDIA_HOLD_DELAY = 0.5
    MEDIA_VOL_REPEAT = 0.021
    _MEDIA_KEYS = {
        "UP":    sui.Keys.KEY_VOLUMEUP,
        "DOWN":  sui.Keys.KEY_VOLUMEDOWN,
        "LEFT":  sui.Keys.KEY_PREVIOUSSONG,
        "RIGHT": sui.Keys.KEY_NEXTSONG,
    }

    def __init__(self, force_kill=None):
        # uinput Mouse/Keyboard share module-global devices, so these don't
        # create new devices — they drive the same cursor/keyboard as the SC.
        self._mouse = sui.Mouse()
        self._kb = sui.Keyboard()
        # Callable that force-shutdowns the focused window (Home+B), or None.
        self._force_kill = force_kill
        self._last_t = 0.0
        self._acc_x = 0.0
        self._acc_y = 0.0
        self._prev = 0
        self._down = set()     # mouse buttons currently held (for click-drag)
        self._arrow_zone = "NEUTRAL"
        self._arrow_repeat_at = 0.0
        # Home-chord state: L3 edge (play/pause), media-stick zone, VIEW edge +
        # held-Alt (Alt+Tab), B latch (force-shutdown fires once per hold).
        self._l3_prev = False
        self._media_zone = "NEUTRAL"
        self._media_repeat_at = 0.0
        self._start_prev = False
        self._alt_held = False
        self._force_kill_done = False

    def reset(self):
        """Release any held button and clear edge/accumulator state, so a
        handoff (OSK open, Steam-pause, pad unplug) never strands a click down
        or fires a stale edge."""
        for name in list(self._down):
            self._mouse.button(name, False)
        self._down.clear()
        if self._alt_held:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self._alt_held = False
        self._prev = 0
        self._acc_x = self._acc_y = 0.0
        self._last_t = 0.0
        self._arrow_zone = "NEUTRAL"
        self._arrow_repeat_at = 0.0
        self._media_zone = "NEUTRAL"
        self._media_repeat_at = 0.0
        self._l3_prev = False
        self._start_prev = False
        self._force_kill_done = False

    @staticmethod
    def _axis(v, deadzone, exponent):
        if abs(v) <= deadzone:
            return 0.0
        sign = 1.0 if v > 0 else -1.0
        mag = min(1.0, (abs(v) - deadzone) / (32767.0 - deadzone))
        return sign * (mag ** exponent)

    def update(self, sci, now):
        b = sci.buttons
        dt = now - self._last_t if self._last_t else 0.0
        self._last_t = now
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0

        # Right stick -> cursor (stick-up moves up; screen Y grows downward).
        # "Lizard mode Pointer Speed" (Nintendo Switch submenu) scales the speed.
        _spd = self.MOUSE_SPEED * adusk_state.get_switch_mouse_speed()
        self._acc_x += self._axis(sci.rstick_x, self.MOUSE_DEADZONE, self.MOUSE_EXPONENT) * _spd * dt
        self._acc_y += -self._axis(sci.rstick_y, self.MOUSE_DEADZONE, self.MOUSE_EXPONENT) * _spd * dt
        mvx, mvy = int(self._acc_x), int(self._acc_y)
        self._acc_x -= mvx
        self._acc_y -= mvy
        if mvx or mvy:
            self._mouse.move(mvx, mvy)

        steam_held = bool(b & (SCButtons.STEAM | SCButtons.QAM))

        # Home(Steam)-button chords (media / play-pause / Alt+Tab / force-kill),
        # mirroring the Steam Controller. Gates internally on Home and releases
        # Alt when Home is let go.
        self._handle_steam_chords(sci, now, steam_held)

        # Left stick -> arrow keys (one tap on deflection, then auto-repeat).
        self._update_arrow_stick(sci.lstick_x, sci.lstick_y, now, steam_held)

        rising = b & ~self._prev
        falling = ~b & self._prev

        # Mouse clicks: press/release so a held button drag-selects. Never START
        # a click while Home is held (open chord), but always release one down.
        for bit, name in self._CLICKS:
            if (rising & bit) and not steam_held:
                self._mouse.button(name, True)
                self._down.add(name)
            elif (falling & bit) and name in self._down:
                self._mouse.button(name, False)
                self._down.discard(name)

        # Edge-triggered key taps, suppressed while Home is held so chords
        # (Home+X = open keyboard) don't leak desktop keys.
        if not steam_held:
            for bit, key in self._KEY_TAPS:
                if rising & bit:
                    self._kb.pressEvent([key])
                    self._kb.releaseEvent([key])
            # L / R (bumpers) → previous / next browser tab (Ctrl+Shift+Tab /
            # Ctrl+Tab), matching the L1/R1 = switch-tab console convention and
            # the Steam Controller's bumpers.
            if rising & SCButtons.LB:
                self._kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
                self._kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                self._kb.pressEvent([sui.Keys.KEY_TAB])
                self._kb.releaseEvent([sui.Keys.KEY_TAB])
                self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
                self._kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
            if rising & SCButtons.RB:
                self._kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
                self._kb.pressEvent([sui.Keys.KEY_TAB])
                self._kb.releaseEvent([sui.Keys.KEY_TAB])
                self._kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
            # L3 (left stick click) → middle click at the cursor (open a link in a
            # new tab / close a tab), matching the Steam Controller. Home+L3 stays
            # Play/Pause (handled in _handle_steam_chords, gated on Home).
            if rising & SCButtons.L3:
                self._mouse.button("middle", True)
                self._mouse.button("middle", False)

        # Remember this frame's buttons for next frame's rising/falling edges.
        self._prev = b

    def _update_arrow_stick(self, x, y, now, steam_held):
        """Map left-stick deflection to arrow-key taps: one step on entering a
        direction, then auto-repeat after a hold delay. y is +up (SDL Y already
        inverted upstream), so stick-up sends Up."""
        zone = "NEUTRAL"
        if abs(x) > self.ARROW_DEADZONE or abs(y) > self.ARROW_DEADZONE:
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        fire = False
        if zone != self._arrow_zone:
            fire = zone != "NEUTRAL"
            self._arrow_repeat_at = now + self.ARROW_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._arrow_repeat_at:
            fire = True
            self._arrow_repeat_at = now + self.ARROW_REPEAT
        self._arrow_zone = zone

        if fire and not steam_held:
            key = self._ARROW_KEYS.get(zone)
            if key is not None:
                self._kb.pressEvent([key])
                self._kb.releaseEvent([key])

    def _handle_steam_chords(self, sci, now, steam_held):
        """Home(Steam)-button chords, matching the Steam Controller's desktop
        chords: Home+L3 = Play/Pause; Home+left stick = volume (up/down, ramps
        while held) / previous/next track (left/right); Home+VIEW = Alt+Tab (hold
        Home, each VIEW press advances one slot); Home+B = force-shutdown the
        focused window. Edge-triggered so one press/deflection = one action."""
        b = sci.buttons

        # Home + L3 → Play/Pause (rising edge).
        l3 = bool(b & SCButtons.L3)
        if steam_held and l3 and not self._l3_prev:
            self._kb.pressEvent([sui.Keys.KEY_PLAYPAUSE])
            self._kb.releaseEvent([sui.Keys.KEY_PLAYPAUSE])
        self._l3_prev = l3

        # Home + left stick → volume (up/down) / track (left/right).
        x, y = sci.lstick_x, sci.lstick_y
        zone = "NEUTRAL"
        if steam_held and (abs(x) > self.MEDIA_DEADZONE
                           or abs(y) > self.MEDIA_DEADZONE):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"
        fire = False
        if zone != self._media_zone:
            fire = zone != "NEUTRAL"
            self._media_repeat_at = now + self.MEDIA_HOLD_DELAY
        elif zone in ("UP", "DOWN") and now >= self._media_repeat_at:
            fire = True
            self._media_repeat_at = now + self.MEDIA_VOL_REPEAT
        self._media_zone = zone
        if fire:
            key = self._MEDIA_KEYS.get(zone)
            if key is not None:
                self._kb.pressEvent([key])
                self._kb.releaseEvent([key])

        # Home + START ("+") → Alt+Tab. Hold Alt while Home is held so the
        # switcher stays up; each "+" rising edge taps Tab once. Alt drops on
        # Home release. (Uses "+"/START, not "-"/VIEW, per user preference for
        # the Switch Pro.)
        plus = bool(b & SCButtons.START)
        if steam_held and plus and not self._start_prev:
            if not self._alt_held:
                self._kb.pressEvent([sui.Keys.KEY_LEFTALT])
                self._alt_held = True
            self._kb.pressEvent([sui.Keys.KEY_TAB])
            self._kb.releaseEvent([sui.Keys.KEY_TAB])
        self._start_prev = plus
        if not steam_held and self._alt_held:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self._alt_held = False

        # Home + A → force-shutdown the focused window (once per hold). The
        # Switch Pro's "A" button is the RIGHT face button = SDL EAST = SCButtons.B.
        if steam_held and (b & SCButtons.B):
            if not self._force_kill_done:
                self._force_kill_done = True
                if self._force_kill is not None:
                    try:
                        result = self._force_kill()
                        print(f"[forcekill] Home+face -> {result}")
                    except Exception as e:
                        print(f"[forcekill] failed: {e!r}")
        else:
            self._force_kill_done = False


# --- App --------------------------------------------------------------------

# Steam Controller OSK L2/R2 actuation levels → analog trigger threshold
# (0..32767; None = firmware full-pull digital bit only, the default). Lower
# values engage Shift/Enter at a lighter pull. Only applied to the SC, OSK-only.
_SC_ACTUATION_THRESHOLDS = {"default": None, "low": 3000}

# Steam Controller "Pointer Speed" → right-stick mouse speed multiplier (1.0 =
# the tuned default). Scales the OSK right-stick mouse + the SC desktop mouse.
_SC_MOUSE_SPEEDS = {"low": 0.6, "medium": 1.0, "high": 1.6}


class App:
    def __init__(self):
        self.settings = _load_settings()
        # Push the current autostart preference to disk so it matches the
        # saved setting (handles "user moved the binary" cases too).
        _apply_autostart(self.settings["start_at_login"])
        # Publish the per-controller haptics switches to the runtime flags the
        # OSK haptic-click paths read (gated by the active controller's toggle).
        adusk_state.set_rumble_enabled("sc", self.settings["rumble_enabled_sc"])
        adusk_state.set_rumble_enabled("sdl", self.settings["rumble_enabled_switch"])
        # Normalize + publish the selected OSK skin so screen.Screen picks it up
        # the next time the keyboard opens. Fall back to the default if the
        # saved name no longer matches a bundled skin.
        if self.settings.get("skin") not in adusk_skins.available_skins():
            self.settings["skin"] = adusk_skins.DEFAULT_SKIN
        adusk_skins.set_active_skin(self.settings["skin"])
        # Publish the OSK transparency level so screen.Screen renders it.
        adusk_skins.set_transparency(self.settings.get("osk_transparency", "off"))
        # Publish the OSK window size so screen.Screen() builds the window at
        # the right dimensions the next time the keyboard opens.
        adusk_screen.set_osk_size(self.settings.get("osk_size", "medium"))
        # Publish the Steam Controller-only OSK settings (left-stick nav + L2/R2
        # actuation) so controller.py applies them on the input thread.
        adusk_state.set_sc_kbd_stick_nav(self.settings.get("sc_left_stick_nav", True))
        adusk_state.set_sc_osk_trigger_threshold(
            _SC_ACTUATION_THRESHOLDS.get(self.settings.get("sc_osk_trigger_actuation", "default")))
        adusk_state.set_sc_mouse_speed(
            _SC_MOUSE_SPEEDS.get(self.settings.get("sc_pointer_speed", "medium"), 1.0))
        # Same for the Switch Pro Controller (left-stick nav + pointer speed).
        adusk_state.set_switch_kbd_stick_nav(self.settings.get("switch_left_stick_nav", True))
        adusk_state.set_switch_mouse_speed(
            _SC_MOUSE_SPEEDS.get(self.settings.get("switch_pointer_speed", "medium"), 1.0))
        # Seed the OSK's Shift/Enter glyph set from the last-used controller so
        # the right hints (SC L2/R2 vs Switch Pro ZL/ZR) show on the very first
        # open after launch, before any input. Then register a hook so a live
        # controller switch is saved back to disk and survives a reboot.
        saved_ctrl = self.settings.get("last_osk_controller", "sc")
        if saved_ctrl not in ("sc", "sdl"):
            saved_ctrl = "sc"
        adusk_state.init_active_controller(saved_ctrl)
        adusk_state.set_active_controller_persist(self._persist_active_controller)

        self._stop_event = threading.Event()
        # Set when Steam is running AND the user opted into pausing.
        self._steam_active = threading.Event()
        # Set by the menu's "Open" item to ask the main thread to bring up
        # the OSK. The OSK MUST run on the main thread (SDL constraint), so
        # the menu callback only signals — the run loop in main() does the
        # actual work.
        self._open_kbd_event = threading.Event()
        # Set while the OSK is on screen so we don't try to reopen it on top
        # of itself (the second SDL_Init would fail).
        self._kbd_open = False
        # Controller family ("sc"/"sdl"/None) that requested the pending OSK
        # open, so the OSK starts on that controller's glyphs. None = a
        # non-controller open (tray menu / Ctrl+Alt+K) — keep the last-used one.
        self._pending_open_controller = None
        # Reference to the pystray Icon, set in main() after construction.
        # Used by background threads to update tooltips / hide menu items.
        self._icon = None
        # The live SteamController instance (set by chord_watcher_thread while
        # its sc.run() is active), so battery_thread can poll get_battery().
        self._current_sc = None
        # Battery status (see battery_thread). _battery is the last
        # SteamControllerBattery polled from the live controller, or None until
        # one streams a power report. _battery_label is the cached menu text.
        # _low_warned_at is the lowest low-battery band (20/10/5) already
        # toasted this discharge cycle; it resets on charge or recovery above
        # the hysteresis line. _charge_complete_notified latches the "charged"
        # toast. _was_charging tracks charge state across polls so we can toast
        # the discharging→charging edge (the "plugged in" notification).
        self._battery = None
        self._battery_label = None
        # Latched True once a Steam Controller is ever detected this session, so
        # the "Steam Controller" tray menu stays visible the whole session.
        self._sc_ever_connected = False
        # Same latch for a Nintendo Switch Pro / SDL pad — set in
        # sdl_gamepad_thread; gates the "Switch Pro Controller" submenu.
        self._switch_ever_connected = False
        self._low_warned_at = None
        self._charge_complete_notified = False
        self._was_charging = False

        # SDL3 gamepad backend (Xbox/DualSense/Switch Pro/...). The tray owns a
        # persistent SDL_INIT_GAMEPAD (on the main thread, here) so the OSK can
        # borrow it via SDL_InitSubSystem without it being torn down on OSK
        # close. sdl_gamepad_thread drives a desktop mouse/keyboard from the pad
        # and opens the OSK on bare Y; while the OSK is open adusk polls this same
        # source. Stays None if SDL init fails — the Steam Controller path is
        # wholly unaffected.
        # Allow gamepad events when no SDL window is focused — the OSK window is
        # no-focus, and without this SDL freezes the pad to all-zero while it's
        # open (set before SDL_Init in case the hint is read at subsystem init;
        # adusk re-sets it too). This is what makes the pad drive the OSK.
        S.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        # Keep SDL's HIDAPI driver off the Steam Controller. We drive the SC
        # entirely through our own steamcontroller HID backend (never as an SDL
        # gamepad); SDL3 recognizes the Triton PIDs 0x1304/0x1302 and grabs the
        # device on GAMEPAD init, which blocks our exclusive open ("Block
        # SteamInput Steam Controller grab") and can otherwise duplicate input.
        S.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_STEAM", b"0")
        self._sdl_source = None
        try:
            if S.SDL_Init(S.SDL_INIT_GAMEPAD):
                self._sdl_source = adusk_inputsrc.Sdl3GamepadSource()
            else:
                print(f"SDL gamepad init failed: {S.get_error()}")
        except Exception as e:
            print(f"SDL gamepad backend unavailable: {e!r}")
        # Hand the source to adusk so its main loop can poll it on the SDL
        # event-pump thread while the OSK is open (this tray's sdl_gamepad_thread
        # cedes then — SDL only refreshes gamepad state on the event-pump thread,
        # so polling it here once the OSK's loop runs reads stale/blind frames).
        adusk_state.set_sdl_source(self._sdl_source)

    # tray menu state predicates --------------------------------------------

    def is_start_at_login_checked(self, item):
        return self.settings["start_at_login"]

    def is_disable_while_steam_checked(self, item):
        return self.settings["disable_while_steam_running"]

    def is_exit_on_steam_checked(self, item):
        return self.settings["exit_on_steam_launch"]

    def is_sc_rumble_checked(self, item):
        return self.settings["rumble_enabled_sc"]

    def is_switch_rumble_checked(self, item):
        return self.settings["rumble_enabled_switch"]

    def is_debug_unlocked(self, item):
        """Visibility callback for the hidden Debug submenu."""
        return self.settings["debug_menu_unlocked"]

    def is_block_sc_hid_checked(self, item):
        return self.settings["block_sc_hid"]

    def _kbd_menu_label(self, item):
        """Dynamic label for the top menu item: shows the action a click will
        perform given the OSK's current open/closed state. The menu is
        refreshed via icon.update_menu() whenever _kbd_open flips."""
        return "Close keyboard" if self._kbd_open else "Open keyboard"

    # tray menu actions -----------------------------------------------------

    def open_kbd(self, icon, item):
        """Menu handler: bring up the OSK, or close it if it's already open."""
        if self._kbd_open:
            try:
                adusk_state.close()
            except Exception:
                pass
            return
        self._open_kbd_event.set()

    def toggle_start_at_login(self, icon, item):
        new = not self.settings["start_at_login"]
        self.settings["start_at_login"] = new
        _save_settings(self.settings)
        _apply_autostart(new)

    def toggle_disable_while_steam(self, icon, item):
        new = not self.settings["disable_while_steam_running"]
        self.settings["disable_while_steam_running"] = new
        if new:
            # Mutually exclusive with exit-on-steam.
            self.settings["exit_on_steam_launch"] = False
        _save_settings(self.settings)

    def toggle_exit_on_steam(self, icon, item):
        new = not self.settings["exit_on_steam_launch"]
        self.settings["exit_on_steam_launch"] = new
        if new:
            self.settings["disable_while_steam_running"] = False
        _save_settings(self.settings)

    def toggle_sc_rumble(self, icon, item):
        # Steam Controller haptics. Read from settings rather than item.checked:
        # pystray's AppIndicator backend doesn't always populate item.checked.
        new = not self.settings["rumble_enabled_sc"]
        self.settings["rumble_enabled_sc"] = new
        _save_settings(self.settings)
        adusk_state.set_rumble_enabled("sc", new)

    def toggle_switch_rumble(self, icon, item):
        # Nintendo Switch (SDL pad) haptics. Settings-read, as above.
        new = not self.settings["rumble_enabled_switch"]
        self.settings["rumble_enabled_switch"] = new
        _save_settings(self.settings)
        adusk_state.set_rumble_enabled("sdl", new)

    def toggle_debug_menu(self, icon, item):
        new = not self.settings["debug_menu_unlocked"]
        self.settings["debug_menu_unlocked"] = new
        _save_settings(self.settings)

    def toggle_block_sc_hid(self, icon, item):
        new = not self.settings["block_sc_hid"]
        self.settings["block_sc_hid"] = new
        _save_settings(self.settings)
        self._kick_sc()

    def _kick_sc(self):
        """Force the current SteamController loop to exit so chord_thread
        rebuilds it with the new `block_sc_hid` setting (exclusive vs shared
        HID open)."""
        if self._current_sc is not None:
            try:
                self._current_sc.addExit()
            except Exception:
                pass

    # Skin submenu: one radio item per bundled skin. pystray needs a distinct
    # checked-predicate and action per name, so we build small closures. The
    # AppIndicator backend re-reads the checked predicate when the menu opens,
    # so a selection shows up without an explicit menu refresh.
    def is_skin_checked(self, name):
        return lambda item: self.settings.get("skin") == name

    def select_skin(self, name):
        def _select(icon, item):
            self.settings["skin"] = name
            _save_settings(self.settings)
            adusk_skins.set_active_skin(name)
            # If the keyboard is open it re-skins live on its next frame (the
            # render loop polls skins.get_generation); otherwise it just opens
            # with the new skin next time.
        return _select

    def is_transparency_checked(self, level):
        return lambda item: self.settings.get("osk_transparency", "off") == level

    def select_transparency(self, level):
        # OSK transparency level (Keyboard Skin → Transparent submenu). Shares the
        # skin generation counter, so an open keyboard switches live on its next
        # frame; otherwise it applies on the next open.
        def _select(icon, item):
            self.settings["osk_transparency"] = level
            _save_settings(self.settings)
            adusk_skins.set_transparency(level)
        return _select

    # OSK size (Keyboard Skin → Size submenu): "small" / "medium" (default) /
    # "full" (fills the display - good for a Steam Deck). Each OSK open
    # builds a fresh Screen(), so this just needs to be saved/published.
    def is_osk_size_checked(self, name):
        return lambda item: self.settings.get("osk_size", "medium") == name

    def select_osk_size(self, name):
        def _select(icon, item):
            self.settings["osk_size"] = name
            _save_settings(self.settings)
            adusk_screen.set_osk_size(name)
        return _select

    # --- Steam Controller submenu (shown only while an SC is connected) -------
    def is_sc_connected(self, item):
        # Latched: once an SC is ever detected the menu stays for the whole
        # session. The live signal flickers (_current_sc goes None while adusk
        # owns the SC with the OSK open), which made the menu vanish; battery_thread
        # also sets the latch so it's set even if the menu is never opened live.
        if self._current_sc is not None or self._battery is not None:
            self._sc_ever_connected = True
        # Debug menu mode forces every controller submenu visible regardless of
        # connection, so settings can be tweaked without the hardware attached.
        return self._sc_ever_connected or self.settings["debug_menu_unlocked"]

    def is_switch_connected(self, item):
        # Latched like is_sc_connected; set in sdl_gamepad_thread when a pad frame
        # is read. Gates the "Switch Pro Controller" submenu.
        return self._switch_ever_connected or self.settings["debug_menu_unlocked"]

    def is_sc_left_stick_nav_checked(self, item):
        return self.settings.get("sc_left_stick_nav", True)

    def toggle_sc_left_stick_nav(self, icon, item):
        self.settings["sc_left_stick_nav"] = not item.checked
        _save_settings(self.settings)
        adusk_state.set_sc_kbd_stick_nav(self.settings["sc_left_stick_nav"])

    def is_sc_actuation_checked(self, level):
        return lambda item: self.settings.get("sc_osk_trigger_actuation", "default") == level

    def select_sc_actuation(self, level):
        def _select(icon, item):
            self.settings["sc_osk_trigger_actuation"] = level
            _save_settings(self.settings)
            adusk_state.set_sc_osk_trigger_threshold(_SC_ACTUATION_THRESHOLDS.get(level))
        return _select

    def is_sc_pointer_speed_checked(self, level):
        return lambda item: self.settings.get("sc_pointer_speed", "medium") == level

    def select_sc_pointer_speed(self, level):
        def _select(icon, item):
            self.settings["sc_pointer_speed"] = level
            _save_settings(self.settings)
            adusk_state.set_sc_mouse_speed(_SC_MOUSE_SPEEDS.get(level, 1.0))
        return _select

    # --- Switch Pro Controller submenu (same as the SC, no actuation) ----
    def is_switch_left_stick_nav_checked(self, item):
        return self.settings.get("switch_left_stick_nav", True)

    def toggle_switch_left_stick_nav(self, icon, item):
        self.settings["switch_left_stick_nav"] = not self.settings.get(
            "switch_left_stick_nav", True)
        _save_settings(self.settings)
        adusk_state.set_switch_kbd_stick_nav(self.settings["switch_left_stick_nav"])

    def is_switch_pointer_speed_checked(self, level):
        return lambda item: self.settings.get("switch_pointer_speed", "medium") == level

    def select_switch_pointer_speed(self, level):
        def _select(icon, item):
            self.settings["switch_pointer_speed"] = level
            _save_settings(self.settings)
            adusk_state.set_switch_mouse_speed(_SC_MOUSE_SPEEDS.get(level, 1.0))
        return _select

    def _persist_active_controller(self, kind):
        """Save the controller (Steam Controller "sc" / SDL pad "sdl") last used
        on the OSK so its Shift/Enter glyphs persist across restarts. Called by
        adusk.state only when the active controller actually changes (on the
        input thread), so writes are rare. No menu refresh — invisible to the
        tray UI; it only affects which glyphs the keyboard draws."""
        if kind not in ("sc", "sdl") or self.settings.get("last_osk_controller") == kind:
            return
        self.settings["last_osk_controller"] = kind
        _save_settings(self.settings)

    def exit_app(self, icon, item):
        self._stop_event.set()
        # If the OSK is currently on screen, ask it to shut down. The main
        # thread is blocked inside adusk_app.main() until this fires, so
        # without it the process can't observe stop_event and tear down.
        if self._kbd_open:
            try:
                adusk_state.close()
            except Exception:
                pass
        try:
            icon.stop()
        except Exception:
            pass

    # battery status --------------------------------------------------------

    # Discharge bands that trigger a low-battery toast (and a haptic nudge),
    # ascending so `next(b for b in bands if pct <= b)` picks the tightest
    # (most severe) band the pack is under. Each band warns once; dropping to a
    # more-severe (lower) band warns again.
    _LOW_BATT_BANDS = (5, 10, 20, 30)
    # Recovery hysteresis: clear the low-battery latch only once the pack climbs
    # back above this (above the highest band), so a reading hovering at a
    # threshold doesn't re-warn.
    _LOW_BATT_RECOVER = 35
    # How often to poll the live controller's cached battery reading. Short so
    # plug-in / unplug feedback is prompt; the poll itself is a single attribute
    # read and we only touch the UI when the reading actually changes.
    _BATTERY_POLL_SECONDS = 5.0
    # Drop the battery display after the controller has been gone this long, so
    # a USB-C unplug doesn't leave a stale "(charging)" line in the menu. Longer
    # than a normal sc rebuild (brief drop) so that doesn't blink the line.
    _BATTERY_STALE_SECONDS = 8.0

    def is_battery_known(self, item):
        """Visibility callback for the battery menu line — hidden until the
        controller has actually reported a level."""
        return self._battery is not None

    def battery_menu_label(self, item):
        return self._battery_label or "Steam Controller: …"

    def _notify(self, title, message):
        # Spawn notify-send directly instead of pystray's icon.notify().
        # pystray reuses a single notification id (`replaces_id`) for every call,
        # so the desktop notification daemon (Plasma) silently *updates* a single
        # dismissed notification instead of popping a new one — meaning only the
        # first toast in a session would actually show. notify-send with no
        # replaces-id creates a fresh notification each time.
        try:
            subprocess.Popen(
                ["notify-send", "--app-name=SteamlessKeyboard",
                 "--icon=input-gaming", title, message],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _await_battery_pct(self, timeout=8.0):
        """Wait up to `timeout`s for a live battery reading (e.g. just after a
        USB-C connect, before battery_thread's slower poll has it), reading the
        live controller directly. Returns the percent, or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop_event.is_set():
            sc = self._current_sc
            b = sc.get_battery() if sc is not None else None
            if b is not None:
                return b.percent
            self._stop_event.wait(0.5)
        return None

    def _update_battery_ui(self, batt):
        """Refresh the tray tooltip + menu line from a battery reading."""
        pct = batt.percent
        if batt.charge_complete:
            state = f"{pct}% (charged)"
        elif batt.charging:
            state = f"{pct}% (charging)"
        else:
            state = f"{pct}%"
        self._battery_label = f"Steam Controller: {state}"
        icon = self._icon
        if icon is not None:
            try:
                icon.title = f"SteamlessKeyboard — Steam Controller {state}"
            except Exception:
                pass
            try:
                icon.update_menu()
            except Exception:
                pass

    def _battery_notifications(self, batt):
        """Fire battery toasts: charging started, fully charged, and low-battery
        threshold crossings."""
        # Charging started. If the controller is tethered via USB-C the device
        # watcher already toasts "connected … charging" (a USB presence event),
        # so only announce here for the wireless puck dock — docking on the puck
        # causes no USB presence change, so this is the only signal for it.
        # charge_complete has its own toast below; the reverse (off-charger)
        # edge gets the "unplugged" toast.
        charging = batt.charging
        if charging and not self._was_charging and not batt.charge_complete:
            from steamcontroller import present_product_ids, PRODUCT_ID_WIRED
            if PRODUCT_ID_WIRED not in present_product_ids():
                self._notify("Steam Controller charging",
                             f"On the puck — {batt.percent}%.")
        elif not charging and self._was_charging:
            self._notify("Steam Controller unplugged",
                         f"Off the puck — {batt.percent}% on battery.")
        self._was_charging = charging

        if batt.charge_complete:
            if not self._charge_complete_notified:
                self._charge_complete_notified = True
                self._notify("Steam Controller fully charged",
                             "Steam Controller battery is full.")
        else:
            self._charge_complete_notified = False

        pct = batt.percent
        # On the charger (or comfortably recovered) → arm the warning again.
        if batt.charging or pct > self._LOW_BATT_RECOVER:
            self._low_warned_at = None
        if batt.charging:
            return

        band = next((b for b in self._LOW_BATT_BANDS if pct <= b), None)
        if band is None:
            return
        if self._low_warned_at is not None and band >= self._low_warned_at:
            return
        self._low_warned_at = band
        if band <= 5:
            self._notify("Steam Controller battery critical",
                         f"{pct}% left — charge the controller now.")
        elif band <= 10:
            self._notify("Steam Controller battery low",
                         f"{pct}% left — charge soon.")
        else:
            self._notify("Steam Controller battery getting low",
                         f"{pct}% remaining.")
        sc = self._current_sc
        if sc is not None and adusk_state.is_rumble_enabled("sc"):
            try:
                sc.haptic_click()
            except Exception:
                pass

    def battery_thread(self):
        """Poll the live controller's cached battery reading and drive the
        tray tooltip/menu plus low-battery / charged notifications. The reading
        itself is captured for free on the SteamController read loop; this
        thread just samples it on a slow timer (battery changes slowly)."""
        last_key = None
        last_seen = None
        while not self._stop_event.is_set():
            # While the OSK is open the chord watcher releases the controller
            # so adusk can claim it, which makes _current_sc go None and would
            # otherwise stale-clear the cached reading. The controller is still
            # very much alive — just owned by another process — so we keep the
            # last reading on screen and skip the poll until the OSK closes.
            if self._kbd_open:
                self._stop_event.wait(self._BATTERY_POLL_SECONDS)
                continue
            sc = self._current_sc
            batt = sc.get_battery() if sc is not None else None
            # Latch SC-ever-connected so the "Steam Controller" menu stays for
            # the session once detected (even while adusk owns the SC, OSK open).
            if sc is not None or batt is not None:
                self._sc_ever_connected = True
            now = time.monotonic()
            if batt is not None:
                last_seen = now
                self._battery = batt
                # Only touch the UI / re-evaluate notifications when the
                # reading actually changes, so a tight poll doesn't churn the
                # menu or re-toast.
                key = (batt.percent, batt.charging, batt.charge_complete)
                if key != last_key:
                    last_key = key
                    self._update_battery_ui(batt)
                    self._battery_notifications(batt)
            elif self._battery is not None and (
                    sc is not None
                    or last_seen is None
                    or now - last_seen > self._BATTERY_STALE_SECONDS):
                # Drop the now-stale reading. `sc is not None` = the controller
                # link is up but it reported no battery (powered off via Steam+Y
                # or dropped its wireless link while the dongle stays plugged) —
                # clear promptly. Otherwise (sc None: a brief rebuild or a full
                # unplug) wait the grace window so a brief rebuild doesn't blink
                # the line off and back on. Reset the latches so a reconnect is
                # treated as a fresh charge cycle.
                self._battery = None
                self._battery_label = None
                last_key = None
                self._was_charging = False
                self._low_warned_at = None
                self._charge_complete_notified = False
                icon = self._icon
                if icon is not None:
                    try:
                        icon.title = "SteamlessKeyboard"
                    except Exception:
                        pass
                    try:
                        icon.update_menu()
                    except Exception:
                        pass
            self._stop_event.wait(self._BATTERY_POLL_SECONDS)

    # How often to poll USB for the receiver / wired controller appearing.
    _DEVICE_POLL_SECONDS = 3.0

    def device_watch_thread(self):
        """Toast when the wireless receiver (puck, PID 0x1304) or the USB-C
        wired controller (PID 0x1302) is plugged into / unplugged from the PC.
        Independent of the battery poll (which needs a live, paired device):
        this only enumerates HID, so it fires even when nothing is paired."""
        try:
            from steamcontroller import present_product_ids, PRODUCT_ID_WIRED
        except Exception as e:
            print(f"device watcher disabled: {e}")
            return
        wired_was = None
        while not self._stop_event.is_set():
            wired = PRODUCT_ID_WIRED in present_product_ids()
            # First loop just seeds state so we don't toast what's already
            # plugged in at startup.
            if wired_was is not None and wired != wired_was:
                if wired:
                    pct = self._await_battery_pct()
                    extra = f" — {pct}%" if pct is not None else ""
                    self._notify("Steam Controller connected",
                                 f"Plugged in via USB-C — charging{extra}.")
                else:
                    self._notify("Steam Controller disconnected",
                                 "USB-C cable unplugged.")
            wired_was = wired
            self._stop_event.wait(self._DEVICE_POLL_SECONDS)

    # background threads ----------------------------------------------------

    def steam_watch_thread(self):
        """Poll for Steam at 2 Hz. Fires _steam_active so the controller
        watcher can release the device while Steam is up, and triggers
        exit_app if the user picked "Exit when Steam is running"."""
        was_running = False
        while not self._stop_event.is_set():
            running = _steam_running()
            if running and not was_running:
                if self.settings["exit_on_steam_launch"]:
                    self._stop_event.set()
                    if self._icon is not None:
                        try:
                            self._icon.stop()
                        except Exception:
                            pass
                    return
                if self.settings["disable_while_steam_running"]:
                    self._steam_active.set()
            elif not running and was_running:
                self._steam_active.clear()
            was_running = running
            if self._stop_event.wait(0.5):
                return

    def chord_watcher_thread(self):
        """Controller chord watcher (desktop / passive mode). Ports the
        full Windows _Watcher in tray.py minus the gamepad-mode branches
        (no ViGEm equivalent wired up yet). Chord set:

          * Steam+X        → open OSK
          * Steam+VIEW     → Alt+Tab (held Alt + Tab tap per VIEW edge)
          * Steam+L3       → Play/Pause
          * Steam+L-stick  → Volume (up/down, hold-repeats) / Prev-Next
          * Steam+Y        → power off controller
          * Steam+B        → SIGTERM the focused window's process
          * Y alone        → Space
          * R4 / R5        → Page Up / Page Down
          * L4 / L5 (hold) → hold Shift / Super
          * Left stick     → arrow keys (hold-repeats)
          * Right stick    → mouse cursor

        Sleeps while Steam is up or the OSK is open so we don't fight
        Steam / adusk for the HID handle."""
        try:
            from steamcontroller import SteamController, SCButtons, SCStatus
            import steamcontroller.uinput as sui
        except Exception as e:
            print(f"steamcontroller unavailable, chord watcher disabled: {e}")
            return

        STICK_DEADZONE = 14000
        STICK_HOLD_DELAY = 0.5
        STICK_VOL_REPEAT = 0.021
        ARROW_HOLD_DELAY = 0.35
        ARROW_REPEAT = 0.05
        DPAD_HOLD_DELAY = 0.35
        DPAD_REPEAT = 0.05
        MOUSE_DEADZONE = 6000
        MOUSE_SPEED = 1400.0
        # Bigger exponent = longer ramp (more stick travel maps to slow speeds),
        # so precise control needs less surgical thumb precision.
        MOUSE_EXPONENT = 5.0
        # Minimum speed (fraction of full) the instant the stick passes the
        # deadzone, so the first bit of travel moves a usable amount (>1px/frame)
        # for fine control instead of the near-zero the steep exponent gives.
        MOUSE_MIN = 0.05
        # Trackpad position units are int16 (~-32767..32767). Scale to
        # screen pixels; lower = slower cursor. Tuned to match firmware
        # lizard's trackpad-mouse feel (~half-screen per full swipe).
        RPAD_SCALE = 0.0066
        # Inertia after lift: keep moving at the swipe's velocity, decay
        # exponentially. DECAY is in 1/seconds — bigger = stops sooner.
        # MOMENTUM_MIN in px/sec snaps to zero below the threshold.
        RPAD_MOMENTUM_DECAY = 2.5
        RPAD_MOMENTUM_MIN = 30.0
        # Inertia only kicks in if the finger was moving faster than
        # this at lift (px/sec). Slow drags lift cleanly with no glide.
        RPAD_MOMENTUM_TRIGGER = 400.0
        # Velocity is averaged over this many seconds of touch samples so
        # the last frame's near-zero "lift slowdown" doesn't kill the
        # carryover. 50 ms ≈ 3-6 input frames at 120 Hz.
        RPAD_VELOCITY_WINDOW = 0.05

        DPAD_MAP = (
            (SCButtons.DPAD_UP,    sui.Keys.KEY_UP),
            (SCButtons.DPAD_DOWN,  sui.Keys.KEY_DOWN),
            (SCButtons.DPAD_LEFT,  sui.Keys.KEY_LEFT),
            (SCButtons.DPAD_RIGHT, sui.Keys.KEY_RIGHT),
        )
        DPAD_MASK = (SCButtons.DPAD_UP | SCButtons.DPAD_DOWN
                     | SCButtons.DPAD_LEFT | SCButtons.DPAD_RIGHT)

        # Zone→key maps built once here (like DPAD_MAP above) instead of as dict
        # literals rebuilt on every HID frame inside the stick handlers — pure
        # per-frame allocation churn on the hot path.
        MEDIA_KEYS = {
            "UP":    sui.Keys.KEY_VOLUMEUP,
            "DOWN":  sui.Keys.KEY_VOLUMEDOWN,
            "LEFT":  sui.Keys.KEY_PREVIOUSSONG,
            "RIGHT": sui.Keys.KEY_NEXTSONG,
        }
        ARROW_KEYS = {
            "UP":    sui.Keys.KEY_UP,
            "DOWN":  sui.Keys.KEY_DOWN,
            "LEFT":  sui.Keys.KEY_LEFT,
            "RIGHT": sui.Keys.KEY_RIGHT,
        }

        chord = _ChordState()

        class _Watcher:
            def __init__(self, owner):
                self.owner = owner
                self.chord = chord
                # Edge / repeat-timer state.
                self._stick_zone_prev = "NEUTRAL"
                self._stick_repeat_at = 0.0
                self._l3_was_pressed = False
                self._arrow_zone_prev = "NEUTRAL"
                self._arrow_repeat_at = 0.0
                self._mouse_last_t = 0.0
                self._mouse_acc_x = 0.0
                self._mouse_acc_y = 0.0
                self._powered_off = False
                self._force_kill_done = False
                self._y_alone_was_pressed = False
                self._x_open_was_pressed = False
                self._a_was_pressed = False
                self._b_was_pressed = False
                self._r4_was_pressed = False
                self._r5_was_pressed = False
                # L1 / R1 (bumpers) → previous / next browser tab. Rising edge.
                self._lb_was_pressed = False
                self._rb_was_pressed = False
                # L3 (left stick click) alone → middle click at the cursor
                # (Steam+L3 is Play/Pause). Rising edge, tracked every frame.
                self._l3_mid_prev = False
                self._dpad_repeat_at = {}  # btn -> next-fire time
                # Right trackpad → mouse cursor. Position-deltas while
                # the finger is in contact; reset on lift so a finger
                # re-touch doesn't fling. Mirrors firmware lizard's
                # trackpad-mouse mode, which gets disabled the moment we
                # open iface 2 for Triton input on this hardware.
                self._rpad_touched_was = False
                self._rpad_prev_x = 0
                self._rpad_prev_y = 0
                self._rpad_click_was = False
                self._rpad_last_t = 0.0
                self._rpad_vx = 0.0  # carryover velocity in px/sec
                self._rpad_vy = 0.0
                self._rpad_acc_x = 0.0  # fractional pixel accumulator
                self._rpad_acc_y = 0.0
                # Recent touch samples (now, x, y) used to compute a
                # smoothed lift-velocity. Trimmed to the window each
                # frame.
                from collections import deque as _deque
                self._rpad_history = _deque()
                # Triggers as mouse buttons. R2 = left click (primary
                # finger), L2 = right click. Edge-triggered so a hold
                # registers as a held button (drag-friendly).
                self._lt_was_pressed = False
                self._rt_was_pressed = False

            def _handle_media_chords(self, sc, sci, steam_now, now):
                l3_now = bool(sci.buttons & SCButtons.L3)
                if steam_now and l3_now and not self._l3_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_PLAYPAUSE])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_PLAYPAUSE])
                self._l3_was_pressed = l3_now

                x = sci.lstick_x
                y = sci.lstick_y
                zone = "NEUTRAL"
                if steam_now and (abs(x) > STICK_DEADZONE
                                  or abs(y) > STICK_DEADZONE):
                    if abs(y) >= abs(x):
                        zone = "UP" if y > 0 else "DOWN"
                    else:
                        zone = "RIGHT" if x > 0 else "LEFT"
                key = MEDIA_KEYS.get(zone)

                fire = False
                if zone != self._stick_zone_prev:
                    fire = zone != "NEUTRAL"
                    self._stick_repeat_at = now + STICK_HOLD_DELAY
                elif zone in ("UP", "DOWN") and now >= self._stick_repeat_at:
                    fire = True
                    self._stick_repeat_at = now + STICK_VOL_REPEAT
                self._stick_zone_prev = zone

                if fire and key is not None:
                    self.chord.kb.pressEvent([key])
                    self.chord.kb.releaseEvent([key])

            def _handle_arrow_stick(self, sci, steam_now, now):
                x = sci.lstick_x
                y = sci.lstick_y
                zone = "NEUTRAL"
                if not steam_now and (abs(x) > STICK_DEADZONE
                                      or abs(y) > STICK_DEADZONE):
                    if abs(y) >= abs(x):
                        zone = "UP" if y > 0 else "DOWN"
                    else:
                        zone = "RIGHT" if x > 0 else "LEFT"
                key = ARROW_KEYS.get(zone)

                fire = False
                if zone != self._arrow_zone_prev:
                    fire = zone != "NEUTRAL"
                    self._arrow_repeat_at = now + ARROW_HOLD_DELAY
                elif zone != "NEUTRAL" and now >= self._arrow_repeat_at:
                    fire = True
                    self._arrow_repeat_at = now + ARROW_REPEAT
                self._arrow_zone_prev = zone

                if fire and key is not None:
                    self.chord.kb.pressEvent([key])
                    self.chord.kb.releaseEvent([key])

            def _handle_dpad(self, sci, steam_now, now):
                """D-pad → arrow keys with the same tap/hold-repeat feel as
                the left stick. Skipped while Steam is held so chord uses
                of the d-pad stay free for later."""
                if steam_now:
                    # Clear repeat timers so a freshly-released hold
                    # doesn't auto-fire on the next non-Steam frame.
                    self._dpad_repeat_at.clear()
                    return
                for btn, key in DPAD_MAP:
                    held = bool(sci.buttons & btn)
                    next_at = self._dpad_repeat_at.get(btn)
                    if held and next_at is None:
                        # Rising edge: fire immediately, then wait.
                        self.chord.kb.pressEvent([key])
                        self.chord.kb.releaseEvent([key])
                        self._dpad_repeat_at[btn] = now + DPAD_HOLD_DELAY
                    elif held and now >= next_at:
                        self.chord.kb.pressEvent([key])
                        self.chord.kb.releaseEvent([key])
                        self._dpad_repeat_at[btn] = now + DPAD_REPEAT
                    elif not held and next_at is not None:
                        del self._dpad_repeat_at[btn]

            def _handle_trackpad_mouse(self, sci, steam_now, now):
                """Right trackpad → mouse cursor with momentum/inertia.
                While the finger is in contact, move by position deltas.
                On lift, capture the velocity averaged over the last
                ~RPAD_VELOCITY_WINDOW seconds (so the lift's slowdown
                frames don't kill the carryover) and decay it. Right-pad
                click → left mouse button. Skipped while Steam is held."""
                touched = bool(sci.buttons & SCButtons.RPADTOUCH) and not steam_now
                if touched:
                    x, y = sci.rpad_x, sci.rpad_y
                    # Move by per-frame delta for live tracking.
                    if self._rpad_touched_was and self._rpad_last_t:
                        dx = (x - self._rpad_prev_x) * RPAD_SCALE
                        dy = -(y - self._rpad_prev_y) * RPAD_SCALE
                        self._rpad_acc_x += dx
                        self._rpad_acc_y += dy
                        mvx = int(self._rpad_acc_x)
                        mvy = int(self._rpad_acc_y)
                        self._rpad_acc_x -= mvx
                        self._rpad_acc_y -= mvy
                        if mvx or mvy:
                            self.chord.mouse.move(mvx, mvy)
                    # Keep a short rolling history so a touch's lift-
                    # velocity is the average over the last window, not
                    # the (often near-zero) last frame.
                    self._rpad_history.append((now, x, y))
                    cutoff = now - RPAD_VELOCITY_WINDOW
                    while self._rpad_history and self._rpad_history[0][0] < cutoff:
                        self._rpad_history.popleft()
                    if len(self._rpad_history) >= 2:
                        t0, x0, y0 = self._rpad_history[0]
                        t1, x1, y1 = self._rpad_history[-1]
                        span = max(1e-3, t1 - t0)
                        self._rpad_vx = (x1 - x0) * RPAD_SCALE / span
                        self._rpad_vy = -(y1 - y0) * RPAD_SCALE / span
                    self._rpad_prev_x = x
                    self._rpad_prev_y = y
                    self._rpad_last_t = now
                else:
                    # Lift: glide on remembered velocity, exponential decay.
                    speed_sq = (self._rpad_vx * self._rpad_vx
                                + self._rpad_vy * self._rpad_vy)
                    if speed_sq > RPAD_MOMENTUM_MIN * RPAD_MOMENTUM_MIN:
                        dt = (now - self._rpad_last_t) if self._rpad_last_t else 1 / 120
                        # Clamp the step so a stalled callback can't fling.
                        dt = max(1e-3, min(dt, 1 / 30))
                        self._rpad_acc_x += self._rpad_vx * dt
                        self._rpad_acc_y += self._rpad_vy * dt
                        mvx = int(self._rpad_acc_x)
                        mvy = int(self._rpad_acc_y)
                        self._rpad_acc_x -= mvx
                        self._rpad_acc_y -= mvy
                        if mvx or mvy:
                            self.chord.mouse.move(mvx, mvy)
                        decay = math.exp(-RPAD_MOMENTUM_DECAY * dt)
                        self._rpad_vx *= decay
                        self._rpad_vy *= decay
                        self._rpad_last_t = now
                    else:
                        # Below threshold: stop cleanly.
                        self._rpad_vx = 0.0
                        self._rpad_vy = 0.0
                        self._rpad_acc_x = 0.0
                        self._rpad_acc_y = 0.0
                        self._rpad_last_t = 0.0
                    if self._rpad_touched_was:
                        # Just lifted: if the swipe wasn't fast enough,
                        # don't glide — the user wants slow drags to
                        # stop dead.
                        speed = math.hypot(self._rpad_vx, self._rpad_vy)
                        if speed < RPAD_MOMENTUM_TRIGGER:
                            self._rpad_vx = 0.0
                            self._rpad_vy = 0.0
                            self._rpad_acc_x = 0.0
                            self._rpad_acc_y = 0.0
                            self._rpad_last_t = 0.0
                        # Clear the touch history on lift so the next
                        # touch starts a fresh window.
                        self._rpad_history.clear()
                self._rpad_touched_was = touched

                # Right-pad click → left mouse button.
                click_now = bool(sci.buttons & SCButtons.RPAD) and not steam_now
                if click_now != self._rpad_click_was:
                    self.chord.mouse.button("left", click_now)
                self._rpad_click_was = click_now

            def _handle_mouse_stick(self, sci, now):
                dt = now - self._mouse_last_t if self._mouse_last_t else 0.0
                self._mouse_last_t = now
                x = sci.rstick_x
                y = sci.rstick_y
                mag = (x * x + y * y) ** 0.5
                if mag <= MOUSE_DEADZONE:
                    self._mouse_acc_x = 0.0
                    self._mouse_acc_y = 0.0
                    return
                if dt <= 0.0 or dt > 0.1:
                    dt = 1.0 / 60.0
                # RADIAL speed: apply the curve to the stick's DISTANCE from
                # center, then move along its unit direction, so a diagonal push
                # is as fast as a pure horizontal/vertical one. (Per-axis exponent
                # made diagonals much slower, very visible at high exponents.)
                m = min(1.0, (mag - MOUSE_DEADZONE) / (32767.0 - MOUSE_DEADZONE))
                unit = MOUSE_MIN + (1.0 - MOUSE_MIN) * (m ** MOUSE_EXPONENT)
                scaled = unit / mag
                # Screen Y grows downward; stick-up (positive y) → -dy.
                # "Pointer Speed" (tray Steam Controller menu) scales the base
                # px/sec, matching the OSK right-stick mouse.
                _spd = MOUSE_SPEED * adusk_state.get_sc_mouse_speed()
                self._mouse_acc_x += (x * scaled) * _spd * dt
                self._mouse_acc_y += -(y * scaled) * _spd * dt
                mvx = int(self._mouse_acc_x)
                mvy = int(self._mouse_acc_y)
                self._mouse_acc_x -= mvx
                self._mouse_acc_y -= mvy
                if mvx or mvy:
                    self.chord.mouse.move(mvx, mvy)

            def on_input(self, sc, sci):
                if sci.status != SCStatus.INPUT:
                    return
                if (self.owner._stop_event.is_set()
                        or self.owner._steam_active.is_set()
                        or self.owner._kbd_open):
                    # Drop modifiers so they don't stick at the OS level.
                    self.chord.release_all_held()
                    sc.addExit()
                    return

                steam_now = bool(sci.buttons & (SCButtons.STEAM | SCButtons.QAM))  # "..." (QAM) acts like Steam
                x_now = bool(sci.buttons & SCButtons.X)
                y_now = bool(sci.buttons & SCButtons.Y)
                b_now = bool(sci.buttons & SCButtons.B)
                view_now = bool(sci.buttons & SCButtons.VIEW)
                now = time.monotonic()

                # L3 (left stick click) ALONE → middle click at the cursor
                # (Steam+L3 is Play/Pause in the media chords). For web browsing:
                # middle-click a link to open it in a new background tab, or a tab
                # to close it. Edge tracked every frame so releasing Steam while
                # still holding L3 can't spuriously fire a click.
                l3_mid_now = bool(sci.buttons & SCButtons.L3)
                if not steam_now and l3_mid_now and not self._l3_mid_prev:
                    self.chord.mouse.button("middle", True)
                    self.chord.mouse.button("middle", False)
                self._l3_mid_prev = l3_mid_now

                # Steam release → drop Alt-Tab.
                if not steam_now:
                    self.chord.release_alt()

                # X (with or without Steam) → open OSK. Rising-edge so
                # one press = one open. The Steam Controller opened it → start
                # the OSK on its glyphs.
                if x_now and not self._x_open_was_pressed:
                    self.owner._pending_open_controller = "sc"
                    self.owner._open_kbd_event.set()
                    self.chord.release_all_held()
                    sc.addExit()
                self._x_open_was_pressed = x_now

                # Steam+VIEW → Alt+Tab.
                if steam_now and view_now and not self.chord.view_was_pressed:
                    if not self.chord.alt_held:
                        self.chord.kb.pressEvent([sui.Keys.KEY_LEFTALT])
                        self.chord.alt_held = True
                    self.chord.kb.pressEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_TAB])
                self.chord.view_was_pressed = view_now

                # Steam + L-stick / L3 → media transport.
                self._handle_media_chords(sc, sci, steam_now, now)
                # Left stick (no Steam) → arrow keys.
                self._handle_arrow_stick(sci, steam_now, now)
                # Right stick → mouse cursor.
                self._handle_mouse_stick(sci, now)
                # Right trackpad → mouse cursor + left-click on pad press.
                self._handle_trackpad_mouse(sci, steam_now, now)

                # Triggers → mouse buttons. Edge-triggered: a full pull
                # sets the button down, a release lifts it (so dragging
                # works). Skipped during Steam-hold so chord uses are
                # free to repurpose triggers later.
                lt_now = bool(sci.buttons & SCButtons.LT) and not steam_now
                if lt_now != self._lt_was_pressed:
                    self.chord.mouse.button("right", lt_now)
                    # Haptic tick on the press (rising edge) so a click feels
                    # like a button, matching the Windows desktop L2/R2 feedback.
                    # Gated by the global vibration switch.
                    if lt_now and adusk_state.is_rumble_enabled("sc"):
                        sc.haptic_click()
                self._lt_was_pressed = lt_now

                rt_now = bool(sci.buttons & SCButtons.RT) and not steam_now
                if rt_now != self._rt_was_pressed:
                    self.chord.mouse.button("left", rt_now)
                    if rt_now and adusk_state.is_rumble_enabled("sc"):
                        sc.haptic_click()
                self._rt_was_pressed = rt_now

                # Steam+Y → power off controller. Latched so it fires once.
                if steam_now and y_now:
                    if not self._powered_off:
                        self._powered_off = True
                        try:
                            sc.turn_off()
                        except Exception as e:
                            print(f"Steam+Y turn_off failed: {e}")
                else:
                    self._powered_off = False

                # Steam+B → kill focused window's process. Latched.
                # Tries X11 _NET_WM_PID first (XWayland clients incl.
                # Steam games), falls back to KWin scripting for native
                # Wayland apps (KDE apps on Plasma 6).
                if steam_now and b_now:
                    if not self._force_kill_done:
                        self._force_kill_done = True
                        result = _kill_focused_window()
                        print(f"Steam+B kill focused: {result}")
                else:
                    self._force_kill_done = False

                # Bare-button bindings (skipped while Steam is held —
                # the Steam-chord variants above already consumed those
                # frames). Linux disables firmware lizard, so without
                # these the controller emits nothing for A/B/d-pad. Match
                # Steam's default desktop config.

                # A alone → Enter.
                a_now = bool(sci.buttons & SCButtons.A)
                a_alone = a_now and not steam_now
                if a_alone and not self._a_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_ENTER])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_ENTER])
                self._a_was_pressed = a_alone

                # B alone → Escape.
                b_alone = b_now and not steam_now
                if b_alone and not self._b_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_ESC])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_ESC])
                self._b_was_pressed = b_alone

                # D-pad → arrow keys (tap + hold-repeat).
                self._handle_dpad(sci, steam_now, now)

                # Y alone → Space.
                y_alone = y_now and not steam_now
                if y_alone and not self._y_alone_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_SPACE])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_SPACE])
                self._y_alone_was_pressed = y_alone

                # R4 → Page Up, R5 → Page Down.
                r4_now = bool(sci.buttons & SCButtons.RGRIP1) and not steam_now
                if r4_now and not self._r4_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_PAGEUP])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_PAGEUP])
                self._r4_was_pressed = r4_now

                r5_now = bool(sci.buttons & SCButtons.RGRIP2) and not steam_now
                if r5_now and not self._r5_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_PAGEDOWN])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_PAGEDOWN])
                self._r5_was_pressed = r5_now

                # L1 / R1 (bumpers) → previous / next browser tab
                # (Ctrl+Shift+Tab / Ctrl+Tab), matching the console convention.
                lb_now = bool(sci.buttons & SCButtons.LB) and not steam_now
                if lb_now and not self._lb_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                    self.chord.kb.pressEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
                self._lb_was_pressed = lb_now

                rb_now = bool(sci.buttons & SCButtons.RB) and not steam_now
                if rb_now and not self._rb_was_pressed:
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
                    self.chord.kb.pressEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_TAB])
                    self.chord.kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
                self._rb_was_pressed = rb_now

                # L4 → hold Shift, L5 → hold Super. The release branch
                # also runs while Steam is held so transient chords don't
                # strand the modifier.
                l4_hold = (bool(sci.buttons & SCButtons.LGRIP1)
                           and not steam_now)
                if l4_hold and not self.chord.shift_held:
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                    self.chord.shift_held = True
                elif not l4_hold and self.chord.shift_held:
                    self.chord.release_shift()

                l5_hold = (bool(sci.buttons & SCButtons.LGRIP2)
                           and not steam_now)
                if l5_hold and not self.chord.win_held:
                    self.chord.kb.pressEvent([sui.Keys.KEY_LEFTMETA])
                    self.chord.win_held = True
                elif not l5_hold and self.chord.win_held:
                    self.chord.release_win()

        while not self._stop_event.is_set():
            # Release the controller while Steam owns it or the OSK is up.
            if self._steam_active.is_set() or self._kbd_open:
                if self._stop_event.wait(1.0):
                    return
                continue
            watcher = _Watcher(self)
            sc = SteamController(callback=watcher.on_input, passive=True,
                                  exclusive=self.settings["block_sc_hid"])
            self._current_sc = sc
            try:
                sc.run()
            except Exception as e:
                print(f"chord watcher error: {e}")
            finally:
                self._current_sc = None
            # Whatever caused sc.run() to exit, make sure no modifier is
            # stuck pressed (the watcher should have done this on the
            # last frame, but belt-and-suspenders against crashes).
            chord.release_all_held()
            if self._stop_event.is_set():
                return
            if self._stop_event.wait(1.0):
                return

    def hotkey_thread(self):
        """Listen for Ctrl+Alt+K to toggle the OSK. Opens when closed,
        closes when open. X11/XWayland only — pynput's GlobalHotKeys
        silently no-ops on a pure Wayland session, but our SDL window
        runs through XWayland anyway."""
        try:
            from pynput import keyboard as pkb
        except Exception as e:
            print(f"pynput unavailable, hotkey listener disabled: {e}")
            return

        def _on_toggle():
            if self._stop_event.is_set():
                return
            if self._kbd_open:
                try:
                    adusk_state.close()
                except Exception as e:
                    print(f"Ctrl+Alt+K close failed: {e}")
            else:
                self._open_kbd_event.set()

        try:
            listener = pkb.GlobalHotKeys({"<ctrl>+<alt>+k": _on_toggle})
            listener.daemon = True
            listener.start()
        except Exception as e:
            print(f"hotkey listener failed to start: {e}")
            return

        # Esc closes the on-screen keyboard if it's open. Not suppressed, so
        # Esc still reaches whatever window has focus as normal — this just
        # adds the OSK close as a side effect (the OSK never has focus itself).
        def _on_esc_press(key):
            if self._stop_event.is_set():
                return
            if key == pkb.Key.esc and self._kbd_open:
                try:
                    adusk_state.close()
                except Exception as e:
                    print(f"Esc close failed: {e}")

        esc_listener = None
        try:
            esc_listener = pkb.Listener(on_press=_on_esc_press)
            esc_listener.daemon = True
            esc_listener.start()
        except Exception as e:
            print(f"esc listener failed to start: {e}")

        self._stop_event.wait()
        try:
            listener.stop()
        except Exception:
            pass
        if esc_listener is not None:
            try:
                esc_listener.stop()
            except Exception:
                pass

    def sdl_gamepad_thread(self):
        """Poll SDL-recognized pads (Switch Pro / Xbox / DualSense / ...) so a
        non-Steam controller can (a) OPEN the OSK and (b) act as a desktop
        mouse/keyboard — the synthesized equivalent of the Steam Controller's
        firmware lizard mode (which Linux disables). Linux has no ViGEm, so
        unlike Windows there's no game-feed path. The Steam Controller itself is
        excluded by Sdl3GamepadSource (name match), so the two never fight.

        Bare Y on the Switch Pro — positionally SCButtons.X, like the SC's bare-X
        desktop open — opens the keyboard. While the OSK is open this thread
        CEDES: `adusk.main` polls this same source on its own SDL event-pump
        thread and publishes the frames, because SDL only refreshes gamepad state
        on the event-pump thread — polling here too once that loop runs reads
        stale/blind frames (froze the pad to no input, and a frozen deflected
        stick drifted the cursor). Defensive throughout — errors must never take
        down the tray."""
        src = self._sdl_source
        if src is None:
            return
        x_prev = False
        # force_kill = Home+B → kill the focused window's process (the SDL-pad
        # equivalent of the Steam Controller's Steam+B force-shutdown chord).
        desktop = _SdlDesktopController(force_kill=_kill_focused_window)
        was_kbd_open = False
        osk_close_time = 0.0       # monotonic time of last OSK close (debounce)
        OSK_REOPEN_COOLDOWN = 0.4  # ignore Y for this long after the OSK closes
        steam_kill_prev = False    # Home+face edge while Steam-ceded (force-kill)
        while not self._stop_event.is_set():
            # Paused for Steam (disable_while_steam_running + Steam up): let Steam
            # own the pad — don't inject desktop kb/mouse — matching how the
            # Steam Controller path pauses (its _Watcher cedes on _steam_active).
            if self._steam_active.is_set():
                desktop.reset()
                x_prev = False
                # BUT still honor Home+B force-shutdown — its whole purpose is to
                # kill a running (often Steam) game, which is exactly when we're
                # ceded. Nothing else is injected. (Skip while the OSK is open so
                # we don't double-poll the pad against adusk.)
                sci = None
                if not self._kbd_open:
                    try:
                        sci = src.poll()
                    except Exception:
                        sci = None
                kill_now = bool(sci is not None
                                and (sci.buttons & (SCButtons.STEAM | SCButtons.QAM))
                                and (sci.buttons & SCButtons.B))  # Home + Switch A
                if kill_now and not steam_kill_prev:
                    try:
                        print(f"[forcekill] (steam-ceded) Home+face -> "
                              f"{_kill_focused_window()}")
                    except Exception as e:
                        print(f"[forcekill] failed: {e!r}")
                steam_kill_prev = kill_now
                self._stop_event.wait(0.05)
                continue
            steam_kill_prev = False
            # Cede all SDL access while the OSK is open — adusk owns it then.
            if self._kbd_open:
                was_kbd_open = True
                desktop.reset()
                x_prev = True  # treat Y as held so its release doesn't re-open
                self._stop_event.wait(0.05)
                continue
            # The OSK just closed — start a cooldown so buffered Y presses during
            # close don't immediately re-open it, and force a clean rising edge.
            if was_kbd_open:
                was_kbd_open = False
                osk_close_time = time.monotonic()
                x_prev = True
            try:
                sci = src.poll()
            except Exception as e:
                print(f"sdl gamepad poll error: {e!r}")
                sci = None
            if sci is not None:
                # A pad frame means a Switch Pro / SDL pad is connected — latch
                # it so the "Switch Pro Controller" tray submenu appears.
                if not self._switch_ever_connected:
                    self._switch_ever_connected = True
                # Bare Y (SCButtons.X on the Switch Pro's positional map) opens
                # the OSK, matching the Steam Controller's bare-X desktop open.
                # Rising edge so one press = one open; gated by the close cooldown.
                x = bool(sci.buttons & SCButtons.X)
                if (x and not x_prev and not self._kbd_open
                        and (time.monotonic() - osk_close_time) > OSK_REOPEN_COOLDOWN):
                    self._pending_open_controller = "sdl"
                    self._open_kbd_event.set()
                x_prev = x
                # Desktop mouse/keyboard from the SDL pad (right stick = cursor,
                # left stick / D-pad = arrows, ZR/ZL = clicks, Y = Space, bumpers
                # = PageUp/Dn). Physical Y (= SCButtons.X) is the OSK opener and
                # is excluded from the desktop key taps inside update().
                try:
                    desktop.update(sci, time.monotonic())
                except Exception as e:
                    print(f"sdl desktop update failed: {e!r}")
            else:
                x_prev = False
                desktop.reset()
            self._stop_event.wait(0.008)  # ~125 Hz

    def _shutdown_sdl(self):
        """Tear down the persistent SDL gamepad subsystem. Called from main()
        on the main thread after the OSK loop has fully exited, so it never
        races adusk.main()'s own SDL teardown."""
        src = self._sdl_source
        self._sdl_source = None
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        try:
            S.SDL_Quit()
        except Exception:
            pass


def _open_osk_once(app):
    """Reset per-session state and run the OSK on the calling thread (SDL
    constraint: video init + event pump must be the main thread)."""
    app._kbd_open = True
    if app._icon is not None:
        try:
            app._icon.update_menu()
        except Exception:
            pass
    # Start the OSK on the glyphs of the controller that opened it: the Steam
    # Controller (Steam+X) tags "sc", an SDL pad (Switch Guide+X) tags "sdl";
    # a non-controller open (tray menu / Ctrl+Alt+K) leaves it on the last-used
    # controller. set_active_controller persists the choice too.
    opener = app._pending_open_controller
    app._pending_open_controller = None
    if opener is not None:
        adusk_state.set_active_controller(opener)
    try:
        adusk_state.reset_session()
        adusk_app.main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"adusk crashed: {e!r}")
    finally:
        app._kbd_open = False
        if app._icon is not None:
            try:
                app._icon.update_menu()
            except Exception:
                pass


def _build_menu(app):
    # Transparency: a collapsible submenu with Off + three opacity levels
    # (radio). The levels scale the whole transparent look uniformly — Low is
    # 30% more opaque, High 30% more transparent, than the tuned Medium.
    transparent_submenu = pystray.Menu(
        pystray.MenuItem("Off", app.select_transparency("off"),
                         checked=app.is_transparency_checked("off"), radio=True),
        pystray.MenuItem("Low", app.select_transparency("low"),
                         checked=app.is_transparency_checked("low"), radio=True),
        pystray.MenuItem("Medium", app.select_transparency("medium"),
                         checked=app.is_transparency_checked("medium"), radio=True),
        pystray.MenuItem("High", app.select_transparency("high"),
                         checked=app.is_transparency_checked("high"), radio=True),
    )

    # OSK window size: "Small" (less screen blocked), "Default" (the original
    # 1286x369 size), "Full Screen" (fills the display - good for a Steam Deck).
    size_submenu = pystray.Menu(
        pystray.MenuItem("Small", app.select_osk_size("small"),
                         checked=app.is_osk_size_checked("small"), radio=True),
        pystray.MenuItem("Default", app.select_osk_size("medium"),
                         checked=app.is_osk_size_checked("medium"), radio=True),
        pystray.MenuItem("Full Screen", app.select_osk_size("full"),
                         checked=app.is_osk_size_checked("full"), radio=True),
    )

    # Steam on-screen-keyboard skins (radio; applied on the next OSK open).
    # The "Size" and "Transparent" submenus sit at the top, above the skin list.
    skin_submenu = pystray.Menu(
        pystray.MenuItem("Size", size_submenu),
        pystray.MenuItem("Transparent", transparent_submenu),
        pystray.Menu.SEPARATOR,
        *[
            pystray.MenuItem(name, app.select_skin(name),
                             checked=app.is_skin_checked(name), radio=True)
            for name in adusk_skins.available_skins()
        ]
    )

    steam_running_submenu = pystray.Menu(
        pystray.MenuItem(
            "Pause SteamlessKeyboard",
            app.toggle_disable_while_steam,
            checked=app.is_disable_while_steam_checked,
        ),
        pystray.MenuItem(
            "Exit SteamlessKeyboard",
            app.toggle_exit_on_steam,
            checked=app.is_exit_on_steam_checked,
        ),
    )

    # Startup-related settings, grouped under one submenu.
    startup_submenu = pystray.Menu(
        pystray.MenuItem(
            "Start at login",
            app.toggle_start_at_login,
            checked=app.is_start_at_login_checked,
        ),
        pystray.MenuItem("When Steam is running", steam_running_submenu),
        pystray.MenuItem("Advanced Settings", app.toggle_debug_menu,
                         checked=app.is_debug_unlocked),
    )

    # Hidden Debug submenu, unlocked via "Startup → Debug menu". Mirrors
    # windows/tray.py's debug_submenu minus "Block SteamInput Xbox Controller
    # grab" (block_gamepad_takeover) — that toggle hides the virtual ViGEm
    # Xbox 360 pad from Steam, and there's no ViGEm/uinput gamepad on Linux.
    debug_submenu = pystray.Menu(
        pystray.MenuItem(
            "Block SteamInput Steam Controller grab",
            app.toggle_block_sc_hid,
            checked=app.is_block_sc_hid_checked,
        ),
    )

    # Steam Controller settings (shown only while an SC is connected). A toggle
    # for left-stick OSK navigation + a radio submenu for the L2/R2 OSK actuation
    # point. SC-only; the actuation affects OSK Shift/Enter, not lizard/gamepad.
    sc_actuation_submenu = pystray.Menu(
        pystray.MenuItem("Default", app.select_sc_actuation("default"),
                         checked=app.is_sc_actuation_checked("default"), radio=True),
        pystray.MenuItem("Low", app.select_sc_actuation("low"),
                         checked=app.is_sc_actuation_checked("low"), radio=True),
    )
    sc_pointer_speed_submenu = pystray.Menu(
        pystray.MenuItem("Low", app.select_sc_pointer_speed("low"),
                         checked=app.is_sc_pointer_speed_checked("low"), radio=True),
        pystray.MenuItem("Medium", app.select_sc_pointer_speed("medium"),
                         checked=app.is_sc_pointer_speed_checked("medium"), radio=True),
        pystray.MenuItem("High", app.select_sc_pointer_speed("high"),
                         checked=app.is_sc_pointer_speed_checked("high"), radio=True),
    )
    steam_controller_submenu = pystray.Menu(
        pystray.MenuItem("Keyboard Sticks/Mouse controls",
                         app.toggle_sc_left_stick_nav,
                         checked=app.is_sc_left_stick_nav_checked),
        pystray.MenuItem("Keyboard Trigger Actuation", sc_actuation_submenu),
        pystray.MenuItem("Lizard mode Pointer Speed", sc_pointer_speed_submenu),
        pystray.MenuItem("Vibration", app.toggle_sc_rumble,
                         checked=app.is_sc_rumble_checked),
    )

    # Switch Pro Controller settings (shown only while a Switch Pro / SDL
    # pad is connected): the same submenu as the SC, minus trigger actuation,
    # plus its own Vibration toggle.
    switch_pointer_speed_submenu = pystray.Menu(
        pystray.MenuItem("Low", app.select_switch_pointer_speed("low"),
                         checked=app.is_switch_pointer_speed_checked("low"), radio=True),
        pystray.MenuItem("Medium", app.select_switch_pointer_speed("medium"),
                         checked=app.is_switch_pointer_speed_checked("medium"), radio=True),
        pystray.MenuItem("High", app.select_switch_pointer_speed("high"),
                         checked=app.is_switch_pointer_speed_checked("high"), radio=True),
    )
    nintendo_switch_submenu = pystray.Menu(
        pystray.MenuItem("Keyboard Sticks/Mouse controls",
                         app.toggle_switch_left_stick_nav,
                         checked=app.is_switch_left_stick_nav_checked),
        pystray.MenuItem("Lizard mode Pointer Speed", switch_pointer_speed_submenu),
        pystray.MenuItem("Vibration", app.toggle_switch_rumble,
                         checked=app.is_switch_rumble_checked),
    )

    return pystray.Menu(
        pystray.MenuItem(
            app.battery_menu_label,
            None,
            enabled=False,
            visible=app.is_battery_known,
        ),
        pystray.MenuItem(
            app._kbd_menu_label,
            app.open_kbd,
            default=True,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Startup", startup_submenu),
        pystray.MenuItem("Steam Controller", steam_controller_submenu,
                         visible=app.is_sc_connected),
        pystray.MenuItem("Switch Pro Controller", nintendo_switch_submenu,
                         visible=app.is_switch_connected),
        pystray.MenuItem("Keyboard Skin", skin_submenu),
        pystray.MenuItem("Advanced Settings", debug_submenu,
                         visible=app.is_debug_unlocked),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", app.exit_app),
    )


def main():
    parser = argparse.ArgumentParser(
        description="SteamlessKeyboard tray launcher (Linux)."
    )
    parser.add_argument(
        "--no-tray", action="store_true",
        help="Run without the tray icon (terminal-only, like adusk_linux.py).",
    )
    args = parser.parse_args()

    app = App()

    if args.no_tray:
        # Headless mode: behave like adusk_linux --controller. Useful when
        # the user is debugging on a session with no compatible tray.
        threading.Thread(target=app.chord_watcher_thread, daemon=True).start()
        threading.Thread(target=app.hotkey_thread, daemon=True).start()
        threading.Thread(target=app.steam_watch_thread, daemon=True).start()
        threading.Thread(target=app.battery_thread, daemon=True).start()
        threading.Thread(target=app.device_watch_thread, daemon=True).start()
        threading.Thread(target=app.sdl_gamepad_thread, daemon=True).start()
        print(f"{TRAY_TITLE} (no-tray) running. Steam+X or Ctrl+Alt+K to open.")
        try:
            while not app._stop_event.is_set():
                if app._open_kbd_event.wait(timeout=1.0):
                    app._open_kbd_event.clear()
                    _open_osk_once(app)
        except KeyboardInterrupt:
            pass
        app._stop_event.set()
        app._shutdown_sdl()
        return

    image = _load_icon_image()
    menu = _build_menu(app)
    icon = pystray.Icon("SteamlessKeyboard", image, TRAY_TITLE, menu)
    app._icon = icon

    # pystray's AppIndicator backend run_detached() doesn't actually start
    # a GLib main loop — every @mainloop-decorated call (including the
    # set_status(ACTIVE) that registers the SNI item with KDE) gets queued
    # to a loop that never runs, so the tray entry never appears. We have
    # to use icon.run() (blocking, runs GLib.MainLoop.run()) instead, on
    # its own thread so the main thread stays free for SDL.
    theme_dir, icon_name = _install_tray_icon_theme()

    def setup(ic):
        ic.visible = True
        # KDE Plasma 6 won't render an SNI item whose IconName is an
        # absolute file path (pystray's default). Point AppIndicator at our
        # private theme dir holding the project icon and resolve it by name.
        # Called via the GLib mainloop on the tray thread so the
        # AppIndicator calls land on the right thread.
        try:
            from gi.repository import GLib

            def _apply():
                try:
                    if theme_dir and icon_name:
                        ic._appindicator.set_icon_theme_path(theme_dir)
                        ic._appindicator.set_icon_full(icon_name, TRAY_TITLE)
                    else:
                        # Fallback to a Breeze name that always resolves.
                        ic._appindicator.set_icon_full(
                            "input-keyboard-virtual-show", TRAY_TITLE)
                except Exception as e:
                    print(f"tray: set_icon_full failed: {e!r}")
                return False
            GLib.idle_add(_apply)
        except Exception as e:
            print(f"tray: icon-theme override failed: {e!r}")

    tray_thread = threading.Thread(
        target=lambda: icon.run(setup=setup), daemon=True)
    tray_thread.start()

    threading.Thread(target=app.chord_watcher_thread, daemon=True).start()
    threading.Thread(target=app.hotkey_thread, daemon=True).start()
    threading.Thread(target=app.steam_watch_thread, daemon=True).start()
    threading.Thread(target=app.battery_thread, daemon=True).start()
    threading.Thread(target=app.device_watch_thread, daemon=True).start()
    threading.Thread(target=app.sdl_gamepad_thread, daemon=True).start()

    try:
        while not app._stop_event.is_set():
            if app._open_kbd_event.wait(timeout=1.0):
                app._open_kbd_event.clear()
                _open_osk_once(app)
    except KeyboardInterrupt:
        pass
    finally:
        app._stop_event.set()
        try:
            icon.stop()
        except Exception:
            pass
        app._shutdown_sdl()


if __name__ == "__main__":
    main()
