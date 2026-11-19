"""Tests for tools/ci/release_checks.py, the checks in release.yml (SPEC REQ-68; #10)."""

from __future__ import annotations

import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("release_checks", ROOT / "tools/ci/release_checks.py")
rc = importlib.util.module_from_spec(spec)
sys.modules["release_checks"] = rc  # dataclasses look the module up
spec.loader.exec_module(rc)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


# --- tag names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, pep440, pre",
    [
        ("v0.0.1", "0.0.1", False),
        ("v1.2.3", "1.2.3", False),
        ("v10.20.30", "10.20.30", False),
        ("v0.0.1-rc.1", "0.0.1rc1", True),
        ("v2.0.0-rc.12", "2.0.0rc12", True),
    ],
)
def test_valid_tags(name, pep440, pre):
    assert rc.parse_tag(name) == rc.Tag(name, pep440, pre)


@pytest.mark.parametrize(
    "name",
    [
        "1.2.3",  # no v
        "v1.2",
        ".".join(["v1", "2", "3", "4"]),  # four parts (built, so it is not an IP literal)
        "v01.2.3",  # leading zeros
        "v1.02.3",
        "v1.2.03",
        "v1.2.3-rc.0",  # rc numbers start at 1
        "v1.2.3-rc.01",
        "v1.2.3-rc1",
        "v1.2.3rc1",  # the PEP 440 form is the version, not the tag
        "v1.2.3-beta.1",
        "v1.2.3-rc.",
        "v1.2.3 ",
        "v1.2.3\n",
        "V1.2.3",
        "v1.2.٣",  # an Arabic-Indic digit is not [0-9]
        "v",
        "",
    ],
)
def test_malformed_tags_fail(name):
    with pytest.raises(rc.CheckFailed, match="is not vX.Y.Z or vX.Y.Z-rc.N"):
        rc.parse_tag(name)


def test_parse_ref_needs_a_tag_ref():
    assert rc.parse_ref("refs/tags/v1.0.0").pep440 == "1.0.0"
    for ref in ("refs/heads/v1.0.0", "v1.0.0", "refs/tags/"):
        with pytest.raises(rc.CheckFailed):
            rc.parse_ref(ref)


# --- the static __version__ -----------------------------------------------------------------


def write_init(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "__init__.py"
    p.write_text(text, encoding="utf-8")
    return p


def test_version_matches(tmp_path):
    init = write_init(tmp_path, '"""Doc."""\n\n__version__ = "0.0.1rc1"\n')
    rc.check_version(rc.parse_tag("v0.0.1-rc.1"), init)


@pytest.mark.parametrize(
    "value",
    ["0.0.1", "0.0.1-rc.1", "0.0.1rc2", "0.0.1.rc1", "0.0.1rc1 "],
)
def test_version_mismatch_fails_without_normalising(tmp_path, value):
    init = write_init(tmp_path, f'__version__ = "{value}"\n')
    with pytest.raises(rc.CheckFailed, match="bump it by pull request"):
        rc.check_version(rc.parse_tag("v0.0.1-rc.1"), init)


@pytest.mark.parametrize(
    "text, why",
    [
        ("", "found 0"),
        ('VERSION = "1.0.0"\n', "found 0"),
        ('__version__ = "1.0.0"\n__version__ = "1.0.0"\n', "found 2"),
        ("__version__ = 1\n", "not a string literal"),
        ('__version__ = "1." + "0.0"\n', "not a string literal"),
        ("__version__ = (\n", "cannot read"),
    ],
)
def test_static_version_rejects(tmp_path, text, why):
    with pytest.raises(rc.CheckFailed, match=why):
        rc.static_version(write_init(tmp_path, text))


def test_static_version_ignores_nested_and_other_assignments(tmp_path):
    text = 'x = y = 1\na, b = 1, 2\ndef f():\n    __version__ = "9"\n__version__ = "1.0.0"\n'
    assert rc.static_version(write_init(tmp_path, text)) == "1.0.0"


def test_static_version_of_a_missing_file(tmp_path):
    with pytest.raises(rc.CheckFailed, match="cannot read"):
        rc.static_version(tmp_path / "missing.py")


def test_the_repository_version_is_a_valid_release_version():
    version = rc.static_version(ROOT / "node/seriatim_node/__init__.py")
    core, _, n = version.partition("rc")
    assert rc.parse_tag(f"v{core}-rc.{n}" if n else f"v{core}").pep440 == version


# --- reachability from main -----------------------------------------------------------------


def git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.invalid",
    }


