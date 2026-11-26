"""`auth.py` (TD-slice-2 §2.1, §4, §7): reading credentials (A1), the keyring, scopes and keys
(A2), and `authorise`; keys in the configuration (A3), the startup refusals (A4), and `keygen`
with `main(argv)` (A5).

Keys come from `new_key()`, never from a literal, so nothing that looks like a key is committed
(TD-slice-2 §8).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from harness import WAIT_S, ThreadServer, key_tables, log_lines, ready_gate, reserve_socket, status
from hypothesis import given
from hypothesis import strategies as st

import seriatim_node.auth as auth
from seriatim_node import app
from seriatim_node.auth import (
    MAX_KEYS,
    Ambiguous,
    Decision,
    KeyEntry,
    Keyring,
    Missing,
    Presented,
    Principal,
    Verdict,
    allows,
    authorise,
    new_key,
    read_credentials,
)
from seriatim_node.config import (
    ALLOW_WARNING,
    ALLOW_WITH_KEYS_WARNING,
    NO_KEYS,
    UNKNOWN_FIELD,
    ConfigError,
    KeyConfig,
    Settings,
    keyring,
    load_settings,
)
from seriatim_node.gate import Kind, Route, Scope, classify

TWO = Ambiguous("two Authorization headers")
SCHEME = Ambiguous("a scheme other than Bearer")
SPACE = Ambiguous("whitespace in the key")

# RFC 6750 §2.1: b64token = 1*( ALPHA / DIGIT / "-" / "." / "_" / "~" / "+" / "/" ) *"="
B64TOKEN = st.from_regex(rb"\A[A-Za-z0-9\-._~+/]+=*\Z", fullmatch=True)
KEY_RE = re.compile(r"seriatim_[A-Za-z0-9_-]{43}")


def auth_header(value: bytes) -> list[tuple[bytes, bytes]]:
    return [(b"host", b"node"), (b"authorization", value)]


def entry(key: str, id: str, scope: Scope = Scope.INFER) -> KeyEntry:
    return KeyEntry(id, scope, hashlib.sha256(key.encode("ascii")).digest())


def digest(n: int) -> bytes:
    return hashlib.sha256(str(n).encode()).digest()


class Unreadable(list):
    """Headers that fail the test if anything reads them."""

    def __iter__(self):
        raise AssertionError("the headers were read")


# --- A1: read_credentials ---------------------------------------------------------------------


@pytest.mark.td("A1")
def test_read_credentials_table() -> None:
    """A1 (REQ-13; Q8 to Q10): each form of `Authorization` gives its fixed result."""
    key = new_key().encode("ascii")
    cases: list[tuple[list[tuple[bytes, bytes]], object]] = [
        ([], Missing()),
        ([(b"host", b"node")], Missing()),
        (auth_header(b""), Missing()),
        (auth_header(b"Bearer"), Missing()),
        (auth_header(b"bearer"), Missing()),
        (auth_header(b"Bearer " + key), Presented(key)),
        (auth_header(b"bearer " + key), Presented(key)),
        (auth_header(b"BEARER " + key), Presented(key)),
        (auth_header(b"bEaReR " + key), Presented(key)),
        (auth_header(b"Bearer  " + key), Presented(key)),
        (auth_header(b"Bearer    " + key), Presented(key)),
        # Bytes from 0x21 up are the token's, whatever they are: the sidecar hashes them.
        (auth_header(b"Bearer a!b"), Presented(b"a!b")),
        (auth_header(b"Bearer !"), Presented(b"!")),
        (auth_header(b"Bearer \xff\x7f"), Presented(b"\xff\x7f")),
        (auth_header(b"Basic dXNlcjpwYXNz"), SCHEME),
        (auth_header(b"Digest x"), SCHEME),
        (auth_header(b"Token " + key), SCHEME),
        (auth_header(b"Bearerx " + key), SCHEME),
        (auth_header(b"Bearer," + key), SCHEME),
        (auth_header(b"Bearer" + key), SCHEME),
        (auth_header(b"Basic"), SCHEME),
        (auth_header(b"Bearer a b"), SPACE),
        (auth_header(b"Bearer a\tb"), SPACE),
        (auth_header(b"Bearer a, Bearer b"), SPACE),
        (auth_header(b"Bearer " + key + b" x"), SPACE),
        (auth_header(b"Bearer a\x00b"), SPACE),
        (auth_header(b"Bearer a\x1fb"), SPACE),
        (auth_header(b"Bearer\t" + key), SPACE),
        (auth_header(b"Bearer \t" + key), SPACE),
        (auth_header(b"Bearer\t " + key), SPACE),
        (auth_header(b"Bearer  \t  " + key), SPACE),
    ]
    for headers, want in cases:
        assert read_credentials(headers) == want, headers


@pytest.mark.td("A1")
def test_read_credentials_two_headers_are_ambiguous() -> None:
    """A1: two `Authorization` headers, equal or different, one of them valid → ambiguous."""
    key = new_key().encode("ascii")
    other = new_key().encode("ascii")
    for first, second in [
        (b"Bearer " + key, b"Bearer " + key),
        (b"Bearer " + key, b"Bearer " + other),
        (b"Bearer " + key, b"Basic x"),
        (b"", b"Bearer " + key),
        (b"Bearer " + key, b""),
    ]:
        headers = [(b"authorization", first), (b"host", b"node"), (b"authorization", second)]
        assert read_credentials(headers) == TWO
    three = [(b"authorization", b"Bearer " + key)] * 3
    assert read_credentials(three) == TWO


@pytest.mark.td("A1")
def test_read_credentials_ignores_every_other_place() -> None:
    """A1 (S2-R6): a key in `X-API-Key`, `Cookie` or `Proxy-Authorization`, or in the query
    string, is not a credential. `read_credentials` is given the headers only, so the query
    string never reaches it."""
    key = new_key().encode("ascii")
    for headers in [
        [(b"x-api-key", key)],
        [(b"cookie", b"key=" + key)],
        [(b"proxy-authorization", b"Bearer " + key)],
        [(b"authorisation", b"Bearer " + key)],
        [(b"x-authorization", b"Bearer " + key)],
        [(b"referer", b"http://client.invalid/api/tags?key=" + key)],
        [(b"x-api-key", key), (b"cookie", b"key=" + key), (b"proxy-authorization", key)],
    ]:
        assert read_credentials(headers) == Missing(), headers


@pytest.mark.td("A1")
def test_read_credentials_keeps_the_token_bytes_exactly() -> None:
    """A1: the token is the bytes after the separator, unchanged (no case change, no decode)."""
    key = new_key().encode("ascii")
    got = read_credentials(auth_header(b"BEARER  " + key.upper()))
    assert got == Presented(key.upper())
    assert type(got.token) is bytes


@pytest.mark.td("A1")
@given(token=B64TOKEN, spaces=st.integers(1, 8))
def test_read_credentials_round_trips_any_b64token(token: bytes, spaces: int) -> None:
    """A1 (Hypothesis): any `b64token` after one or more spaces is presented byte for byte."""
    assert read_credentials(auth_header(b"Bearer" + b" " * spaces + token)) == Presented(token)


@pytest.mark.td("A1")
@given(
    head=B64TOKEN,
    gap=st.lists(st.sampled_from([b" ", b"\t"]), min_size=1, max_size=4).map(b"".join),
    tail=B64TOKEN,
)
def test_read_credentials_embedded_whitespace_is_ambiguous(
    head: bytes, gap: bytes, tail: bytes
) -> None:
    """A1 (Hypothesis): a token with an embedded space or tab is ambiguous."""
    assert read_credentials(auth_header(b"Bearer " + head + gap + tail)) == SPACE


# --- A2: the keyring, scopes and new_key ------------------------------------------------------


def five_keys() -> tuple[list[str], Keyring]:
    keys = [new_key() for _ in range(5)]
    scopes = [Scope.INFER, Scope.ADMIN, Scope.INFER, Scope.INFER, Scope.ADMIN]
    entries = [entry(k, f"key-{i}", s) for i, (k, s) in enumerate(zip(keys, scopes, strict=True))]
    return keys, Keyring(entries)


@pytest.mark.td("A2")
def test_match_compares_every_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2 (REQ-13; T4): with 5 keys, `compare_digest` runs exactly 5 times whether the key
    matches the first entry, the third, the last or none, and SHA-256 runs once per call."""
    keys, ring = five_keys()
    compared: list[tuple[bytes, bytes]] = []
    hashed: list[bytes] = []
    real_compare, real_sha256 = auth.compare_digest, auth.sha256

    def counting_compare(a: bytes, b: bytes) -> bool:
        compared.append((a, b))
        return real_compare(a, b)

    def counting_sha256(data: bytes):
        hashed.append(data)
        return real_sha256(data)

    monkeypatch.setattr(auth, "compare_digest", counting_compare)
    monkeypatch.setattr(auth, "sha256", counting_sha256)
    cases = [
        (keys[0], Principal("key-0", Scope.INFER)),
        (keys[2], Principal("key-2", Scope.INFER)),
        (keys[4], Principal("key-4", Scope.ADMIN)),
        (new_key(), None),
    ]
    for key, want in cases:
        compared.clear()
        hashed.clear()
        token = key.encode("ascii")
        assert ring.match(token) == want
        assert len(compared) == 5
        assert hashed == [token]
        presented = hashlib.sha256(token).digest()
        assert all(a == presented for a, _ in compared)
        # In configuration order, each entry once.
        assert [b for _, b in compared] == [
            hashlib.sha256(k.encode("ascii")).digest() for k in keys
        ]


