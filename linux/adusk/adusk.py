#!/usr/env/python3
# -*- coding: utf-8 -*-

import ctypes
import os
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
_IS_LINUX = sys.platform.startswith("linux")

if _IS_LINUX:
    # The OSK's no-keyboard-focus relies on X11 WM hints (set via libX11 below),
    # so pin SDL to the X11 backend — natively or via XWayland — rather than
    # letting SDL3 default to Wayland, where those hints don't apply. setdefault
    # so a user can still override with SDL_VIDEODRIVER in the environment.
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")


# ---------------------------------------------------------------------------
# X11/XWayland: deny keyboard focus
# ---------------------------------------------------------------------------
#
# SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN is a no-op on SDL's X11 backend, so by
# default the OSK window steals focus the moment it's mapped. KWin (Plasma 6)
# and most other WMs accept two parallel signals to keep a window out of the
# focus rotation:
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
    """Pull the (X11 Display*, Window) pair out of the SDL3 window properties,
    or (None, None) when SDL isn't on the X11 backend / the call failed."""
    display = S.get_x11_display(sdl_window)
    window = S.get_x11_window(sdl_window)
    if not display or not window:
        return None, None
    return display, window


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


class _XRectangle(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
    ]


# From <X11/extensions/shape.h>: ShapeInput is the shape kind that controls
# which pixels of a window accept pointer input (independent of the bounding
# shape used for drawing). ShapeSet replaces it outright.
_SHAPE_INPUT = 2
_SHAPE_SET = 0

_libxext_cache = None


def _libxext():
    global _libxext_cache
    if _libxext_cache is not None:
        return _libxext_cache
    try:
        lib = ctypes.cdll.LoadLibrary("libXext.so.6")
    except OSError:
        return None
    lib.XShapeCombineRectangles.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(_XRectangle), ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    lib.XShapeCombineRectangles.restype = None
    lib.XShapeCombineMask.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_ulong, ctypes.c_int,
    ]
    lib.XShapeCombineMask.restype = None
    _libxext_cache = lib
    return lib


def _set_x11_click_through(sdl_window, enabled):
    """Set/clear the OSK's X11 INPUT shape via the Shape extension.

    With an EMPTY input shape (enabled=True) the window still draws normally
    but the X server delivers no pointer events to it at all — motion, clicks
    and the scroll wheel land on whatever window is behind it, exactly like
    WS_EX_TRANSPARENT on Windows. ShapeCombineMask with a NULL pixmap
    (enabled=False) resets the input shape back to the window's default (the
    whole window), restoring normal OSK mouse interaction."""
    display, window = _x11_handles(sdl_window)
    if not display or not window:
        return
    xext = _libxext()
    if xext is None:
        return
    try:
        if enabled:
            xext.XShapeCombineRectangles(
                display, window, _SHAPE_INPUT, 0, 0, None, 0, _SHAPE_SET, 0)
        else:
            xext.XShapeCombineMask(
                display, window, _SHAPE_INPUT, 0, 0, 0, _SHAPE_SET)
        x11 = _libx11()
        if x11 is not None:
            x11.XFlush(display)
    except Exception as e:
        print(f"warning: X11 click-through shape failed: {e!r}")


def _hwnd_of(sdl_window):
    # SDL3 dropped SDL_GetWindowWMInfo for window properties; sdl3w wraps it.
    # On Linux this returns None (no win32 prop) — the Win32 path below is dead.
    return S.get_win32_hwnd(sdl_window)


# Win32 constants used by the focus / z-order helpers below (dead on Linux,
# kept so this file mirrors the Windows tree 1:1).
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
    """Make `hwnd` topmost without stealing focus (Windows only)."""
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
    Linux/X11 to request _NET_WM_STATE_ABOVE."""
    try:
        S.SDL_SetWindowAlwaysOnTop(sdl_window, True)
    except Exception:
        pass


def _reassert_topmost(sdl_window):
    """Re-assert the OSK window's topmost z-order. The open animation's settle
    phase issues a burst of SDL_SetWindowPosition calls (~30 in well under a
    second); each is a chance for the WM to silently re-resolve z-order
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
    """On Windows: apply WS_EX_NOACTIVATE|TOOLWINDOW|TOPMOST to the HWND while
    hidden. On Linux the SDL "no activation" hint is a no-op for X11, so we
    patch WM_HINTS.input=False + _NET_WM_WINDOW_TYPE_DOCK via libX11 directly.
    The SDL window is still SDL_WINDOW_HIDDEN at this point — the WM only reads
    these atoms when the window is mapped."""
    if not _IS_WINDOWS:
        _make_window_no_focus_x11(sdl_window)
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
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
                        | _SWP_FRAMECHANGED)


