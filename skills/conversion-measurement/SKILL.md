---
name: conversion-measurement
description: "Reference skill defining what counts as a conversion for CPA/ROAS in YOUR account: the conversion actions listed in the account profile (and only those), whether their values are real revenue or assumed constants, the labelling rule every CPA/ROAS figure must follow, conversion lag, and the data-integrity cross-check against an outside source of truth. Load this before computing or reporting any CPA or ROAS number."
---

# Conversion Measurement

Every skill that computes or reports CPA or ROAS, or reasons about conversion
counts, loads this skill first. The list of conversion actions lives in
`tools/account-config.json` (`conversion_actions`) and in
`google-ads/account-profile.md`, both written by `account-setup`. **Do not keep
a second copy of the list anywhere else.**

If the config still holds the SYNTHETIC example actions (`Basic Trial Signup`
and friends), stop and run `account-setup` first.

## Only the listed actions drive CPA/ROAS

- CPA and ROAS are computed on the listed actions only, filtered with
  `segments.conversion_action_name` (or `conversion_action` resource names).
- The builders refuse a pull that contains an action outside the list. That is
  deliberate: a new action appearing silently changes every CPA.
- `priority` actions (optional) are the highest-value subset, for example the
  top plan tier. A search term with any priority conversion in the last 90
  days is never a negative candidate.

## Our CPA/ROAS is not the basis Google bids on

Smart Bidding optimises toward the campaign's conversion goal set, which is
often broader than the listed actions. So a campaign can look poor on our
basis while it is hitting Google's target, and the reverse. Every CPA/ROAS
sentence names its basis ("on the N listed actions"), and any recommendation
to change a bid target says which basis it used.

## Assumed values vs real revenue

`account-setup` records whether each action's value is real revenue (passed
per conversion) or an assumed constant (a fixed default value). When values are
assumed:

- ROAS is a plan-mix signal, not a revenue return. Label it every time:
  "ROAS on assumed values".
- Put a real-money anchor (CAC from `budget-plan.md`) beside it.
- Re-pull `conversion_action.value_settings` each run and fail loudly if a
  default value changed.

### Labelling rule: the qualifier travels with the number

A CPA or ROAS figure never appears without its basis and its value type in the
same sentence or table header. A table column header counts.

## Conversion lag / data maturity

Recent days are incomplete. Do not judge the last 3 to 7 days (set the exact
lag in the profile) on conversions. Say "immature window" instead of calling a
drop.

## Data-integrity check: tracking drops look like performance dips

Before calling a conversion drop real, compare Google Ads conversion counts
with an outside source the owner trusts (product analytics, CRM, or billing),
named in the profile. The expected ratio band and week-over-week tolerance are
`thresholds.conversion_integrity_band_pct` and
`thresholds.conversion_integrity_wow_move_pp`. Outside the band, report a
possible tracking problem first and hold any performance conclusion. If no
outside source is set up, say the check was not run.
