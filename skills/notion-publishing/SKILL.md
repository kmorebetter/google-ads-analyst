---
name: notion-publishing
description: "Reference skill defining how this pack's outputs get published to this Notion 'Google Ads & Analytics — Reports Directory' page: the two page templates (weekly-review snapshot, full-toolkit-run Executive Brief), when each applies, the exact section order and Notion-markdown block patterns for each, and the required directory-index update. Load whenever asked to publish, post, or write up a report in Notion, or after any session where more than one skill ran end-to-end against live data."
---

# Notion Publishing

This pack has two Notion page templates, standardized from the two that
were already live in the account's Notion before this file existed — the
weekly-review snapshot page and the toolkit Executive Brief. Both are
derived from real published examples, not invented. Follow these
exactly rather than re-deriving the format each time — that's the point of
this file.

## Where things go

Every published page is a **child page** of the directory:
the reports directory page the user names during setup (record its URL in
`google-ads/account-profile.md` under "Notion reports directory"). If none is
recorded, ask once and save it.
After creating a page, **always** add one bullet for it under the matching
category section in the directory's own content (`Weekly Performance`,
`Budget Optimization`, `Keywords & Creative`, `Account Audits`, `Site
Analytics`, or `Cross-Functional Synthesis` for an Executive Brief) —
format: `- [Title](url) — one-line hook`, matching the existing bullets'
style. A page that exists only as a child page with no directory bullet is
an incomplete publish; the directory is what people actually browse.

## Which template applies

- **Weekly-review snapshot** — one per `weekly-review` run, every time,
  regardless of whether other skills ran in the same session. This is the
  routine, lightweight artifact.
- **Toolkit Findings & Executive Brief** — only after a session where
  **more than one skill ran end-to-end against live account data**, i.e. a
  full or near-full toolkit pass, not a single-skill run. It's a
  **companion to, not a replacement for**, that session's weekly-review
  page — state that explicitly in the brief's header and don't restate the
  account snapshot numbers, only build on them.
- The other 6 action skills (`budget-optimize`, `mine-search-terms`,
  `audit-ad-copy`, `investigate-campaign`, `pmax-audit`)
  each have their own directory category already (see the table above), so
  they evidently get published too, but **no confirmed template exists yet
  for those** — this file doesn't cover them. If asked to publish one of
  those on its own (not as part of a multi-skill session with a Brief),
  adapt the weekly-review template's shape (header line, TL;DR callout,
  findings, recommended actions, verification-notes toggle) rather than
  guessing from nothing, and fold the confirmed result back into this file
  afterward so it stops being a gap.

## Template 1 — Weekly-review snapshot

**Title:** `<emoji> Google Ads Weekly Review — <Mon D, YYYY>: <short
headline>`. Emoji and headline should match that week's leading story
(e.g. 📉 for a decline pattern, ⚠️ for a flagged data-integrity issue) —
not a fixed emoji every week.

**Section order:**

1. Header line — one line, `|`-separated: `**Account:**` (name, customer
   ID, currency) `|` `**This week:**` dates `(still maturing)` `|` `**Last
   week:**` dates `(mature)` `|` campaign count, e.g. "35 enabled
   campaigns, 12 with any spend". (The former trailing-4-wk-avg entry is
   omitted while that pull is retired in `weekly-review` — reinstate it
   only if the pull returns as a named pull.)
2. `<callout icon="<same emoji as title>" color="yellow_bg">` — **TL;DR**
   bold lead-in, then 4–6 bullets, most important first, each with a
   **bolded** lead phrase.
3. `## Account Snapshot` — table: Metric × {This Week, Last Week}, rows
   for Spend, Conversions (8-action), Conv. Value, CPA, ROAS. (No
   Trailing-4-wk column while that pull is retired in `weekly-review`.) One-line headline sentence under the table calling out the
   maturity/integrity caveat.
