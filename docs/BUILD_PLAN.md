# Build plan with executable gates

All commands below marked “implement” are future CLI/test contracts, not commands
that already exist in this pack. Codex must make them real, document prerequisites,
and record actual command output in STATUS.md. Do not fabricate successful exits.

## M0 — preflight and decisions (Sept 10 morning)
Prove Docker server + safe labelled throwaway container; pin runtime digest;
validate app-to-trusted-probe networking without egress; lock backend tools; confirm real Strands tool-call
provider or name that blocker. Review this plan. Do not scaffold unused services.
Deliver a non-destructive doctor command and exact tested versions.

## M1 — trustworthy engine before live AI (Sept 10)
Implement target/recipe validation, rendered README check, typed run evidence,
baseline runner, external HTTP probe and independent proof with the known oracle.
Implement CLI contract:
`python -m firstrun verify-local --repo <approved-path> --target <approved-target>`
It must refuse arbitrary host execution and require the sandbox prerequisite.
Return distinct nonzero outcomes for failed, unsupported, policy and infrastructure
errors; publish the exact chosen exit codes. Unit test outcome mapping.
Implement tests for actual clean-state failure, known repair, unchanged target,
docs mismatch, and hidden-state rejection. No LLM required for this gate.

## M2 — Strands repair loop (Sept 10 ambitious / Sept 11)
Integrate narrow tools and schema-validated outcomes. Propose recipe change, render
matching documentation, enforce candidate policy, destroy investigation state,
launch proof and record exact inputs. Test renamed-script variant and NeedsInput.
Implement `python -m firstrun repair-local --repo <approved-path>` with explicit
provider config. A fake proposer only meets test-harness needs, not M2 acceptance.

## M3 — genuine GitHub workflow (Sept 11)
Register least-privilege App; configure owner-approved demo repo; verify signature
and installation authorization; durable event dedupe; exact-SHA source fetch;
repair commit and PR; read-back on success/timeout; no auto-merge. Add a check only
on the actual tested SHA. Repeat delivery must not duplicate PR. Changed head must
mark stale and start a new verification instead of silently carrying proof over.
Persist state before network writes; reconcile uncertain writes after restart.

## M4 — usable web product (Sept 12)
Implement repo view, case timeline/evidence/diff and human decision path. Use real
backend state. Show main health separately from repair status. Authenticate mutation
and artifacts, handle empty/error/loading/cancel/stale states. Reviewer sees exact
versions, commands and functional proof, not private chain-of-thought.
The repository/contract interaction is human approval of target and initial recipe,
not a form with fake defaults for an untested platform.

## M5 — deployment + wider realism (Sept 13)
Select one deployment after an early feasibility spike. Prefer real AWS Strands +
Bedrock; AgentCore encouraged if it works without compromising worker isolation.
Do not migrate every storage layer on the last day. Provide durable state/artifacts
and a clear judge-access route; recorded evidence must be labelled.
Test an authorized repo beyond the teaching fixture. Add broader ecosystem/service
support only with positive/negative test cases; do not list unsupported adapters.
Create a permissions/spend/teardown note for whatever is actually deployed.

## M6 — freeze, rehearse, submit (Sept 14)
No new architecture. Three reset demo rehearsals; working known-failure + NeedsInput;
README setup; public license/repo; honest implemented-vs-planned list; architecture
rendering; <=5-minute working video; Builder ID; judge instructions and availability.
Target submission: Sept 14 evening IST. Official hard cutoff converts to Sept 15,
05:30 IST; do not plan work into that last-hour margin. Rules in SOURCES_AND_RULES.md.

## Schedule pressure policy
Do not cut Strands, meaningful acceptance, independent proof, truthful status, or
authorized real output. Defer extra language adapters, broad multi-user onboarding,
nightly schedules, elaborate dashboards and optional infrastructure first.
A small but honest supported lane is acceptable; advertised broader functionality
requires tests and implementation. Scope can grow without weakening the product.

## Exit report for each milestone
Observed behavior; commit; exact commands/exits; acceptance cases passed/failed/skipped;
known limitations; security/secret/spend implications; next single task.
“Production ready” and “all done” are not useful evidence.
