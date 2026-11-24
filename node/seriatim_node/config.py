"""Sidecar settings: loading, validation and the default lock directory (TD-slice-1 §2.1, §6).

`load_settings` is the only way settings are built. It reads the TOML file named by
`SERIATIM_NODE_CONFIG`, then the `SERIATIM_NODE_*` variables (which win), and validates the
result. Every failure is a `ConfigError` whose message starts with the variable it is about.

The keys (TD-slice-2 §2.2, §6) are `[[keys]]` tables in that file only, never in the
environment (REQ-23; S2-R4). Error messages may name a key's id, never its `sha256`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from . import auth
from .auth import KeyEntry, Keyring
from .gate import Scope

PREFIX = "SERIATIM_NODE_"
CONFIG_VAR = "SERIATIM_NODE_CONFIG"
URL_VAR = "SERIATIM_NODE_OLLAMA_URL"
HOST_VAR = "SERIATIM_NODE_LISTEN_HOST"
LOCK_VAR = "SERIATIM_NODE_LOCK_DIR"
ALLOW_VAR = "SERIATIM_NODE_ALLOW_UNAUTHENTICATED"
KEYS_VAR = "SERIATIM_NODE_KEYS"
ALLOW_WARNING = ALLOW_VAR + "=true: the sidecar has no authentication (PPE and test mode only)"
ALLOW_WITH_KEYS_WARNING = ALLOW_VAR + "=true: test mode; keys are enforced (PPE and test mode only)"
NO_KEYS = (
    f"{KEYS_VAR}: no keys are configured; add [[keys]] to the {CONFIG_VAR} file"
    f" (seriatim-node keygen), or set {ALLOW_VAR}=true in the PPE and tests only"
)
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11435"
MAX_KEYS = auth.MAX_KEYS  # 256 (Q3); defined in auth.py, which Keyring needs (S2-1)

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Invalid configuration. The message names the offending key."""


def default_lock_dir() -> Path:
    """The per-OS directory where every production sidecar on a host meets (REQ-12b)."""
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "seriatim" / "run"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/seriatim/run")
    return Path("/run/seriatim")


class KeyConfig(BaseModel):
    """One `[[keys]]` table in the TOML file (TD-slice-2 §2.2, §6)."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")  # Q3
    scope: Literal["infer", "admin"]
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    model_config = ConfigDict(extra="forbid")


class Settings(BaseSettings):
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(11434, ge=1, le=65535)
    ollama_url: AnyHttpUrl = DEFAULT_OLLAMA_URL  # type: ignore[assignment]
    id: str = Field("node", pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    slots: int = Field(1, ge=1, le=64)
    retry_after_s: int = Field(15, ge=1, le=3600)
    drain_s: float = Field(10, ge=1, le=600)
    max_hold_s: float = Field(1800, ge=1, le=86400)
    connect_timeout_s: float = Field(5, gt=0, le=60)
    close_timeout_s: float = Field(5, gt=0, le=60)
    ungated_read_timeout_s: float = Field(300, gt=0, le=3600)
    max_body_bytes: int = Field(64 * 2**20, ge=1024)
    body_timeout_s: float = Field(30, gt=0, le=3600)
    health_probe_timeout_s: float = Field(2, gt=0, le=30)
    allow_unauthenticated: bool = False
    lock_dir: Path = Field(default_factory=default_lock_dir)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    keys: tuple[KeyConfig, ...] = ()  # from the TOML file only (S2-R4)

    model_config = SettingsConfigDict(
        env_prefix=PREFIX, extra="forbid", validate_default=True, allow_inf_nan=False
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Only what load_settings passes in: never os.environ or a .env file behind its back.
        return (init_settings,)


FIELDS = frozenset(Settings.model_fields)


def _var(field: object) -> str:
    return PREFIX + str(field).upper()


UNKNOWN_FIELD = "<unknown field>"  # stands for an unknown field's name, which the user chose


def _where(err: Mapping[str, Any]) -> str:
    """The variable an error is about: `SERIATIM_NODE_KEYS[0].sha256` inside `keys`.

    An unknown field's name comes from the file, so it could be a key or its hash: it is
    replaced with `UNKNOWN_FIELD`, never echoed.
    """
    first, *rest = err["loc"]
    if err["type"] == "extra_forbidden":
        rest[-1] = UNKNOWN_FIELD
    where = _var(first)
    for part in rest:
        where += f"[{part}]" if isinstance(part, int) else f".{part}"
    return where


def _read_file(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"{CONFIG_VAR}: cannot read {path!r} ({e})") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:  # TOML must be UTF-8
        raise ConfigError(f"{CONFIG_VAR}: {path!r} is not valid TOML: {e}") from None
    for key in data:
        if key not in FIELDS:
            raise ConfigError(f"{CONFIG_VAR}: unknown key {key!r} in {path!r}")
    return data


def _loopback_literal(host: str) -> bool:
    """True only for an IP literal in 127.0.0.0/8, or ::1. A name never resolves to true."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 4:
        return ip in ipaddress.ip_network("127.0.0.0/8")
    return ip == ipaddress.IPv6Address("::1")


