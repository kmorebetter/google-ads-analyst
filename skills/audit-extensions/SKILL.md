---
name: audit-extensions
description: "Audit Google Ads asset/extension coverage and performance across campaigns: a campaign x field type coverage grid resolved into campaign-level, inherited-from-account and genuinely absent, per-field-type serve performance from asset_field_type_view, and named recommendations with campaign IDs and deep links. Pulls campaign_asset, customer_asset and asset_field_type_view via the local read-only Google Ads CLI. Use when the user says 'audit extensions', 'extension recommendations', 'ad extensions', 'assets audit', 'sitelinks', 'callouts', 'structured snippets', 'price extensions', 'which campaigns are missing extensions', 'do we have enough sitelinks', or asks what extensions to add."
---

# Audit Extensions

Which enabled campaigns are covered for which extension type, how each type
actually serves, and which extension to add where. Runs **Tier 1 + 2** (no
money moves here). Target **100-200 lines**, opening with "what extension
should I add where" — campaign, type, reason. VF-rule rationale lives in
`docs/VERIFICATION-RULES.md`; incident history in `MAINTENANCE.md`.

## Prerequisites

Load `verification-protocol`, `account-conventions`, `campaign-guardrails`,
`evidence-links` and `conversion-measurement`. Also required:
**`STANDING-ITEMS.md`** (the required read; `MAINTENANCE.md` on demand).

## Two counts, never one (VF-19 for extensions)

`campaign_asset` returns one row per campaign-asset **attachment**, and this
account shares assets across campaigns heavily. Two different figures answer
two different questions, and every stated extension count says which it is:

| Figure | Unit | Answers |
| :- | :- | :- |
| link rows | `campaign_asset_link_rows` | **coverage** — how many campaigns carry this type |
| distinct `asset.id` | `distinct_assets` | **variety** — how many different creatives exist |

Row keys: `(campaign.id, asset.id, field_type)` for `campaign_asset`,
`(asset.id, field_type)` for `customer_asset`, `(campaign.id, field_type)`
for `asset_field_type_view`. The account-wide asset figure is the **union**
across the campaign- and account-level pulls — most account-level assets are
also campaign-attached, so adding the two counts double-counts them.

## Scope — `campaign_asset.status` does not filter the campaign

It filters the **attachment**; the pull returns rows on paused and removed
campaigns. Every count is joined client-side to the undated
`campaign.status='ENABLED'` pull on `campaign.id`, every scope label says
so, and the filter goes in the report header verbatim. Never write
"account-wide extensions" without it.

## Account level versus campaign level — get this right or every finding is wrong

A campaign with no campaign-level sitelinks is **not** uncovered.
**Inheritance is `[verified]` from this account's own data** (campaigns draw
serve impressions for types they hold no campaign-level asset of).
**Suppression is `[assumption]`**: that a campaign-level asset *replaces*
the account-level set rather than adding to it is Google's documented
behaviour, not derivable from these pulls — label any variety claim that
rests on it. Only `ext.account_level.<type>.distinct_asset.count` answers
"is this inheritable"; `ext.union.<type>...` answers "how much variety
exists" — one word apart, opposite meanings, and citing the union as "the
account level" passes the mechanical checker with the wrong attribution (VF-19's
hazard; only Tier 2 catches it).

A coverage cell has **three** states, never two — a two-state grid reports
inherited coverage as gaps and a reader will act on it:

| State | Meaning | Is it a gap? |
| :- | :- | :- |
| `campaign_level` | the campaign has its own asset of that type | no |
| `inherited_from_account` | no campaign-level asset; type exists at account level | **no** |
| `absent_everywhere` | neither level has one | **yes** |

**Ad-group-level assets are out of scope** — no `ad_group_asset` pull exists
here, so an ad-group override is invisible (PROMOTION serving with no
visible asset is the known symptom). Report that as a limit of this skill,
not an account defect.

## Steps

### 1. Pull the data — four pulls, none optional

Save each to `OUTPUTS_DIR/raw/YYYY-MM-DD/`, declared in `_manifest.json` as
saved (literal filter, row count, temporality). The paved path: `python3 tools/declare_pull.py <raw_dir> <filename> --gaql '<the exact query that ran>'` writes the entry for you (rows, truncation, temporality derived from the query itself); hand-written entries remain valid. Check each pull's row count
against its distinct-key count and print both.

| File | Query |
| :- | :- |
| `campaign-assets-current.json` | `FROM campaign_asset WHERE campaign_asset.status='ENABLED'`, undated, selecting `campaign.id`, `campaign_asset.field_type/status`, `asset.id/type`, `asset.sitelink_asset.link_text`, `asset.callout_asset.callout_text`, `asset.structured_snippet_asset.header`. |
| `customer-assets-current.json` | `FROM customer_asset WHERE customer_asset.status='ENABLED'`, undated, selecting `customer_asset.field_type/status`, `asset.id/type`. **Zero rows is a real answer** — nothing is inherited and every blank cell is a genuine gap. |
| `asset-field-type-30d.json` | `FROM asset_field_type_view WHERE segments.date DURING LAST_30_DAYS`, selecting `campaign.id`, `field_type`, `metrics.impressions/clicks`. |
| `campaigns-current.json` | Enabled campaigns, undated — reuse the preflight drift pull. Names, IDs, `advertising_channel_type` for the join and links. |

