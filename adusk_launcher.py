"""Persistent launcher for adusk.

Run this once from PowerShell:
    python adusk_launcher.py

It opens the Steam Controller in raw mode and watches for Steam+X. When the
chord is detected it releases the controller and calls adusk.main() in
the same Python process, so the keyboard opens without a fresh interpreter
startup. Closing the keyboard returns control here to resume watching.

Ctrl+C in this terminal stops the launcher.
"""

import os
import time

# ADUSK_DATA must be set BEFORE importing adusk.* — adusk.resources captures
# it into a module-level tuple at import time, and it's used to locate the
# YAML config and bundled images/glyphs.
_project_dir = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault('ADUSK_DATA', os.path.join(_project_dir, 'data'))

# Heavy imports up front so each Steam+X open is just a function call,
# not a subprocess spawn that re-imports sdl2 / pynput / yaml from scratch.
from steamcontroller import SteamController, SCButtons, SCStatus
from adusk import adusk as adusk_app
from adusk import state as adusk_state


class _Watcher:
    """One pass of Steam+X detection. The callback exits the SteamController
    run loop when the chord is seen, so the device is released and adusk can
    grab it."""

    def __init__(self):
        self.triggered = False
        self._steam_was_pressed = False
        self._saw_x_during_steam = False

    def on_input(self, sc, sci):
        if sci.status != SCStatus.INPUT:
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


def main():
    print("adusk launcher running. Press Steam+X to open the keyboard.")
    print("Ctrl+C here stops the launcher.")

    while True:
        watcher = _Watcher()
        # passive=True: don't disable lizard mode, so the controller still
        # works as a normal mouse/keyboard between adusk sessions.
        sc = SteamController(callback=watcher.on_input, passive=True)
        try:
            sc.run()
        except KeyboardInterrupt:
            return

        if not watcher.triggered:
            return

        print("Steam+X detected. Launching adusk...")
        # Tiny pause so the HID handle is fully released before adusk's
        # controller thread tries to reopen it.
        time.sleep(0.1)
        adusk_state.reset_session()
        try:
            adusk_app.main()
        except KeyboardInterrupt:
            return
        except Exception as e:
            print(f"adusk crashed: {e!r}")
        print("adusk closed. Watching for Steam+X again...")
        time.sleep(0.1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nlauncher stopped")
