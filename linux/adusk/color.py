"""A tiny dependency-free RGBA color, replacing sdl2.ext.Color after the SDL3
cutover.

The renderer (screen.py) and the skin loader (skins.py) only ever read
.r/.g/.b (occasionally .a), so this is a drop-in for the handful of
sdl2.ext.Color uses — and it keeps SDL out of modules like skins.py (imported
by the tray) that merely need to describe a color.
"""


class Color:
    __slots__ = ("r", "g", "b", "a")

    def __init__(self, r, g, b, a=255):
        self.r = int(r)
        self.g = int(g)
        self.b = int(b)
        self.a = int(a)

    def __iter__(self):
        # Lets `Color(*other)` / tuple(color) round-trip like sdl2.ext.Color.
        yield self.r
        yield self.g
        yield self.b
        yield self.a

    def __eq__(self, other):
        return (isinstance(other, Color) and self.r == other.r
                and self.g == other.g and self.b == other.b
                and self.a == other.a)

    def __repr__(self):
        return "Color({}, {}, {}, {})".format(self.r, self.g, self.b, self.a)
