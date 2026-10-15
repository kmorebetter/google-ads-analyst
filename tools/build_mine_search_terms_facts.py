#!/usr/bin/env python3
"""Build the `mine-search-terms` facts file from saved raw JSON (VF-15).

Usage:
    python3 tools/build_mine_search_terms_facts.py <raw_dir> \
        --min-spend-cad 250 --min-clicks 10 \
        --target-cpa-cad 500 --target-cpa-multiple 3

Reads only the raw JSON already on disk. Makes no API calls. Writes
`<raw_dir>/mine-search-terms-facts.json` unless `--out` says otherwise.

Six things about this script matter more than the arithmetic.

1. **The four thresholds have no defaults and the script refuses without them.**
   They are the account owner's numbers, not ours (account-setup: thresholds). A
   previous run in this pack invented a threshold and shipped an unstated subset
   of the terms it applied to. `require_thresholds` raises
   `MissingThresholdError` naming every absent parameter and what it decides, so
   the refusal is the finding rather than a silent guess.

2. **`decide()` cannot see the term text.** It takes a `Numbers` record holding
   cost, clicks, 8-action conversions, assumed value, the term's account-wide
   conversion total and the search-term status. No text, no category, no
   campaign theme. The category is attached afterwards by `categorise()`, which
   writes to a different field and is never read back. That is the structural
   form of the ask: the decision is data, the category is a description.

3. **Truncation is checked, not assumed.** The unfiltered version of the cost
   query returns exactly 10,000 rows carrying a `nextPageToken` and is silently
   capped; `metrics.cost_micros > 0` brings it to 1,681 and untruncated. Any
   pull returning exactly 10,000 rows is capped. `assert_not_truncated` refuses
   on a page token, on a row count of exactly 10,000, and on disagreement with
   the manifest's own `truncated` field.

4. **Row keys are pairs (VF-19).** The term key is
   `(campaign.id, ad_group.id, search_term, keyword text)` and the keyword key is
   `(ad_group.id, criterion_id)`. Measured in this very pull: 39 of 162 criterion
   ids appear under more than one ad group, so keying on `criterion_id` alone
   makes cost attribution depend on dict insertion order.

5. **Cost and `segments.conversion_action_name` cannot coexist** (VF-3, HTTP
   400). Two pulls, joined on the four-part term key. The conversions pull does
   not select `matchType`, which is why match type is not in the key.

6. **No fact per term.** VF-15a rule 6: a 6,448-row facts file is what made
   coincidental value matches likely. This emits aggregates, counts, the four
   thresholds, and the top-10 table rows. 1,681 terms times six figures each is
   the failure mode, not the goal.

Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_config  # noqa: E402
import gaql  # noqa: E402
import pulls  # noqa: E402
import scopes as S  # noqa: E402
from facts import FactSet, micros_to_currency, money  # noqa: E402

# --------------------------------------------------------------------------
# Raw file names and scope literals
# --------------------------------------------------------------------------
# Canonical filenames and filter labels live in `pull_specs.py` - the single
# source for which pulls exist. (This block once said they "belong in
# scopes.py"; pull_specs.py is where that consolidation landed.)

import pull_specs

COST = pull_specs.ST_COST_30D
CONVERSIONS = pull_specs.ST_CONV_30D
CAMPAIGNS = pull_specs.CAMPAIGNS_CURRENT
CHANGE_EVENTS = pull_specs.CHANGE_EVENTS_14D

#: The 90-day 8-action do-not-negate clearance. A fifth REQUIRED pull, read
#: from the raw dir by this canonical name so no caller has to remember a flag.
#:
#: Why it is required rather than optional: the 2026-08-05 and 2026-08-10 runs
#: both pulled 90 days and both called the check mandatory. Step 1b was later
#: written as `LAST_30_DAYS`, the pull stopped happening, and on 2026-08-20 the
#: term `jotform` came back onto a negation list it had been removed from 15
#: days earlier - because it converted 90 days ago, outside a 30-day window.
#: A 30-day window cannot see a 90-day conversion, so without this pull the
#: CSV is not shippable and this builder emits no rows.
DONOTNEGATE_90D = pull_specs.DONOTNEGATE_90D

#: The cost pull's filter, minus the temporality clause VF-17 records in
#: `window`. The `cost_micros > 0` clause is not a nicety: without it the query
#: returns exactly 10,000 rows and a `nextPageToken`.
COST_SCOPE = pull_specs.ST_COST_LABEL

#: The conversions pull's filter, minus temporality. `basis` is `8-action`.
CONVERSIONS_SCOPE = pull_specs.ST_CONV_LABEL

CAMPAIGNS_SCOPE = pull_specs.ENABLED_CAMPAIGNS

#: The join every decision rests on.
TERM_KEY = "(campaign.id, ad_group.id, search_term, keyword text)"
KEYWORD_KEY = "(ad_group.id, ad_group_criterion.criterion_id)"

#: A pull returning exactly this many rows is capped. Verified on the unfiltered
#: cost query 2026-08-20: exactly 10,000 rows plus a `nextPageToken`.
PAGE_CAP_ROWS = 10_000

#: Brand Core, by campaign id, from `account-conventions`' category list. By id
#: and never by name: `'Brand' in name` also matches `JS - Non-Branded
#: Keywords`, `Search-NonBrand-T1/T2` and `DD | Non Branded`, which is eight
#: campaigns misclassified into the guardrail that governs Brand.
#: Read from `account-config.json` (campaign_buckets.brand_campaign_ids),
#: which carries the id -> name map.
BRAND_CORE_CAMPAIGN_IDS = account_config.brand_campaign_ids()

#: Substrings that make a query branded, including the two inversions this
#: account really receives (`better practice`, `acme app`). **This list
#: drives no negate/keep decision.** It splits the keyword-idea list into brand
#: housekeeping and growth, because `campaign-guardrails` rule 2 forbids scaling
#: Brand, and it is a review aid nowhere else.
BRAND_TOKENS = account_config.brand_tokens()

#: Google's documented negative-keyword text limit. **Not verified against the
#: API or against Google Ads Editor by this pack** - it is linted and the count
#: reported so a human can check, never silently trusted.
KEYWORD_MAX_CHARS = 80
KEYWORD_MAX_CHARS_PROVENANCE = (
    "Google's documented keyword text limit. NOT verified against the API or "
    "against Google Ads Editor by this pack; the count is reported for a human "
    "to confirm before import."
)

#: How many rows the report's headline table carries.
TOP_N = 10

# --------------------------------------------------------------------------
# Decisions. Strings, so a CSV and a fact can both carry them.
# --------------------------------------------------------------------------

NEGATE_ZERO = "NEGATE (zero conversions at or above the spend floor)"
NEGATE_OVER_CPA = "NEGATE (converts, assumed-value CPA above the cut multiple)"
HOLD_THIN_CLICKS = "HOLD (spend clears the floor, clicks do not - zero not trusted)"
KEEP_BELOW_FLOOR = "KEEP (spend below the floor)"
KEEP_CONVERTING = "KEEP (converts within the cut multiple)"
KEEP_CONVERTS_ELSEWHERE = "KEEP (this term converts elsewhere in the account)"
KEEP_CONVERTED_90D = "KEEP (this term converted inside the trailing 90 days)"
NOT_ELIGIBLE_ADDED = "NOT ELIGIBLE (status ADDED - this term is a keyword)"
NOT_ELIGIBLE_EXCLUDED = "NOT ELIGIBLE (status EXCLUDED - already negative)"

#: Only these two decisions may reach the CSV.
CSV_DECISIONS = (NEGATE_ZERO, NEGATE_OVER_CPA)

CATEGORIES = (
    "wrong intent",
    "competitor brand",
    "research-only",
    "too generic",
    "off-topic",
    "geographic mismatch",
    "uncategorised",
)


class BuildError(pulls.BuildError):
    """A raw pull or a declared scope failed a check. Write nothing.

    Subclasses `pulls.BuildError` so the shared `pulls.ProblemLog` collector
    catches it during the consolidated validation pass.
    """


class TruncatedPullError(BuildError):
    """A saved pull is capped, so every total computed from it is a floor."""


class MissingThresholdError(BuildError):
    """The account owner's thresholds were not supplied. Refuse to decide."""


# --------------------------------------------------------------------------
# Thresholds - required inputs, no defaults
# --------------------------------------------------------------------------

#: Each entry is (attribute, CLI flag, what it decides). The text is the refusal
#: message, so it says what the script needs rather than that it is missing.
THRESHOLD_SPEC = (
    (
        "min_spend_cad",
        "--min-spend-cad",
        "minimum 30-day spend, CAD, before a zero-conversion term may be "
        "negated at all",
    ),
    (
        "min_clicks",
        "--min-clicks",
        "minimum clicks on a term before its zero is trusted. Below this the "
        "term is HELD, not negated: the account really does hold terms at CAD "
        "400+ on 2 clicks",
    ),
    (
        "target_cpa_cad",
        "--target-cpa-cad",
        "the target CPA, CAD, on the 8 actions. Required because NO Search "
        "campaign in this account carries campaign.target_cpa - only Demand Gen "
        "does - so a multiple alone has nothing to multiply",
    ),
    (
        "target_cpa_multiple",
        "--target-cpa-multiple",
        "how many times the target CPA a converting term may reach before it is "
        "cut anyway",
    ),
)


