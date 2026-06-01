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

# Point PySDL2 at the SDL2 DLLs bundled into the EXE; without this, sdl2.dll
# searches the system PATH and fails inside a PyInstaller --onefile build.
if _is_frozen():
    _sdl_dll_dir = os.path.join(_bundle_dir(), "sdl2dll", "dll")
    if os.path.isdir(_sdl_dll_dir):
        os.environ["PYSDL2_DLL_PATH"] = _sdl_dll_dir


import pystray  # noqa: E402
from PIL import Image  # noqa: E402
from pynput import keyboard as _pynput_kb  # noqa: E402

from steamcontroller import SteamController, SCButtons, SCStatus  # noqa: E402
from steamcontroller import uinput as sui  # noqa: E402
from steamcontroller.gamepad import VirtualGamepad, ViGEmUnavailable  # noqa: E402
from adusk import adusk as adusk_app  # noqa: E402
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
    # Global haptics switch: gates BOTH the on-screen-keyboard click feedback
    # and the gamepad-mode game rumble. Off = no haptics in any mode.
    "rumble_enabled": True,
    # Debug: open the Steam Controller HID exclusively so Steam can't read the
    # physical controller (no Steam Input / forced lizard while we hold it).
    # Must be enabled before Steam opens the controller to win the grab.
    "block_sc_hid": False,
    # Debug: apply block_sc_hid even while gamepad mode is active. When off
    # (default), HID exclusivity is dropped during gamepad mode so Steam Input
    # can still configure controllers for Steam games alongside our ViGEm output.
    "block_gamepad_takeover": False,
    # When False the Debug submenu is hidden; toggle the Rumble/Haptics item
    # this many times in a row to reveal it.
    "debug_menu_unlocked": False,
}

# Consecutive Rumble/Haptics toggles needed to show/hide the Debug submenu.
# Even, so the haptics setting nets back to its starting value after a burst.
_DEBUG_UNLOCK_CLICKS = 4
# Presses more than this many seconds apart restart the count (so it takes a
# deliberate burst, but still leaves time to reopen the tray menu between).
_DEBUG_UNLOCK_WINDOW = 5.0

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
    pid = _foreground_game_pid()
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
        merged.update({k: bool(v) for k, v in data.items() if k in DEFAULT_SETTINGS})
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    # Gamepad mode is now mutually exclusive — if a settings file from an
    # older build has both on, prefer Auto-enable.
    if merged["gamepad_mode"] and merged["auto_gamepad_mode"]:
        merged["gamepad_mode"] = False
    # Migrate old exclusive_access key to block_sc_hid.
    if "exclusive_access" in data:
        merged["block_sc_hid"] = bool(data["exclusive_access"])
    return merged


