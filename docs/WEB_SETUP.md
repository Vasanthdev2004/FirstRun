# M4 web product — local development

The Next.js web app uses the existing Python controller API, SQLite cases and
content-addressed artifacts. The API never runs repository code or starts a repair
agent. A separate, explicitly configured worker consumes queued cases. There is
no demo login, invented run feed, second database or hosted deployment.

## Open the UI without credentials

Use the installed Python 3.12 environment with the locked `github` extra. Start
the API in one terminal and the frontend in another:

```powershell
New-Item -ItemType Directory -Force .local
.venv\Scripts\python.exe -I -m firstrun web-serve --database .local\firstrun.sqlite
```

```powershell
cd apps/web
npm ci --ignore-scripts
npm run dev -- --hostname 127.0.0.1
```

Open [FirstRun locally](http://localhost:3000). Without owner configuration the
page explains setup; it cannot list repositories, queue work or serve artifacts.
`GET /health` proves only that the API is responding, not that Docker, GitHub or
Bedrock is available. UI development does not require Docker.

## Activate GitHub sign-in

First complete the explicit repository/App approval in [GITHUB_SETUP.md](GITHUB_SETUP.md).
The M4 screen displays that approved target and initial recipe; it does not invent
an approval for an arbitrary repository or allow the repair agent to change it.
Broad multi-repository onboarding is outside this controlled prototype.

Enable user authorization on that same GitHub App and register the exact callback:
`https://YOUR_ORIGIN/api/auth/callback`. App installation is not user login.
Use an operator-managed HTTPS origin in non-local environments. No tunnel or cloud
resources are provisioned by this implementation.

Create private OAuth JSON outside the checkout, with these fields:

- `client_id`: that App's actual OAuth client ID (not the numeric App ID).
- `client_secret_path`: absolute path to the protected OAuth client secret file.
  Store only the secret bytes, without a trailing newline; never paste them into
  chat, the frontend, repository files or a command argument.
- `public_origin`: exact frontend origin, without a trailing slash or path.
- `allow_insecure_localhost`: default `false`. For explicit loopback development
  only, `true` permits an exact HTTP localhost/127.0.0.1/::1 origin. Never use this
  exception on a public address.

```powershell
.venv\Scripts\python.exe -I -m firstrun web-serve --config C:\FirstRunPrivate\github.json --auth-config C:\FirstRunPrivate\web.json --database .local\firstrun.sqlite
```

Use exactly the configured origin in the browser; `localhost` and `127.0.0.1` are
different cookie/CSRF origins. The Next.js server proxies `/api/*` to the private
API origin. Its server-only `FIRSTRUN_API_ORIGIN` defaults to
`http://127.0.0.1:8765`; do not prefix it with `NEXT_PUBLIC_`. The worker must use
the same SQLite database and sibling `artifacts` directory as the API.

Login uses one-use state, a browser nonce and PKCE. A GitHub user token identifies
the user and checks access, then is discarded. Browser sessions are opaque,
HttpOnly, same-site, expire after one hour, and are stored as hashes. Every data
request rechecks the user's current permission for the configured repository;
write/admin is required for mutations. Mutation requests also require the exact
Origin and session CSRF nonce. Logout revokes the local session.

## What the screens mean

- Repository health refers only to the stored checked default-branch SHA. A repair
  PR and candidate proof are separate; merging requires a new current-head run.
- The contract screen is the approved snapshot, not a claim about the current
  branch. It shows the pinned target, recipe steps, managed README instructions,
  and version digests.
- Cases show persisted phases, selected-stage details, bounded sanitized command
  tails, candidate diff and exact proof identities. No raw artifact JSON, provider
  configuration, private model reasoning or arbitrary file path is exposed.
- Run/recheck only enqueue work; keep the separate M3 worker operational. Request
  UUIDs make retries idempotent. Human decisions bind repository, SHA and current
  case version; stale decisions return conflict instead of changing a new case.
- A NeedsInput recheck starts a distinct case after the owner fixes the missing
  fact/configuration outside the agent. There is no secret-entry field, credential
  broker, target override, automatic approval, merge or deployment action.
- Queued cancellation is immediate. Active cancellation remains requested until
  the bounded worker phase finishes and its cleanup is verified, before further
  work/publication. Unconfirmed cleanup remains quarantined. This is **not an
  immediate active-process kill switch**; that stronger security gate is pending.
  Publishing/reconciliation cannot be cancelled through a stale UI decision.

## Focused build checks

```powershell
.venv\Scripts\python.exe -I tools/export_web_types.py --check apps/web/src/lib/api-types.ts
.venv\Scripts\python.exe -I -m unittest discover -s tests -p 'test_m4_*.py' -v
cd apps/web
npm run typecheck
npm run build
```

Public TypeScript DTOs are generated from Pydantic; do not hand-maintain duplicate
contracts. The frontend renders untrusted text as text, never raw Markdown/HTML.
Production builds use local/system fonts and need no font-provider connection.

M4 implementation/build checks do not establish live OAuth, M2 provider or M3
installed-App acceptance. See [STATUS.md](../STATUS.md) for commands actually run
and remaining gates.

Protocol reference: [GitHub App user authorization](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-with-a-github-app-on-behalf-of-a-user).
