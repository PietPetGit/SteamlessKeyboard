"""Direct on-screen-keyboard launcher for the Windows lock screen.

Unlike tray.py this has NO tray icon and does NOT wait for Steam+X: the secure
(Winlogon) desktop where the lock screen lives has no Explorer shell and no
notification area, so a tray launcher is useless there. This process instead
brings the keyboard up *immediately* when it starts, and keeps it up (re-opening
if it is closed) until the machine is unlocked — at which point the secure
desktop tears down and this process is killed automatically.

It is meant to be started by the accessibility-tool hijack described in
`Desktop/windows hack/GUIDE.md`, so it runs as SYSTEM on the secure desktop and
its injected keystrokes land in the lock-screen password box.

Escape hatch: close the keyboard (Move / B / L4+L5) three times within five
seconds to make this process fully exit instead of re-opening.
"""

import os
import sys
import threading
import time


def _is_frozen():
    return getattr(sys, "frozen", False)


def _bundle_dir():
    """Directory containing the bundled data/ folder and SDL2 DLLs."""
    if _is_frozen():
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


# Mirror tray.py's bootstrap: adusk.resources captures ADUSK_DATA at import
# time, and PySDL2 needs PYSDL2_DLL_PATH to find the bundled SDL2 DLLs inside a
# PyInstaller --onefile build. Both must be set BEFORE importing adusk.*.
os.environ["ADUSK_DATA"] = os.path.join(_bundle_dir(), "data")
if _is_frozen():
    _sdl_dll_dir = os.path.join(_bundle_dir(), "sdl2dll", "dll")
    if os.path.isdir(_sdl_dll_dir):
        os.environ["PYSDL2_DLL_PATH"] = _sdl_dll_dir

from adusk import adusk as adusk_app   # noqa: E402
from adusk import state as adusk_state  # noqa: E402


