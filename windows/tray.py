"""Steam Controller Keyboard — system-tray launcher.

This is the bundled entry point for the portable EXE. It:
  * Runs a tray icon (right-click menu: Launch at PC start, Close when Steam
    starts, Exit). Settings persist in `settings.json` next to the EXE.
  * Watches the Steam Controller for the Steam+X chord and brings up the
    on-screen keyboard in-process (no subprocess startup cost).
  * Optionally pauses the listener while Steam is running and resumes after
    Steam exits (the controller is released so Steam can grab it).
"""

import ctypes
import json
import os
import sys
import threading
import time
import winreg
from ctypes import wintypes


# --- Resource / path helpers ------------------------------------------------

def _is_frozen():
    return getattr(sys, "frozen", False)


def _bundle_dir():
    """Directory containing read-only bundled resources (data/, glyphs)."""
    if _is_frozen():
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _exe_dir():
    """Directory we treat as the install location (for portable settings)."""
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _exe_path():
    return os.path.abspath(sys.executable) if _is_frozen() else os.path.abspath(__file__)


# IMPORTANT: ADUSK_DATA must be set before importing adusk.* — adusk.resources
# captures its env-var search path at import time.
os.environ["ADUSK_DATA"] = os.path.join(_bundle_dir(), "data")
# (SDL3 DLLs are located by sdl3w/_loader.py via sys._MEIPASS — no env var needed.)


import pystray  # noqa: E402

# pystray's Win32 backend opens the tray menu, and every nested submenu,
# anchored/cascading toward the right of the cursor. TrackPopupMenuEx
# always clamps its requested position to keep the menu fully on-screen,
# so requesting an anchor point far past the right edge (with
# TPM_RIGHTALIGN, which pystray already passes below) lands the menu
# flush against that edge instead. With zero room to its right, Windows'
# normal submenu placement then auto-flips every nested flyout to open
# leftward too — using ordinary left-to-right item rendering (text +
# arrow), unlike TPM_LAYOUTRTL which also mirrors that layout.
from pystray._util import win32 as _pystray_win32  # noqa: E402
_pystray_track_popup_menu_ex = _pystray_win32.TrackPopupMenuEx


def _track_popup_menu_ex_left(hmenu, flags, x, y, hwnd, params):
    # Anchor the menu to the RIGHT edge of the monitor under the cursor so the
    # right-align/left-flip behavior described above kicks in. We clamp to *that*
    # monitor instead of using a blind `x + 10000`: a large fixed offset can push
    # the anchor point onto a monitor further to the right, so on a multi-monitor
    # setup the whole menu would open on the wrong screen (e.g. a right-hand
    # monitor when the tray was clicked on the middle one). MonitorFromPoint +
    # GetMonitorInfo keep it on the screen the user actually clicked.
    right = x + 10000
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        hmon = user32.MonitorFromPoint(
            wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
        if hmon:
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
                # rcWork excludes the taskbar, matching how TrackPopupMenuEx
                # clamps the menu within the monitor's usable area.
                right = mi.rcWork.right
    except Exception:
        pass
    return _pystray_track_popup_menu_ex(
        hmenu, flags, right, y, hwnd, params)


_pystray_win32.TrackPopupMenuEx = _track_popup_menu_ex_left

from PIL import Image  # noqa: E402
from pynput import keyboard as _pynput_kb  # noqa: E402

import sdl3w as S  # noqa: E402
from steamcontroller import SteamController, SCButtons, SCStatus  # noqa: E402
from steamcontroller import uinput as sui  # noqa: E402
from steamcontroller.gamepad import VirtualGamepad, ViGEmUnavailable  # noqa: E402
from adusk import adusk as adusk_app  # noqa: E402
from adusk import inputsrc as adusk_inputsrc  # noqa: E402
from adusk import screen as adusk_screen  # noqa: E402
from adusk import skins as adusk_skins  # noqa: E402
from adusk import state as adusk_state  # noqa: E402


SETTINGS_FILENAME = "settings.json"
RUN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_REG_NAME = "SteamControllerKeyboard"
STEAM_PROC_NAME = "steam.exe"

DEFAULT_SETTINGS = {
    "start_with_windows": True,
    "disable_while_steam_running": True,
    "exit_on_steam_launch": False,
    # When on, the controller is presented to the OS as a virtual Xbox 360
    # gamepad (via ViGEm). Lizard mode (the firmware mouse/kb emulation)
    # is disabled while this is active. Steam+X still opens the OSK.
    "gamepad_mode": False,
    # When on, gamepad mode is automatically toggled on while a fullscreen
    # game is in the foreground, and back off when that process exits.
    # Default ON for first-run users so the controller "just works" in games
    # without requiring a manual toggle.
    "auto_gamepad_mode": True,
    # Per-controller haptics: gates the on-screen-keyboard click feedback AND
    # gamepad/desktop rumble for that controller. Each controller's tray submenu
    # has its own Vibration toggle (no global switch). "sc" = Steam Controller,
    # "switch" = the Nintendo Switch Pro (and other SDL pads).
    "rumble_enabled_sc": True,
    "rumble_enabled_switch": True,
    # "Block SteamInput Steam Controller grab": open the physical Steam Controller
    # HID exclusively so Steam can't read it (no Steam Input / forced lizard while
    # we hold it). Applies in ALL modes (desktop + gamepad) on its own — see the
    # use_exclusive line in launcher_thread. Must be enabled before Steam opens the
    # controller to win the grab.
    "block_sc_hid": False,
    # "Block SteamInput Xbox Controller grab": hide the VIRTUAL ViGEm Xbox 360 pad
    # from Steam (via the SDL_GAMECONTROLLER_IGNORE_DEVICES user env var) so Steam
    # Input can't grab it. Independent of block_sc_hid; takes effect the next time
    # Steam is launched. See _set_xbox_ignore.
    "block_gamepad_takeover": False,
    # When False the Debug submenu is hidden; toggled via the "Debug menu"
    # item in the Startup submenu.
    "debug_menu_unlocked": False,
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
    # like the Steam Deck). Applied on the next OSK open after the setting
    # changes (see App._rebuild_cached_screen).
    "osk_size": "medium",
    # Steam Controller-only OSK settings (tray "Steam Controller" submenu, shown
    # only while an SC is connected). "Sticks Control Keyboard" on/off (key kept
    # as sc_left_stick_nav; OFF = OSK goes click-through, sticks/mouse drive the
    # desktop, L2/R2 = mouse buttons); and the L2/R2 OSK actuation point:
    # "default" (firmware full pull) / "low".
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
}

# Foreground processes that legitimately run fullscreen but aren't games.
_NON_GAME_FULLSCREEN = {
    "explorer.exe",
    "searchapp.exe",
    "searchui.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "applicationframehost.exe",
    "lockapp.exe",
    "logonui.exe",
    "dwm.exe",
    "steam.exe",
    "steamwebhelper.exe",
    "steamcontrollerkeyboard.exe",
}

# Image / video / document viewers people commonly fullscreen but which aren't
# games. (Browsers and media players are covered by _NON_GAME_INPUT_USERS,
# which the fullscreen check also consults — see _foreground_game_pid.)
_NON_GAME_VIEWERS = {
    # Windows Photos / Photo Viewer
    "microsoft.photos.exe", "photos.exe", "windowsphotoviewer.exe",
    # third-party image viewers
    "irfanview.exe", "i_view64.exe", "i_view32.exe",
    "nomacs.exe", "imageglass.exe", "honeyview.exe", "jpegview.exe",
    "xnview.exe", "xnviewmp.exe", "fsviewer.exe", "qimgv.exe",
    # PDF / document viewers
    "acrobat.exe", "acrord32.exe", "sumatrapdf.exe", "foxitpdfreader.exe",
}

# Known game-store / launcher executables. A process whose parent is one of
# these is treated as a likely game, regardless of windowing mode.
_GAME_LAUNCHERS = {
    "steam.exe",
    "epicgameslauncher.exe",
    "galaxyclient.exe",
    "eadesktop.exe",
    "origin.exe",
    "battle.net.exe",
    "upc.exe",
    "ubisoftconnect.exe",
    "rockstargameslauncher.exe",
    "amazongameslauncher.exe",
    "itch.exe",
    "playniteui.exe",
}

# Apps that load XInput / DirectInput for legitimate non-game reasons (PTT,
# remapping, recording). Without this list the XInput-DLL heuristic would
# false-trigger on them. Process-name (basename, lowercase).
_NON_GAME_INPUT_USERS = {
    # Browsers (some implement Gamepad API which dlopens xinput)
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
    "opera.exe", "vivaldi.exe", "iexplore.exe",
    # Chat / voice (gamepad-as-PTT features)
    "discord.exe", "discordcanary.exe", "discordptb.exe",
    "slack.exe", "teams.exe", "ms-teams.exe", "zoom.exe", "skype.exe",
    # IDEs / dev tools
    "code.exe", "code - insiders.exe", "devenv.exe",
    "idea64.exe", "pycharm64.exe", "rider64.exe", "webstorm64.exe",
    "clion64.exe", "goland64.exe", "phpstorm64.exe",
    # Controller / remapper utilities
    "ds4windows.exe", "x360ce.exe", "joytokey.exe", "rewasd.exe",
    "controllercompanion.exe", "steaminput.exe",
    # Media
    "spotify.exe", "vlc.exe", "mpc-hc.exe", "mpc-hc64.exe",
    "mpc-be.exe", "mpc-be64.exe", "obs64.exe", "obs32.exe",
    # Office
    "winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe",
    "onenote.exe",
}

# DLL basename prefixes that strongly indicate a process consumes gamepad
# input — i.e., it's likely a game (or a game-adjacent tool).
_INPUT_DLL_PREFIXES = ("xinput", "dinput8", "xgameruntime")

# Helper processes spawned by the launchers themselves — these have launcher
# parents AND visible windows, so we need to explicitly exclude them.
_LAUNCHER_HELPERS = {
    # Steam
    "steamwebhelper.exe", "steamservice.exe", "gameoverlayui.exe",
    "streaming_client.exe", "vrserver.exe", "vrcompositor.exe",
    "vrdashboard.exe", "vrmonitor.exe", "vrstartup.exe",
    "html5app_steam.exe", "crashhandler.exe",
    # Epic
    "epicwebhelper.exe", "epiconlineservices.exe",
    "epiconlineservicesuihelper.exe", "epiconlineservicesinstaller.exe",
    # GOG
    "galaxyclient helper.exe", "galaxycommunication.exe",
    "galaxyoverlay.exe",
    # EA / Origin
    "eabackgroundservice.exe", "originwebhelperservice.exe",
    "ealink.exe",
    # Battle.net
    "battle.net helper.exe", "agent.exe",
    # Ubisoft
    "upcwebbrowser.exe", "upcrenderinghost.exe",
}


# --- Steam-running detection ------------------------------------------------

def _steam_running():
    """True if a steam.exe process is currently running."""
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(attrs=["name"]):
        try:
            if (proc.info.get("name") or "").lower() == STEAM_PROC_NAME:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


# --- Foreground-game detection ----------------------------------------------

class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _pids_with_visible_windows():
    """PIDs that own at least one visible top-level window with a non-empty
    title. Used to filter out background-only processes (services, helpers)
    when scanning for launcher-child games."""
    result = set()
    user32 = ctypes.windll.user32

    def cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                result.add(pid.value)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_ENUM_WINDOWS_PROC(cb), 0)
    except Exception:
        return set()
    return result


