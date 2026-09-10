"""Bounded M0 Strands + Amazon Bedrock provider preflight.

The probe proves that the explicitly selected model can execute one
controller-owned tool and return validated structured output.  Imports are lazy
so deterministic tests and the rest of FirstRun do not require provider packages.
No credential value is read by this module or included in its result.
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import multiprocessing
import os
import re
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from firstrun.domain.outcomes import Outcome


PROVIDER_ID = "amazon-bedrock"
SUPPORTED_STRANDS_VERSION = "1.55.1"
NONCE_TOOL_NAME = "firstrun_nonce_round_trip"

_LOCKED_DISTRIBUTIONS = {
    "strands": ("strands-agents", SUPPORTED_STRANDS_VERSION),
    "boto3": ("boto3", "1.43.91"),
    "botocore": ("botocore", "1.43.91"),
    "pydantic": ("pydantic", "2.13.5"),
}

_MAX_WALL_TIME_SECONDS = 120.0
_MAX_TURNS = 4
_MAX_OUTPUT_TOKENS = 512
_MAX_TOTAL_TOKENS = 2048
_MAX_PROVIDER_ATTEMPTS = 2
_WORKER_STOP_GRACE_SECONDS = 2.0
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_REGION = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+-[0-9]+$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_EVIDENCE_CONTROLLER = "controller"
_EVIDENCE_INJECTED = "injected_test_adapter"
_EVIDENCE_LIVE = "live_provider_subprocess"


@dataclass(frozen=True)
class StrandsPreflightConfig:
    """Explicit provider selection and controller-owned probe budgets."""

    aws_profile: str
    region: str
    model_id: str
    timeout_seconds: float = 60.0
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    max_turns: int = 4
    max_output_tokens: int = 256
    max_total_tokens: int = 1024
    provider_total_attempts: int = 2
    provider_cost_acknowledged: bool = False
    credential_identity_verified: bool = False

    def __post_init__(self) -> None:
        _require_explicit_text(self.aws_profile, "aws_profile", max_length=256)
        _require_explicit_text(self.region, "region", max_length=128)
        if _REGION.fullmatch(self.region) is None:
            raise ValueError("region must be a canonical AWS region identifier")
        _require_explicit_text(self.model_id, "model_id", max_length=2048)
        _require_range(
            self.timeout_seconds,
            "timeout_seconds",
            upper=_MAX_WALL_TIME_SECONDS,
        )
        _require_range(
            self.connect_timeout_seconds,
            "connect_timeout_seconds",
            upper=self.timeout_seconds,
        )
        _require_range(
            self.read_timeout_seconds,
            "read_timeout_seconds",
            upper=self.timeout_seconds,
        )
        _require_positive_int(self.max_turns, "max_turns", upper=_MAX_TURNS)
        _require_positive_int(
            self.max_output_tokens,
            "max_output_tokens",
            upper=_MAX_OUTPUT_TOKENS,
        )
        _require_positive_int(
            self.max_total_tokens,
            "max_total_tokens",
            upper=_MAX_TOTAL_TOKENS,
        )
        if self.max_total_tokens < self.max_output_tokens:
            raise ValueError("max_total_tokens must be at least max_output_tokens")
        _require_positive_int(
            self.provider_total_attempts,
            "provider_total_attempts",
            upper=_MAX_PROVIDER_ATTEMPTS,
        )
        if not isinstance(self.provider_cost_acknowledged, bool):
            raise ValueError("provider_cost_acknowledged must be a boolean")
        if not isinstance(self.credential_identity_verified, bool):
            raise ValueError("credential_identity_verified must be a boolean")


@dataclass(frozen=True)
class StrandsProbeObservation:
    """Narrow observation returned by a provider adapter.

    Raw model text and provider exceptions are deliberately excluded.  Nonces are
    used only inside the controller to calculate booleans in the public result.
    """

    sdk_version: str | None
    invoked_nonces: tuple[str, ...]
    structured_nonce: str | None
    provider_endpoint: str | None = None


class StrandsProbeInvoker(Protocol):
    """Dependency-injection seam used by deterministic unit tests."""

    def __call__(
        self, config: StrandsPreflightConfig, nonce: str
    ) -> Awaitable[StrandsProbeObservation]:
        ...


@dataclass(frozen=True)
class StrandsPreflightResult:
    """Sanitized machine-readable provider preflight evidence."""

    outcome: Outcome
    provider: str
    aws_profile: str
    region: str
    model_id: str
    sdk_version: str | None
    provider_endpoint: str | None
    tool_invocation_count: int
    tool_nonce_matched: bool
    structured_nonce_matched: bool
    nonce_matched: bool
    timeout_seconds: float
    max_turns: int
    max_output_tokens: int
    max_total_tokens: int
    provider_total_attempts: int
    strands_model_attempts_per_turn: int
    provider_cost_acknowledged: bool
    credential_identity_verified: bool
    isolated_python: bool
    evidence_source: str
    live_provider_invoked: bool
    milestone_eligible: bool
    error_code: str | None
    message: str
    schema_version: int = 1

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASSED


class _StrandsUnavailable(RuntimeError):
    def __init__(self, error_code: str, sdk_version: str | None) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.sdk_version = sdk_version


@dataclass(frozen=True)
class _StrandsDependencies:
    agent_type: Any
    bedrock_model_type: Any
    boto_session_type: Any
    boto_config_type: Any
    base_model_type: Any
    field: Any
    tool: Any
    structured_output_exception_type: type[Exception]
    sdk_version: str


async def run_strands_preflight_async(
    config: StrandsPreflightConfig,
    *,
    invoker: StrandsProbeInvoker | None = None,
    nonce_factory: Callable[[], str] | None = None,
) -> StrandsPreflightResult:
    """Evaluate a test adapter without producing milestone-grade evidence.

    Live provider work is intentionally available only through the synchronous
    wrapper, which owns a killable child process.  This prevents cancellation of
    an async facade from leaving a paid, non-streaming request alive in a worker
    thread.
    """

    if invoker is None:
        raise RuntimeError(
            "live provider probes must use run_strands_preflight so the hard "
            "subprocess timeout can be enforced"
        )

    controller_nonce = (nonce_factory or _new_nonce)()
    if not _valid_controller_nonce(controller_nonce):
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            evidence_source=_EVIDENCE_CONTROLLER,
            error_code="invalid_controller_nonce",
            message="The controller could not create a valid preflight nonce.",
        )

    try:
        observation = await asyncio.wait_for(
            invoker(config, controller_nonce),
            timeout=config.timeout_seconds,
        )
    except _StrandsUnavailable as exc:
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            sdk_version=exc.sdk_version,
            evidence_source=_EVIDENCE_INJECTED,
            error_code=exc.error_code,
            message="The injected test adapter reported an unavailable runtime.",
        )
    except TimeoutError:
        return _result(
            config,
            outcome=Outcome.TIMED_OUT,
            evidence_source=_EVIDENCE_INJECTED,
            error_code="injected_adapter_timeout",
            message="The injected test adapter timed out.",
        )
    except Exception as exc:
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            evidence_source=_EVIDENCE_INJECTED,
            error_code=_classify_provider_exception(exc),
            message="The injected test adapter failed.",
        )

    return _evaluate_observation(
        config,
        controller_nonce,
        observation,
        evidence_source=_EVIDENCE_INJECTED,
        live_provider_invoked=False,
    )


def run_strands_preflight(
    config: StrandsPreflightConfig,
    *,
    invoker: StrandsProbeInvoker | None = None,
    nonce_factory: Callable[[], str] | None = None,
) -> StrandsPreflightResult:
    """Run a live probe in a killable worker or evaluate an injected test adapter."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if invoker is not None:
            return asyncio.run(
                run_strands_preflight_async(
                    config,
                    invoker=invoker,
                    nonce_factory=nonce_factory,
                )
            )

        if not config.provider_cost_acknowledged:
            return _result(
                config,
                outcome=Outcome.POLICY_BLOCKED,
                evidence_source=_EVIDENCE_CONTROLLER,
                error_code="provider_cost_not_acknowledged",
                message="Live provider cost was not explicitly acknowledged.",
            )
        if not config.credential_identity_verified:
            return _result(
                config,
                outcome=Outcome.POLICY_BLOCKED,
                evidence_source=_EVIDENCE_CONTROLLER,
                error_code="credential_identity_not_verified",
                message=(
                    "Temporary, non-root identity verification was not explicitly "
                    "confirmed for the selected profile."
                ),
            )
        if not sys.flags.isolated:
            return _result(
                config,
                outcome=Outcome.POLICY_BLOCKED,
                evidence_source=_EVIDENCE_CONTROLLER,
                error_code="isolated_python_required",
                message=(
                    "Live provider probes require Python isolated mode; rerun the "
                    "controller with python -I."
                ),
            )
        controller_nonce = (nonce_factory or _new_nonce)()
        if not _valid_controller_nonce(controller_nonce):
            return _result(
                config,
                outcome=Outcome.INFRASTRUCTURE_ERROR,
                evidence_source=_EVIDENCE_CONTROLLER,
                error_code="invalid_controller_nonce",
                message="The controller could not create a valid preflight nonce.",
            )
        return _run_live_provider_isolated(config, controller_nonce)
    raise RuntimeError(
        "run_strands_preflight cannot run inside an event loop; "
        "call it from a synchronous controller boundary"
    )


