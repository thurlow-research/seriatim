"""Fixtures for the real-socket harness in `harness.py` (TD-slice-1 §7)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from harness import (
    ContractBodies,
    FakeOllama,
    RawUpstream,
    Sidecar,
    SidecarProcess,
    check_contract_bodies,
    contract_validators,
    start_process,
    start_sidecar,
)

import seriatim_node.app as app_module
import seriatim_node.proxy as proxy_module


@pytest.fixture(scope="session")
def contract_schemas():
    """Validators for the contract's `ErrorEnvelope`, `StreamErrorRecord` and `Health`."""
    return contract_validators()


@pytest.fixture(autouse=True)
def contract_bodies(monkeypatch, contract_schemas) -> Iterator[ContractBodies]:
    """K-OAS2 (TD §7 "Error bodies"): collect every error body, in-stream record and health
    body the sidecar makes during a test, in process (the servers run in threads), and check
    each against the contract at teardown. A body that doesn't validate fails the test."""
    bodies = ContractBodies()
    response = app_module.ErrorFactory.response
    record = proxy_module.stream_error_record
    health = app_module._Dispatcher._health

    def collect_response(self, *args, **kwargs):
        result = response(self, *args, **kwargs)
        bodies.errors.append(result[2])
        return result

    def collect_record(*args, **kwargs):
        result = record(*args, **kwargs)
        bodies.records.append(result)
        return result

    async def collect_health(self, request):
        result = await health(self, request)
        bodies.health.append(result.body)
        return result

    monkeypatch.setattr(app_module.ErrorFactory, "response", collect_response)
    monkeypatch.setattr(proxy_module, "stream_error_record", collect_record)
    monkeypatch.setattr(app_module._Dispatcher, "_health", collect_health)
    yield bodies
    check_contract_bodies(bodies, contract_schemas)


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

    def start(handler) -> RawUpstream:
        started.append(RawUpstream(handler).start())
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
