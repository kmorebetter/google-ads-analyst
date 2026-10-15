# Evals for the three bot templates

    python3 evals/run_evals.py            # prints PASS/FAIL per threshold
    python3 evals/run_evals.py --write --label after   # saves results/after.json

`tools/test_csv_templates.py` runs the same suite inside the unit tests, so a
regression fails `python3 -m unittest`.

## What is tested

| Area | Scenarios |
| :- | :- |
| Negative Keyword Miner | N1 messy small export, N2 brand / misspelled brand / look-alikes / competitor terms, N3 ambiguous intent + 90-day clearance, N4 UTF-16 Excel export with non-English and duplicate terms, N5 3,000-row account |
| Google Ads Auditor | A1 small account (company name in every campaign, paused disapproved ad, removed callout), A2 messy UTF-16 export with PMax, Demand Gen and policy-limited ads, A3 60-campaign account |
| Weekly Ads Reviewer | W1 typical week with pacing, W2 tracking break, W3 planned 20% step down, W4 device-segmented rows and a renamed campaign, W5 80-campaign account |
| Bad inputs | 14 cases: wrong report, empty, .xlsx bytes, header only, missing thresholds, short clearance file, missing file, bad number, same week twice, 30 vs 7 days, pacing without budget, bad date |
| Hold-out | H1 hard search terms, written after the main fixes (reported, not gating) |

Answer keys are written as a senior PPC reviewer would grade the work (see
`scenarios.py`). Rows an expert could defend either way are marked EITHER and
excluded from scoring.

## Thresholds

Negatives precision >= 0.95 and recall >= 0.90. Audit and weekly findings
precision >= 0.90, recall >= 0.90, usefulness >= 0.95 (each finding names a
place, shows evidence and starts its action with a verb). Clarity: report prose
at or under grade 9.0 (Flesch-Kincaid, tables and names stripped), zero em
dashes. Every bad input refused with a plain message and no traceback. Every
valid scenario completes. Large accounts under 10 seconds.

## Scores

| Metric | Before fixes | After fixes |
| :- | -: | -: |
| Negatives precision | 1.000 (N2 could not run) | 1.000 |
| Negatives recall | 0.992 | 1.000 |
| Audit precision | 0.929 | 1.000 |
| Audit recall | 0.265 | 1.000 |
| Audit usefulness | 0.944 | 1.000 |
| Weekly precision | 1.000 (W3 could not run) | 1.000 |
| Weekly recall | 1.000 | 1.000 |
| Weekly usefulness | 1.000 | 1.000 |
| Clarity grade (neg / audit / weekly) | 3.6 / 3.1 / 3.8 | 4.1 / 3.2 / 3.8 |
| Em dashes | 0 | 0 |
| Bad inputs handled | 0.786 | 1.000 |
| Valid scenarios completed | 0.846 | 1.000 |
| Hold-out negatives precision | 0.50 | 0.75 |

Fixes made: competitor terms in conquest campaigns go to REVIEW (navigational
ones stay negatives); sound-alike brand guard; the 90-day clearance only
rescues a term that converts at an acceptable cost; one-word head terms go to
REVIEW; the audit finds brand campaigns by ID or the word brand, not the
company name; paused ads are not policy findings; actions start with a verb;
the weekly review takes a planned spend change; missing files, .xlsx files and
the wrong report type are refused cleanly.

## Where it still falls short

- Hold-out: `akmi invoice` (a two-letter brand misspelling) is still negated.
  The sound-alike guard catches one-letter slips only. Add misspellings to
  `--brand-token` during setup.
- The hold-out was used once to motivate the one-word fix, so its 0.75 is
  slightly optimistic.
- The answer keys were written by the same agent that built the tools. Have
  an independent PPC expert review `scenarios.py` before claiming these numbers
  publicly.
- The clarity score is a formula on the prose. It does not judge tone, and
  campaign names are stripped before scoring.