def _evaluate_observation(
    config: StrandsPreflightConfig,
    controller_nonce: str,
    observation: object,
    *,
    evidence_source: str,
    live_provider_invoked: bool,
) -> StrandsPreflightResult:
    if not isinstance(observation, StrandsProbeObservation):
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            evidence_source=evidence_source,
            live_provider_invoked=live_provider_invoked,
            error_code="invalid_adapter_result",
            message="The Strands provider adapter returned an invalid result.",
        )
    if observation.sdk_version != SUPPORTED_STRANDS_VERSION:
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            sdk_version=observation.sdk_version,
            provider_endpoint=observation.provider_endpoint,
            evidence_source=evidence_source,
            live_provider_invoked=live_provider_invoked,
            error_code="strands_version_mismatch",
            message="The locked Strands provider runtime is unavailable.",
        )
    if live_provider_invoked and not _is_canonical_bedrock_endpoint(
        observation.provider_endpoint, config.region
    ):
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            sdk_version=observation.sdk_version,
            provider_endpoint=observation.provider_endpoint,
            evidence_source=evidence_source,
            live_provider_invoked=True,
            error_code="provider_endpoint_mismatch",
            message="The live provider endpoint did not match the selected AWS region.",
        )

    invocation_count = len(observation.invoked_nonces)
    tool_nonce_matched = (
        invocation_count == 1 and observation.invoked_nonces[0] == controller_nonce
    )
    structured_nonce_matched = observation.structured_nonce == controller_nonce
    nonce_matched = tool_nonce_matched and structured_nonce_matched
    if invocation_count != 1 or not nonce_matched:
        return _result(
            config,
            outcome=Outcome.FAILED,
            sdk_version=observation.sdk_version,
            provider_endpoint=observation.provider_endpoint,
            tool_invocation_count=invocation_count,
            tool_nonce_matched=tool_nonce_matched,
            structured_nonce_matched=structured_nonce_matched,
            nonce_matched=nonce_matched,
            evidence_source=evidence_source,
            live_provider_invoked=live_provider_invoked,
            error_code="probe_contract_failed",
            message="The model did not complete the exact nonce tool-call contract.",
        )

    message = (
        "The configured model completed one live Strands nonce tool call."
        if live_provider_invoked
        else "The injected test adapter satisfied the nonce contract; this is not live evidence."
    )
    return _result(
        config,
        outcome=Outcome.PASSED,
        sdk_version=observation.sdk_version,
        provider_endpoint=observation.provider_endpoint,
        tool_invocation_count=invocation_count,
        tool_nonce_matched=True,
        structured_nonce_matched=True,
        nonce_matched=True,
        evidence_source=evidence_source,
        live_provider_invoked=live_provider_invoked,
        message=message,
    )


