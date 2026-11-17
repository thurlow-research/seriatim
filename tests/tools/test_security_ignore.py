"""Tests for tools/ci/security_ignore.py (SPEC REQ-63)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "security_ignore", ROOT / "tools/ci/security_ignore.py"
)
si = importlib.util.module_from_spec(_spec)
sys.modules["security_ignore"] = si
_spec.loader.exec_module(si)

ON = dt.date(2026, 11, 16)
NOSEC = "# no" + "sec"  # built at run time, so this file's own comments stay clean


def entry(**kw) -> str:
    e = {"tool": '"pip-audit"', "id": '"GHSA-4xqg-gf5c-ghwq"', "reason": '"not reachable"',
         "expires": "2026-12-31"} | kw  # fmt: skip
    return "[[ignore]]\n" + "".join(f"{k} = {v}\n" for k, v in e.items() if v is not None)


def load_text(tmp_path, text, on=ON):
    f = tmp_path / ".security-ignore.toml"
    f.write_text(text)
    return si.load(f, on)


def test_valid_entries_load(tmp_path):
    text = (
        entry() + entry(tool='"bandit"', id='"B603"') + entry(tool='"trivy"', id='"CVE-2026-1234"')
    )
    assert [e["id"] for e in load_text(tmp_path, text)] == [
        "GHSA-4xqg-gf5c-ghwq",
        "B603",
        "CVE-2026-1234",
    ]
    assert load_text(tmp_path, "# nothing yet\n") == []


def test_an_entry_on_its_expiry_day_is_still_valid(tmp_path):
    assert load_text(tmp_path, entry(expires="2026-11-16"))


@pytest.mark.parametrize(
    "text, message",
    [
        (entry(expires="2026-11-15"), "expired on 2026-11-15"),
        (entry(reason=None), "missing \\['reason'\\]"),
        (entry(expires=None), "missing \\['expires'\\]"),
        (entry(reason='"  "'), "reason must be a non-empty string"),
        (entry(expires='"2026-12-31"'), "must be a TOML date"),
        (entry(expires="2026-12-31T00:00:00"), "must be a TOML date"),
        (entry(tool='"semgrep"'), "tool must be one of"),
        (entry(tool='"bandit"', id='"B60"'), "is not a bandit finding id"),
        (entry(id='"--index-url=x"'), "is not a pip-audit finding id"),
        (entry(tool='"trivy"', id='"x y"'), "is not a trivy finding id"),
        (entry() + "owner = 'x'\n", "unknown \\['owner'\\]"),
        ("ignore = 1\n", "array of tables"),
        ("[other]\n", "unknown top-level keys"),
        ("[[ignore]\n", "Expected"),
    ],
)
def test_invalid_entries_fail(tmp_path, text, message):
    with pytest.raises(si.IgnoreError, match=message):
        load_text(tmp_path, text)


def test_missing_file_fails(tmp_path):
    with pytest.raises(si.IgnoreError, match="is missing"):
        si.load(tmp_path / "nope.toml", ON)


def test_every_problem_is_reported(tmp_path):
    with pytest.raises(si.IgnoreError) as e:
        load_text(tmp_path, entry(expires="2020-01-01") + entry(reason='""'))
    assert "entry 1" in str(e.value) and "entry 2" in str(e.value)


@pytest.mark.parametrize(
    "line",
    [
        f"call()  {NOSEC} B603 # fixed argument list",
        f"call()  {NOSEC} B603, B607 # fixed argument list",
        f"call()  {NOSEC} B603 , B607 # spaces either side of the comma",
        f"call()  {NOSEC} # reviewed: constant input",
        f"call()  # see below {NOSEC} B603 # after other comment text",
        f"call()  {NOSEC.upper()} B310 #not a suppression: Bandit reads lowercase only",
        f"s = '{NOSEC} B603'  # a string, not a comment",
    ],
)
def test_well_formed_nosec_passes(line):
    assert si.nosec_problems("x.py", line + "\n") == []


FORM = "x.py:2: '# nosec' must name known test ids and give a reason, as '# nosec B603 # <reason>'"


@pytest.mark.parametrize(
    "line",
    [
        f"call()  {NOSEC}",
        f"call()  {NOSEC} B603",
        f"call()  {NOSEC} B603 - fixed list",
        f"call()  {NOSEC} B603 #",
        f"call()  {NOSEC}: B603 fixed list",
        f"call()  {NOSEC}: B603 # a colon Bandit accepts",
        # Bandit 1.9.4 reads these as suppressions too (#25 review):
        f"call()  {NOSEC}B602 # no space",
        f"call()  {NOSEC}urity review pending",
        f"call()  {NOSEC} b602 # lowercase id, which Bandit drops",
        f"call()  {NOSEC} subprocess_popen_with_shell_equals_true # a test name, not an id",
    ],
)
def test_malformed_nosec_fails(line):
    assert si.nosec_problems("x.py", "a = 1\n" + line + "\n") == [FORM]


@pytest.mark.parametrize(
    ("ids", "unknown"),
    [("B999", "B999"), ("B602, B999", "B999"), ("B000, B998", "B000, B998")],
)
def test_nosec_with_an_unknown_test_id_fails(ids, unknown):
    # Bandit drops unknown ids; with none left, it suppresses every finding on the line.
    assert si.nosec_problems("x.py", f"a = 1\ncall()  {NOSEC} {ids} # reason\n") == [
        f"x.py:2: '# nosec' names {unknown}, which Bandit 1.9.4 doesn't know; Bandit "
        "ignores unknown ids, so it could suppress every finding on the line"
    ]


def test_nosec_ids_without_a_space_after_the_comma_fail():
    # Bandit captures only the last id of "B602,B603", so B602 would not be suppressed.
    assert si.nosec_problems("x.py", f"call()  {NOSEC} B602,B603 # reason\n") == [
        "x.py:1: Bandit reads this '# nosec' as ['B603'], not ['B602', 'B603']; "
        "separate test ids with ', '"
    ]


def test_bandit_regexes_and_ids_match_the_installed_bandit():
    bandit_manager = pytest.importorskip("bandit.core.manager")
    from bandit.core import extension_loader

    assert pytest.importorskip("bandit").__version__ == "1.9.4"
    assert si.BANDIT_NOSEC.pattern == bandit_manager.NOSEC_COMMENT.pattern
    assert si.BANDIT_NOSEC_TESTS.pattern == bandit_manager.NOSEC_COMMENT_TESTS.pattern
    assert si.BANDIT_NOSEC_TESTS.flags == bandit_manager.NOSEC_COMMENT_TESTS.flags
    m = extension_loader.MANAGER
    known = set(m.plugins_by_id) | set(m.blacklist_by_id) | set(m.builtin)
    assert known == si.BANDIT_TEST_IDS


@pytest.mark.parametrize(
    "comment",
    [
        "# nosec B602 # r",
        "# nosec B602, B603 # r",
        "# nosec # r",
        "# nosecB602 # r",
        "# nosec B999 # r",
        "# nosec B602,B603 # r",
        "# nosec: B602 # r",
        "# not a suppression",
    ],
)
def test_lint_agrees_with_bandit_on_what_is_suppressed(comment):
    bandit_manager = pytest.importorskip("bandit.core.manager")
    bandit_read = bandit_manager._parse_nosec_comment(comment)
    problem = si.nosec_comment_problem(comment)
    if bandit_read is None:
        assert problem is None
    elif problem is None:
        # Accepted: Bandit suppresses exactly the ids written (none written = blanket).
        assert bandit_read == set(re_ids(comment))


def re_ids(comment):
    return re.findall(r"B[0-9]{3}", comment.split("#")[1])


def test_untokenizable_source_fails():
    assert "cannot tokenize" in si.nosec_problems("x.py", "s = '''\n")[0]


def test_tool_args(tmp_path):
    entries = [
        {"tool": "bandit", "id": "B607"},
        {"tool": "bandit", "id": "B603"},
        {"tool": "pip-audit", "id": "PYSEC-2026-1"},
        {"tool": "trivy", "id": "CVE-2026-1"},
    ]
    assert si.tool_args("bandit", entries, str(tmp_path)) == ["--skip", "B603,B607"]
    assert si.tool_args("bandit", [], str(tmp_path)) == []
    assert si.tool_args("pip-audit", entries, str(tmp_path)) == ["--ignore-vuln", "PYSEC-2026-1"]
    args = si.tool_args("trivy", entries, str(tmp_path))
    assert args[0] == "--ignorefile" and Path(args[1]).read_text() == "CVE-2026-1\n"


def bandit_json(tmp_path, results=(), errors=(), **extra) -> Path:
    f = tmp_path / "bandit.json"
    data = {"results": list(results), "errors": list(errors), "metrics": {"_totals": {"loc": 9}}}
    f.write_text(json.dumps(data | extra))
    return f


def test_bandit_report(tmp_path, capsys):
    assert si.bandit_report(bandit_json(tmp_path)) == []
    assert "9 lines scanned" in capsys.readouterr().out
    finding = {"filename": "a.py", "line_number": 3, "test_id": "B310",
               "issue_severity": "MEDIUM", "issue_text": "urlopen"}  # fmt: skip
    assert si.bandit_report(bandit_json(tmp_path, [finding])) == ["a.py:3: B310 MEDIUM: urlopen"]
    error = {"filename": "b.py", "reason": "syntax error"}
    assert si.bandit_report(bandit_json(tmp_path, errors=[error])) == [
        "Bandit could not scan b.py: syntax error"
    ]


@pytest.mark.parametrize("content", [None, "", "{}", '{"results": {}, "errors": []}'])
def test_unusable_bandit_output_fails(tmp_path, content):
    f = tmp_path / "bandit.json"
    if content is not None:
        f.write_text(content)
    assert "is unusable" in si.bandit_report(f)[0]


def test_main_usage_and_exec(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert si.main(["nonsense"]) == 2
    (tmp_path / ".security-ignore.toml").write_text(entry(expires="2020-01-01"))
    assert si.main(["exec", "bandit", "--", sys.executable, "-c", "pass"]) == 1
    assert "expired" in capsys.readouterr().out
    (tmp_path / ".security-ignore.toml").write_text(
        entry(tool='"bandit"', id='"B603"', expires="2099-01-01")
    )
    code = "import sys; sys.exit(0 if sys.argv[1:] == ['--skip', 'B603'] else 7)"
    assert si.main(["exec", "bandit", "--", sys.executable, "-c", code]) == 0
    assert si.main(["exec", "bandit", "--", sys.executable, "-c", "import sys; sys.exit(3)"]) == 3


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_lint_reads_tracked_python(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    (tmp_path / ".security-ignore.toml").write_text("")
    (tmp_path / "ok.py").write_text(f"x = 1  {NOSEC} B101 # test only\n")
    (tmp_path / "untracked.py").write_text(f"x = 1  {NOSEC}\n")
    subprocess.run(["git", "add", "ok.py", ".security-ignore.toml"], check=True)
    assert si.main(["lint"]) == 0
    (tmp_path / "bad.py").write_text(f"x = 1  {NOSEC}\n")
    subprocess.run(["git", "add", "bad.py"], check=True)
    assert si.main(["lint"]) == 1
    assert "bad.py:1:" in capsys.readouterr().out