@dataclass(frozen=True)
class Thresholds:
    """The account owner's four numbers. Nothing here has a default."""

    min_spend_cad: Decimal
    min_clicks: int
    target_cpa_cad: Decimal
    target_cpa_multiple: Decimal

    @property
    def cut_cpa_cad(self) -> Decimal:
        return self.target_cpa_cad * self.target_cpa_multiple


def require_thresholds(supplied: dict) -> Thresholds:
    """Build `Thresholds` or raise, naming every parameter still needed.

    The refusal is the point. These are the account owner's numbers (captured by the account-setup skill), and a
    run that picks its own ships an unstated subset of the terms it applied to,
    which has already happened once in this pack.
    """
    missing = [
        (flag, why) for attr, flag, why in THRESHOLD_SPEC if supplied.get(attr) is None
    ]
    if missing:
        lines = [
            "REFUSING to decide anything. The negation thresholds are the "
            "account owner's to set, not this script's, and none of them has a "
            "default (account-setup: thresholds).",
            f"{len(missing)} of {len(THRESHOLD_SPEC)} required parameter(s) were "
            "not supplied:",
        ]
        lines += [f"  {flag}  -  {why}" for flag, why in missing]
        lines.append(
            "Supply all four, or report that the run is blocked on them. Do not "
            "invent one: a run that did shipped an unstated subset."
        )
        raise MissingThresholdError("\n".join(lines))

    negative = [
        flag
        for attr, flag, _ in THRESHOLD_SPEC
        if Decimal(str(supplied[attr])) < 0
    ]
    if negative:
        raise MissingThresholdError(
            f"these thresholds are negative, which has no reading: "
            f"{', '.join(negative)}"
        )
    if Decimal(str(supplied["target_cpa_multiple"])) <= 0:
        raise MissingThresholdError(
            "--target-cpa-multiple must be greater than 0; at 0 every "
            "converting term is cut."
        )
    return Thresholds(
        min_spend_cad=Decimal(str(supplied["min_spend_cad"])),
        min_clicks=int(supplied["min_clicks"]),
        target_cpa_cad=Decimal(str(supplied["target_cpa_cad"])),
        target_cpa_multiple=Decimal(str(supplied["target_cpa_multiple"])),
    )


# --------------------------------------------------------------------------
# Loading, with the truncation refusal
# --------------------------------------------------------------------------


