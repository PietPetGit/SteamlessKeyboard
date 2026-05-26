#!/usr/env/python3
# -*- coding: utf-8 -*-

import ctypes
from threading import Thread

import sdl2
import sdl2.ext

from adusk import screen
from adusk.screen import CoordFraction
from adusk import config
from adusk import controller
from adusk import state
from adusk import vkb
from adusk import vptr


def _hwnd_of(sdl_window):
    wm_info = sdl2.SDL_SysWMinfo()
    sdl2.SDL_VERSION(wm_info.version)
    if sdl2.SDL_GetWindowWMInfo(sdl_window, ctypes.byref(wm_info)) != sdl2.SDL_TRUE:
        return None
    return wm_info.info.win.window


# Win32 constants used by the focus / z-order helpers below.
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TOPMOST    = 0x00000008
_SW_SHOWNOACTIVATE = 4
_SWP_NOMOVE        = 0x0002
_SWP_NOSIZE        = 0x0001
_SWP_NOACTIVATE    = 0x0010
_SWP_FRAMECHANGED  = 0x0020
_SWP_SHOWWINDOW    = 0x0040
# HWND_TOPMOST is the sentinel (-1) for SetWindowPos's hWndInsertAfter param.
# It must be passed as a 64-bit HANDLE on x64; using c_void_p(-1) coerces it
# to all-bits-set in the wider register.
_HWND_TOPMOST = ctypes.c_void_p(-1)


def _user32():
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]
    user32.SetWindowPos.restype = ctypes.c_bool
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.ShowWindow.restype = ctypes.c_bool
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.AttachThreadInput.restype = ctypes.c_bool
    user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    return user32


def _force_topmost(hwnd):
    """Make `hwnd` topmost without stealing focus. On Windows, a non-foreground
    process can have its SetWindowPos(HWND_TOPMOST) silently downgraded — most
    common workaround is to briefly attach our input queue to the foreground
    thread's, then issue the SetWindowPos. The attach makes the elevation
    check pass; SWP_NOACTIVATE + WS_EX_NOACTIVATE on the window keep focus
    where it was."""
    user32 = _user32()
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

    flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
    foreground = user32.GetForegroundWindow()
    fg_thread = (user32.GetWindowThreadProcessId(foreground, None)
                 if foreground else 0)
    cur_thread = kernel32.GetCurrentThreadId()

    attached = False
    if fg_thread and fg_thread != cur_thread:
        attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
    try:
        user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, flags)
    finally:
        if attached:
            user32.AttachThreadInput(cur_thread, fg_thread, False)


def _make_window_non_activating(sdl_window):
    """Apply WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST to the SDL
    window's HWND while it's still hidden. NOACTIVATE keeps the user's
    target app (e.g. a browser search field) focused; TOPMOST registers
    the window as always-on-top so SetWindowPos can actually elevate it."""
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        print("warning: SDL_GetWindowWMInfo failed; window will steal focus")
        return
    user32 = _user32()
    current = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE,
                             current | _WS_EX_NOACTIVATE
                             | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST)
    # Flush the latent ex-style change (SetWindowLong values only take
    # effect after a SetWindowPos with SWP_FRAMECHANGED).
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
                        | _SWP_FRAMECHANGED)


def _show_window_noactivate(sdl_window):
    """Bring the OSK on screen without stealing focus and force it into the
    topmost z-order. WS_EX_NOACTIVATE on the HWND means even Win32 calls
    that would normally activate the window (SetForegroundWindow,
    SetWindowPos without SWP_NOACTIVATE) leave focus alone."""
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        sdl2.SDL_ShowWindow(sdl_window)
        return
    user32 = _user32()
    user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
    _force_topmost(hwnd)


# Index into the 6-position window rotation, advanced by Shift+Move.
# 0 starts at down-mid (the default open location).
_position_index = [0]


