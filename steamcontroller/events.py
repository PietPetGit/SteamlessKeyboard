"""Minimal EventMapper replacement matching only the surface adusk uses:
    setButtonAction(button, key)        -> press/release the mapped key
    setButtonCallback(button, callback) -> call callback(self, btn, pressed)
    process(sc, sci)                    -> diff button bitmask vs previous
"""

from steamcontroller import SCStatus, SCButtons, SCI_NULL
from steamcontroller.uinput import Keyboard


class EventMapper:
    def __init__(self):
        self._kb = Keyboard()
        # button -> ('key', keycode) or ('cb', callable)
        self._btn_map = {}
        self._prev = SCI_NULL

    def setButtonAction(self, button, key):
        self._btn_map[int(button)] = ('key', key)

    def setButtonCallback(self, button, callback):
        self._btn_map[int(button)] = ('cb', callback)

    def process(self, sc, sci):
        if sci.status != SCStatus.INPUT:
            return

        prev_buttons = self._prev.buttons
        cur_buttons = sci.buttons
        xor = prev_buttons ^ cur_buttons
        newly_pressed = xor & cur_buttons
        newly_released = xor & prev_buttons

        for mask, (kind, payload) in self._btn_map.items():
            if mask & newly_pressed:
                if kind == 'key':
                    self._kb.pressEvent([payload])
                else:
                    payload(self, mask, True)
            elif mask & newly_released:
                if kind == 'key':
                    self._kb.releaseEvent([payload])
                else:
                    payload(self, mask, False)

        self._prev = sci