def _run_live_provider_isolated(
    config: StrandsPreflightConfig,
    controller_nonce: str,
    *,
    process_context: Any | None = None,
) -> StrandsPreflightResult:
    """Run non-abortable SDK work in a process the controller can terminate."""

    context = process_context or multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_provider_probe_worker,
        args=(sender, config, controller_nonce),
        name="firstrun-strands-preflight",
        daemon=True,
    )
    deadline = time.monotonic() + config.timeout_seconds
    live_provider_invoked = False
    terminal_message: object | None = None
    deadline_reached = False
    started = False
    try:
        process.start()
        started = True
        sender.close()
        while terminal_message is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                deadline_reached = True
                break
            if not receiver.poll(remaining):
                deadline_reached = True
                break
            try:
                message = receiver.recv()
            except EOFError:
                break
            if isinstance(message, dict) and message.get("kind") == "provider_invoked":
                live_provider_invoked = True
                continue
            terminal_message = message
    except (OSError, RuntimeError):
        stopped = not started or _finish_worker(process, terminate=True)
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            evidence_source=_EVIDENCE_LIVE,
            live_provider_invoked=live_provider_invoked,
            error_code=(
                "provider_worker_start_failed"
                if stopped
                else "provider_worker_stop_failed"
            ),
            message=(
                "The isolated Strands provider worker failed."
                if stopped
                else "The isolated Strands provider worker could not be stopped cleanly."
            ),
        )
    except BaseException:
        if started:
            _finish_worker(process, terminate=True)
        raise
    finally:
        receiver.close()
        if not started:
            sender.close()

    timed_out = terminal_message is None and deadline_reached
    clean_exit = _finish_worker(process, terminate=terminal_message is None)
    if not clean_exit:
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            evidence_source=_EVIDENCE_LIVE,
            live_provider_invoked=live_provider_invoked,
            error_code="provider_worker_stop_failed",
            message="The isolated Strands provider worker could not be stopped cleanly.",
        )
    if timed_out:
        return _result(
            config,
            outcome=Outcome.TIMED_OUT,
            evidence_source=_EVIDENCE_LIVE,
            live_provider_invoked=live_provider_invoked,
            error_code="provider_timeout",
            message="The isolated Strands provider probe reached its hard timeout.",
        )
    if terminal_message is None:
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            evidence_source=_EVIDENCE_LIVE,
            live_provider_invoked=live_provider_invoked,
            error_code="provider_worker_no_result",
            message="The isolated Strands provider worker exited without a result.",
        )
    return _result_from_worker_message(
        config,
        controller_nonce,
        terminal_message,
        live_provider_invoked=live_provider_invoked,
    )


