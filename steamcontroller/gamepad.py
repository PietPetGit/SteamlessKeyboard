"""Virtual Xbox 360 gamepad bridge for the Steam Controller 2026.

Ported from the C++ SteamlessController project (src/app/VirtualController.cpp).
Takes parsed SteamControllerInput records and pushes the equivalent XInput
state through ViGEm via the `vgamepad` Python wrapper.

ViGEmBus must be installed on the host system; vgamepad ships ViGEmClient.dll
but the kernel driver is a separate install (https://github.com/ViGEm/ViGEmBus).
"""

try:
    import vgamepad as vg
    _VGAMEPAD_IMPORT_ERROR = None
except Exception as e:  # ImportError, OSError when ViGEmClient.dll fails
    vg = None
    _VGAMEPAD_IMPORT_ERROR = e


class ViGEmUnavailable(RuntimeError):
    """vgamepad / ViGEmBus is not usable on this machine."""


# -- Bit positions (taken from C++ SteamController.h; do not use the Python
# -- SCButtons enum here — its names don't match the C++ reference and the
# -- whole point of this module is to mirror the C++ Translate() exactly.)

# buf[2] (low byte of the uint32 button mask)
_BTN_A    = 0x01
_BTN_B    = 0x02
_BTN_X    = 0x04
_BTN_Y    = 0x08
_BTN_RS   = 0x20  # right stick click
_BTN_MENU = 0x40  # Menu / Start

# buf[3]
_BTN_RB      = 0x02
_BTN_DPAD_DN = 0x04
_BTN_DPAD_RT = 0x08
_BTN_DPAD_LT = 0x10
_BTN_DPAD_UP = 0x20
_BTN_VIEW    = 0x40  # View / Back
_BTN_LS      = 0x80  # left stick click

# buf[4]
_BTN_STEAM = 0x01  # Guide
_BTN_LB    = 0x08


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class VirtualGamepad:
    """Wraps a vgamepad VX360Gamepad and exposes update(sci) which translates
    a Steam Controller input record into an XUSB report and sends it to ViGEm.
    """

    def __init__(self):
        if vg is None:
            raise ViGEmUnavailable(
                f"vgamepad not available: {_VGAMEPAD_IMPORT_ERROR!r}. "
                "Install the ViGEmBus driver (https://github.com/ViGEm/ViGEmBus/releases) "
                "and `pip install vgamepad`."
            )
        try:
            self._pad = vg.VX360Gamepad()
        except Exception as e:
            raise ViGEmUnavailable(
                f"Failed to create virtual Xbox 360 pad: {e!r}. "
                "Is the ViGEmBus driver installed?"
            ) from e

        # Cache the XUSB button enum so update() doesn't re-lookup each call.
        self._XB = vg.XUSB_BUTTON
        # Strong ref to the force-feedback callback (vgamepad keeps its own too,
        # but holding it here makes the lifecycle explicit).
        self._rumble_cb = None

    def register_rumble(self, handler):
        """Forward game force-feedback to `handler(large, small)` — the XInput
        large/small motor intensities (0..255) — whenever the game updates
        rumble on the virtual pad. The callback runs on a ViGEm thread."""
        pad = self._pad
        if pad is None:
            return

        # Signature must match vgamepad's expected callback exactly.
        def _cb(client, target, large_motor, small_motor, led_number, user_data):
            try:
                handler(large_motor, small_motor)
            except Exception as e:
                print(f"rumble callback error: {e!r}")

        self._rumble_cb = _cb
        try:
            pad.register_notification(callback_function=_cb)
        except Exception as e:
            print(f"register rumble notification failed: {e!r}")

    def close(self):
        pad = self._pad
        self._pad = None
        if pad is not None:
            try:
                pad.unregister_notification()
            except Exception:
                pass
            self._rumble_cb = None
            try:
                # Zero the report so games don't see ghost input after we leave.
                pad.reset()
                pad.update()
            except Exception:
                pass
            # vgamepad's __del__ unregisters the target with ViGEm.

    def reset(self):
        """Zero the XInput report and push it. Used to release any held
        buttons when we're about to stop pushing input (e.g. handing the
        controller back to firmware lizard mode mid-session)."""
        pad = self._pad
        if pad is None:
            return
        try:
            pad.reset()
            pad.update()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def update(self, sci):
        """Translate a SteamControllerInput into an XInput report and push it.

        Mirrors VirtualController::Translate() from the C++ project.
        """
        pad = self._pad
        if pad is None:
            return

        buttons = sci.buttons
        b0 = (buttons >> 0) & 0xFF
        b1 = (buttons >> 8) & 0xFF
        b2 = (buttons >> 16) & 0xFF

        XB = self._XB
        w = 0
        # Face buttons
        if b0 & _BTN_A: w |= XB.XUSB_GAMEPAD_A
        if b0 & _BTN_B: w |= XB.XUSB_GAMEPAD_B
        if b0 & _BTN_X: w |= XB.XUSB_GAMEPAD_X
        if b0 & _BTN_Y: w |= XB.XUSB_GAMEPAD_Y
        # Bumpers
        if b2 & _BTN_LB: w |= XB.XUSB_GAMEPAD_LEFT_SHOULDER
        if b1 & _BTN_RB: w |= XB.XUSB_GAMEPAD_RIGHT_SHOULDER
        # Menu / View
        if b0 & _BTN_MENU: w |= XB.XUSB_GAMEPAD_START
        if b1 & _BTN_VIEW: w |= XB.XUSB_GAMEPAD_BACK
        # Stick clicks
        if b1 & _BTN_LS: w |= XB.XUSB_GAMEPAD_LEFT_THUMB
        if b0 & _BTN_RS: w |= XB.XUSB_GAMEPAD_RIGHT_THUMB
        # Guide
        if b2 & _BTN_STEAM: w |= XB.XUSB_GAMEPAD_GUIDE
        # D-pad
        if b1 & _BTN_DPAD_UP: w |= XB.XUSB_GAMEPAD_DPAD_UP
        if b1 & _BTN_DPAD_DN: w |= XB.XUSB_GAMEPAD_DPAD_DOWN
        if b1 & _BTN_DPAD_LT: w |= XB.XUSB_GAMEPAD_DPAD_LEFT
        if b1 & _BTN_DPAD_RT: w |= XB.XUSB_GAMEPAD_DPAD_RIGHT

        report = pad.report
        report.wButtons = w
        # Triggers: int16 0..0x7FFF → uint8 0..255 (same `>> 7` the C++ uses).
        report.bLeftTrigger  = _clamp(sci.ltrig >> 7, 0, 255)
        report.bRightTrigger = _clamp(sci.rtrig >> 7, 0, 255)
        # Sticks: int16 same range as XInput — pass straight through.
        report.sThumbLX = _clamp(sci.lstick_x, -32768, 32767)
        report.sThumbLY = _clamp(sci.lstick_y, -32768, 32767)
        report.sThumbRX = _clamp(sci.rstick_x, -32768, 32767)
        report.sThumbRY = _clamp(sci.rstick_y, -32768, 32767)

        pad.update()
