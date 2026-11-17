"""Tests for the CI scripts under tools/ci/ (SPEC REQ-51, REQ-61, REQ-63)."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import stat
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
private_terms = load("private_terms")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
posix_only = pytest.mark.skipif(os.name != "posix", reason="CI runs this on ubuntu only")


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


@pytest.mark.parametrize("outcome", ["passes", "mutmut fails", "raises"])
def test_run_package_restores_pyproject(tmp_path, monkeypatch, outcome):
    pdir = tmp_path / "node"
    pdir.mkdir()
    original = b'[tool.mutmut]\nonly_mutate = ["seriatim_node/*"]\nx = 1\n'
    (pdir / "pyproject.toml").write_bytes(original)
    seen = []

    def fake_run(cmd, cwd, check):
        seen.append((pdir / "pyproject.toml").read_text(encoding="utf-8"))
        if outcome == "raises":
            raise OSError("no uv")
        return subprocess.CompletedProcess(cmd, 1 if outcome == "mutmut fails" else 0)

    monkeypatch.setattr(mutation.subprocess, "run", fake_run)
    monkeypatch.setattr(mutation, "staged", lambda pdir: contextlib.nullcontext())
    monkeypatch.setattr(mutation, "generated_mutants", lambda pdir, files: 3)
    monkeypatch.setattr(mutation, "score_file", lambda path: (90.0, 3))
    if outcome == "passes":
        assert mutation.run_package(str(pdir), ["seriatim_node/gate.py"]) == (90.0, 3)
    else:
        with pytest.raises((mutation.GateError, OSError)):
            mutation.run_package(str(pdir), ["seriatim_node/gate.py"])
    assert 'only_mutate = ["seriatim_node/gate.py"]' in seen[0]  # edited during the run
    assert (pdir / "pyproject.toml").read_bytes() == original  # and restored after it


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
        b"Ruby::Cafe and Foo::Bar::baz and key:::1 and IP:" + b":".join([b"", b"", b"1"]),
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
        # Sentence punctuation after an address doesn't hide it.
        (f"host {v6('fe80', '', '1')}.", v6("fe80", "", "1")),
        (f"host {v6('fe80', '', '1')}, then", v6("fe80", "", "1")),
        (f"(host {v6('fe80', '', '1')})", v6("fe80", "", "1")),
        (f"host {v6('fe80', '', '1')}: up", v6("fe80", "", "1")),
        (f"{v6('a', '', '')}.", v6("a", "", "")),
        # A "label:" straight before an address doesn't hide it.
        (f"IP:{v6('fe80', '', '1')}", v6("fe80", "", "1")),
        (f"address:{v6('fe80', '', '1')}", v6("fe80", "", "1")),
        (f"ipv6:{v6('fe80', '', '1')}.", v6("fe80", "", "1")),
        (f"x_y-z:{v6('fe80', '', '1')}", v6("fe80", "", "1")),
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


@needs_git
def test_pr_size_counts_binaries_at_full_size(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = run_git(repo, "rev-parse", "HEAD")
    (repo / "zeros.bin").write_bytes(b"\0" * 1_000_000)  # compresses to a few KB as a patch
    head = commit(repo, {})
    files, size = pr_size.measure(base, head)
    assert files == ["zeros.bin"] and size >= 1_000_000
    assert pr_size.main(["--base", base, "--head", head]) == 1
    gone = commit(repo, {"zeros.bin": None})
    assert pr_size.measure(head, gone)[1] >= 1_000_000  # a deleted binary counts too


def test_mutation_counts_generated_mutants(tmp_path):
    pdir = tmp_path / "node"
    meta = pdir / "mutants" / "seriatim_node"
    meta.mkdir(parents=True)
    (meta / "__init__.py.meta").write_text('{"exit_code_by_key": {}}')
    (meta / "app.py.meta").write_text('{"exit_code_by_key": {"a": null, "b": 1}}')
    files = ["seriatim_node/__init__.py", "seriatim_node/app.py"]
    assert mutation.by_package(["node/" + f for f in files]) == {"node": files}
    assert mutation.generated_mutants(pdir, files[:1]) == 0
    assert mutation.generated_mutants(pdir, files) == 2
    with pytest.raises(mutation.GateError):
        mutation.generated_mutants(pdir, ["seriatim_node/missing.py"])
    (meta / "bad.py.meta").write_text('{"exit_code_by_key": []}')
    with pytest.raises(mutation.GateError):
        mutation.generated_mutants(pdir, ["seriatim_node/bad.py"])


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


@pytest.mark.parametrize("version", ["3.0.3", "3.1", "'3.2.0'", "3.1.0-rc"])
def test_contract_lint_requires_openapi_31(tmp_path, monkeypatch, capsys, version):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contract").mkdir()
    doc = MINIMAL.replace("openapi: 3.1.0", f"openapi: {version}") % "A"
    (tmp_path / "contract" / "openapi.yaml").write_text(doc)
    assert contract_lint.main() == 1
    assert "openapi must be 3.1.x" in capsys.readouterr().out


def test_unresolved_refs():
    doc = {"a": [{"x": 1}], "b": {"$ref": "#/a/0"}, "c": [{"$ref": "other.yaml#/x"}]}
    assert contract_lint.unresolved_refs(doc) == ["other.yaml#/x"]
    assert contract_lint.unresolved_refs({"a~/b": 1, "r": {"$ref": "#/a~0~1b"}}) == []
    assert contract_lint.unresolved_refs({"a": [1], "r": {"$ref": "#/a/5"}}) == ["#/a/5"]


# --- private terms (REQ-63) ---------------------------------------------------------------

TERMS = private_terms.parse_terms("  CanaryHost-7f3a \n\nalice example\n")


def scan_range(repo: Path, base: str, head: str):
    gitdir = str(repo / ".git")
    commits = private_terms.commit_range(gitdir, head, base)
    return private_terms.matches(private_terms.collect(gitdir, commits, None), TERMS)


def test_parse_terms():
    assert TERMS == [b"alice example", b"canaryhost-7f3a"]
    assert private_terms.parse_terms("\n \n") == []


@needs_git
@posix_only
def test_term_added_then_deleted_is_still_found(repo):
    base = run_git(repo, "rev-parse", "HEAD")
    commit(repo, {"notes.md": "deploy to canaryhost-7f3A\n"})
    head = commit(repo, {"notes.md": "deploy somewhere\n"})
    found = scan_range(repo, base, head)
    assert len(found) == 1 and "notes.md" in found[0] and found[0].startswith("blob ")


@needs_git
@posix_only
def test_term_introduced_only_by_a_merge_resolution_is_found(repo):
    base = run_git(repo, "rev-parse", "HEAD")
    run_git(repo, "switch", "-q", "-c", "side")
    commit(repo, {"f.txt": "side\n"})
    run_git(repo, "switch", "-q", "main")
    commit(repo, {"f.txt": "main\n"})
    run_git(repo, "merge", "-q", "side", check=False)  # an add/add conflict, resolved below
    assert (repo / ".git" / "MERGE_HEAD").exists()
    (repo / "f.txt").write_text("resolved with CANARYHOST-7F3A\n")
    run_git(repo, "add", "f.txt")
    run_git(repo, "commit", "-q", "--no-edit")
    head = run_git(repo, "rev-parse", "HEAD")
    found = scan_range(repo, base, head)
    assert any("f.txt" in f for f in found)


@needs_git
@posix_only
def test_terms_in_paths_and_commit_metadata(repo):
    base = run_git(repo, "rev-parse", "HEAD")
    commit(repo, {"hosts/canaryhost-7f3a.txt": "clean\n"}, message="Alice Example did this")
    head = commit(repo, {"hosts/canaryhost-7f3a.txt": None})
    found = scan_range(repo, base, head)
    assert any(f.startswith("path hosts/canaryhost-7f3a.txt") for f in found)
    assert any(f.startswith("commit ") and "alice example" in f for f in found)
    assert not any(f.startswith("blob ") for f in found)


@needs_git
@posix_only
def test_clean_range_and_whole_tree(repo):
    base = run_git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"a/b.txt": "fine\n"})
    assert scan_range(repo, base, head) == []
    gitdir = str(repo / ".git")
    blobs, paths = private_terms.whole_tree(gitdir, head)
    assert sorted(paths) == [b"README", b"a/b.txt"] and len(blobs) == 2


@needs_git
@posix_only
def test_scan_errors(repo):
    gitdir = str(repo / ".git")
    head = run_git(repo, "rev-parse", "HEAD")
    with pytest.raises(private_terms.ScanError, match="no commits"):
        private_terms.commit_range(gitdir, head, head)
    with pytest.raises(private_terms.ScanError, match="missing"):
        private_terms.read_objects(gitdir, ["0" * 39 + "1"])
    with pytest.raises(private_terms.ScanError):
        private_terms.git(gitdir, "cat-file", "-t", "nope")


def test_report_is_padded_or_truncated_to_exactly_64_kib():
    small = private_terms.build_report(["x"], "ctx")
    assert len(small) == private_terms.REPORT_SIZE
    big = private_terms.build_report(["y" * 1000] * 100, "ctx")
    assert len(big) == private_terms.REPORT_SIZE
    assert private_terms.TRUNCATED in big


def test_encrypt_rejects_a_bad_recipient():
    with pytest.raises(private_terms.ScanError, match="recipient"):
        private_terms.encrypt("age", "not-a-recipient", b"x")


@pytest.mark.parametrize(
    "env",
    [
        {"REPO": "bad repo", "EVENT": "push"},
        {"REPO": "o/r", "EVENT": "pull_request"},
        {"REPO": "o/r", "EVENT": "pull_request_target", "BASE_SHA": "a", "HEAD_SHA": "b"},
        {"REPO": "o/r", "EVENT": "push", "PUSH_SHA": "main"},
    ],
)
def test_scan_refuses_bad_input_before_fetching(env, tmp_path):
    with pytest.raises(private_terms.ScanError):
        private_terms.scan(env, str(tmp_path / "x.git"))


@posix_only
def test_main_prints_only_the_generic_line_and_always_writes_a_report(
    tmp_path, monkeypatch, capsys
):
    fake_age = tmp_path / "age"
    fake_age.write_text("#!/bin/sh\ncat >/dev/null\nprintf 'CIPHERTEXT'\n")
    fake_age.chmod(fake_age.stat().st_mode | stat.S_IXUSR)
    report = tmp_path / "report.age"
    for k in ("PRIVATE_TERMS", "BASE_SHA", "HEAD_SHA", "PUSH_SHA", "BEFORE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EVENT", "unknown")
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("AGE_BIN", str(fake_age))
    monkeypatch.setenv("PRIVATE_TERMS_AGE_RECIPIENT", "age1" + "q" * 58)
    monkeypatch.setenv("REPORT_PATH", str(report))
    assert private_terms.main() == 1
    assert capsys.readouterr().out == private_terms.GENERIC_FAIL + "\n"
    assert report.read_bytes() == b"CIPHERTEXT"
    monkeypatch.setenv("AGE_BIN", str(tmp_path / "missing-age"))
    assert private_terms.main() == 1
    assert capsys.readouterr().out == private_terms.GENERIC_FAIL + "\n"


def test_generic_fail_line_is_the_spec_wording():
    """REQ-63: the public failure line is fixed, names nothing, and claims no notification."""
    assert private_terms.GENERIC_FAIL == (
        "Private-terms check failed. The maintainer can decrypt the report artifact."
    )
    assert "notified" not in private_terms.GENERIC_FAIL


def test_python_is_new_enough():
    assert sys.version_info >= (3, 12)
