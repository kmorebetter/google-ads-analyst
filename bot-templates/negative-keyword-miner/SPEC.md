# Template 1 (ship first): Negative Keyword Miner

## Promise
Upload your Google Ads search terms CSV, get a vetted negative keyword list
and the reason for every term. No API, no developer token.

## Who it is for
Small business owners and freelance PPC managers who want waste cut without
learning GAQL or waiting for API approval.

## Contents
- Plugin: Google Ads Analyst
- Skills used: `getting-started` (template-only, below), `account-setup`
  (short mode: brand words, competitor words, four thresholds, currency),
  `csv-import`, `search-term-methodology`
- Tool: `tools/csv_negatives.py`
- Memories:
  - "This bot is read-only. It never changes the Google Ads account."
  - "Thresholds are the owner's. Never invent one. Ask."
  - "Every negative's reason is its numbers, never how the words look."
  - "Brand terms and Added keywords are never negatives."
- Routine (optional): first Monday of the month, remind the user to upload a
  fresh 30-day and 90-day export.

## getting-started skill (template-only)
1. Greet, say what the bot does in two sentences.
2. Run `account-setup` in short mode.
3. Show the export steps from SETUP.md Part 1, Step 3.
4. On upload: run `csv-import`, send summary.md, attach negatives.csv and
   decisions.csv.
5. Remind about the 90-day check and HOLD rows.

## Flow
upload 30-day CSV -> NEGATE / CUT / HOLD / KEEP table -> user uploads 90-day
CSV -> rerun with any term that converted in 90 days moved to protect list ->
final negatives.csv -> user imports in Google Ads Editor.

## Status: built and evaluated
- Engine: `tools/csv_negatives.py` with `--clearance-export` (90-day
  cross-check, CPA-aware), `--competitor-token`, brand misspelling guard,
  one-word head-term REVIEW.
- Template files: `template.json`, `skills/getting-started/SKILL.md`.
- Evals: `evals/run_evals.py`, negatives section (see evals/README.md).
- Still open: listing screenshots and price; one known miss (see evals).

## Success check
A new user goes from install to a negatives.csv in under 15 minutes with no
help.