@pytest.mark.td("A2")
def test_match_returns_the_matching_principal() -> None:
    """A2: each configured key gives its own id and scope."""
    keys, ring = five_keys()
    assert len(ring) == 5
    scopes = [Scope.INFER, Scope.ADMIN, Scope.INFER, Scope.INFER, Scope.ADMIN]
    for i, (key, scope) in enumerate(zip(keys, scopes, strict=True)):
        assert ring.match(key.encode("ascii")) == Principal(f"key-{i}", scope)


@pytest.mark.td("A2")
def test_match_refuses_near_misses() -> None:
    """A2: a key differing in one character, the key's hex hash sent as the key, and an empty
    token match nothing."""
    keys, ring = five_keys()
    key = keys[2]
    last = key[-1]
    near = key[:-1] + ("A" if last != "A" else "B")
    first = "t" + key[1:]
    for token in [
        near.encode("ascii"),
        first.encode("ascii"),
        key.upper().encode("ascii"),
        key.encode("ascii") + b"=",
        hashlib.sha256(key.encode("ascii")).hexdigest().encode("ascii"),
        hashlib.sha256(key.encode("ascii")).digest(),
        b"",
    ]:
        assert ring.match(token) is None, token


@pytest.mark.td("A2")
def test_allows_every_pair() -> None:
    """A2 (REQ-14): only an `infer` key needing `admin` is refused; an `admin` key may use
    `infer` routes. (A `NONE` "have" is not a key scope; it is checked for completeness.)"""
    for have in Scope:
        for need in Scope:
            want = need is Scope.NONE or have is Scope.ADMIN or need is have
            assert allows(have, need) is want, (have, need)
    assert allows(Scope.INFER, Scope.NONE) is True
    assert allows(Scope.INFER, Scope.INFER) is True
    assert allows(Scope.INFER, Scope.ADMIN) is False
    assert allows(Scope.ADMIN, Scope.NONE) is True
    assert allows(Scope.ADMIN, Scope.INFER) is True
    assert allows(Scope.ADMIN, Scope.ADMIN) is True
    assert allows(Scope.NONE, Scope.INFER) is False
    assert allows(Scope.NONE, Scope.ADMIN) is False


