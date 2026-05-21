import os
import glob
from PIL import Image, ImageDraw, ImageFont

SRC = r"C:\Users\Administrator\Desktop\adusk-master\scripts\_tenfoot_input"
OUT = r"C:\Users\Administrator\Desktop\adusk-master\scripts\_tenfoot_input_grid.png"

paths = sorted(glob.glob(os.path.join(SRC, "*.png")))
cell = 128
gap = 8
label_h = 18
cols = 6
rows = (len(paths) + cols - 1) // cols

W = cols * cell + (cols + 1) * gap
H = rows * (cell + label_h) + (rows + 1) * gap
grid = Image.new("RGB", (W, H), (14, 20, 27))
draw = ImageDraw.Draw(grid)
font = None
try:
    font = ImageFont.truetype(r"C:\Windows\Fonts\seguisb.ttf", 12)
except Exception:
    font = ImageFont.load_default()

for i, p in enumerate(paths):
    r, c = divmod(i, cols)
    x = gap + c * (cell + gap)
    y = gap + r * (cell + label_h + gap)
    im = Image.open(p).convert("RGBA").resize((cell, cell), Image.LANCZOS)
    grid.paste(im, (x, y))
    name = os.path.splitext(os.path.basename(p))[0].replace("ghost_080_input_", "")
    draw.text((x, y + cell + 2), name, fill=(220, 220, 220), font=font)

grid.save(OUT)
print(OUT)
