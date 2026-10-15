---
name: weekly-review
description: "Run a weekly Google Ads performance review: real budget pacing (spend-to-date against budget pro-rated by day, in CAD), spend and 8-action conversions week over week, brand / non-brand / top-of-funnel subtotals, impression share, and what changed in the account. Every figure traces to a fact id in a per-run facts file. Use when the user says 'weekly review', 'weekly report', 'how did we do this week', 'performance summary', 'WoW comparison', 'are we on budget', 'pacing', or wants a regular cadence check on their ad account."
---

# Weekly Review

The weekly read on the account. Recommended actions here move money, so this
skill runs **Tier 1 + 2 + 3**. Rationale for every VF-numbered rule cited
below lives in `docs/VERIFICATION-RULES.md`; incident history lives in `MAINTENANCE.md`.
Neither is restated here.

## Prerequisites

Load `verification-protocol`, `account-conventions`, `conversion-measurement`,
`campaign-guardrails` (rules 1, 2, 5, 7, 8 own the reasoning this skill only
applies), and `evidence-links`. Also required: **`budget-plan.md`** (the only
source for a stated budget figure or real-money CAC anchor) and
**`STANDING-ITEMS.md`** (the standing open items — the required read;
`MAINTENANCE.md` is history, consult on demand).

## The report this produces

Target **150-250 lines**, opening with the answer to "are we on budget and is
the money working": real pacing, then the non-brand CPA. A report opening
with a metric table has buried its own finding.

## Standing rules (enforced in code; a builder/checker refusal is a finding)

- **Scope filters — print them, never mix them.** Campaign pulls:
  `campaign.status = 'ENABLED'`. Account pull: no status filter. The two
  totals are different populations; never difference one against the other.
- **Row keys (VF-19).** Child objects key on the `(parent_id, child_id)`
  pair, never the child ID alone, never a name. Conversions aggregate on
  `(campaign.id, conversion_action_name)` before any join. Print each pull's
  row count beside its distinct-key count.
- **Freshness (VF-17).** Current-state claims come only from this session's
  undated pull. A saved pull is evidence about its own date — say that date.
  Check `change_event` before calling anything unremediated. Temporality is
  declared in `_manifest.json` at save time, never inferred.
- **Deltas and ratios (VF-15).** Every comparison goes through the builder's
  derivation profiles (`wow-delta`, `cost-over-conversions`, `plan-vs-actual`,
  `actual-vs-target` — documented in `tools/README.md`). `compare_unsafe()`
  is not used; `unsafe_comparisons` must come back empty. Never do arithmetic
  in prose.
- **Two-query rule (VF-3).** Cost and conversion-action-segmented metrics
  cannot share a query; they arrive as separate pulls joined by campaign.
- **Assumed-value labelling (VF-8).** Every CPA and ROAS figure carries the
  qualifier in the same sentence ("assumed-value CPA CAD 123.45") with a
  real-money CAC anchor from `budget-plan.md` beside it. Presence check, not
  a banned word. Never present an assumed value as revenue.
- **Shared budgets** are read from `campaign_budget.id` groupings, never the
  `explicitly_shared` flag. A recommendation moving one campaign's budget
  names every sibling on its pool.
- **Targets** are read from the facts, never from campaign names (`-tROAS`
  names without targets exist, and the reverse).

## Steps

### 1. Preflight and header

Run `verification-protocol`'s preflight. Before any number: state the
attribution model, the two windows, and that the trailing 3-7 days are
**still maturing** per `conversion-measurement`. Run the data-integrity
spot-check (listed-action volume vs. the profile's outside source via its connector, or
the cheaper `budget-plan.md` CAC-tab cross-read — say which). If the source
is unreachable, the header reads "integrity check SKIPPED: source
unreachable", never nothing.

### 2. Pull the data — seven pulls, mandatory filenames

Save each to `OUTPUTS_DIR/raw/YYYY-MM-DD/` and declare it in
`_manifest.json` as it is saved (literal filter, row count, temporality).
`tools/build_weekly_review_facts.py` opens each file by exactly this name and
refuses to build if one is missing. The paved path: `python3 tools/declare_pull.py <raw_dir> <filename> --gaql '<the exact query that ran>'` writes the entry for you (rows, truncation, temporality derived from the query itself); hand-written entries remain valid. Select the identical field list for both
weeks of any paired pull — an omitted field silently kills its WoW row.

