import ctypes
import os
import sys
import time

import sdl3w as S
from PIL import Image as PILImage
from PIL import ImageChops as PILImageChops

from adusk import resources
from adusk import skins
from adusk import state
from adusk import utils
from adusk.color import Color


# FALLBACK font lookup only. The OSK normally uses the BUNDLED Selawik Semibold
# (an open SIL-OFL, Segoe-UI-metric-compatible font ≈ Steam Big Picture's
# keyboard look) — see Screen.__init__. These per-platform system fonts (Segoe
# UI / common Linux fonts) are tried only if the bundled font is somehow missing.
_FONT_CANDIDATES_WIN = [r"C:\Windows\Fonts\seguisb.ttf"]
_SYM_CANDIDATES_WIN = [r"C:\Windows\Fonts\seguisym.ttf"]
_FONT_CANDIDATES_LINUX = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
# DejaVu Sans covers the geometric-shape glyphs (◀ ▶ etc.) we'd otherwise
# pull from Segoe UI Symbol on Windows.
_SYM_CANDIDATES_LINUX = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
    "/usr/share/fonts/noto/NotoSansSymbols2-Regular.ttf",
]


def _first_existing(paths):
    for p in paths:
        if os.path.isfile(p):
            return p
    return None

width = 1286
height = 369

# Base ("Default") OSK dimensions for the tray "Keyboard Skin -> Size" submenu.
# `width`/`height` above hold the ACTIVE size — Screen.__init__ recomputes them
# from `_active_osk_size` on every construction, so "small"/"full" are derived
# from these base values (and, for "full", the current display).
_BASE_WIDTH = 1286
_BASE_HEIGHT = 369
# "Small" scales both dimensions down uniformly, for users who don't want the
# OSK to cover much of the screen.
_SMALL_SCALE = 0.7

_active_osk_size = "medium"


def set_osk_size(name):
    """Select the OSK window size ("small"/"medium"/"full") used by the NEXT
    Screen() construction. "medium" is the original fixed 1286x369 size;
    "small" scales it down; "full" stretches the width to span the primary
    display's usable bounds edge-to-edge (keeping the default height,
    recomputed at construction time, so it tracks the current display)."""
    global _active_osk_size
    _active_osk_size = name if name in ("small", "medium", "full") else "medium"


def get_osk_size():
    return _active_osk_size


def _compute_size(name):
    if name == "small":
        return (int(round(_BASE_WIDTH * _SMALL_SCALE)),
                int(round(_BASE_HEIGHT * _SMALL_SCALE)))
    if name == "full":
        bounds = S.SDL_Rect()
        disp = S.SDL_GetPrimaryDisplay()
        if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
            return bounds.w, _BASE_HEIGHT
        return _BASE_WIDTH, _BASE_HEIGHT
    return _BASE_WIDTH, _BASE_HEIGHT


class _FrameLimiter:
    """Pure-Python frame pacer replacing sdl2.sdlgfx's FPSManager (SDL3 ships
    no sdlgfx). Sleeps just enough to hold a target FPS; if a frame runs long
    it resyncs to "now" instead of trying to catch up, so a hitch can't spiral
    into a burst of zero-delay frames."""

    def __init__(self, fps):
        self.set_fps(fps)
        self._next = time.monotonic()

    def set_fps(self, fps):
        self._interval = 1.0 / fps

    def delay(self):
        self._next += self._interval
        now = time.monotonic()
        sleep_for = self._next - now
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            self._next = now


class _Font:
    """Minimal replacement for sdl2.ext.FontManager: opens a single TTF at a
    fixed point size and renders blended UTF-8 text to a fresh SDL_Surface
    (caller turns it into a texture and frees it)."""

    def __init__(self, path, size):
        self.font = S.TTF_OpenFont(path.encode("utf-8"), float(size))
        if not self.font:
            raise RuntimeError(
                "TTF_OpenFont failed for {!r}: {}".format(path, S.get_error()))

    def render_surface(self, text, color):
        if not text:
            return None
        col = S.SDL_Color(color.r, color.g, color.b, 255)
        # length 0 => NUL-terminated UTF-8.
        return S.TTF_RenderText_Blended(self.font, text.encode("utf-8"), 0, col)


class CoordFraction:
    @staticmethod
    def from_absolute(x, y):
        return CoordFraction(x / width, y / height)

    def __init__(self, x_fraction, y_fraction):
        self.x_fraction = x_fraction
        self.y_fraction = y_fraction

    def to_absolute(self):
        return self.x_fraction * width, self.y_fraction * height

    def update_absolute(self, x, y):
        self.x_fraction = x / width
        self.y_fraction = y / height


