import os
from PIL import Image

GLYPHS = r"C:\Users\Administrator\Desktop\adusk-master\data\images\glyphs"
for n in ("glyph_smiley.png", "glyph_keyboard.png"):
    p = os.path.join(GLYPHS, n)
    im = Image.open(p).convert("RGBA")
    bg = Image.new("RGB", im.size, (14, 20, 27))
    bg.paste(im, (0, 0), im)
    bg.save(p.replace(".png", "_dark.png"))
