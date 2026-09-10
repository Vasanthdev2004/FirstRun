"""Docker implementation of the narrow, trusted M1 sandbox worker.

Only the Docker executable runs on the host.  Repository commands are exact argv
inside a non-root, networkless container populated from a trusted Git archive.
The functional verifier is controller-owned and runs in a second container that
shares only the app container's loopback network namespace.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from firstrun.domain.contracts import Recipe, RecipeStep, Target
from firstrun.domain.evidence import (
    AcceptanceProbeEvidence,
    AttemptEvidence,
    CleanupEvidence,
    CommandEvidence,
    ContentDigest,
    ControllerBinding,
    ControllerObservation,
    FreshStateEvidence,
    ReadinessEvidence,
    RunEvidence,
    RuntimeImageEvidence,
    build_run_evidence,
)
from firstrun.domain.outcomes import Outcome
from firstrun.preflight.docker import (
    CommandResult,
    DockerCliRunner,
    DockerPreflightConfig,
    ResolvedImage,
    SubprocessDockerCliRunner,
    _EndpointDockerCliRunner,
    _InfrastructureProblem,
    _PolicyProblem,
    _UnsupportedProblem,
    _assert_daemon_security,
    _parse_byte_size,
    _resolve_image,
    _resolve_local_docker_endpoint,
)
from firstrun.verification.policy import (
    CONTROLLED_NODE_FIXTURE_POLICY,
    CONTROLLED_NODE_FIXTURE_POLICY_DIGEST,
    classify_target_tuple,
)
from firstrun.verification.readme import check_block_bytes, replace_block_bytes
from firstrun.verification.source import (
    CandidatePatch,
    SourcePolicyError,
    SourceSnapshot,
    build_candidate_patch,
    file_digests,
)


OWNER_LABEL = "firstrun.owner"
RUN_LABEL = "firstrun.run_id"
ATTEMPT_LABEL = "firstrun.attempt_id"
ROLE_LABEL = "firstrun.role"
OWNER_VALUE = "m1-controlled-local"
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_CONTROLLER_ERROR = 4096
_MAX_COMMAND_TAIL = 8192
_FRESHNESS_MARKER = "/workspace/.firstrun-m1-freshness-marker"
_QUARANTINED_ENDPOINTS: set[str] = set()
_QUARANTINE_LOCK = threading.Lock()

_INSPECT_FORMAT = (
    '{"Id":{{json .Id}},"Image":{{json .Image}},"Config":{'
    '"User":{{json .Config.User}},"Labels":{{json .Config.Labels}},'
    '"Image":{{json .Config.Image}},"Entrypoint":{{json .Config.Entrypoint}},'
    '"Cmd":{{json .Config.Cmd}},"Env":{{json .Config.Env}},'
    '"Volumes":{{json .Config.Volumes}},'
    '"WorkingDir":{{json .Config.WorkingDir}}},"HostConfig":{'
    '"NetworkMode":{{json .HostConfig.NetworkMode}},'
    '"CapDrop":{{json .HostConfig.CapDrop}},"CapAdd":{{json .HostConfig.CapAdd}},'
    '"SecurityOpt":{{json .HostConfig.SecurityOpt}},'
    '"ReadonlyRootfs":{{json .HostConfig.ReadonlyRootfs}},'
    '"Memory":{{json .HostConfig.Memory}},"MemorySwap":{{json .HostConfig.MemorySwap}},'
    '"NanoCpus":{{json .HostConfig.NanoCpus}},'
    '"PidsLimit":{{json .HostConfig.PidsLimit}},"ShmSize":{{json .HostConfig.ShmSize}},'
    '"Tmpfs":{{json .HostConfig.Tmpfs}},"Binds":{{json .HostConfig.Binds}},'
    '"Privileged":{{json .HostConfig.Privileged}},'
    '"PidMode":{{json .HostConfig.PidMode}},"IpcMode":{{json .HostConfig.IpcMode}},'
    '"PortBindings":{{json .HostConfig.PortBindings}},'
    '"Devices":{{json .HostConfig.Devices}},'
    '"DeviceRequests":{{json .HostConfig.DeviceRequests}},'
    '"RestartPolicy":{{json .HostConfig.RestartPolicy}},'
    '"LogConfig":{{json .HostConfig.LogConfig}}},"Mounts":{{json .Mounts}}}'
)
_RECOVERY_FORMAT = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},'
    '"Config":{"Labels":{{json .Config.Labels}}}}'
)

_IDLE_SUPERVISOR = """\
const hold = setInterval(() => {}, 60_000);
function stop() { clearInterval(hold); process.exit(0); }
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
"""

_WRITE_WORKSPACE = """\
const fs = require('node:fs');
const path = require('node:path');
const files = JSON.parse(Buffer.from(process.argv[1], 'base64').toString('utf8'));
const root = '/workspace';
for (const [name, encoded] of Object.entries(files)) {
  if (typeof name !== 'string' || name.startsWith('/') || name.includes('\\\\') ||
      name.split('/').some(part => !part || part === '.' || part === '..') ||
      typeof encoded !== 'string') throw new Error('invalid controller source entry');
  const destination = path.join(root, ...name.split('/'));
  fs.mkdirSync(path.dirname(destination), {recursive: true, mode: 0o755});
  const content = Buffer.from(encoded, 'base64');
  fs.writeFileSync(destination, content, {mode: 0o644, flag: 'wx'});
}
console.log(JSON.stringify({writtenFiles: Object.keys(files).length}));
"""

_SEED_FRESH_WORKSPACE = """\
const fs = require('node:fs');
const crypto = require('node:crypto');
const marker = process.argv[1];
const content = Buffer.from(process.argv[2], 'utf8');
if (fs.existsSync(marker)) {
  console.log(JSON.stringify({preexistingAbsent: false, markerDigest: null}));
  process.exitCode = 10;
} else {
  fs.writeFileSync(marker, content, {mode: 0o600, flag: 'wx'});
  const digest = 'sha256:' + crypto.createHash('sha256').update(content).digest('hex');
  console.log(JSON.stringify({preexistingAbsent: true, markerDigest: digest}));
}
"""

_RUN_COMMAND = """\
const {spawn} = require('node:child_process');
const spec = JSON.parse(Buffer.from(process.argv[1], 'base64').toString('utf8'));
const root = '/workspace';
const cwd = spec.cwd === '.' ? root : root + '/' + spec.cwd;
const limit = 8192;
let stdout = Buffer.alloc(0), stderr = Buffer.alloc(0), overflow = false, settled = false;
function collect(which, chunk) {
  if (!Buffer.isBuffer(chunk)) chunk = Buffer.from(chunk);
  const current = which === 'stdout' ? stdout : stderr;
  if (current.length < limit) {
    const next = Buffer.concat([current, chunk.subarray(0, limit - current.length)]);
    if (which === 'stdout') stdout = next; else stderr = next;
  }
  if (current.length + chunk.length > limit) overflow = true;
}
function emit(value) { if (!settled) { settled = true; console.log(JSON.stringify(value)); } }
let child;
try {
  child = spawn(spec.argv[0], spec.argv.slice(1), {
    cwd, shell: false, stdio: ['ignore', 'pipe', 'pipe'],
    env: {...process.env, HOME: '/tmp', npm_config_cache: '/tmp/npm-cache',
      npm_config_update_notifier: 'false', npm_config_audit: 'false'}
  });
} catch (error) {
  emit({exitCode: null, signal: null, stdout: '', stderr: String(error.message), overflow: false});
  process.exitCode = 70;
}
if (child) {
  child.stdout.on('data', value => collect('stdout', value));
  child.stderr.on('data', value => collect('stderr', value));
  child.on('error', error => {
    emit({exitCode: null, signal: null, stdout: stdout.toString('utf8'),
      stderr: String(error.message), overflow});
    process.exitCode = 70;
  });
  child.on('close', (code, signal) => emit({exitCode: code, signal,
    stdout: stdout.toString('utf8'), stderr: stderr.toString('utf8'), overflow}));
}
"""

_START_COMMAND = """\
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const spec = JSON.parse(Buffer.from(process.argv[1], 'base64').toString('utf8'));
const cwd = spec.cwd === '.' ? '/workspace' : '/workspace/' + spec.cwd;
const resultPath = '/tmp/firstrun-start-result.json';
const logPath = '/tmp/firstrun-app.log';
const limit = 65536;
let written = 0;
function log(chunk) {
  if (!Buffer.isBuffer(chunk)) chunk = Buffer.from(chunk);
  if (written >= limit) return;
  const selected = chunk.subarray(0, limit - written);
  fs.appendFileSync(logPath, selected);
  written += selected.length;
}
const child = spawn(spec.argv[0], spec.argv.slice(1), {
  cwd, shell: false, stdio: ['ignore', 'pipe', 'pipe'],
  env: {...process.env, PORT: String(spec.port), HOME: '/tmp',
    npm_config_cache: '/tmp/npm-cache', npm_config_update_notifier: 'false',
    npm_config_audit: 'false'}
});
child.stdout.on('data', log);
child.stderr.on('data', log);
child.on('error', error => {
  fs.writeFileSync(resultPath, JSON.stringify({exitCode: null, error: String(error.message)}));
  process.exit(70);
});
child.on('close', (code, signal) => {
  fs.writeFileSync(resultPath, JSON.stringify({exitCode: code, signal}));
  process.exit(code === 0 ? 0 : 1);
});
function stop() { child.kill('SIGTERM'); }
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
"""

_READ_START_RESULT = """\
const fs = require('node:fs');
const wait = Number(process.argv[1]);
const resultPath = '/tmp/firstrun-start-result.json';
function inspect() {
  if (!fs.existsSync(resultPath)) {
    console.log('{"exists":false}');
    return;
  }
  try {
    const value = JSON.parse(fs.readFileSync(resultPath, 'utf8'));
    const validExit = value.exitCode === null || Number.isInteger(value.exitCode);
    const validSignal = value.signal === undefined || value.signal === null || typeof value.signal === 'string';
    const validError = value.error === undefined || typeof value.error === 'string';
    if (!validExit || !validSignal || !validError) throw new Error('invalid fields');
    console.log(JSON.stringify({exists: true, exitCode: value.exitCode,
      signal: value.signal ?? null, error: value.error ?? null}));
  } catch (_) {
    console.log('{"exists":true,"malformed":true}');
  }
}
setTimeout(inspect, Number.isFinite(wait) && wait >= 0 && wait <= 1000 ? wait : 0);
"""

_HASH_WORKSPACE = """\
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const names = JSON.parse(Buffer.from(process.argv[1], 'base64').toString('utf8'));
const root = '/workspace';
const output = {};
for (const name of names) {
  let current = root;
  for (const part of name.split('/')) {
    current = path.join(current, part);
    const info = fs.lstatSync(current);
    if (info.isSymbolicLink()) throw new Error('workspace path became a symbolic link');
  }
  const info = fs.lstatSync(current);
  if (!info.isFile()) throw new Error('tracked workspace path is not a regular file');
  output[name] = 'sha256:' + crypto.createHash('sha256').update(fs.readFileSync(current)).digest('hex');
}
console.log(JSON.stringify(output));
"""

_NETWORK_GUARD = """\
function onlyLoopback() {
  const fs = require('node:fs');
  const interfaces = fs.readdirSync('/sys/class/net');
  if (interfaces.length !== 1 || interfaces[0] !== 'lo') return false;
  const ipv4 = fs.readFileSync('/proc/net/route', 'utf8').split(/\\r?\\n/).filter(Boolean);
  if (!ipv4.length || ipv4[0].trim().split(/\\s+/)[0] !== 'Iface') return false;
  if (!ipv4.slice(1).every(line => line.trim().split(/\\s+/)[0] === 'lo')) return false;
  const ipv6 = fs.readFileSync('/proc/net/ipv6_route', 'utf8').split(/\\r?\\n/).filter(Boolean);
  return ipv6.every(line => line.trim().split(/\\s+/)[9] === 'lo');
}
async function egressIsBlocked() {
  const net = require('node:net');
  return await new Promise(resolve => {
    const socket = net.createConnection({host: '1.1.1.1', port: 443});
    let done = false;
    const finish = value => { if (!done) { done = true; socket.destroy(); resolve(value); } };
    socket.once('connect', () => finish(false));
    socket.once('error', () => finish(true));
    socket.setTimeout(750, () => finish(true));
  });
}
"""

TRUSTED_VERIFIER_V1 = (
    """\
