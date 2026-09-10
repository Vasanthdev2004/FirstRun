# FirstRun — product specification v2

## Promise
Keep the declared first-time developer setup path working. When it breaks,
investigate the actual failure, repair the allowed instructions/configuration,
verify that exact candidate from clean state, and open a reviewable pull request.

**Audience:** maintainers, indie developers, small engineering teams.
**Platform:** web app + GitHub App. A runner and internal CLI support the product.
**Track recommendation:** Professional Agents; primary user is a developer.
**Working name:** FirstRun. No claim about domain or trademark availability.

## What the customer does
Install on selected repos, choose the supported environment and intended smoke
outcome, and approve the initial setup recipe. The app displays the commands it
will test and the README section that presents them to newcomers.
Subsequent configured push/PR events initiate verification. A healthy check stays
quiet. An authorized confirmed repair opens a PR; ambiguity becomes a focused
human question, not a stream of generic recommendations.

## Product loop
Event -> pin revision/target/runtime -> execute published recipe in clean state.
On failure: investigate with Strands -> propose bounded patch -> enforce policy ->
discard investigative state -> re-execute the revised recipe independently ->
perform protected acceptance -> publish evidence-backed PR or report a blocker.

## User-visible outcomes
- **Verified at commit X:** this target and recipe passed in the recorded environment.
- **Broken at commit X:** a real setup/acceptance failure was observed.
- **Repair ready:** a candidate passed proof; the default branch may still be broken.
- **Needs input:** missing authority, private configuration, or ambiguous intent.
- **Blocked by infrastructure:** environment/provider unavailable; not a code diagnosis.
- **Stale:** tested revision is not current; never imply the current head passed.
- **Unsupported:** this repo/platform cannot yet be tested under the implemented policy.

## Initial supported lane
Linux Node/npm repositories, with controlled source and no private services in the
first demonstration. The included Node/SQLite fixture has a real missing-migration
failure and no external package dependencies. This is not universal repository
support. Add supported language/service adapters only with fixtures and acceptance
coverage; Python/Docker-service expansion may be substantial without changing the
product's core promise.

## Necessary capabilities and why each belongs
| Capability | Reason |
|---|---|
| Approved recipe and target | Makes “working” explicit without hidden agent improvisation |
| Fresh execution | Reproduces a newcomer's state rather than a maintainer's machine |
| Evidence-driven Strands repair | Handles ambiguous relationships among logs/scripts/docs |
| Independent proof | Separates a plausible repair from an observed working outcome |
| GitHub checks/repair PR | Completes the maintainer's existing review workflow |
| Small evidence/decision web interface | Lets people inspect proof or resolve a blocker |
| Durable case state | Prevents duplicate work, permits bounded recovery/resumption |

## Success boundaries
FirstRun verifies one declared environment and outcome, not every OS or human
interpretation of prose. An HTTP health response alone is not the demonstrated
functional outcome. Passing tests is bounded evidence, not a security certificate,
formal proof of all behavior, or a guarantee of future dependency availability.

## No unrelated features
No generic chat-with-repo, issue-to-code system, code review bot, production deploy,
auto-merge, billing UI, productivity score, or unrelated integrations. The absence
of these is focus, not a limit on serious engineering or appropriate stack coverage.

## Product acceptance
A maintainer can authorize a repo, approve a setup path, receive a genuine failed
check, inspect an agent-proposed repair, see a clean independent proof with hashes,
and open a real PR. A required secret causes a specific question. An unmerged or
stale candidate never turns the current branch green. All visible states are real
backend observations; recorded demonstrations are labelled as recorded.