def _finish_worker(process: Any, *, terminate: bool) -> bool:
    if terminate and process.is_alive():
        process.terminate()
    process.join(timeout=_WORKER_STOP_GRACE_SECONDS)
    if process.is_alive():
        kill = getattr(process, "kill", None)
        if not callable(kill):
            return False
        kill()
        process.join(timeout=_WORKER_STOP_GRACE_SECONDS)
    if process.is_alive():
        return False
    succeeded = terminate or process.exitcode == 0
    close = getattr(process, "close", None)
    if callable(close):
        close()
    return succeeded


def _provider_probe_worker(
    sender: Any,
    config: StrandsPreflightConfig,
    controller_nonce: str,
) -> None:
    """Child-process entrypoint; sends only bounded, sanitized observations."""

    def mark_provider_invoked() -> None:
        sender.send({"kind": "provider_invoked"})

    try:
        _harden_provider_worker()
        observation = asyncio.run(
            _invoke_with_strands(
                config,
                controller_nonce,
                on_provider_invoke=mark_provider_invoked,
            )
        )
    except _StrandsUnavailable as exc:
        sender.send(
            {
                "kind": "unavailable",
                "error_code": exc.error_code,
                "sdk_version": exc.sdk_version,
            }
        )
    except Exception as exc:
        sender.send(
            {
                "kind": "error",
                "error_code": _classify_provider_exception(exc),
            }
        )
    else:
        sender.send(
            {
                "kind": "observation",
                "sdk_version": observation.sdk_version,
                "invoked_nonces": list(observation.invoked_nonces),
                "structured_nonce": observation.structured_nonce,
                "provider_endpoint": observation.provider_endpoint,
            }
        )
    finally:
        sender.close()


