import copy
import time
from collections import deque
from threading import Lock

import steamcontroller.uinput as sui
from steamcontroller import SCButtons, SCStatus, SCI_NULL
from steamcontroller.events import EventMapper

from adusk import inputsrc
from adusk import screen
from adusk.screen import CoordFraction
from adusk import state
from adusk import utils
from adusk import vkb
from adusk import vptr


class ControllerState:
    click_queue = deque()

    _pointers = None
    _pointer_lock = Lock()

    def set_pointers(self, ptr_left, ptr_right):
        with self._pointer_lock:
            self._pointers = (ptr_left, ptr_right)

    def get_pointers(self):
        with self._pointer_lock:
            ret = copy.deepcopy(self._pointers)
        return ret


# Right-stick-as-mouse tuning (shared by every controller while the OSK is
# open). Mirrors the tray's desktop-mode cursor feel.
_MOUSE_DEADZONE = 6000
_MOUSE_SPEED = 1400.0       # px/sec at full stick deflection
# Bigger exponent = longer ramp (more stick travel maps to slow speeds), so
# precise control needs less surgical thumb precision. Matches the tray _Watcher.
_MOUSE_EXPONENT = 5.0
# Minimum speed (fraction of full) the instant the stick passes the deadzone, so
# the first bit of travel moves a usable amount (>1px/frame) instead of the
# near-zero the steep exponent gives — fine control needs perceptible feedback.
_MOUSE_MIN = 0.05


def _mouse_vector(x, y, deadzone, exponent):
    """Radial stick → mouse velocity (vx, vy), each component scaled 0..1. Speed
    is a function of the stick's DISTANCE from center applied to the unit
    direction — NOT per-axis — so a diagonal full push moves at the same speed as
    a pure horizontal/vertical push. (Applying the exponent per-axis made
    diagonals ~radius·exp slower, very visible at high exponents.)"""
    mag = (x * x + y * y) ** 0.5
    if mag <= deadzone:
        return 0.0, 0.0
    m = min(1.0, (mag - deadzone) / (32767.0 - deadzone))
    # Floor + ramp: a small flat minimum the moment we pass the deadzone, then the
    # m**exponent curve on top (the floor only matters near center, where m**exp≈0).
    unit = _MOUSE_MIN + (1.0 - _MOUSE_MIN) * (m ** exponent)
    scaled = unit / mag  # `unit` speed along the unit vector (x/mag, y/mag)
    return x * scaled, y * scaled


def adjust_raw_x(raw_x, center_fraction, scalar=6/5):
    abs_max = 0x20000
    return utils.round_to_int(screen.width * (center_fraction + scalar * raw_x/abs_max))


def adjust_raw_y(raw_y, center_fraction, scalar=6/5):
    abs_max = 0x10000
    return utils.round_to_int(screen.height * (center_fraction + scalar * -raw_y/abs_max))