4. Data-integrity section (own `##` heading) **only if the check is
   flagged** — omit entirely on a clean week rather than a "passed"
   filler heading, unless you want a one-line "Data-integrity check
   passed" note folded into the Account Snapshot headline sentence instead
   (either is fine; don't add a heading with nothing to say).
5. `## Wins`
6. `## Concerns`
7. `## Budget Pacing vs. Plan` — or `— Skipped` with the one-line reason
   if no target was available.
8. `## Recommended Actions` — numbered list, one line each, tied to a
   named skill where applicable.
9. `## Campaign-Level Detail (...)` — table of active campaigns only,
    sorted by cost descending, columns: Campaign, Cost TW, Cost LW, Conv
    TW, Conv LW, CPA LW, ROAS LW, Target ROAS, Lost IS Budget TW, Lost IS
    Rank TW. Footnote any data-quality caveats (e.g. the impression-share
    floor) with a `\*` marker on affected cells, and a closing italic note
    on how many campaigns were excluded as dormant and why.
10. `---` divider, then `<details><summary>Data & verification notes</summary>`
    — one paragraph on data source/pull date/drift check, one on the
    verification tiers actually run (and any agent-discovery substitution,
    named honestly), one pointing to the full `.md` report and
    `recommendations-log.csv` paths on disk.

## Template 2 — Toolkit Findings & Executive Brief

**Title:** `<emoji> Google Ads Toolkit Findings & Executive Brief — <Mon D,
YYYY>` — no headline suffix (unlike the weekly page); 🔍 is the precedent
emoji, reuse it unless a different one is clearly a better fit for that
run.

**Section order:**

1. Header line: `**Account:**` `|` `**Scope:**` naming every skill that
   ran and the date. Immediately follow with one sentence linking that
   session's weekly-review page and stating this brief **builds on it
   without restating it**. **If the run split across a calendar boundary**
   (e.g. `weekly-review` ran one day, the other skills ran the next), say so plainly in the Scope line rather than
   implying one continuous sitting: name which skill(s) ran on which date
   and what window each pulled.
2. `<callout icon="<same emoji as title>" color="yellow_bg">` — **TL;DR**,
   4–6 bullets. Lead with the single most important cross-skill finding
   (a *mechanism*, not a metric, if one was found), not necessarily the
   first skill that ran.
3. `## Key Call-outs` — one table, columns `Priority | Finding |
   Recommended Action`, one row per major finding across all skills that
   ran, color-coded by row (`<tr color="...">`):
   - `red_bg` 🔴 — process failure or guardrail violation; needs action now.
   - `yellow_bg` 🟡 — root cause found, Quality Score issue, still-open
     item, or amendment needed; needs action, not urgent-critical.
   - `blue_bg` 🔵 — opportunity; upside, not a problem.
   - `gray_bg` ⚪ — resolved, not a concern, or a methodology/toolkit gap;
     informational.
4. `## Executive Brief` — narrative prose, several paragraphs, synthesizing
   *across* skills rather than listing each skill's output in turn. Call
   out explicitly when two+ independent analyses converged on the same
   root cause (that convergence is itself evidence worth stating), and
   when a first-draft conclusion was revised after seeing a fuller picture
   — don't hide the correction.
5. One `## Detail: <Topic> {toggle="true"}` section per major finding from
   the Key Call-outs table (toggle heading, contents indented so they're
   collapsible) — not one per skill. Each ends with `Full detail:
   \`<skill>-YYYY-MM-DD.md\`` pointing at the on-disk report.
6. `## Still Open — Not Closed Today {toggle="true"}` — bulleted, every
   item that was flagged but not resolved this run, including toolkit gaps
   found along the way (a methodology blind spot is exactly the kind of
   thing that belongs here **and** in `MAINTENANCE.md` — log it in both).

**On-disk convention:** draft the brief at
`google-ads/outputs/toolkit-executive-brief-YYYY-MM-DD.md` (dated to the
day the brief itself was compiled, not necessarily the same date as every
skill run it synthesizes) before publishing to Notion. Get a second-opinion
review of the draft (numbers, claims against the underlying skill reports)
before creating the Notion page — this file is exactly what a `.md` review
pass should be checked against.

## Shared conventions across both templates

- Escape `$` as `\$` in all Notion markdown body text and table cells
  (required by Notion's markdown spec, easy to forget when copying numbers
  straight from a `.md` report).
- **Use the `<table>` block syntax for every table, never plain
  `|`-pipe markdown tables** — Notion's markdown spec doesn't define pipe
  tables at all, only the `<table fit-page-width=... header-row=...>`
  block; a pipe table silently fails to render as a table. Easy to slip
  into when adapting prose that already has a small ad hoc comparison
  table in it.
- **Tabs, not spaces, indent children of a callout or `{toggle="true"}`
  heading** — one tab per top-level child line (paragraph or bullet). For
  a `<table>` nested inside a toggle or callout, only the opening
  `<table ...>` and closing `</table>` tag lines get the parent's
  indentation tab; the `<tr>`/`<td>` lines inside stay at column 0. Verify
  by fetching the page back after creating it and checking the toggle
  sections actually collapsed their content, not just that the create call
  succeeded.
- Never fabricate a number to fill a template slot — if a section's data
  isn't available (budget pacing, Auction Insights, etc.), say so
  explicitly in that section rather than omitting it silently, same
  no-silent-partials rule as `verification-protocol`.
- State any verification-agent substitution honestly in the
  verification-notes toggle (e.g. "ran as a general-purpose agent using
  the `google-ads-verifier` brief verbatim — the named agent isn't
  surfacing as a callable subagent type this session") rather than
  implying the named agent ran when it didn't.
- If a page format ever needs to deviate from this file (a genuinely new
  section type, a color convention that doesn't fit), update this file in
  the same session — don't let a second, undocumented variant start
  drifting in Notion the way the first two did before this file existed.
