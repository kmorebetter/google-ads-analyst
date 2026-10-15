---
name: account-setup
description: "Interview the user about their own Google Ads account and write the account profile every other skill depends on: customer ID, currency, campaign buckets (brand, non-brand, top-of-funnel, legacy) by ID, brand words, competitor words, the conversion actions that count for CPA/ROAS, whether conversion values are real or assumed, monthly budgets, thresholds, and the outside source used to sanity-check conversions. Use on first install, when the user says 'set up my account', 'configure google ads', 'start', or whenever another skill finds the SYNTHETIC example profile still in place. Also runs in update mode when the drift check reports new or renamed campaigns."
---

# Account Setup

Captures the buyer's own account facts once, so every other skill reads them
instead of guessing. Nothing here writes to Google Ads.

## Outputs

1. `tools/account-config.json`: machine-readable constants for the builders.
   The shipped file is a SYNTHETIC example (Acme Software, customer
   `1234567890`). Replace every value.
2. `google-ads/account-profile.md`: the same facts in plain words, plus naming
   patterns, ad group themes, shared budget clusters, and notes.
3. `budget-plan.md`: the owner's stated budgets, CAC table and budget bands, in
   the exact table shapes of the shipped example (`tools/test_account_config.py`
   checks the config and this file agree).
4. `STANDING-ITEMS.md`: a short list (max 50 lines) of open items every run
   reads first. Start it empty with a heading.
5. `account-links.md`: the `ocid` used for Google Ads deep links (optional;
   see `evidence-links`).

## How to run it

Ask in small groups, never one giant form. Accept "skip" for anything optional
and write `null` with a reason instead of inventing a value. Offer to read
answers from data when it exists: with API access, pull the campaign list and
conversion actions and ask the user to confirm; without it, ask the user to
upload a Campaigns report CSV and a Conversions (Goals > Summary) CSV and read
names and IDs from those.

### Group 1: the account

- Business name and what they sell (one line).
- Customer ID (10 digits, shown top-right in Google Ads as 123-456-7890;
  store without dashes).
- Account currency (store the code, for example USD). Note: the builders
  label money in the config currency field; if it is not CAD, set `fx_usd_to_cad`
  to `1` and tell the user the field names still say "cad" internally.
- Data access: CSV only, or API/CLI (see SETUP.md).

### Group 2: campaigns

- List enabled campaigns with IDs (from the pull or the Campaigns CSV).
- Ask which are **brand** (bidding on their own name), which is the **main
  brand campaign** (`brand_core_campaign_id`), which are **top-of-funnel**
  (Video, Demand Gen, Display awareness), and which are **legacy/one-off**.
- Brand words (`brand_tokens`): their name and common misspellings.
- Competitor words (`conquest_tokens`): competitor names they bid on, plus
  `conquest`, `competitor`.
- Naming patterns and shared budgets for the profile.

### Group 3: conversions

- Which conversion actions count for CPA/ROAS (exact names). The builders
  expect exactly eight in `conversion_actions.eight`; if the user has fewer,
  repeat-pad is NOT allowed. Instead list them all and tell the user the
  builder count check must be edited (open item; see README "Known limits").
- Optional `priority` actions (highest value; never negate a term that drove
  one in 90 days).
- Are values real revenue or assumed constants? If assumed, record each value.
- Conversion lag in days.
- Outside source for the integrity check (analytics, CRM, billing) or "none".

### Group 4: money and thresholds

- Monthly Google Ads budget for each month they know. Unknown months stay
  `null`; never derive one from a total.
- Planned month-over-month change, if any.
- A recent CAC and cost per trial or lead (real money), for the CAC table.
- Budget bands: how much more per month they believe is efficient, and where
  efficiency is known to drop. Unknown means no recommendation above current.
- Negative keyword thresholds (all four, no defaults): minimum spend, minimum
  clicks, target CPA, and the CPA multiple at which a converting term is cut.

## After writing

1. Run `python3 tools/test_account_config.py` and fix any mismatch with
   `budget-plan.md`.
2. Show the user a one-screen summary of what was saved and ask them to
   confirm.
3. Remove the word SYNTHETIC from the `_comment` block only once every value
   is theirs.

## Update mode

When the drift check (`account-conventions`) reports new, removed or renamed
campaigns, ask only about those, update both files, and add a dated line to
`google-ads/account-profile.md` under "Changes".
