import ctypes
from collections import deque
from enum import IntEnum
from threading import Lock

should_exit = False
should_exit_lock = Lock()

_visible = True
_visible_lock = Lock()

_shift_held = False
_shift_lock = Lock()

# Latched Shift from the mouse/click path (clicking the on-screen Shift key or
# right-click). Kept separate from _shift_held because a connected controller
# rewrites _shift_held every input frame — the toggle must read its own latch
# to decide on/off, or it would never turn back off.
_shift_latched = False
_shift_latch_lock = Lock()

# Set of keycode strings (e.g. "KEY_BACKSPACE") that should render in the
# CLICK (blue) state, e.g. while their corresponding controller button is held.
_highlighted = set()
_highlight_lock = Lock()

# Touchpad capacitive-touch state. Renderer uses it to hide the L2/R2 hint
# glyphs on Shift/Enter while LT/RT's alternate "click under pad" role is
# active.
_lpad_touched = False
_rpad_touched = False
_touch_lock = Lock()

# DPAD cursor over the virtual keyboard's grid. The cursor key is painted
# as HOVER; the A button presses it.
_cursor = (2, 5)
_cursor_lock = Lock()

# (row, col) of the key currently held down by the left mouse button, or None.
# Painted in the CLICK (blue) state so a mouse press flashes like a real key
# press. Kept separate from _highlighted (which a controller frame overwrites).
_mouse_press_cell = None
_mouse_press_lock = Lock()

# Number of keys in each row of the active keyboard layout. Published by
# the main thread after the layout is built so the controller thread can
# clamp DPAD navigation.
_grid_dims = []
_grid_lock = Lock()

# Queue of (row, col) cells whose KeyButton callback should fire on the
# main thread (the A button enqueues here on each rising edge).
_key_press_queue = deque()
_key_press_lock = Lock()

# DPAD direction events posted by the controller thread; consumed by the
# main loop, which has access to the keyboard layout for pixel-aware nav.
_dpad_queue = deque()
_dpad_lock = Lock()

# Set by the Move-key (shift held) callback to ask the main thread to
# advance the keyboard window through its 6-position rotation.
_position_cycle_requested = False
_position_cycle_lock = Lock()

# Lock-screen launcher: ask the main thread to JUMP to a SPECIFIC position
# index (0-5, see adusk._apply_window_position) instead of cycling — used to
# move the OSK out of the way of the LogonUI password box once its on-screen
# location is known. None = no pending request.
_window_position_request = None
_window_position_lock = Lock()

# Tracks whether the OS emoji picker (opened by the on-screen emoji key) is
# currently open, so pressing the emoji key again closes it (sends Escape)
# instead of re-opening. Reset per OSK open so a fresh session starts closed.
_emoji_open = False
_emoji_lock = Lock()

# HWND (int) of the window the user was typing in just before the OSK opened.
# adusk.main restores focus to it after showing the OSK: a controller-open
# fires the firmware lizard's mouse-click, which can land off the target field
# and steal focus. The OSK window is NOACTIVATE so it never takes focus, so
# re-activating this window puts the caret back. None = nothing to restore.
_focus_restore_target = None
_focus_restore_lock = Lock()

# Latest SDL-pad frame (SteamControllerInput) published by the tray's
# sdl_gamepad_thread while the OSK is open. adusk reads it via inputsrc
# .SharedSdlFrameSource instead of opening the pad a SECOND time — two
# Sdl3GamepadSource instances on two threads double-drove the same pad and
# delivered no input. None = no SDL pad frame this tick.
_sdl_frame = None
_sdl_frame_lock = Lock()

# Reference to the tray's Sdl3GamepadSource. While the OSK is open, adusk.main
# polls it on ITS OWN thread — the one pumping SDL events — because SDL only
# refreshes gamepad state on the event-pump thread, so the tray thread goes
# blind (reads all-zero buttons) once the OSK window's event loop is running.
_sdl_source = None

# Optional haptic-feedback hook. The controller thread registers a callable
# (bound to the live SteamController) here; the main thread calls haptic_tick()
# on each key press for a trackpad "tick". None when no controller is open.
_haptic_tick = None
# Separate, stronger hook for the simulated physical pad-click (press/release)
# so only that feedback is deeper/more intense than the light UI tick.
_pad_click_haptic = None
# Per-controller on/off for haptics (UI ticks AND gamepad rumble), keyed by the
# controller family the feedback belongs to: "sc" (Steam Controller) or "sdl"
# (an SDL pad — e.g. the Nintendo Switch Pro). Each controller's tray submenu has
# its own Vibration toggle (there is no global switch). OSK key-press ticks fan
# to the ACTIVE controller, so they read the active controller's entry.
_rumble_enabled = {"sc": True, "sdl": True}
_haptic_lock = Lock()

