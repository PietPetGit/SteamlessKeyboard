"""Multi-controller input sources for the on-screen keyboard.

The custom Steam Controller hidapi driver (steamcontroller/) stays the primary
backend. This adds an SDL3 backend so ANY SDL-recognized pad (Xbox, DualSense,
Switch Pro, 8BitDo, ...) drives the same OSK. Every active source is polled each
frame and OR-merged into ONE SteamControllerInput, which the existing
ControllerManager.handle_input consumes unchanged.

INVARIANT: when no SDL pad is connected, the merged frame equals the Steam
Controller's frame exactly (OR with 0 / max with 0 / untouched-pad), so the
proven Steam-Controller-only path never changes behavior.

Each source exposes:
    poll()            -> latest SteamControllerInput, or None when it has no
                         live device (so the merger can skip it and, when ALL
                         sources are idle, the input loop does no work)
    set_lizard(bool)  -> firmware kb/mouse toggle (no-op for generic pads)
    haptic_click()    -> light "key tap" feedback
    haptic_pad_click()-> firmer "select" feedback
    addExit()/close() -> teardown
An InputMerger fans set_lizard/haptics out to every source and presents the same
interface handle_input expects from a SteamController (the `sc` argument).
"""

import time
from threading import Lock, Thread

import sdl3w as S
from steamcontroller import (SteamController, SCButtons, SCStatus, SCI_NULL,
                             SteamControllerInput)

from adusk import state


# Reconnect cadence for a dropped/absent Steam Controller (mirrors the old
# input_thread loop).
_RECONNECT_DELAY = 0.5


def _clamp16(v):
    return -32767 if v < -32767 else 32767 if v > 32767 else v


# Stick magnitude (of 32767) above which a frame counts as "actively in use".
# Comfortably above resting drift (~3000) but below an intentional push.
_ACTIVITY_STICK = 8000


def _frame_has_activity(f):
    """True if this input frame shows the controller is actively being used
    (any button/trigger, or a stick pushed past the deadzone). Used to decide
    which controller 'owns' the current interaction so haptics go only to it."""
    if f.buttons:
        return True
    return (abs(f.lstick_x) > _ACTIVITY_STICK or abs(f.lstick_y) > _ACTIVITY_STICK
            or abs(f.rstick_x) > _ACTIVITY_STICK or abs(f.rstick_y) > _ACTIVITY_STICK)


def merge_inputs(a, b):
    """OR-merge two SteamControllerInput frames into one. Buttons OR together,
    triggers take the max, each stick takes the larger-magnitude source, and a
    trackpad's coordinates come from whichever source is actively touching it
    (`a` wins ties — pass the Steam Controller as `a` so its tuned pads lead)."""
    buttons = a.buttons | b.buttons
    ltrig = a.ltrig if a.ltrig >= b.ltrig else b.ltrig
    rtrig = a.rtrig if a.rtrig >= b.rtrig else b.rtrig
    lstick_x = a.lstick_x if abs(a.lstick_x) >= abs(b.lstick_x) else b.lstick_x
    lstick_y = a.lstick_y if abs(a.lstick_y) >= abs(b.lstick_y) else b.lstick_y
    rstick_x = a.rstick_x if abs(a.rstick_x) >= abs(b.rstick_x) else b.rstick_x
    rstick_y = a.rstick_y if abs(a.rstick_y) >= abs(b.rstick_y) else b.rstick_y
    if (b.buttons & SCButtons.LPADTOUCH) and not (a.buttons & SCButtons.LPADTOUCH):
        lpad_x, lpad_y = b.lpad_x, b.lpad_y
    else:
        lpad_x, lpad_y = a.lpad_x, a.lpad_y
    if (b.buttons & SCButtons.RPADTOUCH) and not (a.buttons & SCButtons.RPADTOUCH):
        rpad_x, rpad_y = b.rpad_x, b.rpad_y
    else:
        rpad_x, rpad_y = a.rpad_x, a.rpad_y
    return SteamControllerInput(
        status=SCStatus.INPUT, seq=a.seq, buttons=buttons,
        ltrig=ltrig, rtrig=rtrig,
        lpad_x=lpad_x, lpad_y=lpad_y, rpad_x=rpad_x, rpad_y=rpad_y,
        lstick_x=lstick_x, lstick_y=lstick_y,
        rstick_x=rstick_x, rstick_y=rstick_y)


