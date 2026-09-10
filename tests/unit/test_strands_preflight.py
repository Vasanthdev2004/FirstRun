from __future__ import annotations

import asyncio
import math
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from firstrun.domain.outcomes import Outcome
from firstrun.preflight.strands import (
    NONCE_TOOL_NAME,
    SUPPORTED_STRANDS_VERSION,
    StrandsPreflightConfig,
    StrandsProbeObservation,
    _StrandsDependencies,
    _StrandsUnavailable,
    _harden_provider_worker,
    _invoke_with_strands,
    _load_strands_dependencies,
    _provider_probe_worker,
    _run_live_provider_isolated,
    run_strands_preflight,
)


NONCE = "controller_nonce_0123456789"
CANONICAL_ENDPOINT = "https://bedrock-runtime.us-west-2.amazonaws.com"


class _FakePipeEnd:
    def __init__(self, messages: list[object], *, block_after_messages: bool) -> None:
        self.messages = messages
        self.block_after_messages = block_after_messages
        self.closed = False

    def poll(self, timeout: float) -> bool:
        if self.messages:
            return True
        if self.block_after_messages:
            time.sleep(timeout)
        return False

    def recv(self) -> object:
        return self.messages.pop(0)

    def send(self, _message: object) -> None:
        raise AssertionError("the fake child sender must never execute in the parent")

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, *, blocking: bool) -> None:
        self.blocking = blocking
        self.started = False
        self.alive = False
        self.exitcode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def start(self) -> None:
        self.started = True
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, *, timeout: float) -> None:
        del timeout
        if not self.blocking:
            self.alive = False
            self.exitcode = 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.alive = False
        self.exitcode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.alive = False
        self.exitcode = -9


class _FakeProcessContext:
    def __init__(self, messages: list[object], *, blocking: bool) -> None:
        self.receiver = _FakePipeEnd(messages, block_after_messages=blocking)
        self.sender = _FakePipeEnd([], block_after_messages=False)
        self.process = _FakeProcess(blocking=blocking)
        self.process_options: dict[str, object] | None = None

    def Pipe(self, *, duplex: bool):
        if duplex:
            raise AssertionError("preflight IPC must be one-way")
        return self.receiver, self.sender

    def Process(self, **options: object) -> _FakeProcess:
        self.process_options = options
        return self.process


class _CapturingSender:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.closed = False

    def send(self, message: object) -> None:
        self.messages.append(message)

    def close(self) -> None:
        self.closed = True


def config(**changes: object) -> StrandsPreflightConfig:
    values: dict[str, object] = {
        "aws_profile": "firstrun-dev",
        "region": "us-west-2",
        "model_id": "us.example.tool-model-v1:0",
    }
    values.update(changes)
    return StrandsPreflightConfig(**values)  # type: ignore[arg-type]


def live_config(**changes: object) -> StrandsPreflightConfig:
    return config(
        provider_cost_acknowledged=True,
        credential_identity_verified=True,
        **changes,
    )


