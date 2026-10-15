---
name: account-conventions
description: "Reference skill that tells the other skills how to read YOUR Google Ads account structure: campaign naming pattern, which campaigns are brand, non-brand, top-of-funnel or legacy, ad group themes, shared budgets, and how bid strategies show up in names. It reads the account profile written by the account-setup skill. Load it before interpreting campaign names, bucketing performance, or mapping search terms to intent."
---

# Account Conventions

Reference skill loaded by every analysis skill (each skill's Prerequisites
block says who loads what). It does not hold any account facts itself. The
facts live in two files that the `account-setup` skill writes for the buyer:

- `google-ads/account-profile.md`: the human-readable profile (naming pattern,
  campaign categories, ad group themes, shared budgets, notes).
- `tools/account-config.json`: the machine-readable constants the Python
  builders use (customer ID, currency, campaign-ID buckets, brand tokens,
  conversion actions, budgets, thresholds).

**If either file still holds the shipped SYNTHETIC example (Acme Software,
customer `1234567890`), stop and run `account-setup` first.** Never analyse a
real account against example conventions.

For behaviour rules (what may scale, what is protected, what is TOF-only) see
`campaign-guardrails`. This skill is structure only.

## Bucket campaigns by ID, never by name

Campaign buckets (brand, top-of-funnel, legacy) are lists of campaign IDs in
`account-config.json`. A word like "Brand" in a name also matches
"Non-Brand" campaigns, so a name match misclassifies. Read the bucket from the
ID list. A campaign whose ID is in no bucket is non-brand by default, and a
campaign that is new since the profile was written is flagged, not guessed.

## Naming generations

Most accounts carry more than one naming convention at once (an old pattern,
a mid-period pattern, and the current one). The profile lists each pattern
with one example. When a name fits no pattern, flag it as one-off rather than
guessing intent from the words in it.

## Bid-strategy suffix decoder (generic)

Campaign names often carry the bid strategy as a suffix. Common ones:

| Suffix | Bidding strategy | Notes |
| :- | :- | :- |
| `tROAS` | `MAXIMIZE_CONVERSION_VALUE` / `TARGET_ROAS` | Check a target is actually set. The name is not proof. |
| `tCPA` | `MAXIMIZE_CONVERSIONS` with target CPA | Same check. |
| `MaxConv` | `MAXIMIZE_CONVERSIONS` | No target. |
| `Clicks` | `TARGET_SPEND` | Judge on spend to budget only, never on CPA/ROAS. |
| `IS` | `TARGET_IMPRESSION_SHARE` | Judge on impression share and spend, never on CPA/ROAS. |

Always confirm against `campaign.bidding_strategy_type` in the pull. The
profile can add account-specific suffixes.

## Shared budgets

Campaigns that share one `campaign_budget.resource_name` move together: a
budget change to one changes the pool for all. The profile lists each cluster.
Never recommend a budget change to one member without naming the others.

## Drift-check contract (consumed by `verification-protocol` preflight)

Each run compares the live list of enabled campaigns against the profile:

1. A campaign ID in the pull but not in the profile: report it as drift and
   treat it as unbucketed (non-brand) until the owner classifies it.
2. A campaign ID in the profile but not enabled in the pull: report it.
3. A bid strategy type that does not match the decoder for its name: report it.

Drift is reported in the run's preflight section. It never blocks a run, and
it is never silently fixed. The owner reruns `account-setup` (update mode) to
fold it in.

## Keeping this current

Rerun `account-setup` in update mode when campaigns are added, renamed, or
retired, or when the drift check reports anything.