# --- Keep the lock-screen password box focused -----------------------------
#
# Clicking the Ease of Access button to launch us moves focus OFF the password
# field, and once we grab the controller the firmware "lizard" mouse is gone, so
# the user can't click the field back. Our injected keystrokes (SendInput) go to
# whatever has keyboard focus, so we must put focus back on the credential UI
# ourselves. The lock/logon credential box is hosted by LogonUI.exe on the
# secure desktop; we periodically bring its top-level window to the foreground.
# Our own SDL window is WS_EX_NOACTIVATE + TOPMOST, so it stays visible on top
# without ever stealing focus — LogonUI keeps the password edit focused and the
# typed characters land in it.
#
# Windows-only and self-contained, so the main app's normal-desktop behaviour is
# untouched. On a normal (unlocked) desktop LogonUI has no visible window, so the
# watcher simply finds nothing and idles — safe to always run.

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _user32.SetActiveWindow.argtypes = [wintypes.HWND]
    _user32.SetFocus.argtypes = [wintypes.HWND]
    _user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    _user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_void_p]
    _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _user32.mouse_event.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]

    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def _proc_name(pid):
        if not pid:
            return None
        h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            _kernel32.CloseHandle(h)
        return None

    def _find_logonui_hwnd():
        found = []

        def _cb(hwnd, _lparam):
            try:
                if not _user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                name = _proc_name(pid.value)
                if name and name.lower() == "logonui.exe":
                    found.append(hwnd)
                    return False
            except Exception:
                pass
            return True

        _user32.EnumWindows(_ENUMPROC(_cb), 0)
        return found[0] if found else None

    _VK_MENU = 0x12          # Alt
    _KEYEVENTF_KEYUP = 0x0002
    _ASFW_ANY = 0xFFFFFFFF   # AllowSetForegroundWindow(ASFW_ANY)

    def _force_foreground(hwnd):
        # Windows silently refuses SetForegroundWindow from a process that didn't
        # receive the last input event (the "foreground lock"). The reliable
        # work-around used by on-screen keyboards: (1) synthesize an Alt tap so
        # the system thinks the user just provided input, (2) attach our input
        # queue to the TARGET (LogonUI) thread so we share its focus state, then
        # (3) force foreground/active/focus together. With LogonUI active, the
        # lock screen routes our injected characters into the password box the
        # same way it does for a real keyboard.
        try:
            _user32.keybd_event(_VK_MENU, 0, 0, None)
            _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, None)
        except Exception:
            pass

        target_thread = _user32.GetWindowThreadProcessId(hwnd, None)
        cur = _kernel32.GetCurrentThreadId()
        attached = False
        if target_thread and target_thread != cur:
            attached = bool(_user32.AttachThreadInput(cur, target_thread, True))
        try:
            try:
                _user32.AllowSetForegroundWindow(_ASFW_ANY)
            except Exception:
                pass
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetActiveWindow(hwnd)
            _user32.SetFocus(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(cur, target_thread, False)

    # --- UI Automation: click the password box into focus ---------------------
    #
    # SetForegroundWindow can't reliably focus the credential *element* — the
    # lock-screen password box is a XAML control, not a child HWND, and the
    # foreground lock fights cross-process focus changes. A real mouse click does
    # exactly what the user does: it focuses whatever sits under the cursor. Our
    # process runs as SYSTEM at the same integrity level as LogonUI, so injected
    # mouse input isn't blocked by UIPI (the same reason our keystrokes already
    # reach the box once it's focused). UI Automation locates the password Edit
    # control and gives us its on-screen rectangle so we can click its centre.

    _UIA_ControlTypePropertyId = 30003
    _UIA_EditControlTypeId = 50004
    _TreeScope_Subtree = 4
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004

    _uia = {"iface": None, "ready": False, "failed": False}

    def _uia_init():
        """Lazily create the IUIAutomation instance on the calling thread.
        Generates the UIA wrapper in memory (gen_dir=None) so it works inside a
        read-only, frozen onefile build."""
        if _uia["ready"]:
            return True
        if _uia["failed"]:
            return False
        try:
            import comtypes
            import comtypes.client
            comtypes.CoInitialize()
            comtypes.client.gen_dir = None
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIAc
            _uia["iface"] = comtypes.client.CreateObject(
                UIAc.CUIAutomation, interface=UIAc.IUIAutomation)
            _uia["ready"] = True
        except Exception as e:
            print(f"UIA init failed: {e!r}")
            _uia["failed"] = True
        return _uia["ready"]

    def _click_password_box():
        """Find the lock-screen password Edit via UIA and click its centre so it
        takes keyboard focus. Returns True if a box was found and clicked."""
        if not _uia_init():
            return False
        hwnd = _find_logonui_hwnd()
        if not hwnd:
            return False
        try:
            iface = _uia["iface"]
            root = iface.ElementFromHandle(hwnd)
            cond = iface.CreatePropertyCondition(
                _UIA_ControlTypePropertyId, _UIA_EditControlTypeId)
            edit = root.FindFirst(_TreeScope_Subtree, cond)
            if not edit:
                return False
            r = edit.CurrentBoundingRectangle
            if r.right <= r.left or r.bottom <= r.top:
                return False  # zero-size/offscreen — not the real box yet
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            try:
                edit.SetFocus()
            except Exception:
                pass
            _user32.SetCursorPos(int(cx), int(cy))
            _user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
            _user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, None)
            return True
        except Exception as e:
            print(f"UIA click failed: {e!r}")
            return False

    def _credential_focus_watcher():
        # The keyboard and LogonUI take a moment to settle after the Ease-of-
        # Access click. Keep trying to click the password box into focus until it
        # succeeds (the Edit only appears once the credential UI is up), then go
        # quiet so we don't keep yanking the cursor away from the user. If UIA is
        # unavailable for any reason, fall back to the old foreground nudge.
        clicked = False
        attempts = 0
        while True:
            try:
                if not clicked:
                    if _click_password_box():
                        clicked = True
                    else:
                        hwnd = _find_logonui_hwnd()
                        if hwnd and _user32.GetForegroundWindow() != hwnd:
                            _force_foreground(hwnd)
                    attempts += 1
                else:
                    # Re-click only if focus genuinely left the credential UI
                    # (e.g. a stray click elsewhere), never while it's already
                    # the active window — keeps the cursor still during typing.
                    hwnd = _find_logonui_hwnd()
                    if hwnd and _user32.GetForegroundWindow() != hwnd:
                        _click_password_box()
            except Exception:
                pass
            if clicked:
                time.sleep(1.5)
            else:
                time.sleep(0.3 if attempts < 60 else 1.0)

    def _start_credential_focus():
        threading.Thread(target=_credential_focus_watcher, daemon=True).start()

else:
    def _start_credential_focus():
        pass


def main():
    # Keep the lock-screen password box focused so injected keystrokes land in
    # it (see the long comment above). No-op off the secure desktop.
    _start_credential_focus()

    # Run the keyboard once; closing it exits cleanly. We deliberately do NOT
    # re-open on close: the user can always re-summon the keyboard by pressing
    # the lock screen's Ease-of-Access button again (which relaunches us), so a
    # single close = dismiss, with no surprise pop-backs.
    adusk_state.reset_session()
    try:
        adusk_app.main()
    except Exception as e:
        print(f"adusk crashed: {e!r}")


if __name__ == "__main__":
    main()
