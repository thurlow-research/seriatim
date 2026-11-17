"""`Gate` and `Lease` (TD-slice-1 §2.2, §3.1, §3.2; tests G1 to G12 of §8.2).

The path tests (K1 to K6) are added by S1-3.
"""

from __future__ import annotations

import asyncio
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from seriatim_node.gate import Busy, Gate, HolderInfo, Lease, SlotState, SlotView, Starting


class Clock:
    """An injectable clock, advanced by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make(
    slots: int = 1,
    retry_after_s: int = 15,
    drain_s: float = 5.0,
    max_hold_s: float = 600.0,
    *,
    ready: bool = True,
) -> tuple[Gate, Clock, Clock]:
    clock, wall = Clock(1000.0), Clock(1_700_000_000.0)
    gate = Gate(slots, retry_after_s, drain_s, max_hold_s, clock=clock, wall=wall)
    if ready:
        clock.advance(drain_s)
    return gate, clock, wall


def lease(gate: Gate, path: str = "/api/chat", rid: str = "r") -> Lease:
    got = gate.try_acquire(path, rid)
    assert isinstance(got, Lease), got
    return got


def states(gate: Gate) -> list[SlotState]:
    return [v.state for v in gate.slots()]


@pytest.mark.td("G1")
def test_g1_after_the_quarantine_a_lease_holds_the_slot():
    gate, clock, wall = make()
    got = lease(gate, "/api/generate", "req-1")
    assert states(gate) == [SlotState.HELD]
    assert got.info == HolderInfo("/api/generate", "req-1", clock.t, wall.t)
    assert got.deadline == clock.t + 600.0


@pytest.mark.td("G2")
def test_g2_one_slot_held_gives_busy_with_retry_after():
    gate, clock, _ = make()
    lease(gate)
    clock.advance(2.5)
    got = gate.try_acquire("/api/chat", "r2")
    assert got == Busy(retry_after_s=15, busy_for_s=2.5)


@pytest.mark.td("G3")
def test_g3_two_slots_two_leases_then_busy():
    gate, _, _ = make(slots=2)
    a, b = lease(gate, rid="a"), lease(gate, rid="b")
    assert a.info.request_id == "a" and b.info.request_id == "b"
    assert states(gate) == [SlotState.HELD, SlotState.HELD]
    assert isinstance(gate.try_acquire("/api/chat", "c"), Busy)


@pytest.mark.td("G4")
def test_g4_clean_finish_frees_at_once():
    gate, _, _ = make()
    assert lease(gate).finish(clean=True) is True
    assert states(gate) == [SlotState.FREE]
    lease(gate)


@pytest.mark.td("G4")
def test_g4_unclean_finish_drains_for_drain_s():
    gate, clock, _ = make(retry_after_s=1, drain_s=5.0)
    assert lease(gate).finish(clean=False) is True
    assert states(gate) == [SlotState.DRAINING]
    clock.advance(1.5)
    got = gate.try_acquire("/api/chat", "r2")
    assert got == Busy(retry_after_s=math.ceil(3.5), busy_for_s=0.0)
    clock.advance(3.25)
    assert gate.try_acquire("/api/chat", "r3") == Busy(retry_after_s=1, busy_for_s=0.0)
    clock.advance(0.25)  # exactly drain_s after the finish: drain_until <= now
    lease(gate, rid="r4")


@pytest.mark.td("G4")
def test_g4_retry_after_is_never_below_the_configured_value():
    gate, clock, _ = make(retry_after_s=15, drain_s=5.0)
    lease(gate).finish(clean=False)
    assert gate.try_acquire("/api/chat", "r").retry_after_s == 15


@pytest.mark.td("G5")
def test_g5_finish_is_idempotent_and_never_frees_a_newer_lease():
    gate, _, _ = make()
    old = lease(gate, rid="old")
    assert old.finish(clean=True) is True
    new = lease(gate, rid="new")
    assert old.finish(clean=True) is False
    assert old.finish(clean=False) is False
    assert old.stuck() is False
    view = gate.slots()[0]
    assert view.state is SlotState.HELD and view.holder == new.info
    assert isinstance(gate.try_acquire("/api/chat", "x"), Busy)


@pytest.mark.td("G5")
def test_g5_stuck_then_finish_stays_stuck():
    gate, clock, _ = make()
    held = lease(gate)
    assert held.stuck() is True
    assert held.stuck() is False
    assert held.finish(clean=True) is False
    assert held.finish(clean=False) is False
    clock.advance(10_000)
    assert states(gate) == [SlotState.STUCK]
    assert isinstance(gate.try_acquire("/api/chat", "x"), Busy)


@pytest.mark.td("G6")
def test_g6_slots_show_state_holder_and_drain():
    gate, clock, _ = make(slots=4, drain_s=5.0)
    held, drained, stuck = lease(gate, rid="h"), lease(gate, rid="d"), lease(gate, rid="s")
    drained.finish(clean=False)
    stuck.stuck()
    clock.advance(2.0)
    assert gate.slots() == [
        SlotView(SlotState.HELD, held.info, None),
        SlotView(SlotState.DRAINING, None, 3.0),
        SlotView(SlotState.STUCK, stuck.info, None),
        SlotView(SlotState.FREE, None, None),
    ]


@pytest.mark.td("G6")
def test_g6_overdue_after_max_hold_plus_grace_and_any_stuck():
    gate, clock, _ = make(slots=3, max_hold_s=100.0)
    held = lease(gate, rid="h")
    lease(gate, rid="s").stuck()
    lease(gate, rid="d").finish(clean=False)
    clock.advance(129.0)
    assert gate.overdue() == []
    clock.advance(2.0)
    assert gate.overdue() == [held.info]  # default grace 30 s; the stuck holder never counts
    assert gate.overdue(grace_s=100.0) == []
    assert gate.any_stuck() is True


@pytest.mark.td("G6")
def test_g6_any_stuck_is_false_without_a_stuck_slot():
    gate, _, _ = make(slots=2)
    lease(gate).finish(clean=False)
    lease(gate)
    assert gate.any_stuck() is False


@pytest.mark.td("G7")
def test_g7_a_thousand_concurrent_callers_one_lease():
    gate, _, _ = make(slots=1)

    async def caller(i: int) -> Lease | Busy | Starting:
        await asyncio.sleep(0)
        return gate.try_acquire("/api/chat", f"r{i}")

    async def main() -> list[Lease | Busy | Starting]:
        return await asyncio.gather(*(caller(i) for i in range(1000)))

    results = asyncio.run(main())
    assert sum(isinstance(r, Lease) for r in results) == 1
    assert sum(isinstance(r, Busy) for r in results) == 999


@pytest.mark.td("G8")
def test_g8_startup_quarantine():
    gate, clock, _ = make(slots=2, drain_s=5.0, ready=False)
    assert gate.starting() is True
    assert gate.try_acquire("/api/chat", "r") == Starting(retry_after_s=5)
    assert states(gate) == [SlotState.DRAINING, SlotState.DRAINING]
    clock.advance(1.25)
    assert gate.try_acquire("/api/chat", "r") == Starting(retry_after_s=4)
    assert gate.starting() is True
    clock.advance(3.75)
    assert gate.starting() is False
    assert states(gate) == [SlotState.FREE, SlotState.FREE]
    lease(gate)


@pytest.mark.td("G8")
def test_g8_fractional_drain_rounds_up():
    gate, _, _ = make(drain_s=2.5, ready=False)
    assert gate.try_acquire("/api/chat", "r") == Starting(retry_after_s=3)


OPS = st.lists(
    st.one_of(
        st.just(("acquire",)),
        st.tuples(st.just("finish"), st.booleans(), st.integers(0, 7)),
        st.tuples(st.just("stuck"), st.integers(0, 7)),
        st.tuples(st.just("tick"), st.floats(0, 8, allow_nan=False)),
    ),
    max_size=60,
)


@pytest.mark.td("G9")
@settings(max_examples=300, deadline=None)
@given(n=st.integers(1, 4), ops=OPS)
def test_g9_property_the_limit_holds(n, ops):
    gate, clock, _ = make(slots=n, drain_s=3.0, ready=False)
    leases: list[Lease] = []
    for op in ops:
        if op[0] == "acquire":
            before = states(gate)
            got = gate.try_acquire("/api/chat", "r")
            if isinstance(got, Lease):
                assert SlotState.FREE in before
                leases.append(got)
            elif isinstance(got, Busy):
                assert SlotState.FREE not in before
                assert got.retry_after_s >= 15
        elif op[0] == "finish" and leases:
            leases[op[2] % len(leases)].finish(clean=op[1])
        elif op[0] == "stuck" and leases:
            leases[op[1] % len(leases)].stuck()
        elif op[0] == "tick":
            clock.advance(op[1])
        s = states(gate)
        assert len(s) == n
        busy = sum(x in (SlotState.HELD, SlotState.DRAINING, SlotState.STUCK) for x in s)
        assert busy <= n
        assert s.count(SlotState.HELD) == sum(
            1 for v in gate.slots() if v.state is SlotState.HELD and v.holder is not None
        )


@pytest.mark.td("G10")
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"slots": 0}, "slots must be at least 1"),
        ({"slots": -1}, "slots must be at least 1"),
        ({"drain_s": 0}, "drain_s must be greater than 0"),
        ({"drain_s": -1.0}, "drain_s must be greater than 0"),
        ({"drain_s": float("nan")}, "drain_s must be greater than 0"),
        ({"max_hold_s": 0}, "max_hold_s must be greater than 0"),
        ({"max_hold_s": float("nan")}, "max_hold_s must be greater than 0"),
        ({"retry_after_s": 0}, "retry_after_s must be at least 1"),
    ],
)
def test_g10_invalid_gate_settings_raise(kwargs, message):
    args = {"slots": 1, "retry_after_s": 15, "drain_s": 5.0, "max_hold_s": 600.0} | kwargs
    with pytest.raises(ValueError) as e:
        Gate(**args)
    assert str(e.value) == message


@pytest.mark.td("G10")
def test_g10_the_smallest_valid_settings_are_accepted():
    gate = Gate(slots=1, retry_after_s=1, drain_s=0.001, max_hold_s=0.001)
    assert len(gate.slots()) == 1


@pytest.mark.td("G11")
@pytest.mark.parametrize("jump", [86_400.0, -86_400.0])
def test_g11_wall_clock_moves_change_nothing(jump):
    gate, clock, wall = make(slots=2, retry_after_s=1, drain_s=5.0, max_hold_s=100.0)
    held = lease(gate, rid="h")
    lease(gate, rid="d").finish(clean=False)
    clock.advance(1.0)
    before = (gate.try_acquire("/api/chat", "x"), gate.slots(), gate.overdue())
    wall.advance(jump)
    assert (gate.try_acquire("/api/chat", "x"), gate.slots(), gate.overdue()) == before
    assert before[0] == Busy(retry_after_s=4, busy_for_s=1.0)
    assert held.info.started_wall == 1_700_000_000.0


@pytest.mark.td("G11")
def test_g11_the_overdue_boundary_is_strict():
    clock = Clock(0.0)
    gate = Gate(1, 15, 4.0, 64.0, clock=clock, wall=Clock(0.0))
    clock.advance(4.0)
    held = lease(gate)
    clock.advance(64.0 + 32.0)  # exactly max_hold_s + grace_s (all exact in binary)
    assert gate.overdue(grace_s=32.0) == []
    clock.advance(0.001)
    assert gate.overdue(grace_s=32.0) == [held.info]


@pytest.mark.td("G12")
def test_g12_retry_after_uses_the_earliest_drain():
    gate, clock, _ = make(slots=3, retry_after_s=1, drain_s=10.0)
    held = lease(gate, rid="h")
    a, b = lease(gate, rid="a"), lease(gate, rid="b")
    clock.advance(1.0)
    a.finish(clean=False)  # drains until +11
    clock.advance(5.0)
    b.finish(clean=False)  # drains until +16
    clock.advance(1.0)  # now +7: 4 s and 9 s left
    assert [v.drain_remaining_s for v in gate.slots()] == [None, 4.0, 9.0]
    assert gate.try_acquire("/api/chat", "x") == Busy(retry_after_s=4, busy_for_s=7.0)
    assert held.info.request_id == "h"


@pytest.mark.td("G12")
def test_g12_every_slot_draining_gives_busy_for_zero():
    gate, clock, _ = make(slots=2, retry_after_s=1, drain_s=10.0)
    lease(gate).finish(clean=False)
    clock.advance(3.0)
    lease(gate).finish(clean=False)
    assert gate.try_acquire("/api/chat", "x") == Busy(retry_after_s=7, busy_for_s=0.0)


@pytest.mark.td("G12")
def test_g12_busy_for_is_the_oldest_holder_and_ignores_stuck():
    gate, clock, _ = make(slots=3, retry_after_s=2)
    lease(gate, rid="s").stuck()
    clock.advance(1.0)
    lease(gate, rid="old")
    clock.advance(2.0)
    lease(gate, rid="new")
    clock.advance(3.0)
    assert gate.try_acquire("/api/chat", "x") == Busy(retry_after_s=2, busy_for_s=5.0)
