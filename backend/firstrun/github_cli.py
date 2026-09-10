"""Small local operational surface for the M3 webhook/worker boundary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def add_commands(subcommands: argparse._SubParsersAction) -> None:
    schema = subcommands.add_parser("github-config-schema", help="print the GitHub registration schema (no credentials)")
    schema.set_defaults(github_command=True)
    serve = subcommands.add_parser("github-serve", help="serve signed webhook ingress; execution stays in the local worker")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--database", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(github_command=True)
    web = subcommands.add_parser("web-serve", help="serve authenticated web API, or a safe unconfigured setup surface")
    web.add_argument("--config", type=Path, help="owner-approved GitHub registration JSON")
    web.add_argument("--auth-config", type=Path, help="private OAuth configuration JSON; contains a secret path, not a secret")
    web.add_argument("--database", type=Path, required=True)
    web.add_argument("--port", type=int, default=8765)
    web.set_defaults(github_command=True)
    worker = subcommands.add_parser("github-worker", help="claim and process one authorized durable GitHub case")
    worker.add_argument("--config", type=Path, required=True)
    worker.add_argument("--database", type=Path, required=True)
    worker.add_argument("--aws-profile", required=True)
    worker.add_argument("--region", required=True)
    worker.add_argument("--model-id", required=True)
    worker.add_argument("--acknowledge-provider-cost", action="store_true")
    worker.add_argument("--confirm-verified-temporary-non-root-credentials", action="store_true")
    worker.set_defaults(github_command=True)
    inspect = subcommands.add_parser("github-case", help="inspect one local persisted case; does not execute work")
    inspect.add_argument("--database", type=Path, required=True)
    inspect.add_argument("--case-id", required=True)
    inspect.set_defaults(github_command=True)


def run_command(args: argparse.Namespace) -> int:
    from firstrun.github_state import GitHubConfig, SQLiteStore

    try:
        if args.command == "github-config-schema":
            print(json.dumps(GitHubConfig.model_json_schema(), indent=2))
            return 0
        if args.command == "github-case":
            if not args.database.is_file():
                raise ValueError("State database does not exist")
            print(json.dumps(SQLiteStore(args.database).get_case(args.case_id), indent=2))
            return 0
        if not sys.flags.isolated:
            return _blocked("Run GitHub operations with python -I to exclude repository import shadowing")
        from firstrun.api import load_github_config
        config = load_github_config(args.config) if args.config else None
        if args.command in {"github-serve", "web-serve"}:
            if not 1024 <= args.port <= 65535:
                raise ValueError("Port must be between 1024 and 65535")
            import uvicorn
            from firstrun.api import create_app
            auth_config = None
            if args.command == "web-serve" and args.auth_config is not None:
                from firstrun.web_auth import WebAuthConfig
                if args.auth_config.stat().st_size > 16 * 1024:
                    raise ValueError("Web auth configuration exceeds the 16KiB limit")
                values = json.loads(args.auth_config.read_bytes())
                if not isinstance(values, dict) or set(values) - {
                    "client_id", "client_secret_path", "public_origin", "allow_insecure_localhost"
                }:
                    raise ValueError("Invalid web auth configuration fields")
                values["client_secret_path"] = Path(values["client_secret_path"])
                auth_config = WebAuthConfig(**values)
            # An operator-managed HTTPS reverse proxy is required for GitHub delivery.
            # Neither App auth nor the Docker/agent worker is exposed as a route.
            uvicorn.run(create_app(config, args.database, auth_config=auth_config), host="127.0.0.1", port=args.port,
                        access_log=False, server_header=False, proxy_headers=False)
            return 0
        if args.command == "github-worker":
            from firstrun.domain.repair import RepairProviderConfig
            from firstrun.orchestration.github import process_one
            provider = RepairProviderConfig(
                aws_profile=args.aws_profile, region=args.region, model_id=args.model_id,
                provider_cost_acknowledged=args.acknowledge_provider_cost,
                credential_identity_verified=args.confirm_verified_temporary_non_root_credentials,
            )
            if not provider.provider_cost_acknowledged or not provider.credential_identity_verified:
                return _blocked("Worker requires explicit provider cost and temporary non-root identity acknowledgement")
            result = process_one(config, SQLiteStore(args.database), provider,
                                 artifact_root=args.database.absolute().parent / "artifacts")
            # Full evidence stays behind local access, not an unauthenticated HTTP URL.
            state = result.get("phase", result.get("state", "unknown"))
            print(json.dumps({"case_id": result.get("id"), "state": state,
                              "message": result.get("payload", {}).get("message")}, indent=2))
            return 0 if state in {"idle", "verified", "pr_open"} else 16 if state == "needs_input" else 13
    except (ValueError, OSError, TypeError, KeyError):
        return _blocked("Invalid or unavailable local configuration/state; no credential contents are emitted")
    return _blocked("Unknown GitHub operation")


def _blocked(message: str) -> int:
    print(json.dumps({"state": "blocked", "outcome": "policy_blocked", "message": message, "exit_code": 13}))
    return 13
