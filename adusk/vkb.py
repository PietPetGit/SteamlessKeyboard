from collections import namedtuple

import sdl2.ext
import steamcontroller.uinput as sui

from adusk import config
from adusk import screen
from adusk import state
from adusk import utils

kb = sui.Keyboard()


def _parse_hex_color(s):
    s = s.lstrip("#")
    return sdl2.ext.Color(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


class VirtualKeyboard:
    padding_inner = 3
    padding_outer = 10
    key_width = []
    key_height = 0

    KeyLayout = namedtuple("KeyLayout", "x y w h row col")

    def __init__(self, keys):
        self.keys = keys
        self.key_rows = len(keys)
        self.update_dimensions()

    def _uniform_key_width(self, row):
        unpadded_width = screen.width - self.padding_outer * 2 - (len(self.keys[row]) * self.padding_inner * 2)
        weights_total = 0
        for key in self.keys[row]:
            weights_total += key.width_weight

        return unpadded_width / weights_total

    def _uniform_key_height(self):
        return (screen.height - self.padding_outer * 2 - (self.key_rows * self.padding_inner * 2)) / self.key_rows

    def update_dimensions(self):
        self.key_height = self._uniform_key_height()
        self.key_width = []
        for i in range(0, self.key_rows):
            self.key_width.append(self._uniform_key_width(i))

    def find_key_row(self, y_coord):
        return int((y_coord - self.padding_outer) / (self.key_height + self.padding_inner * 2))

    def find_key(self, x_coord, y_coord):
        i_row = self.find_key_row(y_coord)
        i_row = utils.clamp(i_row, 0, self.key_rows - 1)

        iterated_x = self.padding_outer
        for key in self.keys[i_row]:
            adjusted_key_width = key.width_weight * self.key_width[i_row]
            iterated_x += adjusted_key_width + self.padding_inner * 2
            if x_coord < iterated_x:
                return key
        return None

    def get_key_layout(self, target_row, target_col):
        for layout in self.gen_key_layouts():
            if layout.row == target_row and layout.col == target_col:
                return layout
        return None

    def find_col_at_x(self, target_row, x):
        """Pick the column in `target_row` whose pixel range covers `x`,
        falling back to the closest by center if `x` lands in a gap."""
        if not (0 <= target_row < self.key_rows):
            return None
        layouts = [l for l in self.gen_key_layouts() if l.row == target_row]
        if not layouts:
            return None
        for l in layouts:
            if l.x <= x < l.x + l.w:
                return l.col
        best = layouts[0].col
        best_dist = abs((layouts[0].x + layouts[0].w / 2) - x)
        for l in layouts[1:]:
            d = abs((l.x + l.w / 2) - x)
            if d < best_dist:
                best = l.col
                best_dist = d
        return best

    def gen_key_layouts(self):
        iterated_y = self.padding_outer

        for i_row, row in enumerate(self.keys):
            iterated_x = self.padding_outer

            for i_key, key in enumerate(row):
                adj_x = iterated_x + self.padding_inner
                adj_y = iterated_y + self.padding_inner
                adj_w = key.width_weight * self.key_width[i_row]
                adj_h = self.key_height

                yield self.KeyLayout(utils.round_to_int(adj_x), utils.round_to_int(adj_y),
                                     utils.round_to_int(adj_w), utils.round_to_int(adj_h),
                                     i_row, i_key)

                iterated_x += adj_w + self.padding_inner * 2
            iterated_y += self.key_height + self.padding_inner * 2


class KeyButton:
    def __init__(self, str, keycode, callback, width_weight=1.0, shifted=None, modifier=False, align="center", valign="center", glyph=None, font="default", text_color=None, bg_color=None, swap_on_shift=False, shift_glyph=None, shift_valign=None, font_size=None, shift_keycode=None):
        self.str = str
        self.shifted = shifted   # Label shown when shift is held; None means show `str`.
        self.keycode = keycode
        self.callback = callback
        self.width_weight = width_weight
        self.modifier = modifier   # Renderer paints modifier keys with a pure-black background.
        self.align = align         # "left" | "center" | "right" — label alignment inside the key.
        self.valign = valign       # "top" | "center" | "bottom" — vertical label alignment.
        self.glyph = glyph         # Path-relative filename in data/images/glyphs/ (e.g. "glyph_l2.png").
        self.font = font           # "default" | "symbol" — picks the symbol font for glyphs Segoe lacks.
        self.text_color = text_color  # Optional sdl2.ext.Color overriding INACTIVE text color.
        self.bg_color = bg_color   # Optional sdl2.ext.Color overriding INACTIVE key background.
        # When True, the shifted variant fully replaces the main label on shift
        # (e.g. arrow keys ◀↔▲); when False the shifted variant renders as a
        # small gray "shadow" label above the main one (typewriter keys).
        self.swap_on_shift = swap_on_shift
        self.shift_glyph = shift_glyph    # Overrides glyph while shift held ("" = no glyph).
        self.shift_valign = shift_valign  # Overrides valign while shift held.
        self.font_size = font_size        # Optional explicit pixel size for the main label.
        # Optional alternate keycode used when Shift is held at dispatch time
        # (e.g. ◀ sends KEY_LEFT unshifted, KEY_UP while Shift is held).
        self.shift_keycode = shift_keycode
        # Per-key DPAD navigation overrides (target column in the adjacent
        # row); when None, the main loop falls back to pixel-x mapping.
        self.dpad_up = None
        self.dpad_down = None
        self.dpad_left = None
        self.dpad_right = None

    def display_label(self, shift_held, caps_on=False):
        if self.shifted is None:
            return self.str
        # Single-letter alpha keys honor BOTH shift and caps lock.
        if len(self.str) == 1 and self.str.isalpha():
            return self.shifted if (shift_held ^ caps_on) else self.str
        # Number / symbol keys only honor shift (caps lock has no effect).
        return self.shifted if shift_held else self.str


def on_key_generic(virtual_kb, keycode):
    kb.pressEvent([keycode])
    kb.releaseEvent([keycode])


def on_key_shift(virtual_kb, keycode):
    return


def on_key_alt(virtual_kb, keycode):
    return


def on_key_done(virtual_kb, keycode):
    state.close()


def on_key_paste(virtual_kb, keycode):
    # Ctrl+V regardless of any pre-held Shift — temporarily release Shift if
    # the user is holding it via LT, restore afterwards so the OS only sees
    # Ctrl+V, not Ctrl+Shift+V.
    shift_held = state.is_shift_held()
    if shift_held:
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
    kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
    kb.pressEvent([sui.Keys.KEY_V])
    kb.releaseEvent([sui.Keys.KEY_V])
    kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
    if shift_held:
        kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])


