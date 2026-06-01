"""Windows replacement for the Linux uinput key sender.
Maps the KEY_* names used by adusk to pynput keys and sends them via the
OS-level injection layer so the keystrokes land in whichever window is
focused (which on Windows is whatever the user had focused before adusk
started, since the SDL2 window doesn't steal focus unless clicked)."""

from pynput.keyboard import Controller as _Controller, Key as _Key, KeyCode as _KeyCode
from pynput.mouse import Controller as _MouseController


class _KeysProxy:
    """`Keys[name]` and `Keys.NAME` both return the name string so the rest of
    the code can pass keycodes around as strings and we resolve them inside
    Keyboard.pressEvent / releaseEvent."""

    def __getitem__(self, name):
        return name

    def __getattr__(self, name):
        return name


Keys = _KeysProxy()


def _build_keymap():
    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m['KEY_' + c.upper()] = _KeyCode.from_char(c)
    for d in "0123456789":
        m['KEY_' + d] = _KeyCode.from_char(d)
    m.update({
        'KEY_SPACE':      _Key.space,
        'KEY_ENTER':      _Key.enter,
        'KEY_BACKSPACE':  _Key.backspace,
        'KEY_TAB':        _Key.tab,
        'KEY_ESC':        _Key.esc,
        'KEY_CAPSLOCK':   _Key.caps_lock,
        'KEY_LEFTSHIFT':  _Key.shift,
        'KEY_RIGHTSHIFT': _Key.shift_r,
        'KEY_LEFTCTRL':   _Key.ctrl,
        'KEY_RIGHTCTRL':  _Key.ctrl_r,
        'KEY_LEFTALT':    _Key.alt,
        'KEY_RIGHTALT':   _Key.alt_r,
        'KEY_LEFTMETA':   _Key.cmd,    # Windows / Super key
        'KEY_LEFTWIN':    _Key.cmd,
        'KEY_MINUS':      _KeyCode.from_char('-'),
        'KEY_EQUAL':      _KeyCode.from_char('='),
        'KEY_DOT':        _KeyCode.from_char('.'),
        'KEY_COMMA':      _KeyCode.from_char(','),
        'KEY_SLASH':      _KeyCode.from_char('/'),
        'KEY_BACKSLASH':  _KeyCode.from_char('\\'),
        'KEY_SEMICOLON':  _KeyCode.from_char(';'),
        'KEY_APOSTROPHE': _KeyCode.from_char("'"),
        'KEY_GRAVE':      _KeyCode.from_char('`'),
        'KEY_LEFTBRACE':  _KeyCode.from_char('['),
        'KEY_RIGHTBRACE': _KeyCode.from_char(']'),
        'KEY_QUESTION':   _KeyCode.from_char('?'),
        'KEY_LEFT':       _Key.left,
        'KEY_RIGHT':      _Key.right,
        'KEY_UP':         _Key.up,
        'KEY_DOWN':       _Key.down,
        'KEY_PAGEUP':     _Key.page_up,
        'KEY_PAGEDOWN':   _Key.page_down,
        'KEY_HOME':       _Key.home,
        'KEY_END':        _Key.end,
        # Media transport keys (driven by the Steam + left-stick chords).
        'KEY_VOLUMEUP':    _Key.media_volume_up,
        'KEY_VOLUMEDOWN':  _Key.media_volume_down,
        'KEY_MUTE':        _Key.media_volume_mute,
        'KEY_PREVIOUSSONG': _Key.media_previous,
        'KEY_NEXTSONG':    _Key.media_next,
        'KEY_PLAYPAUSE':   _Key.media_play_pause,
    })
    return m


_KEYMAP = _build_keymap()


class Keyboard:
    def __init__(self):
        self._kb = _Controller()

    def _resolve(self, code):
        if isinstance(code, str):
            return _KEYMAP.get(code)
        return None

    def pressEvent(self, keys):
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            try:
                self._kb.press(k)
            except Exception as e:
                print(f"uinput: press {code!r} failed: {e}")

    def releaseEvent(self, keys):
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            try:
                self._kb.release(k)
            except Exception as e:
                print(f"uinput: release {code!r} failed: {e}")


class Mouse:
    """Thin wrapper over pynput's mouse for relative cursor movement, so the
    stick-as-mouse code can stay symmetric with the Keyboard wrapper."""

    def __init__(self):
        self._m = _MouseController()

    def move(self, dx, dy):
        if not dx and not dy:
            return
        try:
            self._m.move(int(dx), int(dy))
        except Exception as e:
            print(f"uinput: mouse move ({dx},{dy}) failed: {e}")
