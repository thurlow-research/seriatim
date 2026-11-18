"""TD-slice-1 §8.1: configuration (C1 to C11; C8 is `main()`)."""

from __future__ import annotations

import inspect
import logging
import os
import sys
from pathlib import Path

import anyio
import pytest

from seriatim_node import app, config
from seriatim_node.config import ConfigError, Settings, default_lock_dir, load_settings

P = "SERIATIM_NODE_"
# The tests feed this to the validator to check it is refused; nothing binds to it.
ALL_INTERFACES = "0.0.0.0"  # nosec B104 # a test input for the listen-address check, never bound
FLOAT_FIELDS = [name for name, f in Settings.model_fields.items() if f.annotation is float]


def env(**kw: object) -> dict[str, str]:
    return {P + k.upper(): str(v) for k, v in kw.items()}


def refused(environ: dict[str, str], var: str) -> ConfigError:
    with pytest.raises(ConfigError) as e:
        load_settings(environ)
    assert var in str(e.value)
    return e.value


def toml_file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "node.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.mark.td("C1")
def test_defaults():
    s = load_settings({})
    assert s.model_dump() == {
        "listen_host": "127.0.0.1",
        "listen_port": 11434,
        "ollama_url": s.ollama_url,
        "id": "node",
        "slots": 1,
        "retry_after_s": 15,
        "drain_s": 10,
        "max_hold_s": 1800,
        "connect_timeout_s": 5,
        "close_timeout_s": 5,
        "ungated_read_timeout_s": 300,
        "max_body_bytes": 67108864,
        "body_timeout_s": 30,
        "health_probe_timeout_s": 2,
        "allow_unauthenticated": False,
        "lock_dir": default_lock_dir(),
        "log_level": "INFO",
    }
    assert str(s.ollama_url) == "http://127.0.0.1:11435/"


@pytest.mark.td("C1")
@pytest.mark.parametrize(
    ("platform", "program_data", "expected"),
    [
        ("linux", None, Path("/run/seriatim")),
        ("darwin", None, Path("/Library/Application Support/seriatim/run")),
        ("win32", r"D:\PD", Path(r"D:\PD") / "seriatim" / "run"),
        ("win32", None, Path(r"C:\ProgramData") / "seriatim" / "run"),
    ],
)
def test_default_lock_dir_per_os(monkeypatch, platform, program_data, expected):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("ProgramData", raising=False)
    if program_data:
        monkeypatch.setenv("ProgramData", program_data)
    assert default_lock_dir() == expected


@pytest.mark.td("C2")
def test_file_then_environment(tmp_path):
    path = toml_file(tmp_path, 'slots = 2\nid = "node-b"\nretry_after_s = 30\n')
    s = load_settings({P + "CONFIG": path, P + "SLOTS": "3"})
    assert (s.slots, s.id, s.retry_after_s) == (3, "node-b", 30)


@pytest.mark.td("C2")
def test_unknown_key_in_file(tmp_path):
    path = toml_file(tmp_path, "slotz = 2\n")
    err = refused({P + "CONFIG": path}, "slotz")
    assert P + "CONFIG" in str(err)


@pytest.mark.td("C2")
def test_a_bad_value_in_the_file_names_the_variable(tmp_path):
    path = toml_file(tmp_path, "slots = 0\n")
    refused({P + "CONFIG": path}, P + "SLOTS")


@pytest.mark.td("C3")
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SLOTS", "abc"),
        ("SLOTS", "0"),
        ("SLOTS", "-1"),
        ("SLOTS", "65"),
        ("RETRY_AFTER_S", "0"),
        ("DRAIN_S", "0"),
        ("DRAIN_S", "0.5"),
        ("MAX_HOLD_S", "0"),
        ("CLOSE_TIMEOUT_S", "0"),
    ],
)
def test_out_of_range_or_wrong_type(key, value):
    refused({P + key: value}, P + key)