def _foreground_game_pid():
    """Return the PID of a fullscreen non-shell foreground window, or None.

    Used by auto gamepad mode: a window that exactly covers its monitor and
    isn't a known shell/system process is treated as a game.
    """
    try:
        import psutil
    except ImportError:
        return None

    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

        hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return None

        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None

        if not (rect.left == mi.rcMonitor.left
                and rect.top == mi.rcMonitor.top
                and rect.right == mi.rcMonitor.right
                and rect.bottom == mi.rcMonitor.bottom):
            return None

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None

        try:
            name = (psutil.Process(pid.value).name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

        # Shell/system, viewers (image/video/PDF), and known non-game input
        # users (browsers, media players, chat, dev tools) can all cover the
        # whole monitor without being a game — don't auto-enable for them.
        if (name in _NON_GAME_FULLSCREEN
                or name in _NON_GAME_VIEWERS
                or name in _NON_GAME_INPUT_USERS):
            return None
        return pid.value
    except Exception:
        return None


def _foreground_window_kill_pid():
    """PID of the foreground window's process for the EXPLICIT Home+B force-kill
    chord — like _foreground_game_pid but WITHOUT the fullscreen requirement, so
    it also closes WINDOWED games (the user deliberately asked to kill whatever
    is in front). Still refuses to target the shell/system, Steam, or our own
    process so the desktop / launcher can't be killed by accident."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return None
        try:
            name = (psutil.Process(pid.value).name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        # Never kill the shell/system/Steam/our own app (they'd break the
        # desktop or the launcher). Other foreground apps ARE fair game — the
        # user explicitly pressed the chord to kill what's in front.
        if name in _NON_GAME_FULLSCREEN:
            return None
        return pid.value
    except Exception:
        return None


# Path segment names (case-insensitive) that indicate a game install dir.
# Detection: split the exe path on / and \ and check if any segment matches.
# Catches both storefront install layouts (steamapps/, "Epic Games/") and
# common user-organized folders ("Games", "My Games", etc.).
_GAME_DIR_NAMES = {
    # Storefront install roots
    "steamapps",
    "epic games",
    "gog games", "gog galaxy",
    "ea games", "origin games",
    "ubisoft", "uplay",
    "battle.net",
    "amazon games",
    "riot games",
    "itch.io", "itch",
    "playnite",
    # User-organized game folders
    "games", "game",
    "my games", "pc games", "steam games", "portable games",
    "emulators",
}


def _exe_in_game_dir(exe_path):
    """True if any segment of `exe_path` is a recognized games-folder name."""
    if not exe_path:
        return False
    norm = exe_path.lower().replace("\\", "/")
    for seg in norm.split("/"):
        if seg in _GAME_DIR_NAMES:
            return True
    return False


def _foreground_pid():
    """PID of the process owning the current foreground window, or 0."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def _ancestor_pids(pid, max_depth=6):
    """Yield ancestor PIDs of `pid`, starting at its direct parent."""
    try:
        import psutil
    except ImportError:
        return
    try:
        current = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for _ in range(max_depth):
        try:
            current = current.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        if current is None:
            return
        yield current.pid


def _is_latched_focused(latched_pid):
    """True if the foreground window belongs to `latched_pid` or one of its
    descendants (handles games whose foreground window is a child process)."""
    fp = _foreground_pid()
    if not fp:
        return False
    if fp == latched_pid:
        return True
    for ancestor_pid in _ancestor_pids(fp):
        if ancestor_pid == latched_pid:
            return True
    return False


def _process_loads_input_dll(proc):
    """True if `proc` has mapped an XInput / DirectInput / XGameRuntime DLL.
    Used as a path-independent game signal: if a process is reading from a
    gamepad, you almost certainly want gamepad mode active for it."""
    try:
        maps = proc.memory_maps()
    except Exception:
        # Permission denied, NotImplemented, NoSuchProcess, etc. — be silent
        # and let the caller fall back to other heuristics.
        return False
    for mm in maps:
        path = (getattr(mm, "path", "") or "").lower().replace("\\", "/")
        if not path:
            continue
        base = path.rsplit("/", 1)[-1]
        for prefix in _INPUT_DLL_PREFIXES:
            if base.startswith(prefix):
                return True
    return False


def _ancestor_names(proc, max_depth=6):
    """Yield (depth, name_lower) for each ancestor of `proc`, starting at
    its direct parent (depth=1). Stops on permission errors or root."""
    try:
        import psutil
    except ImportError:
        return
    current = proc
    for depth in range(1, max_depth + 1):
        try:
            current = current.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        if current is None:
            return
        try:
            yield depth, (current.name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return


def _launched_game_pid(debug_log=None):
    """Return the PID of a process that looks like a launched game (parent
    chain includes a known storefront, or exe lives in a game library
    directory) and owns at least one visible window. Catches windowed
    games that _foreground_game_pid() misses.

    If `debug_log` is a writable file, dump per-process diagnostic info so
    the user can see why detection did or did not fire."""
    try:
        import psutil
    except ImportError:
        return None

    visible = _pids_with_visible_windows()
    if not visible:
        if debug_log:
            debug_log.write("  (no visible top-level windows)\n")
        return None

    candidates = []  # (create_time, pid) — newer wins
    for proc in psutil.process_iter(attrs=["pid", "name", "ppid", "create_time"]):
        try:
            info = proc.info
            pid = info.get("pid")
            if pid is None or pid not in visible:
                continue
            name = (info.get("name") or "").lower()
            if name in _NON_GAME_FULLSCREEN or name in _LAUNCHER_HELPERS:
                if debug_log:
                    debug_log.write(f"  skip pid={pid} {name} (helper/system)\n")
                continue
            if name in _GAME_LAUNCHERS:
                if debug_log:
                    debug_log.write(f"  skip pid={pid} {name} (launcher itself)\n")
                continue

            try:
                exe = ""
                try:
                    exe = proc.exe() or ""
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                ancestors = list(_ancestor_names(proc))
            except Exception:
                continue

            looks_game = False
            match_reason = ""
            for _depth, an in ancestors:
                if an in _GAME_LAUNCHERS:
                    looks_game = True
                    match_reason = f"launcher-ancestor:{an}"
                    break
            if not looks_game and _exe_in_game_dir(exe):
                looks_game = True
                match_reason = "game-dir-path"
            if (not looks_game
                    and name not in _NON_GAME_INPUT_USERS
                    and _process_loads_input_dll(proc)):
                looks_game = True
                match_reason = "loads-input-dll"

            if debug_log:
                anc_str = " ← ".join(f"{n}" for _d, n in ancestors) or "<none>"
                tag = f"MATCH({match_reason})" if looks_game else "no-match"
                debug_log.write(
                    f"  visible pid={pid} name={name} "
                    f"ancestors=[{anc_str}] exe={exe!r} {tag}\n"
                )

            if looks_game:
                candidates.append((info.get("create_time", 0.0), pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _detect_game_pid(debug_log=None):
    """Combined detection: fullscreen-foreground (fast), then process-scan
    for launcher-child / game-library-path processes (catches windowed)."""
    pid = _foreground_game_pid()
    if pid:
        if debug_log:
            debug_log.write(f"  foreground-fullscreen MATCH pid={pid}\n")
        return pid
    return _launched_game_pid(debug_log=debug_log)


def _force_kill_foreground_game():
    """Force-shutdown the foreground game, leaving its launcher ('parent')
    alive. Climbs from the foreground fullscreen process up to the highest
    ancestor that is still BELOW a known launcher (steam.exe etc.) or the
    shell — i.e. the game's own root process — then force-kills that whole
    subtree. Returns the killed root pid, or None if no game was found.

    Stopping the climb at a launcher/shell is the 'cleared from parent' part:
    we never kill Steam/Explorer, only the game and everything it spawned."""
    try:
        import psutil
    except ImportError:
        return None
    # Use the non-fullscreen foreground pid so WINDOWED games close too (the
    # fullscreen-only _foreground_game_pid is for auto gamepad mode, not this
    # explicit kill chord).
    pid = _foreground_window_kill_pid()
    if not pid:
        return None
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

    # Climb to the game's root: keep moving up while the parent is an ordinary
    # process. Stop when the parent is a launcher, a shell/system process, or
    # gone — that parent is the boundary we must not cross. Depth-capped so a
    # weird chain can't walk us up to init.
    root = proc
    try:
        cur = proc
        for _ in range(8):
            par = cur.parent()
            if par is None:
                break
            pname = (par.name() or "").lower()
            if (pname in _GAME_LAUNCHERS
                    or pname in _NON_GAME_FULLSCREEN
                    or par.pid <= 4):
                break  # parent is the launcher / shell / system — stop here
            root = cur = par
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    # Kill the whole subtree: children first, then the root.
    victims = []
    try:
        victims = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    victims.append(root)
    killed_root = root.pid
    for p in victims:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed_root


# --- Settings persistence ---------------------------------------------------

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
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    # Gamepad mode is now mutually exclusive — if a settings file from an
    # older build has both on, prefer Auto-enable.
    if merged["gamepad_mode"] and merged["auto_gamepad_mode"]:
        merged["gamepad_mode"] = False
    # Migrate old exclusive_access key to block_sc_hid.
    if "exclusive_access" in data:
        merged["block_sc_hid"] = bool(data["exclusive_access"])
    # The single global "rumble_enabled" split into per-controller toggles — seed
    # both from the old value so a saved preference carries over.
    if "rumble_enabled" in data:
        on = bool(data["rumble_enabled"])
        merged["rumble_enabled_sc"] = on
        merged["rumble_enabled_switch"] = on
    # The two-level "low"(6000)/"lower"(3000) actuation collapsed to a single
    # "low" using the lighter 3000 pull — fold a saved "lower" into "low".
    if merged.get("sc_osk_trigger_actuation") == "lower":
        merged["sc_osk_trigger_actuation"] = "low"
    return merged


def _save_settings(settings):
    path = os.path.join(_exe_dir(), SETTINGS_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        print(f"settings save failed: {e}")


# --- "Block SteamInput Xbox Controller grab" --------------------------------
# Hide the VIRTUAL ViGEm Xbox 360 pad (VID 045E / PID 028E) from Steam so Steam
# Input can't grab it. Steam — like SDL, which it uses to enumerate controllers
# — skips any controller listed in the SDL_GAMECONTROLLER_IGNORE_DEVICES *user*
# env var, which it reads when it launches. That matches the intended workflow
# (enable the block, THEN open Steam). Verified: with this set, SDL stops
# enumerating the Xbox 360 pad entirely. Tradeoff while it's on: Steam and other
# SDL apps also skip real Xbox-360-type pads; XInput games still see our pad
# (XInput doesn't consult this list). Windows-only (HKCU\Environment); the
# helper no-ops elsewhere so the Linux mirror stays import-safe.
_IGNORE_ENV = "SDL_GAMECONTROLLER_IGNORE_DEVICES"
_VIGEM_X360_IGNORE = "0x045E/0x028E"


def _set_xbox_ignore(enabled):
    """Add (enabled) or remove (not enabled) our ViGEm Xbox 360 pad from the
    user's SDL ignore list, preserving any entries the user set themselves, then
    broadcast the change so a Steam launched afterwards inherits it. No-op off
    Windows."""
    if os.name != "nt":
        return
    try:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                cur = str(winreg.QueryValueEx(k, _IGNORE_ENV)[0])
        except OSError:
            cur = ""
        parts = [p.strip() for p in cur.split(",") if p.strip()]
        tgt = _VIGEM_X360_IGNORE.lower()
        has = any(p.lower() == tgt for p in parts)
        if enabled and not has:
            parts.append(_VIGEM_X360_IGNORE)
        elif not enabled and has:
            parts = [p for p in parts if p.lower() != tgt]
        else:
            return  # already in the desired state
        new_val = ",".join(parts)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE) as k:
            if new_val:
                winreg.SetValueEx(k, _IGNORE_ENV, 0, winreg.REG_SZ, new_val)
            else:
                try:
                    winreg.DeleteValue(k, _IGNORE_ENV)
                except OSError:
                    pass
        # Nudge Explorer (which launches Steam) to refresh its environment block.
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, ctypes.c_wchar_p("Environment"),
            0x0002, 2000, ctypes.byref(ctypes.c_ulong()))
    except Exception as e:
        print(f"_set_xbox_ignore failed: {e!r}")


def _chime_log(msg):
    """Best-effort diagnostic log for the gamepad-mode chime trigger, written
    next to the EXE as chime_debug.log. Opt-in via ADUSK_GAMEPAD_DEBUG (same
    switch as the auto-gamepad debug log) so normal use writes nothing."""
    if not os.environ.get("ADUSK_GAMEPAD_DEBUG"):
        return
    try:
        path = os.path.join(_exe_dir(), "chime_debug.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# --- Windows "Run on startup" registry --------------------------------------

def _apply_startup_registry(enabled):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                value = f'"{_exe_path()}"'
                if not _is_frozen():
                    value = f'"{sys.executable}" "{_exe_path()}"'
                winreg.SetValueEx(key, RUN_REG_NAME, 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(key, RUN_REG_NAME)
                except FileNotFoundError:
                    pass
    except OSError as e:
        print(f"registry update failed: {e}")


# --- Lock-screen guard ------------------------------------------------------
#
# This tray app runs in the *interactive user session* and keeps reading the
# controller even while the PC is locked. Without this guard, pressing X on the
# lock screen would pop our keyboard up on the user's (Default) desktop —
# invisible *behind* the secure Winlogon lock screen — instead of doing nothing.
# (The lock screen has its own separate keyboard launched via the accessibility
# hook.) OpenInputDesktop succeeds only when the *Default* desktop owns input;
# while the secure desktop is up (lock screen, UAC, Ctrl+Alt+Del) it fails from
# a user-session process, which is exactly our "is it locked?" signal.

_user32 = ctypes.windll.user32
_user32.OpenInputDesktop.restype = wintypes.HANDLE
_user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_user32.CloseDesktop.argtypes = [wintypes.HANDLE]
_user32.CloseDesktop.restype = wintypes.BOOL


def _workstation_locked():
    """True while the secure desktop owns input (lock screen / UAC / Secure
    Attention Sequence), so we must NOT open the keyboard behind it."""
    hdesk = _user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not hdesk:
        return True
    _user32.CloseDesktop(hdesk)
    return False


# Shell / desktop / system window classes that are never a real "type into me"
# target — so a stray firmware click onto the empty desktop or taskbar (or our
# own OSK) doesn't get remembered as the window to restore focus to.
_SHELL_WINDOW_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow", "ForegroundStaging", "MultitaskingViewFrame",
    "XamlExplorerHostIslandWindow",
}


def _foreground_target_hwnd():
    """The foreground window the user is typing in: a normal window owned by
    ANOTHER process. Returns None for our own windows and for the shell/desktop,
    so those never get recorded as the focus-restore target. HWND as an int."""
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return None
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
        if buf.value in _SHELL_WINDOW_CLASSES:
            return None
        return int(hwnd)
    except Exception:
        return None


# --- Steam+X chord watcher (reused from adusk_launcher) ---------------------


class _ChordState:
    """Persistent state for the Steam+VIEW=Alt+Tab chord. Lives at the App
    level (not on _Watcher) because sc.run() can be kicked mid-chord by
    auto-gamepad-detect when alt-tab steals focus from the game. If this
    state lived on _Watcher, the rebuild would forget that Alt was held
    and the subsequent Alt release would never fire — leaving Alt stuck
    at the OS level (every keypress turns into Alt+key)."""

    def __init__(self):
        self.kb = sui.Keyboard()
        self.mouse = sui.Mouse()
        # True while LEFTALT is currently being held by us.
        self.alt_held = False
        # Rising-edge tracking for VIEW so one physical press = one Tab.
        self.view_was_pressed = False
        # Desktop-mode held paddle modifiers: L4 = Shift, L5 = Windows key.
        # Held here (not on _Watcher) for the same reason as alt_held — a
        # mid-hold sc.run() rebuild must not strand them pressed at the OS
        # level.
        self.shift_held = False
        self.win_held = False
        # Injected mouse buttons held by the gamepad-mode Steam+stick mouse
        # mode (L2 = left, R2 = right). Held here so a mid-hold rebuild can't
        # strand the button down at the OS level.
        self.mouse_left_held = False
        self.mouse_right_held = False

    def release_alt(self):
        if self.alt_held:
            self.kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self.alt_held = False

    def release_shift(self):
        if self.shift_held:
            self.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self.shift_held = False

    def release_win(self):
        if self.win_held:
            self.kb.releaseEvent([sui.Keys.KEY_LEFTMETA])
            self.win_held = False

    def release_mouse_buttons(self):
        if self.mouse_left_held:
            self.mouse.release("left")
            self.mouse_left_held = False
        if self.mouse_right_held:
            self.mouse.release("right")
            self.mouse_right_held = False

    def release_all_held(self):
        self.release_alt()
        self.release_shift()
        self.release_win()
        self.release_mouse_buttons()


class _Watcher:
    def __init__(self, should_abort, gamepad=None, chord=None):
        self.triggered = False
        # HWND (int) of the desktop window the user was typing in just before
        # an OSK-open press, sampled while neither Steam nor X is held. The
        # launcher hands this to adusk so it can restore focus after the OSK
        # opens (a controller-open's firmware mouse-click can steal it).
        self._last_user_hwnd = None
        self._fg_poll_at = 0.0
        # Callable returning True when the sc.run() loop should exit early
        # (e.g. tray-Exit was clicked, or Steam started).
        self._should_abort = should_abort
        # Optional VirtualGamepad — when present, every input frame is
        # forwarded to ViGEm so the controller acts as an Xbox 360 pad.
        self._gamepad = gamepad
        # Tracks whether we've asked the controller to switch into firmware
        # lizard mode for the duration of a Steam-button hold. Only meaningful
        # when _gamepad is not None (gamepad mode is active).
        self._steam_hold_lizard = False
        # Tracks the lizard state we last set in gamepad mode so we only
        # send a feature report when it actually needs to change.
        self._gamepad_lizard_on = False
        # Latches set while Steam is held: pad-touch engages lizard for the
        # rest of the hold (so brief finger lifts don't flicker the firmware
        # mouse); VIEW commits to chord mode for the rest of the hold (so
        # subsequent VIEW taps don't flip lizard on/off mid-Alt-Tab).
        self._steam_hold_pad_used = False
        self._steam_hold_chord_used = False
        # Shared chord state (Alt held flag, VIEW edge, kb) so the chord
        # survives sc.run() restarts. Falls back to a local _ChordState if
        # the caller doesn't supply one (e.g. tests).
        self._chord = chord if chord is not None else _ChordState()
        # In passive (firmware-lizard-on) mode, holding Steam temporarily
        # turns lizard OFF so the firmware doesn't emit its own Tab when
        # VIEW is pressed (which would race with our Steam+VIEW=Alt+Tab and
        # also rapid-cycle via Windows key auto-repeat while VIEW is held).
        self._passive_lizard_suppressed = False
        # Timestamp of the last DISABLE_LIZARD re-assertion during a Steam
        # hold; the hardware watchdog re-enables lizard every 3-5s so we
        # re-send periodically to keep it suppressed for the whole hold.
        self._last_lizard_suppress = 0.0
        # Steam + left-stick media chords (volume / track skip) and Steam + L3
        # (play/pause). Mirrors adusk/controller.py so the chords work whether
        # or not the on-screen keyboard is open.
        self._stick_zone_prev = "NEUTRAL"
        self._stick_repeat_at = 0.0
        self._l3_was_pressed = False
        # Left stick → arrow keys in passive/desktop mode (no Steam). Dominant
        # axis, auto-repeating while held so it feels like holding an arrow.
        self._arrow_zone_prev = "NEUTRAL"
        self._arrow_repeat_at = 0.0
        # Right stick → mouse in passive/desktop mode. Velocity scales with
        # deflection; movement is integrated over real time and fractional
        # pixels are carried between frames so slow movement isn't lost.
        self._mouse_last_t = 0.0
        self._mouse_acc_x = 0.0
        self._mouse_acc_y = 0.0
        # Steam + Y → power off the controller (like Steam Input). _powered_off
        # latches so we only send the command once per chord press.
        self._powered_off = False
        # Steam + B → force-kill the foreground game (cleared from its parent
        # launcher). Latches so it fires once per chord press.
        self._force_kill_done = False
        # Y alone (no Steam) in passive/desktop mode → Space. Rising-edge
        # latch so one press = one Space. NOTE: firmware lizard is still on in
        # passive mode, so the controller may also emit its own Y action.
        self._y_alone_was_pressed = False
        # X opens the on-screen keyboard (bare X in desktop mode, Steam+X in
        # any mode). Rising-edge latch so one press = one open.
        self._x_open_was_pressed = False
        # Right back paddles in passive/desktop mode: R4 (RGRIP1) → Page Up,
        # R5 (RGRIP2) → Page Down. Rising-edge latches.
        self._r4_was_pressed = False
        self._r5_was_pressed = False
        # L1 / R1 (bumpers) in desktop mode → previous / next browser tab.
        # Rising-edge latches.
        self._lb_was_pressed = False
        self._rb_was_pressed = False
        # L3 (left stick click) alone in desktop mode → middle click at the
        # cursor (Steam+L3 is Play/Pause). Rising-edge latch, tracked every frame.
        self._l3_mid_prev = False
        # L2 / R2 full-pull (firmware mouse left/right click in desktop mode):
        # rising-edge latches so each full pull buzzes the haptic click once.
        self._lt_was_pressed = False
        self._rt_was_pressed = False

    # Left-stick deflection (int16) past this magnitude counts as a direction.
    STICK_DEADZONE = 14000
    # Volume feel: a tap = one step. Holding up/down past STICK_HOLD_DELAY
    # seconds then rapidly ramps, one step every STICK_VOL_REPEAT seconds.
    STICK_HOLD_DELAY = 0.5
    STICK_VOL_REPEAT = 0.021
    # Arrow-key feel: a tap = one press; held past ARROW_HOLD_DELAY it repeats
    # every ARROW_REPEAT seconds (like an OS key-repeat). 0.05 gave ~20s to
    # scroll a test page; /0.7 made it 30% slower, then *1.1 another 10% slower
    # (user-tuned to match the Switch Pro, which gets the same factors below).
    ARROW_HOLD_DELAY = 0.35
    ARROW_REPEAT = 0.05 / 0.7 * 1.1
    # Right-stick mouse: deadzone (int16), top speed in px/sec at full
    # deflection, and an exponent >1 for fine control near center. A bigger
    # exponent = a longer ramp (more of the stick travel maps to slow speeds),
    # so precise cursor control needs less surgical thumb precision.
    MOUSE_DEADZONE = 6000
    MOUSE_SPEED = 1400.0
    MOUSE_EXPONENT = 5.0
    # Minimum speed (fraction of full) the instant the stick passes the deadzone,
    # so the first bit of travel moves a usable amount (>1px/frame) for fine
    # control instead of the near-zero the steep exponent gives.
    MOUSE_MIN = 0.05

    # Zone→key maps, built once at class scope. Previously these were dict
    # literals rebuilt on every HID frame inside the stick handlers — pure
    # per-frame allocation churn on the hot path.
    _MEDIA_KEYS = {
        "UP":    sui.Keys.KEY_VOLUMEUP,
        "DOWN":  sui.Keys.KEY_VOLUMEDOWN,
        "LEFT":  sui.Keys.KEY_PREVIOUSSONG,
        "RIGHT": sui.Keys.KEY_NEXTSONG,
    }
    _ARROW_KEYS = {
        "UP":    sui.Keys.KEY_UP,
        "DOWN":  sui.Keys.KEY_DOWN,
        "LEFT":  sui.Keys.KEY_LEFT,
        "RIGHT": sui.Keys.KEY_RIGHT,
    }

    def _handle_media_chords(self, sc, sci, steam_now, now):
        """Steam + left stick → media transport (Up/Down = volume, repeating
        while held; Left/Right = previous/next track, one per deflection).
        Steam + L3 (stick click) → Play/Pause. Edge-triggered so one
        deflection / click = one media key."""
        # Steam + L3 → Play/Pause (rising edge).
        l3_now = bool(sci.buttons & SCButtons.L3)
        if steam_now and l3_now and not self._l3_was_pressed:
            self._chord.kb.pressEvent([sui.Keys.KEY_PLAYPAUSE])
            self._chord.kb.releaseEvent([sui.Keys.KEY_PLAYPAUSE])
        self._l3_was_pressed = l3_now

        x = sci.lstick_x
        y = sci.lstick_y  # positive = up (same hardware sign as the pads)
        zone = "NEUTRAL"
        if steam_now and (abs(x) > self.STICK_DEADZONE
                          or abs(y) > self.STICK_DEADZONE):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        key = self._MEDIA_KEYS.get(zone)

        fire = False
        is_edge = False
        if zone != self._stick_zone_prev:
            # Edge fires once (the tap), then wait STICK_HOLD_DELAY before any
            # rapid repeat — so a tap or sub-second hold is exactly one step.
            fire = zone != "NEUTRAL"
            is_edge = fire
            self._stick_repeat_at = now + self.STICK_HOLD_DELAY
        elif zone in ("UP", "DOWN") and now >= self._stick_repeat_at:
            # Held past the delay: volume ramps fast. Track skip never repeats.
            fire = True
            self._stick_repeat_at = now + self.STICK_VOL_REPEAT
        self._stick_zone_prev = zone

        if fire and key is not None:
            self._chord.kb.pressEvent([key])
            self._chord.kb.releaseEvent([key])
            # Haptic tick on a volume TAP only (one 2% step) — not the rapid
            # hold-ramp, and not track skip (left/right). Gated by the global
            # haptics switch.
            if is_edge and zone in ("UP", "DOWN") and adusk_state.is_rumble_enabled("sc"):
                sc.haptic_click()

    def _handle_arrow_stick(self, sci, steam_now, now):
        """Desktop mode: left stick → arrow keys (dominant axis), one per
        deflection then auto-repeating while held. Disabled in gamepad mode
        (the stick is the analog stick) and while Steam is held (that's the
        media chord)."""
        active = self._gamepad is None and not steam_now
        x = sci.lstick_x
        y = sci.lstick_y  # positive = up (same hardware sign as the pads)
        zone = "NEUTRAL"
        if active and (abs(x) > self.STICK_DEADZONE
                       or abs(y) > self.STICK_DEADZONE):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        key = self._ARROW_KEYS.get(zone)

        fire = False
        if zone != self._arrow_zone_prev:
            # New direction (or release): the press fires immediately, then we
            # wait ARROW_HOLD_DELAY before the first repeat.
            fire = zone != "NEUTRAL"
            self._arrow_repeat_at = now + self.ARROW_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._arrow_repeat_at:
            fire = True
            self._arrow_repeat_at = now + self.ARROW_REPEAT
        self._arrow_zone_prev = zone

        if fire and key is not None:
            self._chord.kb.pressEvent([key])
            self._chord.kb.releaseEvent([key])

    def _handle_mouse_stick(self, sci, now):
        """Right stick moves the mouse cursor. Velocity scales with deflection
        past the deadzone (with an exponent for fine control), integrated over
        real elapsed time so the speed is frame-rate independent. The caller
        gates *when* this runs: every frame in desktop mode, and only during a
        Steam/"..." hold in gamepad mode (XInput is paused then, so the right
        stick is free to act as a mouse — mirroring the Steam+trackpad latch).
        """
        dt = now - self._mouse_last_t if self._mouse_last_t else 0.0
        self._mouse_last_t = now

        x = sci.rstick_x
        y = sci.rstick_y  # positive = up
        mag = (x * x + y * y) ** 0.5
        if mag <= self.MOUSE_DEADZONE:
            # Idle: reset accumulators so a fresh push starts clean, and don't
            # carry a stale dt forward.
            self._mouse_acc_x = 0.0
            self._mouse_acc_y = 0.0
            return
        # Clamp dt so a pause between reports (or the first frame) can't fling
        # the cursor; assume a typical ~60 Hz frame if it's out of range.
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0

        # RADIAL speed: apply the curve to the stick's DISTANCE from center, then
        # move along its unit direction, so a diagonal push is as fast as a pure
        # horizontal/vertical one. (Per-axis exponent made diagonals much slower,
        # very visible at high exponents.)
        m = min(1.0, (mag - self.MOUSE_DEADZONE) / (32767.0 - self.MOUSE_DEADZONE))
        unit = self.MOUSE_MIN + (1.0 - self.MOUSE_MIN) * (m ** self.MOUSE_EXPONENT)
        scaled = unit / mag
        # Screen Y grows downward, so stick-up (positive y) moves up (-dy).
        # "Pointer Speed" (tray Steam Controller menu) scales the base px/sec,
        # matching the OSK right-stick mouse so the pointer feels the same
        # whether the keyboard is open or closed.
        speed = self.MOUSE_SPEED * adusk_state.get_sc_mouse_speed()
        self._mouse_acc_x += (x * scaled) * speed * dt
        self._mouse_acc_y += -(y * scaled) * speed * dt
        mvx = int(self._mouse_acc_x)
        mvy = int(self._mouse_acc_y)
        self._mouse_acc_x -= mvx
        self._mouse_acc_y -= mvy
        if mvx or mvy:
            self._chord.mouse.move(mvx, mvy)

    def _handle_gamepad_mouse_clicks(self, sc, sci, steam_now):
        """Gamepad mode: while Steam/"..." is held (the right-stick / trackpad
        mouse mode), L2 → left click and R2 → right click — injected as real
        mouse buttons, since firmware lizard isn't driving them during the hold.
        Press/release (held while the trigger is) so it can drag-select.
        Reconciled every gamepad-mode frame so releasing Steam OR the trigger
        releases the button. A press edge gets the same haptic snap as the
        desktop-mode trigger click.

        Suppressed while the Steam+trackpad lizard mouse latch is on: there the
        firmware already injects L2/R2 clicks, so injecting again would
        double-fire. This is the right-stick mouse path's clicks."""
        allow = steam_now and not self._gamepad_lizard_on
        want_left = allow and bool(sci.buttons & SCButtons.LT)
        want_right = allow and bool(sci.buttons & SCButtons.RT)
        if want_left != self._chord.mouse_left_held:
            if want_left:
                self._chord.mouse.press("left")
                if adusk_state.is_rumble_enabled("sc"):
                    sc.haptic_click()
            else:
                self._chord.mouse.release("left")
            self._chord.mouse_left_held = want_left
        if want_right != self._chord.mouse_right_held:
            if want_right:
                self._chord.mouse.press("right")
                if adusk_state.is_rumble_enabled("sc"):
                    sc.haptic_click()
            else:
                self._chord.mouse.release("right")
            self._chord.mouse_right_held = want_right

    def on_input(self, sc, sci):
        if sci.status != SCStatus.INPUT:
            return
        if self._should_abort():
            # Drop any held modifiers so they don't stick at the OS level when
            # this watcher tears down (e.g. tray Exit / Steam launch).
            self._chord.release_all_held()
            sc.addExit()
            return

        steam_now = bool(sci.buttons & (SCButtons.STEAM | SCButtons.QAM))  # "..." (QAM) acts like Steam
        x_now = bool(sci.buttons & SCButtons.X)

        # Release Alt-Tab on Steam release BEFORE we touch the gamepad. If
        # we let gamepad.update push an XInput frame before releasing Alt,
        # the next-window commit gets dropped in gamepad mode (alt-tab UI
        # stays up and the user has to press A to confirm). In passive mode
        # this didn't matter because nothing was pushing XInput.
        if not steam_now:
            self._chord.release_alt()

        if self._gamepad is not None:
            # Hold Steam to pause XInput so chord buttons (Steam+VIEW) don't
            # leak into the game.
            if steam_now and not self._steam_hold_lizard:
                self._gamepad.reset()
                self._steam_hold_lizard = True
            elif not steam_now and self._steam_hold_lizard:
                self._steam_hold_lizard = False

            # Latch-based mode selection during a Steam hold:
            #   * Touch the right pad → "mouse mode" latched on for the rest
            #     of the hold (lizard ON). Capacitive touch flickers when
            #     fingers shift, so latching avoids rapid lizard toggling
            #     that would break click and make movement feel stuttery.
            #   * Press VIEW → "chord mode" latched on for the rest of the
            #     hold (lizard OFF). Wins over mouse mode so the Steam+VIEW
            #     =Alt+Tab injection isn't fighting firmware-emitted keys.
            # Both latches reset when Steam is released.
            rpad_touched = bool(sci.buttons & SCButtons.RPADTOUCH)
            view_for_lizard = bool(sci.buttons & SCButtons.VIEW)
            if not steam_now:
                self._steam_hold_pad_used = False
                self._steam_hold_chord_used = False
            else:
                if rpad_touched:
                    self._steam_hold_pad_used = True
                if view_for_lizard:
                    self._steam_hold_chord_used = True
            want_lizard = (steam_now
                           and self._steam_hold_pad_used
                           and not self._steam_hold_chord_used)
            if want_lizard != self._gamepad_lizard_on:
                sc.set_lizard(want_lizard)
                self._gamepad_lizard_on = want_lizard

            if not self._steam_hold_lizard:
                try:
                    self._gamepad.update(sci)
                except Exception as e:
                    print(f"gamepad update failed; disabling: {e!r}")
                    self._gamepad = None
        else:
            # Passive (lizard-on) mode: holding Steam temporarily turns lizard
            # OFF so the firmware doesn't auto-emit Tab on VIEW (or any other
            # mapped key) while our chord injectors are active. Re-asserts
            # every ~2s during the hold so the hardware watchdog can't sneak
            # lizard back on.
            now = time.monotonic()
            if steam_now:
                if (not self._passive_lizard_suppressed
                        or now - self._last_lizard_suppress > 2.0):
                    sc.set_lizard(False)
                    self._passive_lizard_suppressed = True
                    self._last_lizard_suppress = now
            elif self._passive_lizard_suppressed:
                sc.set_lizard(True)
                self._passive_lizard_suppressed = False

        # Remember the window the user is typing in, sampled (≤10 Hz) only while
        # neither Steam nor X is held — i.e. BEFORE the opening press. When X is
        # then pressed to open the OSK, the firmware lizard also fires X's mouse
        # action onto the desktop, which can land off the field and steal focus;
        # adusk re-focuses this saved window after the OSK is up. Skip in active
        # gamepad mode (controller is a pad, not a desktop mouse/kb).
        if self._gamepad is None and not steam_now and not x_now:
            _now = time.monotonic()
            if _now - self._fg_poll_at > 0.1:
                self._fg_poll_at = _now
                tgt = _foreground_target_hwnd()
                if tgt:
                    self._last_user_hwnd = tgt

        # X opens the on-screen keyboard. In desktop mode bare X works (and
        # Steam+X too); in gamepad mode bare X is a face button, so only
        # Steam+X opens it. Rising-edge so one press = one open; releasing the
        # controller here lets adusk grab it. Suppressed while the workstation
        # is locked so it can't open behind the secure lock-screen desktop.
        x_opens = x_now and (self._gamepad is None or steam_now)
        if x_opens and not self._x_open_was_pressed and not _workstation_locked():
            self.triggered = True
            sc.addExit()
        self._x_open_was_pressed = x_opens

        # Steam + VIEW (small button upper-right of the Steam logo) → Alt+Tab.
        # Hold Alt for the duration of the Steam hold so the switcher stays
        # visible; each VIEW rising edge taps Tab once to advance one slot.
        # Firmware kb is suppressed above so VIEW doesn't double-fire, and
        # rising-edge detection prevents one physical hold from cycling.
        # Releasing Steam drops Alt and commits the selection.
        view_now = bool(sci.buttons & SCButtons.VIEW)
        if steam_now and view_now and not self._chord.view_was_pressed:
            if not self._chord.alt_held:
                self._chord.kb.pressEvent([sui.Keys.KEY_LEFTALT])
                self._chord.alt_held = True
            self._chord.kb.pressEvent([sui.Keys.KEY_TAB])
            self._chord.kb.releaseEvent([sui.Keys.KEY_TAB])
        self._chord.view_was_pressed = view_now
        # Alt release on Steam-release is handled near the top of this
        # method, before gamepad.update fires (see comment there).

        # One clock read shared by all the time-based handlers below (was three
        # separate monotonic() calls per frame).
        now = time.monotonic()

        # Desktop mode: L3 (left stick click) ALONE → middle click at the cursor
        # (Steam+L3 is Play/Pause, handled in the media chords). Great for web
        # browsing — middle-click a link to open it in a new background tab, or a
        # tab to close it. The edge is tracked every frame so releasing Steam
        # while still holding L3 can't spuriously fire a click.
        l3_mid_now = bool(sci.buttons & SCButtons.L3)
        if (self._gamepad is None and not steam_now
                and l3_mid_now and not self._l3_mid_prev):
            self._chord.mouse.press("middle")
            self._chord.mouse.release("middle")
        self._l3_mid_prev = l3_mid_now

        # Steam + left stick / L3 → media transport. Cheap when Steam isn't held
        # (it just keeps its zone/edge bookkeeping in sync), so it stays called
        # every frame to preserve exact edge behavior.
        self._handle_media_chords(sc, sci, steam_now, now)

        # Left stick → arrow keys, right stick → mouse. In desktop mode both run
        # every frame. In gamepad mode the sticks are the analog sticks, so they
        # stay off the gameplay hot path — EXCEPT the right-stick mouse still
        # runs during a Steam/"..." hold (XInput is paused then), so Steam+right
        # stick moves the cursor just like the Steam+trackpad mouse latch.
        if self._gamepad is None:
            self._handle_arrow_stick(sci, steam_now, now)
            self._handle_mouse_stick(sci, now)
        else:
            # Gamepad mode: Steam/"..." + right stick moves the cursor and
            # L2/R2 click. The mouse-stick only runs during the hold (XInput is
            # paused then); the click handler runs EVERY frame so releasing
            # Steam or the trigger releases the injected mouse button.
            if steam_now:
                self._handle_mouse_stick(sci, now)
            self._handle_gamepad_mouse_clicks(sc, sci, steam_now)

        # Steam + Y → power off the controller instantly (mirrors Steam
        # Input). Latches so it only sends once per chord; the device
        # disconnects shortly after, ending sc.run() on its own.
        y_now = bool(sci.buttons & SCButtons.Y)
        if steam_now and y_now:
            if not self._powered_off:
                self._powered_off = True
                sc.turn_off()
        else:
            self._powered_off = False

        # Steam + B → force-shutdown the foreground game and its children,
        # leaving the launcher (Steam/Explorer) alive. Latches once per chord.
        b_now = bool(sci.buttons & SCButtons.B)
        if steam_now and b_now:
            if not self._force_kill_done:
                self._force_kill_done = True
                killed = _force_kill_foreground_game()
                print(f"Steam+B force-kill game: pid={killed}")
        else:
            self._force_kill_done = False

        # Passive/desktop-mode button keys (skipped in gamepad mode, where
        # these are pad buttons, and when Steam is held). All edge-triggered =
        # one keypress per press.
        if self._gamepad is None:
            # Y alone → Space (Steam+Y stays the power-off chord above).
            y_alone = y_now and not steam_now
            if y_alone and not self._y_alone_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_SPACE])
                self._chord.kb.releaseEvent([sui.Keys.KEY_SPACE])
            self._y_alone_was_pressed = y_alone

            # R4 (right upper paddle) → Page Up.
            r4_now = bool(sci.buttons & SCButtons.RGRIP1) and not steam_now
            if r4_now and not self._r4_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_PAGEUP])
                self._chord.kb.releaseEvent([sui.Keys.KEY_PAGEUP])
            self._r4_was_pressed = r4_now

            # R5 (right lower paddle) → Page Down.
            r5_now = bool(sci.buttons & SCButtons.RGRIP2) and not steam_now
            if r5_now and not self._r5_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_PAGEDOWN])
                self._chord.kb.releaseEvent([sui.Keys.KEY_PAGEDOWN])
            self._r5_was_pressed = r5_now

            # L1 / R1 (bumpers) → previous / next browser tab (Ctrl+Shift+Tab /
            # Ctrl+Tab), matching the L1/R1 = switch-tab convention on consoles.
            lb_now = bool(sci.buttons & SCButtons.LB) and not steam_now
            if lb_now and not self._lb_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
                self._chord.kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                self._chord.kb.pressEvent([sui.Keys.KEY_TAB])
                self._chord.kb.releaseEvent([sui.Keys.KEY_TAB])
                self._chord.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
                self._chord.kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
            self._lb_was_pressed = lb_now

            rb_now = bool(sci.buttons & SCButtons.RB) and not steam_now
            if rb_now and not self._rb_was_pressed:
                self._chord.kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
                self._chord.kb.pressEvent([sui.Keys.KEY_TAB])
                self._chord.kb.releaseEvent([sui.Keys.KEY_TAB])
                self._chord.kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
            self._rb_was_pressed = rb_now

            # L2 / R2 full-pull → left / right mouse click. The click itself is
            # done by firmware lizard mode (we don't inject it); we just add the
            # same haptic "click" the on-screen keyboard uses so the trigger
            # pull has a tactile snap. Rising-edge = one buzz per full pull,
            # gated by the global haptics switch.
            lt_now = bool(sci.buttons & SCButtons.LT) and not steam_now
            if lt_now and not self._lt_was_pressed and adusk_state.is_rumble_enabled("sc"):
                sc.haptic_click()
            self._lt_was_pressed = lt_now

            rt_now = bool(sci.buttons & SCButtons.RT) and not steam_now
            if rt_now and not self._rt_was_pressed and adusk_state.is_rumble_enabled("sc"):
                sc.haptic_click()
            self._rt_was_pressed = rt_now

        # L4 (left upper paddle) → hold Left Shift; L5 (left lower paddle) →
        # hold the Windows key. Held modifiers (not taps), tracked on the
        # shared chord state so a rebuild mid-hold can't strand them. The
        # release branch runs in EVERY mode (gamepad too), so switching into a
        # game while a paddle is held still drops the modifier; only the
        # engage side is gated to desktop mode.
        l4_hold = (self._gamepad is None
                   and bool(sci.buttons & SCButtons.LGRIP1) and not steam_now)
        if l4_hold and not self._chord.shift_held:
            self._chord.kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
            self._chord.shift_held = True
        elif not l4_hold and self._chord.shift_held:
            self._chord.release_shift()

        l5_hold = (self._gamepad is None
                   and bool(sci.buttons & SCButtons.LGRIP2) and not steam_now)
        if l5_hold and not self._chord.win_held:
            self._chord.kb.pressEvent([sui.Keys.KEY_LEFTMETA])
            self._chord.win_held = True
        elif not l5_hold and self._chord.win_held:
            self._chord.release_win()


