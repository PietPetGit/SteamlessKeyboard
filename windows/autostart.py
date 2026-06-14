"""Per-user "launch at logon" via a Start Menu Startup-folder shortcut.

Older builds registered autostart by writing
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``. Microsoft Defender's
behavioral ML (``Behavior:Win32/Persistence.A!ml``) flags that Run-key write
when it comes from an unsigned, freshly downloaded binary and quarantines the
app on first launch. A ``.lnk`` placed in the user's Startup folder hooks the
exact same logon event but is the conventional, non-flagged autostart
mechanism, so it sidesteps that detection.

Implementation notes:
  * Pure ``ctypes`` / COM (``IShellLinkW`` + ``IPersistFile``) — no pywin32 and
    no ``subprocess`` spawn. (Shelling out to ``powershell`` just to author a
    shortcut is itself a persistence heuristic, so we avoid it.) Nothing here
    adds an AV signal of its own.
  * Works on Windows 7 through 11: ``SHGetFolderPathW(CSIDL_STARTUP)`` and the
    Shell Link COM object are present on every one of those releases.
  * ``ctypes.windll`` is only touched inside functions so this module stays
    import-safe on non-Windows (it is never *used* off Windows, but importing
    it must not raise).
"""

import os
import sys


# Name of the shortcut we drop in the Startup folder.
SHORTCUT_NAME = "SteamlessKeyboard.lnk"

# Legacy autostart entry that older builds wrote and Defender flagged. We delete
# it whenever autostart state is applied so migrating users stop tripping the
# persistence detection.
_LEGACY_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_LEGACY_RUN_NAME = "SteamControllerKeyboard"

# COM identifiers for the Shell Link object.
_CLSID_ShellLink = "{00021401-0000-0000-C000-000000000046}"
_IID_IShellLinkW = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPersistFile = "{0000010B-0000-0000-C000-000000000046}"
_CLSCTX_INPROC_SERVER = 1

# CSIDL for the per-user Startup folder; SHGFP_TYPE_CURRENT asks for the live
# (possibly redirected) path rather than the default.
_CSIDL_STARTUP = 0x0007
_SHGFP_TYPE_CURRENT = 0
_MAX_PATH = 260


def _is_frozen():
    return getattr(sys, "frozen", False)


def _startup_dir():
    """Absolute path to the current user's Start Menu Startup folder, or None."""
    import ctypes
    try:
        buf = ctypes.create_unicode_buffer(_MAX_PATH)
        # SHGetFolderPathW(hwndOwner, nFolder, hToken, dwFlags, pszPath)
        hr = ctypes.windll.shell32.SHGetFolderPathW(
            None, _CSIDL_STARTUP, None, _SHGFP_TYPE_CURRENT, buf)
        if hr == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    # Fallback: the canonical layout under %APPDATA%.
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "Microsoft", "Windows",
                            "Start Menu", "Programs", "Startup")
    return None


def _shortcut_path():
    d = _startup_dir()
    return os.path.join(d, SHORTCUT_NAME) if d else None


def _target():
    """(target, arguments, working_dir) the shortcut should launch.

    Frozen: the EXE itself. From source: the current interpreter running the
    entry script (so "Start with Windows" still works during development)."""
    if _is_frozen():
        exe = os.path.abspath(sys.executable)
        return exe, "", os.path.dirname(exe)
    script = os.path.abspath(sys.argv[0])
    return os.path.abspath(sys.executable), f'"{script}"', os.path.dirname(script)


# --- pure-ctypes COM helpers ------------------------------------------------

def _guid(text):
    import ctypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    g = GUID()
    # CLSIDFromString parses both CLSIDs and IIDs in "{...}" form.
    if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text),
                                           ctypes.byref(g)) != 0:
        raise OSError(f"CLSIDFromString failed for {text}")
    return g


def _vtbl(iptr, index, restype, *argtypes):
    """Bind vtable method #index of COM interface pointer `iptr` (with the
    interface pointer itself bound as the implicit first argument)."""
    import ctypes
    vtable = ctypes.cast(iptr, ctypes.POINTER(ctypes.c_void_p))[0]
    func = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(func)


def _create_shortcut(lnk_path, target, arguments="", workdir="", icon=None):
    """Author a .lnk at `lnk_path` pointing at `target`. Returns True on success.

    IShellLinkW / IPersistFile vtable indices (after IUnknown's 0..2):
      7 SetDescription · 9 SetWorkingDirectory · 11 SetArguments ·
      17 SetIconLocation · 20 SetPath   (IShellLinkW)
      6 Save                            (IPersistFile)
    """
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.windll.ole32
    init = ole32.CoInitialize(None)  # 0/1 = we own it; else already initialized
    try:
        psl = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid(_CLSID_ShellLink)), None, _CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(_IID_IShellLinkW)), ctypes.byref(psl))
        if hr != 0 or not psl.value:
            return False
        try:
            _vtbl(psl, 20, ctypes.HRESULT, wintypes.LPCWSTR)(psl, target)
            if arguments:
                _vtbl(psl, 11, ctypes.HRESULT, wintypes.LPCWSTR)(psl, arguments)
            if workdir:
                _vtbl(psl, 9, ctypes.HRESULT, wintypes.LPCWSTR)(psl, workdir)
            if icon:
                _vtbl(psl, 17, ctypes.HRESULT, wintypes.LPCWSTR,
                      ctypes.c_int)(psl, icon, 0)

            ppf = ctypes.c_void_p()
            hr = _vtbl(psl, 0, ctypes.HRESULT, ctypes.c_void_p,
                       ctypes.c_void_p)(
                psl, ctypes.byref(_guid(_IID_IPersistFile)), ctypes.byref(ppf))
            if hr != 0 or not ppf.value:
                return False
            try:
                _vtbl(ppf, 6, ctypes.HRESULT, wintypes.LPCWSTR,
                      wintypes.BOOL)(ppf, lnk_path, True)  # Save(path, remember)
            finally:
                _vtbl(ppf, 2, ctypes.c_ulong)(ppf)  # Release
        finally:
            _vtbl(psl, 2, ctypes.c_ulong)(psl)  # Release
        return os.path.isfile(lnk_path)
    except OSError:
        return False
    finally:
        if init in (0, 1):
            ole32.CoUninitialize()


def _remove_legacy_run_key():
    """Delete the old HKCU\\...\\Run value if present (best effort)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _LEGACY_RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, _LEGACY_RUN_NAME)
            except FileNotFoundError:
                pass
    except OSError:
        pass


# --- public API -------------------------------------------------------------

def is_enabled():
    """True if the Startup-folder shortcut currently exists."""
    path = _shortcut_path()
    return bool(path and os.path.isfile(path))


def enable():
    """Create (or refresh) the Startup-folder shortcut. Returns True on success."""
    path = _shortcut_path()
    if not path:
        return False
    target, arguments, workdir = _target()
    icon = target if _is_frozen() else None
    return _create_shortcut(path, target, arguments, workdir, icon)


def disable():
    """Remove the Startup-folder shortcut. Returns True if it is gone afterwards."""
    path = _shortcut_path()
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            return False
    return True


def set_enabled(enabled):
    """Apply the desired autostart state and always clear the legacy Run key so
    migrating users stop tripping the persistence detection."""
    _remove_legacy_run_key()
    return enable() if enabled else disable()
