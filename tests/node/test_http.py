"""HTTP-level tests (TD-slice-1 §8.3). S1-1: the error table against the contract (K-OAS3)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from seriatim_node.app import ERROR_TABLE, ErrorCode, ErrorFactory, ErrorSpec, main

# Resolved from this file, so the same path works when mutmut copies tests/ and contract/
# into node/ (tools/ci/mutation.py).
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contract" / "openapi.yaml"


@pytest.fixture(scope="module")
def contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def envelope_validator(contract):
    """A validator for ErrorEnvelope whose local $refs resolve within the contract."""
    schema = {
        **contract["components"]["schemas"]["ErrorEnvelope"],
        "components": contract["components"],
    }
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.td("K-OAS3")
def test_error_code_enum_equals_the_contract(contract):
    assert {c.value for c in ErrorCode} == set(
        contract["components"]["schemas"]["ErrorCode"]["enum"]
    )


@pytest.mark.td("K-OAS3")
def test_error_table_equals_x_seriatim_codes(contract):
    codes = contract["components"]["x-seriatim-codes"]
    table = {
        code.value: {"status": s.status, "retryable": s.retryable, "replay_safe": s.replay_safe}
        for code, s in ERROR_TABLE.items()
    }
    assert table == codes
    assert set(ERROR_TABLE) == set(ErrorCode)


def test_error_table_is_read_only():
    with pytest.raises(TypeError):
        ERROR_TABLE[ErrorCode.GPU_BUSY] = ErrorSpec(200, False, False)  # type: ignore[index]


class _Factory:
    """Builds the factory per call, so mutation testing sees its constructor run."""

    def response(self, *args, **kwargs):
        return ErrorFactory("node-a").response(*args, **kwargs)


FACTORY = _Factory()


def _headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    names = [k for k, _ in raw]
    assert len(names) == len(set(names)), "no header is sent twice"
    assert all(k == k.lower() for k in names)
    return {k.decode(): v.decode() for k, v in raw}


def test_gpu_busy_matches_appendix_a(envelope_validator):
    status, raw, body = FACTORY.response(
        ErrorCode.GPU_BUSY,
        "GPU busy: another request is running on this node",
        retry_after=15,
        gate_value="ungated",
        request_id="r-1",
        busy_for_s=12.4,
    )
    assert status == 429
    assert body == (
        b'{"error":"GPU busy: another request is running on this node","error_code":"GPU_BUSY",'
        b'"retryable":true,"replay_safe":true,"retry_after":15,"source":"sidecar","node":"node-a",'
        b'"busy_for_s":12.4}'
    )
    assert _headers(raw) == {
        "content-type": "application/json",
        "content-length": str(len(body)),
        "retry-after": "15",
        "x-seriatim-gate": "busy",
        "x-seriatim-node": "node-a",
        "x-request-id": "r-1",
    }
    envelope_validator.validate(json.loads(body))


@pytest.mark.parametrize("code", [c for c in ErrorCode if ERROR_TABLE[c].status is not None])
@pytest.mark.parametrize("gate_value", ["acquired", "ungated"])
def test_every_response_code_uses_the_table(code, gate_value, envelope_validator):
    status, raw, body = FACTORY.response(code, "msg", gate_value=gate_value, request_id="rid")
    spec = ERROR_TABLE[code]
    doc = json.loads(body)
    assert status == spec.status
    assert doc == {
        "error": "msg",
        "error_code": code.value,
        "retryable": spec.retryable,
        "replay_safe": spec.replay_safe,
        "source": "sidecar",
        "node": "node-a",
    }
    headers = _headers(raw)
    assert "retry-after" not in headers
    assert headers["content-length"] == str(len(body))
    assert headers["x-request-id"] == "rid"
    assert headers["x-seriatim-node"] == "node-a"
    envelope_validator.validate(doc)


@pytest.mark.parametrize(
    ("code", "gate"),
    [
        (ErrorCode.GPU_BUSY, "busy"),
        (ErrorCode.NODE_STARTING, "refused"),
        (ErrorCode.NODE_OFFLINE, "refused"),
        (ErrorCode.EXPOSURE_UNVERIFIED, "refused"),
        (ErrorCode.OLLAMA_EXPOSED, "refused"),
        (ErrorCode.NUM_CTX_EXCEEDS_LIMIT, "refused"),
        (ErrorCode.BAD_PATH, "ungated"),
        (ErrorCode.LOCK_HOLD_EXCEEDED, "acquired"),
    ],
)
def test_gate_header(code, gate):
    caller = "acquired" if gate == "acquired" else "ungated"
    _, raw, _ = FACTORY.response(code, "m", gate_value=caller, request_id="r")
    assert _headers(raw)["x-seriatim-gate"] == gate


def test_fixed_gate_ignores_the_call_site():
    _, raw, _ = FACTORY.response(
        ErrorCode.NODE_STARTING, "m", gate_value="acquired", request_id="r"
    )
    assert _headers(raw)["x-seriatim-gate"] == "refused"


def test_retry_after_zero_is_sent():
    _, raw, body = FACTORY.response(
        ErrorCode.NODE_SATURATED, "m", retry_after=0, gate_value="ungated", request_id="r"
    )
    assert _headers(raw)["retry-after"] == "0"
    assert json.loads(body)["retry_after"] == 0


def test_headers_are_in_order():
    _, raw, _ = FACTORY.response(
        ErrorCode.NODE_STARTING, "m", retry_after=3, gate_value="ungated", request_id="r"
    )
    assert [k for k, _ in raw] == [
        b"content-type",
        b"content-length",
        b"retry-after",
        b"x-seriatim-gate",
        b"x-seriatim-node",
        b"x-request-id",
    ]


def test_extra_fields_are_appended(envelope_validator):
    _, _, body = FACTORY.response(
        ErrorCode.BODY_TOO_LARGE,
        "too big",
        gate_value="ungated",
        request_id="r",
        limit=1024,
        hint="h",
    )
    doc = json.loads(body)
    assert list(doc)[-2:] == ["limit", "hint"]
    assert doc["limit"] == 1024
    envelope_validator.validate(doc)


def test_in_stream_code_is_refused():
    with pytest.raises(ValueError, match="in-stream"):
        FACTORY.response(
            ErrorCode.UPSTREAM_STREAM_BROKEN, "m", gate_value="acquired", request_id="r"
        )


@pytest.mark.parametrize("bad", ["busy", "refused", "", "ACQUIRED"])
def test_bad_gate_value_is_refused(bad):
    with pytest.raises(ValueError, match="gate_value"):
        FACTORY.response(ErrorCode.INTERNAL, "m", gate_value=bad, request_id="r")


@pytest.mark.parametrize("key", ["retryable", "replay_safe", "source", "node", "error", "other"])
def test_unknown_or_core_extra_is_refused(key):
    with pytest.raises(ValueError, match="not ErrorEnvelope fields"):
        FACTORY.response(ErrorCode.INTERNAL, "m", gate_value="ungated", request_id="r", **{key: 1})


@pytest.mark.parametrize("bad", [-1, 1.5, True, "5"])
def test_bad_retry_after_is_refused(bad):
    with pytest.raises(ValueError, match="retry_after"):
        FACTORY.response(
            ErrorCode.GPU_BUSY, "m", retry_after=bad, gate_value="ungated", request_id="r"
        )


def test_non_finite_extra_is_refused():
    with pytest.raises(ValueError):
        FACTORY.response(
            ErrorCode.GPU_BUSY, "m", gate_value="ungated", request_id="r", busy_for_s=float("nan")
        )


def test_main_is_a_placeholder_until_s1_12(capsys):
    assert main() == 2
    assert "not implemented until slice 1" in capsys.readouterr().err


def test_node_id_comes_from_the_factory():
    _, raw, body = ErrorFactory("node-b").response(
        ErrorCode.INTERNAL, "m", gate_value="ungated", request_id="r"
    )
    assert json.loads(body)["node"] == "node-b"
    assert _headers(raw)["x-seriatim-node"] == "node-b"
