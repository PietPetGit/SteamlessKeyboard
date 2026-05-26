import ctypes
import os

import sdl2
import sdl2.ext
import sdl2.sdlgfx
import sdl2.sdlimage
from PIL import Image as PILImage

from adusk import resources
from adusk import state
from adusk import utils

width = 1286
height = 369


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
    bg_color = sdl2.ext.Color(0x23, 0x26, 0x2E)

    key_color = {
        state.InputState.INACTIVE: sdl2.ext.Color(0x0E, 0x14, 0x1B),
        state.InputState.HOVER: sdl2.ext.Color(0xFF, 0xFF, 0xFF),
        state.InputState.CLICK: sdl2.ext.Color(0x1A, 0x9F, 0xFF),
    }
    # Modifier keys (Tab, Caps, Shift, Enter, Backspace) use pure black when idle.
    modifier_idle_color = sdl2.ext.Color(0x00, 0x00, 0x00)
    # Text needs to flip dark when the key turns white on hover, otherwise
    # the label vanishes against the same-color background.
    text_color = {
        state.InputState.INACTIVE: sdl2.ext.Color(0xEE, 0xF3, 0xF7),
        state.InputState.HOVER: sdl2.ext.Color(0x0E, 0x14, 0x1B),
        state.InputState.CLICK: sdl2.ext.Color(0xEE, 0xF3, 0xF7),
    }
    # Subtle top-edge highlight to fake a 3D bevel on each key.
    key_highlight_color = sdl2.ext.Color(0x4a, 0x5d, 0x70)
    # Color for the small "shadow" label that previews a key's shifted form.
    shadow_label_color = sdl2.ext.Color(0x7B, 0x7E, 0x82)

    def __init__(self):
        # Make sure SDL never auto-activates the window when it gets shown,
        # so opening the OSK doesn't steal focus from the user's target app
        # (e.g. a browser address bar / YouTube search field).
        sdl2.SDL_SetHint(sdl2.SDL_HINT_WINDOW_NO_ACTIVATION_WHEN_SHOWN, b"1")
        # Default texture filter to "best" (anisotropic where supported,
        # else linear). Must be set before the renderer is created — once
        # the renderer exists, textures inherit its default scale mode.
        # Glyphs additionally pin their scale mode per-texture below.
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_SCALE_QUALITY, b"best")

        # Anchor the window to the bottom-center of the primary display's
        # usable area (i.e. above the taskbar).
        bounds = sdl2.SDL_Rect()
        if sdl2.SDL_GetDisplayUsableBounds(0, ctypes.byref(bounds)) == 0:
            win_x = bounds.x + max(0, (bounds.w - width) // 2)
            win_y = bounds.y + max(0, bounds.h - height)
        else:
            win_x = sdl2.SDL_WINDOWPOS_CENTERED
            win_y = sdl2.SDL_WINDOWPOS_CENTERED
        # SDL_WINDOW_HIDDEN lets us apply the WS_EX_NOACTIVATE bit BEFORE the
        # window is ever visible; otherwise the very first paint can steal
        # focus regardless of subsequent style changes.
        self.window = sdl2.ext.Window("", (width, height), position=(win_x, win_y),
                                      flags=sdl2.SDL_WINDOW_BORDERLESS
                                      | sdl2.SDL_WINDOW_HIDDEN)
        self.renderer = sdl2.ext.Renderer(self.window)

        # Use Windows' Segoe UI Semibold (Steam Big Picture's keyboard font);
        # fall back to the bundled DejaVu if it's missing.
        font_path = r"C:\Windows\Fonts\seguisb.ttf"
        if not os.path.isfile(font_path):
            font_name = "fonts/DejaVuSansCondensed-Bold.ttf"
            font_path = resources.find_data_resource(font_name)
            assert font_path is not None, "Could not find font file `{}`!".format(font_name)
        print("Found font file at `{}`".format(font_path))
        self.font_manager = sdl2.ext.FontManager(font_path, size=26)
        # Modifier keys (Tab, Caps, Shift, Enter, Backspace) wear a smaller label.
        self.font_manager_small = sdl2.ext.FontManager(font_path, size=20)

        # Segoe UI Symbol covers geometric shapes (◀ ▶ etc.) that Segoe UI
        # Semibold draws as missing-glyph boxes.
        sym_path = r"C:\Windows\Fonts\seguisym.ttf"
        if not os.path.isfile(sym_path):
            sym_path = font_path
        self.font_manager_symbol = sdl2.ext.FontManager(sym_path, size=26)
        self.font_manager_symbol_small = sdl2.ext.FontManager(sym_path, size=20)
        # Shadow labels (small grey previews of a key's shifted variant).
        self.font_manager_shadow = sdl2.ext.FontManager(font_path, size=21)
        self.font_manager_shadow_symbol = sdl2.ext.FontManager(sym_path, size=21)

        # Cache for one-off custom-size FontManagers keyed by (path, size).
        self._font_paths = {"default": font_path, "symbol": sym_path}
        self._font_cache = {}

        # Cache of glyph PNGs (button hint icons) keyed by filename.
        self._glyph_textures = {}
        sdl2.sdlimage.IMG_Init(sdl2.sdlimage.IMG_INIT_PNG)

        self.frame_rate_manager = sdl2.sdlgfx.FPSManager()
        sdl2.sdlgfx.SDL_initFramerate(ctypes.byref(self.frame_rate_manager))
        # 120 fps: halves the render-loop latency for touchpad-driven haptics
        # (hover-switch) and pointer rendering vs 60 fps, at some extra CPU.
        sdl2.sdlgfx.SDL_setFramerate(ctypes.byref(self.frame_rate_manager), 120)

        self.clear()
        # NOTE: window stays SDL_WINDOW_HIDDEN; adusk.main() applies the
        # WS_EX_NOACTIVATE bit and then shows it via the Win32 no-activate
        # path. Calling self.window.show() here would activate it.

    def clear(self):
        self.renderer.clear(color=self.bg_color)

    def delay(self):
        sdl2.sdlgfx.SDL_framerateDelay(ctypes.byref(self.frame_rate_manager))

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

    def _load_glyph_texture(self, path):
        """LANCZOS-downsample the source PNG, then upload as an SDL texture
        with linear filtering for the final on-screen blit."""
        pil = PILImage.open(path).convert("RGBA")
        if max(pil.size) > self._GLYPH_CACHE_PX:
            pil.thumbnail(
                (self._GLYPH_CACHE_PX, self._GLYPH_CACHE_PX), PILImage.LANCZOS
            )
        w, h = pil.size
        data = pil.tobytes()
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        # SDL_PIXELFORMAT_ABGR8888 matches PIL's RGBA byte order on
        # little-endian (red byte first in memory).
        surf = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
            ctypes.cast(buf, ctypes.c_void_p), w, h, 32, w * 4,
            sdl2.SDL_PIXELFORMAT_ABGR8888,
        )
        if not surf:
            return None, (0, 0)
        tex = sdl2.SDL_CreateTextureFromSurface(self.renderer.renderer, surf)
        sdl2.SDL_FreeSurface(surf)
        if tex:
            sdl2.SDL_SetTextureScaleMode(tex, sdl2.SDL_ScaleModeLinear)
            sdl2.SDL_SetTextureBlendMode(tex, sdl2.SDL_BLENDMODE_BLEND)
        return tex, (w, h)

    def _get_sized_font(self, font_name, size):
        key = (font_name, size)
        fm = self._font_cache.get(key)
        if fm is None:
            path = self._font_paths.get(font_name, self._font_paths["default"])
            fm = sdl2.ext.FontManager(path, size=size)
            self._font_cache[key] = fm
        return fm

    def render_key(self, txt, key, key_state, modifier=False, align="center", valign="center", glyph=None, font="default", text_color_override=None, bg_color_override=None, shadow_label=None, font_size=None):
        if bg_color_override is not None and key_state == state.InputState.INACTIVE:
            fill = bg_color_override
        elif modifier and key_state == state.InputState.INACTIVE:
            fill = self.modifier_idle_color
        else:
            fill = self.key_color[key_state]
        # Flat fill, then a 1-px top highlight rim for a subtle raised look.
        self.renderer.fill([(key.x, key.y, key.w, key.h)], color=fill)
        hi = self.key_highlight_color
        sdl2.sdlgfx.hlineRGBA(self.renderer.renderer, key.x, key.x + key.w - 1, key.y,
                              hi.r, hi.g, hi.b, 255)

        # Glyph icon (controller button hint). Drawn on the opposite side of
        # the text label; for keys with no label it's centered.
        if glyph:
            gtex, (gw, gh) = self._get_glyph(glyph)
            if gtex is not None:
                # Centerpiece: glyph-only key with no specific edge alignment —
                # the glyph IS the button's content, so it goes big & centered.
                is_centerpiece = (txt == "" and align == "center")
                gh_draw = int(key.h * (0.54 if is_centerpiece else 0.46))
                gw_draw = int(gw * (gh_draw / gh)) if gh else gh_draw
                # Horizontal placement: for glyph-only keys (empty label) the
                # `align` value picks the side directly; for keys with text the
                # glyph sits on the opposite side from the label.
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
                    bottom_pad = 6
                    gy = key.y + key.h - gh_draw - bottom_pad
                # The glyph PNGs are white-on-transparent. When the key is in
                # HOVER state its background is white, so the glyph would be
                # invisible — apply a dark color-mod so it inverts to dark on
                # white, matching how the text label flips color in HOVER.
                if key_state == state.InputState.HOVER:
                    dark = self.text_color[state.InputState.HOVER]
                    sdl2.SDL_SetTextureColorMod(gtex, dark.r, dark.g, dark.b)
                else:
                    sdl2.SDL_SetTextureColorMod(gtex, 255, 255, 255)
                dst = sdl2.SDL_Rect(gx, gy, gw_draw, gh_draw)
                sdl2.SDL_RenderCopy(self.renderer.renderer, gtex, None, ctypes.byref(dst))

        # Shadow label: small grey preview of the shifted variant, centered
        # above the main label. Forces the main label to bottom alignment so
        # the two stack cleanly.
        if shadow_label:
            # When the YAML supplies an explicit font_size, use it for the
            # shadow too so per-key overrides scale both labels together.
            if font_size is not None:
                shadow_font_obj = self._get_sized_font(font, font_size)
            else:
                shadow_font_obj = self.font_manager_shadow_symbol if font == "symbol" else self.font_manager_shadow
            sh_surf = shadow_font_obj.render(shadow_label, color=self.shadow_label_color)
            sh_tex_p = sdl2.SDL_CreateTextureFromSurface(self.renderer.renderer, ctypes.byref(sh_surf))
            sh_tw = sh_surf.clip_rect.w
            sh_th = sh_surf.clip_rect.h
            sh_x = key.x + (key.w - sh_tw) // 2
            sh_y = key.y + 3
            sh_src = (sh_surf.clip_rect.x, sh_surf.clip_rect.y, sh_tw, sh_th)
            sh_dst = (sh_x, sh_y, sh_tw, sh_th)
            self.renderer.copy(sh_tex_p[0], sh_src, sh_dst)
            sdl2.SDL_DestroyTexture(sh_tex_p)
            sdl2.SDL_FreeSurface(ctypes.byref(sh_surf))
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
        if text_color_override is not None and key_state == state.InputState.INACTIVE:
            text_col = text_color_override
        else:
            text_col = self.text_color[key_state]
        text_surface = font_obj.render(txt, color=text_col)
        text_texture_p = sdl2.SDL_CreateTextureFromSurface(self.renderer.renderer, ctypes.byref(text_surface))
        sdl2.SDL_FreeSurface(ctypes.byref(text_surface))

        tw = text_surface.clip_rect.w
        th = text_surface.clip_rect.h
        edge_pad = 14
        if align == "left":
            text_x = key.x + edge_pad
        elif align == "right":
            text_x = key.x + key.w - tw - edge_pad
        else:
            text_x = key.x + (key.w - tw) // 2
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

        src_rect = (text_surface.clip_rect.x, text_surface.clip_rect.y, tw, th)
        dst_rect = (text_x, text_y, tw, th)
        self.renderer.copy(text_texture_p[0], src_rect, dst_rect)
        sdl2.SDL_DestroyTexture(text_texture_p)

    def render_ptr(self, ptr):
        # User-supplied touchcircle.png drawn at the pointer position with
        # 50% opacity. Same artwork for both pads.
        ptr_x, ptr_y = ptr.coord_frac.to_absolute()
        tex, (tw, th) = self._get_glyph("touchcircle.png")
        if tex is None:
            return
        sdl2.SDL_SetTextureAlphaMod(tex, 166)
        cx = utils.round_to_int(ptr_x)
        cy = utils.round_to_int(ptr_y)
        dst = sdl2.SDL_Rect(cx - tw // 2, cy - th // 2, tw, th)
        sdl2.SDL_RenderCopy(self.renderer.renderer, tex, None, ctypes.byref(dst))

    def render_vkb(self, virtual_kb, pointers):
        shift_held = state.is_shift_held()
        caps_on = state.is_caps_on()
        highlighted = state.get_highlighted()
        lpad_touched = state.is_lpad_touched()
        rpad_touched = state.is_rpad_touched()
        cursor_row, cursor_col = state.get_cursor()
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

            # Single-alpha letter keys just swap case on shift/caps — they
            # don't warrant a permanent "shadow" preview. Non-letter dual-state
            # keys (numbers, punctuation) show the shifted form as a small
            # grey shadow above the main label while shift is *not* held; with
            # shift held, only the shifted form is shown, vertically centered.
            is_letter = len(kb_key.str) == 1 and kb_key.str.isalpha()
            dual_eligible = (kb_key.shifted and not kb_key.swap_on_shift
                             and not is_letter)
            shadow = None
            valign = kb_key.valign
            if dual_eligible:
                if shift_held:
                    label = kb_key.shifted
                    valign = "center"
                else:
                    label = kb_key.str
                    shadow = kb_key.shifted
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
            if glyph == "glyph_r2.png" and rpad_touched:
                glyph = None

            # Dual-label keys size their main label to match the shadow
            # preview, unless the YAML explicitly overrides font_size.
            key_font_size = kb_key.font_size
            if dual_eligible and key_font_size is None:
                key_font_size = 21

            self.render_key(label, key, input_state, modifier=kb_key.modifier,
                            align=kb_key.align, valign=valign, glyph=glyph,
                            font=kb_key.font, text_color_override=kb_key.text_color,
                            bg_color_override=kb_key.bg_color, shadow_label=shadow,
                            font_size=key_font_size)

    def render(self, virtual_kb, pointers):
        self.clear()
        self.render_vkb(virtual_kb, pointers)
        # Only show the finger circles while the trackpad is actually being
        # touched; otherwise the screen would have two big idle pointers.
        if pointers[0].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[0])
        if pointers[1].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[1])
        self.renderer.present()
        self.window.refresh()
