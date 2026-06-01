"""Build script for the Linux on-screen-keyboard build.

Run on a Linux machine (NOT inside WSL unless your distro can actually
talk to the display server you're targeting):

    python build_linux.py

Produces a release-ready tarball next to the unpacked binary in dist/:

    dist/SteamlessKeyboard               # ELF binary (no .exe — Linux convention)
    dist/SteamlessKeyboard.png           # 256x256 icon
    dist/SteamlessKeyboard.desktop       # portable launcher (uses %k for paths)
    dist/LICENSE                         # bundled into the tarball
    dist/SteamlessKeyboard-linux.tar.gz  # ← upload this to a GitHub Release

Scope is intentionally narrow: only the on-screen keyboard is ported.
Tray, autostart, Steam-detection, and the ViGEm virtual gamepad stay
Windows-only for now.

System prerequisites (Debian/Ubuntu names — adjust for your distro):
    sudo apt install python3-dev libsdl2-2.0-0 libsdl2-image-2.0-0 \\
                     libsdl2-gfx-1.0-0 libhidapi-hidraw0 libxkbcommon0

Python prerequisites:
    pip install pyinstaller pysdl2 pillow pynput hidapi pyyaml

The binary loads libSDL2/libhidapi from the system at runtime, so the host
distro must have the matching shared libraries installed.
"""

import os
import shutil
import subprocess
import sys
import tarfile

from PIL import Image


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PROJECT_DIR)
# tray_linux is the default entry; --no-tray on the resulting binary falls
# back to the same headless behavior adusk_linux.py provides.
ENTRY = "tray_linux.py"
# No .exe suffix — Linux binaries are extensionless. The platform tag lives
# in the release tarball name, not on the binary.
OUTPUT_NAME = "SteamlessKeyboard"
TARBALL_NAME = "SteamlessKeyboard-linux.tar.gz"
DATA_DIR = os.path.join(PROJECT_DIR, "data")


def _check_platform():
    if not sys.platform.startswith("linux"):
        raise SystemExit(
            f"build_linux.py must be run on Linux (got {sys.platform!r}). "
            "Use build.py for the Windows build."
        )


def _run_pyinstaller():
    # Use /tmp for intermediate build files — required when PROJECT_DIR is on an
    # NTFS-mounted Windows partition, where PyInstaller hooks (e.g. GdkPixbuf)
    # create files in the workpath before PyInstaller has fully created the dir.
    work_dir = "/tmp/SteamlessKeyboard-build"
    # PyInstaller's --add-data separator on POSIX is ':' (it's ';' on Windows).
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--workpath", work_dir,
        # Linux has no "windowed" detached-from-tty mode like Windows --windowed.
        # Leave stdout/stderr attached so launch errors surface in the terminal.
        "--name", OUTPUT_NAME,
        "--add-data", f"{DATA_DIR}:data",
        # pynput's X11 backend is the one we need at runtime; the others would
        # just fail to import on Linux.
        "--hidden-import", "pynput.keyboard._xorg",
        "--hidden-import", "pynput.mouse._xorg",
        "--hidden-import", "PIL._tkinter_finder",
        # pystray's Linux backend selection happens at import time and uses
        # dynamic imports PyInstaller can't follow. tray_linux.py forces the
        # AppIndicator backend (xorg has no menu support and doesn't render
        # well on KDE Plasma), so pull it in explicitly.
        "--hidden-import", "pystray._appindicator",
        "--hidden-import", "pystray._util.gtk",
        "--hidden-import", "pystray._util.notify_dbus",
        # gi (PyGObject) is the AppIndicator backend's import path. Without
        # this PyInstaller skips the entire gi/_gi modules and the bundle
        # crashes on first AppIndicator call. Doesn't bundle the underlying
        # GTK shared libs — those have to come from the host distro.
        "--hidden-import", "gi",
        "--hidden-import", "gi.repository.Gtk",
        "--hidden-import", "gi.repository.AyatanaAppIndicator3",
        # Windows-only modules: never include them in a Linux build. winhid is
        # only loaded when the Windows-only exclusive-HID feature is requested,
        # which the Linux entry never does — excluding it stops PyInstaller from
        # tripping on its ctypes.WinDLL("kernel32") module-level call.
        "--exclude-module", "steamcontroller.winhid",
        "--exclude-module", "vgamepad",
        "--exclude-module", "winreg",
        "--exclude-module", "pynput.keyboard._win32",
        "--exclude-module", "pynput.mouse._win32",
        # The Windows tray.py module is Windows-only and pulls in winreg etc.
        # Drop it from the build entirely so static analysis doesn't follow it.
        "--exclude-module", "tray",
        ENTRY,
    ]
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def _clean_dist():
    # Wipe dist/ so stale binaries from previous builds (e.g. an old `.exe`
    # named output that PyInstaller --noconfirm won't touch) don't end up
    # in the release tarball.
    dist_dir = os.path.join(PROJECT_DIR, "dist")
    if os.path.isdir(dist_dir):
        shutil.rmtree(dist_dir, ignore_errors=True)


