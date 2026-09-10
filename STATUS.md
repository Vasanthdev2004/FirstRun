# Implementation status — observed evidence only

**Last authored:** 2026-09-10.  
**Current checkpoint:** M4 local web implementation is present at the owner's
request to continue development. M2 acceptance/live provider validation, the real
installed-App M3 workflow and live-configured M4 acceptance remain outstanding.
Full Docker/provider/App tests are deferred, not passed.
**Product target:** web app + GitHub App; Next.js now fronts the existing trusted
Python controller, with execution kept in the separate worker.

| Capability | State | Evidence |
|---|---|---|
| v2 specification and controlled fixture | Present and pack-checked | `uv run --frozen python tools/check_pack.py` — exit 0; 24 handoff-asset checks |
| M0 Python/outcome foundation | Implemented | Python 3.12; stable exits 0/10/11/12/13/14/15; locked dependencies |
| M0 Docker preflight | Live passed on this machine | Docker Desktop Linux engine, local named pipe, digest-pinned Node image, isolated verifier, exact-ID cleanup |
| M0 Strands/Bedrock preflight | Implemented and unit-tested; live provider check deferred | No profile, region, model, spend authorization, or independently verified temporary non-root identity was supplied; no AWS request or credential-content access occurred |
| M1 target and recipe contracts | Implemented | Strict Pydantic models reject extra fields, ambiguous paths, shell text, invalid steps, and verifier-in-recipe input |
| M1 README/recipe contract | Implemented | Recipe is executable source; managed README bytes are deterministically rendered and divergence is policy-blocked |
| M1 committed-source capture | Implemented | Exact `HEAD^{commit}` and Git trees; regular 0644 blobs only; bounded canonical archive; no working-tree bytes, filters, links, executable entries, or repo-local Git/Docker binaries |
| M1 baseline and proof worker | Implemented | Independent non-root, read-only, capability-dropped, seccomp-confined, no-egress Docker containers with tmpfs state and controller deadlines |
| M1 acceptance | Implemented | Controller-owned external create/read nonce probe; health alone cannot pass; model/client payloads cannot certify success |
| M1 evidence and cleanup | Implemented | Typed evidence binds commit/tree, source archive, candidate patch/tree, target, recipe, README, verifier, policy, runtime digests, commands, observations, identities, and cleanup; leaked owned resources quarantine the endpoint |
| M2 Strands repair loop | Implemented; acceptance pending | `repair-local` now performs baseline capture, bounded Strands investigation, controller-rendered recipe/README candidate construction, investigation teardown, and independent proof. Fake agents are test-only and cannot produce live milestone evidence |
| M3 GitHub workflow | Implemented local integration slice; live activation/acceptance pending | Signed selected-repo push ingress, SQLite dedupe/leases/write journal, content-addressed artifacts, exact-SHA source fetch, bounded worker, exact-commit fresh verification, reconciled PR/check publisher. No live App/webhook/repair PR/check used yet; never auto-merge |
| M4 web product | Local implementation and focused build checks; live acceptance pending | Next.js repository/contract/case views, authenticated projected evidence, OAuth sessions, versioned run/cancel/recheck actions; real unconfigured desktop/mobile UI inspected |
| M5 hosted execution | Not implemented | No cloud resources provisioned |

## M1 acceptance coverage

- A01 and A09: the committed fixture becomes healthy but its functional create/read
  probe fails.
- A02 and A06: the allowlisted known recipe repair passes only in a separately
  created workspace with a distinct run, attempt, workspace, app, and verifier;
  seeded hidden-state markers must be absent.
- A07, A08, A13, and A14: protected-target edits, README divergence, untrusted proof
  claims, arbitrary repositories, traversal, links, and host executable shadowing are
  rejected.
- A15: successful candidate proof does not alter the committed broken baseline.
- A17: incomplete cleanup yields `cleanup_failed`, bounded evidence, and a persistent
  process-level Docker-endpoint quarantine.

The exact M1 checkpoint command was:

