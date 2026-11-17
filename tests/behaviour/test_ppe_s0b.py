"""S0b's PPE checks, run only by the `ppe` job against a PPE started with `ppe/ppe up --ci`.

Issue #9, answer 5(a): Ollama answers, and the three micro-models are present with their real
`parameter_size` metadata (SPEC §8.3, Models). S1 adds P1-P7 here; S0b part 2 (#26) adds the
fixture shape check. Black-box over HTTP (REQ-62): the base URL comes from SERIATIM_PPE_URL.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

import pytest

pytestmark = pytest.mark.behaviour

BASE_URL = os.environ.get("SERIATIM_PPE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_VERSION = "0.40.16"
# The real parameter_size Ollama reports, as a range: the PPE's size routing (max_params_b)
# is set between these, so each must be distinct and in its band.
SIZES = {
    "qwen2.5:0.5b": (0.3e9, 0.7e9),
    "qwen2.5:1.5b": (1.2e9, 1.9e9),
    "all-minilm:22m": (10e6, 40e6),
}
SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([KMBT])$")
UNIT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as r:  # nosec B310 # PPE loopback URL from CI
        assert r.status == 200
        return json.load(r)


def parameters(size: str) -> float:
    m = SIZE_RE.fullmatch(size)
    assert m, f"unexpected parameter_size {size!r}"
    return float(m.group(1)) * UNIT[m.group(2)]


def test_ollama_answers_with_the_pinned_version():
    assert get("/api/version") == {"version": OLLAMA_VERSION}


def test_the_three_models_are_present_with_their_real_parameter_size():
    models = {m["name"]: m for m in get("/api/tags")["models"]}
    assert set(SIZES) <= set(models), sorted(models)
    report = {}
    for name, (low, high) in SIZES.items():
        size = models[name]["details"]["parameter_size"]
        report[name] = size
        assert low <= parameters(size) <= high, f"{name}: parameter_size {size}"
    print(f"parameter_size: {report}")


def test_no_model_is_an_ollama_cp_alias():
    # An alias shares its source's digest and metadata, so the sizes would not be real.
    models = {m["name"]: m["digest"] for m in get("/api/tags")["models"] if m["name"] in SIZES}
    assert len(set(models.values())) == len(SIZES), models
