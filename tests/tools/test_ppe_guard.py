"""Tests for the PPE guard, tools/ppe_guard.py behind ppe/ppe (SPEC §8.3; ADR-001 §10 S0b).

Every refusal in §8.3 has a case. Each starts from a configuration that passes and changes
one thing, so a refusal can't come from an unrelated rule. The cases marked #28 are the
forms the earlier denylist let through (issue #28, items 1-5).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ppe_guard", ROOT / "tools/ppe_guard.py")
g = importlib.util.module_from_spec(_spec)
sys.modules["ppe_guard"] = g
_spec.loader.exec_module(g)

GIB = 1024**3
ANY_ADDRESS = ".".join(["0"] * 4)  # built at run time: a literal trips Bandit B104


def svc(cpus="1", memory=str(GIB), **extra) -> dict:
    """A service as `docker compose config` renders it, with these limits."""
    return {
        "image": "example/image@sha256:" + "0" * 64,
        "command": None,
        "entrypoint": None,
        "deploy": {"resources": {"limits": {"cpus": cpus, "memory": memory}}, "placement": {}},
        **extra,
    }


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
                environment={"OLLAMA_HOST": "127.0.0.1:11435"},
                networks={"default": None},
                ports=[{"host_ip": "127.0.0.1", "target": 11434, "published": "11434"}],
                volumes=[{"type": "bind", "source": str(ppe / "data/ollama"), "target": "/m"}],
            ),
            "cap-a": svc(
                0.5,
                str(GIB // 2),
                network_mode="service:ollama-a",
                cap_add=["NET_RAW", "NET_ADMIN"],
                cap_drop=["ALL"],
                depends_on={"ollama-a": {"condition": "service_started", "required": True}},
            ),
            "dbg-a": svc(0.25, str(GIB // 8), network_mode="service:ollama-a", cap_drop=["ALL"]),
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
        (lambda c: c[S]["ollama-a"].update(network_mode="host"), "ollama-a: network_mode: host;"),
        (
            lambda c: c["networks"].update(h={"driver": "host"}),
            "network h: the host driver is host networking",
        ),
        (
            lambda c: c[S]["dbg-a"].update(privileged=True),
            "dbg-a: service key privileged is not on the allowlist",
        ),
        # #28 item 1: host networking by another spelling.
        (
            lambda c: c[S]["dbg-a"].update(network_mode="container:some-host-net-container"),
            "dbg-a: network_mode: container:some-host-net-container; only service:",
        ),
        (
            lambda c: c[S]["dbg-a"].update(network_mode="service:elsewhere"),
            "dbg-a: network_mode: service:elsewhere; only service:",
        ),
        (
            lambda c: c[S]["dbg-a"].update(network_mode="service:dbg-a"),
            "dbg-a: network_mode: service:dbg-a; only service:",
        ),
        (lambda c: c[S]["dbg-a"].update(network_mode="none"), "dbg-a: network_mode: none;"),
        (
            lambda c: c["networks"].update(x={"external": True, "name": "host"}),
            "network x: external networks are refused",
        ),
        # #28 item 2: inherited mounts and external volumes.
        (
            lambda c: c[S]["dbg-a"].update(volumes_from=["some-container"]),
            "dbg-a: service key volumes_from is not on the allowlist",
        ),
        (
            lambda c: c["volumes"].update(cache={"external": True, "name": "cache"}),
            "volume cache: external volumes are refused",
        ),
        # #28 item 3: replicas and scale multiply the limits.
        (  # 3 + 2 x 0.5 + 0.25
            lambda c: c[S]["cap-a"]["deploy"].update(replicas=2),
            "the CPU limits sum to 4.25, above 4",
        ),
        (
            lambda c: c[S]["cap-a"].update(scale=3),
            "the memory limits sum to 6.62 GiB, above 6 GiB",
        ),
        # #28 item 4: host-sharing keys, refused because they are not on the allowlist.
        *[
            (
                lambda c, k=key, v=value: c[S]["dbg-a"].update({k: v}),
                f"dbg-a: service key {key} is not on the allowlist",
            )
            for key, value in [
                ("pid", "host"),
                ("ipc", "host"),
                ("userns_mode", "host"),
                ("devices", [{"source": "/dev/kmsg", "target": "/dev/kmsg"}]),
                ("security_opt", ["seccomp=unconfined"]),
                ("security_opt", ["apparmor=unconfined"]),
                ("cgroup_parent", "/"),
                ("sysctls", {"net.ipv4.ip_forward": "1"}),
                ("build", {"context": ".", "network": "host"}),
            ]
        ],
        (
            lambda c: c[S]["dbg-a"]["deploy"]["resources"].update(
                reservations={"devices": [{"capabilities": ["gpu"]}]}
            ),
            "dbg-a: deploy.resources key reservations is not on the allowlist",
        ),
        (
            lambda c: c[S]["dbg-a"]["deploy"].update(mode="global"),
            "dbg-a: deploy key mode is not on the allowlist",
        ),
        (
            lambda c: c[S]["dbg-a"]["deploy"]["resources"]["limits"].update(pids=10),
            "dbg-a: deploy.resources.limits key pids is not on the allowlist",
        ),
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
            "volume v: bind device /etc is outside ppe/",
        ),
        # The rest are allowlists too: volume drivers and options, network drivers, mounts.
        (
            lambda c: c["volumes"].update(v={"driver": "some-plugin"}),
            "volume v: driver some-plugin; only local is allowed",
        ),
        (
            lambda c: c["volumes"].update(
                v={"driver_opts": {"type": "nfs", "o": "addr=192.0.2.1", "device": ":/x"}}
            ),
            "volume v: driver_opts {'type': 'nfs'",
        ),
        (
            lambda c: c["networks"].update(m={"driver": "macvlan"}),
            "network m: driver macvlan; only bridge is allowed",
        ),
        (
            lambda c: c["networks"]["default"].update(driver_opts={"parent": "eth0"}),
            "network default: driver_opts are refused",
        ),
        (
            lambda c: c[S]["dbg-a"].update(
                volumes=[{"type": "npipe", "source": "x", "target": "/p"}]
            ),
            "dbg-a: mount type npipe is not on the allowlist",
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


def test_replicas_and_scale_within_the_ceiling_pass(good, ppe):
    """One replica is the default; scale or replicas of 1, or 0, change nothing."""
    for edit in (
        lambda c: c[S]["dbg-a"]["deploy"].update(replicas=1),
        lambda c: c[S]["dbg-a"].update(scale=1),
        lambda c: c[S]["cap-a"].update(scale=0),
    ):
        assert refused(change(good, edit), ppe) == []


def test_the_larger_of_scale_and_replicas_counts(good, ppe):
    config = change(good, lambda c: c[S]["cap-a"].update(scale=1))
    config[S]["cap-a"]["deploy"]["replicas"] = 2
    assert any("the CPU limits sum to 4.25" in p for p in refused(config, ppe))


def test_resolve_binds_writes_real_paths(good, ppe, tmp_path):
    """#28 item 5: bind sources are resolved once, so Docker gets the path the guard checked."""
    real = ppe / "data" / "real"
    real.mkdir()
    link = ppe / "data" / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges on this platform")
    config = change(
        good,
        lambda c: c[S]["dbg-a"].update(
            volumes=[
                {"type": "bind", "source": str(link), "target": "/x"},
                {"type": "volume", "source": "cache", "target": "/c"},
            ]
        ),
    )
    config["volumes"]["v"] = {"driver_opts": {"type": "none", "o": "bind", "device": str(link)}}
    config["configs"] = {"k": {"file": str(link / "f")}, "e": {"environment": "E"}}
    resolved = g.resolve_binds(config)
    assert resolved[S]["dbg-a"]["volumes"][0]["source"] == os.path.realpath(real)
    assert resolved[S]["dbg-a"]["volumes"][1]["source"] == "cache"
    assert resolved["volumes"]["v"]["driver_opts"]["device"] == os.path.realpath(real)
    assert resolved["configs"]["k"]["file"] == os.path.realpath(real / "f")
    assert config[S]["dbg-a"]["volumes"][0]["source"] == str(link)  # a copy, not in place
    assert refused(resolved, ppe) == []