class SteamHidSource:
    """The custom Steam Controller hidapi driver, made pollable. Runs
    SteamController.run() on its own thread (with reconnect), stashing the
    latest input frame for the merge loop to read. Haptics/lizard forward to
    the live device; teardown lets run()'s cleanup restore lizard mode."""

    # Identifies this source's controller family for the OSK glyph swap (Steam
    # Controller L2/R2 vs an SDL pad's ZL/ZR). See InputMerger.poll().
    controller_kind = "sc"

    # No input frame for this long => treat the device as released/gone.
    STALE_AFTER = 1.0

    def __init__(self):
        self._lock = Lock()
        self._sc = None
        self._latest = SCI_NULL
        self._latest_t = 0.0
        self._exit = False
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _on_frame(self, sc, sci):
        # Runs on the SteamController read thread, once per HID input frame.
        with self._lock:
            self._latest = sci
            self._latest_t = time.monotonic()

    def _run_loop(self):
        while not self._exit and not state.should_close():
            sc = SteamController(callback=self._on_frame)
            with self._lock:
                self._sc = sc
            try:
                sc.run()  # blocks until the device drops or addExit() fires
            except Exception as e:
                print(f"SteamHidSource: run error: {e!r}")
            finally:
                with self._lock:
                    self._sc = None
                    self._latest = SCI_NULL
            if self._exit or state.should_close():
                break
            time.sleep(_RECONNECT_DELAY)

    def poll(self):
        with self._lock:
            if self._sc is None:
                return None
            if time.monotonic() - self._latest_t > self.STALE_AFTER:
                return None
            return self._latest

    def _live(self):
        with self._lock:
            return self._sc

    def set_lizard(self, enabled):
        sc = self._live()
        if sc is not None:
            sc.set_lizard(enabled)

    def haptic_click(self):
        sc = self._live()
        if sc is not None:
            sc.haptic_click()

    def haptic_pad_click(self):
        sc = self._live()
        if sc is not None:
            sc.haptic_pad_click()

    def addExit(self):
        sc = self._live()
        if sc is not None:
            sc.addExit()

    def close(self):
        self._exit = True
        self.addExit()
        self._thread.join(timeout=1.0)


# SDL gamepad button -> Steam Controller button bit. Uses the SCButtons VALUES
# so a synthesized frame is bit-identical to a real Triton frame: both the OSK
# (which reads SCButtons names) and the ViGEm bridge (which reads the matching
# C++ byte positions) then treat an SDL pad exactly like the Steam Controller.
_SDL_TO_SC = [
    (S.SDL_GAMEPAD_BUTTON_SOUTH, SCButtons.A),
    (S.SDL_GAMEPAD_BUTTON_EAST,  SCButtons.B),
    (S.SDL_GAMEPAD_BUTTON_WEST,  SCButtons.X),
    (S.SDL_GAMEPAD_BUTTON_NORTH, SCButtons.Y),
    (S.SDL_GAMEPAD_BUTTON_BACK,  SCButtons.VIEW),
    (S.SDL_GAMEPAD_BUTTON_START, SCButtons.START),
    (S.SDL_GAMEPAD_BUTTON_GUIDE, SCButtons.STEAM),
    (S.SDL_GAMEPAD_BUTTON_LEFT_STICK,  SCButtons.L3),
    (S.SDL_GAMEPAD_BUTTON_RIGHT_STICK, SCButtons.R3),
    (S.SDL_GAMEPAD_BUTTON_LEFT_SHOULDER,  SCButtons.LB),
    (S.SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER, SCButtons.RB),
    (S.SDL_GAMEPAD_BUTTON_DPAD_UP,    SCButtons.DPAD_UP),
    (S.SDL_GAMEPAD_BUTTON_DPAD_DOWN,  SCButtons.DPAD_DOWN),
    (S.SDL_GAMEPAD_BUTTON_DPAD_LEFT,  SCButtons.DPAD_LEFT),
    (S.SDL_GAMEPAD_BUTTON_DPAD_RIGHT, SCButtons.DPAD_RIGHT),
    # Back paddles -> grips (close / space), matching the SC paddle bindings.
    (S.SDL_GAMEPAD_BUTTON_LEFT_PADDLE1,  SCButtons.LGRIP1),
    (S.SDL_GAMEPAD_BUTTON_LEFT_PADDLE2,  SCButtons.LGRIP2),
    (S.SDL_GAMEPAD_BUTTON_RIGHT_PADDLE1, SCButtons.RGRIP1),
    (S.SDL_GAMEPAD_BUTTON_RIGHT_PADDLE2, SCButtons.RGRIP2),
]

