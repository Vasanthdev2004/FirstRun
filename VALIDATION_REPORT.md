# Validation report — September 9, 2026

## Performed
- `python tools/check_pack.py`: **24 tests passed**, no skipped checks on the authoring
  environment. Tests cover handoff assets, recipe shape/renderer behavior, rejection
  of selected invalid inputs, fixture syntax, and oracle/doc consistency.
- JSON Schema Draft 2020-12 definitions checked using jsonschema 4.26.0; both recipe
  examples and the target validated; an injected acceptance field in a recipe was
  rejected. These are data-shape checks, not a sandbox or authorization test.
- Controlled fixture functional mechanics tested with Python 3.13.5 and Node
  v22.16.0 in separate temporary working directories in the authoring environment:

| Case | Readiness | Functional acceptance | Observed |
|---|---|---|---|
| Baseline recipe | HTTP 200 | Failed: notes table absent | Expected failure |
| Known-corrected recipe | HTTP 200 | Create + independent read-back succeeded | Expected pass |
| Another fresh baseline directory | HTTP 200 | Failed: notes table absent | Expected failure |

The correction invokes the existing migration; no application code change is needed.
The fixture test executes the recipe's actual foreground commands and starts the app,
then uses the supplied HTTP probe. Its temporary processes/directories were cleaned.
It is a controlled fixture authoring test, **not the FirstRun sandbox implementation**.

## Not performed / not implemented
There is no Docker daemon in this authoring environment. Docker isolation, namespace
joining, cleanup enforcement and worker authentication remain implementation gates.
No real Strands model call, AWS service deployment, GitHub write, web UI, or live
FirstRun end-to-end run was performed here. The 18 cases in the acceptance matrix
are requirements for the implementation, not 18 completed runtime tests.

Do not claim that this pack is a finished application or a tested secure execution
platform. It supplies corrected specifications, starter acceptance assets and
verified fixture mechanics so Codex can build the application against concrete goals.
