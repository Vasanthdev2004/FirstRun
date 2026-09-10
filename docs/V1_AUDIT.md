# v1 audit and deliberate corrections

The original pack has useful product boundaries, but it should not be implemented
unchanged. The following are design findings, not evidence of faults in existing
application code (no application was supplied).

| Finding | Why it matters | v2 decision |
|---|---|---|
| One Setup Contract combines editable setup commands with protected acceptance | A missing migration inside an immutable contract cannot be repaired by editing only README | Split mutable recipe from pinned target/verifier |
| Proof executes the contract, not necessarily the README | Updating docs can leave execution unchanged; a pass may not establish the public instructions work | One executable recipe and mechanically synchronized public setup block |
| Arbitrary README interpretation is unspecified | Multiple OS guides, optional commands, prose, and background processes make deterministic replay ambiguous | Start with a maintainer-approved explicit recipe; no claim of universal prose interpretation |
| Separate service directories suggest premature microservices | More APIs/deployments create coordination work before the central loop exists | One backend codebase with modules; separate execution trust boundary, not business microservices |
| Hashing a test file is not sufficient protection | A script alias/hook/runtime could bypass the test without editing it | Controller-owned HTTP acceptance probe, pinned meaning, external result collection |
| One repo status mixes failure, repair, and current branch health | A passing unmerged patch may be presented as a healthy main branch | Separate checked revision outcome, repair case state, and current-head status |
| Phase ordering proves isolation after agent integration | An apparently good agent can mask a verifier design defect | Prove a known repair in an independent clean environment before live agent repair |
| Plans list acceptance commands as future work | Codex can report success without an executable contract | Implement named commands early, fail on missing prerequisites, record actual exits |
| Configurable architecture has no measured feasibility gate | Cloud account/sandbox constraints can consume the remaining build window | Day-one Docker and model-tool-call spikes; one deployment choice after measurement |

## Changes we are NOT making
No new general coding features, no compulsory multi-agent system, no commercial
multi-tenant security claim, and no change away from Professional Agents.
The web product and GitHub integration remain. Larger stack/service coverage is
allowed after the core invariant survives tests; this is sequencing, not a toy-only
product mandate.

## Migration from v1
Archive old specs outside the active instruction tree after a Git checkpoint.
Translate any existing `contract.yaml` into target + recipe with human review.
Do not automatically carry over a previous “verified” label: v2 proof semantics
are stronger and require new evidence. Never delete source code during migration.
