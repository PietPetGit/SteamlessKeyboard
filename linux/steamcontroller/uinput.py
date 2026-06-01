"""Key/mouse injection backend used by adusk.

Two implementations behind the same Keyboard/Mouse API:

1. Linux: open /dev/uinput via python-evdev and inject events at the
   kernel level. This is the only path that works on a Wayland session
   — pynput's XTest path silently drops keys destined for native
   Wayland apps. Requires /dev/uinput to be writable by the user
   (CachyOS / most modern distros grant this via a uaccess ACL).

2. Fallback (Windows, or Linux without evdev / uinput access): pynput,
   which on Windows uses SendInput and on Linux uses XTest.
"""

import sys
import time


# Adusk passes keycodes around as strings (e.g. "KEY_A"); this proxy lets
# code write `sui.Keys.KEY_A` or `sui.Keys["KEY_A"]` interchangeably.
class _KeysProxy:
    def __getitem__(self, name):
        return name

    def __getattr__(self, name):
        return name


Keys = _KeysProxy()


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_BACKEND = None  # "uinput" or "pynput"
_uinput_kb = None
_uinput_mouse = None
_uinput_keymap = None
_uinput_init_error = None

try:
    if sys.platform.startswith("linux"):
        import evdev
        from evdev import UInput, ecodes as _e
        _has_evdev = True
    else:
        _has_evdev = False
except Exception as _exc:
    _has_evdev = False
    _uinput_init_error = _exc


def _build_uinput_keymap():
    """Map adusk's KEY_* names to Linux input event codes. Most names
    already match the kernel input.h constants verbatim (they were
    copied from there), so evdev.ecodes resolves them directly. A few
    adusk-specific aliases are listed explicitly."""
    m = {}
    # Pass-through for any KEY_* / BTN_* name evdev recognizes.
    for name in dir(_e):
        if name.startswith("KEY_") or name.startswith("BTN_"):
            m[name] = getattr(_e, name)
    # Adusk aliases that aren't 1:1 kernel names.
    aliases = {
        "KEY_LEFTWIN": "KEY_LEFTMETA",
        "KEY_QUESTION": "KEY_SLASH",  # '?' is shift+'/'; same physical key
    }
    for alias, real in aliases.items():
        if real in m:
            m[alias] = m[real]
    return m


def _init_uinput():
    """Open /dev/uinput once at first use. Declares a virtual keyboard
    and virtual mouse. Returns True on success."""
    global _uinput_kb, _uinput_mouse, _uinput_keymap, _BACKEND, _uinput_init_error
    if _BACKEND == "uinput":
        return True
    if not _has_evdev:
        return False
    try:
        keymap = _build_uinput_keymap()

        # Declare every keyboard key code we might ever emit. Filter to
        # the valid 1..KEY_MAX range so we don't try to register sentinel
        # symbols like KEY_CNT (=KEY_MAX+1), which trip the uinput ioctl
        # with EINVAL.
        kb_codes = sorted({
            v for k, v in keymap.items()
            if k.startswith("KEY_") and 0 < v <= _e.KEY_MAX
        })
        kb_caps = {_e.EV_KEY: kb_codes}
        _uinput_kb = UInput(
            kb_caps, name="SteamlessKeyboard-virtual-kb", version=1)

        # Mouse: relative X/Y plus the three standard buttons (so the
        # kernel actually treats this device as a pointer).
        mouse_caps = {
            _e.EV_KEY: [_e.BTN_LEFT, _e.BTN_RIGHT, _e.BTN_MIDDLE],
            _e.EV_REL: [_e.REL_X, _e.REL_Y, _e.REL_WHEEL],
        }
        _uinput_mouse = UInput(
            mouse_caps, name="SteamlessKeyboard-virtual-mouse", version=1)

        _uinput_keymap = keymap
        _BACKEND = "uinput"
        # Compositors take a moment to notice a freshly created uinput
        # device. Without a brief settle, the very first key event after
        # opening the OSK can be dropped.
        time.sleep(0.1)
        return True
    except Exception as exc:
        _uinput_init_error = exc
        _uinput_kb = None
        _uinput_mouse = None
        return False