def _save_settings(settings):
    path = os.path.join(_exe_dir(), SETTINGS_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        print(f"settings save failed: {e}")


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

    def release_all_held(self):
        self.release_alt()
        self.release_shift()
        self.release_win()


class _Watcher:
    def __init__(self, should_abort, gamepad=None, chord=None):
        self.triggered = False
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
    # every ARROW_REPEAT seconds (like an OS key-repeat).
    ARROW_HOLD_DELAY = 0.35
    ARROW_REPEAT = 0.05
    # Right-stick mouse: deadzone (int16), top speed in px/sec at full
    # deflection, and an exponent >1 for fine control near center.
    MOUSE_DEADZONE = 6000
    MOUSE_SPEED = 1400.0
    MOUSE_EXPONENT = 1.6

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
            if is_edge and zone in ("UP", "DOWN") and adusk_state.is_rumble_enabled():
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
        """Desktop mode: right stick moves the mouse cursor. Velocity scales
        with deflection past the deadzone (with an exponent for fine control),
        integrated over real elapsed time so the speed is frame-rate
        independent. Disabled in gamepad mode (the stick is the analog stick).
        """
        dt = now - self._mouse_last_t if self._mouse_last_t else 0.0
        self._mouse_last_t = now

        x = sci.rstick_x
        y = sci.rstick_y  # positive = up
        if (self._gamepad is not None
                or (abs(x) <= self.MOUSE_DEADZONE
                    and abs(y) <= self.MOUSE_DEADZONE)):
            # Idle / gamepad mode: reset accumulators so a fresh push starts
            # clean, and don't carry a stale dt forward.
            self._mouse_acc_x = 0.0
            self._mouse_acc_y = 0.0
            return
        # Clamp dt so a pause between reports (or the first frame) can't fling
        # the cursor; assume a typical ~60 Hz frame if it's out of range.
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0

        span = 32767.0 - self.MOUSE_DEADZONE

        def axis(v):
            if abs(v) <= self.MOUSE_DEADZONE:
                return 0.0
            sign = 1.0 if v > 0 else -1.0
            mag = min(1.0, (abs(v) - self.MOUSE_DEADZONE) / span)
            return sign * (mag ** self.MOUSE_EXPONENT)

        # Screen Y grows downward, so stick-up (positive y) moves up (-dy).
        self._mouse_acc_x += axis(x) * self.MOUSE_SPEED * dt
        self._mouse_acc_y += -axis(y) * self.MOUSE_SPEED * dt
        mvx = int(self._mouse_acc_x)
        mvy = int(self._mouse_acc_y)
        self._mouse_acc_x -= mvx
        self._mouse_acc_y -= mvy
        if mvx or mvy:
            self._chord.mouse.move(mvx, mvy)

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

        # Steam + left stick / L3 → media transport. Cheap when Steam isn't held
        # (it just keeps its zone/edge bookkeeping in sync), so it stays called
        # every frame to preserve exact edge behavior.
        self._handle_media_chords(sc, sci, steam_now, now)

        # Left stick → arrow keys, right stick → mouse: both desktop-mode only
        # (in gamepad mode the sticks are the analog sticks, and these handlers
        # would just early-return). Gate the calls so they don't run on the
        # gameplay hot path at all.
        if self._gamepad is None:
            self._handle_arrow_stick(sci, steam_now, now)
            self._handle_mouse_stick(sci, now)

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

            # L2 / R2 full-pull → left / right mouse click. The click itself is
            # done by firmware lizard mode (we don't inject it); we just add the
            # same haptic "click" the on-screen keyboard uses so the trigger
            # pull has a tactile snap. Rising-edge = one buzz per full pull,
            # gated by the global haptics switch.
            lt_now = bool(sci.buttons & SCButtons.LT) and not steam_now
            if lt_now and not self._lt_was_pressed and adusk_state.is_rumble_enabled():
                sc.haptic_click()
            self._lt_was_pressed = lt_now

            rt_now = bool(sci.buttons & SCButtons.RT) and not steam_now
            if rt_now and not self._rt_was_pressed and adusk_state.is_rumble_enabled():
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


# --- App orchestration ------------------------------------------------------

class App:
    def __init__(self):
        self.settings = _load_settings()
        # Hidden-Debug-menu unlock: count of recent title-row clicks + the
        # timestamp of the last one (see secret_unlock_click).
        self._debug_click_count = 0
        self._debug_click_last = 0.0
        # Push the current startup setting into the registry so the on-disk
        # state matches the user's saved preference.
        _apply_startup_registry(self.settings["start_with_windows"])
        # Publish the global haptics switch to the shared runtime flag that all
        # haptic paths (UI ticks + gamepad rumble) read.
        adusk_state.set_rumble_enabled(self.settings["rumble_enabled"])

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

    def is_rumble_enabled_checked(self, item):
        return self.settings["rumble_enabled"]

    def is_block_sc_hid_checked(self, item):
        return self.settings["block_sc_hid"]

    def is_block_gamepad_takeover_checked(self, item):
        return self.settings["block_gamepad_takeover"]

    def is_debug_unlocked(self, item):
        """Visibility callback for the hidden Debug submenu."""
        return self.settings["debug_menu_unlocked"]

    def _count_debug_unlock(self, icon):
        """Count consecutive Rumble/Haptics toggles. _DEBUG_UNLOCK_CLICKS in a
        row (each within _DEBUG_UNLOCK_WINDOW of the last) toggle the hidden
        Debug submenu (Android dev-options style)."""
        now = time.monotonic()
        if now - self._debug_click_last > _DEBUG_UNLOCK_WINDOW:
            self._debug_click_count = 0
        self._debug_click_last = now
        self._debug_click_count += 1
        if self._debug_click_count >= _DEBUG_UNLOCK_CLICKS:
            self._debug_click_count = 0
            unlocked = not self.settings["debug_menu_unlocked"]
            self.settings["debug_menu_unlocked"] = unlocked
            _save_settings(self.settings)
            try:
                icon.notify("Debug menu " + ("unlocked" if unlocked else "hidden"),
                            "SteamlessKeyboard")
            except Exception:
                pass

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
        self.settings["block_gamepad_takeover"] = not item.checked
        _save_settings(self.settings)
        self._kick_sc()

    def toggle_rumble(self, icon, item):
        # Global haptics switch — gates UI ticks and gamepad rumble alike.
        self.settings["rumble_enabled"] = not item.checked
        _save_settings(self.settings)
        adusk_state.set_rumble_enabled(self.settings["rumble_enabled"])
        # Hidden Debug unlock: toggling this _DEBUG_UNLOCK_CLICKS times in a row
        # shows/hides the Debug submenu.
        self._count_debug_unlock(icon)
        # Turning it off mid-rumble: stop any motors currently playing.
        if not self.settings["rumble_enabled"]:
            self._last_rumble = (None, None)
            sc = self._current_sc
            if sc is not None:
                try:
                    sc.set_rumble(0, 0)
                except Exception:
                    pass

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
        if not adusk_state.is_rumble_enabled():
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
        """ViGEm force-feedback callback: forward the game's large/small motor
        intensities (0..255) to the physical controller's rumble motors. Runs
        on a ViGEm thread; dedups so we only write when the value changes."""
        if not adusk_state.is_rumble_enabled():
            # Global haptics off — drop FFB and re-apply on the next change
            # once re-enabled.
            self._last_rumble = (None, None)
            return
        vals = (int(large), int(small))
        if vals == self._last_rumble:
            return
        self._last_rumble = vals
        sc = self._current_sc
        if sc is not None:
            sc.set_rumble(vals[0], vals[1])

    def _close_persistent_gamepad(self):
        pad = self._persistent_gamepad
        self._persistent_gamepad = None
        if pad is not None:
            try:
                pad.close()
            except Exception:
                pass

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

    def toggle_keyboard_hotkey(self):
        """Ctrl+Alt+K: open the on-screen keyboard, or close it if it's open.
        Lets people without a Steam Controller preview the keyboard. Runs on the
        pynput hotkey thread, so it only signals — launcher_thread owns the
        window and actually opens/closes it."""
        if self._kbd_open:
            adusk_state.close()
            return
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
        if sc is not None and adusk_state.is_rumble_enabled():
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
            # block_sc_hid opens the HID exclusively to block Steam from reading
            # the physical controller. During gamepad mode we drop it unless
            # block_gamepad_takeover is also on (which forces exclusive even
            # in gamepad mode, preventing Steam Input from configuring the
            # controller for Steam games — game sees only our ViGEm XInput).
            use_exclusive = (self.settings["block_sc_hid"] and
                             (not gamepad_active
                              or self.settings["block_gamepad_takeover"]))
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
            # Brief HID-handoff settle, then open the keyboard in-process.
            time.sleep(0.1)
            adusk_state.reset_session()
            self._kbd_open = True
            self._refresh_menu()  # label → "Close Keyboard"
            try:
                adusk_app.main()
            except Exception as e:
                print(f"adusk crashed: {e!r}")
            finally:
                self._kbd_open = False
                self._refresh_menu()  # label → "Open Keyboard"
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
            "Always enable (Steam+Trackpad to control mouse)",
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
            "Block Steam controller grab",
            app.toggle_block_sc_hid,
            checked=app.is_block_sc_hid_checked,
        ),
        pystray.MenuItem(
            "Block Steam Xbox Gamepad grab",
            app.toggle_block_gamepad_takeover,
            checked=app.is_block_gamepad_takeover_checked,
        ),
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
        pystray.MenuItem(
            "Start with Windows",
            app.toggle_start_with_windows,
            checked=app.is_start_with_windows_checked,
        ),
        pystray.MenuItem("When Steam Is Running", steam_running_submenu),
        pystray.MenuItem("Gamepad Mode", gamepad_submenu),
        pystray.MenuItem(
            "Vibration",
            app.toggle_rumble,
            checked=app.is_rumble_enabled_checked,
        ),
        pystray.MenuItem("Debug", debug_submenu,
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

    icon.run(setup=setup)


if __name__ == "__main__":
    main()