@pytest.mark.td("A2")
def test_keyring_refuses_bad_entries() -> None:
    """A2: `Keyring` refuses 0 entries, 257, a repeated id and a repeated digest; it accepts
    1 and 256. Each message names the key id, never a digest."""
    with pytest.raises(ValueError, match=r"^a keyring holds 1 to 256 keys, not 0$"):
        Keyring([])
    many = [KeyEntry(f"k{i}", Scope.INFER, digest(i)) for i in range(MAX_KEYS + 1)]
    assert MAX_KEYS == 256
    with pytest.raises(ValueError, match=r"^a keyring holds 1 to 256 keys, not 257$"):
        Keyring(many)
    assert len(Keyring(many[:256])) == 256
    assert len(Keyring(many[:1])) == 1
    assert len(Keyring(iter(many[:3]))) == 3
    with pytest.raises(ValueError, match=r"^key 'k1': the id is repeated$"):
        Keyring([KeyEntry("k1", Scope.INFER, digest(1)), KeyEntry("k1", Scope.ADMIN, digest(2))])
    with pytest.raises(ValueError, match=r"^key 'k2': the digest is repeated$"):
        Keyring([KeyEntry("k1", Scope.INFER, digest(1)), KeyEntry("k2", Scope.ADMIN, digest(1))])


@pytest.mark.td("A2")
def test_keyring_refuses_a_bad_scope_or_digest() -> None:
    """A2 (S2-1 detail): an entry's scope is `infer` or `admin`, and its digest is 32 bytes."""
    with pytest.raises(ValueError, match=r"^key 'k': the scope must be infer or admin$"):
        Keyring([KeyEntry("k", Scope.NONE, digest(0))])
    for size in (0, 31, 33, 64):
        with pytest.raises(ValueError, match=r"^key 'k': the digest must be 32 bytes$"):
            Keyring([KeyEntry("k", Scope.INFER, b"\x01" * size)])
    ring = Keyring([KeyEntry("a", Scope.INFER, digest(0)), KeyEntry("b", Scope.ADMIN, digest(1))])
    assert len(ring) == 2


@pytest.mark.td("A2")
def test_new_key_form_and_uniqueness() -> None:
    """A2 (Q2; §7): `seriatim_` and 43 base64url characters that decode to 32 bytes; 100 calls
    give 100 different keys."""
    keys = [new_key() for _ in range(100)]
    assert len(set(keys)) == 100
    for key in keys:
        assert type(key) is str
        assert KEY_RE.fullmatch(key), key
        body = key.removeprefix("seriatim_")
        assert len(body) == 43
        assert len(base64.urlsafe_b64decode(body + "=")) == 32


# --- authorise (§2.1 steps 1 to 6; the http tests of S2-3 discharge its IDs) ------------------


INFER_ROUTE = classify("POST", b"/api/chat")
ADMIN_ROUTE = classify("POST", b"/api/pull")
HEALTH_ROUTE = classify("GET", b"/seriatim/health")


def ring_of(infer_key: str, admin_key: str) -> Keyring:
    return Keyring([entry(infer_key, "client-a"), entry(admin_key, "ops", Scope.ADMIN)])


def bearer(key: str, *extra: tuple[bytes, bytes]) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", b"Bearer " + key.encode("ascii")), *extra]


def test_authorise_test_mode_reads_nothing() -> None:
    """§2.1 step 1 (Q12): with no keyring every route is allowed and no header is read."""
    for route in (INFER_ROUTE, ADMIN_ROUTE, HEALTH_ROUTE, classify("OPTIONS", b"/api/tags")):
        assert authorise(route, Unreadable(), None) == Decision(Verdict.ALLOW, None, None, False)


