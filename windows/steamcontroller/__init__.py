"""Windows port of the Steam Controller driver targeting the newer
"Triton" wireless adapter (PID 0x1304 — Valve internal codename Proteus).

The original ynsta/steamcontroller library targets the 2015 wired/wireless
SteamController (PID 0x1102/0x1142) with a 64-byte input report. The Triton
hardware uses a different format with state report ID 0x45 (newer firmware;
46 bytes) or the legacy 0x42 (USB/full state, 54 bytes) — same field layout,
see TRITON_INPUT_REPORT_IDS. Both layouts
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
from struct import Struct, unpack

# Precompiled parser for the Triton input report body (bytes 1..29). Built once
# and used via unpack_from so the per-frame hot path does a single C-level
# unpack with no intermediate slice allocations.
#   B seq | I buttons | h h triggers | h h h h sticks | h h H h h H pads+pressure
_TRITON_STRUCT = Struct('<BIhhhhhhhhHhhH')

import hid


VENDOR_ID = 0x28DE
PRODUCT_ID_PROTEUS = 0x1304  # Steam Controller Puck / Triton (wireless dongle)
PRODUCT_ID_WIRED   = 0x1302  # Steam Controller 2026 (wired USB)

# Remembered across SteamController instances: the interface path that last
# returned input reports. Tried first on the next open so a rebuild (e.g. the
# gamepad<->lizard switch on alt-tab) skips the dongle's silent slots and comes
# live in milliseconds instead of probing each slot for up to 1.5s — which is
# what made the mode chime lag ~1s behind the actual switch.
_LAST_GOOD_PATH = None

# Triton input ("state") report IDs. A firmware update bumped the primary
# REPORT_STATE id from 0x42 to 0x45 (the BLE / "no-quaternion" variant); wired
# units can still emit the legacy 0x42 (USB / full-state). The byte layout after
# the id is IDENTICAL for both, so accept EITHER — mirroring the reference
# project's IsStateReportId(), which treats 0x45 and 0x42 the same. Gating on a
# single id (and on the full 54-byte length) is what broke input after the
# update: the 0x45 report can be shorter than the USB one, so we only require
# enough bytes to unpack the fields (TRITON_INPUT_MIN_LEN), not the full length.
TRITON_INPUT_REPORT_IDS = (0x45, 0x42)
TRITON_INPUT_REPORT_LEN = 54   # full USB (0x42) report length — reference only
TRITON_INPUT_MIN_LEN = 30      # bytes needed to unpack the SCI fields below

# Power/battery status report (HID input report id 0x43, 17 bytes). It streams
# interleaved with the game-input reports on the same vendor interface. Layout
# after the report id byte: [1]=charge state, [2]=battery percent (0..100),
# [3:5]=battery voltage mV (uint16 LE), then system/input voltage, current and
# temperature (unused here). Charge-state values and the percent/voltage offsets
# were taken from Valve's SDL steam_triton driver and the Bloss battery-indicator
# reference (GamepadBatteryParser.TryParseSteamTritonBatteryStatus).
TRITON_BATTERY_REPORT_ID = 0x43
TRITON_BATTERY_REPORT_LEN = 17

# report[1] charge-state byte values.
CHARGE_STATE_RESET = 0
CHARGE_STATE_DISCHARGING = 1
CHARGE_STATE_CHARGING = 2
CHARGE_STATE_SOURCE_VALIDATE = 3
CHARGE_STATE_CHARGING_DONE = 4

# Treat the battery as unknown if no input/battery frame has arrived for this
# long. The controller streams input continuously while connected, so a gap this
# big means it powered off (Steam+Y) or dropped its wireless link — even though
# the dongle is still plugged in. Keeps the tray from showing a stale %.
BATTERY_FRESH_SECONDS = 4.0

# Wireless link-status reports (byte[1]: 1=disconnected, 2=connected). We skip
# them in the read loop so they don't get mis-parsed as input frames.
TRITON_WIRELESS_STATUS_IDS = (0x46, 0x79)

# Feature-report commands (sent via send_feature_report with report ID 1)
FEATURE_REPORT_ID = 0x01
FEATURE_REPORT_LEN = 64

ID_SET_SETTINGS_VALUES = 0x87
SETTING_LIZARD_MODE = 9
LIZARD_MODE_OFF = 0
LIZARD_MODE_ON = 1

# Power-off command. On Valve's controllers this feature report tells the
# controller to turn itself off (the "hold Steam+Y to turn off" behavior in
# Steam Input). Payload is the ASCII string "off!". Confirmed on the original
# Steam Controller / SDL's hidapi driver; experimental on Triton hardware.
ID_TURN_OFF_CONTROLLER = 0x9F

# Haptics. Unlike lizard/turn-off (feature reports), haptics are HID OUTPUT
# reports sent with a plain write (byte 0 = report ID, 65-byte buffer). Format
# and actuator mapping confirmed on real 2026 hardware by the SteamHapticsSinger
# project: 0x83 plays an LFO tone on one actuator, 0x82 stops it.
HID_OUTPUT_REPORT_LEN = 65
ID_OUT_HAPTIC_LFO_TONE = 0x83   # play a tone: [id, actuator, gain, freqLo, freqHi, 0xFF, 0x7F]
ID_OUT_HAPTIC_STOP     = 0x82   # stop an actuator: [id, actuator]

# Actuator indices (no-swap mapping from SteamHapticsSinger):
HAPTIC_PAD_LEFT     = 0   # left trackpad
HAPTIC_PAD_RIGHT    = 1   # right trackpad
HAPTIC_RUMBLE_LEFT  = 3   # left back rumble motor
HAPTIC_RUMBLE_RIGHT = 4   # right back rumble motor

# Tone gain is a signed int8: nearer +127 is loudest, more-negative is quieter
# (the changelog warns the loud end can damage the motors). SteamHapticsSinger
# ships -2 (0xFE) for audible music; UI ticks want much less, so HAPTIC_CLICK_GAIN
# is well down the scale for a light tap.
HAPTIC_DEFAULT_GAIN = 0xFE
# Gain is ~dB-like and steep: -2 is near full blast, -80 is inaudible. A light
# but feelable click sits near the top; the SHORT burst count keeps it clicky.
HAPTIC_CLICK_GAIN = -6
# The simulated trackpad-click (a physical pad press) gets its own tick: a
# short, crisp, slightly firmer pop than the light key tap. Kept high-frequency
# and short so it reads as a real button click, not a deep buzz.
HAPTIC_PAD_CLICK_GAIN = -5
HAPTIC_PAD_CLICK_FREQ = 500
# Mode-change "chime": a short, deliberately subtle two-tone played on both
# trackpads, with the two pads detuned a couple Hz so they beat gently
# ("chorus") and a barely-there low-D pedal on a rumble motor for warmth. Kept
# quiet and low because it fires on every gamepad mode change. This voicing was
# chosen by ear (a low rising fifth) over louder/melodic alternatives.
HAPTIC_CHIME_GAIN = 3        # just above the -2 "music" level: clear, not loud
# "Ding-dong": a two-tone major third (F#4, A4). ON rises F#4->A4, OFF falls
# A4->F#4 (play_chime reverses for off). Equal-tempered.
CHIME_NOTES = (370, 440)     # F#4, A4
CHIME_DURATIONS = (0.10, 0.15)  # quick two-tone blip, second rings a touch
# Each chime tone is bounded to this long so a crash mid-chime can't leave a
# pad/motor buzzing (the body pedal is a rumble motor). Far longer than any
# single note, so in normal play the next note's stop cuts it first — the sound
# is unchanged; only the crash tail self-terminates. See play_chime.
CHIME_TONE_SECONDS = 1.0
CHIME_DETUNE_HZ = 2          # left pad offset from right -> faint chorus beat
CHIME_BODY_FREQ = 147        # D3 pedal under the tones for warmth (Hz)
CHIME_BODY_GAIN = -12        # gentle warmth, well inside the safe motor band
CHIME_BODY_ACTUATOR = HAPTIC_RUMBLE_LEFT

# Game force-feedback → back rumble motors. The XInput large/small motor
# intensities (0..255) each play a continuous tone on one motor; intensity
# scales the (signed) gain, capped below the level the changelog warns can
# damage the motors. Low/high frequencies give the large (heavy) / small
# (buzzy) feel of a normal pad.
RUMBLE_FREQ_LOW = 90     # large motor (left, actuator 3) — heavy
RUMBLE_FREQ_HIGH = 180   # small motor (right, actuator 4) — buzzy
RUMBLE_GAIN_MIN = -40    # lightest audible rumble (intensity 1)
RUMBLE_GAIN_MAX = -4     # strongest (intensity 255), still below the damage zone

# Self-terminating rumble (anti-"infinite buzz"): each motor tone is sent as a
# SHORT bounded burst lasting ~RUMBLE_TONE_SECONDS, and a keepalive thread
# re-arms it every RUMBLE_REFRESH_SECONDS while the game still wants rumble. The
# burst outlasts the refresh so sustained rumble feels continuous — but if this
# process dies (crash / hard kill / a game that quits without zeroing FFB)
# before the usual set_rumble(0,0) stop is delivered, the last burst simply
# lapses on its own within ~RUMBLE_TONE_SECONDS instead of the motor buzzing
# until the controller is power-cycled. (A tone's length in cycles = freq_hz *
# seconds.) This is the same self-expiring contract SDL_RumbleGamepad(ms)
# already gives the SDL pads, so neither controller path can get stuck on.
RUMBLE_TONE_SECONDS = 1.5
RUMBLE_REFRESH_SECONDS = 1.0

# Watchdog: the controller re-enables lizard mode if we don't keep disabling
# it. SDL re-sends every 3s; we use a slightly tighter interval to be safe.
LIZARD_REFRESH_SECONDS = 2.0


class SCStatus(IntEnum):
    INPUT = 0x42       # Triton input-state report type


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
# Stick fields are appended on the end so existing positional uses keep working.
SteamControllerInput = namedtuple(
    'SteamControllerInput',
    'status seq buttons ltrig rtrig lpad_x lpad_y rpad_x rpad_y '
    'lstick_x lstick_y rstick_x rstick_y'
)

SCI_NULL = SteamControllerInput(
    status=0, seq=0, buttons=0,
    ltrig=0, rtrig=0,
    lpad_x=0, lpad_y=0, rpad_x=0, rpad_y=0,
    lstick_x=0, lstick_y=0, rstick_x=0, rstick_y=0,
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


def _build_turn_off_report():
    """Build the feature report that asks the controller to power off.
    Command 0x9F with the 4-byte payload "off!" (same as SDL's driver)."""
    buf = bytearray(FEATURE_REPORT_LEN + 1)  # +1 for report ID prefix
    buf[0] = FEATURE_REPORT_ID
    buf[1] = ID_TURN_OFF_CONTROLLER
    buf[2] = 0x04                     # payload length
    buf[3:7] = b"off!"                # 0x6F 0x66 0x66 0x21
    return list(buf)


TURN_OFF_REPORT = _build_turn_off_report()


def _build_haptic_tone_report(actuator, freq_hz, gain, count=0x7FFF):
    """Build the 0x83 LFO-tone OUTPUT report: play `freq_hz` on `actuator`.
    `count` (bytes 5-6) is the burst length; 0x7FFF ~= continuous (until a
    stop), while a small value plays just a few cycles for a crisp click."""
    f = int(freq_hz) & 0xFFFF
    c = int(count) & 0xFFFF
    buf = bytearray(HID_OUTPUT_REPORT_LEN)  # 65 bytes, id included
    buf[0] = ID_OUT_HAPTIC_LFO_TONE
    buf[1] = actuator & 0xFF
    buf[2] = gain & 0xFF
    buf[3] = f & 0xFF
    buf[4] = (f >> 8) & 0xFF
    buf[5] = c & 0xFF
    buf[6] = (c >> 8) & 0xFF
    return bytes(buf)


def _build_haptic_stop_report(actuator):
    """Build the 0x82 stop OUTPUT report for `actuator`."""
    buf = bytearray(HID_OUTPUT_REPORT_LEN)  # 65 bytes, id included
    buf[0] = ID_OUT_HAPTIC_STOP
    buf[1] = actuator & 0xFF
    return bytes(buf)


def _tone_count(freq_hz, seconds):
    """Burst length (cycles) for a tone of `freq_hz` that lasts ~`seconds`,
    clamped to the report's 16-bit count field. Used to make every sustained
    tone self-expiring so a crash/hard-kill can't leave an actuator buzzing
    (the controller stops the actuator itself once the burst finishes)."""
    return min(0xFFFF, max(1, int(freq_hz * seconds)))


def _rumble_gain(intensity):
    """Map an XInput motor intensity (1..255) to a signed tone gain within the
    safe [RUMBLE_GAIN_MIN, RUMBLE_GAIN_MAX] range (higher = louder)."""
    i = max(1, min(255, int(intensity)))
    return int(round(RUMBLE_GAIN_MIN
                     + (i / 255.0) * (RUMBLE_GAIN_MAX - RUMBLE_GAIN_MIN)))


def _enumerate_data_interfaces():
    """Vendor-specific HID interfaces (usage page 0xFF00, usage 1) for both
    the wireless dongle (PID 0x1304) and the wired controller (PID 0x1302).
    The dongle typically exposes 4 interfaces (one per paired controller)."""
    out = []
    for pid in (PRODUCT_ID_PROTEUS, PRODUCT_ID_WIRED):
        for d in hid.enumerate(VENDOR_ID, pid):
            if d.get('usage_page') == 0xFF00 and d.get('usage') == 1:
                out.append(d)
    out.sort(key=lambda d: (d.get('product_id', 0), d.get('interface_number', 0)))
    return out


def present_product_ids():
    """Set of Steam Controller product IDs (e.g. PRODUCT_ID_PROTEUS for the
    wireless receiver/puck, PRODUCT_ID_WIRED for a USB-C-tethered controller)
    currently enumerable on USB. Cheap presence probe — lists HID, never opens
    a handle — so it works whether or not we (or Steam) hold the device, and
    even when nothing is paired/connected. Used by the tray's device watcher."""
    pids = set()
    try:
        for d in hid.enumerate(VENDOR_ID, 0):
            pid = d.get('product_id')
            if pid:
                pids.add(pid)
    except Exception:
        pass
    return pids


# Battery snapshot handed to callers via SteamController.get_battery().
#   percent         0..100
#   charge_state    raw CHARGE_STATE_* byte
#   charging        True while a charger is supplying power (charging, source-
#                   validate, or charge-complete) — mirrors the reference's
#                   IsCharging (anything but Discharging/Reset).
#   charge_complete True once the pack is full and the charger has stopped.
#   voltage_mv      battery voltage in millivolts (diagnostic).
SteamControllerBattery = namedtuple(
    'SteamControllerBattery',
    'percent charge_state charging charge_complete voltage_mv'
)


def _parse_battery(data):
    """Parse a 0x43 power-status report into a SteamControllerBattery, or None
    if the frame isn't a (valid) battery report.

    Windows hidapi delivers 17 bytes; Linux's hidraw backend trims it to 15
    (firmware-side descriptor differs). All fields we actually use sit in the
    first 5 bytes (report id + state + percent + voltage LE), so we only
    require that much."""
    if len(data) < 5 or data[0] != TRITON_BATTERY_REPORT_ID:
        return None
    percent = data[2]
    if percent > 100:
        return None  # firmware sends 0xFF-ish placeholders before it has a reading
    cs = data[1]
    charging = cs in (CHARGE_STATE_CHARGING,
                      CHARGE_STATE_SOURCE_VALIDATE,
                      CHARGE_STATE_CHARGING_DONE)
    # A 0% reading while not charging isn't a real level — the firmware emits it
    # as the controller powers off (e.g. the Steam+Y turn-off) and before it has
    # taken a reading. Treat it as "no reading" so it can't fire a bogus 0%
    # critical-battery warning. (Mirrors the reference's IsDisplayableController-
    # Battery: a battery is real only if it's charging or percent > 0.)
    if percent == 0 and not charging:
        return None
    return SteamControllerBattery(
        percent=percent,
        charge_state=cs,
        charging=charging,
        charge_complete=cs == CHARGE_STATE_CHARGING_DONE,
        voltage_mv=data[3] | (data[4] << 8),
    )


def _parse_triton(data: bytes) -> SteamControllerInput:
    """Parse a 54-byte Triton input report into the SCI tuple."""
    if len(data) < TRITON_INPUT_MIN_LEN or data[0] not in TRITON_INPUT_REPORT_IDS:
        return None
    # Skip byte 0 (report ID 0x45 / legacy 0x42). Layout after that:
    #   B  seq            (1 byte)
    #   I  buttons        (4 bytes, uint32 LE)
    #   h  sTriggerLeft   (2 bytes, int16)
    #   h  sTriggerRight  (2 bytes, int16)
    #   h  sLeftStickX
    #   h  sLeftStickY
    #   h  sRightStickX
    #   h  sRightStickY
    #   h  sLeftPadX
    #   h  sLeftPadY
    #   H  sPressureLeft  (ignored)
    #   h  sRightPadX
    #   h  sRightPadY
    #   H  sPressureRight (ignored)
    (seq, buttons, ltrig, rtrig,
     lstick_x, lstick_y, rstick_x, rstick_y,
     lpad_x, lpad_y, _pL, rpad_x, rpad_y, _pR) = _TRITON_STRUCT.unpack_from(data, 1)
    return SteamControllerInput(
        status=SCStatus.INPUT,
        seq=seq, buttons=buttons,
        ltrig=ltrig, rtrig=rtrig,
        lpad_x=lpad_x, lpad_y=lpad_y,
        rpad_x=rpad_x, rpad_y=rpad_y,
        lstick_x=lstick_x, lstick_y=lstick_y,
        rstick_x=rstick_x, rstick_y=rstick_y,
    )


# HID-open retry: toggling a block setting ("Block SteamInput Steam Controller grab"
# or "Block SteamInput Xbox Controller grab") closes the current handle and immediately
# reopens in the other mode (shared<->exclusive). The OS can take a moment to
# release the just-closed handle, so the first reopen can hit a transient
# sharing violation in EITHER direction. Retry a few times so the toggle applies
# live (turning the block both on AND off) instead of only after a restart.
# Retries run only on a *failed* open, so a cold/first open — the normal case,
# and every interface probe in _open_first_responsive — pays nothing.
OPEN_RETRY_ATTEMPTS = 5
OPEN_RETRY_DELAY = 0.1


class SteamController:
    """API-compatible with adusk's expectations:
        SteamController(callback, callback_args=None)
        sc.run()
        sc.addExit()
    """

    def __init__(self, callback, callback_args=None, passive=False, exclusive=False):
        self._cb = callback
        self._cb_args = callback_args if callback_args is not None else ()
        self._passive = passive
        # When True, open the controller with no sharing so other apps (Steam)
        # can't grab it. Falls back to shared if exclusive open is denied.
        self._exclusive = exclusive
        self._dev = None
        self._dev_lock = threading.Lock()
        self._exit = threading.Event()
        self._lizard_thread = None
        # True once this instance has successfully opened a controller. Lets the
        # launcher tell "device absent" (open failed) from "ran then was kicked"
        # so it can back off reconnect attempts only when nothing is there.
        self.opened = False
        # In non-passive mode, the lizard state we want the watchdog to keep
        # re-asserting. Defaults to off (XInput / gamepad path). set_lizard()
        # flips this on the fly — used by tray.py to let "hold Steam" briefly
        # re-enable firmware mouse/kb while gamepad mode is active.
        self._lizard_enabled = False
        # Last battery status seen on the wire (a SteamControllerBattery, or
        # None until the controller streams its first 0x43 power report). Set on
        # the read thread, read via get_battery(); a single attribute
        # read/write of an immutable tuple is atomic under the GIL, so no lock.
        self._battery = None
        # time.monotonic() of the last input/battery frame, for get_battery()'s
        # freshness check (see BATTERY_FRESH_SECONDS).
        self._last_frame_t = 0.0
        # Game-rumble keepalive (see set_rumble / _rumble_keepalive): the last
        # requested (large, small) motor intensities, and the daemon thread that
        # re-arms the self-expiring bursts while they're non-zero. A single tuple
        # read/write is atomic under the GIL; the thread is started lazily on the
        # first non-zero rumble and dies with this instance.
        self._rumble_state = (0, 0)
        self._rumble_thread = None

    def _open_device(self, path):
        """Open `path`. In exclusive mode, try a no-sharing open (blocks Steam)
        and fall back to normal shared hidapi if that's denied — e.g. because
        Steam already holds the device — so the controller still works.

        Both opens are retried (see OPEN_RETRY_ATTEMPTS): a block toggle reopens
        in the other mode right after closing our own handle, which can briefly
        race the OS releasing it in either direction. Retries fire only on a
        failed open, so normal opens and the probe loop are unaffected."""
        last_err = None
        if self._exclusive:
            for attempt in range(OPEN_RETRY_ATTEMPTS):
                try:
                    from . import winhid
                    dev = winhid.ExclusiveHidDevice()
                    dev.open_path(path)
                    print("steamcontroller: opened EXCLUSIVE (Steam blocked)")
                    return dev
                except Exception as e:
                    # A sharing violation right after our own close clears once
                    # the OS releases the device; a genuine conflict (Steam holds
                    # it) won't, so cap the retries and then fall back to shared.
                    last_err = e
                    if attempt + 1 < OPEN_RETRY_ATTEMPTS:
                        time.sleep(OPEN_RETRY_DELAY)
            print(f"steamcontroller: exclusive open denied ({last_err}); "
                  "falling back to shared")
        # Shared hidapi open — retried for the same race when a block is toggled
        # OFF and we reopen shared right after releasing the exclusive handle.
        for attempt in range(OPEN_RETRY_ATTEMPTS):
            try:
                dev = hid.device()
                dev.open_path(path)
                return dev
            except Exception as e:
                last_err = e
                if attempt + 1 < OPEN_RETRY_ATTEMPTS:
                    time.sleep(OPEN_RETRY_DELAY)
        raise last_err if last_err is not None else OSError(f"could not open {path}")

    def _open_first_responsive(self):
        global _LAST_GOOD_PATH
        candidates = _enumerate_data_interfaces()
        if not candidates:
            raise RuntimeError(
                "No Steam Controller 2026 interface found "
                f"(VID 0x{VENDOR_ID:04X}, "
                f"PID 0x{PRODUCT_ID_PROTEUS:04X} dongle / "
                f"0x{PRODUCT_ID_WIRED:04X} wired)."
            )

        # Try the last-known-good interface first. Stable sort: the matching
        # path (key False/0) moves to the front, everything else keeps order.
        if _LAST_GOOD_PATH is not None:
            candidates.sort(key=lambda c: c['path'] != _LAST_GOOD_PATH)

        last_err = None
        for cand in candidates:
            path = cand['path']
            try:
                dev = self._open_device(path)
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
                if data and len(data) >= TRITON_INPUT_MIN_LEN and data[0] in TRITON_INPUT_REPORT_IDS:
                    got_input = True
                    break

            if got_input:
                self._dev = dev
                _LAST_GOOD_PATH = path
                print(f"steamcontroller: opened iface {cand['interface_number']}")
                return

            dev.close()

        raise RuntimeError(
            "Found Steam Controller 2026 interfaces but none returned "
            "input reports. Is the controller paired/powered? "
            f"Last error: {last_err!r}"
        )

    def _lizard_watchdog(self):
        """Re-assert whichever lizard state we currently want every
        LIZARD_REFRESH_SECONDS, so the controller's own watchdog doesn't
        revert it. _lizard_enabled is read under _dev_lock so set_lizard()
        can never lose a race with a watchdog tick."""
        while not self._exit.is_set():
            if self._exit.wait(LIZARD_REFRESH_SECONDS):
                return
            with self._dev_lock:
                if self._dev is None:
                    return
                report = (ENABLE_LIZARD_REPORT if self._lizard_enabled
                          else DISABLE_LIZARD_REPORT)
                try:
                    self._dev.send_feature_report(report)
                except Exception:
                    pass

    def set_lizard(self, enabled):
        """Toggle lizard (firmware mouse/kb) mode at runtime. Works in both
        passive and non-passive modes — passive callers use this to briefly
        suppress firmware kb/mouse during chord injections (e.g. so the
        Steam+VIEW → Alt+Tab chord isn't fighting a firmware-emitted Tab
        from the same VIEW button). The hardware watchdog re-asserts lizard
        in 3-5s if we don't keep re-sending, so callers needing longer
        suppression must re-send periodically."""
        with self._dev_lock:
            self._lizard_enabled = bool(enabled)
            if self._dev is None:
                return
            report = (ENABLE_LIZARD_REPORT if self._lizard_enabled
                      else DISABLE_LIZARD_REPORT)
            try:
                self._dev.send_feature_report(report)
            except Exception:
                pass

    def turn_off(self):
        """Ask the controller to power itself off (Steam Input's hold-Steam+Y
        behavior). Sends the ID_TURN_OFF_CONTROLLER feature report. Returns
        True if the report was sent. Experimental on Triton hardware — if the
        firmware ignores 0x9F the controller simply stays on."""
        with self._dev_lock:
            if self._dev is None:
                return False
            try:
                self._dev.send_feature_report(TURN_OFF_REPORT)
                print("steamcontroller: sent turn-off command")
                return True
            except Exception as e:
                print(f"steamcontroller: turn_off failed: {e}")
                return False

    def haptic_tone(self, actuator, freq_hz, gain=HAPTIC_DEFAULT_GAIN, count=0x7FFF):
        """Play an LFO tone on one actuator (0x83). Default `count` plays until
        stopped; a small `count` plays a short burst (a click)."""
        with self._dev_lock:
            if self._dev is None:
                return False
            try:
                self._dev.write(_build_haptic_tone_report(actuator, freq_hz, gain, count))
                return True
            except Exception as e:
                print(f"steamcontroller: haptic_tone failed: {e}")
                return False

    def haptic_stop(self, actuator):
        """Stop the tone on one actuator (0x82)."""
        with self._dev_lock:
            if self._dev is None:
                return False
            try:
                self._dev.write(_build_haptic_stop_report(actuator))
                return True
            except Exception as e:
                print(f"steamcontroller: haptic_stop failed: {e}")
                return False

    def haptic_pad_click(self):
        """'Physical pad click' tick for the simulated trackpad click (press
        AND release) and the L2/R2 selects. A short, crisp, slightly firmer pop
        than the light key tap — high-frequency and brief so it feels like a
        real button click rather than a deep buzz."""
        self.haptic_click(freq_hz=HAPTIC_PAD_CLICK_FREQ, gain=HAPTIC_PAD_CLICK_GAIN,
                          count=4, duration=0.014)

    def haptic_click(self, freq_hz=550, gain=HAPTIC_CLICK_GAIN, count=5, duration=0.018):
        """Crisp trackpad 'click' for UI feedback: play a very short burst
        (`count` cycles) on both trackpad actuators so it snaps rather than
        buzzes. A higher frequency gives a faster attack (snappier onset) and
        the short safety-stop keeps the tail tight, so rapid press/release
        ticks read as distinct clicks instead of smearing into a buzz. Both
        pad writes go out under a single lock for minimal onset latency; the
        timed stop after `duration` is a safety net in case the hardware
        ignores the burst count and plays continuously."""
        pads = (HAPTIC_PAD_LEFT, HAPTIC_PAD_RIGHT)
        with self._dev_lock:
            if self._dev is None:
                return
            try:
                for act in pads:
                    self._dev.write(_build_haptic_tone_report(act, freq_hz, gain, count))
            except Exception as e:
                print(f"steamcontroller: haptic_click failed: {e}")
                return

        def _stop():
            with self._dev_lock:
                if self._dev is None:
                    return
                for act in pads:
                    try:
                        self._dev.write(_build_haptic_stop_report(act))
                    except Exception:
                        pass

        threading.Timer(duration, _stop).start()

    def play_chime(self, on=True):
        """Play a short rising (on) / falling (off) arpeggio on both trackpads
        to confirm a mode change, echoing the controller's power on/off jingle.
        Tones go to the pad actuators (0/1), not the motors, so it's audible
        with no damage risk. Blocks for the chime's duration (~0.35s) — call
        from a worker thread if you don't want to wait, and call it BEFORE the
        device is torn down or the trailing stops will cut the chime short.

        Voicing (chosen by ear): the melody plays on the right pad with the
        left pad a few Hz higher (CHIME_DETUNE_HZ) so they beat together for a
        fuller chorus, over a steady soft low-D pedal on a rumble motor for
        body. A trailing stop silences all three actuators."""
        notes = CHIME_NOTES if on else tuple(reversed(CHIME_NOTES))
        acts = (HAPTIC_PAD_RIGHT, HAPTIC_PAD_LEFT, CHIME_BODY_ACTUATOR)
        for freq, dur in zip(notes, CHIME_DURATIONS):
            # (actuator, frequency, gain) for this note: detuned pad pair + body
            voicing = (
                (HAPTIC_PAD_RIGHT, freq, HAPTIC_CHIME_GAIN),
                (HAPTIC_PAD_LEFT, freq + CHIME_DETUNE_HZ, HAPTIC_CHIME_GAIN),
                (CHIME_BODY_ACTUATOR, CHIME_BODY_FREQ, CHIME_BODY_GAIN),
            )
            with self._dev_lock:
                if self._dev is None:
                    return
                try:
                    for act, f, gain in voicing:
                        # Stop before each tone for a clean onset (also required
                        # on the motor — omitting it there can reboot the unit).
                        # Bounded count so a crash mid-chime self-terminates.
                        self._dev.write(_build_haptic_stop_report(act))
                        self._dev.write(_build_haptic_tone_report(
                            act, f, gain, _tone_count(f, CHIME_TONE_SECONDS)))
                except Exception as e:
                    print(f"steamcontroller: play_chime failed: {e}")
                    return
            time.sleep(dur)
        with self._dev_lock:
            if self._dev is None:
                return
            for act in acts:
                try:
                    self._dev.write(_build_haptic_stop_report(act))
                except Exception:
                    pass

    def _emit_rumble(self, large, small):
        """Write the two back-motor tones for the given intensities as SHORT,
        self-expiring bursts (RUMBLE_TONE_SECONDS). A stop precedes each tone —
        per SteamHapticsSinger this avoids the controller rebooting when
        re-driving the motors. Re-validates against the latest requested state
        under the lock so a concurrent set_rumble(0,0) can't be clobbered by a
        stale keepalive re-arm (which would briefly turn a just-stopped motor
        back on). Returns True if written."""
        with self._dev_lock:
            if self._dev is None:
                return False
            if (large, small) != self._rumble_state:
                # A newer request landed; let its own emit win.
                return False
            try:
                for act, intensity, freq in (
                    (HAPTIC_RUMBLE_LEFT, large, RUMBLE_FREQ_LOW),
                    (HAPTIC_RUMBLE_RIGHT, small, RUMBLE_FREQ_HIGH),
                ):
                    self._dev.write(_build_haptic_stop_report(act))
                    if intensity and intensity > 0:
                        self._dev.write(_build_haptic_tone_report(
                            act, freq, _rumble_gain(intensity),
                            _tone_count(freq, RUMBLE_TONE_SECONDS)))
                return True
            except Exception as e:
                print(f"steamcontroller: set_rumble failed: {e}")
                return False

    def set_rumble(self, large, small):
        """Drive the two back rumble motors from XInput large/small motor
        intensities (0..255); 0 stops a motor. The tones are SELF-TERMINATING
        bursts re-armed by a keepalive thread (see RUMBLE_TONE_SECONDS), so if
        this process dies before the usual set_rumble(0,0) stop is sent, the
        motors fall silent on their own within ~RUMBLE_TONE_SECONDS rather than
        buzzing indefinitely. Returns True if the immediate write succeeded."""
        large = max(0, min(255, int(large)))
        small = max(0, min(255, int(small)))
        self._rumble_state = (large, small)
        ok = self._emit_rumble(large, small)
        if large or small:
            self._ensure_rumble_keepalive()
        return ok

    def _ensure_rumble_keepalive(self):
        """Start the rumble keepalive thread if it isn't already running."""
        t = self._rumble_thread
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=self._rumble_keepalive, daemon=True)
        self._rumble_thread = t
        t.start()

    def _rumble_keepalive(self):
        """Re-arm the bounded rumble bursts every RUMBLE_REFRESH_SECONDS while a
        motor should be on, so sustained game rumble feels continuous. Exits the
        moment the rumble is zero or the device goes away — and then the last
        burst lapses by itself, which is the whole point: a dropped stop (crash,
        hard kill) can never leave a motor stuck on."""
        while not self._exit.is_set():
            large, small = self._rumble_state
            if not (large or small):
                return
            with self._dev_lock:
                gone = self._dev is None
            if gone:
                return
            self._emit_rumble(large, small)
            self._exit.wait(RUMBLE_REFRESH_SECONDS)

    def get_battery(self):
        """Most recent SteamControllerBattery seen on the wire, or None if the
        controller hasn't streamed one yet this session OR has gone silent
        (powered off via Steam+Y / dropped its wireless link) — detected as no
        input/battery frame for BATTERY_FRESH_SECONDS, so the tray doesn't keep
        showing a stale % while the dongle stays plugged in."""
        b = self._battery
        if b is None:
            return None
        if time.monotonic() - self._last_frame_t > BATTERY_FRESH_SECONDS:
            return None
        return b

    def is_live(self):
        """True once the device is open and usable (run() has opened it and it
        hasn't been closed). `opened` alone isn't enough — it stays True after
        close — so we also require a live handle."""
        with self._dev_lock:
            return self.opened and self._dev is not None

    def addExit(self):
        self._exit.set()

    def run(self):
        try:
            self._open_first_responsive()
        except Exception as e:
            print(f"steamcontroller: open failed: {e}")
            return
        self.opened = True

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
                # Power-status and link-status reports stream interleaved with
                # the game-input reports. Pull battery out here (cheap: one byte
                # compare on the read thread, off the watcher hot path) and drop
                # the link-status frames so they aren't mis-parsed as input.
                head = data[0]
                if head == TRITON_BATTERY_REPORT_ID:
                    self._last_frame_t = time.monotonic()
                    batt = _parse_battery(bytes(data))
                    if batt is not None:
                        self._battery = batt
                    continue
                if head in TRITON_WIRELESS_STATUS_IDS:
                    continue
                sci = _parse_triton(bytes(data))
                if sci is None:
                    continue
                # A real input frame — the controller is alive and streaming.
                self._last_frame_t = time.monotonic()
                try:
                    self._cb(self, sci, *self._cb_args)
                except Exception as e:
                    print(f"steamcontroller: callback raised: {e}")
        finally:
            self._exit.set()
            with self._dev_lock:
                try:
                    if self._dev is not None:
                        # Stop any haptics still playing so the controller
                        # doesn't keep buzzing after we release the device
                        # (e.g. a haptic_click whose timed stop hasn't fired).
                        for act in (HAPTIC_PAD_LEFT, HAPTIC_PAD_RIGHT,
                                    HAPTIC_RUMBLE_LEFT, HAPTIC_RUMBLE_RIGHT):
                            try:
                                self._dev.write(_build_haptic_stop_report(act))
                            except Exception:
                                pass
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
