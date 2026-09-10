# FirstRun — repository instructions (v2)

## Product
Web app + GitHub App. Continuously test the declared newcomer setup path,
investigate failures with Strands, repair allowed setup content, and open a
pull request backed by an independently executed fresh proof. Never auto-merge.

## Authority and reading order
This v2 pack supersedes the earlier FirstRun pack. Do not reconcile old v1 files.
Read `START_HERE.md`, then `docs/PRODUCT.md`, `docs/CONTRACTS.md`, and the current
milestone in `docs/BUILD_PLAN.md`. Consult other documents only as the task needs.
`STATUS.md` records facts about implementation, not aspirations.
When specifications or tests conflict, report the contradiction; do not silently
choose a winner, weaken a test, or pretend the requirement is satisfied.

## Non-negotiable rules
- A repair may change the recipe; it cannot change the pinned success target,
  verifier, permissions, or proof evidence for its own case.
- The recipe is the executable source. The README managed setup block is rendered
  from it; a deterministic check rejects divergence. No arbitrary prose compiler.
- Baseline, investigation, and proof use independent mutable state. Immutable base
  image layers may be shared; project files, volumes, databases, and caches may not.
- Repository code never executes on the credentialed API/agent process. The trusted
  runner controls isolated execution; the LLM receives narrow capability tools.
- A model response, healthy HTTP endpoint, or patch alone cannot certify success.
- Verification attaches to exact commit/tree, recipe, target, verifier, and runtime
  digests. A repaired PR is not proof that the default branch is fixed.
- Keep repository installation credentials out of code sandboxes, model context,
  repository archives, logs, and browser payloads.
- External writes require configured permission and read-back reconciliation.
- No unrestricted remote repository execution, production deployment, or auto-merge.

## Build approach
One Python backend codebase with modules; separate worker trust boundary; one web
frontend. Do not scaffold microservices, a queue cluster, or a second database
unless an approved milestone needs them. The product can expand after its central
workflow works. Do not expand into general code generation or generic chat.

## Codex task protocol
For a difficult/new boundary, plan before coding. For a clear approved task,
implement and test without asking for permission on every file. Pause for product,
security, provider-spend, dependency-boundary, or architecture changes.
Work on one milestone/checkpoint at a time. Commit reviewable slices. Keep normal
sandbox/approval controls enabled; never request blanket full access as a shortcut.
At completion show actual commands, exit codes, acceptance outcomes, limitations,
and changes to STATUS.md. A skipped test is not a passing test.

## Existing checks and protected assets
`python tools/check_pack.py` checks this handoff pack; it does NOT run FirstRun.
The included fixture and `tools/probe_notes.py` are reference acceptance assets.
Do not change their expected behavior to accommodate a broken implementation.
A deliberate change to acceptance meaning requires human approval and a recorded
reason. Add independent regression tests; do not rely only on tests you generated
alongside the implementation. Never run arbitrary repository commands on the host.

## Sources
Current official references and their limits: `docs/SOURCES_AND_RULES.md`.
Implementation APIs/versions must be checked and locked on the build machine;
this pack does not assume an AWS service is already enabled on the user's account.