def _harden_provider_worker() -> None:
    """Remove caller-controlled import roots before any credentialed SDK import."""

    original_cwd = Path.cwd().resolve()
    runtime_roots = (
        Path(sys.base_prefix).resolve(),
        Path(sys.prefix).resolve(),
        Path(__file__).resolve().parents[2],
    )
    executable = Path(sys.executable).resolve()
    if not any(_is_within(executable, root) for root in runtime_roots[:2]):
        raise _StrandsUnavailable("provider_worker_isolation_failed", None)

    trusted_paths: list[str] = []
    for raw_path in sys.path:
        if not raw_path:
            continue
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = original_cwd / candidate
        candidate = candidate.resolve()
        if any(_is_within(candidate, root) for root in runtime_roots):
            normalized = str(candidate)
            if normalized not in trusted_paths:
                trusted_paths.append(normalized)
    if not trusted_paths:
        raise _StrandsUnavailable("provider_worker_isolation_failed", None)

    sys.path[:] = trusted_paths
    os.environ.pop("PYTHONPATH", None)
    try:
        os.chdir(runtime_roots[0])
    except OSError as exc:
        raise _StrandsUnavailable("provider_worker_isolation_failed", None) from exc


def _result_from_worker_message(
    config: StrandsPreflightConfig,
    controller_nonce: str,
    message: object,
    *,
    live_provider_invoked: bool,
) -> StrandsPreflightResult:
    if not isinstance(message, dict):
        return _invalid_worker_result(config, live_provider_invoked)
    kind = message.get("kind")
    if kind == "unavailable":
        error_code = message.get("error_code")
        sdk_version = message.get("sdk_version")
        if not _valid_worker_error(error_code) or not _valid_optional_text(sdk_version, 64):
            return _invalid_worker_result(config, live_provider_invoked)
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            sdk_version=sdk_version,
            evidence_source=_EVIDENCE_LIVE,
            live_provider_invoked=live_provider_invoked,
            error_code=error_code,
            message="The locked Strands provider runtime is unavailable.",
        )
    if kind == "error":
        error_code = message.get("error_code")
        if not _valid_worker_error(error_code):
            return _invalid_worker_result(config, live_provider_invoked)
        return _result(
            config,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            evidence_source=_EVIDENCE_LIVE,
            live_provider_invoked=live_provider_invoked,
            error_code=error_code,
            message="The Strands provider invocation failed.",
        )
    if kind != "observation" or set(message) != {
        "kind",
        "sdk_version",
        "invoked_nonces",
        "structured_nonce",
        "provider_endpoint",
    }:
        return _invalid_worker_result(config, live_provider_invoked)

    sdk_version = message.get("sdk_version")
    invoked_nonces = message.get("invoked_nonces")
    structured_nonce = message.get("structured_nonce")
    provider_endpoint = message.get("provider_endpoint")
    if (
        not _valid_optional_text(sdk_version, 64)
        or not isinstance(invoked_nonces, list)
        or len(invoked_nonces) > 64
        or any(not _valid_bounded_text(value, 256) for value in invoked_nonces)
        or not _valid_optional_nonce(structured_nonce)
        or not _valid_optional_text(provider_endpoint, 512)
    ):
        return _invalid_worker_result(config, live_provider_invoked)
    observation = StrandsProbeObservation(
        sdk_version=sdk_version,
        invoked_nonces=tuple(invoked_nonces),
        structured_nonce=structured_nonce,
        provider_endpoint=provider_endpoint,
    )
    return _evaluate_observation(
        config,
        controller_nonce,
        observation,
        evidence_source=_EVIDENCE_LIVE,
        live_provider_invoked=live_provider_invoked,
    )


def _invalid_worker_result(
    config: StrandsPreflightConfig, live_provider_invoked: bool
) -> StrandsPreflightResult:
    return _result(
        config,
        outcome=Outcome.INFRASTRUCTURE_ERROR,
        evidence_source=_EVIDENCE_LIVE,
        live_provider_invoked=live_provider_invoked,
        error_code="invalid_provider_worker_result",
        message="The isolated Strands provider worker returned an invalid result.",
    )


