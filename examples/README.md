# Test oracle — do not expose this to the repair agent
`recipe.fixed.json` is the known-good recipe for deterministic engine testing.
It is not a generated agent result. The runtime agent must inspect the fixture's
own repo and failure evidence to discover the existing migration script.
Copy only `fixtures/notes-app/` into an authorized model-facing sandbox; never send
this folder, acceptance matrix, or the wider FirstRun repository as its context.
There is no invented image digest in the sample target; the controller must resolve
and record an actual digest during the first execution feasibility check.