# ---------------------------------------------------------------------------
# pynput fallback
# ---------------------------------------------------------------------------

_pynput_kb = None
_pynput_mouse = None
_pynput_keymap = None


def _init_pynput():
    global _pynput_kb, _pynput_mouse, _pynput_keymap, _BACKEND
    if _BACKEND == "pynput":
        return True
    try:
        from pynput.keyboard import (
            Controller as _Controller,
            Key as _Key,
            KeyCode as _KeyCode,
        )
        from pynput.mouse import Controller as _MouseController
    except Exception as exc:
        print(f"uinput: pynput unavailable: {exc}")
        return False

    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m["KEY_" + c.upper()] = _KeyCode.from_char(c)
    for d in "0123456789":
        m["KEY_" + d] = _KeyCode.from_char(d)
    m.update({
        "KEY_SPACE":      _Key.space,
        "KEY_ENTER":      _Key.enter,
        "KEY_BACKSPACE":  _Key.backspace,
        "KEY_TAB":        _Key.tab,
        "KEY_ESC":        _Key.esc,
        "KEY_CAPSLOCK":   _Key.caps_lock,
        "KEY_LEFTSHIFT":  _Key.shift,
        "KEY_RIGHTSHIFT": _Key.shift_r,
        "KEY_LEFTCTRL":   _Key.ctrl,
        "KEY_RIGHTCTRL":  _Key.ctrl_r,
        "KEY_LEFTALT":    _Key.alt,
        "KEY_RIGHTALT":   _Key.alt_r,
        "KEY_LEFTMETA":   _Key.cmd,
        "KEY_LEFTWIN":    _Key.cmd,
        "KEY_MINUS":      _KeyCode.from_char("-"),
        "KEY_EQUAL":      _KeyCode.from_char("="),
        "KEY_DOT":        _KeyCode.from_char("."),
        "KEY_COMMA":      _KeyCode.from_char(","),
        "KEY_SLASH":      _KeyCode.from_char("/"),
        "KEY_BACKSLASH":  _KeyCode.from_char("\\"),
        "KEY_SEMICOLON":  _KeyCode.from_char(";"),
        "KEY_APOSTROPHE": _KeyCode.from_char("'"),
        "KEY_GRAVE":      _KeyCode.from_char("`"),
        "KEY_LEFTBRACE":  _KeyCode.from_char("["),
        "KEY_RIGHTBRACE": _KeyCode.from_char("]"),
        "KEY_QUESTION":   _KeyCode.from_char("?"),
        "KEY_LEFT":       _Key.left,
        "KEY_RIGHT":      _Key.right,
        "KEY_UP":         _Key.up,
        "KEY_DOWN":       _Key.down,
        "KEY_PAGEUP":     _Key.page_up,
        "KEY_PAGEDOWN":   _Key.page_down,
        "KEY_HOME":       _Key.home,
        "KEY_END":        _Key.end,
        "KEY_VOLUMEUP":    _Key.media_volume_up,
        "KEY_VOLUMEDOWN":  _Key.media_volume_down,
        "KEY_MUTE":        _Key.media_volume_mute,
        "KEY_PREVIOUSSONG": _Key.media_previous,
        "KEY_NEXTSONG":    _Key.media_next,
        "KEY_PLAYPAUSE":   _Key.media_play_pause,
    })

    _pynput_kb = _Controller()
    _pynput_mouse = _MouseController()
    _pynput_keymap = m
    _BACKEND = "pynput"
    return True


