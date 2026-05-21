import os
from PIL import Image

CANDIDATES = [
    r"C:\Program Files (x86)\Steam\graphics\icon_emoticon.png",
    r"C:\Program Files (x86)\Steam\graphics\icon_emoticon@2x.png",
    r"C:\Program Files (x86)\Steam\graphics\icon_emoticon_hover.png",
    r"C:\Program Files (x86)\Steam\graphics\icon_emoticon_hover@2x.png",
    r"C:\Program Files (x86)\Steam\steamui\images\osk2.png",
    r"C:\Program Files (x86)\Steam\steamui\images\interstitial_controller_osk.png",
]

OUT = r"C:\Users\Administrator\Desktop\adusk-master\scripts\_previews"
os.makedirs(OUT, exist_ok=True)

for p in CANDIDATES:
    im = Image.open(p).convert("RGBA")
    bg = Image.new("RGB", im.size, (14, 20, 27))
    bg.paste(im, (0, 0), im)
    bg.save(os.path.join(OUT, os.path.basename(p)))
    print(os.path.basename(p), im.size, im.getextrema())
