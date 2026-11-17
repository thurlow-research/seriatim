"""Path safety, route classification, and the GPU gate (TD-slice-1 §2.2, §3).

`check_raw_path` and `classify` work on the raw path bytes and never decode them (SPEC
REQ-20a, REQ-5). The gate holds the slots, leases, the startup quarantine and the drain.

Every method is synchronous and never awaits, so admission is atomic on the single event
loop (TD §3.2). Times are monotonic (`clock`); the wall clock (`wall`) is only recorded in
`HolderInfo.started_wall` for display.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

# --- Path safety (SPEC REQ-20a) ------------------------------------------------------------


class PathError(ValueError):
    """The raw request path is refused before any routing (400 `BAD_PATH`)."""


# Compared with the lower-cased raw path, so `%2E` and `%2e` are both refused.
_FORBIDDEN_ESCAPES = (b"%2e", b"%2f", b"%5c", b"%00")


def check_raw_path(raw_path: bytes) -> None:
    """Raise PathError unless raw_path is safe to route and forward unchanged."""
    if not raw_path.startswith(b"/"):
        raise PathError("the path must start with /")
    if b"//" in raw_path:
        raise PathError("the path contains //")
    if b"\\" in raw_path:
        raise PathError("the path contains a backslash")
    if any(b < 0x20 or b == 0x7F for b in raw_path):
        raise PathError("the path contains a control byte")
    if any(seg in (b".", b"..") for seg in raw_path.split(b"/")):
        raise PathError("the path contains a . or .. segment")
    lowered = raw_path.lower()
    for escape in _FORBIDDEN_ESCAPES:
        if escape in lowered:
            raise PathError(f"the path contains {escape.decode()}")


# --- Classification (SPEC REQ-5) -----------------------------------------------------------


class Kind(StrEnum):
    SERIATIM = "seriatim"
    UNGATED = "ungated"
    GATED = "gated"


class Scope(StrEnum):
    NONE = "none"
    INFER = "infer"
    ADMIN = "admin"


@dataclass(frozen=True)
class Route:
    kind: Kind
    scope: Scope  # used from S2; S1 records it in the log only
    known: bool  # False for unlisted pairs (gated + admin)
    seriatim_status: int | None  # 200 known, 404 unknown path, 405 wrong method (SERIATIM only)


# Every table holds (method, raw_path); paths are compared as bytes, never decoded.
SERIATIM_ROUTES: Mapping[bytes, frozenset[str]] = MappingProxyType(
    {b"/seriatim/health": frozenset({"GET"})}
)
UNGATED_INFER: frozenset[tuple[str, bytes]] = frozenset(
    {
        ("GET", b"/"),
        ("HEAD", b"/"),
        ("GET", b"/api/tags"),
        ("GET", b"/api/ps"),
        ("GET", b"/api/version"),
        ("POST", b"/api/show"),
        ("GET", b"/v1/models"),
    }
)
UNGATED_ADMIN: frozenset[tuple[str, bytes]] = frozenset(  # plus HEAD/POST on BLOB_RE
    {
        ("POST", b"/api/pull"),
        ("POST", b"/api/push"),
        ("POST", b"/api/copy"),
        ("DELETE", b"/api/delete"),
    }
)
GATED_INFER: frozenset[tuple[str, bytes]] = frozenset(
    {
        ("POST", b"/api/generate"),
        ("POST", b"/api/chat"),
        ("POST", b"/api/embed"),
        ("POST", b"/api/embeddings"),
        ("POST", b"/v1/chat/completions"),
        ("POST", b"/v1/completions"),
        ("POST", b"/v1/embeddings"),
    }
)
GATED_ADMIN: frozenset[tuple[str, bytes]] = frozenset({("POST", b"/api/create")})
BLOB_RE = re.compile(rb"^/api/blobs/sha256:[0-9a-f]{64}$")  # used with fullmatch
BLOB_METHODS = frozenset({"HEAD", "POST"})

_UNGATED_INFER = Route(Kind.UNGATED, Scope.INFER, True, None)
_UNGATED_ADMIN = Route(Kind.UNGATED, Scope.ADMIN, True, None)
_GATED_INFER = Route(Kind.GATED, Scope.INFER, True, None)
_GATED_ADMIN = Route(Kind.GATED, Scope.ADMIN, True, None)
_UNKNOWN = Route(Kind.GATED, Scope.ADMIN, False, None)


def classify(method: str, raw_path: bytes) -> Route:
    """Classify a request by its exact method and raw path (TD §2.2 rules 1 to 5).

    Call it only after `check_raw_path` has passed. Methods are compared exactly, as h11
    delivers them, so `post` is not `POST` and falls to rule 5.
    """
    if raw_path == b"/seriatim" or raw_path.startswith(b"/seriatim/"):
        methods = SERIATIM_ROUTES.get(raw_path)
        if methods is None:
            return Route(Kind.SERIATIM, Scope.NONE, False, 404)
        if method not in methods:
            return Route(Kind.SERIATIM, Scope.NONE, False, 405)
        return Route(Kind.SERIATIM, Scope.NONE, True, 200)
    pair = (method, raw_path)
    if pair in UNGATED_INFER:
        return _UNGATED_INFER
    if pair in UNGATED_ADMIN or (method in BLOB_METHODS and BLOB_RE.fullmatch(raw_path)):
        return _UNGATED_ADMIN
    if pair in GATED_INFER:
        return _GATED_INFER
    if pair in GATED_ADMIN:
        return _GATED_ADMIN
    return _UNKNOWN


# --- The gate (TD §3) ----------------------------------------------------------------------


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
        if not slots >= 1:  # written so that NaN is refused too
            raise ValueError("slots must be at least 1")
        if not retry_after_s >= 1:
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
