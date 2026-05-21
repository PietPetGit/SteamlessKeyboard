import os
import glob
from PIL import Image

SRC = r"C:\Program Files (x86)\Steam\tenfoot\resource\images\library\controller\binding_icons"
OUT = r"C:\Users\Administrator\Desktop\adusk-master\scripts\_tenfoot_input"
os.makedirs(OUT, exist_ok=True)

for p in sorted(glob.glob(os.path.join(SRC, "ghost_080_input_*.png"))):
    im = Image.open(p).convert("RGBA")
    bg = Image.new("RGB", im.size, (14, 20, 27))
    bg.paste(im, (0, 0), im)
    bg.save(os.path.join(OUT, os.path.basename(p)))
print("done")