def test_authorise_health_reads_nothing() -> None:
    """§2.1 step 2 (S2-R5): `GET /seriatim/health` is allowed without reading the headers."""
    assert HEALTH_ROUTE == Route(Kind.SERIATIM, Scope.NONE, True, 200)
    ring = ring_of(new_key(), new_key())
    assert authorise(HEALTH_ROUTE, Unreadable(), ring) == Decision(Verdict.ALLOW, None, None, False)


def test_authorise_reads_every_other_seriatim_route() -> None:
    """§2.1 step 2 covers health only: a `/seriatim/*` 404 or 405, and a known `/seriatim/*`
    route with another scope (as S4a's `/seriatim/status` will be), need a key."""
    ring = ring_of(new_key(), new_key())
    for route in (
        classify("GET", b"/seriatim/nope"),
        classify("POST", b"/seriatim/health"),
        classify("HEAD", b"/seriatim/health"),
        Route(Kind.SERIATIM, Scope.INFER, True, 200),
        Route(Kind.SERIATIM, Scope.NONE, False, 200),
        Route(Kind.UNGATED, Scope.NONE, True, 200),
    ):
        assert authorise(route, [], ring) == Decision(Verdict.MISSING, None, None, False), route


def test_authorise_verdicts() -> None:
    """§2.1 steps 3 to 6, in order: ambiguous, then missing, then invalid, then scope."""
    infer_key, admin_key = new_key(), new_key()
    ring = ring_of(infer_key, admin_key)
    client = Principal("client-a", Scope.INFER)
    ops = Principal("ops", Scope.ADMIN)
    two = bearer(admin_key, (b"authorization", b"Bearer " + admin_key.encode("ascii")))
    cases = [
        (ADMIN_ROUTE, two, Decision(Verdict.AMBIGUOUS, None, "two Authorization headers", False)),
        (
            INFER_ROUTE,
            [(b"authorization", b"Basic x")],
            Decision(Verdict.AMBIGUOUS, None, "a scheme other than Bearer", False),
        ),
        (
            INFER_ROUTE,
            [(b"authorization", b"Bearer a b")],
            Decision(Verdict.AMBIGUOUS, None, "whitespace in the key", False),
        ),
        (INFER_ROUTE, [], Decision(Verdict.MISSING, None, None, False)),
        (
            ADMIN_ROUTE,
            [(b"authorization", b"Bearer")],
            Decision(Verdict.MISSING, None, None, False),
        ),
        (INFER_ROUTE, bearer(new_key()), Decision(Verdict.INVALID, None, None, False)),
        (ADMIN_ROUTE, bearer(new_key()), Decision(Verdict.INVALID, None, None, False)),
        (ADMIN_ROUTE, bearer(infer_key), Decision(Verdict.FORBIDDEN, client, None, False)),
        (INFER_ROUTE, bearer(infer_key), Decision(Verdict.ALLOW, client, None, False)),
        (INFER_ROUTE, bearer(admin_key), Decision(Verdict.ALLOW, ops, None, False)),
        (ADMIN_ROUTE, bearer(admin_key), Decision(Verdict.ALLOW, ops, None, False)),
    ]
    for route, headers, want in cases:
        assert authorise(route, headers, ring) == want, (route, headers)


def test_authorise_scope_matrix() -> None:
    """§4: an `infer` key passes `infer` routes and gets FORBIDDEN on `admin` ones, unknown
    pairs included; an `admin` key passes both."""
    infer_key, admin_key = new_key(), new_key()
    ring = ring_of(infer_key, admin_key)
    infer_routes = [("GET", b"/api/tags"), ("POST", b"/api/chat"), ("GET", b"/v1/models")]
    admin_routes = [
        ("POST", b"/api/create"),
        ("DELETE", b"/api/delete"),
        ("POST", b"/api/blobs/sha256:" + b"0" * 64),
        ("OPTIONS", b"/api/tags"),
        ("POST", b"/API/chat"),
    ]
    for method, path in infer_routes:
        route = classify(method, path)
        assert authorise(route, bearer(infer_key), ring).verdict is Verdict.ALLOW
        assert authorise(route, bearer(admin_key), ring).verdict is Verdict.ALLOW
    for method, path in admin_routes:
        route = classify(method, path)
        assert authorise(route, bearer(infer_key), ring).verdict is Verdict.FORBIDDEN
        assert authorise(route, bearer(admin_key), ring).verdict is Verdict.ALLOW


def test_authorise_notes_x_api_key() -> None:
    """Q11: an `X-API-Key` header is never a credential, but the decision records it (the
    401's hint), whatever the verdict."""
    infer_key = new_key()
    ring = ring_of(infer_key, new_key())
    api_key = (b"x-api-key", infer_key.encode("ascii"))
    assert authorise(INFER_ROUTE, [api_key], ring) == Decision(Verdict.MISSING, None, None, True)
    assert authorise(INFER_ROUTE, [(b"x-api-keys", b"x")], ring).api_key_seen is False
    assert authorise(INFER_ROUTE, bearer(infer_key, api_key), ring) == Decision(
        Verdict.ALLOW, Principal("client-a", Scope.INFER), None, True
    )
    assert authorise(INFER_ROUTE, bearer(new_key(), api_key), ring).api_key_seen is True
    assert authorise(HEALTH_ROUTE, [api_key], ring).api_key_seen is False
    assert authorise(INFER_ROUTE, [api_key], None).api_key_seen is False


