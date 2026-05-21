import os
from PIL import Image

GLYPHS = os.path.join(os.path.dirname(__file__), "..", "data", "images", "glyphs")
GLYPHS = os.path.abspath(GLYPHS)

for n in ("glyph_keyboard.png", "glyph_smiley.png"):
    p = os.path.join(GLYPHS, n)
    im = Image.open(p).convert("RGBA")
    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 0:
                px[x, y] = (255, 255, 255, a)
    im.save(p)
    bg = Image.new("RGB", im.size, (14, 20, 27))
    bg.paste(im, (0, 0), im)
    bg.save(p.replace(".png", "_preview.png"))

print("done")