def run_git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=git_env(), check=True
    )
    return r.stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", name)
    return run_git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """main has two commits; side branches off the first and is never merged."""
    r = tmp_path / "repo"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    first = commit(r, "a")
    on_main = commit(r, "b")
    run_git(r, "switch", "-q", "-c", "side", first)
    off_main = commit(r, "c")
    run_git(r, "switch", "-q", "main")
    run_git(r, "tag", "v1.0.0", on_main)  # lightweight
    run_git(r, "tag", "-a", "-m", "rc", "v1.1.0-rc.1", on_main)  # annotated
    run_git(r, "tag", "v1.0.1", first)
    run_git(r, "tag", "v9.0.0", off_main)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return r, {"first": first, "on_main": on_main, "off_main": off_main}


@needs_git
@pytest.mark.parametrize(
    "tag, which", [("v1.0.0", "on_main"), ("v1.1.0-rc.1", "on_main"), ("v1.0.1", "first")]
)
def test_tags_on_main_pass(repo, tag, which):
    r, shas = repo
    rc.check_reachable(rc.parse_tag(tag), shas[which], "refs/heads/main", r)


@needs_git
def test_a_tag_not_on_main_fails(repo):
    r, shas = repo
    with pytest.raises(rc.CheckFailed, match="is not reachable from refs/heads/main"):
        rc.check_reachable(rc.parse_tag("v9.0.0"), shas["off_main"], "refs/heads/main", r)


@needs_git
def test_the_tag_must_name_the_commit_being_built(repo):
    r, shas = repo
    with pytest.raises(rc.CheckFailed, match="but this run builds"):
        rc.check_reachable(rc.parse_tag("v1.0.0"), shas["first"], "refs/heads/main", r)


@needs_git
@pytest.mark.parametrize(
    "tag, sha_key, main_ref, why",
    [
        ("v7.7.7", "on_main", "refs/heads/main", "refs/tags/v7.7.7 does not resolve"),
        ("v1.0.0", "on_main", "refs/remotes/origin/main", "origin/main does not resolve"),
        ("v1.0.0", "on_main", "--all", "--all does not resolve"),
    ],
)
def test_unresolvable_refs_fail_closed(repo, tag, sha_key, main_ref, why):
    r, shas = repo
    with pytest.raises(rc.CheckFailed, match=why):
        rc.check_reachable(rc.parse_tag(tag), shas[sha_key], main_ref, r)


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "g" * 40, "0" * 39, "0" * 41])
def test_sha_must_be_a_full_sha(sha):
    with pytest.raises(rc.CheckFailed, match="full commit SHA"):
        rc.check_reachable(rc.parse_tag("v1.0.0"), sha, "refs/heads/main")


@needs_git
def test_a_merge_base_error_fails_closed(repo, monkeypatch):
    r, shas = repo
    real = rc.git

    def broken(cwd, *args):
        if args[0] == "merge-base":
            return subprocess.CompletedProcess(args, 128, "", "fatal: boom")
        return real(cwd, *args)

    monkeypatch.setattr(rc, "git", broken)
    with pytest.raises(rc.CheckFailed, match="merge-base failed: fatal: boom"):
        rc.check_reachable(rc.parse_tag("v1.0.0"), shas["on_main"], "refs/heads/main", r)


# --- the built distributions ----------------------------------------------------------------


def make_wheel(dist: Path, name: str, member: str, version: str | None) -> Path:
    p = dist / name
    with zipfile.ZipFile(p, "w") as zf:
        meta = "Metadata-Version: 2.4\nName: seriatim-node\n"
        if version is not None:
            meta += f"Version: {version}\n"
        zf.writestr(member, meta + "\nlong description\nVersion: 6.6.6\n")
    return p


