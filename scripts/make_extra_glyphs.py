"""Rasterize Steam's actual inline-SVG smiley and keyboard icons (extracted
from steamui's JS chunk) into white-on-transparent PNGs that match adusk's
existing knockout glyphs.
"""
import os
from PIL import Image
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM

HERE = os.path.dirname(__file__)
OUT_DIR = os.path.abspath(os.path.join(HERE, "..", "data", "images", "glyphs"))
SVG_DIR = os.path.join(HERE, "_jsx_svgs")

# Inline SVGs extracted from steamui/chunk~2dcc5aaf7.js:
#  svg_0010 → smiley face (AddReactionIcon component)
#  svg_0193 → keyboard icon (return-arrow nub + keyed rectangle body)
SOURCES = {
    "glyph_smiley.png":   os.path.join(SVG_DIR, "svg_0010.svg"),
    "glyph_keyboard.png": os.path.join(SVG_DIR, "svg_0194.svg"),
}


def rasterize(svg_path, out_path, target_size=240):
    # svglib's renderPM doesn't grok `currentColor`; bake it to white.
    with open(svg_path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = raw.replace('"currentColor"', '"#ffffff"').replace("'currentColor'", "'#ffffff'")
    raw = raw.replace('fill="freeze"', 'fill="#ffffff"')
    import io
    import tempfile
    tmp = tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False, encoding="utf-8")
    tmp.write(raw)
    tmp.close()
    try:
        d = svg2rlg(tmp.name)
    finally:
        os.unlink(tmp.name)
    scale = target_size / max(d.width, d.height)
    d.width *= scale
    d.height *= scale
    d.scale(scale, scale)
    pil_img = renderPM.drawToPIL(d, dpi=72, bg=0x000000)
    img = pil_img.convert("RGBA")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, _a = px[x, y]
            lum = max(r, g, b)
            px[x, y] = (255, 255, 255, lum)
    img.save(out_path)
    print("Wrote", out_path)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for fname, src in SOURCES.items():
        rasterize(src, os.path.join(OUT_DIR, fname))


if __name__ == "__main__":
    main()
