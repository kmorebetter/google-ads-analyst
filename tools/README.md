# tools/ — the facts-file harness

Reports stop doing math in prose. A script reads the raw JSON that a run already
saved, writes every number into a facts file, and then a checker confirms every
number printed in the report came from that file.

This is VERIFICATION-RULES **VF-15**. Read that section before changing anything here.

## Why this exists

On 2026-08-10 a report compared **$49,210.77** against **$48,323.11** and called
the difference growth. Both numbers were correct. The first one included two
conquesting insertion ad groups. The second one did not. Nothing in the report
said so, so nobody could see the two numbers were not the same thing.

The fix is a label. Every number carries the exact filter it came from, and the
code **refuses** to subtract two numbers whose labels differ.

## The files

| File | What it is |
| :- | :- |
| `facts.py` | The library. The `Fact` record, the `FactSet` container, the comparison guard, the micros helper. (It once also held a value-search report checker; that was removed 2026-08-27 — see "One checker" below.) |
| `account-config.json` | The single source for account and calendar constants: the 8 conversion actions, campaign-ID buckets, stated monthly budgets (unstated months are explicit nulls with the reason), thresholds, elasticity bands, the CAC anchor. Edit this, not a builder, when the account or the plan changes. |
| `account_config.py` | Loads the config. `stated_budget("2026-09")` returns null-with-reason for an unstated month; an unknown month raises. `test_account_config.py` re-parses `budget-plan.md` and fails if the config drifts from the doc. |
| `build_audit_ad_copy_facts.py` | Reads one day's raw JSON for `audit-ad-copy` and writes that day's facts file. Start here. |
| `asset_facts.py` | The per-headline facts. The most dangerous pull in the pack, so its rules are enforced in code. |
| `citations.py` | **The report checker.** Every stated number — whole, decimal or percentage — must name the fact it quotes, and the named fact must agree. |
| `pulls.py` | Reads a saved pull, checks it against the filter it claims, and works out what it proves about ad group status. Also `ProblemLog`, which lets a builder report every validation contradiction in one pass instead of dying on the first. |
| `builder_core.py` | Machinery shared by the builders: target reading (all four fields), sentinel handling at both ends, change-event parsing, percentage change, and the anchored-week pair guard (`verify_week_pair`). |
| `pull_specs.py` | The pull contract, one place: per skill, every pull's mandatory filename, resource, filter label and temporality kind. Builders import their filenames from here, `test_skill_builder_contract.py` checks skills and builders against it, and `declare_pull.py` enforces it. Adding or renaming a pull is one spec edit. |
| `declare_pull.py` | Writes a pull's `_manifest.json` entry from the query that ran: `python3 tools/declare_pull.py <raw_dir> <filename> --gaql '<query>'`. Rows and truncation from the saved file, temporality from the query, spec cross-checked. Idempotent; hand-written entries stay valid. |
| `reconcile_recommendations.py` | Closes the recommendations loop: detects applied changes from current-state pulls + `change_event`, proposes status flips with evidence, backfills `after_value`, measures outcomes in matched windows, surfaces long-open rows. Report-only by default; `--apply` writes. |
| `scopes.py` | The filter text, the file names (aliased from `pull_specs.py`), and reading `_manifest.json`. |
| `claims.py` | The one answer to "is this number a claim about the account". `citations.py` imports it. Nothing else defines exemptions. |
| `gaql.py` | Compares the query that ran against the label a fact carries, clause by clause. |
| `test_facts.py` | The tests (about 200). |

Python 3, standard library only. No install step, no dependencies.

## How to run it

**1. Build the facts file for a run.**

```
python3 tools/build_audit_ad_copy_facts.py google-ads/outputs/raw/2026-08-19
```

Writes `google-ads/outputs/raw/2026-08-19/audit-ad-copy-facts.json`. Add
`--out <path>` to write somewhere else, `--print` to see every fact on screen.
Per-headline detail is emitted only for the ads the report actually tables
(the serve-table selection rule, from `account-config.json`) plus any ad
named with `--ad <ad_id>` — every ad is still processed for the account
totals and integrity checks, and three facts record the rule and the
covered/excluded counts. This is deliberate: the old
every-headline-of-every-ad emission (6,448 facts on Aug-19) built the dense
value space where fabricated numbers passed by coincidence.

It prints how many facts it wrote and lists the ones that came out **null**. A
null fact means "this number cannot be worked out from the files you saved, and
here is why." That is on purpose. A missing fact looks like someone forgot.

