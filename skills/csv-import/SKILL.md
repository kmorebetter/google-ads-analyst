---
name: csv-import
description: "Work from Google Ads report CSVs the user downloads and uploads, with no API, developer token, or CLI. Explains exactly which report to export, checks the file, and runs the CSV tools (negative keyword candidates today) with the owner's thresholds. Use when the user uploads a Google Ads CSV, says they have no API access, or asks for negatives, a search term review, or a quick audit from an export."
---

# CSV Import (no-API path)

The fastest way to use this plugin. The user exports a report from the Google
Ads website and uploads it in chat. Nothing connects to their account and
nothing is changed in it.

## Which export to ask for

| Task | Report in Google Ads | Settings |
| :- | :- | :- |
| Negative keywords, search term review | Insights and reports > Search terms | Last 30 days (also 90 days for the safety check), all campaigns, columns: Search term, Match type, Added/Excluded, Campaign, Ad group, Clicks, Impr., Cost, Conversions |
| Campaign overview | Campaigns | Last 30 days, columns: Campaign, Campaign ID, Budget, Cost, Conversions, Conv. value |
| Conversion actions (for account-setup) | Goals > Summary | Name, Action optimisation, Value |

Download as **CSV** (or "Excel CSV"; both read fine).

## Steps

1. Confirm the file is a Search terms export (the tool refuses anything else
   and says why).
2. Make sure the four thresholds exist (from `account-setup` or ask now). Never
   pick them yourself.
3. Run:

       python3 tools/csv_negatives.py <uploaded file> \
         --min-spend <S> --min-clicks <C> --target-cpa <T> --target-cpa-multiple <M> \
         --brand-token "<brand word>" [--brand-token ...] [--protect-term "<term>"] \
         --out outputs/csv/<date>/

4. Send the user `summary.md` in chat and attach `negatives.csv` and
   `decisions.csv`.
5. Say plainly: the export covers one date range only; re-check with a 90-day
   export before uploading negatives. HOLD rows are not negatives.

## Other CSV tools

- `csv-audit` (Campaigns, Ads, Assets, Search terms): one-time audit.
- `csv-weekly-review` (two weekly Campaigns exports, month to date): weekly review.
- Always pass the 90-day file as `--clearance-export` before calling a negatives list final.
- Competitor names go in `--competitor-token`; brand names and misspellings in `--brand-token`.

## What CSV mode cannot do

- No change history, keyword-level joins, or per-headline asset data. Those
  need the API path in SETUP.md.
- The facts-file builders (`build_*_facts.py`) expect API pulls.