# Which controller most recently drove the on-screen keyboard: "sc" (Steam
# Controller) or "sdl" (a generic SDL pad — e.g. the Nintendo Switch Pro). The
# renderer reads this to pick the Shift/Enter trigger glyphs (Steam Controller
# L2/R2 vs Switch Pro ZL/ZR). Seeded at startup from the saved setting so the
# right glyphs show before any input, then updated live by InputMerger.poll() as
# each controller is used. A registered persist hook saves changes to disk so
# the last-used controller's glyphs stick across restarts. NOT cleared by
# reset_session — the choice must survive each OSK open/close.
_active_controller = "sc"
_active_controller_lock = Lock()
_active_controller_persist = None  # callable(kind) set by the tray to save it

# Win32: ask the OS whether Caps Lock is currently toggled on. Lets the
# on-screen keyboard mirror the system caps state automatically — we don't
# need to track L3 ourselves because L3 just sends KEY_CAPSLOCK to the OS.
_VK_CAPITAL = 0x14
try:
    _user32 = ctypes.windll.user32
    _user32.GetKeyState.restype = ctypes.c_short
except Exception:
    _user32 = None


def close():
    global should_exit
    with should_exit_lock:
        should_exit = True


def reset_session():
    """Wipe per-session state so adusk.main() can be invoked again from a
    long-lived launcher process (no subprocess startup cost)."""
    global should_exit, _visible, _shift_held, _shift_latched, _highlighted
    global _lpad_touched, _rpad_touched, _cursor, _grid_dims
    global _position_cycle_requested, _mouse_press_cell, _focus_restore_target
    global _sdl_frame, _emoji_open, _window_position_request
    with should_exit_lock:
        should_exit = False
    with _emoji_lock:
        _emoji_open = False
    with _focus_restore_lock:
        _focus_restore_target = None
    with _sdl_frame_lock:
        _sdl_frame = None
    with _visible_lock:
        _visible = True
    with _shift_lock:
        _shift_held = False
    with _shift_latch_lock:
        _shift_latched = False
    with _highlight_lock:
        _highlighted = set()
    with _touch_lock:
        _lpad_touched = False
        _rpad_touched = False
    with _cursor_lock:
        _cursor = (2, 5)
    with _mouse_press_lock:
        _mouse_press_cell = None
    with _grid_lock:
        _grid_dims = []
    with _key_press_lock:
        _key_press_queue.clear()
    with _dpad_lock:
        _dpad_queue.clear()
    with _position_cycle_lock:
        _position_cycle_requested = False
    with _window_position_lock:
        _window_position_request = None


def set_focus_restore_target(hwnd):
    """Record the window (HWND int) to re-focus after the OSK opens, or None."""
    global _focus_restore_target
    with _focus_restore_lock:
        _focus_restore_target = hwnd


def get_focus_restore_target():
    with _focus_restore_lock:
        return _focus_restore_target


def set_sdl_frame(frame):
    """Publish the latest SDL-pad frame (SteamControllerInput) for the OSK."""
    global _sdl_frame
    with _sdl_frame_lock:
        _sdl_frame = frame


def get_sdl_frame():
    with _sdl_frame_lock:
        return _sdl_frame


def set_sdl_source(src):
    """Register the tray's Sdl3GamepadSource so adusk.main can poll it on its
    own SDL event-pump thread while the OSK is open."""
    global _sdl_source
    _sdl_source = src


def get_sdl_source():
    return _sdl_source


def should_close():
    global should_exit
    with should_exit_lock:
        ret = should_exit
    return ret


def is_visible():
    with _visible_lock:
        return _visible


def show():
    global _visible
    with _visible_lock:
        _visible = True


def is_shift_held():
    with _shift_lock:
        return _shift_held


def set_shift_held(value):
    global _shift_held
    with _shift_lock:
        _shift_held = bool(value)


def is_shift_latched():
    with _shift_latch_lock:
        return _shift_latched


def set_shift_latched(value):
    global _shift_latched
    with _shift_latch_lock:
        _shift_latched = bool(value)


