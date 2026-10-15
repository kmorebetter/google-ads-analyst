---
name: mine-search-terms
description: "Mine the Google Ads search term report for negative keyword candidates, decided by numbers only — 30-day spend, clicks and 8-action conversions against thresholds the account owner sets, never by relevance judgement. Produces a top-10 decision table, a cross-campaign overlap section, keyword ideas taken from the account's own converting terms, and a linted negatives CSV. Pulls via the local read-only Google Ads CLI, refuses a truncated pull, refuses to run without the thresholds, keys every join on the (parent, child) pair, and traces every figure to a facts file. Use when the user says 'mine search terms', 'find negatives', 'search term report', 'negative keywords', 'clean up search terms', 'search term overlap', 'cannibalization', 'keyword ideas', or wants to audit search term spend."
---

# Mine Search Terms

Decide which search terms to negate from the numbers, produce the top-10
table the account owner asked for, and ship a linted negatives CSV. This
output moves money: **Tier 1 + 2 + 3**. Target **150-250 lines**; a
1,000-line report is a failure of this skill. VF-rule rationale lives in
`docs/VERIFICATION-RULES.md`; incident history in `MAINTENANCE.md`.

## Prerequisites

Load `verification-protocol`, `search-term-methodology` (owns **the whole
decision rule** — not restated here), `account-conventions`,
`campaign-guardrails` (rules 2, 5, 7), `conversion-measurement`,
and `evidence-links`. A term with any priority conversion
(`conversion_actions.priority`) in 90 days is never a negative. Also required: **`budget-plan.md`**
(real-money CAC anchors) and **`STANDING-ITEMS.md`** (the required read).
**No API?** Use `csv-import` and `tools/csv_negatives.py` instead; it applies
the same numbers-only rule to a search-terms CSV export.

## Refusals (enforced in the builder; each is stated, never routed around)

1. **No thresholds, no run.** The owner's four parameters per
   `search-term-methodology`. If any is absent, stop and say what is
   needed — a run that picked plausible numbers shipped an unstated subset.
   Offer the `--sensitivity` grid so the owner can choose with the consequence in
   front of them.
2. **A truncated pull is not data.** A pull returning exactly 10,000 rows or
   carrying a `nextPageToken` is capped; the manifest records `truncated`
   per file and the run refuses — a total off a capped pull is a floor, not
   a value. Narrow or paginate until the token is gone, then re-save. (The
   old `LIMIT 2000` guard is retired and stays retired — a `LIMIT` hides
   the cap rather than exposing it.)
3. **No 90-day clearance, no CSV.** The builder emits zero rows without
   `donotnegate-90d-8action.json` in the raw directory — a 30-day window
   cannot see a 90-day conversion. No flag can supply it; do not work
   around it by hand.
4. **No manifest, no facts file** (VF-17: temporality is declared, never
   inferred).

## Standing rules

- **Row keys (VF-19).** Search terms key on
  `(campaign.id, ad_group.id, search_term, keyword text)`; keywords on
  `(ad_group.id, criterion_id)` — criterion IDs are reused across ad groups
  in this account. Print row counts beside distinct-key counts. Match type
  is not in the key (the conversions pull cannot select it); assert adding
  it creates no new distinct keys.
- **Two-query rule (VF-3)** on `search_term_view`; the conversions pull is
  segmented by action — aggregate per term key **before** joining.
- **`LAST_90_DAYS` is not valid GAQL** — use the `BETWEEN` form for any
  window over 30 days.
- **Assumed-value labelling (VF-8)** on every CPA/ROAS sentence, real-money
  CAC anchor beside it; re-pull `conversion_action.value_settings` each run
  and fail loudly if a default changed.
- **No CTR from this pull** — `search_term_view` segmented by keyword
  returns rows with more clicks than impressions; report the anomaly count,
  never compute a rate. **Search-term links** only in the
  verified campaign-scoped form (`evidence-links`); the term itself is
  still cited as text with campaign/ad-group names and IDs.
- **Facts and citations (VF-15a).** Every figure carries `[fact: id]`.
  Aggregates, counts, thresholds and top-10 rows only — never a fact per
  term (a dense value space is what makes fabricated numbers pass).

## Steps

### 1. Pull the data — six pulls

Save each to `OUTPUTS_DIR/raw/YYYY-MM-DD/`, declared in `_manifest.json` as
saved (literal filter, row count, truncation state, temporality). The paved path: `python3 tools/declare_pull.py <raw_dir> <filename> --gaql '<the exact query that ran>'` writes the entry for you (rows, truncation, temporality derived from the query itself); hand-written entries remain valid.

| File | Query |
| :- | :- |
| `search-terms-cost-30d.json` | `FROM search_term_view WHERE segments.date DURING LAST_30_DAYS AND metrics.cost_micros > 0`, selecting `campaign.id/name`, `ad_group.id`, **`ad_group.name`** (Editor imports negatives by ad-group *name* — a pull without it fills the CSV with placeholders), `search_term_view.search_term/status`, `segments.keyword.info.text/match_type`, `segments.keyword.ad_group_criterion`, `metrics.cost_micros/clicks/impressions`. **No campaign filter** — overlap cannot be seen from one campaign's slice. |
| `search-terms-conversions-30d.json` | Same resource and window plus `segments.conversion_action_name IN (<the 8, verbatim>)`, selecting the term key fields, the segment, `metrics.conversions/conversions_value`. |
| `campaigns-current.json` | `FROM campaign WHERE campaign.status = 'ENABLED'` — names and bid strategy, undated. |
| `change-events-14d.json` | `FROM change_event WHERE change_event.change_date_time DURING LAST_14_DAYS` with the standard field list and a `LIMIT` (400s without one). |
| `donotnegate-90d-8action.json` | **Required.** Same resource and 8-action clause over 90 days via `BETWEEN '<90 days ago>' AND '<yesterday>'`, selecting term key fields plus names, the segment, `metrics.conversions/conversions_value`. Any term with a non-zero 8-action conversion in 90 days gets `KEEP`, outranking every negate path. |
| Existing negatives (two pulls) | `campaign_criterion` and `ad_group_criterion` where `negative = true`. Pass to the builder with `--existing-negatives`; it opens each file and checks every candidate against EXACT/PHRASE/BROAD coverage at the term's ad group and campaign level — covered candidates come off the CSV into the scope trail. A missing path is a hard refusal. Without the flag every CSV row says the check did not run, and the file must not be imported. |