@pytest.mark.td("C4")
@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.5:11435",
        "http://example.com:11435",
        "http://localhost:11435",
        f"http://{ALL_INTERFACES}:11435",
        "https://127.0.0.1:11435",
        "http://127.0.0.1:11435/x",
        "http://127.0.0.1:11435/?a",
        "http://127.0.0.1:11435/#f",
        "http://u:p@127.0.0.1:11435",
        "http://127.0.0.1",
        "http://127.0.0.1:",
        "http://127.0.0.1:0",
        "http://127.0.0.1:11435?",
        "http://127.0.0.1:11435#",
        "http://[::ffff:127.0.0.1]:11435",
    ],
)
def test_ollama_url_refused(url):
    refused({P + "OLLAMA_URL": url}, P + "OLLAMA_URL")


@pytest.mark.td("C4")
@pytest.mark.parametrize(
    "url",
    [
        "http://[::1]:11435",
        "http://127.0.0.2:11435",
        "http://127.255.0.1:1/",
        "http://127.0.0.1:65535",
    ],
)
def test_ollama_url_accepted(url):
    s = load_settings({P + "OLLAMA_URL": url})
    assert str(s.ollama_url).rstrip("/") == url.rstrip("/")


@pytest.mark.td("C4")
def test_ollama_url_in_the_file_is_checked_too(tmp_path):
    path = toml_file(tmp_path, 'ollama_url = "http://localhost:11435"\n')
    refused({P + "CONFIG": path}, P + "OLLAMA_URL")


@pytest.mark.td("C4")
def test_the_real_environment_is_never_read(monkeypatch):
    monkeypatch.setenv(P + "SLOTS", "5")
    assert load_settings({}).slots == 1


@pytest.mark.td("C5")
def test_non_loopback_listen_needs_allow_unauthenticated(caplog):
    refused(env(listen_host=ALL_INTERFACES), P + "LISTEN_HOST")
    with caplog.at_level(logging.WARNING, logger="seriatim_node.config"):
        s = load_settings(env(listen_host=ALL_INTERFACES, allow_unauthenticated="true"))
    assert s.listen_host == ALL_INTERFACES
    assert s.allow_unauthenticated is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [w.getMessage() for w in warnings] == [
        "SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true: the sidecar has no authentication"
        " (PPE and test mode only)"
    ]


@pytest.mark.td("C5")
def test_no_warning_by_default(caplog):
    with caplog.at_level(logging.DEBUG, logger="seriatim_node.config"):
        load_settings({})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.td("C5")
def test_lock_dir_needs_allow_unauthenticated(tmp_path):
    other = str(tmp_path / "x")
    err = refused(env(lock_dir=other), P + "LOCK_DIR")
    assert str(err) == (
        f"SERIATIM_NODE_LOCK_DIR: only the default {str(default_lock_dir())!r} is allowed"
        " unless SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true"
    )
    s = load_settings(env(lock_dir=other, allow_unauthenticated="true"))
    assert s.lock_dir == Path(other)


@pytest.mark.td("C5")
def test_the_default_lock_dir_may_be_named():
    assert load_settings(env(lock_dir=default_lock_dir())).lock_dir == default_lock_dir()


@pytest.mark.td("C6")
@pytest.mark.parametrize("key", [P + "SLOTZ", P + "slots", P + "Slots"])
def test_unknown_node_variable(key):
    refused({key: "1"}, key)


@pytest.mark.td("C6")
def test_other_seriatim_variables_are_ignored():
    s = load_settings(
        {
            "SERIATIM_APP_ID": "1",
            "SERIATIM_CONFIG_DIR": "/x",
            "SERIATIM_GW_FOO": "1",
            "SERIATIM_NODEX": "1",
        }
    )
    assert s == load_settings({})


@pytest.mark.td("C7")
def test_missing_config_file(tmp_path):
    refused({P + "CONFIG": str(tmp_path / "nonexistent.toml")}, P + "CONFIG")


@pytest.mark.td("C7")
def test_malformed_config_file(tmp_path):
    path = toml_file(tmp_path, "slots = = 2\n")
    refused({P + "CONFIG": path}, P + "CONFIG")


