"""Standalone proof that the sdl3w rendering + TTF + surface/texture binding
works end-to-end on screen, before porting adusk/ off pysdl2.

Run:  python -m sdl3w._rendertest
Opens a small window for ~1.5s drawing a bg, a filled rect, a top line, a
PIL-uploaded texture, and a line of TTF text. Prints PASS/FAIL per step.
"""

import ctypes
import os
import time

import sdl3w as S


def _ck(ok, what):
    if not ok:
        print(f"  FAIL: {what}: {S.get_error()}")
    return ok


def main():
    assert S.SDL_Init(S.SDL_INIT_VIDEO | S.SDL_INIT_EVENTS), S.get_error()
    assert S.TTF_Init(), "TTF_Init: " + S.get_error()

    win = S.SDL_CreateWindow(b"sdl3w rendertest", 420, 220, S.SDL_WINDOW_BORDERLESS)
    _ck(bool(win), "SDL_CreateWindow")
    S.SDL_SetWindowPosition(win, S.SDL_WINDOWPOS_CENTERED, S.SDL_WINDOWPOS_CENTERED)
    S.SDL_ShowWindow(win)
    ren = S.SDL_CreateRenderer(win, None)
    _ck(bool(ren), "SDL_CreateRenderer")
    S.SDL_SetRenderDrawBlendMode(ren, S.SDL_BLENDMODE_BLEND)

    # --- a PIL-style RGBA buffer -> SDL surface -> texture (the glyph path) ---
    w = h = 48
    px = bytearray()
    for y in range(h):
        for x in range(w):
            px += bytes((0x1A, 0x9F, 0xFF, 0xFF))  # opaque steam-blue, RGBA
    pbuf = (ctypes.c_ubyte * len(px)).from_buffer_copy(bytes(px))
    surf = S.SDL_CreateSurfaceFrom(w, h, S.SDL_PIXELFORMAT_ABGR8888,
                                   ctypes.cast(pbuf, ctypes.c_void_p), w * 4)
    _ck(bool(surf), "SDL_CreateSurfaceFrom")
    img_tex = S.SDL_CreateTextureFromSurface(ren, surf) if surf else None
    _ck(bool(img_tex), "CreateTextureFromSurface(image)")
    if surf:
        S.SDL_DestroySurface(surf)
    if img_tex:
        S.SDL_SetTextureScaleMode(img_tex, S.SDL_SCALEMODE_LINEAR)
        S.SDL_SetTextureBlendMode(img_tex, S.SDL_BLENDMODE_BLEND)

    # --- TTF text -> surface -> texture ---
    font_path = r"C:\Windows\Fonts\seguisb.ttf"
    if not os.path.isfile(font_path):
        font_path = r"C:\Windows\Fonts\segoeui.ttf"
    font = S.TTF_OpenFont(font_path.encode("utf-8"), 26.0)
    _ck(bool(font), "TTF_OpenFont")
    txt_tex = None
    tw = th = 0
    if font:
        col = S.SDL_Color(0xEE, 0xF3, 0xF7, 0xFF)
        tsurf = S.TTF_RenderText_Blended(font, b"Hello SDL3 \xe2\x97\x80\xe2\x96\xb6", 0, col)
        _ck(bool(tsurf), "TTF_RenderText_Blended")
        if tsurf:
            tw, th = tsurf.contents.w, tsurf.contents.h
            txt_tex = S.SDL_CreateTextureFromSurface(ren, tsurf)
            _ck(bool(txt_tex), "CreateTextureFromSurface(text)")
            S.SDL_DestroySurface(tsurf)

    # --- draw a few frames ---
    ev = S.SDL_Event()
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        while S.SDL_PollEvent(ctypes.byref(ev)):
            pass
        S.SDL_SetRenderDrawColor(ren, 0x23, 0x26, 0x2E, 0xFF)
        S.SDL_RenderClear(ren)
        # filled key rect
        S.SDL_SetRenderDrawColor(ren, 0x0E, 0x14, 0x1B, 0xFF)
        S.SDL_RenderFillRect(ren, ctypes.byref(S.SDL_FRect(20, 20, 380, 80)))
        # top highlight line
        S.SDL_SetRenderDrawColor(ren, 0x4A, 0x5D, 0x70, 0xFF)
        S.SDL_RenderLine(ren, 20, 20, 399, 20)
        # image texture
        if img_tex:
            S.SDL_RenderTexture(ren, img_tex, None, ctypes.byref(S.SDL_FRect(30, 35, 48, 48)))
        # text
        if txt_tex:
            S.SDL_RenderTexture(ren, txt_tex, None,
                                ctypes.byref(S.SDL_FRect(20, 130, tw, th)))
        S.SDL_RenderPresent(ren)
        time.sleep(0.016)

    if txt_tex:
        S.SDL_DestroyTexture(txt_tex)
    if img_tex:
        S.SDL_DestroyTexture(img_tex)
    if font:
        S.TTF_CloseFont(font)
    hwnd = S.get_win32_hwnd(win)
    print(f"  win32 HWND resolved: {hwnd!r}")
    S.SDL_DestroyRenderer(ren)
    S.SDL_DestroyWindow(win)
    S.TTF_Quit()
    S.SDL_Quit()
    print("rendertest done")


if __name__ == "__main__":
    main()
