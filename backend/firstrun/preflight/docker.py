"""Non-destructive Docker sandbox preflight.

The preflight executes only controller-owned inline programs.  Repository content
is never mounted or invoked.  Every Docker call is an explicit argv sequence and
all cleanup is limited to container IDs created by this invocation.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, Sequence

from firstrun.domain.outcomes import Outcome


RUN_LABEL = "firstrun.run_id"
ROLE_LABEL = "firstrun.role"
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_REPO_DIGEST = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
_CONTEXT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_IMAGE_INSPECT_FORMAT = (
    '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}},'
    '"Os":{{json .Os}},"Architecture":{{json .Architecture}}}'
)
_DAEMON_INFO_FORMAT = (
    '{"OSType":{{json .OSType}},"SecurityOptions":{{json .SecurityOptions}},'
    '"ServerVersion":{{json .ServerVersion}}}'
)
_CONTAINER_INSPECT_FORMAT = (
    '{"Image":{{json .Image}},"Config":{'
    '"User":{{json .Config.User}},"Labels":{{json .Config.Labels}},'
    '"Image":{{json .Config.Image}},"Entrypoint":{{json .Config.Entrypoint}},'
    '"Cmd":{{json .Config.Cmd}}},"HostConfig":{'
    '"NetworkMode":{{json .HostConfig.NetworkMode}},'
    '"CapDrop":{{json .HostConfig.CapDrop}},"CapAdd":{{json .HostConfig.CapAdd}},'
    '"SecurityOpt":{{json .HostConfig.SecurityOpt}},'
    '"ReadonlyRootfs":{{json .HostConfig.ReadonlyRootfs}},'
    '"Memory":{{json .HostConfig.Memory}},"MemorySwap":{{json .HostConfig.MemorySwap}},'
    '"NanoCpus":{{json .HostConfig.NanoCpus}},'
    '"PidsLimit":{{json .HostConfig.PidsLimit}},"ShmSize":{{json .HostConfig.ShmSize}},'
    '"Tmpfs":{{json .HostConfig.Tmpfs}},"LogConfig":{{json .HostConfig.LogConfig}},'
    '"RestartPolicy":{{json .HostConfig.RestartPolicy}},'
    '"Privileged":{{json .HostConfig.Privileged}},"PidMode":{{json .HostConfig.PidMode}},'
    '"IpcMode":{{json .HostConfig.IpcMode}},"UTSMode":{{json .HostConfig.UTSMode}},'
    '"Binds":{{json .HostConfig.Binds}},'
    '"PortBindings":{{json .HostConfig.PortBindings}},'
    '"Devices":{{json .HostConfig.Devices}},'
    '"DeviceRequests":{{json .HostConfig.DeviceRequests}}},'
    '"Mounts":{{json .Mounts}}}'
)
_RECOVERY_INSPECT_FORMAT = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},'
    '"Config":{"Labels":{{json .Config.Labels}}}}'
)

_HEALTH_SERVER = """\
const http = require('node:http');
const server = http.createServer((request, response) => {
  if (request.url === '/health') {
    response.writeHead(200, {'content-type': 'text/plain'});
    response.end('ok');
    return;
  }
  response.writeHead(404);
  response.end('not found');
});
server.on('error', (error) => { console.error(error); process.exit(1); });
server.listen(__PORT__, '127.0.0.1');
"""

_NETWORK_GUARD = """\
function hasOnlyLoopbackInterface(interfaceNames) {
  return Array.isArray(interfaceNames)
    && interfaceNames.length === 1
    && interfaceNames[0] === 'lo';
}
function routesUseOnlyLoopback(ipv4Routes, ipv6Routes) {
  if (typeof ipv4Routes !== 'string' || typeof ipv6Routes !== 'string') return false;
  const ipv4Lines = ipv4Routes.split(/\\r?\\n/).filter(line => line.trim());
  if (ipv4Lines.length === 0) return false;
  const ipv4Header = ipv4Lines[0].trim().split(/\\s+/);
  if (ipv4Header[0] !== 'Iface' || ipv4Header[1] !== 'Destination') return false;
  const ipv4IsLoopbackOnly = ipv4Lines.slice(1).every(line => {
    const fields = line.trim().split(/\\s+/);
    return fields.length === 11 && fields[0] === 'lo';
  });
  const ipv6IsLoopbackOnly = ipv6Routes
    .split(/\\r?\\n/)
    .filter(line => line.trim())
    .every(line => {
      const fields = line.trim().split(/\\s+/);
      return fields.length === 10 && fields[9] === 'lo';
    });
  return ipv4IsLoopbackOnly && ipv6IsLoopbackOnly;
}
"""

_VERIFIER = (
    """\
