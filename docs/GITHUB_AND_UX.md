# GitHub integration and web UX

## App integration
Request only implemented permissions: metadata read; contents read/write for repair
branches; pull requests read/write; checks read/write only when used. Validate raw
webhook body signature and match installation/repository authorization before any
fetch/run. No execution of arbitrary fork commits. Owner explicitly opts in to
automatic verified-repair PRs; auto-merge is never permitted.
Installation callback alone is not application login. Use a documented identity
flow plus server-side sessions; verify OAuth state, CSRF for cookie-authenticated
mutations, repo membership, and decision version. Never send App private keys to web.

## Write semantics
Before publishing compare configured branch head with pinned base. Resolve staleness
as specified in CONTRACTS.md. Use a stable repair branch identity tied to case/base.
After uncertain network outcomes, find existing branch/PR and reconcile; do not
blindly retry POST. Compare PR head contents with the candidate tested. A retrieved
URL/number without matching contents is not enough proof of the published repair.

## PR body
What failed; the relevant observed log; exact permitted changes; separate clean
proof result; tested SHA/tree, target and runtime; evidence link; remaining limits.
Use “verified candidate” until merged/rechecked. No unsupported hours-saved claim.

## Web surfaces
Repository list: repo, current head, latest checked SHA/outcome, pending repair/action.
Repository detail: approved target/recipe, public setup instructions, latest runs.
Case page: failure -> evidence -> proposed diff -> clean proof -> GitHub handoff.
Human-input panel: one focused blocker, supported choices, no plaintext secret field
unless a real protected secret broker exists. Blocked cloud access is not a demand
that the user change billing settings.

## Visual language
Calm developer tool, semantic status labels, readable typography/logs/diffs, keyboard
access and visible focus. Status cannot be conveyed only by color. Actual timeline
events come from persisted backend events; never play a fake thinking animation.
No fabricated token metrics or AI confidence score. Show concise evidence/rationale,
not hidden chain-of-thought. Event streaming/polling is an implementation choice;
choose whichever reliably shows real progress with reconnect behavior.

## Critical state examples
“main @ abc failed; repair PR #12 passed at def” is honest.
“Repository verified” immediately after opening that PR is not.
“Recorded run, Sept 12” is honest when showing saved evidence.
“Live proof” is not honest when the UI is playing a local JSON fixture.
After merge and a fresh check at current head, show current first-run verified.
