"""DEMO: covered lines, half the branches."""


def clamp(x: int) -> int:
    if x < 0:
        x = 0
    if x > 10:
        x = 10
    return x
