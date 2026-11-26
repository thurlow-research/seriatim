"""Bearer keys: reading credentials, the keyring and the scope rule (TD-slice-2 §2.1, §4, §7).

`authorise` is the one place a request's credentials are judged (SPEC REQ-13, REQ-14). It
returns a `Decision`; the dispatcher maps its `Verdict` to an `ErrorCode`, so this module never
imports `app.py`. It imports only `gate.py` from the package, so `config.py` and `app.py` can
both import it without a cycle.

A key is never logged, echoed or kept: the keyring holds SHA-256 digests only, and a request's
key is hashed once and compared with every digest in constant time (REQ-13; ADR-001 D6, T4).
`keygen` (§7) prints a new key and its `[[keys]]` table, and nothing else: it reads no
configuration and writes no file.
"""

from __future__ import annotations

import argparse
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest
from typing import Any, TextIO

from .gate import Kind, Route, Scope

MAX_KEYS = 256  # TD-slice-2 Q3; config.py's MAX_KEYS is this one
DIGEST_SIZE = 32  # bytes in a SHA-256 digest
KEY_PREFIX = "seriatim_"  # Q2: lets a secret scanner recognise a leaked key (§7)

AUTHORIZATION = b"authorization"  # ASGI header names are lower case (h11 gives them so)
X_API_KEY = b"x-api-key"
BEARER = b"bearer"  # compared with the scheme in lower case (Q8)

TWO_HEADERS = "two Authorization headers"
OTHER_SCHEME = "a scheme other than Bearer"
WHITESPACE = "whitespace in the key"

# The health route as `gate.classify` gives it for `GET /seriatim/health` (§2.1 step 2).
_HEALTH = Route(Kind.SERIATIM, Scope.NONE, True, 200)


@dataclass(frozen=True, slots=True)
class Principal:
    """The key a request was authenticated with."""

    id: str  # the key id: logged, never the key
    scope: Scope  # Scope.INFER or Scope.ADMIN


@dataclass(frozen=True, slots=True)
class KeyEntry:
    id: str
    scope: Scope
    digest: bytes  # 32 bytes: bytes.fromhex(the configured sha256)


class Keyring:
    """The configured keys, as digests. Built once at startup; never changed."""

    __slots__ = ("_entries",)

    def __init__(self, entries: Sequence[KeyEntry]) -> None:
        """Take 1 to `MAX_KEYS` entries with unique ids and digests, or raise ValueError."""
        entries = tuple(entries)
        if not 1 <= len(entries) <= MAX_KEYS:
            raise ValueError(f"a keyring holds 1 to {MAX_KEYS} keys, not {len(entries)}")
        ids: set[str] = set()
        digests: set[bytes] = set()
        for entry in entries:
            if entry.scope is not Scope.INFER and entry.scope is not Scope.ADMIN:
                raise ValueError(f"key {entry.id!r}: the scope must be infer or admin")
            if len(entry.digest) != DIGEST_SIZE:
                raise ValueError(f"key {entry.id!r}: the digest must be {DIGEST_SIZE} bytes")
            if entry.id in ids:
                raise ValueError(f"key {entry.id!r}: the id is repeated")
            if entry.digest in digests:
                raise ValueError(f"key {entry.id!r}: the digest is repeated")
            ids.add(entry.id)
            digests.add(entry.digest)
        self._entries = entries

    def __len__(self) -> int:
        return len(self._entries)

    def match(self, token: bytes) -> Principal | None:
        """The principal whose digest is the token's SHA-256, or None.

        The token is hashed once, and the digest is compared with every entry's, in
        configuration order, without stopping at a match (REQ-13; T4). The work done per entry
        does not depend on where, or whether, the key matches.
        """
        digest = sha256(token).digest()
        found = None
        for entry in self._entries:  # all of them, always
            if compare_digest(digest, entry.digest):
                found = entry  # digests are unique: at most one match
        return None if found is None else Principal(found.id, found.scope)


# --- Reading the credential (§2.1) ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Missing:
    """No `Authorization` header, or one with no credential (Q10)."""


@dataclass(frozen=True, slots=True)
class Ambiguous:
    reason: str  # TWO_HEADERS, OTHER_SCHEME or WHITESPACE: never a header's value


@dataclass(frozen=True, slots=True)
class Presented:
    token: bytes  # the token's bytes, exactly as sent


def _is_space_or_tab(byte: int) -> bool:
    return byte == 0x20 or byte == 0x09


