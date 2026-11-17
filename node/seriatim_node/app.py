"""The sidecar's application module (TD-slice-1 §2.4).

S1-1 adds the error table: `ErrorCode`, `ERROR_TABLE` and `ErrorFactory`. `contract/openapi.yaml`
is the source of truth, and K-OAS3 checks that this table equals its `x-seriatim-codes`. The
dispatcher, health, `RequestLog` and the singleton lock follow in S1-5, and `main()` in S1-12.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from seriatim_node.__main__ import NOT_YET


class ErrorCode(StrEnum):
    """Exactly the contract's `ErrorCode` enum (ADR-001 §5.4; K-OAS3)."""

    GPU_BUSY = "GPU_BUSY"
    BAD_PATH = "BAD_PATH"
    AUTH_AMBIGUOUS = "AUTH_AMBIGUOUS"
    BAD_REQUEST_BODY = "BAD_REQUEST_BODY"
    NUM_CTX_AMBIGUOUS = "NUM_CTX_AMBIGUOUS"
    NUM_CTX_UNSUPPORTED_ROUTE = "NUM_CTX_UNSUPPORTED_ROUTE"
    NODE_REQUIRED = "NODE_REQUIRED"
    UNKNOWN_TASK = "UNKNOWN_TASK"
    AUTH_MISSING = "AUTH_MISSING"
    AUTH_INVALID = "AUTH_INVALID"
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"
    NOT_FOUND = "NOT_FOUND"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    REQUEST_BODY_TIMEOUT = "REQUEST_BODY_TIMEOUT"
    UPLOAD_INTERRUPTED = "UPLOAD_INTERRUPTED"
    BODY_TOO_LARGE = "BODY_TOO_LARGE"
    NUM_CTX_EXCEEDS_LIMIT = "NUM_CTX_EXCEEDS_LIMIT"
    MODEL_SIZE_UNKNOWN = "MODEL_SIZE_UNKNOWN"
    MODEL_NOT_ELIGIBLE = "MODEL_NOT_ELIGIBLE"
    INTERNAL = "INTERNAL"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_PROTOCOL_ERROR = "UPSTREAM_PROTOCOL_ERROR"
    NODE_STARTING = "NODE_STARTING"
    NODE_OFFLINE = "NODE_OFFLINE"
    EXPOSURE_UNVERIFIED = "EXPOSURE_UNVERIFIED"
    OLLAMA_EXPOSED = "OLLAMA_EXPOSED"
    NODE_SATURATED = "NODE_SATURATED"
    DISCOVERY_INCOMPLETE = "DISCOVERY_INCOMPLETE"
    SIZE_POLICY_MISSING = "SIZE_POLICY_MISSING"
    GPU_OFFLINE = "GPU_OFFLINE"
    FLEET_UNAVAILABLE = "FLEET_UNAVAILABLE"
    LOCK_HOLD_EXCEEDED = "LOCK_HOLD_EXCEEDED"
    HTTP_VERSION_UNSUPPORTED = "HTTP_VERSION_UNSUPPORTED"
    UPSTREAM_STREAM_BROKEN = "UPSTREAM_STREAM_BROKEN"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """One row of the error table. `status` is None for an in-stream-only code."""

    status: int | None
    retryable: bool
    replay_safe: bool


_E = ErrorCode
ERROR_TABLE: Mapping[ErrorCode, ErrorSpec] = MappingProxyType(
    {
        _E.GPU_BUSY: ErrorSpec(429, True, True),
        _E.BAD_PATH: ErrorSpec(400, False, True),
        _E.AUTH_AMBIGUOUS: ErrorSpec(400, False, True),
        _E.BAD_REQUEST_BODY: ErrorSpec(400, False, True),
        _E.NUM_CTX_AMBIGUOUS: ErrorSpec(400, False, True),
        _E.NUM_CTX_UNSUPPORTED_ROUTE: ErrorSpec(400, False, True),
        _E.NODE_REQUIRED: ErrorSpec(400, False, True),
        _E.UNKNOWN_TASK: ErrorSpec(400, False, True),
        _E.AUTH_MISSING: ErrorSpec(401, False, True),
        _E.AUTH_INVALID: ErrorSpec(401, False, True),
        _E.FORBIDDEN_SCOPE: ErrorSpec(403, False, True),
        _E.NOT_FOUND: ErrorSpec(404, False, True),
        _E.MODEL_NOT_FOUND: ErrorSpec(404, False, True),
        _E.METHOD_NOT_ALLOWED: ErrorSpec(405, False, True),
        _E.REQUEST_BODY_TIMEOUT: ErrorSpec(408, True, True),
        _E.UPLOAD_INTERRUPTED: ErrorSpec(408, True, False),
        _E.BODY_TOO_LARGE: ErrorSpec(413, False, True),
        _E.NUM_CTX_EXCEEDS_LIMIT: ErrorSpec(422, False, True),
        _E.MODEL_SIZE_UNKNOWN: ErrorSpec(422, False, True),
        _E.MODEL_NOT_ELIGIBLE: ErrorSpec(422, False, True),
        _E.INTERNAL: ErrorSpec(500, True, False),
        _E.UPSTREAM_UNAVAILABLE: ErrorSpec(502, True, True),
        _E.UPSTREAM_PROTOCOL_ERROR: ErrorSpec(502, True, False),
        _E.NODE_STARTING: ErrorSpec(503, True, True),
        _E.NODE_OFFLINE: ErrorSpec(503, False, True),
        _E.EXPOSURE_UNVERIFIED: ErrorSpec(503, True, True),
        _E.OLLAMA_EXPOSED: ErrorSpec(503, False, True),
        _E.NODE_SATURATED: ErrorSpec(503, True, True),
        _E.DISCOVERY_INCOMPLETE: ErrorSpec(503, True, True),
        _E.SIZE_POLICY_MISSING: ErrorSpec(503, False, True),
        _E.GPU_OFFLINE: ErrorSpec(503, False, True),
        _E.FLEET_UNAVAILABLE: ErrorSpec(503, True, True),
        _E.LOCK_HOLD_EXCEEDED: ErrorSpec(504, True, False),
        _E.HTTP_VERSION_UNSUPPORTED: ErrorSpec(505, False, True),
        _E.UPSTREAM_STREAM_BROKEN: ErrorSpec(None, True, False),
    }
)
del _E