class Screen:
    # Steam Big Picture palette.
    bg_color = Color(0x23, 0x26, 0x2E)

    key_color = {
        state.InputState.INACTIVE: Color(0x0E, 0x14, 0x1B),
        state.InputState.HOVER: Color(0xFF, 0xFF, 0xFF),
        state.InputState.CLICK: Color(0x1A, 0x9F, 0xFF),
    }
    # Modifier keys (Tab, Caps, Shift, Enter, Backspace) use pure black when idle.
    modifier_idle_color = Color(0x00, 0x00, 0x00)
    # Text needs to flip dark when the key turns white on hover, otherwise
    # the label vanishes against the same-color background. The CLICK entry is
    # the held/pressed (toggle-on) highlight; it must contrast key_color[CLICK].
    text_color = {
        state.InputState.INACTIVE: Color(0xEE, 0xF3, 0xF7),
        state.InputState.HOVER: Color(0x0E, 0x14, 0x1B),
        state.InputState.CLICK: Color(0xEE, 0xF3, 0xF7),
    }
    # Idle text/glyph color on modifier keys (Steam's --key-meta-color); some
    # skins differ from the normal --key-color here (e.g. TotallyTubular).
    modifier_text_color = Color(0xEE, 0xF3, 0xF7)
    # Subtle top-edge highlight to fake a 3D bevel on each key.
    key_highlight_color = Color(0x4a, 0x5d, 0x70)
    # Color for the small "shadow" label that previews a key's shifted form
    # (also the Move-key label and the arrow keys' ▲▼ previews, via skin:shadow).
    # Fixed grey across all skins, not derived from --key-shift-label-color.
    shadow_label_color = Color(120, 123, 127)

    # Seconds for the Shift slide/fade transition on dual-state keys. Driven by
    # wall-clock time in render_vkb so it's frame-rate independent.
    _SHIFT_ANIM_DUR = 0.074

    # Transparent-mode text outline defaults (per-key overridable via the YAML
    # `outline_opacity` / `outline_px`). Opacity 0..1 (lower = finer); px is the
    # sub-pixel offset of the outline ring (larger = thicker).
    _OUTLINE_ALPHA = 0.40
    _OUTLINE_PX = 0.3

    def __init__(self):
        # Apply the tray-selected OSK size ("Keyboard Skin -> Size"). Updates
        # the module-level width/height (read by CoordFraction and vkb's key
        # layout) for THIS window. `_font_scale` scales font/glyph sizes by
        # the same ratio so "Small"/"Full Screen" stay proportional to the
        # original 1286x369 "Default" look instead of just changing the grid.
        global width, height
        width, height = _compute_size(_active_osk_size)
        self._font_scale = height / _BASE_HEIGHT

        # Apply the user-selected Steam OSK skin (overrides the built-in
        # palette class attributes with per-instance ones). Done first so the
        # opening clear() and every render use the skin colors.
        self._apply_skin(skins.get_active_skin())
        self._skin_generation = skins.get_generation()
        # Make sure SDL never auto-activates the window when it gets shown,
        # so opening the OSK doesn't steal focus from the user's target app
        # (e.g. a browser address bar / YouTube search field). "0" = do not
        # activate on show (the SDL3 successor to the old NO_ACTIVATION hint).
        S.SDL_SetHint(S.SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN, b"0")

        # Anchor the window to the bottom-center of the primary display's
        # usable area (i.e. above the taskbar).
        bounds = S.SDL_Rect()
        disp = S.SDL_GetPrimaryDisplay()
        if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
            win_x = bounds.x + max(0, (bounds.w - width) // 2)
            win_y = bounds.y + max(0, bounds.h - height)
        else:
            win_x = S.SDL_WINDOWPOS_CENTERED
            win_y = S.SDL_WINDOWPOS_CENTERED
        # SDL_WINDOW_HIDDEN lets us apply the WS_EX_NOACTIVATE bit BEFORE the
        # window is ever visible; otherwise the very first paint can steal
        # focus regardless of subsequent style changes. SDL3's SDL_CreateWindow
        # drops the x/y args, so the position is set separately while hidden.
        # Create transparency-capable (layered/composited) so the tray's
        # "Transparent" toggle can switch the OSK to a see-through background +
        # translucent keys live, without recreating the window. When the toggle
        # is off we just clear to the opaque background, so it looks identical to
        # an ordinary window. Fall back to an opaque window if the platform
        # rejects the transparent flag (transparency then simply unavailable).
        _base_flags = S.SDL_WINDOW_BORDERLESS | S.SDL_WINDOW_HIDDEN
        self.window = S.SDL_CreateWindow(
            b"", width, height, _base_flags | S.SDL_WINDOW_TRANSPARENT)
        if not self.window:
            self.window = S.SDL_CreateWindow(b"", width, height, _base_flags)
        if not self.window:
            raise RuntimeError("SDL_CreateWindow failed: " + S.get_error())
        S.SDL_SetWindowPosition(self.window, win_x, win_y)
        self.renderer = S.SDL_CreateRenderer(self.window, None)
        if not self.renderer:
            raise RuntimeError("SDL_CreateRenderer failed: " + S.get_error())
        # Blend so the alpha in glyph/text textures composites over the keys.
        S.SDL_SetRenderDrawBlendMode(self.renderer, S.SDL_BLENDMODE_BLEND)

        # Use Windows' Segoe UI Semibold (Steam Big Picture's keyboard font)
        # when available; otherwise try common Linux system fonts; fall back
        # to the bundled DejaVu as a last resort.
        win_candidates = _FONT_CANDIDATES_WIN if sys.platform == "win32" else []
        linux_candidates = _FONT_CANDIDATES_LINUX if sys.platform != "win32" else []
        # Use the BUNDLED Selawik Semibold — Microsoft's SIL-OFL, metric-compatible
        # Segoe UI substitute (so it looks like Steam's keyboard) — so the OSK font
        # is identical on every platform AND freely redistributable (Segoe UI and
        # Steam's Motiva Sans are proprietary and can't be shipped). System
        # Segoe / Linux fonts and bundled DejaVu stay as fallbacks only.
        font_path = (resources.find_data_resource("fonts/Selawik-Semibold.ttf")
                     or _first_existing(win_candidates + linux_candidates))
        if font_path is None:
            font_name = "fonts/DejaVuSansCondensed-Bold.ttf"
            font_path = resources.find_data_resource(font_name)
            assert font_path is not None, "Could not find font file `{}`!".format(font_name)
        print("Found font file at `{}`".format(font_path))
        self.font_manager = _Font(font_path, self._scaled(26))
        # Modifier keys (Tab, Caps, Shift, Enter, Backspace) wear a smaller label.
        self.font_manager_small = _Font(font_path, self._scaled(20))

        # Segoe UI Symbol covers geometric shapes (◀ ▶ etc.) that Segoe UI
        # Semibold draws as missing-glyph boxes. On Linux DejaVu Sans already
        # covers the same shapes, so the symbol font search points there.
        sym_win = _SYM_CANDIDATES_WIN if sys.platform == "win32" else []
        sym_linux = _SYM_CANDIDATES_LINUX if sys.platform != "win32" else []
        # Arrow-key shapes (◀ ▶ ▲ ▼): Selawik is a UI font and doesn't include
        # them, so use the bundled DejaVu (verified to cover them, and freely
        # redistributable). Identical on every platform.
        sym_path = (resources.find_data_resource("fonts/DejaVuSansCondensed-Bold.ttf")
                    or _first_existing(sym_win + sym_linux))
        if sym_path is None:
            sym_path = font_path
        self.font_manager_symbol = _Font(sym_path, self._scaled(26))
        self.font_manager_symbol_small = _Font(sym_path, self._scaled(20))
        # Shadow labels (small grey previews of a key's shifted variant).
        self.font_manager_shadow = _Font(font_path, self._scaled(21))
        self.font_manager_shadow_symbol = _Font(sym_path, self._scaled(21))

        # Cache for one-off custom-size fonts keyed by (font_name, size).
        self._font_paths = {"default": font_path, "symbol": sym_path}
        self._font_cache = {}

        # Cache of glyph PNGs (button hint icons) keyed by filename.
        self._glyph_textures = {}
        # NOTE: no SDL3_image — every glyph/skin PNG loads through Pillow and
        # uploads via SDL_CreateSurfaceFrom (see _load_glyph_texture).
        # Glyph cache resolution, scaled with the OSK size so "Full Screen"
        # icons stay crisp and "Small" ones aren't oversampled.
        self._glyph_cache_px = max(32, self._scaled(self._GLYPH_CACHE_PX))

        # 120 fps: halves the render-loop latency for touchpad-driven haptics
        # (hover-switch) and pointer rendering vs 60 fps, at some extra CPU.
        self._frame_limiter = _FrameLimiter(120)

        # Shift slide/fade animation: eased progress 0 (unshifted) → 1 (shifted),
        # advanced each frame in render_vkb. `None` timestamp = snap to the live
        # shift state on the first frame (no animation when the OSK first opens).
        self._shift_anim = 0.0
        self._shift_anim_t = None

        # Offscreen render target for the OSK OPEN animation (lazily created in
        # render_open_anim). The keyboard is drawn into it at full opacity, then
        # blitted to the window faded (alpha-mod) + clipped (reveal). None until
        # first used / if the GPU can't make a target texture (then no animation).
        self._anim_target = None

        # Transparency (tray "Keyboard Skin → Transparent" submenu). Cached here
        # and refreshed in maybe_reload_skin so an open keyboard switches live.
        # `_tscale` is the level's global opacity multiplier folded into every
        # transparent-mode alpha (text/icons/fills/outlines) so the dialed-in
        # ratios stay fixed and only the overall level changes.
        self._transparent = skins.is_transparent()
        self._tscale = skins.get_transparency_scale()
        # The font (readable text) is ALWAYS full opacity — the transparency
        # level never scales it; only fills, icons and outlines scale.
        self._text_alpha = 255
        self._icon_alpha = min(255, int(round(204 * self._tscale))) if self._transparent else 255

        self.clear()
        # NOTE: window stays SDL_WINDOW_HIDDEN; adusk.main() applies the
        # WS_EX_NOACTIVATE bit and then shows it via the Win32 no-activate
        # path. Showing it here would activate it.

    def _apply_skin(self, name):
        """Override the built-in color palette with the selected skin's colors.
        Sets instance attributes that shadow the Screen class defaults, so a
        missing/unparseable skin simply leaves the stock palette in place."""
        pal = skins.load_palette(name)
        if not pal:
            return
        self.bg_color = pal["bg"]
        self.key_color = {
            state.InputState.INACTIVE: pal["key_inactive"],
            state.InputState.HOVER: pal["key_hover"],
            state.InputState.CLICK: pal["key_click"],
        }
        self.modifier_idle_color = pal["modifier"]
        self.text_color = {
            state.InputState.INACTIVE: pal["text_inactive"],
            state.InputState.HOVER: pal["text_hover"],
            # CLICK = held/pressed highlight: the toggle-on text color, chosen
            # to contrast the toggle-on fill (key_color[CLICK]).
            state.InputState.CLICK: pal["text_click"],
        }
        self.modifier_text_color = pal["text_modifier"]
        self.key_highlight_color = pal["highlight"]
        # shadow_label_color is intentionally NOT skinned — it's a fixed grey
        # (see the class attribute) shared by all skins.

    def _resolve_skin_color(self, c):
        """Resolve a per-key color override (see vkb._decode_key_color): a
        Color passes through unchanged; a 'skin:ROLE' marker maps to the
        active skin's palette so the key follows skin changes instead of a
        frozen literal (e.g. the Move key uses 'skin:key' / 'skin:shadow')."""
        if not isinstance(c, str):
            return c
        role = c[5:] if c.startswith("skin:") else c
        if role == "bg":
            return self.bg_color
        if role == "accent":
            return self.key_color[state.InputState.CLICK]
        if role == "text":
            return self.text_color[state.InputState.INACTIVE]
        if role == "modifier":
            return self.modifier_idle_color
        if role == "shadow":
            return self.shadow_label_color
        # "key" and any unknown role fall back to the normal idle key color.
        return self.key_color[state.InputState.INACTIVE]

    def _scaled(self, px):
        """Scale a design-time pixel size (font point size, glyph cache
        resolution, ...) by `_font_scale` so "Small"/"Full Screen" keep the
        same proportions as the 1286x369 "Default" layout."""
        return max(1, int(round(px * self._font_scale)))

    def maybe_reload_skin(self):
        """Re-apply the palette if the tray changed the active skin since the
        last frame, so an open keyboard switches skins live. Runs on the render
        thread (called from adusk.main's loop), so the color swap never races
        the renderer. Cheap no-op otherwise — one int compare per frame."""
        gen = skins.get_generation()
        if gen != self._skin_generation:
            self._skin_generation = gen
            self._apply_skin(skins.get_active_skin())
            self._transparent = skins.is_transparent()
            self._tscale = skins.get_transparency_scale()
            self._text_alpha = 255  # font opacity never scales (always 100%)
            self._icon_alpha = min(255, int(round(204 * self._tscale))) if self._transparent else 255

    def clear(self):
        if self._transparent:
            # Erase to alpha 0 so the desktop shows through — the background
            # solid is removed entirely. RenderClear writes the draw color
            # (including alpha) directly, ignoring the blend mode.
            S.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 0)
        else:
            c = self.bg_color
            S.SDL_SetRenderDrawColor(self.renderer, c.r, c.g, c.b, 255)
        S.SDL_RenderClear(self.renderer)

    def delay(self):
        self._frame_limiter.delay()

    def set_framerate(self, fps):
        """Switch the frame cap at runtime — adusk's adaptive ACTIVE/IDLE FPS
        (fast while there's activity, low when the open keyboard is idle)."""
        self._frame_limiter.set_fps(fps)

    # Glyph cache target size. Source PNGs are 128–240 px but get drawn at
    # ~30–50 px on screen — an 8× one-step GPU downscale aliases hard even
    # with linear filtering. Pre-resampling to ~2× the typical draw size
    # with PIL/LANCZOS, then letting the GPU do the final tiny rescale,
    # matches the quality Steam's own keyboard renders at.
    _GLYPH_CACHE_PX = 96

    def _get_glyph(self, name):
        """Load (and cache) a controller-button glyph PNG by basename."""
        if name in self._glyph_textures:
            return self._glyph_textures[name]
        path = resources.find_data_resource("images/glyphs/" + name)
        tex = None
        size = (0, 0)
        if path is not None:
            tex, size = self._load_glyph_texture(path)
        else:
            print("Glyph not found: images/glyphs/{}".format(name))
        entry = (tex, size)
        self._glyph_textures[name] = entry
        return entry

    @staticmethod
    def _normalize_glyph(pil):
        """Glyphs are tinted with a multiply (SetTextureColorMod), which can
        only darken. A glyph that bakes in dark detail (e.g. sc_r2_md's black
        "R2" on a light button) therefore breaks on hover: the body darkens to
        match the tint and the already-black detail merges into it and vanishes.

        Fix such glyphs by flattening them to a white silhouette whose alpha
        tracks luminance — the dark detail drops to zero alpha and becomes a
        transparent cut-out that shows the key background through it, so it
        inverts correctly in every state (dark-on-light idle, light-on-dark
        hover). Pure white-on-transparent glyphs have no dark opaque pixels and
        are returned unchanged."""
        r, g, b, a = pil.split()
        lum = PILImage.merge("RGB", (r, g, b)).convert("L")
        dark = lum.point(lambda v: 255 if v < 64 else 0)
        opaque = a.point(lambda v: 255 if v > 128 else 0)
        dark_opaque = PILImageChops.multiply(dark, opaque).histogram()[255]
        if dark_opaque < 50:
            return pil
        # alpha' = alpha * luminance/255 → light body stays, dark detail cut out.
        new_alpha = PILImageChops.multiply(a, lum)
        white = PILImage.new("RGB", pil.size, (255, 255, 255))
        white.putalpha(new_alpha)
        return white

    def _load_glyph_texture(self, path):
        """LANCZOS-downsample the source PNG, then upload as an SDL texture
        with linear filtering for the final on-screen blit."""
        pil = self._normalize_glyph(PILImage.open(path).convert("RGBA"))
        if max(pil.size) > self._glyph_cache_px:
            pil.thumbnail(
                (self._glyph_cache_px, self._glyph_cache_px), PILImage.LANCZOS
            )
        w, h = pil.size
        data = pil.tobytes()
        # Keep `buf` alive until CreateTextureFromSurface copies the pixels —
        # SDL_CreateSurfaceFrom references the buffer, it does not own it.
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        # SDL_PIXELFORMAT_ABGR8888 matches PIL's RGBA byte order on
        # little-endian (red byte first in memory).
        surf = S.SDL_CreateSurfaceFrom(
            w, h, S.SDL_PIXELFORMAT_ABGR8888,
            ctypes.cast(buf, ctypes.c_void_p), w * 4)
        if not surf:
            return None, (0, 0)
        tex = S.SDL_CreateTextureFromSurface(self.renderer, surf)
        S.SDL_DestroySurface(surf)
        if tex:
            S.SDL_SetTextureScaleMode(tex, S.SDL_SCALEMODE_LINEAR)
            S.SDL_SetTextureBlendMode(tex, S.SDL_BLENDMODE_BLEND)
        return tex, (w, h)

    def _get_sized_font(self, font_name, size):
        size = self._scaled(size)
        key = (font_name, size)
        fm = self._font_cache.get(key)
        if fm is None:
            path = self._font_paths.get(font_name, self._font_paths["default"])
            fm = _Font(path, size)
            self._font_cache[key] = fm
        return fm

    def _make_text_texture(self, font_obj, text, color):
        """Render `text` with `font_obj` in `color` to a fresh texture,
        returning (texture, w, h) — the caller draws it then SDL_DestroyTexture's
        it — or (None, 0, 0). A new texture per frame mirrors the old
        FontManager path; the glyph/skin caches cover the expensive bits, while
        short label strings are cheap to re-rasterize."""
        surf = font_obj.render_surface(text, color)
        if not surf:
            return None, 0, 0
        w = surf.contents.w
        h = surf.contents.h
        tex = S.SDL_CreateTextureFromSurface(self.renderer, surf)
        S.SDL_DestroySurface(surf)
        if not tex:
            return None, 0, 0
        S.SDL_SetTextureBlendMode(tex, S.SDL_BLENDMODE_BLEND)
        return tex, w, h

    def render_key(self, txt, key, key_state, modifier=False, align="center", valign="center", glyph=None, font="default", text_color_override=None, bg_color_override=None, shadow_label=None, font_size=None, text_dx=0, shadow_font_size=None, dual_anim=None, outline_px=None, outline_opacity=None):
        if bg_color_override is not None and key_state == state.InputState.INACTIVE:
            fill = self._resolve_skin_color(bg_color_override)
        elif modifier and key_state == state.InputState.INACTIVE:
            fill = self.modifier_idle_color
        else:
            fill = self.key_color[key_state]
        # Effective label + glyph color for this key & state, used for BOTH the
        # text label and any glyph icon so they always match. Modifier keys wear
        # their own idle (meta) text color; held/clicked keys (CLICK) use the
        # toggle-on text color so the label/glyph stays legible on the highlight.
        if text_color_override is not None and key_state == state.InputState.INACTIVE:
            label_color = self._resolve_skin_color(text_color_override)
        elif key_state == state.InputState.INACTIVE and modifier:
            label_color = self.modifier_text_color
        else:
            label_color = self.text_color[key_state]
        # Glyph (button-hint icon) tint: same as the label EXCEPT a text-color
        # override never applies to the icon. The Move key's text is forced to
        # the grey shadow color, but its keyboard icon must stay white/meta.
        if key_state == state.InputState.INACTIVE and modifier:
            glyph_color = self.modifier_text_color
        else:
            glyph_color = self.text_color[key_state]
        # Flat fill, then (opaque mode only) a 1-px top highlight rim for a
        # subtle raised look. Transparent mode: the idle key fill is 45% opacity
        # and the hover/click highlight 90%, and the bevel rim is omitted.
        if self._transparent:
            base_a = 115 if key_state == state.InputState.INACTIVE else 230
            fill_a = min(255, int(round(base_a * self._tscale)))
        else:
            fill_a = 255
        S.SDL_SetRenderDrawColor(self.renderer, fill.r, fill.g, fill.b, fill_a)
        S.SDL_RenderFillRect(
            self.renderer, ctypes.byref(S.SDL_FRect(key.x, key.y, key.w, key.h)))
        if not self._transparent:
            hi = self.key_highlight_color
            S.SDL_SetRenderDrawColor(self.renderer, hi.r, hi.g, hi.b, 255)
            S.SDL_RenderLine(self.renderer, key.x, key.y, key.x + key.w - 1, key.y)

        # Glyph icon (controller button hint), tinted to match the label.
        if glyph:
            self._draw_glyph(key, glyph, align, txt, glyph_color)

        # Dual-state key mid-Shift-transition: slide both forms downward and
        # cross-fade instead of the static shadow-above-main stack. Replaces the
        # shadow_label + main-label path below (see render_vkb / _shift_anim).
        if dual_anim is not None:
            self._render_dual_anim(key, dual_anim, label_color, glyph_color, font, align, text_dx)
            return

        # Shadow label: small grey preview of the shifted variant, centered
        # above the main label. Forces the main label to bottom alignment so
        # the two stack cleanly.
        if shadow_label:
            # Shadow size: explicit shadow_font_size wins (lets a key shrink its
            # preview independently — e.g. the arrows' ▲▼); else the key's
            # font_size; else the default shadow font.
            sh_size = shadow_font_size if shadow_font_size is not None else font_size
            if sh_size is not None:
                shadow_font_obj = self._get_sized_font(font, sh_size)
            else:
                shadow_font_obj = self.font_manager_shadow_symbol if font == "symbol" else self.font_manager_shadow
            sh_tex, sh_tw, sh_th = self._make_text_texture(
                shadow_font_obj, shadow_label, self.shadow_label_color)
            if sh_tex:
                sh_x = key.x + (key.w - sh_tw) // 2
                sh_y = key.y + 3
                sh_dst = S.SDL_FRect(sh_x, sh_y, sh_tw, sh_th)
                S.SDL_RenderTexture(self.renderer, sh_tex, None, ctypes.byref(sh_dst))
                S.SDL_DestroyTexture(sh_tex)
            valign = "bottom"

        # We don't need to continue rendering text if there's nothing to render!
        if txt == "":
            return

        if font_size is not None:
            font_obj = self._get_sized_font(font, font_size)
        elif font == "symbol":
            font_obj = self.font_manager_symbol_small if modifier else self.font_manager_symbol
        else:
            font_obj = self.font_manager_small if modifier else self.font_manager
        tex, tw, th = self._make_text_texture(font_obj, txt, label_color)
        if not tex:
            return

        edge_pad = 14
        if align == "left":
            text_x = key.x + edge_pad
        elif align == "right":
            text_x = key.x + key.w - tw - edge_pad
        else:
            text_x = key.x + (key.w - tw) // 2
        # Per-key horizontal nudge (e.g. arrows shifted 1px outward to look
        # better centered against the symbol font's bearings).
        text_x += text_dx
        if valign == "top":
            text_y = key.y + 4
        elif valign == "bottom":
            # Dual-label keys: push the main label nearer the bottom edge so it
            # doesn't crowd the shadow preview above it. Modifier-style bottom
            # labels keep the higher 9-px lift.
            bottom_pad = 4 if shadow_label else 9
            text_y = key.y + key.h - th - bottom_pad
        else:
            text_y = key.y + (key.h - th) // 2

        self._draw_text_outline(font_obj, txt, label_color, text_x, text_y, 255,
                                outline_px, outline_opacity)
        S.SDL_SetTextureAlphaMod(tex, self._text_alpha)
        dst = S.SDL_FRect(text_x, text_y, tw, th)
        S.SDL_RenderTexture(self.renderer, tex, None, ctypes.byref(dst))
        S.SDL_DestroyTexture(tex)

    def _draw_glyph(self, key, glyph, align, txt, glyph_color, dy=0.0, alpha=255):
        """Draw a controller-button glyph icon on `key`, tinted to `glyph_color`.
        For keys with no text label the glyph is the centerpiece (big, centered);
        otherwise it sits at the bottom on the opposite side from the label. `dy`
        shifts it vertically and `alpha` scales its opacity — used by the Shift
        animation to slide the Move key's keyboard icon down and fade it out."""
        gtex, (gw, gh) = self._get_glyph(glyph)
        if gtex is None:
            return
        # Centerpiece: glyph-only key with no specific edge alignment — the glyph
        # IS the button's content, so it goes big & centered.
        is_centerpiece = (txt == "" and align == "center")
        gh_draw = int(key.h * (0.54 if is_centerpiece else 0.46))
        gw_draw = int(gw * (gh_draw / gh)) if gh else gh_draw
        # Horizontal placement: for glyph-only keys (empty label) the `align`
        # value picks the side directly; for keys with text the glyph sits on
        # the opposite side from the label.
        edge_pad = 12
        if txt == "":
            if align == "left":
                gx = key.x + edge_pad
            elif align == "right":
                gx = key.x + key.w - gw_draw - edge_pad
            else:
                gx = key.x + (key.w - gw_draw) // 2
        elif align == "left":
            gx = key.x + key.w - gw_draw - edge_pad
        elif align == "right":
            gx = key.x + edge_pad
        else:
            gx = key.x + (key.w - gw_draw) // 2
        if is_centerpiece:
            gy = key.y + (key.h - gh_draw) // 2
        else:
            gy = key.y + key.h - gh_draw - 6
        # The glyph PNGs are white-on-transparent; tint to the label color so
        # they track the skin AND stay legible in every state. Alpha is always
        # set (default 255) so a faded Move icon never leaks onto reused glyphs.
        S.SDL_SetTextureColorMod(gtex, glyph_color.r, glyph_color.g, glyph_color.b)
        # Fold in the icon opacity (80% in transparent mode) on top of any
        # animation alpha (the Move icon's fade).
        S.SDL_SetTextureAlphaMod(gtex, alpha * self._icon_alpha // 255)
        dst = S.SDL_FRect(gx, gy + dy, gw_draw, gh_draw)
        S.SDL_RenderTexture(self.renderer, gtex, None, ctypes.byref(dst))

    def _draw_text_outline(self, font_obj, txt, color, x, y, alpha,
                           px=None, opacity=None):
        """Transparent mode only: draw a hairline outline behind a text label in
        the inverse of the font color, by blitting the text at the 8 neighbour
        offsets. `px`/`opacity` override the per-key defaults (thicker for the
        arrows, fainter for the modifier/bracket keys). No-op when opaque."""
        if not self._transparent or not txt:
            return
        opacity = self._OUTLINE_ALPHA if opacity is None else opacity
        o = self._OUTLINE_PX if px is None else px
        inv = Color(255 - color.r, 255 - color.g, 255 - color.b)
        otex, ow, oh = self._make_text_texture(font_obj, txt, inv)
        if not otex:
            return
        # Semi-transparent outline reads as a finer, softer edge than a solid
        # one; scaled by the label's own alpha so it fades with an animating
        # label. Sub-pixel offsets keep it a hairline (text is linearly filtered).
        S.SDL_SetTextureAlphaMod(otex, min(255, int(alpha * opacity * self._tscale)))
        for ox, oy in ((-o, -o), (0, -o), (o, -o), (-o, 0),
                       (o, 0), (-o, o), (0, o), (o, o)):
            d = S.SDL_FRect(x + ox, y + oy, ow, oh)
            S.SDL_RenderTexture(self.renderer, otex, None, ctypes.byref(d))
        S.SDL_DestroyTexture(otex)

    @staticmethod
    def _lerp_color(c0, c1, t):
        return Color(
            int(round(c0.r + (c1.r - c0.r) * t)),
            int(round(c0.g + (c1.g - c0.g) * t)),
            int(round(c0.b + (c1.b - c0.b) * t)),
        )

    def _render_dual_anim(self, key, spec, label_color, glyph_color, font, align, text_dx):
        """Draw a dual-state key mid-Shift-transition.

        `spec["progress"]` runs 0 (unshifted) → 1 (shifted). The "upper" form
        (`spec["shifted"]`) slides from its grey top-perch down into the center
        while its color fades up from grey to the full label color; the "lower"
        form slides down by the same distance and fades out. The lower form is
        either text (`spec["unshifted"]`, the number/punctuation keys) or a glyph
        (`spec["glyph"]`, the Move key's keyboard icon). At the 0/1 extremes this
        matches the old static layout. (The Move label is grey at both ends, so
        its grey→grey fade is a no-op — only the slide shows.)"""
        p = spec["progress"]
        size = spec.get("font_size") or 21
        font_obj = self._get_sized_font(font, size)
        edge_pad = 14

        def x_for(tw):
            if align == "left":
                x = key.x + edge_pad
            elif align == "right":
                x = key.x + key.w - tw - edge_pad
            else:
                x = key.x + (key.w - tw) // 2
            return x + text_dx

        # Upper (shifted) form: top → center, grey → full color. Dual keys fade
        # to the label color; the Move key fades to its meta/glyph color (white
        # when idle) so "Move" brightens grey→white as it centers.
        # `slide` (its downward travel in px) is reused below so the lower form
        # moves the same distance at the same speed.
        slide = 0.0
        # The upper form brightens from grey to its full color as it centers on
        # shift. Dual keys → label color; the Move key → its meta/glyph color —
        # the same active text color the rest of the keys take in shift state, in
        # BOTH opaque and transparent modes (so "Move" doesn't stay grey).
        top_target = glyph_color if spec.get("glyph") else label_color
        sh_color = self._lerp_color(self.shadow_label_color, top_target, p)
        sh_tex, sh_tw, sh_th = self._make_text_texture(font_obj, spec["shifted"], sh_color)
        if sh_tex:
            # Upper-label rest perch. The dialed-in per-key nudges (closer-
            # together pair, legacy positions, per-key top_dy) apply ONLY to the
            # transparent skins; opaque skins keep the original key.y+3 perch —
            # only the Shift animation itself carries over to opaque. center_y
            # (the p=1 target) is unchanged either way, so the endpoint matches.
            if not self._transparent or spec.get("glyph"):
                top_y = key.y + 3
            else:
                top_y = key.y + (4 if spec.get("legacy_pos") else 2)
            if self._transparent:
                top_y += spec.get("top_dy", 0)  # per-key fine offset (YAML)
            center_y = key.y + (key.h - sh_th) // 2
            slide = (center_y - top_y) * p
            ux, uy = x_for(sh_tw), top_y + slide
            # Outline on the upper form fades IN with the shift progress: the
            # unshifted preview stays outline-free and the centered/shifted char
            # picks up the outline like any primary label. The Move "Move" text
            # uses this same shifted-text formatting — so it has NO outline in its
            # unshifted rest state (per user pref), only as it centers on shift.
            out_a = int(255 * p)
            if out_a > 0:
                self._draw_text_outline(font_obj, spec["shifted"], sh_color, ux, uy, out_a,
                                        spec.get("outline_px"), spec.get("outline_opacity"))
            S.SDL_SetTextureAlphaMod(sh_tex, self._text_alpha)
            dst = S.SDL_FRect(ux, uy, sh_tw, sh_th)
            S.SDL_RenderTexture(self.renderer, sh_tex, None, ctypes.byref(dst))
            S.SDL_DestroyTexture(sh_tex)

        # Lower form: bottom → slide down by the same `slide`, fading out.
        # Skipped once fully transparent (fully shifted).
        alpha = int(round(255 * (1.0 - p)))
        if alpha <= 0:
            return
        lower_glyph = spec.get("glyph")
        if lower_glyph:
            # Move key: the keyboard icon drops away (passing the label text so
            # _draw_glyph keeps the same bottom placement, not centerpiece).
            self._draw_glyph(key, lower_glyph, align, spec["shifted"],
                             glyph_color, dy=slide, alpha=alpha)
        else:
            un_tex, un_tw, un_th = self._make_text_texture(
                font_obj, spec["unshifted"], label_color)
            if un_tex:
                # Lower-label rest: the dialed-in nudges (8-px pad nudged / 5-px
                # legacy / per-key bottom_dy) apply ONLY to transparent skins;
                # opaque skins keep the original 4-px pad. p=1 is fully faded
                # either way, so the animation endpoint is unaffected.
                if self._transparent:
                    bottom_y = (key.y + key.h - un_th - (5 if spec.get("legacy_pos") else 8)
                                + spec.get("bottom_dy", 0))
                else:
                    bottom_y = key.y + key.h - un_th - 4
                lx, ly = x_for(un_tw), bottom_y + slide
                # Outline tracks the label's fade alpha so it dissolves together.
                self._draw_text_outline(font_obj, spec["unshifted"], label_color, lx, ly, alpha,
                                        spec.get("outline_px"), spec.get("outline_opacity"))
                S.SDL_SetTextureAlphaMod(un_tex, alpha)  # animation fade only; font opacity not scaled
                dst = S.SDL_FRect(lx, ly, un_tw, un_th)
                S.SDL_RenderTexture(self.renderer, un_tex, None, ctypes.byref(dst))
                S.SDL_DestroyTexture(un_tex)

    def render_ptr(self, ptr):
        # User-supplied touchcircle.png drawn at the pointer position with
        # 50% opacity. Same artwork for both pads.
        ptr_x, ptr_y = ptr.coord_frac.to_absolute()
        tex, (tw, th) = self._get_glyph("touchcircle.png")
        if tex is None:
            return
        S.SDL_SetTextureAlphaMod(tex, 166)
        cx = utils.round_to_int(ptr_x)
        cy = utils.round_to_int(ptr_y)
        dst = S.SDL_FRect(cx - tw // 2, cy - th // 2, tw, th)
        S.SDL_RenderTexture(self.renderer, tex, None, ctypes.byref(dst))

    def render_vkb(self, virtual_kb, pointers):
        shift_held = state.is_shift_held()
        caps_on = state.is_caps_on()
        highlighted = state.get_highlighted()
        lpad_touched = state.is_lpad_touched()
        rpad_touched = state.is_rpad_touched()
        cursor_row, cursor_col = state.get_cursor()
        mouse_press_cell = state.get_mouse_press_cell()
        # Swap the Shift/Enter trigger hint glyphs to match the controller last
        # used on the OSK: a generic SDL pad (Switch Pro) shows its ZL/ZR
        # glyphs, the Steam Controller its L2/R2 ones. Read once per frame.
        sdl_glyphs = state.get_active_controller() == "sdl"

        # Advance the Shift slide/fade animation toward the live shift state.
        # Dual-state keys (numbers/punctuation) use `shift_anim` (eased 0→1) to
        # slide their two labels and cross-fade instead of snapping. Wall-clock
        # driven so it's independent of the render frame rate. (adusk.main keeps
        # ACTIVE_FPS across a shift edge so the animation isn't starved.)
        now_t = time.monotonic()
        target = 1.0 if shift_held else 0.0
        if self._shift_anim_t is None:
            self._shift_anim = target  # snap on the first frame after open
        elif self._SHIFT_ANIM_DUR > 0:
            # Clamp dt so a single slow frame (e.g. the first one after the
            # render loop ramps back up from its idle FPS) can't make the
            # animation jump — it just starts a touch slower instead.
            dt = min(now_t - self._shift_anim_t, 0.02)
            step = dt / self._SHIFT_ANIM_DUR
            if self._shift_anim < target:
                self._shift_anim = min(target, self._shift_anim + step)
            elif self._shift_anim > target:
                self._shift_anim = max(target, self._shift_anim - step)
        else:
            self._shift_anim = target
        self._shift_anim_t = now_t
        # Smoothstep easing (ease-in-out) of the linear progress.
        _ap = self._shift_anim
        shift_anim = _ap * _ap * (3.0 - 2.0 * _ap)

        for key in virtual_kb.gen_key_layouts():
            input_state = state.InputState.INACTIVE
            if pointers[0].in_box(key.x, key.y, key.w, key.h):
                input_state = max(pointers[0].state, input_state)
            if pointers[1].in_box(key.x, key.y, key.w, key.h):
                input_state = max(pointers[1].state, input_state)
            kb_key = virtual_kb.keys[key.row][key.col]
            # Controller-button highlight: paint the on-screen key as if it
            # were being clicked while its bound button is physically held.
            if kb_key.keycode and kb_key.keycode in highlighted:
                input_state = max(state.InputState.CLICK, input_state)
            # DPAD cursor: paint the selected key in HOVER so the user can
            # see where the A button will land. Hidden while either touchpad
            # is being touched — the pointer is the focus then, not the cursor.
            if (key.row == cursor_row and key.col == cursor_col
                    and not lpad_touched and not rpad_touched):
                input_state = max(state.InputState.HOVER, input_state)
            # Mouse press: paint the key held under the left button blue, so a
            # mouse click flashes the same CLICK highlight as a real press.
            if (mouse_press_cell is not None
                    and key.row == mouse_press_cell[0]
                    and key.col == mouse_press_cell[1]):
                input_state = max(state.InputState.CLICK, input_state)

            # Single-alpha letter keys just swap case on shift/caps — they
            # don't warrant a permanent "shadow" preview. Non-letter dual-state
            # keys (numbers, punctuation) show the shifted form as a small
            # grey shadow above the main label while shift is *not* held; with
            # shift held, only the shifted form is shown, vertically centered.
            is_letter = len(kb_key.str) == 1 and kb_key.str.isalpha()
            dual_eligible = (kb_key.shifted and not kb_key.swap_on_shift
                             and not is_letter)
            # Move key: its label slides top→center on shift (valign→shift_valign)
            # and its keyboard glyph disappears (shift_glyph: ""). The empty-string
            # shift_glyph uniquely identifies it. Animate it like the dual keys.
            is_move_key = (kb_key.shift_glyph == "" and bool(kb_key.glyph))
            shadow = None
            valign = kb_key.valign
            dual_anim = None
            if dual_eligible:
                # Slide/fade between the unshifted (bottom) and shifted (center)
                # forms as Shift engages; handled in render_key via
                # _render_dual_anim (replaces the static shadow + main label).
                label = ""
                dual_anim = {
                    "shifted": kb_key.shifted,
                    "unshifted": kb_key.str,
                    "progress": shift_anim,
                    "font_size": kb_key.font_size or 21,
                    "legacy_pos": kb_key.legacy_label_pos,
                    "top_dy": kb_key.dual_top_dy,
                    "bottom_dy": kb_key.dual_bottom_dy,
                    "outline_px": kb_key.outline_px,
                    "outline_opacity": kb_key.outline_opacity,
                }
            elif is_move_key:
                # "Move" text is the upper form (slides top→center; it's grey at
                # both ends so only the slide shows); the keyboard glyph is the
                # lower form (slides down + fades out). render_key draws no static
                # glyph (forced None below) — the animated path draws it.
                label = ""
                dual_anim = {
                    "shifted": kb_key.str,
                    "unshifted": None,
                    "glyph": kb_key.glyph,
                    "progress": shift_anim,
                    "font_size": kb_key.font_size or 20,
                    "top_dy": kb_key.dual_top_dy,
                }
            else:
                label = kb_key.display_label(shift_held, caps_on)

            glyph = kb_key.glyph
            if shift_held:
                if kb_key.shift_glyph is not None:
                    glyph = kb_key.shift_glyph or None
                if kb_key.shift_valign is not None:
                    valign = kb_key.shift_valign
            # Hide the L2/R2 hint glyphs while the same-side touchpad is
            # being touched — in that state LT/RT click the pad target
            # instead of acting as Shift/Enter.
            if glyph == "glyph_l2.png" and lpad_touched:
                glyph = None
            if glyph == "sc_r2_md.png" and rpad_touched:
                glyph = None
            # Switch Pro (or any SDL pad) last used → show its glyphs in place of
            # the Steam Controller's. Done after the touch-hide check so both
            # controllers' hints hide the same way; the YAML only ever carries
            # the SC glyph names.
            #   * L2/R2 (Shift/Enter) → the Switch Pro's ZL/ZR art.
            #   * X (Backspace) / Y (Space) glyphs are SWAPPED: Nintendo places
            #     the physical X and Y buttons opposite the Xbox/Steam layout, so
            #     the SC's "X" button sits where the Switch's "Y" is and vice
            #     versa — swapping the icons keeps each hint on the button the
            #     user actually presses.
            if sdl_glyphs:
                if glyph == "glyph_l2.png":
                    glyph = "switchpro_l2_md.png"
                elif glyph == "sc_r2_md.png":
                    glyph = "switchpro_r2_md.png"
                elif glyph == "glyph_x.png":
                    glyph = "glyph_y.png"
                elif glyph == "glyph_y.png":
                    glyph = "glyph_x.png"

            # Dual-label keys size their main label to match the shadow
            # preview, unless the YAML explicitly overrides font_size.
            key_font_size = kb_key.font_size
            if dual_eligible and key_font_size is None:
                key_font_size = 21
            # The Move key's keyboard glyph is drawn (slid + faded) by the
            # animated path, so suppress render_key's static glyph draw.
            if is_move_key:
                glyph = None

            # Clip each key's content to its own rect so a label sliding past
            # the key edge during the Shift animation can't show outside the key
            # (now visible in transparent mode, which has no opaque background to
            # hide it). Expanded out by 1px so the fill edges aren't trimmed;
            # adjacent keys' clips overlap harmlessly at the shared boundary.
            cx, cy = int(key.x), int(key.y)
            clip = S.SDL_Rect(cx, cy, int(key.x + key.w) - cx + 1,
                              int(key.y + key.h) - cy + 1)
            S.SDL_SetRenderClipRect(self.renderer, ctypes.byref(clip))
            self.render_key(label, key, input_state, modifier=kb_key.modifier,
                            align=kb_key.align, valign=valign, glyph=glyph,
                            font=kb_key.font, text_color_override=kb_key.text_color,
                            bg_color_override=kb_key.bg_color, shadow_label=shadow,
                            font_size=key_font_size, text_dx=kb_key.text_dx,
                            shadow_font_size=kb_key.shadow_font_size,
                            dual_anim=dual_anim,
                            outline_px=kb_key.outline_px,
                            outline_opacity=kb_key.outline_opacity)

        # Drop the per-key clip so the pointer circles (and the next frame's
        # clear) aren't restricted to the last key's rect.
        S.SDL_SetRenderClipRect(self.renderer, None)

    def render(self, virtual_kb, pointers):
        self.clear()
        self.render_vkb(virtual_kb, pointers)
        # Only show the finger circles while the trackpad is actually being
        # touched; otherwise the screen would have two big idle pointers.
        if pointers[0].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[0])
        if pointers[1].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[1])
        S.SDL_RenderPresent(self.renderer)

    def _ensure_anim_target(self):
        """Lazily create (once) the offscreen render-target texture the open
        animation draws into. Returns it, or None if the renderer can't provide
        a target — in which case the caller simply skips the animation."""
        if self._anim_target is None:
            tex = S.SDL_CreateTexture(
                self.renderer, S.SDL_PIXELFORMAT_ABGR8888,
                S.SDL_TEXTUREACCESS_TARGET, width, height)
            if tex:
                # Rendering onto this texture (cleared to (0,0,0,0)) with the
                # renderer's normal BLEND mode leaves it holding PREMULTIPLIED
                # RGB (SDL's render-to-transparent-texture quirk: a draw of
                # color C @ alpha A onto a zeroed target yields stored
                # (C*A/255, A)). Compositing it back with plain BLEND would
                # multiply that already-premultiplied RGB by alpha AGAIN,
                # darkening translucent fills (visible as a "pop" to the
                # correct color when the animation ends and the normal
                # single-multiply render takes over). PREMULTIPLIED composites
                # it correctly and keeps the fade a pure alpha ramp.
                S.SDL_SetTextureBlendMode(tex, S.SDL_BLENDMODE_BLEND_PREMULTIPLIED)
            self._anim_target = tex
        return self._anim_target

    def render_open_anim(self, virtual_kb, pointers, fade, cut_px):
        """Render ONE frame of the OSK OPEN animation.

        `fade` (0..1) is the whole-keyboard opacity; `cut_px` pixels are hidden
        off the BOTTOM (the reveal shrinks this to 0). The keyboard is drawn into
        an offscreen texture at FULL opacity — so the font/text alpha is never
        scaled (the standing 100%-font rule holds) — then composited onto the
        window: cleared fully transparent, the keyboard blitted with a uniform
        alpha-mod (the fade) and clipped to the un-cut top region (the reveal).
        The cut strip and the fade's see-through both come from the window's
        per-pixel alpha (it is a TRANSPARENT/composited window). The downward
        settle is done by the CALLER repositioning the window. Returns False if
        no offscreen target is available (caller falls back to a normal render)."""
        tex = self._ensure_anim_target()
        if not tex:
            return False
        # 1) Keyboard -> offscreen texture, full opacity, normal appearance.
        S.SDL_SetRenderTarget(self.renderer, tex)
        self.clear()
        self.render_vkb(virtual_kb, pointers)
        if pointers[0].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[0])
        if pointers[1].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[1])
        # 2) Composite onto the window: clear fully transparent, then blit the
        #    faded keyboard clipped to the still-revealed (un-cut) top region.
        S.SDL_SetRenderTarget(self.renderer, None)
        S.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 0)
        S.SDL_RenderClear(self.renderer)
        vis_h = height - max(0, int(round(cut_px)))
        if vis_h > 0:
            clip = S.SDL_Rect(0, 0, width, vis_h)
            S.SDL_SetRenderClipRect(self.renderer, ctypes.byref(clip))
            alpha = 0 if fade <= 0.0 else 255 if fade >= 1.0 else int(round(fade * 255))
            S.SDL_SetTextureAlphaMod(tex, alpha)
            dst = S.SDL_FRect(0.0, 0.0, float(width), float(height))
            S.SDL_RenderTexture(self.renderer, tex, None, ctypes.byref(dst))
            S.SDL_SetRenderClipRect(self.renderer, None)
        S.SDL_RenderPresent(self.renderer)
        return True

    def prime_open_anim_buffers(self, n=5):
        """Present `n` fully-transparent frames to fill EVERY buffer in the
        window's swap chain (the renderer has no vsync, and X11/XWayland double-
        or triple-buffers). Called while the window is still parked off-screen
        during the open-animation priming: the compositor's first ON-screen
        composite can otherwise land on an as-yet-unpresented back-buffer that's
        still the undefined/black map-buffer — a one-frame black box over the
        desktop that shows up intermittently (~1 in N opens, N = buffer count),
        most visibly in transparent mode. Cheap (off-screen, no vsync); each
        present rotates to the next buffer so n>=buffer-count clears them all."""
        for _ in range(max(1, n)):
            S.SDL_SetRenderTarget(self.renderer, None)
            S.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 0)
            S.SDL_RenderClear(self.renderer)
            S.SDL_RenderPresent(self.renderer)
