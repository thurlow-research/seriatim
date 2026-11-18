"""The PPE launcher and guard behind `ppe/ppe` (SPEC §8.3; TD-slice-1 App. B, S0b).

Standard library only. It lives under tools/ so ruff, Bandit and the nosec lint cover it.

    ppe/ppe up [--ci]     check the effective configuration, start the PPE, pull the models
    ppe/ppe down [--ci]   stop it and remove its containers
    ppe/ppe purge [--ci]  down, then delete its volumes and ppe/data/ (the model cache)

**Where it may run.** Every command checks this first. `--ci` is accepted only when
`GITHUB_ACTIONS=true`. Without it, this is a local pre-check run, allowed only on the designated
CPU-only development host: the hostname must equal the contents of the gitignored
`ppe/.allowed-host`. The PPE never runs on, or connects to, a production or GPU machine.

**The guard** is an allowlist. Before anything starts, `up` renders the effective
configuration with `docker compose config`, resolves every bind source to its real path, and
refuses to start if any of these holds:
- a service uses a key that is not on SERVICE_KEYS (so `pid`, `ipc`, `userns_mode`,
  `devices`, `security_opt`, `cgroup_parent`, `sysctls`, `volumes_from`, `privileged` and the
  rest are refused), or a `deploy` key not on the deploy allowlists;
- `network_mode` is set to anything but `service:<another service of this project>`;
- a network or a named volume is external, or a network has the `host` driver;
- the per-service limits, each times the service's `scale` or `deploy.replicas`, sum to more
  than 4 CPUs or 6 GiB (a service with no CPU or memory limit is unbounded, so it is refused
  too);
- a published port is not on 127.0.0.1;
- added capabilities beyond NET_RAW/NET_ADMIN on a capture container (`cap-*`, which must also
  drop all others), or any added capability on another service;
- a Docker socket mount, or a bind mount (including a bind-backed named volume, a config or a
  secret file) whose real path is outside ppe/;
- a Docker endpoint (`DOCKER_HOST`, or the current Docker context) that is not a local unix
  socket.
`up` writes the configuration it checked, with the resolved bind sources, to a file of mode
0600 in a fresh private temporary directory, checks the file's hash again just before
`compose up`, and starts the PPE from that file. Docker therefore gets the real paths the guard
checked, not paths a symlink could redirect after the check.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PPE_DIR = Path(__file__).resolve().parent.parent / "ppe"
COMPOSE_FILE = PPE_DIR / "compose.yaml"
DATA_DIR = PPE_DIR / "data"
ALLOWED_HOST = PPE_DIR / ".allowed-host"
PROJECT = "seriatim-ppe"
OLLAMA_URL = "http://127.0.0.1:11434"
MODELS = ("qwen2.5:0.5b", "qwen2.5:1.5b", "all-minilm:22m")  # no `ollama cp` aliases
MAX_CPUS = 4.0
MAX_MEMORY = 6 * 1024**3
CAPTURE_CAPS = frozenset({"NET_RAW", "NET_ADMIN"})
READY_TIMEOUT = 120
# The service keys known to be safe; any other is refused. Keys that can share the host (pid,
# ipc, userns_mode, devices, security_opt, cgroup_parent, sysctls, volumes_from, privileged,
# extra_hosts, build, ...) are absent on purpose. Where a key is allowed but some of its values
# are not (network_mode, ports, cap_add, volumes), refusals() checks the values.
SERVICE_KEYS = frozenset(
    {
        "cap_add",
        "cap_drop",
        "command",
        "cpus",
        "depends_on",
        "deploy",
        "entrypoint",
        "environment",
        "image",
        "mem_limit",
        "network_mode",
        "networks",
        "ports",
        "scale",
        "volumes",
    }
)
DEPLOY_KEYS = frozenset({"placement", "replicas", "resources"})
RESOURCE_KEYS = frozenset({"limits"})  # not reservations, which can claim devices (GPUs)
LIMIT_KEYS = frozenset({"cpus", "memory"})


class GuardError(Exception):
    """The PPE must not start here; the message says why."""


def _cpus(value: object) -> float:
    return float(value)  # compose renders cpus as a number or a numeric string


def _memory(value: object) -> int:
    """Bytes, from compose's rendered form (an integer or a numeric string)."""
    return int(value)


