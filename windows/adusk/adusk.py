#!/usr/env/python3
# -*- coding: utf-8 -*-

import ctypes
import sys
import time
from threading import Thread

import sdl3w as S
import steamcontroller.uinput as sui

from adusk import screen
from adusk.screen import CoordFraction
from adusk import config
from adusk import controller
from adusk import state
from adusk import vkb
from adusk import vptr


_IS_WINDOWS = sys.platform == "win32"

# Main-loop pacing. The loop runs fast so SDL-pad input (polled/published here)
# and the resulting cursor steps / key presses are drained with minimal latency
# — matching the Steam Controller, which the input thread reads directly. The
# expensive render is throttled separately to the display rate.
_LOOP_SLEEP = 0.002          # ~500 Hz loop (cheap input work each iteration)
_RENDER_INTERVAL = 1.0 / 120  # render/hover-haptic cadence
# How often to re-check _is_start_menu_open() while the OSK is already visible,
# to live-reposition if the Start menu opens/closes underneath it. Cheap
# (GetForegroundWindow + a process-name lookup), but not free, so this is
# throttled well below _RENDER_INTERVAL — Start opening/closing is a
# human-timescale event.
_START_MENU_POLL_INTERVAL = 0.2

# OSK OPEN animation (see screen.render_open_anim + main's render loop), over
# _OPEN_ANIM_SECS. Three eased effects, each on its own slice of the timeline:
#   • opacity 0→100% within the first _OPEN_ANIM_FADE_FRAC (a gradual fade-in);
#   • the bottom _OPEN_ANIM_CUT_PX, hidden at the start, revealed as the cut
#     slides down over the first _OPEN_ANIM_REVEAL_FRAC;
#   • then a _OPEN_ANIM_DROP_PX downward settle into the final position, begun at
#     _OPEN_ANIM_MOVE_START_FRAC (earlier than the reveal end, so they overlap)
#     and finishing at the end.
# Tuned to feel like the keyboard rising into place.
_OPEN_ANIM_SECS = 0.40
_OPEN_ANIM_CUT_PX = 140
_OPEN_ANIM_DROP_PX = 35
_OPEN_ANIM_REVEAL_FRAC = 0.66
_OPEN_ANIM_FADE_FRAC = 1.0          # opacity fades 0→100% across the WHOLE open
                                   # — a gradual fade-in (~0.40s, ~3.4x longer
                                   # than the old 0.117s first-third fade)
_OPEN_ANIM_MOVE_START_FRAC = 0.33  # downward settle starts here (earlier)


def _ease_out_cubic(t):
    """Decelerating ease (fast start, soft landing) on a 0..1 progress."""
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return 1.0 - (1.0 - t) ** 3


def _hwnd_of(sdl_window):
    # SDL3 dropped SDL_GetWindowWMInfo in favor of window properties; sdl3w
    # wraps the SDL.window.win32.hwnd lookup. Returns the HWND as an int.
    return S.get_win32_hwnd(sdl_window)


# Win32 constants used by the focus / z-order helpers below.
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TOPMOST    = 0x00000008
# Click-through: the mouse (move/click/wheel) passes straight to the window
# behind. Toggled live so the OSK can become a pure touchpad-typing overlay
# while the sticks/mouse drive the desktop. WS_EX_TRANSPARENT alone only passes
# through to SIBLING windows in our own process; to pass to OTHER apps the
# window must ALSO be WS_EX_LAYERED. SDL3 makes SDL_WINDOW_TRANSPARENT via DWM
# (NOT a layered window — verified at runtime), so we add WS_EX_LAYERED here.
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED     = 0x00080000
_LWA_ALPHA         = 0x00000002
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
    user32.SetForegroundWindow.restype = ctypes.c_bool
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.AllowSetForegroundWindow.restype = ctypes.c_bool
    user32.AllowSetForegroundWindow.argtypes = [ctypes.c_ulong]
    user32.IsWindow.restype = ctypes.c_bool
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
    user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint]
    return user32


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _kernel32():
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


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
    """Ask SDL to keep the window above others without stealing focus. Used on
    Linux/X11 to request _NET_WM_STATE_ABOVE; on Windows the z-order is owned
    by the Win32 WS_EX_TOPMOST path instead."""
    try:
        S.SDL_SetWindowAlwaysOnTop(sdl_window, True)
    except Exception:
        pass


