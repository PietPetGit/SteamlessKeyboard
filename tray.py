"""Steam Controller Keyboard — system-tray launcher.

This is the bundled entry point for the portable EXE. It:
  * Runs a tray icon (right-click menu: Launch at PC start, Close when Steam
    starts, Exit). Settings persist in `settings.json` next to the EXE.
  * Watches the Steam Controller for the Steam+X chord and brings up the
    on-screen keyboard in-process (no subprocess startup cost).
  * Optionally pauses the listener while Steam is running and resumes after
    Steam exits (the controller is released so Steam can grab it).
"""

import json
import os
import sys
import threading
import time
import winreg


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


# --- Settings persistence ---------------------------------------------------

def _load_settings():
    path = os.path.join(_exe_dir(), SETTINGS_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: bool(v) for k, v in data.items() if k in DEFAULT_SETTINGS})
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

class _Watcher:
    def __init__(self, should_abort):
        self.triggered = False
        self._steam_was_pressed = False
        self._saw_x_during_steam = False
        # Callable returning True when the sc.run() loop should exit early
        # (e.g. tray-Exit was clicked, or Steam started).
        self._should_abort = should_abort

    def on_input(self, sc, sci):
        if sci.status != SCStatus.INPUT:
            return
        if self._should_abort():
            sc.addExit()
            return
        steam_now = bool(sci.buttons & SCButtons.STEAM)
        x_now = bool(sci.buttons & SCButtons.X)
        if steam_now and not self._steam_was_pressed:
            self._saw_x_during_steam = False
        if steam_now and x_now and not self._saw_x_during_steam:
            self._saw_x_during_steam = True
            self.triggered = True
            sc.addExit()
        self._steam_was_pressed = steam_now


# --- App orchestration ------------------------------------------------------

class App:
    def __init__(self):
        self.settings = _load_settings()
        # Push the current startup setting into the registry so the on-disk
        # state matches the user's saved preference.
        _apply_startup_registry(self.settings["start_with_windows"])

        self._stop_event = threading.Event()
        # Set when Steam is running AND the user opted into pausing for Steam.
        self._steam_active = threading.Event()
        self._current_sc = None

    # tray menu state predicates --------------------------------------------

    def is_start_with_windows_checked(self, item):
        return self.settings["start_with_windows"]

    def is_disable_while_steam_checked(self, item):
        return self.settings["disable_while_steam_running"]

    def is_exit_on_steam_checked(self, item):
        return self.settings["exit_on_steam_launch"]

    # tray menu actions -----------------------------------------------------

    def toggle_start_with_windows(self, icon, item):
        self.settings["start_with_windows"] = not item.checked
        _save_settings(self.settings)
        _apply_startup_registry(self.settings["start_with_windows"])

    def toggle_disable_while_steam(self, icon, item):
        self.settings["disable_while_steam_running"] = not item.checked
        _save_settings(self.settings)
        # If the user just turned it off, clear the pause flag so the listener
        # resumes immediately even if Steam is still running.
        if not self.settings["disable_while_steam_running"]:
            self._steam_active.clear()

    def toggle_exit_on_steam(self, icon, item):
        self.settings["exit_on_steam_launch"] = not item.checked
        _save_settings(self.settings)

    def exit_app(self, icon, item):
        self._stop_event.set()
        adusk_state.close()
        if self._current_sc is not None:
            try:
                self._current_sc.addExit()
            except Exception:
                pass
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
                # latched pause flag is cleared and skip the check entirely.
                if self._steam_active.is_set():
                    self._steam_active.clear()
                last_running = False
                self._stop_event.wait(2.0)
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
            self._stop_event.wait(2.0)

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
        while not self._stop_event.is_set():
            # If Steam is currently running and we're configured to pause,
            # wait it out without holding the controller HID handle open.
            if self._steam_active.is_set():
                self._stop_event.wait(1.0)
                continue

            watcher = _Watcher(self._should_abort_sc)
            sc = SteamController(callback=watcher.on_input, passive=True)
            self._current_sc = sc
            try:
                sc.run()
            except KeyboardInterrupt:
                return
            finally:
                self._current_sc = None

            if self._stop_event.is_set():
                return
            if self._steam_active.is_set():
                # Pause-for-Steam fired; loop back to wait state.
                continue
            if not watcher.triggered:
                # sc.run() returned because the device wasn't responsive
                # (controller asleep or unpaired). Back off a moment and retry.
                self._stop_event.wait(1.0)
                continue

            # Brief HID-handoff settle, then open the keyboard in-process.
            time.sleep(0.1)
            adusk_state.reset_session()
            try:
                adusk_app.main()
            except Exception as e:
                print(f"adusk crashed: {e!r}")
            time.sleep(0.1)


def _load_icon_image():
    # Generated by build.py from keyboard-full.svg. Falls back to the
    # in-OSK keyboard glyph if the rendered app icon hasn't been built yet
    # (e.g. running from source without having run build.py).
    base = os.path.join(_bundle_dir(), "data", "images")
    for candidate in ("app_icon.png", os.path.join("glyphs", "glyph_keyboard.png")):
        path = os.path.join(base, candidate)
        if os.path.isfile(path):
            return Image.open(path)
    raise FileNotFoundError("no tray icon found under data/images/")


def main():
    app = App()
    image = _load_icon_image()

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

    icon.run(setup=setup)


if __name__ == "__main__":
    main()
