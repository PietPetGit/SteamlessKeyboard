def round_to_int(f):
    return int(round(f))


def clamp(i, min_val, max_val):
    return max(min_val, min(max_val, i))


def compute_lowpass(curr, prev, alpha):
    return prev + alpha * (curr - prev)