def on_key_move(virtual_kb, keycode):
    # Unshifted Move closes the keyboard; with Shift held, instead advance
    # the window through its 6-position rotation (handled in the main loop).
    if state.is_shift_held():
        state.request_position_cycle()
    else:
        state.close()


class VirtualKeyboardConfig(config.ObjectConfig):
    @staticmethod
    def decode_keycode(str):
        try:
            return sui.Keys[str]
        except KeyError:
            assert False, "Invalid keycode `{}`".format(str)

    @staticmethod
    def decode_callback(str):
        if str == "generic":
            pass
        elif str == "shift":
            return on_key_shift
        elif str == "alt":
            return on_key_alt
        elif str == "done":
            return on_key_done
        elif str == "paste":
            return on_key_paste
        elif str == "move":
            return on_key_move
        else:
            assert False, "Invalid behavior `{}`".format(str)
        return on_key_generic

    def construct(self):
        keys = []

        yaml_rows = self.objects["keys"]

        for yaml_row in yaml_rows:
            row = []
            for yaml_key in yaml_row:
                label = "" if "label" not in yaml_key else yaml_key["label"]
                shifted = yaml_key.get("shifted")
                modifier = bool(yaml_key.get("modifier", False))
                align = yaml_key.get("align", "center")
                valign = yaml_key.get("valign", "center")
                glyph = yaml_key.get("glyph")
                font = yaml_key.get("font", "default")
                text_color_str = yaml_key.get("text_color")
                text_color = _parse_hex_color(text_color_str) if text_color_str else None
                bg_color_str = yaml_key.get("bg_color")
                bg_color = _parse_hex_color(bg_color_str) if bg_color_str else None
                swap_on_shift = bool(yaml_key.get("swap_on_shift", False))
                shift_glyph = yaml_key.get("shift_glyph")
                shift_valign = yaml_key.get("shift_valign")
                font_size = yaml_key.get("font_size")
                keycode = 0 if "keycode" not in yaml_key else self.decode_keycode(yaml_key["keycode"])
                shift_keycode_str = yaml_key.get("shift_keycode")
                shift_keycode = self.decode_keycode(shift_keycode_str) if shift_keycode_str else None
                behavior = "generic" if "behavior" not in yaml_key else yaml_key["behavior"]
                width_weight = 1.0 if "width_weight" not in yaml_key else yaml_key["width_weight"]

                callback = self.decode_callback(behavior)
                kb_btn = KeyButton(label, keycode, callback, width_weight,
                                   shifted=shifted, modifier=modifier, align=align, valign=valign,
                                   glyph=glyph, font=font, text_color=text_color, bg_color=bg_color,
                                   swap_on_shift=swap_on_shift, shift_glyph=shift_glyph,
                                   shift_valign=shift_valign, font_size=font_size,
                                   shift_keycode=shift_keycode)
                kb_btn.dpad_up = yaml_key.get("dpad_up")
                kb_btn.dpad_down = yaml_key.get("dpad_down")
                kb_btn.dpad_left = yaml_key.get("dpad_left")
                kb_btn.dpad_right = yaml_key.get("dpad_right")
                row.append(kb_btn)

            keys.append(row)
        return VirtualKeyboard(keys)


