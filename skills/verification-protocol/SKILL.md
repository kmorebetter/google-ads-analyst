---
name: verification-protocol
description: "Reference skill defining the pack-wide reliability contract: session preflight (CLI discovery, customer-ID guard, conventions drift check), raw-pull persistence plus the per-run facts file, the scope-match rule for comparing two figures, the no-silent-partials failure policy, confidence labels, and the three verification tiers (inline self-check, independent verifier agent, adversarial refuter). Load before running any skill that queries the Google Ads account or produces a report. Use whenever pulling data, verifying a report, or deciding how much checking an output needs."
---

# Verification Protocol

The reliability contract every analysis skill in this pack runs under.
Rule rationale (VF-1..VF-20) lives in `docs/VERIFICATION-RULES.md`; incident history in
`MAINTENANCE.md`; standing open items in `STANDING-ITEMS.md` (the required
per-run read). Tier coverage:

- **Tier 1 + 2 + 3** (money-moving): `weekly-review`, `budget-optimize`,
  `mine-search-terms`
- **Tier 1 + 2** (diagnostic; blind-second-diagnosis variant where noted):
  `investigate-campaign`, `performance-analysis`, `audit-ad-copy`,
  `audit-extensions`, `outcomes-review`
- **Preflight + Tier 1 only**: `pmax-audit`
- **Composite**: `run-review` holds the per-skill Tier 2/3 dispatches of the
  skills it orchestrates and replaces them with one composite Tier 2 and
  one Tier 3 pass over the merged output (see that skill).

Each skill's own Prerequisites block states its tiers; if the two disagree,
the skill's block wins and this list needs updating.

**Reference skills every action skill loads:** this file, plus
`account-conventions`, `conversion-measurement`, `campaign-guardrails` and
`evidence-links`, plus whichever domain reference the skill names.

**Read-only posture, stated once for the whole pack:** the Google Ads CLI on
this machine is read-only. No skill mutates the account, ever. Anything that
needs changing is delivered as a Google Ads Editor-ready CSV or an explicit
UI change list the user applies by hand. If a skill ever appears to offer
"apply this directly," that's a bug in the skill — flag it, don't do it.

## Preflight (once per session, before any skill queries)

1. **Discover the data access paths.** Google Ads data comes from one of:
   a Google Ads MCP connector, a local read-only GAQL CLI (see SETUP.md), or
   uploaded CSV exports (`csv-import`). Confirm the API path runs GAQL before
   using it. **If no working path is found, stop and say so**, and offer the
   CSV path. Never improvise another access path or present anything as if
   data were pulled. The outside source for the conversion integrity check
   (named in the account profile) is usually a connector; an unauthorized
   connector is a "source unreachable" case under the failure policy.
2. **Customer-ID guard.** Cheapest possible read confirming customer
   ID from `tools/account-config.json`; pass the ID explicitly on every
   invocation if the CLI can reach more than one account.
3. **Conventions drift check.** Pull enabled campaigns with structure fields
   (name, `bidding_strategy_type`, both target sub-message fields,
   `campaign_budget.resource_name`), save as
   `preflight-conventions-drift.json`, **declare it in `_manifest.json`**
   (preflight pulls are pulls — the same goes for
   `preflight-conversion-values.json`, the `conversion_action.value_settings`
   re-pull VF-8 mandates), and diff against `account-conventions`:
   campaign list, suffix-decoder claims, shared-budget clusters. Any
   mismatch ⇒ flag at the top of the report and treat the affected sections
   as `[assumption]` until refreshed.
4. **Resolve `OUTPUTS_DIR` and `TOOLS_DIR`.** `OUTPUTS_DIR` is the
   `outputs/` directory of the account working area (`google-ads/outputs/`
   relative to wherever that area sits — when the session starts inside
   `google-ads/`, that is `./outputs/`); create it (plus `raw/YYYY-MM-DD/`
   with `_manifest.json`) if missing. `TOOLS_DIR` is the `tools/` directory
   **inside this skills pack** (deployed: `.claude/skills/tools/`) — every
   `python3 tools/<script>` command in this pack means that directory, so
   resolve it once and use its absolute path. Print both absolute paths in
   the report header so there is never ambiguity about where artifacts went
   or which tools ran.
5. **Agent callability check.** The three named agents
   (`google-ads-verifier`, `google-ads-refuter`,
   `google-ads-blind-diagnostician`) load at session start — files or
   symlinks on disk are not proof of registration. Confirm they appear as
   callable subagent types (or attempt one cheap dispatch) and record the
   result in the footer. If not callable, the fallback applies: pass the
   brief **verbatim** to a general-purpose agent and **disclose the
   substitution in the footer** — never a footer implying a named agent ran
   when it did not.

Preflight runs once per session — later skills reuse its results.

## The three run artifacts (per skill run)

