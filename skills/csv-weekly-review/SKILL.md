---
name: csv-weekly-review
description: "Write the weekly Google Ads review from two Campaigns exports (last week and this week, same length) plus an optional month-to-date export for budget pacing. Flags pacing against the owner's stated budget, real spend and conversion moves (with the 5-conversion floor), new or vanished campaigns, and a possible tracking break. Treats a planned spend change as the plan, not a finding. Use for 'weekly review', 'how did ads do this week', or the Monday routine."
---

# CSV Weekly Review

1. Ask for two Campaigns exports with the same number of days (for example
   Monday to Sunday last week, and the week before), with Campaign ID, Campaign
   type, Clicks, Cost and Conversions. For pacing, also a month-to-date export.
2. Read the monthly budget from `tools/account-config.json` (the owner's stated
   figure). If the month has no stated budget, skip pacing and say why.
3. Run:

       python3 tools/csv_weekly.py --this-week tw.csv --last-week lw.csv \
         --mtd mtd.csv --month-budget <B> --as-of <YYYY-MM-DD> \
         [--planned-change-pct <P>] --out outputs/weekly/<date>/

4. Send `weekly.md` in chat. If a TRACKING_CHECK finding appears, lead with it.
