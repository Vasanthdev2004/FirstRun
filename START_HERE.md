# FirstRun v2 — start here

**Artifact type:** implementation handoff + runnable controlled test fixture.
**Not included:** the FirstRun application, Docker worker, Strands agent,
GitHub integration, or deployed AWS environment. Their implementation is the task.

## Use this pack instead of v1
If no implementation exists, extract this ZIP and put the contents of
`firstrun-codex-pack-v2/` at the new repository root. `AGENTS.md` must be at the
actual root, not nested in an unused documentation folder.
If implementation already exists: make a Git checkpoint, review `docs/V1_AUDIT.md`,
and reconcile deliberately. Preserve source code and human edits. Move superseded
v1 specs outside the active repository rather than loading conflicting rules.
This is not a command to delete an existing project or overwrite unreviewed files.

## Read only these first
1. `TOMORROW.md` — September 10 actions and stopping points.
2. `docs/PRODUCT.md` — the product and primary user experience.
3. `docs/CONTRACTS.md` — the repaired execution/proof semantics.
4. `docs/BUILD_PLAN.md` — deliverables and objective gates.

Then open Codex on the repository root. Use `prompts/01-plan.md` first.
After reviewing the plan, use `prompts/02-build.md`.
Review each substantial checkpoint using `prompts/03-review.md` in a fresh
review context or as a human diff review. A second agent is not an independent
security guarantee; its value is a different review task and missing context.

## The first outcome
Not a homepage. Not 20 API endpoints. Not a beautifully generated PRD.
A controlled fixture fails from clean state; a known recipe correction succeeds
from separate clean state; protected acceptance remains unchanged.
Then the real Strands agent must discover that correction from evidence.

## Pack check
Run from the repository root:
```bash
python tools/check_pack.py
```
This verifies JSON syntax, local references, recipe/doc consistency and the
fixture source syntax when Node is available. It is not a sandbox/security test.
See `VALIDATION_REPORT.md` for exactly what was tested when this pack was authored.

## What to keep separate
Codex is your implementation tool. FirstRun's submitted runtime agent is Strands.
A Codex subscription does not by itself establish model API access for FirstRun.
Use a verified Bedrock configuration or another legitimately configured supported
provider; do not assume credentials or undocumented subscription endpoints.
No new paid SaaS integration or domain purchase is necessary for the planned build.
That is not a promise that cloud/model execution has no usage cost.
