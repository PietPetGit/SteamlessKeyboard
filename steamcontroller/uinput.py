"""Windows replacement for the Linux uinput key sender.
Maps the KEY_* names used by adusk to pynput keys and sends them via the
OS-level injection layer so the keystrokes land in whichever window is
focused (which on Windows is whatever the user had focused before adusk
started, since the SDL2 window doesn't steal focus unless clicked)."""

from pynput.keyboard import Controller as _Controller, Key as _Key, KeyCode as _KeyCode


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
