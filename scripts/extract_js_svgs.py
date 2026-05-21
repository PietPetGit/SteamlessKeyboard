"""Walk the Steam Big Picture JS chunk, find every JSX `("svg", {...})` call,
reconstruct it into a standalone SVG file, then rasterize each to PNG on a
dark backdrop so we can eyeball-pick the smiley + keyboard icons."""
import os
import re
import json

JS = r"C:\Program Files (x86)\Steam\steamui\chunk~2dcc5aaf7.js"
OUT_DIR = os.path.join(os.path.dirname(__file__), "_jsx_svgs")
os.makedirs(OUT_DIR, exist_ok=True)

with open(JS, "r", encoding="utf-8", errors="replace") as f:
    s = f.read()

# Match each `jsx(s)?("svg", ...` block to its closing `)`.
def find_blocks():
    # Pattern start: jsxs?(\)\("svg",
    starts = []
    for m in re.finditer(r'jsxs?\)\("svg",\s*\{', s):
        starts.append(m.end() - 1)  # position of `{`
    return starts


def balance(start):
    """Return (start_of_block, end_of_block) walking through balanced braces."""
    depth = 0
    in_str = None
    i = start
    while i < len(s):
        ch = s[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        else:
            if ch in '"\'':
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    return start, len(s)


count = 0
extracted = []
for blk_start in find_blocks():
    blk_start, blk_end = balance(blk_start)
    txt = s[blk_start:blk_end]
    # Look for path d="..." values
    paths = re.findall(r'\bd:"([^"]+)"', txt)
    if not paths:
        # Could be polyline / circle / rect only
        pass
    vb = re.search(r'\bviewBox:"([^"]+)"', txt)
    if not vb:
        continue
    viewBox = vb.group(1)
    # Width/height optional
    wm = re.search(r'\bwidth:"([^"]+)"', txt)
    hm = re.search(r'\bheight:"([^"]+)"', txt)
    w = wm.group(1) if wm else None
    h = hm.group(1) if hm else None
    # Extract all child element specs
    # Pattern: jsxs?(\)\(\"(\w+)\",\{...\}) for child elements
    children_str = []
    for cm in re.finditer(r'jsxs?\)\("(\w+)",\s*\{', txt):
        if cm.start() == 0:
            continue
        child_start, child_end = balance(cm.end() - 1)
        cprops_raw = txt[child_start:child_end]
        ctag = cm.group(1)
        if ctag in ("svg",):
            continue
        # Parse simple key:"value" pairs in cprops_raw
        attrs = {}
        for k, v in re.findall(r'\b([A-Za-z_][\w-]*)\s*:\s*"([^"]*)"', cprops_raw):
            attrs[k] = v
        # Skip if no useful attrs
        if not attrs:
            continue
        a_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        children_str.append(f"<{ctag} {a_str}/>")

    if not children_str:
        continue

    count += 1
    fname = f"svg_{count:04d}.svg"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewBox}"'
        + (f' width="{w}"' if w else "")
        + (f' height="{h}"' if h else "")
        + ' fill="#fff">'
        + "".join(children_str)
        + "</svg>"
    )
    with open(os.path.join(OUT_DIR, fname), "w", encoding="utf-8") as fo:
        fo.write(svg)
    extracted.append((fname, viewBox, blk_start))

print(f"Wrote {count} SVGs to {OUT_DIR}")
