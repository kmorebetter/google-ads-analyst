---
name: audit-ad-copy
description: "Audit Google Ads RSA copy across campaigns for quality, relevance, testing gaps, per-headline serve performance, and keyword-level Quality Score components (Exp. CTR / Ad Relevance / LP Experience). Pulls ad creative data via the local read-only Google Ads CLI, evaluates against ad-copy-principles, and flags underperforming or missing creative. Use when the user says 'audit ad copy', 'review our ads', 'ad copy check', 'RSA audit', 'creative review', 'which headline should we swap', 'quality score', 'landing page experience', 'are our ads good', or wants to improve ad creative quality."
---

# Audit Ad Copy

Audit RSA copy for quality, coverage gaps, per-headline serve performance and
swap candidates. Runs **Tier 1 + 2**. Target **150-250 lines**, opening with
the answer to "which headline should I swap" — ad, headline, replacement.
VF-rule rationale lives in `docs/VERIFICATION-RULES.md`; incident history in
`MAINTENANCE.md`.

## Prerequisites

Load `verification-protocol`, `ad-copy-principles`, `account-conventions`,
`evidence-links`, `conversion-measurement` and `campaign-guardrails` (rules
6, 7, 9 own the reasoning for the trademark exclusion, learning windows and
the null-Quality-Score non-finding). Also required: **`budget-plan.md`** and
**`STANDING-ITEMS.md`** (the required read; `MAINTENANCE.md` on demand).

## Scope filter (VF-5) — every ad-level query, verbatim

```
ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED'
AND campaign.status = 'ENABLED' AND campaign.advertising_channel_type = 'SEARCH'
AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
```

Three statuses, not two (an "enabled" ad in a paused ad group is not
servable), plus channel and ad type (without it, DSA ad groups read as
coverage gaps). A row passing the whole block is **servable**; ads that fail
it may appear as context, never in a denominator, total or flag. The
normalised one-line form lives in `tools/scopes.py` as `VF5_FULL_FILTER` —
reproduce it character for character in the report header and every ad-level
fact's `scope`, and compare each `_manifest.json` filter clause for clause
after normalising. If a resource rejects one of the five fields (step 5's
asset view), filter that dimension client-side against the step 1a inventory
and record it.

## Standing rules

- **Row keys (VF-19).** Ads key on `(ad_group.id, ad.id)`, assets on
  `(ad_group.id, ad.id, asset.id, field_type)`, keywords on
  `(ad_group.id, criterion_id)` — this account reuses every class of child
  ID across parents, and a bare-ID key makes results depend on dict
  insertion order. Print row counts beside distinct-key counts.
- **Freshness (VF-17).** "Is X true right now" is answered only by step 1a's
  undated pull; a saved pull is evidence about its own date; check
  `change_event` before asserting a defect is unremediated.
- **Facts and citations (VF-15a).** Every figure traces to a fact id in
  `audit-ad-copy-facts.json`; missing fact → extend
  `tools/build_audit_ad_copy_facts.py`, re-run. Structurally-uncitable
  exemptions, written in code spans: character counts of proposed copy, the
  footer's own counts, this skill's constants (`top 10`, `CAD 2,000`,
  `CAD 300`).
- **Evidence links (VF-13).** Every object claim carries name and numeric
  ID; ad-level claims carry the ad-group deep link with the ad ID beside it
  (no URL selects a single ad). No login params; search-term links only in the verified campaign-scoped form per `evidence-links`.

## Per-asset data (VF-4) — five rules, none negotiable

Google credits **every** asset in a serve with the whole ad's metrics, so:
(1) never sum metrics across assets — any asset-level total is a fiction;
(2) never compare an asset in one ad against an asset in another ad or ad
group; (3) never report a per-asset conversion count, CPA, ROAS or value —
the asset view rejects the 8-action segment, so the only obtainable figure
is broad-goal-set: available and wrong; do not select conversion metrics on
it at all; (4) the only valid comparison is assets **within a single ad,
ranked by impressions**; (5) `performance_label` is not a headline ranking
signal (no LOW headlines exist account-wide) — print as context, rank by
serve count. **Descriptions are the exception**: a LOW description is a
usable swap candidate under the same within-ad rules.

**The intersect (VF-4b), mandatory.** A 30-day serve pull includes
since-removed assets. Join every serve row to 1a's current inventory:
in inventory + served → rank it; in inventory + zero serves → **dead slot**
(the actionable zero); served + not in inventory → already removed, history
or nothing, **never a live problem**. 1a returns no asset IDs, so this join
matches on `(ad_group.id, ad.id, field_type, text)` — the single authorised
exception to never-key-on-text, and it carries a check: both sides must show
every key count = 1 (printed in the footer); on a collision, report that ad
by asset ID from the serve side and flag it.

