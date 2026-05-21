"""Pull every inline `data:image/svg+xml,...` from the Steam Big Picture CSS,
inspect what's in each, and write them out so we can pick the smiley + keyboard glyphs.
"""
import os
import re
import urllib.parse

CSS_PATH = r"C:\Program Files (x86)\Steam\steamui\css\chunk~2dcc5aaf7.css"
OUT_DIR = os.path.join(os.path.dirname(__file__), "_steamui_svgs")
os.makedirs(OUT_DIR, exist_ok=True)

with open(CSS_PATH, "r", encoding="utf-8") as f:
    css = f.read()

# Capture between url(" and ") OR url(' and ') OR url(...)
pattern = re.compile(r"url\(([^)]+)\)")
count = 0
for i, m in enumerate(pattern.finditer(css)):
    raw = m.group(1).strip("'\" ")
    if not raw.startswith("data:image/svg+xml"):
        continue
    count += 1
    # data:image/svg+xml;utf8,<svg ...>  OR  data:image/svg+xml;base64,...
    head, _, body = raw.partition(",")
    if "base64" in head:
        import base64
        svg = base64.b64decode(body).decode("utf-8", errors="replace")
    else:
        svg = urllib.parse.unquote(body)
    # Look at the offset in the file so we can correlate to the CSS rule.
    start_idx = m.start()
    # Walk back to find the selector — go to the previous '}' then forward to '{'
    prev_close = css.rfind("}", 0, start_idx)
    rule_start = prev_close + 1 if prev_close != -1 else 0
    rule_open = css.find("{", rule_start, start_idx)
    selector = css[rule_start:rule_open].strip()[:300] if rule_open != -1 else "<unknown>"
    fname = f"svg_{count:02d}.svg"
    path = os.path.join(OUT_DIR, fname)
    with open(path, "w", encoding="utf-8") as fo:
        fo.write(svg)
    print(f"[{fname}] selector: {selector}")
    print(f"  -> {path}")
print(f"Wrote {count} SVGs to {OUT_DIR}")
