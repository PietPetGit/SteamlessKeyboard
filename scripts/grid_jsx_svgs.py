import os
import glob
from PIL import Image, ImageDraw, ImageFont
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM

SRC = r"C:\Users\Administrator\Desktop\adusk-master\scripts\_jsx_svgs"
OUT_DIR = r"C:\Users\Administrator\Desktop\adusk-master\scripts\_jsx_pngs"
os.makedirs(OUT_DIR, exist_ok=True)

cell = 140
gap = 6
label_h = 20
cols = 8

paths = sorted(glob.glob(os.path.join(SRC, "*.svg")))


def rasterize(svg_path, size):
    try:
        d = svg2rlg(svg_path)
    except Exception as e:
        return None
    if not d.width or not d.height:
        return None
    scale = size / max(d.width, d.height)
    d.width *= scale
    d.height *= scale
    d.scale(scale, scale)
    try:
        return renderPM.drawToPIL(d, dpi=72, bg=0x0e141b)
    except Exception:
        return None


rows = (len(paths) + cols - 1) // cols
W = cols * cell + (cols + 1) * gap
H = rows * (cell + label_h) + (rows + 1) * gap
grid = Image.new("RGB", (W, H), (35, 38, 46))
draw = ImageDraw.Draw(grid)
try:
    font = ImageFont.truetype(r"C:\Windows\Fonts\seguisb.ttf", 14)
except Exception:
    font = ImageFont.load_default()

ok = 0
for i, p in enumerate(paths):
    r, c = divmod(i, cols)
    x = gap + c * (cell + gap)
    y = gap + r * (cell + label_h + gap)
    im = rasterize(p, cell)
    if im is None:
        continue
    if im.size != (cell, cell):
        im = im.resize((cell, cell), Image.LANCZOS)
    grid.paste(im.convert("RGB"), (x, y))
    name = os.path.splitext(os.path.basename(p))[0].split("_")[1]
    draw.text((x, y + cell + 2), name, fill=(220, 220, 220), font=font)
    ok += 1

out_path = os.path.join(os.path.dirname(SRC), "_jsx_svgs_grid.png")
grid.save(out_path)
print("rasterized:", ok, "of", len(paths))
print("grid:", out_path)