**2. Check the report: every number names its fact, and the report agrees
with its own metadata.**

```
python3 tools/citations.py google-ads/outputs/audit-ad-copy-2026-08-19.md \
        google-ads/outputs/raw/2026-08-19/audit-ad-copy-facts.json \
        google-ads/outputs
```

Exit code 1 if anything is flagged, 0 if nothing is. This is the mechanical
half of Tier 2. The report has to name the fact it is quoting, and the named
fact has to agree — checking is a lookup, never a search.

The third argument is the outputs directory (the one holding
`recommendations-log.csv`) and is optional. Passing it adds the two
self-consistency checks: every pull in the facts file's
`meta.undeclared_pulls` must be named somewhere in the report, and a report
claiming it appended to the log must have a row dated `meta.pulled`. Both were
HIGH Tier 2 findings on 2026-08-28 against a draft this checker had already
passed clean. **Omit it and the append check silently does not run** — so pass
it on any real run.

```
We found 126 dead headline slots [fact: asset_view.dead_slot.total].
The top 3 take 52.0% [fact: ...headline.top_3.share].
```

**Whole numbers, decimals and percentages all need a citation.** Percentages
used to be left alone, on the belief that value matching caught them reliably.
That belief was wrong, and it was proved wrong on 2026-08-19: see "One
checker" below.

Four things get flagged: a number with no `[fact: ...]` beside it, a named fact
that is not in the file, a named fact whose value disagrees with the sentence,
and a percentage whose fact does not say which scale it uses. Every number has to
be covered, so a wrong second number cannot ride along on a correct citation.

It reads a paragraph at a time, so a sentence can wrap and keep its citation —
including when the citation itself lands on the next line. Table rows are checked
one by one, so a wrong cell cannot borrow the row above's proof.

**A percentage matched against its fact.** A fact holding `52.0` with a unit
naming `percent` means 52%. A fact holding `0.52` with a unit naming `ratio`
means 52% too. Both back a report printing `52.0%`. A fact whose unit says only
`share` or `rate` backs neither, and the checker says so instead of guessing.
Give the fact a unit that names its scale.

**Rounding is allowed only where the report showed decimals.** `$3,231.15` may
stand for a fact holding `3231.148422`. A bare `126` may not stand for a fact
holding `126.4` — nothing was rounded there.

One thing is newly in scope and worth knowing: a version number written without
a pointer word, `Python 3.11` or `version 2.4`, now reads as a claim. No report
in this pack writes either, and `claims.py` does not hand out exemptions for
words the pack does not use, so this is left flagged rather than pre-exempted.
Write `v21.1` or put it in backticks.

Dates, times, "the 14th", years, long object IDs, code spans and section labels
are all left alone. Write "removed on Aug 14" and "ad 620000000010" freely. So is
a whole number after a currency word: `CAD 2,000` is a threshold the report chose,
not a figure out of the account, so it has no fact to cite. `CAD $3,231.15` does.

**3. Run the tests.**

```
python3 tools/test_facts.py
```

## What each fact carries

Seven fields, exactly as VF-15 sets out.

- `id` — the name a report cites, like `rsa.servable.count`
- `value` — the number, or `null` when it cannot be worked out
- `unit` — `count`, `currency_cad`, `impressions`, `conversions`, `text`, …
- `scope` — **the literal filter**, word for word. Never a summary of it.
- `window` — either real dates, or `current-as-of-2026-08-19` for a fact about
  right now. Never a relative label like `LAST_30_DAYS`.
- `source` — which raw JSON file it came from
- `basis` — `8-action`, `broad-goal-set`, or `n/a`

## Right now versus over a window

This is the rule that a report broke on Aug 19. It said a misspelled headline
was live on six ads. The owner had removed it five days earlier. The evidence
was a 30-day serve pull, which still showed the old impressions.

So the harness keeps the two apart:

- A fact about right now reads `window: current-as-of-2026-08-19`.
- A fact about a period reads `window: 2026-07-20..2026-08-18`.
- The guard refuses to compare one against the other, and the error says why.

**The pull declares which it is. The harness never guesses.** Each raw folder
carries a `_manifest.json` saying, per file, the exact filter used and whether
it is current or windowed. Guessing from the data got 2 of 6 files wrong on
Aug 19. If a folder has no manifest, every window string says so out loud.

## Headline rankings, and the two traps in them

Ranking headlines inside an ad is the most error-prone thing here. Three rules
are enforced in code, not left to the reader.