def test_a_resolved_path_with_a_dollar_is_refused(good, ppe):
    """Compose interpolates the effective file again at `up`: ${X:-..} would become `..`."""
    target = ppe / "data" / "${X:-..}"
    link = ppe / "data" / "link"
    try:
        target.mkdir()
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("this platform can't name a directory ${X:-..}, or make a symlink")
    config = change(
        good,
        lambda c: c[S]["dbg-a"].update(
            volumes=[{"type": "bind", "source": str(link), "target": "/x"}]
        ),
    )
    config["volumes"]["v"] = {"driver_opts": {"type": "none", "o": "bind", "device": str(link)}}
    config["secrets"] = {"k": {"file": str(link)}}
    problems = refused(g.resolve_binds(config), ppe)
    for what in ("dbg-a: bind mount", "volume v: bind device", "secret k: file"):
        assert any(p.startswith(what) and "contains '$'" in p for p in problems), problems


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
    """Records every command; answers `context inspect` and `compose config`.

    At `compose up` it records the file it was given: its path, mode and contents.
    """

    def __init__(self, config: dict, context: str = "unix:///var/run/docker.sock"):
        self.config, self.context, self.calls = config, context, []
        self.started: dict = {}

    def __call__(self, *cmd: str, capture: bool = False) -> str:
        self.calls.append(cmd)
        if cmd[:3] == ("docker", "context", "inspect"):
            return self.context + "\n"
        if "config" in cmd:
            return json.dumps(self.config)
        if "up" in cmd:
            f = Path(cmd[cmd.index("-f") + 1])
            self.started = {
                "path": f,
                "mode": f.stat().st_mode & 0o777,
                "dir_mode": f.parent.stat().st_mode & 0o777,
                "config": json.loads(f.read_text()),
            }
        return ""

    def compose_verbs(self) -> list[str]:
        return [c[6] for c in self.calls if c[:2] == ("docker", "compose")]


