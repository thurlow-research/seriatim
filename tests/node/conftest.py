"""Fixtures for the real-socket harness in `harness.py` (TD-slice-1 §7)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from harness import (
    FakeOllama,
    RawUpstream,
    Sidecar,
    SidecarProcess,
    start_process,
    start_sidecar,
)


@pytest.fixture
def fake_ollama() -> Iterator[FakeOllama]:
    """A scripted upstream on an ephemeral loopback port, stopped at teardown."""
    fake = FakeOllama().start()
    yield fake
    fake.stop()


@pytest.fixture
def raw_upstream() -> Iterator[Callable[..., RawUpstream]]:
    """`raw_upstream(handler)` starts a socket fake (TD §7); every one stops at teardown."""
    started: list[RawUpstream] = []

    def start(handler, **kwargs) -> RawUpstream:
        started.append(RawUpstream(handler, **kwargs).start())
        return started[-1]

    yield start
    for fake in started:
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


@pytest.fixture
def sidecar_process(tmp_path: Path) -> Iterator[Callable[..., SidecarProcess]]:
    """`sidecar_process(ollama_url, lock_dir=None, **kwargs)` spawns `python -m seriatim_node`
    (TD §7). The lock directory defaults to one under `tmp_path`; every process is killed at
    teardown, with `Popen.kill()`, so it works the same on every OS."""
    started: list[SidecarProcess] = []

    def start(ollama_url: str, lock_dir: Path | None = None, **kwargs: object) -> SidecarProcess:
        proc = start_process(ollama_url, lock_dir or tmp_path / "lock", tmp_path / "logs", **kwargs)
        started.append(proc)
        return proc

    yield start
    for proc in started:
        proc.kill()
