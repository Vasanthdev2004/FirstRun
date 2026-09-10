# Execution contract: target + recipe + evidence

This document is the normative repair/proof specification. Schemas in `schemas/`
express the data shape; they do not enforce process isolation or trust by themselves.

## 1. Target: WHAT must succeed
`.firstrun/target.json` describes the maintainer-approved environment and functional
outcome. At case creation, the controller snapshots it from an approved revision
into trusted storage. The agent and code sandbox cannot replace this snapshot.
A policy/target change in an incoming PR requires approval before it becomes the
new basis for a case. It cannot retroactively validate the old failed case.

For the fixture: Node 22 Linux; `/health` readiness; create a note containing a
fresh random value then fetch that note and compare it. The controller selects a
known `notes-create-read-v1` probe, not `npm test` or an editable script alias.
Changing what constitutes success is a human decision. New target -> new evidence.

## 2. Recipe: HOW to reach the target
`.firstrun/recipe.json` contains ordered foreground steps and one managed startup
command. It may be repaired under policy. It contains no verifier or authority
fields and cannot alter the accepted environment, timeout budget, or permissions.
Commands use explicit argv and relative cwd. The runner executes them inside the
sandbox without interpolating them through the host shell. Recipe schema validity
is necessary but never sufficient authorization to execute a command.

For the first fixture the broken recipe installs and starts; the fixed recipe
adds the repository's already-existing `npm run db:migrate` before starting.
`package.json`, source code, target, and the protected verifier remain unchanged.

## 3. Public instructions: same path, not a second truth
The README contains exactly one managed block between:
`<!-- firstrun:setup:start -->` and `<!-- firstrun:setup:end -->`.
The block is rendered deterministically from the recipe's ordered commands.
`tools/render_recipe.py` is the reference renderer. A pre-execution check must
compare the existing block with the renderer output; no silent regeneration may
hide a baseline documentation error.

After an approved agent recipe patch, the controller renders the matching README
block as part of the candidate patch. Those docs changes are visible in the PR.
The agent need not separately hand-author the same commands.
Outside the block, existing prose is preserved. Docs-only corrections can repair
an actual mismatch, but a prose edit cannot fix a runtime migration failure.
Initial unstructured docs can inform a proposed recipe, which a human approves;
we do not claim arbitrary README prose can be compiled correctly without review.

## 4. Freshness and provenance
Every case pins base commit, base tree digest, approved target digest, verifier
version/digest, policy revision, exact resolved runtime image digest and platform.
Baseline and proof use that same runtime and outcome. Tags are resolved once per
case; never let a floating image tag change between runs. No fabricated image hash.

Baseline, investigation, and proof each receive a new workspace and runtime state.
Reusable immutable image layers are allowed. No copied node_modules, build output,
.env, SQLite files, mutable service volumes, container snapshots, or warm runtime
from investigation. No `docker commit` of the investigation as the proof image.
Pinning a lockfile/image does not freeze every external dependency/service; record
network policy and failures honestly. Day-one fixture needs no external packages.

## 5. Candidate extraction
Compare against the exact base tree, collect only regular allowlisted text changes,
reject traversal/symlinks/binaries/oversized patches, and validate path + semantic
policy. Untracked generated files are not implicitly part of the patch. Hash the
candidate, not the entire mutated investigation workspace. Preserve evidence before
cleanup. Confirm cleanup for run-owned resources; quarantine leaked workers.
Never run global `docker system prune` or delete unrelated development resources.

## 6. The independent proof
On a fresh base archive, apply the exact approved candidate patch. Confirm its tree
and protected file digests; assert docs/recipe agreement. Execute the frozen recipe
with no LLM present. The controller starts/stops the process, observes readiness,
and runs the trusted HTTP probe outside repository control. Timeouts, process exit,
probe failures and infrastructure errors remain distinct outcomes.
The probe and evidence collector are not injected as writable code beside the app.
In the first offline Docker lane, the trusted probe uses a separate container joined
to the app's private network namespace, with separate filesystem/process namespace.
It reaches the app on loopback without opening an internet route or host port.
See SECURITY.md for this explicit `network=none` / verifier-join arrangement.
A marker absence check is one regression test for freshness, not proof against
all sandbox escape or deliberate malicious behavior.

## 7. Verified predicate
A candidate can be labelled verified only if ALL hold:
- exact source/candidate/runtime/target/verifier/policy references are recorded;
- candidate patch policy and protected-input checks pass;
- public instructions equal the rendered executed recipe;
- required recipe commands succeed and readiness is observed;
- the external acceptance probe succeeds;
- evidence comes from the trusted controller for a distinct clean proof;
- no required test was skipped, substituted, or overridden by model output.
This is a product verification predicate, not mathematical verification of arbitrary
software. A structurally valid JSON result is not an authenticated worker result.

## 8. Revision changes and PR handoff
Before publishing, compare the currently configured branch head with the pinned
base. When it changed, mark the result stale, build a new case/candidate on the new
base, and rerun; do not silently rebase and carry a passing label forward.
After PR creation, retrieve its actual head SHA/files and associate proof only with
the exact content tested. A GitHub check succeeds on that tested SHA only.
Default branch health changes only after the relevant default-branch revision has
passed, including after a merge. A green repair branch is not a green main branch.
