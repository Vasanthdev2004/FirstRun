# Implementation status — observed evidence only

**Last authored:** 2026-09-10.  
**Current checkpoint:** M1 implementation complete; exact-revision live acceptance is
the release gate for this checkpoint.  
**Product target:** web app + GitHub App; the current local CLI/worker is the trusted
engine slice, not the end-user product.

| Capability | State | Evidence |
|---|---|---|
| v2 specification and controlled fixture | Present and pack-checked | `uv run --frozen python tools/check_pack.py` — exit 0; 24 handoff-asset checks |
| M0 Python/outcome foundation | Implemented | Python 3.12; stable exits 0/10/11/12/13/14/15; locked dependencies |
| M0 Docker preflight | Live passed on this machine | Docker Desktop Linux engine, local named pipe, digest-pinned Node image, isolated verifier, exact-ID cleanup |
| M0 Strands/Bedrock preflight | Implemented and unit-tested; live provider check deferred | No profile, region, model, spend authorization, or independently verified temporary non-root identity was supplied; no AWS request or credential-content access occurred |
| M1 target and recipe contracts | Implemented | Strict Pydantic models reject extra fields, ambiguous paths, shell text, invalid steps, and verifier-in-recipe input |
| M1 README/recipe contract | Implemented | Recipe is executable source; managed README bytes are deterministically rendered and divergence is policy-blocked |
| M1 committed-source capture | Implemented | Exact `HEAD^{commit}` and Git trees; regular 0644 blobs only; bounded canonical archive; no working-tree bytes, filters, links, executable entries, or repo-local Git/Docker binaries |
| M1 baseline and proof worker | Implemented | Independent non-root, read-only, capability-dropped, seccomp-confined, no-egress Docker containers with tmpfs state and controller deadlines |
| M1 acceptance | Implemented | Controller-owned external create/read nonce probe; health alone cannot pass; model/client payloads cannot certify success |
| M1 evidence and cleanup | Implemented | Typed evidence binds commit/tree, source archive, candidate patch/tree, target, recipe, README, verifier, policy, runtime digests, commands, observations, identities, and cleanup; leaked owned resources quarantine the endpoint |
| M2 Strands repair loop | Not implemented | Next milestone; the known M1 oracle is test-only and not an agent repair command |
| M3 GitHub workflow | Not implemented | No App/webhook/PR/check writes; never auto-merge |
| M4 web product | Not implemented | No UI/backend product workflow yet |
| M5 hosted execution | Not implemented | No cloud resources provisioned |

## M1 acceptance coverage

- A01 and A09: the committed fixture becomes healthy but its functional create/read
  probe fails.
- A02 and A06: the allowlisted known recipe repair passes only in a separately
  created workspace with a distinct run, attempt, workspace, app, and verifier;
  seeded hidden-state markers must be absent.
- A07, A08, A13, and A14: protected-target edits, README divergence, untrusted proof
  claims, arbitrary repositories, traversal, links, and host executable shadowing are
  rejected.
- A15: successful candidate proof does not alter the committed broken baseline.
- A17: incomplete cleanup yields `cleanup_failed`, bounded evidence, and a persistent
  process-level Docker-endpoint quarantine.

The final checkpoint command is:

`$env:FIRSTRUN_RUN_LIVE_M1='1'; uv run --frozen python -m unittest discover -s tests -p 'test_*.py' -v`

It must pass all 125 unit tests and 6 live Docker integration tests with no skips on
the exact clean revision reported in the handoff. A skipped live test is not a pass.

## Checkpoint commits

- `115f825` — strict M1 contracts and evidence foundation.
- `5b9f83f` — isolated local baseline/known-oracle verification.
- `f759d95` — source, host-execution, output-bound, provenance, cleanup, and
  acceptance hardening.

## Known limitations and blockers

1. Live Strands/Bedrock validation remains unknown and requires the owner-selected
   profile, exact region/model, explicit cost acknowledgement, and confirmation of
   independently verified temporary non-root credentials. This is an M0 provider
   blocker deferred while the owner requested the local project engine first.
2. M1 intentionally supports only the controller-owned `fixtures/notes-app` lane and
   a controller-owned known repair oracle. It does not accept arbitrary repositories.
3. M1 source materialization uses a single bounded Docker CLI argument. The current
   committed fixture is safely below Windows' argument limit; widening the source
   policy requires a trusted chunked/streaming transport first.
## Next single task

After the exact-revision M1 gate passes, begin M2 only: integrate the narrow Strands
repair loop without exposing the known oracle, GitHub writes, UI, or later-milestone
scaffolding.