async def _invoke_with_strands(
    config: StrandsPreflightConfig,
    nonce: str,
    *,
    on_provider_invoke: Callable[[], None] | None = None,
) -> StrandsProbeObservation:
    dependencies = _load_strands_dependencies()
    invoked_nonces: list[str] = []

    @dependencies.tool(
        name=NONCE_TOOL_NAME,
        description="Return the supplied FirstRun provider-preflight nonce unchanged.",
    )
    def nonce_round_trip(value: str) -> str:
        """Return a FirstRun preflight nonce unchanged.

        Args:
            value: The exact nonce supplied in the preflight instruction.
        """

        invoked_nonces.append(value)
        return value

    class ProbeOutput(dependencies.base_model_type):
        nonce: str = dependencies.field(
            description="Exact value returned by the FirstRun nonce tool"
        )

    boto_session = dependencies.boto_session_type(
        profile_name=config.aws_profile,
        region_name=config.region,
    )
    boto_config = dependencies.boto_config_type(
        connect_timeout=config.connect_timeout_seconds,
        read_timeout=config.read_timeout_seconds,
        ignore_configured_endpoint_urls=True,
        use_dualstack_endpoint=False,
        use_fips_endpoint=False,
        retries={
            "mode": "standard",
            "total_max_attempts": config.provider_total_attempts,
        },
    )
    model = dependencies.bedrock_model_type(
        model_id=config.model_id,
        boto_session=boto_session,
        streaming=False,
        temperature=0,
        max_tokens=config.max_output_tokens,
        boto_client_config=boto_config,
    )
    provider_endpoint = _validated_bedrock_endpoint(model, config.region)
    agent = dependencies.agent_type(
        model=model,
        tools=[nonce_round_trip],
        system_prompt=(
            "You are a provider preflight. Follow the requested tool protocol "
            "exactly and do not perform unrelated work."
        ),
        callback_handler=None,
        load_tools_from_directory=False,
        retry_strategy=None,
    )
    try:
        if on_provider_invoke is not None:
            on_provider_invoke()
        result = await agent.invoke_async(
            (
                f"Call {NONCE_TOOL_NAME} exactly once with value {nonce!r}. "
                "Then return structured output whose nonce is exactly the tool result."
            ),
            structured_output_model=ProbeOutput,
            limits={
                "turns": config.max_turns,
                "output_tokens": config.max_output_tokens,
                "total_tokens": config.max_total_tokens,
            },
        )
    except dependencies.structured_output_exception_type:
        return StrandsProbeObservation(
            sdk_version=dependencies.sdk_version,
            invoked_nonces=tuple(invoked_nonces),
            structured_nonce=None,
            provider_endpoint=provider_endpoint,
        )
    structured_output = getattr(result, "structured_output", None)
    structured_nonce = getattr(structured_output, "nonce", None)
    if not isinstance(structured_nonce, str):
        structured_nonce = None
    return StrandsProbeObservation(
        sdk_version=dependencies.sdk_version,
        invoked_nonces=tuple(invoked_nonces),
        structured_nonce=structured_nonce,
        provider_endpoint=provider_endpoint,
    )


def _validated_bedrock_endpoint(model: object, region: str) -> str:
    """Reconcile the SDK client with the controller-selected AWS endpoint."""

    client = getattr(model, "client", None)
    metadata_value = getattr(client, "meta", None)
    resolved_region = getattr(metadata_value, "region_name", None)
    endpoint = getattr(metadata_value, "endpoint_url", None)
    if resolved_region != region or not isinstance(endpoint, str):
        raise ValueError("Bedrock client did not preserve the selected region and endpoint")

    if not _is_canonical_bedrock_endpoint(endpoint, region):
        raise ValueError("Bedrock client resolved a non-canonical endpoint")
    return endpoint


def _is_canonical_bedrock_endpoint(endpoint: object, region: str) -> bool:
    if not isinstance(endpoint, str):
        return False
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError:
        return False
    approved_suffixes = (
        "amazonaws.com",
        "amazonaws.com.cn",
        "c2s.ic.gov",
        "sc2s.sgov.gov",
        "cloud.adc-e.uk",
        "csp.hci.ic.gov",
    )
    expected_hosts = {
        f"bedrock-runtime.{region}.{suffix}" for suffix in approved_suffixes
    }
    return not (
        parsed.scheme != "https"
        or parsed.hostname not in expected_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    )