def read_credentials(headers: list[tuple[bytes, bytes]]) -> Missing | Ambiguous | Presented:
    """Read the Bearer token from `Authorization`, and from no other header (REQ-13; S2-R6).

    The steps of TD-slice-2 §2.1, in order: more than one header is ambiguous; none, or an
    empty value, is missing; the scheme (up to the first space or tab) must be `Bearer` in any
    case; the separator is one or more spaces, never a tab; no token is missing; a token with
    any byte below 0x21 is ambiguous. h11 has already stripped the value's outer whitespace.
    """
    values = [value for name, value in headers if name == AUTHORIZATION]
    if len(values) > 1:
        return Ambiguous(TWO_HEADERS)
    if not values or not values[0]:
        return Missing()
    value = values[0]
    end = 0
    while end < len(value) and not _is_space_or_tab(value[end]):
        end += 1
    if value[:end].lower() != BEARER:
        return Ambiguous(OTHER_SCHEME)
    start = end
    while start < len(value) and _is_space_or_tab(value[start]):
        start += 1
    if b"\t" in value[end:start]:
        return Ambiguous(WHITESPACE)
    token = value[start:]
    if not token:
        return Missing()
    if any(byte < 0x21 for byte in token):
        return Ambiguous(WHITESPACE)
    return Presented(token)


# --- Scopes and the decision (§2.1, §4) -----------------------------------------------------


def allows(have: Scope, need: Scope) -> bool:
    """True when a key of scope `have` may use a route that needs `need` (REQ-14).

    An `admin` key may use `infer` routes; an `infer` key never uses `admin` ones.
    """
    if need is Scope.NONE:
        return True
    if need is Scope.INFER:
        return have is Scope.INFER or have is Scope.ADMIN
    return have is Scope.ADMIN


class Verdict(StrEnum):
    ALLOW = "allow"
    MISSING = "missing"
    INVALID = "invalid"
    AMBIGUOUS = "ambiguous"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    principal: Principal | None  # set for ALLOW (with keys) and FORBIDDEN
    reason: str | None  # Ambiguous.reason, for the 400's message
    api_key_seen: bool  # an X-API-Key header was sent (the 401's hint, Q11)


_OPEN = Decision(Verdict.ALLOW, None, None, False)


def authorise(
    route: Route, headers: list[tuple[bytes, bytes]], keyring: Keyring | None
) -> Decision:
    """Judge a request's credentials for `route` (TD-slice-2 §2.1, steps 1 to 6).

    `keyring` is None only in unauthenticated test mode. There, and on `GET /seriatim/health`,
    the headers are not read at all (Q12; S2-R5).
    """
    if keyring is None or route == _HEALTH:
        return _OPEN
    api_key_seen = any(name == X_API_KEY for name, _ in headers)
    credentials = read_credentials(headers)
    if isinstance(credentials, Ambiguous):
        return Decision(Verdict.AMBIGUOUS, None, credentials.reason, api_key_seen)
    if isinstance(credentials, Missing):
        return Decision(Verdict.MISSING, None, None, api_key_seen)
    principal = keyring.match(credentials.token)
    if principal is None:
        return Decision(Verdict.INVALID, None, None, api_key_seen)
    if not allows(principal.scope, route.scope):
        return Decision(Verdict.FORBIDDEN, principal, None, api_key_seen)
    return Decision(Verdict.ALLOW, principal, None, api_key_seen)


def new_key() -> str:
    """A new key: `seriatim_` and 43 base64url characters, 32 random bytes (Q2; §7)."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


# --- keygen (§7) ------------------------------------------------------------------------------

# `config.KeyConfig.id`'s pattern (Q3), copied because this module cannot import config.py;
# test_auth.py checks that the two are the same.
KEY_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
SCOPES = ("infer", "admin")


class Once(argparse.Action):
    """argparse's `store`, except that a repeated option is an error, never the last value."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest) is not None:
            parser.error(f"argument {option_string}: given more than once")
        setattr(namespace, self.dest, values)


def _key_id(value: str) -> str:
    if re.fullmatch(KEY_ID_PATTERN, value) is None:
        raise argparse.ArgumentTypeError(f"{value!r} does not match {KEY_ID_PATTERN}")
    return value


def _keygen_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seriatim-node keygen",
        add_help=False,  # §7: any argument but --id and --scope is refused
        allow_abbrev=False,
    )
    parser.add_argument("--id", required=True, type=_key_id, action=Once)
    parser.add_argument("--scope", required=True, choices=SCOPES, action=Once)
    return parser


def keygen(argv: Sequence[str], out: TextIO) -> int:
    """`seriatim-node keygen --id <id> --scope infer|admin` (TD-slice-2 §7).

    Writes the new key, a blank line and its `[[keys]]` table to `out`, once, and returns 0.
    A bad, missing, repeated or extra argument prints argparse's usage on stderr and returns 2,
    with no key. It opens no file, reads no `SERIATIM_NODE_*` variable or config file, takes no
    lock and starts no server, so it works on a node whose configuration is not yet valid.
    """
    try:
        args = _keygen_parser().parse_args(list(argv))
    except SystemExit:  # argparse has printed its usage and the error on stderr
        return 2
    key = new_key()
    out.write(
        f'{key}\n\n[[keys]]\nid = "{args.id}"\nscope = "{args.scope}"\n'
        f'sha256 = "{sha256(key.encode("ascii")).hexdigest()}"\n'
    )
    return 0
