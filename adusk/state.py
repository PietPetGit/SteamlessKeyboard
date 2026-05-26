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

# Optional haptic-feedback hook. The controller thread registers a callable
# (bound to the live SteamController) here; the main thread calls haptic_tick()
# on each key press for a trackpad "tick". None when no controller is open.
_haptic_tick = None
# Global on/off for ALL haptics (UI ticks AND gamepad rumble), driven by the
# tray-menu switch. Process-wide so every haptic path reads one source.
_rumble_enabled = True
_haptic_lock = Lock()

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
    global should_exit, _visible, _shift_held, _highlighted
    global _lpad_touched, _rpad_touched, _cursor, _grid_dims
    global _position_cycle_requested
    with should_exit_lock:
        should_exit = False
    with _visible_lock:
        _visible = True
    with _shift_lock:
        _shift_held = False
    with _highlight_lock:
        _highlighted = set()
    with _touch_lock:
        _lpad_touched = False
        _rpad_touched = False
    with _cursor_lock:
        _cursor = (2, 5)
    with _grid_lock:
        _grid_dims = []
    with _key_press_lock:
        _key_press_queue.clear()
    with _dpad_lock:
        _dpad_queue.clear()
    with _position_cycle_lock:
        _position_cycle_requested = False


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


def set_grid_dims(cols_per_row):
    global _grid_dims
    with _grid_lock:
        _grid_dims = list(cols_per_row)


def queue_key_press(row, col):
    with _key_press_lock:
        _key_press_queue.append((int(row), int(col)))


def drain_key_press_queue():
    with _key_press_lock:
        out = list(_key_press_queue)
        _key_press_queue.clear()
    return out


def queue_dpad(direction):
    with _dpad_lock:
        _dpad_queue.append(direction)


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


def set_haptic_tick(fn):
    global _haptic_tick
    with _haptic_lock:
        _haptic_tick = fn


def set_rumble_enabled(value):
    global _rumble_enabled
    with _haptic_lock:
        _rumble_enabled = bool(value)


def is_rumble_enabled():
    with _haptic_lock:
        return _rumble_enabled


def haptic_tick():
    """Fire the registered haptic-feedback hook, if any. Safe to call from the
    main thread; swallows errors so feedback never breaks key dispatch. No-op
    when haptics are globally disabled."""
    with _haptic_lock:
        if not _rumble_enabled:
            return
        fn = _haptic_tick
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


class InputState(IntEnum):
    INACTIVE = 0
    HOVER = 1
    CLICK = 2
