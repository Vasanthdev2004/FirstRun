# Devpost submission pack

Everything below is drafted to match what the repository actually does. Before
submitting, fill the two placeholders marked `<<...>>` with what was really recorded.
The rules require the project to "function as depicted in the video and/or expressed in
the text description" — do not describe anything the recording does not show.

Deadline: **Monday, September 14, 2026, 5:00 pm Pacific** (= Tuesday 05:30 IST).

---

## Submission checklist (from the Official Rules and FAQ)

| Requirement | Where it is |
|---|---|
| Built with Strands Agents SDK | `backend/firstrun/agent/` — `strands.Agent`, five `@tool`s, `structured_output_model` |
| One track | **Professional Agents** |
| Public repo, MIT/Apache license visible in About | `https://github.com/Vasanthdev2004/FirstRun` — MIT, detected by GitHub |
| README | `README.md` |
| Architecture diagram | Mermaid flowchart in `README.md` — also export a PNG for the Devpost image field |
| Video ≤ 5 min, public YouTube/Vimeo, working demo + pitch (problem / who / why) | `<<VIDEO_URL>>` |
| AWS Builder ID | the email used at `profile.aws.amazon.com` |
| Text description | below |
| Prior-work disclosure | README "Prior work disclosure" — the spec pack commit `a8f6c0e` |
| Testing access for judges | README "Running it" — everything runs locally with Docker |
| (Optional) live demo link | not provided; see "What's next" |

---

## Devpost fields

**Project name:** FirstRun

**Tagline:** Keeps your README's setup steps actually working — and proves it from a
clean machine before it opens the PR.

**Track:** Professional Agents

**Built with:** Strands Agents SDK, Python 3.12, Pydantic, Docker, Node 22, Next.js,
FastAPI, SQLite, Amazon Bedrock, Anthropic Claude, GitHub Apps

### Description

**The problem.** Every repository has a "getting started" section. It worked the day
someone wrote it. Then a migration gets added to the code but not the README, a script
is renamed, a dependency starts needing an extra step. CI never notices — it runs on a
warm machine with a populated cache and a database that already has its tables. The
person who finds out is the newcomer on day one, at the exact moment they have the
least context. This happens constantly, to almost every project, and nobody owns it.

**Who it's for.** Maintainers, indie developers, and small teams who care whether a new
contributor can actually get their project running.

**What FirstRun does.** It continuously tests the declared setup path from a genuinely
clean state — a fresh, non-root, network-less container with nothing cached. When the
path breaks, a Strands agent investigates the real failure using five narrow tools,
proposes a bounded repair to the setup recipe, and that repair is then
**independently re-verified from scratch** in a second clean container the agent never
touched. Only then is a pull request opened. It never auto-merges, and it never claims
something works because a model said so.

**The idea that makes it work.** Three artifacts with different authority. The
*target* says what must succeed and is frozen — humans only. The *recipe* says how to
get there and may be repaired by the agent under policy. The README's setup block is
rendered deterministically from the recipe, and any divergence blocks execution — so
the docs can never drift from the commands actually tested. The agent can propose a new
recipe; it cannot touch the target, the verifier, the runtime, or its own proof.

**Health is not success.** The demo fixture is a small Node + SQLite notes API whose
table is intentionally never created. Its `/health` returns 200. FirstRun's acceptance
probe runs outside the repository: it POSTs a note with a random nonce and reads it back.
The broken setup passes readiness and fails acceptance — which is the entire point.

**How the Strands agent is built.** The credentialed Strands child process receives no
repository archive, no host path, no Docker endpoint, and no proof capability — only
five bounded tools over a private pipe: read a pinned source file, search pinned source,
inspect the pinned diff, read baseline evidence, and run one script already declared in
the committed `package.json`. Every tool call is replay-checked, byte-capped, and
recorded as evidence. The agent must return typed structured output — `repair_proposal`,
`needs_input`, or `blocked` — and its proposal is only an *input* to a controller-owned
policy check and a fresh proof run.

