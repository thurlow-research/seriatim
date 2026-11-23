"""tools/ppe_stage_node.py: staging node-a's code (TD-slice-1 §8.6, S1-14; #69, question 1).

uv is not run here: `stage()` takes the command runner, so these tests check what it would
run and where it would write, without a network or a lock export.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("ppe_stage_node", ROOT / "tools/ppe_stage_node.py")
s = importlib.util.module_from_spec(_spec)
sys.modules["ppe_stage_node"] = s
_spec.loader.exec_module(s)


def test_the_install_requires_hashes_wheels_only_and_no_resolution():
    export, install = s.commands(Path("/r.txt"), Path("/stage"))
    assert export[:3] == ["uv", "export", "--frozen"]
    assert "--no-dev" in export and export[export.index("--package") + 1] == "seriatim-node"
    assert install[:3] == ["uv", "pip", "install"]
    for flag in ("--require-hashes", "--no-deps"):
        assert flag in install
    assert install[install.index("--only-binary") + 1] == ":all:"
    assert install[install.index("--target") + 1] == str(Path("/stage"))
    assert install[install.index("--python-version") + 1] == "3.12"
    requirements = str(Path("/r.txt"))
    assert install[install.index("-r") + 1] == export[export.index("-o") + 1] == requirements


def test_the_python_version_matches_node_a_s_image():
    compose = (ROOT / "ppe/compose.yaml").read_text(encoding="utf-8")
    assert f"image: python:{s.PYTHON_VERSION}." in compose


@pytest.fixture
def ppe(tmp_path, monkeypatch):
    """A scratch ppe/ with the real package beside it."""
    ppe_dir = tmp_path / "ppe"
    ppe_dir.mkdir()
    monkeypatch.setattr(s, "PPE_DIR", ppe_dir)
    monkeypatch.setattr(s, "DATA_DIR", ppe_dir / "data")
    monkeypatch.setattr(s, "STAGE", ppe_dir / "data" / "node")
    return ppe_dir


def test_stage_runs_both_commands_and_copies_the_package(ppe):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    old = ppe / "data" / "node" / "stale.py"
    old.parent.mkdir(parents=True)
    old.write_text("left from a previous stage")
    out = s.stage(run)
    assert [c[:2] for c in calls] == [["uv", "export"], ["uv", "pip"]]
    assert not old.exists()  # each stage starts empty
    assert (out / "seriatim_node" / "app.py").read_bytes() == (s.PACKAGE / "app.py").read_bytes()
    assert not list(out.rglob("__pycache__"))


def test_a_failed_uv_command_stops_the_stage(ppe):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 2)

    with pytest.raises(s.GuardError, match="uv export --frozen ... exited 2"):
        s.stage(run)
    assert not (ppe / "data" / "node" / "seriatim_node").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("link", ["data", "data/node"])
def test_a_symlinked_data_directory_is_refused_before_anything_is_written(ppe, tmp_path, link):
    outside = tmp_path / "outside"
    outside.mkdir()
    (ppe / "data").mkdir(exist_ok=link == "data/node")
    if link == "data":
        (ppe / "data").rmdir()
    (ppe / link).symlink_to(outside, target_is_directory=True)
    with pytest.raises(s.GuardError, match="symlink"):
        s.stage(lambda cmd, **kw: pytest.fail("ran uv"))
    assert list(outside.iterdir()) == []


def test_main_reports_a_refusal(monkeypatch, capsys):
    def refuse():
        raise s.GuardError("no")

    monkeypatch.setattr(s, "stage", refuse)
    assert s.main() == 1
    assert "ppe: no" in capsys.readouterr().err
