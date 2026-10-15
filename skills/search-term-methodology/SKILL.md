---
name: search-term-methodology
description: "The decision rule for negating a Google Ads search term. Negation is decided by numbers only — 30-day spend, clicks, 8-action conversions and assumed-value CPA against three thresholds the account owner sets — and the old relevance categories (wrong intent, off-topic, too generic) are demoted to labels attached after the decision, never reasons for it. Also defines the search-term status gate, the term-scoped do-not-negate check, and the refusal when a threshold is missing. Loaded by mine-search-terms. Use when deciding whether a search term should be negated, building a negative keyword list, or reviewing a search term report."
---

# Search Term Mining Methodology

The decision rule. `mine-search-terms` holds the mechanics; this file holds
what makes a term a negative.

## The change, and why

This skill used to say: negate when a term is **off-theme**, signals **wrong
intent**, or is **clearly irrelevant**. That is a judgement call dressed as a finding, and it negates real buyers:

- A query for an adjacent feature, categorised `wrong intent`, can be a real
  buyer looking for exactly that capability.
- A query naming a general tool, categorised `off-topic`, can be someone
  comparing tools who discovers you.

So the rule is inverted. **The numbers decide. The category describes.**

## The category can never be the reason

A category is a word beside a row so a reader scanning 200 of them can see the
shape. It is attached **after** every decision is fixed and it is read by
nothing.

**Never write "negated because it is off-theme", "wrong intent", "too generic"
or "not our buyer".** Every negation's stated reason is its own numbers. If the
numbers do not carry the negation, the negation does not happen — however
irrelevant the query reads.

`tools/build_mine_search_terms_facts.py` enforces this structurally: `decide()`
takes a `Numbers` record holding cost, clicks, 8-action conversions, assumed
value, the term's account-wide conversion total and the search-term status, and
**nothing else**. It cannot see the search term. `categorise()` runs afterwards
and writes to a different field.
`tools/test_mine_search_terms_facts.py` blanks, shuffles and rotates every
category and asserts the decisions come back byte-identical.

**The labels are approximate and `uncategorised` is a legitimate answer.** They
are matched on whole words, not substrings, so `free` does not label
`freelance` — a visibly wrong label on the owner's own example would make a
reader distrust a column that decides nothing. Never tune a label until it
"looks right"; if the label is the only thing wrong, nothing is wrong.

## The four thresholds, and the refusal

All four are the account owner's to set, captured by `account-setup`. The
fourth exists because the third cannot be computed without it.

| Parameter | What it decides |
| :- | :- |
| `min_spend_cad` | minimum 30-day spend before a zero-conversion term may be negated at all |
| `min_clicks` | minimum clicks before a zero is trusted |
| `target_cpa_cad` | the target CPA on the 8 actions |
| `target_cpa_multiple` | how many times target CPA a converting term may reach before it is cut anyway |

**None of them has a default and the run refuses without all four.** Say what
it needs and stop. A previous run in this pack invented a threshold and shipped
an unstated subset of the terms it applied to; that is the failure being
designed out, and a plausible-looking guess is the same failure with better
manners.

**`target_cpa_cad` is required because a multiple needs a base.** Many Search
campaigns carry no `campaign.target_cpa`, so a multiple alone has nothing to
multiply.
Deriving a target from the account's own blended CPA would be exactly the
invented threshold this rule forbids, and `campaign-guardrails` rule 2 forbids
a blended figure anyway. Ask for the number.

**Offer the consequence, never the answer.** Run
`build_mine_search_terms_facts.py --sensitivity <spend>:<clicks>` over pairs
**the owner named** and show what each would negate. The choice is consequential
enough that it cannot be delegated: on one real account, a high pair negated
nothing at all and a low pair negated 25 rows.
The script generates no candidates of its own.

## The decision, in order

Each branch is a metric or an enum. Nothing below reads the term text.

1. **Status gate.** `search_term_view.status` is `ADDED` → **not eligible**:
   this query is already a keyword here, and a negative would cancel our own
   targeting. Typical case: a competitor-name query with zero conversions in
   the conquesting campaign whose whole purpose it is. `EXCLUDED` → already a
   negative, nothing to do. Only `NONE` is eligible.
2. **Term-scoped safety.** Zero conversions on this row but **the same query
   text converts anywhere in the account** → **keep**. The join key is the
   **search term**, never `(campaign, term)`: the narrower join can pass clean while
   dozens of rows are about to be negated in one campaign while converting in
   another.
3. **Converting rows.** Assumed-value CPA above
   `target_cpa_cad × target_cpa_multiple` → **negate**. At or below → **keep**.
4. **Zero-conversion rows.** Spend below `min_spend_cad` → **keep**. Spend at or
   above it but clicks below `min_clicks` → **hold**, not negate: the zero is
   not yet trustworthy. This branch is load-bearing: accounts really do hold terms
   at hundreds in spend on **2 clicks**. Both floors cleared → **negate**.

## Two things the numbers alone must not decide

- **Cross-campaign ownership.** A query serving from more than one campaign is a
  which-campaign-owns-it finding, reported separately. It never reaches the CSV:
  a routing negative only works paired with adding the term to the campaign that
  should own it, and a one-row-per-negative file cannot express the second half,
  so a reader importing it applies half the fix.
- **Token and pattern negatives.** Never recommend a `free` / `template`-style
  token negative unless **that pattern** has been run against the 8-action pull.
  On one real account, a `free` token negative would have blocked 19 converting
  terms, some of them priority-plan conversions. Clearing the individual rows does
  not clear a generalisation of them.

## What every negation states

The search term, the triggering keyword and its match type, the campaign and ad
group with numeric IDs, spend, clicks, 8-action conversions, assumed-value CPA
and ROAS — and the reason **as numbers**. The category goes in its own column,
labelled as a label.

**No search-term deep links.** That URL path is unverified (`evidence-links`).
IDs only, until a verified search-terms URL is recorded in `account-links.md`.
