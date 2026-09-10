"""Command-line entry points for trusted local FirstRun operations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from firstrun.doctor import run_doctor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firstrun")
    subcommands = parser.add_subparsers(dest="command", required=True)

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
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "doctor":  # pragma: no cover - argparse owns this invariant
        raise AssertionError(f"unhandled command: {args.command}")

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


def main() -> None:
    raise SystemExit(run())
