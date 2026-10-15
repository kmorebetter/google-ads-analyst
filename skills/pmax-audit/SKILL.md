---
name: pmax-audit
description: "Audit the Performance Max campaign's asset-group coverage and performance, and check whether a brand-exclusion list is attached (is PMax scavenging brand terms Brand Core should own?). Channel-level breakdown and PMax search-term/category insights are NOT available via the Google Ads API — this skill is explicit about that gap rather than fabricating a channel split. Use when the user says 'pmax audit', 'performance max review', 'asset group performance', 'is pmax eating brand', or wants to check Performance Max health."
---

# PMax Audit

Audit the account's single `Performance Max` campaign (per `account-conventions`)
for asset-group coverage, asset-group-level performance, and brand
cannibalization risk. **Scoped to what the Google Ads API actually exposes**
— several things a full PMax review would normally cover (channel-level
spend split, search-term/category insights) are UI-only and are called out
as such below rather than approximated.

## Prerequisites

Also load `evidence-links`: every object-level claim in
this skill's output carries the object's name **and** numeric ID, plus a deep
link where one exists.

Load `verification-protocol` (preflight + Tier 1 — this skill is
diagnostic, not money-moving, so no verifier agents by default),
`account-conventions`, `campaign-guardrails` (the Brand Core rule is
what the brand-exclusion check in step 3 is testing), and
`conversion-measurement` (the 8 actions step 2's conversions must be
restricted to). Save every pull to `OUTPUTS_DIR/raw/`.

## Steps

### 1. Asset group inventory and coverage

Query `asset_group` for the PMax campaign, then `asset_group_asset` per
group:
```
SELECT asset_group.name, asset_group.status FROM asset_group WHERE campaign.id = <pmax_campaign_id>
SELECT asset_group_asset.field_type FROM asset_group_asset
WHERE asset_group_asset.asset_group = '<asset_group_resource_name>' AND asset_group_asset.status = 'ENABLED'
```
Note: don't infer asset composition from the asset group's **name** — a
group named e.g. "45s Videos" can still carry a full complement of headlines,
descriptions, images, and long headlines alongside its video assets (verified
live: one such group here has 15 headlines, 5 descriptions, 4 marketing
images, 13 square images, 3 portrait images, and 3 YouTube videos). Pull the
actual field-type counts per group rather than assuming from the label.

For each asset group, flag gaps against the same coverage bar as RSAs (fewer
than the practical minimum for headlines/descriptions/images is a real
finding, not just an RSA-specific rule) — see `ad-copy-principles` for the
underlying quality bar to apply to headline/description text.

### 2. Asset-group-level performance

Query `asset_group` with metrics segmented by date:
```
SELECT asset_group.name, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
FROM asset_group WHERE campaign.id = <pmax_campaign_id> AND segments.date DURING LAST_30_DAYS
```
Confirmed through the API: the asset-group metrics
segmentation by date, and the 8-action conversion restriction segmented by
`conversion_action_name` at the asset-group level, both run without error.
Rank asset groups by conversions and by cost-per-conversion. This is the one
PMax cut that's fully API-accessible and directly comparable to Search
campaign performance — use it to answer "which creative concept is actually
working," even without a channel breakdown.

**Restrict conversions to the 8 actions in `conversion-measurement`** the
same way every other skill does — pull the conversions segment separately if
needed, same two-query rule.

### 3. Brand-exclusion check

Query `campaign_criterion` for the PMax campaign, looking for a negative
keyword list or `brand_list` criterion:
```
SELECT campaign_criterion.negative, campaign_criterion.keyword.text, campaign_criterion.brand_list.shared_set
FROM campaign_criterion WHERE campaign.id = <pmax_campaign_id>
```
If no negative/brand_list criterion is present, **PMax has no brand
exclusion configured** — meaning it's structurally free to bid on and win
branded queries that `campaign-guardrails` says Brand Core should own
exclusively. This is a binary structural check (list attached: yes/no), not
proof that scavenging is actually happening — confirming actual query-level
overlap requires the UI Insights page (see step 4), but a missing exclusion
list on its own is worth flagging as a standing risk.

### 4. What this skill cannot check (UI-only, not API-accessible)

Be explicit about these limits in the output rather than guessing:
- **Channel-level breakdown** (Search/Display/YouTube/Gmail/Maps split within
  PMax) — `segments.ad_network_type` returns `MIXED` for PMax campaigns; there's no queryable per-channel split via GAQL. Only available in the
  Ads UI "Insights" page.
- **Search-term/category insights** — `search_term_view` returns no rows for
  PMax campaigns (confirmed). PMax search-category data is UI-only.
- Recommend the user periodically pull both manually from the UI Insights
  page and share them if a deeper brand-scavenging or channel-mix read is
  needed — this skill's job is to flag the structural risk and the
  performance data it *can* reach, not to fabricate the rest.

### 5. Present the audit

1. **Asset group coverage table**: group name, asset counts by type, gaps flagged
2. **Asset group performance ranking**: conversions, cost/conversion, share of PMax spend
3. **Brand-exclusion status**: attached or not, with the standing-risk framing from step 3
4. **What wasn't checked**: the UI-only gap from step 4, stated plainly so the user knows what this audit does and doesn't cover

Tier 1 before presenting: cost-per-conversion and ranking numbers in the
tables recomputed from the raw files. Persist to
`OUTPUTS_DIR/pmax-audit-YYYY-MM-DD.md` with the verification footer.
