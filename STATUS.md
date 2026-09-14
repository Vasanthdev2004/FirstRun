# Implementation status — observed evidence only

**Last authored:** 2026-09-14.
**Current checkpoint:** the M1 live gate passed on current `main` (`fe1ef55`) with
zero skips; the Strands model provider is now selectable (`e635781`) after the demo
AWS account's `bedrock-runtime` data plane was found to be blocked at account level.
No live agent run has been recorded yet. The real installed-App M3 workflow and
live-configured M4 acceptance remain outstanding.
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

## M4 review fix evidence

Commit `020f88c`. This slice is a defect review of the existing M4 checkpoint, not
new milestone scope. No production behavior changed beyond making one exception
handler resolvable, and no acceptance expectation was altered or weakened.

- `verification/local.py` listed `ContractFileError` in the first `except` clause of
  `verify_known_oracle` without importing it. Python evaluates that clause's tuple
  whenever any exception propagates out of the enclosing `try`, so every failure
  inside the M1 known-oracle harness raised `NameError` instead of returning a typed
  result. An unreachable Docker server produced a crash rather than
  `infrastructure_error`; a controller-asset policy failure produced a crash rather
  than `policy_blocked`. The name is now imported from `firstrun.domain.contracts`.
- Two regression tests were added to `tests/unit/test_local_verification.py` for the
  `WorkerProblem` and `SourcePolicyError` handler paths. With the import removed both
  fail with the original `NameError`, so the coverage is bound to the actual defect
  rather than to the corrected code.
- `tests/unit/test_strands_preflight.py` asserted the not-isolated live-preflight
  branch while reading the ambient interpreter flag. It therefore passed under plain
  `python` and failed under `python -I`, the mode used for the recorded M3 and M4 test
  runs, so the full suite had never been green in both modes at once. It now patches
  `firstrun.preflight.strands.sys.flags` with the `SimpleNamespace` idiom already used
  three times elsewhere in that file.
- The stale `Checkpoint commits` list above was left unchanged. It records M1 commits
  only; the M2, M3 and M4 checkpoint commits were never added to it. That gap is
  recorded here rather than silently repaired.

### Commands observed for this slice

- `.venv\Scripts\python.exe -I -m unittest discover -s tests -p 'test_*.py'`: exit 0,
  187 tests, 6 skipped (the live Docker integration tests; `FIRSTRUN_RUN_LIVE_M1` was
  unset). The same command reported one failure before this change.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'`: exit 0,
  187 tests, 6 skipped. The suite now passes in both interpreter modes.
- `.venv\Scripts\python.exe -I -m compileall -q backend/firstrun`: exit 0.
- `.venv\Scripts\python.exe tools/check_pack.py`: exit 0, 24 handoff-asset checks.
- `git diff --check`: exit 0.

No containers were launched, no provider or AWS requests were made, no OAuth exchange
occurred, and no runtime GitHub App writes occurred during this slice. `020f88c` is
committed locally and has not been pushed to `origin`.

## Live gate on current main — September 13

With Docker Desktop running on this machine, the full suite was run with the live
Docker integration tests enabled at `fe1ef55`:

- `FIRSTRUN_RUN_LIVE_M1=1 .venv\Scripts\python.exe -I -m unittest discover -s tests -p 'test_*.py'`:
  exit 0, **187 tests, OK, zero skips**, 30.1 s. All six live Docker integration tests
  passed. This re-establishes the M1 acceptance coverage (A01, A02, A06, A07, A08, A09,
  A13, A14, A15, A17) on the current default-branch revision rather than only on
  `c404d1e`.
- `.venv\Scripts\python.exe -I -m firstrun verify-local --repo fixtures/notes-app --target fixtures/notes-app/.firstrun/target.json`:
  exit 10 (`failed`). Source revision `fe1ef554cccc728378c41461850b9fc10688a3e8`,
  run `449f8902-f426-4915-b619-4ce5e1f461d5`; readiness passed, functional acceptance
  failed, cleanup passed. This is the committed fixture's intended broken baseline.
- `.venv\Scripts\python.exe -I -m firstrun doctor`: all checks passed once Docker was
  started; `provider: unknown` by design.

## Provider blocker diagnosis — September 13–14