def _cleanup():
    # Trim PyInstaller's intermediate artifacts (kept in /tmp); keep dist/ and .spec.
    build_dir = "/tmp/SteamlessKeyboard-build"
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)


def _write_dist_icon_and_desktop():
    """Drop a portable PNG + .desktop launcher into dist/ for the release zip.

    Linux ELF binaries can't carry an embedded icon (unlike Windows EXEs).
    The portable replacement is a .desktop file whose Exec uses `%k` (the
    runtime path to the .desktop itself) so the launcher works from any
    extraction location. Icon= uses a bare XDG name — tray_linux.py's
    _install_xdg_icon() drops SteamlessKeyboard.png into
    ~/.local/share/icons/ on first run, after which the icon resolves
    everywhere on the desktop."""
    dist_dir = os.path.join(PROJECT_DIR, "dist")

    # Convert the largest .ico frame to PNG so the bundled icon is a plain
    # image file (also lets users manually copy it to ~/.local/share/icons/
    # before the first launch if they want the .desktop icon to show in
    # their file manager right away).
    ico_path = os.path.join(PROJECT_DIR, "data", "images", "app_icon.ico")
    png_path = os.path.join(dist_dir, "SteamlessKeyboard.png")
    img = Image.open(ico_path)
    sizes = sorted(img.info.get("sizes", set()), key=lambda s: s[0], reverse=True)
    if sizes:
        img.size = sizes[0]
        img.load()
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    img.save(png_path, "PNG")
    print(f"icon:    {png_path}")

    # Portable .desktop. `%k` expands to the .desktop's own path at launch,
    # so `dirname` yields the folder it sits in — same folder as the binary
    # in our release zip. Quoting handles spaces in the extraction path.
    desktop_path = os.path.join(dist_dir, "SteamlessKeyboard.desktop")
    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=SteamlessKeyboard\n"
        f"Exec=sh -c 'exec \"$(dirname \"%k\")\"/{OUTPUT_NAME}'\n"
        "Icon=SteamlessKeyboard\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )
    with open(desktop_path, "w", encoding="utf-8") as f:
        f.write(contents)
    os.chmod(desktop_path, 0o755)
    print(f"desktop: {desktop_path}")


def _bundle_license():
    """Copy the repo LICENSE into dist/ so it ends up in the release tarball."""
    src = os.path.join(REPO_DIR, "LICENSE")
    if not os.path.isfile(src):
        return
    dst = os.path.join(PROJECT_DIR, "dist", "LICENSE")
    shutil.copy2(src, dst)
    print(f"license: {dst}")


def _make_tarball():
    """Package dist/ contents into a .tar.gz suitable for a GitHub Release.

    The tarball extracts to a top-level `SteamlessKeyboard/` folder
    (vs. tar-bombing the user's cwd) — standard Linux convention. Inside
    that folder the user finds the binary, icon, .desktop, and LICENSE
    ready to run from anywhere.

    The tarball is written into dist/ alongside the source files. Snapshot
    the file list *before* opening the tarball for write so it never tries
    to include itself."""
    dist_dir = os.path.join(PROJECT_DIR, "dist")
    sources = sorted(os.listdir(dist_dir))
    tar_path = os.path.join(dist_dir, TARBALL_NAME)
    with tarfile.open(tar_path, "w:gz") as tar:
        for name in sources:
            full = os.path.join(dist_dir, name)
            tar.add(full, arcname=os.path.join("SteamlessKeyboard", name))
    print(f"tarball: {tar_path}")


def main():
    _check_platform()
    _clean_dist()
    _run_pyinstaller()
    _cleanup()
    _write_dist_icon_and_desktop()
    _bundle_license()
    _make_tarball()
    out = os.path.join(PROJECT_DIR, "dist", OUTPUT_NAME)
    print(f"\nbuilt: {out}")
    print(f"test it:    ./dist/{OUTPUT_NAME}             # tray + chord + hotkey")
    print(f"            ./dist/{OUTPUT_NAME} --no-tray   # headless (terminal-only)")
    print(f"release:    upload dist/{TARBALL_NAME} to your GitHub Releases page")


if __name__ == "__main__":
    main()
