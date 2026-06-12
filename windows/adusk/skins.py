"""Steam on-screen-keyboard skins.

Each skin is one of Steam's official OSK theme CSS files, bundled under
data/skins/<Name>.css. We don't render CSS — Steam's web keyboard and ours
both draw the keys procedurally and only tint them — so a "skin" here is just
the handful of color variables Steam exposes, mapped onto the flat SDL palette
in screen.Screen.

Steam's CSS uses a few forms we have to flatten to opaque RGB:
  * #rrggbb / #rgb               → straight RGB
  * rgb()/rgba(r,g,b,a)          → RGB, alpha composited over the skin's own
                                    background color (so translucent tints land
                                    on-theme instead of washing out)
  * linear-gradient(angle, ...)  → the first color stop (our fills are flat)
  * transparent                  → treated as "unset" (caller keeps its default)
"""

import os
import re
import threading

from adusk import resources
from adusk.color import Color

DEFAULT_SKIN = "DefaultTheme"

# Display order for the tray submenu. DefaultTheme first (it matches the
# built-in palette). Any bundled .css not listed here is appended
# alphabetically by available_skins().
_SKIN_ORDER = [
    "DefaultTheme", "Digital", "NightShift", "Ruby",
    "Grape", "Cerulean", "Seafoam", "Pumpkin",
]

# Active skin name, set by the tray from settings.json at startup and on each
# menu change; read by screen.Screen when the keyboard opens. `_generation`
# bumps on every actual change so an already-open keyboard can detect it and
# re-skin live (see screen.Screen.maybe_reload_skin) with a single int compare
# per frame — no lock on that hot path.
_active_lock = threading.Lock()
_active_skin = DEFAULT_SKIN
_generation = 0
# Transparency (tray "Keyboard Skin" → "Transparent" submenu). `_transparent`
# is on/off; `_transparency_scale` is a GLOBAL opacity multiplier applied to
# every transparent-mode alpha (fills, icons, text, outlines) so the dialed-in
# ratios are preserved and only the overall level changes. Shares `_generation`
# so an open keyboard picks up changes live via maybe_reload_skin.
_transparent = False
_transparency_scale = 1.0
# Submenu levels → (enabled, opacity scale). "low" = 30% more opaque, "high" =
# 30% more transparent, than the tuned "medium" baseline.
_TRANSPARENCY_LEVELS = {
    "off":    (False, 1.0),
    "low":    (True, 1.3),
    "medium": (True, 1.0),
    "high":   (True, 0.7),
}


def set_active_skin(name):
    global _active_skin, _generation
    name = name or DEFAULT_SKIN
    with _active_lock:
        if name != _active_skin:
            _active_skin = name
            _generation += 1


def set_transparency(level):
    """Apply a transparency level by name ("off"/"low"/"medium"/"high")."""
    global _transparent, _transparency_scale, _generation
    on, scale = _TRANSPARENCY_LEVELS.get(level, (False, 1.0))
    with _active_lock:
        if on != _transparent or scale != _transparency_scale:
            _transparent = on
            _transparency_scale = scale
            _generation += 1


def is_transparent():
    with _active_lock:
        return _transparent


def get_transparency_scale():
    """Global opacity multiplier for transparent-mode rendering (1.0 baseline)."""
    with _active_lock:
        return _transparency_scale


def get_active_skin():
    with _active_lock:
        return _active_skin


def get_generation():
    """Monotonic counter bumped on each real skin change. Plain int read
    (atomic under the GIL); a one-frame-stale value just defers a live switch
    by a single frame, so the render loop can poll this essentially for free."""
    return _generation


def _skins_dir():
    """Directory holding the bundled skin CSS, or None. Resolved by locating
    the always-present DefaultTheme.css and taking its parent."""
    p = resources.find_data_resource("skins/" + DEFAULT_SKIN + ".css")
    return os.path.dirname(p) if p else None


def available_skins():
    """Bundled skin names (no .css), in display order, that actually exist."""
    out = [n for n in _SKIN_ORDER
           if resources.find_data_resource("skins/" + n + ".css")]
    d = _skins_dir()
    if d and os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".css"):
                nm = fn[:-4]
                if nm not in out:
                    out.append(nm)
    return out or [DEFAULT_SKIN]


# --- CSS color parsing ------------------------------------------------------

# `--var-name: value` declarations. Steam's themes put every color variable in
# the first `.SkinName{ ... }` rule; later rules are img filters / borders with
# no --vars, so a whole-file scan with first-wins is safe.
_VAR_RE = re.compile(r"--([a-z0-9-]+)\s*:\s*([^;}]+)")


def _clamp8(v):
    return max(0, min(255, int(round(v))))