`$env:FIRSTRUN_RUN_LIVE_M1='1'; uv run --frozen python -m unittest discover -s tests -p 'test_*.py' -v`

It passed all 125 unit tests and 6 live Docker integration tests with no skips on
exact revision `c404d1e9491bdbbc48d13bea8ff6c8c76d3da5fe`.

## M2 implementation evidence

- `backend/firstrun/domain/repair.py` defines strict, bounded provider config,
  typed `repair_proposal` / `needs_input` / `blocked` results, per-attempt decisions,
  and controller-owned tool/run evidence.
- `backend/firstrun/agent/` contains the live Strands adapter and parent-owned
  capability broker. The credentialed child receives no repository archive, host
  path, Docker endpoint, verifier, proof capability, or unrestricted command tool.
- `backend/firstrun/worker/investigation.py` creates eager fresh mutable state and
  permits only scripts declared by pinned `package.json`; exact cleanup must
  reconcile before proof.
- `backend/firstrun/orchestration/repair.py` keeps the target/verifier/policy/runtime
  frozen, renders README from the proposed recipe, binds candidate bytes/digests,
  and accepts success only from a separate fresh controller proof.
- `python -m firstrun repair-local --repo <approved-path>` is implemented with
  explicit profile, region, model, cost acknowledgement, and temporary non-root
  identity confirmation. Live use fails closed unless Python isolated mode (`-I`)
  is active.
- Focused checks on this working tree: backend `compileall` exit 0; M2 schema/import
  smoke exit 0; CLI help exit 0; invalid provider config and missing authorization
  both returned typed `policy_blocked` exit 13. No provider request was made.

Full M2 unit/integration/evaluation coverage and an authorized live Strands repair
run have not been run. This checkpoint is therefore not recorded as M2 accepted.

## Checkpoint commits

- `115f825` — strict M1 contracts and evidence foundation.
- `5b9f83f` — isolated local baseline/known-oracle verification.
- `f759d95` — source, host-execution, output-bound, provenance, cleanup, and
  acceptance hardening.
- `c404d1e` — exact cleanup lifetime fix and passing M1 live gate.

## Known limitations and blockers

1. Live Strands/Bedrock validation remains unknown and requires the owner-selected
   profile, exact region/model, explicit cost acknowledgement, and confirmation of
   independently verified temporary non-root credentials. This is an M0 provider
   blocker deferred while the owner requested the local project engine first.
2. M1 intentionally supports only the controller-owned `fixtures/notes-app` lane and
   a controller-owned known repair oracle. It does not accept arbitrary repositories.
3. M1 source materialization uses a single bounded Docker CLI argument. The current
   committed fixture is safely below Windows' argument limit; widening the source
   policy requires a trusted chunked/streaming transport first.
4. The local M2 CLI returns evidence directly. The M3 worker now persists case
   phases, attempt bindings, leases, artifact references and external-write
   intents/results. Interrupted execution is quarantined across restart; automatic
   crash cleanup and an operator recovery command remain pending. Do not clear
   interrupted state without inspecting owned resources and establishing cleanup.
5. Live M3 requires an owner-selected controlled demo repository, least-privilege
   GitHub App installation, protected key/secret paths, approved input/runtime
   digests, an operator-managed HTTPS webhook endpoint and explicit automatic-PR
   policy. None was provisioned or inferred from the developer's `gh` login.

## M3 implementation evidence

- `backend/firstrun/webhooks.py` authenticates raw bytes before decoding, then
  matches installation, numeric repo, owner/name, non-fork branch and full SHA.
- `backend/firstrun/github_state.py` persists delivery/logical-case dedupe,
  repository health, fenced leases, phase events and immutable write-ahead intents.
  `backend/firstrun/artifacts.py` atomically stores bounded content-addressed JSON
  and verifies digests on read. No public artifact route exists before M4 auth.
- `backend/firstrun/integrations/github.py` uses App JWTs and exact-repo installation
  tokens over fixed-origin HTTPS. Fetching is bounded Git commit/tree/blob access;
  protected source must match the controller's committed fixture. No Git credentials
  are passed to the code sandbox or Strands child.
