#!/usr/env/python3
# -*- coding: utf-8 -*-

import ctypes
import sys
import time
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


_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# X11/XWayland: deny keyboard focus
# ---------------------------------------------------------------------------
#
# SDL_HINT_WINDOW_NO_ACTIVATION_WHEN_SHOWN is a no-op on SDL's X11 backend,
# so by default the OSK window steals focus the moment it's mapped. KWin
# (Plasma 6) and most other WMs accept two parallel signals to keep a
# window out of the focus rotation:
#
#   1. WM_HINTS.input = False   — ICCCM "don't give me keyboard focus"
#   2. _NET_WM_WINDOW_TYPE_DOCK — EWMH dock windows are not focusable
#
# Plus SKIP_TASKBAR/SKIP_PAGER/ABOVE so the OSK doesn't clutter the task
# switcher and stays on top. The window can still receive its own pointer
# events; only kbd focus is denied.

_X11_INPUT_HINT = 1 << 0  # InputHint from <X11/Xutil.h>


class _XWMHints(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_long),
        ("input", ctypes.c_int),
        ("initial_state", ctypes.c_int),
        ("icon_pixmap", ctypes.c_ulong),
        ("icon_window", ctypes.c_ulong),
        ("icon_x", ctypes.c_int),
        ("icon_y", ctypes.c_int),
        ("icon_mask", ctypes.c_ulong),
        ("window_group", ctypes.c_ulong),
    ]


_libx11_cache = None


def _libx11():
    global _libx11_cache
    if _libx11_cache is not None:
        return _libx11_cache
    try:
        lib = ctypes.cdll.LoadLibrary("libX11.so.6")
    except OSError:
        return None
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XChangeProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
    ]
    lib.XChangeProperty.restype = ctypes.c_int
    lib.XSetWMHints.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
    lib.XSetWMHints.restype = ctypes.c_int
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XFlush.restype = ctypes.c_int
    _libx11_cache = lib
    return lib


def _x11_handles(sdl_window):
    """Pull the X11 (Display*, Window) pair out of SDL_SysWMinfo, or
    (None, None) when SDL isn't on the X11 backend / call failed."""
    wm_info = sdl2.SDL_SysWMinfo()
    sdl2.SDL_VERSION(wm_info.version)
    if sdl2.SDL_GetWindowWMInfo(sdl_window, ctypes.byref(wm_info)) != sdl2.SDL_TRUE:
        return None, None
    try:
        if wm_info.subsystem != sdl2.SDL_SYSWM_X11:
            return None, None
    except AttributeError:
        return None, None
    try:
        return wm_info.info.x11.display, wm_info.info.x11.window
    except AttributeError:
        return None, None


def _make_window_no_focus_x11(sdl_window):
    """Mark the OSK as non-focusable to the X11 WM. Must run BEFORE
    SDL_ShowWindow — KWin reads WM_HINTS at map time. The SDL window is
    created with SDL_WINDOW_HIDDEN precisely to give us this window."""
    if not _IS_LINUX:
        return
    display, window = _x11_handles(sdl_window)
    if not display or not window:
        return
    x11 = _libx11()
    if x11 is None:
        return
    try:
        hints = _XWMHints(flags=_X11_INPUT_HINT, input=0)
        x11.XSetWMHints(display, window, ctypes.byref(hints))

        type_atom = x11.XInternAtom(display, b"_NET_WM_WINDOW_TYPE", 0)
        dock_atom = x11.XInternAtom(display, b"_NET_WM_WINDOW_TYPE_DOCK", 0)
        atom_val = (ctypes.c_ulong * 1)(dock_atom)
        # XA_ATOM=4, PropModeReplace=0, format=32, nelements=1
        x11.XChangeProperty(display, window, type_atom, 4, 32, 0,
                            ctypes.cast(atom_val, ctypes.c_void_p), 1)

        state_atom = x11.XInternAtom(display, b"_NET_WM_STATE", 0)
        skip_tb = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_TASKBAR", 0)
        skip_pg = x11.XInternAtom(display, b"_NET_WM_STATE_SKIP_PAGER", 0)
        above = x11.XInternAtom(display, b"_NET_WM_STATE_ABOVE", 0)
        states = (ctypes.c_ulong * 3)(skip_tb, skip_pg, above)
        x11.XChangeProperty(display, window, state_atom, 4, 32, 0,
                            ctypes.cast(states, ctypes.c_void_p), 3)
        x11.XFlush(display)
    except Exception as e:
        print(f"warning: X11 no-focus setup failed: {e!r}")


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