**One ad in one ad group.** Google returns the same ad under every ad group it
is linked to, and the asset pull has no status filter. Ad 620000000001 comes
back 140 times across 5 ad groups; narrowed to one ad group it is 22 rows. So
every fact ID names both, like
`asset_view.adgroup.620000000008.ad.620000000001.headline.rank_1.impressions`.
Naming only the ad crashed an earlier version of this script on a duplicate ID,
which is the friendliest possible version of that bug.

**Serves are a window; the ad is right now.** A 30-day pull includes headlines
that have since been deleted. The builder joins the two and sorts every headline
into one of three buckets:

| Bucket | Meaning |
| :- | :- |
| On the ad, serving | Ranked. |
| On the ad, zero serves | **Dead slot.** This is the finding worth acting on. |
| Serving, not on the ad | History. Never a live problem. |

On Aug 19 that is 878 live headline slots, 126 dead slots across 21 ads, 40 ads
with none, and 196 serve rows correctly set aside as already removed.

Counting dead slots across ads is allowed, even though adding up asset
impressions is not. The ban is on adding **metrics** that Google credited to
every asset in a serve. A slot count adds 1 per headline no matter how it served,
so there is nothing to inflate. Those facts carry the unit `count` and say so in
their scope.

**Serve credits are not impressions.** Google credits every headline in an ad
with the whole ad's numbers, so adding them up inflates the total. One ad's 15
live headlines sum to 40,552 serve credits while the ad itself had 23,796
impressions. The sum is only ever used as the bottom of a percentage inside that
one ad, it carries its own unit, `sum()` refuses it, and the ad's real
impressions sit right beside it as a fact.

## The guard

`compare()`, `diff()` and `sum()` all refuse unless **unit, scope, window and
basis** match on both sides. The error names which one differed and prints both
values, so you can see the problem without going to look it up.

Two more refusals:

- A fact whose value is null cannot be used in math. You get a clear message,
  not a crash.
- Asset-level impressions and clicks can never be summed. Google credits every
  headline in an ad with the whole ad's numbers, so adding them up overstates
  the truth by about 3.4x (VF-4). Those facts carry units ending
  `_asset_credited` and `sum()` blocks them by name.

If you truly must relate two facts with different labels, `compare_unsafe()`
does it, but only if you pass a written reason of at least 20 characters. The
reason and the mismatch both get written into the facts file. There is no quiet
way around the guard.

### Derivation profiles — the sanctioned bridges

A report is full of comparisons the bare guard would refuse (a
week-over-week delta *is* two windows). Rather than routing around the guard,
the builders declare **derivation profiles** (`PERMITTED_PROFILES` in
`build_weekly_review_facts.py`): each names exactly which guarded attributes
it may bridge, and everything else must still match.

| Profile | May differ | Must match | What the restriction catches |
| :- | :- | :- | :- |
| `wow-delta` | `window` only | unit, scope, basis | two individually-correct figures on different scopes, differenced and reported as a trend |
| `cost-over-conversions` | unit, scope, basis | **`window`** | a CPA dividing this week's cost by last week's conversions |
| `plan-vs-actual` | `scope` only | unit, window, basis | a budget compared against a different month's spend, or a different currency |
| `actual-vs-target` | unit, scope, window, basis | — see below | a gap measured across a target edit |

Three enforced conditions: the permitted attribute must *actually* differ
(otherwise a parent id is wrong); outside `actual-vs-target` neither parent
may be a current-state fact (differencing `current-as-of-<date>` against a
window is VF-17); and a delta's unit gets a `_delta` suffix so the guard
blocks summing a delta into a level while still allowing delta-vs-delta.
`actual-vs-target` is the one profile allowed a current-state parent — a
target is a live setting, an actual spans a window — and it carries its own
guard: the gap is refused when the change log shows the target was edited
inside the window. Every derivation writes its parents, profile and rule to
`meta.derivations`.

## What the checker skips

The skip list lives in `claims.py` and nowhere else. (It was born as the peace
treaty between two checkers that disagreed — `30-day` passed one and failed
the other, `### 3.3` the reverse, and no wording passed both. The second
checker is gone; the single list stays the law.)

It is meant to over-report. A human reads the list. It ignores:

- code blocks and anything inside backticks
- links, web addresses, and `[fact: id]` tokens
- heading numbers (`### 3.3`, `### 4.`) and numbered list markers
- dates, times and ordinals: `2026-08-19`, `Aug 14`, `14:56`, `the 14th`, and
  bare years
- numbers hyphenated or underscored into a word: `30-day`, `8-action`,
  `LAST_30_DAYS`, `H3`
