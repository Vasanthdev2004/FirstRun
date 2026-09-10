# M3 GitHub workflow — local activation

The implementation connects signed push delivery to durable SQLite cases, the
existing isolated repair worker, an exact-commit fresh proof, and a reconciled
GitHub repair PR/check. It is not yet live-validated. M2 live Strands acceptance is
also pending. No GitHub App, webhook tunnel, demo repository, or AWS resources were
created by this implementation task.

## Owner configuration required

Use a GitHub App owned by your account, installed on **only the selected controlled
demo repository**. The developer's `gh` login is not a replacement for App
installation credentials. Development commits use the repository owner's Git
identity; runtime App operations are visibly attributed to that App on GitHub.
Set the repair commit author/committer to an identity you own and approve.

Request metadata read, contents read/write, pull requests read/write, and checks
read/write. Subscribe to push events. Do not enable Actions administration,
organization permissions, deployments, or merge behavior. Configure a strong
random webhook secret and HTTPS webhook URL ending `/github/webhook`. Keep the App
private key and webhook secret in protected local files outside the checkout.
Never paste their contents into chat, commit them, or put them in a repository
environment file. The configured runtime reads them only in trusted integration
code; repository archives and the Strands child never receive them.

Only the committed controlled `fixtures/notes-app` source lane is supported. You
can place that exact fixture at the demo repo root (`source_prefix: ""`) or at the
same prefix in a containing repository (`source_prefix: "fixtures/notes-app"`).
The controller additionally compares every protected file against its own committed
fixture. A valid installation alone does not authorize new application code.
Submodules, LFS, links, executable source entries, binary files, forks, and other
ecosystems are unsupported. README text outside the managed setup block cannot be
changed by a repair.

Install the locked optional integration dependencies:

```powershell
uv sync --frozen --extra github --extra provider
.venv\Scripts\python.exe -I -m firstrun github-config-schema
```

Create a private local JSON configuration matching that printed schema. There are
no pretend working IDs, hashes, or provider defaults. The top-level fields are
`registration`, `private_key_path`, and `webhook_secret_path`. Registration needs:

- The App, installation, and numeric repository IDs; exact owner/name/branch.
- `approved_sha`: a full approved commit, plus `source_prefix`.
- SHA-256 digests of the approved raw target and initial recipe bytes, the
  protected file map (`integrations.github.protected_digest`), and the trusted
  verifier (`worker.docker.TRUSTED_VERIFIER_DIGEST`). Include `policy_revision` and
  `policy_digest` from `verification.policy.CONTROLLED_NODE_FIXTURE_POLICY` and
  `CONTROLLED_NODE_FIXTURE_POLICY_DIGEST` as well.
- Both the locally available runtime repository digest (`runtime_digest`) and
  Docker image ID (`runtime_image_id`). These are checked before execution.
- Owner-approved `author_name` and `author_email`. For this development account:
  `Vasanth T`, `148849890+Vasanthdev2004@users.noreply.github.com`.
- Explicit `automatic_prs: true` to permit external repair/check writes. The
  default is false. Enabling it is an installation policy decision.

Approve the target and **initial recipe at approved_sha**. Subsequent branch heads
may change the recipe only within the supported semantic policy; protected code or
target changes stop for a new approval. Dedupe includes the full registration,
including runtime/verifier identities, so changing approval creates new evidence.

## Run the two local processes

The webhook service only validates and persists events. It does not spawn a
background agent or accept shell commands. It binds loopback; supply an
operator-managed HTTPS reverse proxy before GitHub can deliver events. This task
does not provision one. The health route reveals no repository data; case and
artifact routes await M4 authentication.

```powershell
.venv\Scripts\python.exe -I -m firstrun github-serve --config C:\FirstRunPrivate\github.json --database .local\firstrun.sqlite
```

Run the separate worker for one queued case (replace provider placeholders with
your verified temporary non-root profile and enabled region/model). This can
launch isolated Docker containers and incur bounded model charges:

