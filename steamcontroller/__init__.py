"""Windows port of the Steam Controller driver targeting the newer
"Triton" wireless adapter (PID 0x1304 — Valve internal codename Proteus).

The original ynsta/steamcontroller library targets the 2015 wired/wireless
SteamController (PID 0x1102/0x1142) with a 64-byte input report. The Triton
hardware uses a different 54-byte format with report ID 0x42. Both layouts
were identified from Valve's open-source headers in libsdl-org/SDL
(src/joystick/hidapi/steam/controller_structs.h and the steam_triton
driver). This file maps the Triton wire format onto the small adusk-facing
API surface (SteamController, SCButtons, SCStatus, SteamControllerInput,
SCI_NULL, EventMapper.process inputs).
"""

import threading
import time
from collections import namedtuple
from enum import IntEnum
from struct import unpack

import hid


VENDOR_ID = 0x28DE
PRODUCT_ID_PROTEUS = 0x1304  # Steam Controller Puck / Triton

# Triton input report constants
TRITON_INPUT_REPORT_ID = 0x42
TRITON_INPUT_REPORT_LEN = 54

# Feature-report commands (sent via send_feature_report with report ID 1)
FEATURE_REPORT_ID = 0x01
FEATURE_REPORT_LEN = 64

ID_SET_SETTINGS_VALUES = 0x87
SETTING_LIZARD_MODE = 9
LIZARD_MODE_OFF = 0
LIZARD_MODE_ON = 1

# Watchdog: the controller re-enables lizard mode if we don't keep disabling
# it. SDL re-sends every 3s; we use a slightly tighter interval to be safe.
LIZARD_REFRESH_SECONDS = 2.0


class SCStatus(IntEnum):
    INPUT = 0x42       # Triton input-state report type
    IDLE = 0x00


# Button bit assignments — Triton-specific. Names map to what adusk's
# controller.py expects (LGRIP, LB, RB, A, B, LPADTOUCH, RPADTOUCH, LT, RT).
# Source: TritonButtons enum in SDL_hidapi_steam_triton.c
class SCButtons(IntEnum):
    # Face buttons
    A      = 0x00000001
    B      = 0x00000002
    X      = 0x00000004
    Y      = 0x00000008
    # Right cluster
    QAM    = 0x00000010
    R3     = 0x00000020   # right stick click
    VIEW   = 0x00000040   # select/view/back
    RGRIP1 = 0x00000080   # right back paddle (Triton R4)
    RGRIP2 = 0x00000100   # right back paddle (Triton R5)
    RB     = 0x00000200   # right bumper
    DPAD_DOWN  = 0x00000400
    DPAD_RIGHT = 0x00000800
    DPAD_LEFT  = 0x00001000
    DPAD_UP    = 0x00002000
    START      = 0x00004000   # menu
    L3         = 0x00008000   # left stick click
    STEAM      = 0x00010000
    LGRIP1     = 0x00020000   # left back paddle (Triton L4) — bound to KEY_LEFTSHIFT in adusk
    LGRIP2     = 0x00040000   # left back paddle (Triton L5)
    LB         = 0x00080000   # left bumper
    RPADJOY_TOUCH = 0x00100000   # right joystick touch
    RPADTOUCH     = 0x00200000   # right trackpad touch
    RPAD          = 0x00400000   # right trackpad click
    RT            = 0x00800000   # right trigger digital click (full pull)
    LPADJOY_TOUCH = 0x01000000   # left joystick touch
    LPADTOUCH     = 0x02000000   # left trackpad touch
    LPAD          = 0x04000000   # left trackpad click
    LT            = 0x08000000   # left trigger digital click
    RGRIP_REST    = 0x10000000   # right grip touch (always-on resting)
    LGRIP_REST    = 0x20000000   # left grip touch
    # adusk expects an "LGRIP" alias — combined mask for either left paddle.
    LGRIP = 0x00060000           # LGRIP1 (L4) | LGRIP2 (L5)
    RGRIP = 0x00000180           # RGRIP1 (R4) | RGRIP2 (R5)


# adusk's controller.py expects an SCI tuple with these exact field names.
SteamControllerInput = namedtuple(
    'SteamControllerInput',
    'status seq buttons ltrig rtrig lpad_x lpad_y rpad_x rpad_y'
)

SCI_NULL = SteamControllerInput(
    status=0, seq=0, buttons=0,
    ltrig=0, rtrig=0,
    lpad_x=0, lpad_y=0, rpad_x=0, rpad_y=0,
)


def _build_lizard_report(mode_value):
    """Build the 65-byte feature report that sets the LIZARD_MODE setting."""
    buf = bytearray(FEATURE_REPORT_LEN + 1)  # +1 for report ID prefix
    buf[0] = FEATURE_REPORT_ID
    buf[1] = ID_SET_SETTINGS_VALUES
    buf[2] = 3                        # length: 1 ControllerSetting = 1+2 bytes
    buf[3] = SETTING_LIZARD_MODE      # settingNum
    buf[4] = mode_value & 0xFF        # settingValue low byte
    buf[5] = (mode_value >> 8) & 0xFF
    return list(buf)


DISABLE_LIZARD_REPORT = _build_lizard_report(LIZARD_MODE_OFF)
ENABLE_LIZARD_REPORT = _build_lizard_report(LIZARD_MODE_ON)


def _enumerate_data_interfaces():
    """Vendor-specific HID interfaces (usage page 0xFF00, usage 1) for the
    Proteus dongle. There are typically 4 (one per wireless port)."""
    out = []
    for d in hid.enumerate(VENDOR_ID, PRODUCT_ID_PROTEUS):
        if d.get('usage_page') == 0xFF00 and d.get('usage') == 1:
            out.append(d)
    out.sort(key=lambda d: d.get('interface_number', 0))
    return out


