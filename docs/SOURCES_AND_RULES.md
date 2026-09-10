# Official sources and hackathon constraints

Checked September 9, 2026. These sources support external facts, not a guarantee
that your individual account has a service/model enabled. Recheck account-dependent
permissions, model availability, CLI/API signatures and quotas when building.

## Hackathon facts (S1–S3)
Build a new substantive Strands agent. Professional Agents matches maintainers.
AgentCore is encouraged, not mandatory. Submit public MIT/Apache source, README,
architecture diagram, AWS Builder ID, <=5-minute public YouTube/Vimeo demonstration
and project description. Disclose incorporated prior work; describe implemented
behavior accurately. Provide a working test build/demo for judges through the
judging period. The submission cutoff is Sept 14, 2026, 17:00 PDT (= Sept 15, 05:30
IST); judging ends Oct 8. Live demo is optional but helpful. Qualifying public build
posts can earn up to 0.6 bonus points; follow exact title/publication requirements.
This summary does not replace reading the rules. Credit availability/account-plan
restrictions are not verified by this pack and no extra promotional credit is assumed.

## Source directory
S1 — Official rules
https://agentsforhumans.devpost.com/rules

S2 — Official FAQ
https://agentsforhumans.devpost.com/details/faqs

S3 — Official schedule
https://agentsforhumans.devpost.com/details/dates

S4 — Codex best practices: plan difficult work; provide verifiable context
https://developers.openai.com/codex/learn/best-practices

S5 — Codex AGENTS.md discovery/instruction layering
https://developers.openai.com/codex/guides/agents-md

S6 — OpenAI harness engineering: short instruction map and structured repository docs
https://openai.com/index/harness-engineering/

S7 — Docker security and explicit resource constraints
https://docs.docker.com/engine/security/
https://docs.docker.com/engine/containers/resource_constraints/

S8 — Docker WSL2 workflow and filesystem guidance
https://docs.docker.com/desktop/features/wsl/
https://docs.docker.com/desktop/features/wsl/best-practices/

S9 — Codex native Windows and WSL support
https://developers.openai.com/codex/windows
https://developers.openai.com/codex/windows/wsl

S10 — Strands Python quickstart and structured output
https://strandsagents.com/docs/user-guide/quickstart/python/
https://strandsagents.com/docs/user-guide/concepts/agents/structured-output/

S11 — Strands Bedrock model provider
https://strandsagents.com/docs/user-guide/concepts/model-providers/amazon-bedrock/

S12 — GitHub App permissions and webhook validation
https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app
https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries

S13 — Node built-in SQLite API used by the controlled fixture
https://nodejs.org/api/sqlite.html

S14 — Docker network isolation and container network-namespace joining
https://docs.docker.com/engine/network/
https://docs.docker.com/engine/network/drivers/none/

## Independent decisions, not external requirements
The target/recipe split, external probe, Python modular backend, Node first lane,
chosen budgets, fixture design, task sequencing and UI are our engineering design.
The hackathon does not mandate those decisions. No claim that FirstRun is a wholly
unprecedented invention or that this architecture guarantees a prize.