def _load_strands_dependencies() -> _StrandsDependencies:
    trusted_distribution_files: dict[str, frozenset[str]] = {}
    sdk_version: str | None = None
    for import_name, (distribution_name, required_version) in _LOCKED_DISTRIBUTIONS.items():
        try:
            installed_version = metadata.version(distribution_name)
        except metadata.PackageNotFoundError as exc:
            raise _StrandsUnavailable("strands_dependency_missing", sdk_version) from exc
        if import_name == "strands":
            sdk_version = installed_version
        if installed_version != required_version:
            error_code = (
                "strands_version_mismatch"
                if import_name == "strands"
                else "provider_dependency_version_mismatch"
            )
            raise _StrandsUnavailable(error_code, sdk_version)
        trusted_distribution_files[import_name] = _verify_import_origin(
            import_name,
            distribution_name,
            sdk_version,
        )
    if sdk_version is None:
        raise _StrandsUnavailable("strands_dependency_missing", None)

    try:
        import boto3
        import botocore
        import pydantic
        import strands
        from botocore.config import Config as BotoConfig
        from pydantic import BaseModel, Field
        from strands import Agent, tool
        from strands.models import BedrockModel
        from strands.types.exceptions import StructuredOutputException
    except ImportError as exc:
        raise _StrandsUnavailable("strands_dependency_missing", sdk_version) from exc

    for import_name, imported_module in (
        ("strands", strands),
        ("boto3", boto3),
        ("botocore", botocore),
        ("pydantic", pydantic),
    ):
        module_file = getattr(imported_module, "__file__", None)
        if (
            not isinstance(module_file, str)
            or _normalized_path(module_file) not in trusted_distribution_files[import_name]
        ):
            raise _StrandsUnavailable("untrusted_dependency_origin", sdk_version)

    return _StrandsDependencies(
        agent_type=Agent,
        bedrock_model_type=BedrockModel,
        boto_session_type=boto3.Session,
        boto_config_type=BotoConfig,
        base_model_type=BaseModel,
        field=Field,
        tool=tool,
        structured_output_exception_type=StructuredOutputException,
        sdk_version=sdk_version,
    )


def _verify_import_origin(
    import_name: str,
    distribution_name: str,
    sdk_version: str | None,
) -> frozenset[str]:
    try:
        distribution = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError as exc:
        raise _StrandsUnavailable("strands_dependency_missing", sdk_version) from exc
    files = distribution.files
    if files is None:
        raise _StrandsUnavailable("untrusted_dependency_origin", sdk_version)
    trusted_files = frozenset(
        _normalized_path(distribution.locate_file(path)) for path in files
    )
    spec = importlib.util.find_spec(import_name)
    if (
        spec is None
        or not isinstance(spec.origin, str)
        or _normalized_path(spec.origin) not in trusted_files
    ):
        raise _StrandsUnavailable("untrusted_dependency_origin", sdk_version)
    return trusted_files


