import os
import glob
from PIL import Image, ImageDraw, ImageFont
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM

SRC = r"C:\Users\Administrator\Desktop\adusk-master\scripts\_jsx_svgs"

# Pick SVGs around svg_0010 (the smiley) and a broader middle range to scan.
# Also include some specific ones we haven't checked.
indices = list(range(130, 230))
paths = []
for i in indices:
    p = os.path.join(SRC, f"svg_{i:04d}.svg")
    if os.path.exists(p):
        paths.append(p)

cell = 150
gap = 6
label_h = 22
cols = 8

rows = (len(paths) + cols - 1) // cols
W = cols * cell + (cols + 1) * gap
H = rows * (cell + label_h) + (rows + 1) * gap
grid = Image.new("RGB", (W, H), (35, 38, 46))
draw = ImageDraw.Draw(grid)
try:
    font = ImageFont.truetype(r"C:\Windows\Fonts\seguisb.ttf", 12)
except Exception:
    font = ImageFont.load_default()

for i, p in enumerate(paths):
    r, c = divmod(i, cols)
    x = gap + c * (cell + gap)
    y = gap + r * (cell + label_h + gap)
    try:
        d = svg2rlg(p)
        if d.width and d.height:
            scale = cell / max(d.width, d.height)
            d.width *= scale
            d.height *= scale
            d.scale(scale, scale)
            im = renderPM.drawToPIL(d, dpi=72, bg=0x0e141b)
            if im.size != (cell, cell):
                im = im.resize((cell, cell), Image.LANCZOS)
            grid.paste(im.convert("RGB"), (x, y))
    except Exception:
        pass
    name = os.path.splitext(os.path.basename(p))[0].split("_")[1]
    draw.text((x, y + cell + 2), name, fill=(220, 220, 220), font=font)

out_path = r"C:\Users\Administrator\Desktop\adusk-master\scripts\_jsx_specific_grid.png"
grid.save(out_path)
print(out_path)
