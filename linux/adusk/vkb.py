from collections import namedtuple

import steamcontroller.uinput as sui

from adusk import config
from adusk.color import Color
from adusk import screen
from adusk import state
from adusk import utils

kb = sui.Keyboard()

# Hold-to-repeat cadence shared by EVERY "press a key" input path (controller
# X button, A button, L2/R2/pad-click, and mouse left-click): the key fires
# once, then after KEY_REPEAT_DELAY repeats every KEY_REPEAT_INTERVAL seconds.
# Single source of truth so all input modes rub out / arrow-step at one speed.
KEY_REPEAT_DELAY = 0.4
KEY_REPEAT_INTERVAL = 0.205
# Only these keycodes auto-repeat when held: Backspace and the four arrow
# directions (the ◀▶ keys send ▲▼ under Shift). Every other key fires once.
REPEATABLE_KEYS = frozenset({
    sui.Keys.KEY_BACKSPACE,
    sui.Keys.KEY_LEFT, sui.Keys.KEY_RIGHT,
    sui.Keys.KEY_UP, sui.Keys.KEY_DOWN,
})


def is_repeatable(key):
    """True if holding this key should auto-repeat. Checks both the base and
    the shift keycode so the ◀▶ arrow keys repeat whether or not Shift is
    swapping them to ▲▼."""
    if key is None:
        return False
    return (key.keycode in REPEATABLE_KEYS
            or (key.shift_keycode is not None and key.shift_keycode in REPEATABLE_KEYS))


def _parse_hex_color(s):
    s = s.lstrip("#")
    return Color(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _decode_key_color(s):
    """A per-key color override from the layout: a literal '#rrggbb', or a
    'skin:ROLE' marker (e.g. 'skin:key', 'skin:shadow') resolved against the
    active skin at render time so the key tracks skin changes, or None."""
    if not s:
        return None
    if isinstance(s, str) and s.startswith("skin:"):
        return s
    return _parse_hex_color(s)


class VirtualKeyboard:
    padding_inner = 3
    # Gap from the window edge to the outermost keys = padding_outer +
    # padding_inner, so 2 + 3 = 5 px of background border around the grid.
    padding_outer = 2
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

    def find_key_rc(self, x_coord, y_coord):
        """Like find_key but returns the (row, col) grid index — used by the
        mouse handler to drive the same cursor/press path as the DPAD. Clamps
        to the nearest in-bounds cell so an edge click never misses."""
        i_row = utils.clamp(self.find_key_row(y_coord), 0, self.key_rows - 1)
        iterated_x = self.padding_outer
        for i_key, key in enumerate(self.keys[i_row]):
            iterated_x += key.width_weight * self.key_width[i_row] + self.padding_inner * 2
            if x_coord < iterated_x:
                return (i_row, i_key)
        return (i_row, len(self.keys[i_row]) - 1)

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
        self.text_color = text_color  # Optional Color overriding INACTIVE text color.
        self.bg_color = bg_color   # Optional Color overriding INACTIVE key background.
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
        # Per-key horizontal label nudge in px (e.g. arrows shifted outward).
        self.text_dx = 0
        # Optional smaller font for the shadow preview only (e.g. arrow ▲▼).
        self.shadow_font_size = None
        # Keep this dual-state key's labels at their pre-2026-06-09 rest
        # positions (upper at key.y+3, lower at 4px bottom pad) instead of the
        # nudged-up defaults — set per key in the layout YAML. Animation target
        # (centered) is unchanged either way.
        self.legacy_label_pos = False
        # Per-key fine offsets (px, +down) added to the dual-label REST
        # positions only (the Shift-centered target is unchanged): top_dy nudges
        # the upper label, bottom_dy the lower label. Opposite signs pull the
        # pair together / apart; equal signs shift it. Set per key in the YAML.
        self.dual_top_dy = 0
        self.dual_bottom_dy = 0
        # Per-key transparent-mode text-outline overrides (None = use the
        # Screen defaults): outline_px = sub-pixel ring offset (thicker outline),
        # outline_opacity = 0..1 outline alpha factor. Set per key in the YAML.
        self.outline_px = None
        self.outline_opacity = None

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


def tap_keycode(keycode):
    """Press + release a single keycode (used by the mouse side buttons)."""
    kb.pressEvent([keycode])
    kb.releaseEvent([keycode])


def toggle_shift():
    """Flip the latched-Shift state. Unlike the controller's L2 (held only while
    the trigger is down), the mouse/keyboard path latches Shift so it stays on
    until clicked again — the only sane model when there's no button to hold.
    Holds real KEY_LEFTSHIFT on the OS so the next key produces its shifted
    form, and paints the on-screen Shift keys blue while engaged.

    Decides on/off from our OWN latch flag, not state.is_shift_held(): a
    connected controller rewrites is_shift_held() every input frame, so reading
    it here would see False on every click and re-press forever (never toggle
    off). The controller ORs the latch into the display state, so they cooperate."""
    new = not state.is_shift_latched()
    state.set_shift_latched(new)
    if new:
        kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
        state.set_highlighted({sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT})
    else:
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        state.set_highlighted(set())
    state.set_shift_held(new)


def release_shift():
    """Force-release the OS Shift key so hiding/closing the keyboard never
    leaves Shift stuck down. Unconditional on purpose: the latched-state flag
    can be out of sync with the real OS key (a controller input frame
    overwrites state.is_shift_held() every tick, and either the mouse toggle or
    a held L2 may own the OS key), and a pynput Shift key-up is idempotent —
    harmless if nothing was held — so we always send it."""
    kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
    kb.releaseEvent([sui.Keys.KEY_RIGHTSHIFT])
    state.set_shift_latched(False)
    state.set_shift_held(False)
    state.set_highlighted(set())


def clear_shift_latch(release_os=True):
    """Drop a mouse-latched Shift when another input source takes over (the
    Steam Controller's A button or the DPAD), reverting the sticky mouse toggle
    to the hold model those controls use. Releases the OS Shift the latch was
    holding unless release_os is False (e.g. L2 is currently holding Shift, so
    the OS key must stay down). Display state/highlight are recomputed by the
    controller's next input frame."""
    if not state.is_shift_latched():
        return
    state.set_shift_latched(False)
    if release_os:
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        state.set_shift_held(False)


def on_key_shift(virtual_kb, keycode):
    # Clicking the on-screen Shift key (mouse left-click or the A button)
    # toggles the latched Shift state.
    toggle_shift()


def on_key_paste(virtual_kb, keycode):
    # Paste, or Copy when Shift is held: Shift → Ctrl+C, otherwise Ctrl+V.
    # ALWAYS release both shifts first — not only when our logical state says
    # Shift is held. A shift-mode press re-presses Shift on THIS keyboard
    # instance (vkb.kb) to restore an L2-held Shift, but L2's release lands on
    # the controller thread's SEPARATE instance, so vkb.kb is left believing
    # Shift is still down. pynput then re-asserts that Shift around the next key,
    # breaking the chord (e.g. Ctrl+V arrives as Shift+V → "V"). Releasing here
    # resets vkb.kb's modifier state; then the chord via tap_with_modifier (raw
    # VK so it combines with Ctrl), then restore Shift if it's logically held.
    shift_held = state.is_shift_held()
    kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT])
    kb.tap_with_modifier(sui.Keys.KEY_LEFTCTRL,
                         sui.Keys.KEY_C if shift_held else sui.Keys.KEY_V)
    if shift_held:
        kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])


