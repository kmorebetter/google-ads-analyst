---
name: campaign-guardrails
description: "Reference skill encoding account-level guardrails for a Google Ads account: YouTube/Demand Gen are TOF-only (never judged on BOFU CPA/ROAS), Brand Core stays branded-only and is never a growth lever, the learning phase is protected from disruptive changes (14 days Search / 28 days Performance Max), Quality Score is out of scope for ad groups carrying a trademark restriction (detected per ad from the policy field, never by campaign type), keywords with no Quality Score are not a finding, the Google Ads budget is read from the owner's workbook and increases are sized against documented elasticity, and target-based bidding is a standing monitored event. Load before generating recommendations, judging campaign health, or flagging a campaign for a bid/budget/pacing change. Use whenever making or evaluating a Google Ads recommendation."
---

# Campaign Guardrails

Account-level rules that constrain what recommendations are allowed to say,
not metrics to report. These rules are also **machine-checked**: the Tier 2
verifier agent defined in `verification-protocol` independently re-checks
every report against them (its checklist lives there), so a guardrail
violation should be caught even if the authoring skill misses it. **Rules 6
to 9 are checked the same way** — each one is written as a
predicate the verifier can evaluate against a finished report without
re-deriving any number. Load this alongside `account-conventions` (structure)
and `conversion-measurement` (what counts as a conversion) whenever a skill
is about to judge campaign health or propose a change — the loading skills'
own Prerequisites blocks are the source of truth for which do.

## 1. YouTube + Demand Gen = top-of-funnel support only

The campaigns in the `tof_campaign_ids` bucket (see `account-conventions`) exist to
build awareness and re-engage, not to close. **Never judge either on
BOFU CPA/ROAS from the listed conversion actions.** Measure them on reach,
engagement, view-through/assisted conversions instead. A recommendation that
says "cut YouTube, CPA is too high" is a guardrail violation — flag it before
it reaches the user.

## 2. Brand Core = branded terms only

The main brand campaign (`brand_core_campaign_id`) and the other campaigns in
the brand bucket exist to defend branded queries. **Don't scale Brand
Core with generic terms** — it inflates blended ROAS (Brand converts cheaply
almost by definition) and cannibalizes what the Non-Brand Generic campaigns
should be earning credit for. If a search term audit finds generic queries
serving from a Brand campaign, that's a `mine-search-terms` finding
(cross-campaign overlap), not a scaling opportunity.

**Brand is never a growth lever.** Brand captures demand that largely already
exists, and it can cannibalise organic. **Never recommend increasing Brand
budget on performance grounds.** A cheap Brand CPA is not evidence of
headroom — it is the definition of the campaign type.

**The only admissible trigger** for a Brand budget or bid recommendation is
evidence of competitive pressure on the brand itself:
`metrics.search_rank_lost_impression_share` on Brand Core **rising week over
week**. Quote both weeks' values beside the recommendation. No other trigger
is acceptable, including lost impression share, cheap CPA, strong ROAS or
available budget elsewhere.

**Report brand and non-brand subtotals separately. Never lead with a blended
figure.** A blended CPA/ROAS is Brand's cheap conversions flattering Generic's
expensive ones. If a blended number is genuinely needed, it comes *after* both
subtotals and is labelled "blended".

## 3. Learning phase = protected

Any campaign that has recently had a significant bid strategy, budget, or
target change is in (or re-entering) the learning phase. **Flag any proposed
recommendation that would reset it** — a second change before the algorithm
re-stabilizes compounds the disruption rather than fixing anything. Default
to batching changes rather than applying them one at a time as they're
discovered; call out the batching in the recommendation itself so the user
can approve the whole batch at once.

## 4. Target-based bidding on budget-limited campaigns

When budget-limited campaigns run `MAXIMIZE_CONVERSION_VALUE` /
`MAXIMIZE_CONVERSIONS` with a target (or `TARGET_ROAS`/`TARGET_CPA`), Google
holds them to that target. Mechanics and surfacing live in `budget-optimize`
step 4 (tighten-before-budget for clusters beating their target); whether a
target change pulled overperforming clusters back toward target is
`outcomes-review`'s drift verdict. Skills touching budget or target
recommendations defer to those two rather than recomputing the logic.

**Caveat (see `conversion-measurement`):** if the account's Smart Bidding goal
set is broader than the listed conversion actions, "actual ROAS on the listed
actions" and "Google's set target" are on different bases. Present any
actual-vs-target gap with that caveat attached, until the owner confirms which
basis is intended.

## 5. Low-volume floor — no bid/budget/target recommendation on thin data

A budget, bid, or target change recommendation must rest on at least **5
conversions from the 8 actions inside the analysis window** for the campaign
(or shared-budget cluster) in question. Below that, the same observation is
allowed only as a **"monitor"** note — the week-to-week noise on a handful
of lagging conversions is larger than the effects being diagnosed.
Impression-share and spend observations (pure volume mechanics, no
conversion denominator) are exempt. The Tier 2 verifier checks this floor
like any other guardrail.

**The guardrail this creates right now:** if a budget-limited campaign is
already beating its target, the correct move is to **tighten the target**
(Bid Target Adjustment Tool), not add budget. Google's own "recover lost
impression share" prompts point the wrong way for these campaigns — the
recoverable impressions are the marginal, lower-value tail, and adding budget
just delays the target-tightening the campaign actually needs. Any
recommendation to increase budget on a budget-limited campaign that beats its
target must justify why target-tightening isn't the better first move.

