---
name: performance-analysis
description: "Deep-dive performance analysis for Google Ads campaigns, ad groups, or keywords. Breaks down metrics by segment (device, time, audience, geo) to find what's driving changes. Use when the user says 'why did CPA spike', 'performance deep dive', 'what's driving the change', 'segment analysis', 'drill into performance', or needs to understand the root cause of a metric shift."
---

# Performance Analysis

Deep-dive into Google Ads performance to understand what's driving metric changes. Goes beyond surface-level numbers to segment and isolate root causes.

## Prerequisites

Also load `campaign-guardrails` and `evidence-links`: guardrails constrain what a diagnosis may recommend;
`evidence-links` requires every object-level claim to carry the object's
name **and** numeric ID, plus a deep link where one exists.

Load `verification-protocol` (preflight, raw-pull persistence, verification
tiers — this skill uses the blind-second-diagnosis variant of Tier 2),
`account-conventions`, and `conversion-measurement` (the 8 actions any
CPA/ROAS decomposition in this skill must be computed from). Run the
protocol's preflight before step 2; save every pull to `OUTPUTS_DIR/raw/`.

## Steps

### 1. Identify the scope

Clarify with the user:
- Which campaigns/ad groups/keywords?
- What time period? (default: last 30 days vs. prior 30 days)
- What metric is the concern? (CPA, conversion volume, ROAS, impression share)

### 2. Pull segmented data

Query via GAQL with segments:
- **By device**: Desktop, mobile, tablet
- **By day of week**: Identify day-level patterns
- **By hour of day**: If available, find time-based patterns
- **By network**: Search, search partners, display
- **By top vs. other**: Ad position impact

### 3. Decompose the change

For the flagged metric, break it down:

**If CPA increased:**
- Did CPC increase? (auction pressure)
- Did conversion rate decrease? (landing page, audience quality, seasonality)
- Did click volume change? (budget, impression share shift)
- Is it concentrated in specific campaigns/ad groups or broad?

**If conversion volume dropped:**
- Did impressions drop? (budget, impression share)
- Did CTR drop? (ad fatigue, competition)
- Did conversion rate drop? (landing page, offer, audience)
- Is there a device-specific shift?

**If ROAS declined:**
- Is it a conversion value issue or a volume issue?
- Did the mix shift toward lower-value conversions?
- Are specific campaigns dragging down the average?

### 4. Isolate the driver

Find the single biggest contributor to the change. Usually one of:
- A specific campaign that shifted dramatically
- A device type that diverged
- A day-of-week pattern that changed
- A new competitor entering the auction
- A landing page or offer change

### 5. Verify: blind second diagnosis

Per `verification-protocol`: Tier 1 first (recompute every decomposition
number from the raw files — a CPA decomposition that doesn't arithmetically
reconcile with the raw pull is wrong by definition). Then invoke
**`google-ads-blind-diagnostician`** (the blind-second-diagnosis variant of
Tier 2): it gets only the raw files and reference skills (not your draft),
isolates the driver independently, and the two conclusions are diffed.
Agreement → confidence; divergence → present both reads side-by-side with
evidence, never a merged false consensus.

### 6. Present findings

Structure as:
1. **The headline**: One sentence on what happened and why
2. **The evidence**: 2-3 data points that prove it
3. **The recommendation**: What to do about it
4. **Supporting data**: Segmented tables for the user to review

Keep the narrative tight. Lead with the answer, then show the work.

Persist to `OUTPUTS_DIR/performance-analysis-YYYY-MM-DD.md` with the
verification footer from `verification-protocol`.
