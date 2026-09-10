# Implementation status — observed evidence only

**Last authored:** 2026-09-10.
**Current milestone:** M0 — implementation present; live acceptance incomplete.
**Target platform:** web app + GitHub App; local runner/CLI are engineering surfaces.

| Capability | State | Evidence |
|---|---|---|
| v2 specification and controlled fixture | Authored and pack-checked | `VALIDATION_REPORT.md`; `tools/check_pack.py` passed 24 handoff-only checks |
| Python foundation and outcome contract | Implemented | Python 3.12.13; stable exits 0/10/11/12/13/14/15; commits `bddc022`, `6fbea08` |
| Read-only environment doctor | Implemented and unit-tested | `python -m firstrun doctor --json`; actual result below |
| Backend dependency lock | Implemented | uv 0.12.9; Hatchling 1.32.0; Pydantic 2.13.5; Strands Agents 1.55.1; boto3/botocore 1.43.91 |
| Docker client and selected context | Available | Docker client 29.7.2; context `desktop-linux`; local endpoint `npipe:////./pipe/dockerDesktopLinuxEngine` |
| Docker server on this machine | Passed | At 2026-09-10T13:23:00+05:30: Docker Desktop 4.88.1, Engine 29.7.2/API 1.55, Linux/amd64 |
| Guarded Docker M0 preflight | Live passed | Run `5d97eab095824899a89865f57637d559`; digest-pinned runtime, isolated verifier, and independently reconciled cleanup |
| Guarded Strands/Bedrock M0 preflight | Implemented and unit-tested; provider/account unknown | Killable isolated child, exact dependency-origin checks, canonical endpoint reconciliation, test/live provenance, explicit spend and identity prerequisites |
| Real Strands tool call | Not run | No named profile/region/model and no cost authorization were supplied; no provider call or credential read was made |
| Baseline isolated runner and fresh proof | Not implemented | M1 has not started |
| Agent-generated repair | Not implemented | M2 |
| Real GitHub PR/check | Not implemented | M3 |
| Web product | Not implemented | M4 |
| Hosted AWS execution | Not implemented | M5 feasibility decision |

## Current blockers

1. Live provider proof requires a user-selected named AWS profile, exact region and
   model ID, explicit cost acknowledgement, and confirmation that the current
   identity was independently verified as temporary and non-root. These facts are
   unknown; no AWS request has been made and no credential contents were accessed.

M0 is not accepted until the remaining live Strands preflight produces recorded
evidence or the repository owner accepts the specific provider blocker above. Unit
doubles and the pack checker cannot satisfy that gate.

## Superseded host observation

At 2026-09-10T13:03:10+05:30 an earlier Docker Desktop start crashed on a stale
runtime socket, and a direct `docker-desktop` WSL start reported a read-only fallback
mount. FirstRun performed no VHD repair, reset, or data-disk mutation. By 13:23 the
Linux engine was reachable and the live preflight passed, so the earlier condition
is not current; its external resolution remains unknown.

## Latest checkpoint

- Implementation commit: `4c56690` (`feat: add guarded M0 preflights`)
- Commands and exit codes:
  - `uv lock --check` — 0; 51 packages resolved.
  - `uv run --frozen --extra provider --python 3.12.13 python -m unittest discover -s tests -v` — 0; 51 tests passed.
  - `uv run --frozen --extra provider --python 3.12.13 python -m compileall -q backend tests` — 0.
  - `uv run --frozen --extra provider --python 3.12.13 python tools/check_pack.py` — 0; 24 handoff-asset checks passed (not runtime proof).
  - `uv run --frozen --extra provider --python 3.12.13 python -m firstrun doctor --json` — 0; all required environment checks passed, Git tree was clean, provider remained deliberately unchecked.
  - `docker context show` — 0; `desktop-linux`.
  - `docker context inspect --format "{{json .Endpoints.docker.Host}}" desktop-linux` — 0; local named pipe above.
  - `docker version --format "{{json .Server}}"` — 0; Docker Desktop 4.88.1,
    Engine 29.7.2/API 1.55, Linux/amd64.
  - `uv run --frozen --extra provider --python 3.12.13 python -m firstrun preflight-docker --acknowledge-container-changes --allow-pull --json` — 0 at source revision
    `b52f368`; run `5d97eab095824899a89865f57637d559`, endpoint
    `npipe:////./pipe/dockerDesktopLinuxEngine`, image ID and repository digest
    `sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5`,
    app program `sha256:8a9849af7047ab3fe00e9438d0f90c8f35142048c5cd2fe201c5e2caf2af1843`,
    verifier program `sha256:fc86cc408d76465561eb80a7ed7aa4f2b558b0c86e0e41ef6b202fbd937f9a75`.
  - Verifier output — health true, only loopback interfaces, no non-loopback
    routes, and egress blocked; all 13 declared checks passed.
  - Independent cleanup reconciliation — the run-label query returned no IDs;
    both reported container IDs returned `No such container`; exact image inspect
    confirmed the recorded digest and Linux/amd64 platform.
- Accepted behavior: permission gates stop Docker/provider side effects by default;
  fake provider evidence is marked non-live and cannot be milestone-eligible;
  ambiguous Docker creates are recovered by deterministic name and verified labels
  before exact-ID cleanup; no non-loopback interface or route may satisfy egress proof.
- Security review: no remaining P0/P1 findings in the implemented M0 boundaries.
- Known limitations: the pinned Node runtime image remains in Docker's local image
  cache; no AWS identity/model/usage has been observed; M1 is intentionally absent.
- Next single task: either provide the approved AWS profile, exact region and model
  ID plus the two required acknowledgements for a live Strands call, or explicitly
  accept the named provider blocker for M0.
