# Tomorrow — Thursday, September 10, 2026 (India time)

This is a sequence of focused blocks, not a promise of exact completion times.
The first-day target is M1, with M2 as the ambitious finish. A functioning provider
and sandbox are prerequisites for proving the live agent, not for writing tests.

## Before starting: keep the product fixed
FirstRun remains a web app + GitHub App. Tomorrow's command-line output is the
internal engine under construction, not a pivot to a CLI-only product.
Do not spend the morning on a new name, purchased domain, UI template, or another
idea search. Do not repackage Zero. Start new work and disclose any reused material.

## Block 1 — 45–60 minutes: environment + risk checks
Keep the Codex interface you already use. On Windows, choose one consistent
execution environment. I recommend WSL2/Linux for this Linux-container-heavy
project, but Codex also supports native Windows; WSL is not a Codex requirement.
Prefer a repo under the WSL Linux filesystem, such as `~/projects/firstrun`, when
using that workflow. Do not mix Windows and Linux virtual environments.

In PowerShell, inspect rather than reinstall everything:
```powershell
wsl --status
wsl -l -v
git --version
docker version
```
In the chosen Linux/WSL terminal:
```bash
git --version
python3 --version
node --version
npm --version
docker version
```
Both Docker client and server must be reachable; a version string from the client
alone is insufficient. Do not reset Docker or delete existing containers to fix it.
Use current installed compatible tools and record exact versions. The fixture
needs Node 22.16+ with `node:sqlite`; the chosen container image will pin the actual
runtime. Python 3.12+ is a project choice, not a claim about the SDK's minimum.

Put this pack in the new repo, create a Git checkpoint, and run:
```bash
python3 tools/check_pack.py
```
Ask Codex to create a **non-destructive preflight** that, with your approval,
runs a controlled throwaway container, confirms it can create/read/delete only
its own labelled resources, and captures the resolved image digest.
No global prune; no host Docker socket inside a repository container.

In parallel, check whether Bedrock can perform one small real Strands tool call.
Use a named profile/temporary credentials and a verified model ID/region, not root
keys or guessed defaults. Do not spend over 30–45 minutes repeating the same
account-verification error. Record it and continue the local work.
If no model is available, use a labelled fake proposer for unit/integration tests;
it cannot satisfy the Strands milestone or be represented as the working agent.

## Block 2 — 30 minutes: review the plan
Give Codex `prompts/01-plan.md`.
Approve only a plan that separates immutable target from mutable recipe, runs the
acceptance probe outside repository control, and makes a fresh proof observable.
Use the audit document as a checklist, not another round of product brainstorming.

## Block 3 — 2–3 hours: prove the engine without an LLM
Implement M1 using `fixtures/notes-app` and the known-good recipe in
`examples/recipe.fixed.json` as a **test oracle only**.
Baseline: `/health` succeeds, but creating a note fails because the table is absent.
Fixed recipe: invoke the existing migration; a fresh proof can create and read a
randomly generated note. No application source or target change is necessary.
Discard mutable state between runs. Verify a marker from investigation is absent
from proof. Confirm the README setup block exactly reflects the executed recipe.
Never feed the known-good recipe or an answer key to the runtime agent.

## Block 4 — 2–3 hours: make Strands discover the repair
Implement M2 only after the known repair is demonstrably verifiable.
The agent gets the failed run evidence, authorized repo files, and narrow tools.
It discovers the migration script, proposes the recipe change, and the controller
renders the documentation and launches a fresh deterministic proof.
No hard-coded `if fixture_name: migration` branch.
Test a variant with a different script name and a negative case where a secret or
human decision is required. A missing provider is a blocker, not a fake success.

## Block 5 — 45–60 minutes: review and close the day
Run the acceptance matrix, inspect the actual diff, and create a Git checkpoint.
Update STATUS.md with commands/results, measured runtime, and unresolved blockers.
Record a short screen capture of the *real* loop if it works. This is debugging
and build evidence, not the final polished demo.

## Day-one success
Minimum: fresh failure and independently fresh known repair; protected target;
no state carryover; real evidence; one repeatable command in the implementation.
Strong finish: Strands discovers the repair and the same verifier proves it.
Not success: a pretty web page plus mocked “verified” events.

## When a block goes badly
Docker unavailable: repair the execution environment or select a documented
isolated backend. Never run arbitrary repo scripts on the credentialed API host.
Provider unavailable: finish deterministic engine/tests, mark M2 blocked.
Fixture flaky: remove uncontrolled dependencies; do not hide failure with retries.
Oversized plan: remove deployment machinery, not independent proof or permission
checks. These gates determine sequencing, not the eventual size of the product.