## Steps

### 1. Pull the data — three pulls, never one

Never select the repeated `headlines`/`descriptions` arrays and date-filtered
metrics in one query — rows repeat the ad's spend and any sum overstates it.
Declare every pull in `_manifest.json` as saved. The paved path: `python3 tools/declare_pull.py <raw_dir> <filename> --gaql '<the exact query that ran>'` writes the entry for you (rows, truncation, temporality derived from the query itself); hand-written entries remain valid.

| File | Query |
| :- | :- |
| `audit-ad-copy-rsa-inventory.json` | **1a. Inventory, undated, no metrics** — the only valid source for a current-state claim. Canonical filter, selecting campaign/ad-group id+name+status, `ad_group_ad.ad.id`, `.status`, `.ad_strength`, `responsive_search_ad.headlines/.descriptions`, `final_urls`, `policy_summary.approval_status/.policy_topic_entries`. |
| `audit-ad-copy-rsa-metrics-30d.json` | **1b. Metrics, windowed, no asset arrays** — canonical filter + `LAST_30_DAYS`, selecting ids + `metrics.impressions/clicks/ctr/cost_micros`. |
| `audit-ad-copy-rsa-conversions-30d.json` | **1c. Conversions, 8 actions** (two-query rule): same resource/window, selecting ids (**`ad_group.id` mandatory**), the segment, `metrics.conversions/conversions_value`, plus the 8-action IN clause. |

Before summing 1b/1c, check row count vs distinct `(ad_group.id, ad.id)`
count; aggregate per pair first if segmented.

### 2. Read the change log

`FROM change_event ... DURING LAST_14_DAYS` (the maximum), standard field
list, saved as `audit-ad-copy-change-events-14d-detail.json`. Report what
changed and whether it matched a prior recommendation — its own section.
The "creative side is frozen" finding is retired; the account is actively
edited.

### 3. Quality Score components per keyword — two pulls

**3a** `audit-ad-copy-keyword-quality-current.json`, undated: `keyword_view`
filtered on enabled campaign + ad group + criterion + SEARCH channel,
selecting ids, keyword text, **match_type** (step 6 needs it), and
`quality_info.quality_score/.creative_quality_score/.post_click_quality_score/.search_predicted_ctr`
(= QS, Ad Relevance, LP Experience, Exp. CTR; three-level enums, empty for
low-impression keywords — normal, and per rule 9 never a finding).
**3b** `audit-ad-copy-keyword-cost-30d.json`, same filters + 30-day window,
selecting cost/impressions/clicks; join on the `(ad_group.id, criterion_id)`
pair. Two flags and only these two: **LP Experience BELOW_AVERAGE on a
keyword above `CAD 300` 30-day cost** → that lander to the A/B queue (a
landing-page finding, not a copy fix; the threshold keeps the flag from
naming ~55 keywords or shipping an unstated subset); **Ad Relevance
BELOW_AVERAGE while Exp. CTR is fine** → a copy-relevance problem.

### 4. Classify policy restrictions, then scope out the trademark cases

Enumerate every `policy_topic_entries[]` topic before deciding anything:
`TRADEMARK*` → **excluded** (legal exposure, confirmed with the owner);
`FREE_DESKTOP_SOFTWARE` → in scope on its own terms, never bundled with
trademark; `UNAPPROVED_SUBSTANCES` → in scope, report it and **diagnose
nothing** (no cause ever verified; `APPROVED_LIMITED` means it runs with
limits, never call it disapproved); any other topic → report the string
verbatim, exclude nothing. Never hard-code a policy census — compute it from
this run's inventory. The policy field is per asset, so 1a says which
headlines carry the restriction.

**The trademark exclusion is per ad, read from the policy field, never per
campaign by name.** Trademark-restricted ads are out of copy suggestions,
ad-strength/QS/keyword-insertion findings and step-8 swaps, but not out of
the step-5 serve table (a diagnostic, not a recommendation). Exclude an ad
group's QS when any servable ad in it carries a TRADEMARK topic. **A
conquesting ad with no restriction on record stays in scope** — rule 6 reads
the same way, and excluding conquesting by category would forbid the
keyword-insertion fix `ad-copy-principles` mandates. State the exclusion
(names, IDs, topic strings) and stop.

### 5. Per-headline serve report

