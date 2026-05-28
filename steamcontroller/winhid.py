"""Exclusive HID device backend (Windows) for the "block Steam takeover" mode.

The standard hidapi library opens controllers in *shared* mode on purpose, so
multiple apps (e.g. Steam) can read them at once. That's exactly what lets Steam
grab the Steam Controller out from under us. This backend instead opens the
device with no sharing (`CreateFileW` dwShareMode=0): while we hold it, the OS
refuses to let any other process — Steam included — open the same device, so it
can't read the Steam button or force lizard mode.

It exposes the small slice of the `hid.device()` API that SteamController uses:
open_path / read / write / send_feature_report / set_nonblocking / close.

Caveat: an exclusive open only succeeds if nobody already has the device open,
so we must grab it before Steam does. If exclusive is denied, open_path raises
and the caller falls back to normal shared hidapi.
"""

import ctypes
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_hid = ctypes.WinDLL("hid")

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0x00000000
INFINITE = 0xFFFFFFFF
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _HIDP_CAPS(ctypes.Structure):
    # Only the first five fields matter to us; the rest pad the struct out to
    # the size HidP_GetCaps writes.
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
_kernel32.ReadFile.restype = wintypes.BOOL
_kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_OVERLAPPED)]
_kernel32.WriteFile.restype = wintypes.BOOL
_kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_OVERLAPPED)]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.GetOverlappedResult.restype = wintypes.BOOL
_kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED),
                                          ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
_kernel32.CancelIoEx.restype = wintypes.BOOL
_kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL,
                                   wintypes.LPCWSTR]
_kernel32.ResetEvent.argtypes = [wintypes.HANDLE]

_hid.HidD_GetPreparsedData.restype = wintypes.BOOL
_hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
_hid.HidD_FreePreparsedData.restype = wintypes.BOOL
_hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
_hid.HidD_SetFeature.restype = wintypes.BOOL
_hid.HidD_SetFeature.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD]
_hid.HidP_GetCaps.restype = ctypes.c_long
_hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDP_CAPS)]


class ExclusiveHidDevice:
    """hid.device()-compatible (subset) wrapper that opens the device with no
    sharing, so other processes (Steam) cannot open it while we hold it."""

    def __init__(self):
        self._handle = None
        self._read_ov = None
        self._write_ov = None
        self._input_len = 64
        self._output_len = 65
        self._feature_len = 65

    def open_path(self, path):
        if isinstance(path, bytes):
            path = path.decode(errors="ignore")
        handle = _kernel32.CreateFileW(
            path, GENERIC_READ | GENERIC_WRITE,
            0,                      # dwShareMode = 0 → exclusive
            None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if not handle or handle == _INVALID_HANDLE:
            err = ctypes.get_last_error()
            raise OSError(f"exclusive CreateFileW failed (winerr {err})")
        self._handle = handle
        self._read_ov = _OVERLAPPED(hEvent=_kernel32.CreateEventW(None, True, False, None))
        self._write_ov = _OVERLAPPED(hEvent=_kernel32.CreateEventW(None, True, False, None))
        self._load_caps()

    def _load_caps(self):
        pp = ctypes.c_void_p()
        if _hid.HidD_GetPreparsedData(self._handle, ctypes.byref(pp)):
            caps = _HIDP_CAPS()
            if _hid.HidP_GetCaps(pp, ctypes.byref(caps)) >= 0:
                if caps.InputReportByteLength:
                    self._input_len = caps.InputReportByteLength
                if caps.OutputReportByteLength:
                    self._output_len = caps.OutputReportByteLength
                if caps.FeatureReportByteLength:
                    self._feature_len = caps.FeatureReportByteLength
            _hid.HidD_FreePreparsedData(pp)

    def set_nonblocking(self, _flag):
        # We always read with an explicit timeout, so this is a no-op.
        return 0

    def read(self, length, timeout_ms=None):
        if self._handle is None:
            return []
        n = max(int(length), self._input_len)
        buf = (ctypes.c_ubyte * n)()
        nread = wintypes.DWORD(0)
        _kernel32.ResetEvent(self._read_ov.hEvent)
        ok = _kernel32.ReadFile(self._handle, buf, n, ctypes.byref(nread),
                                ctypes.byref(self._read_ov))
        if not ok:
            if ctypes.get_last_error() != ERROR_IO_PENDING:
                return []
            wait = INFINITE if timeout_ms is None else int(timeout_ms)
            if _kernel32.WaitForSingleObject(self._read_ov.hEvent, wait) != WAIT_OBJECT_0:
                # Timed out (or failed) — cancel the pending read and drain it.
                _kernel32.CancelIoEx(self._handle, ctypes.byref(self._read_ov))
                _kernel32.GetOverlappedResult(self._handle, ctypes.byref(self._read_ov),
                                              ctypes.byref(nread), True)
                return []
            if not _kernel32.GetOverlappedResult(self._handle, ctypes.byref(self._read_ov),
                                                 ctypes.byref(nread), False):
                return []
        return list(buf[:nread.value])

    def write(self, data):
        if self._handle is None:
            return -1
        n = self._output_len
        b = bytes(data)[:n].ljust(n, b"\x00")
        buf = (ctypes.c_ubyte * n).from_buffer_copy(b)
        nwritten = wintypes.DWORD(0)
        _kernel32.ResetEvent(self._write_ov.hEvent)
        ok = _kernel32.WriteFile(self._handle, buf, n, ctypes.byref(nwritten),
                                 ctypes.byref(self._write_ov))
        if not ok:
            if ctypes.get_last_error() != ERROR_IO_PENDING:
                return -1
            _kernel32.WaitForSingleObject(self._write_ov.hEvent, 1000)
            _kernel32.GetOverlappedResult(self._handle, ctypes.byref(self._write_ov),
                                          ctypes.byref(nwritten), True)
        return nwritten.value

    def send_feature_report(self, data):
        if self._handle is None:
            return -1
        n = self._feature_len
        b = bytes(data)[:n].ljust(n, b"\x00")
        buf = (ctypes.c_ubyte * n).from_buffer_copy(b)
        return n if _hid.HidD_SetFeature(self._handle, buf, n) else -1

    def close(self):
        h = self._handle
        self._handle = None
        if h is not None:
            try:
                _kernel32.CancelIoEx(h, None)
            except Exception:
                pass
            try:
                _kernel32.CloseHandle(h)
            except Exception:
                pass
        for ov in (self._read_ov, self._write_ov):
            if ov is not None and ov.hEvent:
                try:
                    _kernel32.CloseHandle(ov.hEvent)
                except Exception:
                    pass
        self._read_ov = self._write_ov = None