1. **Raw pulls.** Every query result is saved **before** analysis to
   `OUTPUTS_DIR/raw/YYYY-MM-DD/<skill>-<query-name>.json`, and **declared in
   `_manifest.json` at the moment it is saved**: the literal filter that
   ran, the row count, the truncation state, and the temporality —
   `current-as-of-<date>` or `window:<range>`. Temporality is declared,
   never inferred (VF-17 rule 5); an undeclared pull produces figures of
   unknown temporality and is a hard fail. The paved path:
   `python3 tools/declare_pull.py <raw_dir> <filename> --gaql '<the exact
   query that ran>'` writes the entry from the query itself; hand-written
   entries remain valid. Analysis reads from these files,
   not from memory of tool output. Same-day reuse: a later skill may reuse
   a same-day raw file freely unless it needs intraday freshness (say so in
   the header when reusing).
2. **The facts file.** Reports do not do arithmetic in prose. After the
   pulls and before drafting, the skill's builder
   (`tools/build_<skill>_facts.py`) writes `<skill>-facts.json`: one row
   per figure the report will use — `id`, `value` (or null with reason),
   `unit`, `scope` (the literal filter, verbatim), `window`, `source`,
   `basis` (`8-action` / `broad-goal-set` / `n/a`). **Every figure in a
   report traces to a fact id.** For skills without a builder yet
   (`investigate-campaign`, `performance-analysis`, `pmax-audit`), Tier 1 recomputation from raw files stands in.
3. **The undated current-state pull.** Any run making a current-state claim
   ("X is live", "still unfixed") persists a pull with **no date filter**
   covering the objects claimed about. A run making no current-state claim
   says so in the footer rather than omitting the line.

## Comparing two figures — the scope-match rule

**Two figures may only be compared, differenced or summed when `unit`,
`scope`, `window` and `basis` all match.** All four; the builders' derivation
profiles are the only sanctioned bridges (documented in `tools/README.md`),
`compare_unsafe()` requires a written justification, and the
`unsafe_comparisons` count stays near zero. Arithmetic checking cannot see a
scope mismatch — two individually-correct figures on different scopes was
the most expensive error in this pack's history.

**Prior-report rule.** A run-over-run delta requires opening the prior dated
report in `OUTPUTS_DIR/` and reading how it defined the figure — a one-line
summary elsewhere is not enough to establish a basis. The prior report is a
required Tier 2 input whenever a delta appears.

## Failure policy — no silent partials

A query that errors or returns zero rows where rows were expected: retry
once, then **mark the section missing** ("Section X not available: <why>")
and continue. Never estimate into a gap, never present a partial dataset as
complete, never quietly drop a campaign that appears in one pull but not
another — list it with the gap explained. An unreachable integrity-check
connector reads "integrity check SKIPPED: source unreachable", never
nothing.

## Confidence labels

- `[verified]` — recomputed from raw files this run, or confirmed live this
  run.
- `[derived]` — computed from verified inputs; correct if they are.
- `[assumption]` — carried from a snapshot, doc, or inference; not confirmed
  this run (drifted `account-conventions` sections and the goal-basis caveat
  live here).

The API truths this pack relies on (two-query rule, change_event limits, sentinels, PMax gaps,
Auction Insights inaccessible) are cited inline where each skill needs them.

## The three verification tiers

Depth is proportional to stakes. The user saying **"quick"** drops a run to
Tier 1 only — and the footer must say so. **Two checks are never dropped at
any tier: figure tracing and freshness.** They are hard fails about whether
a claim is *true*, not about review depth; quick mode narrows scope, it
never licenses an untraceable figure or a current-state claim on a stale
file.

### Tier 1 — mechanical self-check (every run, every skill, no agent)

- **Figure tracing and self-consistency**: run
  `python3 tools/citations.py <report> <facts> <outputs_dir>` until clean —
  every stated figure names its fact; untraceable figures are traced (extend
  the builder) or removed, never shipped with a caveat.
  Where no builder exists: recompute every stated number from the raw files
  with Bash/`jq` (never mental math; `*_micros` ÷ 1,000,000, units stated).
  Passing `<outputs_dir>` (the directory holding `recommendations-log.csv`)
  also runs the two self-consistency checks: every pull in the facts file's
  `meta.undeclared_pulls` must be named somewhere in the report, and a report
  that claims it appended to the log must have a row dated `meta.pulled`.
  Omit the third argument and the append check silently
  does not run.
- **Join completeness**: objects present in one pull but absent from
  another are listed, never dropped.
- **Row keys**: each pull's row count printed beside its distinct-key
  count; child objects keyed on `(parent_id, child_id)` pairs (VF-19).
- **Asset-view figures** (`audit-ad-copy`/`audit-extensions`): keyed on
  `(ad_group.id, ad.id, asset.id, field_type)`, never `ad.id` alone or
  asset text, and always intersected with current inventory before
  reporting (serving-but-removed is history, never a live problem) — both
  enforced in `tools/asset_facts.py`.
- **CSV lint** (any CSV output) and **character counts** (any suggested ad
  copy: `printf '%s' "text" | wc -m`, characters not bytes), with Bash,
  never by eye.

