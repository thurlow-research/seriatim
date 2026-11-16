"""Tests for the CI scripts under tools/ci/ (SPEC REQ-51, REQ-61, REQ-63)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "ci" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mutation = load("mutation")
ip_literals = load("ip_literals")
pr_size = load("pr_size")
contract_lint = load("contract_lint")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def stats(**kw):
    base = dict.fromkeys(mutation.EXPECTED_KEYS, 0)
    base.update(kw)
    return base


# --- mutation score (REQ-61) ------------------------------------------------------------


def test_score_counts_timeouts_as_killed():
    assert mutation.score(stats(killed=6, timeout=2, survived=2, total=10)) == (0.8, 10)


def test_score_excludes_skipped_and_counts_no_tests_suspicious_segfault_against():
    s = stats(killed=3, skipped=4, no_tests=1, suspicious=1, segfault=1, total=10)
    assert mutation.score(s) == (0.5, 6)


def test_score_with_nothing_scored():
    assert mutation.score(stats(skipped=2, total=2)) == (1.0, 0)


@pytest.mark.parametrize(
    "bad, why",
    [
        ([], "not a JSON object"),
        ({k: 0 for k in mutation.EXPECTED_KEYS - {"segfault"}}, "unknown export format"),
        (stats(extra=0), "unknown export format"),
        (stats(killed="1", total=1), "not a non-negative integer"),
        (stats(killed=True, total=1), "not a non-negative integer"),
        (stats(killed=-1, total=-1), "not a non-negative integer"),
        (stats(killed=1, total=1, check_was_interrupted_by_user=1), "interrupted"),
        (stats(total=16), "16 of 16 mutants were not checked"),
    ],
)
def test_score_rejects_bad_exports(bad, why):
    with pytest.raises(mutation.GateError, match=why):
        mutation.score(bad)


def test_score_file_missing_and_malformed(tmp_path):
    with pytest.raises(mutation.GateError, match="no export"):
        mutation.score_file(tmp_path / "nope.json")
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(mutation.GateError, match="malformed"):
        mutation.score_file(tmp_path / "bad.json")


def test_main_score_threshold(tmp_path, capsys):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(stats(killed=3, survived=1, total=4)))
    assert mutation.main(["--score", str(f)]) == 0
    f.write_text(json.dumps(stats(killed=2, survived=2, total=4)))
    assert mutation.main(["--score", str(f)]) == 1
    assert "below 75 %" in capsys.readouterr().out
    f.write_text("{}")
    assert mutation.main(["--score", str(f)]) == 1


def test_set_only_mutate():
    text = '[tool.mutmut]\nonly_mutate = ["seriatim_node/*"]\nx = 1\n'
    out = mutation.set_only_mutate(text, ["seriatim_node/gate.py"])
    assert 'only_mutate = ["seriatim_node/gate.py"]\nx = 1' in out
    with pytest.raises(mutation.GateError):
        mutation.set_only_mutate("x = 1\n", ["a.py"])
    with pytest.raises(mutation.GateError):
        mutation.set_only_mutate(text + text, ["a.py"])


def test_by_package():
    files = ["node/seriatim_node/a.py", "gateway/g/b.py", "node/seriatim_node/c.py"]
    assert mutation.by_package(files) == {
        "node": ["seriatim_node/a.py", "seriatim_node/c.py"],
        "gateway": ["g/b.py"],
    }


def test_staged_copies_tests_and_contract_then_removes_them(tmp_path):
    for d in ("tests/node", "contract", "pkg"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "tests" / "node" / "test_x.py").write_text("")
    pdir = tmp_path / "pkg"
    with mutation.staged(pdir, root=tmp_path):
        assert (pdir / "tests" / "node" / "test_x.py").exists()
        assert (pdir / "contract").is_dir()
    assert not (pdir / "tests").exists() and not (pdir / "contract").exists()
    with pytest.raises(RuntimeError):
        with mutation.staged(pdir, root=tmp_path):
            raise RuntimeError
    assert not (pdir / "tests").exists()
    (pdir / "tests").mkdir()
    with pytest.raises(mutation.GateError, match="already exists"):
        with mutation.staged(pdir, root=tmp_path):
            pass


def test_changed_files_needs_a_sha():
    with pytest.raises(mutation.GateError, match="40-character"):
        mutation.changed_files("main")


# --- IP literals (REQ-51) ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        b"127.0.0.1",
        b"127.255.0.9",
        b"0.0.0.0",
        b"192.0.2.5",
        b"198.51.100.0/24",
        b"203.0.113.77",
        b"::1",
        b"[::1]:11434",
        b"::ffff:127.0.0.1 and ::ffff:7f00:1 and ::ffff:c000:205",
        b"version 0.40.16 and 1.2.3",
        b"12:34:56 and aa:bb:cc:dd:ee:ff",
        b'echo "::error::x" and std::vector and License :: OSI',
        b"sha256:abcdef0123456789",
        b"2026-11-16T10:00:00.123Z",
    ],
)
def test_ip_allowed_or_not_an_address(text):
    assert ip_literals.find(text) == []


def v4(*octets: int) -> str:
    """Build an address at run time, so the IP-literal check doesn't flag this file."""
    return ".".join(map(str, octets))


