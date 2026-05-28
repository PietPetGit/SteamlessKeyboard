"""Minimal EventMapper replacement matching only the surface adusk uses:
    setButtonAction(button, key)        -> press/release the mapped key
    process(sc, sci)                    -> diff button bitmask vs previous
"""

from steamcontroller import SCStatus, SCI_NULL
from steamcontroller.uinput import Keyboard


class EventMapper:
    def __init__(self):
        self._kb = Keyboard()
        # button -> keycode
        self._btn_map = {}
        self._prev = SCI_NULL

    def setButtonAction(self, button, key):
        self._btn_map[int(button)] = key

    def process(self, sc, sci):
        if sci.status != SCStatus.INPUT:
            return

        prev_buttons = self._prev.buttons
        cur_buttons = sci.buttons
        xor = prev_buttons ^ cur_buttons
        newly_pressed = xor & cur_buttons
        newly_released = xor & prev_buttons

        for mask, key in self._btn_map.items():
            if mask & newly_pressed:
                self._kb.pressEvent([key])
            elif mask & newly_released:
                self._kb.releaseEvent([key])

        self._prev = sci