@pytest.mark.td("C7")
def test_config_file_not_utf8(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_bytes(b'id = "a\xff"\n')  # TOML must be UTF-8; tomllib raises UnicodeDecodeError
    assert "not valid TOML" in str(refused({P + "CONFIG": str(path)}, P + "CONFIG"))


@pytest.mark.td("C7")
def test_config_path_is_a_directory(tmp_path):
    refused({P + "CONFIG": str(tmp_path)}, P + "CONFIG")


C9_REFUSED = [
    ("LISTEN_PORT", "0"),
    ("LISTEN_PORT", "65536"),
    ("LISTEN_PORT", "x"),
    ("ID", "Node"),
    ("ID", "-a"),
    ("ID", "a" * 33),
    ("ID", ""),
    ("CONNECT_TIMEOUT_S", "0"),
    ("CONNECT_TIMEOUT_S", "61"),
    ("CLOSE_TIMEOUT_S", "0"),
    ("CLOSE_TIMEOUT_S", "61"),
    ("UNGATED_READ_TIMEOUT_S", "0"),
    ("UNGATED_READ_TIMEOUT_S", "3601"),
    ("MAX_BODY_BYTES", "1023"),
    ("BODY_TIMEOUT_S", "0"),
    ("BODY_TIMEOUT_S", "3601"),
    ("HEALTH_PROBE_TIMEOUT_S", "0"),
    ("HEALTH_PROBE_TIMEOUT_S", "31"),
    ("LOG_LEVEL", "debug"),
    ("LOG_LEVEL", "TRACE"),
    ("SLOTS", "1.5"),
    ("RETRY_AFTER_S", "3601"),
    ("DRAIN_S", "601"),
    ("MAX_HOLD_S", "86401"),
    ("ALLOW_UNAUTHENTICATED", "maybe"),
]


@pytest.mark.td("C9")
@pytest.mark.parametrize(("key", "value"), C9_REFUSED)
def test_remaining_ranges_and_types(key, value):
    refused({P + key: value}, P + key)


@pytest.mark.td("C9")
@pytest.mark.parametrize("field", FLOAT_FIELDS)
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_floats_must_be_finite(field, value):
    refused({P + field.upper(): value}, P + field.upper())


def test_float_fields_are_the_seven_in_the_td():
    assert sorted(FLOAT_FIELDS) == sorted(
        [
            "drain_s",
            "max_hold_s",
            "connect_timeout_s",
            "close_timeout_s",
            "ungated_read_timeout_s",
            "body_timeout_s",
            "health_probe_timeout_s",
        ]
    )


@pytest.mark.td("C9")
@pytest.mark.parametrize(
    ("key", "value", "attr", "expected"),
    [
        ("LISTEN_PORT", "1", "listen_port", 1),
        ("LISTEN_PORT", "65535", "listen_port", 65535),
        ("CONNECT_TIMEOUT_S", "60", "connect_timeout_s", 60),
        ("CLOSE_TIMEOUT_S", "60", "close_timeout_s", 60),
        ("UNGATED_READ_TIMEOUT_S", "3600", "ungated_read_timeout_s", 3600),
        ("BODY_TIMEOUT_S", "3600", "body_timeout_s", 3600),
        ("MAX_BODY_BYTES", "1024", "max_body_bytes", 1024),
        ("HEALTH_PROBE_TIMEOUT_S", "30", "health_probe_timeout_s", 30),
        ("SLOTS", "64", "slots", 64),
        ("RETRY_AFTER_S", "1", "retry_after_s", 1),
        ("RETRY_AFTER_S", "3600", "retry_after_s", 3600),
        ("DRAIN_S", "1", "drain_s", 1),
        ("DRAIN_S", "600", "drain_s", 600),
        ("MAX_HOLD_S", "86400", "max_hold_s", 86400),
        ("CONNECT_TIMEOUT_S", "0.001", "connect_timeout_s", 0.001),
        ("ID", "a" * 32, "id", "a" * 32),
        ("ID", "0-node", "id", "0-node"),
        ("LOG_LEVEL", "DEBUG", "log_level", "DEBUG"),
        ("LOG_LEVEL", "ERROR", "log_level", "ERROR"),
    ],
)
def test_boundaries_accepted(key, value, attr, expected):
    assert getattr(load_settings({P + key: value}), attr) == expected


@pytest.mark.td("C10")
@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="mode 000 doesn't stop Windows or root from reading",
)
def test_unreadable_config_file(tmp_path):
    path = toml_file(tmp_path, "slots = 2\n")
    os.chmod(path, 0)
    try:
        refused({P + "CONFIG": path}, P + "CONFIG")
    finally:
        os.chmod(path, 0o600)