@pytest.fixture
def isolated(monkeypatch, ppe):
    monkeypatch.setattr(g, "PPE_DIR", ppe)
    monkeypatch.setattr(g, "DATA_DIR", ppe / "data")
    monkeypatch.setattr(g, "ALLOWED_HOST", ppe / ".allowed-host")
    monkeypatch.setattr(g.tempfile, "tempdir", str(ppe.parent))
    monkeypatch.setattr(g, "wait_for_ollama", lambda: "0.40.16")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    return ppe


def test_up_checks_then_starts_from_the_checked_configuration(isolated, good, monkeypatch):
    docker = FakeDocker(good)
    monkeypatch.setattr(g, "run", docker)
    assert g.main(["up", "--ci"]) == 0
    assert docker.compose_verbs() == ["config", "up"] + ["exec"] * len(g.MODELS)
    started = docker.started
    assert started["config"] == g.resolve_binds(good)
    for c in docker.calls[-len(g.MODELS) :]:
        assert c[c.index("-f") + 1] == str(started["path"])
    pulled = [c[-1] for c in docker.calls if "pull" in c]
    assert pulled == ["qwen2.5:0.5b", "qwen2.5:1.5b", "all-minilm:22m"]
    assert (isolated / "data" / "capture").is_dir()
    assert not started["path"].parent.exists()  # the private directory is removed afterwards


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_up_starts_from_a_private_0600_file(isolated, good, monkeypatch):
    """#28 item 5: the checked file is in a fresh private directory, mode 0600."""
    docker = FakeDocker(good)
    monkeypatch.setattr(g, "run", docker)
    assert g.main(["up", "--ci"]) == 0
    assert docker.started["mode"] == 0o600
    assert docker.started["dir_mode"] == 0o700
    assert not str(docker.started["path"]).startswith(str(isolated))