**What we actually proved.** The verification engine passed its full live gate with
real Docker containers — 199 tests, including the complete baseline-fails /
repair-proves cycle, freshness marker checks, and protected-input rejection. The
recorded demonstration shows `<<DEMO_SUMMARY: e.g. "the broken fixture failing its
functional probe while /health returns 200, then the Strands agent proposing the
missing migration step and the controller proving it in a fresh container">>`.

**Challenges, honestly.** The AWS account used for this submission has an
account-level hold on the `bedrock-runtime` data plane: every invocation returns
`ValidationException: Operation not allowed` regardless of model, region, or identity,
with a support case open since September 11. Rather than fake it, we made the model
provider selectable — Amazon Bedrock, Bedrock's Mantle endpoint, or Anthropic direct —
with the endpoint pinned and validated on each path. The recorded run uses
`<<PROVIDER_USED>>`. One flag switches providers; nothing else in the run changes.

**What's next.** Live GitHub App activation on a demo repository, the web workspace
against real case data, and hosted execution. All three are implemented and
offline-tested; none has been exercised live within the submission window, and the
README says so.

---

## Video shot list (target 4:30)

Screen recording plus voiceover. No camera needed. Record the terminal at a large font.

| Time | On screen | Say |
|---|---|---|
| 0:00–0:35 | Title card, then a README with a "Getting started" section | "Every repo has one of these. It worked the day it was written. Then someone adds a migration to the code and not the docs. CI doesn't notice — it runs on a warm machine. The person who finds out is the newcomer on day one. FirstRun is for maintainers who want that path to actually work." |
| 0:35–1:20 | Terminal: `verify-local` on the fixture | "This is a real Node app in a fresh, network-less, non-root container. Watch: install passes, start passes, `/health` returns 200 — and it *fails*. Because the functional probe tried to save a note and the table doesn't exist. Health is not success. Nothing in CI catches this." |
| 1:20–1:50 | `fixtures/notes-app/.firstrun/target.json` and `recipe.json` side by side, then the README block | "Three files with different authority. The target is what must succeed — frozen, humans only. The recipe is how — the agent may repair it. The README block is rendered *from* the recipe; if they ever disagree, execution is refused." |
| 1:50–3:10 | Terminal: `repair-local --provider <<PROVIDER_USED>> ...` running | "Now the Strands agent. It gets five tools and nothing else — no shell, no repo archive, no Docker. It reads the baseline evidence… searches the source… finds `db:migrate` already declared in package.json… and proposes a recipe with that step added. It's structured output — it can't just say 'fixed'." Pause on the proposal. "And now the part that matters: the controller applies exactly those bytes to a *new* clean container the agent never touched, and runs the same probe." Pause on `proof: passed`. |
| 3:10–3:40 | Test output: `FIRSTRUN_RUN_LIVE_M1=1 … 199 tests OK` | "That isn't a story. The full live gate — baseline fails, repair proves, freshness markers absent, protected files untouchable — runs in real Docker on every commit." |
| 3:40–4:10 | README status table and "Model providers" section | "One honest note. Our AWS account has a hold on Bedrock invocation — a support case has been open for days. So we made the provider selectable with a pinned, validated endpoint on each path, and the run you just saw used `<<PROVIDER_USED>>`. We'd rather show you that than pretend." |
| 4:10–4:30 | Architecture diagram | "Repository code never runs where credentials live. The agent gets tools, not a shell. And no run ever shares state with the one before it. That's FirstRun." |

**Do not** narrate anything the recording doesn't show. If the agent run is not
recorded, cut 1:50–3:10 and say plainly that the loop is implemented and offline-tested.

---

## Recording commands

```bash
uv run --frozen python -m firstrun verify-local --repo fixtures/notes-app --target fixtures/notes-app/.firstrun/target.json
```

```bash
uv run --frozen python -I -m firstrun repair-local --repo fixtures/notes-app --provider <<PROVIDER_USED>> <<PROVIDER_FLAGS>> --model-id <<MODEL_ID>> --acknowledge-provider-cost --confirm-verified-temporary-non-root-credentials
```

```bash
$env:FIRSTRUN_RUN_LIVE_M1='1'; uv run --frozen python -m unittest discover -s tests -p 'test_*.py'
```