Also read `change-events-14d.json` (step 4).

### 2. Coverage grid

One row per enabled campaign, one column per field type, each cell one of
the three states. Campaign name **and** numeric ID on every row, with the
`/aw/adgroups?ocid=...&campaignId=<c>` deep link (the verified path that
resolves to a named campaign). **Known limitation, stated:** no verified
path reaches the extensions view — the link lands on ad groups and the
reader navigates to Assets; never imply it opens the extension.

Field-type treatment:

| Field type | Treatment | Why |
| :- | :- | :- |
| `SITELINK`, `CALLOUT`, `STRUCTURED_SNIPPET` | **judged** on coverage and variety | Google serve minimums exist (`2` sitelinks, `2` callouts, `3` snippet values) and they move a search ad |
| `PRICE` | **judged as an opportunity**, not a failed minimum | no Google minimum, but nothing at account level means nothing serves |
| `AD_IMAGE`, `BUSINESS_NAME`, logos | **reported only** | PMax/automation types, no search minimum to judge |
| `PROMOTION` | **reported only**, as a scope limit | needs the `ad_group_asset` pull this skill does not make |
| `VIDEO` / `DEMAND_GEN` campaigns | **reported only** | guardrail 1; their extension model is asset-group level |

**A clean bill is a finding** — zero `absent_everywhere` on the judged types
is said plainly, never padded into a manufactured gap; the residual finding
is usually variety, not coverage.

### 3. Performance per field type — and what it cannot tell you

`asset_field_type_view` attributes per **field type**, not per asset — it
cannot say which sitelink earned a click; never answer "which asset should I
drop" from it. Its impressions fan out (VF-4 rule 1: one impression credits
every showing type), so: never sum across field types (the builder emits
the sum once, labelled as the artefact it is, so a report can name it
instead of computing it); never present one type's impressions as a share
of the account's; per-field-type cost is not emitted at all. Comparing two
field types on the same campaigns and window **is** valid (same
unit/scope/window/basis). `basis` is `broad-goal-set` — this resource
carries no conversion-action segment, so nothing here is on the 8 actions.

### 4. Guardrails — two rules that look like one

**Guardrail 7 does not apply** — coverage/structure findings are on its
never-downgrade list; a budget change did not remove a price extension. No
downgrade logic in this skill. **VF-17 rule 3 applies independently and is
live**: before asserting a campaign lacks an extension, check the change
pull for `CAMPAIGN_ASSET` / `CUSTOMER_ASSET` / `ASSET` events on it —
extensions are actively edited in this account (count events and campaigns
separately and say which). Dropping both rules while believing one covered
them is the easy mistake. **Guardrail 6, trademark — state it, admit it
cannot be checked here:** the restriction is recorded per ad, and all four
pulls are campaign/account level. Structural coverage findings are in scope
for every campaign, conquesting included; a recommendation proposing **new
extension text** on a conquesting campaign inherits the exclusion
(cross-reference `account-conventions`). Say in the footer that the
restriction was not checked and why — detecting it needs `audit-ad-copy`'s
ad-level pull.

### 5. Recommendations

Each names the campaign, numeric ID and deep link; then which field type,
which grid state, and why it helps. Two shapes only, each **one**
recommendation, not one per campaign: **`absent_everywhere` on a judged or
opportunity type** — one recommendation carrying a table of every affected
campaign (never a subset picked by eye; if ranking inside the table, rank
by 7-day cost from `campaign-cost-is-7d.json` and emit the rule and count
as facts); **variety below the Google minimum on a covered type**. Do not
recommend from an `inherited_from_account` cell unless adding a
campaign-specific asset for a stated reason — and then say the suppression
assumption out loud: adding one **removes** the account-level assets from
that campaign; a trade, not an addition. No prose-quality sweeps of
extension text, no per-asset performance claims, no ad-group-level
recommendations.

### 6. Build the facts file, then write every figure from it

Run `python3 tools/build_audit_extensions_facts.py <raw_dir>` before
drafting a single number; it writes `audit-extensions-facts.json`. Every
figure carries `[fact: id]`; this skill's own thresholds and the footer's
counts go in code spans. Write window lengths hyphenated (`14-day`, not
"14 days" — a bare numeral reads as a figure). Comparisons obey the
scope-match rule — an attachment count and an asset count carry different
units for exactly that reason, and the refusal is the finding.

### 7. Verify, then present

Tier 1 + 2 per `verification-protocol`. Footer prints: row/distinct-key
counts per pull; the campaign-status join with row counts before and after;
declared filters matching scope labels clause for clause; every
current-state fact reading `current-as-of-<date>`; and that the trademark
restriction was **not** checked, with why. Tier 2 (`google-ads-verifier`)
judges the four things no checker can test: no `inherited_from_account`
cell reported as a gap, no sums or shares across field types, every count
labelled attachments-or-assets, no new-ad-text recommendation on a
conquesting campaign.

Present, inside 100-200 lines: **recommendations** first; the **scope line**
(literal filter, the join, current-as-of date); the **coverage grid** with
its three states; **performance per field type** with the fan-out caveat
beside it; **what this run could not see** (ad-group-level assets, the
trademark check). Persist to `OUTPUTS_DIR/audit-extensions-YYYY-MM-DD.md`
with the verification footer.