const fs = require('node:fs');
const net = require('node:net');
const sleep = (milliseconds) => new Promise(resolve => setTimeout(resolve, milliseconds));
"""
    + _NETWORK_GUARD
    + """\
async function healthIsReady() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch('http://127.0.0.1:__PORT__/health');
      if (response.status === 200 && await response.text() === 'ok') return true;
    } catch (_) {}
    await sleep(100);
  }
  return false;
}
function confirmNoEgress() {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({host: '1.1.1.1', port: 443});
    let settled = false;
    const blocked = () => { if (!settled) { settled = true; socket.destroy(); resolve(); } };
    socket.once('connect', () => {
      if (!settled) { settled = true; socket.destroy(); reject(new Error('external egress reachable')); }
    });
    socket.once('error', blocked);
    socket.setTimeout(1000, blocked);
  });
}
(async () => {
  if (!hasOnlyLoopbackInterface(fs.readdirSync('/sys/class/net'))) {
    throw new Error('network namespace contains a non-loopback interface');
  }
  if (!routesUseOnlyLoopback(
    fs.readFileSync('/proc/net/route', 'utf8'),
    fs.readFileSync('/proc/net/ipv6_route', 'utf8'),
  )) {
    throw new Error('network namespace contains a non-loopback or malformed route');
  }
  if (!(await healthIsReady())) throw new Error('loopback health check failed');
  await confirmNoEgress();
  console.log(JSON.stringify({
    health: true,
    onlyLoopbackInterfaces: true,
    noNonLoopbackRoutes: true,
    egressBlocked: true,
  }));
})().catch(error => { console.error(error.message); process.exit(1); });
"""
)


@dataclass(frozen=True)
class CommandResult:
    """Result returned by the injected Docker CLI runner."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class DockerCliRunner(Protocol):
    """Narrow runner contract; implementations receive argv, never shell text."""

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> CommandResult:
        ...


