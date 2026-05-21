import os
import glob
from PIL import Image

SRC = r"C:\Program Files (x86)\Steam\steamui\images\controller"
OUT = r"C:\Users\Administrator\Desktop\adusk-master\scripts\_ghost_previews"
os.makedirs(OUT, exist_ok=True)

# Just the input category — these are likely keyboard / text input related
for p in sorted(glob.glob(os.path.join(SRC, "ghost_080_input_*.png"))):
    im = Image.open(p).convert("RGBA")
    bg = Image.new("RGB", im.size, (14, 20, 27))
    bg.paste(im, (0, 0), im)
    bg.save(os.path.join(OUT, os.path.basename(p)))
    print(os.path.basename(p), im.size)
print("done")