# X-Seriatim-Gate for codes whose value never depends on the call site (TD §5): `busy` only
# for GPU_BUSY, and `refused` for the refusals given before Ollama is contacted (SPEC REQ-37).
_FIXED_GATE: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.GPU_BUSY: "busy",
        ErrorCode.NODE_STARTING: "refused",
        ErrorCode.NODE_OFFLINE: "refused",
        ErrorCode.EXPOSURE_UNVERIFIED: "refused",
        ErrorCode.OLLAMA_EXPOSED: "refused",
        ErrorCode.NUM_CTX_EXCEEDS_LIMIT: "refused",
    }
)
# Every other code is `acquired` when a slot was taken, otherwise `ungated`.
_CALLER_GATES = frozenset({"acquired", "ungated"})
# The optional ErrorEnvelope properties a call site may add (contract: additionalProperties
# is false, so any other key would make the body invalid).
_EXTRA_FIELDS = frozenset({"busy_for_s", "limit", "hint", "nodes"})

Headers = list[tuple[bytes, bytes]]


class ErrorFactory:
    """Builds every 4xx and 5xx the sidecar produces (TD §5, "Building error responses").

    `retryable` and `replay_safe` always come from `ERROR_TABLE`, never from the call site.
    """

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id

    def response(
        self,
        code: ErrorCode,
        message: str,
        *,
        retry_after: int | None = None,
        gate_value: str,
        request_id: str,
        **extra: object,
    ) -> tuple[int, Headers, bytes]:
        """Return `(status, headers, body)` for an `ErrorEnvelope` response.

        `gate_value` (`acquired` or `ungated`) is used unless the code fixes the gate header
        itself (`busy` for GPU_BUSY, `refused` for the pre-Ollama refusals). `Retry-After` is
        sent, and `retry_after` set in the body, whenever `retry_after` is given.
        """
        spec = ERROR_TABLE[code]
        if spec.status is None:
            raise ValueError(f"{code} is an in-stream record, not a response")
        gate = _FIXED_GATE.get(code)
        if gate is None:
            if gate_value not in _CALLER_GATES:
                raise ValueError(f"gate_value must be acquired or ungated, not {gate_value!r}")
            gate = gate_value
        unknown = set(extra) - _EXTRA_FIELDS
        if unknown:
            raise ValueError(f"not ErrorEnvelope fields: {sorted(unknown)}")
        if retry_after is not None and (
            isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after < 0
        ):
            raise ValueError(f"retry_after must be a whole number of seconds, not {retry_after!r}")

        envelope: dict[str, object] = {
            "error": message,
            "error_code": code.value,
            "retryable": spec.retryable,
            "replay_safe": spec.replay_safe,
        }
        if retry_after is not None:
            envelope["retry_after"] = retry_after
        envelope["source"] = "sidecar"
        envelope["node"] = self.node_id
        envelope.update(extra)
        body = json.dumps(envelope, separators=(",", ":"), allow_nan=False).encode()

        headers: Headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if retry_after is not None:
            headers.append((b"retry-after", str(retry_after).encode()))
        headers += [
            (b"x-seriatim-gate", gate.encode()),
            (b"x-seriatim-node", self.node_id.encode()),
            (b"x-request-id", request_id.encode()),
        ]
        return spec.status, headers, body


def main() -> int:
    """Placeholder until S1-12: keeps `python -m seriatim_node` and the console script
    exiting 2 with the S0a message now that this module exists."""
    print(NOT_YET, file=sys.stderr)
    return 2
