# Security and execution boundary

This is a controlled prototype, not a hardened multi-tenant arbitrary-code platform.
The policy below must be implemented and tested, not merely placed in an agent prompt.

## Supported trust level
Only owner-approved, explicitly configured repositories with an approved base and
supported recipe are executable. GitHub authorization alone does not make repository
code trustworthy. For the demonstration use controlled synthetic sources. Do not
run fork contributions, unknown Dockerfiles, unreviewed install hooks, privileged
Compose services, or arbitrary public URLs as a convenience feature.

## Sandbox controls
Use maintained container tooling. Run non-root where feasible, drop capabilities,
keep no-new-privileges/default seccomp, and set memory, CPU, PID, disk/output, and
wall-time limits. No host networking, host PID namespace, privileged mode, host
home mounts, or Docker socket in the repository container. Resource limits are
explicit; Docker defaults are not an adequate budget. See official source S7.
Only the trusted local worker holds daemon access. Its API is narrow/authenticated,
not an internet-exposed Docker or arbitrary command server.

Day-one fixture needs no external package traffic: deny egress.
For the local Docker lane, use an app container with `--network=none` and a separate
trusted verifier container joining ONLY that app's network namespace via
`--network=container:<run-owned-app-id>`. They share loopback for HTTP, but not
filesystem, PID namespace, credentials or result files. The probe opens no server
or management port. Do not try host port publishing with network=none; it does not
create the required route. The worker collects the verifier's own exit/output.
Pin the trusted verifier image as well as the app image. This network arrangement
must be demonstrated in M0/M1; it has not been Docker-tested in this pack.
Docker documents both network modes (S14). Subsequent npm
support requires a measured egress policy/proxy or an explicitly documented trusted
fixture-only limitation. Block cloud instance metadata, worker/API management
addresses, and credential endpoints. Do not attach a broad cloud role to a code
execution environment. A deny-list of shell strings is not sandboxing: npm hooks
and scripts themselves can execute arbitrary code.

## Fetch and publish are outside the code sandbox
The trusted integration fetches exact authorized commits with short-lived tokens,
then passes a credential-free archive to the worker. No token in remote URL,
.git/config, .netrc, environment or logs. Git submodules/LFS are unsupported until
authorized fetches and provenance are implemented explicitly.
The model has no GitHub token, root/AWS keys, Docker API endpoint or billing access.

## Repair policy
Start with exact allowed `.firstrun/recipe.json` and the managed README block;
optional `.env.example`/named setup files require approved extensions. Deny target,
verifier, application source, lockfile/package changes, CI and all other paths by
default in the first lane. Do not allow `package.json` wholesale just because a
script looks setup-related. Later support needs semantic checks on allowed fields.
Normalize paths, reject absolute/traversal/backslash ambiguity, symlinks/hardlinks,
submodules, binary patches, excessive size and deletions outside explicit authority.
Deny wins. Do not use a blanket `.firstrun/**` deny that accidentally bans the recipe.
Verify the actual candidate tree as well as proposed filenames; recheck protected
inputs after execution from the trusted controller.

## Acceptance protection
The probe is controlled by FirstRun, called outside repository code, and selected
from an approved versioned target. No model-provided PASS endpoint or test command.
Readiness and functional acceptance differ. Test anti-bypass cases such as changing
a package test alias, writing a fake result file, and requesting a different probe.
All remote worker events need authenticated case binding and expected attempt IDs.
Do not accept a fabricated JSON proof from a client or agent as trusted evidence.

## Secrets and prompt injection
Prefer synthetic/no-secret demo cases. A required private integration stops for
human policy; do not invent tokens, add paid integrations, or send production keys
into the sandbox. A later broker may inject a scoped ephemeral test credential,
but the repo can read any secret it is given; redaction cannot prevent all exfiltration.
Redact known values/obvious token patterns before model or UI exposure and limit
logs. Do not claim complete redaction for all encoded secrets.
Repository text/logs are untrusted data, not instructions. They cannot authorize
new tools, alter target, request secrets or access external accounts.

## Human permissions and spend
Allow automatic PR creation only after explicit installation policy and passing
proof. Never merge or deploy. Require confirmation for new provider costs, cloud
resources beyond an agreed plan, target/policy change, or sensitive credentials.
Enforce controller-side tool/attempt/time/token budgets. Stop flag cancels queued
work and kills only run-owned active processes. Billing alerts are not hard caps.

## Cleanup and evidence
Label every run resource and delete only those owned by the completed/cancelled run.
Keep cleanup outcome in evidence. Quarantine a failed-cleanup worker before reuse;
a fresh verified label must not hide a resource leak. No global pruning commands.
Treat unauthorized artifact access, path traversal, and stale tenant decisions as
auth failures with tests, even in a small demo.