const crypto = require('node:crypto');
const spec = JSON.parse(Buffer.from(process.argv[1], 'base64').toString('utf8'));
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
"""
    + _NETWORK_GUARD
    + """\
function emit(value, code) {
  console.log(JSON.stringify({schemaVersion: 1, runId: spec.runId,
    attemptId: spec.attemptId, verifierId: 'notes-create-read-v1', ...value}));
  process.exitCode = code;
}
async function boundedJson(response) {
  const text = await response.text();
  if (Buffer.byteLength(text) > 262144) throw new Error('oversized response');
  const value = JSON.parse(text);
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('expected JSON object');
  return value;
}
(async () => {
  if (!onlyLoopback() || !(await egressIsBlocked())) {
    emit({outcome: 'infrastructure_error', readinessAttempts: 0,
      observedStatus: null, passedChecks: [], nonceDigest: null,
      error: 'network namespace policy was not enforced'}, 12);
    return;
  }
  const readinessDeadline = Date.now() + spec.readinessTimeout * 1000;
  let attempts = 0, observedStatus = null;
  while (Date.now() < readinessDeadline) {
    attempts += 1;
    try {
      const response = await fetch('http://127.0.0.1:' + spec.port + spec.readinessPath,
        {signal: AbortSignal.timeout(1000)});
      observedStatus = response.status;
      await response.arrayBuffer();
      if (observedStatus === spec.readinessStatus) break;
    } catch (_) {}
    await sleep(100);
  }
  if (observedStatus !== spec.readinessStatus) {
    emit({outcome: 'timed_out', readinessAttempts: attempts, observedStatus,
      passedChecks: [], nonceDigest: null, error: 'readiness deadline expired'}, 11);
    return;
  }
  const nonce = 'firstrun-' + crypto.randomUUID();
  const nonceDigest = 'sha256:' + crypto.createHash('sha256').update(nonce).digest('hex');
  try {
    const signal = AbortSignal.timeout(spec.acceptanceTimeout * 1000);
    const createdResponse = await fetch('http://127.0.0.1:' + spec.port + '/notes', {
      method: 'POST', signal, headers: {'content-type': 'application/json'},
      body: JSON.stringify({message: nonce})
    });
    const created = await boundedJson(createdResponse);
    if (createdResponse.status !== 201 || !Number.isInteger(created.id) || created.id <= 0) {
      throw new Error('create_note failed with HTTP ' + createdResponse.status);
    }
    const fetchedResponse = await fetch('http://127.0.0.1:' + spec.port + '/notes/' + created.id,
      {signal});
    const fetched = await boundedJson(fetchedResponse);
    if (fetchedResponse.status !== 200 || fetched.id !== created.id || fetched.message !== nonce) {
      throw new Error('read_back_same_note did not match');
    }
    emit({outcome: 'passed', readinessAttempts: attempts, observedStatus,
      passedChecks: ['create_note', 'read_back_same_note'], nonceDigest, error: null}, 0);
  } catch (error) {
    const timedOut = error && (error.name === 'TimeoutError' || error.name === 'AbortError');
    emit({outcome: timedOut ? 'timed_out' : 'failed', readinessAttempts: attempts,
      observedStatus, passedChecks: [], nonceDigest, error: String(error.message).slice(0, 1000)},
      timedOut ? 11 : 10);
  }
})().catch(error => emit({outcome: 'infrastructure_error', readinessAttempts: 0,
  observedStatus: null, passedChecks: [], nonceDigest: null,
  error: String(error.message).slice(0, 1000)}, 12));
