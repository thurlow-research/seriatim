"""The GPU gate: slots, leases, the startup quarantine and the drain (TD-slice-1 §2.2, §3).

Path safety and classification (`check_raw_path`, `classify` and the route tables) are added
here by S1-3.

Every method is synchronous and never awaits, so admission is atomic on the single event
loop (TD §3.2). Times are monotonic (`clock`); the wall clock (`wall`) is only recorded in
`HolderInfo.started_wall` for display.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class SlotState(StrEnum):
    FREE = "free"
    HELD = "held"
    DRAINING = "draining"
    STUCK = "stuck"


@dataclass(frozen=True)
class HolderInfo:
    path: str
    request_id: str
    started_mono: float
    started_wall: float


@dataclass(frozen=True)
class SlotView:
    state: SlotState
    holder: HolderInfo | None
    drain_remaining_s: float | None


@dataclass(frozen=True)
class Busy:
    retry_after_s: int
    busy_for_s: float


@dataclass(frozen=True)
class Starting:
    retry_after_s: int


class _Slot:
    __slots__ = ("drain_until", "holder", "state")

    def __init__(self, drain_until: float) -> None:
        self.state = SlotState.DRAINING
        self.drain_until = drain_until
        self.holder: HolderInfo | None = None


class Lease:
    """One admission. Exactly one of `finish` or `stuck` takes effect (TD §3.1 invariant 2)."""

    def __init__(self, gate: Gate, slot: _Slot, info: HolderInfo, deadline: float) -> None:
        self._gate = gate
        self._slot = slot
        self._ended = False
        self.info = info
        self.deadline = deadline

    def _end(self) -> bool:
        if self._ended:
            return False
        self._ended = True
        self._slot.holder = None
        return True

    def finish(self, *, clean: bool) -> bool:
        """Free the slot (clean) or start its drain. True only on the first effective end."""
        if not self._end():
            return False
        if clean:
            self._slot.state = SlotState.FREE
        else:
            self._slot.state = SlotState.DRAINING
            self._slot.drain_until = self._gate._clock() + self._gate.drain_s
        return True

    def stuck(self) -> bool:
        """Mark the slot STUCK: closing upstream was not proven, so it is never freed."""
        if self._ended:
            return False
        info = self.info
        self._end()
        self._slot.state = SlotState.STUCK
        self._slot.holder = info
        return True


class Gate:
    def __init__(
        self,
        slots: int,
        retry_after_s: int,
        drain_s: float,
        max_hold_s: float,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        if slots < 1:
            raise ValueError("slots must be at least 1")
        if retry_after_s < 1:
            raise ValueError("retry_after_s must be at least 1")
        if not drain_s > 0:
            raise ValueError("drain_s must be greater than 0")
        if not max_hold_s > 0:
            raise ValueError("max_hold_s must be greater than 0")
        self.retry_after_s = retry_after_s
        self.drain_s = drain_s
        self.max_hold_s = max_hold_s
        self._clock = clock
        self._wall = wall
        # Startup quarantine (REQ-12a): every slot drains for drain_s first.
        self._starting_until = clock() + drain_s
        self._slots = [_Slot(self._starting_until) for _ in range(slots)]

    def _expire(self, now: float) -> None:
        for s in self._slots:
            if s.state is SlotState.DRAINING and s.drain_until <= now:
                s.state = SlotState.FREE

    def starting(self) -> bool:
        now = self._clock()
        self._expire(now)
        return now < self._starting_until

    def try_acquire(self, path: str, request_id: str) -> Lease | Busy | Starting:
        """Admit onto the first FREE slot, or say why not. Never awaits."""
        now = self._clock()
        self._expire(now)
        if now < self._starting_until:
            return Starting(retry_after_s=math.ceil(self._starting_until - now))
        for s in self._slots:
            if s.state is SlotState.FREE:
                info = HolderInfo(path, request_id, now, self._wall())
                lease = Lease(self, s, info, now + self.max_hold_s)
                s.state = SlotState.HELD
                s.holder = info
                return lease
        drains = [s.drain_until for s in self._slots if s.state is SlotState.DRAINING]
        retry = self.retry_after_s
        if drains:
            retry = max(retry, math.ceil(min(drains) - now))
        held = [
            s.holder.started_mono
            for s in self._slots
            if s.state is SlotState.HELD and s.holder is not None
        ]
        busy_for = now - min(held) if held else 0.0
        return Busy(retry_after_s=retry, busy_for_s=busy_for)

    def slots(self) -> list[SlotView]:
        now = self._clock()
        self._expire(now)
        return [
            SlotView(
                state=s.state,
                holder=s.holder,
                drain_remaining_s=s.drain_until - now if s.state is SlotState.DRAINING else None,
            )
            for s in self._slots
        ]

    def overdue(self, grace_s: float = 30) -> list[HolderInfo]:
        """Holders past max_hold_s + grace_s (strictly). Only HELD slots count."""
        now = self._clock()
        limit = self.max_hold_s + grace_s
        return [
            s.holder
            for s in self._slots
            if s.state is SlotState.HELD
            and s.holder is not None
            and now - s.holder.started_mono > limit
        ]

    def any_stuck(self) -> bool:
        return any(s.state is SlotState.STUCK for s in self._slots)
