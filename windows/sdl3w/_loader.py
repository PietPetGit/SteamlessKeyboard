"""Locate and load the vendored SDL3 DLLs (core + image + ttf).

This is the one place that knows where the binaries live, both when running
from source (windows/sdl3w/dll) and when frozen by PyInstaller (the DLLs are
added to the bundle root / a sdl3w/dll subdir via build.py --add-binary).

Hand-rolled rather than depending on PySDL3 so the shipped onefile carries its
own pinned SDL3 (no runtime download) and we control exactly what's bound.
"""

import ctypes
import os
import sys

# Pinned SDL3 component versions vendored under dll/. Kept here for reference /
# diagnostics (printed by the smoke test); not used for loading.
SDL3_VERSION = "3.4.10"
SDL3_TTF_VERSION = "3.2.2"
# NOTE: SDL3_image is intentionally NOT vendored — the OSK loads every PNG
# (glyphs/skins) through Pillow and uploads via SDL_CreateSurfaceFrom, so the
# only SDL_image use in the old code (IMG_Init) is dead weight under SDL3.

_CORE_DLL = "SDL3.dll"
_TTF_DLL = "SDL3_ttf.dll"


def _candidate_dirs():
    """Directories to search for the SDL3 DLLs, most-specific first."""
    dirs = []
    # PyInstaller onefile extracts to sys._MEIPASS; build.py drops the DLLs both
    # at the bundle root and under sdl3w/dll, so check both.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "sdl3w", "dll"))
        dirs.append(meipass)
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(os.path.join(here, "dll"))
    dirs.append(here)
    # De-dup while preserving order.
    seen = set()
    out = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _find_dir():
    for d in _candidate_dirs():
        if os.path.exists(os.path.join(d, _CORE_DLL)):
            return d
    raise OSError(
        "SDL3.dll not found. Looked in: " + os.pathsep.join(_candidate_dirs())
    )


def load():
    """Load SDL3 and SDL3_ttf and return (SDL, TTF, dll_dir).

    SDL3_ttf depends on SDL3.dll, so we add the dll dir to the Windows DLL
    search path and load SDL3.dll first.
    """
    dll_dir = _find_dir()
    # Let dependent DLLs (SDL3.dll for SDL3_ttf) resolve out of the same folder.
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(dll_dir)
        except OSError:
            pass
    os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")

    SDL = ctypes.CDLL(os.path.join(dll_dir, _CORE_DLL))
    TTF = ctypes.CDLL(os.path.join(dll_dir, _TTF_DLL))
    return SDL, TTF, dll_dir