def make_sdist(dist: Path, name: str, member: str, version: str) -> Path:
    p = dist / name
    data = f"Metadata-Version: 2.4\nName: seriatim-node\nVersion: {version}\n".encode()
    with tarfile.open(p, "w:gz") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return p


def good_dist(tmp_path: Path, v: str = "0.0.1rc1") -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    make_wheel(
        dist, f"seriatim_node-{v}-py3-none-any.whl", f"seriatim_node-{v}.dist-info/METADATA", v
    )
    make_sdist(dist, f"seriatim_node-{v}.tar.gz", f"seriatim_node-{v}/PKG-INFO", v)
    return dist


TAG = rc.parse_tag("v0.0.1-rc.1")


def test_good_dist(tmp_path):
    dist = good_dist(tmp_path)
    (dist / ".gitignore").write_text("*\n")  # written by uv build --out-dir
    names = [p.name for p in rc.check_dist(TAG, dist)]
    assert names == ["seriatim_node-0.0.1rc1-py3-none-any.whl", "seriatim_node-0.0.1rc1.tar.gz"]


def test_dist_must_be_a_directory(tmp_path):
    with pytest.raises(rc.CheckFailed, match="is not a directory"):
        rc.check_dist(TAG, tmp_path / "nope")


def test_dist_with_an_extra_file_fails(tmp_path):
    dist = good_dist(tmp_path)
    (dist / "SHA256SUMS").write_text("x")
    with pytest.raises(rc.CheckFailed, match="unexpected files in .*: SHA256SUMS"):
        rc.check_dist(TAG, dist)


def test_dist_needs_exactly_one_of_each(tmp_path):
    dist = good_dist(tmp_path)
    (dist / "seriatim_node-0.0.1rc1.tar.gz").unlink()
    with pytest.raises(rc.CheckFailed, match="found 1 and 0"):
        rc.check_dist(TAG, dist)
    make_sdist(dist, "seriatim_node-0.0.1rc1.tar.gz", "seriatim_node-0.0.1rc1/PKG-INFO", "0.0.1rc1")
    make_wheel(dist, "seriatim_node-0.0.1rc1-py2-none-any.whl", "x", "0.0.1rc1")
    with pytest.raises(rc.CheckFailed, match="found 2 and 1"):
        rc.check_dist(TAG, dist)


def test_a_dist_built_at_another_version_fails(tmp_path):
    with pytest.raises(rc.CheckFailed, match="is not seriatim_node-0.0.1rc1-"):
        rc.check_dist(TAG, good_dist(tmp_path, "0.0.0"))


def test_an_sdist_with_the_wrong_name_fails(tmp_path):
    dist = good_dist(tmp_path)
    (dist / "seriatim_node-0.0.1rc1.tar.gz").rename(dist / "seriatim-node-0.0.1rc1.tar.gz")
    with pytest.raises(rc.CheckFailed, match="sdist seriatim-node-0.0.1rc1.tar.gz is not"):
        rc.check_dist(TAG, dist)


def test_metadata_must_carry_the_version(tmp_path):
    dist = good_dist(tmp_path)
    whl = dist / "seriatim_node-0.0.1rc1-py3-none-any.whl"
    make_wheel(dist, whl.name, "seriatim_node-0.0.1rc1.dist-info/METADATA", "0.0.2")
    with pytest.raises(rc.CheckFailed, match="says version 0.0.2"):
        rc.check_dist(TAG, dist)
    make_wheel(dist, whl.name, "seriatim_node-0.0.1rc1.dist-info/METADATA", None)
    with pytest.raises(rc.CheckFailed, match="has no Version"):
        rc.check_dist(TAG, dist)


def test_sdist_metadata_mismatch_fails(tmp_path):
    dist = good_dist(tmp_path)
    sd = dist / "seriatim_node-0.0.1rc1.tar.gz"
    make_sdist(dist, sd.name, "seriatim_node-0.0.1rc1/PKG-INFO", "0.0.1")
    with pytest.raises(rc.CheckFailed, match="says version 0.0.1,"):
        rc.check_dist(TAG, dist)