```powershell
.venv\Scripts\python.exe -I -m firstrun github-worker --config C:\FirstRunPrivate\github.json --database .local\firstrun.sqlite --aws-profile YOUR_PROFILE --region YOUR_REGION --model-id YOUR_MODEL --acknowledge-provider-cost --confirm-verified-temporary-non-root-credentials
.venv\Scripts\python.exe -I -m firstrun github-case --database .local\firstrun.sqlite --case-id CASE_ID
```

Each worker invocation claims at most one case. Invoke it again to drain another
case or reconcile a pending publish; no queue cluster or hosted scheduler is
required for this checkpoint. Full evidence is held in content-addressed artifact
files next to the database; SQLite contains digest references. Artifact writes are
atomic and reads recheck the content hash. The PR
links to a bounded GitHub check proof summary. Protect the SQLite directory and
backups with owner-only filesystem access. Do not expose its contents as static
web artifacts.

## Publication and restart behavior

1. Verify signature over raw bytes, then installation/repo/branch authorization;
   dedupe delivery and logical case atomically. No fetch occurs in ingress.
2. Fetch exact commit/tree/blob bytes with a selected-repo installation token,
   validate hashes and controlled-source approval, then drop credential material
   before passing the source snapshot to worker/agent capabilities.
3. Persist bindings before execution, retain bounded evidence, and quarantine an
   interrupted execution instead of trusting its leftover state. One active case
   per repository; owner tokens fence every state transition and external write.
4. Persist the verified candidate before creating immutable Git tree/commit
   objects. Their hashes are computed locally, then retrieved from GitHub. A
   crashed object write can safely reconcile/recreate the same content-addressed
   object. The candidate commit has a deterministic owner-author identity/date.
5. Re-fetch that actual commit and independently execute it with **no LLM** and
   new mutable state. Require the original target/verifier/policy/runtime and
   candidate bytes, plus successful external functional probe and cleanup.
6. Create a stable repair branch, check only that tested commit, then create the
   PR. Read back the branch, exact head, changed-file blob IDs, and check contents.
   Never auto-merge, force-push, or change the configured default branch.
7. A moved default head marks the old case stale and queues a new case. Successful
   repair checks do not mark main healthy; a subsequent push/merge needs a fresh
   current-head run.

An uncertain PR/check/ref write is **not resubmitted** when read-back finds nothing.
It remains `reconcile_pending`, even across restart. A visible matching object can
complete reconciliation; an absent or conflicting object needs operator review.
This conservative boundary can pause liveness but prevents duplicate external
writes. A closed repair PR is never reopened or replaced automatically.

An expired execution lease becomes `interrupted` and blocks further execution for
that repository. Persisted phase/binding IDs and controller Docker labels identify
the owned resources; do not prune globally. Automated crash cleanup/recovery and a
human recovery command remain an activation limitation. Do not edit SQLite to
pretend cleanup succeeded. Normal worker completion retains exact-ID cleanup.

## Acceptance still needed before M3 sign-off

- Complete M2 acceptance and one explicitly authorized live Strands repair.
- Deliver a genuine signed event from the installed demo App; confirm source SHA,
  recipe/README correspondence, actual failed baseline, isolated repair, exact
  commit fresh proof, real PR, actual head/files, and check SHA by read-back.
- Repeat the same delivery; confirm one logical case and one PR/check.
- Interrupt after each external write and before journal confirmation; restart
  and confirm reconciliation without duplicate writes.
- Move default head during proof/publication; confirm stale/new case and no green
  default-branch claim. Merge as the owner, then verify the resulting new SHA.
- Exercise worker interruption/cleanup ownership before unattended operation.

The focused offline checks in STATUS.md are development evidence, not evidence
that an App installation or this full workflow has worked live.

Protocol references: [App installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app),
[webhook signatures](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries),
[Git trees](https://docs.github.com/en/rest/git/trees?apiVersion=2026-03-10),
[Git commits](https://docs.github.com/en/rest/git/commits?apiVersion=2026-03-10),
and [check runs](https://docs.github.com/en/rest/checks/runs?apiVersion=2026-03-10).