class ControllerManager:
    pad_smoothing = 0.15
    sc_input_previous = SCI_NULL
    # Grace window after the OSK opens during which a Steam(/Home) release is NOT
    # treated as a "close" gesture. Covers the Steam Controller HID handoff: the
    # OSK appears a beat before its SteamHidSource re-acquires the controller, so
    # the merged Steam reads released first (clearing the open seed below), then
    # the SC reconnects still carrying the open chord's lingering Steam — whose
    # release would otherwise instantly close the just-opened keyboard (~0.5 s).
    # The Switch Pro has no such gap (its SDL frames are already live, so its
    # seed holds), but the grace is harmless for it too.
    _OPEN_CLOSE_GRACE = 1.0

    def __init__(self, controller_state):
        self.controller_state = controller_state

        prev_ptrs = controller_state.get_pointers()
        self.prev_ptr_left = prev_ptrs[0]
        self.prev_ptr_right = prev_ptrs[1]

        # Steam+X / Steam-alone chord tracking. Seed both TRUE: the OSK was just
        # opened by a Steam(+X) chord that may still be held on the first frame —
        # most visibly on SDL pads (Switch Pro: Home+Y), where the OSK appears a
        # beat after the chord, by which point X (Y) is released but Steam (Home)
        # often isn't. Seeding _steam_was_pressed=True stops the first frame from
        # treating that lingering Steam as a fresh press, and _saw_x_during_steam=
        # True marks the opening chord as "used" so the tail of its Steam release
        # doesn't immediately close the keyboard. A later, deliberate Steam tap
        # still closes normally (its own rising edge re-clears the flag).
        self._steam_was_pressed = True
        self._saw_x_during_steam = True
        # When the OSK opened, for _OPEN_CLOSE_GRACE (the Steam-release auto-close
        # is suppressed until this elapses, so the SC reconnect blip can't close).
        self._open_t = time.monotonic()

        self.evm = EventMapper()
        self._map_events()

    def _map_events(self):
        # Face buttons whose action is unconditional ride EventMapper. The
        # conditional bindings (LT/RT switch role while the same-side touchpad
        # is being touched) and the latching ones (L3, B, LGRIP, A, DPAD) are
        # handled manually in handle_input.
        # X → Backspace is handled manually below so it can hold-to-repeat
        # (slow continuous delete); the rest ride EventMapper as single taps.
        self.evm.setButtonAction(SCButtons.Y, sui.Keys.KEY_SPACE)      # Y → Space
        # R4 / R5 back paddles → Space (Steam OSK official mapping).
        self.evm.setButtonAction(SCButtons.RGRIP1, sui.Keys.KEY_SPACE)
        self.evm.setButtonAction(SCButtons.RGRIP2, sui.Keys.KEY_SPACE)

        # Rising-edge latches for manually-handled buttons.
        self._l3_was_pressed = False
        self._b_was_pressed = False
        self._lgrip_was_pressed = False
        self._a_was_pressed = False
        # A-button (press key under cursor) hold-to-repeat clock, same cadence
        # as X; the main thread only repeats it over Backspace.
        self._a_repeat_at = 0.0
        self._start_was_pressed = False   # START / "+" (position-cycle edge)
        self._view_was_pressed = False    # VIEW / "-" (Steam+VIEW Alt+Tab; alone = position-cycle)
        self._alt_held_for_tab = False
        self._dpad_prev = 0
        # X (Backspace) hold-to-repeat: deletes once on press, then slow-repeats
        # while held. _x_repeat_at is the monotonic time of the next repeat.
        self._x_was_pressed = False
        self._x_repeat_at = 0.0
        # Same hold-to-repeat for the pad "enter the key" action (L2/R2 trigger
        # or a physical pad click): while held, re-enter the key on the same
        # BACKSPACE clock. Keyed per pad (by select-button mask) so the left and
        # right pads keep independent timers. The main thread only repeats a hit
        # that lands on Backspace, so holding rubs out text like the X button.
        self._click_repeat_at = {}
        # LT's role is decided on its rising edge from whether the left pad was
        # being touched: "shift" (pressed untouched) or "click" (pressed while
        # touching). Latched until LT is released so a later touch can't flip it.
        self._lt_prev = False
        self._rt_prev = False
        self._lt_role = None
        # Steam + left-stick media chords: track the stick's current direction
        # zone (edge-triggered) and the next allowed repeat time for volume.
        self._stick_zone_prev = "NEUTRAL"
        self._stick_repeat_at = 0.0
        # Left stick → keyboard cursor navigation (when Steam is NOT held).
        # Separate zone/repeat state from the media chord so the two stick
        # roles don't clobber each other's edge tracking.
        self._kbd_stick_zone_prev = "NEUTRAL"
        self._kbd_stick_repeat_at = 0.0
        self._kbd_scroll_at = 0.0  # next left-stick scroll tick (nav-off mode)
        self._kbd_scroll_zone_prev = "NEUTRAL"  # arrow-stick zone for the scroll
        # Fire a single haptic "open" tick on the first input frame.
        self._open_tick_pending = True
        # Steam-hold suppression of firmware lizard (kb/mouse) — see comment
        # in handle_input below.
        self._passive_lizard_suppressed = False
        self._last_lizard_suppress = 0.0
        # Tracks whether we are currently holding KEY_LEFTSHIFT / KEY_ENTER on
        # the OS side (driven by LT/RT but gated by touchpad contact).
        self._shift_active = False
        self._enter_active = False
        # In "control desktop" mode (Sticks Control Keyboard OFF) L2/R2 act as
        # the left/right MOUSE buttons instead of Shift/Enter — unless the pad is
        # being touched, where they keep the OSK key-press role. Track the held
        # state so the button mirrors the trigger (press/release, drag).
        self._mouse_l_active = False
        self._mouse_r_active = False
        self._kb = sui.Keyboard()
        # Right-stick-as-mouse while the OSK is open. Works for ANY controller in
        # the merged frame (Steam Controller, Switch Pro, Xbox, ...): the right
        # stick moves the system cursor so you can point-and-click the keys (or
        # anything else) without closing the keyboard. The right stick is unused
        # by the OSK otherwise (it navigates via the left stick / DPAD / pads).
        self._mouse = sui.Mouse()
        self._mouse_acc_x = 0.0
        self._mouse_acc_y = 0.0
        self._mouse_last_t = 0.0

    def handle_pad_input(self, coord_frac, buttons, touch_button_mask, select_button_mask,
                         click_button_mask=0, allow_click=True, now=0.0,
                         trigger_pressed=False, trigger_prev=False):
        prev = self.sc_input_previous.buttons
        # Releasing the physical pad press rumbles too, so a click feels like a
        # full button: one tick pressing down, one coming back up. Checked
        # before the touch gate so it still fires if the finger lifts off at
        # the same instant the pad-click releases. Uses the stronger pad-click
        # haptic (deeper/more intense than the light UI tick).
        if (not (buttons & click_button_mask)) and (prev & click_button_mask):
            state.pad_click_haptic()
        if not (buttons & touch_button_mask):
            return state.InputState.INACTIVE
        # Two ways to "enter" the key under the pointer while touching:
        #   • the trigger (L2/R2) — only when allowed by its current role —
        #     on the rising edge of the touch+trigger combo;
        #   • a physical pad press (pressing the trackpad down), which always
        #     selects regardless of the trigger role.
        # Each fires a rumble once on the rising edge — that rumble IS the
        # simulated click. Both the physical pad press and the L2/R2 trigger
        # select use the stronger pad-click haptic so all "enter the key"
        # feedback matches. Fired here on the controller thread for lowest
        # latency (fires even off a key).
        # trigger_pressed/_prev are the analog-aware actuation (see
        # _osk_trigger_pressed) so the touchpad-click honors the lowered
        # "Trigger Actuation" setting too, not just the firmware full-pull bit.
        trigger_held = allow_click and trigger_pressed
        pad_clicked = bool(buttons & click_button_mask)
        touch_was = bool(prev & touch_button_mask)
        trigger_edge = trigger_held and (not touch_was or not trigger_prev)
        pad_edge = pad_clicked and not (prev & click_button_mask)
        click_active = trigger_held or pad_clicked
        # Hold-to-repeat, keyed per pad. First hit enters the key, rumbles, and
        # arms the repeat clock; held past BACKSPACE_HOLD_DELAY it re-enters the
        # key every BACKSPACE_REPEAT. Repeat hits are tagged so the main thread
        # only acts on them over Backspace (no rumble on repeat — matches X).
        repeat_key = int(select_button_mask)
        if trigger_edge or pad_edge:
            self.controller_state.click_queue.append(coord_frac)
            state.pad_click_haptic()
            self._click_repeat_at[repeat_key] = now + self.BACKSPACE_HOLD_DELAY
        elif click_active and now >= self._click_repeat_at.get(repeat_key, float("inf")):
            self.controller_state.click_queue.append(("repeat", coord_frac))
            self._click_repeat_at[repeat_key] = now + self.BACKSPACE_REPEAT
        if not click_active:
            self._click_repeat_at.pop(repeat_key, None)
        if click_active:
            return state.InputState.CLICK
        return state.InputState.HOVER

    # Left-stick deflection (int16) past this magnitude counts as a direction.
    STICK_DEADZONE = 14000
    # Volume feel: a tap = one step. Holding up/down past STICK_HOLD_DELAY
    # seconds then rapidly ramps, one step every STICK_VOL_REPEAT seconds.
    STICK_HOLD_DELAY = 0.5
    STICK_VOL_REPEAT = 0.021
    # Left-stick keyboard navigation: tap = one key; held past the delay it
    # repeats one key every KBD_STICK_REPEAT seconds (slow enough to land on
    # the intended key without overshooting).
    KBD_STICK_HOLD_DELAY = 0.35
    KBD_STICK_REPEAT = 0.15
    # Deflection before the left stick steps the OSK key cursor. 32% larger than
    # the base STICK_DEADZONE (20% then another 10%) so the cursor doesn't
    # actuate on a light push (the media chord keeps the smaller STICK_DEADZONE).
    # Mirror this in inputsrc's _GLYPH_LSTICK_THRESHOLD so the glyph swap still
    # tracks real key movement.
    KBD_STICK_DEADZONE = round(STICK_DEADZONE * 1.32)
    # The Switch Pro / SDL pads switch OSK keys at a SMALLER left-stick deflection
    # than the Steam Controller (user pref): 30% below KBD_STICK_DEADZONE. Applied
    # only while an SDL pad is the active controller — see _handle_kbd_stick.
    KBD_STICK_DEADZONE_SDL = round(KBD_STICK_DEADZONE * 0.7)
    # When "Sticks Control Keyboard" is OFF, the SC left stick scrolls the window
    # behind the OSK. It sends ARROW-KEY taps (not mouse-wheel notches) with the
    # SAME deadzone (STICK_DEADZONE) / hold / repeat as the tray _Watcher's
    # desktop arrow-stick, so the scroll speed is identical whether the OSK is
    # open or closed. (A wheel notch scrolls ~3 lines vs an arrow's ~1, which is
    # why the old wheel-based scroll felt faster than the closed-OSK scroll.)
    KBD_SCROLL_HOLD_DELAY = 0.35
    KBD_SCROLL_REPEAT = 0.05 / 0.7 * 1.1
    _SCROLL_ARROW_KEYS = {
        "UP":    sui.Keys.KEY_UP,
        "DOWN":  sui.Keys.KEY_DOWN,
        "LEFT":  sui.Keys.KEY_LEFT,
        "RIGHT": sui.Keys.KEY_RIGHT,
    }
    # Hold-to-repeat cadence for every controller "press a key" path (X, A,
    # L2/R2/pad-click): one hit on press, then (after holding past the delay) a
    # deliberately slow repeat. Single-sourced from vkb so the mouse path and
    # every key (Backspace + arrows) rub out / step at one matched speed.
    BACKSPACE_HOLD_DELAY = vkb.KEY_REPEAT_DELAY
    BACKSPACE_REPEAT = vkb.KEY_REPEAT_INTERVAL

    def _handle_media_stick(self, sc_input, steam_now, now):
        """Steam + left stick → media transport. Up/Down = volume (repeats
        while held); Left/Right = previous/next track (one per deflection).
        Edge-triggered: the stick must return toward center before the same
        direction fires again."""
        x = sc_input.lstick_x
        y = sc_input.lstick_y  # positive = up (same hardware sign as the pads)

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
            # Entering a new non-neutral zone always fires once (the "tap").
            # Then wait STICK_HOLD_DELAY before any rapid repeat begins, so a
            # quick tap (or a sub-second hold) is exactly one step.
            fire = zone != "NEUTRAL"
            is_edge = fire
            self._stick_repeat_at = now + self.STICK_HOLD_DELAY
        elif zone in ("UP", "DOWN") and now >= self._stick_repeat_at:
            # Held past the delay: volume ramps fast. Track skip never repeats.
            fire = True
            self._stick_repeat_at = now + self.STICK_VOL_REPEAT
        self._stick_zone_prev = zone

        if fire and key is not None:
            self._kb.pressEvent([key])
            self._kb.releaseEvent([key])
            # Mark the Steam press as "used" so releasing it doesn't close the OSK.
            self._saw_x_during_steam = True
            # Haptic tick on a volume TAP only (one 2% step) — not the rapid
            # hold-ramp, and not track skip (left/right).
            if is_edge and zone in ("UP", "DOWN"):
                state.haptic_tick()

    def _handle_kbd_stick(self, sc_input, steam_now, now):
        """Left stick → move the on-screen-keyboard cursor (one key per
        deflection; auto-repeats while held). Only active when Steam is NOT
        held, since Steam + left stick is the media chord above. The actual
        cursor move — and its key-switch haptic — happens in the main loop
        via step_cursor, so this just posts DPAD direction events."""
        # With "Keyboard Sticks/Mouse controls" turned off for the ACTIVE
        # controller (its tray submenu), its left stick scrolls the window behind
        # the OSK instead of moving the key cursor — so you can scroll a page
        # while the OSK is open (firmware lizard is OFF while the OSK owns the
        # controller, so the app injects the scroll itself). Applies to the Steam
        # Controller AND the Switch Pro, each per its own toggle.
        active = state.get_active_controller()
        if not state.is_kbd_stick_nav_enabled_for(active):
            self._kbd_stick_zone_prev = "NEUTRAL"
            self._handle_kbd_stick_scroll(sc_input, steam_now, now)
            return
        # Not scrolling: clear the scroll zone so toggling the setting mid-hold
        # re-fires an initial tap instead of treating the deflection as ongoing.
        self._kbd_scroll_zone_prev = "NEUTRAL"
        x = sc_input.lstick_x
        y = sc_input.lstick_y  # positive = up

        # Switch Pro / SDL pads switch keys at a smaller deflection than the SC.
        deadzone = (self.KBD_STICK_DEADZONE_SDL
                    if active == "sdl"
                    else self.KBD_STICK_DEADZONE)
        zone = "NEUTRAL"
        if not steam_now and (abs(x) > deadzone or abs(y) > deadzone):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        fire = False
        if zone != self._kbd_stick_zone_prev:
            # Entering a new direction always steps once (the "tap"), then
            # waits KBD_STICK_HOLD_DELAY before the held auto-repeat begins.
            fire = zone != "NEUTRAL"
            self._kbd_stick_repeat_at = now + self.KBD_STICK_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._kbd_stick_repeat_at:
            fire = True
            self._kbd_stick_repeat_at = now + self.KBD_STICK_REPEAT
        self._kbd_stick_zone_prev = zone

        if fire:
            state.queue_dpad(zone, haptic=True)

    def _handle_kbd_stick_scroll(self, sc_input, steam_now, now):
        """Left stick → scroll the window behind the OSK (used when "Sticks
        Control Keyboard" is off). Sends ARROW-KEY taps on the dominant axis —
        one on entering a direction, then auto-repeating while held — with the
        exact deadzone / hold delay / repeat cadence the tray _Watcher uses for
        desktop arrow-stick scrolling, so the speed matches the OSK-closed scroll.
        The taps land on the focused window behind the no-focus OSK."""
        x = sc_input.lstick_x
        y = sc_input.lstick_y  # positive = up
        dz = self.STICK_DEADZONE
        zone = "NEUTRAL"
        if not steam_now and (abs(x) > dz or abs(y) > dz):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"
        fire = False
        if zone != self._kbd_scroll_zone_prev:
            # New direction (or release): the press fires immediately, then we
            # wait KBD_SCROLL_HOLD_DELAY before the first repeat.
            fire = zone != "NEUTRAL"
            self._kbd_scroll_at = now + self.KBD_SCROLL_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._kbd_scroll_at:
            fire = True
            self._kbd_scroll_at = now + self.KBD_SCROLL_REPEAT
        self._kbd_scroll_zone_prev = zone
        key = self._SCROLL_ARROW_KEYS.get(zone)
        if fire and key is not None:
            self._kb.pressEvent([key])
            self._kb.releaseEvent([key])

    def _osk_trigger_pressed(self, buttons, bit, analog):
        """True if the OSK should treat this trigger (L2/R2) as pressed for
        Shift/Enter. Always true on the firmware full-pull digital bit; with a
        lowered actuation set (tray "Steam Controller" menu) it also engages at a
        lighter analog pull (0..32767). NOTE: no active=="sc" gate — the active
        controller only flips to "sc" on the FULL-pull digital edge, which would
        keep the lighter analog point from ever engaging (chicken-and-egg). The
        menu is SC-only and reads the merged trigger; an SC-only user is the SC."""
        if buttons & bit:
            return True
        thr = state.get_sc_osk_trigger_threshold()
        if thr is None:
            return False
        return analog >= thr

    def handle_input(self, sc, sc_input):
        self.evm.process(sc, sc_input)

        # Haptic feedback: one tick when the keyboard first opens.
        if self._open_tick_pending:
            self._open_tick_pending = False
            state.haptic_tick()

        # Single monotonic timestamp for this frame, used by every hold-to-
        # repeat clock below (DPAD/A, X, the pad-click repeat, media stick).
        now = time.monotonic()
        # Which controller family is driving the OSK right now ("sc" / "sdl").
        # Per-controller settings (pointer speed, Sticks-Control-Keyboard) read
        # the entry for this family so the SC and the Switch Pro can differ.
        active = state.get_active_controller()

        # Right stick -> system mouse cursor so any pad can point-and-click the
        # OSK keys (hover highlights, A presses the hovered key) without closing
        # the keyboard. Sub-pixel motion is accumulated so slow nudges register.
        dt = now - self._mouse_last_t if self._mouse_last_t else 0.0
        self._mouse_last_t = now
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0
        # "Pointer Speed" (tray, per active controller) scales the base px/sec.
        # Radial speed (see _mouse_vector) so diagonals aren't slower than axes.
        mouse_speed = _MOUSE_SPEED * state.get_mouse_speed_for(active)
        _mvecx, _mvecy = _mouse_vector(sc_input.rstick_x, sc_input.rstick_y,
                                       _MOUSE_DEADZONE, _MOUSE_EXPONENT)
        self._mouse_acc_x += _mvecx * mouse_speed * dt
        # Stick-up moves the cursor up; screen Y grows downward, so invert.
        self._mouse_acc_y += -_mvecy * mouse_speed * dt
        _mvx, _mvy = int(self._mouse_acc_x), int(self._mouse_acc_y)
        self._mouse_acc_x -= _mvx
        self._mouse_acc_y -= _mvy
        if _mvx or _mvy:
            self._mouse.move(_mvx, _mvy)

        # Steam held gates the media chords below (Steam + left stick / L3).
        steam_now = bool(sc_input.buttons & (SCButtons.STEAM | SCButtons.QAM))  # "..." (QAM) acts like Steam

        # L3 → Caps Lock, unless Steam is held, in which case Steam + L3 is
        # Play/Pause. Manual rising-edge detection so the binding doesn't
        # re-fire while the user keeps their finger on the stick after clicking.
        l3_pressed = bool(sc_input.buttons & SCButtons.L3)
        if l3_pressed and not self._l3_was_pressed:
            if steam_now:
                self._kb.pressEvent([sui.Keys.KEY_PLAYPAUSE])
                self._kb.releaseEvent([sui.Keys.KEY_PLAYPAUSE])
                # Mark the Steam press as "used" so releasing it doesn't close
                # the OSK (same rule as the Steam + VIEW chord below).
                self._saw_x_during_steam = True
            else:
                self._kb.pressEvent([sui.Keys.KEY_CAPSLOCK])
                self._kb.releaseEvent([sui.Keys.KEY_CAPSLOCK])
        self._l3_was_pressed = l3_pressed

        # Publish touchpad capacitive-touch state so the renderer can hide
        # the L2/R2 hint glyphs while LT/RT's pad-click alternate is active.
        lpad_touched = bool(sc_input.buttons & SCButtons.LPADTOUCH)
        rpad_touched = bool(sc_input.buttons & SCButtons.RPADTOUCH)
        state.set_pad_touched(lpad_touched, rpad_touched)

        # "Control the desktop" mode (Keyboard Sticks/Mouse controls OFF for the
        # ACTIVE controller): the OSK is click-through and L2/R2 act as the LEFT/
        # RIGHT mouse buttons — UNLESS the matching pad is touched, where they
        # keep their OSK key-press role.
        desktop_mode = not state.is_kbd_stick_nav_enabled_for(active)

        # LT (L2) role, fixed at the moment it's pressed:
        #   • touching the left pad → "click" the key under the pointer (queued
        #     by handle_pad_input below); shift state is whatever is currently
        #     latched/held, same as a plain touchpad click with no L2.
        #   • else in desktop mode  → "mouse" = hold the LEFT mouse button.
        #   • else                  → "shift". Held until LT releases, even if you
        #     then touch the pad, so you can slide the pad without dropping Shift.
        lt_pressed = self._osk_trigger_pressed(sc_input.buttons, SCButtons.LT, sc_input.ltrig)
        lt_was = self._lt_prev  # capture before the update below, for handle_pad_input's edge
        if lt_pressed and not self._lt_prev:
            if lpad_touched:
                self._lt_role = "click"
            elif desktop_mode:
                self._lt_role = "mouse"
            else:
                self._lt_role = "shift"
                # Pulling L2 takes over from a mouse-toggled Shift latch and
                # stops the toggle. The controller re-presses Shift just
                # below, so it stays held under L2 instead of the latch.
                # Only the "shift" role does this: a "click" (pad touched) or
                # "mouse" (desktop mode) L2 press is unrelated to Shift, and
                # clearing the latch there would un-latch a Shift the user
                # just toggled on and turn the click's key unshifted.
                vkb.clear_shift_latch(release_os=not self._shift_active)
        elif not lt_pressed:
            self._lt_role = None
        self._lt_prev = lt_pressed
        shift_should_hold = lt_pressed and self._lt_role == "shift"
        if shift_should_hold and not self._shift_active:
            self._kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = True
            state.pad_click_haptic()  # strong tick when Shift engages (match pad click)
        elif not shift_should_hold and self._shift_active:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = False
        # L2 → RIGHT mouse button while in the "mouse" role (press / hold to drag
        # / release). L2/R2 are swapped vs the obvious mapping, per user pref.
        # (_mouse_l_active just means "L2's mouse button is held".)
        mouse_l_hold = lt_pressed and self._lt_role == "mouse"
        if mouse_l_hold and not self._mouse_l_active:
            self._mouse.press("right")
            self._mouse_l_active = True
            state.pad_click_haptic()  # click feedback, like a pad press
        elif not mouse_l_hold and self._mouse_l_active:
            self._mouse.release("right")
            self._mouse_l_active = False

        # RT (R2): right pad touched → OSK key-press (handle_pad_input, below);
        # else in desktop mode → hold the LEFT mouse button; else → Enter.
        rt_pressed = self._osk_trigger_pressed(sc_input.buttons, SCButtons.RT, sc_input.rtrig)
        rt_was = self._rt_prev  # for handle_pad_input's analog-aware click edge
        self._rt_prev = rt_pressed
        enter_should_hold = rt_pressed and not rpad_touched and not desktop_mode
        if enter_should_hold and not self._enter_active:
            self._kb.pressEvent([sui.Keys.KEY_ENTER])
            self._enter_active = True
            state.pad_click_haptic()  # strong tick when Enter engages (match pad click)
        elif not enter_should_hold and self._enter_active:
            self._kb.releaseEvent([sui.Keys.KEY_ENTER])
            self._enter_active = False
        # R2 → LEFT mouse button in desktop mode while the right pad is not
        # touched. (_mouse_r_active just means "R2's mouse button is held".)
        mouse_r_hold = rt_pressed and not rpad_touched and desktop_mode
        if mouse_r_hold and not self._mouse_r_active:
            self._mouse.press("left")
            self._mouse_r_active = True
            state.pad_click_haptic()  # click feedback, like a pad press
        elif not mouse_r_hold and self._mouse_r_active:
            self._mouse.release("left")
            self._mouse_r_active = False

        # Mirror Shift state to the renderer so it can show uppercase labels.
        # OR in the mouse/click latch so a controller frame doesn't stomp a
        # latched Shift (which would desync the display and break the toggle).
        state.set_shift_held(self._shift_active or state.is_shift_latched())

        # B and L4/L5 (LGRIP) close the keyboard, on rising edge.
        b_pressed = bool(sc_input.buttons & SCButtons.B)
        if b_pressed and not self._b_was_pressed:
            state.close()
        self._b_was_pressed = b_pressed

        lgrip_pressed = bool(sc_input.buttons & SCButtons.LGRIP)
        if lgrip_pressed and not self._lgrip_was_pressed:
            state.close()
        self._lgrip_was_pressed = lgrip_pressed

        # DPAD navigates the cursor over the keyboard grid (one step per
        # press). Direction events are queued for the main loop, which knows
        # the layout's pixel widths and can pick the visually-aligned target.
        dpad_mask = (SCButtons.DPAD_UP | SCButtons.DPAD_DOWN
                     | SCButtons.DPAD_LEFT | SCButtons.DPAD_RIGHT)
        dpad_now = sc_input.buttons & dpad_mask
        dpad_newly = (self._dpad_prev ^ dpad_now) & dpad_now
        # DPAD navigation deliberately does NOT clear the Shift latch: a Shift
        # toggled on via DPAD+A must persist while you move the cursor to the
        # keys you want to capitalise, just like the mouse toggle. Only the L2
        # hold model resets the latch (handled in the LT block above).
        if dpad_newly & SCButtons.DPAD_UP:
            state.queue_dpad("UP")
        if dpad_newly & SCButtons.DPAD_DOWN:
            state.queue_dpad("DOWN")
        if dpad_newly & SCButtons.DPAD_LEFT:
            state.queue_dpad("LEFT")
        if dpad_newly & SCButtons.DPAD_RIGHT:
            state.queue_dpad("RIGHT")
        self._dpad_prev = dpad_now

        # A → press the key currently under the DPAD cursor. Press once on the
        # rising edge, then (held past BACKSPACE_HOLD_DELAY) repeat on the
        # BACKSPACE clock. The main thread only repeats a hit that lands on
        # Backspace, so holding A on the delete key rubs out text like X.
        a_pressed = bool(sc_input.buttons & SCButtons.A)
        a_row, a_col = state.get_cursor()
        if a_pressed and not self._a_was_pressed:
            # A is a press/toggle model like the mouse: pressing it does NOT
            # clear the Shift latch, so a Shift toggled on via DPAD+A stays on
            # until Shift is pressed again (only L2's hold model resets it).
            state.queue_key_press(a_row, a_col)
            self._a_repeat_at = now + self.BACKSPACE_HOLD_DELAY
        elif a_pressed and now >= self._a_repeat_at:
            state.queue_key_press(a_row, a_col, repeat=True)
            self._a_repeat_at = now + self.BACKSPACE_REPEAT
        # Paint the cursor key blue (CLICK) while A is held, so a controller
        # press flashes like a mouse click. Reuses the mouse press-cell slot;
        # only touched on A's edges/hold so it never clobbers a mouse press.
        if a_pressed:
            state.set_mouse_press_cell((a_row, a_col))
        elif self._a_was_pressed:
            state.set_mouse_press_cell(None)
        self._a_was_pressed = a_pressed

        # Visual highlight: paint the on-screen key blue while its bound
        # controller button is held down.
        highlights = set()
        if self._shift_active or state.is_shift_latched():
            highlights.add(sui.Keys.KEY_LEFTSHIFT)
            highlights.add(sui.Keys.KEY_RIGHTSHIFT)
        if l3_pressed and not steam_now:
            highlights.add(sui.Keys.KEY_CAPSLOCK)
        if sc_input.buttons & SCButtons.X:
            highlights.add(sui.Keys.KEY_BACKSPACE)
        if self._enter_active:
            highlights.add(sui.Keys.KEY_ENTER)
        if sc_input.buttons & (SCButtons.Y | SCButtons.RGRIP):
            highlights.add(sui.Keys.KEY_SPACE)
        state.set_highlighted(highlights)

        # Steam+X opens the keyboard; Steam pressed and released alone closes it.
        # (steam_now was computed at the top of this method.)
        x_now = bool(sc_input.buttons & SCButtons.X)
        if steam_now and not self._steam_was_pressed:
            self._saw_x_during_steam = False
        if steam_now and x_now and not self._saw_x_during_steam:
            self._saw_x_during_steam = True
            state.show()

        # The OSK owns the controller while it's open, so firmware lizard
        # (kb/mouse) must stay OFF the whole time — otherwise the firmware
        # ALSO emits its own keys/clicks (D-pad→arrows, A→click/Enter) into the
        # focused window on top of the OSK reading the same buttons. In gamepad
        # mode the OSK opens with Steam+X (Steam held); releasing Steam used to
        # restore lizard ON, which then navigated and launched items in the
        # focused Start menu while the user typed. The device is opened lizard-
        # off (with a watchdog); re-assert OFF during a Steam hold (so a
        # Steam+VIEW=Alt+Tab chord isn't fought by a firmware Tab) and force it
        # back OFF — never ON — on release.
        if steam_now:
            if (not self._passive_lizard_suppressed
                    or now - self._last_lizard_suppress > 2.0):
                sc.set_lizard(False)
                self._passive_lizard_suppressed = True
                self._last_lizard_suppress = now
        elif self._passive_lizard_suppressed:
            sc.set_lizard(False)
            self._passive_lizard_suppressed = False

        # X → Backspace, handled here (not via EventMapper) so holding it
        # slow-repeats the delete. One delete on press, then after
        # BACKSPACE_HOLD_DELAY a delete every BACKSPACE_REPEAT seconds. Gated
        # off while Steam is held, since Steam+X opens the keyboard.
        x_pressed = bool(sc_input.buttons & SCButtons.X) and not steam_now
        if x_pressed and not self._x_was_pressed:
            self._kb.pressEvent([sui.Keys.KEY_BACKSPACE])
            self._kb.releaseEvent([sui.Keys.KEY_BACKSPACE])
            self._x_repeat_at = now + self.BACKSPACE_HOLD_DELAY
        elif x_pressed and now >= self._x_repeat_at:
            self._kb.pressEvent([sui.Keys.KEY_BACKSPACE])
            self._kb.releaseEvent([sui.Keys.KEY_BACKSPACE])
            self._x_repeat_at = now + self.BACKSPACE_REPEAT
        self._x_was_pressed = x_pressed

        # Steam + left stick → media transport (volume / track skip).
        self._handle_media_stick(sc_input, steam_now, now)

        # Left stick (no Steam) → move the on-screen-keyboard cursor.
        self._handle_kbd_stick(sc_input, steam_now, now)

        # Steam + VIEW ("-" on the Switch Pro / small button upper-right of the
        # Steam logo) → Alt+Tab. Hold Alt for the duration of the Steam hold so
        # the switcher stays visible; each VIEW rising edge taps Tab once to
        # advance one slot. Releasing Steam drops Alt and commits the selection.
        # Marks the Steam press as "used" so releasing Steam doesn't close the OSK.
        view_now = bool(sc_input.buttons & SCButtons.VIEW)
        if steam_now and view_now and not self._view_was_pressed:
            if not self._alt_held_for_tab:
                self._kb.pressEvent([sui.Keys.KEY_LEFTALT])
                self._alt_held_for_tab = True
            self._kb.pressEvent([sui.Keys.KEY_TAB])
            self._kb.releaseEvent([sui.Keys.KEY_TAB])
            self._saw_x_during_steam = True
        elif view_now and not self._view_was_pressed:
            # VIEW alone ("-") → advance the OSK position rotation. This is the
            # Steam Controller's original position-cycle button.
            state.request_position_cycle()
        self._view_was_pressed = view_now
        # START ("+" on the Switch Pro / Start on other pads) → same OSK position
        # cycle, the action as the Move key held with Shift. Rising edge so a held
        # button cycles once. NOTE: the Steam Controller and SDL pads are
        # OR-merged before they reach here, so we can't tell which pad pressed
        # what — accepting BOTH "-" and "+" lets each controller keep its
        # expected button (Steam Controller "-", Switch Pro / other pads "+").
        start_now = bool(sc_input.buttons & SCButtons.START)
        if start_now and not self._start_was_pressed:
            state.request_position_cycle()
        self._start_was_pressed = start_now
        if not steam_now and self._alt_held_for_tab:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self._alt_held_for_tab = False

        if (self._steam_was_pressed and not steam_now and not self._saw_x_during_steam
                and now - self._open_t > self._OPEN_CLOSE_GRACE):
            state.close()
        self._steam_was_pressed = steam_now

        if self.sc_input_previous == SCI_NULL:
            self.sc_input_previous = sc_input
            return

        ptr_left_coords = CoordFraction.from_absolute(adjust_raw_x(sc_input.lpad_x, 1/4),
                                                      adjust_raw_y(sc_input.lpad_y, 1/2))
        ptr_right_coords = CoordFraction.from_absolute(adjust_raw_x(sc_input.rpad_x, 3/4),
                                                       adjust_raw_y(sc_input.rpad_y, 1/2))

        input_state_left = self.handle_pad_input(ptr_left_coords, sc_input.buttons,
                                                 SCButtons.LPADTOUCH, SCButtons.LT,
                                                 click_button_mask=SCButtons.LPAD,
                                                 allow_click=self._lt_role == "click",
                                                 now=now,
                                                 trigger_pressed=lt_pressed, trigger_prev=lt_was)
        input_state_right = self.handle_pad_input(ptr_right_coords, sc_input.buttons,
                                                  SCButtons.RPADTOUCH, SCButtons.RT,
                                                  click_button_mask=SCButtons.RPAD,
                                                  now=now,
                                                  trigger_pressed=rt_pressed, trigger_prev=rt_was)

        ptr_left = vptr.VirtualPointer(input_state_left, ptr_left_coords)
        ptr_right = vptr.VirtualPointer(input_state_right, ptr_right_coords)

        ptr_left.smoothen(self.prev_ptr_left, self.pad_smoothing)
        ptr_right.smoothen(self.prev_ptr_right, self.pad_smoothing)
        self.prev_ptr_left = copy.deepcopy(ptr_left)
        self.prev_ptr_right = copy.deepcopy(ptr_right)
        self.sc_input_previous = sc_input

        self.controller_state.set_pointers(ptr_left, ptr_right)

    def release_held(self):
        """Release anything we're holding on the OS side so closing the OSK
        (which tears down this manager) can never strand a key or — worse — a
        mouse button down. Called from input_thread's finally."""
        if self._mouse_l_active:  # L2 holds the RIGHT button (swapped)
            self._mouse.release("right")
            self._mouse_l_active = False
        if self._mouse_r_active:  # R2 holds the LEFT button (swapped)
            self._mouse.release("left")
            self._mouse_r_active = False
        if self._shift_active:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = False
        if self._enter_active:
            self._kb.releaseEvent([sui.Keys.KEY_ENTER])
            self._enter_active = False