@pytest.mark.parametrize("which", ["wheel", "sdist"])
def test_unreadable_or_missing_metadata_fails(tmp_path, which):
    dist = good_dist(tmp_path)
    if which == "wheel":
        whl = dist / "seriatim_node-0.0.1rc1-py3-none-any.whl"
        make_wheel(dist, whl.name, "other/METADATA", "0.0.1rc1")
        with pytest.raises(rc.CheckFailed, match="cannot read seriatim_node-0.0.1rc1.dist-info"):
            rc.check_dist(TAG, dist)
        whl.write_bytes(b"not a zip")
    else:
        sd = dist / "seriatim_node-0.0.1rc1.tar.gz"
        make_sdist(dist, sd.name, "other/PKG-INFO", "0.0.1rc1")
        with pytest.raises(rc.CheckFailed, match="cannot read seriatim_node-0.0.1rc1/PKG-INFO"):
            rc.check_dist(TAG, dist)
        sd.write_bytes(b"not a tarball")
    with pytest.raises(rc.CheckFailed, match="cannot read"):
        rc.check_dist(TAG, dist)


def test_sdist_member_that_is_not_a_file_fails(tmp_path):
    dist = good_dist(tmp_path)
    sd = dist / "seriatim_node-0.0.1rc1.tar.gz"
    with tarfile.open(sd, "w:gz") as tf:
        info = tarfile.TarInfo("seriatim_node-0.0.1rc1/PKG-INFO")
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
    with pytest.raises(rc.CheckFailed, match="is not a file"):
        rc.check_dist(TAG, dist)


def test_sha256sums_format(tmp_path):
    a, b = tmp_path / "b.whl", tmp_path / "a.tar.gz"
    a.write_bytes(b"")
    b.write_bytes(b"abc")
    assert rc.sha256sums([a, b]) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  a.tar.gz\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  b.whl\n"
    )


# --- build constraints (#68) ---------------------------------------------------------------

H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64
CONSTRAINTS = f"""\
# This file was autogenerated by uv via the following command:
#    uv pip compile node/build-constraints.in --generate-hashes -o node/build-constraints.txt
hatchling==1.33.0 \\
    --hash=sha256:{H1} \\
    --hash=sha256:{H2}
    # via -r node/build-constraints.in
trove-classifiers==2026.9.21.13 \\
    --hash=sha256:{H3}
    # via hatchling
tomli==2.2.1 ; python_full_version < '3.11' \\
    --hash=sha256:{H1}
"""


def write_pyproject(tmp_path, requires):
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[build-system]\nrequires = {requires}\nbuild-backend = "hatchling.build"\n')
    return path


def write_constraints(tmp_path, text=CONSTRAINTS):
    path = tmp_path / "build-constraints.txt"
    path.write_text(text)
    return path


def test_constraint_pins_parses_uv_output(tmp_path):
    assert rc.constraint_pins(write_constraints(tmp_path)) == {
        "hatchling": ("1.33.0", [H1, H2]),
        "trove-classifiers": ("2026.9.21.13", [H3]),
        "tomli": ("2.2.1", [H1]),
    }


def test_build_constraints_cover_pyproject(tmp_path):
    pyproject = write_pyproject(tmp_path, '["Hatchling == 1.33.0"]')
    assert rc.check_build_constraints(pyproject, write_constraints(tmp_path)) == {
        "hatchling": "1.33.0"
    }


def test_the_committed_constraints_cover_node_pyproject():
    # The build job's check, on the committed files: each [build-system] requirement is
    # hash-pinned at its version. The input is those same requirements, and the header records
    # the command that compiles it, so the file can be regenerated.
    pins = rc.check_build_constraints(
        ROOT / "node/pyproject.toml", ROOT / "node/build-constraints.txt"
    )
    lines = (ROOT / "node/build-constraints.in").read_text().splitlines()
    assert [ln for ln in lines if ln and not ln.startswith("#")] == [
        f"{n}=={v}" for n, v in pins.items()
    ]
    header = (ROOT / "node/build-constraints.txt").read_text().splitlines()[:2]
    assert header == [
        "# This file was autogenerated by uv via the following command:",
        "#    uv pip compile node/build-constraints.in --generate-hashes --universal"
        " --python-version 3.12 -o node/build-constraints.txt",
    ]