def on_key_emoji(virtual_kb, keycode):
    # Toggle the OS emoji picker: open it with Win+. (Windows) / Meta+. (Linux),
    # or — if our last emoji press opened it — close it with Escape, so pressing
    # the on-screen emoji key again dismisses the picker. ALWAYS release both
    # shifts first so the OS sees Meta+. (not Meta+Shift+., a different shortcut)
    # AND to clear any Shift stranded in vkb.kb's modifier state by a prior L2
    # shift paste (see on_key_paste), then restore Shift only if logically held.
    shift_held = state.is_shift_held()
    kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT])
    if state.is_emoji_open():
        # Already open → Escape closes the focused picker.
        kb.pressEvent([sui.Keys.KEY_ESC])
        kb.releaseEvent([sui.Keys.KEY_ESC])
        state.set_emoji_open(False)
    else:
        kb.tap_with_modifier(sui.Keys.KEY_LEFTMETA, sui.Keys.KEY_DOT)
        state.set_emoji_open(True)
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
        elif str == "paste":
            return on_key_paste
        elif str == "emoji":
            return on_key_emoji
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
                text_color = _decode_key_color(yaml_key.get("text_color"))
                bg_color = _decode_key_color(yaml_key.get("bg_color"))
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
                kb_btn.text_dx = yaml_key.get("text_dx", 0)
                kb_btn.shadow_font_size = yaml_key.get("shadow_font_size")
                kb_btn.legacy_label_pos = yaml_key.get("legacy_label_pos", False)
                kb_btn.dual_top_dy = yaml_key.get("dual_top_dy", 0)
                kb_btn.dual_bottom_dy = yaml_key.get("dual_bottom_dy", 0)
                kb_btn.outline_px = yaml_key.get("outline_px")
                kb_btn.outline_opacity = yaml_key.get("outline_opacity")
                row.append(kb_btn)

            keys.append(row)
        return VirtualKeyboard(keys)


def step_cursor(virtual_kb, direction, haptic=False):
    start = state.get_cursor()
    row, col = start
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
        else:
            # Wrap horizontally back to the last key in the row.
            col = len(virtual_kb.keys[row]) - 1
    elif direction == "RIGHT":
        override = cur_btn.dpad_right if cur_btn else None
        if override is not None:
            col = override
        elif col < len(virtual_kb.keys[row]) - 1:
            col += 1
        else:
            # Wrap horizontally back to the first key in the row.
            col = 0
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
    # Tick the haptics when the selected key changes AND the move was driven by
    # the left stick (haptic=True). DPAD navigation passes haptic=False so only
    # the stick buzzes. Gated internally by the global haptics switch.
    if haptic and (row, col) != start:
        state.haptic_tick()


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
        item = queue.popleft()
        # A bare coord is a normal first hit (any key). A ("repeat", coord)
        # tuple is an auto-repeat from holding L2/R2/pad-click — honoured only
        # over Backspace, so holding rubs out text but won't machine-gun
        # ordinary keys (matches the X-button delete repeat).
        is_repeat = isinstance(item, tuple) and item and item[0] == "repeat"
        coord = item[1] if is_repeat else item
        x, y = coord.to_absolute()
        key = virtual_kb.find_key(x, y)
        if key is None:
            continue
        if is_repeat and not is_repeatable(key):
            continue
        dispatch_key(virtual_kb, key)
        # Note: the click haptic is fired earlier, on the controller thread at
        # click-detection (see ControllerManager.handle_pad_input), for lowest
        # latency — so it is intentionally NOT fired here.