def set_active_controller(kind):
    """Record which controller is driving the OSK: "sc" (Steam Controller) or
    "sdl" (generic SDL pad, e.g. Switch Pro). Callers pass this only on a fresh
    INTENTIONAL input edge (a button/click/stick deflection — see
    InputMerger.poll), so a hand merely resting on a controller can't flip the
    glyphs. Persists via the registered hook only when the value actually
    changes, so disk writes happen just on a real switch."""
    global _active_controller
    if kind not in ("sc", "sdl"):
        return
    with _active_controller_lock:
        if kind == _active_controller:
            return
        _active_controller = kind
        cb = _active_controller_persist
    if cb is not None:
        try:
            cb(kind)
        except Exception:
            pass


def get_active_controller():
    with _active_controller_lock:
        return _active_controller


def init_active_controller(kind):
    """Seed the active controller at startup from the saved setting, WITHOUT
    firing the persist hook (the value already matches what's on disk)."""
    global _active_controller
    if kind not in ("sc", "sdl"):
        return
    with _active_controller_lock:
        _active_controller = kind


def set_active_controller_persist(fn):
    """Register a callback invoked (with the new "sc"/"sdl" kind) whenever the
    active controller changes, so the tray can save it to settings.json."""
    global _active_controller_persist
    with _active_controller_lock:
        _active_controller_persist = fn


def is_caps_on():
    """True if the OS has Caps Lock currently toggled on."""
    if _user32 is None:
        return False
    return bool(_user32.GetKeyState(_VK_CAPITAL) & 0x0001)


def set_highlighted(items):
    global _highlighted
    with _highlight_lock:
        _highlighted = set(items)


def get_highlighted():
    with _highlight_lock:
        return set(_highlighted)


def is_lpad_touched():
    with _touch_lock:
        return _lpad_touched


def is_rpad_touched():
    with _touch_lock:
        return _rpad_touched


def set_pad_touched(left, right):
    global _lpad_touched, _rpad_touched
    with _touch_lock:
        _lpad_touched = bool(left)
        _rpad_touched = bool(right)


def get_cursor():
    with _cursor_lock:
        return _cursor


def set_cursor(row, col):
    global _cursor
    with _cursor_lock:
        _cursor = (int(row), int(col))


def get_mouse_press_cell():
    with _mouse_press_lock:
        return _mouse_press_cell


def set_mouse_press_cell(cell):
    global _mouse_press_cell
    with _mouse_press_lock:
        _mouse_press_cell = tuple(cell) if cell is not None else None


def set_grid_dims(cols_per_row):
    global _grid_dims
    with _grid_lock:
        _grid_dims = list(cols_per_row)


def queue_key_press(row, col, repeat=False):
    # repeat=True marks an auto-repeat hit (A held); the main thread only acts
    # on it over Backspace, so holding rubs out text without machine-gunning
    # ordinary keys.
    with _key_press_lock:
        _key_press_queue.append((int(row), int(col), bool(repeat)))


def drain_key_press_queue():
    with _key_press_lock:
        out = list(_key_press_queue)
        _key_press_queue.clear()
    return out


def queue_dpad(direction, haptic=False):
    with _dpad_lock:
        _dpad_queue.append((direction, bool(haptic)))


def drain_dpad_queue():
    with _dpad_lock:
        out = list(_dpad_queue)
        _dpad_queue.clear()
    return out


def request_position_cycle():
    global _position_cycle_requested
    with _position_cycle_lock:
        _position_cycle_requested = True


def take_position_cycle_request():
    global _position_cycle_requested
    with _position_cycle_lock:
        v = _position_cycle_requested
        _position_cycle_requested = False
    return v


def request_window_position(index):
    """Ask the main loop to jump the OSK to position-rotation slot `index`
    (0-5). Used by the lock-screen launcher to dodge the password box."""
    global _window_position_request
    with _window_position_lock:
        _window_position_request = index


def take_window_position_request():
    global _window_position_request
    with _window_position_lock:
        v = _window_position_request
        _window_position_request = None
    return v


def is_emoji_open():
    with _emoji_lock:
        return _emoji_open


def set_emoji_open(value):
    global _emoji_open
    with _emoji_lock:
        _emoji_open = bool(value)


def set_haptic_tick(fn):
    global _haptic_tick
    with _haptic_lock:
        _haptic_tick = fn


def set_pad_click_haptic(fn):
    global _pad_click_haptic
    with _haptic_lock:
        _pad_click_haptic = fn


def set_rumble_enabled(kind, value):
    """Enable/disable haptics for controller family `kind` ("sc" / "sdl")."""
    with _haptic_lock:
        _rumble_enabled[kind] = bool(value)