"""
)
TRUSTED_VERIFIER_DIGEST = ContentDigest.from_bytes(TRUSTED_VERIFIER_V1.encode("utf-8"))


class WorkerProblem(RuntimeError):
    """A classified failure at the trusted worker boundary."""

    def __init__(self, outcome: Outcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


class _DeadlineDockerCliRunner:
    """Cap every phase operation to one controller-owned monotonic deadline."""

    def __init__(
        self,
        delegate: DockerCliRunner,
        deadline: float,
        *,
        clock: Any = time.monotonic,
        budget_name: Literal["phase", "cleanup"] = "phase",
    ) -> None:
        self._delegate = delegate
        self._deadline = deadline
        self._clock = clock
        self._budget_name = budget_name

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> CommandResult:
        remaining = self._deadline - float(self._clock())
        if remaining <= 0:
            raise WorkerProblem(
                Outcome.TIMED_OUT,
                f"M1 {self._budget_name} wall-time budget expired",
            )
        effective_timeout = min(float(timeout_seconds), remaining)
        result = self._delegate.run(argv, timeout_seconds=effective_timeout)
        if float(self._clock()) > self._deadline:
            raise WorkerProblem(
                Outcome.TIMED_OUT,
                f"M1 {self._budget_name} wall-time budget expired",
            )
        if result.returncode == 124 and effective_timeout < float(timeout_seconds):
            raise WorkerProblem(
                Outcome.TIMED_OUT,
                f"M1 {self._budget_name} wall-time budget expired",
            )
        return result


@dataclass(frozen=True)
class PreparedDockerRuntime:
    docker_context: str
    docker_endpoint: str
    docker_server_version: str
    image: ResolvedImage
    runner: DockerCliRunner
    config: DockerPreflightConfig


@dataclass(frozen=True)
class DockerPhaseResult:
    outcome: Outcome
    binding: ControllerBinding
    evidence: RunEvidence | None
    error: str | None = None
    attempt: AttemptEvidence | None = None


def prepare_docker_runtime(
    target: Target,
    *,
    controller_repository_root: Path,
    runner: DockerCliRunner | None = None,
) -> PreparedDockerRuntime:
    """Resolve and inspect the one runtime once for a baseline/proof case."""

    classification = classify_target_tuple(
        image_reference=target.runtime.image_ref,
        platform=target.runtime.platform,
        verifier_id=target.acceptance.verifier_id,
    )
    if classification is Outcome.UNSUPPORTED:
        raise WorkerProblem(Outcome.UNSUPPORTED, "target is not registered by the M1 policy")

    policy = CONTROLLED_NODE_FIXTURE_POLICY
    os_name, architecture = target.runtime.platform.split("/", 1)
    config = DockerPreflightConfig(
        app_image=target.runtime.image_ref,
        verifier_image=target.runtime.image_ref,
        allow_pull=False,
        expected_os=os_name,
        expected_architecture=architecture,
        container_user=policy.docker.user,
        memory_limit=str(policy.docker.memory_bytes),
        cpu_limit=str(policy.docker.nano_cpus / 1_000_000_000),
        pids_limit=policy.docker.pids,
        tmpfs_size=str(policy.docker.tmp_tmpfs_bytes),
        shm_size=str(policy.docker.shm_bytes),
        command_timeout_seconds=30,
    )
    try:
        trusted_root = controller_repository_root.resolve(strict=True)
        base_runner = runner or SubprocessDockerCliRunner(
            max_output_chars=policy.docker.max_output_bytes,
            forbidden_roots=(Path.cwd().resolve(), trusted_root),
        )
        context, endpoint = _resolve_local_docker_endpoint(base_runner, config)
        if _endpoint_is_quarantined(endpoint):
            raise WorkerProblem(
                Outcome.CLEANUP_FAILED,
                "M1 worker endpoint is quarantined after an earlier cleanup failure",
            )
        pinned = _EndpointDockerCliRunner(base_runner, config.docker_binary, endpoint)
        server_version = _assert_daemon_security(pinned, config)
        image = _resolve_image(pinned, config, target.runtime.image_ref)
    except _PolicyProblem as exc:
        raise WorkerProblem(Outcome.POLICY_BLOCKED, _sanitize(str(exc))) from exc
    except _UnsupportedProblem as exc:
        raise WorkerProblem(Outcome.UNSUPPORTED, _sanitize(str(exc))) from exc
    except (_InfrastructureProblem, ValueError) as exc:
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, _sanitize(str(exc))) from exc

    residue = _list_containers_by_label(pinned, config, f"{OWNER_LABEL}={OWNER_VALUE}")
    if residue:
        _quarantine_endpoint(endpoint)
        raise WorkerProblem(
            Outcome.CLEANUP_FAILED,
            "M1 worker is quarantined because prior run-owned containers remain: "
            + ", ".join(residue),
        )
    return PreparedDockerRuntime(context, endpoint, server_version, image, pinned, config)


def run_docker_phase(
    runtime: PreparedDockerRuntime,
    snapshot: SourceSnapshot,
    target: Target,
    recipe: Recipe,
    *,
    phase: Literal["baseline", "proof"],
    candidate: CandidatePatch | None = None,
    binding: ControllerBinding | None = None,
    authorized_recipe: bool = False,
) -> DockerPhaseResult:
    """Execute one independent phase and return controller-bound typed evidence.

    ``authorized_recipe`` is a trusted-controller assertion used only when an
    already-reviewed repaired recipe is committed at the prepared source revision.
    It leaves the default M1/M2 baseline allowlist unchanged and still applies the
    semantic proof-recipe policy before any repository container is created.
    """

    authority = binding or ControllerBinding(
        run_id=uuid.uuid4(), attempt_id=uuid.uuid4(), workspace_id=uuid.uuid4()
    )
    if _endpoint_is_quarantined(runtime.docker_endpoint):
        return DockerPhaseResult(
            Outcome.CLEANUP_FAILED,
            authority,
            None,
            "M1 worker endpoint is quarantined after an earlier cleanup failure",
        )
    phase_runtime = replace(
        runtime,
        runner=_DeadlineDockerCliRunner(
            runtime.runner,
            time.monotonic() + CONTROLLED_NODE_FIXTURE_POLICY.docker.wall_time_seconds,
        ),
    )
    if (phase == "baseline") != (candidate is None):
        return DockerPhaseResult(
            Outcome.POLICY_BLOCKED,
            authority,
            None,
            "baseline must use base bytes and proof must use an exact candidate",
        )
    if type(authorized_recipe) is not bool or (authorized_recipe and phase != "baseline"):
        return DockerPhaseResult(
            Outcome.POLICY_BLOCKED,
            authority,
            None,
            "authorized_recipe is valid only for a candidate-free baseline",
        )
    try:
        if candidate is not None:
            _assert_candidate_authorized(snapshot, candidate, recipe)
        selected_files = _selected_files(snapshot, candidate)
        expected_os, expected_architecture = target.runtime.platform.split("/", 1)
        if (
            runtime.image.supplied_reference != target.runtime.image_ref
            or runtime.image.os != expected_os
            or runtime.image.architecture != expected_architecture
        ):
            raise SourcePolicyError("prepared runtime is not bound to the selected target")
        recipe_policy_phase: Literal["baseline", "proof"] = (
            "proof" if authorized_recipe else phase
        )
        _assert_recipe_authorized(
            recipe, phase=recipe_policy_phase, source_files=snapshot.files
        )
        _assert_preexecution_contracts(selected_files, recipe, target)
        runtime_evidence = _runtime_image_evidence(runtime, target)
    except (SourcePolicyError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        return DockerPhaseResult(Outcome.POLICY_BLOCKED, authority, None, _sanitize(str(exc)))

    created: list[tuple[str, str]] = []
    app_id: str | None = None
    verifier_id: str | None = None
    workspace_created = False
    command_evidence: list[CommandEvidence] = []
    readiness: ReadinessEvidence | None = None
    acceptance: AcceptanceProbeEvidence | None = None
    observed_digests: Mapping[str, str] | None = None
    workspace_marker_digest: ContentDigest | None = None
    primary_outcome = Outcome.INFRASTRUCTURE_ERROR
    errors: list[str] = []
    cleanup_errors: list[str] = []
    app_removed = False
    verifier_removed = False
    workspace_removed = False
    owned_only = True

    try:
        app_id = _create_app(phase_runtime, authority)
        created.append((app_id, "app"))
        _assert_container(
            phase_runtime,
            app_id,
            authority,
            "app",
            "none",
            _IDLE_SUPERVISOR,
            None,
        )
        _start_container(phase_runtime, app_id)
        workspace_created = True
        workspace_marker_digest = _seed_fresh_workspace(
            phase_runtime, app_id, authority
        )
        _write_workspace(phase_runtime, selected_files, app_id)

        for step in recipe.steps:
            record = _execute_foreground(phase_runtime, app_id, step)
            command_evidence.append(record)
            if not record.succeeded:
                primary_outcome = record.outcome
                raise _StopPhase

        _launch_start(phase_runtime, app_id, recipe.start, target.app_port)
        start_state = _read_start_result(phase_runtime, app_id, wait_milliseconds=250)
        command_evidence.append(_start_evidence(recipe.start, start_state))
        if start_state.get("exists") is True:
            primary_outcome = Outcome.FAILED
            raise _StopPhase

        verifier_id = _create_verifier(phase_runtime, authority, app_id, target)
        created.append((verifier_id, "verifier"))
        _assert_container(
            phase_runtime,
            verifier_id,
            authority,
            "verifier",
            f"container:{app_id}",
            TRUSTED_VERIFIER_V1,
            _verifier_argument(authority, target),
        )
        verifier_result = _run_verifier(phase_runtime, verifier_id, target)
        readiness, acceptance, primary_outcome = _parse_verifier_result(
            verifier_result, authority, target
        )
        start_state = _read_start_result(phase_runtime, app_id, wait_milliseconds=100)
        if start_state.get("exists") is True:
            command_evidence[-1] = _start_evidence(recipe.start, start_state)
            if primary_outcome in {Outcome.PASSED, Outcome.FAILED}:
                primary_outcome = Outcome.FAILED
        observed_digests = _hash_workspace(phase_runtime, app_id, selected_files)
    except _StopPhase:
        pass
    except WorkerProblem as exc:
        primary_outcome = exc.outcome
        errors.append(_sanitize(str(exc)))
    except Exception as exc:
        primary_outcome = Outcome.INFRASTRUCTURE_ERROR
        errors.append(_sanitize(f"unexpected trusted worker failure: {exc}"))
    finally:
        cleanup_runtime = replace(
            runtime,
            runner=_DeadlineDockerCliRunner(
                runtime.runner,
                time.monotonic()
                + CONTROLLED_NODE_FIXTURE_POLICY.docker.cleanup_wall_time_seconds,
                budget_name="cleanup",
            ),
        )
        cleanup_errors, removed, recovered_roles = _cleanup(
            cleanup_runtime, authority, created
        )
        app_id = app_id or recovered_roles.get("app")
        verifier_id = verifier_id or recovered_roles.get("verifier")
        app_removed = app_id is not None and app_id in removed
        verifier_removed = verifier_id is not None and verifier_id in removed
        # The workspace is an anonymous tmpfs owned exclusively by the app
        # container.  Exact container removal is therefore workspace removal;
        # no repository bytes are staged in a host temporary directory.
        workspace_removed = workspace_created and app_removed
        cleanup_errors = _bounded_errors(cleanup_errors)
        if cleanup_errors:
            _quarantine_endpoint(runtime.docker_endpoint)
            primary_outcome = Outcome.CLEANUP_FAILED

    cleanup = CleanupEvidence(
        attempted=True,
        app_container_created=app_id is not None,
        app_container_removed=app_removed,
        verifier_container_created=verifier_id is not None,
        verifier_container_removed=verifier_removed,
        workspace_created=workspace_created,
        workspace_removed=workspace_removed,
        run_owned_resources_only=owned_only,
        worker_quarantined=bool(cleanup_errors),
        sanitized_errors=tuple(cleanup_errors),
    )
    if cleanup_errors:
        errors.extend(cleanup_errors)
    errors = _bounded_errors(errors)
    attempt = AttemptEvidence(
        phase=phase,
        run_id=authority.run_id,
        attempt_id=authority.attempt_id,
        workspace_id=authority.workspace_id,
        base_commit=snapshot.base_commit,
        base_git_tree=snapshot.base_tree,
        source_git_tree=snapshot.source_tree,
        base_tree_digest=ContentDigest(snapshot.content_tree_digest),
        base_archive_digest=ContentDigest(snapshot.archive_digest),
        candidate_digest=ContentDigest(candidate.patch_digest) if candidate else None,
        candidate_tree_digest=(
            ContentDigest(candidate.candidate_tree_digest) if candidate else None
        ),
        target_reference=CONTROLLED_NODE_FIXTURE_POLICY.target_path,
        target_digest=ContentDigest.from_bytes(
            selected_files[CONTROLLED_NODE_FIXTURE_POLICY.target_path]
        ),
        recipe_reference=CONTROLLED_NODE_FIXTURE_POLICY.recipe_path,
        recipe_digest=ContentDigest.from_bytes(
            selected_files[CONTROLLED_NODE_FIXTURE_POLICY.recipe_path]
        ),
        readme_reference=CONTROLLED_NODE_FIXTURE_POLICY.readme_path,
        readme_digest=ContentDigest.from_bytes(
            selected_files[CONTROLLED_NODE_FIXTURE_POLICY.readme_path]
        ),
        verifier_id=target.acceptance.verifier_id,
        verifier_digest=TRUSTED_VERIFIER_DIGEST,
        policy_revision=CONTROLLED_NODE_FIXTURE_POLICY.revision,
        policy_digest=CONTROLLED_NODE_FIXTURE_POLICY_DIGEST,
        app_runtime=runtime_evidence,
        verifier_runtime=runtime_evidence,
        app_container_id=app_id,
        verifier_container_id=verifier_id,
        workspace_marker_digest=workspace_marker_digest,
        commands=tuple(command_evidence),
        readiness=readiness,
        acceptance_probe=acceptance,
        cleanup=cleanup,
        outcome=primary_outcome,
        sanitized_errors=tuple(errors),
    )
    if not (
        app_id
        and verifier_id
        and readiness is not None
        and acceptance is not None
        and observed_digests is not None
        and workspace_marker_digest is not None
        and len(command_evidence) == len(recipe.steps) + 1
    ):
        error = "; ".join(errors) or "phase stopped before complete evidence"
        return DockerPhaseResult(
            primary_outcome,
            authority,
            None,
            _sanitize(error),
            attempt,
        )
    evidence = _build_evidence(
        runtime=runtime,
        snapshot=snapshot,
        target=target,
        recipe=recipe,
        phase=phase,
        candidate=candidate,
        authority=authority,
        app_id=app_id,
        verifier_id=verifier_id,
        selected_files=selected_files,
        observed_digests=observed_digests,
        workspace_marker_digest=workspace_marker_digest,
        commands=tuple(command_evidence),
        readiness=readiness,
        acceptance=acceptance,
        cleanup=cleanup,
        outcome=primary_outcome,
        errors=tuple(errors),
    )
    return DockerPhaseResult(primary_outcome, authority, evidence, attempt=attempt)


class _StopPhase(Exception):
    pass


def _selected_files(
    snapshot: SourceSnapshot, candidate: CandidatePatch | None
) -> dict[str, bytes]:
    selected = dict(snapshot.files)
    if candidate:
        selected.update(candidate.replacements)
    return selected


def _assert_candidate_authorized(
    snapshot: SourceSnapshot,
    candidate: CandidatePatch,
    recipe: Recipe,
) -> None:
    policy = CONTROLLED_NODE_FIXTURE_POLICY
    required_paths = {policy.recipe_path, policy.readme_path}
    if set(candidate.replacements) != required_paths:
        raise SourcePolicyError(
            "M1 candidate must contain only the paired recipe and managed README update"
        )
    expected_readme = replace_block_bytes(
        snapshot.content(policy.readme_path), recipe
    )
    if candidate.replacements[policy.readme_path] != expected_readme:
        raise SourcePolicyError(
            "candidate README changed bytes outside the deterministic managed block"
        )
    rebuilt = build_candidate_patch(snapshot, candidate.replacements)
    if (
        rebuilt.patch_digest != candidate.patch_digest
        or rebuilt.candidate_tree_digest != candidate.candidate_tree_digest
    ):
        raise SourcePolicyError("candidate digests do not match the exact replacement bytes")


def _assert_recipe_authorized(
    recipe: Recipe,
    *,
    phase: Literal["baseline", "proof"],
    source_files: Mapping[str, bytes],
) -> None:
    policy = CONTROLLED_NODE_FIXTURE_POLICY
    if phase == "baseline":
        actual = tuple(
            (step.id, step.argv, step.cwd, step.timeout_seconds) for step in recipe.steps
        )
        expected = tuple(
            (step.step_id, step.argv, step.cwd, step.timeout_seconds)
            for step in policy.baseline_commands
        )
        if actual != expected:
            raise SourcePolicyError("baseline recipe differs from the approved snapshot policy")
    else:
        baseline = policy.baseline_commands[0]
        first = recipe.steps[0]
        if (first.id, first.argv, first.cwd, first.timeout_seconds) != (
            baseline.step_id,
            baseline.argv,
            baseline.cwd,
            baseline.timeout_seconds,
        ):
            raise SourcePolicyError("proof recipe must retain the approved install step")
        package = _load_unique_json(source_files["package.json"])
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if not isinstance(scripts, dict):
            raise SourcePolicyError("approved package does not declare scripts")
        for step in recipe.steps[1:]:
            if (
                step.cwd != "."
                or len(step.argv) != 3
                or step.argv[:2] != ("npm", "run")
                or step.argv[2] not in scripts
            ):
                raise SourcePolicyError(
                    "proof recipe command is not an existing immutable package script"
                )
    start = policy.start_command
    if (recipe.start.id, recipe.start.argv, recipe.start.cwd, recipe.start.timeout_seconds) != (
        start.step_id,
        start.argv,
        start.cwd,
        start.timeout_seconds,
    ):
        raise SourcePolicyError("managed start command differs from the approved policy")


def _assert_preexecution_contracts(
    files: Mapping[str, bytes], recipe: Recipe, target: Target
) -> None:
    recipe_bytes = files[CONTROLLED_NODE_FIXTURE_POLICY.recipe_path]
    target_bytes = files[CONTROLLED_NODE_FIXTURE_POLICY.target_path]
    loaded_recipe = Recipe.model_validate(_load_unique_json(recipe_bytes))
    loaded_target = Target.model_validate(_load_unique_json(target_bytes))
    if loaded_recipe != recipe:
        raise SourcePolicyError("executed recipe is not the exact selected recipe bytes")
    if loaded_target != target:
        raise SourcePolicyError("target bytes differ from the approved target")
    readme = files[CONTROLLED_NODE_FIXTURE_POLICY.readme_path]
    if not check_block_bytes(readme, recipe):
        raise SourcePolicyError("README managed block does not match the executed recipe")


def _load_unique_json(content: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SourcePolicyError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        content.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(
            SourcePolicyError(f"unsupported JSON constant: {value}")
        ),
    )


def _create_app(runtime: PreparedDockerRuntime, binding: ControllerBinding) -> str:
    return _create_owned_container(
        runtime,
        binding,
        role="app",
        network_mode="none",
        user=CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
        tmpfs=(
            f"/workspace:rw,noexec,nosuid,nodev,size={CONTROLLED_NODE_FIXTURE_POLICY.docker.workspace_tmpfs_bytes},uid=65534,gid=65534,mode=0750",
            f"/tmp:rw,noexec,nosuid,nodev,size={CONTROLLED_NODE_FIXTURE_POLICY.docker.tmp_tmpfs_bytes},uid=65534,gid=65534,mode=0700",
        ),
        program=_IDLE_SUPERVISOR,
        program_argument=None,
        working_directory="/workspace",
    )


def _create_verifier(
    runtime: PreparedDockerRuntime,
    binding: ControllerBinding,
    app_id: str,
    target: Target,
) -> str:
    argument = _verifier_argument(binding, target)
    return _create_owned_container(
        runtime,
        binding,
        role="verifier",
        network_mode=f"container:{app_id}",
        user=CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
        tmpfs=(
            f"/tmp:rw,noexec,nosuid,nodev,size={CONTROLLED_NODE_FIXTURE_POLICY.docker.tmp_tmpfs_bytes},uid=65534,gid=65534,mode=0700",
        ),
        program=TRUSTED_VERIFIER_V1,
        program_argument=argument,
        working_directory="/tmp",
    )


def _verifier_argument(binding: ControllerBinding, target: Target) -> str:
    spec = {
        "runId": str(binding.run_id),
        "attemptId": str(binding.attempt_id),
        "port": target.app_port,
        "readinessPath": target.readiness.path,
        "readinessStatus": target.readiness.status,
        "readinessTimeout": target.readiness.timeout_seconds,
        "acceptanceTimeout": target.acceptance.timeout_seconds,
    }
    return _encode_json(spec)


def _create_owned_container(
    runtime: PreparedDockerRuntime,
    binding: ControllerBinding,
    *,
    role: str,
    network_mode: str,
    user: str,
    tmpfs: Sequence[str],
    program: str,
    program_argument: str | None,
    working_directory: str,
) -> str:
    run_text = binding.run_id.hex
    attempt_text = binding.attempt_id.hex
    name = f"firstrun-m1-{run_text[:16]}-{role}"
    policy = CONTROLLED_NODE_FIXTURE_POLICY.docker
    argv: list[str] = [
        runtime.config.docker_binary,
        "container",
        "create",
        "--name",
        name,
        "--label",
        f"{OWNER_LABEL}={OWNER_VALUE}",
        "--label",
        f"{RUN_LABEL}={run_text}",
        "--label",
        f"{ATTEMPT_LABEL}={attempt_text}",
        "--label",
        f"{ROLE_LABEL}={role}",
        "--platform",
        CONTROLLED_NODE_FIXTURE_POLICY.platform,
        "--pull",
        "never",
        "--network",
        network_mode,
        "--user",
        user,
        "--workdir",
        working_directory,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--security-opt",
        "seccomp=builtin",
        "--read-only",
        "--memory",
        str(policy.memory_bytes),
        "--memory-swap",
        str(policy.memory_swap_bytes),
        "--cpus",
        str(policy.nano_cpus / 1_000_000_000),
        "--pids-limit",
        str(policy.pids),
        "--shm-size",
        str(policy.shm_bytes),
        "--stop-timeout",
        str(policy.stop_timeout_seconds),
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=1",
        "--env",
        "HOME=/tmp",
        "--env",
        "npm_config_cache=/tmp/npm-cache",
    ]
    for mount in tmpfs:
        argv.extend(("--tmpfs", mount))
    argv.extend(("--entrypoint", "node", runtime.image.repository_digest, "-e", program))
    if program_argument is not None:
        argv.append(program_argument)
    result = runtime.runner.run(tuple(argv), timeout_seconds=30)
    container_id = result.stdout.strip()
    if result.returncode == 0 and _CONTAINER_ID.fullmatch(container_id):
        return container_id
    recovered = _recover_owned(runtime, name, binding, role)
    if recovered:
        # Docker can create the container but lose/truncate the CLI response.  A
        # recovered ID is accepted only after exact name/label checks; the caller
        # records it immediately and performs the full pre-start inspection.
        return recovered
    detail = _command_error(f"create {role} container", result)
    raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, detail)


def _recover_owned(
    runtime: PreparedDockerRuntime,
    name: str,
    binding: ControllerBinding,
    role: str,
) -> str | None:
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "inspect",
            "--format",
            _RECOVERY_FORMAT,
            name,
        ),
        timeout_seconds=30,
    )
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
        labels = value["Config"]["Labels"]
        container_id = value["Id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if (
        _CONTAINER_ID.fullmatch(str(container_id))
        and value.get("Name") in (name, f"/{name}")
        and _labels_match(labels, binding, role)
    ):
        return str(container_id)
    return None


def _start_container(runtime: PreparedDockerRuntime, container_id: str) -> None:
    result = runtime.runner.run(
        (runtime.config.docker_binary, "container", "start", container_id),
        timeout_seconds=30,
    )
    if result.returncode != 0:
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, _command_error("start app", result))


def _seed_fresh_workspace(
    runtime: PreparedDockerRuntime,
    app_id: str,
    binding: ControllerBinding,
) -> ContentDigest:
    """Fail before repository execution if predecessor state is observable."""

    marker_content = f"{binding.run_id.hex}:{binding.attempt_id.hex}:{binding.workspace_id.hex}"
    expected_digest = ContentDigest.from_bytes(marker_content.encode("utf-8"))
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "exec",
            "--user",
            CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
            app_id,
            "node",
            "-e",
            _SEED_FRESH_WORKSPACE,
            _FRESHNESS_MARKER,
            marker_content,
        ),
        timeout_seconds=5,
    )
    try:
        value = _last_json_object(result.stdout)
    except ValueError as exc:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            "fresh workspace marker returned malformed evidence",
        ) from exc
    if result.returncode == 10 and value.get("preexistingAbsent") is False:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            "fresh workspace contained a predecessor-state marker",
        )
    if (
        result.returncode != 0
        or value
        != {
            "preexistingAbsent": True,
            "markerDigest": str(expected_digest),
        }
    ):
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            "fresh workspace marker could not be established",
        )
    return expected_digest


def _write_workspace(
    runtime: PreparedDockerRuntime,
    files: Mapping[str, bytes],
    app_id: str,
) -> None:
    encoded_files = {
        path: base64.b64encode(content).decode("ascii") for path, content in files.items()
    }
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "exec",
            "--user",
            CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
            app_id,
            "node",
            "-e",
            _WRITE_WORKSPACE,
            _encode_json(encoded_files),
        ),
        timeout_seconds=30,
    )
    if result.returncode != 0:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            _command_error("materialize immutable workspace", result),
        )


def _execute_foreground(
    runtime: PreparedDockerRuntime, app_id: str, step: RecipeStep
) -> CommandEvidence:
    spec = _encode_json({"argv": list(step.argv), "cwd": step.cwd})
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "exec",
            "--user",
            CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
            "--workdir",
            "/workspace",
            app_id,
            "node",
            "-e",
            _RUN_COMMAND,
            spec,
        ),
        timeout_seconds=step.timeout_seconds,
    )
    if result.returncode == 124:
        return CommandEvidence(
            step_id=step.id,
            kind="foreground",
            argv=step.argv,
            cwd=step.cwd,
            timeout_seconds=step.timeout_seconds,
            outcome=Outcome.TIMED_OUT,
            exit_code=None,
            sanitized_stderr_tail="repository command exceeded its controller deadline",
        )
    if result.returncode != 0:
        return CommandEvidence(
            step_id=step.id,
            kind="foreground",
            argv=step.argv,
            cwd=step.cwd,
            timeout_seconds=step.timeout_seconds,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            exit_code=result.returncode,
            sanitized_stderr_tail=_tail(_command_error("sandbox command adapter", result)),
        )
    try:
        value = _last_json_object(result.stdout)
        exit_code = value.get("exitCode")
        stdout = _tail(str(value.get("stdout", "")))
        stderr = _tail(str(value.get("stderr", "")))
        overflow = value.get("overflow") is True
    except (ValueError, TypeError, AttributeError):
        return CommandEvidence(
            step_id=step.id,
            kind="foreground",
            argv=step.argv,
            cwd=step.cwd,
            timeout_seconds=step.timeout_seconds,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            exit_code=None,
            sanitized_stderr_tail="sandbox command adapter returned malformed evidence",
        )
    outcome = Outcome.PASSED if exit_code == 0 and not overflow else Outcome.FAILED
    return CommandEvidence(
        step_id=step.id,
        kind="foreground",
        argv=step.argv,
        cwd=step.cwd,
        timeout_seconds=step.timeout_seconds,
        outcome=outcome,
        exit_code=exit_code if type(exit_code) is int else None,
        sanitized_stdout_tail=stdout,
        sanitized_stderr_tail=stderr or ("command output exceeded its limit" if overflow else ""),
    )


def _launch_start(
    runtime: PreparedDockerRuntime,
    app_id: str,
    step: RecipeStep,
    port: int,
) -> None:
    spec = _encode_json({"argv": list(step.argv), "cwd": step.cwd, "port": port})
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "exec",
            "--detach",
            "--user",
            CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
            "--workdir",
            "/workspace",
            app_id,
            "node",
            "-e",
            _START_COMMAND,
            spec,
        ),
        timeout_seconds=10,
    )
    if result.returncode != 0:
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, _command_error("launch app", result))


def _read_start_result(
    runtime: PreparedDockerRuntime,
    app_id: str,
    *,
    wait_milliseconds: int,
) -> dict[str, Any]:
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "exec",
            "--user",
            CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
            app_id,
            "node",
            "-e",
            _READ_START_RESULT,
            str(wait_milliseconds),
        ),
        timeout_seconds=3,
    )
    if result.returncode != 0:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            _command_error("observe managed start process", result),
        )
    try:
        value = _last_json_object(result.stdout)
    except ValueError as exc:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            "managed start observer returned malformed evidence",
        ) from exc
    if value.get("exists") is False and set(value) == {"exists"}:
        return value
    expected_fields = {"exists", "exitCode", "signal", "error"}
    if value.get("exists") is not True or set(value) != expected_fields:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            "managed start observer returned an invalid state",
        )
    exit_code = value.get("exitCode")
    signal = value.get("signal")
    error = value.get("error")
    if (
        (exit_code is not None and type(exit_code) is not int)
        or (signal is not None and type(signal) is not str)
        or (error is not None and type(error) is not str)
        or (exit_code is None and signal is None and error is None)
    ):
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            "managed start observer returned an invalid state",
        )
    return value


def _start_evidence(step: RecipeStep, state: Mapping[str, Any]) -> CommandEvidence:
    if state.get("exists") is not True:
        return CommandEvidence(
            step_id=step.id,
            kind="start",
            argv=step.argv,
            cwd=step.cwd,
            timeout_seconds=step.timeout_seconds,
            outcome=Outcome.PASSED,
            exit_code=None,
        )
    raw_exit = state.get("exitCode")
    exit_code = raw_exit if type(raw_exit) is int else None
    detail = state.get("error") or state.get("signal") or "managed start process exited"
    return CommandEvidence(
        step_id=step.id,
        kind="start",
        argv=step.argv,
        cwd=step.cwd,
        timeout_seconds=step.timeout_seconds,
        outcome=Outcome.FAILED,
        exit_code=exit_code,
        sanitized_stderr_tail=_tail(str(detail)),
    )


def _run_verifier(
    runtime: PreparedDockerRuntime, verifier_id: str, target: Target
) -> CommandResult:
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "start",
            "--attach",
            verifier_id,
        ),
        timeout_seconds=(
            target.readiness.timeout_seconds + target.acceptance.timeout_seconds + 5
        ),
    )
    if result.returncode == 124:
        raise WorkerProblem(Outcome.TIMED_OUT, "trusted verifier exceeded its deadline")
    return result


def _parse_verifier_result(
    result: CommandResult,
    binding: ControllerBinding,
    target: Target,
) -> tuple[ReadinessEvidence, AcceptanceProbeEvidence, Outcome]:
    try:
        value = _last_json_object(result.stdout)
    except ValueError as exc:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR, "trusted verifier returned malformed evidence"
        ) from exc
    if (
        value.get("schemaVersion") != 1
        or value.get("runId") != str(binding.run_id)
        or value.get("attemptId") != str(binding.attempt_id)
        or value.get("verifierId") != target.acceptance.verifier_id
    ):
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR, "trusted verifier binding did not match the attempt"
        )
    try:
        outcome = Outcome(value.get("outcome"))
    except ValueError as exc:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR, "trusted verifier returned an unknown outcome"
        ) from exc
    expected_exit = {
        Outcome.PASSED: 0,
        Outcome.FAILED: 10,
        Outcome.TIMED_OUT: 11,
        Outcome.INFRASTRUCTURE_ERROR: 12,
    }.get(outcome)
    if expected_exit is None or result.returncode != expected_exit:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR, "trusted verifier exit and payload disagreed"
        )
    observed = value.get("observedStatus")
    attempts = value.get("readinessAttempts")
    if type(attempts) is not int or attempts < 0 or attempts > 10_000:
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, "invalid readiness attempt count")
    if observed is not None and (type(observed) is not int or not 100 <= observed <= 599):
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, "invalid readiness status")
    readiness_outcome = (
        Outcome.PASSED
        if observed == target.readiness.status and attempts > 0
        else Outcome.TIMED_OUT
    )
    readiness = ReadinessEvidence(
        path=target.readiness.path,
        expected_status=target.readiness.status,
        observed_status=observed,
        timeout_seconds=target.readiness.timeout_seconds,
        attempts=attempts,
        outcome=readiness_outcome,
        sanitized_error=(
            None if readiness_outcome is Outcome.PASSED else _sanitize(str(value.get("error")))
        ),
    )
    raw_checks = value.get("passedChecks")
    if not isinstance(raw_checks, list) or any(type(item) is not str for item in raw_checks):
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, "invalid verifier check list")
    raw_nonce = value.get("nonceDigest")
    if raw_nonce is not None and (
        type(raw_nonce) is not str or _DIGEST.fullmatch(raw_nonce) is None
    ):
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, "invalid verifier nonce digest")
    error = value.get("error")
    required_checks = ("create_note", "read_back_same_note")
    if outcome is Outcome.PASSED and (
        readiness_outcome is not Outcome.PASSED
        or tuple(raw_checks) != required_checks
        or raw_nonce is None
        or error not in (None, "")
    ):
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            "trusted verifier claimed pass without complete acceptance observations",
        )
    acceptance = AcceptanceProbeEvidence(
        verifier_id=target.acceptance.verifier_id,
        verifier_digest=TRUSTED_VERIFIER_DIGEST,
        timeout_seconds=target.acceptance.timeout_seconds,
        outcome=outcome,
        nonce_digest=ContentDigest(raw_nonce) if raw_nonce else None,
        required_checks=required_checks,
        passed_checks=tuple(raw_checks),
        sanitized_error=_sanitize(str(error)) if error else None,
    )
    return readiness, acceptance, outcome


def _hash_workspace(
    runtime: PreparedDockerRuntime,
    app_id: str,
    selected_files: Mapping[str, bytes],
) -> Mapping[str, str]:
    names = sorted(selected_files)
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "exec",
            "--user",
            CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
            app_id,
            "node",
            "-e",
            _HASH_WORKSPACE,
            _encode_json(names),
        ),
        timeout_seconds=15,
    )
    if result.returncode != 0:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR, _command_error("hash protected workspace", result)
        )
    try:
        value = _last_json_object(result.stdout)
    except ValueError as exc:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR, "workspace hasher returned malformed evidence"
        ) from exc
    if set(value) != set(names) or any(
        type(digest) is not str or _DIGEST.fullmatch(digest) is None
        for digest in value.values()
    ):
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, "workspace digest set is invalid")
    expected = file_digests(selected_files)
    if value != dict(expected):
        raise WorkerProblem(Outcome.POLICY_BLOCKED, "repository changed protected input bytes")
    return value


def _assert_container(
    runtime: PreparedDockerRuntime,
    container_id: str,
    binding: ControllerBinding,
    role: str,
    network_mode: str,
    program: str,
    program_argument: str | None,
) -> None:
    inspection = _inspect(runtime, container_id)
    config = inspection.get("Config")
    host = inspection.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, "container inspection was incomplete")
    failures: list[str] = []
    if inspection.get("Id") != container_id or not _labels_match(
        config.get("Labels"), binding, role
    ):
        failures.append("ownership identity")
    if (
        inspection.get("Image") != runtime.image.image_id
        or config.get("Image") != runtime.image.repository_digest
    ):
        failures.append("runtime digest")
    command = config.get("Cmd")
    expected_command = ["-e", program]
    if program_argument is not None:
        expected_command.append(program_argument)
    if config.get("Entrypoint") != ["node"] or command != expected_command:
        failures.append("controller-owned entrypoint")
    if config.get("User") != CONTROLLED_NODE_FIXTURE_POLICY.docker.user:
        failures.append("non-root user")
    if config.get("WorkingDir") not in {"/workspace", "/tmp"}:
        failures.append("working directory")
    if host.get("NetworkMode") != network_mode:
        failures.append("network namespace")
    environment = config.get("Env")
    if not isinstance(environment, list) or not {
        "HOME=/tmp",
        "npm_config_cache=/tmp/npm-cache",
    }.issubset(set(environment)):
        failures.append("bounded environment")
    elif any(_forbidden_environment_name(item) for item in environment):
        failures.append("credential-like environment")
    if config.get("Volumes") not in (None, {}):
        failures.append("image-declared volumes")
    if "ALL" not in {str(item).upper() for item in (host.get("CapDrop") or [])}:
        failures.append("capability drop")
    if host.get("CapAdd"):
        failures.append("added capabilities")
    security = {str(item).lower() for item in (host.get("SecurityOpt") or [])}
    if "seccomp=builtin" not in security or not any(
        item.startswith("no-new-privileges") and not item.endswith("=false")
        for item in security
    ):
        failures.append("security options")
    policy = CONTROLLED_NODE_FIXTURE_POLICY.docker
    if (
        host.get("ReadonlyRootfs") is not True
        or host.get("Memory") != policy.memory_bytes
        or host.get("MemorySwap") != policy.memory_swap_bytes
        or host.get("NanoCpus") != policy.nano_cpus
        or host.get("PidsLimit") != policy.pids
        or host.get("ShmSize") != policy.shm_bytes
    ):
        failures.append("resource limits")
    tmpfs = host.get("Tmpfs")
    expected_destinations = {"/tmp"} if role == "verifier" else {"/tmp", "/workspace"}
    if not isinstance(tmpfs, dict) or set(tmpfs) != expected_destinations:
        failures.append("bounded tmpfs")
    elif any(not _tmpfs_options_are_safe(value) for value in tmpfs.values()):
        failures.append("tmpfs options")
    if (
        host.get("Binds")
        or host.get("Privileged") is True
        or host.get("PidMode") not in (None, "")
        or str(host.get("IpcMode") or "").lower() not in ("", "private")
        or host.get("PortBindings")
        or host.get("Devices")
        or host.get("DeviceRequests")
    ):
        failures.append("host isolation")
    mounts = inspection.get("Mounts")
    if mounts not in (None, []):
        if not isinstance(mounts, list) or any(
            not isinstance(mount, dict)
            or str(mount.get("Type", "")).lower() != "tmpfs"
            or mount.get("Destination") not in expected_destinations
            or mount.get("RW") is not True
            for mount in mounts
        ):
            failures.append("shared mounts")
    restart = host.get("RestartPolicy") or {}
    if not isinstance(restart, dict) or restart.get("Name") not in (None, "", "no"):
        failures.append("restart policy")
    if host.get("LogConfig") != {
        "Type": "json-file",
        "Config": {"max-size": "1m", "max-file": "1"},
    }:
        failures.append("bounded container logs")
    if failures:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR,
            f"{role} container inspection failed: {', '.join(failures)}",
        )


def _inspect(runtime: PreparedDockerRuntime, container_id: str) -> dict[str, Any]:
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "inspect",
            "--format",
            _INSPECT_FORMAT,
            container_id,
        ),
        timeout_seconds=30,
    )
    if result.returncode != 0:
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, _command_error("inspect container", result))
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, "container inspection was invalid") from exc
    if not isinstance(value, dict):
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, "container inspection was not an object")
    return value


def _cleanup(
    runtime: PreparedDockerRuntime,
    binding: ControllerBinding,
    created: Sequence[tuple[str, str]],
) -> tuple[list[str], set[str], dict[str, str]]:
    errors: list[str] = []
    removed: set[str] = set()
    recovered_roles: dict[str, str] = {}
    for container_id, role in reversed(created):
        try:
            _remove_exact(runtime, container_id, binding, role)
            removed.add(container_id)
        except Exception as exc:
            errors.append(_sanitize(f"cleanup of recorded {role} failed: {exc}"))
    try:
        residue = _list_containers_by_label(
            runtime.runner,
            runtime.config,
            f"{RUN_LABEL}={binding.run_id.hex}",
        )
        for container_id in residue:
            try:
                inspection = _inspect(runtime, container_id)
                config = inspection.get("Config")
                labels = config.get("Labels") if isinstance(config, dict) else None
                role = labels.get(ROLE_LABEL) if isinstance(labels, dict) else None
                if role not in {"app", "verifier"} or not _labels_match(
                    labels, binding, role
                ):
                    raise WorkerProblem(
                        Outcome.CLEANUP_FAILED,
                        f"refused cleanup of {container_id}: ownership labels are invalid",
                    )
                if role in recovered_roles and recovered_roles[role] != container_id:
                    errors.append(f"multiple run-owned {role} containers were discovered")
                recovered_roles.setdefault(role, container_id)
                _remove_exact(runtime, container_id, binding, role)
                removed.add(container_id)
            except Exception as exc:
                errors.append(_sanitize(f"cleanup recovery failed: {exc}"))
        remaining = _list_containers_by_label(
            runtime.runner,
            runtime.config,
            f"{RUN_LABEL}={binding.run_id.hex}",
        )
        if remaining:
            errors.append("run-owned container residue remains: " + ", ".join(remaining))
    except Exception as exc:
        errors.append(_sanitize(f"cleanup residue inspection failed: {exc}"))
    return errors, removed, recovered_roles


def _remove_exact(
    runtime: PreparedDockerRuntime,
    container_id: str,
    binding: ControllerBinding,
    role: str,
) -> None:
    inspection = _inspect(runtime, container_id)
    config = inspection.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not _labels_match(labels, binding, role):
        raise WorkerProblem(
            Outcome.CLEANUP_FAILED,
            f"refused cleanup of {container_id}: ownership labels changed",
        )
    result = runtime.runner.run(
        (
            runtime.config.docker_binary,
            "container",
            "rm",
            "--force",
            "--volumes",
            container_id,
        ),
        timeout_seconds=30,
    )
    if result.returncode != 0:
        raise WorkerProblem(Outcome.CLEANUP_FAILED, _command_error("remove container", result))


def _list_containers_by_label(
    runner: DockerCliRunner,
    config: DockerPreflightConfig,
    label: str,
) -> tuple[str, ...]:
    result = runner.run(
        (
            config.docker_binary,
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            f"label={label}",
        ),
        timeout_seconds=30,
    )
    if result.returncode != 0:
        raise WorkerProblem(
            Outcome.INFRASTRUCTURE_ERROR, _command_error("inspect worker residue", result)
        )
    values = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
    if any(_CONTAINER_ID.fullmatch(value) is None for value in values):
        raise WorkerProblem(Outcome.INFRASTRUCTURE_ERROR, "Docker returned an invalid container ID")
    return values


def _labels_match(value: Any, binding: ControllerBinding, role: str) -> bool:
    return isinstance(value, dict) and value == {
        OWNER_LABEL: OWNER_VALUE,
        RUN_LABEL: binding.run_id.hex,
        ATTEMPT_LABEL: binding.attempt_id.hex,
        ROLE_LABEL: role,
    }


def _tmpfs_options_are_safe(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    options = {item.strip().lower() for item in value.split(",") if item.strip()}
    if not {"rw", "noexec", "nosuid", "nodev"}.issubset(options):
        return False
    sizes = [item.split("=", 1)[1] for item in options if item.startswith("size=")]
    if len(sizes) != 1:
        return False
    try:
        size = _parse_byte_size(sizes[0], "tmpfs size")
    except ValueError:
        return False
    allowed = {
        CONTROLLED_NODE_FIXTURE_POLICY.docker.workspace_tmpfs_bytes,
        CONTROLLED_NODE_FIXTURE_POLICY.docker.tmp_tmpfs_bytes,
    }
    return size in allowed


def _forbidden_environment_name(entry: Any) -> bool:
    if not isinstance(entry, str) or "=" not in entry:
        return True
    name = entry.split("=", 1)[0].upper()
    sensitive_fragments = (
        "AWS_",
        "GITHUB_",
        "GH_",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "DOCKER_HOST",
        "SSH_",
    )
    return any(fragment in name for fragment in sensitive_fragments)


def _runtime_image_evidence(
    runtime: PreparedDockerRuntime,
    target: Target,
) -> RuntimeImageEvidence:
    try:
        repository_digest = runtime.image.repository_digest.rsplit("@", 1)[1]
    except IndexError as exc:
        raise ValueError("resolved runtime reference lacks a repository digest") from exc
    return RuntimeImageEvidence(
        requested_reference=runtime.image.supplied_reference,
        resolved_reference=runtime.image.repository_digest,
        repository_digest=ContentDigest(repository_digest),
        image_id=ContentDigest(runtime.image.image_id),
        platform=target.runtime.platform,
    )


def _build_evidence(
    *,
    runtime: PreparedDockerRuntime,
    snapshot: SourceSnapshot,
    target: Target,
    recipe: Recipe,
    phase: Literal["baseline", "proof"],
    candidate: CandidatePatch | None,
    authority: ControllerBinding,
    app_id: str,
    verifier_id: str,
    selected_files: Mapping[str, bytes],
    observed_digests: Mapping[str, str],
    workspace_marker_digest: ContentDigest,
    commands: tuple[CommandEvidence, ...],
    readiness: ReadinessEvidence,
    acceptance: AcceptanceProbeEvidence,
    cleanup: CleanupEvidence,
    outcome: Outcome,
    errors: tuple[str, ...],
) -> RunEvidence:
    policy = CONTROLLED_NODE_FIXTURE_POLICY
    recipe_bytes = selected_files[policy.recipe_path]
    target_bytes = selected_files[policy.target_path]
    readme_bytes = selected_files[policy.readme_path]
    runtime_evidence = _runtime_image_evidence(runtime, target)
    observation = ControllerObservation(
        binding=authority,
        phase=phase,
        base_commit=snapshot.base_commit,
        base_git_tree=snapshot.base_tree,
        source_git_tree=snapshot.source_tree,
        base_tree_digest=ContentDigest(snapshot.content_tree_digest),
        base_archive_digest=ContentDigest(snapshot.archive_digest),
        candidate_digest=ContentDigest(candidate.patch_digest) if candidate else None,
        candidate_tree_digest=(
            ContentDigest(candidate.candidate_tree_digest) if candidate else None
        ),
        target_reference=policy.target_path,
        target_digest=ContentDigest.from_bytes(target_bytes),
        observed_target_digest=ContentDigest(observed_digests[policy.target_path]),
        recipe_reference=policy.recipe_path,
        recipe_digest=ContentDigest.from_bytes(recipe_bytes),
        executed_recipe_digest=ContentDigest(observed_digests[policy.recipe_path]),
        readme_reference=policy.readme_path,
        readme_digest=ContentDigest.from_bytes(readme_bytes),
        rendered_readme_digest=ContentDigest.from_bytes(
            replace_block_bytes(readme_bytes, recipe)
        ),
        verifier_id=target.acceptance.verifier_id,
        verifier_digest=TRUSTED_VERIFIER_DIGEST,
        observed_verifier_digest=TRUSTED_VERIFIER_DIGEST,
        policy_revision=policy.revision,
        policy_digest=CONTROLLED_NODE_FIXTURE_POLICY_DIGEST,
        observed_policy_digest=CONTROLLED_NODE_FIXTURE_POLICY_DIGEST,
        app_runtime=runtime_evidence,
        verifier_runtime=runtime_evidence,
        app_container_id=app_id,
        verifier_container_id=verifier_id,
        fresh_state=FreshStateEvidence(
            workspace_created_for_attempt=True,
            preexisting_workspace_marker_absent=True,
            workspace_marker_digest=workspace_marker_digest,
            mutable_state_reused=False,
            shared_mutable_resource_ids=(),
            only_immutable_image_layers_reused=True,
        ),
        policy_authorized=True,
        commands=commands,
        readiness=readiness,
        acceptance_probe=acceptance,
        cleanup=cleanup,
        outcome=outcome,
        sanitized_errors=errors,
    )
    return build_run_evidence(binding=authority, observation=observation)


def _last_json_object(output: str) -> dict[str, Any]:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise ValueError("missing JSON output")
    value = json.loads(lines[-1])
    if not isinstance(value, dict):
        raise ValueError("JSON output is not an object")
    return value


def _encode_json(value: Any) -> str:
    content = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def _command_error(action: str, result: CommandResult) -> str:
    detail = (result.stderr or result.stdout).strip()
    if detail:
        return _sanitize(f"{action} failed with exit {result.returncode}: {detail}")
    return f"{action} failed with exit {result.returncode}"


def _sanitize(value: str) -> str:
    text = value.replace("\x00", "?").replace("\r", " ").strip()
    patterns = (
        (r"AKIA[0-9A-Z]{16}", "[redacted-aws-key]"),
        (r"gh[pousr]_[A-Za-z0-9_]{20,}", "[redacted-github-token]"),
        (r"(?i)(token|password|secret)=([^\s]+)", r"\1=[redacted]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return (text or "unspecified worker error")[:_MAX_CONTROLLER_ERROR]


def _bounded_errors(values: Sequence[str]) -> list[str]:
    sanitized = [_sanitize(value) for value in values[:31]]
    if len(values) > 31:
        sanitized.append("additional bounded errors were omitted")
    return sanitized


def _endpoint_is_quarantined(endpoint: str) -> bool:
    with _QUARANTINE_LOCK:
        return endpoint in _QUARANTINED_ENDPOINTS


def _quarantine_endpoint(endpoint: str) -> None:
    with _QUARANTINE_LOCK:
        _QUARANTINED_ENDPOINTS.add(endpoint)


def _tail(value: str) -> str:
    sanitized = _sanitize(value) if value else ""
    return sanitized[-_MAX_COMMAND_TAIL:]


__all__ = [
    "DockerPhaseResult",
    "PreparedDockerRuntime",
    "TRUSTED_VERIFIER_DIGEST",
    "TRUSTED_VERIFIER_V1",
    "WorkerProblem",
    "prepare_docker_runtime",
    "run_docker_phase",
]
