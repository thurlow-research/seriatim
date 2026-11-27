"""The S0a package skeleton: version and entry point."""

import os
import re
import runpy
import subprocess
import sys
import types

import pytest

import seriatim_node
from seriatim_node import __main__ as entry


def test_version_is_pep440_release_or_rc():
    assert re.fullmatch(r"\d+\.\d+\.\d+(rc\d+)?", seriatim_node.__version__)


def test_run_exits_2_without_the_app_module(monkeypatch, capsys):
    def missing(name):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(entry.importlib, "import_module", missing)
    assert entry.run() == 2
    assert capsys.readouterr().err.strip() == entry.NOT_YET


def test_run_calls_app_main_when_present(monkeypatch):
    app = types.ModuleType("seriatim_node.app")
    app.main = lambda: 7
    monkeypatch.setitem(sys.modules, "seriatim_node.app", app)
    assert entry.run() == 7


def test_run_treats_non_int_result_as_success(monkeypatch):
    app = types.ModuleType("seriatim_node.app")
    app.main = lambda: None
    monkeypatch.setitem(sys.modules, "seriatim_node.app", app)
    assert entry.run() == 0


def test_run_reraises_other_missing_modules(monkeypatch):
    def boom(name):
        raise ModuleNotFoundError("No module named 'dependency'", name="dependency")

    monkeypatch.setattr(entry.importlib, "import_module", boom)
    with pytest.raises(ModuleNotFoundError):
        entry.run()


def test_module_main_block_exits_with_run_status(monkeypatch, capsys):
    monkeypatch.setenv("SERIATIM_NODE_SLOTS", "0")  # invalid, so main() exits 2 at once
    monkeypatch.setattr(sys, "argv", ["seriatim-node"])  # not pytest's own arguments (S2-4)
    monkeypatch.delitem(sys.modules, "seriatim_node.app", raising=False)
    monkeypatch.delitem(sys.modules, "seriatim_node.__main__", raising=False)
    with pytest.raises(SystemExit) as e:
        runpy.run_module("seriatim_node", run_name="__main__", alter_sys=True)
    assert e.value.code == 2
    assert "SERIATIM_NODE_SLOTS" in capsys.readouterr().err  # the config error, not usage


@pytest.mark.process
def test_module_entry_point_runs_main():
    env = {k: v for k, v in os.environ.items() if not k.startswith("SERIATIM_NODE_")}
    env["SERIATIM_NODE_SLOTS"] = "0"  # invalid, so main() exits 2 before it binds anything
    r = subprocess.run(
        [sys.executable, "-m", "seriatim_node"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert r.returncode == 2
    assert "SERIATIM_NODE_SLOTS" in r.stderr
