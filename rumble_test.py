"""Standalone rumble tester for SteamlessKeyboard's gamepad-mode rumble.

It sends XInput vibration to the virtual Xbox pad, which ViGEm relays to the
app's force-feedback callback exactly like a real game would — so you can test
and tune rumble without launching a game.

Setup:
  1. Run SteamlessKeyboard and set tray menu -> Gamepad Mode -> Always On
     (this creates the virtual Xbox pad).
  2. Make sure the app is actually listening: either close Steam, or turn off
     "Disable While Steam Is Running" (otherwise the app pauses and there is no
     virtual pad).
  3. Turn the Steam Controller on so the app has a physical device to rumble.

Run:
    python rumble_test.py            # auto-pick the first XInput controller
    python rumble_test.py 1          # force XInput index 1 (0-3)
"""

import ctypes
import sys
import time
from ctypes import wintypes


def _load_xinput():
    for name in ("XInput1_4.dll", "XInput1_3.dll", "XInput9_1_0.dll"):
        try:
            return ctypes.WinDLL(name)
        except OSError:
            continue
    raise SystemExit("No XInput DLL found on this system.")


_xinput = _load_xinput()


class XINPUT_VIBRATION(ctypes.Structure):
    # Motor speeds are 0..65535 (ViGEm rescales to the 0..255 the app sees).
    _fields_ = [("wLeftMotorSpeed", wintypes.WORD),
                ("wRightMotorSpeed", wintypes.WORD)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD),
                ("_pad", ctypes.c_byte * 16)]


_set = _xinput.XInputSetState
_set.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_VIBRATION)]
_set.restype = wintypes.DWORD

_get = _xinput.XInputGetState
_get.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_STATE)]
_get.restype = wintypes.DWORD

ERROR_SUCCESS = 0


def set_rumble(index, left, right):
    vib = XINPUT_VIBRATION(left & 0xFFFF, right & 0xFFFF)
    return _set(index, ctypes.byref(vib))


def first_connected_index():
    st = XINPUT_STATE()
    for i in range(4):
        if _get(i, ctypes.byref(st)) == ERROR_SUCCESS:
            return i
    return None


def main():
    if len(sys.argv) > 1:
        idx = int(sys.argv[1])
    else:
        idx = first_connected_index()
        if idx is None:
            raise SystemExit(
                "No XInput controller found. Is SteamlessKeyboard running with "
                "Gamepad Mode -> Always On, and Steam closed (or 'Disable While "
                "Steam Is Running' off)?")
    print(f"Sending vibration to XInput index {idx}. Ctrl+C to stop.")

    try:
        print("-> LEFT motor (heavy) ramp up/down")
        for v in list(range(0, 65536, 4096)) + list(range(65535, -1, -4096)):
            set_rumble(idx, v, 0)
            time.sleep(0.06)
        set_rumble(idx, 0, 0)
        time.sleep(0.4)

        print("-> RIGHT motor (buzzy) ramp up/down")
        for v in list(range(0, 65536, 4096)) + list(range(65535, -1, -4096)):
            set_rumble(idx, 0, v)
            time.sleep(0.06)
        set_rumble(idx, 0, 0)
        time.sleep(0.4)

        print("-> BOTH motors, 4 full pulses")
        for _ in range(4):
            set_rumble(idx, 65535, 65535)
            time.sleep(0.3)
            set_rumble(idx, 0, 0)
            time.sleep(0.25)
    finally:
        set_rumble(idx, 0, 0)
        print("done (motors zeroed)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        try:
            set_rumble(int(sys.argv[1]) if len(sys.argv) > 1 else 0, 0, 0)
        except Exception:
            pass
        print("\nstopped")