@pytest.mark.parametrize(
    "requires, error",
    [
        ('["hatchling==1.34.0"]', "requires hatchling==1.34.0 but"),  # a bump, not recompiled
        ('["hatchling==1.33.0", "hatch-vcs==0.4.0"]', "does not pin hatch-vcs"),
        ('["hatchling>=1.33"]', "is not name==version"),
        ('["hatchling"]', "is not name==version"),
        ('["hatchling[x]==1.33.0"]', "is not name==version"),
        ("[\"hatchling==1.33.0; python_version>'3'\"]", "is not name==version"),
        ("[]", "missing or empty"),
        ('"hatchling==1.33.0"', "missing or empty"),
        ("[1]", "is not name==version"),
    ],
)
def test_build_constraints_fail_closed_on_pyproject(tmp_path, requires, error):
    pyproject = write_pyproject(tmp_path, requires)
    with pytest.raises(rc.CheckFailed, match=re.escape(error)):
        rc.check_build_constraints(pyproject, write_constraints(tmp_path))


@pytest.mark.parametrize(
    "text, error",
    [
        ("hatchling==1.33.0\n", "hatchling==1.33.0 has no sha256 hash"),
        (f"hatchling==1.33.0 --hash=sha256:{H1[:-1]}\n", "no sha256 hash, or a malformed one"),
        (f"hatchling==1.33.0 --hash=md5:{H1}\n", "no sha256 hash, or a malformed one"),
        (f"hatchling==1.33.0 --hash=sha256:{H1}x\n", "no sha256 hash, or a malformed one"),
        (f"hatchling==1.33.0 --hash=sha256:{H1} junk\n", "no sha256 hash, or a malformed one"),
        (f"hatchling>=1.33.0 --hash=sha256:{H1}\n", "is not an exact name==version pin"),
        (
            f"hatchling==1.33.0 --hash=sha256:{H1}\nhatchling==1.33.0 --hash=sha256:{H2}\n",
            "hatchling is listed twice",
        ),
        (CONSTRAINTS.replace("hatchling==1.33.0", "hatchling==1.32.0"), "pins hatchling==1.32.0"),
        ("# nothing pinned\n", "does not pin hatchling"),
    ],
)
def test_build_constraints_fail_closed_on_constraints(tmp_path, text, error):
    pyproject = write_pyproject(tmp_path, '["hatchling==1.33.0"]')
    with pytest.raises(rc.CheckFailed, match=re.escape(error)):
        rc.check_build_constraints(pyproject, write_constraints(tmp_path, text))


def test_build_constraints_unreadable_files_fail_closed(tmp_path):
    good = write_pyproject(tmp_path, '["hatchling==1.33.0"]')
    with pytest.raises(rc.CheckFailed, match="cannot read"):
        rc.check_build_constraints(good, tmp_path / "missing.txt")
    bad = tmp_path / "bad.toml"
    bad.write_text("[build-system\n")
    with pytest.raises(rc.CheckFailed, match="cannot read"):
        rc.check_build_constraints(bad, write_constraints(tmp_path))
    with pytest.raises(rc.CheckFailed, match="missing or empty"):
        rc.check_build_constraints(write_pyproject(tmp_path, "[]"), write_constraints(tmp_path))


# --- the command line -----------------------------------------------------------------------


def test_cli_tag_writes_outputs(tmp_path, capsys):
    out = tmp_path / "out"
    out.write_text("earlier=1\n")
    assert rc.main(["tag", "--ref", "refs/tags/v0.0.1-rc.1", "--github-output", str(out)]) == 0
    expected = "tag=v0.0.1-rc.1\nversion=0.0.1rc1\nprerelease=true\n"
    assert out.read_text() == "earlier=1\n" + expected
    assert capsys.readouterr().out == expected
    assert rc.main(["tag", "--ref", "refs/tags/v1.0.0"]) == 0
    assert "prerelease=false" in capsys.readouterr().out


