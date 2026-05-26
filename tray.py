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
        # True while LEFTALT is currently being held by us.
        self.alt_held = False
        # Rising-edge tracking for VIEW so one physical press = one Tab.
        self.view_was_pressed = False

    def release_alt(self):
        if self.alt_held:
            self.kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self.alt_held = False


class _Watcher:
    def __init__(self, should_abort, gamepad=None, chord=None):
        self.triggered = False
        self._steam_was_pressed = False
        self._saw_x_during_steam = False
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
        # Steam + Y → power off the controller (like Steam Input). _powered_off
        # latches so we only send the command once per chord press.
        self._powered_off = False

    # Left-stick deflection (int16) past this magnitude counts as a direction.
    STICK_DEADZONE = 14000
    # Volume feel: a tap = one step. Holding up/down past STICK_HOLD_DELAY
    # seconds then rapidly ramps, one step every STICK_VOL_REPEAT seconds.
    STICK_HOLD_DELAY = 0.5
    STICK_VOL_REPEAT = 0.021

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

        key = {
            "UP":    sui.Keys.KEY_VOLUMEUP,
            "DOWN":  sui.Keys.KEY_VOLUMEDOWN,
            "LEFT":  sui.Keys.KEY_PREVIOUSSONG,
            "RIGHT": sui.Keys.KEY_NEXTSONG,
        }.get(zone)

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

    def on_input(self, sc, sci):
        if sci.status != SCStatus.INPUT:
            return
        if self._should_abort():
            sc.addExit()
            return

        steam_now = bool(sci.buttons & SCButtons.STEAM)
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

        if steam_now and not self._steam_was_pressed:
            self._saw_x_during_steam = False
        if steam_now and x_now and not self._saw_x_during_steam:
            self._saw_x_during_steam = True
            self.triggered = True
            sc.addExit()

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

        # Steam + left stick / L3 → media transport. While Steam is held the
        # gamepad output is paused (see above) and firmware lizard is
        # suppressed, so the stick/click are free to drive media keys.
        self._handle_media_chords(sc, sci, steam_now, time.monotonic())

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

        self._steam_was_pressed = steam_now


# --- App orchestration ------------------------------------------------------

class App:
    def __init__(self):
        self.settings = _load_settings()
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
        self._current_sc = None
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
        _save_settings(self.settings)
        # If the user just turned it off, clear the pause flag so the listener
        # resumes immediately even if Steam is still running.
        if not self.settings["disable_while_steam_running"]:
            self._steam_active.clear()
        # Wake the steam-watch thread so it re-evaluates whether to poll/idle.
        self._steam_watch_wake.set()

    def toggle_exit_on_steam(self, icon, item):
        self.settings["exit_on_steam_launch"] = not item.checked
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

    def _kick_sc(self):
        """Force the current SteamController loop to exit so launcher_thread
        re-evaluates settings (gamepad mode, auto-detected game state)."""
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
            if not watcher.triggered:
                # sc.run() returned without a Steam+X. Two cases:
                if sc.opened:
                    # It opened and ran, so this was a deliberate kick (gamepad-
                    # mode toggle / focus change) or the device dropped mid-use.
                    # Retry promptly so the new mode applies, and reset backoff.
                    reconnect_wait = RECONNECT_WAIT_MIN
                    self._stop_event.wait(RECONNECT_WAIT_MIN)
                else:
                    # Open failed — no controller present. Back off so we don't
                    # re-enumerate HID every second while it stays disconnected.
                    self._stop_event.wait(reconnect_wait)
                    reconnect_wait = min(reconnect_wait * 2, RECONNECT_WAIT_MAX)
                continue

            # A controller is present and gave us Steam+X — reset the backoff.
            reconnect_wait = RECONNECT_WAIT_MIN
            # Brief HID-handoff settle, then open the keyboard in-process.
            time.sleep(0.1)
            adusk_state.reset_session()
            try:
                adusk_app.main()
            except Exception as e:
                print(f"adusk crashed: {e!r}")
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
            "Auto-enable (when game in foreground)",
            app.toggle_auto_gamepad_mode,
            checked=app.is_auto_gamepad_mode_checked,
        ),
        pystray.MenuItem(
            "Always On (disables mouse controls)",
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
            "Block SC HID Takeover",
            app.toggle_block_sc_hid,
            checked=app.is_block_sc_hid_checked,
        ),
        pystray.MenuItem(
            "Block Xbox Gamepad Takeover",
            app.toggle_block_gamepad_takeover,
            checked=app.is_block_gamepad_takeover_checked,
        ),
    )

    menu = pystray.Menu(
        pystray.MenuItem(
            "Start with Windows",
            app.toggle_start_with_windows,
            checked=app.is_start_with_windows_checked,
        ),
        pystray.MenuItem(
            "Disable While Steam Is Running",
            app.toggle_disable_while_steam,
            checked=app.is_disable_while_steam_checked,
        ),
        pystray.MenuItem(
            "Exit on Steam Launch",
            app.toggle_exit_on_steam,
            checked=app.is_exit_on_steam_checked,
        ),
        pystray.MenuItem("Gamepad Mode", gamepad_submenu),
        pystray.MenuItem(
            "Rumble / Haptics",
            app.toggle_rumble,
            checked=app.is_rumble_enabled_checked,
        ),
        pystray.MenuItem("Debug", debug_submenu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", app.exit_app),
    )

    icon = pystray.Icon("SteamControllerKeyboard", image,
                        "Steam Controller Keyboard", menu)
    app._icon_ref = icon

    def setup(icon):
        icon.visible = True
        threading.Thread(target=app.launcher_thread, daemon=True).start()
        threading.Thread(target=app.steam_watch_thread, daemon=True).start()
        threading.Thread(target=app.auto_gamepad_thread, daemon=True).start()

    icon.run(setup=setup)


if __name__ == "__main__":
    main()
