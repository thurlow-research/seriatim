"""Tests for tools/ci/td_test_ids.py and the td marker hook (SPEC REQ-60, REQ-63; #16)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


td_test_ids = load(ROOT / "tools" / "ci" / "td_test_ids.py", "td_test_ids")
hooks = load(ROOT / "tests" / "conftest.py", "td_hooks")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

ADR = "| slice | files |\n|---|---|\n| **S0a** Tooling | x |\n| **S1** Gate | `node/` |\n"


def td_text(ids: list[str], enforced: str | None = "no", extra: str = "") -> str:
    flag = f"\nEnforced: {enforced}\n" if enforced is not None else ""
    rows = "".join(f"| {i} | does {i} | 1 |\n" for i in ids)
    table = "| id | test | REQ |\n|---|---|---|\n"
    return f"# TD\n\n**Status:** x.\n{flag}\n## 8. Tests\n\n{table}{rows}{extra}"


def junit(cases: list[tuple[str, list[str], str | None]]) -> str:
    """cases: (name, ids, outcome element or None for a pass)."""
    body = ""
    for name, ids, outcome in cases:
        props = "".join(f'<property name="td" value="{i}"/>' for i in ids)
        out = f"<{outcome}/>" if outcome else ""
        props = f"<properties>{props}</properties>"
        body += f'<testcase classname="t" name="{name}">{props}{out}</testcase>'
    return f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>'


# --- parsing ------------------------------------------------------------------------------


def test_parse_td_reads_ids_slice_and_flag():
    td = td_test_ids.parse_td("TD-slice-4a.md", td_text(["C1", "K-OAS1", "HL4"], "yes"))
    assert (td.name, td.slice, td.enforced) == ("TD-slice-4a", "S4a", True)
    assert list(td.ids) == ["C1", "K-OAS1", "HL4"]


def test_parse_td_ignores_other_tables():
    extra = "\n| # | file |\n|---|---|\n| 1 | `x.py` |\n"
    assert list(td_test_ids.parse_td("TD-slice-1.md", td_text(["C1"], extra=extra)).ids) == ["C1"]


@pytest.mark.parametrize(
    "filename, text, message",
    [
        ("TD-slice-1.md", td_text(["C1"], None), "exactly one 'Enforced"),
        ("TD-slice-1.md", td_text(["C1"]) + "\nEnforced: no\n", "exactly one 'Enforced"),
        ("TD-slice-1.md", td_text(["C1"], "maybe"), "must be 'yes' or 'no'"),
        ("TD-slice-1.md", td_text(["C1"], "Yes"), "must be 'yes' or 'no'"),
        ("TD-slice-1.md", "\n" * 25 + td_text(["C1"]), "within the first 20"),
        ("TD-slice-1.md", "# TD\n\nEnforced: no\n\nno tables\n", "no test table"),
        ("TD-slice-1.md", "Enforced: no\n| id | test |\n| C1 | x |\n", "no separator"),
        ("TD-slice-1.md", "Enforced: no\n| id | test |\n", "no separator"),
        ("TD-slice-1.md", td_text(["C1", "c2"]), "'c2' is not a test ID"),
        ("TD-slice-1.md", td_text(["C1", "**C2**"]), "is not a test ID"),
        ("TD-slice-1.md", td_text(["C1", "C1"]), "C1 is listed twice"),
        ("TD-slice-one.md", td_text(["C1"]), "not a TD-slice-<N>.md name"),
    ],
)
def test_parse_td_is_strict(filename, text, message):
    with pytest.raises(td_test_ids.CheckError, match=message):
        td_test_ids.parse_td(filename, text)


def test_base_td_without_flag_counts_as_not_enforced():
    td = td_test_ids.parse_td("TD-slice-1.md", td_text(["C1"], None), require_flag=False)
    assert td.enforced is False
    with pytest.raises(td_test_ids.CheckError, match="must be"):
        td_test_ids.parse_td("TD-slice-1.md", td_text(["C1"], "on"), require_flag=False)


def write_docs(root: Path, tds: dict[str, str], adr: str = ADR) -> None:
    design = root / "docs" / "design"
    design.mkdir(parents=True, exist_ok=True)
    for old in design.glob("TD-slice-*.md"):
        old.unlink()
    for name, text in tds.items():
        (design / name).write_text(text)
    (root / "docs" / "adr").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "adr" / "ADR-001-architecture.md").write_text(adr)


def test_load_tds_needs_an_adr_row_and_unique_ids(tmp_path):
    design, adr = (
        tmp_path / "docs" / "design",
        tmp_path / "docs" / "adr" / "ADR-001-architecture.md",
    )
    write_docs(tmp_path, {"TD-slice-1.md": td_text(["C1"]), "TD-slice-0a.md": td_text(["T1"])})
    assert [t.name for t in td_test_ids.load_tds(design, adr)] == ["TD-slice-0a", "TD-slice-1"]
    write_docs(tmp_path, {"TD-slice-2.md": td_text(["C1"])})
    with pytest.raises(td_test_ids.CheckError, match="S2 has no row"):
        td_test_ids.load_tds(design, adr)
    write_docs(tmp_path, {"TD-slice-1.md": td_text(["C1"]), "TD-slice-0a.md": td_text(["C1"])})
    with pytest.raises(td_test_ids.CheckError, match="C1 is listed in both"):
        td_test_ids.load_tds(design, adr)


# --- JUnit --------------------------------------------------------------------------------


def test_junit_results_only_passing_cases_discharge(tmp_path):
    f = tmp_path / "j.xml"
    f.write_text(
        junit(
            [
                ("ok", ["C1", "C2"], None),
                ("skip", ["C3"], "skipped"),
                ("fail", ["C4"], "failure"),
                ("err", ["C5"], "error"),
                ("untagged", [], None),
            ]
        )
    )
    passed, other, count = td_test_ids.junit_results(f)
    assert set(passed) == {"C1", "C2"} and set(other) == {"C3", "C4", "C5"} and count == 5


@pytest.mark.parametrize(
    "content, message",
    [
        (None, "FileNotFoundError"),
        ("", "ParseError"),
        ("<testsuites><testsuite>", "ParseError"),
        ("<html/>", "unexpected root"),
        ("<testsuites><testsuite/></testsuites>", "no test cases"),
    ],
)
def test_junit_missing_empty_or_malformed_fails(tmp_path, content, message):
    f = tmp_path / "j.xml"
    if content is not None:
        f.write_text(content)
    with pytest.raises(td_test_ids.CheckError, match=message):
        td_test_ids.junit_results(f)


# --- the whole check ----------------------------------------------------------------------


def run(tmp_path, monkeypatch, capsys, cases, event="push", **env) -> tuple[int, str]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "j.xml").write_text(junit(cases))
    monkeypatch.setenv("EVENT", event)
    for k in ("BASE_SHA", "PR_BODY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    code = td_test_ids.main(["--junit", "j.xml"])
    return code, capsys.readouterr().out


def test_enforced_td_needs_every_id_passing(tmp_path, monkeypatch, capsys):
    write_docs(tmp_path, {"TD-slice-1.md": td_text(["C1", "C2"], "yes")})
    code, out = run(tmp_path, monkeypatch, capsys, [("a", ["C1"], None), ("b", ["C2"], None)])
    assert code == 0 and "every check passed" in out
    code, out = run(tmp_path, monkeypatch, capsys, [("a", ["C1"], None), ("b", ["C2"], "skipped")])
    assert code == 1 and "C2 (TD-slice-1) has no passing test (did not pass: t::b)" in out
    code, out = run(tmp_path, monkeypatch, capsys, [("a", ["C1"], None)])
    assert code == 1 and "C2 (TD-slice-1) has no passing test" in out


def test_unenforced_td_prints_a_notice(tmp_path, monkeypatch, capsys):
    write_docs(tmp_path, {"TD-slice-1.md": td_text(["C1", "C2"], "no")})
    code, out = run(tmp_path, monkeypatch, capsys, [("a", ["C1"], None)])
    assert code == 0
    assert "TD-slice-1 (S1) is 'Enforced: no': 2 IDs, 1 discharged" in out
    assert "run on pull requests only" in out


def test_unknown_id_fails_even_when_not_enforced(tmp_path, monkeypatch, capsys):
    write_docs(tmp_path, {"TD-slice-1.md": td_text(["C1"], "no")})
    code, out = run(tmp_path, monkeypatch, capsys, [("a", ["Z9"], "skipped")])
    assert code == 1 and "Z9 is named by a test but listed in no TD (t::a)" in out


def test_bad_event_or_base_fails(tmp_path, monkeypatch, capsys):
    write_docs(tmp_path, {"TD-slice-1.md": td_text(["C1"])})
    assert run(tmp_path, monkeypatch, capsys, [("a", [], None)], event="")[0] == 1
    code, out = run(
        tmp_path, monkeypatch, capsys, [("a", [], None)], "pull_request", BASE_SHA="main"
    )
    assert code == 1 and "BASE_SHA must be" in out


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def base_repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    write_docs(tmp_path, {"TD-slice-1.md": td_text(["C1", "C2"], "yes")})
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


PASSING = [("a", ["C1"], None), ("b", ["C2"], None)]


@needs_git
@pytest.mark.parametrize(
    "body, code",
    [
        ("", 1),
        ("TD-ID-REMOVED: C2", 1),  # no reason
        ("TD-ID-REMOVED: C2 - merged into C1", 0),
        ("TD-ID-REMOVED: C2 — merged into C1", 0),
        ("intro\n  TD-ID-REMOVED: C2 - merged into C1  \nmore", 0),
        ("TD-ID-REMOVED: C3 - wrong id", 1),
    ],
)
def test_removed_id_needs_a_waiver(base_repo, monkeypatch, capsys, body, code):
    repo, base = base_repo
    write_docs(repo, {"TD-slice-1.md": td_text(["C1"], "yes")})
    got, out = run(
        repo, monkeypatch, capsys, PASSING[:1], "pull_request", BASE_SHA=base, PR_BODY=body
    )
    assert got == code
    if code:
        assert "C2 (TD-slice-1) was removed" in out


@needs_git
@pytest.mark.parametrize(
    "body, code",
    [
        ("", 1),
        ("TD-ENFORCEMENT-OFF: TD-slice-1 - S1 reverted", 0),
        ("TD-ENFORCEMENT-OFF: TD-slice-2 - wrong TD", 1),
    ],
)
def test_turning_enforcement_off_needs_a_waiver(base_repo, monkeypatch, capsys, body, code):
    repo, base = base_repo
    write_docs(repo, {"TD-slice-1.md": td_text(["C1", "C2"], "no")})
    got, out = run(repo, monkeypatch, capsys, PASSING, "pull_request", BASE_SHA=base, PR_BODY=body)
    assert got == code
    if code:
        assert "TD-slice-1 was 'Enforced: yes' on the base" in out


@needs_git
def test_deleting_an_enforced_td_needs_both_waivers(base_repo, monkeypatch, capsys):
    repo, base = base_repo
    write_docs(repo, {})
    code, out = run(repo, monkeypatch, capsys, [("a", [], None)], "pull_request", BASE_SHA=base)
    assert code == 1
    assert "C1 (TD-slice-1) was removed" in out and "was 'Enforced: yes'" in out
    body = (
        "TD-ID-REMOVED: C1 - gone\nTD-ID-REMOVED: C2 - gone\nTD-ENFORCEMENT-OFF: TD-slice-1 - gone"
    )
    code, _ = run(
        repo, monkeypatch, capsys, [("a", [], None)], "pull_request", BASE_SHA=base, PR_BODY=body
    )
    assert code == 0


@needs_git
def test_push_skips_the_removal_checks(base_repo, monkeypatch, capsys):
    repo, _ = base_repo
    write_docs(repo, {"TD-slice-1.md": td_text(["C1"], "no")})
    code, out = run(repo, monkeypatch, capsys, PASSING[:1], "push")
    assert code == 0 and "run on pull requests only" in out


@needs_git
def test_base_td_without_flag_is_not_ratcheted(tmp_path, monkeypatch, capsys):
    git(tmp_path, "init", "-q", "-b", "main")
    write_docs(tmp_path, {"TD-slice-1.md": td_text(["C1"], None)})
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "before the check existed")
    base = git(tmp_path, "rev-parse", "HEAD")
    write_docs(tmp_path, {"TD-slice-1.md": td_text(["C1"], "no")})
    code, _ = run(
        tmp_path, monkeypatch, capsys, [("a", ["C1"], None)], "pull_request", BASE_SHA=base
    )
    assert code == 0


# --- the td marker hook -------------------------------------------------------------------


def item(*marks):
    return SimpleNamespace(
        nodeid="t::x",
        user_properties=[],
        iter_markers=lambda name: [m.mark for m in marks if m.mark.name == name],
    )


def test_td_marker_records_properties():
    it = item(pytest.mark.td("C1", "K-OAS1"), pytest.mark.td("C1"), pytest.mark.skip)
    hooks.pytest_collection_modifyitems([it])
    assert it.user_properties == [("td", "C1"), ("td", "K-OAS1")]


@pytest.mark.parametrize(
    "mark", [pytest.mark.td(), pytest.mark.td("c1"), pytest.mark.td(1), pytest.mark.td(id="C1")]
)
def test_td_marker_misuse_is_an_error(mark):
    with pytest.raises(pytest.UsageError):
        hooks.td_ids(item(mark))