- `backend/firstrun/orchestration/github.py` reuses M2 through `repair_snapshot`,
  persists candidate proof, materializes deterministic Git objects, then performs
  a second fresh no-LLM run at the actual repair commit via `verify_snapshot`.
  Branch/PR/check writes require configured permission and read-back; uncertain
  non-idempotent writes remain reconciliation-only across restart. Changed branch
  heads produce stale/new cases; main health remains separate from repair checks.
- The prepared-source seam preserves the original M1 local baseline restrictions
  while permitting the reviewed repaired recipe on a later default-branch commit.
  Both runtime image ID and repository digest are checked before execution.
- CLI commands: `github-config-schema`, `github-serve`, `github-worker` (one case per
  invocation), and `github-case`. API ingress is loopback-only and never runs the
  worker in a background coroutine. Activation: `docs/GITHUB_SETUP.md`.
- Fixed the existing M2 baseline-failure marker lookup: complete run evidence uses
  `fresh_state.workspace_marker_digest`; partial attempts use their direct field.
- `uv lock` and `uv sync --frozen --extra github --extra provider` exited 0. The
  GitHub extra pins FastAPI 0.141.1, Uvicorn 0.52.4 and PyJWT[crypto] 2.13.0;
  existing dependency pins remain unchanged.
- `.venv\Scripts\python.exe -I -m unittest discover -s tests -p 'test_m3_*.py' -v`
  exited 0: 33 focused offline tests passed, no skips. Coverage includes signature
  order/authorization, durable dedupe/fencing/quarantine, permission/token/source
  bounds, Git tree integrity, uncertain-write reconciliation after store reopen,
  pre- and post-run staleness, distinct timeout/infrastructure check conclusions,
  artifact identity, private API surface, runtime pins and the prepared-source seam.
- `.venv\Scripts\python.exe -I -m compileall -q backend/firstrun` exited 0.
  Isolated `firstrun --help`, `github-config-schema`, and imports of the GitHub
  orchestrator/FastAPI ingress exited 0. `git diff --check` exited 0.
- The seam review also ran 24 existing local-verification/worker unit tests, all
  passing with mocked execution. The full suite and Docker/provider/App integration
  tests were not run. The ingress smoke emits a dependency deprecation warning
  from Starlette/AnyIO; it does not fail.
- An earlier failing test left a synthetic SQLite/secret fixture directory in the
  OS temporary folder. Its handle-lifetime bug is fixed and final tests clean up;
  removal of the old residue was denied by host policy. This is test data, not a
  live sandbox, real credential, or application artifact.

No fixture application scripts ran on the host, no containers were launched, no
real credential contents were inspected, and no AWS calls or runtime GitHub App writes occurred
during this slice. Development repository pushes use Vasanthdev2004's account.

## M4 implementation evidence

- `apps/web` is a pinned Next.js 16.3.4 / React 19.3.0 / TypeScript 7.0.2 app.
  It displays repository default-branch health separately from repair status,
  approved target/recipe/README, case history, selected execution stages, bounded
  logs, candidate diff, proof identities, and reconciled GitHub links. There is
  no demo login, invented repository row or sample passing run.
- `backend/firstrun/web_auth.py` implements GitHub App user OAuth with one-use
  state, browser binding, S256 PKCE, exact repository/user permission checks and
  opaque one-hour server sessions. GitHub user tokens are not persisted or sent
  to the browser/model. Every API read rechecks current repository permission;
  writes require write/admin, exact Origin and a session CSRF nonce.
- `web_service.py` projects only case-bound browser DTOs from existing SQLite and
  artifact storage. Evidence is rebound to exact case/source/approval identities;
  raw provider configuration, tool payloads, credentials and private reasoning
  are not public artifacts. `domain/web.py` and `domain/web_session.py` generate
  the shared frontend types through `tools/export_web_types.py`.
