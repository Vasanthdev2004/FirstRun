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
| Docker server on this machine | Blocked | Docker Desktop 4.88.1 crashed during startup and WSL mounted its registered root VHD read-only as a recovery fallback; engine type remains unknown |
| Guarded Docker M0 preflight | Implemented and unit-tested; not live-run | Controller-owned digest-pinned containers, loopback-only verifier, hard limits, ownership-checked cleanup; 16 focused tests |
| Guarded Strands/Bedrock M0 preflight | Implemented and unit-tested; provider/account unknown | Killable isolated child, exact dependency-origin checks, canonical endpoint reconciliation, test/live provenance, explicit spend and identity prerequisites |
| Real Strands tool call | Not run | No named profile/region/model and no cost authorization were supplied; no provider call or credential read was made |
| Baseline isolated runner and fresh proof | Not implemented | M1 has not started |
| Agent-generated repair | Not implemented | M2 |
| Real GitHub PR/check | Not implemented | M3 |
| Web product | Not implemented | M4 |
| Hosted AWS execution | Not implemented | M5 feasibility decision |

## Current blockers

1. Docker Desktop's Linux daemon is unavailable. `docker desktop start --detach`
   reports that startup began, but the backend then crashes while removing the exact
   runtime socket `C:\Users\vasan\AppData\Local\Docker\run\sailor-ingest.sock`:
   `The file cannot be accessed by the system.` The follow-up
   `docker version --format "{{json .Server}}"` exits 1 because
   `//./pipe/dockerDesktopLinuxEngine` does not exist. No container was launched,
   no image was pulled, and no runtime image digest has been captured.
   With Docker and its WSL distribution stopped, an exact-file `Move-Item` retry
   still failed with Windows error 1920. Starting only the registered
   `docker-desktop` distribution then reported that its disk was mounted read-only
   as a fallback. The registered root disk is
   `C:\Users\vasan\AppData\Local\Docker\wsl\main\ext4.vhdx`; no VHD repair,
   Docker reset, or data-disk mutation was attempted.
2. Live provider proof requires a user-selected named AWS profile, exact region and
   model ID, explicit cost acknowledgement, and confirmation that the current
   identity was independently verified as temporary and non-root. These facts are
   unknown; no AWS request has been made and no credential contents were accessed.

M0 is not accepted until both live preflights produce recorded evidence or a
specific external blocker is accepted. Unit doubles and the pack checker cannot
satisfy those gates.

## Latest checkpoint

- Implementation commit: `4c56690` (`feat: add guarded M0 preflights`)
- Commands and exit codes:
  - `uv lock --check` — 0; 51 packages resolved.
  - `uv run --frozen --extra provider --python 3.12.13 python -m unittest discover -s tests -v` — 0; 51 tests passed.
  - `uv run --frozen --extra provider --python 3.12.13 python -m compileall -q backend tests` — 0.
  - `uv run --frozen --extra provider --python 3.12.13 python tools/check_pack.py` — 0; 24 handoff-asset checks passed (not runtime proof).
  - `uv run --frozen --extra provider --python 3.12.13 python -m firstrun doctor --json` — 12 (`infrastructure_error`); all required checks except Docker server/engine passed, Git tree was clean, provider remained deliberately unchecked.
  - `docker context show` — 0; `desktop-linux`.
  - `docker context inspect --format "{{json .Endpoints.docker.Host}}" desktop-linux` — 0; local named pipe above.
  - `docker version --format "{{json .Server}}"` — 1; server unavailable.
  - `docker desktop start --detach` — 0 for the start request; the backend then
    crashed on the exact runtime socket above and the engine remained unavailable.
  - `docker desktop stop --force --timeout 30` — 0; all Docker Desktop processes
    stopped before the exact runtime-socket rename was attempted.
  - Exact-file `Move-Item` — 1 with `The file cannot be accessed by the system`;
    source remained present and no backup destination was created.
  - `wsl.exe -d docker-desktop ...` — 1 and reported a read-only fallback mount;
    `wsl.exe --terminate docker-desktop` and `wsl.exe --shutdown` both exited 0.
- Accepted behavior: permission gates stop Docker/provider side effects by default;
  fake provider evidence is marked non-live and cannot be milestone-eligible;
  ambiguous Docker creates are recovered by deterministic name and verified labels
  before exact-ID cleanup; no non-loopback interface or route may satisfy egress proof.
- Security review: no remaining P0/P1 findings in the implemented M0 boundaries.
- Known limitations: no live Docker topology, cleanup, or image digest has been
  observed; no AWS identity/model/usage has been observed; M1 is intentionally absent.
- Next single task: with explicit approval and a recoverable backup, repair the
  registered Docker WSL root VHD using the current Microsoft recovery procedure;
  then restart Docker, re-run the doctor, and run the live Docker preflight.