class StrandsPreflightTests(unittest.TestCase):
    def test_provider_worker_removes_caller_controlled_import_roots(self) -> None:
        repository = Path.cwd().resolve()
        test_paths = ["", str(repository), str(repository / "attacker"), *sys.path]

        with (
            patch.object(sys, "path", test_paths),
            patch("firstrun.preflight.strands.os.chdir") as chdir,
        ):
            _harden_provider_worker()
            hardened = tuple(Path(entry).resolve() for entry in sys.path)

        self.assertNotIn(repository, hardened)
        self.assertFalse(any(path == repository / "attacker" for path in hardened))
        self.assertTrue(all(path.is_absolute() for path in hardened))
        chdir.assert_called_once_with(Path(sys.base_prefix).resolve())

    def test_locked_provider_sdk_imports_from_verified_distributions(self) -> None:
        dependencies = _load_strands_dependencies()

        self.assertEqual(SUPPORTED_STRANDS_VERSION, dependencies.sdk_version)
        self.assertEqual("Agent", dependencies.agent_type.__name__)
        self.assertEqual("BedrockModel", dependencies.bedrock_model_type.__name__)

    def test_pass_requires_one_tool_call_and_both_nonce_matches(self) -> None:
        async def invoke(
            selected: StrandsPreflightConfig, nonce: str
        ) -> StrandsProbeObservation:
            self.assertEqual("firstrun-dev", selected.aws_profile)
            return StrandsProbeObservation(
                sdk_version=SUPPORTED_STRANDS_VERSION,
                invoked_nonces=(nonce,),
                structured_nonce=nonce,
            )

        result = run_strands_preflight(
            config(), invoker=invoke, nonce_factory=lambda: NONCE
        )

        self.assertEqual(Outcome.PASSED, result.outcome)
        self.assertEqual("amazon-bedrock", result.provider)
        self.assertEqual("us-west-2", result.region)
        self.assertEqual("us.example.tool-model-v1:0", result.model_id)
        self.assertEqual(SUPPORTED_STRANDS_VERSION, result.sdk_version)
        self.assertEqual(1, result.tool_invocation_count)
        self.assertTrue(result.tool_nonce_matched)
        self.assertTrue(result.structured_nonce_matched)
        self.assertTrue(result.nonce_matched)
        self.assertEqual("injected_test_adapter", result.evidence_source)
        self.assertFalse(result.live_provider_invoked)
        self.assertFalse(result.milestone_eligible)
        self.assertIn("not live evidence", result.message)

    def test_zero_duplicate_wrong_and_unstructured_calls_fail(self) -> None:
        cases = (
            ((), NONCE),
            ((NONCE, NONCE), NONCE),
            (("wrong-nonce",), NONCE),
            ((NONCE,), None),
        )
        for invoked, structured in cases:
            with self.subTest(invoked=invoked, structured=structured):
                async def invoke(
                    _selected: StrandsPreflightConfig,
                    _nonce: str,
                    calls: tuple[str, ...] = invoked,
                    output: str | None = structured,
                ) -> StrandsProbeObservation:
                    return StrandsProbeObservation(
                        sdk_version=SUPPORTED_STRANDS_VERSION,
                        invoked_nonces=calls,
                        structured_nonce=output,
                    )

                result = run_strands_preflight(
                    config(), invoker=invoke, nonce_factory=lambda: NONCE
                )

                self.assertEqual(Outcome.FAILED, result.outcome)
                self.assertEqual("probe_contract_failed", result.error_code)
                self.assertFalse(result.nonce_matched)

    def test_provider_error_is_sanitized_infrastructure_error(self) -> None:
        async def invoke(
            _selected: StrandsPreflightConfig, _nonce: str
        ) -> StrandsProbeObservation:
            raise RuntimeError("secret-access-key=do-not-leak")

        result = run_strands_preflight(
            config(), invoker=invoke, nonce_factory=lambda: NONCE
        )

        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertEqual("provider_invocation_error", result.error_code)
        self.assertNotIn("secret-access-key", repr(result))
        self.assertNotIn("do-not-leak", repr(result))

    def test_provider_error_uses_sanitized_reproducible_classification(self) -> None:
        profile_error_type = type("ProfileNotFound", (Exception,), {})
        client_error_type = type("ClientError", (Exception,), {})

        profile_error = profile_error_type("profile name and secret details")
        access_error = client_error_type("request/account/secret details")
        access_error.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "AccessDeniedException", "Message": "do-not-leak"},
            "ResponseMetadata": {"RequestId": "private-request-id"},
        }
        wrapped_access_error = RuntimeError("wrapper details")
        wrapped_access_error.original_exception = access_error  # type: ignore[attr-defined]

        for error, expected_code in (
            (profile_error, "aws_profile_not_found"),
            (wrapped_access_error, "provider_access_denied"),
        ):
            with self.subTest(expected_code=expected_code):
                async def invoke(
                    _selected: StrandsPreflightConfig,
                    _nonce: str,
                    selected_error: Exception = error,
                ) -> StrandsProbeObservation:
                    raise selected_error

                result = run_strands_preflight(
                    config(), invoker=invoke, nonce_factory=lambda: NONCE
                )

                self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
                self.assertEqual(expected_code, result.error_code)
                self.assertNotIn("do-not-leak", repr(result))
                self.assertNotIn("private-request-id", repr(result))

    def test_timeout_is_distinct_and_bounded(self) -> None:
        async def invoke(
            _selected: StrandsPreflightConfig, _nonce: str
        ) -> StrandsProbeObservation:
            await asyncio.sleep(1)
            raise AssertionError("unreachable")

        result = run_strands_preflight(
            config(
                timeout_seconds=0.01,
                connect_timeout_seconds=0.005,
                read_timeout_seconds=0.005,
            ),
            invoker=invoke,
            nonce_factory=lambda: NONCE,
        )

        self.assertEqual(Outcome.TIMED_OUT, result.outcome)
        self.assertEqual("injected_adapter_timeout", result.error_code)
        self.assertFalse(result.milestone_eligible)

    def test_live_worker_pass_is_distinct_milestone_evidence(self) -> None:
        context = _FakeProcessContext(
            [
                {"kind": "provider_invoked"},
                {
                    "kind": "observation",
                    "sdk_version": SUPPORTED_STRANDS_VERSION,
                    "invoked_nonces": [NONCE],
                    "structured_nonce": NONCE,
                    "provider_endpoint": CANONICAL_ENDPOINT,
                },
            ],
            blocking=False,
        )

        with patch(
            "firstrun.preflight.strands.sys.flags",
            SimpleNamespace(isolated=1),
        ):
            result = _run_live_provider_isolated(
                live_config(), NONCE, process_context=context
            )

        self.assertEqual(Outcome.PASSED, result.outcome)
        self.assertEqual("live_provider_subprocess", result.evidence_source)
        self.assertTrue(result.live_provider_invoked)
        self.assertTrue(result.milestone_eligible)
        self.assertEqual(CANONICAL_ENDPOINT, result.provider_endpoint)
        self.assertTrue(context.process.started)
        self.assertEqual(0, context.process.terminate_calls)
        self.assertTrue(context.process_options["daemon"])  # type: ignore[index]

    def test_live_worker_hard_timeout_terminates_blocking_process(self) -> None:
        context = _FakeProcessContext(
            [{"kind": "provider_invoked"}],
            blocking=True,
        )
        selected = live_config(
            timeout_seconds=0.01,
            connect_timeout_seconds=0.005,
            read_timeout_seconds=0.005,
        )
        started = time.monotonic()

        with patch(
            "firstrun.preflight.strands.sys.flags",
            SimpleNamespace(isolated=1),
        ):
            result = _run_live_provider_isolated(
                selected, NONCE, process_context=context
            )

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(Outcome.TIMED_OUT, result.outcome)
        self.assertEqual("provider_timeout", result.error_code)
        self.assertTrue(result.live_provider_invoked)
        self.assertFalse(result.milestone_eligible)
        self.assertEqual(1, context.process.terminate_calls)
        self.assertFalse(context.process.is_alive())

    def test_live_evidence_rejects_a_configured_endpoint_override(self) -> None:
        context = _FakeProcessContext(
            [
                {"kind": "provider_invoked"},
                {
                    "kind": "observation",
                    "sdk_version": SUPPORTED_STRANDS_VERSION,
                    "invoked_nonces": [NONCE],
                    "structured_nonce": NONCE,
                    "provider_endpoint": "https://localhost:9000",
                },
            ],
            blocking=False,
        )

        with patch(
            "firstrun.preflight.strands.sys.flags",
            SimpleNamespace(isolated=1),
        ):
            result = _run_live_provider_isolated(
                live_config(), NONCE, process_context=context
            )

        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertEqual("provider_endpoint_mismatch", result.error_code)
        self.assertFalse(result.milestone_eligible)

    def test_worker_never_serializes_provider_exception_details(self) -> None:
        async def fail(
            _selected: StrandsPreflightConfig,
            _nonce: str,
            *,
            on_provider_invoke,
        ) -> StrandsProbeObservation:
            on_provider_invoke()
            raise RuntimeError("secret-access-key=do-not-cross-ipc")

        sender = _CapturingSender()
        with (
            patch("firstrun.preflight.strands._harden_provider_worker"),
            patch("firstrun.preflight.strands._invoke_with_strands", new=fail),
        ):
            _provider_probe_worker(sender, config(), NONCE)

        self.assertTrue(sender.closed)
        self.assertEqual({"kind": "provider_invoked"}, sender.messages[0])
        self.assertEqual(
            {"kind": "error", "error_code": "provider_invocation_error"},
            sender.messages[1],
        )
        self.assertNotIn("secret-access-key", repr(sender.messages))

    def test_missing_or_mismatched_sdk_is_infrastructure_error(self) -> None:
        for code, version in (
            ("strands_dependency_missing", None),
            ("strands_version_mismatch", "99.0.0"),
        ):
            with self.subTest(code=code):
                async def invoke(
                    _selected: StrandsPreflightConfig,
                    _nonce: str,
                    error_code: str = code,
                    sdk_version: str | None = version,
                ) -> StrandsProbeObservation:
                    raise _StrandsUnavailable(error_code, sdk_version)

                result = run_strands_preflight(
                    config(), invoker=invoke, nonce_factory=lambda: NONCE
                )

                self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
                self.assertEqual(code, result.error_code)
                self.assertEqual(version, result.sdk_version)

        async def wrong_version_observation(
            _selected: StrandsPreflightConfig, nonce: str
        ) -> StrandsProbeObservation:
            return StrandsProbeObservation(
                sdk_version="99.0.0",
                invoked_nonces=(nonce,),
                structured_nonce=nonce,
            )

        result = run_strands_preflight(
            config(),
            invoker=wrong_version_observation,
            nonce_factory=lambda: NONCE,
        )
        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertEqual("strands_version_mismatch", result.error_code)

    def test_explicit_provider_fields_and_budgets_are_required(self) -> None:
        for field in ("aws_profile", "region", "model_id"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                config(**{field: ""})
        for field, value in (
            ("region", "https://example.invalid"),
            ("timeout_seconds", 121),
            ("max_turns", 5),
            ("max_output_tokens", 513),
            ("max_total_tokens", 2049),
            ("provider_total_attempts", 3),
            ("max_turns", 1.5),
            ("max_output_tokens", 32.5),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                config(**{field: value})
        with self.assertRaises(ValueError):
            config(timeout_seconds=math.nan)
        with self.assertRaises(ValueError):
            config(provider_cost_acknowledged=1)
        with self.assertRaises(ValueError):
            config(credential_identity_verified="yes")

    def test_invalid_controller_nonce_stops_before_provider_invocation(self) -> None:
        invoked = False

        async def invoke(
            _selected: StrandsPreflightConfig, _nonce: str
        ) -> StrandsProbeObservation:
            nonlocal invoked
            invoked = True
            raise AssertionError("provider must not be invoked")

        result = run_strands_preflight(
            config(), invoker=invoke, nonce_factory=lambda: "invalid nonce\nvalue"
        )

        self.assertFalse(invoked)
        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertEqual("invalid_controller_nonce", result.error_code)

    def test_live_entrypoint_requires_cost_and_verified_identity_prerequisites(self) -> None:
        with patch("firstrun.preflight.strands._run_live_provider_isolated") as live:
            missing_cost = run_strands_preflight(config())
            missing_identity = run_strands_preflight(
                config(provider_cost_acknowledged=True)
            )
            missing_isolation = run_strands_preflight(live_config())

        self.assertEqual(Outcome.POLICY_BLOCKED, missing_cost.outcome)
        self.assertEqual("provider_cost_not_acknowledged", missing_cost.error_code)
        self.assertEqual(Outcome.POLICY_BLOCKED, missing_identity.outcome)
        self.assertEqual(
            "credential_identity_not_verified", missing_identity.error_code
        )
        self.assertEqual(Outcome.POLICY_BLOCKED, missing_isolation.outcome)
        self.assertEqual("isolated_python_required", missing_isolation.error_code)
        live.assert_not_called()

    def test_default_adapter_wires_explicit_bedrock_and_current_async_api(self) -> None:
        calls: dict[str, object] = {}

        class FakeBaseModel:
            def __init__(self, **values: object) -> None:
                self.__dict__.update(values)

        def fake_field(**_kwargs: object) -> None:
            return None

        def fake_tool(**options: object):
            calls["tool_options"] = options

            def decorate(function):
                return function

            return decorate

        def fake_session(**options: object) -> object:
            calls["session_options"] = options
            return "SESSION"

        def fake_boto_config(**options: object) -> object:
            calls["boto_config_options"] = options
            return "BOTO_CONFIG"

        def fake_model(**options: object) -> object:
            if options.get("boto_session") is not None and options.get("region_name") is not None:
                raise AssertionError("BedrockModel rejects boto_session plus region_name")
            calls["model_options"] = options
            return SimpleNamespace(
                client=SimpleNamespace(
                    meta=SimpleNamespace(
                        region_name="us-west-2",
                        endpoint_url=CANONICAL_ENDPOINT,
                    )
                )
            )

        class FakeAgent:
            def __init__(self, **options: object) -> None:
                calls["agent_options"] = options
                self.tool = options["tools"][0]  # type: ignore[index]

            async def invoke_async(self, prompt: str, **options: object) -> object:
                calls["prompt"] = prompt
                calls["invoke_options"] = options
                tool_result = self.tool(NONCE)
                output_type = options["structured_output_model"]
                return SimpleNamespace(
                    structured_output=output_type(nonce=tool_result)
                )

        dependencies = _StrandsDependencies(
            agent_type=FakeAgent,
            bedrock_model_type=fake_model,
            boto_session_type=fake_session,
            boto_config_type=fake_boto_config,
            base_model_type=FakeBaseModel,
            field=fake_field,
            tool=fake_tool,
            structured_output_exception_type=RuntimeError,
            sdk_version=SUPPORTED_STRANDS_VERSION,
        )

        with patch(
            "firstrun.preflight.strands._load_strands_dependencies",
            return_value=dependencies,
        ):
            observation = asyncio.run(_invoke_with_strands(config(), NONCE))

        self.assertEqual((NONCE,), observation.invoked_nonces)
        self.assertEqual(NONCE, observation.structured_nonce)
        self.assertEqual(CANONICAL_ENDPOINT, observation.provider_endpoint)
        self.assertEqual(
            {"profile_name": "firstrun-dev", "region_name": "us-west-2"},
            calls["session_options"],
        )
        self.assertEqual(
            {
                "connect_timeout": 5.0,
                "read_timeout": 30.0,
                "ignore_configured_endpoint_urls": True,
                "use_dualstack_endpoint": False,
                "use_fips_endpoint": False,
                "retries": {"mode": "standard", "total_max_attempts": 2},
            },
            calls["boto_config_options"],
        )
        self.assertEqual(
            {
                "model_id": "us.example.tool-model-v1:0",
                "boto_session": "SESSION",
                "streaming": False,
                "temperature": 0,
                "max_tokens": 256,
                "boto_client_config": "BOTO_CONFIG",
            },
            calls["model_options"],
        )
        self.assertEqual(
            {"turns": 4, "output_tokens": 256, "total_tokens": 1024},
            calls["invoke_options"]["limits"],  # type: ignore[index]
        )
        self.assertIn("structured_output_model", calls["invoke_options"])
        self.assertEqual(NONCE_TOOL_NAME, calls["tool_options"]["name"])  # type: ignore[index]
        self.assertIsNone(calls["agent_options"]["callback_handler"])  # type: ignore[index]
        self.assertFalse(calls["agent_options"]["load_tools_from_directory"])  # type: ignore[index]
        self.assertIsNone(calls["agent_options"]["retry_strategy"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
