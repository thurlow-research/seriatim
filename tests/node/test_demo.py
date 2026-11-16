from seriatim_node._demo import add


def test_add_runs():
    add(1, 2)  # no assertion: every mutant survives