def v6(*groups: str) -> str:
    return ":".join(groups)


@pytest.mark.parametrize(
    "text, literal",
    [
        (f"host {v4(10, 1, 2, 3)}.", v4(10, 1, 2, 3)),
        (v4(8, 8, 8, 8), v4(8, 8, 8, 8)),
        (f"see {v4(192, 168, 1, 20)}:80", v4(192, 168, 1, 20)),
        (v6("fe80", "", "1"), v6("fe80", "", "1")),
        # REQ-51 allows only the IPv4 RFC 5737 ranges, not the IPv6 documentation range.
        (f"[{v6('2001', 'db8', '', '1')}]", v6("2001", "db8", "", "1")),
        (v6(*"12345678"), v6(*"12345678")),
        # "a", "", "b" joined is a valid IPv6 literal (a, six zero groups, b): flagged too.
        (f"x {v6('a', '', 'b')}", v6("a", "", "b")),
        (v6("", "", "ffff", v4(10, 0, 0, 9)), v6("", "", "ffff", v4(10, 0, 0, 9))),
        # IPv4-mapped in hex form has no dotted quad: judged by the address it maps to.
        (v6("", "", "ffff", "a00", "1"), v6("", "", "ffff", "a00", "1")),
        (f"[{v6('', '', 'ffff', 'a00', '1')}]:443", v6("", "", "ffff", "a00", "1")),
    ],
)
def test_ip_disallowed(text, literal):
    assert ip_literals.find(text.encode()) == [(1, literal)]


def test_ip_reports_line_numbers():
    assert ip_literals.find(f"a\nb\n{v4(10, 0, 0, 1)}\n".encode()) == [(3, v4(10, 0, 0, 1))]


# --- git fixtures -------------------------------------------------------------------------


def git_env() -> dict[str, str]:
    """A fixed identity and no user config, so tests behave the same on any machine."""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.invalid",
    }


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=git_env(), check=check
    )
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    (r / "README").write_text("base\n")
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "base")
    return r


def commit(repo: Path, files: dict[str, str | None], message: str = "change") -> str:
    for name, content in files.items():
        p = repo / name
        if content is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


# --- PR size (REQ-63) ---------------------------------------------------------------------


@needs_git
def test_pr_size_limits(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    base = run_git(repo, "rev-parse", "HEAD")
    head = commit(repo, {f"f{i}.txt": "x\n" for i in range(15)} | {"uv.lock": "lock\n"})
    assert pr_size.main(["--base", base, "--head", head]) == 0
    assert "15 changed files" in capsys.readouterr().out
    head = commit(repo, {"f15.txt": "x\n"})
    assert pr_size.main(["--base", base, "--head", head]) == 1
    big = commit(repo, {f"f{i}.txt": None for i in range(1, 16)} | {"big.txt": "y" * 150_001})
    files, size = pr_size.measure(head, big)
    assert size > pr_size.MAX_BYTES
    assert pr_size.main(["--base", head, "--head", big]) == 1


def test_pr_size_needs_shas():
    assert pr_size.main(["--base", "main", "--head", "HEAD"]) == 1


def test_lock_files_are_excluded():
    assert pr_size.is_lock("uv.lock") and pr_size.is_lock("node/uv.lock")
    assert not pr_size.is_lock("uv.lock.txt")


# --- contract lint (REQ-63) ---------------------------------------------------------------

MINIMAL = """openapi: 3.1.0
info: {title: t, version: "1"}
paths:
  /x:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {$ref: "#/components/schemas/%s"}
components:
  schemas:
    A: {type: object}
"""


def test_contract_lint(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert contract_lint.main() == 0
    assert "no contract/ directory yet" in capsys.readouterr().out
    (tmp_path / "contract").mkdir()
    assert contract_lint.main() == 1
    (tmp_path / "contract" / "openapi.yaml").write_text(MINIMAL % "A")
    assert contract_lint.main() == 0
    (tmp_path / "contract" / "openapi.yaml").write_text(MINIMAL % "Missing")
    assert contract_lint.main() == 1
    assert "#/components/schemas/Missing" in capsys.readouterr().out
    (tmp_path / "contract" / "openapi.yaml").write_text("openapi: 3.1.0\n")
    assert contract_lint.main() == 1


def test_unresolved_refs():
    doc = {"a": [{"x": 1}], "b": {"$ref": "#/a/0"}, "c": [{"$ref": "other.yaml#/x"}]}
    assert contract_lint.unresolved_refs(doc) == ["other.yaml#/x"]
    assert contract_lint.unresolved_refs({"a~/b": 1, "r": {"$ref": "#/a~0~1b"}}) == []
    assert contract_lint.unresolved_refs({"a": [1], "r": {"$ref": "#/a/5"}}) == ["#/a/5"]


def test_python_is_new_enough():
    assert sys.version_info >= (3, 12)
