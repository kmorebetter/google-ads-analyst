---
name: budget-optimize
description: "Analyze Google Ads campaign budgets and recommend changes from impression share, lost-to-budget metrics, real pacing against the owner's stated monthly budget, and the documented CPA elasticity bands. Keys every budget figure on campaign_budget.id because this account shares budgets across campaigns. Pulls via the local read-only Google Ads CLI, builds a facts file, and cites every figure to it. Also runs the target-tightening follow-up. Use when the user says 'budget optimization', 'impression share', 'lost to budget', 'increase budget', 'cut spend', 'budget recommendation', 'am I budget constrained', 'are we over budget', 'pacing', 'bid target calibration', or wants to optimize spend allocation."
---

# Budget Optimize

Answer "where should the money go" with figures that trace to a fact, and
refuse the recommendation when a guardrail says the data cannot support one.
Target a **150-250 line** report answering "what should I do with the money
this week" in the first five lines: real pacing, then which recommendations
survive the gates. A run where nothing survives says so in line one — that is
an answer, not a failure. This output moves money: **Tier 1 + 2 + 3**.
VF-rule rationale lives in `docs/VERIFICATION-RULES.md`; incident history in
`MAINTENANCE.md`.

## Prerequisites

Load `verification-protocol`, `account-conventions`, `conversion-measurement`,
`campaign-guardrails` (rules 1, 2, 5, 7, 8 own the reasoning; this skill
restates only mechanics), and `evidence-links`. Also required:
**`budget-plan.md`** and **`STANDING-ITEMS.md`** (the required read;
`MAINTENANCE.md` on demand).

## Standing rules (enforced in code; a builder refusal is a finding)

- **The budget is the key (VF-19).** Every budget figure keys on
  `campaign_budget.id`, attributed to campaigns afterwards — a campaign-keyed
  total counts a shared pool once per campaign. Detect sharing by counting
  distinct budget ids; the `explicitly_shared` flag is unusable in this
  account. Never take cluster membership or amounts from
  `account-conventions` — read this run's pull. A pool's spend, conversions
  and learning status are the cluster's, and a share cannot be summed across
  campaigns (a cluster's lost-IS is its worst single campaign's; the fact's
  scope says so).
- **Freshness (VF-17).** Budgets, targets, strategies, structure are
  current-state claims: undated pull only, `change_event` checked before
  calling a setting wrong, temporality declared in `_manifest.json` at save
  time.
- **Budget figures are read, never derived** (guardrail 8): stated monthly
  figures come from `tools/account-config.json` (sourced from
  `budget-plan.md`). A month with no stated figure is a null fact with the
  reason — the builder refuses to pace it; ask the owner, never reuse another
  month's. Pace in CAD and name the currency. The planned month-over-month
  step down is the plan, never a problem; overspend against the lower budget
  is the separate, real finding.
- **Facts and citations (VF-15/15a).** Every figure carries `[fact: id]`;
  missing fact → extend `tools/build_budget_optimize_facts.py`, re-run.
  Skill thresholds and footer counts sit in code spans (checker-exempt).
- **Evidence links (VF-13).** Object claims carry name and numeric ID — for
  a shared pool, the budget id and every campaign on it.
- **Sentinels.** `0.0999`/`0.9001` are bounds, not values — suppress per
  field, not per campaign (lost-IS fields on the same row may be real).

## Steps

### 1. Pull the data — five pulls, one not scoped to campaigns

Save each to `OUTPUTS_DIR/raw/YYYY-MM-DD/`, declared in `_manifest.json` as
saved (literal filter, row count, temporality). The paved path: `python3 tools/declare_pull.py <raw_dir> <filename> --gaql '<the exact query that ran>'` writes the entry for you (rows, truncation, temporality derived from the query itself); hand-written entries remain valid. Row counts checked against
distinct-key counts before any sum.