One table per high-spend servable ad — selection rule, printed:
`top 10 servable RSAs by 30-day cost, plus any above CAD 2,000`.
`FROM ad_group_ad_asset_view WHERE field_type IN ('HEADLINE','DESCRIPTION')
AND segments.date DURING LAST_30_DAYS`, selecting ids, `asset.id`,
`asset.text_asset.text`, `field_type`, `performance_label`,
`metrics.impressions/clicks`. Save as
`audit-ad-copy-asset-serves-30d.json`. Add the canonical status filters only
if the resource accepts them (it has rejected them; the intersect covers the
gap). Key on the asset 4-tuple, apply **the intersect**, rank survivors. Per-headline facts exist only for the tabled ads (the selection rule above); a deeper dive on another ad re-runs the builder with `--ad <ad_id>` rather than doing prose arithmetic.

Columns: Headline | Asset ID | Impressions | Clicks | Label | Flag, in
descending impression order (no rank column — order carries it). Three flags
only: **protect** = top 3 by impressions within the ad; **swap candidate** =
**bottom 2** by impressions within the ad (strongest on a live headline with
zero serves over a full window); unflagged between. **Precedence:** protect
governs performance-motivated swaps only — a policy or copy-defect finding
(prohibited insertion, misspelling, false claim) overrides any flag at any
rank; say which reason applies and cite the serve share given up. The
primary output is the within-ad ranking, not dead slots.

### 6. Mechanical checks per servable ad

Only checks a fact or field answers; skip trademark-restricted ads in all:
fewer than 10 headlines / 3 descriptions · ad strength POOR/AVERAGE (a
variety score only — it does not read truth, spelling or policy) · keyword
insertion in a broad-match ad group or **any** conquesting ad group (hard
prohibition) · a LOW description. **Pinning cannot be read** — the API omits
`pinned_field` on unpinned slots, so "nothing pinned" and "not returned" are
indistinguishable: never state a pinning count; recommend the default (value
prop H1, CTA H3, rest unpinned). **No prose-quality sweeps across all ads**
(variety, social proof, front-loading) — nothing citable comes out;
`ad-copy-principles` holds the standards for *writing* step 8's replacement,
not a checklist.

### 7. Gaps at the ad group level

Only-1-RSA ad groups (see step 8's open question before recommending) ·
BELOW_AVERAGE LP Experience on high-spend keywords (a lander gap, never
bundled into a copy rewrite) · spend above `CAD 2,000` with zero 8-action
conversions, **at the ad level** (the level the data supports). Every
current-defect claim is checked against step 2's change log first; rule 7
learning-window notes attach where a change landed inside the window.

### 8. Recommendations — one change at a time

**At most ONE copy change per ad per run** (headline swap or description
swap, never both, never a new ad in place of a swap) — one change is what
makes the next run's read interpretable. Take the stronger case, queue the
other. Each swap: campaign/ad group/ad with IDs and deep link; the removed
headline with asset ID, impressions, serve share (performance swaps must be
flagged swap candidates; policy/defect removals override); replacement text
with a Bash-verified character count (`LC_ALL=en_US.UTF-8 printf '%s' "text"
| wc -m`); **confirm from 1a the headline is still on the ad today**; skip
trademark ads.

**Open question, pending with the owner — do not resolve it in a report:**
most live ad groups run only one ad, where a second ad is a bigger, cleaner
test than a swap (an open question for the account owner). Until
answered: recommend the swap, print the question beside it.

**Assumed-value labelling (VF-8).** Re-pull `conversion_action.value_settings`
each run (`preflight-conversion-values.json`); fail loudly if a default
changed. No CPA/ROAS figure is expected in this skill — a run writing one
has invented it. Any figure that ever does derive from those values carries
the qualifier in-sentence with a real-money CAC anchor.

### 9. Build the facts file, then write every figure from it

Run `python3 tools/build_audit_ad_copy_facts.py <raw_dir>` before drafting a
single number. Comparisons obey the scope-match rule; a refusal is the
finding — never prose arithmetic.

### 10. Verify, then present

Tier 1 + 2 per `verification-protocol`. Footer prints: row/distinct-key
counts per pull, the text-join collision check clean both ways with the
removed-asset count, `VF5_FULL_FILTER` reproduced character for character
with every manifest filter matching clause for clause, every figure cited or
exempt. Tier 2 (`google-ads-verifier`) judges what no checker can:
freshness, the intersect, per-asset rules 2 and 3, step 4's per-ad
trademark reading (nothing excluded merely for being conquesting), row
keys, no DSA-as-coverage-gap, no "creative is frozen" framing.

Present, inside 150-250 lines: **the swap list** first; the **scope line**
(literal filter, servable counts, selection rule, current-as-of date);
**what changed since the last audit**; then secondary or collapsed: serve
tables with flags, Quality Score split into Ad Relevance (copy fix) vs LP
Experience (lander fix), gaps and testing roadmap, policy restrictions.
Persist to `OUTPUTS_DIR/audit-ad-copy-YYYY-MM-DD.md` with the verification
footer.
