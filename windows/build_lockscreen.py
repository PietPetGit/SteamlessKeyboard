"""Build script for the lock-screen on-screen-keyboard EXE.

Run:
    python build_lockscreen.py

Produces `dist/LockScreenKeyboard.exe` — a single-file, no-console EXE that
opens the keyboard immediately (no tray, no Steam+X wait). This is the binary
the accessibility-tool hijack in `Desktop/windows hack/GUIDE.md` points at so
the keyboard appears on the Windows lock screen.

It is a trimmed clone of build.py: same data/ + SDL2 bundling, but it omits the
gamepad (vgamepad/ViGEm) collection since the lock-screen launcher never enters
gamepad mode, and it uses lockscreen_osk.py as the entry point.
"""

import glob
import os
import shutil
import subprocess
import sys

import sdl2dll


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY = "lockscreen_osk.py"
OUTPUT_NAME = "LockScreenKeyboard"
APP_ICON_ICO = os.path.join(PROJECT_DIR, "data", "images", "app_icon.ico")
DATA_DIR = os.path.join(PROJECT_DIR, "data")


def _run_pyinstaller():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", OUTPUT_NAME,
        "--add-data", f"{DATA_DIR};data",
        "--hidden-import", "pynput.keyboard._win32",
        "--hidden-import", "pynput.mouse._win32",
        # comtypes drives UI Automation to click the lock-screen password box
        # into focus. Collect the whole package (incl. comtypes.gen) so the
        # in-memory UIA codegen has everything it needs inside the frozen build.
        "--collect-all", "comtypes",
    ]
    if os.path.isfile(APP_ICON_ICO):
        cmd += ["--icon", APP_ICON_ICO]

    # PySDL2 finds SDL2.dll via PYSDL2_DLL_PATH (lockscreen_osk.py points it at
    # <bundle>/sdl2dll/dll), so ship the SDL2 DLL family into that same path.
    sdl_dll_dir = os.path.join(os.path.dirname(sdl2dll.__file__), "dll")
    for dll in glob.glob(os.path.join(sdl_dll_dir, "*.dll")):
        cmd += ["--add-binary", f"{dll};sdl2dll/dll"]

    cmd.append(ENTRY)
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def _cleanup():
    build_dir = os.path.join(PROJECT_DIR, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)


def main():
    _run_pyinstaller()
    _cleanup()
    out = os.path.join(PROJECT_DIR, "dist", f"{OUTPUT_NAME}.exe")
    print(f"\nbuilt: {out}")


if __name__ == "__main__":
    main()
