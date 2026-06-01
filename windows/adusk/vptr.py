from adusk import utils


class VirtualPointer:
    def __init__(self, state, coord_frac):
        self.state = state
        self.coord_frac = coord_frac

    def in_box(self, bx, by, bw, bh):
        x, y = self.coord_frac.to_absolute()
        return x >= bx and y >= by and x <= bx + bw and y <= by + bh

    def smoothen(self, prev_vptr, alpha):
        x, y = self.coord_frac.to_absolute()
        prev_x, prev_y = prev_vptr.coord_frac.to_absolute()
        x = utils.round_to_int(utils.compute_lowpass(x, prev_x, alpha))
        y = utils.round_to_int(utils.compute_lowpass(y, prev_y, alpha))
        self.coord_frac.update_absolute(x, y)