def _cycle_window_position(sdl_window):
    bounds = sdl2.SDL_Rect()
    if sdl2.SDL_GetDisplayUsableBounds(0, ctypes.byref(bounds)) != 0:
        return
    _position_index[0] = (_position_index[0] + 1) % 6
    w = screen.width
    h = screen.height
    x_left = bounds.x
    x_mid = bounds.x + max(0, (bounds.w - w) // 2)
    x_right = bounds.x + max(0, bounds.w - w)
    y_top = bounds.y
    y_bot = bounds.y + max(0, bounds.h - h)
    # 0 down-mid (start) → 1 down-left → 2 up-left → 3 up-mid → 4 up-right → 5 down-right → 0.
    seq = [
        (x_mid,   y_bot),
        (x_left,  y_bot),
        (x_left,  y_top),
        (x_mid,   y_top),
        (x_right, y_top),
        (x_right, y_bot),
    ]
    x, y = seq[_position_index[0]]
    sdl2.SDL_SetWindowPosition(sdl_window, x, y)


def load_kb_config():
    kb_config = vkb.VirtualKeyboardConfig()
    kb_layout_file = config.YamlFile("keyboard-layout.yaml")
    kb_layout_file.read()
    kb_layout_file.add_to_config("keys", kb_config)
    return kb_config


def main():
    # Reset the Move-key position cycle so each fresh open lands on the
    # default (down-middle) location.
    _position_index[0] = 0

    controller_state = controller.ControllerState()
    controller_state.set_pointers(
            vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(1/4, 1/2)),
            vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(3/4, 1/2))
    )

    virtual_kb = load_kb_config().construct()
    # Publish per-row key counts so the controller thread can clamp DPAD
    # navigation against the actual layout.
    state.set_grid_dims([len(r) for r in virtual_kb.keys])

    sc_thread = Thread(target=controller.input_thread, args=(controller_state,), daemon=True)
    sc_thread.start()

    sdl2.ext.init()
    scr = screen.Screen()
    # Tag the still-hidden window with NOACTIVATE + TOPMOST + TOOLWINDOW,
    # then bring it on screen via the no-activate Win32 path. Doing it in
    # this order keeps focus on the user's target app AND puts the OSK on
    # top of normal windows like the browser.
    _make_window_non_activating(scr.window.window)
    _show_window_noactivate(scr.window.window)
    was_visible = True
    # Last key under each touchpad pointer, for haptic "switched key" ticks.
    last_hover = [None, None]

    while not state.should_close():
        for event in sdl2.ext.get_events():
            if event.type == sdl2.SDL_QUIT:
                state.close()
                break
            if event.type == sdl2.SDL_WINDOWEVENT:
                if event.window.event == sdl2.SDL_WINDOWEVENT_RESIZED:
                    screen.width = event.window.data1
                    screen.height = event.window.data2

        cur_visible = state.is_visible()
        if cur_visible != was_visible:
            if cur_visible:
                _show_window_noactivate(scr.window.window)
            else:
                sdl2.SDL_HideWindow(scr.window.window)
            was_visible = cur_visible

        if cur_visible:
            virtual_kb.update_dimensions()
            # DPAD: step the cursor using the actual layout pixel positions.
            for direction in state.drain_dpad_queue():
                vkb.step_cursor(virtual_kb, direction)
            vkb.process_click_queue(virtual_kb, controller_state.click_queue)
            # A button: fire the callback of the key under the DPAD cursor.
            for row, col in state.drain_key_press_queue():
                if 0 <= row < len(virtual_kb.keys) and 0 <= col < len(virtual_kb.keys[row]):
                    vkb.dispatch_key(virtual_kb, virtual_kb.keys[row][col])
            if state.take_position_cycle_request():
                _cycle_window_position(scr.window.window)
            pointers = controller_state.get_pointers()
            # Haptic tick when a touchpad pointer moves onto a different key
            # (touchpad mode only — the pointer is INACTIVE when not touching).
            for i in (0, 1):
                ptr = pointers[i]
                if ptr.state != state.InputState.INACTIVE:
                    px, py = ptr.coord_frac.to_absolute()
                    hovered = virtual_kb.find_key(px, py)
                    if hovered is not None and hovered is not last_hover[i]:
                        state.haptic_tick()
                        last_hover[i] = hovered
                else:
                    last_hover[i] = None
            scr.render(virtual_kb, pointers)
        else:
            # Drain any clicks that fired while hidden so they don't pile up.
            controller_state.click_queue.clear()
            state.drain_key_press_queue()
            state.drain_dpad_queue()
        scr.delay()

    # Give the controller thread up to 1 second to run its cleanup (sends
    # the enable-lizard packet before closing the HID handle). Without this
    # wait the daemon thread is killed before it can re-enable lizard mode.
    sc_thread.join(timeout=1.0)
    sdl2.ext.quit()


if __name__ == '__main__':
    main()
