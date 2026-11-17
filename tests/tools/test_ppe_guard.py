"""Tests for the PPE guard, tools/ppe_guard.py behind ppe/ppe (SPEC §8.3; ADR-001 §10 S0b).

Every refusal in §8.3 has a case. Each starts from a configuration that passes and changes
one thing, so a refusal can't come from an unrelated rule.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ppe_guard", ROOT / "tools/ppe_guard.py")
g = importlib.util.module_from_spec(_spec)
sys.modules["ppe_guard"] = g
_spec.loader.exec_module(g)

GIB = 1024**3
ANY_ADDRESS = ".".join(["0"] * 4)  # built at run time: a literal trips Bandit B104


def svc(cpus="1", memory=str(GIB), **extra) -> dict:
    return {"deploy": {"resources": {"limits": {"cpus": cpus, "memory": memory}}}, **extra}


@pytest.fixture
def ppe(tmp_path) -> Path:
    d = tmp_path / "ppe"
    (d / "data").mkdir(parents=True)
    return d


@pytest.fixture
def good(ppe) -> dict:
    """The shape of the real effective configuration, which passes."""
    return {
        "name": "seriatim-ppe",
        "services": {
            "ollama-a": svc(
                "3",
                str(5 * GIB),
                ports=[{"host_ip": "127.0.0.1", "target": 11434, "published": "11434"}],
                volumes=[{"type": "bind", "source": str(ppe / "data/ollama"), "target": "/m"}],
            ),
            "cap-a": svc(
                0.5,
                str(GIB // 2),
                network_mode="service:ollama-a",
                cap_add=["NET_RAW", "NET_ADMIN"],
                cap_drop=["ALL"],
            ),
            "dbg-a": svc(0.25, str(GIB // 8), network_mode="service:ollama-a"),
        },
        "networks": {"default": {"name": "seriatim-ppe_default"}},
        "volumes": {"cache": {}},
    }


def refused(config: dict, ppe: Path) -> list[str]:
    return g.refusals(config, ppe)


def test_the_good_configuration_passes(good, ppe):
    assert refused(good, ppe) == []


def test_limits_exactly_at_the_maximum_pass(good, ppe):
    good["services"]["ollama-a"] = svc("3.25", str(5 * GIB + GIB * 3 // 8))  # 4 CPUs, 6 GiB
    assert refused(good, ppe) == []


def change(good: dict, edit) -> dict:
    config = copy.deepcopy(good)
    edit(config)
    return config


S = "services"


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        # A sum of per-service limits above 4 CPUs or 6 GiB.
        (lambda c: c[S]["ollama-a"].update(svc("3.5")), "the CPU limits sum to 4.25, above 4"),
        (
            lambda c: c[S]["ollama-a"].update(svc("3", str(6 * GIB))),
            "the memory limits sum to 6.62 GiB, above 6 GiB",
        ),
        (lambda c: c[S].update({"extra": svc("1")}), "the CPU limits sum to 4.75, above 4"),
        # The tighter of deploy and service-level limits counts; none at all is unbounded.
        (lambda c: c[S]["dbg-a"].pop("deploy"), "dbg-a: no CPU limit"),
        (lambda c: c[S]["dbg-a"].pop("deploy"), "dbg-a: no memory limit"),
        (
            lambda c: c[S]["dbg-a"]["deploy"]["resources"]["limits"].pop("memory"),
            "dbg-a: no memory limit",
        ),
        # A published port that is not on 127.0.0.1.
        (
            lambda c: c[S]["ollama-a"]["ports"][0].update(host_ip=ANY_ADDRESS),
            "is published on 0.0.0.0, not 127.0.0.1",
        ),
        (
            lambda c: c[S]["ollama-a"]["ports"][0].pop("host_ip"),
            "is published on every address, not 127.0.0.1",
        ),
        (
            lambda c: c[S]["ollama-a"]["ports"][0].update(host_ip="::1"),
            "is published on ::1, not 127.0.0.1",
        ),
        # Host networking and privileged.
        (lambda c: c[S]["ollama-a"].update(network_mode="host"), "ollama-a: network_mode: host"),
        (
            lambda c: c["networks"].update(h={"driver": "host"}),
            "network h: the host driver is host networking",
        ),
        (lambda c: c[S]["dbg-a"].update(privileged=True), "dbg-a: privileged"),
        # Capabilities: only NET_RAW/NET_ADMIN, only on capture containers, which drop the rest.
        (
            lambda c: c[S]["cap-a"]["cap_add"].append("SYS_ADMIN"),
            "cap-a: capabilities ['SYS_ADMIN'] beyond NET_RAW/NET_ADMIN",
        ),
        (
            lambda c: c[S]["cap-a"].update(cap_add=["ALL"]),
            "cap-a: capabilities ['ALL'] beyond NET_RAW/NET_ADMIN",
        ),
        (
            lambda c: c[S]["cap-a"].pop("cap_drop"),
            "cap-a: a capture container must drop all other capabilities",
        ),
        (
            lambda c: c[S]["dbg-a"].update(cap_add=["CAP_NET_RAW"]),
            "dbg-a: added capabilities ['NET_RAW']; only capture containers",
        ),
        # A Docker socket mount, or a bind mount outside ppe/.
        (
            lambda c: c[S]["dbg-a"].update(
                volumes=[{"type": "bind", "source": "/var/run/docker.sock", "target": "/s"}]
            ),
            "dbg-a: mounts the Docker socket (/var/run/docker.sock)",
        ),
        (
            lambda c: c[S]["dbg-a"].update(
                volumes=[{"type": "volume", "source": "x", "target": "/run/docker.sock"}]
            ),
            "dbg-a: mounts the Docker socket (x)",
        ),
        (
            lambda c: c[S]["dbg-a"].update(volumes=["/etc:/host-etc"]),
            "dbg-a: bind mount /etc:/host-etc is outside ppe/",
        ),
        (
            lambda c: c["volumes"].update(
                v={"driver_opts": {"type": "none", "o": "bind", "device": "/etc"}}
            ),
            "volume v: binds /etc, outside ppe/",
        ),
        (lambda c: c.update(configs={"k": {"file": "/etc/passwd"}}), "config k: file /etc/passwd"),
        (lambda c: c.update(secrets={"k": {"file": "/root/.ssh/id"}}), "secret k: file /root"),
        (lambda c: c.update(services={}), "the configuration has no services"),
    ],
)
def test_each_rule_refuses(good, ppe, edit, message):
    problems = refused(change(good, edit), ppe)
    assert any(message in p for p in problems), problems


def test_bind_mounts_resolve_symlinks_and_dot_dots(good, ppe, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ppe / "data" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges on this platform")
    for source in (str(link), str(ppe / "data" / ".." / ".." / "outside")):
        config = change(
            good,
            lambda c, s=source: c[S]["dbg-a"].update(
                volumes=[{"type": "bind", "source": s, "target": "/x"}]
            ),
        )
        assert any("is outside ppe/" in p for p in refused(config, ppe))


def test_a_mount_of_ppe_itself_is_inside(good, ppe):
    config = change(
        good,
        lambda c: c[S]["dbg-a"].update(
            volumes=[{"type": "bind", "source": str(ppe), "target": "/x"}]
        ),
    )
    assert refused(config, ppe) == []


# --- where it may run -------------------------------------------------------------------


def test_ci_needs_github_actions(tmp_path):
    g.check_where(True, {"GITHUB_ACTIONS": "true"}, "runner", tmp_path / ".allowed-host")
    for env in ({}, {"GITHUB_ACTIONS": "false"}, {"GITHUB_ACTIONS": "1"}):
        with pytest.raises(g.GuardError, match="--ci is accepted only when GITHUB_ACTIONS=true"):
            g.check_where(True, env, "runner", tmp_path / ".allowed-host")


def test_a_local_run_needs_the_allowed_host(tmp_path):
    allowed = tmp_path / ".allowed-host"
    with pytest.raises(g.GuardError, match="local runs need .allowed-host"):
        g.check_where(False, {"GITHUB_ACTIONS": "true"}, "dev-box", allowed)
    allowed.write_text("dev-box\n")
    g.check_where(False, {}, "dev-box", allowed)
    for host in ("other", "dev-box2", ""):
        with pytest.raises(g.GuardError, match="is not the one .allowed-host names"):
            g.check_where(False, {}, host, allowed)
    allowed.write_text("\n")
    with pytest.raises(g.GuardError, match="is not the one"):
        g.check_where(False, {}, "", allowed)


@pytest.mark.parametrize(
    ("env", "context", "ok"),
    [
        ({}, "unix:///var/run/docker.sock", True),
        ({}, "unix:///run/user/1000/docker.sock", True),
        ({"DOCKER_HOST": "unix:///var/run/docker.sock"}, "tcp://192.0.2.1:2376", True),
        ({}, "tcp://192.0.2.1:2376", False),
        ({}, "ssh://user@example.invalid", False),
        ({"DOCKER_HOST": "tcp://192.0.2.1:2375"}, "unix:///var/run/docker.sock", False),
        ({"DOCKER_HOST": "ssh://example.invalid"}, "unix:///var/run/docker.sock", False),
    ],
)
def test_only_a_local_socket_endpoint(env, context, ok):
    if ok:
        g.check_endpoint(env, context)
    else:
        with pytest.raises(g.GuardError, match="is not the local socket"):
            g.check_endpoint(env, context)


# --- up, down, purge, without Docker ------------------------------------------------------


class FakeDocker:
    """Records every command; answers `context inspect` and `compose config`."""

    def __init__(self, config: dict, context: str = "unix:///var/run/docker.sock"):
        self.config, self.context, self.calls = config, context, []

    def __call__(self, *cmd: str, capture: bool = False) -> str:
        self.calls.append(cmd)
        if cmd[:3] == ("docker", "context", "inspect"):
            return self.context + "\n"
        if "config" in cmd:
            return json.dumps(self.config)
        return ""

    def compose_verbs(self) -> list[str]:
        return [c[6] for c in self.calls if c[:2] == ("docker", "compose")]


@pytest.fixture
def isolated(monkeypatch, ppe):
    monkeypatch.setattr(g, "PPE_DIR", ppe)
    monkeypatch.setattr(g, "DATA_DIR", ppe / "data")
    monkeypatch.setattr(g, "EFFECTIVE", ppe / "data" / "effective-compose.json")
    monkeypatch.setattr(g, "ALLOWED_HOST", ppe / ".allowed-host")
    monkeypatch.setattr(g, "wait_for_ollama", lambda: "0.40.16")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    return ppe


def test_up_checks_then_starts_from_the_checked_configuration(isolated, good, monkeypatch):
    docker = FakeDocker(good)
    monkeypatch.setattr(g, "run", docker)
    assert g.main(["up", "--ci"]) == 0
    assert docker.compose_verbs() == ["config", "up"] + ["exec"] * len(g.MODELS)
    started = [c for c in docker.calls if "up" in c][0]
    assert started[started.index("-f") + 1] == str(g.EFFECTIVE)
    assert json.loads(g.EFFECTIVE.read_text()) == good
    pulled = [c[-1] for c in docker.calls if "pull" in c]
    assert pulled == ["qwen2.5:0.5b", "qwen2.5:1.5b", "all-minilm:22m"]
    assert (isolated / "data" / "capture").is_dir()


def test_up_refuses_before_starting_anything(isolated, good, monkeypatch, capsys):
    bad = change(good, lambda c: c[S]["dbg-a"].update(privileged=True))
    docker = FakeDocker(bad)
    monkeypatch.setattr(g, "run", docker)
    assert g.main(["up", "--ci"]) == 1
    assert docker.compose_verbs() == ["config"]
    assert "refusing to start" in capsys.readouterr().err
    assert not g.EFFECTIVE.exists()


def test_up_refuses_a_remote_endpoint_before_rendering(isolated, good, monkeypatch, capsys):
    docker = FakeDocker(good, context="tcp://192.0.2.1:2376")
    monkeypatch.setattr(g, "run", docker)
    assert g.main(["up", "--ci"]) == 1
    assert docker.compose_verbs() == []
    assert "is not the local socket" in capsys.readouterr().err


def test_up_without_ci_on_an_undesignated_host(isolated, good, monkeypatch, capsys):
    docker = FakeDocker(good)
    monkeypatch.setattr(g, "run", docker)
    assert g.main(["up"]) == 1
    assert docker.calls == []
    assert "local runs need .allowed-host" in capsys.readouterr().err


def test_down_and_purge(isolated, good, monkeypatch):
    docker = FakeDocker(good)
    monkeypatch.setattr(g, "run", docker)
    (isolated / "data" / "ollama").mkdir()
    assert g.main(["down"]) == 0
    assert (isolated / "data").exists()
    assert g.main(["purge"]) == 0
    assert docker.compose_verbs() == ["down", "down"]
    assert "--volumes" in docker.calls[-1]
    assert not (isolated / "data").exists()
    assert g.main(["purge"]) == 0  # nothing left to delete is fine


def test_purge_explains_root_owned_files(isolated, good, monkeypatch, capsys):
    monkeypatch.setattr(g, "run", FakeDocker(good))

    def denied(path):
        raise PermissionError(path)

    monkeypatch.setattr(g.shutil, "rmtree", denied)
    assert g.main(["purge"]) == 1
    assert "sudo rm -rf" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["up", "--local"], ["down", "--ci"], ["start"]])
def test_usage(argv, capsys):
    assert g.main(argv) == 2
    assert "ppe/ppe up [--ci]" in capsys.readouterr().err


def test_run_reports_a_failed_command():
    with pytest.raises(g.GuardError, match="exited 3"):
        g.run(sys.executable, "-c", "import sys; sys.exit(3)")
    assert g.run(sys.executable, "-c", "print('ok')", capture=True).strip() == "ok"


def test_wait_for_ollama_times_out(monkeypatch):
    monkeypatch.setattr(g, "OLLAMA_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    with pytest.raises(g.GuardError, match="did not answer"):
        g.wait_for_ollama(timeout=0)


def test_the_committed_compose_file_passes_the_guard():
    """The real compose.yaml, rendered by Docker Compose, passes (skipped without Docker)."""
    if not shutil.which("docker"):
        pytest.skip("docker is not installed")
    r = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "ppe/compose.yaml"), "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        pytest.skip(f"docker compose is unavailable: {r.stderr.strip()[:200]}")
    config = json.loads(r.stdout)
    assert g.refusals(config, ROOT / "ppe") == []
    assert set(config["services"]) == {"ollama-a", "cap-a", "dbg-a"}
    env = config["services"]["ollama-a"]["environment"]
    assert env == {
        "OLLAMA_HOST": "127.0.0.1:11435",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_CONTEXT_LENGTH": "2048",
    }
    assert "tcp dst port 11435" in config["services"]["cap-a"]["command"]