### 2. Decide every term

Run the builder. It applies the status gate, the term-scoped safety check,
the CPA cut line and the two zero-conversion floors, in that order, from
numbers only, then attaches a category afterwards. **The category is a label
and can never be the reason** — state the numbers; the owner rejected
category-as-reason by name.

### 3. Cross-campaign overlap (guardrail 2)

Group the full unfiltered cost pull by search-term text; for every term
serving from more than one campaign, list each campaign's clicks, cost and
8-action conversions and say which campaign should own it per
`account-conventions`. **Brand Core is identified by campaign ID, never by
the word "Brand" in a name** (which also matches eight non-brand campaigns);
the IDs live in `tools/account-config.json`. A generic query serving from a
Brand campaign is an overlap finding, never a scaling opportunity. **This
section never reaches the CSV** — a routing negative only works paired with
adding the term to the owning campaign, which a negatives file cannot
express.

### 4. Keyword ideas — from the account, not the planner

Terms with `status = 'NONE'` that converted on the 8 actions are converting
queries that are not yet keywords. Split branded from non-branded — a
branded idea is Brand housekeeping, never a growth finding. Keyword Planner
sizes a term, never picks one: use `generate-keyword-ideas` only to attach
volume to a term the account already converted on, vertical-filter every
suggestion, and report the count dropped.

### 5. Learning windows (guardrail 7, scoped to criteria)

A negation rests on one term's 30-day numbers inside one ad group, so only
a **criterion** change there could plausibly cause it: match
`AD_GROUP_CRITERION` / `CAMPAIGN_CRITERION` events on the term's own ad
group or campaign, cite the event type, and downgrade matched rows to
"monitor — in learning" (off the CSV) with **date and type** stated. A
`CAMPAIGN_BUDGET` change does not downgrade a term-level negation — the
unscoped reading downgrades everything and says nothing.

### 6. The negatives CSV

Waste negatives only, deduplicated, at one declared level. Columns, in
order: `Level`, `Campaign`, `Campaign ID`, `Ad Group`, `Ad Group ID`,
`Keyword`, `Match Type`, `Search Term`, `Triggering Keyword`,
`Triggering Match Type`, `Clicks`, `Impressions`, `Cost (CAD)`,
`Conversions (8 actions only)`, `Assumed Value (CAD)`, `Decision`,
`Why (numbers)`, `Category (label only)`, `Existing Negative Check`.
One negative is one `(campaign, ad group, keyword text, match type)` — merge
duplicates, sum their traffic, report the collapsed count. Lint before
declaring ready (column counts, quoting, 80-char keyword limit — documented,
never verified against Editor: say so), and report every count in the
footer. Save to `OUTPUTS_DIR/search-term-negatives-YYYY-MM-DD.csv`.

### 7. Build the facts file, then write every figure from it

Run `python3 tools/build_mine_search_terms_facts.py <raw_dir>
--min-spend-cad … --min-clicks … --target-cpa-cad … --target-cpa-multiple …
--existing-negatives …` (see `--help`) before drafting a single number. It
writes `mine-search-terms-facts.json`. Comparisons obey the scope-match
rule; a refusal is the finding.

### 8. Verify, then present

Tier 1, 2, 3 per `verification-protocol`. Footer prints: truncation state
per pull, row/distinct-key counts, join completeness (conversion keys with
no cost row listed, never dropped), the four thresholds and that they came
from the owner, the CSV lint counts, and the existing-negatives check state.
**Tier 3 is not optional here** — it is the only tier that has ever
overturned conclusions on this skill; point `google-ads-refuter` at the
negation list and any pattern-level negative. (In a `run-review` session
the composite Tier 3 pass covers this, and the CSV stays a draft until it
passes.)

Present, inside 150-250 lines: **the thresholds used** (named as the
owner's, with the sensitivity line if unchosen), **the top-10 table** (the
10 highest-spend rows whatever their decision — keeps and negates both;
term, matched keyword, match type, campaign/ad group names and IDs, spend,
clicks, 8-action conversions, assumed-value CPA/ROAS, decision, one-line
why in numbers; no rank column — order carries it; `n/a` in a code span,
never `0`), **the negation total** (rows, dollars, held and not-eligible
counts); then secondary or collapsed: **overlap**, **keyword ideas** split
branded/non-branded, **learning-window downgrades** with dates and types,
**the CSV link with its lint results**. Persist to
`OUTPUTS_DIR/mine-search-terms-YYYY-MM-DD.md` with the verification footer;
log every batch to `recommendations-log.csv` per `budget-optimize` step 8.

### 9. Hand off — this pack is read-only

Negatives are never applied from here. The deliverable is the CSV; the user
imports it. Say plainly that the schema is unverified against Editor and
that the existing-negatives check must have run before anything is imported.