@pytest.mark.td("C10")
def test_dotenv_is_ignored(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(P + "SLOTS=5\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_settings({}).slots == 1


@pytest.mark.td("C11")
@pytest.mark.parametrize(
    "host", ["192.0.2.10", "::", "example.com", "localhost", ALL_INTERFACES, "::ffff:127.0.0.1", ""]
)
def test_listen_host_refused(host):
    err = refused(env(listen_host=host), P + "LISTEN_HOST")
    assert str(err) == (
        f"SERIATIM_NODE_LISTEN_HOST: {host!r} is not a loopback IP literal;"
        " a non-loopback address needs SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true"
    )


@pytest.mark.td("C11")
@pytest.mark.parametrize("host", ["::1", "127.0.0.2", "127.0.0.1"])
def test_listen_host_loopback_accepted(host):
    assert load_settings(env(listen_host=host)).listen_host == host


def test_settings_reads_no_source_but_its_arguments(monkeypatch):
    monkeypatch.setenv(P + "SLOTS", "7")
    assert Settings().slots == 1
    assert config.FIELDS == frozenset(Settings.model_fields)


@pytest.fixture
def node_env(monkeypatch):
    """A real `os.environ` without any `SERIATIM_NODE_*`, for `main()`; uvicorn never runs
    (the fake records its call), and a lock that `main()` takes is closed afterwards."""
    for key in list(os.environ):
        if key.startswith(P):
            monkeypatch.delenv(key)
    monkeypatch.setattr(app, "_SINGLETON_FD", None)
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(app.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    yield calls
    if app._SINGLETON_FD is not None:
        os.close(app._SINGLETON_FD)


async def _get(asgi, path: bytes) -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "raw_path": path,
        "path": path.decode(),
        "query_string": b"",
        "headers": [],
    }
    await asgi(scope, receive, send)
    return sent


@pytest.mark.td("C8")
def test_main_invalid_config_exits_2(node_env, monkeypatch, capsys):
    monkeypatch.setenv(P + "SLOTS", "0")
    assert app.main() == 2
    assert "SERIATIM_NODE_SLOTS" in capsys.readouterr().err
    assert node_env == [] and app._SINGLETON_FD is None


@pytest.mark.td("C8")
def test_main_runs_uvicorn_with_one_h11_worker(node_env, monkeypatch, tmp_path):
    monkeypatch.setenv(P + "ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.setenv(P + "LOCK_DIR", str(tmp_path))
    monkeypatch.setenv(P + "LISTEN_PORT", "12345")
    monkeypatch.setenv(P + "ID", "node-c")
    real_create_app = app.create_app
    seen: list[tuple] = []

    def create_app(settings):
        dispatcher = real_create_app(settings)

        async def recording(scope, receive, send):
            seen.append((scope, receive, send))
            await dispatcher(scope, receive, send)

        return recording

    monkeypatch.setattr(app, "create_app", create_app)
    assert app.main() == 0
    ((args, kwargs),) = node_env
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 12345,
        "http": "h11",
        "workers": 1,
        "lifespan": "on",
        "log_config": None,
        "server_header": False,
        "date_header": True,
    }
    (asgi,) = args
    # A plain 3-argument ASGI function, so uvicorn can never take it for a factory.
    assert inspect.iscoroutinefunction(asgi)
    assert list(inspect.signature(asgi).parameters) == ["scope", "receive", "send"]
    start, _ = anyio.run(_get, asgi, b"/seriatim/nope")
    ((scope, receive, send),) = seen  # handed on unchanged
    assert scope["raw_path"] == b"/seriatim/nope" and callable(receive) and callable(send)
    assert start["status"] == 404
    assert (b"x-seriatim-node", b"node-c") in start["headers"]
    assert app._SINGLETON_FD is not None  # taken before uvicorn starts
    assert (tmp_path / "upstream-11435.lock").is_file()


def test_main_exits_3_when_it_cannot_lock(node_env, monkeypatch, tmp_path, capsys):
    parent = tmp_path / "f"
    parent.write_text("x", encoding="utf-8")
    monkeypatch.setenv(P + "ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.setenv(P + "LOCK_DIR", str(parent / "run"))
    assert app.main() == 3
    err = capsys.readouterr().err
    assert err.startswith("seriatim-node: cannot lock ") and str(parent) in err
    assert node_env == []