def is_rumble_enabled(kind):
    """Whether haptics are on for controller family `kind` ("sc" / "sdl")."""
    with _haptic_lock:
        return _rumble_enabled.get(kind, True)


# Steam Controller-only OSK settings (tray "Steam Controller" submenu, shown only
# while an SC is connected). Set on the tray thread, read on the input thread.
# Apply ONLY to the Steam Controller — controller.py gates them on
# get_active_controller() == "sc".
_sc_lock = Lock()
# Left-stick OSK cursor navigation. Off = the SC's left stick doesn't move the
# OSK cursor, so its firmware-lizard behavior (e.g. scrolling a page) passes
# through while the OSK is open. Default on.
_sc_kbd_stick_nav = True
# OSK L2/R2 (Shift/Enter) actuation: None = firmware full-pull digital bit only
# (default); an int 0..32767 also engages Shift/Enter at that lighter analog pull.
_sc_osk_trigger_threshold = None
# Right-stick → mouse pointer speed multiplier (tray "Pointer Speed"). 1.0 =
# the tuned default; <1 slower, >1 faster. Scales the base px/sec in the OSK
# right-stick mouse (controller.py) and the SC desktop mouse (tray _Watcher).
_sc_mouse_speed = 1.0
# Same two settings for the Nintendo Switch Pro (and other SDL pads), driven by
# the tray "Switch Pro Controller" submenu. The OSK reads whichever pair
# matches the ACTIVE controller (see *_for helpers); the desktop handlers read
# their own controller's pair directly (_Watcher → sc, _SdlDesktopController →
# switch).
_switch_kbd_stick_nav = True
_switch_mouse_speed = 1.0


def set_sc_kbd_stick_nav(enabled):
    global _sc_kbd_stick_nav
    with _sc_lock:
        _sc_kbd_stick_nav = bool(enabled)


def is_sc_kbd_stick_nav_enabled():
    with _sc_lock:
        return _sc_kbd_stick_nav


def set_switch_kbd_stick_nav(enabled):
    global _switch_kbd_stick_nav
    with _sc_lock:
        _switch_kbd_stick_nav = bool(enabled)


def is_switch_kbd_stick_nav_enabled():
    with _sc_lock:
        return _switch_kbd_stick_nav


def is_kbd_stick_nav_enabled_for(kind):
    """Nav setting for the active controller family: "sdl" → Switch, else SC."""
    return (is_switch_kbd_stick_nav_enabled() if kind == "sdl"
            else is_sc_kbd_stick_nav_enabled())


def set_sc_mouse_speed(factor):
    global _sc_mouse_speed
    with _sc_lock:
        _sc_mouse_speed = float(factor)


def get_sc_mouse_speed():
    with _sc_lock:
        return _sc_mouse_speed


def set_switch_mouse_speed(factor):
    global _switch_mouse_speed
    with _sc_lock:
        _switch_mouse_speed = float(factor)


def get_switch_mouse_speed():
    with _sc_lock:
        return _switch_mouse_speed


def get_mouse_speed_for(kind):
    """Pointer-speed multiplier for the active controller family: "sdl" → Switch,
    else SC."""
    return get_switch_mouse_speed() if kind == "sdl" else get_sc_mouse_speed()


def set_sc_osk_trigger_threshold(threshold):
    global _sc_osk_trigger_threshold
    with _sc_lock:
        _sc_osk_trigger_threshold = threshold


def get_sc_osk_trigger_threshold():
    with _sc_lock:
        return _sc_osk_trigger_threshold


def haptic_tick():
    """Fire the registered haptic-feedback hook, if any. Safe to call from the
    main thread; swallows errors so feedback never breaks key dispatch. The tick
    fans out to the ACTIVE controller, so it's gated by that controller's
    Vibration toggle."""
    kind = get_active_controller()
    with _haptic_lock:
        if not _rumble_enabled.get(kind, True):
            return
        fn = _haptic_tick
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


def pad_click_haptic():
    """Fire the stronger physical-pad-click hook (press/release of the
    simulated trackpad click). Falls back to the normal tick if no dedicated
    hook is registered. Gated by the active controller's Vibration toggle like
    haptic_tick()."""
    kind = get_active_controller()
    with _haptic_lock:
        if not _rumble_enabled.get(kind, True):
            return
        fn = _pad_click_haptic or _haptic_tick
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


class InputState(IntEnum):
    INACTIVE = 0
    HOVER = 1
    CLICK = 2
