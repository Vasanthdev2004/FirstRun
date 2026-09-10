# Architecture: simple code structure, explicit trust boundaries

## Selected first implementation
- Web: Next.js + TypeScript, later in the build, consuming backend state.
- Backend: one Python codebase, FastAPI + Pydantic domain types.
- Agent: Strands Python module inside that codebase; not a separate microservice yet.
- State: SQLite with explicit transactions for single-controller prototype.
- Artifacts: local filesystem behind a small store interface; S3 when deployed.
- Runner: isolated Docker execution controlled by a narrow worker interface.
- Model: explicit verified provider/model/region. Bedrock preferred once available.
- GitHub: App-based selected-repository integration, external writes in trusted code.

Python 3.12+ and Node 22.16+ for the provided fixture are build choices. Pin the
actually validated versions and lock dependencies during M0; do not scatter guessed
“latest” versions through docs. Do not swap the stack merely to follow a preference.

## Suggested repository after implementation starts
```
apps/web/                  # create at M4, not an empty product facade on day one
backend/firstrun/
  api/                     # auth, webhook, evidence/decision endpoints
  domain/                  # typed states, targets, candidates, transition checks
  orchestration/           # durable case lifecycle, budgets, leases
  agent/                   # Strands investigation, controlled capabilities
  integrations/github/     # token handling and idempotent publication
  worker/                  # trusted sandbox lifecycle/command adapter
  verification/            # external acceptance probes and proof policy
fixtures/notes-app/         # controlled example, isolated when run by FirstRun
schemas/                   # machine-readable specification contracts
spec-tests/                # stable acceptance expectations, not model answers
```
Shared code does NOT imply repository code may execute in the API process.
The worker communicates via validated requests; no public arbitrary shell endpoint.

## Runtime flow
GitHub event -> authenticated API -> durable case with pinned inputs -> worker
baseline -> stored sanitized evidence -> Strands tools on investigation sandbox ->
candidate -> deterministic policy/docs rendering -> fresh worker proof -> trusted
external probe -> evidence -> authorized PR publisher -> GitHub read-back.
Web reads that state and submits authenticated scoped human decisions.
For the offline fixture, app and trusted probe have separate filesystems/process
namespaces and share only the run-owned loopback network namespace. No host-port
mapping is assumed for a `network=none` app. This must pass the early Docker spike.

## Concurrency and recovery
One active case per repo initially; two total sandbox slots is a reasonable starting
resource policy, to be adjusted to measured host capacity. SQLite is a prototype
choice, not multi-region durability. Persist phase, attempt, lease owner/expiry and
sandbox identity. Do not rely on an in-memory background coroutine to represent a
long-running job. On restart, inspect resource ownership, recover or terminate the
attempt, and never replay an external write blindly.

## Event identity
Dedupe delivery by provider + delivery ID. Dedupe logical verification by repo ID,
revision, target digest, runtime digest, and requested mode. Manual rerun gets an
explicit rerun identity, not accidental suppression. PR dedupe includes case/base
and candidate hash. Webhook signature + installation/repo authorization are both
required; a valid signature does not grant permission for every repository.

## Domain boundaries
Case state: queued, baseline_running, investigating, needs_input, proof_running,
repair_ready, publishing, pr_open, unresolved, stale, cancelled.
Run result: passed, failed, timed_out, infrastructure_error, policy_blocked.
Repository health: latest observed head, latest checked SHA, result at that SHA,
and separately open repair case. Avoid a single enum that collapses these facts.
LLM output may propose a transition but cannot issue authoritative completion.

## HTTP/API contract first, not endpoint sprawl
Implement only: configured repos; start run; inspect case/events/artifacts; answer a
pending decision; GitHub webhook/install callbacks. Every route checks identity,
repo authorization, and stale decision version. Serve artifacts by authenticated
case access, not arbitrary filesystem paths. Paginate logs and cap response sizes.
Typed domain result is the source for OpenAPI/frontend types; avoid hand-maintaining
three conflicting versions of the same payload.

## AWS migration without architecture churn
First prove Strands + Bedrock from the local trusted process. After a feasibility
spike choose one deployment: AgentCore for the trusted investigation agent plus an
authenticated capability service, OR keep that trusted backend together on a
suitable AWS host. AgentCore is not an instruction to run arbitrary repos there.
Run-owned sandbox execution remains a separate trust/resource boundary.
Use S3/artifact storage and durable state only when deployed and verified. Do not
add DynamoDB/queues/CodeBuild simply to populate a diagram. Any alternative runner
must meet the same external-probe, freshness, secret, and provenance requirements.
A cloud job with powerful credentials beside repository code is NOT compliant.

## Judge access
The final demo must be genuinely usable as documented. A downloadable controlled
test build is preferable to a public arbitrary-code-execution service. A web replay
view is useful but must say “recorded run”; it is not live execution. Do not make a
private always-on laptop a hidden dependency in the published setup instructions.