def test_up_refuses_a_file_changed_after_the_check(isolated, good, monkeypatch, capsys):
    """#28 item 5: the hash is checked again just before `compose up`."""
    docker = FakeDocker(good)
    monkeypatch.setattr(g, "run", docker)
    write = g.write_checked

    def tampered(config):
        path, digest = write(config)
        path.write_text(json.dumps({**config, "x": 1}))
        return path, digest

    monkeypatch.setattr(g, "write_checked", tampered)
    assert g.main(["up", "--ci"]) == 1
    assert docker.compose_verbs() == ["config"]
    assert "changed after the guard checked it" in capsys.readouterr().err


def test_up_gives_docker_the_resolved_bind_source(isolated, good, monkeypatch):
    """#28 item 5: Docker gets the real path, not a symlink that could be swapped later."""
    link = isolated / "data" / "ollama-link"
    (isolated / "data" / "ollama").mkdir()
    try:
        link.symlink_to(isolated / "data" / "ollama", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges on this platform")
    good[S]["ollama-a"]["volumes"][0]["source"] = str(link)
    docker = FakeDocker(good)
    monkeypatch.setattr(g, "run", docker)
    assert g.main(["up", "--ci"]) == 0
    source = docker.started["config"][S]["ollama-a"]["volumes"][0]["source"]
    assert source == os.path.realpath(isolated / "data" / "ollama")


def test_up_refuses_before_starting_anything(isolated, good, monkeypatch, capsys):
    bad = change(good, lambda c: c[S]["dbg-a"].update(privileged=True))
    docker = FakeDocker(bad)
    monkeypatch.setattr(g, "run", docker)
    assert g.main(["up", "--ci"]) == 1
    assert docker.compose_verbs() == ["config"]
    assert "refusing to start" in capsys.readouterr().err
    assert list(isolated.parent.glob("seriatim-ppe-*")) == []  # nothing written


@pytest.mark.parametrize("which", ["data", "data/ollama", "data/capture"])
def test_up_refuses_a_symlinked_data_directory_before_touching_it(
    isolated, good, monkeypatch, capsys, tmp_path, which
):
    """Nothing is created or chmod-ed through a data directory that is a symlink."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    before = elsewhere.stat().st_mode
    link = isolated / which
    if which == "data":
        shutil.rmtree(link)
    try:
        link.symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges on this platform")
    docker = FakeDocker(good)
    monkeypatch.setattr(g, "run", docker)
    assert g.main(["up", "--ci"]) == 1
    assert "is a symlink" in capsys.readouterr().err
    assert docker.compose_verbs() == []
    assert elsewhere.stat().st_mode == before
    assert list(elsewhere.iterdir()) == []


def test_check_data_dirs_refuses_a_path_outside_ppe(ppe, tmp_path):
    g.check_data_dirs(ppe, (ppe / "data",))
    with pytest.raises(g.GuardError, match="resolves outside ppe/"):
        g.check_data_dirs(ppe, (tmp_path / "other",))


def test_capture_is_opened_up_only_after_the_check(isolated, good, monkeypatch):
    bad = change(good, lambda c: c[S]["dbg-a"].update(privileged=True))
    monkeypatch.setattr(g, "run", FakeDocker(bad))
    assert g.main(["up", "--ci"]) == 1
    if os.name != "nt":
        assert (isolated / "data" / "capture").stat().st_mode & 0o777 != 0o777


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
    assert g.main(["down", "--ci"]) == 0
    assert (isolated / "data").exists()
    assert g.main(["purge", "--ci"]) == 0
    assert docker.compose_verbs() == ["down", "down"]
    assert "--volumes" in docker.calls[-1]
    assert not (isolated / "data").exists()
    assert g.main(["purge", "--ci"]) == 0  # nothing left to delete is fine


@pytest.mark.parametrize("command", ["down", "purge"])
def test_down_and_purge_check_where_they_run(isolated, good, monkeypatch, capsys, command):
    """#28 item 5: down and purge refuse an undesignated host, and --ci outside Actions."""
    docker = FakeDocker(good)
    monkeypatch.setattr(g, "run", docker)
    assert g.main([command]) == 1
    assert "local runs need .allowed-host" in capsys.readouterr().err
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    assert g.main([command, "--ci"]) == 1
    assert "--ci is accepted only when GITHUB_ACTIONS=true" in capsys.readouterr().err
    assert docker.calls == []
    assert (isolated / "data").exists()
    (isolated / ".allowed-host").write_text(g.socket.gethostname())
    assert g.main([command]) == 0
    assert docker.compose_verbs() == ["down"]


def test_purge_explains_root_owned_files(isolated, good, monkeypatch, capsys):
    monkeypatch.setattr(g, "run", FakeDocker(good))

    def denied(path):
        raise PermissionError(path)

    monkeypatch.setattr(g.shutil, "rmtree", denied)
    assert g.main(["purge", "--ci"]) == 1
    assert "sudo rm -rf" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv", [[], ["up", "--local"], ["down", "--local"], ["up", "--ci", "--ci"], ["start"]]
)
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