# Trigger pull (0..32767) at/above which the LT/RT *digital* bit engages — the
# OSK uses it for Shift (L2) / Enter (R2) / select. The analog ltrig/rtrig is
# always carried too (for the ViGEm Xbox triggers).
_TRIGGER_DIGITAL_ON = 12000


class Sdl3GamepadSource:
    """Polls every SDL-recognized gamepad and synthesizes a SteamControllerInput
    (OR-merged across multiple pads). No trackpad-pointer synthesis — SDL pads
    drive the OSK via DPAD/stick navigation + A to press, triggers for
    Shift/Enter, X=Backspace, Y=Space (the trackpad pointer stays a Steam
    Controller exclusive)."""

    # Generic SDL pads (Switch Pro / DualSense / Xbox) — drives the OSK to the
    # Switch Pro ZL/ZR glyphs. See InputMerger.poll().
    controller_kind = "sdl"

    def __init__(self):
        self._pads = {}          # instance_id -> SDL_Gamepad*
        self._next_scan = 0.0
        self._available = True
        # instance_id -> monotonic time that pad last had a button/trigger/stick
        # active. Used to target key-press haptics at the pad actually being
        # used, so a second connected controller doesn't buzz when the first one
        # types. See _active_gamepad / haptic_click.
        self._pad_active_t = {}
        # adusk.main initializes SDL with SDL_INIT_GAMEPAD before the input
        # thread starts; if that ever fails, every SDL call below no-ops.

    def _rescan(self):
        try:
            pads = S.list_gamepads()  # [(instance_id, name)]
        except Exception:
            return
        # Exclude the Steam Controller: the custom hidapi driver owns it, and
        # letting SDL open it too would double-drive the same device and fight
        # over the HID handle. (Generic pads — Xbox/DualSense/etc. — are kept.)
        names = {jid: (name or "") for jid, name in pads
                 if "steam" not in (name or "").lower()}
        ids = set(names)
        for jid in list(self._pads):
            if jid not in ids:
                g = self._pads[jid]
                # Best-effort: clear the Home LED as the pad leaves so a
                # controller that lit blue for gamepad mode doesn't keep the LED
                # stuck on. NOTE: once a controller is physically detached SDL
                # refuses LED writes to it, so this only lands when the pad is
                # still reachable (a soft/transient drop); a hard yank can't be
                # cleared remotely — the controller holds it until it sleeps or
                # reconnects, at which point _home_led_applied below re-applies.
                try:
                    S.SDL_SetGamepadLED(g, 0, 0, 0)
                except Exception:
                    pass
                try:
                    S.SDL_CloseGamepad(g)
                except Exception:
                    pass
                del self._pads[jid]
                self._pad_active_t.pop(jid, None)
                print(f"Sdl3GamepadSource: closed gamepad {jid}")
        for jid in ids:
            if jid not in self._pads:
                try:
                    g = S.SDL_OpenGamepad(jid)
                except Exception:
                    g = None
                if g:
                    self._pads[jid] = g
                    print(f"Sdl3GamepadSource: opened gamepad {jid} ({names[jid]})")

    def _read_pad(self, g):
        buttons = 0
        for sdl_btn, sc_bit in _SDL_TO_SC:
            try:
                if S.SDL_GetGamepadButton(g, sdl_btn):
                    buttons |= sc_bit
            except Exception:
                pass
        lt = S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_LEFT_TRIGGER)
        rt = S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_RIGHT_TRIGGER)
        lt = lt if lt > 0 else 0
        rt = rt if rt > 0 else 0
        if lt >= _TRIGGER_DIGITAL_ON:
            buttons |= SCButtons.LT
        if rt >= _TRIGGER_DIGITAL_ON:
            buttons |= SCButtons.RT
        lx = _clamp16(S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_LEFTX))
        ly = _clamp16(S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_LEFTY))
        rx = _clamp16(S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_RIGHTX))
        ry = _clamp16(S.SDL_GetGamepadAxis(g, S.SDL_GAMEPAD_AXIS_RIGHTY))
        # Hardware sticks are +up; SDL reports +down — invert Y so the OSK and
        # the ViGEm/XInput bridge both see the right vertical sign.
        return SteamControllerInput(
            status=SCStatus.INPUT, seq=0, buttons=buttons,
            ltrig=lt, rtrig=rt,
            lpad_x=0, lpad_y=0, rpad_x=0, rpad_y=0,
            lstick_x=lx, lstick_y=-ly, rstick_x=rx, rstick_y=-ry)

    def _pump(self):
        """Shared per-frame SDL housekeeping for poll()/poll_all(): refresh
        gamepad state and periodically rescan for (dis)connects. Returns the
        monotonic 'now', or None when SDL is unavailable / the update failed."""
        if not self._available:
            return None
        try:
            # SDL_UpdateGamepads only refreshes already-open pads; it does not
            # run udev hotplug detection. Without SDL_PumpEvents, a controller
            # plugged in after this source was created never appears in
            # SDL_GetGamepads() (list_gamepads() in _rescan), so it's invisible
            # until the program is restarted. Pumping here makes hotplugs show
            # up live.
            S.SDL_PumpEvents()
            S.SDL_UpdateGamepads()
        except Exception:
            return None
        now = time.monotonic()
        if now >= self._next_scan:
            self._next_scan = now + 0.5
            self._rescan()
        return now

    def poll(self):
        return self.poll_all()[0]

    def poll_all(self):
        """Both views the tray's gamepad path needs, from ONE pump: the
        OR-merged frame (drives the single desktop user's OSK-open / mouse /
        chords) AND a per-pad dict {instance_id: SteamControllerInput}, one
        entry per open SDL pad, NOT merged — so each physical controller can
        drive its OWN dedicated virtual XInput device (automatic multiplayer).
        Returns (merged_or_None, frames_dict); (None, {}) when no pad is live.
        Scales to any number of pads / any mix of controller types."""
        now = self._pump()
        if now is None or not self._pads:
            return None, {}
        merged = None
        frames = {}
        for jid, g in list(self._pads.items()):
            try:
                f = self._read_pad(g)
            except Exception:
                continue
            # Remember which pad is actively being used, so the key-press haptic
            # can buzz only that controller. Counts BOTH buttons/triggers AND
            # stick deflection — the OSK navigates its grid with the left stick
            # (an axis, not a button), so a button-only check missed every
            # stick-driven key change and the haptic went silent.
            if _frame_has_activity(f):
                self._pad_active_t[jid] = now
            frames[jid] = f
            merged = f if merged is None else merge_inputs(merged, f)
        return merged, frames

    # A pad counts as the "active" one for haptics if it was used this recently
    # (covers the brief gap between the input and the key dispatch that fires
    # the tick).
    _ACTIVE_WINDOW = 1.0

    def _active_gamepad(self):
        """The SDL_Gamepad* of the pad most recently providing input, or None
        if nothing has been pressed lately. Used so key-press haptics go to the
        controller actually typing, not every connected pad."""
        now = time.monotonic()
        best_jid, best_t = None, 0.0
        for jid, t in self._pad_active_t.items():
            if t > best_t:
                best_jid, best_t = jid, t
        if best_jid is not None and (now - best_t) <= self._ACTIVE_WINDOW:
            return self._pads.get(best_jid)
        return None

    def _rumble(self, low, high, ms):
        for g in list(self._pads.values()):
            try:
                S.SDL_RumbleGamepad(g, low, high, ms)
            except Exception:
                pass

    def _rumble_one(self, g, low, high, ms):
        if g is None:
            return
        try:
            S.SDL_RumbleGamepad(g, low, high, ms)
        except Exception:
            pass

    def haptic_click(self):
        # Only the pad currently being used buzzes on a key press. Biased hard to
        # the HIGH-frequency motor with a short pulse: the high-freq actuator has
        # a much faster attack than the low-freq one, so the tick is FELT sooner
        # (sharper, less "delayed") instead of ramping up softly. Kept low
        # amplitude so it's a light tick, not a jolt. Tunable.
        self._rumble_one(self._active_gamepad(), 0x0200, 0x0C00, 10)

    def haptic_pad_click(self):
        self._rumble_one(self._active_gamepad(), 0x1A00, 0x2800, 24)

    def set_rumble(self, large, small):
        """Game force-feedback: large/small motor (0..255) -> a sustained
        rumble, refreshed on each change (1s window so it persists between
        updates; the game sends 0,0 to stop). Targets the active pad if one is
        clearly in use, else all pads (so a single idle pad still rumbles)."""
        low = max(0, min(255, int(large))) * 257
        high = max(0, min(255, int(small))) * 257
        g = self._active_gamepad()
        if g is not None:
            self._rumble_one(g, low, high, 1000)
        else:
            self._rumble(low, high, 1000)

    def set_rumble_pad(self, jid, large, small):
        """Game force-feedback for ONE specific pad (by SDL instance id) — used
        in separate-XInput mode so each player's virtual pad rumbles only its
        own physical controller, never the others."""
        g = self._pads.get(jid)
        if g is None:
            return
        low = max(0, min(255, int(large))) * 257
        high = max(0, min(255, int(small))) * 257
        self._rumble_one(g, low, high, 1000)

    def has_pads(self):
        return bool(self._pads)

    def set_lizard(self, enabled):
        pass  # generic pads have no firmware lizard mode

    def addExit(self):
        pass

    def close(self):
        for g in list(self._pads.values()):
            # Clear the Home LED on the way out so quitting the app doesn't leave
            # a controller's gamepad-mode LED stuck blue. The pad is still live
            # here (unlike a yanked disconnect), so this actually lands.
            try:
                S.SDL_SetGamepadLED(g, 0, 0, 0)
            except Exception:
                pass
            try:
                S.SDL_CloseGamepad(g)
            except Exception:
                pass
        self._pads.clear()
        # The SDL_INIT_GAMEPAD subsystem is torn down by adusk.main's SDL_Quit.


