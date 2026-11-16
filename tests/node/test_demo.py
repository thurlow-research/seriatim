from seriatim_node._demo import clamp


def test_clamp_low():
    assert clamp(-1) == 0