def _normalized_path(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _result(
    config: StrandsPreflightConfig,
    *,
    outcome: Outcome,
    sdk_version: str | None = None,
    provider_endpoint: str | None = None,
    tool_invocation_count: int = 0,
    tool_nonce_matched: bool = False,
    structured_nonce_matched: bool = False,
    nonce_matched: bool = False,
    evidence_source: str = _EVIDENCE_CONTROLLER,
    live_provider_invoked: bool = False,
    error_code: str | None = None,
    message: str,
) -> StrandsPreflightResult:
    return StrandsPreflightResult(
        outcome=outcome,
        provider=PROVIDER_ID,
        aws_profile=config.aws_profile,
        region=config.region,
        model_id=config.model_id,
        sdk_version=sdk_version,
        provider_endpoint=provider_endpoint,
        tool_invocation_count=tool_invocation_count,
        tool_nonce_matched=tool_nonce_matched,
        structured_nonce_matched=structured_nonce_matched,
        nonce_matched=nonce_matched,
        timeout_seconds=config.timeout_seconds,
        max_turns=config.max_turns,
        max_output_tokens=config.max_output_tokens,
        max_total_tokens=config.max_total_tokens,
        provider_total_attempts=config.provider_total_attempts,
        strands_model_attempts_per_turn=1,
        provider_cost_acknowledged=config.provider_cost_acknowledged,
        credential_identity_verified=config.credential_identity_verified,
        isolated_python=bool(sys.flags.isolated),
        evidence_source=evidence_source,
        live_provider_invoked=live_provider_invoked,
        milestone_eligible=(
            live_provider_invoked
            and outcome is Outcome.PASSED
            and config.provider_cost_acknowledged
            and config.credential_identity_verified
            and bool(sys.flags.isolated)
        ),
        error_code=error_code,
        message=message,
    )


def _new_nonce() -> str:
    return secrets.token_urlsafe(24)


def _valid_controller_nonce(value: object) -> bool:
    return isinstance(value, str) and _NONCE.fullmatch(value) is not None


def _valid_worker_error(value: object) -> bool:
    return isinstance(value, str) and _SAFE_ERROR_CODE.fullmatch(value) is not None


def _valid_bounded_text(value: object, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and all(character.isprintable() for character in value)
    )


def _valid_optional_text(value: object, max_length: int) -> bool:
    return value is None or _valid_bounded_text(value, max_length)


def _valid_optional_nonce(value: object) -> bool:
    return value is None or _valid_bounded_text(value, 256)


def _classify_provider_exception(error: Exception) -> str:
    exception_codes = {
        "ProfileNotFound": "aws_profile_not_found",
        "ConfigNotFound": "aws_config_not_found",
        "NoCredentialsError": "aws_credentials_not_found",
        "PartialCredentialsError": "aws_credentials_incomplete",
        "CredentialRetrievalError": "aws_credential_retrieval_failed",
        "UnknownRegionError": "aws_region_unknown",
        "InvalidRegionError": "aws_region_invalid",
        "NoRegionError": "aws_region_missing",
        "EndpointConnectionError": "provider_endpoint_unreachable",
        "ConnectTimeoutError": "provider_connect_timeout",
        "ReadTimeoutError": "provider_read_timeout",
        "SSLError": "provider_tls_error",
        "ProxyConnectionError": "provider_proxy_error",
        "ModelThrottledException": "provider_throttled",
        "ContextWindowOverflowException": "provider_model_context_rejected",
    }
    aws_codes = {
        "accessdeniedexception": "provider_access_denied",
        "unauthorizedexception": "provider_access_denied",
        "unrecognizedclientexception": "aws_credentials_rejected",
        "invalidsignatureexception": "aws_credentials_rejected",
        "expiredtokenexception": "aws_credentials_expired",
        "resourcenotfoundexception": "provider_model_not_found",
        "validationexception": "provider_configuration_invalid",
        "throttlingexception": "provider_throttled",
        "toomanyrequestsexception": "provider_throttled",
        "serviceunavailableexception": "provider_service_unavailable",
        "internalserverexception": "provider_service_unavailable",
        "modelnotreadyexception": "provider_model_not_ready",
        "modeltimeoutexception": "provider_model_timeout",
    }

    for candidate in _exception_chain(error):
        classified = exception_codes.get(type(candidate).__name__)
        if classified is not None:
            return classified
        response = getattr(candidate, "response", None)
        if isinstance(response, dict):
            error_record = response.get("Error")
            if isinstance(error_record, dict):
                code = error_record.get("Code")
                if isinstance(code, str):
                    classified = aws_codes.get(code.lower())
                    if classified is not None:
                        return classified
        if isinstance(candidate, ValueError):
            return "provider_configuration_invalid"
    return "provider_invocation_error"


def _exception_chain(error: Exception) -> tuple[Exception, ...]:
    found: list[Exception] = []
    seen_ids: set[int] = set()
    current: Exception | None = error
    while current is not None and len(found) < 8 and id(current) not in seen_ids:
        seen_ids.add(id(current))
        found.append(current)
        original = getattr(current, "original_exception", None)
        if isinstance(original, Exception):
            current = original
        elif isinstance(current.__cause__, Exception):
            current = current.__cause__
        elif isinstance(current.__context__, Exception):
            current = current.__context__
        else:
            current = None
    return tuple(found)


def _require_explicit_text(value: object, name: str, *, max_length: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be an explicit non-empty string")
    if len(value) > max_length or any(not character.isprintable() for character in value):
        raise ValueError(f"{name} contains unsupported characters or is too long")


def _require_range(value: object, name: str, *, upper: float | int) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(float(value)) or value <= 0 or value > upper:
        raise ValueError(f"{name} must be greater than zero and at most {upper}")


def _require_positive_int(value: object, name: str, *, upper: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value <= 0 or value > upper:
        raise ValueError(f"{name} must be greater than zero and at most {upper}")
