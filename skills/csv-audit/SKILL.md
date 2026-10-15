---
name: csv-audit
description: "Run a one-time Google Ads account audit from uploaded exports (Campaigns, Ads, Assets, and optionally Search terms with a 90-day file). Flags wasted spend, high cost per conversion, budget-limited campaigns, weak or disapproved ads, keyword insertion in competitor campaigns, and missing sitelinks, callouts and snippets, while respecting the guardrails (top-of-funnel and brand campaigns are not judged on CPA; under 5 conversions means no call). Use when the user asks for an audit, a health check, or a second opinion and uploads exports."
---

# CSV Audit

1. Ask for the exports (see `csv-import` for where each one is in Google Ads):
   Campaigns (with Campaign ID, Campaign type, Campaign status, Search lost IS
   (budget)), Ads, Assets, and optionally Search terms for 30 and 90 days.
2. Get the owner's target CPA (required; never invent it) and, for search
   terms, the four negative keyword thresholds.
3. Run:

       python3 tools/csv_audit.py --campaigns campaigns.csv --ads ads.csv --assets assets.csv \
         --search-terms st30.csv --clearance-export st90.csv \
         --target-cpa <T> --target-cpa-multiple <M> --min-spend <S> --min-clicks <C> \
         --out outputs/audit/<date>/

4. Send `audit.md` in chat. Attach `change-list.csv` and `findings.csv`.
5. Say what the audit could not check (listed at the end of audit.md).

Brand campaigns are found by Campaign ID from the account profile, or by the
word "brand" in the name. Never by the company name, because many accounts
put it in every campaign.
