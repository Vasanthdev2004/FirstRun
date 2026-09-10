"""Command-line entry points for trusted local FirstRun operations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError

from firstrun.doctor import run_doctor
from firstrun.domain import Outcome, exit_code_for
from firstrun.domain.repair import RepairProviderConfig
from firstrun.orchestration.repair import repair_local
from firstrun.preflight.docker import DockerPreflightConfig, run_docker_preflight
from firstrun.preflight.strands import StrandsPreflightConfig, run_strands_preflight
from firstrun.verification.local import verify_local


M0_RUNTIME_IMAGE = "node:22-bookworm-slim"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firstrun")
    subcommands = parser.add_subparsers(dest="command", required=True)
    from firstrun.github_cli import add_commands
    add_commands(subcommands)

    doctor = subcommands.add_parser(
        "doctor", help="inspect M0 prerequisites without changing system state"
    )
    doctor.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="repository working tree to inspect (default: current directory)",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="emit the complete machine-readable diagnostic result",
    )

    docker_preflight = subcommands.add_parser(
        "preflight-docker",
        help="exercise the trusted M0 Docker isolation topology",
    )
    docker_preflight.add_argument(
        "--acknowledge-container-changes",
        action="store_true",
        help="allow creation and exact-ID cleanup of labelled throwaway containers",
    )
    docker_preflight.add_argument(
        "--allow-pull",
        action="store_true",
        help="allow Docker to pull the controller-selected runtime image",
    )
    docker_preflight.add_argument(
        "--json",
        action="store_true",
        help="emit the complete machine-readable preflight result",
    )

    strands_preflight = subcommands.add_parser(
        "preflight-strands",
        help="make one bounded Strands tool-call probe through Amazon Bedrock",
    )
    strands_preflight.add_argument("--aws-profile", required=True)
    strands_preflight.add_argument("--region", required=True)
    strands_preflight.add_argument("--model-id", required=True)
    strands_preflight.add_argument(
        "--acknowledge-provider-cost",
        action="store_true",
        help=(
            "allow one bounded probe that may make multiple model requests and "
            "incur AWS charges"
        ),
    )
    strands_preflight.add_argument(
        "--confirm-verified-temporary-non-root-credentials",
        action="store_true",
        help=(
            "confirm the named profile's current AWS identity was independently "
            "verified as temporary and non-root"
        ),
    )
    strands_preflight.add_argument(
        "--json",
        action="store_true",
        help="emit the complete machine-readable preflight result",
    )

    verify = subcommands.add_parser(
        "verify-local",
        help="verify the one controller-approved M1 fixture in isolated Docker",
    )
    verify.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="exact path of the controller-approved fixture repository",
    )
    verify.add_argument(
        "--target",
        type=Path,
        required=True,
        help="exact <repo>/.firstrun/target.json path",
    )
    verify.add_argument(
        "--json",
        action="store_true",
        help="emit complete typed run evidence",
    )

    repair = subcommands.add_parser(
        "repair-local",
        help="investigate and prove an authorized recipe repair with Strands",
    )
    repair.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="exact path of the controller-approved fixture repository",
    )
    repair.add_argument("--aws-profile", required=True)
    repair.add_argument("--region", required=True)
    repair.add_argument("--model-id", required=True)
    repair.add_argument(
        "--acknowledge-provider-cost",
        action="store_true",
        help="allow bounded Strands requests that may incur AWS charges",
    )
    repair.add_argument(
        "--confirm-verified-temporary-non-root-credentials",
        action="store_true",
        help=(
            "confirm the named profile's current AWS identity was independently "
            "verified as temporary and non-root"
        ),
    )
    repair.add_argument(
        "--json",
        action="store_true",
        help="emit complete typed repair, investigation, and proof evidence",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "github_command", False):
        from firstrun.github_cli import run_command
        return run_command(args)
    if args.command == "doctor":
        result = run_doctor(args.repo)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"FirstRun doctor: {result['outcome']}")
            checks = result["checks"]
            if isinstance(checks, dict):
                for check_id, check in checks.items():
                    if not isinstance(check, dict):
                        continue
                    print(
                        f"- {check_id}: {check.get('status', 'unknown')}"
                        f" — {check.get('message', '')}"
                    )
        return int(result["exit_code"])

    if args.command == "preflight-docker":
        if not args.acknowledge_container_changes:
            return _print_preflight_policy_block(args.json)

        result = run_docker_preflight(
            DockerPreflightConfig(
                app_image=M0_RUNTIME_IMAGE,
                verifier_image=M0_RUNTIME_IMAGE,
                allow_pull=args.allow_pull,
            )
        )
        payload = asdict(result)
        payload["outcome"] = result.outcome.value
        payload["exit_code"] = exit_code_for(result.outcome)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"FirstRun Docker preflight: {result.outcome.value}")
            for check in result.checks:
                print(f"- passed: {check}")
            for error in result.errors:
                print(f"- error: {error}")
            for error in result.cleanup_errors:
                print(f"- cleanup error: {error}")
        return exit_code_for(result.outcome)

    if args.command == "preflight-strands":
        if (
            not args.acknowledge_provider_cost
            or not args.confirm_verified_temporary_non_root_credentials
        ):
            return _print_strands_policy_block(
                args.json,
                provider_cost_acknowledged=args.acknowledge_provider_cost,
                credential_identity_verified=(
                    args.confirm_verified_temporary_non_root_credentials
                ),
            )

        result = run_strands_preflight(
            StrandsPreflightConfig(
                aws_profile=args.aws_profile,
                region=args.region,
                model_id=args.model_id,
                provider_cost_acknowledged=True,
                credential_identity_verified=True,
            )
        )
        payload = asdict(result)
        payload["outcome"] = result.outcome.value
        payload["exit_code"] = exit_code_for(result.outcome)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"FirstRun Strands preflight: {result.outcome.value}")
            print(f"- provider: {result.provider}")
            print(f"- model: {result.model_id} ({result.region})")
            print(f"- result: {result.message}")
        return exit_code_for(result.outcome)

    if args.command == "verify-local":
        result = verify_local(args.repo, args.target)
        if args.json:
            payload = result.to_dict()
            payload["exit_code"] = exit_code_for(result.outcome)
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"FirstRun local verification: {result.outcome.value}")
            if result.source_revision:
                print(f"- source revision: {result.source_revision}")
            if result.baseline is not None:
                print(f"- run ID: {result.baseline.run_id}")
                print(f"- readiness: {result.baseline.readiness.outcome.value}")
                print(
                    "- functional acceptance: "
                    f"{result.baseline.acceptance_probe.outcome.value}"
                )
                print(f"- cleanup: {'passed' if result.baseline.cleanup.succeeded else 'failed'}")
            elif result.baseline_attempt is not None:
                print(f"- attempt ID: {result.baseline_attempt.attempt_id}")
                print(f"- attempt outcome: {result.baseline_attempt.outcome.value}")
                print(
                    "- cleanup: "
                    f"{'passed' if result.baseline_attempt.cleanup.succeeded else 'failed'}"
                )
            if result.message:
                print(f"- result: {result.message}")
        return exit_code_for(result.outcome)

    if args.command == "repair-local":
        try:
            provider_config = RepairProviderConfig(
                aws_profile=args.aws_profile,
                region=args.region,
                model_id=args.model_id,
                provider_cost_acknowledged=args.acknowledge_provider_cost,
                credential_identity_verified=(
                    args.confirm_verified_temporary_non_root_credentials
                ),
            )
        except ValidationError as exc:
            fields = sorted(
                {
                    str(error["loc"][0])
                    for error in exc.errors(include_url=False, include_input=False)
                    if error.get("loc")
                }
            )
            return _print_repair_config_block(args.json, fields)
        result = repair_local(args.repo, provider_config)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(
                "FirstRun local repair: "
                f"{result.state.value} ({result.outcome.value})"
            )
            if result.source_revision:
                print(f"- source revision: {result.source_revision}")
            if result.agent is not None:
                print(f"- agent attempts: {result.agent.attempts_used}")
                print(f"- tool calls: {len(result.agent.tool_calls)}")
            if result.candidate is not None:
                print(f"- candidate: {result.candidate.patch_digest}")
                print(f"- candidate tree: {result.candidate.candidate_tree_digest}")
            print(f"- result: {result.message}")
        return result.exit_code

    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


def _print_preflight_policy_block(as_json: bool) -> int:
    message = (
        "preflight-docker creates and removes labelled throwaway containers; "
        "rerun with --acknowledge-container-changes"
    )
    exit_code = exit_code_for(Outcome.POLICY_BLOCKED)
    if as_json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": Outcome.POLICY_BLOCKED.value,
                    "exit_code": exit_code,
                    "message": message,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"FirstRun Docker preflight: {Outcome.POLICY_BLOCKED.value}")
        print(f"- error: {message}")
    return exit_code


def _print_strands_policy_block(
    as_json: bool,
    *,
    provider_cost_acknowledged: bool = False,
    credential_identity_verified: bool = False,
) -> int:
    missing: list[str] = []
    if not provider_cost_acknowledged:
        missing.append("--acknowledge-provider-cost")
    if not credential_identity_verified:
        missing.append("--confirm-verified-temporary-non-root-credentials")
    message = (
        "preflight-strands may make multiple paid model requests and requires "
        "an independently verified temporary, non-root AWS identity; missing "
        + ", ".join(missing)
    )
    exit_code = exit_code_for(Outcome.POLICY_BLOCKED)
    if as_json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": Outcome.POLICY_BLOCKED.value,
                    "exit_code": exit_code,
                    "provider_cost_acknowledged": provider_cost_acknowledged,
                    "credential_identity_verified": credential_identity_verified,
                    "message": message,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"FirstRun Strands preflight: {Outcome.POLICY_BLOCKED.value}")
        print(f"- error: {message}")
    return exit_code


def _print_repair_config_block(as_json: bool, fields: list[str]) -> int:
    message = "repair provider configuration is invalid"
    if fields:
        message += ": " + ", ".join(fields)
    exit_code = exit_code_for(Outcome.POLICY_BLOCKED)
    if as_json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "blocked",
                    "outcome": Outcome.POLICY_BLOCKED.value,
                    "exit_code": exit_code,
                    "message": message,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            "FirstRun local repair: "
            f"blocked ({Outcome.POLICY_BLOCKED.value})"
        )
        print(f"- result: {message}")
    return exit_code


def main() -> None:
    raise SystemExit(run())
