"""`auth.py` (TD-slice-2 §2.1, §4, §7): reading credentials (A1), the keyring, scopes and keys
(A2), and `authorise`.

Keys come from `new_key()`, never from a literal, so nothing that looks like a key is committed
(TD-slice-2 §8).
"""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

import seriatim_node.auth as auth
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
