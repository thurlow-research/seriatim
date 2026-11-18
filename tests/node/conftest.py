"""Fixtures for the real-socket harness in `harness.py` (TD-slice-1 §7)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from harness import FakeOllama, Sidecar, start_sidecar


@pytest.fixture
def fake_ollama() -> Iterator[FakeOllama]:
    """A scripted upstream on an ephemeral loopback port, stopped at teardown."""
    fake = FakeOllama().start()
    yield fake
    fake.stop()


@pytest.fixture
def sidecar() -> Iterator[Callable[..., Sidecar]]:
    """`sidecar(ollama_url, gate=None, **settings)` starts one; every one stops at teardown."""
    started: list[Sidecar] = []

    def start(ollama_url: str, **kwargs: object) -> Sidecar:
        car = start_sidecar(ollama_url, **kwargs)
        started.append(car)
        return car

    yield start
    for car in started:
        car.server.stop()
