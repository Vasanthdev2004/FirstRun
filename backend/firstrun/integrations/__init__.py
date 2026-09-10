"""Trusted external integrations for FirstRun."""

from firstrun.integrations.github import (
    GitHubAppConfig,
    GitHubClient,
    GitHubError,
    GitHubTransport,
    StdlibGitHubTransport,
    TransportResponse,
    fetch_snapshot,
    protected_digest,
)

__all__ = [
    "GitHubAppConfig",
    "GitHubClient",
    "GitHubError",
    "GitHubTransport",
    "StdlibGitHubTransport",
    "TransportResponse",
    "fetch_snapshot",
    "protected_digest",
]