def _reassert_topmost(sdl_window):
    """Re-assert the OSK window's topmost z-order. The open animation's settle
    phase issues a burst of SDL_SetWindowPosition calls (~30 in well under a
    second); each is a chance for Windows to silently re-resolve z-order
    against another always-on-top window (e.g. a fullscreen game), which can
    leave the OSK visible but no longer the window that receives mouse input.
    Called once the animation finishes settling."""
    if _IS_WINDOWS:
        hwnd = _hwnd_of(sdl_window)
        if hwnd is not None:
            _force_topmost(hwnd)
    else:
        _set_always_on_top_portable(sdl_window)


def _make_window_non_activating(sdl_window):
    """Apply WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST to the SDL
    window's HWND while it's still hidden. NOACTIVATE keeps the user's
    target app (e.g. a browser search field) focused; TOPMOST registers
    the window as always-on-top so SetWindowPos can actually elevate it.

    On non-Windows the heavy lifting is done by the SDL hint set at window
    creation (SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN=0) plus always-on-top;
    this function is a no-op there."""
    if not _IS_WINDOWS:
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        print("warning: win32 HWND lookup failed; window will steal focus")
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


def _set_click_through(sdl_window, enabled):
    """Add/remove WS_EX_TRANSPARENT on the OSK HWND. When enabled the mouse
    ignores the OSK entirely — moves, clicks and the scroll wheel fall through
    to the app behind it — so the right-stick mouse and left-stick scroll drive
    the desktop while the touchpads still type on the keyboard. No-op off Windows
    (X11 click-through is a separate mechanism — see the Linux tree's TODO)."""
    if not _IS_WINDOWS:
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        return
    user32 = _user32()
    current = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    bits = _WS_EX_LAYERED | _WS_EX_TRANSPARENT
    new = (current | bits) if enabled else (current & ~bits)
    if new == current:
        return
    user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, new)
    if enabled:
        # A freshly-layered window has undefined alpha until we set it; force it
        # fully opaque so the keyboard stays visible (uniform alpha overrides the
        # per-pixel transparent-skin see-through while click-through is active).
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, _LWA_ALPHA)
    # Flush the latent ex-style change so hit-testing picks it up immediately.
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
                        | _SWP_FRAMECHANGED)


def _show_window_noactivate(sdl_window):
    """Bring the OSK on screen without stealing focus and force it into the
    topmost z-order. WS_EX_NOACTIVATE on the HWND means even Win32 calls
    that would normally activate the window (SetForegroundWindow,
    SetWindowPos without SWP_NOACTIVATE) leave focus alone.

    On X11 we fall back to SDL's portable show + always-on-top; combined
    with SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN=0 (set in Screen.__init__)
    most compositors will skip focus on map."""
    if not _IS_WINDOWS:
        S.SDL_ShowWindow(sdl_window)
        _set_always_on_top_portable(sdl_window)
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        S.SDL_ShowWindow(sdl_window)
        return
    user32 = _user32()
    user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
    _force_topmost(hwnd)


# AllowSetForegroundWindow(ASFW_ANY): drop the foreground lock so our restore
# below is honored even though we aren't the foreground process.
_ASFW_ANY = ctypes.c_ulong(-1).value


def _restore_foreground(hwnd):
    """Re-focus the window the user was typing in before the OSK opened.

    A controller-open fires the firmware lizard's mouse-click (X's desktop
    action) which can land off the target text field and steal its focus. The
    OSK window is WS_EX_NOACTIVATE so it never takes focus itself, so
    re-activating the saved window restores the caret while the OSK stays on
    top. No-op off Windows, when nothing was saved, or if the window is gone."""
    if not _IS_WINDOWS or not hwnd:
        return
    user32 = _user32()
    if not user32.IsWindow(ctypes.c_void_p(hwnd)):
        return
    # SetForegroundWindow from a background process is refused unless we relax
    # the foreground lock and attach our input queue to the current foreground
    # thread's (the same elevation trick as _force_topmost).
    try:
        user32.AllowSetForegroundWindow(_ASFW_ANY)
    except Exception:
        pass
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = False
    if fg_thread and fg_thread != cur_thread:
        attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
    try:
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(cur_thread, fg_thread, False)


# Index into the 6-position window rotation, advanced by Shift+Move.
# 0 starts at down-mid (the default open location).
_position_index = [0]
# Index of the up-right spot in _apply_window_position's seq, used to force
# the OSK there on open (without disturbing _position_index) when the Windows
# Start menu is covering its usual spot.
_POS_UP_RIGHT = 4