def step_cursor(virtual_kb, direction):
    row, col = state.get_cursor()
    rows = len(virtual_kb.keys)
    if not (0 <= row < rows):
        row = max(0, min(row, rows - 1))
        col = 0
    cur_btn = virtual_kb.keys[row][col] if 0 <= col < len(virtual_kb.keys[row]) else None

    if direction == "LEFT":
        override = cur_btn.dpad_left if cur_btn else None
        if override is not None:
            col = override
        elif col > 0:
            col -= 1
    elif direction == "RIGHT":
        override = cur_btn.dpad_right if cur_btn else None
        if override is not None:
            col = override
        elif col < len(virtual_kb.keys[row]) - 1:
            col += 1
    elif direction in ("UP", "DOWN"):
        new_row = row - 1 if direction == "UP" else row + 1
        if 0 <= new_row < rows:
            override = (cur_btn.dpad_up if direction == "UP" else cur_btn.dpad_down) if cur_btn else None
            if override is not None:
                col = override
            else:
                cur_layout = virtual_kb.get_key_layout(row, col)
                if cur_layout is not None:
                    x_center = cur_layout.x + cur_layout.w // 2
                    new_col = virtual_kb.find_col_at_x(new_row, x_center)
                    if new_col is not None:
                        col = new_col
            row = new_row

    col = max(0, min(col, len(virtual_kb.keys[row]) - 1))
    state.set_cursor(row, col)


def dispatch_key(virtual_kb, key):
    # Keys with a `shift_keycode` (e.g. ◀▶ → ▲▼) want to send the alternate
    # keycode WITHOUT the OS seeing a Shift modifier; otherwise Shift+Arrow
    # selects text instead of just moving the caret. Briefly drop and
    # re-raise Shift around the dispatch so the OS sees only the arrow.
    if key.shift_keycode and state.is_shift_held():
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        key.callback(virtual_kb, key.shift_keycode)
        if state.is_shift_held():
            kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
    else:
        key.callback(virtual_kb, key.keycode)


def process_click_queue(virtual_kb, queue):
    while len(queue) > 0:
        x, y = queue.popleft().to_absolute()
        key = virtual_kb.find_key(x, y)
        if key is None:
            continue
        dispatch_key(virtual_kb, key)
