Review the current diff as a skeptical maintainer. Read docs/CONTRACTS.md,
docs/SECURITY.md and spec-tests/acceptance_matrix.json. Do not edit code yet.

Try to falsify these claims:
1. A recipe repair changes the executed public setup path.
2. The agent cannot change what counts as success or invoke a weaker probe.
3. Baseline/investigation/proof share no mutable project/service state.
4. Health/readiness cannot substitute for functional acceptance.
5. A fake or skipped result cannot set verified.
6. Proof belongs to exact candidate/runtime/target; current main is not mislabeled.
7. Code sandboxes and model inputs contain no integration credentials.
8. Duplicate events, write timeouts and stale heads do not publish misleading PRs.

Inspect the implementation and run relevant negative tests, not just the author's
summary. Report findings with severity, file/line, reproducer and smallest fix.
Explicitly state untested boundaries. Do not congratulate the implementation in
place of review, and do not weaken acceptance to make findings disappear.