## 6. Trademark-restricted Quality Score is out of scope

Ad-serving restrictions on competitor-brand ad groups usually cannot be
resolved without legal exposure. Confirm with the account owner during
`account-setup`.

- **Exclude trademark-restricted ad groups from Quality Score findings
  entirely**, and from ad-strength and copy-improvement recommendations.
- **The scope test is the restriction, never the campaign type.** Detect via
  `ad_group_ad.policy_summary.policy_topic_entries[]` where a topic contains
  `TRADEMARK`. The exclusion is **per ad**, and an ad group leaves Quality Score
  scope when **any** of its servable ads carries such a topic. **A conquesting
  ad with no restriction on record stays in scope.** `audit-ad-copy` step 4
  states the same reading; the two must not drift.
- List which ad groups are in and out of scope, by ID, from the run's own pull.
- A trademark recommendation the owner has already declined is a guardrail
  violation if it reappears, not a finding. Record declines in
  `STANDING-ITEMS.md`.

**Do not bundle other policy classes in with trademark.** A restriction such as
`FREE_DESKTOP_SOFTWARE` or `UNAPPROVED_SUBSTANCES` is a different class: report
it on its own terms, count it from the run's own pull and cite the fact,
diagnose nothing, and leave those ads in scope for copy work. `audit-ad-copy`
step 4 holds the handling for every policy class, including a catch-all for a
class not seen before, so no restriction is met with silence.

## 7. Learning windows — 14 days Search, 28 days Performance Max

The concrete, checkable form of rule 3.

**Scope first, or this rule neuters every report.** In an actively managed
account, nearly every campaign may carry a `CAMPAIGN_BUDGET` change inside the
14-day window. Applied to "every finding," the rule would downgrade the entire
audit to "monitor" and say nothing. An unscoped reading is not conservative;
it is silence.

*Count events and campaigns separately, and say which.* One campaign can carry
several change events, so "N of them" must say whether N counts events or
campaigns. A count is meaningless without its unit.

**The window applies only to a finding whose cause the change could plausibly
be.** Concretely:

| Finding type | Learning window applies? |
| :- | :- |
| Efficiency or performance: CPA, ROAS, conversion rate, volume moves, "underperforming" | **Yes.** Downgrade. |
| Bid, budget or target recommendation | **Yes.** Downgrade. |
| Copy defect: misspelling, grammar, false claim | **No.** A budget change did not misspell a headline. |
| Policy or compliance: trademark, prohibited keyword insertion | **No.** |
| Coverage or structure: headline count, single-RSA ad groups, missing extensions | **No.** |
| Serve-frequency ranking within one ad | **No**, provided the ad itself had no `AD` edit in the window. Use the `AD` / `AD_GROUP_AD` event, not the campaign's budget event. |

**Match the change to the claim, not to the campaign.** A campaign budget event
cannot downgrade an ad-level copy finding. An `AD` edit inside the window
**can**, because it resets the serve data the ranking rests on. Cite the event
type you matched.

For the finding types where it does apply, a campaign carrying a
bid, budget or target change inside:

- **14 days** for Search, or
- **28 days** for Performance Max

is **downgraded from a recommendation to "monitor — in learning"**. Downgraded,
not softened or caveated.

**State the change date and the change type** beside the downgrade — e.g.
`monitor — in learning: target changed YYYY-MM-DD (TARGET_ROAS)`. A downgrade
missing either field is incomplete. The verifier checks that every
recommendation either sits outside its window or carries the downgrade with
both fields present.

## 8. Budget and pacing — read the figure, never derive it

Facts and current actuals live in `budget-plan.md` and
`tools/account-config.json` (both written by `account-setup`). This rule is the
durable part; the numbers move.

**Read the Google Ads budget the owner stated. Never derive it from a share of
an all-channel total.** A derived figure is a guess with a decimal point. Pace
in the account currency and name the currency.

**A month with no stated Google Ads budget stays unstated.** Ask the account
owner. Do not derive one, and do not carry another month's figure forward.

**Real pacing = spend-to-date vs budget pro-rated by day.** A spreadsheet
"pace" column that compares yesterday's spend with a 7-day average is an
acceleration check, not a budget check. Report the real figure.

**A planned month-over-month step down is the plan, not a problem.** Never
report the planned reduction as a finding. Overspend *against* the lower budget
is a separate and real finding; report that one.

**Sizing an increase (elasticity bands, from the owner's own projections in
`account-config.json`):** band 1 is understood to be efficient, band 2 carries
a stated CPA degradation, and above the last stated band no data exists, so do
not recommend it.

This is a **ceiling on an increase that has already cleared every other
guardrail**, not a licence to raise budget. Rule 2 forbids Brand increases on
performance grounds, and the note at the end of rule 5 still stands: for a
budget-limited campaign already beating its target, tightening the target
comes before adding budget. Any budget-increase recommendation must state
which band it sits in.

## 9. Keywords with no Quality Score are not a finding

Expected behaviour on low-volume broad match — Google doesn't assign a Quality
Score without enough auction data. **Confirm with the account owner during `account-setup`.**

The typical finding this retires: **"N keywords, $X spend, no Quality
Score."** It is neither a data-quality problem nor an optimisation
opportunity. **Never report a null Quality Score as a finding.** Absence of a
score is absence of data, not a defect.

The reason is recorded here so it doesn't reappear on the next audit. If it
does, the verifier flags it as a guardrail violation.
