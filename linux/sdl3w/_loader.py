"""Locate and load the SDL3 shared objects (core + ttf) on Linux.

Prefer a vendored copy under sdl3w/dll (so a PyInstaller bundle can ship its own
pinned SDL3), but fall back to the system libSDL3.so.0 / libSDL3_ttf.so.0 — the
normal dev path on a distro that ships SDL3 (e.g. CachyOS `pacman -S sdl3
sdl3_ttf`).

Hand-rolled rather than depending on PySDL3 so we control exactly what's bound;
the Linux mirror of windows/sdl3w/_loader.py (which loads the .dll equivalents).
"""

import ctypes
import os
import sys

# Pinned versions matched to what the Windows tree vendors / what CachyOS ships.
SDL3_VERSION = "3.4.10"
SDL3_TTF_VERSION = "3.2.2"

# Most-specific soname first; the bare soname is resolved from the system
# library path by ld.so when no vendored copy is present.
_CORE_NAMES = ["libSDL3.so.0", "libSDL3.so"]
_TTF_NAMES = ["libSDL3_ttf.so.0", "libSDL3_ttf.so"]


def _candidate_dirs():
    """Directories to search for a vendored SDL3, most-specific first."""
    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "sdl3w", "dll"))
        dirs.append(meipass)
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(os.path.join(here, "dll"))
    seen = set()
    out = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _vendored_dir():
    """Return the dir holding a vendored libSDL3, or None (→ use the system)."""
    for d in _candidate_dirs():
        for n in _CORE_NAMES:
            if os.path.exists(os.path.join(d, n)):
                return d
    return None


def _load(names, dll_dir):
    """Load the first available .so — a vendored copy in dll_dir if present,
    else the bare soname resolved by ld.so (system install)."""
    if dll_dir:
        for n in names:
            p = os.path.join(dll_dir, n)
            if os.path.exists(p):
                return ctypes.CDLL(p)
    last = None
    for n in names:
        try:
            return ctypes.CDLL(n)
        except OSError as e:
            last = e
    raise last if last is not None else OSError("could not load " + names[0])


def load():
    """Load SDL3 and SDL3_ttf and return (SDL, TTF, source).

    SDL3_ttf depends on SDL3, which we load first so its soname is already in
    the process when ld.so resolves SDL3_ttf's NEEDED entry.
    """
    dll_dir = _vendored_dir()
    if dll_dir:
        os.environ["LD_LIBRARY_PATH"] = (
            dll_dir + os.pathsep + os.environ.get("LD_LIBRARY_PATH", ""))
    SDL = _load(_CORE_NAMES, dll_dir)
    TTF = _load(_TTF_NAMES, dll_dir)
    return SDL, TTF, (dll_dir or "system")