def _limit(service: dict, deploy_key: str, service_key: str, parse) -> float | None:
    """The tightest limit a service declares, or None when it declares none."""
    limits = (((service.get("deploy") or {}).get("resources") or {}).get("limits")) or {}
    values = [parse(v) for v in (limits.get(deploy_key), service.get(service_key)) if v]
    return min(values) if values else None


def _instances(service: dict) -> int:
    """How many containers a service starts: its `scale` or `deploy.replicas`, else 1."""
    counts = [service.get("scale"), (service.get("deploy") or {}).get("replicas")]
    return max([int(c) for c in counts if c is not None] or [1])


def _unknown(name: str, where: str, keys: object, allowed: frozenset[str]) -> list[str]:
    return [
        f"{name}: {where} {k} is not on the allowlist" for k in sorted(set(keys or ()) - allowed)
    ]


def _inside(path: str, root: Path) -> bool:
    real = Path(os.path.realpath(path))
    return real == root or root in real.parents


def refusals(config: dict, ppe_dir: Path | None = None) -> list[str]:
    """Every §8.3 rule the effective configuration breaks; empty when it may start."""
    root = Path(os.path.realpath(ppe_dir or PPE_DIR))
    out: list[str] = []
    services = config.get("services") or {}
    if not services:
        out.append("the configuration has no services")
    cpus = memory = 0.0
    for name, svc in sorted(services.items()):
        svc = svc or {}
        deploy = svc.get("deploy") or {}
        resources = deploy.get("resources") or {}
        out += _unknown(name, "service key", svc, SERVICE_KEYS)
        out += _unknown(name, "deploy key", deploy, DEPLOY_KEYS)
        out += _unknown(name, "deploy.resources key", resources, RESOURCE_KEYS)
        out += _unknown(name, "deploy.resources.limits key", resources.get("limits"), LIMIT_KEYS)
        n = _instances(svc)
        c = _limit(svc, "cpus", "cpus", _cpus)
        m = _limit(svc, "memory", "mem_limit", _memory)
        if c is None:
            out.append(f"{name}: no CPU limit, so the 4-CPU total is unbounded")
        else:
            cpus += c * n
        if m is None:
            out.append(f"{name}: no memory limit, so the 6 GiB total is unbounded")
        else:
            memory += m * n
        for port in svc.get("ports") or []:
            host_ip = port.get("host_ip") if isinstance(port, dict) else None
            if host_ip != "127.0.0.1":
                out.append(
                    f"{name}: port {port} is published on {host_ip or 'every address'}"
                    ", not 127.0.0.1"
                )
        mode = svc.get("network_mode")
        if mode is not None and mode not in {f"service:{s}" for s in services if s != name}:
            out.append(
                f"{name}: network_mode: {mode}; only service:<another service of this "
                "project> is allowed"
            )
        added = {str(c).upper().removeprefix("CAP_") for c in svc.get("cap_add") or []}
        dropped = {str(c).upper() for c in svc.get("cap_drop") or []}
        if name.startswith("cap-"):
            if added - CAPTURE_CAPS:
                out.append(
                    f"{name}: capabilities {sorted(added - CAPTURE_CAPS)} beyond NET_RAW/NET_ADMIN"
                )
            if "ALL" not in dropped:
                out.append(
                    f"{name}: a capture container must drop all other capabilities "
                    "(cap_drop: [ALL])"
                )
        elif added:
            out.append(
                f"{name}: added capabilities {sorted(added)}; only capture containers "
                "(cap-*) may add NET_RAW/NET_ADMIN"
            )
        for vol in svc.get("volumes") or []:
            vol = vol if isinstance(vol, dict) else {"type": "bind", "source": str(vol)}
            source, target = str(vol.get("source") or ""), str(vol.get("target") or "")
            if "docker.sock" in source or "docker.sock" in target:
                out.append(f"{name}: mounts the Docker socket ({source or target})")
            elif vol.get("type") == "bind" and not _inside(source, root):
                out.append(f"{name}: bind mount {source} is outside ppe/")
    if cpus > MAX_CPUS:
        out.append(f"the CPU limits sum to {cpus:g}, above {MAX_CPUS:g}")
    if memory > MAX_MEMORY:
        out.append(f"the memory limits sum to {memory / 1024**3:.3g} GiB, above 6 GiB")
    for name, net in sorted((config.get("networks") or {}).items()):
        if (net or {}).get("external"):
            out.append(f"network {name}: external networks are refused")
        if (net or {}).get("driver") == "host":
            out.append(f"network {name}: the host driver is host networking")
    for name, vol in sorted((config.get("volumes") or {}).items()):
        if (vol or {}).get("external"):
            out.append(f"volume {name}: external volumes are refused")
        opts = (vol or {}).get("driver_opts") or {}
        device = opts.get("device")
        if device and "bind" in str(opts.get("o", "")) and not _inside(str(device), root):
            out.append(f"volume {name}: binds {device}, outside ppe/")
    for kind in ("configs", "secrets"):
        for name, item in sorted((config.get(kind) or {}).items()):
            f = (item or {}).get("file")
            if f and not _inside(str(f), root):
                out.append(f"{kind[:-1]} {name}: file {f} is outside ppe/")
    return out