# Processes that host the Start menu and its search-results view, across
# different Windows builds. On 24H2+ both the Start launcher and its search
# view run as SearchApp.exe; older builds use StartMenuExperienceHost/
# SearchHost. While typing into Start's search box (e.g. via the OSK), focus
# hands between these without the menu visually closing, so
# _is_start_menu_open() must treat any of them as "Start is open".
_START_MENU_PROCESSES = {
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe"}


def _is_start_menu_open():
    """True if the Windows Start menu (or its search-results view) is
    currently open and focused. Start opens from the bottom-center and grows
    upward, covering the keyboard at its usual remembered spot — so the open
    call sites force _POS_UP_RIGHT instead while this is true."""
    if not _IS_WINDOWS:
        return False
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        return buf.value.rsplit("\\", 1)[-1].lower() in _START_MENU_PROCESSES
    finally:
        kernel32.CloseHandle(handle)


def _reposition_window(sdl_window):
    """Place the OSK at the bottom-center of the primary display's usable area.
    Called when reusing a cached Screen so a changed display layout is respected."""
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
        win_x = bounds.x + max(0, (bounds.w - screen.width) // 2)
        win_y = bounds.y + max(0, bounds.h - screen.height)
        S.SDL_SetWindowPosition(sdl_window, win_x, win_y)


def _apply_window_position(sdl_window, index=None):
    """Move the OSK to the spot for `index` (default: the CURRENT
    _position_index, 0 = down-mid). Used to restore the remembered position
    when the OSK (re)opens, after advancing the index by
    _cycle_window_position, or to force _POS_UP_RIGHT on open when the Start
    menu is covering the usual spot (see _is_start_menu_open)."""
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if not (disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds))):
        return None
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
    x, y = seq[_position_index[0] if index is None else index]
    S.SDL_SetWindowPosition(sdl_window, x, y)
    # The resting (x, y) — the open animation settles the window down INTO this.
    return (x, y)


def _cycle_window_position(sdl_window):
    _position_index[0] = (_position_index[0] + 1) % 6
    _apply_window_position(sdl_window)


def _begin_open_anim(scr, virtual_kb, controller_state, rest):
    """Prime the OSK open animation: pre-render the invisible first frame (so the
    just-shown window never flashes its background), then raise the window by the
    settle distance so the animation can drop it back into place. Returns the
    monotonic start time, or None if the animation can't run (no display bounds
    or no GPU render target) — the caller then just shows the keyboard normally.

    `rest` is the resting (x, y) from _apply_window_position."""
    if rest is None:
        return None
    # Force the window into per-pixel-alpha (non-click-through) mode for the
    # animation: the fade/reveal composite shows the desktop through the
    # transparent/cut pixels, which a uniform layered-window alpha (set by
    # click-through) would override. The real click-through state is re-applied
    # the moment the animation ends (the caller resets clickthrough_on).
    _set_click_through(scr.window, False)
    pointers = controller_state.get_pointers()
    # fade=0 + full cut => a fully transparent frame: the window is invisible the
    # instant it's shown, then the loop fades/reveals it in.
    if not scr.render_open_anim(virtual_kb, pointers, 0.0, _OPEN_ANIM_CUT_PX):
        return None
    rx, ry = rest
    S.SDL_SetWindowPosition(scr.window, rx, ry - _OPEN_ANIM_DROP_PX)
    return time.monotonic()


def load_kb_config():
    kb_config = vkb.VirtualKeyboardConfig()
    kb_layout_file = config.YamlFile("keyboard-layout.yaml")
    kb_layout_file.read()
    kb_layout_file.add_to_config("keys", kb_config)
    return kb_config