def _ensure_backend():
    """Pick uinput on Linux when possible, pynput otherwise. The first
    Keyboard()/Mouse() constructed triggers initialization; the chosen
    backend is then reused for the lifetime of the process."""
    if _BACKEND is not None:
        return
    if sys.platform.startswith("linux") and _init_uinput():
        print("uinput: using /dev/uinput backend (works under Wayland)")
        return
    if _init_pynput():
        why = ""
        if _uinput_init_error is not None:
            why = f" (uinput init failed: {_uinput_init_error!r})"
        print(f"uinput: using pynput backend{why}")
        return
    print("uinput: NO backend available — key/mouse injection disabled")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Keyboard:
    def __init__(self):
        _ensure_backend()

    def _resolve(self, code):
        if not isinstance(code, str):
            return None
        if _BACKEND == "uinput":
            return _uinput_keymap.get(code)
        if _BACKEND == "pynput":
            return _pynput_keymap.get(code)
        return None

    def pressEvent(self, keys):
        if _BACKEND is None:
            return
        if _BACKEND == "uinput":
            for code in keys:
                k = self._resolve(code)
                if k is None:
                    continue
                try:
                    _uinput_kb.write(_e.EV_KEY, k, 1)
                except Exception as exc:
                    print(f"uinput: press {code!r} failed: {exc}")
                # Track caps state ourselves on Linux: KWin under Wayland
                # doesn't propagate the latched-caps state through XWayland's
                # XKB, so adusk.state.is_caps_on() needs a manual signal to
                # know when to flip the OSK glyphs.
                if code == "KEY_CAPSLOCK":
                    try:
                        from adusk import state as _adusk_state
                        _adusk_state.notify_caps_key_sent()
                    except Exception:
                        pass
            try:
                _uinput_kb.syn()
            except Exception as exc:
                print(f"uinput: syn failed: {exc}")
            return
        # pynput
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            try:
                _pynput_kb.press(k)
            except Exception as exc:
                print(f"uinput: press {code!r} failed: {exc}")

    def releaseEvent(self, keys):
        if _BACKEND is None:
            return
        if _BACKEND == "uinput":
            for code in keys:
                k = self._resolve(code)
                if k is None:
                    continue
                try:
                    _uinput_kb.write(_e.EV_KEY, k, 0)
                except Exception as exc:
                    print(f"uinput: release {code!r} failed: {exc}")
            try:
                _uinput_kb.syn()
            except Exception as exc:
                print(f"uinput: syn failed: {exc}")
            return
        # pynput
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            try:
                _pynput_kb.release(k)
            except Exception as exc:
                print(f"uinput: release {code!r} failed: {exc}")


class Mouse:
    """Relative cursor movement; symmetric with Keyboard."""

    def __init__(self):
        _ensure_backend()

    def move(self, dx, dy):
        if not dx and not dy:
            return
        if _BACKEND == "uinput":
            try:
                _uinput_mouse.write(_e.EV_REL, _e.REL_X, int(dx))
                _uinput_mouse.write(_e.EV_REL, _e.REL_Y, int(dy))
                _uinput_mouse.syn()
            except Exception as exc:
                print(f"uinput: mouse move ({dx},{dy}) failed: {exc}")
            return
        if _BACKEND == "pynput":
            try:
                _pynput_mouse.move(int(dx), int(dy))
            except Exception as exc:
                print(f"uinput: mouse move ({dx},{dy}) failed: {exc}")

    def button(self, button, pressed):
        """Press (pressed=True) or release (False) a mouse button.
        `button` is 'left', 'right', or 'middle'."""
        if _BACKEND == "uinput":
            code = {
                "left": _e.BTN_LEFT,
                "right": _e.BTN_RIGHT,
                "middle": _e.BTN_MIDDLE,
            }.get(button)
            if code is None:
                return
            try:
                _uinput_mouse.write(_e.EV_KEY, code, 1 if pressed else 0)
                _uinput_mouse.syn()
            except Exception as exc:
                print(f"uinput: mouse button {button} failed: {exc}")
            return
        if _BACKEND == "pynput":
            try:
                from pynput.mouse import Button as _B
                btn = {"left": _B.left, "right": _B.right,
                       "middle": _B.middle}.get(button)
                if btn is None:
                    return
                if pressed:
                    _pynput_mouse.press(btn)
                else:
                    _pynput_mouse.release(btn)
            except Exception as exc:
                print(f"uinput: mouse button {button} failed: {exc}")