def resolve_binds(config: dict) -> dict:
    """A copy of the configuration with every bind source replaced by its real path.

    The guard checks these resolved paths and Docker starts from them, so a symlink swapped
    in after the check can't redirect a mount.
    """
    config = copy.deepcopy(config)
    for svc in (config.get("services") or {}).values():
        for vol in (svc or {}).get("volumes") or []:
            if isinstance(vol, dict) and vol.get("type") == "bind" and vol.get("source"):
                vol["source"] = os.path.realpath(str(vol["source"]))
    for vol in (config.get("volumes") or {}).values():
        opts = (vol or {}).get("driver_opts") or {}
        if opts.get("device") and "bind" in str(opts.get("o", "")):
            opts["device"] = os.path.realpath(str(opts["device"]))
    for kind in ("configs", "secrets"):
        for item in (config.get(kind) or {}).values():
            if (item or {}).get("file"):
                item["file"] = os.path.realpath(str(item["file"]))
    return config


def write_checked(config: dict) -> tuple[Path, str]:
    """Write the checked configuration, mode 0600, in a fresh private temporary directory.

    Returns the file and the SHA-256 of what was written.
    """
    data = json.dumps(config, indent=1).encode("utf-8")
    path = Path(tempfile.mkdtemp(prefix="seriatim-ppe-")) / "effective-compose.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path, hashlib.sha256(data).hexdigest()


def check_unchanged(path: Path, digest: str) -> None:
    """Refuse to start if the checked file changed after the guard wrote it."""
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise GuardError(f"{path} changed after the guard checked it")


def check_where(ci: bool, env: dict[str, str], hostname: str, allowed_host: Path) -> None:
    """Refuse a --ci run outside GitHub Actions, or a local run on any other host."""
    if ci:
        if env.get("GITHUB_ACTIONS") != "true":
            raise GuardError("--ci is accepted only when GITHUB_ACTIONS=true")
        return
    try:
        allowed = allowed_host.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise GuardError(
            f"local runs need {allowed_host.name} naming this designated CPU-only host"
        ) from None
    if not allowed or allowed != hostname:
        raise GuardError(f"this host ({hostname}) is not the one {allowed_host.name} names")