class SubprocessDockerCliRunner:
    """Run trusted Docker CLI argv with bounded, non-interactive output."""

    def __init__(
        self,
        *,
        max_output_chars: int = 8192,
        forbidden_roots: Sequence[str | Path] | None = None,
    ) -> None:
        if max_output_chars < 128:
            raise ValueError("max_output_chars must be at least 128")
        self._max_output_chars = max_output_chars
        roots = forbidden_roots if forbidden_roots is not None else (Path.cwd(),)
        self._forbidden_roots = tuple(Path(root).resolve() for root in roots)

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> CommandResult:
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("argv must be a non-empty sequence of strings")
        if any(not isinstance(part, str) or not part for part in argv):
            raise ValueError("argv entries must be non-empty strings")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        resolved_program = shutil.which(argv[0])
        if resolved_program is None:
            return CommandResult(
                127,
                stderr=f"trusted executable not found: {argv[0]}",
            )
        program_path = Path(resolved_program).resolve()
        if any(_is_within(program_path, root) for root in self._forbidden_roots):
            return CommandResult(
                126,
                stderr=f"refused executable beneath an untrusted root: {program_path}",
            )
        execution_argv = [str(program_path), *argv[1:]]
        try:
            completed = subprocess.run(
                execution_argv,
                cwd=str(program_path.parent),
                stdin=subprocess.DEVNULL,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            return CommandResult(127, stderr=self._clip(str(exc)))
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                124,
                self._clip(exc.stdout),
                self._clip(exc.stderr) or "Docker CLI timed out",
            )
        except OSError as exc:
            return CommandResult(126, stderr=self._clip(str(exc)))
        return CommandResult(
            completed.returncode,
            self._clip(completed.stdout),
            self._clip(completed.stderr),
        )

    def _clip(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if len(value) <= self._max_output_chars:
            return value
        marker = "\n...[truncated]"
        return value[: self._max_output_chars - len(marker)] + marker


class _EndpointDockerCliRunner:
    """Pin all daemon operations to the preflight-validated local endpoint."""

    def __init__(self, delegate: DockerCliRunner, docker_binary: str, endpoint: str) -> None:
        self._delegate = delegate
        self._docker_binary = docker_binary
        self._endpoint = endpoint

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> CommandResult:
        if not argv or argv[0] != self._docker_binary:
            raise ValueError("Docker argv does not start with the configured executable")
        pinned_argv = (
            self._docker_binary,
            "--host",
            self._endpoint,
            *argv[1:],
        )
        return self._delegate.run(pinned_argv, timeout_seconds=timeout_seconds)


@dataclass(frozen=True)
class DockerPreflightConfig:
    app_image: str
    verifier_image: str
    allow_pull: bool = False
    docker_binary: str = "docker"
    expected_os: str = "linux"
    expected_architecture: str = "amd64"
    container_user: str = "65534:65534"
    health_port: int = 18765
    memory_limit: str = "128m"
    cpu_limit: str = "0.25"
    pids_limit: int = 64
    tmpfs_size: str = "16m"
    shm_size: str = "16m"
    command_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.app_image.strip() or not self.verifier_image.strip():
            raise ValueError("app_image and verifier_image are required")
        if not self.docker_binary.strip():
            raise ValueError("docker_binary is required")
        if not (1024 <= self.health_port <= 65535):
            raise ValueError("health_port must be an unprivileged TCP port")
        if self.pids_limit <= 0 or self.command_timeout_seconds <= 0:
            raise ValueError("resource and command limits must be positive")
        if not _is_non_root_user(self.container_user):
            raise ValueError("container_user must be an explicit non-root numeric UID:GID")
        _parse_byte_size(self.memory_limit, "memory_limit")
        _parse_byte_size(self.tmpfs_size, "tmpfs_size")
        _parse_byte_size(self.shm_size, "shm_size")
        _nano_cpus(self.cpu_limit)

    @property
    def platform(self) -> str:
        return f"{self.expected_os}/{self.expected_architecture}"


@dataclass(frozen=True)
class ResolvedImage:
    supplied_reference: str
    repository_digest: str
    image_id: str
    os: str
    architecture: str


@dataclass(frozen=True)
class DockerPreflightResult:
    outcome: Outcome
    run_id: str
    docker_context: str | None
    docker_endpoint: str | None
    docker_server_version: str | None
    app_image: ResolvedImage | None
    verifier_image: ResolvedImage | None
    app_program_digest: str
    verifier_program_digest: str
    app_container_id: str | None
    verifier_container_id: str | None
    checks: tuple[str, ...]
    errors: tuple[str, ...]
    cleanup_errors: tuple[str, ...]
    cleanup_attempted: bool
    cleanup_completed: bool
    verifier_output: str | None = None
    schema_version: int = 1

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASSED


class _InfrastructureProblem(RuntimeError):
    pass


class _CreatedContainerProblem(_InfrastructureProblem):
    def __init__(self, message: str, container_id: str) -> None:
        super().__init__(message)
        self.container_id = container_id


class _PolicyProblem(RuntimeError):
    pass


class _UnsupportedProblem(RuntimeError):
    pass


def run_docker_preflight(
    config: DockerPreflightConfig,
    *,
    runner: DockerCliRunner | None = None,
    run_id: str | None = None,
) -> DockerPreflightResult:
    """Exercise the isolated app/verifier topology, then remove it.

    Pulling is opt-in.  Even after an optional pull, containers are created from
    inspected immutable repository digests with ``--pull never``.
    """

    base_cli = runner or SubprocessDockerCliRunner()
    cli: DockerCliRunner = base_cli
    current_run_id = run_id or uuid.uuid4().hex
    if not _RUN_ID.fullmatch(current_run_id):
        raise ValueError("run_id contains unsupported characters or is too long")

    app_image: ResolvedImage | None = None
    verifier_image: ResolvedImage | None = None
    docker_context: str | None = None
    docker_endpoint: str | None = None
    docker_server_version: str | None = None
    app_program = _HEALTH_SERVER.replace("__PORT__", str(config.health_port))
    verifier_program = _VERIFIER.replace("__PORT__", str(config.health_port))
    app_id: str | None = None
    verifier_id: str | None = None
    created: list[tuple[str, str]] = []
    checks: list[str] = []
    primary_errors: list[str] = []
    policy_errors: list[str] = []
    cleanup_errors: list[str] = []
    unsupported = False
    cleanup_attempted = False
    cleanup_completed = False
    verifier_output: str | None = None
    creation_attempted = False

    try:
        docker_context, docker_endpoint = _resolve_local_docker_endpoint(base_cli, config)
        cli = _EndpointDockerCliRunner(base_cli, config.docker_binary, docker_endpoint)
        checks.append("local_docker_endpoint")
        docker_server_version = _assert_daemon_security(cli, config)
        checks.extend(("linux_docker_engine", "daemon_seccomp"))
        app_image = _resolve_image(cli, config, config.app_image)
        verifier_image = _resolve_image(cli, config, config.verifier_image)
        checks.extend(("app_image_pinned", "verifier_image_pinned", "platform_linux_amd64"))

        existing = _list_run_containers(cli, config, current_run_id)
        if existing:
            raise _InfrastructureProblem("run label is already present before preflight")

        creation_attempted = True
        try:
            app_id = _create_container(
                cli,
                config,
                run_id=current_run_id,
                role="app",
                network_mode="none",
                image=app_image.repository_digest,
                inline_program=app_program,
            )
        except _CreatedContainerProblem as exc:
            app_id = exc.container_id
            created.append((app_id, "app"))
            raise
        created.append((app_id, "app"))
        _assert_container_security(
            _inspect_container(cli, config, app_id),
            config,
            current_run_id,
            "app",
            "none",
            app_image,
            app_program,
        )
        checks.append("app_security_config")
        _checked(cli, config, (config.docker_binary, "container", "start", app_id), "start app")

        try:
            verifier_id = _create_container(
                cli,
                config,
                run_id=current_run_id,
                role="verifier",
                network_mode=f"container:{app_id}",
                image=verifier_image.repository_digest,
                inline_program=verifier_program,
            )
        except _CreatedContainerProblem as exc:
            verifier_id = exc.container_id
            created.append((verifier_id, "verifier"))
            raise
        created.append((verifier_id, "verifier"))
        _assert_container_security(
            _inspect_container(cli, config, verifier_id),
            config,
            current_run_id,
            "verifier",
            f"container:{app_id}",
            verifier_image,
            verifier_program,
        )
        checks.append("verifier_security_config")
        _checked(
            cli,
            config,
            (config.docker_binary, "container", "start", verifier_id),
            "start verifier",
        )
        wait_result = _checked(
            cli,
            config,
            (config.docker_binary, "container", "wait", verifier_id),
            "wait for verifier",
        )
        try:
            verifier_exit = int(wait_result.stdout.strip())
        except ValueError as exc:
            raise _InfrastructureProblem("Docker returned an invalid verifier exit code") from exc

        logs = _checked(
            cli,
            config,
            (config.docker_binary, "container", "logs", "--tail", "20", verifier_id),
            "read verifier output",
        )
        verifier_output = logs.stdout.strip()
        if verifier_exit != 0:
            detail = (logs.stderr or logs.stdout).strip()
            raise _InfrastructureProblem(f"trusted verifier exited {verifier_exit}: {detail}")
        _assert_verifier_output(verifier_output)
        checks.extend(
            (
                "loopback_health",
                "only_loopback_interfaces",
                "no_non_loopback_routes",
                "egress_blocked",
            )
        )
    except _PolicyProblem as exc:
        policy_errors.append(str(exc))
    except _UnsupportedProblem as exc:
        primary_errors.append(str(exc))
        unsupported = True
    except _InfrastructureProblem as exc:
        primary_errors.append(str(exc))
    finally:
        if creation_attempted:
            cleanup_attempted = True
            cleanup_errors.extend(_cleanup(cli, config, current_run_id, created))
            cleanup_completed = not cleanup_errors
            if cleanup_completed:
                checks.append("cleanup_complete")

    if cleanup_errors:
        outcome = Outcome.CLEANUP_FAILED
    elif policy_errors:
        outcome = Outcome.POLICY_BLOCKED
    elif unsupported:
        outcome = Outcome.UNSUPPORTED
    elif primary_errors:
        outcome = Outcome.INFRASTRUCTURE_ERROR
    else:
        outcome = Outcome.PASSED

    return DockerPreflightResult(
        outcome=outcome,
        run_id=current_run_id,
        docker_context=docker_context,
        docker_endpoint=docker_endpoint,
        docker_server_version=docker_server_version,
        app_image=app_image,
        verifier_image=verifier_image,
        app_program_digest=_sha256(app_program),
        verifier_program_digest=_sha256(verifier_program),
        app_container_id=app_id,
        verifier_container_id=verifier_id,
        checks=tuple(checks),
        errors=tuple(policy_errors or primary_errors),
        cleanup_errors=tuple(cleanup_errors),
        cleanup_attempted=cleanup_attempted,
        cleanup_completed=cleanup_completed,
        verifier_output=verifier_output,
    )


def _resolve_local_docker_endpoint(
    runner: DockerCliRunner, config: DockerPreflightConfig
) -> tuple[str, str]:
    context_result = _checked(
        runner,
        config,
        (config.docker_binary, "context", "show"),
        "read active Docker context",
    )
    context = context_result.stdout.strip()
    if not _CONTEXT_NAME.fullmatch(context):
        raise _InfrastructureProblem("Docker returned an invalid active context name")

    endpoint_result = _checked(
        runner,
        config,
        (
            config.docker_binary,
            "context",
            "inspect",
            "--format",
            "{{json .Endpoints.docker.Host}}",
            context,
        ),
        f"inspect Docker context {context}",
    )
    endpoint = _parse_json(endpoint_result.stdout, "Docker context endpoint")
    if not isinstance(endpoint, str) or not _is_local_docker_endpoint(endpoint):
        raise _PolicyProblem(
            "active Docker context does not use an approved local unix socket or named pipe"
        )
    return context, endpoint


def _assert_daemon_security(
    runner: DockerCliRunner, config: DockerPreflightConfig
) -> str:
    result = _checked(
        runner,
        config,
        (config.docker_binary, "info", "--format", _DAEMON_INFO_FORMAT),
        "inspect Docker daemon security",
    )
    value = _parse_json(result.stdout, "Docker daemon security information")
    if not isinstance(value, dict):
        raise _InfrastructureProblem("Docker daemon security information was not an object")
    os_type = value.get("OSType")
    if os_type != config.expected_os:
        raise _UnsupportedProblem(
            f"Docker daemon is {os_type!r}, expected {config.expected_os!r}"
        )
    security_options = value.get("SecurityOptions")
    if not isinstance(security_options, list) or not any(
        isinstance(option, str)
        and option.lower().startswith("name=seccomp")
        and "unconfined" not in option.lower()
        for option in security_options
    ):
        raise _UnsupportedProblem("Docker daemon does not advertise an enabled seccomp profile")
    server_version = value.get("ServerVersion")
    if not isinstance(server_version, str) or not server_version.strip():
        raise _InfrastructureProblem("Docker daemon did not report its server version")
    return server_version.strip()


def _resolve_image(
    runner: DockerCliRunner, config: DockerPreflightConfig, reference: str
) -> ResolvedImage:
    if config.allow_pull:
        _checked(
            runner,
            config,
            (
                config.docker_binary,
                "image",
                "pull",
                "--platform",
                config.platform,
                reference,
            ),
            f"pull image {reference}",
        )

    result = _checked(
        runner,
        config,
        (
            config.docker_binary,
            "image",
            "inspect",
            "--format",
            _IMAGE_INSPECT_FORMAT,
            reference,
        ),
        f"inspect image {reference}",
    )
    value = _parse_json(result.stdout, f"image inspection for {reference}")
    if not isinstance(value, dict):
        raise _InfrastructureProblem(f"image inspection for {reference} was not an object")
    image = value
    os_name = image.get("Os")
    architecture = image.get("Architecture")
    if os_name != config.expected_os or architecture != config.expected_architecture:
        raise _InfrastructureProblem(
            f"image {reference} is {os_name}/{architecture}, expected {config.platform}"
        )
    digests = image.get("RepoDigests")
    if not isinstance(digests, list):
        digests = []
    candidate_digests = sorted(
        digest for digest in digests if isinstance(digest, str) and _REPO_DIGEST.fullmatch(digest)
    )
    repository = _repository_name(reference)
    valid_digests = [
        digest for digest in candidate_digests if _repository_name(digest) == repository
    ]
    if not valid_digests:
        raise _InfrastructureProblem(f"image {reference} has no immutable repository digest")
    if "@" in reference and reference not in valid_digests:
        raise _InfrastructureProblem(
            "image inspection did not return the supplied repository digest"
        )
    supplied_digest = reference if "@" in reference else valid_digests[0]
    image_id = image.get("Id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise _InfrastructureProblem(f"image {reference} has no valid image ID")
    return ResolvedImage(reference, supplied_digest, image_id, os_name, architecture)


def _create_container(
    runner: DockerCliRunner,
    config: DockerPreflightConfig,
    *,
    run_id: str,
    role: str,
    network_mode: str,
    image: str,
    inline_program: str,
) -> str:
    name = f"firstrun-m0-{run_id[:24]}-{role}"
    argv = (
        config.docker_binary,
        "container",
        "create",
        "--name",
        name,
        "--label",
        f"{RUN_LABEL}={run_id}",
        "--label",
        f"{ROLE_LABEL}={role}",
        "--platform",
        config.platform,
        "--pull",
        "never",
        "--network",
        network_mode,
        "--user",
        config.container_user,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--security-opt",
        "seccomp=builtin",
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={config.tmpfs_size}",
        "--memory",
        config.memory_limit,
        "--memory-swap",
        config.memory_limit,
        "--cpus",
        config.cpu_limit,
        "--pids-limit",
        str(config.pids_limit),
        "--shm-size",
        config.shm_size,
        "--stop-timeout",
        "2",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=1",
        "--entrypoint",
        "node",
        image,
        "-e",
        inline_program,
    )
    result = runner.run(argv, timeout_seconds=config.command_timeout_seconds)
    container_id = result.stdout.strip()
    if result.returncode == 0 and _CONTAINER_ID.fullmatch(container_id):
        return container_id

    if result.returncode == 0:
        creation_error = f"Docker returned an invalid {role} container ID"
    else:
        creation_error = _command_error(f"create {role} container", result)
    recovered_id = _recover_created_container(
        runner,
        config,
        run_id=run_id,
        role=role,
        name=name,
    )
    if recovered_id is not None:
        raise _CreatedContainerProblem(
            f"{creation_error}; recovered the owned container for cleanup",
            recovered_id,
        )
    raise _InfrastructureProblem(creation_error)


def _recover_created_container(
    runner: DockerCliRunner,
    config: DockerPreflightConfig,
    *,
    run_id: str,
    role: str,
    name: str,
) -> str | None:
    result = runner.run(
        (
            config.docker_binary,
            "container",
            "inspect",
            "--format",
            _RECOVERY_INSPECT_FORMAT,
            name,
        ),
        timeout_seconds=config.command_timeout_seconds,
    )
    if result.returncode != 0:
        return None
    try:
        inspection = _parse_json(result.stdout, f"container recovery inspection for {name}")
    except _InfrastructureProblem:
        return None
    if not isinstance(inspection, dict):
        return None
    container_id = inspection.get("Id")
    container_name = inspection.get("Name")
    container_config = inspection.get("Config")
    if not isinstance(container_config, dict):
        return None
    labels = container_config.get("Labels")
    if not isinstance(labels, dict):
        return None
    if (
        not isinstance(container_id, str)
        or not _CONTAINER_ID.fullmatch(container_id)
        or container_name not in (name, f"/{name}")
        or labels.get(RUN_LABEL) != run_id
        or labels.get(ROLE_LABEL) != role
    ):
        return None
    return container_id


def _inspect_container(
    runner: DockerCliRunner, config: DockerPreflightConfig, container_id: str
) -> dict[str, Any]:
    result = _checked(
        runner,
        config,
        (
            config.docker_binary,
            "container",
            "inspect",
            "--format",
            _CONTAINER_INSPECT_FORMAT,
            container_id,
        ),
        f"inspect container {container_id}",
    )
    value = _parse_json(result.stdout, f"container inspection for {container_id}")
    if not isinstance(value, dict):
        raise _InfrastructureProblem(f"container inspection for {container_id} was not an object")
    return value


def _assert_container_security(
    inspection: dict[str, Any],
    config: DockerPreflightConfig,
    run_id: str,
    role: str,
    network_mode: str,
    image: ResolvedImage,
    inline_program: str,
) -> None:
    container_config = inspection.get("Config")
    host = inspection.get("HostConfig")
    if not isinstance(container_config, dict) or not isinstance(host, dict):
        raise _InfrastructureProblem(f"{role} inspection omitted security configuration")
    labels = container_config.get("Labels") or {}
    failures: list[str] = []
    if labels.get(RUN_LABEL) != run_id or labels.get(ROLE_LABEL) != role:
        failures.append("ownership labels")
    if (
        inspection.get("Image") != image.image_id
        or container_config.get("Image") != image.repository_digest
    ):
        failures.append("pinned image identity")
    if container_config.get("Entrypoint") != ["node"] or container_config.get("Cmd") != [
        "-e",
        inline_program,
    ]:
        failures.append("trusted inline command")
    if container_config.get("User") != config.container_user or not _is_non_root_user(
        str(container_config.get("User", ""))
    ):
        failures.append("non-root user")
    if host.get("NetworkMode") != network_mode:
        failures.append("network namespace")
    cap_drop = {str(value).upper() for value in (host.get("CapDrop") or [])}
    if "ALL" not in cap_drop or host.get("CapAdd"):
        failures.append("capability drop")
    security_options = {str(value).lower() for value in (host.get("SecurityOpt") or [])}
    if not any(
        value.startswith("no-new-privileges") and not value.endswith("=false")
        for value in security_options
    ):
        failures.append("no-new-privileges")
    if "seccomp=builtin" not in security_options:
        failures.append("built-in seccomp")
    if host.get("ReadonlyRootfs") is not True:
        failures.append("read-only root filesystem")
    expected_memory = _parse_byte_size(config.memory_limit, "memory_limit")
    if host.get("Memory") != expected_memory or host.get("MemorySwap") != expected_memory:
        failures.append("memory limit")
    if host.get("NanoCpus") != _nano_cpus(config.cpu_limit):
        failures.append("CPU limit")
    if host.get("PidsLimit") != config.pids_limit:
        failures.append("PID limit")
    if host.get("ShmSize") != _parse_byte_size(config.shm_size, "shm_size"):
        failures.append("shared-memory limit")
    if not _valid_tmpfs(host.get("Tmpfs"), config.tmpfs_size):
        failures.append("bounded tmpfs")
    if host.get("LogConfig") != {
        "Type": "json-file",
        "Config": {"max-size": "1m", "max-file": "1"},
    }:
        failures.append("bounded logs")
    restart_policy = host.get("RestartPolicy") or {}
    if (
        not isinstance(restart_policy, dict)
        or restart_policy.get("Name") not in (None, "", "no")
    ):
        failures.append("no restart policy")
    if host.get("Privileged") is True:
        failures.append("unprivileged mode")
    if host.get("PidMode") not in (None, ""):
        failures.append("separate PID namespace")
    ipc_mode = str(host.get("IpcMode") or "").lower()
    uts_mode = str(host.get("UTSMode") or "").lower()
    if ipc_mode not in {"", "private"} or uts_mode not in {""}:
        failures.append("separate IPC/UTS namespaces")
    if host.get("Binds") or not _valid_mounts(inspection.get("Mounts")):
        failures.append("no shared mounts")
    if host.get("PortBindings"):
        failures.append("no host port bindings")
    if host.get("Devices") or host.get("DeviceRequests"):
        failures.append("no host devices")
    if failures:
        raise _InfrastructureProblem(f"{role} security inspection failed: {', '.join(failures)}")


def _assert_verifier_output(output: str) -> None:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise _InfrastructureProblem("trusted verifier produced no result")
    value = _parse_json(lines[-1], "trusted verifier output")
    expected = {
        "health": True,
        "onlyLoopbackInterfaces": True,
        "noNonLoopbackRoutes": True,
        "egressBlocked": True,
    }
    if value != expected:
        raise _InfrastructureProblem("trusted verifier did not confirm all network checks")


def _cleanup(
    runner: DockerCliRunner,
    config: DockerPreflightConfig,
    run_id: str,
    created: Sequence[tuple[str, str]],
) -> list[str]:
    errors: list[str] = []
    for container_id, role in reversed(created):
        try:
            inspection = _inspect_container(runner, config, container_id)
        except _InfrastructureProblem as exc:
            errors.append(f"could not recheck {container_id} before cleanup: {exc}")
            continue
        labels = (inspection.get("Config") or {}).get("Labels") or {}
        if labels.get(RUN_LABEL) != run_id or labels.get(ROLE_LABEL) != role:
            errors.append(f"refused to remove {container_id}: ownership label changed")
            continue
        result = runner.run(
            (
                config.docker_binary,
                "container",
                "rm",
                "--force",
                "--volumes",
                container_id,
            ),
            timeout_seconds=config.command_timeout_seconds,
        )
        if result.returncode != 0:
            errors.append(_command_error(f"remove container {container_id}", result))

    try:
        residue = _list_run_containers(runner, config, run_id)
    except _InfrastructureProblem as exc:
        errors.append(f"could not verify cleanup: {exc}")
    else:
        if residue:
            errors.append(f"run-labelled container residue remains: {', '.join(residue)}")
    return errors


def _list_run_containers(
    runner: DockerCliRunner, config: DockerPreflightConfig, run_id: str
) -> tuple[str, ...]:
    result = _checked(
        runner,
        config,
        (
            config.docker_binary,
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            f"label={RUN_LABEL}={run_id}",
        ),
        "list run-labelled containers",
    )
    container_ids = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    if any(not _CONTAINER_ID.fullmatch(container_id) for container_id in container_ids):
        raise _InfrastructureProblem("Docker returned an invalid labelled container ID")
    return container_ids


def _checked(
    runner: DockerCliRunner,
    config: DockerPreflightConfig,
    argv: Sequence[str],
    action: str,
) -> CommandResult:
    result = runner.run(tuple(argv), timeout_seconds=config.command_timeout_seconds)
    if result.returncode != 0:
        raise _InfrastructureProblem(_command_error(action, result))
    return result


def _command_error(action: str, result: CommandResult) -> str:
    detail = (result.stderr or result.stdout).strip()
    suffix = f": {detail}" if detail else ""
    return f"{action} failed with exit {result.returncode}{suffix}"


def _parse_json(raw: str, description: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _InfrastructureProblem(f"{description} was not valid JSON") from exc


def _is_non_root_user(user: str) -> bool:
    return re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", user.strip()) is not None


def _parse_byte_size(value: str, field_name: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([kmgt]?)", value.strip().lower())
    if match is None:
        raise ValueError(
            f"{field_name} must be a positive integer with optional k/m/g/t suffix"
        )
    multipliers = {
        "": 1,
        "k": 1024,
        "m": 1024**2,
        "g": 1024**3,
        "t": 1024**4,
    }
    return int(match.group(1)) * multipliers[match.group(2)]


def _nano_cpus(value: str) -> int:
    try:
        cpus = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("cpu_limit must be a positive decimal number") from exc
    nanocpus = cpus * 1_000_000_000
    if not cpus.is_finite() or cpus <= 0 or nanocpus != nanocpus.to_integral_value():
        raise ValueError(
            "cpu_limit must resolve to a positive whole number of NanoCPUs"
        )
    return int(nanocpus)


def _valid_tmpfs(value: Any, expected_size: str) -> bool:
    if not isinstance(value, dict) or set(value) != {"/tmp"}:
        return False
    raw_options = value.get("/tmp")
    if not isinstance(raw_options, str):
        return False
    options = {
        option.strip().lower()
        for option in raw_options.split(",")
        if option.strip()
    }
    if not {"rw", "noexec", "nosuid", "nodev"}.issubset(options):
        return False
    size_options = [
        option.split("=", 1)[1]
        for option in options
        if option.startswith("size=")
    ]
    if len(size_options) != 1:
        return False
    try:
        actual_size = _parse_byte_size(size_options[0], "tmpfs size")
        configured_size = _parse_byte_size(expected_size, "tmpfs_size")
    except ValueError:
        return False
    return actual_size == configured_size


def _valid_mounts(value: Any) -> bool:
    if value in (None, []):
        return True
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return False
    mount = value[0]
    return (
        str(mount.get("Type", "")).lower() == "tmpfs"
        and mount.get("Destination") == "/tmp"
        and mount.get("RW") is True
    )


def _repository_name(reference: str) -> str:
    without_digest = reference.split("@", 1)[0]
    slash = without_digest.rfind("/")
    colon = without_digest.rfind(":")
    return without_digest[:colon] if colon > slash else without_digest


def _is_local_docker_endpoint(endpoint: str) -> bool:
    if re.fullmatch(r"unix:///[^\\\r\n]+", endpoint):
        return True
    return re.fullmatch(r"npipe:////\./pipe/[A-Za-z0-9_.-]+", endpoint) is not None


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "CommandResult",
    "DockerCliRunner",
    "DockerPreflightConfig",
    "DockerPreflightResult",
    "ResolvedImage",
    "SubprocessDockerCliRunner",
    "run_docker_preflight",
]