# --- Keys in the configuration (A3) ----------------------------------------------------------

P = "SERIATIM_NODE_"
CONFIG = P + "CONFIG"
KEYS = P + "KEYS"


def sha(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def table(**fields: str) -> str:
    return "[[keys]]\n" + "".join(f'{name} = "{value}"\n' for name, value in fields.items())


def config_file(tmp_path: Path, text: str) -> dict[str, str]:
    path = tmp_path / "node.toml"
    path.write_text(text, encoding="utf-8")
    return {CONFIG: str(path)}


def refused(environ: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as e:
        load_settings(environ)
    return str(e.value)


def hidden(message: str, *hashes: str) -> bool:
    """True when no hash in `hashes` is in `message`, in either case."""
    return not any(h.lower() in message.lower() for h in hashes)


@pytest.mark.td("A3")
def test_keys_load_in_order_and_match(tmp_path) -> None:
    infer_key, admin_key = new_key(), new_key()
    tables = key_tables([("client-a", "infer", infer_key), ("ops", "admin", admin_key)])
    settings = load_settings(config_file(tmp_path, tables))
    assert [(k.id, k.scope, k.sha256) for k in settings.keys] == [
        ("client-a", "infer", sha(infer_key)),
        ("ops", "admin", sha(admin_key)),
    ]
    ring = keyring(settings)
    assert isinstance(ring, Keyring) and len(ring) == 2
    assert ring.match(infer_key.encode("ascii")) == Principal("client-a", Scope.INFER)
    assert ring.match(admin_key.encode("ascii")) == Principal("ops", Scope.ADMIN)
    assert ring.match(new_key().encode("ascii")) is None


@pytest.mark.td("A3")
def test_an_upper_case_sha256_is_the_same_key(tmp_path) -> None:
    key = new_key()
    upper = load_settings(
        config_file(tmp_path, table(id="a", scope="infer", sha256=sha(key).upper()))
    )
    lower = load_settings(config_file(tmp_path, table(id="a", scope="infer", sha256=sha(key))))
    assert upper.keys[0].sha256 == sha(key).upper()
    assert bytes.fromhex(upper.keys[0].sha256) == bytes.fromhex(lower.keys[0].sha256)
    assert keyring(upper).match(key.encode("ascii")) == Principal("a", Scope.INFER)


MISSING = object()  # a field left out of the table

A3_BAD_FIELDS = [
    ("id", MISSING),
    ("scope", MISSING),
    ("sha256", MISSING),
    ("scope", "read"),
    ("id", "Client"),
    ("id", "-a"),
    ("id", "a" * 33),
    ("id", ""),
    ("sha256", "0" * 63),
    ("sha256", "0" * 65),
    ("sha256", "g" + "0" * 63),
]


@pytest.mark.td("A3")
@pytest.mark.parametrize(("field", "value"), A3_BAD_FIELDS)
def test_a_bad_table_names_its_index_and_field(tmp_path, field, value) -> None:
    good, bad = sha(new_key()), sha(new_key())
    fields = {"id": "b", "scope": "infer", "sha256": bad}
    if value is MISSING:
        del fields[field]
    else:
        fields[field] = value  # type: ignore[assignment]
    text = table(id="a", scope="admin", sha256=good) + table(**fields)
    message = refused(config_file(tmp_path, text))
    assert message.startswith(f"{KEYS}[1].{field}: "), message
    configured = [good, bad] + ([value] if field == "sha256" and value is not MISSING else [])
    assert hidden(message, *configured)  # type: ignore[arg-type]


@pytest.mark.td("A3")
@pytest.mark.parametrize("name", ["note", "digest"])
def test_an_unknown_field_is_refused_without_its_name(tmp_path, name) -> None:
    # The name comes from the file, so it could be a key's hash: the message never echoes it.
    good, bad = sha(new_key()), sha(new_key())
    field = name if name == "note" else sha(new_key())
    text = table(id="a", scope="admin", sha256=good) + table(
        id="b", scope="infer", sha256=bad, **{field: "x"}
    )
    message = refused(config_file(tmp_path, text))
    assert message.startswith(f"{KEYS}[1].{UNKNOWN_FIELD}: "), message
    assert field not in message
    assert hidden(message, good, bad, field)


@pytest.mark.td("A3")
def test_keys_must_be_tables(tmp_path) -> None:
    message = refused(config_file(tmp_path, 'keys = "x"\n'))
    assert message.startswith(f"{KEYS}: "), message


def tables_of(n: int) -> str:
    return "".join(table(id=f"k{i}", scope="infer", sha256=sha(str(i))) for i in range(n))


@pytest.mark.td("A3")
def test_at_most_256_keys(tmp_path) -> None:
    assert len(keyring(load_settings(config_file(tmp_path, tables_of(256))))) == 256
    assert refused(config_file(tmp_path, tables_of(257))) == f"{KEYS}: at most 256 keys, not 257"


@pytest.mark.td("A3")
def test_a_repeated_id_is_refused(tmp_path) -> None:
    first, second = sha(new_key()), sha(new_key())
    text = table(id="dup", scope="infer", sha256=first) + table(
        id="dup", scope="admin", sha256=second
    )
    message = refused(config_file(tmp_path, text))
    assert message == f"{KEYS}: the id 'dup' is repeated"


@pytest.mark.td("A3")
def test_a_repeated_sha256_in_any_case_is_refused(tmp_path) -> None:
    digest = sha(new_key())
    text = (
        table(id="c", scope="infer", sha256=sha(new_key()))
        + table(id="a", scope="infer", sha256=digest.lower())
        + table(id="b", scope="admin", sha256=digest.upper())
    )
    message = refused(config_file(tmp_path, text))
    assert message == f"{KEYS}: 'a' and 'b' have the same sha256"
    assert hidden(message, digest)


@pytest.mark.td("A3")
@pytest.mark.parametrize("valid_json", [True, False])
def test_keys_in_the_environment_are_refused(tmp_path, valid_json) -> None:
    key = new_key()
    value = json.dumps([{"id": "a", "scope": "infer", "sha256": sha(key)}]) if valid_json else "x"
    environ = config_file(tmp_path, key_tables([("base", "infer", new_key())])) | {KEYS: value}
    message = refused(environ)
    assert message == f"{KEYS}: keys are set in the {CONFIG} file only"
    assert hidden(message, sha(key))


# --- The startup refusals (A4) ---------------------------------------------------------------

NON_LOOPBACK = [".".join(["0"] * 4), "::", "192.0.2.10"]  # built: a literal trips Bandit B104


@pytest.fixture
def node_env(monkeypatch):
    """A real `os.environ` without any `SERIATIM_NODE_*`, for `main()`; uvicorn never runs
    (the fake records its call), and a lock that `main()` takes is closed afterwards."""
    for name in list(os.environ):
        if name.startswith(P):
            monkeypatch.delenv(name)
    monkeypatch.setattr(app, "_SINGLETON_FD", None)
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(app.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    yield calls
    if app._SINGLETON_FD is not None:
        os.close(app._SINGLETON_FD)


def warnings_in(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.td("A4")
def test_no_keys_and_no_flag_refuses_to_start(node_env, capsys) -> None:
    assert refused({}) == NO_KEYS
    assert NO_KEYS.startswith(f"{KEYS}: ")
    assert app.main([]) == 2
    assert capsys.readouterr().err == f"seriatim-node: {NO_KEYS}\n"
    assert node_env == [] and app._SINGLETON_FD is None


@pytest.mark.td("A4")
def test_no_keys_with_the_flag_starts_in_test_mode(node_env, monkeypatch, tmp_path, caplog) -> None:
    environ = {P + "ALLOW_UNAUTHENTICATED": "true", P + "LOCK_DIR": str(tmp_path)}
    with caplog.at_level(logging.WARNING, logger="seriatim_node.config"):
        settings = load_settings(environ)
    assert settings.keys == () and keyring(settings) is None
    assert warnings_in(caplog) == [ALLOW_WARNING]
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="seriatim_node.config"):
        assert app.main([]) == 0
    assert len(node_env) == 1
    assert warnings_in(caplog) == [ALLOW_WARNING]


@pytest.mark.td("A4")
@pytest.mark.parametrize("with_keys", [False, True])
@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("host", NON_LOOPBACK)
def test_a_non_loopback_listen_host_is_always_refused(
    node_env, monkeypatch, tmp_path, capsys, caplog, host, allow, with_keys
) -> None:
    """S2-R2: the flag never relaxes the listen address, with keys or without."""
    environ = {P + "LISTEN_HOST": host}
    if allow:
        environ[P + "ALLOW_UNAUTHENTICATED"] = "true"
    if with_keys:
        environ |= config_file(tmp_path, key_tables([("client-a", "infer", new_key())]))
    expected = (
        f"{P}LISTEN_HOST: {host!r} is not a loopback IP literal;"
        " a non-loopback address needs keys and the exposure check (S4a)"
    )
    with caplog.at_level(logging.WARNING, logger="seriatim_node.config"):
        assert refused(environ) == expected
    assert warnings_in(caplog) == []  # a refused start logs no startup warning
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    assert app.main([]) == 2
    assert capsys.readouterr().err == f"seriatim-node: {expected}\n"
    assert node_env == [] and app._SINGLETON_FD is None


@pytest.mark.td("A4")
@pytest.mark.parametrize("allow", [False, True])
def test_configured_keys_start_with_a_keyring(
    node_env, monkeypatch, tmp_path, caplog, allow
) -> None:
    """Keys, with the flag or without, start the sidecar, and its app enforces them (S2-R1);
    with the flag, the startup warning is the "keys are enforced" one."""
    key = new_key()
    environ = config_file(tmp_path, key_tables([("client-a", "infer", key)]))
    if allow:
        environ |= {P + "ALLOW_UNAUTHENTICATED": "true", P + "LOCK_DIR": str(tmp_path)}
    with caplog.at_level(logging.WARNING, logger="seriatim_node.config"):
        settings = load_settings(environ)
    ring = keyring(settings)
    assert isinstance(ring, Keyring)  # configured keys are always kept (S2-R1)
    assert ring.match(key.encode("ascii")) == Principal("client-a", Scope.INFER)
    assert warnings_in(caplog) == ([ALLOW_WITH_KEYS_WARNING] if allow else [])
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    built: list[object] = []
    create_app = app.create_app
    monkeypatch.setattr(app, "create_app", lambda s: built.append(create_app(s)) or built[-1])
    locked: list[object] = []  # without the flag the lock is the system one, so it is faked
    monkeypatch.setattr(app, "acquire_singleton", locked.append)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="seriatim_node.config"):
        assert app.main([]) == 0
    assert len(locked) == 1 and len(node_env) == 1  # the lock, then uvicorn.run: it serves
    assert warnings_in(caplog) == ([ALLOW_WITH_KEYS_WARNING] if allow else [])
    (served,) = built
    assert served.keyring.match(key.encode("ascii")) == Principal("client-a", Scope.INFER)


@pytest.mark.td("A4")
def test_create_app_refuses_no_keys_without_the_flag() -> None:
    """Fail closed (§2.4): `Settings` built directly, which `load_settings` never builds."""
    with pytest.raises(ConfigError) as e:
        app.create_app(Settings(keys=(), allow_unauthenticated=False))
    assert str(e.value) == NO_KEYS
    assert app.create_app(Settings(keys=(), allow_unauthenticated=True)).keyring is None


def test_create_app_refuses_keys_the_keyring_refuses() -> None:
    """`Settings` built directly with a repeated key: the keyring's refusal, as a
    `ConfigError` that names the id and never the hash (§2.2)."""
    key = KeyConfig(id="a", scope="infer", sha256=sha(new_key()))
    with pytest.raises(ConfigError) as e:
        app.create_app(Settings(keys=(key, key)))
    assert str(e.value) == f"{KEYS}: key 'a': the id is repeated"
    assert hidden(str(e.value), key.sha256)


# --- keygen (A5) -------------------------------------------------------------------------------

KEYGEN = ["keygen", "--id", "client-a", "--scope", "infer"]


@dataclass
class KeygenOnly:
    """The places `keygen` must leave alone (§7)."""

    cwd: Path
    lock_dir: Path

    def untouched(self) -> None:
        assert list(self.cwd.iterdir()) == []
        assert not self.lock_dir.exists()


@pytest.fixture
def keygen_only(monkeypatch, tmp_path) -> KeygenOnly:
    """`main()` as `keygen` runs: an empty working directory, an environment the sidecar
    refuses (an invalid `SLOTS`, a `CONFIG` that is a directory), and every serving step
    patched to fail the test if it is reached."""
    cwd, lock_dir, config_dir = tmp_path / "cwd", tmp_path / "lock", tmp_path / "config.d"
    cwd.mkdir()
    config_dir.mkdir()
    monkeypatch.chdir(cwd)
    for name in list(os.environ):
        if name.startswith(P):
            monkeypatch.delenv(name)
    monkeypatch.setenv(P + "SLOTS", "0")
    monkeypatch.setenv(CONFIG, str(config_dir))
    monkeypatch.setenv(P + "LOCK_DIR", str(lock_dir))
    with pytest.raises(ConfigError):
        load_settings()  # the environment is one the sidecar would refuse

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("keygen reached a step that serves")

    for name in ("load_settings", "acquire_singleton", "create_app"):
        monkeypatch.setattr(app, name, never)
    monkeypatch.setattr(app.uvicorn, "run", never)
    return KeygenOnly(cwd, lock_dir)


def printed_key(out: str, id: str, scope: str) -> str:
    """The key in `keygen`'s output, checked to be the whole of it: the key, a blank line and
    the table, with the key's SHA-256 (§7, Appendix A)."""
    key = out.split("\n", 1)[0]
    assert KEY_RE.fullmatch(key), out
    assert len(base64.urlsafe_b64decode(key.removeprefix("seriatim_") + "=")) == 32
    assert out == f'{key}\n\n[[keys]]\nid = "{id}"\nscope = "{scope}"\nsha256 = "{sha(key)}"\n'
    return key


@pytest.mark.td("A5")
def test_keygen_prints_a_key_and_its_table(keygen_only, capsys) -> None:
    assert app.main(KEYGEN) == 0
    first, err = capsys.readouterr()
    assert err == ""
    key = printed_key(first, "client-a", "infer")
    assert first == key + "\n\n" + key_tables([("client-a", "infer", key)]).removesuffix("\n")
    assert app.main(["keygen", "--scope", "admin", "--id", "ops"]) == 0
    second = printed_key(capsys.readouterr().out, "ops", "admin")
    assert second != key  # two runs, two keys
    keygen_only.untouched()


@pytest.mark.td("A5")
def test_keygen_writes_to_the_stream_it_is_given(keygen_only, capsys) -> None:
    out = io.StringIO()
    assert auth.keygen(["--id=ops", "--scope=admin"], out) == 0
    printed_key(out.getvalue(), "ops", "admin")
    assert capsys.readouterr() == ("", "")
    keygen_only.untouched()


@pytest.mark.td("A5")
def test_main_reads_its_arguments_from_sys_argv(keygen_only, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["seriatim-node", *KEYGEN])
    assert app.main() == 0
    printed_key(capsys.readouterr().out, "client-a", "infer")
    keygen_only.untouched()


@pytest.mark.td("A5")
def test_the_printed_table_loads_and_its_key_is_accepted(fake_ollama, tmp_path, capsys) -> None:
    fake_ollama.script("GET", "/api/tags", status(200, b'{"models":[]}'))
    assert auth.keygen(KEYGEN[1:], sys.stdout) == 0
    out = capsys.readouterr().out
    key = printed_key(out, "client-a", "infer")
    path = tmp_path / "node.toml"
    path.write_text(out.split("\n\n", 1)[1], encoding="utf-8")  # the table, as an operator would
    environ = {CONFIG: str(path), P + "OLLAMA_URL": fake_ollama.url, P + "ID": "node-a"}
    settings = load_settings(environ)
    assert keyring(settings).match(key.encode("ascii")) == Principal("client-a", Scope.INFER)
    server = ThreadServer(
        app.create_app(settings, gate=ready_gate()[0]), reserve_socket(), lifespan="on"
    ).start()
    try:
        url = f"http://127.0.0.1:{server.port}/api/tags"
        headers = {"authorization": f"Bearer {key}", "x-request-id": "a5"}
        assert httpx.get(url, headers=headers, timeout=WAIT_S).status_code == 200
    finally:
        server.stop()
    (line,) = [line for line in log_lines(capsys.readouterr().out) if line["request_id"] == "a5"]
    assert (line["client"], line["scope"], line["status"]) == ("client-a", "infer", 200)


ID_TWICE = "argument --id: given more than once\n"
SCOPE_TWICE = "argument --scope: given more than once\n"


def bad_id(value: str) -> str:
    return f"argument --id: {value!r} does not match {auth.KEY_ID_PATTERN}\n"


KEYGEN_REFUSED = [
    (["--id", "client-a", "--scope", "read"], "argument --scope: invalid choice: "),
    (["--id", "Bad", "--scope", "infer"], bad_id("Bad")),
    (["--id", "a\n", "--scope", "infer"], bad_id("a\n")),
    (["--id", "a" * 33, "--scope", "infer"], bad_id("a" * 33)),
    (["--scope", "infer"], "required: --id"),
    (["--id", "client-a"], "required: --scope"),
    ([], "required: --id, --scope"),
    (["--id", "a", "--id", "b", "--scope", "infer"], ID_TWICE),
    (["--id", "a", "--id", "a", "--scope", "infer"], ID_TWICE),
    (["--id", "a", "--scope", "infer", "--scope", "admin"], SCOPE_TWICE),
    (["--id", "a", "--scope", "infer", "--scope", "infer"], SCOPE_TWICE),
    (["--id", "a", "--scope", "infer", "extra"], "unrecognized arguments: extra"),
    (["--id", "a", "--scope", "infer", "--help"], "unrecognized arguments: --help"),
    (["--i", "a", "--scope", "infer"], "required: --id"),
]


@pytest.mark.td("A5")
@pytest.mark.parametrize(("argv", "error"), KEYGEN_REFUSED)
def test_keygen_refuses_bad_arguments(keygen_only, capsys, argv, error) -> None:
    assert app.main(["keygen", *argv]) == 2
    out, err = capsys.readouterr()
    assert out == ""  # no key
    assert err.startswith("usage: seriatim-node keygen --id ID --scope {infer,admin}\n"), err
    assert "seriatim-node keygen: error: " in err and error in err, err
    if error.endswith("\n"):  # the messages keygen writes itself are checked whole
        assert err.endswith(f"seriatim-node keygen: error: {error}"), err
    assert not re.search("seriatim_", err)
    keygen_only.untouched()


@pytest.mark.td("A5")
@pytest.mark.parametrize("argv", [["nope"], ["serve"], ["--id", "a"], ["", "keygen"]])
def test_main_refuses_any_other_command(keygen_only, capsys, argv) -> None:
    usage = "usage: seriatim-node [keygen --id <id> --scope infer|admin]\n"
    assert app.USAGE + "\n" == usage
    assert capsys.readouterr() == ("", "")
    assert app.main(argv) == 2
    assert capsys.readouterr() == ("", usage)
    keygen_only.untouched()


def test_keygen_ids_follow_the_key_id_rule() -> None:
    """auth.py's copy of the key-id pattern is `KeyConfig.id`'s (Q3)."""
    assert auth.KEY_ID_PATTERN == KeyConfig.model_json_schema()["properties"]["id"]["pattern"]
    assert auth.SCOPES == ("infer", "admin")