class SharedSdlFrameSource:
    """Reads SDL-pad frames published by the tray's sdl_gamepad_thread rather
    than polling SDL itself.

    The tray already owns one working Sdl3GamepadSource (it detects Guide+X to
    open the OSK). Having adusk open a SECOND Sdl3GamepadSource on its input
    thread double-drove the same pad across two threads and delivered no input,
    so a non-Steam pad (Xbox/DualSense/Switch Pro) couldn't drive the OSK once
    open. This source keeps ALL SDL access on the tray's thread: it just returns
    the latest frame the tray published via state.set_sdl_frame().

    Haptics forward to the tray's live Sdl3GamepadSource (registered via
    state.set_sdl_source). The tray's sdl_gamepad_thread owns that source's SDL
    access; SDL_RumbleGamepad is safe to call cross-thread, so the Switch Pro /
    Xbox / DualSense buzzes on each OSK key press (matching the SC)."""

    # The tray publishes only generic-SDL-pad frames here (Switch Pro, etc.) —
    # drives the OSK to that family's ZL/ZR glyphs. See InputMerger.poll().
    controller_kind = "sdl"

    def poll(self):
        return state.get_sdl_frame()

    def set_lizard(self, enabled):
        pass

    @staticmethod
    def _tray_src():
        try:
            return state.get_sdl_source()
        except Exception:
            return None

    def haptic_click(self):
        src = self._tray_src()
        if src is not None:
            try:
                src.haptic_click()
            except Exception:
                pass

    def haptic_pad_click(self):
        # No-op for SDL pads. The "strong" pad-click haptic only fires for them
        # on the L2/R2 trigger engage (ZL/ZR = Shift/Enter on the Switch Pro) —
        # SDL pads have no trackpad, so the physical-pad-click path that also
        # uses this never applies, and we don't want ZL/ZR to buzz. (Normal key
        # taps still use haptic_click; the SC's pad-click feedback is unaffected
        # — it goes through SteamHidSource, not this source.)
        return

    def set_rumble(self, large, small):
        src = self._tray_src()
        if src is not None:
            try:
                src.set_rumble(large, small)
            except Exception:
                pass

    def addExit(self):
        pass

    def close(self):
        pass