| File | Query |
| :- | :- |
| `campaigns-current.json` | Undated structure: `FROM campaign WHERE campaign.status = 'ENABLED'`, selecting id/name/status/channel, `bidding_strategy_type`, `maximize_conversion_value.target_roas`, `maximize_conversions.target_cpa_micros`, `target_cpa.target_cpa_micros`, legacy `target_roas.target_roas` (as a control — targets live on the sub-message, and an empty target column is a field-name bug until proven otherwise), `campaign_budget.id/amount_micros/explicitly_shared`. The only valid source for a current-state claim. |
| `campaign-cost-is-7d.json`, `campaign-cost-is-prior7d.json` | Same filter plus the two windows: current = the last full Mon-Sun week as an explicit `BETWEEN '<mon>' AND '<sun>'`, prior = the week before it, same form (`LAST_7_DAYS` is accepted for an intraweek check; the builder refuses a pair that overlaps, differs in length, or declares the prior week as a relative token), selecting `campaign.id`, `metrics.cost_micros/impressions/clicks/search_impression_share/search_budget_lost_impression_share/search_rank_lost_impression_share/search_absolute_top_impression_share/search_top_impression_share`. |
| `campaign-conversions-7d.json`, `campaign-conversions-prior7d.json` | Same filter, same two windows as the cost pair, plus `segments.conversion_action_name IN (<the 8, verbatim from conversion-measurement>)`, selecting `campaign.id`, the segment, `metrics.conversions/conversions_value`. Several rows per campaign; the row key is the pair. |
| `account-spend-mtd.json` | `FROM customer WHERE segments.date BETWEEN '<first of month>' AND '<yesterday>'`, selecting `segments.date`, `metrics.cost_micros`. No status filter, on purpose — the pacing input. |
| `change-events-14d.json` | `FROM change_event WHERE change_event.change_date_time DURING LAST_14_DAYS` (this resource's maximum), selecting date/type/operation/`changed_fields`/campaign/ad_group/user_email/client_type, `ORDER BY change_event.change_date_time DESC LIMIT 10000` (the query 400s without a LIMIT). |

### 3. Build the facts file, then write every figure from it

Run `python3 tools/build_weekly_review_facts.py <raw_dir>` before drafting a
single number. It writes `weekly-review-facts.json` beside the pulls. If a
number you want has no fact, add it to the builder and re-run — never compute
it in the report body, never state it uncited. If the builder refuses, the
refusal is the finding: report it, never fall back to prose arithmetic.

### 4. Real pacing — lead with this

Spend-to-date against the stated budget pro-rated by day, in CAD, named.
The budget is **read** (from `tools/account-config.json`, sourced from
`budget-plan.md`), never derived; a month with no stated figure gets a
null-with-reason fact — ask the owner, never carry another month forward.
Report the pacing percentage, overspend-to-date, the projected full month
(label projections as projections), and the daily cut needed to land on
budget. The planned month-over-month step down is the plan, never a problem;
overspend against the lower budget is the separate, real finding — keep the
two sentences apart. Any increase names its elasticity band (config
`thresholds`), and no increase is on the table while pacing is over 100%.

### 5. Brand, non-brand and top-of-funnel — three subtotals, never one

Per guardrails 1 and 2, a blended CPA or ROAS never leads:
**Brand Core** on its own CPA, never scaled on performance grounds — the only
admissible Brand trigger is `search_rank_lost_impression_share` rising WoW,
both weeks quoted. **Non-brand Search + PMax** on assumed-value CPA/ROAS —
the number the report leads with after pacing. **TOF (YouTube, Demand Gen)**
on reach/engagement, never BOFU CPA/ROAS, and held out of the non-brand
denominator. The three subtotals are asserted to sum to the account total.

### 6. Week over week, and what changed

Every metric against the prior week from the delta facts, absolute and
percentage, sign printed ("down 3.8%" fails the checker against `-3.83`).
A campaign surfaces in Wins/Concerns only if it crosses a threshold from
`tools/account-config.json` (`thresholds` block: lost-IS gates, WoW move,
pacing band, Brand Abs-Top-IS floor, and actual-vs-target gap —
`target_gap_pct` — worse **or** better than target, since beating a target
is the post-target-change exposure case). Silence on a metric is a valid report
state — do not pad. Then report what changed from the change-events pull:
who changed what, and whether it matched a prior recommendation.

An `actual-vs-target` gap is suppressed when the target moved inside the
window (report the edit and its date instead), carries the goal-basis caveat
from `conversion-measurement` when shown, and — like every performance
claim — takes step 8's learning-window downgrade where one applies.

### 7. Impression share and available impressions

Impression-share fields return sentinels at both ends (`0.0999` = "<10%",
`0.9001` = ">90%") across the whole field family; the builder suppresses
them as null facts with reasons. Available impressions
(`impressions ÷ search_impression_share`) is computed only off non-sentinel
shares; suppress it elsewhere and say why.

> **GATED — not cleared to ship.** Available impressions waits on the account
> owner confirming one figure against their Google Ads custom column. Until
> then it appears only in internal drafts marked gated, never to the
> stakeholder. Every such fact id carries `.gated.`.

### 8. Learning windows — match the change type to the claim type

Guardrail 7, scoped (this account changes budgets most weeks; unscoped, this
rule silences the whole report): CPA/ROAS/volume findings and
bid/budget/target recommendations on a campaign with a bid/budget/target
`change_event` inside 14 days (Search) or 28 (PMax) downgrade to
"monitor — in learning", stating **both the change date and type**. Pacing,
impression-share and spend mechanics, and copy/policy/structure findings do
not downgrade. Guardrail 5's floor also binds: below 5 in-window 8-action
conversions for the campaign or cluster, a bid/budget/target recommendation
becomes a monitor note.

### 9. Recommended actions, and log them

3-5 actions, each tied to the skill that does the work (budget →
`budget-optimize`; search-term waste → `mine-search-terms`; underperformance
→ `investigate-campaign`; root cause → `performance-analysis`; copy →
`audit-ad-copy`). Each states its
learning-window and conversion-floor treatment. Campaigns in one pull but
not the other are listed, never dropped. Append each action to
`OUTPUTS_DIR/recommendations-log.csv` per `budget-optimize` step 8 — that
spec, not a local variant.

### 10. Verify, then present

Tier 1, 2, 3 per `verification-protocol`. Both mechanical checks clean
before any agent runs: `python3 tools/citations.py <report> <facts>
<outputs_dir>` plus the builder's own validation. Pass `<outputs_dir>` (the
directory holding `recommendations-log.csv`) — without it the append check
silently does not run, and this skill is one that claims an append. Tier 2 (`google-ads-verifier`) judges what no
checker can: subtotal discipline, assumed-value qualifiers, learning-window
scoping, freshness, the Brand rule, characterisation. Tier 3
(`google-ads-refuter`) attacks the recommended actions. When stating a
run-over-run delta, open the prior dated report and read how it defined the
figure first (a required Tier 2 input).

Present, inside the 150-250 line target: **Pacing** first, **non-brand
assumed-value CPA/ROAS** with the real CAC anchor, **Brand and TOF** on their
own labelled lines, **Concerns** and **Wins** (threshold-crossing only),
**Recommended actions**; then, secondary or collapsed: the campaign table,
what changed, and impression share with its gate banner. Persist to
`OUTPUTS_DIR/weekly-review-YYYY-MM-DD.md` with the verification footer —
presenting in conversation only is an incomplete run.

## Retired — do not reintroduce (reasons in MAINTENANCE.md)

The "nobody touches this account" framing ·
one-off date countdowns (post-change logic lives in `budget-optimize`)
· trailing 4-week average and mature-vs-mature windows (never pulled; either
returns only as a named pull in step 2) · weekly segment cuts (on request,
via `performance-analysis`) · Auction Insights (the `metrics.auction_insight_*`
fields exist in the GAQL schema but return 403 `PERMISSION_DENIED` under
a Basic Access developer token, the
accurate version of VF-12's "not queryable"; a Standard Access upgrade
would unblock it. Use the rank-lost proxy; an operator CSV may be read if
present, never blocked on).