def _split_top_commas(s):
    """Split on commas that are not inside parentheses (so rgba(...) stays
    intact when splitting a gradient's argument list)."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _parse_color(value, base=None):
    """Flatten a CSS color string to an opaque (r, g, b) tuple, or None.

    `base` is the (r, g, b) to composite rgba() over when alpha < 1."""
    v = value.strip()
    if not v or v == "transparent":
        return None
    if v.startswith("linear-gradient"):
        inner = v[v.find("(") + 1:]
        for part in _split_top_commas(inner):
            c = _parse_color(part.strip(), base)
            if c is not None:  # first real color stop (skip the angle/side)
                return c
        return None
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) >= 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                return None
        return None
    if v.startswith("rgb"):
        nums = re.findall(r"[\d.]+", v)
        if len(nums) >= 3:
            r, g, b = float(nums[0]), float(nums[1]), float(nums[2])
            a = float(nums[3]) if len(nums) >= 4 else 1.0
            if a < 1.0 and base is not None:
                br, bg, bb = base
                r = r * a + br * (1 - a)
                g = g * a + bg * (1 - a)
                b = b * a + bb * (1 - a)
            return (_clamp8(r), _clamp8(g), _clamp8(b))
        return None
    return None


def _parse_vars(css_text):
    vars = {}
    for m in _VAR_RE.finditer(css_text):
        name = m.group(1).strip()
        if name not in vars:  # first declaration wins
            vars[name] = m.group(2).strip()
    return vars


def _blend(c1, c2, t):
    """Linear blend from c1 toward c2 by fraction t (0..1)."""
    return Color(_clamp8(c1.r + (c2.r - c1.r) * t),
                 _clamp8(c1.g + (c2.g - c1.g) * t),
                 _clamp8(c1.b + (c2.b - c1.b) * t))


def _luminance(c):
    return 0.299 * c.r + 0.587 * c.g + 0.114 * c.b


def _too_close(c1, c2, thresh=48):
    """True if two fills are so close that painting one over the other gives no
    visible state change (sum of per-channel abs differences under thresh)."""
    return (abs(c1.r - c2.r) + abs(c1.g - c2.g) + abs(c1.b - c2.b)) < thresh


def _press_shade(hover, idle):
    """A pressed (CLICK) fill clearly distinct from BOTH the hover and idle
    fills: blend the hover color away from idle — toward white when idle is the
    darker of the two, else toward black — so a press visibly flashes instead of
    looking identical to the hovered (or idle) key."""
    target = _WHITE if _luminance(idle) <= _luminance(hover) else (0, 0, 0)
    return _blend(hover, Color(*target), 0.45)


# Fallbacks mirror screen.Screen's built-in Big-Picture palette so a missing or
# malformed variable degrades to the stock look rather than crashing.
_WHITE = (0xFF, 0xFF, 0xFF)


def load_palette(name):
    """Return a dict of role -> sdl2.ext.Color for skin `name`, or None when
    the CSS can't be found/parsed (caller keeps its built-in defaults).

    Roles: bg, key_inactive, key_hover, key_click, text_inactive, text_hover,
    modifier, shadow, highlight."""
    path = resources.find_data_resource("skins/" + name + ".css")
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            css = f.read()
    except OSError:
        return None
    v = _parse_vars(css)
    if not v:
        return None

    def col(*keys, default, base=None):
        for k in keys:
            if k in v:
                c = _parse_color(v[k], base=base)
                if c is not None:
                    return Color(*c)
        return Color(*default)

    bg = col("background-color", default=(0x23, 0x26, 0x2E))
    base = (bg.r, bg.g, bg.b)

    key_inactive = col("key-background-color", base=base, default=(0x0E, 0x14, 0x1B))
    key_hover = col("key-focused-background-color", base=base, default=_WHITE)
    # CLICK state = a held/pressed key (held modifier highlight, touchpad click).
    # Steam styles that as "toggled on", so use the toggle-on colors — they're
    # designed to contrast each other, unlike --key-color vs the accent which
    # coincide in some skins (Digital green-on-green) or where the accent equals
    # the modifier idle fill (TotallyTubular pink-on-pink → no visible press).
    key_click = col("key-toggleon-background-color", "key-pointer-background-color",
                    base=base, default=(0x1A, 0x9F, 0xFF))
    text_click = col("key-toggleon-color", "key-focused-color", base=base, default=_WHITE)
    text_inactive = col("key-color", base=base, default=(0xEE, 0xF3, 0xF7))
    # Modifier keys (Tab/Caps/Shift/Enter/...) get their own idle text color.
    text_modifier = col("key-meta-color", "key-color", base=base, default=(0xEE, 0xF3, 0xF7))
    text_hover = col("key-focused-color", base=base, default=(0x0E, 0x14, 0x1B))
    modifier = col("key-meta-background-color", "key-shift-background-color",
                   base=base, default=(0x00, 0x00, 0x00))
    shadow = col("key-shift-label-color", base=base, default=(0x7B, 0x7E, 0x82))

    # A few skins (e.g. Digital, green-on-green) define the toggle-on / pressed
    # fill IDENTICAL to the hover fill, so a pressed key looks no different from
    # a hovered one — Steam shows the press as a glow our flat renderer can't
    # draw. When the pressed fill is indistinguishable from hover, derive a
    # distinct one so a press visibly flashes (text_click already contrasts it).
    if _too_close(key_click, key_hover):
        key_click = _press_shade(key_hover, key_inactive)

    return {
        "bg": bg,
        "key_inactive": key_inactive,
        "key_hover": key_hover,
        "key_click": key_click,
        "text_inactive": text_inactive,
        "text_modifier": text_modifier,
        "text_hover": text_hover,
        "text_click": text_click,
        "modifier": modifier,
        "shadow": shadow,
        # Subtle per-skin top-edge bevel: nudge the idle key color toward white.
        "highlight": _blend(key_inactive, Color(*_WHITE), 0.22),
    }
