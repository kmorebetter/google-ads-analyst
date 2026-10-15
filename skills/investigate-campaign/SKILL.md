---
name: investigate-campaign
description: "Investigate a specific Google Ads campaign to understand what's happening and why. Pulls all relevant data — keywords, search terms, ads, bids, budgets, audience, schedule — and synthesizes into a clear diagnostic. Use when the user says 'investigate this campaign', 'what's going on with [campaign]', 'dig into [campaign]', 'campaign deep dive', 'why is this campaign underperforming', or points to a specific campaign that needs attention."
---

# Investigate Campaign

Run a full diagnostic on a specific campaign. Pull everything, connect the dots, and give the user a clear picture of what's happening and what to do about it.

## Prerequisites

Also load `evidence-links`: every object-level claim in
this skill's output carries the object's name **and** numeric ID, plus a deep
link where one exists.

Load `verification-protocol` (preflight, raw-pull persistence, verification
tiers — this skill uses the blind-second-diagnosis variant of Tier 2),
`account-conventions`, `search-term-methodology`, `performance-analysis`,
`conversion-measurement` (the 8 actions CPA/ROAS must be computed from), and
`campaign-guardrails` (don't diagnose a TOF campaign as broken on BOFU
CPA/ROAS; check learning-phase status before recommending a change). Run the
protocol's preflight before step 2; save every pull to `OUTPUTS_DIR/raw/`.

## Steps

### 1. Identify the campaign

Get the campaign name or ID from the user. If they describe it loosely ("the brand campaign" or "the one that's tanking"), use account-conventions to match it.

### 2. Pull the full picture

Query via GAQL for the target campaign (last 30 days vs. prior 30 days):

**Campaign level:**
- Budget, bid strategy, status, networks
- Cost, clicks, impressions, conversions, CPA, ROAS
- Impression share, lost IS (budget), lost IS (rank)

**Keyword level:**
- All active keywords with performance metrics
- Quality scores (keyword, landing page, ad relevance, expected CTR)
- Bid strategy signals

**Search term level:**
- Top 100 search terms by spend
- Match type distribution
- Search terms with spend but zero conversions

**Ad level:**
- All active RSAs with performance and ad strength
- Asset-level performance (winning/losing headlines)

**Audience and schedule:**
- Device performance split
- Day-of-week and hour-of-day patterns
- Audience segment performance (if applicable)

### 3. Change history — causal read

Query the `change_event` resource for this campaign:
```
SELECT change_event.change_date_time, change_event.change_resource_type,
change_event.resource_change_operation, change_event.user_email
FROM change_event WHERE change_event.change_date_time DURING LAST_14_DAYS
AND change_event.campaign = '<campaign_resource_name>'
```
**Confirmed limit: query with `LAST_30_DAYS` fails (`INVALID_ARGUMENT`);
`LAST_14_DAYS` works**. If the diagnosis window is longer than 14 days,
chain multiple 14-day (or narrower) windows rather than assuming a single
30-day pull will work.

Line up change dates against the metric-shift timeline from step 2 — did a
specific bid, budget, or target change precede the shift being diagnosed? If
a change lines up:
- That's likely the driver, or a contributing one — say so directly, and
  note whether the campaign is now in (or should be protected from
  re-entering) the learning phase per `campaign-guardrails`.
- If the recommendation in step 4 would be to revert or further adjust that
  same lever, flag the learning-phase risk explicitly before recommending
  another change on top of a recent one.

If no change lines up with the shift, that's also worth stating — it rules
out "someone changed something" and points the diagnosis toward external
factors (auction competition, seasonality, tracking).

### 4. Diagnose

Synthesize everything into a diagnostic:

**The State**: Is this campaign healthy, struggling, or broken?

**The Driver**: What's the single biggest factor affecting performance? Common drivers:
- Budget constraint choking volume
- Poor quality scores inflating CPCs
- Irrelevant search terms burning budget
- Ad copy fatigue (declining CTR)
- Landing page conversion rate drop
- Auction competition increase
- Bid strategy misconfiguration
- **A specific account change** (from step 3) that lines up with the shift

**The Evidence**: 3-5 specific data points that prove the diagnosis — include the change-history match from step 3 when one exists, it's usually the most convincing evidence available.

### 5. Recommend actions

Provide 3-5 specific, prioritized actions:
- What to change
- Why it will help
- Expected impact (directional)
- Which skill to run next if applicable (e.g., "Run mine-search-terms to clean up the 15 irrelevant search terms burning $X/day")
- If the driver was a recent change (step 3): recommend reverting it, or explicitly recommend against another change until the learning phase clears, rather than layering a new fix on top

### 6. Verify: blind second diagnosis

Per `verification-protocol`, after Tier 1 (recompute every cited number from
the raw files), invoke **`google-ads-blind-diagnostician`** (the
blind-second-diagnosis variant of Tier 2), giving it **only the raw pull
files and the reference skills — not your draft diagnosis**. It diagnoses
the campaign independently: state, single biggest driver, 3-5 evidence
points.

- **Diagnoses agree** → present with confidence; note the independent
  confirmation in the verification footer.
- **Diagnoses diverge** → present **both** reads side-by-side with the
  evidence for each, and say which you find more likely and why. Never
  merge a divergent diagnosis into false consensus — a genuine disagreement
  between two independent reads of the same data is exactly what the user
  needs to see.

### 7. Present the investigation

Lead with the diagnosis (one paragraph), then supporting evidence, then recommendations. Include the detailed data tables below for the user to explore.

Keep it narrative-driven, not just data tables. The user wants to understand what's happening, not parse a spreadsheet.

Persist to `OUTPUTS_DIR/investigate-campaign-<campaign-slug>-YYYY-MM-DD.md`
with the verification footer.

### 8. Log the recommendation

Append the recommendation(s) from step 5 to
`OUTPUTS_DIR/recommendations-log.csv` — see the shared logging format
in `budget-optimize` step 8.

Note on Tier 3: this skill doesn't run the adversarial refuter itself —
its recommendations route execution to `budget-optimize` /
`mine-search-terms`, which refute at their own Tier 3 before anything is
handed to the user to apply. That's deliberate, not an omission.
