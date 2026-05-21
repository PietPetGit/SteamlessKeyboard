"""Build script for the portable Steam Controller Keyboard EXE.

Run:
    python build.py

Produces `dist/Steam Controller Keyboard.exe`. Generates a multi-resolution
.ico from `data/images/glyphs/glyph_keyboard.png` and bundles the data/
folder as PyInstaller datas. The output is a single-file, no-console exe
suitable for dropping anywhere on a user's machine.
"""

import glob
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image
import sdl2dll
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY = "tray.py"
OUTPUT_NAME = "SteamlessKeyboard"
SVG_SRC = os.path.join(PROJECT_DIR, "keyboard-full2.svg")
APP_ICON_PNG = os.path.join(PROJECT_DIR, "data", "images", "app_icon.png")
ICON_OUT = os.path.join(PROJECT_DIR, "_build_icon.ico")
DATA_DIR = os.path.join(PROJECT_DIR, "data")


def _flatten_gradient_strokes(svg_text):
    """svglib can't rasterize `stroke="url(#gradient)"` references — it
    draws nothing. Rewrite any gradient-stroke usage to a solid hex color
    that's the midpoint between the gradient's first and last stops.
    Gives a flat but visible icon; close enough at small render sizes."""
    grads = {}
    for gm in re.finditer(
            r'<linearGradient[^>]*\bid="([^"]+)"[^>]*>(.*?)</linearGradient>',
            svg_text, re.DOTALL):
        gid, body = gm.group(1), gm.group(2)
        stops = re.findall(r'stop-color="(#[0-9A-Fa-f]{6})"', body)
        if not stops:
            continue
        first = stops[0]
        last = stops[-1]
        r1, g1, b1 = int(first[1:3], 16), int(first[3:5], 16), int(first[5:7], 16)
        r2, g2, b2 = int(last[1:3], 16), int(last[3:5], 16), int(last[5:7], 16)
        mid = "#{:02X}{:02X}{:02X}".format(
            (r1 + r2) // 2, (g1 + g2) // 2, (b1 + b2) // 2)
        grads[gid] = mid

    for gid, mid in grads.items():
        svg_text = re.sub(
            r'url\(#' + re.escape(gid) + r'\)', mid, svg_text)
    return svg_text


def _render_svg(target_size):
    """Rasterize the SVG to a square RGBA PIL image. reportlab paints a
    white background by default, so we knock the white back out afterwards
    to recover transparency. Gradients are flattened to a solid midpoint
    color so svglib actually draws them."""
    if not os.path.isfile(SVG_SRC):
        raise SystemExit(f"svg source not found: {SVG_SRC}")
    with open(SVG_SRC, "r", encoding="utf-8") as f:
        svg_text = f.read()
    svg_text = _flatten_gradient_strokes(svg_text)

    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False, mode="w",
                                      encoding="utf-8") as tmp:
        tmp.write(svg_text)
        tmp_path = tmp.name
    try:
        drawing = svg2rlg(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    scale = target_size / max(drawing.width, drawing.height)
    drawing.width *= scale
    drawing.height *= scale
    drawing.scale(scale, scale)
    raw = renderPM.drawToString(drawing, fmt="PNG")
    img = Image.open(io.BytesIO(raw)).convert("RGBA")

    # Make near-white pixels fully transparent so the strokes stand alone.
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            lo = min(r, g, b)
            if lo >= 245:
                px[x, y] = (r, g, b, 0)
            elif lo >= 200:
                t = (lo - 200) / 45.0
                px[x, y] = (r, g, b, int(round(a * (1.0 - t))))
    return img


def _make_ico():
    """Render the SVG once at 256×256, save it as the runtime tray icon, and
    save the same image as a multi-size .ico for the EXE icon."""
    img = _render_svg(256)
    # Bundled tray icon (loaded by tray.py at runtime).
    os.makedirs(os.path.dirname(APP_ICON_PNG), exist_ok=True)
    img.save(APP_ICON_PNG, format="PNG")
    print(f"tray icon: {APP_ICON_PNG}")
    # EXE icon (used by PyInstaller --icon).
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
             (128, 128), (256, 256)]
    img.save(ICON_OUT, format="ICO", sizes=sizes)
    print(f"exe icon: {ICON_OUT}")


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
        "--icon", ICON_OUT,
        "--add-data", f"{DATA_DIR};data",
        # pystray uses platform-specific backends loaded at runtime; pyinstaller
        # doesn't always pick them up unless we name them explicitly.
        "--hidden-import", "pystray._win32",
        "--hidden-import", "pynput.keyboard._win32",
        "--hidden-import", "pynput.mouse._win32",
        "--hidden-import", "PIL._tkinter_finder",
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
    if os.path.isfile(ICON_OUT):
        os.remove(ICON_OUT)


def main():
    _make_ico()
    _run_pyinstaller()
    _cleanup()
    out = os.path.join(PROJECT_DIR, "dist", f"{OUTPUT_NAME}.exe")
    print(f"\nbuilt: {out}")


if __name__ == "__main__":
    main()
