"""Build script for the portable Steam Controller Keyboard EXE.

Run:
    python build.py

Produces `dist/SteamlessKeyboard-windows.exe`. Uses the prebuilt
`data/images/app_icon.ico` (multi-resolution) directly as the EXE icon
and bundles the data/ folder as PyInstaller datas. The output is a
single-file, no-console exe suitable for dropping anywhere.
"""

import glob
import os
import shutil
import subprocess
import sys

import sdl2dll


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY = "tray.py"
OUTPUT_NAME = "SteamlessKeyboard-windows"
APP_ICON_ICO = os.path.join(PROJECT_DIR, "data", "images", "app_icon.ico")
DATA_DIR = os.path.join(PROJECT_DIR, "data")


def _check_icon():
    if not os.path.isfile(APP_ICON_ICO):
        raise SystemExit(f"app icon not found: {APP_ICON_ICO}")
    print(f"exe icon: {APP_ICON_ICO}")


def _run_pyinstaller():
    # `data;data` tells PyInstaller to drop the data/ folder into the bundle
    # rooted at "data". On Windows the separator in --add-data is ";".
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", OUTPUT_NAME,
        "--icon", APP_ICON_ICO,
        "--add-data", f"{DATA_DIR};data",
        # pystray uses platform-specific backends loaded at runtime; pyinstaller
        # doesn't always pick them up unless we name them explicitly.
        "--hidden-import", "pystray._win32",
        "--hidden-import", "pynput.keyboard._win32",
        "--hidden-import", "pynput.mouse._win32",
        "--hidden-import", "PIL._tkinter_finder",
        # vgamepad ships ViGEmClient.dll inside its package; collect-all
        # picks up the dll + the .pyd ctypes shim so gamepad mode works
        # inside the frozen exe.
        "--collect-all", "vgamepad",
    ]

    # PySDL2 looks for SDL2.dll via PYSDL2_DLL_PATH at import time. tray.py
    # sets that env var to <bundle>/sdl2dll/dll, so we ship the SDL2 family
    # of DLLs into that same path inside the EXE.
    sdl_dll_dir = os.path.join(os.path.dirname(sdl2dll.__file__), "dll")
    for dll in glob.glob(os.path.join(sdl_dll_dir, "*.dll")):
        cmd += ["--add-binary", f"{dll};sdl2dll/dll"]

    cmd.append(ENTRY)
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def _cleanup():
    # Trim PyInstaller's intermediate artifacts; keep dist/ and the .spec.
    build_dir = os.path.join(PROJECT_DIR, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)


def main():
    _check_icon()
    _run_pyinstaller()
    _cleanup()
    out = os.path.join(PROJECT_DIR, "dist", f"{OUTPUT_NAME}.exe")
    print(f"\nbuilt: {out}")


if __name__ == "__main__":
    main()