def _set_always_on_top_portable(sdl_window):
    """SDL 2.0.16+ exposes SDL_SetWindowAlwaysOnTop. PySDL2 may not bind it,
    so dlsym it off the loaded SDL2 library directly. Used on Linux/X11 to
    request _NET_WM_STATE_ABOVE without focus stealing."""
    try:
        fn = sdl2.SDL_SetWindowAlwaysOnTop  # type: ignore[attr-defined]
    except AttributeError:
        try:
            dll = sdl2.dll.dll  # PySDL2 wraps the loaded SDL2 library
        except AttributeError:
            return
        try:
            fn = dll.SDL_SetWindowAlwaysOnTop
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
            fn.restype = None
        except (AttributeError, OSError):
            return
    try:
        fn(sdl_window, 1)
    except Exception:
        pass


def _make_window_non_activating(sdl_window):
    """Apply WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST to the SDL
    window's HWND while it's still hidden. NOACTIVATE keeps the user's
    target app (e.g. a browser search field) focused; TOPMOST registers
    the window as always-on-top so SetWindowPos can actually elevate it.

    On Linux the SDL "no activation" hint is a no-op for the X11 backend,
    so we patch WM_HINTS.input=False + _NET_WM_WINDOW_TYPE_DOCK via
    libX11 directly. The SDL window is still SDL_WINDOW_HIDDEN at this
    point — the WM only reads these atoms when the window is mapped."""
    if not _IS_WINDOWS:
        _make_window_no_focus_x11(sdl_window)
        return
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
    SetWindowPos without SWP_NOACTIVATE) leave focus alone.

    On X11 we fall back to SDL's portable show + always-on-top; combined
    with SDL_HINT_WINDOW_NO_ACTIVATION_WHEN_SHOWN (set in Screen.__init__)
    most compositors will skip focus on map."""
    if not _IS_WINDOWS:
        sdl2.SDL_ShowWindow(sdl_window)
        _set_always_on_top_portable(sdl_window)
        return
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

    # Adaptive render rate. The keyboard only needs a fast loop while the user
    # is actually doing something (smooth pointer + low-latency hover haptics);
    # an open-but-idle keyboard redrawing 120x/sec is wasted CPU. So we run at
    # ACTIVE_FPS while there's activity and for a short grace period after, then
    # drop to IDLE_FPS. The next input snaps it straight back to ACTIVE_FPS.
    ACTIVE_FPS = 120
    IDLE_FPS = 15
    IDLE_GRACE = 0.4
    current_fps = ACTIVE_FPS
    last_active = time.monotonic()

    while not state.should_close():
        activity = False
        for event in sdl2.ext.get_events():
            if event.type == sdl2.SDL_QUIT:
                state.close()
                break
            if event.type == sdl2.SDL_WINDOWEVENT:
                if event.window.event == sdl2.SDL_WINDOWEVENT_RESIZED:
                    screen.width = event.window.data1
                    screen.height = event.window.data2
                    activity = True

        cur_visible = state.is_visible()
        if cur_visible != was_visible:
            if cur_visible:
                _show_window_noactivate(scr.window.window)
            else:
                sdl2.SDL_HideWindow(scr.window.window)
            was_visible = cur_visible
            activity = True

        if cur_visible:
            virtual_kb.update_dimensions()
            # DPAD: step the cursor using the actual layout pixel positions.
            dpad_steps = state.drain_dpad_queue()
            for direction, haptic in dpad_steps:
                vkb.step_cursor(virtual_kb, direction, haptic=haptic)
            if controller_state.click_queue:
                activity = True
            vkb.process_click_queue(virtual_kb, controller_state.click_queue)
            # A button: fire the callback of the key under the DPAD cursor.
            key_presses = state.drain_key_press_queue()
            for row, col in key_presses:
                if 0 <= row < len(virtual_kb.keys) and 0 <= col < len(virtual_kb.keys[row]):
                    vkb.dispatch_key(virtual_kb, virtual_kb.keys[row][col])
            if state.take_position_cycle_request():
                _cycle_window_position(scr.window.window)
                activity = True
            if dpad_steps or key_presses:
                activity = True
            pointers = controller_state.get_pointers()
            # Haptic tick when a touchpad pointer moves onto a different key
            # (touchpad mode only — the pointer is INACTIVE when not touching).
            for i in (0, 1):
                ptr = pointers[i]
                if ptr.state != state.InputState.INACTIVE:
                    activity = True  # finger on the pad → keep the loop fast
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

        # Choose the frame cap from recent activity. The grace period keeps us at
        # ACTIVE_FPS through brief pauses (e.g. between keystrokes) so the rate
        # doesn't flap; a genuinely idle keyboard settles to IDLE_FPS.
        nowt = time.monotonic()
        if activity:
            last_active = nowt
        desired_fps = ACTIVE_FPS if (nowt - last_active) < IDLE_GRACE else IDLE_FPS
        if desired_fps != current_fps:
            scr.set_framerate(desired_fps)
            current_fps = desired_fps
        scr.delay()

    # Give the controller thread up to 1 second to run its cleanup (sends
    # the enable-lizard packet before closing the HID handle). Without this
    # wait the daemon thread is killed before it can re-enable lizard mode.
    sc_thread.join(timeout=1.0)
    sdl2.ext.quit()


if __name__ == '__main__':
    main()