class _SdlDesktopController:
    """Turns a non-Steam SDL pad (Switch Pro / Xbox / DualSense / ...) into a
    desktop mouse + keyboard — the SDL-pad equivalent of the Steam Controller's
    firmware lizard mode, which we have to synthesize because those pads have no
    firmware desktop mode. Driven from sdl_gamepad_thread while the pad isn't
    feeding a focused game (and no Steam Controller is active).

    Mapping: right stick = cursor, left stick = arrow keys (up/down/left/right),
    ZR (right trigger) = left click, ZL (left trigger) = right click, D-pad =
    arrow keys, Y = Space, L/R bumpers = Page Up / Page Down. (Physical Y opens
    the OSK, handled in sdl_gamepad_thread.) Clicks/keys are suppressed while
    Guide (Home) is held so the open-keyboard chord doesn't also fire desktop
    actions."""

    MOUSE_DEADZONE = 6000
    MOUSE_SPEED = 1400.0       # px/sec at full stick deflection
    MOUSE_EXPONENT = 1.6
    # Left stick -> arrow keys: deadzone + tap-then-repeat cadence (matches the
    # OSK's stick navigation feel).
    ARROW_DEADZONE = 14000
    # Fallback arrow auto-repeat cadence if the OS settings can't be read.
    # __init__ overrides these from the actual OS keyboard repeat rate/delay so a
    # held stick scrolls at the SAME speed as the Steam Controller (whose
    # firmware holds the key for true OS autorepeat; our injected taps don't
    # autorepeat, so we mimic the OS cadence manually).
    ARROW_HOLD_DELAY = 0.30    # first tap, then wait this long before repeating
    ARROW_REPEAT = 0.04        # repeat interval while held
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
        # X (Switch Pro physical Y) is the open-keyboard button, so it's NOT a
        # desktop key tap — otherwise opening would also fire a Backspace.
        # L / R (bumpers) are handled separately in update() as tab-switching
        # (Ctrl+Shift+Tab / Ctrl+Tab), not simple key taps.
    )
    # Triggers (ZR/ZL) as mouse buttons — the digital LT/RT bit engages at the
    # _TRIGGER_DIGITAL_ON threshold, so a light pull clicks. ZR = primary (left).
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
        self._mouse = sui.Mouse()
        self._kb = sui.Keyboard()
        # Callable that force-shutdowns the foreground game (Home+B), or None.
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
        # Match the OS keyboard auto-repeat, then slow it to the Steam
        # Controller's measured scroll speed: on the same page the SC took 20s
        # to reach the bottom vs the Switch Pro's 14s, so stretch the repeat
        # interval by 20/14 (the SC's firmware-held key autorepeats slightly
        # slower than the raw OS rate our injected taps hit).
        self._arrow_hold_delay, self._arrow_repeat = self._os_key_repeat()
        # 20/14 matches the Steam Controller's measured speed; /0.7 then makes
        # both controllers 30% slower, *1.1 another 10% slower (user-tuned).
        self._arrow_repeat *= (20.0 / 14.0) / 0.7 * 1.1

    @staticmethod
    def _os_key_repeat():
        """(hold_delay, repeat_interval) in seconds from the Windows keyboard
        settings. SPI_GETKEYBOARDDELAY 0..3 -> 250..1000 ms; SPI_GETKEYBOARDSPEED
        0..31 -> ~2.5..30 repeats/sec. Falls back to the class defaults."""
        try:
            u = ctypes.windll.user32
            speed = ctypes.c_int(0)
            delay = ctypes.c_int(0)
            u.SystemParametersInfoW(0x000A, 0, ctypes.byref(speed), 0)  # GETKEYBOARDSPEED
            u.SystemParametersInfoW(0x0016, 0, ctypes.byref(delay), 0)  # GETKEYBOARDDELAY
            rps = 2.5 + (max(0, min(31, speed.value)) / 31.0) * (30.0 - 2.5)
            return (max(0, min(3, delay.value)) + 1) * 0.25, 1.0 / rps
        except Exception:
            return _SdlDesktopController.ARROW_HOLD_DELAY, _SdlDesktopController.ARROW_REPEAT

    def reset(self):
        """Release any held button and clear edge/accumulator state, so a
        handoff (OSK open, gamepad mode, pad unplug) never strands a click down
        or fires a stale edge."""
        for btn in list(self._down):
            self._mouse.release(btn)
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

        # Left stick -> arrow keys (one tap on deflection, then auto-repeat while
        # held; dominant axis wins). The desktop equivalent of the D-pad arrows.
        self._update_arrow_stick(sci.lstick_x, sci.lstick_y, now, steam_held)

        rising = b & ~self._prev
        falling = ~b & self._prev

        # Mouse clicks: press/release so a held button drag-selects. Never START
        # a click while Guide is held (that's the open chord), but always release
        # one already down.
        for bit, name in self._CLICKS:
            if (rising & bit) and not steam_held:
                self._mouse.press(name)
                self._down.add(name)
            elif (falling & bit) and name in self._down:
                self._mouse.release(name)
                self._down.discard(name)

        # Edge-triggered key taps, suppressed while Guide is held so chords
        # (Guide+X = open keyboard) don't leak desktop keys.
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
                self._mouse.press("middle")
                self._mouse.release("middle")

        # Remember this frame's buttons for next frame's rising/falling edges.
        # (Without this, clicks would re-press every frame and never release.)
        self._prev = b

    def update_mouse_only(self, sci, now):
        """GAMEPAD-mode Home-hold: right stick = cursor, ZR/ZL = left/right
        mouse click — and nothing else (no arrow keys, no key taps). Mirrors the
        Steam Controller's Steam-hold mouse behavior in gamepad mode. The caller
        pauses the ViGEm pad while Home is held; call reset() when leaving this
        mode to release any still-held click."""
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
        # ZR/ZL -> left/right click (press/release for drag). NOT gated on Guide
        # here — the Home-hold is what activated this mouse mode.
        rising = b & ~self._prev
        falling = ~b & self._prev
        for bit, name in self._CLICKS:
            if rising & bit:
                self._mouse.press(name)
                self._down.add(name)
            elif (falling & bit) and name in self._down:
                self._mouse.release(name)
                self._down.discard(name)
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
            # New direction always steps once, then waits before repeating.
            fire = zone != "NEUTRAL"
            self._arrow_repeat_at = now + self._arrow_hold_delay
        elif zone != "NEUTRAL" and now >= self._arrow_repeat_at:
            fire = True
            self._arrow_repeat_at = now + self._arrow_repeat
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
        foreground game. Edge-triggered so one press/deflection = one action."""
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

        # Home + A → force-shutdown the foreground game (once per hold). The
        # Switch Pro's "A" button is the RIGHT face button = SDL EAST = SCButtons.B.
        if steam_held and (b & SCButtons.B):
            if not self._force_kill_done:
                self._force_kill_done = True
                if self._force_kill is not None:
                    try:
                        self._force_kill()
                    except Exception:
                        pass  # no console on the --windowed build to print to
        else:
            self._force_kill_done = False


# --- App orchestration ------------------------------------------------------

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
        # Push the current startup setting into the registry so the on-disk
        # state matches the user's saved preference.
        _apply_startup_registry(self.settings["start_with_windows"])
        # Publish the per-controller haptics switches to the shared runtime flags
        # all haptic paths (UI ticks + gamepad/desktop rumble) read.
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
        # Publish the OSK window size so screen.Screen() builds the cached
        # window (below) at the right dimensions.
        adusk_screen.set_osk_size(self.settings.get("osk_size", "medium"))
        # True once a size change is saved while the OSK is open — the cached
        # Screen can't be rebuilt while adusk.main() is using it, so
        # launcher_thread rebuilds it right after that run finishes.
        self._pending_size_change = False
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
        # Sync "Block SteamInput Xbox Controller grab" to the user env var so a
        # Steam started this session honors it — and a stale entry from a previous
        # run with it ON is cleared when it's now off. See _set_xbox_ignore.
        _set_xbox_ignore(self.settings.get("block_gamepad_takeover", False))
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
        # Set when Steam is running AND the user opted into pausing for Steam.
        self._steam_active = threading.Event()
        # Wake events so the background threads can BLOCK (zero polling) while
        # their feature is inactive instead of waking on a timer. A tray-menu
        # toggle (or shutdown) sets the relevant event to wake the thread.
        self._auto_gamepad_wake = threading.Event()
        self._steam_watch_wake = threading.Event()
        # Set by _kick_sc() so launcher_thread can tell a deliberate kick (mode
        # toggle / auto focus change) from an unexpected device drop: a kick
        # should rebuild immediately, while a drop keeps the reconnect backoff.
        # Without this, the 1s backoff also delayed the post-switch mode chime.
        self._intentional_kick = threading.Event()
        self._current_sc = None
        # Ctrl+Alt+K hotkey support: _open_kbd_event asks launcher_thread to
        # open the on-screen keyboard (so people without a controller can try
        # it); _launcher_wake wakes the launcher out of its reconnect backoff
        # so the request is honored promptly even with no controller attached;
        # _kbd_open tracks whether adusk_app.main() is currently running.
        self._open_kbd_event = threading.Event()
        self._launcher_wake = threading.Event()
        self._kbd_open = False
        # Window to restore focus to after an SDL-pad / hotkey OSK open (the
        # Steam Controller path uses the watcher's own capture instead).
        self._pending_restore_hwnd = None
        # Controller family ("sdl"/None) that requested the pending OSK open via
        # toggle_keyboard_hotkey, so the launcher can start the OSK on that
        # controller's glyphs (a Steam Controller Steam+X open is detected
        # separately via watcher.triggered). None = a non-controller open (tray
        # menu / Ctrl+Alt+K) — leave the glyphs on the last-used controller.
        self._pending_open_controller = None
        self._hotkey_listener = None
        # Set by auto_gamepad_thread to the PID of the detected game while
        # auto gamepad mode has it latched on; None when no game is active.
        self._auto_gamepad_pid = None
        # True iff the latched game (or one of its descendants) currently
        # owns the foreground window. Gates whether we push XInput frames:
        # game in focus → gamepad active; game backgrounded → lizard mode so
        # the controller works as mouse/kb on the desktop / in Discord / etc.
        self._auto_gamepad_focused = False
        # Long-lived ViGEm virtual pad. Kept alive while either gamepad_mode
        # or auto_gamepad_mode is on, so games enumerate it at *their* startup
        # rather than missing it if we create it after the game has launched.
        # Lifecycle is owned by launcher_thread (single-writer).
        self._persistent_gamepad = None
        # Automatic multiplayer: one dedicated ViGEm pad per ADDITIONAL SDL
        # controller, keyed by SDL instance id (the FIRST controller reuses
        # _persistent_gamepad as player 1, so a lone pad never spawns a phantom
        # 2nd device). Owned by sdl_gamepad_thread (single-writer); empty unless
        # 2+ controllers are live in gamepad mode.
        self._sdl_gamepads = {}
        # SDL instance id of the pad currently reusing _persistent_gamepad as
        # player 1 (None when a Steam Controller owns it, or no SDL pad is live).
        self._primary_sdl_jid = None
        # SDL3 gamepad backend for non-Steam pads (Xbox/DualSense/Switch/...).
        # The tray owns a persistent SDL_INIT_GAMEPAD (the OSK borrows it via
        # SDL_InitSubSystem so it survives keyboard open/close). sdl_gamepad_thread
        # polls _sdl_source to open the OSK (Guide+X) and feed ViGEm. Stays None
        # if SDL init fails — the Steam Controller path is wholly unaffected.
        self._sdl_source = None
        # Process gamepad input even when no SDL window is focused — the OSK
        # window is NOACTIVATE, and without this SDL drops all pad events while
        # it's open (every SDL pad reads all-zero). Set before the GAMEPAD init.
        try:
            S.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
            # Keep SDL's HIDAPI driver off the Steam Controller. We drive the SC
            # entirely through our own steamcontroller HID backend (never as an
            # SDL gamepad), but SDL3 (unlike SDL2) recognizes the Triton PIDs
            # 0x1304/0x1302 and, on GAMEPAD init, opens a *shared* handle on the
            # device. That shared handle makes our exclusive CreateFileW
            # (dwShareMode=0) fail with ERROR_SHARING_VIOLATION, silently
            # breaking "Block SteamInput Steam Controller grab" (block_sc_hid)
            # — it would just fall back to shared and do nothing. Disabling the
            # Steam HIDAPI driver leaves the device free for our exclusive open
            # while keeping SDL's other pad drivers (Xbox/Switch/PlayStation).
            S.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_STEAM", b"0")
        except Exception:
            pass
        try:
            if S.SDL_Init(S.SDL_INIT_GAMEPAD | S.SDL_INIT_VIDEO | S.SDL_INIT_EVENTS):
                self._sdl_source = adusk_inputsrc.Sdl3GamepadSource()
                S.TTF_Init()
            else:
                print(f"SDL init failed: {S.get_error()}")
        except Exception as e:
            print(f"SDL backend unavailable: {e!r}")
        # Hand the source to adusk so its main loop can poll the pad on the SDL
        # event-pump thread while the OSK is open (see state._sdl_source).
        adusk_state.set_sdl_source(self._sdl_source)
        # Pre-build the OSK Screen once at startup (loads 6 TTF fonts + creates
        # SDL window/renderer). adusk_app.main() reuses this on every open
        # instead of rebuilding from scratch — cuts open latency from ~300ms to
        # near-zero (just show the already-built hidden window). None if SDL
        # VIDEO unavailable or Screen construction fails.
        self._cached_screen = None
        try:
            self._cached_screen = adusk_screen.Screen()
            from adusk import adusk as _adusk_mod
            _adusk_mod._make_window_non_activating(self._cached_screen.window)
        except Exception as e:
            print(f"Screen pre-warm failed (will build on first open): {e!r}")
        # True while launcher_thread wants real XInput output (gamepad mode on,
        # or auto-mode game focused); gates SDL->ViGEm feeding in the SDL thread.
        self._gamepad_active = False
        # Chord state shared across every _Watcher rebuild so an in-progress
        # Steam+VIEW=Alt+Tab doesn't lose track of held keys when sc.run()
        # is kicked mid-chord (e.g. by auto-gamepad-detect on focus change).
        self._chord = _ChordState()
        # Last (large, small) rumble we forwarded to the controller, so the
        # ViGEm force-feedback callback only writes when it actually changes.
        self._last_rumble = (None, None)
        # Last seen gamepad<->lizard state (the real "gamepad active" flag, not
        # just the selected mode). The on/off chime fires on every transition:
        # menu toggles AND auto-mode game focus changes both flip it. None until
        # launcher_thread seeds it on its first loop, so startup is silent.
        self._chime_prev_active = None
        # Battery status (see battery_thread). _battery is the last
        # SteamControllerBattery polled from the live SteamController, or None
        # until one streams a power report. _battery_label is the cached menu /
        # tooltip text. _low_warned_at is the lowest low-battery band (20/10/5)
        # we've already toasted at this discharge cycle, so each band warns once;
        # it resets when the pack charges or recovers above the hysteresis line.
        # _charge_complete_notified latches the "fully charged" toast.
        # _was_charging tracks the charge state across polls so we can toast the
        # discharging→charging edge (the "plugged in" notification).
        self._battery = None
        self._battery_label = None
        self._low_warned_at = None
        self._charge_complete_notified = False
        # Latched True once a Steam Controller is ever detected this session, so
        # the "Steam Controller" tray menu stays visible the whole session (see
        # is_sc_connected). Set by battery_thread and is_sc_connected.
        self._sc_ever_connected = False
        # Same latch for a Nintendo Switch Pro / SDL pad — set in
        # sdl_gamepad_thread when a pad frame is read; gates the "Nintendo Switch
        # Controller" tray submenu. See is_switch_connected.
        self._switch_ever_connected = False
        self._was_charging = False

    # tray menu state predicates --------------------------------------------

    def is_start_with_windows_checked(self, item):
        return self.settings["start_with_windows"]

    def is_disable_while_steam_checked(self, item):
        return self.settings["disable_while_steam_running"]

    def is_exit_on_steam_checked(self, item):
        return self.settings["exit_on_steam_launch"]

    def is_gamepad_mode_checked(self, item):
        return self.settings["gamepad_mode"]

    def is_auto_gamepad_mode_checked(self, item):
        return self.settings["auto_gamepad_mode"]

    def is_gamepad_off_checked(self, item):
        # "Off" reflects the absence of either gamepad mode being enabled.
        return (not self.settings["gamepad_mode"]
                and not self.settings["auto_gamepad_mode"])

    def is_sc_rumble_checked(self, item):
        return self.settings["rumble_enabled_sc"]

    def is_switch_rumble_checked(self, item):
        return self.settings["rumble_enabled_switch"]

    def is_block_sc_hid_checked(self, item):
        return self.settings["block_sc_hid"]

    def is_block_gamepad_takeover_checked(self, item):
        return self.settings["block_gamepad_takeover"]

    def is_debug_unlocked(self, item):
        """Visibility callback for the hidden Debug submenu."""
        return self.settings["debug_menu_unlocked"]

    def toggle_debug_menu(self, icon, item):
        self.settings["debug_menu_unlocked"] = not item.checked
        _save_settings(self.settings)

    def _persist_active_controller(self, kind):
        """Save the controller (Steam Controller "sc" / SDL pad "sdl") last used
        on the OSK so its Shift/Enter glyphs persist across restarts. Called by
        adusk.state only when the active controller actually changes (on the
        input thread), so writes are rare. No menu refresh — this is invisible
        to the tray UI; it only affects which glyphs the keyboard draws."""
        if kind not in ("sc", "sdl") or self.settings.get("last_osk_controller") == kind:
            return
        self.settings["last_osk_controller"] = kind
        _save_settings(self.settings)

    # Skin submenu: one radio item per bundled skin. pystray needs a distinct
    # checked-predicate and action per name, so we build small closures.
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
            self._refresh_menu()
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
            self._refresh_menu()
        return _select

    # OSK size (Keyboard Skin → Size submenu): "small" / "medium" (default) /
    # "full" (fills the display - good for a Steam Deck). Unlike skin/
    # transparency this changes the window's pixel size and font sizes, which
    # are baked in at Screen() construction time, so it needs the cached
    # Screen rebuilt (see _rebuild_cached_screen).
    def is_osk_size_checked(self, name):
        return lambda item: self.settings.get("osk_size", "medium") == name

    def select_osk_size(self, name):
        def _select(icon, item):
            self.settings["osk_size"] = name
            _save_settings(self.settings)
            adusk_screen.set_osk_size(name)
            if self._kbd_open:
                # adusk.main() is using _cached_screen on launcher_thread right
                # now — rebuild it once that run finishes (see toggle_keyboard_hotkey).
                self._pending_size_change = True
            else:
                self._rebuild_cached_screen()
            self._refresh_menu()
        return _select

    def _rebuild_cached_screen(self):
        """Destroy and recreate the cached OSK Screen so a new "Size" setting
        takes effect on the next open. Only safe while the OSK is closed (the
        cached Screen isn't being used by adusk.main() on launcher_thread)."""
        if self._cached_screen is None:
            return
        try:
            S.SDL_DestroyRenderer(self._cached_screen.renderer)
            S.SDL_DestroyWindow(self._cached_screen.window)
        except Exception:
            pass
        try:
            self._cached_screen = adusk_screen.Screen()
            from adusk import adusk as _adusk_mod
            _adusk_mod._make_window_non_activating(self._cached_screen.window)
        except Exception as e:
            print(f"Screen rebuild failed: {e!r}")
            self._cached_screen = None

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
        self.settings["switch_left_stick_nav"] = not item.checked
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

    # tray menu actions -----------------------------------------------------

    def toggle_start_with_windows(self, icon, item):
        self.settings["start_with_windows"] = not item.checked
        _save_settings(self.settings)
        _apply_startup_registry(self.settings["start_with_windows"])

    def toggle_block_sc_hid(self, icon, item):
        self.settings["block_sc_hid"] = not item.checked
        _save_settings(self.settings)
        self._kick_sc()

    def toggle_block_gamepad_takeover(self, icon, item):
        # "Block SteamInput Xbox Controller grab" — hide the virtual ViGEm Xbox
        # 360 pad from Steam (see _set_xbox_ignore). Independent of block_sc_hid;
        # takes effect the next time Steam is launched, so no SC kick is needed.
        self.settings["block_gamepad_takeover"] = not item.checked
        _save_settings(self.settings)
        _set_xbox_ignore(self.settings["block_gamepad_takeover"])

    def toggle_sc_rumble(self, icon, item):
        # Steam Controller haptics — gates its OSK ticks, desktop/gamepad rumble.
        self.settings["rumble_enabled_sc"] = not item.checked
        _save_settings(self.settings)
        adusk_state.set_rumble_enabled("sc", self.settings["rumble_enabled_sc"])
        # Turning it off mid-rumble: stop any SC motors currently playing.
        if not self.settings["rumble_enabled_sc"]:
            self._last_rumble = (None, None)
            sc = self._current_sc
            if sc is not None:
                try:
                    sc.set_rumble(0, 0)
                except Exception:
                    pass

    def toggle_switch_rumble(self, icon, item):
        # Nintendo Switch (SDL pad) haptics — gates its OSK ticks + rumble pulses.
        self.settings["rumble_enabled_switch"] = not item.checked
        _save_settings(self.settings)
        adusk_state.set_rumble_enabled("sdl", self.settings["rumble_enabled_switch"])

    def toggle_disable_while_steam(self, icon, item):
        self.settings["disable_while_steam_running"] = not item.checked
        # Mutually exclusive with "Exit on Steam Launch" — only one at a time.
        if self.settings["disable_while_steam_running"]:
            self.settings["exit_on_steam_launch"] = False
        _save_settings(self.settings)
        # If the user just turned it off, clear the pause flag so the listener
        # resumes immediately even if Steam is still running.
        if not self.settings["disable_while_steam_running"]:
            self._steam_active.clear()
        # Wake the steam-watch thread so it re-evaluates whether to poll/idle.
        self._steam_watch_wake.set()

    def toggle_exit_on_steam(self, icon, item):
        self.settings["exit_on_steam_launch"] = not item.checked
        # Mutually exclusive with "Disable While Steam Is Running" — turning
        # this on forces that off (so the listener isn't left paused).
        if self.settings["exit_on_steam_launch"]:
            self.settings["disable_while_steam_running"] = False
            self._steam_active.clear()
        _save_settings(self.settings)
        self._steam_watch_wake.set()

    def toggle_gamepad_mode(self, icon, item):
        # Mutually exclusive with auto mode: turning Always-On on forces
        # Auto-enable off (and drops any latched game), so the two options
        # behave like radio buttons.
        self.settings["gamepad_mode"] = not item.checked
        if self.settings["gamepad_mode"]:
            self.settings["auto_gamepad_mode"] = False
            if self._auto_gamepad_pid is not None:
                self._auto_gamepad_pid = None
                self._auto_gamepad_focused = False
        _save_settings(self.settings)
        # Kick the current SC loop so the launcher thread picks up the new
        # mode immediately instead of waiting for the next chord event.
        self._kick_sc()
        # Wake the (now idle) auto-gamepad thread so it re-evaluates.
        self._auto_gamepad_wake.set()

    def toggle_auto_gamepad_mode(self, icon, item):
        # Mutually exclusive with manual mode: turning Auto-enable on forces
        # Always-On off.
        self.settings["auto_gamepad_mode"] = not item.checked
        if self.settings["auto_gamepad_mode"]:
            self.settings["gamepad_mode"] = False
        _save_settings(self.settings)
        # If the user just turned auto mode off, drop any latched game
        # immediately so the launcher reverts to the manual setting.
        if not self.settings["auto_gamepad_mode"] and self._auto_gamepad_pid is not None:
            self._auto_gamepad_pid = None
            self._auto_gamepad_focused = False
        self._kick_sc()
        # Wake the auto-gamepad thread so it starts scanning (or idles) now.
        self._auto_gamepad_wake.set()

    def select_gamepad_off(self, icon, item):
        # Third radio option: disable both gamepad paths. No-op if already
        # off (clicking the checked item shouldn't toggle anything on).
        if not self.settings["gamepad_mode"] and not self.settings["auto_gamepad_mode"]:
            return
        self.settings["gamepad_mode"] = False
        self.settings["auto_gamepad_mode"] = False
        _save_settings(self.settings)
        if self._auto_gamepad_pid is not None:
            self._auto_gamepad_pid = None
            self._auto_gamepad_focused = False
        self._kick_sc()
        # Wake the auto-gamepad thread so it idles immediately.
        self._auto_gamepad_wake.set()

    def _start_chime(self, sc, on):
        """Play the gamepad on/off chime on `sc` in a daemon thread once it's
        live. The launcher caller is about to block in sc.run(), which is what
        actually opens the device (~1s later — see the rebuild-latency note),
        so we wait for sc.is_live() rather than playing on a not-yet-open handle
        (the bug that first made the chime silent). Gated by the global haptics
        switch. Logging is opt-in via ADUSK_GAMEPAD_DEBUG."""
        if not adusk_state.is_rumble_enabled("sc"):
            _chime_log(f"chime(on={on}) skipped: haptics switch off")
            return

        def _worker():
            for i in range(250):  # up to ~5s (250 * 20ms)
                if self._stop_event.is_set():
                    return
                if sc.is_live():
                    _chime_log(f"chime(on={on}): device live after {i*20}ms, playing")
                    try:
                        sc.play_chime(on)
                    except Exception as e:
                        _chime_log(f"chime(on={on}): play_chime raised: {e!r}")
                    return
                time.sleep(0.02)
            _chime_log(f"chime(on={on}): gave up, device never opened (~5s)")

        threading.Thread(target=_worker, daemon=True).start()

    def _kick_sc(self):
        """Force the current SteamController loop to exit so launcher_thread
        re-evaluates settings (gamepad mode, auto-detected game state). Flags
        the exit as intentional so the launcher rebuilds immediately instead of
        applying the reconnect backoff (which otherwise delays the mode chime)."""
        self._intentional_kick.set()
        if self._current_sc is not None:
            try:
                self._current_sc.addExit()
            except Exception:
                pass
        # Also wake the launcher out of its reconnect-backoff sleep. With NO
        # Steam Controller present (e.g. only a Switch Pro), there's no sc.run()
        # to break, so without this the launcher wouldn't recompute
        # _gamepad_active until the backoff (up to 5s) expired — making auto
        # gamepad mode lag badly for SDL pads. The SDL thread reads
        # _gamepad_active, so this makes its mode switch as instant as the SC's.
        self._launcher_wake.set()

    def _ensure_persistent_gamepad(self):
        """Construct the ViGEm virtual pad if it doesn't already exist.
        Sets self._persistent_gamepad to None on failure."""
        if self._persistent_gamepad is not None:
            return
        try:
            self._persistent_gamepad = VirtualGamepad()
            # Forward game force-feedback to the physical rumble motors.
            self._persistent_gamepad.register_rumble(self._on_game_rumble)
        except ViGEmUnavailable as e:
            print(f"gamepad requested but unavailable: {e}")
            self._persistent_gamepad = None

    def _on_game_rumble(self, large, small):
        """ViGEm force-feedback callback for the PERSISTENT pad (player 1).
        Forward the game's large/small motor intensities (0..255) to whichever
        physical controller currently owns that pad: the live Steam Controller,
        or — when no SC is live — the primary SDL pad (the first controller,
        which reuses the persistent pad). Each ADDITIONAL SDL pad has its own
        virtual pad with its own rumble callback, so players never cross-buzz.
        Runs on a ViGEm thread; dedups so we only write when the value changes."""
        vals = (int(large), int(small))
        sc = self._current_sc
        if sc is not None and sc.is_live():
            if not adusk_state.is_rumble_enabled("sc"):
                # Global SC haptics off — drop FFB, re-apply on the next change.
                self._last_rumble = (None, None)
                return
            if vals == self._last_rumble:
                return
            self._last_rumble = vals
            sc.set_rumble(vals[0], vals[1])
            return
        # No live SC → the persistent pad is the primary SDL controller's slot;
        # rumble only that one physical pad (by its SDL instance id).
        if not adusk_state.is_rumble_enabled("switch"):
            self._last_rumble = (None, None)
            return
        if vals == self._last_rumble:
            return
        self._last_rumble = vals
        src = self._sdl_source
        jid = self._primary_sdl_jid
        if src is not None and jid is not None:
            try:
                src.set_rumble_pad(jid, vals[0], vals[1])
            except Exception:
                pass

    def _close_persistent_gamepad(self):
        pad = self._persistent_gamepad
        self._persistent_gamepad = None
        if pad is not None:
            try:
                pad.close()
            except Exception:
                pass

    # --- Automatic multiplayer: one dedicated virtual pad per SDL controller -
    #
    # All owned by sdl_gamepad_thread (single-writer), so no lock is needed on
    # self._sdl_gamepads. Rumble callbacks run on ViGEm threads but only call
    # back into SDL rumble (defensive / thread-safe enough). Active whenever
    # gamepad output is live and a 2nd+ controller is present (the first reuses
    # the persistent pad); otherwise the pool stays empty.

    def _ensure_sdl_gamepad(self, jid):
        """Get/create the dedicated ViGEm pad for SDL instance `jid`, wiring its
        game force-feedback back to that ONE physical controller. Returns the
        pad, or None if ViGEm is unavailable."""
        pad = self._sdl_gamepads.get(jid)
        if pad is not None:
            return pad
        try:
            pad = VirtualGamepad()
        except ViGEmUnavailable as e:
            print(f"separate-xinput pad for {jid} unavailable: {e}")
            return None
        # Route THIS pad's force-feedback to only this physical pad (by id).
        src = self._sdl_source

        def _rumble(large, small, _jid=jid, _src=src):
            if not adusk_state.is_rumble_enabled("switch"):
                return
            if _src is not None:
                try:
                    _src.set_rumble_pad(_jid, large, small)
                except Exception:
                    pass

        try:
            pad.register_rumble(_rumble)
        except Exception:
            pass
        self._sdl_gamepads[jid] = pad
        return pad

    def _close_sdl_gamepads(self):
        """Free every per-controller SDL pad (multiplayer mode off / paused)."""
        pads = self._sdl_gamepads
        self._sdl_gamepads = {}
        for pad in pads.values():
            try:
                pad.close()
            except Exception:
                pass

    def _reset_sdl_gamepads(self):
        """Zero every per-controller SDL pad WITHOUT freeing it (e.g. while the
        OSK temporarily owns the pad) so no input sticks, then they resume."""
        for pad in list(self._sdl_gamepads.values()):
            try:
                pad.reset()
            except Exception:
                pass

    def _feed_sdl_gamepads(self, frames, sc_live):
        """Automatic multiplayer: drive one XInput pad per connected SDL
        controller from the given per-pad frames. The FIRST controller to appear
        while no Steam Controller owns the persistent pad inherits it as player 1
        (so a lone controller never spawns a 2nd phantom device); every other
        controller gets its OWN dedicated pad, created on connect and freed on
        disconnect — any number, any mix. A pad whose OWN Home/"..." is held is
        driving the desktop (mouse/chords), so its XInput output is paused: Home
        never leaks through as the Guide button and the held sticks stay out of
        that game.

        Pad assignment is STICKY: a controller keeps whatever virtual device it
        already has and is NEVER reshuffled by a transient change in `sc_live`.
        That is what stops the XInput pad disconnecting/reconnecting every time
        the OSK is toggled — opening the OSK kicks the Steam Controller and it
        takes ~1 s to rebuild, during which sc_live briefly reads False; an
        already-assigned SDL pad must NOT grab the persistent pad in that gap and
        then hand it straight back. Only a genuine SC connect migrates a pad."""
        _HOME = SCButtons.STEAM | SCButtons.QAM
        # A live Steam Controller owns the persistent pad (player 1, fed by the
        # launcher). If an SDL pad had been using it as player 1, give it its OWN
        # pad instead — a one-time migration on a genuine SC connect. (An OSK
        # toggle never triggers this mid-rebuild: the thread cedes the pad while
        # _kbd_open, so sc_live only reads True here once the SC is fully back.)
        if sc_live and self._primary_sdl_jid is not None:
            self._primary_sdl_jid = None
        primary = self._primary_sdl_jid
        # If the player-1 SDL pad disconnected, release the slot so the next pad
        # to appear can inherit it.
        if primary is not None and primary not in frames:
            primary = None
            self._primary_sdl_jid = None
        # Free dedicated pads whose controller disconnected.
        for jid in list(self._sdl_gamepads):
            if jid not in frames:
                pad = self._sdl_gamepads.pop(jid)
                try:
                    pad.close()
                except Exception:
                    pass
        # Feed each live controller. STICKY: keep the pad it already owns; only a
        # brand-new controller is assigned (the free persistent pad if available,
        # else its own). Pause whichever pad is holding its Home.
        for jid, f in frames.items():
            if jid == primary:
                pad = self._persistent_gamepad
            elif jid in self._sdl_gamepads:
                pad = self._sdl_gamepads[jid]
            elif (primary is None and not sc_live
                    and self._persistent_gamepad is not None):
                # Persistent pad is free → this new controller becomes player 1
                # (so a lone pad doesn't spawn a 2nd phantom XInput device).
                primary = jid
                self._primary_sdl_jid = jid
                pad = self._persistent_gamepad
            else:
                pad = self._ensure_sdl_gamepad(jid)
            if pad is None:
                continue
            if f.buttons & _HOME:
                try:
                    pad.reset()
                except Exception:
                    pass
            else:
                try:
                    pad.update(f)
                except Exception as e:
                    print(f"sdl gamepad update failed for {jid}: {e!r}")

    def sdl_gamepad_thread(self):
        """Poll SDL-recognized pads (Xbox/DualSense/Switch Pro/8BitDo/...) so a
        non-Steam controller can (a) open the OSK with Guide+X, (b) feed the
        ViGEm virtual pad in gamepad mode, and (c) act as a desktop mouse/keyboard
        otherwise (the synthesized equivalent of the Steam Controller's firmware
        lizard mode). The Steam Controller is handled by launcher_thread and is
        excluded by Sdl3GamepadSource (name match), so the two never fight.
        Defensive throughout — any error here must never take down the tray."""
        src = self._sdl_source
        if src is None:
            return
        guide_x_prev = False
        # force_kill = Home+B → force-shutdown the foreground game and its
        # children (the SDL-pad equivalent of the SC's Steam+B chord).
        desktop = _SdlDesktopController(force_kill=_force_kill_foreground_game)
        _was_kbd_open = False
        _osk_close_time = 0.0   # monotonic time of last OSK close (for debounce)
        _OSK_REOPEN_COOLDOWN = 0.4  # seconds to ignore Y presses after OSK closes
        _ga_prev = None              # last gamepad-mode state (for the toggle rumble)
        _steam_kill_prev = False     # Home+face edge while Steam-ceded (force-kill)
        while not self._stop_event.is_set():
            # Paused for Steam (disable_while_steam_running + Steam up): let
            # Steam own the controllers. Don't inject desktop kb/mouse or feed
            # ViGEm from the SDL pad — the launcher pauses the Steam Controller
            # the same way. Without this, the SDL pad kept driving desktop
            # mouse/keyboard into the Steam game.
            if self._steam_active.is_set():
                desktop.reset()
                self._close_sdl_gamepads()  # let Steam own the pads
                guide_x_prev = False
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
                if kill_now and not _steam_kill_prev:
                    try:
                        _force_kill_foreground_game()
                    except Exception:
                        pass
                _steam_kill_prev = kill_now
                self._stop_event.wait(0.05)
                continue
            _steam_kill_prev = False
            # While the OSK is open, adusk.main owns the pad: it polls it on its
            # own SDL event-pump thread and publishes frames (SDL only refreshes
            # gamepad state on the thread pumping its events, so polling here
            # would read all-zero). Cede the pad until the OSK closes.
            if self._kbd_open:
                _was_kbd_open = True
                desktop.reset()
                self._reset_sdl_gamepads()  # OSK owns the pad; no stuck input
                guide_x_prev = True  # treat Y as "held" so release doesn't re-open
                self._stop_event.wait(0.03)
                continue
            # Record the moment the OSK just closed so the cooldown can gate
            # re-opens — prevents buffered Y presses during close from firing.
            if _was_kbd_open:
                _was_kbd_open = False
                _osk_close_time = time.monotonic()
                guide_x_prev = True  # force a clean rising-edge on the next press
            # Light the controller's LED (blue) while gamepad mode is active,
            # off otherwise. set_home_led only flags the change; the SDL pump
            # applies it on this (SDL) thread. On the gamepad-mode TRANSITION,
            # buzz a two-pulse confirmation (light→strong on, strong→light off);
            # _ga_prev=None on the first pass so startup doesn't rumble.
            ga = self._gamepad_active
            if _ga_prev is not None and ga != _ga_prev:
                src.play_mode_rumble(ga)
            _ga_prev = ga
            src.set_home_led(ga)
            # ONE pump → the OR-merged frame (drives OSK-open detection) AND a
            # per-pad dict {jid: frame} (drives one dedicated XInput pad per
            # physical controller — automatic multiplayer, no toggle needed).
            try:
                sci, frames = src.poll_all()
            except Exception as e:
                print(f"sdl gamepad poll error: {e!r}")
                sci, frames = None, {}
            if sci is not None:
                # A pad frame means a Switch Pro / SDL pad is connected — latch
                # it so the "Switch Pro Controller" tray submenu appears.
                if not self._switch_ever_connected:
                    self._switch_ever_connected = True
                x = bool(sci.buttons & SCButtons.X)
                steam = bool(sci.buttons & SCButtons.STEAM)
                # In DESKTOP mode, pressing Y on its own opens the OSK (rising
                # edge). On the Switch Pro positional map physical Y = SCButtons.X.
                # In GAMEPAD mode bare Y is a face button the game needs, so the
                # OSK only opens on Steam(Home)+Y — matching the Steam Controller,
                # whose watcher likewise requires Steam+X (or QAM "..."+X) in
                # gamepad mode. Cooldown after OSK close prevents buffered Y
                # presses from re-opening immediately.
                x_opens = x and (not self._gamepad_active or steam)
                if (x_opens and not guide_x_prev and not self._kbd_open
                        and not _workstation_locked()
                        and (time.monotonic() - _osk_close_time) > _OSK_REOPEN_COOLDOWN):
                    # An SDL pad (Switch Pro) opened it → start on its glyphs.
                    self.toggle_keyboard_hotkey(opener="sdl")
                guide_x_prev = x_opens
                _sc = self._current_sc
                _sc_live = _sc is not None and _sc.is_live()
                if self._gamepad_active:
                    # Gamepad mode → automatic multiplayer: every connected SDL
                    # pad drives its OWN dedicated XInput device (the first reuses
                    # the persistent pad; see _feed_sdl_gamepads), so any number /
                    # mix of controllers each become a separate player.
                    #
                    # The single human desktop user's layer still runs, driven by
                    # whichever pad is holding its Home/"..." (its OWN frame, not
                    # the merge, so other players' sticks don't reach the cursor):
                    #   • Hold Home → mouse mode (right stick = cursor, ZR/ZL =
                    #     click) + Steam chords (media / play-pause / Alt+Tab /
                    #     force-kill on left stick + L3 + "+" + B).
                    # The pad whose Home is held has its OWN XInput output paused
                    # inside _feed_sdl_gamepads, so Home never leaks through as the
                    # Guide button and the held sticks don't reach that game.
                    _now = time.monotonic()
                    home_frame = None
                    for _f in frames.values():
                        if _f.buttons & (SCButtons.STEAM | SCButtons.QAM):
                            home_frame = _f
                            break
                    if home_frame is not None:
                        desktop.update_mouse_only(home_frame, _now)
                        desktop._handle_steam_chords(home_frame, _now, True)
                    else:
                        desktop.reset()  # release any click held during the hold
                    self._feed_sdl_gamepads(frames, _sc_live)
                else:
                    # Desktop mode: ALWAYS drive the mouse/keyboard from the SDL
                    # pad. A merely-connected Steam Controller dongle must NOT
                    # block this — gating it on the SC killed the Switch Pro
                    # mouse whenever the puck was plugged in. (If the physical
                    # Steam Controller is also actively driving its firmware
                    # lizard mouse, both move the cursor, but in practice only
                    # one controller is used at a time.)
                    if self._sdl_gamepads:
                        self._close_sdl_gamepads()  # no XInput off the desktop
                    self._primary_sdl_jid = None
                    try:
                        desktop.update(sci, time.monotonic())
                    except Exception as e:
                        print(f"sdl desktop update failed: {e!r}")
            else:
                guide_x_prev = False
                desktop.reset()
                if self._sdl_gamepads:
                    self._close_sdl_gamepads()  # all pads gone
                self._primary_sdl_jid = None
            self._stop_event.wait(0.008)  # ~125 Hz

    def exit_app(self, icon, item):
        self._stop_event.set()
        # Wake any event-idle background threads so they observe the stop.
        self._auto_gamepad_wake.set()
        self._steam_watch_wake.set()
        self._launcher_wake.set()
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        adusk_state.close()
        if self._current_sc is not None:
            try:
                self._current_sc.addExit()
            except Exception:
                pass
        # Defensive: if exit happens mid-chord, make sure we don't leave
        # Alt held at the OS level.
        try:
            self._chord.release_alt()
        except Exception:
            pass
        self._close_persistent_gamepad()
        self._close_sdl_gamepads()
        if self._sdl_source is not None:
            try:
                self._sdl_source.close()
            except Exception:
                pass
        try:
            S.SDL_Quit()
        except Exception:
            pass
        icon.stop()

    # background threads ----------------------------------------------------

    def _should_abort_sc(self):
        return self._stop_event.is_set() or self._steam_active.is_set()

    def _launcher_wait(self, timeout):
        """Backoff sleep for launcher_thread that also wakes early on a stop or
        an open-keyboard request (so Ctrl+Alt+K is responsive even when no
        controller is attached and the loop is in its reconnect backoff)."""
        self._launcher_wake.wait(timeout)
        self._launcher_wake.clear()

    def _kbd_menu_label(self, item):
        """Dynamic label for the tray's top menu item: shows the action that a
        click will perform given the keyboard's current open/closed state."""
        return "Close Keyboard" if self._kbd_open else "Open Keyboard"

    def open_or_close_keyboard(self, icon, item):
        """Tray menu: open the on-screen keyboard, or close it if it's already
        open. Shares the Ctrl+Alt+K toggle path (launcher_thread owns the
        window)."""
        self.toggle_keyboard_hotkey()

    def toggle_keyboard_hotkey(self, opener=None):
        """Ctrl+Alt+K: open the on-screen keyboard, or close it if it's open.
        Lets people without a Steam Controller preview the keyboard. Runs on the
        pynput hotkey thread, so it only signals — launcher_thread owns the
        window and actually opens/closes it. `opener` names the controller family
        requesting the open ("sdl" for an SDL pad such as the Switch Pro), so the
        launcher can start the OSK on that controller's glyphs; None for a
        non-controller open (tray menu / hotkey)."""
        if self._kbd_open:
            adusk_state.close()
            return
        self._pending_open_controller = opener
        # Remember the window the user was in so adusk can restore focus after
        # the OSK opens (SDL-pad / hotkey opens don't go through the Steam
        # Controller watcher that normally captures this). Foreground is still
        # the user's app here — an SDL pad's buttons don't inject anything.
        self._pending_restore_hwnd = _foreground_target_hwnd()
        self._open_kbd_event.set()
        self._launcher_wake.set()
        # Break the current sc.run() (if a controller is connected) so the
        # launcher loop proceeds straight to opening the keyboard.
        sc = self._current_sc
        if sc is not None:
            try:
                sc.addExit()
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
    # than a normal sc rebuild (gamepad-mode toggle / brief drop) so those don't
    # blink the line off and back on.
    _BATTERY_STALE_SECONDS = 8.0

    def is_battery_known(self, item):
        """Visibility callback for the battery menu line — hidden until the
        controller has actually reported a level."""
        return self._battery is not None

    def battery_menu_label(self, item):
        return self._battery_label or "Steam Controller: …"

    def _notify(self, title, message):
        icon = self._icon_ref
        if icon is None:
            return
        try:
            icon.notify(message, title)
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
        icon = self._icon_ref
        if icon is not None:
            try:
                icon.title = f"SteamlessKeyboard — Steam Controller {state}"
            except Exception:
                pass
        self._refresh_menu()

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

        # Fully charged: notify once per charge completion.
        if batt.charge_complete:
            if not self._charge_complete_notified:
                self._charge_complete_notified = True
                self._notify("Steam Controller fully charged",
                             "Steam Controller battery is full.")
        else:
            self._charge_complete_notified = False

        pct = batt.percent
        # On the charger (or comfortably recovered) → arm the low-battery
        # warning again for the next discharge cycle.
        if batt.charging or pct > self._LOW_BATT_RECOVER:
            self._low_warned_at = None
        if batt.charging:
            return

        band = next((b for b in self._LOW_BATT_BANDS if pct <= b), None)
        if band is None:
            return
        # Warn on the first low band hit, and again each time we drop to a
        # more-severe (lower) band — but not repeatedly within the same band.
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
        # A short haptic nudge so it's noticeable mid-game (haptics switch
        # permitting, and only if the device is still live).
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
        thread just samples it on a slow timer (battery changes slowly) so the
        gaming hot path stays untouched."""
        last_key = None
        last_seen = None
        while not self._stop_event.is_set():
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
                # unplug) wait the grace window so a gamepad-mode rebuild doesn't
                # blink the line off and back on. Reset the latches so a
                # reconnect is treated as a fresh charge cycle.
                self._battery = None
                self._battery_label = None
                last_key = None
                self._was_charging = False
                self._low_warned_at = None
                self._charge_complete_notified = False
                icon = self._icon_ref
                if icon is not None:
                    try:
                        icon.title = "SteamlessKeyboard"
                    except Exception:
                        pass
                self._refresh_menu()
            self._stop_event.wait(self._BATTERY_POLL_SECONDS)

    # How often to poll USB for the receiver / wired controller appearing.
    _DEVICE_POLL_SECONDS = 3.0

    def device_watch_thread(self):
        """Toast when the USB-C wired controller (PID 0x1302) is plugged into /
        unplugged from the PC. (The wireless receiver/puck's own USB presence
        isn't announced.) Independent of the battery poll — only enumerates HID,
        so it fires even when nothing is paired."""
        from steamcontroller import present_product_ids, PRODUCT_ID_WIRED
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

    def steam_watch_thread(self):
        last_running = False
        while not self._stop_event.is_set():
            exit_on_launch = self.settings["exit_on_steam_launch"]
            disable_while = self.settings["disable_while_steam_running"]

            if not exit_on_launch and not disable_while:
                # Neither Steam-reactive setting is enabled — make sure any
                # latched pause flag is cleared, then BLOCK (no polling) until a
                # tray toggle or shutdown wakes us. Zero wakeups while idle.
                if self._steam_active.is_set():
                    self._steam_active.clear()
                last_running = False
                self._steam_watch_wake.wait()
                self._steam_watch_wake.clear()
                continue

            running = _steam_running()
            just_started = running and not last_running

            if just_started and exit_on_launch:
                # "Exit on Steam Launch" wins over "Disable While …" — fully
                # tear down the tray app so Steam has the controller to itself.
                print("Steam detected; exiting per 'Exit on Steam Launch'.")
                self._stop_event.set()
                adusk_state.close()
                if self._current_sc is not None:
                    try:
                        self._current_sc.addExit()
                    except Exception:
                        pass
                self._exit_icon_ref()
                return

            if disable_while:
                if running and not self._steam_active.is_set():
                    # Pause the listener and close any open OSK so Steam can
                    # grab the controller for itself.
                    self._steam_active.set()
                    adusk_state.close()
                    if self._current_sc is not None:
                        try:
                            self._current_sc.addExit()
                        except Exception:
                            pass
                elif not running and self._steam_active.is_set():
                    self._steam_active.clear()

            last_running = running
            self._stop_event.wait(5.0)

    def auto_gamepad_thread(self):
        """Detect a likely-game process and latch onto it. While latched,
        poll the foreground window every 500ms so gamepad mode follows the
        game's focus state (alt-tab out → lizard mode for the desktop;
        alt-tab back → gamepad mode). When the game exits, release the
        latch. Diagnostic logging is opt-in via the ADUSK_GAMEPAD_DEBUG env
        var; without it the scan does no disk I/O."""
        try:
            import psutil
        except ImportError:
            return

        debug_enabled = bool(os.environ.get("ADUSK_GAMEPAD_DEBUG"))
        log_path = os.path.join(_exe_dir(), "auto_gamepad_debug.log")

        def _scan():
            # Detection runs unconditionally; the per-process diagnostic log is
            # written only when ADUSK_GAMEPAD_DEBUG is set, so normal desktop
            # use does no continuous disk I/O or log formatting.
            if not debug_enabled:
                return _detect_game_pid()
            try:
                if (os.path.exists(log_path)
                        and os.path.getsize(log_path) > 256 * 1024):
                    open(log_path, "w").close()
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"scan (auto-mode on, unlatched) ===\n")
                    pid = _detect_game_pid(debug_log=f)
                    f.write(f"  result: {'pid=' + str(pid) if pid else 'NO MATCH'}\n")
                    return pid
            except Exception:
                # If logging fails for any reason, fall back to silent detection
                # so the auto thread keeps working.
                return _detect_game_pid()

        while not self._stop_event.is_set():
            if not self.settings["auto_gamepad_mode"]:
                # Auto mode is off (Gamepad Off or Always-On) — nothing to scan.
                # BLOCK until a tray toggle or shutdown wakes us, so this thread
                # costs zero wakeups in those modes instead of polling every 2s.
                if self._auto_gamepad_pid is not None:
                    self._auto_gamepad_pid = None
                    self._auto_gamepad_focused = False
                    self._kick_sc()
                self._auto_gamepad_wake.wait()
                self._auto_gamepad_wake.clear()
                continue

            if self._auto_gamepad_pid is not None:
                # Latched — cheap checks at 500ms so alt-tab is responsive.
                if not psutil.pid_exists(self._auto_gamepad_pid):
                    self._auto_gamepad_pid = None
                    self._auto_gamepad_focused = False
                    self._kick_sc()
                else:
                    now_focused = _is_latched_focused(self._auto_gamepad_pid)
                    # Defer focus-change restarts while the Steam+VIEW chord
                    # is active — otherwise the alt-tab switcher stealing
                    # focus from the game would trigger a sc.run() rebuild
                    # that swallows subsequent VIEW presses (so cycling
                    # through windows stops working after the first press).
                    if (now_focused != self._auto_gamepad_focused
                            and not self._chord.alt_held):
                        self._auto_gamepad_focused = now_focused
                        self._kick_sc()
                self._stop_event.wait(0.5)
            else:
                # Unlatched — full scan (process enumeration + DLL checks)
                # is heavy, so run it at a relaxed interval.
                pid = _scan()
                if pid:
                    self._auto_gamepad_pid = pid
                    self._auto_gamepad_focused = _is_latched_focused(pid)
                    self._kick_sc()
                self._stop_event.wait(3.5)

    # Set by main() so the watch thread can stop the tray icon on Steam exit.
    _icon_ref = None

    def _exit_icon_ref(self):
        icon = self._icon_ref
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def _refresh_menu(self):
        """Rebuild the tray menu so the dynamic Open/Close Keyboard label
        re-reads _kbd_open. Called whenever _kbd_open flips on the launcher
        thread — the keyboard opens/closes asynchronously, so the rebuild
        pystray does right after a menu click happens before _kbd_open has
        actually changed and would otherwise leave the label stale."""
        icon = self._icon_ref
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass

    def launcher_thread(self):
        # Reconnect backoff: when no controller is present, opening fails fast
        # and we'd otherwise re-enumerate HID every second forever (the common
        # case — tray app running with the controller turned off). Back off up
        # to RECONNECT_WAIT_MAX, resetting the instant a controller appears.
        reconnect_wait = 1.0
        RECONNECT_WAIT_MIN = 1.0
        RECONNECT_WAIT_MAX = 5.0
        while not self._stop_event.is_set():
            # If Steam is currently running and we're configured to pause,
            # release ViGEm so Steam can present its own virtual pad, and
            # wait it out without holding the controller HID handle open.
            # steam_active can't clear faster than the 5s steam-watch poll, so
            # waiting 5s (vs 1s) here costs nothing in responsiveness but cuts
            # this thread's idle wakeups while Steam is running by 5x.
            if self._steam_active.is_set():
                self._close_persistent_gamepad()
                self._stop_event.wait(5.0)
                continue

            # Snapshot toggles for this iteration; toggle_*_mode and
            # auto_gamepad_thread both call _kick_sc() to force re-eval.
            manual_on = self.settings["gamepad_mode"]
            auto_enabled = self.settings["auto_gamepad_mode"]
            auto_latched = self._auto_gamepad_pid is not None
            auto_focused = auto_latched and self._auto_gamepad_focused

            # Keep ViGEm alive whenever the user might want gamepad output
            # any time soon, so games enumerate it at *their* startup. We
            # only push real input frames when "active" — manual on, OR
            # auto has latched a running game AND that game is focused
            # (when the game is backgrounded the controller reverts to
            # firmware mouse/kb so it's usable on the desktop).
            vg_should_live = manual_on or auto_enabled
            gamepad_active = manual_on or auto_focused
            # Published for sdl_gamepad_thread's SDL->ViGEm gate.
            self._gamepad_active = gamepad_active

            if vg_should_live:
                self._ensure_persistent_gamepad()
                if self._persistent_gamepad is None:
                    # ViGEm construction failed — fall back to non-gamepad.
                    gamepad_active = False
            else:
                self._close_persistent_gamepad()

            # Chime on the real gamepad<->lizard transition. gamepad_active is
            # the single source of truth: it flips for menu toggles (Always-On,
            # Off) AND for auto-mode game focus changes, so one check covers
            # both. The first loop just seeds the state (silent at startup);
            # the chime plays on the device built below, once it opens (~1s).
            chime_now = None
            if self._chime_prev_active is None:
                self._chime_prev_active = gamepad_active
            elif gamepad_active != self._chime_prev_active:
                self._chime_prev_active = gamepad_active
                chime_now = gamepad_active

            # Non-passive when active: lizard mode (firmware mouse/kb
            # emulation) must be off so it doesn't fight the XInput output.
            # Passive otherwise so the controller keeps working as mouse/kb
            # between Steam+X presses; the watcher is given no gamepad to
            # avoid duplicating input (xinput + lizard kb/mouse at once).
            watcher = _Watcher(
                self._should_abort_sc,
                gamepad=self._persistent_gamepad if gamepad_active else None,
                chord=self._chord,
            )
            # block_sc_hid opens the physical Steam Controller HID exclusively so
            # Steam can't read it — applied in ALL modes (desktop AND gamepad), so
            # the toggle blocks Steam from the Steam Controller on its own. (It used
            # to also require block_gamepad_takeover in gamepad mode, which surprised
            # users: unchecking the Xbox toggle re-exposed the SC to Steam.) The two
            # blocks are now independent; block_gamepad_takeover hides the VIRTUAL
            # Xbox 360 pad from Steam separately (see _set_xbox_ignore).
            use_exclusive = self.settings["block_sc_hid"]
            sc = SteamController(callback=watcher.on_input,
                                 passive=not gamepad_active,
                                 exclusive=use_exclusive)
            self._current_sc = sc
            # New device instance starts with motors off; forget the last
            # forwarded rumble so the next FFB update is always re-applied.
            self._last_rumble = (None, None)
            # If gamepad<->lizard just flipped, chime once on this device as
            # soon as it's open (a daemon waits for the open, then plays).
            if chime_now is not None:
                self._start_chime(sc, chime_now)
            try:
                sc.run()
            except KeyboardInterrupt:
                self._close_persistent_gamepad()
                return
            finally:
                self._current_sc = None

            if self._stop_event.is_set():
                return
            if self._steam_active.is_set():
                # Pause-for-Steam fired; loop back to wait state.
                continue
            # Open the keyboard on a controller Steam+X (watcher.triggered) OR
            # on a Ctrl+Alt+K hotkey request (_open_kbd_event).
            open_kbd = watcher.triggered or self._open_kbd_event.is_set()
            self._open_kbd_event.clear()
            if not open_kbd:
                # sc.run() returned without an open request. Two cases:
                if sc.opened:
                    # It opened and ran, so this was a deliberate kick (gamepad-
                    # mode toggle / focus change) or the device dropped mid-use.
                    reconnect_wait = RECONNECT_WAIT_MIN
                    if self._intentional_kick.is_set():
                        # Deliberate kick — rebuild immediately so the new mode
                        # (and its on/off chime) applies without a 1s lag.
                        self._intentional_kick.clear()
                    else:
                        # Unexpected drop mid-use — brief backoff before retry.
                        self._launcher_wait(RECONNECT_WAIT_MIN)
                else:
                    # Open failed — no controller present. Back off so we don't
                    # re-enumerate HID every second while it stays disconnected.
                    self._launcher_wait(reconnect_wait)
                    reconnect_wait = min(reconnect_wait * 2, RECONNECT_WAIT_MAX)
                continue

            # Steam+X or Ctrl+Alt+K — reset the backoff and open the keyboard.
            reconnect_wait = RECONNECT_WAIT_MIN
            # Snapshot the window the user was typing in NOW, before the HID
            # handoff — the watcher sampled it just before the opening press, so
            # adusk can restore focus to it once the OSK is up (the controller-
            # open's firmware mouse-click can otherwise leave the field unfocused).
            # Steam Controller open → the watcher's sample; SDL-pad / hotkey
            # open → the window captured in toggle_keyboard_hotkey.
            restore_hwnd = watcher._last_user_hwnd or self._pending_restore_hwnd
            self._pending_restore_hwnd = None
            # Start the OSK on the glyphs of the controller that opened it: a
            # Steam Controller Steam+X sets watcher.triggered; an SDL pad
            # (Switch Pro) tagged the pending open as "sdl". A non-controller
            # open (tray menu / Ctrl+Alt+K) leaves it on the last-used controller.
            opener = "sc" if watcher.triggered else self._pending_open_controller
            self._pending_open_controller = None
            if opener is not None:
                adusk_state.set_active_controller(opener)
            # Brief HID-handoff settle, then open the keyboard in-process.
            time.sleep(0.1)
            adusk_state.reset_session()
            adusk_state.set_focus_restore_target(restore_hwnd)
            self._kbd_open = True
            self._refresh_menu()  # label → "Close Keyboard"
            try:
                adusk_app.main(cached_screen=self._cached_screen)
            except Exception as e:
                print(f"adusk crashed: {e!r}")
            finally:
                self._kbd_open = False
                self._refresh_menu()  # label → "Open Keyboard"
                # A "Size" change was selected while the OSK was open (the
                # cached Screen was busy on this thread) — rebuild it now so
                # the new size takes effect on the next open.
                if self._pending_size_change:
                    self._pending_size_change = False
                    self._rebuild_cached_screen()
            time.sleep(0.1)


def _load_icon_image():
    # Prefer the multi-resolution app_icon.ico (hand-tuned per size, so
    # the small tray frame is crisp). Falls back to the in-OSK keyboard
    # glyph PNG if the ico isn't present.
    base = os.path.join(_bundle_dir(), "data", "images")
    try:
        small = ctypes.windll.user32.GetSystemMetrics(49)  # SM_CXSMICON
    except Exception:
        small = 16
    target = max(small * 2, 32)  # 2× for HiDPI headroom

    ico_path = os.path.join(base, "app_icon.ico")
    if os.path.isfile(ico_path):
        ico = Image.open(ico_path)
        # Pick the smallest embedded frame that's >= target so we sharpen
        # by downscaling, not upscaling, then LANCZOS to the exact size.
        sizes = sorted(ico.info.get("sizes", [ico.size]))
        pick = next((s for s in sizes if s[0] >= target), sizes[-1])
        ico.size = pick
        return ico.convert("RGBA").resize((target, target), Image.LANCZOS)

    fallback = os.path.join(base, "glyphs", "glyph_keyboard.png")
    if os.path.isfile(fallback):
        return Image.open(fallback).convert("RGBA").resize(
            (target, target), Image.LANCZOS)
    raise FileNotFoundError("no tray icon found under data/images/")


def main():
    app = App()
    image = _load_icon_image()

    gamepad_submenu = pystray.Menu(
        pystray.MenuItem(
            "Auto enable",
            app.toggle_auto_gamepad_mode,
            checked=app.is_auto_gamepad_mode_checked,
        ),
        pystray.MenuItem(
            "Always enable (Home+Stick to control mouse)",
            app.toggle_gamepad_mode,
            checked=app.is_gamepad_mode_checked,
        ),
        pystray.MenuItem(
            "Off",
            app.select_gamepad_off,
            checked=app.is_gamepad_off_checked,
        ),
    )

    debug_submenu = pystray.Menu(
        pystray.MenuItem(
            "Block SteamInput Steam Controller grab",
            app.toggle_block_sc_hid,
            checked=app.is_block_sc_hid_checked,
        ),
        pystray.MenuItem(
            "Block SteamInput Xbox Controller grab",
            app.toggle_block_gamepad_takeover,
            checked=app.is_block_gamepad_takeover_checked,
        ),
    )

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

    # Mutually-exclusive Steam-running behavior (radio-style; the toggle
    # handlers clear the other so at most one is ever on).
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
            "Start with Windows",
            app.toggle_start_with_windows,
            checked=app.is_start_with_windows_checked,
        ),
        pystray.MenuItem("When Steam Is Running", steam_running_submenu),
        pystray.MenuItem("Advanced Settings", app.toggle_debug_menu,
                         checked=app.is_debug_unlocked),
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

    menu = pystray.Menu(
        pystray.MenuItem(
            app.battery_menu_label,
            None,
            enabled=False,
            visible=app.is_battery_known,
        ),
        pystray.MenuItem(
            app._kbd_menu_label,
            app.open_or_close_keyboard,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Startup", startup_submenu),
        pystray.MenuItem("Gamepad Mode", gamepad_submenu),
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

    icon = pystray.Icon("SteamControllerKeyboard", image,
                        "SteamlessKeyboard", menu)
    app._icon_ref = icon

    def setup(icon):
        icon.visible = True
        threading.Thread(target=app.launcher_thread, daemon=True).start()
        threading.Thread(target=app.steam_watch_thread, daemon=True).start()
        threading.Thread(target=app.auto_gamepad_thread, daemon=True).start()
        threading.Thread(target=app.sdl_gamepad_thread, daemon=True).start()
        threading.Thread(target=app.battery_thread, daemon=True).start()
        threading.Thread(target=app.device_watch_thread, daemon=True).start()
        # Global Ctrl+Alt+K opens (or closes) the on-screen keyboard, so it can
        # be tried without a Steam Controller to press Steam+X.
        try:
            listener = _pynput_kb.GlobalHotKeys(
                {"<ctrl>+<alt>+k": app.toggle_keyboard_hotkey})
            listener.daemon = True
            listener.start()
            app._hotkey_listener = listener
        except Exception as e:
            print(f"hotkey listener failed to start: {e!r}")

        # Esc closes the on-screen keyboard if it's open. Not suppressed, so Esc
        # still reaches whatever window has focus as normal — this just adds the
        # OSK close as a side effect (the OSK is WS_EX_NOACTIVATE and never has
        # focus itself).
        def _on_esc_press(key):
            if key == _pynput_kb.Key.esc and app._kbd_open:
                app.toggle_keyboard_hotkey()

        try:
            esc_listener = _pynput_kb.Listener(on_press=_on_esc_press)
            esc_listener.daemon = True
            esc_listener.start()
            app._esc_listener = esc_listener
        except Exception as e:
            print(f"esc listener failed to start: {e!r}")

    try:
        icon.run(setup=setup)
    except OSError as e:
        # pystray's win32 backend can raise "[WinError 1401] Invalid menu
        # handle" while tearing down the tray menu during Exit (icon.stop()).
        # The app is already shutting down, so swallow that specific error to
        # avoid a spurious PyInstaller crash dialog; re-raise anything else.
        if getattr(e, "winerror", None) != 1401:
            raise


if __name__ == "__main__":
    main()