def update(sc, sc_input, manager):
    if state.should_close():
        # Adusk is shutting down — tell the controller thread to exit so it
        # can run its cleanup (re-enable lizard mode) before being killed.
        sc.addExit()
        return
    if sc_input.status != SCStatus.INPUT:
        return
    manager.handle_input(sc, sc_input)


# Delay between controller (re)connect attempts while the keyboard is open but
# no controller is responding. Only ticks in that transient state; once a
# controller is open sc.run() blocks (no polling), and a closed keyboard isn't
# running this thread at all.
_RECONNECT_DELAY = 0.5


# Poll/merge cadence. The Steam Controller still streams at its own HID rate
# (SteamHidSource stashes its latest frame on its own thread); this loop reads
# every source, OR-merges, and dispatches one combined frame. ~250 Hz keeps the
# touchpad-pointer and haptic latency low without busy-spinning.
_MERGE_INTERVAL = 0.004


def input_thread(controller_state):
    manager = ControllerManager(controller_state)
    # Input sources merged into one stream: the custom Steam Controller hidapi
    # driver (trackpads, tuned haptics, lizard) PLUS every SDL-recognized pad
    # (Xbox, DualSense, Switch Pro, ...). Both synthesize SteamControllerInput;
    # InputMerger OR-merges them and is the `sc` facade handle_input drives
    # (set_lizard + the two haptic ticks fan out to every source). With no SDL
    # pad attached the merged frame equals the Steam Controller's exactly, so
    # the proven SC-only path is unchanged. The merger's sources self-reconnect,
    # so a controller plugged in mid-session starts working without reopening.
    merger = inputsrc.InputMerger()
    merger.add(inputsrc.SteamHidSource())
    # SDL pads (Xbox/DualSense/Switch Pro) are read by the tray's one
    # sdl_gamepad_thread and published via state.set_sdl_frame(); adusk just
    # consumes those frames. Opening a second Sdl3GamepadSource here double-drove
    # the same pad across two threads and delivered no input.
    merger.add(inputsrc.SharedSdlFrameSource())
    # Expose haptic "ticks" to the main thread (dispatch_key buzzes on each key
    # press; the stronger pad-click tick for the simulated trackpad click).
    # Cleared on exit so a closed device's haptic methods are never called.
    state.set_haptic_tick(merger.haptic_click)
    state.set_pad_click_haptic(merger.haptic_pad_click)
    try:
        while not state.should_close():
            merged = merger.poll()
            if merged is not None:
                update(merger, merged, manager)
            time.sleep(_MERGE_INTERVAL)
    finally:
        # Drop any OS key / mouse button we were holding before tearing down,
        # so closing the OSK mid-pull can't strand (e.g.) the left mouse button.
        manager.release_held()
        state.set_haptic_tick(None)
        state.set_pad_click_haptic(None)
        # Stops the SDL pads and signals the Steam Controller thread to exit so
        # its cleanup (re-enable lizard mode) runs before we return.
        merger.close()