def test_cli_malformed_tag_fails(capsys):
    assert rc.main(["tag", "--ref", "refs/tags/v1.0"]) == 1
    assert capsys.readouterr().out.startswith("::error::release check failed: tag 'v1.0'")


@needs_git
def test_cli_source(repo, tmp_path, monkeypatch, capsys):
    r, shas = repo
    monkeypatch.chdir(r)
    init = write_init(tmp_path, '__version__ = "1.0.0"\n')
    args = ["source", "--sha", shas["on_main"], "--main-ref", "refs/heads/main"]
    assert rc.main([*args, "--ref", "refs/tags/v1.0.0", "--init", str(init)]) == 0
    assert "v1.0.0 is" in capsys.readouterr().out
    # The right commit on main, but __version__ was not bumped for the rc.
    assert rc.main([*args, "--ref", "refs/tags/v1.1.0-rc.1", "--init", str(init)]) == 1
    assert "needs '1.1.0rc1'" in capsys.readouterr().out


def test_cli_dist(tmp_path, capsys):
    dist = good_dist(tmp_path)
    sums = dist / "SHA256SUMS"
    assert rc.main(["dist", "--ref", "refs/tags/v0.0.1-rc.1", "--sums", str(sums), str(dist)]) == 0
    assert sums.read_text().count("  seriatim_node-0.0.1rc1") == 2
    assert "are version 0.0.1rc1" in capsys.readouterr().out
    # SHA256SUMS is now in dist, so a second run (a rebuilt directory) fails closed.
    assert rc.main(["dist", "--ref", "refs/tags/v0.0.1-rc.1", "--sums", str(sums), str(dist)]) == 1


def test_cli_build_constraints(tmp_path, capsys):
    constraints = str(write_constraints(tmp_path))
    good = str(write_pyproject(tmp_path, '["hatchling==1.33.0"]'))
    assert rc.main(["build-constraints", "--pyproject", good, "--constraints", constraints]) == 0
    assert "pins hatchling==1.33.0 by sha256" in capsys.readouterr().out
    bumped = tmp_path / "bumped"
    bumped.mkdir()
    bumped = str(write_pyproject(bumped, '["hatchling==1.34.0"]'))
    assert rc.main(["build-constraints", "--pyproject", bumped, "--constraints", constraints]) == 1
    out = capsys.readouterr().out
    assert out.startswith("::error::release check failed:") and "recompile it" in out


# --- the workflow ---------------------------------------------------------------------------


def test_release_workflow_shape():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    on = wf[True] if True in wf else wf["on"]  # PyYAML reads the key `on` as True
    assert on == {"push": {"tags": ["v*"]}}
    assert wf["permissions"] == {}
    jobs = wf["jobs"]
    assert set(jobs) == {"build", "publish"}
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert "environment" not in jobs["build"]
    publish = jobs["publish"]
    assert publish["environment"] == "release"
    assert publish["needs"] == "build"
    assert publish["permissions"] == {
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }
    # Every action is pinned by a full SHA, and no event value is interpolated into run:.
    for job in jobs.values():
        for step in job["steps"]:
            if "uses" in step:
                ref = step["uses"].partition("@")[2]
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), step
            assert "${{" not in step.get("run", ""), step
    # The build installs its backend only from the hash-pinned constraints, after checking
    # that they cover node/pyproject.toml's [build-system] (#68).
    runs = [s.get("run", "") for s in jobs["build"]["steps"]]
    (build,) = [i for i, r in enumerate(runs) if r.startswith("uv build")]
    assert "--build-constraints node/build-constraints.txt --require-hashes" in runs[build]
    (check,) = [i for i, r in enumerate(runs) if "release_checks.py build-constraints" in r]
    assert check < build
    names = [s.get("name", s.get("uses", "")) for s in publish["steps"]]
    order = [i for i, n in enumerate(names) if n.startswith(("actions/attest", "Create", "Upload"))]
    assert order == sorted(order) and len(order) == 3
    assert names.index("Publish it") > max(order)


def test_script_runs_isolated_and_fails_closed():
    r = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools/ci/release_checks.py"), "tag", "--ref", "v1.0.0"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 1
    assert "is not a tag ref" in r.stdout
