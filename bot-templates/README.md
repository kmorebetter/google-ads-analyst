# Bot template specs

Three shareable Grok Bot templates built on this plugin, each with a
`template.json` (skills, memories, routines), a `getting-started` skill, a
SPEC, a CSV engine in `tools/`, and graded evals in `evals/`. Ship order:
1. Negative Keyword Miner, 2. Google Ads Auditor, 3. Weekly Google Ads
Reviewer. Nothing here is published.

Template rules that apply to all three: no personal names, no private links,
no client data, no custom MCP servers packed in (name a connector in a memory
instead), and a getting-started skill that runs `account-setup` first.
