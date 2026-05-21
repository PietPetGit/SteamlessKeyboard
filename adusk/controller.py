import copy
from collections import deque
from threading import Lock

import steamcontroller.uinput as sui
from steamcontroller import SteamController, SCButtons, SCStatus, SCI_NULL
from steamcontroller.events import EventMapper

from adusk import screen
from adusk.screen import CoordFraction
from adusk import state
from adusk import utils
from adusk import vptr


class ControllerState:
    click_queue = deque()

    _pointers = None
    _pointer_lock = Lock()

    def set_pointers(self, ptr_left, ptr_right):
        with self._pointer_lock:
            self._pointers = (ptr_left, ptr_right)

    def get_pointers(self):
        with self._pointer_lock:
            ret = copy.deepcopy(self._pointers)
        return ret


def on_button_exit(evm, button, pressed):
    if not pressed:
        state.close()


def adjust_raw_x(raw_x, center_fraction, scalar=6/5):
    abs_max = 0x20000
    return utils.round_to_int(screen.width * (center_fraction + scalar * raw_x/abs_max))


def adjust_raw_y(raw_y, center_fraction, scalar=6/5):
    abs_max = 0x10000
    return utils.round_to_int(screen.height * (center_fraction + scalar * -raw_y/abs_max))


class ControllerManager:
    pad_smoothing = 0.15
    sc_input_previous = SCI_NULL

    def __init__(self, controller_state):
        self.controller_state = controller_state

        prev_ptrs = controller_state.get_pointers()
        self.prev_ptr_left = prev_ptrs[0]
        self.prev_ptr_right = prev_ptrs[1]

        # Steam+X / Steam-alone chord tracking
        self._steam_was_pressed = False
        self._saw_x_during_steam = False

        self.evm = EventMapper()
        self._map_events()

    def _map_events(self):
        # Face buttons whose action is unconditional ride EventMapper. The
        # conditional bindings (LT/RT switch role while the same-side touchpad
        # is being touched) and the latching ones (L3, B, LGRIP, A, DPAD) are
        # handled manually in handle_input.
        self.evm.setButtonAction(SCButtons.X, sui.Keys.KEY_BACKSPACE)  # X → Backspace
        self.evm.setButtonAction(SCButtons.Y, sui.Keys.KEY_SPACE)      # Y → Space
        # R4 / R5 back paddles → Space (Steam OSK official mapping).
        self.evm.setButtonAction(SCButtons.RGRIP1, sui.Keys.KEY_SPACE)
        self.evm.setButtonAction(SCButtons.RGRIP2, sui.Keys.KEY_SPACE)

        # Rising-edge latches for manually-handled buttons.
        self._l3_was_pressed = False
        self._b_was_pressed = False
        self._lgrip_was_pressed = False
        self._a_was_pressed = False
        self._dpad_prev = 0
        # Tracks whether we are currently holding KEY_LEFTSHIFT / KEY_ENTER on
        # the OS side (driven by LT/RT but gated by touchpad contact).
        self._shift_active = False
        self._enter_active = False
        self._kb = sui.Keyboard()

    def handle_pad_input(self, coord_frac, buttons, touch_button_mask, select_button_mask):
        if buttons & touch_button_mask:
            if buttons & select_button_mask:
                # Handle click if previous buttons did not include both `touch_button` and `select_button`
                if ~self.sc_input_previous.buttons & (touch_button_mask | select_button_mask) != 0:
                    self.controller_state.click_queue.append(coord_frac)
                return state.InputState.CLICK
            else:
                return state.InputState.HOVER
        return state.InputState.INACTIVE

    def handle_input(self, sc, sc_input):
        self.evm.process(sc, sc_input)

        # L3 → Caps Lock. Manual rising-edge detection so the binding doesn't
        # re-fire while the user keeps their finger on the stick after clicking.
        l3_pressed = bool(sc_input.buttons & SCButtons.L3)
        if l3_pressed and not self._l3_was_pressed:
            self._kb.pressEvent([sui.Keys.KEY_CAPSLOCK])
            self._kb.releaseEvent([sui.Keys.KEY_CAPSLOCK])
        self._l3_was_pressed = l3_pressed

        # Publish touchpad capacitive-touch state so the renderer can hide
        # the L2/R2 hint glyphs while LT/RT's pad-click alternate is active.
        lpad_touched = bool(sc_input.buttons & SCButtons.LPADTOUCH)
        rpad_touched = bool(sc_input.buttons & SCButtons.RPADTOUCH)
        state.set_pad_touched(lpad_touched, rpad_touched)

        # LT (L2) → Shift, but only when the left pad is NOT being touched.
        # Touching the pad takes LT out of "shift" mode and into "click the
        # key under the left pointer" mode (the click itself is queued by
        # handle_pad_input below).
        lt_pressed = bool(sc_input.buttons & SCButtons.LT)
        shift_should_hold = lt_pressed and not lpad_touched
        if shift_should_hold and not self._shift_active:
            self._kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = True
        elif not shift_should_hold and self._shift_active:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = False

        # RT (R2) → Enter, but only when the right pad is NOT being touched
        # (mirror of the LT rule above).
        rt_pressed = bool(sc_input.buttons & SCButtons.RT)
        enter_should_hold = rt_pressed and not rpad_touched
        if enter_should_hold and not self._enter_active:
            self._kb.pressEvent([sui.Keys.KEY_ENTER])
            self._enter_active = True
        elif not enter_should_hold and self._enter_active:
            self._kb.releaseEvent([sui.Keys.KEY_ENTER])
            self._enter_active = False

        # Mirror Shift state to the renderer so it can show uppercase labels.
        state.set_shift_held(self._shift_active)

        # B and L4/L5 (LGRIP) close the keyboard, on rising edge.
        b_pressed = bool(sc_input.buttons & SCButtons.B)
        if b_pressed and not self._b_was_pressed:
            state.close()
        self._b_was_pressed = b_pressed

        lgrip_pressed = bool(sc_input.buttons & SCButtons.LGRIP)
        if lgrip_pressed and not self._lgrip_was_pressed:
            state.close()
        self._lgrip_was_pressed = lgrip_pressed

        # DPAD navigates the cursor over the keyboard grid (one step per
        # press). Direction events are queued for the main loop, which knows
        # the layout's pixel widths and can pick the visually-aligned target.
        dpad_mask = (SCButtons.DPAD_UP | SCButtons.DPAD_DOWN
                     | SCButtons.DPAD_LEFT | SCButtons.DPAD_RIGHT)
        dpad_now = sc_input.buttons & dpad_mask
        dpad_newly = (self._dpad_prev ^ dpad_now) & dpad_now
        if dpad_newly & SCButtons.DPAD_UP:
            state.queue_dpad("UP")
        if dpad_newly & SCButtons.DPAD_DOWN:
            state.queue_dpad("DOWN")
        if dpad_newly & SCButtons.DPAD_LEFT:
            state.queue_dpad("LEFT")
        if dpad_newly & SCButtons.DPAD_RIGHT:
            state.queue_dpad("RIGHT")
        self._dpad_prev = dpad_now

        # A → press the key currently under the DPAD cursor (rising edge).
        a_pressed = bool(sc_input.buttons & SCButtons.A)
        if a_pressed and not self._a_was_pressed:
            row, col = state.get_cursor()
            state.queue_key_press(row, col)
        self._a_was_pressed = a_pressed

        # Visual highlight: paint the on-screen key blue while its bound
        # controller button is held down.
        highlights = set()
        if self._shift_active:
            highlights.add(sui.Keys.KEY_LEFTSHIFT)
            highlights.add(sui.Keys.KEY_RIGHTSHIFT)
        if l3_pressed:
            highlights.add(sui.Keys.KEY_CAPSLOCK)
        if sc_input.buttons & SCButtons.X:
            highlights.add(sui.Keys.KEY_BACKSPACE)
        if self._enter_active:
            highlights.add(sui.Keys.KEY_ENTER)
        if sc_input.buttons & (SCButtons.Y | SCButtons.RGRIP):
            highlights.add(sui.Keys.KEY_SPACE)
        state.set_highlighted(highlights)

        # Steam+X opens the keyboard; Steam pressed and released alone closes it.
        steam_now = bool(sc_input.buttons & SCButtons.STEAM)
        x_now = bool(sc_input.buttons & SCButtons.X)
        if steam_now and not self._steam_was_pressed:
            self._saw_x_during_steam = False
        if steam_now and x_now and not self._saw_x_during_steam:
            self._saw_x_during_steam = True
            state.show()
        if self._steam_was_pressed and not steam_now and not self._saw_x_during_steam:
            state.close()
        self._steam_was_pressed = steam_now

        if self.sc_input_previous == SCI_NULL:
            self.sc_input_previous = sc_input
            return

        ptr_left_coords = CoordFraction.from_absolute(adjust_raw_x(sc_input.lpad_x, 1/4),
                                                      adjust_raw_y(sc_input.lpad_y, 1/2))
        ptr_right_coords = CoordFraction.from_absolute(adjust_raw_x(sc_input.rpad_x, 3/4),
                                                       adjust_raw_y(sc_input.rpad_y, 1/2))

        input_state_left = self.handle_pad_input(ptr_left_coords, sc_input.buttons,
                                                 SCButtons.LPADTOUCH, SCButtons.LT)
        input_state_right = self.handle_pad_input(ptr_right_coords, sc_input.buttons,
                                                  SCButtons.RPADTOUCH, SCButtons.RT)

        ptr_left = vptr.VirtualPointer(input_state_left, ptr_left_coords)
        ptr_right = vptr.VirtualPointer(input_state_right, ptr_right_coords)

        ptr_left.smoothen(self.prev_ptr_left, self.pad_smoothing)
        ptr_right.smoothen(self.prev_ptr_right, self.pad_smoothing)
        self.prev_ptr_left = copy.deepcopy(ptr_left)
        self.prev_ptr_right = copy.deepcopy(ptr_right)
        self.sc_input_previous = sc_input

        self.controller_state.set_pointers(ptr_left, ptr_right)


def update(sc, sc_input, manager):
    if state.should_close():
        # Adusk is shutting down — tell the controller thread to exit so it
        # can run its cleanup (re-enable lizard mode) before being killed.
        sc.addExit()
        return
    if sc_input.status != SCStatus.INPUT:
        return
    manager.handle_input(sc, sc_input)


def input_thread(controller_state):
    manager = ControllerManager(controller_state)
    sc = SteamController(callback=update, callback_args=(manager,))
    sc.run()
