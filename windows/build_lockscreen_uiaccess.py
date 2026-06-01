"""Build the uiAccess variant of the lock-screen keyboard.

Run:
    python build_lockscreen_uiaccess.py

Produces a ONE-DIR build at dist/LockScreenKeyboardUIA/ whose main exe has a
manifest with uiAccess="true". uiAccess is what lets the keyboard set the
foreground/focus on the secure (lock-screen) desktop so typed characters reach
the password box. For that privilege to actually be granted, the exe must ALSO
be (1) signed by a trusted cert and (2) run from a secure location such as
%ProgramFiles% -- both handled by setup_uiaccess.ps1.

One-dir (not one-file) is deliberate: a uiAccess exe must run from the secure
folder it is installed in; one-file extracts to %TEMP% (user-writable, not
secure), which voids uiAccess.
"""

import glob
import os
import shutil
import subprocess
import sys

import sdl2dll


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY = "lockscreen_osk.py"
OUTPUT_NAME = "LockScreenKeyboardUIA"
APP_ICON_ICO = os.path.join(PROJECT_DIR, "data", "images", "app_icon.ico")
DATA_DIR = os.path.join(PROJECT_DIR, "data")


def _run_pyinstaller():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--uac-uiaccess",
        "--name", OUTPUT_NAME,
        "--add-data", f"{DATA_DIR};data",
        "--hidden-import", "pynput.keyboard._win32",
        "--hidden-import", "pynput.mouse._win32",
    ]
    if os.path.isfile(APP_ICON_ICO):
        cmd += ["--icon", APP_ICON_ICO]

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
    out = os.path.join(PROJECT_DIR, "dist", OUTPUT_NAME, f"{OUTPUT_NAME}.exe")
    print(f"\nbuilt: {out}")


if __name__ == "__main__":
    main()