| File | Query |
| :- | :- |
| `campaigns-current.json` | Undated structure, the only valid source for a budget/target/strategy claim: `FROM campaign WHERE campaign.status = 'ENABLED'`, selecting id/name/status/channel, `bidding_strategy_type`, `maximize_conversion_value.target_roas`, `maximize_conversions.target_cpa_micros`, **and** legacy `target_roas.target_roas` / `target_cpa.target_cpa_micros` (all four, so the report can say which field answered), `campaign_budget.id/amount_micros/explicitly_shared`. |
| `campaign-cost-is-7d.json`, `campaign-cost-is-prior7d.json` | Same filter plus the two windows: current = the last full Mon-Sun week as an explicit `BETWEEN '<mon>' AND '<sun>'`, prior = the week before it, same form (`LAST_7_DAYS` is accepted for an intraweek check; the builder refuses a pair that overlaps, differs in length, or declares the prior week as a relative token), selecting `campaign.id`, `metrics.cost_micros/impressions/clicks/search_impression_share/search_budget_lost_impression_share/search_rank_lost_impression_share/search_absolute_top_impression_share`. Identical field list both weeks — an omitted field kills its WoW row. |
| `campaign-conversions-7d.json`, `campaign-conversions-prior7d.json` | Two-query rule (VF-3): same filter, same two windows as the cost pair, plus `segments.conversion_action_name IN (<the 8, verbatim from conversion-measurement>)`, selecting `campaign.id/name`, the segment, `metrics.conversions/conversions_value`. Joined on `campaign.id`, then aggregated to the budget. |
| `account-spend-mtd.json` | `FROM customer WHERE segments.date BETWEEN '<month start>' AND '<yesterday>'`, selecting `segments.date`, `metrics.cost_micros`. No campaign or status filter, on purpose — its total is never compared against a sum over enabled campaigns. |
| `change-events-14d.json` | `FROM change_event WHERE change_event.change_date_time DURING LAST_14_DAYS`, selecting date/type/operation/`changed_fields`/campaign/ad_group/user_email/client_type, with a `LIMIT` (400s without one). **No `change_resource_type` filter** — a `CAMPAIGN`-only filter misses `CAMPAIGN_BUDGET` events entirely; pull every type, filter in analysis. |

### 2. Identify budget-constrained clusters

Per **budget cluster**, never per campaign: lost IS (budget) above the 30%
gate across the window, impression share trending down while lost-to-budget
trends up. Step 4 uses the separate 20% gate (shared with `weekly-review` so
the two skills produce the same list); gates live in
`tools/account-config.json`.

### 3. Analyze the constraint before recommending anything

- **Budget vs. rank.** Lost-to-budget >> lost-to-rank: budget would help.
  Reversed: it would not — the work is bids, targeting or quality. Quote both.
- **Recent changes first.** Check the change pull for the cluster before
  concluding competition or seasonality; a budget cut, strategy migration or
  target change is usually the real explanation. A synchronised un-batched
  pattern across campaigns is a rule-3 batching violation explaining one
  shared move, not N separate problems.
- **Learning windows, scoped** (guardrail 7): a budget/bid/target
  recommendation is downgraded to "monitor — in learning" by a
  `CAMPAIGN_BUDGET` amount change or a `CAMPAIGN` bid/target change inside
  14 days (Search) / 28 (PMax), stating **both date and type**. A criterion,
  asset or ad edit does not downgrade a budget recommendation. A 28-day PMax
  window can never be cleared from a 14-day pull — say it could not be
  cleared; silence is not clean. **A cut is a budget change too.**

### 4. Target-based bidding: tighten before adding budget

For a budget-limited cluster
already **beating** its set target, the first move is to **tighten the
target** (Bid Target Adjustment Tool), not add budget — the recoverable
impressions are the marginal tail, and Google's "recover lost impression
share" prompts point the wrong way. Mechanics:

- **Read the target from the right field.** Targets live on the
  bidding-strategy sub-message (`maximize_conversion_value.target_roas`);
  the legacy field is a control that should come back empty — a wrong field
  that returns nothing looks like an answer. Say which field answered. If no
  Search campaign carries a target CPA, no rule may be phrased as a multiple
  of target CPA on Search.
- **Actual and target are two numbers, never one clean gap** (rule 4): our
  actual is 8-actions-only, Google's target is managed against a broader
  goal set. The facts file carries a direction-only beating-target flag, no
  delta.
- **A pool whose campaigns carry different targets has no pool-level gap** —
  compare per campaign or not at all.
- Post-change drift is the open measurement: whether previously-overperforming
  clusters got pulled back toward their stated targets. `outcomes-review`
  owns that verdict once learning windows clear. For PMax and Demand Gen,
  traffic may redistribute across channels post-change — watch it, do not
  quantify it here.

### 5. Size the recommendation, or refuse it — four gates, in order

A recommendation failing a gate is downgraded or dropped, and the report says
which gate stopped it. 1. **Low-volume floor** (rule 5): at least 5 in-window
8-action conversions for the cluster, else monitor note only — the builder
enforces this by omission (no efficiency fact below the floor).
2. **Top of funnel** (rule 1): YouTube/Demand Gen never judged on BOFU
CPA/ROAS; no efficiency fact exists for them. 3. **Brand is never a growth
lever** (rule 2): only admissible trigger is Brand Core
`search_rank_lost_impression_share` rising week over week, both weeks quoted;
subtotals reported separately, blended last and labelled. 4. **Elasticity
band** (rule 8, values in config): every increase names its band; above the
top band no data exists — do not recommend; an increase totalled across
clusters is checked against the band once, on the total.