def _reposition_bottom_center(sdl_window):
    """Re-assert the OSK at the bottom-center of the primary display's usable
    area AFTER it's mapped. Screen.__init__ already positions it while hidden,
    but some compositors (notably under Wayland/XWayland) ignore a position set
    on a still-hidden window and map it centered — setting it again once shown
    fixes that on X11."""
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
        win_x = bounds.x + max(0, (bounds.w - screen.width) // 2)
        win_y = bounds.y + max(0, bounds.h - screen.height)
        S.SDL_SetWindowPosition(sdl_window, win_x, win_y)


def _set_click_through(sdl_window, enabled):
    """When enabled the mouse ignores the OSK entirely — moves, clicks and the
    scroll wheel fall through to the app behind it — so the right-stick mouse
    and left-stick scroll drive the desktop while the touchpads still type on
    the keyboard.

    On Windows this adds/removes WS_EX_TRANSPARENT (+LAYERED) on the OSK HWND.
    On X11 it's done via _set_x11_click_through (an empty Shape-extension INPUT
    region), since there's no HWND."""
    if not _IS_WINDOWS:
        _set_x11_click_through(sdl_window, enabled)
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


def _show_window_noactivate(sdl_window, pos=None):
    """Bring the OSK on screen without stealing focus and force it topmost.

    On X11 we use SDL's portable show + always-on-top; combined with the
    WM_HINTS.input=False + _NET_WM_WINDOW_TYPE_DOCK atoms set above, most
    compositors skip focus on map.

    `pos`, if given, is the (x, y) to re-assert once mapped — used by the open
    animation to keep the window at its RAISED starting position (a
    hidden-window position can otherwise be ignored by the compositor and
    re-mapped centered, or reset back to rest here, which would skip the
    animation's downward settle). If None, re-assert the remembered Move-key
    rest position instead (at index 0 this is the default bottom-center
    spot)."""
    if not _IS_WINDOWS:
        S.SDL_ShowWindow(sdl_window)
        _set_always_on_top_portable(sdl_window)
        if pos is not None:
            S.SDL_SetWindowPosition(sdl_window, pos[0], pos[1])
        else:
            _apply_window_position(sdl_window)
        # Block until the WM has actually mapped/positioned the window before
        # the open animation starts presenting frames. Without this, the first
        # animation frame(s) can render while the window is still at its old
        # (or default-centered) position/visibility under XWayland, producing
        # a one-frame flash/jump at the start of the animation.
        S.SDL_SyncWindow(sdl_window)
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        S.SDL_ShowWindow(sdl_window)
        return
    user32 = _user32()
    user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
    _force_topmost(hwnd)


_ASFW_ANY = ctypes.c_ulong(-1).value


def _restore_foreground(hwnd):
    """Re-focus the window the user was typing in before the OSK opened.

    On Windows a controller-open fires the firmware lizard's mouse-click, which
    can land off the target field and steal focus; the saved window is then
    re-activated (the OSK is NOACTIVATE, so it never takes focus itself).

    TODO(linux): port the capture (tray) + an X11 restore here via
    _NET_ACTIVE_WINDOW / XSetInputFocus. For now this is a no-op off Windows,
    and the saved target is always None on Linux, so nothing changes."""
    if not _IS_WINDOWS or not hwnd:
        return
    user32 = _user32()
    if not user32.IsWindow(ctypes.c_void_p(hwnd)):
        return
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


# The SDL VIDEO subsystem is inited once per process and kept up across OSK
# opens (see main()). Re-initing it costs ~400 ms (it rebuilds the XWayland
# connection + video driver), which dominated open latency; keeping it inited
# while the OSK is closed costs no CPU (no window, no render loop) — just an
# idle X connection. False until the first open inits it.
_video_inited = False

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


# How often to re-check _is_start_menu_open() while the OSK is already visible,
# to live-reposition if the Start menu opens/closes underneath it. Cheap
# (GetForegroundWindow + a process-name lookup), but not free, so this is
# throttled rather than checked every frame — Start opening/closing is a
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


def _apply_window_position(sdl_window, index=None):
    """Move the OSK to the spot for `index` (default: the CURRENT
    _position_index, 0 = down-mid). Used to restore the remembered position
    when the OSK (re)opens — also re-asserting it after the window maps, which
    some Wayland/XWayland compositors require — after advancing the index by
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
    if _IS_WINDOWS:
        S.SDL_SetWindowPosition(scr.window, rx, ry - _OPEN_ANIM_DROP_PX)
    else:
        # Map the window OFF-SCREEN (below the display). On X11/XWayland the
        # first frame KWin composites for a newly-mapped window is an opaque
        # black backbuffer regardless of what we've already Present()ed into
        # it -- that's the black flash before the OSK fades in. Our window is
        # _NET_WM_WINDOW_TYPE_DOCK, which KWin positions exactly where the app
        # asks (panels self-place) instead of clamping it on-screen, so we let
        # that black map-frame happen off-screen; _finish_open_anim_map then
        # moves the window (now holding the correct transparent buffer) onto
        # its raised start position. This replaces an earlier SetWindowOpacity
        # hide whose 0->1 reveal triggered KWin's compositor Fade effect on top
        # of our own animation -- a visibly slower, less responsive open.
        S.SDL_SetWindowPosition(scr.window, rx, _open_anim_offscreen_y(ry))
    return time.monotonic()


def _open_anim_offscreen_y(ry):
    """Window-top Y that parks the OSK fully BELOW the display, so the black
    map-frame (see _begin_open_anim) stays off-screen no matter WHERE the OSK
    will rest. Anchored to the display's bottom edge — NOT to `ry` — because the
    Move key can put the rest at the TOP of the screen (ry ≈ 0), where the old
    `ry + height` landed mid-screen and the black frame flashed there. Pushing a
    full window-height past the usable bottom also clears any taskbar/panel gap
    between the usable area and the physical screen edge. `ry` is only the
    fallback when the display bounds are unavailable."""
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
        return bounds.y + bounds.h + screen.height + 50
    return ry + screen.height + 50


def _open_anim_show_pos(open_anim_start, open_anim_rest):
    """The (x, y) _show_window_noactivate should re-assert once the window is
    mapped. If an open animation just started, that's the position
    _begin_open_anim moved the (still-hidden) window to — re-asserting `rest`
    here instead would snap the window straight to its final spot and skip the
    animation. On Windows that's the RAISED start position; on X11 it's the
    OFF-SCREEN map position (the black map-frame stays off-screen, then
    _finish_open_anim_map moves it on-screen). Otherwise it's just `rest`."""
    if open_anim_start is None or open_anim_rest is None:
        return open_anim_rest
    rx, ry = open_anim_rest
    if _IS_WINDOWS:
        return (rx, ry - _OPEN_ANIM_DROP_PX)
    return (rx, _open_anim_offscreen_y(ry))


def _finish_open_anim_map(scr, virtual_kb, controller_state,
                          open_anim_start, open_anim_rest):
    """X11 only: after the window was shown+synced off-screen (so its black
    map-frame stayed off-screen), re-present the transparent open frame and
    move the window onto its raised on-screen start position to begin the
    animation. No-op on Windows or when the animation isn't running."""
    if open_anim_start is None or open_anim_rest is None or _IS_WINDOWS:
        return
    # Re-present the transparent open frame, then prime EVERY swap-chain buffer
    # with a transparent frame while the window is still off-screen — otherwise
    # the compositor's first on-screen composite occasionally lands on an
    # unpresented (black) back-buffer, flashing a one-frame black box (~1 in N
    # opens) over the desktop in transparent mode. See prime_open_anim_buffers.
    scr.render_open_anim(virtual_kb, controller_state.get_pointers(),
                         0.0, _OPEN_ANIM_CUT_PX)
    scr.prime_open_anim_buffers()
    rx, ry = open_anim_rest
    S.SDL_SetWindowPosition(scr.window, rx, ry - _OPEN_ANIM_DROP_PX)


def load_kb_config():
    kb_config = vkb.VirtualKeyboardConfig()
    kb_layout_file = config.YamlFile("keyboard-layout.yaml")
    kb_layout_file.read()
    kb_layout_file.add_to_config("keys", kb_config)
    return kb_config


def main():
    # NOTE: _position_index is NOT reset here — the Move-key window position is
    # remembered across OSK opens within a session and only resets to down-mid
    # on a program restart (when this module is freshly imported). It's restored
    # in _show_window_noactivate when the window maps.

    controller_state = controller.ControllerState()
    controller_state.set_pointers(
            vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(1/4, 1/2)),
            vptr.VirtualPointer(state.InputState.INACTIVE, CoordFraction(3/4, 1/2))
    )

    virtual_kb = load_kb_config().construct()
    # Publish per-row key counts so the controller thread can clamp DPAD
    # navigation against the actual layout.
    state.set_grid_dims([len(r) for r in virtual_kb.keys])

    # Keep gamepad input flowing while the OSK window is up. Our window is
    # no-focus (X11 WM_HINTS.input=False / _NET_WM_WINDOW_TYPE_DOCK), and SDL by
    # default DROPS joystick/gamepad events whenever no SDL window has input
    # focus — which froze every SDL pad (Switch Pro/Xbox/...) to all-zero buttons
    # the instant the OSK opened (the tray's poll reads stale state then), so the
    # OSK got zero controller input. Must be set before the gamepad event pump.
    S.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
    # Keep SDL's HIDAPI driver off the Steam Controller — we own the SC via our
    # steamcontroller HID backend, and SDL3 grabbing it (Triton PIDs) blocks our
    # exclusive open (see tray / block_sc_hid). Under the tray this is already
    # set before its SDL_Init; set it here too for the standalone OSK path.
    S.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_STEAM", b"0")
    # SDL3: bring up video+events+gamepad BEFORE the input thread starts, so its
    # Sdl3GamepadSource can safely poll. InitSubSystem/QuitSubSystem (not
    # SDL_Init/SDL_Quit) so a tray-owned persistent SDL_INIT_GAMEPAD survives
    # the OSK closing; refcounted, balances the QuitSubSystem at teardown.
    #
    # VIDEO is inited ONCE per process and deliberately never quit (see
    # teardown). Re-initing it costs ~400 ms — it rebuilds the XWayland
    # connection + video driver — which dominated the OSK's open latency and
    # made rapid open/close sluggish. Keeping it inited between opens costs no
    # CPU: there's no window and no render loop while the OSK is closed (main()
    # returns and the window/renderer are destroyed), just an idle X connection
    # held open. The keyboard still fully disappears on close.
    global _video_inited
    if not _video_inited:
        if not S.SDL_InitSubSystem(S.SDL_INIT_VIDEO):
            raise RuntimeError("SDL_InitSubSystem(VIDEO) failed: " + S.get_error())
        _video_inited = True
    if not S.SDL_InitSubSystem(S.SDL_INIT_EVENTS | S.SDL_INIT_GAMEPAD):
        raise RuntimeError("SDL_InitSubSystem failed: " + S.get_error())
    if not S.TTF_Init():
        raise RuntimeError("TTF_Init failed: " + S.get_error())

    sc_thread = Thread(target=controller.input_thread, args=(controller_state,), daemon=True)
    sc_thread.start()

    scr = screen.Screen()
    # Tag the still-hidden window as non-focusable (X11 WM hints / Win32
    # NOACTIVATE), then bring it on screen without stealing focus.
    _make_window_non_activating(scr.window)
    # Place the window at its resting spot, then prime the open animation
    # (pre-renders the invisible first frame + raises the window) BEFORE showing,
    # so the keyboard fades/rises in instead of popping. open_anim_start is None
    # if it can't run → plain instant show. If the Start menu is open, force
    # up-right instead (without touching _position_index) so its
    # search-results panel doesn't cover the keyboard.
    start_menu_open = _is_start_menu_open()
    open_anim_rest = _apply_window_position(
        scr.window, _POS_UP_RIGHT if start_menu_open else None)
    open_anim_start = _begin_open_anim(scr, virtual_kb, controller_state, open_anim_rest)
    _show_window_noactivate(scr.window, _open_anim_show_pos(open_anim_start, open_anim_rest))
    _finish_open_anim_map(scr, virtual_kb, controller_state,
                          open_anim_start, open_anim_rest)
    if open_anim_start is not None:
        # Start the animation clock NOW that the window is actually on-screen.
        # The show + SDL_SyncWindow + off-screen->on-screen move above can take
        # longer than the fade phase itself (~117ms of the 350ms animation), so
        # timing from _begin_open_anim would leave the fade already finished
        # before the window is ever visible — no visible fade-in.
        open_anim_start = time.monotonic()
    # Restore focus to the field the user was typing in (a controller-open's
    # firmware mouse-click can steal it). No-op on Linux until the X11 capture +
    # restore is ported — the saved target is always None there.
    _restore_foreground(state.get_focus_restore_target())
    was_visible = True
    # Last key under each touchpad pointer, for haptic "switched key" ticks.
    last_hover = [None, None]
    # Next time the held left mouse button re-fires its key (inf = not armed).
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
    # Next time to re-poll _is_start_menu_open() while visible (see
    # _START_MENU_POLL_INTERVAL). 0 = check on the first iteration too, though
    # start_menu_open above already matches reality so that check is a no-op.
    next_start_check = 0.0

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
    # Tracks the Shift latch across frames so a change can be flagged as
    # activity (keeps ACTIVE_FPS through the key slide/fade animation).
    prev_shift = state.is_shift_held()

    # One reusable event struct polled each frame (SDL3 SDL_PollEvent fills it).
    ev = S.SDL_Event()

    while not state.should_close():
        activity = False
        now = time.monotonic()
        while S.SDL_PollEvent(ctypes.byref(ev)):
            et = ev.type
            if et == S.SDL_EVENT_QUIT:
                state.close()
                break
            if et == S.SDL_EVENT_WINDOW_RESIZED:
                screen.width = ev.window.data1
                screen.height = ev.window.data2
                activity = True
            # Mouse control: hovering highlights the key under the pointer,
            # left-click presses it (the Shift key toggles latched Shift), and
            # the standard side buttons handle the keys you can't otherwise
            # reach mouse-only. Right-click = Shift, X1 (back) = Backspace,
            # X2 (forward) = Space. The OSK window never takes focus, so
            # clicking it doesn't disturb the app being typed into. (SDL3
            # reports mouse x/y as floats; find_key* handles that.)
            elif et == S.SDL_EVENT_MOUSE_MOTION and state.is_visible():
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
                        activity = True
                mouse_anchor = mpos
                # If the left button isn't held anymore (e.g. it was released
                # off-window), drop any lingering press highlight and stop the
                # hold-to-repeat.
                if not (int(ev.motion.state) & S.SDL_BUTTON_LMASK):
                    state.set_mouse_press_cell(None)
                    mouse_repeat_at = float("inf")
            elif et == S.SDL_EVENT_MOUSE_BUTTON_DOWN and state.is_visible():
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
                activity = True
            elif (et == S.SDL_EVENT_MOUSE_BUTTON_UP
                    and ev.button.button == S.SDL_BUTTON_LEFT):
                state.set_mouse_press_cell(None)
                mouse_repeat_at = float("inf")
                activity = True

        # Poll any SDL pad (Xbox/DualSense/Switch Pro) HERE — on the thread that
        # just drained the SDL event queue. SDL only refreshes gamepad state on
        # its event-pump thread, so the tray's sdl_gamepad_thread reads stale /
        # all-zero frames once this loop is running (which froze SDL pads to no
        # input, and a frozen deflected stick slowly drifted the cursor). We read
        # the fresh state here and publish it for the input thread's
        # SharedSdlFrameSource (which feeds handle_input). The tray cedes while
        # the OSK is open, so this is the sole SDL poller then.
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
                _show_window_noactivate(scr.window, _open_anim_show_pos(open_anim_start, open_anim_rest))
                _finish_open_anim_map(scr, virtual_kb, controller_state,
                                      open_anim_start, open_anim_rest)
                if open_anim_start is not None:
                    # Start the clock now the window is on-screen — see the
                    # matching comment at the initial open above.
                    open_anim_start = time.monotonic()
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
            activity = True

        if cur_visible:
            # "Keyboard Sticks/Mouse controls" OFF (for the active controller) →
            # the sticks/mouse drive the desktop: make the OSK click-through so
            # the mouse falls through to the app behind. Re-checked every frame so
            # a live tray toggle (or a controller switch) takes effect at once.
            # DEFERRED while the open animation plays: click-through forces a
            # uniform layered-window alpha that overrides the per-pixel alpha the
            # fade/reveal composite relies on (applied the moment it ends).
            want_clickthrough = not state.is_kbd_stick_nav_enabled_for(
                state.get_active_controller())
            if open_anim_start is None and want_clickthrough != clickthrough_on:
                _set_click_through(scr.window, want_clickthrough)
                clickthrough_on = want_clickthrough
            # Apply a tray-side skin change live (no-op unless it changed).
            scr.maybe_reload_skin()
            virtual_kb.update_dimensions()
            # DPAD: step the cursor using the actual layout pixel positions.
            dpad_steps = state.drain_dpad_queue()
            for direction, haptic in dpad_steps:
                vkb.step_cursor(virtual_kb, direction, haptic=haptic)
            if controller_state.click_queue:
                activity = True
            vkb.process_click_queue(virtual_kb, controller_state.click_queue)
            # Mouse left-button hold-to-repeat: while held over a repeatable
            # key (Backspace / arrows), re-queue it on the shared cadence.
            # Queued before the drain so it dispatches this same frame. Keeps
            # the loop "active" so the repeat stays smooth at full FPS.
            press_cell = state.get_mouse_press_cell()
            if press_cell is not None:
                activity = True
                if now >= mouse_repeat_at:
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
            key_presses = state.drain_key_press_queue()
            for row, col, is_repeat in key_presses:
                if 0 <= row < len(virtual_kb.keys) and 0 <= col < len(virtual_kb.keys[row]):
                    key = virtual_kb.keys[row][col]
                    if is_repeat and not vkb.is_repeatable(key):
                        continue
                    vkb.dispatch_key(virtual_kb, key)
            if state.take_position_cycle_request():
                _cycle_window_position(scr.window)
                activity = True
                # Moving the window slides it under a stationary mouse, which
                # fires a spurious motion (new window-relative coords) — re-prime
                # so the highlight doesn't jump to the mouse after a Move.
                mouse_anchor = None
            req = state.take_window_position_request()
            if req is not None:
                _position_index[0] = req % 6
                _apply_window_position(scr.window)
                activity = True
                mouse_anchor = None
            # The open-time check above only forces _POS_UP_RIGHT at the moment
            # the OSK becomes visible — if the Start menu opens or closes while
            # the OSK is ALREADY showing, live-reposition in response (instant
            # snap, like the Move key above) instead of leaving it stuck. (No-op
            # on Linux: _is_start_menu_open() is always False there.)
            if now >= next_start_check:
                next_start_check = now + _START_MENU_POLL_INTERVAL
                now_start_open = _is_start_menu_open()
                if now_start_open != start_menu_open:
                    start_menu_open = now_start_open
                    _apply_window_position(
                        scr.window, _POS_UP_RIGHT if start_menu_open else None)
                    activity = True
                    mouse_anchor = None
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
            # A Shift state change (L2/ZL trigger, DPAD+A, right-click) drives
            # the dual-key slide/fade animation. Flag it as activity so the
            # grace period holds ACTIVE_FPS for the ~130 ms transition instead
            # of rendering it at the idle frame rate.
            cur_shift = state.is_shift_held()
            if cur_shift != prev_shift:
                activity = True
                prev_shift = cur_shift
            if open_anim_start is not None:
                # --- OSK OPEN animation frame (render at full FPS until done) ---
                activity = True
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
                    # Opacity eases 0→1 over the first FADE_FRAC; the bottom cut
                    # reveals over the first REVEAL_FRAC; the window settles
                    # DROP_PX downward from MOVE_START_FRAC to the end.
                    fade = _ease_out_cubic(min(1.0, p / _OPEN_ANIM_FADE_FRAC))
                    reveal_t = _ease_out_cubic(min(1.0, p / _OPEN_ANIM_REVEAL_FRAC))
                    cut = _OPEN_ANIM_CUT_PX * (1.0 - reveal_t)
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

    # Release a latched Shift before tearing down, so closing the keyboard
    # never leaves the OS with KEY_LEFTSHIFT held.
    vkb.release_shift()
    # Give the controller thread up to 1 second to run its cleanup (sends
    # the enable-lizard packet before closing the HID handle). Without this
    # wait the daemon thread is killed before it can re-enable lizard mode.
    sc_thread.join(timeout=1.0)
    # Free this session's window/renderer, then drop our SDL subsystem refs
    # (GAMEPAD/EVENTS stay up for a tray-owned persistent watcher).
    try:
        S.SDL_DestroyRenderer(scr.renderer)
        S.SDL_DestroyWindow(scr.window)
    except Exception:
        pass
    S.TTF_Quit()
    # VIDEO is intentionally NOT quit — it's inited once and kept for the life
    # of the process so the next open skips the ~400 ms subsystem re-init (see
    # main()'s init). This is just an idle X connection while closed (no window,
    # no loop → no CPU). EVENTS/GAMEPAD stay refcounted with the tray's watcher.
    S.SDL_QuitSubSystem(S.SDL_INIT_EVENTS | S.SDL_INIT_GAMEPAD)


if __name__ == '__main__':
    main()