class InputMerger:
    """Holds the active input sources, OR-merges their frames, and presents the
    `sc`-facade (set_lizard / haptic_click / haptic_pad_click / addExit) that
    ControllerManager.handle_input expects, fanning each call out to every
    source."""

    # A source stays the haptic target this long after it last showed activity
    # (covers the gap between an input and the key dispatch that fires the tick).
    _ACTIVE_WINDOW = 1.0

    def __init__(self):
        self._sources = []
        # The source whose controller is actively driving the OSK, and when it
        # last showed activity. Haptics route ONLY here so a key press buzzes
        # just the controller being used — not every connected controller.
        self._active_src = None
        self._active_t = 0.0
        # Per-source state for the OSK glyph swap's edge detection — see poll()
        # and _intentional_edge(). Keyed by source identity; each value is a dict
        # holding the previous intentional-button mask, stick-deflected flag, and
        # the per-trackpad slide anchors.
        self._glyph_edge_prev = {}

    def add(self, src):
        self._sources.append(src)

    def poll(self):
        merged = None
        now = time.monotonic()
        # Controller family that made a fresh INTENTIONAL input this frame, for
        # the OSK glyph swap. Edge-detected (not activity level) on purpose: with
        # both controllers connected, a hand resting on the Steam Controller
        # keeps a trackpad-touch bit set EVERY frame, which — as a level signal —
        # fought the Switch's stick taps and made the glyphs flicker / lag. An
        # edge (a newly-pressed button/click or a stick entering deflection)
        # only fires on a deliberate action, so a resting hand is ignored and a
        # real input switches the glyphs immediately. Last source in poll order
        # wins if two act on the same frame (SDL added after the SC).
        frame_kind = None
        for src in self._sources:
            try:
                f = src.poll()
            except Exception as e:
                print(f"InputMerger: source poll error: {e!r}")
                f = None
            if f is None:
                continue
            if _frame_has_activity(f):
                self._active_src = src
                self._active_t = now
            if self._intentional_edge(src, f):
                frame_kind = getattr(src, "controller_kind", frame_kind)
            merged = f if merged is None else merge_inputs(merged, f)
        # Tell the renderer which controller family is in use so the Shift/Enter
        # (and X/Y) glyphs match it. Persisted on a real change in set_active_controller.
        if frame_kind is not None:
            state.set_active_controller(frame_kind)
        return merged

    # Trackpad-touch bits are EXCLUDED from the button edge: a hand resting on a
    # Steam Controller pad holds these set without any deliberate action, so
    # counting them would let an idle hand steal the glyphs. A pad *click*
    # (LPAD/RPAD) is a separate bit and still counts. Deliberate pad SLIDING is
    # picked up separately below (a resting finger doesn't move).
    _GLYPH_EDGE_BTN_MASK = ~(SCButtons.LPADTOUCH | SCButtons.RPADTOUCH)
    # Pad coords are int16 (±32767 full-scale). A finger that has moved this far
    # from its anchor since the last check counts as a deliberate slide; the
    # anchor then resets to the new spot, so the test measures RECENT movement
    # (a finger that slides then rests stops firing) and a resting finger's small
    # capacitive jitter never reaches it.
    _PAD_MOVE_THRESHOLD = 2500
    # Stick deflection needed to count as a glyph-switch edge, matched to when
    # each stick actually DOES something in the OSK so a tiny nudge that moves
    # nothing can't flip the glyphs. The LEFT stick steps the key cursor at
    # ControllerManager.KBD_STICK_DEADZONE (18480 = base deadzone +32%); the
    # RIGHT stick drives the mouse at _MOUSE_DEADZONE (6000). Keep in sync with
    # controller.py.
    _GLYPH_LSTICK_THRESHOLD = 18480
    _GLYPH_RSTICK_THRESHOLD = 6000

    def _intentional_edge(self, src, f):
        """True when this source's frame shows a NEW deliberate input vs its
        previous frame — a button/trigger/click newly pressed, a stick newly
        pushed past the deadzone, or a trackpad finger sliding (Steam Controller).
        Used to decide which controller owns the OSK glyphs (see poll). Steady
        holds — including a hand merely RESTING on a trackpad — produce no edge,
        so they never flip the glyphs; actively using the SC pad switches back to
        the SC glyphs."""
        intent_btns = f.buttons & self._GLYPH_EDGE_BTN_MASK
        stick_defl = (abs(f.lstick_x) > self._GLYPH_LSTICK_THRESHOLD
                      or abs(f.lstick_y) > self._GLYPH_LSTICK_THRESHOLD
                      or abs(f.rstick_x) > self._GLYPH_RSTICK_THRESHOLD
                      or abs(f.rstick_y) > self._GLYPH_RSTICK_THRESHOLD)

        st = self._glyph_edge_prev.get(src)
        if st is None:
            # First frame for this source: there's no prior state to diff, so
            # record a SILENT baseline and fire no edge. The merger is rebuilt on
            # every OSK open, so without this whatever a connected controller
            # happens to report on that first frame (an idle Steam Controller's
            # stick drift / a wake frame, a still-held opening chord) would look
            # like a fresh press and flip the glyphs — making them jump to the
            # Steam Controller every time the OSK is reopened with the Switch.
            # The persisted last-used controller shows until a genuine NEW input.
            self._glyph_edge_prev[src] = {
                "btns": intent_btns, "defl": stick_defl,
                "la": (f.lpad_x, f.lpad_y) if (f.buttons & SCButtons.LPADTOUCH) else None,
                "ra": (f.rpad_x, f.rpad_y) if (f.buttons & SCButtons.RPADTOUCH) else None,
            }
            return False

        edge = bool(intent_btns & ~st["btns"]) or (stick_defl and not st["defl"])
        st["btns"] = intent_btns
        st["defl"] = stick_defl

        # Trackpad slide → deliberate Steam Controller use. Anchor on touch-down
        # (no edge), then fire (and re-anchor) once the finger has moved past the
        # jitter threshold. SDL pads report no pad touch/coords, so this is a
        # no-op for them.
        t = self._PAD_MOVE_THRESHOLD
        for touch_bit, key, px, py in (
                (SCButtons.LPADTOUCH, "la", f.lpad_x, f.lpad_y),
                (SCButtons.RPADTOUCH, "ra", f.rpad_x, f.rpad_y)):
            if f.buttons & touch_bit:
                anchor = st[key]
                if anchor is None:
                    st[key] = (px, py)
                elif abs(px - anchor[0]) > t or abs(py - anchor[1]) > t:
                    edge = True
                    st[key] = (px, py)
            else:
                st[key] = None
        return edge

    def _active_source(self):
        """The source actively in use, or None if nothing has been touched
        recently (callers then fall back to fanning out to all sources)."""
        if (self._active_src is not None
                and (time.monotonic() - self._active_t) <= self._ACTIVE_WINDOW):
            return self._active_src
        return None

    def set_lizard(self, enabled):
        for src in self._sources:
            try:
                src.set_lizard(enabled)
            except Exception:
                pass

    def _fan_haptic(self, method):
        # Route the tick to the controller actually being used; if none is
        # clearly active (rare — e.g. the one-shot open tick), fall back to all.
        active = self._active_source()
        targets = [active] if active is not None else self._sources
        for src in targets:
            try:
                getattr(src, method)()
            except Exception:
                pass

    def haptic_click(self):
        self._fan_haptic("haptic_click")

    def haptic_pad_click(self):
        self._fan_haptic("haptic_pad_click")

    def addExit(self):
        for src in self._sources:
            try:
                src.addExit()
            except Exception:
                pass

    def close(self):
        for src in self._sources:
            try:
                src.close()
            except Exception:
                pass