The demo AWS account (`247670275693`, AWS India, `us-east-1`) cannot invoke any model
through `bedrock-runtime`. Every observation below was made directly; none is inferred.

- `sts get-caller-identity` succeeds for a non-root IAM user `firstrun-agent` with
  `AmazonBedrockFullAccess`. `ListFoundationModels` returns 120 models and
  `ListInferenceProfiles` returns 75 profiles including `global.anthropic.claude-opus-5`.
- `Converse` and `InvokeModel` both return `ValidationException: Operation not allowed`
  for every model tested — ten models across Amazon, OpenAI, DeepSeek, Qwen, Mistral,
  Google, Moonshot, NVIDIA, Z.ai and MiniMax — using base IDs and inference-profile
  IDs, in `us-east-1`, `us-west-2` and `ap-south-1`, as root and as the IAM user.
- The result was unchanged after each of: adding a verified payment method, the
  account-verification banner clearing, upgrading the account from the Free plan to a
  Paid plan, and redeeming the hackathon's $50 promotional credit ($190.00 in active
  credits, $0.00 used, including $20 issued specifically for Bedrock playground use).
- `ap-south-1` briefly returned `AccessDeniedException: Your account is currently being
  verified` before reverting to `Operation not allowed`.
- An AWS expert-accepted re:Post answer to an identical report describes an account-level
  anti-fraud hold on the `bedrock-runtime` data plane that is not exposed through any
  API and has no self-service resolution. AWS support case `178910367900309`
  ("Account verification pending; Bedrock access blocked", Account and billing) has
  been open since 2026-09-11 05:14 with no correspondence from AWS.
- The Bedrock Mantle endpoint (`bedrock-mantle.us-east-1.api.aws`, SigV4 with the same
  profile) authenticates the account and returns ordinary Anthropic API responses rather
  than the hold. It serves only the Anthropic messages API, and Anthropic models on it
  return `not available for this account` until Anthropic's one-time use-case submission
  is completed for the account. That submission was attempted in the console; the
  console banner remained and the API result was unchanged afterwards.

Promotional credits, support-plan upgrades and account-plan upgrades were each ruled
out as fixes by direct test, not assumption. The hackathon rules and FAQ were re-read on
September 13: Strands Agents is the required SDK; Amazon Bedrock and AgentCore are
encouraged for scoring and explicitly not required.

## Selectable provider — `e635781`

`RepairProviderConfig.provider_id` selects `amazon-bedrock` (default, unchanged),
`amazon-bedrock-mantle`, or `anthropic`. The agent, capability broker, tools, structured
output contract and controller-owned proof are identical on every path. Each provider
rejects the other providers' fields; each endpoint is validated against a pinned origin
before the agent is constructed. The Anthropic key is never a configuration value or
argument — only a path to an operator-controlled file, read inside the credentialed
child. `anthropic==1.5.0` is added to the `provider` extra and to the preflight's
verified-distribution set.

- `.venv\Scripts\python.exe -I -m unittest discover -s tests -p 'test_*.py'`: exit 0,
  **199 tests, 7 skipped** (6 live Docker with `FIRSTRUN_RUN_LIVE_M1` unset; 1 symlink
  assertion that cannot run on this Windows host — the directory-rejection half of that
  test runs).
- `.venv\Scripts\python.exe -I -m compileall -q backend/firstrun`: exit 0.
  `git diff --check`: exit 0. `uv lock`: resolved 56 packages.
- `repair-local` with each of the three providers and no credentials returns
  `policy_blocked` (exit 13).

No live agent run has been recorded on any provider. The provider layer makes one
possible; this section does not claim one.

## Next single task

Record one live agent run — A03, the Strands agent discovering the repair from tool
evidence with no answer key. It is the product's central claim and has never executed.
Whichever provider serves it must be stated in the recorded demonstration and in this
file, with the exact command and exit code. Then record the demonstration video and
complete the Devpost submission; `docs/SUBMISSION.md` holds the description and shot
list. Live M3 and M5 remain unscaffolded and are out of scope for the submission window.
Do not describe M2/M3/M4 as accepted until their outstanding real acceptance checks and
the noted M4 cancellation gap are addressed.
