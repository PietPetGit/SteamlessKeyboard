"""Linux entry point for the on-screen keyboard.

Scope of this build is intentionally narrow: it only brings up the OSK.
The Windows-only tray UI, registry autostart, Steam-running detection,
and ViGEm virtual gamepad are NOT ported — the rest of SteamlessKeyboard
will be tackled later.

Usage:
    python adusk_linux.py            # opens the OSK once and exits when closed
    python adusk_linux.py --watch    # stays running; Ctrl+Alt+K opens the OSK
    python adusk_linux.py --controller   # also watch for the Steam+X chord

Frozen single-file binary (built by build_linux.py) does the same thing.
"""

import argparse
import os
import sys
import threading
import time


def _is_frozen():
    return getattr(sys, "frozen", False)


def _bundle_dir():
    if _is_frozen():
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


# IMPORTANT: ADUSK_DATA must be set BEFORE importing adusk.* — adusk.resources
# captures its search path at import time.
os.environ.setdefault("ADUSK_DATA", os.path.join(_bundle_dir(), "data"))

# Force SDL onto the X11 backend (via XWayland) on Linux — otherwise the
# OSK runs as a native Wayland surface and the compositor gives it
# keyboard focus on map, stealing focus from the target app. See the
# matching block in tray_linux.py for the full reasoning.
if sys.platform.startswith("linux"):
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")


from adusk import adusk as adusk_app  # noqa: E402
from adusk import state as adusk_state  # noqa: E402


def _open_osk_once():
    """Reset per-session state and run the OSK on the calling thread. SDL
    must initialize and pump events from the main thread, so this function
    is always called from main (the hotkey listener only signals)."""
    adusk_state.reset_session()
    try:
        adusk_app.main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"adusk crashed: {e!r}")


def _run_controller_watcher(open_event, stop_event):
    """Background thread: keep a passive Steam Controller open and signal
    `open_event` on each Steam+X chord. Mirrors adusk_launcher._Watcher.
    Survives controller disconnects by retrying with a short backoff."""
    try:
        from steamcontroller import SteamController, SCButtons, SCStatus
    except Exception as e:
        print(f"steamcontroller unavailable, controller watcher disabled: {e}")
        return

    class _Watcher:
        def __init__(self):
            self.triggered = False
            self._steam_was_pressed = False
            self._saw_x_during_steam = False

        def on_input(self, sc, sci):
            if sci.status != SCStatus.INPUT:
                return
            if stop_event.is_set():
                sc.addExit()
                return
            steam_now = bool(sci.buttons & (SCButtons.STEAM | SCButtons.QAM))  # "..." (QAM) acts like Steam
            x_now = bool(sci.buttons & SCButtons.X)
            if steam_now and not self._steam_was_pressed:
                self._saw_x_during_steam = False
            if steam_now and x_now and not self._saw_x_during_steam:
                self._saw_x_during_steam = True
                self.triggered = True
                open_event.set()
                sc.addExit()
            self._steam_was_pressed = steam_now

    while not stop_event.is_set():
        watcher = _Watcher()
        sc = SteamController(callback=watcher.on_input, passive=True)
        try:
            sc.run()
        except Exception as e:
            print(f"controller watcher error: {e}")
        # Brief backoff so we don't busy-loop if the controller is unplugged.
        if not watcher.triggered and not stop_event.is_set():
            time.sleep(1.0)


def _run_hotkey_watcher(open_event, stop_event):
    """Background thread: listen for Ctrl+Alt+K and signal `open_event`.
    Uses pynput's GlobalHotKeys, which on Linux talks to the X server. On
    Wayland sessions it usually no-ops silently."""
    try:
        from pynput import keyboard as pkb
    except Exception as e:
        print(f"pynput unavailable, hotkey watcher disabled: {e}")
        return

    def _on_open():
        if not stop_event.is_set():
            open_event.set()

    try:
        listener = pkb.GlobalHotKeys({"<ctrl>+<alt>+k": _on_open})
        listener.start()
    except Exception as e:
        print(f"hotkey listener failed to start: {e}")
        return
    stop_event.wait()
    try:
        listener.stop()
    except Exception:
        pass


def _watch_loop(args):
    """Persistent mode: open the OSK whenever the hotkey or Steam+X chord
    fires. The OSK must run on the main thread (SDL constraint), so the
    watchers live on daemons and pulse a threading.Event."""
    open_event = threading.Event()
    stop_event = threading.Event()

    threads = []
    threads.append(threading.Thread(
        target=_run_hotkey_watcher, args=(open_event, stop_event), daemon=True))
    if args.controller:
        threads.append(threading.Thread(
            target=_run_controller_watcher, args=(open_event, stop_event), daemon=True))
    for t in threads:
        t.start()

    print("SteamlessKeyboard (Linux) running.")
    print(" - Ctrl+Alt+K opens the on-screen keyboard.")
    if args.controller:
        print(" - Steam + X on the controller also opens it.")
    print(" - Ctrl+C in this terminal stops the launcher.")

    try:
        while not stop_event.is_set():
            if not open_event.wait(timeout=1.0):
                continue
            open_event.clear()
            print("opening OSK...")
            _open_osk_once()
            print("OSK closed. Watching for hotkey again...")
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()


def main():
    parser = argparse.ArgumentParser(
        description="SteamlessKeyboard on-screen keyboard (Linux)."
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Stay running; open the OSK on each Ctrl+Alt+K hotkey "
             "(default: open the OSK once and exit when it closes).")
    parser.add_argument(
        "--controller", action="store_true",
        help="Also watch the Steam Controller for the Steam+X chord. "
             "Implies --watch. Requires hidapi access to the device.")
    args = parser.parse_args()

    if args.controller:
        args.watch = True

    if args.watch:
        _watch_loop(args)
    else:
        _open_osk_once()


if __name__ == "__main__":
    main()
