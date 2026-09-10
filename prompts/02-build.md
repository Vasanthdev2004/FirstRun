Implement the approved current milestone from docs/BUILD_PLAN.md.
Follow AGENTS.md and docs/CONTRACTS.md. Check STATUS.md before editing.
Work through the approved checkpoint without pausing for approval on routine files.
Pause only for material ambiguity, authority/spend changes, or spec contradictions.

Keep target/probe/policy immutable for a repair. The allowed recipe can change.
Execute repo commands only in the run sandbox. Verify the README managed block
matches the exact recipe and preserve untouched prose. Prove candidate success
from fresh state with the external acceptance probe. No hard-coded fixture repair.

Do not change reference acceptance meaning, mark fake provider results as live,
add later milestones, auto-merge, or replace real backend state with UI demo data.
Run all milestone acceptance checks; skipped/missing-prerequisite checks are not
passes. Record real output and exit codes. End with the diff summary, commands,
acceptance matrix, unresolved risks, updated STATUS.md and next task.
