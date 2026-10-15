---
name: run-review
description: "Orchestrate a multi-skill Google Ads review session: one preflight, one shared pull set, a triage read that proposes which skills to run and why, ordered execution with outputs feeding forward, one composite Tier 2 + Tier 3 verification pass over the merged findings, and a single publishable report (plus the Executive Brief per notion-publishing). Use when the user says 'run a report', 'full review', 'run the toolkit', 'monthly review', 'what should we run', 'run everything', or asks for a report to publish without naming a single skill."
---

# Run Review

Run several skills as one session instead of N separate ones: one preflight,
shared pulls, an explicit run plan the user approves before anything heavy
executes, and one composite verification pass at the end. This replaces the
ad-hoc multi-agent Executive Brief orchestration — its recorded failure mode
was broadcasting one unverified fact into seven parallel briefs.

## Prerequisites

Load `verification-protocol` and `STANDING-ITEMS.md` first. Each selected
skill loads its own prerequisites when it runs; do not preload them all.

## The skill registry (what triage selects from)

| Skill | Answers | Cadence default | Triage trigger |
| :- | :- | :- | :- |
| `outcomes-review` | did past recommendations get applied, and did they work | every session, first | always (cheap; skip only on "quick") |
| `weekly-review` | are we on budget, is the money working | weekly | always in a weekly/monthly session |
| `budget-optimize` | where should the money go | on signal | lost-IS-budget over the config gate, pacing outside the band, or an idle-allocation question |
| `mine-search-terms` | what to negate, what overlaps | monthly, or on signal | spend concentrated on zero-conversion terms, or the owner supplies/updates thresholds |
| `investigate-campaign` | what is wrong with campaign X | on signal | a specific campaign flagged by weekly-review or named by the user |
| `performance-analysis` | what is driving a metric shift | on signal | an account-level metric moved with no obvious single-campaign driver |
| `audit-ad-copy` | which headline to swap | monthly | 30+ days since last run, or copy/QS signals |
| `audit-extensions` | which extension to add where | monthly | 30+ days since last run |
| `pmax-audit` | PMax health and brand-exclusion risk | monthly | 30+ days since last run, or PMax anomalies |

Last-run dates come from the dated reports in `OUTPUTS_DIR/` — check them,
never guess.

## Steps

### 1. Preflight, triage pull, and state read

Run the protocol's preflight once. Then the triage read, which is cheap on
purpose: `weekly-review`'s seven pulls (they are the shared pull set — every
selected skill reuses them per the same-day-reuse rule), the last-run date
per skill from `OUTPUTS_DIR/`, and the recommendations log (open-row count
and ages). Build the run's facts file before reading signals from it.

**Cadence guard.** If the previous comparable run is fewer than 5 days
old, the anchored Mon-Sun windows will reproduce the same weeks — the WoW
read is the previous run's, not a new observation. Say so in the plan and
present the session as an intraweek refresh (rolling `LAST_7_DAYS`,
labelled as such), not a new week-over-week read.

### 2. Propose the run plan, then ask

Present a table — skill, why (the specific config-threshold signal or
cadence reason, cited to a fact id), depth (full or quick), what it feeds —
and the estimated weight (pull count, verifier agents). **Ask the user to
confirm or adjust before executing** anything beyond the triage read. The
user saying "quick" drops every selected skill to Tier 1 per the protocol,
and the footer says so.

### 3. Execute in order

Fixed ordering rules, then signal-driven skills in any sensible order:

1. `outcomes-review` first — settle what happened to past recommendations
   before making new ones.
2. `weekly-review` before any skill that consumes its flags.
3. `mine-search-terms` treats any term with a priority conversion in 90 days
   as do-not-negate input.

Each skill runs its own steps and Tier 1 against the shared raw directory,
and persists its own dated report — but **holds its Tier 2/3 agent
dispatches**: composite verification (step 5) covers them in one pass.
Solo runs of a skill outside this orchestrator keep their own tiers; the
substitution happens only here, where one pass sees everything.

### 4. Synthesize

One composite report: the session's answer first (pacing + the one or two
findings that matter), then per-skill sections that reference rather than
repeat each other. Cross-skill work the individual skills cannot do — and
the reason this step exists:

- **Consistency**: the same figure quoted in two sections traces to the
  same fact id. Two skills quoting different numbers for one quantity is a
  finding to resolve, not a formatting issue.
- **Contradictions**: recommendations that pull against each other (a
  budget increase on a campaign whose spend another section shows is partly
  waste) are resolved into a sequenced recommendation ("negate first,
  re-evaluate budget in 2 weeks"), never shipped as independent advice.
- **Deduplication**: one recommendation per underlying change; the rec-log
  gets one row, attributed to the skill whose evidence carries it.

### 5. Composite verification — one Tier 2, one Tier 3

After every section's mechanical checks are clean: **one**
`google-ads-verifier` dispatch over the composite report, all facts files,
and the reference skills — its usual checklist plus cross-section
consistency. Then **one** `google-ads-refuter` dispatch over the merged
recommendation list (this replaces the per-skill Tier 3 of every
money-moving skill that ran). Both agents get told which skills
contributed. Every discrepancy is fixed or surfaced; contested
recommendations show both views. **Deliverable files persisted by the
sub-skills (the negatives CSV, change lists) are drafts until this pass
completes** — re-emit any file whose rows the composite refuter killed or
contested, so no stale deliverable outlives its verification. The footer lists, per contributing skill,
the tiers that effectively covered it.

### 6. Persist and publish

Persist the composite to `OUTPUTS_DIR/toolkit-run-YYYY-MM-DD.md` with one
verification footer (the per-skill dated reports stand alongside it). When
more than one skill ran end-to-end against live data, produce the Executive
Brief per `notion-publishing` (it builds on that session's weekly-review
page without restating it); publish only after the composite verification
passed — the Brief inherits the session's facts, and an unverified number
must never reach it. Log recommendations per `budget-optimize` step 8, one
row each after deduplication. Close by updating `MAINTENANCE.md` (and
`STANDING-ITEMS.md` if a standing item changed) once for the whole session.