def check_endpoint(env: dict[str, str], context_host: str) -> None:
    """Refuse any Docker endpoint that is not a local unix socket."""
    docker_host = env.get("DOCKER_HOST", "")
    if docker_host and not docker_host.startswith("unix://"):
        raise GuardError(f"DOCKER_HOST={docker_host} is not the local socket")
    if not docker_host and not context_host.startswith("unix://"):
        raise GuardError(f"the Docker context's endpoint {context_host} is not the local socket")


def run(*cmd: str, capture: bool = False) -> str:
    r = subprocess.run(cmd, check=False, capture_output=capture, text=True)
    if r.returncode != 0:
        detail = f": {r.stderr.strip()}" if capture and r.stderr else ""
        raise GuardError(f"{' '.join(cmd[:4])} ... exited {r.returncode}{detail}")
    return r.stdout if capture else ""


def compose(*args: str, file: Path = COMPOSE_FILE, capture: bool = False) -> str:
    return run("docker", "compose", "-p", PROJECT, "-f", str(file), *args, capture=capture)


def endpoint_ok() -> None:
    host = run(
        "docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", capture=True
    ).strip()
    check_endpoint(dict(os.environ), host)


def wait_for_ollama(timeout: float = READY_TIMEOUT) -> str:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"{OLLAMA_URL}/api/version", timeout=5) as r:  # nosec B310 # fixed loopback URL
                return json.load(r)["version"]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            if time.monotonic() > deadline:
                raise GuardError(
                    f"Ollama did not answer on {OLLAMA_URL} within {timeout:g}s"
                ) from None
            time.sleep(2)


def up(ci: bool) -> None:
    check_where(ci, dict(os.environ), socket.gethostname(), ALLOWED_HOST)
    endpoint_ok()
    # Created before rendering, so the bind sources resolve to directories that exist.
    (DATA_DIR / "ollama").mkdir(parents=True, exist_ok=True)
    capture = DATA_DIR / "capture"
    capture.mkdir(parents=True, exist_ok=True)
    capture.chmod(0o777)  # tcpdump runs as root with no capability but NET_RAW/NET_ADMIN
    config = resolve_binds(json.loads(compose("config", "--format", "json", capture=True)))
    problems = refusals(config)
    if problems:
        raise GuardError("refusing to start:\n  - " + "\n  - ".join(problems))
    effective, digest = write_checked(config)
    try:
        print("ppe: the effective configuration passed the guard", flush=True)
        check_unchanged(effective, digest)
        compose("up", "-d", "--wait", file=effective)
        print(f"ppe: Ollama {wait_for_ollama()} answers on {OLLAMA_URL}", flush=True)
        for model in MODELS:
            # Captured: the progress bars would fill the log. A failure still shows stderr.
            compose("exec", "-T", "ollama-a", "ollama", "pull", model, file=effective, capture=True)
            print(f"ppe: pulled {model}", flush=True)
    finally:
        shutil.rmtree(effective.parent, ignore_errors=True)
    print(f"ppe: up, with {', '.join(MODELS)}", flush=True)


def down(ci: bool) -> None:
    check_where(ci, dict(os.environ), socket.gethostname(), ALLOWED_HOST)
    endpoint_ok()
    compose("down", "--remove-orphans")


def purge(ci: bool) -> None:
    check_where(ci, dict(os.environ), socket.gethostname(), ALLOWED_HOST)
    endpoint_ok()
    compose("down", "--remove-orphans", "--volumes")
    try:
        shutil.rmtree(DATA_DIR)
    except FileNotFoundError:
        pass
    except PermissionError:
        raise GuardError(
            f"{DATA_DIR} holds files the containers wrote as root; remove it with "
            f"'sudo rm -rf {DATA_DIR}'"
        ) from None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        commands = {"up": up, "down": down, "purge": purge}
        if args[:1] and args[0] in commands and args[1:] in ([], ["--ci"]):
            commands[args[0]](ci=args[1:] == ["--ci"])
        else:
            print(__doc__.split("\n\n")[2], file=sys.stderr)
            return 2
    except GuardError as e:
        print(f"ppe: {e}", file=sys.stderr)
        return 1
    return 0
