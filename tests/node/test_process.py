"""TD-slice-1 §8.5: the sidecar as a process (R1 to R9), and the singleton lock (REQ-12a,
REQ-12b, REQ-23).

The R tests spawn `python -m seriatim_node` through the `sidecar_process` fixture and are
marked `process`. The unit tests of `acquire_singleton` at the end run in this process, so
that coverage and mutation testing see the lock code too.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import time
import types
from pathlib import Path

import httpx
import pytest
from harness import (
    PROCESS_S,
    TIMING_BOUND_S,
    WAIT_S,
    Hold,
    SidecarProcess,
    closed_port,
    raw_connect,
    status,
    wait_for,
)

from seriatim_node import app
from seriatim_node.app import SingletonError, acquire_singleton, lock_path
from seriatim_node.config import load_settings

DRAIN_S = 2.0  # wide enough that the first gated request always lands inside the quarantine
POLL_S = 0.02
GENERATE = b'{"model":"m","prompt":"p"}'
VERSION_OK = status(200, b'{"version":"0.40.16"}', [(b"content-type", b"application/json")])
POSIX_NOT_ROOT = pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions; root ignores them"
)


def _upstream() -> str:
    """An upstream nothing listens on: the lock tests never reach it."""
    return f"http://127.0.0.1:{closed_port()}"


def _lock_file(car: SidecarProcess, upstream: str) -> Path:
    return car.lock_dir / f"upstream-{httpx.URL(upstream).port}.lock"


def _gated(car: SidecarProcess) -> httpx.Response:
    return httpx.post(car.url + "/api/generate", content=GENERATE, timeout=WAIT_S)


def _serves(car: SidecarProcess) -> bool:
    """The sidecar answers HTTP itself (health may be degraded: Ollama is not running)."""
    resp = httpx.get(car.url + "/seriatim/health", timeout=WAIT_S)
    return resp.headers.get("x-seriatim-node") == "node-a" and car.proc.poll() is None


def _starting_then_admitted(car: SidecarProcess) -> None:
    """The first gated request is refused with NODE_STARTING, and every one is until
    `drain_s` has passed since the process started; then one is admitted (REQ-12a)."""
    listening = time.perf_counter()
    first = _gated(car)
    assert first.status_code == 503
    assert first.json()["error_code"] == "NODE_STARTING"
    assert 0 < int(first.headers["retry-after"]) <= DRAIN_S
    assert first.headers["x-seriatim-gate"] == "refused"
    deadline = time.monotonic() + DRAIN_S + WAIT_S
    while True:
        resp = _gated(car)
        if resp.status_code != 503:
            break
        assert resp.json()["error_code"] == "NODE_STARTING"
        assert time.monotonic() < deadline, "the quarantine never ended"
        time.sleep(POLL_S)
    admitted = time.perf_counter()
    assert resp.status_code == 200
    # The quarantine starts after the spawn and before the port listens.
    assert admitted - car.spawned_at >= DRAIN_S
    assert admitted - listening <= DRAIN_S + POLL_S + TIMING_BOUND_S


@pytest.mark.process
@pytest.mark.td("R1")
def test_r1_startup_quarantine(fake_ollama, sidecar_process):
    fake_ollama.script("POST", "/api/generate", status(200, b'{"done":true}'))
    car = sidecar_process(fake_ollama.url, drain_s=DRAIN_S)
    car.wait_listening()
    _starting_then_admitted(car)
    assert [r.raw_path for r in fake_ollama.requests] == [b"/api/generate"]  # 503s never reach it


@pytest.mark.process
@pytest.mark.td("R2")
def test_r2_kill_and_restart(fake_ollama, sidecar_process):
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    hold = Hold(headers=[(b"content-type", b"application/x-ndjson")], first=b'{"n":1}\n')
    fake_ollama.script("POST", "/api/generate", hold)
    car = sidecar_process(fake_ollama.url, drain_s=DRAIN_S)
    car.wait_listening()

    def ready() -> bool:
        health = httpx.get(car.url + "/seriatim/health", timeout=WAIT_S)
        return health.status_code == 200

    wait_for(ready, DRAIN_S + WAIT_S)
    with raw_connect(car.port) as sock:
        sock.sendall(
            b"POST /api/generate HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n%s"
            % (len(GENERATE), GENERATE)
        )
        buf = b""
        while b'{"n":1}' not in buf:  # the response has started: the request is in flight
            chunk = sock.recv(65536)
            assert chunk, f"the connection closed early: {buf!r}"
            buf += chunk
        killed = time.monotonic()
        car.proc.kill()
        gone = hold.wait_disconnected(0)
    assert gone - killed < 1.0
    car.kill()  # reaps it: the OS has released its lock

    fake_ollama.script("POST", "/api/generate", status(200, b'{"done":true}'))
    again = sidecar_process(fake_ollama.url, lock_dir=car.lock_dir, port=car.port, drain_s=DRAIN_S)
    again.wait_listening()
    _starting_then_admitted(again)


@pytest.mark.process
@pytest.mark.td("R3")
def test_r3_duplicate_start_exits_3(sidecar_process):
    upstream = _upstream()
    first = sidecar_process(upstream, gc_hook=True)
    first.wait_listening()
    lock = _lock_file(first, upstream)

    second = sidecar_process(upstream, lock_dir=first.lock_dir)
    assert second.wait_exit(WAIT_S) == 3
    assert f"another sidecar holds {lock}" in second.stderr()
    assert _serves(first)

    first.collect_garbage()  # nothing in the sidecar may release the lock when collected
    third = sidecar_process(upstream, lock_dir=first.lock_dir)
    assert third.wait_exit(WAIT_S) == 3
    assert str(lock) in third.stderr()
    assert _serves(first)


@pytest.mark.process
@pytest.mark.td("R4")
def test_r4_lock_dir_parent_is_a_file(tmp_path, sidecar_process):
    parent = tmp_path / "not-a-dir"
    parent.write_text("x", encoding="utf-8")
    car = sidecar_process(_upstream(), lock_dir=parent / "run")
    assert car.wait_exit() == 3
    assert "cannot lock" in car.stderr()


@pytest.mark.process
@pytest.mark.td("R5")
def test_r5_invalid_config_exits_2(sidecar_process):
    car = sidecar_process(_upstream(), slots=0)
    assert car.wait_exit() == 2
    assert "SERIATIM_NODE_SLOTS" in car.stderr()


@pytest.mark.process
@pytest.mark.td("R6")
def test_r6_concurrent_start_one_wins(tmp_path, sidecar_process):
    port = closed_port()
    lock_dir = tmp_path / "shared"
    cars = [
        sidecar_process(f"http://{host}:{port}", lock_dir=lock_dir)
        for host in ("127.0.0.1", "127.0.0.2", "127.0.0.1", "127.0.0.2")
    ]
    wait_for(lambda: sum(c.proc.poll() is not None for c in cars) >= 3, PROCESS_S)
    exited = [c for c in cars if c.proc.poll() is not None]
    (winner,) = [c for c in cars if c.proc.poll() is None]
    assert [c.proc.returncode for c in exited] == [3, 3, 3]
    for c in exited:
        assert f"another sidecar holds {lock_dir / f'upstream-{port}.lock'}" in c.stderr()
    winner.wait_listening()
    assert _serves(winner)


@POSIX_NOT_ROOT
@pytest.mark.process
@pytest.mark.td("R7")
@pytest.mark.parametrize("case", ["dir_0500", "file_000"])
def test_r7_permission_failures_exit_3(tmp_path, sidecar_process, case):
    upstream = _upstream()
    lock_dir = tmp_path / "run"
    lock_dir.mkdir()
    target = lock_dir
    if case == "dir_0500":
        lock_dir.chmod(0o500)
    else:
        target = lock_dir / f"upstream-{httpx.URL(upstream).port}.lock"
        target.touch()
        target.chmod(0o000)
    try:
        car = sidecar_process(upstream, lock_dir=lock_dir)
        assert car.wait_exit() == 3
        assert "cannot lock" in car.stderr()
    finally:
        target.chmod(0o700)


@pytest.mark.process
@pytest.mark.td("R8")
def test_r8_the_lock_file_keeps_its_inode(tmp_path, sidecar_process):
    upstream = _upstream()
    lock_dir = tmp_path / "run"
    inode = None
    for _ in range(3):
        car = sidecar_process(upstream, lock_dir=lock_dir)
        car.wait_listening()
        lock = _lock_file(car, upstream)
        inode = inode or lock.stat().st_ino
        car.proc.terminate()  # graceful on POSIX; on Windows it is TerminateProcess
        car.wait_exit()
        assert lock.exists()
        assert lock.stat().st_ino == inode


@pytest.mark.process
@pytest.mark.td("R9")
def test_r9_different_upstreams_are_independent(tmp_path, sidecar_process):
    lock_dir = tmp_path / "shared"
    cars = [sidecar_process(_upstream(), lock_dir=lock_dir) for _ in range(2)]
    for car in cars:
        car.wait_listening()
    assert all(_serves(car) for car in cars)
    assert len(list(lock_dir.iterdir())) == 2


# --- acquire_singleton in this process ------------------------------------------------------


@pytest.fixture
def singleton(monkeypatch):
    """Isolates `app._SINGLETON_FD`, and closes whatever a test locked."""
    monkeypatch.setattr(app, "_SINGLETON_FD", None)
    opened: list[int] = []
    real_open = os.open

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(app.os, "open", tracking_open)
    yield opened
    if app._SINGLETON_FD is not None:
        os.close(app._SINGLETON_FD)


def _settings(lock_dir: Path, port: int = 11435):
    return load_settings(
        {
            "SERIATIM_NODE_OLLAMA_URL": f"http://127.0.0.1:{port}",
            "SERIATIM_NODE_LOCK_DIR": str(lock_dir),
            "SERIATIM_NODE_ALLOW_UNAUTHENTICATED": "true",
        }
    )


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def test_lock_path_is_named_after_the_upstream_port(tmp_path):
    assert lock_path(_settings(tmp_path, 4321)) == tmp_path / "upstream-4321.lock"


def test_acquire_creates_the_directory_and_keeps_the_descriptor(tmp_path, singleton):
    lock_dir = tmp_path / "a" / "b"
    acquire_singleton(_settings(lock_dir))
    path = lock_dir / "upstream-11435.lock"
    assert path.is_file()
    assert singleton == [app._SINGLETON_FD]
    assert _is_open(app._SINGLETON_FD)
    assert os.path.samestat(os.fstat(app._SINGLETON_FD), path.stat())
    if os.name != "nt":
        umask = os.umask(0)
        os.umask(umask)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644 & ~umask


def test_acquire_twice_is_held_and_closes_the_loser(tmp_path, singleton):
    settings = _settings(tmp_path)
    acquire_singleton(settings)
    held = app._SINGLETON_FD
    with pytest.raises(SingletonError) as e:
        acquire_singleton(settings)
    assert str(e.value) == f"another sidecar holds {tmp_path / 'upstream-11435.lock'}"
    assert app._SINGLETON_FD == held
    loser = singleton[1]
    assert loser != held and not _is_open(loser)
    assert (tmp_path / "upstream-11435.lock").exists()


def test_acquire_on_a_file_parent_cannot_lock(tmp_path, singleton):
    parent = tmp_path / "f"
    parent.write_text("x", encoding="utf-8")
    with pytest.raises(SingletonError) as e:
        acquire_singleton(_settings(parent / "run"))
    message = str(e.value)
    assert message.startswith(f"cannot lock {parent / 'run' / 'upstream-11435.lock'}: ")
    assert "Errno" not in message  # the OS's reason only (strerror), not the whole OSError
    assert app._SINGLETON_FD is None


@pytest.mark.parametrize(
    ("platform", "error", "held"),
    [
        ("linux", BlockingIOError(errno.EWOULDBLOCK, "busy"), True),
        ("linux", PermissionError(errno.EACCES, "denied"), False),
        ("linux", OSError(errno.ENOLCK, "no locks"), False),
        ("win32", PermissionError(errno.EACCES, "locked"), True),
        ("win32", BlockingIOError(errno.EWOULDBLOCK, "busy"), False),
        ("win32", OSError(errno.EDEADLK, "deadlock"), False),
    ],
)
def test_a_failed_lock_is_held_or_cannot_lock(
    tmp_path, singleton, monkeypatch, platform, error, held
):
    def refuse(fd):
        raise error

    monkeypatch.setattr(app, "_lock", refuse)
    monkeypatch.setattr(app.sys, "platform", platform)
    with pytest.raises(SingletonError) as e:
        acquire_singleton(_settings(tmp_path))
    path = tmp_path / "upstream-11435.lock"
    if held:
        assert str(e.value) == f"another sidecar holds {path}"
    else:
        assert str(e.value) == f"cannot lock {path}: {error.strerror}"
    assert app._SINGLETON_FD is None and not _is_open(singleton[0])


def test_lock_uses_flock_on_posix(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(
        LOCK_EX=2, LOCK_NB=4, flock=lambda fd, flags: calls.append((fd, flags))
    )
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    monkeypatch.setattr(app.sys, "platform", "linux")
    app._lock(7)
    assert calls == [(7, 6)]


def test_lock_uses_msvcrt_on_windows(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(
        LK_NBLCK=2, LK_LOCK=1, locking=lambda fd, mode, n: calls.append((fd, mode, n))
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(app.sys, "platform", "win32")
    app._lock(7)
    assert calls == [(7, 2, 1)]
