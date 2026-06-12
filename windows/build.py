"""Build script for the portable Steam Controller Keyboard EXE.

Run:
    python build.py

Produces `dist/SteamlessKeyboard-windows.exe`. Uses the prebuilt
`data/images/app_icon.ico` (multi-resolution) directly as the EXE icon
and bundles the data/ folder as PyInstaller datas. The output is a
single-file, no-console exe suitable for dropping anywhere.

Also rebuilds the lock-screen keyboard (build_lockscreen.py) and copies the
result over lockscreen-keyboard/LockScreenKeyboard.exe, so the packaged
lock-screen exe always matches the current adusk/ source.
"""

import glob
import os
import shutil
import subprocess
import sys

import build_lockscreen


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
        # vgamepad is vendored under windows/vgamepad; import paths are static
        # but the ViGEmClient DLLs are added explicitly below.
        "--hidden-import", "vgamepad",
        "--hidden-import", "vgamepad.win.virtual_gamepad",
        "--hidden-import", "vgamepad.win.vigem_client",
        "--hidden-import", "vgamepad.win.vigem_commons",
        # Our hand-rolled SDL3 binding is imported transitively (adusk.screen
        # -> sdl3w); name it explicitly so PyInstaller always bundles it.
        "--hidden-import", "sdl3w",
    ]

    # sdl3w loads the vendored SDL3 DLLs at import time, searching
    # <bundle>/sdl3w/dll first (see sdl3w/_loader.py). Ship the pinned SDL3
    # family (SDL3.dll + SDL3_ttf.dll) into that same path inside the EXE.
    sdl_dll_dir = os.path.join(PROJECT_DIR, "sdl3w", "dll")
    sdl_dlls = glob.glob(os.path.join(sdl_dll_dir, "*.dll"))
    if not sdl_dlls:
        raise SystemExit(f"no SDL3 DLLs found in {sdl_dll_dir}")
    for dll in sdl_dlls:
        cmd += ["--add-binary", f"{dll};sdl3w/dll"]

    vigem_client_dir = os.path.join(PROJECT_DIR, "vgamepad", "win", "vigem", "client")
    for arch in ("x64", "x86"):
        dll = os.path.join(vigem_client_dir, arch, "ViGEmClient.dll")
        if not os.path.isfile(dll):
            raise SystemExit(f"ViGEmClient.dll not found: {dll}")
        cmd += ["--add-binary", f"{dll};vgamepad/win/vigem/client/{arch}"]

    cmd.append(ENTRY)
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def _cleanup():
    # Trim PyInstaller's intermediate artifacts; keep dist/ and the .spec.
    build_dir = os.path.join(PROJECT_DIR, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)


def _build_lockscreen():
    build_lockscreen.main()
    src = os.path.join(PROJECT_DIR, "dist", "LockScreenKeyboard.exe")
    dst = os.path.join(PROJECT_DIR, "lockscreen-keyboard", "LockScreenKeyboard.exe")
    shutil.copy2(src, dst)
    print(f"updated: {dst}")


def main():
    _check_icon()
    _run_pyinstaller()
    _cleanup()
    out = os.path.join(PROJECT_DIR, "dist", f"{OUTPUT_NAME}.exe")
    print(f"\nbuilt: {out}")

    _build_lockscreen()


if __name__ == "__main__":
    main()