def _check_ollama_url(raw: str) -> None:
    # pydantic has already parsed it as an http(s) URL with a valid port, if any.
    parts = urlsplit(raw)
    if parts.scheme != "http":
        raise ConfigError(f"{URL_VAR}: the scheme must be http")
    if "?" in raw or "#" in raw:
        raise ConfigError(f"{URL_VAR}: no query or fragment is allowed")
    if "@" in parts.netloc:
        raise ConfigError(f"{URL_VAR}: no user info is allowed")
    if parts.path not in ("", "/"):
        raise ConfigError(f"{URL_VAR}: the path must be empty or /")
    if not parts.port:
        raise ConfigError(f"{URL_VAR}: the port must be explicit, 1 to 65535")
    if not _loopback_literal(str(parts.hostname)):
        raise ConfigError(f"{URL_VAR}: the host must be a loopback IP literal (127.0.0.0/8 or ::1)")


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    """Build and validate the settings from `environ` and the optional TOML file (§2.1)."""
    # 1. The config file.
    values: dict[str, Any] = {}
    if CONFIG_VAR in environ:
        values.update(_read_file(environ[CONFIG_VAR]))

    # 2. Unknown SERIATIM_NODE_* variables. Other SERIATIM_* variables are ignored.
    for key in sorted(environ):
        if not key.startswith(PREFIX) or key == CONFIG_VAR:
            continue
        if key == KEYS_VAR:  # a field, but never from the environment (REQ-23; S2-R4)
            raise ConfigError(f"{KEYS_VAR}: keys are set in the {CONFIG_VAR} file only")
        field = key[len(PREFIX) :].lower()
        if key != _var(field) or field not in FIELDS:
            raise ConfigError(f"{key}: unknown variable")
        values[field] = environ[key]

    # 3. Build the settings; the environment overrides the file. Only the first error's `loc`
    # and `msg` are used: str(ValidationError) would include the input, a key's sha256 too.
    try:
        settings = Settings(**values)
    except ValidationError as e:
        err = e.errors()[0]
        raise ConfigError(f"{_where(err)}: {err['msg']}") from None

    # 4. The upstream URL.
    _check_ollama_url(str(values.get("ollama_url", DEFAULT_OLLAMA_URL)))

    # 5. The listen address: loopback only, whatever ALLOW_UNAUTHENTICATED says (S2-R2).
    if not _loopback_literal(settings.listen_host):
        raise ConfigError(
            f"{HOST_VAR}: {settings.listen_host!r} is not a loopback IP literal;"
            " a non-loopback address needs keys and the exposure check (S4a)"
        )

    # 6. The lock directory.
    if not settings.allow_unauthenticated and settings.lock_dir != default_lock_dir():
        raise ConfigError(
            f"{LOCK_VAR}: only the default {str(default_lock_dir())!r} is allowed"
            f" unless {ALLOW_VAR}=true"
        )

    # 7. The keys (TD-slice-2 §2.2). Ids may be named here; hashes never are.
    _check_keys(settings)

    # 8. The startup warning, once every check has passed.
    if settings.allow_unauthenticated:
        log.warning(ALLOW_WITH_KEYS_WARNING if settings.keys else ALLOW_WARNING)
    return settings


def _check_keys(settings: Settings) -> None:
    keys = settings.keys
    if len(keys) > MAX_KEYS:
        raise ConfigError(f"{KEYS_VAR}: at most {MAX_KEYS} keys, not {len(keys)}")
    ids: set[str] = set()
    owners: dict[bytes, str] = {}  # digest → the id of the first key that has it
    for key in keys:
        if key.id in ids:
            raise ConfigError(f"{KEYS_VAR}: the id {key.id!r} is repeated")
        digest = bytes.fromhex(key.sha256)  # as bytes, so the hex digits' case doesn't matter
        if digest in owners:
            raise ConfigError(f"{KEYS_VAR}: {owners[digest]!r} and {key.id!r} have the same sha256")
        ids.add(key.id)
        owners[digest] = key.id
    if not keys and not settings.allow_unauthenticated:
        raise ConfigError(NO_KEYS)


def keyring(settings: Settings) -> Keyring | None:
    """The keyring of `settings.keys`, or None with no keys (unauthenticated test mode).

    `load_settings` refuses no keys unless `allow_unauthenticated` is set, so None only happens
    in test mode. Configured keys are always enforced, whatever that flag says (S2-R1).
    """
    if not settings.keys:
        return None
    return Keyring(
        [KeyEntry(key.id, Scope(key.scope), bytes.fromhex(key.sha256)) for key in settings.keys]
    )
