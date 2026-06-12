"""Build script for the lock-screen on-screen-keyboard EXE.

Run:
    python build_lockscreen.py

Produces `dist/LockScreenKeyboard.exe` — a single-file, no-console EXE that
opens the keyboard immediately (no tray, no Steam+X wait). This is the binary
the installer in `lockscreen-keyboard/` copies into place so the keyboard
appears on the Windows lock screen.

It is a trimmed clone of build.py: same data/ + SDL3 bundling, but it omits the
gamepad (vgamepad/ViGEm) collection since the lock-screen launcher never enters
gamepad mode, and it uses lockscreen_osk.py as the entry point.
"""

import glob
import os
import shutil
import subprocess
import sys


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
        # sdl3w is imported transitively (adusk.screen); name it explicitly.
        "--hidden-import", "sdl3w",
    ]
    if os.path.isfile(APP_ICON_ICO):
        cmd += ["--icon", APP_ICON_ICO]

    # sdl3w loads the vendored SDL3 DLLs from <bundle>/sdl3w/dll (see
    # sdl3w/_loader.py); ship SDL3.dll + SDL3_ttf.dll into that path.
    sdl_dll_dir = os.path.join(PROJECT_DIR, "sdl3w", "dll")
    sdl_dlls = glob.glob(os.path.join(sdl_dll_dir, "*.dll"))
    if not sdl_dlls:
        raise SystemExit(f"no SDL3 DLLs found in {sdl_dll_dir}")
    for dll in sdl_dlls:
        cmd += ["--add-binary", f"{dll};sdl3w/dll"]

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