- a number after a pointer word: `section 5`, `rule 6`, `Tier 2`, `top 10`,
  `rank 3`
- object IDs. After an object noun (`ad group 620000000012`), alone in
  parentheses (`(10363430)`), or as a bare run of 8 or more digits.

A bare ordinal in a table's Rank cell is **not** skipped. Write the cell as
`Rank 1` rather than `1` and it passes, because `rank` is a pointer word.
Masking every small integer in a first cell would hide real counts.

That last threshold was 9 until 2026-08-19, and this account has **8-digit
criterion IDs**, so a keyword table with the ID beside the claim — which
`evidence-links` requires — could not pass. Lowering it is free here: the
largest whole-number fact in the Aug-19 file is 194,056, six digits.

**The risk here is skipping too much, not too little.** Every entry above makes
the checker greener, and a green checker standing in for judgement is exactly
what VF-19 warns about. So the list is closed, each entry has its own test,
and there is a test corpus of real claims (`126 dead headline slots`,
`61 servable RSAs`) that must still fail when uncited.

Everything else counts as a claim and has to cite its fact. A figure prints
fine if it is a rounded version of the fact: `$48,213.90` matches a stored
`48213.904417` — but only because the report showed decimals.

## One checker (why the value search was removed)

The harness used to run a second check first: `facts.verify_report`, which
asked "does this number appear as *some* fact's value?" That is a search, and
a search over a big list finds a match by accident. It failed twice on the
same file before being removed on 2026-08-27:

- The Aug-19 file holds 163 of the 201 integers between 0 and 200 somewhere
  in it, so a wrong "99" matched something. A report claiming 99 dead
  headline slots against a real 126 passed unflagged.
- 1,504 of the file's 6,448 rows are per-rank CTR and share percentages, all
  rounded to one decimal place, all between 0 and 100. On 2026-08-19 a draft
  carried four made-up share percentages; two read `10.6` and `9.6`, both
  real values in the file — on completely different ads — so they matched
  and shipped past everything then in place.

Value matching only ever looked reliable while the file was small. The lookup
in `citations.py` is a strict superset of the protection the pair provided,
so the search is gone. Do not bring it back; the regression tests in
`test_facts.py` (SoleCheckerPortedRegressionTests and the fabricated-
percentage classes) hold the line.

Expect a long list the first time you point the checker at an old report —
they were written before any of this existed.

## Two traps worth knowing

**The join key is `ad_group_ad.resource_name`, never `ad.id`.** Google gives the
same ad ID to identical creative in different ad groups. In the Aug-10 pull one
ad ID appears in four ad groups; in the Aug-19 asset pull, 33 ad IDs appear in
more than one, up to 5 apiece. Join on `ad.id` and you lose rows and mis-assign
spend. The builder refuses to run if the key is not unique.

**The builder checks the filter it claims.** Each input file's filter is written
down in the script, and every clause is re-checked against every row. If the
data contradicts the label, the build stops and writes nothing. If a field was
not saved, the clause is marked "DECLARED" in the facts file rather than
pretended to be verified. A right number under a wrong label is the exact thing
this harness exists to stop.

**Query text and label text are compared clause by clause, not character by
character.** `_manifest.json` records the query that actually ran, quotes and
all: `ad_group_ad.status='ENABLED' ... ad_group_ad.ad.type='...'`. A fact's
`scope` records the tidy label: `ad_group_ad.status=ENABLED ... ad.type=...`.
Those two strings can never be byte-identical, and this pack demanded that they
be until 2026-08-19. `gaql.py` strips the quotes, rewrites
`ad_group_ad.ad.type` to `ad.type`, and then diffs the clause lists. A missing
or extra clause stops the build. A spelling difference does not. The date clause
(`AND segments.date DURING LAST_30_DAYS`) is held out, because a fact records
its date range in `window`, not in `scope` — and the builder checks it against
the declared window instead.

**One name per pull.** The asset pull is
`audit-ad-copy-asset-serves-30d.json`. On 2026-08-19 a second, identically
named-but-different file (`audit-ad-copy-asset-view-30d.json`) sat beside it
with the same contents, and the toolkit accepted either. If two such files ever
disagree, the builder now stops rather than picking one. Identical copies build
fine and the duplicate is named in the audit trail so it can be deleted.

## Older folders

The 2026-08-10 folder has no `_manifest.json` and no asset pull. The builder
still runs on it. Facts that cannot be worked out come back null with the
reason, and every window string says the temporality was never declared. That
is the honest result, not a failure.