def _parse_triton(data: bytes) -> SteamControllerInput:
    """Parse a 54-byte Triton input report into the SCI tuple."""
    if len(data) < 30 or data[0] != TRITON_INPUT_REPORT_ID:
        return None
    # Skip byte 0 (report ID 0x42). Layout after that:
    #   B  seq            (1 byte)
    #   I  buttons        (4 bytes, uint32 LE)
    #   h  sTriggerLeft   (2 bytes, int16)
    #   h  sTriggerRight  (2 bytes, int16)
    #   8x stick fields   (8 bytes, ignored — adusk doesn't use sticks)
    #   h  sLeftPadX
    #   h  sLeftPadY
    #   H  sPressureLeft  (ignored)
    #   h  sRightPadX
    #   h  sRightPadY
    #   H  sPressureRight (ignored)
    seq, buttons, ltrig, rtrig = unpack('<BIhh', data[1:10])
    lpad_x, lpad_y, _pL, rpad_x, rpad_y, _pR = unpack('<hhHhhH', data[18:30])
    return SteamControllerInput(
        status=SCStatus.INPUT,
        seq=seq, buttons=buttons,
        ltrig=ltrig, rtrig=rtrig,
        lpad_x=lpad_x, lpad_y=lpad_y,
        rpad_x=rpad_x, rpad_y=rpad_y,
    )


class SteamController:
    """API-compatible with adusk's expectations:
        SteamController(callback, callback_args=None)
        sc.run()
        sc.addExit()
        sc.addFeedback(pos, ...)
    """

    def __init__(self, callback, callback_args=None, passive=False):
        self._cb = callback
        self._cb_args = callback_args if callback_args is not None else ()
        self._passive = passive
        self._dev = None
        self._dev_lock = threading.Lock()
        self._exit = threading.Event()
        self._lizard_thread = None

    def _open_first_responsive(self):
        candidates = _enumerate_data_interfaces()
        if not candidates:
            raise RuntimeError(
                "No Steam Controller Proteus interface found "
                f"(VID 0x{VENDOR_ID:04X}, PID 0x{PRODUCT_ID_PROTEUS:04X})."
            )

        last_err = None
        for cand in candidates:
            path = cand['path']
            dev = hid.device()
            try:
                dev.open_path(path)
            except Exception as e:
                last_err = e
                continue

            # Tell the controller to stop pretending to be a keyboard/mouse,
            # unless we're in passive mode (just listening for hotkeys).
            if not self._passive:
                try:
                    rc = dev.send_feature_report(DISABLE_LIZARD_REPORT)
                    print(f"steamcontroller: disable-lizard on iface "
                          f"{cand['interface_number']} returned {rc}")
                except Exception as e:
                    last_err = e
                    dev.close()
                    continue

            # Probe: wait briefly for input reports. Unpaired wireless ports
            # stay silent so we keep moving in that case.
            dev.set_nonblocking(0)
            deadline = time.time() + 1.5
            got_input = False
            while time.time() < deadline:
                try:
                    data = dev.read(64, 200)
                except Exception as e:
                    last_err = e
                    break
                if data and len(data) >= TRITON_INPUT_REPORT_LEN and data[0] == TRITON_INPUT_REPORT_ID:
                    got_input = True
                    break

            if got_input:
                self._dev = dev
                print(f"steamcontroller: opened iface {cand['interface_number']}")
                return

            dev.close()

        raise RuntimeError(
            "Found Steam Controller Proteus interfaces but none returned "
            "Triton input reports. Is the controller paired/powered? "
            f"Last error: {last_err!r}"
        )

    def _lizard_watchdog(self):
        """Re-send disable-lizard every LIZARD_REFRESH_SECONDS so the
        controller's own watchdog doesn't put it back into mouse/keyboard
        emulation mode."""
        while not self._exit.is_set():
            if self._exit.wait(LIZARD_REFRESH_SECONDS):
                return
            with self._dev_lock:
                if self._dev is None:
                    return
                try:
                    self._dev.send_feature_report(DISABLE_LIZARD_REPORT)
                except Exception:
                    pass

    def addExit(self):
        self._exit.set()

    def addFeedback(self, position, amplitude=128, period=0, count=1):
        pass

    def run(self):
        try:
            self._open_first_responsive()
        except Exception as e:
            print(f"steamcontroller: open failed: {e}")
            return

        if not self._passive:
            self._lizard_thread = threading.Thread(
                target=self._lizard_watchdog, daemon=True
            )
            self._lizard_thread.start()

        try:
            while not self._exit.is_set():
                with self._dev_lock:
                    dev = self._dev
                if dev is None:
                    break
                try:
                    data = dev.read(64, 200)
                except Exception as e:
                    print(f"steamcontroller: read error: {e}")
                    break
                if not data:
                    continue
                sci = _parse_triton(bytes(data))
                if sci is None:
                    continue
                try:
                    self._cb(self, sci, *self._cb_args)
                except Exception as e:
                    print(f"steamcontroller: callback raised: {e}")
        finally:
            self._exit.set()
            with self._dev_lock:
                try:
                    if self._dev is not None:
                        # Restore lizard mode immediately so the controller
                        # works as a normal mouse/keyboard right away instead
                        # of waiting for the hardware watchdog (~3-5 sec).
                        if not self._passive:
                            try:
                                self._dev.send_feature_report(ENABLE_LIZARD_REPORT)
                            except Exception:
                                pass
                        self._dev.close()
                except Exception:
                    pass
                self._dev = None