def read_payload(raw_dir: Path, filename: str) -> dict:
    path = raw_dir / filename
    if not path.exists():
        raise BuildError(f"{filename}: missing from {raw_dir}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("success", True):
        raise BuildError(f"{filename}: the saved pull records success=false")
    return payload


def assert_not_truncated(
    filename: str, payload: dict, declared_truncated: "bool | None" = None
) -> str:
    """Refuse a capped pull. Three independent tests, any one of them fatal.

    Verified 2026-08-20 on this account: the cost query without
    `metrics.cost_micros > 0` returns **exactly 10,000 rows with a
    `nextPageToken`**. Nothing in the response says "truncated"; the row count
    and the token are the whole signal. Adding the clause brings it to 1,681
    rows and no token.
    """
    data = payload.get("data") or {}
    rows = data.get("results") or []
    token = data.get("nextPageToken") or payload.get("nextPageToken")
    if token:
        raise TruncatedPullError(
            f"{filename}: the saved pull carries a nextPageToken, so it is one "
            f"page of a larger result set ({len(rows)} rows saved). Every total "
            "computed from it is a floor, not a value. Re-pull with "
            "`metrics.cost_micros > 0` (which brought this query from 10,000 "
            "rows to 1,681 on 2026-08-20), or paginate until the token is "
            "absent. Refusing to write a facts file."
        )
    if len(rows) == PAGE_CAP_ROWS:
        raise TruncatedPullError(
            f"{filename}: exactly {PAGE_CAP_ROWS} rows. A pull landing exactly "
            "on the page cap is capped - the page token is simply missing from "
            "what was saved. Narrow the query (`metrics.cost_micros > 0`) or "
            "paginate, then re-save. Refusing to write a facts file."
        )
    if declared_truncated is True:
        raise TruncatedPullError(
            f"{filename}: _manifest.json declares truncated=true. A capped pull "
            "cannot back a total. Refusing to write a facts file."
        )
    if declared_truncated is False and (token or len(rows) == PAGE_CAP_ROWS):
        raise TruncatedPullError(
            f"{filename}: _manifest.json declares truncated=false but the saved "
            "response says otherwise. The manifest is stale."
        )
    return (
        f"{filename}: {len(rows)} rows, no nextPageToken, not on the "
        f"{PAGE_CAP_ROWS}-row page cap"
    )


def results(raw_dir: Path, filename: str, manifest: S.Manifest) -> list:
    payload = read_payload(raw_dir, filename)
    entry = manifest.files.get(filename) or {}
    assert_not_truncated(filename, payload, entry.get("truncated"))
    rows = (payload.get("data") or {}).get("results")
    if not rows:
        raise BuildError(f"{filename}: zero rows")
    return rows


# --------------------------------------------------------------------------
# Row keys (VF-19)
# --------------------------------------------------------------------------


def term_key(row: dict) -> tuple:
    """`(campaign.id, ad_group.id, search_term, keyword text)`.

    The four-part key both pulls share. Match type is deliberately absent: the
    conversions pull does not select `segments.keyword.info.match_type`, so a
    five-part key could never join.
    """
    info = ((row.get("segments") or {}).get("keyword") or {}).get("info") or {}
    return (
        str((row.get("campaign") or {}).get("id") or ""),
        str((row.get("adGroup") or {}).get("id") or ""),
        (row.get("searchTermView") or {}).get("searchTerm") or "",
        info.get("text") or "",
    )


def criterion_pair(row: dict) -> "tuple | None":
    """`(ad_group.id, criterion_id)` from `segments.keyword.ad_group_criterion`.

    The pair, never the bare criterion id. In this pull 39 of 162 criterion ids
    sit under more than one ad group.
    """
    resource = ((row.get("segments") or {}).get("keyword") or {}).get(
        "adGroupCriterion"
    ) or ""
    tail = resource.rsplit("/", 1)[-1]
    if "~" not in tail:
        return None
    ag_id, criterion_id = tail.split("~", 1)
    return (ag_id, criterion_id)


# --------------------------------------------------------------------------
# The decision. Numbers only.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Numbers:
    """Everything `decide()` is allowed to see.

    No search term, no keyword text, no campaign name, no category, no theme.
    If a field cannot be read off a metrics column or a status enum, it does not
    belong here, and that is what makes the decision reproducible.
    """

    cost_cad: Decimal
    clicks: int
    conversions_8action: Decimal
    assumed_value: Decimal
    term_account_conversions_8action: Decimal
    status: str
    #: Did this term text convert on the 8 actions inside the trailing 90 days?
    #: A boolean, not a count: the 90-day pull answers "has it ever converted
    #: recently", and any non-zero answer is a full stop on negating it.
    converted_in_90d: bool = False


def decide(n: Numbers, t: Thresholds) -> str:
    """Negate or keep, from the numbers alone.

    Order matters and each branch is a number or an enum:

    1. `status` gates eligibility. `ADDED` means the term is already a keyword,
       so negating it would negate our own targeting - `rivalco app` is the live
       case, CAD 512.30 and zero conversions, inside the Conquesting campaign
       whose whole purpose it is. `EXCLUDED` is already negative.
    2. A term converting anywhere in the account is never negated from one row.
       The pack measured this: a `(campaign, term)`-scoped safety join passed
       clean while 38 CSV rows / 20 terms / CAD 1,042.10 were about to be
       negated in one campaign while converting on the 8 actions in another.
       Ownership fixes are the overlap section's job, never the CSV's.
    3. A converting row is cut only when its assumed-value CPA exceeds
       `target_cpa_cad * target_cpa_multiple`.
    4. A zero needs both the spend floor and the click floor. Spend without
       clicks is HELD, not negated - the zero is not yet trustworthy.
    """
    if n.status == "ADDED":
        return NOT_ELIGIBLE_ADDED
    if n.status == "EXCLUDED":
        return NOT_ELIGIBLE_EXCLUDED
    if n.conversions_8action > 0:
        cpa = n.cost_cad / n.conversions_8action
        if cpa > t.cut_cpa_cad:
            return NEGATE_OVER_CPA
        return KEEP_CONVERTING
    if n.converted_in_90d:
        return KEEP_CONVERTED_90D
    if n.term_account_conversions_8action > 0:
        return KEEP_CONVERTS_ELSEWHERE
    if n.cost_cad < t.min_spend_cad:
        return KEEP_BELOW_FLOOR
    if n.clicks < t.min_clicks:
        return HOLD_THIN_CLICKS
    return NEGATE_ZERO


def why(n: Numbers, t: Thresholds, decision: str) -> str:
    """The one-line reason, in numbers. Never a category, never a theme."""
    if decision == NOT_ELIGIBLE_ADDED:
        return "status ADDED: this query is already a keyword here"
    if decision == NOT_ELIGIBLE_EXCLUDED:
        return "status EXCLUDED: already a negative here"
    if decision == NEGATE_OVER_CPA:
        cpa = money(n.cost_cad / n.conversions_8action)
        return (
            f"assumed-value CPA CAD {cpa} on {n.conversions_8action} conversions "
            f"exceeds the cut line of CAD {money(t.cut_cpa_cad)} "
            f"({t.target_cpa_multiple}x target CAD {money(t.target_cpa_cad)})"
        )
    if decision == KEEP_CONVERTING:
        cpa = money(n.cost_cad / n.conversions_8action)
        return (
            f"assumed-value CPA CAD {cpa} on {n.conversions_8action} conversions "
            f"is within the cut line of CAD {money(t.cut_cpa_cad)}"
        )
    if decision == KEEP_CONVERTED_90D:
        return (
            "zero conversions in the 30-day window, but this query converted on "
            "the 8 actions inside the trailing 90 days, so negating it would "
            "block a proven converter. A 30-day window cannot see this"
        )
    if decision == KEEP_CONVERTS_ELSEWHERE:
        return (
            f"zero conversions on this row, but this query has "
            f"{n.term_account_conversions_8action} on the 8 actions elsewhere in "
            "the account, so a negative here would suppress a converter"
        )
    if decision == KEEP_BELOW_FLOOR:
        return (
            f"CAD {money(n.cost_cad)} is below the CAD {money(t.min_spend_cad)} "
            "spend floor"
        )
    if decision == HOLD_THIN_CLICKS:
        return (
            f"CAD {money(n.cost_cad)} clears the spend floor but "
            f"{n.clicks} clicks is under the {t.min_clicks}-click floor, so the "
            "zero is not yet trustworthy"
        )
    return (
        f"CAD {money(n.cost_cad)} and {n.clicks} clicks both clear their floors "
        f"with zero conversions on the 8 actions in 30 days"
    )


# --------------------------------------------------------------------------
# The category. A label attached afterwards, never an input.
# --------------------------------------------------------------------------

#: Substring tests, in order. A category is a description a reader can scan; it
#: is written to `category` and read by nothing.
#: Whole words, not substrings. `free` as a substring also labels `freelance`,
#: and a visibly wrong label on `free invoicing app for freelancers` - the
#: account owner's own named example - would make a reader distrust the whole
#: column even though the column decides nothing.
CATEGORY_TESTS = (
    ("wrong intent", ("free", "job", "jobs", "career", "careers", "salary",
                      "cheap", "diy")),
    ("research-only", ("what is", "how to", "how does", "definition", "meaning",
                       "difference between", "vs", "example", "examples",
                       "tutorial")),
    ("geographic mismatch", ("india", "philippines", "nigeria", "pakistan",
                             "uae", "dubai", "kenya")),
)


def categorise(search_term: str, keyword_text: str) -> str:
    """A human-readable label for one term. Runs after every decision is fixed.

    This function is the whole of the old skill's negate/keep logic, demoted.
    The account owner rejected it as the reason - a term can look off-topic and
    still be a real buyer - and it is kept only because a
    reader scanning 200 rows wants a word beside each one.

    It must never be called before `decide()` and its output must never be
    passed to `decide()`. `test_mine_search_terms_facts.py` permutes every
    category and asserts the decisions are byte-identical.
    """
    words = search_term.lower().split()
    haystack = " ".join(words)
    for label, needles in CATEGORY_TESTS:
        for needle in needles:
            if " " in needle:
                if needle in haystack:
                    return label
            elif needle in words:
                return label
    if search_term.strip().lower() == keyword_text.strip().lower():
        return "too generic"
    if len(search_term.split()) <= 1:
        return "too generic"
    return "uncategorised"


# --------------------------------------------------------------------------
# Assembling the rows
# --------------------------------------------------------------------------


@dataclass
class TermRow:
    """One joined `(campaign, ad group, term, keyword)` row."""

    campaign_id: str
    campaign_name: str
    ad_group_id: str
    ad_group_name: str
    search_term: str
    keyword_text: str
    match_type: str
    status: str
    cost_cad: Decimal
    clicks: int
    impressions: int
    conversions: Decimal
    assumed_value: Decimal
    criterion: "tuple | None"
    decision: str = ""
    reason: str = ""
    category: str = ""
    learning_note: str = ""

    @property
    def key(self) -> tuple:
        return (self.campaign_id, self.ad_group_id, self.search_term, self.keyword_text)

    @property
    def cpa(self) -> "Decimal | None":
        if self.conversions <= 0:
            return None
        return self.cost_cad / self.conversions

    @property
    def roas(self) -> "Decimal | None":
        if self.cost_cad <= 0:
            return None
        return self.assumed_value / self.cost_cad


def conversions_by_term_key(rows: list) -> dict:
    """Aggregate the 8-action pull per four-part term key.

    63 distinct keys over 79 rows on 2026-08-20: the pull is segmented by
    conversion action, so summing without aggregating first double counts.
    """
    out: dict = collections.defaultdict(lambda: [Decimal(0), Decimal(0)])
    for row in rows:
        metrics = row.get("metrics") or {}
        acc = out[term_key(row)]
        acc[0] += Decimal(str(metrics.get("conversions") or 0))
        acc[1] += Decimal(str(metrics.get("conversionsValue") or 0))
    return dict(out)


def build_rows(
    cost_rows: list, conv_rows: list, campaign_names: dict
) -> "tuple[list, dict]":
    per_key = conversions_by_term_key(conv_rows)
    rows = []
    for raw in cost_rows:
        key = term_key(raw)
        metrics = raw.get("metrics") or {}
        info = ((raw.get("segments") or {}).get("keyword") or {}).get("info") or {}
        conv, value = per_key.get(key, [Decimal(0), Decimal(0)])
        rows.append(
            TermRow(
                campaign_id=key[0],
                campaign_name=campaign_names.get(key[0], f"<unknown {key[0]}>"),
                ad_group_id=key[1],
                ad_group_name=(raw.get("adGroup") or {}).get("name") or "",
                search_term=key[2],
                keyword_text=key[3],
                match_type=info.get("matchType") or "",
                status=(raw.get("searchTermView") or {}).get("status") or "",
                cost_cad=micros_to_currency(metrics.get("costMicros")) or Decimal(0),
                clicks=int(metrics.get("clicks") or 0),
                impressions=int(metrics.get("impressions") or 0),
                conversions=conv,
                assumed_value=value,
                criterion=criterion_pair(raw),
            )
        )
    unmatched = {k: v for k, v in per_key.items() if k not in {r.key for r in rows}}
    return rows, unmatched


def account_conversions_by_term_text(rows: list) -> dict:
    """8-action conversions per search-term **text**, across every campaign.

    The do-not-negate check is term-scoped, not `(campaign, term)`-scoped. The
    narrower join under-protects across campaigns, measured: 38 CSV rows / 20
    terms / CAD 1,042.10.
    """
    out: dict = collections.defaultdict(Decimal)
    for row in rows:
        out[row.search_term] += row.conversions
    return dict(out)


def converted_in_90d_terms(payload: "dict | None") -> set:
    """Search-term texts that converted on the 8 actions in the trailing 90 days.

    Term-scoped, lowercased, for the same reason
    `account_conversions_by_term_text` is: the do-not-negate rule is about the
    query, not about one campaign's slice of it.
    """
    terms: set = set()
    if not payload:
        return terms
    for row in (payload.get("data") or {}).get("results") or []:
        term = ((row.get("searchTermView") or {}).get("searchTerm") or "").strip()
        try:
            conversions = Decimal(str(row.get("metrics", {}).get("conversions") or 0))
        except Exception:
            conversions = Decimal(0)
        if term and conversions > 0:
            terms.add(term.lower())
    return terms


def apply_decisions(
    rows: list, thresholds: Thresholds, converted_90d: "set | None" = None
) -> None:
    """Decide every row from numbers, then attach a category. In that order."""
    account = account_conversions_by_term_text(rows)
    converted_90d = converted_90d or set()
    for row in rows:
        numbers = Numbers(
            cost_cad=row.cost_cad,
            clicks=row.clicks,
            conversions_8action=row.conversions,
            assumed_value=row.assumed_value,
            term_account_conversions_8action=account.get(row.search_term, Decimal(0)),
            status=row.status,
            converted_in_90d=row.search_term.strip().lower() in converted_90d,
        )
        row.decision = decide(numbers, thresholds)
        row.reason = why(numbers, thresholds, row.decision)
    # Second pass. Nothing below may write to `decision` or `reason`.
    for row in rows:
        row.category = categorise(row.search_term, row.keyword_text)


# --------------------------------------------------------------------------
# Learning windows (campaign-guardrails rule 7, scoped)
# --------------------------------------------------------------------------


def criterion_change_scope(change_rows: list) -> "tuple[set, set, dict]":
    """Ad groups and campaigns whose criteria changed, with the latest date.

    Rule 7 says match the change type to the claim type. A negation rests on one
    term's 30-day cost and conversions inside one ad group, so the change that
    could plausibly be its cause is a change to that ad group's or campaign's
    **criteria** - a keyword or negative added or removed resets which queries
    the term could match. A `CAMPAIGN_BUDGET` amount change is not that: it moves
    volume, which the click floor already governs.

    The unscoped reading is not conservative, it is silence. Every campaign
    holding search-term spend had a budget or campaign change inside the 14 days,
    so "downgrade every performance finding" downgrades CAD 47,536.45 of
    CAD 47,536.45 and says nothing.
    """
    ad_groups: set = set()
    campaigns: set = set()
    detail: dict = {}
    for raw in change_rows:
        event = raw.get("changeEvent") or {}
        kind = event.get("changeResourceType")
        if kind not in ("AD_GROUP_CRITERION", "CAMPAIGN_CRITERION"):
            continue
        when = (event.get("changeDateTime") or "")[:10]
        ag = (event.get("adGroup") or "").rsplit("/", 1)[-1]
        cp = (event.get("campaign") or "").rsplit("/", 1)[-1]
        if kind == "AD_GROUP_CRITERION" and ag:
            ad_groups.add(ag)
            key = ("ad_group", ag)
        elif kind == "CAMPAIGN_CRITERION" and cp:
            campaigns.add(cp)
            key = ("campaign", cp)
        else:
            continue
        if when > detail.get(key, ("", ""))[0]:
            detail[key] = (when, kind)
    return ad_groups, campaigns, detail


def apply_learning_windows(rows: list, change_rows: list) -> dict:
    """Downgrade a negation whose ad group or campaign had a criterion change.

    Rule 7 requires both the date and the change type on every downgrade, so the
    note carries both.
    """
    ad_groups, campaigns, detail = criterion_change_scope(change_rows)
    downgraded = 0
    spend = Decimal(0)
    for row in rows:
        hit = detail.get(("ad_group", row.ad_group_id)) or detail.get(
            ("campaign", row.campaign_id)
        )
        if not hit:
            continue
        when, kind = hit
        row.learning_note = (
            f"monitor - in learning: criteria changed {when} ({kind})"
        )
        if row.decision in CSV_DECISIONS:
            downgraded += 1
            spend += row.cost_cad
    return {
        "ad_groups": len(ad_groups),
        "campaigns": len(campaigns),
        "downgraded": downgraded,
        "downgraded_spend": spend,
        "scoped_spend": sum(
            (r.cost_cad for r in rows if r.learning_note), Decimal(0)
        ),
    }


# --------------------------------------------------------------------------
# Overlap, keyword ideas, and the CSV
# --------------------------------------------------------------------------


def overlap(rows: list) -> dict:
    """Search-term texts serving from more than one campaign.

    This is the mechanical form of `campaign-guardrails` rule 2's diagnostic. It
    is a which-campaign-should-own-it finding, never a negate/keep decision, and
    it never reaches the CSV: a routing negative only works paired with adding
    the term to the campaign that should own it, and a one-row-per-negative file
    cannot express the second half.
    """
    by_term: dict = collections.defaultdict(set)
    for row in rows:
        by_term[row.search_term].add(row.campaign_id)
    multi = {t for t, camps in by_term.items() if len(camps) > 1}
    brand_side = {
        t
        for t in multi
        if by_term[t] & BRAND_CORE_CAMPAIGN_IDS and by_term[t] - BRAND_CORE_CAMPAIGN_IDS
    }
    return {
        "terms": multi,
        "spend": sum((r.cost_cad for r in rows if r.search_term in multi), Decimal(0)),
        "brand_terms": brand_side,
        "brand_spend": sum(
            (r.cost_cad for r in rows if r.search_term in brand_side), Decimal(0)
        ),
    }


def is_branded(search_term: str) -> bool:
    """Brand-token test. Used for the keyword-idea split and nothing else."""
    text = search_term.lower()
    return any(token in text for token in BRAND_TOKENS)


def keyword_ideas(rows: list) -> dict:
    """Converting terms that are not yet keywords: status NONE with conversions.

    The ideas come from the account, never from Keyword Planner. The planner is
    reachable (`customers generate-keyword-ideas`) and its raw output is
    unusable here - a broad category seed can return keywords from unrelated
    industries, and a site seed can return generic high-volume terms. It sizes a term. It never picks one.

    Split branded from non-branded, because `campaign-guardrails` rule 2 forbids
    scaling Brand: a branded idea is Brand housekeeping, not growth.
    """
    ideas = [r for r in rows if r.status == "NONE" and r.conversions > 0]
    branded = [r for r in ideas if is_branded(r.search_term)]
    non_branded = [r for r in ideas if not is_branded(r.search_term)]
    return {
        "all": ideas,
        "branded": branded,
        "non_branded": non_branded,
        "value": sum((r.assumed_value for r in ideas), Decimal(0)),
        "non_branded_value": sum((r.assumed_value for r in non_branded), Decimal(0)),
    }


#: The CSV header. Chosen, and **unverified against Google Ads Editor** - the
#: pack has never confirmed an import. Three columns exist because a prior run
#: shipped the file without them: the level the negatives apply at, the 8-action
#: label on the conversions column (the old file shipped the unrestricted ~20
#: action count beside a negate recommendation), and whether the existing
#: negative inventory was checked.
CSV_HEADER = (
    "Level",
    "Campaign",
    "Campaign ID",
    "Ad Group",
    "Ad Group ID",
    "Keyword",
    "Match Type",
    "Search Term",
    "Triggering Keyword",
    "Triggering Match Type",
    "Clicks",
    "Impressions",
    "Cost (CAD)",
    "Conversions (8 actions only)",
    "Assumed Value (CAD)",
    "Decision",
    "Why (numbers)",
    "Category (label only)",
    "Existing Negative Check",
)

CSV_LEVEL = "ad group"

#: Google Ads Editor imports negatives by ad group **name**, not id, and neither
#: search-term pull selects `ad_group.name`. Emitted verbatim so the gap is
#: visible in the file instead of being guessed at.
AD_GROUP_NAME_MISSING = "<ad_group.name not selected by the pull - re-pull>"

#: One negative is one `(campaign, ad group, keyword text, match type)`. Two
#: triggering keywords in one ad group produce two rows of the same negative, and
#: importing that adds the same negative twice.
CSV_IDENTITY = ("Campaign ID", "Ad Group ID", "Keyword", "Match Type")


#: A negative blocks a search term differently depending on its match type.
#: EXACT blocks only the identical text; PHRASE blocks any term containing the
#: phrase as a contiguous run of words; BROAD blocks any term containing all of
#: its words in any order. Anything else is reported as unrecognised rather than
#: guessed at, because guessing wrong here silently under-reports coverage.
def _covers(neg_text: str, neg_match: str, term: str) -> bool:
    neg_text = (neg_text or "").strip().lower()
    term = (term or "").strip().lower()
    if not neg_text or not term:
        return False
    match = (neg_match or "").strip().upper()
    if match == "EXACT":
        return neg_text == term
    neg_words = neg_text.split()
    term_words = term.split()
    if match == "PHRASE":
        n = len(neg_words)
        return any(
            term_words[i : i + n] == neg_words
            for i in range(len(term_words) - n + 1)
        )
    if match == "BROAD":
        return set(neg_words) <= set(term_words)
    return False


def existing_negatives(payloads: list) -> dict:
    """Index the account's current negative-keyword inventory.

    Accepts the raw payloads of a `campaign_criterion` pull, an
    `ad_group_criterion` pull, or both. Returns
    `{"campaign": {campaign_id: [(text, match_type)]},
      "ad_group": {(campaign_id, ad_group_id): [(text, match_type)]},
      "rows": int, "unrecognised_match_types": set}`.

    Row keys are the `(parent, child)` pair per VF-19, because this account
    reuses ad group ids across campaigns.
    """
    index: dict = {
        "campaign": collections.defaultdict(list),
        "ad_group": collections.defaultdict(list),
        "rows": 0,
        "unrecognised_match_types": set(),
    }
    for payload in payloads:
        for row in (payload.get("data") or {}).get("results") or []:
            campaign_id = ((row.get("campaign") or {}).get("id")) or ""
            crit = row.get("adGroupCriterion") or row.get("campaignCriterion") or {}
            if not crit.get("negative", True):
                continue
            keyword = crit.get("keyword") or {}
            text = keyword.get("text")
            match = keyword.get("matchType")
            if not text:
                continue
            if (match or "").strip().upper() not in ("EXACT", "PHRASE", "BROAD"):
                index["unrecognised_match_types"].add(match)
            index["rows"] += 1
            if "adGroupCriterion" in row:
                ad_group_id = ((row.get("adGroup") or {}).get("id")) or ""
                index["ad_group"][(campaign_id, ad_group_id)].append((text, match))
            else:
                index["campaign"][campaign_id].append((text, match))
    return index


def existing_coverage(row, index: "dict | None") -> "str | None":
    """Why `row`'s negative is already covered, or None if it is not.

    Checks, in the order a reader would care about: the same ad group, then the
    campaign level (which applies to every ad group in the campaign). An
    ad-group negative on a *different* ad group does not cover this row, so it
    is deliberately not consulted.
    """
    if not index:
        return None
    for text, match in index["ad_group"].get((row.campaign_id, row.ad_group_id), ()):
        if _covers(text, match, row.search_term):
            return (
                f"ALREADY NEGATED at this ad group: {match} negative "
                f"{text!r} covers it"
            )
    for text, match in index["campaign"].get(row.campaign_id, ()):
        if _covers(text, match, row.search_term):
            return (
                f"ALREADY NEGATED at campaign level: {match} negative "
                f"{text!r} covers it"
            )
    return None


def csv_rows(rows: list, index: "dict | None") -> "tuple[list, int, list]":
    """Every row the CSV carries, deduplicated. Returns `(rows, collapsed, covered)`.

    Waste negatives only, one level, one conversion basis. Ownership and routing
    fixes are deliberately absent: a routing negative only works paired with
    adding the term to the campaign that should own it, and a
    one-row-per-negative file cannot express the second half, so a reader
    importing it applies half the fix.

    `index` is the output of `existing_negatives()`, or None if no inventory was
    supplied. **When it is supplied every candidate is compared against it and
    the covered ones are removed**, returned as `covered` so the caller can
    report them rather than lose them. Passing None labels every row `NOT RUN`.
    """
    check = (
        "checked against the supplied negative inventory: not already covered"
        if index
        else "NOT RUN - existing negative inventory was not supplied"
    )
    merged: dict = {}
    order: list = []
    collapsed = 0
    covered: list = []
    for row in sorted(rows, key=lambda r: (-r.cost_cad, r.search_term)):
        if row.decision not in CSV_DECISIONS or row.learning_note:
            continue
        why_covered = existing_coverage(row, index)
        if why_covered:
            covered.append((row, why_covered))
            continue
        identity = (row.campaign_id, row.ad_group_id, row.search_term, "Exact")
        if identity in merged:
            existing = merged[identity]
            existing["Triggering Keyword"] += f" | {row.keyword_text}"
            existing["Triggering Match Type"] += f" | {row.match_type}"
            existing["Clicks"] += row.clicks
            existing["Impressions"] += row.impressions
            existing["Cost (CAD)"] = str(
                money(Decimal(existing["Cost (CAD)"]) + row.cost_cad)
            )
            collapsed += 1
            continue
        merged[identity] = {
            "Level": CSV_LEVEL,
            "Campaign": row.campaign_name,
            "Campaign ID": row.campaign_id,
            "Ad Group": row.ad_group_name or AD_GROUP_NAME_MISSING,
            "Ad Group ID": row.ad_group_id,
            "Keyword": row.search_term,
            "Match Type": "Exact",
            "Search Term": row.search_term,
            "Triggering Keyword": row.keyword_text,
            "Triggering Match Type": row.match_type,
            "Clicks": row.clicks,
            "Impressions": row.impressions,
            "Cost (CAD)": str(money(row.cost_cad)),
            "Conversions (8 actions only)": str(row.conversions),
            "Assumed Value (CAD)": str(money(row.assumed_value)),
            "Decision": row.decision,
            "Why (numbers)": row.reason,
            "Category (label only)": row.category,
            "Existing Negative Check": check,
        }
        order.append(identity)
    return [merged[i] for i in order], collapsed, covered


def lint_csv(rows: list) -> dict:
    """Editor-import lint. Reports counts; never silently drops a row."""
    identities = collections.Counter(
        tuple(r[c] for c in CSV_IDENTITY) for r in rows
    )
    return {
        "over_length": [r for r in rows if len(r["Keyword"]) > KEYWORD_MAX_CHARS],
        "quote_char": [
            r for r in rows if '"' in r["Keyword"] or "'" in r["Keyword"]
        ],
        "comma_in_keyword": [r for r in rows if "," in r["Keyword"]],
        "wrong_width": [r for r in rows if len(r) != len(CSV_HEADER)],
        "missing_column": [
            r for r in rows if set(r) != set(CSV_HEADER)
        ],
        "duplicate_identity": [
            r
            for r in rows
            if identities[tuple(r[c] for c in CSV_IDENTITY)] > 1
        ],
        "missing_ad_group_name": [
            r for r in rows if r["Ad Group"] == AD_GROUP_NAME_MISSING
        ],
    }


def write_csv(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(CSV_HEADER), quoting=csv.QUOTE_MINIMAL
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


# --------------------------------------------------------------------------
# Facts
# --------------------------------------------------------------------------


def add_pull_facts(
    fs: FactSet, rows: list, cost_window: str, conv_window: str, unmatched: dict
) -> None:
    keys = [r.key for r in rows]
    fs.add(
        "st.pull.cost.row.count",
        len(rows),
        "count",
        COST_SCOPE,
        cost_window,
        COST,
        "n/a",
    )
    fs.add(
        "st.pull.cost.distinct_term_key.count",
        len(set(keys)),
        "count",
        f"{COST_SCOPE}, keyed on {TERM_KEY}",
        cost_window,
        COST,
        "n/a",
    )
    with_match = {
        (r.campaign_id, r.ad_group_id, r.search_term, r.keyword_text, r.match_type)
        for r in rows
    }
    fs.add(
        "st.pull.cost.distinct_term_key_with_match_type.count",
        len(with_match),
        "count",
        f"{COST_SCOPE}, keyed on {TERM_KEY} plus keyword match type",
        cost_window,
        COST,
        "n/a",
    )
    fs.add(
        "st.pull.conversions.unjoined_key.count",
        len(unmatched),
        "count",
        (
            f"term keys present in {CONVERSIONS} and absent from {COST}. Join "
            "completeness, not a metric"
        ),
        conv_window,
        CONVERSIONS,
        "8-action",
    )
    anomalies = [r for r in rows if r.clicks > r.impressions]
    fs.add(
        "st.pull.cost.clicks_exceed_impressions.row.count",
        len(anomalies),
        "count",
        (
            f"{COST_SCOPE}. Rows where metrics.clicks > metrics.impressions, "
            "which is impossible. search_term_view segmented by keyword returns "
            "these, so no CTR may be computed from this pull"
        ),
        cost_window,
        COST,
        "n/a",
    )
    fs.add(
        "st.pull.cost.clicks_exceed_impressions.spend.total",
        money(sum((r.cost_cad for r in anomalies), Decimal(0))),
        "currency_cad",
        f"{COST_SCOPE} AND metrics.clicks > metrics.impressions",
        cost_window,
        COST,
        "n/a",
    )


def add_row_key_facts(fs: FactSet, rows: list, cost_window: str) -> None:
    """VF-19, demonstrated in this run's own pull rather than quoted."""
    pairs = {r.criterion for r in rows if r.criterion}
    by_criterion: dict = collections.defaultdict(set)
    for ag_id, criterion_id in pairs:
        by_criterion[criterion_id].add(ag_id)
    multi = {c: ags for c, ags in by_criterion.items() if len(ags) > 1}
    scope = f"{COST_SCOPE}, keyed on {KEYWORD_KEY}"
    fs.add(
        "st.keyword.distinct_criterion_id.count",
        len(by_criterion),
        "count",
        scope,
        cost_window,
        COST,
        "n/a",
    )
    fs.add(
        "st.keyword.criterion_id_under_multiple_ad_groups.count",
        len(multi),
        "count",
        (
            scope
            + ". A criterion id appearing under more than one ad group makes a "
            "bare-id key order dependent (VF-19)"
        ),
        cost_window,
        COST,
        "n/a",
    )
    fs.add(
        "st.keyword.criterion_id.max_ad_groups",
        max((len(a) for a in by_criterion.values()), default=0),
        "count",
        scope,
        cost_window,
        COST,
        "n/a",
    )


def add_threshold_facts(fs: FactSet, t: Thresholds, window: str) -> None:
    provenance = (
        "supplied by the account owner as a required input. This script has no "
        "default and refuses to run without it (account-setup: thresholds)"
    )
    fs.add(
        "st.threshold.min_spend_cad",
        money(t.min_spend_cad),
        "currency_cad",
        provenance,
        window,
        "operator input",
        "n/a",
    )
    fs.add(
        "st.threshold.min_clicks",
        t.min_clicks,
        "count",
        provenance,
        window,
        "operator input",
        "n/a",
    )
    fs.add(
        "st.threshold.target_cpa_cad",
        money(t.target_cpa_cad),
        "currency_cad",
        provenance
        + ". Required as a value, not just a multiple: no Search campaign in "
        "this account carries campaign.target_cpa. Compared against ASSUMED-VALUE "
        "CPA on the 8 actions, never against revenue (VF-8)",
        window,
        "operator input",
        "8-action",
    )
    fs.add(
        "st.threshold.target_cpa_multiple",
        money(t.target_cpa_multiple),
        "ratio_multiple",
        provenance,
        window,
        "operator input",
        "n/a",
    )
    fs.add(
        "st.threshold.cut_cpa_cad",
        money(t.cut_cpa_cad),
        "currency_cad",
        "target_cpa_cad * target_cpa_multiple, both operator inputs. The line an "
        "ASSUMED-VALUE CPA must exceed before a converting term is cut (VF-8)",
        window,
        "operator input",
        "8-action",
    )


def add_totals(fs: FactSet, rows: list, cost_window: str, conv_window: str) -> None:
    fs.add(
        "st.spend.total",
        money(sum((r.cost_cad for r in rows), Decimal(0))),
        "currency_cad",
        COST_SCOPE,
        cost_window,
        COST,
        "n/a",
    )
    fs.add(
        "st.term_text.distinct.count",
        len({r.search_term for r in rows}),
        "count",
        COST_SCOPE,
        cost_window,
        COST,
        "n/a",
    )
    fs.add(
        "st.conversions.total",
        sum((r.conversions for r in rows), Decimal(0)),
        "conversions",
        f"{COST_SCOPE} joined to {CONVERSIONS_SCOPE} on {TERM_KEY}",
        conv_window,
        CONVERSIONS,
        "8-action",
    )
    fs.add(
        "st.assumed_value.total",
        money(sum((r.assumed_value for r in rows), Decimal(0))),
        "currency_cad_assumed_value",
        (
            f"{COST_SCOPE} joined to {CONVERSIONS_SCOPE} on {TERM_KEY}. ASSUMED "
            "VALUE, not revenue: every one of the 8 actions carries "
            "always_use_default_value=True (VF-2, VF-8)"
        ),
        conv_window,
        CONVERSIONS,
        "8-action",
    )
    for label, subset in (
        ("zero_conversion", [r for r in rows if r.conversions == 0]),
        ("converting", [r for r in rows if r.conversions > 0]),
    ):
        fs.add(
            f"st.{label}.row.count",
            len(subset),
            "count",
            f"{COST_SCOPE}, split on 8-action conversions, keyed on {TERM_KEY}",
            conv_window,
            CONVERSIONS,
            "8-action",
        )
        fs.add(
            f"st.{label}.spend.total",
            money(sum((r.cost_cad for r in subset), Decimal(0))),
            "currency_cad",
            f"{COST_SCOPE}, split on 8-action conversions, keyed on {TERM_KEY}",
            conv_window,
            CONVERSIONS,
            "8-action",
        )


def add_decision_facts(fs: FactSet, rows: list, t: Thresholds, window: str) -> None:
    scope = (
        f"decided by tools/build_mine_search_terms_facts.py decide() from "
        f"numbers only, at min_spend_cad={money(t.min_spend_cad)}, "
        f"min_clicks={t.min_clicks}, cut CPA CAD {money(t.cut_cpa_cad)}. The "
        "category is attached afterwards and is never an input"
    )
    counts = collections.Counter(r.decision for r in rows)
    spends: dict = collections.defaultdict(Decimal)
    for row in rows:
        spends[row.decision] += row.cost_cad
    slugs = {
        NEGATE_ZERO: "negate_zero_conversion",
        NEGATE_OVER_CPA: "negate_over_cpa",
        HOLD_THIN_CLICKS: "hold_clicks_below_floor",
        KEEP_BELOW_FLOOR: "keep_below_spend_floor",
        KEEP_CONVERTING: "keep_converting",
        KEEP_CONVERTS_ELSEWHERE: "keep_converts_elsewhere",
        KEEP_CONVERTED_90D: "keep_converted_in_90d",
        NOT_ELIGIBLE_ADDED: "not_eligible_added",
        NOT_ELIGIBLE_EXCLUDED: "not_eligible_excluded",
    }
    for decision, slug in slugs.items():
        fs.add(
            f"st.decision.{slug}.row.count",
            counts.get(decision, 0),
            "count",
            scope,
            window,
            CONVERSIONS,
            "8-action",
        )
        fs.add(
            f"st.decision.{slug}.spend.total",
            money(spends.get(decision, Decimal(0))),
            "currency_cad",
            scope,
            window,
            CONVERSIONS,
            "8-action",
        )


def add_top_n_facts(fs: FactSet, rows: list, window: str, top_n: int) -> list:
    """The report's headline table. `top_n` rows, and no fact per term."""
    ranked = sorted(rows, key=lambda r: -r.cost_cad)[:top_n]
    fs.add(
        "st.top_table.selection_rule.top_n",
        top_n,
        "count",
        (
            f"selection rule: the {top_n} highest-spend rows of the cost pull by "
            "30-day spend, whatever their decision. Fixed here so two runs are "
            "comparable"
        ),
        window,
        COST,
        "n/a",
    )
    for rank, row in enumerate(ranked, start=1):
        base = f"st.top_table.rank_{rank}"
        scope = (
            f"one row of the cost pull, keyed on {TERM_KEY}: campaign "
            f"{row.campaign_id}, ad group {row.ad_group_id}"
        )
        fs.add(f"{base}.search_term", row.search_term, "text", scope, window, COST, "n/a")
        fs.add(f"{base}.keyword_text", row.keyword_text, "text", scope, window, COST, "n/a")
        fs.add(f"{base}.match_type", row.match_type, "text", scope, window, COST, "n/a")
        fs.add(f"{base}.campaign_name", row.campaign_name, "text", scope, window, CAMPAIGNS, "n/a")
        fs.add(f"{base}.spend", money(row.cost_cad), "currency_cad", scope, window, COST, "n/a")
        fs.add(f"{base}.clicks", row.clicks, "clicks", scope, window, COST, "n/a")
        fs.add(
            f"{base}.conversions",
            row.conversions,
            "conversions",
            scope,
            window,
            CONVERSIONS,
            "8-action",
        )
        fs.add(
            f"{base}.assumed_value_cpa",
            money(row.cpa) if row.cpa is not None else None,
            "currency_cad_assumed_value",
            scope + ". ASSUMED-VALUE CPA: cost / 8-action conversions (VF-8)",
            window,
            CONVERSIONS,
            "8-action",
        )
        fs.add(
            f"{base}.assumed_value_roas",
            money(row.roas) if row.roas is not None else None,
            "ratio_assumed_value_roas",
            scope
            + ". ASSUMED-VALUE ROAS: assigned constants for value-based bidding, "
            "never observed revenue (VF-2, VF-8)",
            window,
            CONVERSIONS,
            "8-action",
        )
        fs.add(f"{base}.decision", row.decision, "text", scope, window, CONVERSIONS, "8-action")
        fs.add(f"{base}.why", row.reason, "text", scope, window, CONVERSIONS, "8-action")
        fs.add(
            f"{base}.category",
            row.category,
            "text",
            scope + ". LABEL ONLY: attached after the decision, never an input",
            window,
            COST,
            "n/a",
        )
    return ranked


def add_overlap_facts(fs: FactSet, data: dict, window: str) -> None:
    scope = (
        f"{COST_SCOPE}, grouped by search-term text across campaigns. A "
        "which-campaign-owns-it finding, never a negate/keep decision"
    )
    fs.add("st.overlap.term_text.count", len(data["terms"]), "count", scope, window, COST, "n/a")
    fs.add(
        "st.overlap.spend.total",
        money(data["spend"]),
        "currency_cad",
        scope,
        window,
        COST,
        "n/a",
    )
    brand_scope = (
        scope
        + ". Restricted to terms serving from a Brand Core campaign AND a "
        "non-Brand campaign. Brand Core is identified by campaign id from "
        "account-conventions, never by the word Brand in the name, which also "
        "matches Non-Branded and NonBrand campaigns "
        "(campaign-guardrails rule 2)"
    )
    fs.add(
        "st.overlap.brand_core_and_non_brand.term_text.count",
        len(data["brand_terms"]),
        "count",
        brand_scope,
        window,
        COST,
        "n/a",
    )
    fs.add(
        "st.overlap.brand_core_and_non_brand.spend.total",
        money(data["brand_spend"]),
        "currency_cad",
        brand_scope,
        window,
        COST,
        "n/a",
    )


def add_learning_facts(fs: FactSet, learning: dict, rows: list, window: str) -> None:
    scope = (
        "campaign-guardrails rule 7, scoped: matched on AD_GROUP_CRITERION and "
        "CAMPAIGN_CRITERION events on the term's own ad group or campaign, "
        "because a criterion change resets which queries the term could match. "
        "A CAMPAIGN_BUDGET amount change is NOT matched to a term-level negation"
    )
    fs.add(
        "st.learning.criterion_changed.ad_group.count",
        learning["ad_groups"],
        "count",
        scope,
        window,
        CHANGE_EVENTS,
        "n/a",
    )
    fs.add(
        "st.learning.criterion_changed.campaign.count",
        learning["campaigns"],
        "count",
        scope,
        window,
        CHANGE_EVENTS,
        "n/a",
    )
    fs.add(
        "st.learning.scoped.spend.total",
        money(learning["scoped_spend"]),
        "currency_cad",
        scope + ". Search-term spend sitting under a matched criterion change",
        window,
        CHANGE_EVENTS,
        "n/a",
    )
    fs.add(
        "st.learning.unscoped.spend.total",
        money(sum((r.cost_cad for r in rows), Decimal(0))),
        "currency_cad",
        (
            "what the UNSCOPED reading of rule 7 would downgrade: every campaign "
            "holding search-term spend had a CAMPAIGN_BUDGET or CAMPAIGN change "
            "inside 14 days, so the unscoped rule silences the whole skill. "
            "Emitted to show why the scoping exists"
        ),
        window,
        CHANGE_EVENTS,
        "n/a",
    )
    fs.add(
        "st.learning.downgraded_negation.row.count",
        learning["downgraded"],
        "count",
        scope,
        window,
        CHANGE_EVENTS,
        "8-action",
    )
    fs.add(
        "st.learning.downgraded_negation.spend.total",
        money(learning["downgraded_spend"]),
        "currency_cad",
        scope,
        window,
        CHANGE_EVENTS,
        "8-action",
    )


def add_keyword_idea_facts(fs: FactSet, ideas: dict, window: str) -> None:
    scope = (
        f"{COST_SCOPE} AND search_term_view.status='NONE' AND 8-action "
        f"conversions > 0, keyed on {TERM_KEY}. Ideas come from the account's "
        "own converting terms. Keyword Planner sizes a term and never picks one"
    )
    fs.add("st.keyword_idea.row.count", len(ideas["all"]), "count", scope, window, CONVERSIONS, "8-action")
    fs.add(
        "st.keyword_idea.non_branded.row.count",
        len(ideas["non_branded"]),
        "count",
        scope + ". Brand-token split, review aid only, drives no decision",
        window,
        CONVERSIONS,
        "8-action",
    )
    fs.add(
        "st.keyword_idea.branded.row.count",
        len(ideas["branded"]),
        "count",
        scope
        + ". Branded ideas are Brand housekeeping, never a growth finding "
        "(campaign-guardrails rule 2)",
        window,
        CONVERSIONS,
        "8-action",
    )
    fs.add(
        "st.keyword_idea.non_branded.assumed_value.total",
        money(ideas["non_branded_value"]),
        "currency_cad_assumed_value",
        scope + ". ASSUMED VALUE, not revenue (VF-2, VF-8)",
        window,
        CONVERSIONS,
        "8-action",
    )
    fs.add(
        "st.keyword_idea.planner.usable_for_selection",
        None,
        "text",
        S.unavailable(
            "customers generate-keyword-ideas is reachable, but its raw output "
            "is not usable for selection: broad seeds return keywords from "
            "unrelated industries and generic high-volume terms. Use it to "
            "SIZE a term the account already converted on, never to pick one, "
            "and vertical-filter every suggestion, reporting the count dropped"
        ),
        window,
        "not pulled",
        "n/a",
    )


def add_csv_facts(
    fs: FactSet,
    rows: list,
    lint: dict,
    window: str,
    index: "dict | None",
    covered: list,
    dn_present: bool,
    dn_term_count: int,
    collapsed: int = 0,
) -> None:
    scope = (
        "rows written to the negatives CSV: NEGATE decisions only, "
        f"{CSV_LEVEL} level, learning-window downgrades excluded, ownership and "
        "routing fixes excluded by design"
    )
    fs.add("st.csv.row.count", len(rows), "count", scope, window, CONVERSIONS, "8-action")
    fs.add(
        "st.csv.spend.total",
        money(sum((Decimal(r["Cost (CAD)"]) for r in rows), Decimal(0))),
        "currency_cad",
        scope,
        window,
        COST,
        "n/a",
    )
    lint_scope = (
        f"CSV lint. Keyword text limit {KEYWORD_MAX_CHARS} characters: "
        + KEYWORD_MAX_CHARS_PROVENANCE
    )
    for name in sorted(lint):
        fs.add(
            f"st.csv.lint.{name}.row.count",
            len(lint[name]),
            "count",
            lint_scope,
            window,
            COST,
            "n/a",
        )
    fs.add(
        "st.csv.duplicate_negative_collapsed.row.count",
        collapsed,
        "count",
        "rows merged because two triggering keywords in one ad group are one "
        "negative, not two. Their clicks, impressions and cost are summed",
        window,
        COST,
        "n/a",
    )
    fs.add(
        "st.csv.existing_negative_inventory.checked",
        None if not index else True,
        "text" if not index else "boolean",
        (
            "the account's campaign- and ad-group-level negative inventory was "
            "NOT supplied to this run, so no candidate was checked against it. "
            "On 2026-08-10 this omission overturned the second-largest "
            "recommendation in the report: Brand Core already carried 11 EXACT "
            "login negatives and the account held 251 brand-related ad-group "
            "negatives. Pass --existing-negatives before importing anything"
        )
        if not index
        else (
            f"{index['rows']} negative keyword(s) read from the supplied "
            "inventory and every candidate compared against them, at its own "
            "ad group and at campaign level, honouring EXACT, PHRASE and BROAD "
            "coverage"
        ),
        window,
        "operator input" if not index else "campaign_criterion + ad_group_criterion",
        "n/a",
    )
    fs.add(
        "st.donotnegate.90d.clearance_pulled",
        dn_present,
        "boolean",
        "the 90-day 8-action do-not-negate pull is REQUIRED. A 30-day window "
        "cannot see a 90-day conversion, so without it no CSV row is emitted"
        if dn_present
        else S.unavailable(
            f"{DONOTNEGATE_90D} is absent from the raw dir, so no CSV row was "
            "emitted. This is the guard that failed on 2026-08-20"
        ),
        window,
        DONOTNEGATE_90D,
        "8-action",
    )
    fs.add(
        "st.donotnegate.90d.converting_term.count",
        None if not dn_present else dn_term_count,
        "count",
        S.unavailable(f"{DONOTNEGATE_90D} was not present")
        if not dn_present
        else "distinct search terms with any 8-action conversion in the "
        "trailing 90 days. Every one is barred from negation",
        window,
        DONOTNEGATE_90D,
        "8-action",
    )
    fs.add(
        "st.csv.existing_negative_inventory.row.count",
        None if not index else index["rows"],
        "count",
        S.unavailable("no negative inventory was supplied to this run")
        if not index
        else "negative keyword criteria read from the supplied pull(s)",
        window,
        "campaign_criterion + ad_group_criterion",
        "n/a",
    )
    fs.add(
        "st.csv.already_covered.removed.row.count",
        None if not index else len(covered),
        "count",
        S.unavailable("no negative inventory was supplied, so nothing was checked")
        if not index
        else (
            "candidate negatives dropped because the supplied inventory "
            "already covers them. Zero is a real answer and means the CSV adds "
            "no duplicate"
        ),
        window,
        "campaign_criterion + ad_group_criterion",
        "n/a",
    )


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build(raw_dir: Path, thresholds: Thresholds, negative_payloads: list | None = None):
    """Returns `(FactSet, rows, csv_rows, lint)`. Raises rather than guessing."""
    manifest = S.Manifest(raw_dir)
    if manifest.pulled is None:
        raise BuildError(
            f"{Path(raw_dir).name} is not a YYYY-MM-DD directory, so the pull "
            "date cannot be read and no current-state window can be formed "
            "(VF-17 rule 4)."
        )
    trail: list = []
    if not manifest.present:
        raise BuildError(
            f"{S.MANIFEST}: absent from {raw_dir.name}. Temporality is declared, "
            "never inferred (VF-17 rule 5), and neither pull's truncation state "
            "can be cross-checked without it. Refusing to write a facts file."
        )

    # Consolidated validation: every contradiction in one pass, one raise.
    # A single contradiction re-raises the original exception (type and
    # message unchanged - TruncatedPullError included); two or more are
    # listed together. Nothing is written on any failure.
    problems = pulls.ProblemLog()
    loaded = {
        name: problems.run(results, raw_dir, name, manifest, file=name)
        for name in (COST, CONVERSIONS, CAMPAIGNS, CHANGE_EVENTS)
    }
    cost_rows = loaded[COST]
    conv_rows = loaded[CONVERSIONS]
    camp_rows = loaded[CAMPAIGNS]
    change_rows = loaded[CHANGE_EVENTS]

    for name in (COST, CONVERSIONS, CAMPAIGNS, CHANGE_EVENTS):
        rows = loaded[name]
        if rows is None:
            continue
        trail.append(manifest.audit_line(name))
        line = problems.run(
            assert_not_truncated,
            name,
            read_payload(raw_dir, name),
            (manifest.files.get(name) or {}).get("truncated"),
            file=name,
        )
        if line:
            trail.append(line)
        line = problems.run(
            manifest.check_row_count, name, len(rows), file=name
        )
        if line:
            trail.append(line)
        line = problems.run(
            manifest.verify_declared_temporality, name, file=name
        )
        if line:
            trail.append(line)

    # VF-19a: clause-for-clause after normalising, temporality held out.
    for name, label in ((COST, COST_SCOPE), (CONVERSIONS, CONVERSIONS_SCOPE),
                        (CAMPAIGNS, CAMPAIGNS_SCOPE)):
        line = problems.run(manifest.verify_filter, name, label, file=name)
        if line:
            trail.append(line)

    problems.raise_if_any(raw_dir.name)

    undeclared = manifest.undeclared_files()
    if undeclared:
        trail.append(
            f"{S.MANIFEST}: {len(undeclared)} raw pull(s) on disk are NOT "
            f"declared in it: {', '.join(undeclared)}"
        )

    cost_window = manifest.window_for(COST, windowed_fallback=True)
    conv_window = manifest.window_for(CONVERSIONS, windowed_fallback=True)
    camp_window = manifest.window_for(CAMPAIGNS, windowed_fallback=False)
    change_window = manifest.window_for(CHANGE_EVENTS, windowed_fallback=False)

    names = {
        r["campaign"]["id"]: r["campaign"].get("name", "")
        for r in camp_rows
        if (r.get("campaign") or {}).get("id")
    }
    rows, unmatched = build_rows(cost_rows, conv_rows, names)
    if unmatched:
        trail.append(
            f"{CONVERSIONS}: {len(unmatched)} term key(s) carry conversions but "
            f"no row in {COST}. Listed, never dropped: "
            + "; ".join(str(k) for k in sorted(unmatched)[:5])
        )

    # The 90-day do-not-negate clearance. Absent = no CSV rows, by rule.
    dn_path = raw_dir / DONOTNEGATE_90D
    dn_payload = None
    if dn_path.exists():
        dn_payload = json.loads(dn_path.read_text(encoding="utf-8"))
    converted_90d = converted_in_90d_terms(dn_payload)
    if dn_payload is None:
        trail.append(
            f"{DONOTNEGATE_90D}: ABSENT from {raw_dir.name}. The 90-day "
            "8-action do-not-negate clearance is mandatory, a 30-day window "
            "cannot see a 90-day conversion, and on 2026-08-20 its absence put "
            "a term back on a negation list it had been removed from 15 days "
            "earlier. NO CSV ROWS ARE EMITTED without it."
        )
    else:
        trail.append(
            f"{DONOTNEGATE_90D}: {len(converted_90d)} search term(s) converted "
            "on the 8 actions inside the trailing 90 days; any candidate among "
            "them is a full stop"
        )

    apply_decisions(rows, thresholds, converted_90d)
    learning = apply_learning_windows(rows, change_rows)
    overlap_data = overlap(rows)
    ideas = keyword_ideas(rows)

    index = existing_negatives(negative_payloads) if negative_payloads else None
    if index and index["unrecognised_match_types"]:
        trail.append(
            "existing negatives: unrecognised match type(s) "
            f"{sorted(str(m) for m in index['unrecognised_match_types'])} - "
            "coverage for those rows was NOT computed, so a candidate they "
            "cover could still be in the CSV"
        )
    if dn_payload is None:
        out_csv, collapsed, covered = [], 0, []
    else:
        out_csv, collapsed, covered = csv_rows(rows, index)
    if index:
        trail.append(
            f"existing negatives: {index['rows']} criteria read; "
            f"{len(covered)} candidate(s) dropped as already covered"
        )
        for row, why in covered:
            trail.append(f"  dropped {row.search_term!r}: {why}")
    lint = lint_csv(out_csv)
    if collapsed:
        trail.append(
            f"CSV: {collapsed} duplicate negative row(s) collapsed. One negative "
            "is one (campaign, ad group, keyword text, match type); two "
            "triggering keywords in one ad group are one negative, not two."
        )

    fs = FactSet(
        meta={
            "skill": "mine-search-terms",
            "customer_id": account_config.customer_id(),
            "currency": "CAD",
            "raw_dir": str(raw_dir),
            "generator": "tools/build_mine_search_terms_facts.py",
            "term_key": TERM_KEY,
            "keyword_key": KEYWORD_KEY,
            "decision_inputs": [f.name for f in Numbers.__dataclass_fields__.values()],
            "category_is_a_label_not_an_input": True,
            "thresholds": {
                "min_spend_cad": str(thresholds.min_spend_cad),
                "min_clicks": thresholds.min_clicks,
                "target_cpa_cad": str(thresholds.target_cpa_cad),
                "target_cpa_multiple": str(thresholds.target_cpa_multiple),
                "cut_cpa_cad": str(thresholds.cut_cpa_cad),
                "provenance": "operator input, no defaults exist",
            },
            "windows": {
                COST: cost_window,
                CONVERSIONS: conv_window,
                CAMPAIGNS: camp_window,
                CHANGE_EVENTS: change_window,
            },
            "csv_header": list(CSV_HEADER),
            "csv_header_verified_against_google_ads_editor": False,
            "existing_negative_inventory_checked": bool(index),
            "existing_negative_inventory_rows": index["rows"] if index else 0,
            "existing_negatives_already_covered": [
                {"search_term": r.search_term, "campaign_id": r.campaign_id,
                 "ad_group_id": r.ad_group_id, "why": w} for r, w in covered
            ],
            "declared_filter_audit": trail,
        }
    )

    add_pull_facts(fs, rows, cost_window, conv_window, unmatched)
    add_row_key_facts(fs, rows, cost_window)
    add_threshold_facts(fs, thresholds, camp_window)
    add_totals(fs, rows, cost_window, conv_window)
    add_decision_facts(fs, rows, thresholds, conv_window)
    add_top_n_facts(fs, rows, cost_window, TOP_N)
    add_overlap_facts(fs, overlap_data, cost_window)
    add_learning_facts(fs, learning, rows, change_window)
    add_keyword_idea_facts(fs, ideas, conv_window)
    add_csv_facts(
        fs, out_csv, lint, conv_window, index, covered,
        dn_payload is not None, len(converted_90d), collapsed,
    )
    return fs, rows, out_csv, lint


def sensitivity(raw_dir: Path, pairs: list, target_cpa: Decimal, multiple: Decimal) -> list:
    """What each candidate `spend:clicks` pair would negate. Chooses nothing.

    The point of this table is to let the account owner pick their own two numbers
    with the consequence in front of them, not to pick them for them. Every pair in
    it is a pair they named. The sensitivity is large enough that the choice
    cannot be delegated: on this account CAD 250 / 10 clicks negates nothing at
    all, and CAD 50 / 3 clicks negates 25 rows and CAD 3,050.00.
    """
    out = []
    for min_spend, min_clicks in pairs:
        thresholds = require_thresholds(
            {
                "min_spend_cad": min_spend,
                "min_clicks": min_clicks,
                "target_cpa_cad": target_cpa,
                "target_cpa_multiple": multiple,
            }
        )
        _, _, csv_out, _ = build(raw_dir, thresholds)
        out.append(
            {
                "min_spend_cad": money(thresholds.min_spend_cad),
                "min_clicks": thresholds.min_clicks,
                "csv_rows": len(csv_out),
                "csv_spend_cad": money(
                    sum((Decimal(r["Cost (CAD)"]) for r in csv_out), Decimal(0))
                ),
            }
        )
    return out


def parse_pairs(values: list) -> list:
    pairs = []
    for value in values or []:
        try:
            spend, clicks = value.split(":", 1)
            pairs.append((Decimal(spend), int(clicks)))
        except (ValueError, ArithmeticError):
            raise MissingThresholdError(
                f"--sensitivity {value!r} is not a `spend:clicks` pair, e.g. "
                "`250:10`. Every pair must be one the account owner named; this "
                "script does not generate candidates."
            ) from None
    return pairs


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("raw_dir", help="google-ads/outputs/raw/YYYY-MM-DD")
    parser.add_argument("--out", default=None, help="facts file path")
    parser.add_argument("--csv-out", default=None, help="negatives CSV path")
    parser.add_argument(
        "--existing-negatives",
        action="append",
        default=None,
        help=(
            "raw pull of the account's existing campaign- and ad-group-level "
            "negatives (campaign_criterion and/or ad_group_criterion). "
            "Repeatable. Each file IS READ and every candidate is compared "
            "against it, honouring EXACT, PHRASE and BROAD coverage; covered "
            "candidates are dropped from the CSV and listed in the scope trail. "
            "Without it no candidate is checked and the CSV says so on every "
            "row. A path that does not exist is a hard refusal, never a "
            "silent downgrade to unchecked."
        ),
    )
    for attr, flag, why_text in THRESHOLD_SPEC:
        parser.add_argument(
            flag,
            dest=attr,
            default=None,
            type=(int if attr == "min_clicks" else Decimal),
            help=why_text + " (REQUIRED, no default)",
        )
    parser.add_argument(
        "--sensitivity",
        action="append",
        default=None,
        metavar="SPEND:CLICKS",
        help=(
            "repeatable. Print what each candidate spend/click pair would "
            "negate, so the account owner can choose with the consequence in "
            "front of them. Every pair must be one they named; no candidates are "
            "generated. Requires --target-cpa-cad and --target-cpa-multiple."
        ),
    )
    parser.add_argument("--print", action="store_true", help="print every fact")
    args = parser.parse_args(argv)

    raw_dir = Path(args.raw_dir)
    if not raw_dir.is_dir():
        print(f"not a directory: {raw_dir}", file=sys.stderr)
        return 2

    if args.sensitivity:
        if args.target_cpa_cad is None or args.target_cpa_multiple is None:
            print(
                "REFUSED. --sensitivity still needs --target-cpa-cad and "
                "--target-cpa-multiple; it varies the spend and click floors "
                "only.",
                file=sys.stderr,
            )
            return 1
        try:
            grid = sensitivity(
                raw_dir,
                parse_pairs(args.sensitivity),
                args.target_cpa_cad,
                args.target_cpa_multiple,
            )
        except (BuildError, S.ManifestError, gaql.FilterMismatch) as exc:
            print(f"REFUSED.\n{exc}", file=sys.stderr)
            return 1
        print("min spend CAD | min clicks | negatives | spend negated CAD")
        for line in grid:
            print(
                f"{line['min_spend_cad']:>13} | {line['min_clicks']:>10} | "
                f"{line['csv_rows']:>9} | {line['csv_spend_cad']:>17}"
            )
        print(
            "These are candidates, not a recommendation. The pair is the "
            "account owner's to choose."
        )
        return 0

    # Read the inventory files here, so a bad path fails loudly instead of
    # silently degrading to "not checked" - or worse, to a claim of a check
    # that never ran. That was the 2026-08-20 defect: the flag took paths and
    # never opened them.
    negative_payloads = []
    for name in args.existing_negatives or ():
        path = Path(name)
        if not path.is_absolute() and not path.exists():
            path = raw_dir / name
        if not path.exists():
            print(
                f"REFUSED: --existing-negatives {name!r} does not exist "
                f"(looked at {path}). Fix the path or omit the flag; a missing "
                "inventory must never read as a passed check.",
                file=sys.stderr,
            )
            return 1
        try:
            negative_payloads.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"REFUSED: cannot read {path}: {exc}", file=sys.stderr)
            return 1

    try:
        thresholds = require_thresholds(
            {attr: getattr(args, attr) for attr, _, _ in THRESHOLD_SPEC}
        )
        fs, rows, out_csv, lint = build(raw_dir, thresholds, negative_payloads)
    except (BuildError, S.ManifestError, gaql.FilterMismatch) as exc:
        print(f"REFUSED to write a facts file.\n{exc}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else raw_dir / "mine-search-terms-facts.json"
    fs.write(out)
    csv_path = (
        Path(args.csv_out)
        if args.csv_out
        else raw_dir.parent.parent / f"search-term-negatives-{raw_dir.name}.csv"
    )
    write_csv(csv_path, out_csv)

    print(f"wrote {len(fs)} facts to {out}")
    print(f"wrote {len(out_csv)} negative rows to {csv_path}")
    for name, hits in sorted(lint.items()):
        print(f"  lint {name}: {len(hits)}")
    if not negative_payloads:
        print(
            "  WARNING: existing negative inventory not supplied. Every CSV row "
            "says so. Do not import before checking it."
        )
    else:
        read = fs.value("st.csv.existing_negative_inventory.row.count")
        dropped = fs.value("st.csv.already_covered.removed.row.count")
        print(
            f"  existing negatives: {read} criteria read from "
            f"{len(negative_payloads)} file(s); {dropped} candidate(s) dropped "
            "as already covered"
        )
    nulls = [f.id for f in fs if f.is_null]
    print(f"{len(nulls)} deliberately null (unavailable) facts")
    for fact_id in nulls:
        print(f"  - {fact_id}")
    if args.print:
        for fact in fs:
            print(fact.describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
