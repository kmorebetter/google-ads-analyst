# Template 3: Weekly Google Ads Reviewer

## Promise
Every Monday morning: what changed, whether you are on budget, which
campaigns moved, and whether last week's recommendations worked.

## Who it is for
Owners and marketers who manage the account weekly and want a reliable
first read.

## Contents
- Skills: `account-setup`, `weekly-review`, `outcomes-review`,
  `budget-optimize` (pacing only), `verification-protocol`,
  `conversion-measurement`, `campaign-guardrails`, `notion-publishing`
  (optional)
- Routine: Mondays 7:00 local. Pull, build facts, write report, verify,
  deliver by the user's chosen channel (chat, email, Slack or Notion).
- Memories: read-only rule; budget is read never derived; planned step downs
  are not findings; learning windows 14/28 days.

## Data
API path required. The weekly pull is about a dozen GAQL queries.

## Status: built and evaluated (CSV path)
- Engine: `tools/csv_weekly.py`; skill `csv-weekly-review`; template files here.
- Evals: weekly section of `evals/run_evals.py`.
- Still open: unattended Monday pulls need the API path; delivery channel is
  asked in getting-started.