def main(cached_screen=None):
    # NOTE: _position_index is NOT reset here — the Move-key window position is
    # remembered across OSK opens within a session and only resets to down-mid
    # on a program restart (when this module is freshly imported). It's restored
    # below, after the window is shown.

    controller_state = controller.ControllerState()
    controller_state.set_pointers(
            vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(1/4, 1/2)),
            vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(3/4, 1/2))
    )

    virtual_kb = load_kb_config().construct()
    # Publish per-row key counts so the controller thread can clamp DPAD
    # navigation against the actual layout.
    state.set_grid_dims([len(r) for r in virtual_kb.keys])

    # SDL3: bring up video+events (rendering), TTF (key labels), and GAMEPAD
    # (the SDL input backend for non-Steam pads) BEFORE starting the input
    # thread, so its Sdl3GamepadSource can safely call into SDL. We use
    # SDL_InitSubSystem / SDL_QuitSubSystem (not SDL_Init/SDL_Quit) because the
    # tray owns a persistent SDL_INIT_GAMEPAD for its own SDL pad watcher — a
    # full SDL_Quit on OSK close would tear that down. Refcounted, so this
    # balances the SDL_QuitSubSystem at teardown. The custom Steam Controller
    # HID driver runs on its own thread and is independent of SDL.
    # Keep gamepad input flowing while the OSK window is up. Our window is
    # NOACTIVATE / never focused, and SDL by default DROPS joystick/gamepad
    # events whenever no SDL window has input focus — which froze every SDL pad
    # (Switch Pro/Xbox/...) to all-zero buttons the instant the OSK opened.
    S.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
    # Keep SDL's HIDAPI driver off the Steam Controller — we own the SC via our
    # steamcontroller HID backend, and SDL3 grabbing it (shared) blocks our
    # exclusive open (see tray.py / block_sc_hid). Only matters when we own SDL
    # (standalone OSK with no tray-cached screen); under the tray, the tray sets
    # this before its own SDL_Init.
    S.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_STEAM", b"0")
    _owns_sdl = cached_screen is None
    if _owns_sdl:
        if not S.SDL_InitSubSystem(S.SDL_INIT_VIDEO | S.SDL_INIT_EVENTS | S.SDL_INIT_GAMEPAD):
            raise RuntimeError("SDL_InitSubSystem failed: " + S.get_error())
        if not S.TTF_Init():
            raise RuntimeError("TTF_Init failed: " + S.get_error())

    sc_thread = Thread(target=controller.input_thread, args=(controller_state,), daemon=True)
    sc_thread.start()

    if cached_screen is not None:
        scr = cached_screen
        # Re-check the skin in case it changed while the OSK was closed.
        scr.maybe_reload_skin()
    else:
        scr = screen.Screen()
        _make_window_non_activating(scr.window)
    # Restore the remembered Move-key position (persists across opens within a
    # session, resets on program restart). At index 0 this is the default
    # down-mid spot; also re-applies after a display-layout change. Done BEFORE
    # showing so the open animation knows its resting target and the window
    # never flashes at the wrong spot. If the Start menu is open, force
    # up-right instead (without touching _position_index) so its
    # search-results panel doesn't cover the keyboard.
    start_menu_open = _is_start_menu_open()
    open_anim_rest = _apply_window_position(
        scr.window, _POS_UP_RIGHT if start_menu_open else None)
    # Prime the open animation (pre-renders an invisible frame + raises the
    # window) before showing it, so the keyboard fades/rises in instead of
    # popping. open_anim_start is None if it can't run → plain instant show.
    open_anim_start = _begin_open_anim(scr, virtual_kb, controller_state, open_anim_rest)
    _show_window_noactivate(scr.window)
    # The OSK window is up and NOACTIVATE, but a controller-open's firmware
    # mouse-click may have stolen focus from the user's text field on the way
    # in — restore it so typed keys land where they were typing.
    _restore_foreground(state.get_focus_restore_target())
    was_visible = True
    # Last key under each touchpad pointer, for haptic "switched key" ticks.
    last_hover = [None, None]
    # Next time the held left mouse button re-fires its key (inf = not armed).
    # Holding the button over a repeatable key (Backspace / arrows) rubs out /
    # steps on the same cadence as the controller.
    mouse_repeat_at = float("inf")
    # The mouse highlight only follows a REAL move: the first motion event after
    # the OSK opens is usually SDL reporting the cursor's position because the
    # window appeared under it — we record that as this anchor WITHOUT jumping
    # the highlight there. None = re-prime on the next open/show.
    mouse_anchor = None
    # Whether the OSK is currently click-through (mouse falls through to the app
    # behind). Mirrors the "Sticks Control Keyboard" setting inverted: when the
    # sticks/mouse should drive the DESKTOP (setting off), the OSK goes
    # click-through. None = unknown, force a (re)apply on the next visible frame.
    clickthrough_on = None
    # Next time the (expensive) render + trackpad-hover-haptic pass runs. 0 =
    # render immediately on the first iteration. The loop itself runs faster so
    # input is processed with low latency; only rendering is throttled.
    next_render = 0.0
    # Next time to re-poll _is_start_menu_open() while visible (see
    # _START_MENU_POLL_INTERVAL). 0 = check on the first iteration too, though
    # start_menu_open above already matches reality so that check is a no-op.
    next_start_check = 0.0

    # One reusable event struct polled each frame (SDL3 SDL_PollEvent fills it).
    ev = S.SDL_Event()

    while not state.should_close():
        now = time.monotonic()
        while S.SDL_PollEvent(ctypes.byref(ev)):
            et = ev.type
            if et == S.SDL_EVENT_QUIT:
                state.close()
                break
            if et == S.SDL_EVENT_WINDOW_RESIZED:
                screen.width = ev.window.data1
                screen.height = ev.window.data2
            # Mouse control: hovering highlights the key under the pointer,
            # left-click presses it (the Shift key toggles latched Shift), and
            # the standard side buttons handle the keys you can't otherwise
            # reach mouse-only. Right-click = Shift, X1 (back) = Backspace,
            # X2 (forward) = Space. The OSK window is WS_EX_NOACTIVATE, so
            # clicking it never steals focus from the app being typed into.
            # (SDL3 reports mouse x/y as floats; find_key* handles that.)
            if et == S.SDL_EVENT_MOUSE_MOTION and state.is_visible():
                # Only move the highlight on a genuine mouse move (position
                # differs from the anchor). The first event after open just
                # records the anchor, so the OSK doesn't snap to the mouse.
                mpos = (ev.motion.x, ev.motion.y)
                if open_anim_start is not None:
                    # The open animation's settle phase repositions the window
                    # every frame, which makes a STATIONARY mouse's
                    # window-relative coords drift too — track the anchor but
                    # don't move the highlight, or it visibly chases the
                    # cursor while the keyboard slides into place.
                    pass
                elif mouse_anchor is not None and mpos != mouse_anchor:
                    rc = virtual_kb.find_key_rc(*mpos)
                    if rc is not None:
                        state.set_cursor(*rc)
                mouse_anchor = mpos
                # If the left button isn't held anymore (e.g. it was released
                # off-window), drop any lingering press highlight and stop the
                # hold-to-repeat.
                if not (int(ev.motion.state) & S.SDL_BUTTON_LMASK):
                    state.set_mouse_press_cell(None)
                    mouse_repeat_at = float("inf")
            if et == S.SDL_EVENT_MOUSE_BUTTON_DOWN and state.is_visible():
                btn = ev.button.button
                mouse_anchor = (ev.button.x, ev.button.y)  # keep anchor in sync
                if btn == S.SDL_BUTTON_LEFT:
                    rc = virtual_kb.find_key_rc(ev.button.x, ev.button.y)
                    if rc is not None:
                        state.set_cursor(*rc)
                        state.set_mouse_press_cell(rc)  # flash the key blue
                        state.queue_key_press(*rc)
                        # Arm hold-to-repeat; the cur_visible block below only
                        # actually repeats it over a repeatable key.
                        mouse_repeat_at = now + vkb.KEY_REPEAT_DELAY
                elif btn == S.SDL_BUTTON_RIGHT:
                    vkb.toggle_shift()
                elif btn == S.SDL_BUTTON_X1:
                    vkb.tap_keycode(sui.Keys.KEY_BACKSPACE)
                elif btn == S.SDL_BUTTON_X2:
                    vkb.tap_keycode(sui.Keys.KEY_SPACE)
            if (et == S.SDL_EVENT_MOUSE_BUTTON_UP
                    and ev.button.button == S.SDL_BUTTON_LEFT):
                state.set_mouse_press_cell(None)
                mouse_repeat_at = float("inf")

        # Poll any SDL pad (Xbox/DualSense/Switch Pro) HERE — on the thread that
        # just drained the SDL event queue. SDL only refreshes gamepad state on
        # its event-pump thread, so the tray thread reads all-zero while this
        # loop runs; we read the fresh state and publish it for the input
        # thread's SharedSdlFrameSource (which feeds handle_input).
        _sdl_src = state.get_sdl_source()
        if _sdl_src is not None:
            try:
                state.set_sdl_frame(_sdl_src.poll())
            except Exception:
                pass

        cur_visible = state.is_visible()
        if cur_visible != was_visible:
            if cur_visible:
                # Re-prime the open animation (position + invisible first frame +
                # raise) BEFORE showing, so a re-open fades/rises in like the first.
                # Same Start-menu up-right override as the initial open.
                start_menu_open = _is_start_menu_open()
                open_anim_rest = _apply_window_position(
                    scr.window, _POS_UP_RIGHT if start_menu_open else None)
                open_anim_start = _begin_open_anim(
                    scr, virtual_kb, controller_state, open_anim_rest)
                _show_window_noactivate(scr.window)
                # Re-prime so the open's spurious motion doesn't jump the cursor.
                mouse_anchor = None
                # Showing resets the HWND ex-style baseline; re-apply below.
                clickthrough_on = None
            else:
                # Don't leave a mouse-latched Shift stuck down on the OS.
                vkb.release_shift()
                S.SDL_HideWindow(scr.window)
                open_anim_start = None  # abort any in-flight open animation
            was_visible = cur_visible

        if cur_visible:
            # "Keyboard Sticks/Mouse controls" OFF (for the active controller) →
            # the sticks/mouse drive the desktop: make the OSK click-through so
            # the mouse falls through to the app behind. Re-checked every frame so
            # a live tray toggle (or a controller switch) takes effect at once.
            # DEFERRED while the open animation plays: click-through forces a
            # uniform layered-window alpha that overrides the per-pixel alpha the
            # fade/reveal composite relies on (it's applied the moment the
            # animation ends, clickthrough_on still None).
            want_clickthrough = not state.is_kbd_stick_nav_enabled_for(
                state.get_active_controller())
            if open_anim_start is None and want_clickthrough != clickthrough_on:
                _set_click_through(scr.window, want_clickthrough)
                clickthrough_on = want_clickthrough
            # Apply a tray-side skin change live (no-op unless it changed).
            scr.maybe_reload_skin()
            virtual_kb.update_dimensions()
            # --- Input-driven work runs EVERY loop iteration (NOT gated by the
            # render rate). The SDL pad was just polled/published above, so
            # draining the resulting cursor steps and key presses right away —
            # instead of once per rendered frame — gives SDL pads (Switch Pro/
            # Xbox/...) the same low navigation latency the Steam Controller has
            # (its frames go straight to the input thread, skipping this publish).
            # DPAD: step the cursor using the actual layout pixel positions.
            for direction, haptic in state.drain_dpad_queue():
                vkb.step_cursor(virtual_kb, direction, haptic=haptic)
            vkb.process_click_queue(virtual_kb, controller_state.click_queue)
            # Mouse left-button hold-to-repeat: while held over a repeatable
            # key (Backspace / arrows), re-queue it on the shared cadence.
            # Queued before the drain so it dispatches this same frame.
            press_cell = state.get_mouse_press_cell()
            if press_cell is not None and now >= mouse_repeat_at:
                pr, pc = press_cell
                if (0 <= pr < len(virtual_kb.keys) and 0 <= pc < len(virtual_kb.keys[pr])
                        and vkb.is_repeatable(virtual_kb.keys[pr][pc])):
                    state.queue_key_press(pr, pc, repeat=True)
                    mouse_repeat_at = now + vkb.KEY_REPEAT_INTERVAL
                else:
                    mouse_repeat_at = float("inf")
            # Key presses: fire the callback of the queued key. A repeat hit
            # (something held) only fires over a repeatable key (Backspace /
            # arrows), so holding rubs out / steps without machine-gunning
            # ordinary keys.
            for row, col, is_repeat in state.drain_key_press_queue():
                if 0 <= row < len(virtual_kb.keys) and 0 <= col < len(virtual_kb.keys[row]):
                    key = virtual_kb.keys[row][col]
                    if is_repeat and not vkb.is_repeatable(key):
                        continue
                    vkb.dispatch_key(virtual_kb, key)
            if state.take_position_cycle_request():
                _cycle_window_position(scr.window)
                # Moving the window slides it under a stationary mouse, which
                # fires a spurious motion (new window-relative coords) — re-prime
                # so the highlight doesn't jump to the mouse after a Move.
                mouse_anchor = None
            req = state.take_window_position_request()
            if req is not None:
                _position_index[0] = req % 6
                _apply_window_position(scr.window)
                mouse_anchor = None
            # The open-time check above only forces _POS_UP_RIGHT at the moment
            # the OSK becomes visible — if the Start menu opens or closes while
            # the OSK is ALREADY showing, live-reposition in response (instant
            # snap, like the Move key above) instead of leaving it stuck.
            if now >= next_start_check:
                next_start_check = now + _START_MENU_POLL_INTERVAL
                now_start_open = _is_start_menu_open()
                if now_start_open != start_menu_open:
                    start_menu_open = now_start_open
                    _apply_window_position(
                        scr.window, _POS_UP_RIGHT if start_menu_open else None)
                    mouse_anchor = None
            # Render + trackpad-hover haptic are throttled to the display rate;
            # the cheap input work above already ran this iteration.
            if now >= next_render:
                next_render = now + _RENDER_INTERVAL
                pointers = controller_state.get_pointers()
                if open_anim_start is not None:
                    # --- OSK OPEN animation frame ---
                    p = (now - open_anim_start) / _OPEN_ANIM_SECS
                    if p >= 1.0:
                        # Done: settle exactly at rest, then resume normal render.
                        if open_anim_rest is not None:
                            S.SDL_SetWindowPosition(
                                scr.window, open_anim_rest[0], open_anim_rest[1])
                        # The settle phase's burst of SDL_SetWindowPosition calls
                        # can cost the OSK its topmost z-order (see
                        # _reassert_topmost) — left unfixed, the window stays
                        # visible but stops receiving mouse input. Re-prime the
                        # anchor too, so any spurious motion the repositioning
                        # caused doesn't leave the highlight stuck mid-jump.
                        _reassert_topmost(scr.window)
                        mouse_anchor = None
                        open_anim_start = None
                        scr.render(virtual_kb, pointers)
                    else:
                        # Opacity eases 0→1 over the first FADE_FRAC; the bottom
                        # cut reveals over the first REVEAL_FRAC; the window settles
                        # DROP_PX downward from MOVE_START_FRAC to the end.
                        fade = _ease_out_cubic(min(1.0, p / _OPEN_ANIM_FADE_FRAC))
                        reveal_t = _ease_out_cubic(min(1.0, p / _OPEN_ANIM_REVEAL_FRAC))
                        cut = _OPEN_ANIM_CUT_PX * (1.0 - reveal_t)
                        # The window starts raised (set once in _begin_open_anim);
                        # only the settle phase moves it, so we touch the window
                        # position only while it's actually changing.
                        if p > _OPEN_ANIM_MOVE_START_FRAC and open_anim_rest is not None:
                            move_t = _ease_out_cubic(
                                (p - _OPEN_ANIM_MOVE_START_FRAC)
                                / (1.0 - _OPEN_ANIM_MOVE_START_FRAC))
                            rx, ry = open_anim_rest
                            S.SDL_SetWindowPosition(
                                scr.window, rx,
                                int(round(ry - _OPEN_ANIM_DROP_PX * (1.0 - move_t))))
                        if not scr.render_open_anim(virtual_kb, pointers, fade, cut):
                            open_anim_start = None  # target gone → stop animating
                else:
                    # Haptic tick when a touchpad pointer moves onto a different key
                    # (touchpad mode only — pointer is INACTIVE when not touching).
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
        # Pace the loop fast (low input latency) without busy-spinning; rendering
        # is throttled separately above.
        time.sleep(_LOOP_SLEEP)

    # Release a latched Shift before tearing down, so closing the keyboard
    # never leaves the OS with KEY_LEFTSHIFT held.
    vkb.release_shift()
    # Give the controller thread up to 1 second to run its cleanup (sends
    # the enable-lizard packet before closing the HID handle). Without this
    # wait the daemon thread is killed before it can re-enable lizard mode.
    sc_thread.join(timeout=1.0)
    if _owns_sdl:
        # First-session path: we own the SDL subsystems, so tear them down.
        # GAMEPAD/EVENTS stay up for the tray's persistent SDL pad watcher;
        # VIDEO's refcount hits zero here so the window is released.
        try:
            S.SDL_DestroyRenderer(scr.renderer)
            S.SDL_DestroyWindow(scr.window)
        except Exception:
            pass
        S.TTF_Quit()
        S.SDL_QuitSubSystem(S.SDL_INIT_VIDEO | S.SDL_INIT_EVENTS | S.SDL_INIT_GAMEPAD)
    else:
        # Cached-screen path: just hide the window — the tray will reuse it
        # on the next open. SDL subsystems remain up (tray owns them).
        try:
            S.SDL_HideWindow(scr.window)
        except Exception:
            pass


if __name__ == '__main__':
    main()
