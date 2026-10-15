---
name: outcomes-review
description: "Close the recommendations loop: reconcile every open row in recommendations-log.csv against reality (change_event evidence and current-state pulls), auto-flip detected applications, measure whether applied changes moved the metric they targeted in matched mature windows, surface long-open rows, and propose threshold tuning against tools/account-config.json. Also owns the target-change drift verdict. Use when the user says 'outcomes review', 'did our recommendations work', 'reconcile the log', 'what happened to the recommendations', 'close the loop', 'tune the thresholds', or at the start of any run-review session."
---

# Outcomes Review

The pack makes recommendations; this skill finds out what happened to them.
Three jobs: **reconcile** (which recommendations were actually applied),
**measure** (did applied changes move the metric they targeted), **tune**
(are the thresholds in `tools/account-config.json` earning their keep).
Runs **first in every `run-review` session**, and standalone on request.
Tiers: **1 + 2**. VF-rule rationale lives in `docs/VERIFICATION-RULES.md`.

## Prerequisites

Load `verification-protocol`, `account-conventions`, `conversion-measurement`
(conversion lag drives the maturity rules below), `campaign-guardrails`
(rule 5's floor, rule 7's learning windows) and `STANDING-ITEMS.md`.

## Policy — what this skill may and may not decide

- **Evidence flips status; judgment does not.** A row flips `open` →
  `applied` only when the tool's detection pass shows the metric's current
  value moved from `before_value` in the recommended direction, with the
  evidence recorded. Ambiguity is `needs_review`, presented to the user,
  never auto-flipped. `skipped` is always the user's call.
- **The account stays read-only.** This skill writes to the local log and
  proposes config changes; it never touches the account, and a threshold
  proposal is a recommendation for the user to approve, never a silent edit
  to `account-config.json`.
- **Verdicts are conservative.** Below the conversion floor, inside a
  learning window, or missing the needed pull → `insufficient_data`, stated
  with what would resolve it. A confounded verdict (another change on the
  same campaign inside the measurement window) says so in the verdict
  itself. Never present a confounded delta as a clean result.

## Steps

### 1. Preflight and pulls

Protocol preflight (or reuse the session's). Required pulls, reused same-day
when the session already made them: `campaigns-current.json` (current
budgets/targets — the detection comparator) and `change-events-14d.json`
(dated evidence; its 14-day reach is why reconciliation must run at least
fortnightly — evidence older than that survives only as a value diff with
an "on-or-before" date). For the outcome pass, the tool reads four
matched-window pulls from the raw dir — `outcome-cost-pre14d.json`,
`outcome-cost-post14d.json`, `outcome-conversions-pre14d.json`,
`outcome-conversions-post14d.json` (cost: `campaign.id` + cost micros;
conversions: the 8-action segment shape; windows declared in
`_manifest.json`) — and names whichever is missing in its
`insufficient_data` lines: make those pulls and re-run rather than
accepting the gap when the data is one query away.

### 2. Reconcile — run the tool, review, then apply

```
python3 tools/reconcile_recommendations.py <OUTPUTS_DIR> --raw-dir <raw_dir>
```

Report-only first. Review its dispositions: proposed flips (check the
evidence line on each), `needs_review` rows (resolve with the user or leave
open, never guess), long-open rows (≥14 days — surface these as their own
signal: a recommendation nobody applied or declined is process news). Then
re-run with `--apply`. The tool appends the outcome columns to the schema
on first contact, never overwrites a filled cell, and backfills
`after_value`/`after_date` per the canonical spec — after this skill
exists, it owns that job.

### 3. Measure outcomes

For `applied` rows at least 14 days mature: the targeted metric in matched
windows before and after the applied date, mature on both sides per
`conversion-measurement`, conversion floor per config. Verdicts:
`improved` / `no_change` / `worsened` (+ delta and window), `confounded:`
prefix when another change on the same campaign contaminates the window,
`insufficient_data` otherwise. Every verdict is a claim: recompute its
numbers from the raw pulls (no facts builder exists here yet — Tier 1 is
Bash recomputation), and state windows and basis (8-action) in the report.

**Standing deliverable: target-change verdicts.** For each cluster that was
budget-limited and beating its target when its target was tightened: has
efficiency drifted toward the stated target since? The earliest clean read is
the end of the learning window (14 days Search, 28 days PMax) after the change.
Until the windows clear, report "still in learning, verdict due <date>" rather
than a premature read. Present it prominently when it lands.

### 4. Tune thresholds (monthly, or on request)

Inputs: this log's verdicts, the flags raised in persisted reports, and the
weekly integrity-check shares from report headers. Three tests:

- **Noise**: a threshold whose flags repeatedly end `no_change` or never
  become recommendations is too tight — propose loosening, with the flag
  history cited.
- **Blind spots**: harm that arrived with no prior flag. The known shape is
  slow drift — 8%/week for four weeks never crosses a 15% WoW gate —
  propose a cumulative-vs-baseline threshold if one shows up.
- **The integrity band**: `conversion-measurement`'s integrity band
  (`thresholds.conversion_integrity_band_pct`) is explicitly tunable as weeks accumulate — once 8+ weekly
  observations exist, propose narrowing to observed mean ± 3σ.

Each proposal is one row in `recommendations-log.csv` (`metric` = the
config key, `skill` = `outcomes-review`) and a change the user approves;
on approval, edit `account-config.json` and note the change and date in
`MAINTENANCE.md`.

### 5. Verify, then present

Tier 1: every number recomputed; every flip's evidence line checked against
the named source file. Tier 2 (`google-ads-verifier`): judges detection
attribution (is each flip's evidence actually about that row's metric and
campaign — shared pools are the trap), verdict honesty (confounds and
floors respected, no clean-looking contaminated delta), and that no
threshold proposal rests on a single observation.

Present: **the loop status** first (N open / applied / skipped, oldest open,
flips this run), then **verdicts** (with the target-change line), then **tuning
proposals**, then the long-open list. Persist to
`OUTPUTS_DIR/outcomes-review-YYYY-MM-DD.md` with the verification footer.
If the loop's health changed (e.g. the all-rows-stuck-open era ending is
itself a standing-item update), write it to
`OUTPUTS_DIR/maintenance-YYYY-MM-DD.md` — never edit `MAINTENANCE.md` or
`STANDING-ITEMS.md` from a run; see `verification-protocol` → Maintenance
log for why.
