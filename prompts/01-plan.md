Read AGENTS.md, START_HERE.md, STATUS.md, docs/PRODUCT.md,
docs/CONTRACTS.md, docs/SECURITY.md, docs/BUILD_PLAN.md and TOMORROW.md.
Inspect the actual repository. Do not implement the whole product.

First report contradictions or blockers, including whether a patched recipe can
actually change execution while the success target stays immutable. Explain how
the public README commands will match those tested, where the HTTP acceptance probe
runs, and how proof avoids the investigation's mutable state.

Propose the M0 and M1 plan only: files/modules, bounded tasks, the expected baseline
failure, exact acceptance checks, and the first command we will be able to run.
Keep one backend codebase and an isolated worker boundary. No speculative services.
Existing fixture/oracle assets are for tests, never to be placed in the runtime
agent's input. Separate tests from live provider execution.

List prerequisites you have actually checked versus ones requiring human approval
or credentials. Do not print secrets. Do not change cloud billing/permissions or
create chargeable resources. Plan first; stop for review before implementation.