For each surviving change: budget id and every campaign on it, current and
recommended daily budget, percentage, band, expected impact, and why this
number. **Assumed-value labelling (VF-8)** on every CPA/ROAS sentence, with
the real-money CAC anchor from `budget-plan.md` beside it.

### 6. Build the facts file, then write every figure from it

Run `python3 tools/build_budget_optimize_facts.py <raw_dir>` before drafting
a single number; it writes `budget-optimize-facts.json` beside the pulls.
Comparisons obey the scope-match rule (unit/scope/window/basis);
`compare_unsafe` only with a written justification, and the count stays near
zero. If the builder errors, retry once, then the failure policy — never
prose arithmetic.

### 7. Flag what not to increase

High lost-to-rank (budget will not fix it) · poor efficiency despite
headroom · any cluster inside its learning window (the fix is time) · any
cluster whose pool is not constrained as a whole (raising one campaign's
share just shifts allocation) · **idle allocation**: clusters with zero cost
in both windows carry budget that buys nothing — an unspent budget costs
nothing, so the finding is that allocated total has no relationship to the
monthly plan; report as structure to clean up and name the pools.

### 8. Log the recommendation

**Canonical shared logging format** — `weekly-review`, `mine-search-terms`
and `investigate-campaign` point here rather than defining their own.
Append one row per recommendation to `OUTPUTS_DIR/recommendations-log.csv`
(create with this header if missing). **Quote every field that might contain
a comma** — campaign names here routinely do. After every append, run the
Tier 1 CSV lint before considering the log written.

| Column | Meaning |
| :- | :- |
| `date_recommended` | YYYY-MM-DD this row was logged |
| `skill` | Which skill emitted it |
| `campaign` | Campaign name; for a shared pool, the budget id and every campaign on it |
| `metric` | **One** metric from the closed vocabulary below — never two, never free prose |
| `before_value` | Value at the time of the recommendation |
| `recommendation` | What to change it to, in plain text |
| `reasoning` | Why — one line, naming the elasticity band for any increase |
| `status` | `open`, `applied`, or `skipped` |
| `after_value` / `after_date` | Blank until backfilled |

**The `metric` vocabulary is closed** (`READABLE_METRICS` and
`MANUAL_METRICS` in `tools/reconcile_recommendations.py` — that file is the
source of truth; this table follows it):

| Readable — the reconciler reads these from `campaigns-current.json` and can auto-flip them | Judged by hand — legitimate, but no account field can ever confirm them |
| :- | :- |
| `daily_budget`, `target_roas`, `target_cpa` | `search_term_negatives`, `negation_thresholds`, `ad_copy`, `keywords`, `pacing_pct_of_prorated_budget`, `spend_impressions_clicks` |

**One metric per row.** A recommendation that touches two things
(`daily_budget + target_roas`) is **two rows**, because a row carries one
`before_value` and one `after_value` — a compound row has no single value to
compare and is reported as malformed, not reconciled. If a recommendation
genuinely targets something outside both lists, add it to `MANUAL_METRICS`
with a one-line note on how a human settles it; do not invent a metric name
in the CSV. Free prose in this column makes rows unreconcilable.

Every row is written as `open` (this pack is read-only). Status transitions,
the `after_value` backfill, long-`open` surfacing and outcome verdicts are
owned by **`outcomes-review`** (via
`tools/reconcile_recommendations.py`) — this step only appends rows. Never
edit a filled cell here.

### 9. Verify, then present

Tier 1, 2, 3 per `verification-protocol`. Footer prints: budget figures
keyed on `campaign_budget.id` with the campaign-keyed total refused; every
pull declared with row/distinct-key counts; every figure cited or
code-span-exempt. Tier 2 (`google-ads-verifier`) judges row keys,
learning-window scoping, freshness, the Brand rule, characterisation. Tier 3
(`google-ads-refuter`) attacks every surviving recommendation.

Present, inside 150-250 lines: **the answer** (pacing, then what survives
the gates), **pacing detail** (run-rate forecast, budget left, allowed daily
spend, the cut that lands on budget — sizing a cut is the owner's spend
decision: report the arithmetic and which clusters carry the spend),
**constrained clusters** (budget id, campaigns, gates/downgrades applied),
**brand and non-brand subtotals**, **do-not-increase and idle allocation**,
**the target-tightening follow-up**, and **the change list** (only Tier-3 survivors;
campaign, budget resource id, current, new — Editor CSV above ~5 changes).

**This pack is read-only** — never offer to apply changes. Persist to
`OUTPUTS_DIR/budget-optimize-YYYY-MM-DD.md` with the verification footer.