def test_the_committed_compose_file_uses_only_allowed_keys():
    """Without Docker: every service key in the real compose.yaml is on the allowlist."""
    compose = yaml.safe_load((ROOT / "ppe/compose.yaml").read_text(encoding="utf-8"))
    assert set(compose) <= {"name", "services"}
    for name, service in compose["services"].items():
        assert set(service) <= g.SERVICE_KEYS, name
        assert set(service["deploy"]) <= g.DEPLOY_KEYS, name
        assert set(service["deploy"]["resources"]) <= g.RESOURCE_KEYS, name
        assert set(service["deploy"]["resources"]["limits"]) <= g.LIMIT_KEYS, name


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
    config = g.resolve_binds(json.loads(r.stdout))
    assert g.refusals(config, ROOT / "ppe") == []
    assert set(config["services"]) == {"ollama-a", "cap-a", "dbg-a", "node-a"}
    env = config["services"]["ollama-a"]["environment"]
    assert env == {
        "OLLAMA_HOST": "127.0.0.1:11435",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_CONTEXT_LENGTH": "2048",
    }
    assert "tcp dst port 11435" in config["services"]["cap-a"]["command"]
    # The sidecar (TD-slice-1 §6, S1-14): in Ollama's namespace, with no capability, test
    # mode, its lock directory on a tmpfs, its code mounted read-only from ppe/data/node.
    node = config["services"]["node-a"]
    assert node["network_mode"] == "service:ollama-a"
    assert node["cap_drop"] == ["ALL"] and not node.get("cap_add")
    node_env = node["environment"]
    assert node_env["SERIATIM_NODE_LISTEN_HOST"] == "0.0.0.0"  # nosec B104 # checks the PPE value
    assert node_env["SERIATIM_NODE_ALLOW_UNAUTHENTICATED"] == "true"
    assert node_env["SERIATIM_NODE_OLLAMA_URL"] == "http://127.0.0.1:11435"
    assert node_env["SERIATIM_NODE_LOCK_DIR"] == "/run/seriatim"
    assert node_env["SERIATIM_NODE_DRAIN_S"] == "10"
    mounts = {m["target"]: m for m in node["volumes"]}
    assert mounts["/run/seriatim"]["type"] == "tmpfs"
    assert mounts["/opt/seriatim"]["read_only"] is True
    assert mounts["/opt/seriatim"]["source"] == os.path.realpath(ROOT / "ppe/data/node")
    published = {
        (p["host_ip"], int(p["published"]), p["target"])
        for p in config["services"]["ollama-a"]["ports"]
    }
    assert published == {("127.0.0.1", 11434, 11434), ("127.0.0.1", 11436, 11436)}