### Tier 2 — independent verifier (every full run of the Tier-2 skills)

Mechanical checking is done before any agent runs; Tier 2 is **judgment**.
Invoke **`google-ads-verifier`** (fallback per preflight step 5) with: the
raw pull paths, the facts file, the draft, the prior dated report(s) when a
delta appears, and the relevant reference skills. Its checklist lives in
`agents/google-ads-verifier.md` — not restated here, so the two cannot
drift. In outline it judges: row keys, freshness (every current-state claim
from this session's undated pull; `change_event` checked before any
"unremediated" claim), learning-window downgrades (date + type present,
change type plausibly matched to claim type), the Brand rule, subtotal
discipline, assumed-value qualifier presence, guardrail compliance
(`campaign-guardrails` rules 6-9), and characterisation — whether the data
actually supports each finding as worded.

**Blind variant** — `investigate-campaign` / `performance-analysis`: invoke
**`google-ads-blind-diagnostician`** with the raw files and references but
**never the draft**; diff its independent diagnosis against yours.
Agreement = confidence; divergence = present both reads side by side with
evidence. Never merge a divergent diagnosis into false consensus.

Every discrepancy returned is either fixed (noted in the footer) or
surfaced in the report — never silently accepted.

### Tier 3 — adversarial refuter (money-moving outputs only)

After Tier 2 passes, invoke **`google-ads-refuter`** with the
recommendation list, the raw data behind it, and the references (checklist
in `agents/google-ads-refuter.md`). This is the only tier that has ever
overturned conclusions — it is not optional on the three money skills.
REFUTED → pulled or demoted to **"contested"** with both views shown (the
user decides contested calls). WEAKENED → the caveat is attached.

## Verification footer (required in every persisted report)

A report without this footer is unfinished. The whole footer is fenced;
fenced text is checker-exempt (its counts describe the checking). Lift a
line into prose and it correctly fails as untraceable.

```
---
## Verification
- Tier(s) run: 1 / 1+2 / 1+2+3   ("quick mode" if Tier 1 only)
- Raw pulls: <OUTPUTS_DIR>/raw/YYYY-MM-DD/ (N files) · undeclared in _manifest.json: none | <which>
- Facts file: <path> (N facts) · untraceable figures found: N — all resolved (traced | removed)
- Query filter used (verbatim): <one line per figure set>
- Preflight: customer 1234567890 confirmed · conventions drift: none | <summary>
- Agents callable: yes | no — fallback: <brief> passed verbatim to a general-purpose agent
- Freshness: every current-state claim from an undated pull this session | <exception: claim → file → its date>
- Guardrails checked: 6 · 7 · 8 · 9 · assumed-value labelling — violations: none | <list>
- Tier 2 verifier: no discrepancies | <each: claim → finding → resolution>
- Tier 3 refuter: N holds, N weakened, N contested   (money-moving skills only)
- Skipped or reduced: none | <what, why>
```

"Skipped or reduced" answers at **step granularity**: a run that did a
mandated check on 2 of 61 objects reduced it, and the footer says so.
"None" means nothing was skipped and nothing was narrowed. A residual
nonzero untraceable count blocks publication. A footer naming a verifier
agent that did not run is a false statement about the report's provenance.

## Report persistence

Every analysis skill saves its final report to
`OUTPUTS_DIR/<skill>-YYYY-MM-DD.md` (in addition to presenting it) — this is
what makes week-over-week comparison, rec-log backfilling, and auditing
possible. CSVs and the recommendations log keep their names alongside.

## Maintenance log

Before presenting, check whether anything from this run belongs in the
maintenance history. It qualifies if it is about the toolkit or a standing
account fact — a bug in a skill's logic or query, a correction to a
previously-reported finding, a process learning, a new standing open item.
This week's wins/concerns belong in the dated report, not here; under-log
rather than pad.

**Write it to `OUTPUTS_DIR/maintenance-YYYY-MM-DD.md`, never to
`MAINTENANCE.md` or `STANDING-ITEMS.md` directly.** Both live inside
the installed plugin, and a run that edits them leaves it dirty, which can
block the next update and put the notes one careless reset away from being
lost. `OUTPUTS_DIR` is outside the plugin and is committed with the run, so notes
written there survive both the next pull and the next deploy.

Format the file as the MAINTENANCE.md section it will become — `## YYYY-MM-DD
— <title>`, terse bullets, corrections referencing earlier entries rather than
editing them — so porting it is a copy-paste. If the run would also add or
resolve a standing item, say so at the end of the file under a
`### STANDING-ITEMS` heading, quoting the exact line to add or remove and
which existing line it replaces; that digest has a hard cap of 50 lines, so
name what comes out as well as what goes in. Reading `STANDING-ITEMS.md` is
still required every run — only writing to it moves.

Porting `maintenance-<date>.md` into `MAINTENANCE.md` and `STANDING-ITEMS.md`
happens in the **dev repo**, in a session that can commit and push them. Until
that happens the note is not lost: it is in `OUTPUTS_DIR` with the run.