- `web_api.py` adds authenticated repository/case/evidence routes and bounded
  mutation bodies. UUID request identities support retry dedupe. Human decisions
  bind repository, SHA and current version; conflicts do not alter stale cases.
  Store migration adds version/decision bookkeeping without replacing old data.
- `web-serve` supports a fail-closed unconfigured setup surface. Next proxies
  same-origin `/api/*` to the private API; neither process starts the worker.
  Operator startup and OAuth configuration are documented in `docs/WEB_SETUP.md`.
- Cancellation is immediate for queued cases and cooperative at bounded phase
  boundaries for active cases. Successful cancellation requires confirmed cleanup;
  missing/failed cleanup stays interrupted/quarantined. Publication/reconciliation
  rejects new UI cancellation requests. This **does not yet meet SECURITY.md's
  stronger immediate run-owned active-process kill requirement**. That requirement
  is preserved, not weakened or claimed as accepted.
- Owner approval remains the private operator registration from M3, displayed
  read-only in the web app. An in-browser target/initial-recipe approval workflow
  is not implemented. NeedsInput supports explicit external resolution followed
  by a new case, or cancellation; it is not a secret broker or target editor.

### Commands observed for this slice

- `npm install --ignore-scripts` in `apps/web`: exit 0, 28 packages installed;
  npm reported 0 audit vulnerabilities. No backend dependency pins changed.
- `npm run typecheck`: exit 0. `NEXT_TELEMETRY_DISABLED=1 npm run build` (PowerShell
  environment assignment): exit 0, all three page routes compiled successfully.
- `.venv\Scripts\python.exe -I tools/export_web_types.py --check apps/web/src/lib/api-types.ts`:
  exit 0; generated public types match the Pydantic contracts.
- `.venv\Scripts\python.exe -I -m compileall -q backend/firstrun`: exit 0.
- `.venv\Scripts\python.exe -I -m unittest discover -s tests -p 'test_m3_*.py' -q`:
  exit 0, all 33 existing offline M3 regressions passed. The existing
  Starlette/AnyIO deprecation warning remains non-failing.
- `.venv\Scripts\python.exe -I -m unittest discover -s tests -p 'test_m4_*.py' -q`:
  exit 0, 21 focused tests passed, no skips. Covers OAuth/session/permission/CSRF,
  authenticated API/body bounds, cleanup-aware cancellation, typed source-bound
  projections and stale/idempotent decisions. An initial discovery-import error
  in the new service test was fixed without changing acceptance expectations.
- First `web-serve --database .local/m4-preview.sqlite` attempt was blocked because
  the parent directory did not exist (typed policy-blocked response). Created only
  the workspace `.local` directory, then the same command started successfully on
  loopback port 8765. No credentials were configured or read.
- `npm run dev` started on loopback port 3000. Browser loaded the real API-backed
  unconfigured screen. Check connection retained the correct unconfigured state;
  developer warnings/errors were empty. Desktop 1280x720 and mobile 390x844 had
  no page overflow and visible keyboard focus. Independent visual review: PASS
  for this unconfigured surface only. Authenticated views were not visually
  verified. `DESIGN.md` captures the implemented system; no shipping raster assets.
- Impeccable's one scoped mechanical detector run returned `[]`, exit 0.
  `git diff --check` exited 0. Newly auto-generated Next agent instruction files
  were removed and their generation disabled; root `AGENTS.md` is unchanged.

No fixture scripts ran on the host; no containers, provider calls, real OAuth
exchange, runtime App writes, cloud provisioning or deployment occurred. Local
UI preview is development evidence, not proof of the complete FirstRun workflow.

## Next single task

Review the M4 implementation checkpoint. Live activation still requires the
owner-approved demo repository, App/OAuth configuration and provider authority.
M5 deployment needs an explicit hosting/security/spend choice before provisioning.
Do not describe M2/M3/M4 as accepted until their outstanding real acceptance checks
and the noted M4 gaps are addressed. M5/M6 have not been scaffolded.
