#!/usr/bin/env python3
"""Build the `weekly-review` facts file from saved raw JSON (VERIFICATION-RULES VF-15).

Usage:
    python3 tools/build_weekly_review_facts.py google-ads/outputs/raw/2026-08-20
    python3 tools/build_weekly_review_facts.py <raw_dir> --out /tmp/facts.json

Reads only raw JSON already on disk plus `budget-plan.md`. Makes no API calls.
Writes `<raw_dir>/weekly-review-facts.json` unless `--out` says otherwise.

Six things about this script matter more than the arithmetic.

1. **A week-over-week delta is a THIRD fact, not a comparison.** The VF-15
   guard refuses to relate two facts unless `unit`, `scope`, `window` and
   `basis` all match, and a WoW delta is by construction two different windows.
   Routing around the guard with `compare_unsafe` would file this skill's whole
   reason for existing under "overrides a reviewer should distrust", so instead
   `related_fact()` emits a third fact with its own id, computed from two named
   parents, whose `window` and `scope` both name **both** parent windows. The
   helper asserts that the *only* guarded attribute that differs is the one
   named in `permitted_mismatch`, so a delta across two different scopes - the
   Aug-10 error - still fails. See `related_fact` for the full rule.

2. **Two-query rule (VF-3).** `metrics.cost_micros` and
   `segments.conversion_action_name` cannot coexist, so cost and conversions
   arrive in separate pulls with deliberately different filters. Every CPA and
   ROAS therefore joins two scopes. `related_fact` permits it only after
   proving the clause-set difference between the two scopes is **exactly** the
   conversion-action clause and nothing else. That is what makes "same campaign
   population" checkable instead of assumed.

3. **Row keys (VF-19).** Every child object is keyed on the `(parent, child)`
   pair. Here the conversions pull is segmented, so its key is
   `(campaign.id, segments.conversion_action_name)` and it is aggregated per
   campaign **before** any join. Both cost pulls are asserted one-row-per
   campaign. The MTD pull is keyed on `segments.date`.

4. **The impression-share sentinel family (VF-11), both ends.** Google returns
   `0.0999` for "< 10%" and `0.9001` for "> 90%". `facts.is_sentinel_share`
   only knows the low one, and VF-11 only names `search_impression_share`, but
   the sentinels are a property of the whole impression-share family: this
   run's data carries `0.0999` in `search_absolute_top_impression_share` on 7
   campaigns and `0.9001` in `search_rank_lost_impression_share` on one. So the
   suppression here is applied family-wide and at both ends.

5. **Temporality comes from `_manifest.json`, never from the data** (VF-17
   rule 5). Current-state facts carry `window: current-as-of-<date>`.

6. **The budget figure is READ, never derived** (VF-14, guardrail 8). It
   comes from `account-config.json`, whose figures `test_account_config.py`
   re-parses out of `budget-plan.md` and diffs, so the facts file cannot
   drift from the doc. A month with no stated split writes null-with-reason
   pacing facts instead of refusing the whole build.

One shared-module limitation is worked around here and reported, not edited:
`scopes.Manifest.window_for()` resolves only `LAST_30_DAYS`. See
`resolve_relative_window`. A second - `gaql.clauses()` splitting a
`BETWEEN 'a' AND 'b'` predicate on its own `AND` - was fixed upstream on
2026-08-20; `split_and` still splits scope labels locally, because a label must
stay literal and must not depend on which copy of a read-only module is
installed.

Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import calendar
import collections
import datetime as dt
from dataclasses import dataclass
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_config  # noqa: E402
import builder_core as core  # noqa: E402
import gaql  # noqa: E402
import pulls  # noqa: E402
import scopes as S  # noqa: E402
from facts import (  # noqa: E402
    FactSet,
    FactsError,
    SEARCH_IMPRESSION_SHARE_SENTINEL,
    available_impressions,
    current_state_window,
    is_current_state_window,
    micros_to_currency,
    money,
    percentage_convention,
)

BuildError = pulls.BuildError
load_results = pulls.load_results

# --------------------------------------------------------------------------
# Raw filenames. One name per pull, matching `_manifest.json`. The canonical
# spellings live in `pull_specs.py` - the single source for which pulls exist.
# --------------------------------------------------------------------------

import pull_specs  # noqa: E402

CAMPAIGNS = pull_specs.CAMPAIGNS_CURRENT
COST_7D = pull_specs.COST_7D
COST_PRIOR = pull_specs.COST_PRIOR7D
CONV_7D = pull_specs.CONV_7D
CONV_PRIOR = pull_specs.CONV_PRIOR7D
SPEND_MTD = pull_specs.SPEND_MTD
CHANGES = pull_specs.CHANGE_EVENTS_14D

REQUIRED_PULLS = (CAMPAIGNS, COST_7D, COST_PRIOR, CONV_7D, CONV_PRIOR,
                  SPEND_MTD, CHANGES)

#: Where the budget figure is read from. Never derived (VF-14, guardrail 8).
BUDGET_PLAN = "budget-plan.md"

# --------------------------------------------------------------------------
# The impression-share sentinel family, both ends (VF-11)
# --------------------------------------------------------------------------

#: Both sentinel ends, and the family of fields they apply to, live in
#: `builder_core` now — the budget builder needs the same both-ends handling
#: and two copies of a suppression rule is how rules drift (VF-11).
SHARE_SENTINEL_LOW = core.SHARE_SENTINEL_LOW
SHARE_SENTINEL_HIGH = core.SHARE_SENTINEL_HIGH
SHARE_FIELDS = core.SHARE_FIELDS
share_sentinel = core.share_sentinel


# --------------------------------------------------------------------------
# Campaign classification (guardrails 1 and 2)
# --------------------------------------------------------------------------

#: Brand Core and its mid-generation siblings, per `account-conventions`.
#: Guardrail 2: brand and non-brand subtotals are reported separately and a
#: blended figure never leads.
#: Read from `account-config.json`, which carries the id -> name map.
BRAND_CAMPAIGN_IDS = account_config.brand_campaign_ids()

#: Guardrail 1: top-of-funnel. Never judged on BOFU CPA/ROAS, so they are held
#: out of the non-brand denominator too. A correct CPA over a denominator that
#: includes TOF spend is a correct number under a wrong label, which is the
#: VF-15 failure class.
TOF_CAMPAIGN_IDS = account_config.tof_campaign_ids()

BRAND_CORE_ID = account_config.brand_core_campaign_id()

#: Guardrail 5: no bid/budget/target recommendation below this many 8-action
#: conversions in the window. Below it the observation is a "monitor" note.
CONVERSION_FLOOR = account_config.threshold("conversion_floor")

#: Guardrail 7 windows.
LEARNING_DAYS_SEARCH = account_config.threshold("learning_days_search")
LEARNING_DAYS_PMAX = account_config.threshold("learning_days_pmax")

#: Change-event resource/field combinations that reset a learning window.
#: Matched to the claim type by the skill, not here (guardrail 7).
LEARNING_RESOURCE_TYPES = frozenset({"CAMPAIGN_BUDGET", "CAMPAIGN_BIDDING_STRATEGY"})
LEARNING_CAMPAIGN_FIELDS = ("targetroas", "targetcpa", "biddingstrategy",
                            "targetimpressionshare", "campaignbudget")


# --------------------------------------------------------------------------
# De-forked shared helpers (2026-08-27)
# --------------------------------------------------------------------------

#: The literal split lives in `gaql.split_literal` now: the upstream BETWEEN
#: bug that justified a local copy was fixed 2026-08-20, and one split with
#: one home is the point of the shared module. `gaql.clauses()` normalises
#: on top of the same split, so the two can no longer disagree.
split_and = gaql.split_literal

#: Relative-window resolution lives beside `Manifest.window_for()` in
#: `scopes.py` — it appends the resolved range to the declared token, never
#: substituting it (VF-17 rule 5 arithmetic on a declaration).
resolve_relative_window = S.resolve_relative_window


# --------------------------------------------------------------------------
# Scope labels. VF-15: `scope` is the literal filter used, temporality held out
# --------------------------------------------------------------------------
# Temporality lives in `window` (VF-17), and `gaql` holds date clauses out of
# every scope comparison, so a scope label carries the pull's **non-temporal**
# clauses verbatim from the manifest. That is what lets the 7-day and prior-week
# facts share a scope and differ only on window, which is exactly the mismatch a
# delta is allowed to bridge.


def no_nontemporal_scope(filename: str) -> str:
    """The scope label for a pull whose only filter is its date predicate.

    Two such pulls must not share a scope label, or the guard would let their
    facts be compared, so the filename is part of the label. The account-spend
    pull applies no `campaign.status` filter at all, which is a real scope
    difference from the campaign pulls and the reason its total must never be
    differenced against theirs.
    """
    return (
        f"(no non-temporal filter in {filename}: every row inside the window, "
        "with no campaign.status or channel restriction)"
    )


def nontemporal_scope(manifest: S.Manifest, filename: str) -> str:
    """The declared filter with its date predicate held out, verbatim."""
    declared = manifest.declared_filter(filename)
    if not declared:
        raise BuildError(
            f"{filename}: no `filter` declared in {S.MANIFEST}. VF-15 makes "
            "`scope` mandatory and it must be the literal filter used, so there "
            "is nothing honest to label these facts with. Declare the pull."
        )
    literal = [p for p in split_and(declared)
               if not gaql.is_temporality(gaql.normalise(p))]
    if not literal:
        return no_nontemporal_scope(filename)
    return " AND ".join(literal)


# --------------------------------------------------------------------------
# budget-plan.md — read, never derive (VF-14, guardrail 8)
# --------------------------------------------------------------------------

class BudgetPlan:
    """The stated-plan figures, from `account-config.json` (VF-14, rule 8).

    The figures were once regex-parsed out of `budget-plan.md` on every run;
    they now live in the config, and `test_account_config.py` re-parses the
    doc with the same expressions to guarantee the two cannot drift. The
    stated monthly budget is looked up per run month via `stated_budget()`:
    a month with no stated split returns a null-with-reason entry (the
    builder writes null pacing facts, never a derived or carried-over
    figure), and a month the config does not know at all refuses loudly.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.planned_step_down_pct = account_config.planned_step_down_pct()
        anchor = account_config.cac_anchor()
        self.cac_month = anchor["month_short"]
        self.cac_usd = anchor["cac_usd"]

    def stated_budget(self, month: str) -> "account_config.StatedBudget":
        try:
            return account_config.stated_budget(month)
        except account_config.UnknownMonthError as exc:
            raise BuildError(str(exc)) from exc


# --------------------------------------------------------------------------
# related_fact — the one place two facts become a third
# --------------------------------------------------------------------------

#: The only guarded attributes a derived fact may bridge, and the rule for each.
#:
#: `cost-over-conversions` bridges three, and the one it does NOT bridge is the
#: point: `window` must match, so a CPA can never quietly divide this week's
#: cost by last week's conversions. `unit` has to differ because a quotient's
#: unit is a new compound unit, and `scope`/`basis` differ because VF-3 forces
#: cost and conversions into separate queries.
PERMITTED_PROFILES = {
    "wow-delta": ("window",),
    "cost-over-conversions": ("unit", "scope", "basis"),
    "plan-vs-actual": ("scope",),
    "actual-vs-target": ("unit", "scope", "window", "basis"),
}

#: `actual-vs-target` is the one profile allowed a current-state parent: a target
#: is a live setting and the actual is a window, so the two can never share a
#: window and the comparison is still the right one to make. It carries its own
#: guard instead: the caller must prove the target did not CHANGE inside the
#: window, read from the change log. Three campaigns had their target ROAS
#: edited inside 14 days on 2026-08-20, and a gap computed across such an edit
#: is measuring two different targets at once.
CURRENT_STATE_PARENT_PROFILES = frozenset({"actual-vs-target"})


def related_fact(
    fs: FactSet,
    id: str,
    *,
    parents: "tuple[str, str]",
    profile: str,
    value,
    unit: str,
    scope: str,
    window: str,
    basis: str,
    rule: str,
    scope_delta: "frozenset[str] | None" = None,
    setting_changed_in_window: bool = False,
):
    """Emit a third fact computed from two named parents.

    The VF-15 guard exists to stop *accidental* cross-scope arithmetic: on
    2026-08-10 two individually correct spend figures on different scopes were
    differenced and the difference reported as growth. A week-over-week delta is
    *deliberate* cross-window arithmetic and is the reason this skill exists, so
    it needs a route that is neither a silent bypass nor `compare_unsafe` (whose
    `unsafe_comparisons` record is for overrides a reviewer should distrust, and
    filing the skill's headline finding there would invite Tier 3 to discount
    it).

    The route is a third fact, and it is narrower than the guard, not wider:

    1. Exactly the attributes named by `profile` may differ. Any other guarded
       mismatch is a hard failure, so a delta across two scopes still refuses.
    2. Each permitted attribute must *actually* differ. If none does, the plain
       `FactSet.compare()` would have allowed it and this helper is the wrong
       tool - which usually means one parent id is wrong.
    3. Neither parent may be a current-state fact. Differencing a
       `current-as-of-<date>` fact against a windowed one is the VF-17 error.
    4. Where `scope_delta` is given, the clause-set difference between the two
       parents' scopes must be exactly that set. This is what proves a CPA joins
       the same campaign population across the two-query split (VF-3).
    5. The new fact's `window` and `scope` both name both parents' windows, so
       the delta can never be read as a single-window figure, and lineage is
       written to the facts file's `meta.derivations`.
    """
    if profile not in PERMITTED_PROFILES:
        raise FactsError(f"unknown derivation profile {profile!r}")
    permitted = PERMITTED_PROFILES[profile]
    id_a, id_b = parents
    a, b = fs.get(id_a), fs.get(id_b)

    if a.is_null or b.is_null:
        raise FactsError(
            f"{id}: parent {id_a if a.is_null else id_b} is a null fact, so no "
            "derived figure may be computed from it. Suppress the derived "
            "figure and state why."
        )

    differing = {attr for attr, _, _ in fs.mismatches(id_a, id_b)}
    illegal = differing - set(permitted)
    if illegal:
        raise FactsError(
            f"REFUSED: {id} would relate {id_a} and {id_b}, which differ on "
            f"{sorted(illegal)} as well as on {sorted(differing & set(permitted))}. "
            f"Profile {profile!r} permits only {list(permitted)} to differ. This "
            "is the Aug-10 error class: two individually correct figures on "
            "different scopes, differenced anyway."
        )
    current_parents = [x for x in (a, b) if is_current_state_window(x.window)]
    if profile in CURRENT_STATE_PARENT_PROFILES:
        if len(current_parents) != 1:
            raise FactsError(
                f"{id}: profile {profile!r} compares one windowed measurement "
                f"against one live setting, but {len(current_parents)} of its "
                "two parents are current-state facts."
            )
        if setting_changed_in_window:
            raise FactsError(
                f"REFUSED: {id} would compare a measurement taken across a "
                "window against a setting that CHANGED inside that window, so "
                "the gap measures two different settings at once. Suppress the "
                "figure and say the setting moved and when (guardrail 7)."
            )
    elif current_parents:
        raise FactsError(
            f"{id}: parent {current_parents[0].id} is a current-state fact "
            f"({current_parents[0].window}). A windowed metric cannot be "
            "differenced against a current state (VF-17 rule 4)."
        )

    absent = set(permitted) - differing
    if absent:
        raise FactsError(
            f"{id}: profile {profile!r} exists to bridge {sorted(absent)}, but "
            f"{id_a} and {id_b} already agree there. Use FactSet.compare(); if "
            "you expected a difference, one of the parent ids is wrong."
        )
    if scope_delta is not None:
        clauses_a = set(gaql.clauses(a.scope))
        clauses_b = set(gaql.clauses(b.scope))
        actual = (clauses_a ^ clauses_b)
        if actual != set(scope_delta):
            raise FactsError(
                f"{id}: the two parents' scopes differ by {sorted(actual)}, but "
                f"this derivation is only valid when they differ by exactly "
                f"{sorted(scope_delta)}. Anything else means the two pulls did "
                "not cover the same population."
            )

    fs.meta.setdefault("derivations", []).append({
        "id": id,
        "parents": [id_a, id_b],
        "profile": profile,
        "permitted_mismatch": list(permitted),
        "parent_windows": [a.window, b.window],
        "parent_scopes": [a.scope, b.scope],
        "rule": rule,
    })
    return fs.add(
        id=id,
        value=value,
        unit=unit,
        scope=scope,
        window=window,
        source=f"{a.source} + {b.source} via {id_a} and {id_b} (derived)",
        basis=basis,
    )


def bucket(cid: str) -> str:
    """Which of the three reported subtotals a campaign belongs to.

    Three, not two. Guardrail 1 forbids judging TOF on BOFU CPA, so its spend
    must leave the non-brand denominator as well as the non-brand numerator.
    """
    if cid in BRAND_CAMPAIGN_IDS:
        return "brand"
    if cid in TOF_CAMPAIGN_IDS:
        return "tof"
    return "nonbrand"


def wow_window(later: str, earlier: str) -> str:
    """The `window` of a delta fact: both parents' windows, in order."""
    return f"week-over-week delta: [{later}] minus [{earlier}]"


def wow_scope(shared_scope: str, later: str, earlier: str) -> str:
    """The `scope` of a delta fact.

    The literal filter comes first, because that is what VF-15 requires `scope`
    to be and it is the same filter on both sides. The two windows are named
    after it because a delta's scope is not fully described by a filter: the
    same filter over two different weeks is two different populations of rows,
    and a reader who sees only the filter could mistake the delta for a level.
    """
    return (
        f"{shared_scope} | delta across two windows: [{later}] minus "
        f"[{earlier}]"
    )


DELTA_RULE = (
    "a week-over-week delta is deliberate cross-window arithmetic and the "
    "reason this skill exists, so it is emitted as a third fact naming both "
    "windows rather than as a comparison the guard would refuse. Window is "
    "the only guarded attribute allowed to differ."
)


def delta_pair(fs: FactSet, base: str, later: str, earlier: str, unit: str,
               basis: str, shared_scope: str, w_later: str,
               w_earlier: str) -> None:
    """Emit the absolute and percentage week-over-week facts for one figure."""
    a, b = fs.get(later), fs.get(earlier)
    av, bv = Decimal(str(a.value)), Decimal(str(b.value))
    window = wow_window(w_later, w_earlier)
    scope = wow_scope(shared_scope, w_later, w_earlier)
    related_fact(fs, f"{base}.wow.delta", parents=(later, earlier),
                 profile="wow-delta",
                 value=money(av - bv) if "currency" in unit else av - bv,
                 unit=f"{unit}_delta", scope=scope, window=window, basis=basis,
                 rule=DELTA_RULE)
    change = pct_change(av, bv)
    if change is not None:
        related_fact(fs, f"{base}.wow.pct_change", parents=(later, earlier),
                     profile="wow-delta",
                     value=change.quantize(Decimal("0.01")),
                     unit="percent_change_wow", scope=scope, window=window,
                     basis=basis, rule=DELTA_RULE)


pct_change = core.pct_change


# --------------------------------------------------------------------------
# Row keys (VF-19) and per-pull readers
# --------------------------------------------------------------------------


#: The four-field target table lives in `builder_core.TARGET_READERS` — the
#: wrong-field incident (an empty legacy field read as "no campaign has a
#: target") is exactly the knowledge that must not fork between builders.
#: This skill reports the live target only; the legacy fields stay pulled as
#: controls and their emptiness is a fact via `legacy_target_present`.
LEGACY_TARGET_PATHS = core.LEGACY_TARGET_PATHS
legacy_target_present = core.legacy_target_present


#: This skill has always labelled the MCV target with the GAQL (snake_case)
#: spelling in fact scopes; core returns the response-payload spelling. Keep
#: the label stable — a scope string is part of the fact's identity.
_TARGET_FIELD_LABELS = {
    "campaign.maximizeConversionValue.targetRoas":
        "campaign.maximize_conversion_value.target_roas",
}


def read_target(campaign: dict) -> "dict | None":
    """The campaign's bid target, from the bidding-strategy sub-message."""
    kind, value, field = core.read_target(campaign)
    if kind is None or kind == "legacy_target_roas":
        return None
    return {"kind": kind.replace("legacy_", ""), "value": value,
            "field": _TARGET_FIELD_LABELS.get(field, field)}


campaign_id = core.campaign_id


one_row_per_campaign = core.one_row_per_campaign


def conversions_per_campaign(filename: str, rows: list) -> "tuple[dict, dict]":
    """Aggregate the segmented conversions pull onto `campaign.id`.

    The pull's row key is the **pair** `(campaign.id,
    segments.conversion_action_name)` (VF-19). Keying on `campaign.id` alone
    would keep one action per campaign and silently under-report; keying on the
    action name alone would merge campaigns. The pair is asserted unique, then
    summed per campaign.
    """
    per: dict = collections.defaultdict(lambda: [Decimal(0), Decimal(0)])
    seen: set = set()
    actions: set = set()
    for row in rows:
        cid = campaign_id(row)
        action = (row.get("segments") or {}).get("conversionActionName")
        if not action:
            raise BuildError(
                f"{filename}: a row has no segments.conversionActionName, so "
                "the (campaign, action) row key cannot be formed (VF-19)."
            )
        if action not in S.EIGHT_ACTIONS:
            raise BuildError(
                f"{filename}: row carries conversion action {action!r}, which "
                "is not one of the 8 (VF-1). The pull's declared filter and its "
                "contents disagree, so no fact from it can claim an 8-action "
                "basis."
            )
        key = (cid, action)
        if key in seen:
            raise BuildError(
                f"{filename}: duplicate row key {key}. The pull is segmented "
                "further than (campaign, action) and must be aggregated on the "
                "finer key first (VF-19)."
            )
        seen.add(key)
        actions.add(action)
        metrics = row.get("metrics") or {}
        per[cid][0] += Decimal(str(metrics.get("conversions", 0) or 0))
        per[cid][1] += Decimal(str(metrics.get("conversionsValue", 0) or 0))
    return dict(per), {"row_keys": len(seen), "actions": sorted(actions)}


def daily_spend(filename: str, rows: list) -> dict:
    """The MTD pull, keyed on `segments.date`."""
    out: dict = {}
    for row in rows:
        day = (row.get("segments") or {}).get("date")
        if not day:
            raise BuildError(f"{filename}: a row has no segments.date.")
        if day in out:
            raise BuildError(
                f"{filename}: {day} appears twice. The pull is segmented by "
                "something else as well; aggregate per day first."
            )
        out[day] = micros_to_currency((row.get("metrics") or {}).get("costMicros"))
    return out


def cost_of(metrics: dict) -> Decimal:
    return micros_to_currency(metrics.get("costMicros") or 0) or Decimal(0)


def int_of(metrics: dict, field: str) -> int:
    return int(metrics.get(field) or 0)


# --------------------------------------------------------------------------
# Learning windows (guardrail 7)
# --------------------------------------------------------------------------


def learning_state(rows: list, pulled: dt.date) -> dict:
    """Per campaign, the bid/budget/target changes inside the learning window.

    Guardrail 7 is scoped: this returns the raw material (event type, changed
    field, date) and the skill matches the change type to the claim type. A
    campaign budget event cannot downgrade a copy or coverage finding, and an
    unscoped reading would silence this whole report, since 12 of the 13
    campaigns that spent anything carry one.
    """
    per: dict = collections.defaultdict(list)
    counts: dict = collections.Counter()
    for event in core.parse_change_events(rows):
        counts[event["resource_type"] or None] += 1
        relevant = event["resource_type"] in LEARNING_RESOURCE_TYPES or (
            event["resource_type"] == "CAMPAIGN"
            and any(f in event["changed_fields_lower"]
                    for f in LEARNING_CAMPAIGN_FIELDS)
        )
        if not relevant or not event["cid"]:
            continue
        per[event["cid"]].append({
            "date": event["date"],
            "resource_type": event["resource_type"],
            "changed_fields": event["changed_fields"],
            "user": event["user"],
        })
    return {"per_campaign": dict(per), "type_counts": dict(counts)}


# --------------------------------------------------------------------------
# The build
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """Everything the section builders read. Assembled once by `prepare`.

    Sections are separate functions rather than one long `build`, matching
    `build_audit_ad_copy_facts.py`. Frozen, because a section that mutated
    shared state would make the order of the sections load-bearing and the
    numbers order-dependent - the same instability VF-19 is about.
    """

    manifest: object
    pulled: object
    as_of: object
    CURRENT: object
    windows: object
    scopes: object
    trail: object
    names: object
    channels: object
    structure: object
    campaign_rows: object
    cost7: object
    costp: object
    conv7: object
    convp: object
    conv7_meta: object
    per_day: object
    change_rows: object
    learning: object
    plan: object
    plan_path: object
    spending: object
    ever_spent: object
    targets: object
    target_changed: object
    legacy_hits: object
    W7: object
    WP: object
    WC7: object
    WCP: object
    SC: object
    SV: object
    CONV_CLAUSE: object


def prepare(raw_dir: Path, plan_path: Path) -> "tuple[FactSet, Run]":
    """Read, validate and label every pull. Emits no facts."""
    raw_dir = Path(raw_dir)
    manifest = S.Manifest(raw_dir)
    pulled = manifest.pulled
    if not pulled:
        raise BuildError(
            f"{raw_dir.name} is not a YYYY-MM-DD directory, so the pull date "
            "cannot be read and no current-state window can be formed "
            "(VF-17 rule 4)."
        )
    as_of = pulled.isoformat()
    trail: list = []

    if not manifest.present:
        raise BuildError(
            f"no {S.MANIFEST} in {raw_dir.name}. Temporality is declared by "
            "whatever ran the pull and is never inferred (VF-17 rule 5); "
            "inference got 2 of 6 files wrong on 2026-08-19. Refusing to label "
            "facts."
        )
    # -- consolidated validation (one pass, every contradiction) ---------
    # A contradiction is recorded rather than raised, so a raw directory with
    # three bad pulls reports all three in one build instead of one per
    # build cycle. A single contradiction re-raises the original exception,
    # so every message and type below is unchanged. Nothing is written on
    # any failure - problems.raise_if_any() sits before the first fact.
    problems = pulls.ProblemLog()

    missing = [name for name in REQUIRED_PULLS if name not in manifest.files]
    if missing:
        problems.record(BuildError(
            f"undeclared in {S.MANIFEST}: {', '.join(missing)}. An undeclared "
            "pull produces figures of unknown temporality, which is the failure "
            "VF-17 exists to prevent."
        ))
        for name in missing:
            problems.mark_broken(name)

    # -- windows and scopes, both declared -------------------------------
    windows: dict = {}
    scopes: dict = {}
    for name in REQUIRED_PULLS:
        if problems.broken(name):
            continue
        declared = manifest.declared_filter(name) or ""
        line = problems.run(
            gaql.check_declared_temporality,
            declared, manifest.declared_temporality(name) or "",
            file=name,
        )
        if line:
            trail.append(line + f"  [{name}]")
        windows[name] = resolve_relative_window(
            manifest.window_for(name, windowed_fallback=False), pulled
        )
        scope = problems.run(nontemporal_scope, manifest, name, file=name)
        if scope is None:
            continue
        scopes[name] = scope
        kept = [c for c in gaql.clauses(declared)
                if not gaql.is_temporality(c)]
        if kept:
            line = problems.run(
                gaql.assert_semantic_match, name, declared, scopes[name],
                file=name,
            )
            if line:
                trail.append(line)
        else:
            trail.append(
                f"{name}: the declared filter is a date predicate and nothing "
                "else, so there is no non-temporal clause to check against the "
                "scope label. The label says so in words rather than borrowing "
                "a filter the pull did not apply."
            )

    if (not problems.broken(CAMPAIGNS)
            and not is_current_state_window(windows[CAMPAIGNS])):
        problems.record(BuildError(
            f"{CAMPAIGNS} is declared {windows[CAMPAIGNS]!r}, but every "
            "current-state fact in this skill comes from it and VF-17 rule 4 "
            "requires `current-as-of-<date>`."
        ))
    CURRENT = current_state_window(as_of)

    # -- load ------------------------------------------------------------
    loaded = {
        name: problems.run(load_results, raw_dir, name, file=name)
        for name in REQUIRED_PULLS
    }
    campaign_rows = loaded[CAMPAIGNS]
    cost7_rows = loaded[COST_7D]
    costp_rows = loaded[COST_PRIOR]
    conv7_rows = loaded[CONV_7D]
    convp_rows = loaded[CONV_PRIOR]
    mtd_rows = loaded[SPEND_MTD]
    change_rows = loaded[CHANGES]

    for name in REQUIRED_PULLS:
        if loaded[name] is None:
            continue
        line = problems.run(
            manifest.check_row_count, name, len(loaded[name]), file=name
        )
        if line:
            trail.append(line)

    # Row/key validation is part of the same consolidated pass: a duplicate
    # key is a stale pull, and the operator should learn about it alongside
    # any manifest problem rather than one build later.
    structure = (
        problems.run(one_row_per_campaign, CAMPAIGNS, campaign_rows,
                     file=CAMPAIGNS)
        if campaign_rows is not None else None
    )
    cost7 = (problems.run(one_row_per_campaign, COST_7D, cost7_rows,
                          file=COST_7D)
             if cost7_rows is not None else None)
    costp = (problems.run(one_row_per_campaign, COST_PRIOR, costp_rows,
                          file=COST_PRIOR)
             if costp_rows is not None else None)
    conv7_pair = (problems.run(conversions_per_campaign, CONV_7D, conv7_rows,
                               file=CONV_7D)
                  if conv7_rows is not None else None)
    convp_pair = (problems.run(conversions_per_campaign, CONV_PRIOR,
                               convp_rows, file=CONV_PRIOR)
                  if convp_rows is not None else None)
    per_day = (problems.run(daily_spend, SPEND_MTD, mtd_rows, file=SPEND_MTD)
               if mtd_rows is not None else None)

    # The two-query pair must share a non-temporal filter, or no delta
    # between them is valid (VF-3 + the scope-match rule).
    def _scope_pair_check() -> None:
        if (scopes.get(COST_7D) != scopes.get(COST_PRIOR)
                or scopes.get(CONV_7D) != scopes.get(CONV_PRIOR)):
            raise BuildError(
                "the 7-day and prior-week pulls do not share a non-temporal "
                "filter, so their facts differ on scope as well as window and "
                "no delta between them is valid. Re-pull the prior week on "
                "the same filter."
            )
    if all(name in scopes
           for name in (COST_7D, COST_PRIOR, CONV_7D, CONV_PRIOR)):
        problems.run(_scope_pair_check)

    # The two weeks must be a real pair: equal length, adjacent, finished.
    # Anchored (a..b both sides) or rolling (LAST_7_DAYS + the adjacent
    # range); a prior week declared as a relative token is one week counted
    # twice and is refused (builder_core.verify_week_pair).
    for cur_name, pri_name in ((COST_7D, COST_PRIOR), (CONV_7D, CONV_PRIOR)):
        if problems.broken(cur_name) or problems.broken(pri_name):
            continue
        line = problems.run(
            core.verify_week_pair,
            manifest.declared_temporality(cur_name) or "",
            manifest.declared_temporality(pri_name) or "",
            pulled, cur_name, pri_name,
            file=cur_name,
        )
        if line:
            trail.append(line)

    problems.raise_if_any(raw_dir.name)
    # Everything below runs only on a fully-validated raw directory.
    conv7, conv7_meta = conv7_pair
    convp, convp_meta = convp_pair
    names = {campaign_id(r): (r.get("campaign") or {}).get("name")
             for r in campaign_rows}
    channels = {campaign_id(r): (r.get("campaign") or {}).get(
        "advertisingChannelType") for r in campaign_rows}
    learning = learning_state(change_rows, pulled)
    trail.append(
        f"{CONV_7D}: {conv7_meta['row_keys']} distinct (campaign.id, "
        f"segments.conversion_action_name) row keys over {len(conv7_rows)} rows, "
        f"aggregated onto {len(conv7)} campaigns (VF-19)"
    )
    trail.append(
        f"{CAMPAIGNS}: {len(campaign_rows)} rows, {len(structure)} distinct "
        "campaign.id - one row per campaign, response not segmented"
    )

    targets = {campaign_id(r): read_target(r.get("campaign") or {})
               for r in campaign_rows}
    legacy_hits = [cid for cid, r in
                   ((campaign_id(r), r.get("campaign") or {})
                    for r in campaign_rows)
                   if legacy_target_present(r)]
    trail.append(
        f"{CAMPAIGNS}: {sum(1 for v in targets.values() if v)} of "
        f"{len(campaign_rows)} campaigns carry a bid target, read from the "
        "bidding-strategy sub-message. The legacy campaign.target_roas field is "
        f"populated on {len(legacy_hits)} of them. An empty target field is a "
        "field-name bug until proven otherwise: the first 2026-08-20 pull "
        "selected only the legacy field, it returned empty for all 36, and that "
        "read as 'no campaign has a target'."
    )

    # Ordered by this week's spend, descending, so every per-campaign fact and
    # every table row in the report comes out in the same order on every run.
    spending = sorted(
        (cid for cid, m in cost7.items() if cost_of(m) > 0),
        key=lambda c: -cost_of(cost7[c]),
    )
    ever_spent = sorted(
        set(spending) | {cid for cid, m in costp.items() if cost_of(m) > 0},
        key=lambda c: -cost_of(cost7.get(c, {})),
    )

    # A target that moved inside the window cannot be compared against a
    # measurement taken across it (guardrail 7 + VF-17). Read the campaigns
    # whose target changed from the change log, not from the target value.
    target_changed = {
        cid for cid, events in learning["per_campaign"].items()
        if any("targetroas" in (e["changed_fields"] or "").lower()
               or "targetcpa" in (e["changed_fields"] or "").lower()
               for e in events)
    }

    plan = BudgetPlan(plan_path)

    fs = FactSet(meta={
        "skill": "weekly-review",
        "raw_dir": str(raw_dir),
        "pulled": as_of,
        "currency": "CAD",
        "contract": "VERIFICATION-RULES VF-15, built by tools/build_weekly_review_facts.py",
        "budget_source": f"{plan_path.name} (read, never derived - VF-14)",
        "manifest_audit": [manifest.audit_line(n) for n in REQUIRED_PULLS],
        "undeclared_pulls": manifest.undeclared_files(),
        "scope_trail": trail,
        "derivations": [],
        "suppressed_shares": [],
        "gated": [],
        "learning": learning,
        "shared_module_defects": [
            "none open. The three local workarounds this builder carried "
            "(literal clause split, LAST_N_DAYS window resolution, both-ends "
            "share sentinels) were de-forked 2026-08-27 into their shared "
            "homes: gaql.split_literal(), scopes.resolve_relative_window() "
            "and builder_core.share_sentinel(). History in MAINTENANCE.md.",
        ],
    })

    W7 = windows[COST_7D]
    WP = windows[COST_PRIOR]
    WC7 = windows[CONV_7D]
    WCP = windows[CONV_PRIOR]
    SC = scopes[COST_7D]
    SV = scopes[CONV_7D]
    # The pair-scope equality check ran in the consolidated validation pass
    # above (_scope_pair_check), so reaching here means SC == scopes[COST_PRIOR]
    # and SV == scopes[CONV_PRIOR].
    CONV_CLAUSE = frozenset(
        set(gaql.clauses(SV)) ^ set(gaql.clauses(SC))
    )
    trail.append(
        "the cost and conversion pulls differ by exactly "
        f"{sorted(CONV_CLAUSE)} - the two-query rule's forced split (VF-3). "
        "Every CPA and ROAS asserts that difference and no other."
    )

    run = Run(
        manifest=manifest,
        pulled=pulled,
        as_of=as_of,
        CURRENT=CURRENT,
        windows=windows,
        scopes=scopes,
        trail=trail,
        names=names,
        channels=channels,
        structure=structure,
        campaign_rows=campaign_rows,
        cost7=cost7,
        costp=costp,
        conv7=conv7,
        convp=convp,
        conv7_meta=conv7_meta,
        per_day=per_day,
        change_rows=change_rows,
        learning=learning,
        plan=plan,
        plan_path=plan_path,
        spending=spending,
        targets=targets,
        target_changed=target_changed,
        legacy_hits=legacy_hits,
        ever_spent=ever_spent,
        W7=W7,
        WP=WP,
        WC7=WC7,
        WCP=WCP,
        SC=SC,
        SV=SV,
        CONV_CLAUSE=CONV_CLAUSE,
    )
    return fs, run


def add_structure(fs: FactSet, run: Run) -> None:
    """Structure and shared budgets, current state."""
    CURRENT = run.CURRENT
    scopes = run.scopes
    names = run.names
    channels = run.channels
    structure = run.structure
    campaign_rows = run.campaign_rows
    targets = run.targets
    legacy_hits = run.legacy_hits
    spending = run.spending
    target_changed = run.target_changed

    def current(id, value, unit, basis="n/a"):
        return fs.add(id=id, value=value, unit=unit, scope=scopes[CAMPAIGNS],
                      window=CURRENT, source=CAMPAIGNS, basis=basis)

    current("campaigns.enabled.count", len(structure), "count")
    current("campaigns.enabled.search.count",
            sum(1 for c in channels.values() if c == "SEARCH"), "count")

    # Read from `targets`, which reads the bidding-strategy sub-message. The
    # obvious version of this line - `row.get("targetRoas")` - is the legacy
    # field, empty on all 36, and it reported 1 campaign with a target when 15
    # have one.
    with_target = [cid for cid, target in targets.items() if target]
    troas_named_no_target = [
        cid for cid in names
        if "troas" in (names[cid] or "").lower() and cid not in with_target
    ]
    current("campaigns.with_bid_target_set.count", len(with_target), "count")
    current("campaigns.with_target_roas.count",
            sum(1 for v in targets.values()
                if v and v["kind"] == "target_roas"), "count")
    current("campaigns.with_target_cpa.count",
            sum(1 for v in targets.values()
                if v and v["kind"] == "target_cpa"), "count")
    current("campaigns.troas_named.without_target.count",
            len(troas_named_no_target), "count")
    fs.add(id="campaigns.legacy_target_roas_field.populated.count",
           value=len(legacy_hits), unit="count",
           scope=(
               f"{scopes[CAMPAIGNS]} | campaign.target_roas.target_roas, the "
               "LEGACY field, pulled as a control. It is empty in this account "
               "while 14 campaigns carry a target on "
               "campaign.maximize_conversion_value.target_roas. A query on the "
               "legacy field alone succeeds and its empty result reads as 'no "
               "campaign has a target'. An empty target field is a field-name "
               "bug until proven otherwise"
           ),
           window=CURRENT, source=CAMPAIGNS, basis="n/a")
    for cid in spending:
        target = targets.get(cid)
        if not target:
            continue
        fs.add(id=f"target.{target['kind']}.campaign.{cid}",
               value=target["value"],
               unit=("target_roas_ratio" if target["kind"] == "target_roas"
                     else "currency_cad"),
               scope=f"{scopes[CAMPAIGNS]} | read from {target['field']}",
               window=CURRENT, source=CAMPAIGNS, basis="n/a")
    fs.meta["targets"] = {
        "read_from": "the bidding-strategy sub-message, never the legacy field",
        "per_campaign": {cid: (dict(v, value=str(v["value"])) if v else None)
                         for cid, v in targets.items()},
        "changed_inside_the_window": sorted(target_changed),
        "troas_named_without_target": sorted(troas_named_no_target),
    }

    budget_groups: dict = collections.defaultdict(list)
    for row in campaign_rows:
        bid = (row.get("campaignBudget") or {}).get("id")
        budget_groups[bid].append(campaign_id(row))
    shared = {bid: ids for bid, ids in budget_groups.items()
              if bid and len(ids) > 1}
    current("budget.shared_clusters.count", len(shared), "count")
    current("campaigns.in_shared_budget.count",
            sum(len(ids) for ids in shared.values()), "count")
    explicitly_shared = sum(
        1 for row in campaign_rows
        if (row.get("campaignBudget") or {}).get("explicitlyShared") is True
    )
    current("budget.explicitly_shared_flag.true.count", explicitly_shared,
            "count")
    fs.meta["shared_budget_clusters"] = {
        bid: [names[c] for c in ids] for bid, ids in shared.items()
    }
    fs.meta.setdefault("raw_pull_defects", []).append(
        f"{CAMPAIGNS}: campaignBudget.explicitlyShared reads false on all "
        f"{len(structure)} campaigns, yet {len(shared)} budget ids are genuinely "
        f"shared across {sum(len(v) for v in shared.values())} campaigns. Read "
        "cluster membership from campaignBudget.id groupings, never from the "
        "explicitlyShared flag."
    )
    distinct_budgets = {
        (r.get("campaignBudget") or {}).get("id"): r
        for r in campaign_rows
    }
    unreadable_budgets = sorted(
        bid for bid, row in distinct_budgets.items()
        if bid and (row.get("campaignBudget") or {}).get("amountMicros") is None
    )
    current("budget.distinct.count", len(distinct_budgets), "count")
    current("budget.readable_amount.count",
            len(distinct_budgets) - len(unreadable_budgets), "count")
    current("budget.unreadable_amount.count", len(unreadable_budgets), "count")
    daily_budget = sum(
        (micros_to_currency((row.get("campaignBudget") or {}).get("amountMicros"))
         or Decimal(0))
        for row in distinct_budgets.values()
    )
    daily_budget_scope = (
        f"{scopes[CAMPAIGNS]} - keyed on campaignBudget.id, one amount per "
        f"distinct budget. A FLOOR, not a total, while {len(unreadable_budgets)} "
        f"budget(s) ({', '.join(unreadable_budgets) or 'none'}) return no "
        "amountMicros"
    )
    fs.add(id="budget.daily_total.cad", value=money(daily_budget),
           unit="currency_cad", scope=daily_budget_scope, window=CURRENT,
           source=CAMPAIGNS, basis="n/a")


def add_volume(fs: FactSet, run: Run) -> None:
    """Spend, impressions and clicks, per window and segment."""
    cost7 = run.cost7
    costp = run.costp
    spending = run.spending
    ever_spent = run.ever_spent
    W7 = run.W7
    WP = run.WP
    SC = run.SC

    def spend_facts(tag: str, window: str, table: dict, source: str):
        totals = collections.Counter()
        for cid, metrics in table.items():
            totals[bucket(cid)] += cost_of(metrics)
            totals["account"] += cost_of(metrics)
            totals["impressions"] += int_of(metrics, "impressions")
            totals["clicks"] += int_of(metrics, "clicks")
        for key in ("account", "brand", "nonbrand", "tof"):
            fs.add(id=f"spend.{key}.{tag}", value=money(totals[key]),
                   unit="currency_cad", scope=SC, window=window, source=source,
                   basis="n/a")
        fs.add(id=f"impressions.account.{tag}", value=totals["impressions"],
               unit="impressions", scope=SC, window=window, source=source,
               basis="n/a")
        fs.add(id=f"clicks.account.{tag}", value=totals["clicks"],
               unit="clicks", scope=SC, window=window, source=source,
               basis="n/a")
        return totals

    t7 = spend_facts("7d", W7, cost7, COST_7D)
    tp = spend_facts("prior7d", WP, costp, COST_PRIOR)

    tof_impressions = sum(int_of(cost7.get(c, {}), "impressions")
                          for c in TOF_CAMPAIGN_IDS)
    tof_clicks = sum(int_of(cost7.get(c, {}), "clicks")
                     for c in TOF_CAMPAIGN_IDS)
    fs.add(id="impressions.tof.7d", value=tof_impressions, unit="impressions",
           scope=SC, window=W7, source=COST_7D, basis="n/a")
    fs.add(id="clicks.tof.7d", value=tof_clicks, unit="clicks", scope=SC,
           window=W7, source=COST_7D, basis="n/a")

    for cid in ever_spent:
        fs.add(id=f"spend.campaign.{cid}.7d",
               value=money(cost_of(cost7.get(cid, {}))), unit="currency_cad",
               scope=SC, window=W7, source=COST_7D, basis="n/a")
        fs.add(id=f"spend.campaign.{cid}.prior7d",
               value=money(cost_of(costp.get(cid, {}))), unit="currency_cad",
               scope=SC, window=WP, source=COST_PRIOR, basis="n/a")

    fs.add(id="campaigns.with_spend.7d.count", value=len(spending), unit="count",
           scope=SC, window=W7, source=COST_7D, basis="n/a")


def add_conversions(fs: FactSet, run: Run) -> None:
    """8-action conversions from the second query."""
    names = run.names
    cost7 = run.cost7
    conv7 = run.conv7
    convp = run.convp
    conv7_meta = run.conv7_meta
    spending = run.spending
    ever_spent = run.ever_spent
    W7, WC7, WCP, SC, SV = run.W7, run.WC7, run.WCP, run.SC, run.SV

    def conv_facts(tag: str, window: str, table: dict, source: str):
        totals = collections.Counter()
        for cid, (count, value) in table.items():
            totals[bucket(cid)] += count
            totals["account"] += count
            totals[f"value_{bucket(cid)}"] += value
            totals["value_account"] += value
        for key in ("account", "brand", "nonbrand", "tof"):
            fs.add(id=f"conversions.{key}.{tag}", value=totals[key],
                   unit="conversions_8_action", scope=SV, window=window,
                   source=source, basis="8-action")
        # Per bucket, not just the account. Without the brand/non-brand split
        # here there is no non-brand ROAS to cite, and on 2026-08-24 the report
        # said "no non-brand ROAS figure is available this run" and fell back to
        # the blended number guardrail 2 forbids leading with.
        for key in ("account", "brand", "nonbrand", "tof"):
            fs.add(id=f"conversion_value.assumed.{key}.{tag}",
                   value=money(totals[f"value_{key}"]),
                   unit="currency_cad_assumed_value", scope=SV, window=window,
                   source=source, basis="8-action")
        return totals

    c7t = conv_facts("7d", WC7, conv7, CONV_7D)
    cpt = conv_facts("prior7d", WCP, convp, CONV_PRIOR)

    for cid in ever_spent:
        fs.add(id=f"conversions.campaign.{cid}.7d",
               value=conv7.get(cid, [Decimal(0)])[0],
               unit="conversions_8_action", scope=SV, window=WC7,
               source=CONV_7D, basis="8-action")
        fs.add(id=f"conversions.campaign.{cid}.prior7d",
               value=convp.get(cid, [Decimal(0)])[0],
               unit="conversions_8_action", scope=SV, window=WCP,
               source=CONV_PRIOR, basis="8-action")

    fs.add(id="conversions.actions_present.7d.count",
           value=len(conv7_meta["actions"]), unit="count", scope=SV, window=WC7,
           source=CONV_7D, basis="8-action")
    fs.add(id="conversions.eight_actions.defined.count",
           value=len(S.EIGHT_ACTIONS), unit="count", scope=SV, window=WC7,
           source=CONV_7D, basis="8-action")

    spend_no_conv = [cid for cid in spending if cid not in conv7]
    conv_no_spend = [cid for cid in conv7 if cost_of(cost7.get(cid, {})) == 0]
    fs.add(id="join.spend_without_conversions.7d.count",
           value=len(spend_no_conv), unit="count", scope=SC, window=W7,
           source=f"{COST_7D} + {CONV_7D}", basis="8-action")
    fs.add(id="join.conversions_without_spend.7d.count",
           value=len(conv_no_spend), unit="count", scope=SC, window=W7,
           source=f"{COST_7D} + {CONV_7D}", basis="8-action")
    fs.meta["join_completeness"] = {
        "spend_without_conversions": [
            {"id": c, "name": names.get(c), "spend_cad": str(money(cost_of(cost7[c])))}
            for c in spend_no_conv
        ],
        "conversions_without_spend": [
            {"id": c, "name": names.get(c)} for c in conv_no_spend
        ],
    }

    above_floor = [cid for cid in spending
                   if conv7.get(cid, [Decimal(0)])[0] >= CONVERSION_FLOOR]
    fs.add(id="campaigns.above_conversion_floor.7d.count",
           value=len(above_floor), unit="count", scope=SV, window=WC7,
           source=CONV_7D, basis="8-action")
    fs.meta["conversion_floor"] = {
        "floor": CONVERSION_FLOOR,
        "above": [{"id": c, "name": names.get(c),
                   "conversions": str(conv7[c][0])} for c in above_floor],
        "rule": (
            "guardrail 5: below the floor, the same observation is a 'monitor' "
            "note and never a bid, budget or target recommendation."
        ),
    }


def add_efficiency(fs: FactSet, run: Run) -> None:
    """CPA and ROAS: the two-query join."""
    plan = run.plan
    plan_path = run.plan_path
    ever_spent = run.ever_spent
    W7 = run.W7
    WP = run.WP
    WC7 = run.WC7
    WCP = run.WCP
    SC = run.SC
    SV = run.SV
    CONV_CLAUSE = run.CONV_CLAUSE
    spending = run.spending
    targets = run.targets
    target_changed = run.target_changed
    conv7 = run.conv7
    names = run.names
    as_of = run.as_of

    CPA_RULE = (
        "cost and 8-action conversions cannot be selected in one query "
        "(VF-3, HTTP 400), so a CPA necessarily joins two pulls whose filters "
        "differ by exactly the conversion-action clause. The join is valid only "
        "because that difference is asserted to be the only one."
    )
    VF8 = (
        "assumed-value figure: the 8 conversion actions all carry "
        "always_use_default_value=True, so their value is an assigned constant "
        "for value-based bidding, not observed revenue (VF-2, VF-8). The "
        "qualifier belongs in the same sentence as the figure."
    )

    def roas_fact(id, value_id, cost_id, window, scope_note):
        """Assumed-value ROAS for one bucket, or None when it has no spend."""
        cost_v = Decimal(str(fs.value(cost_id)))
        if cost_v == 0:
            return None
        return related_fact(
            fs, id, parents=(value_id, cost_id),
            profile="cost-over-conversions",
            value=Decimal(str(fs.value(value_id))) / cost_v,
            unit="assumed_value_roas_ratio",
            scope=f"{scope_note} | {VF8}", window=window, basis="8-action",
            rule=CPA_RULE, scope_delta=CONV_CLAUSE,
        )

    def cpa_fact(id, cost_id, conv_id, window, scope_note):
        cost_v = Decimal(str(fs.value(cost_id)))
        conv_v = Decimal(str(fs.value(conv_id)))
        if conv_v == 0:
            return None
        return related_fact(
            fs, id, parents=(cost_id, conv_id), profile="cost-over-conversions",
            value=money(cost_v / conv_v),
            unit="currency_cad_assumed_value_cpa",
            scope=f"{scope_note} | {VF8}", window=window, basis="8-action",
            rule=CPA_RULE, scope_delta=CONV_CLAUSE,
        )

    for tag, window, conv_window in (("7d", W7, WC7),
                                     ("prior7d", WP, WCP)):
        joined = f"{SC} joined to {SV} on campaign.id"
        cpa_fact(f"cpa.account.blended.assumed_value.{tag}",
                 f"spend.account.{tag}", f"conversions.account.{tag}",
                 f"{window} joined to {conv_window}",
                 f"{joined} | BLENDED across brand, non-brand and TOF: "
                 "guardrail 2 forbids leading with this figure, because Brand's "
                 "cheap conversions flatter Generic's expensive ones")
        cpa_fact(f"cpa.brand.assumed_value.{tag}", f"spend.brand.{tag}",
                 f"conversions.brand.{tag}", f"{window} joined to {conv_window}",
                 f"{joined}, Brand Core and its siblings only")
        cpa_fact(f"cpa.nonbrand.assumed_value.{tag}", f"spend.nonbrand.{tag}",
                 f"conversions.nonbrand.{tag}",
                 f"{window} joined to {conv_window}",
                 f"{joined}, non-brand Search and Performance Max only. TOF "
                 "(YouTube, Demand Gen) is excluded from the denominator: "
                 "guardrail 1 forbids judging it on BOFU CPA, so leaving its "
                 "spend in would be a correct number under a wrong label")
        roas_window = f"{conv_window} joined to {window}"
        roas_fact(f"roas.account.assumed_value.{tag}",
                  f"conversion_value.assumed.account.{tag}",
                  f"spend.account.{tag}", roas_window,
                  f"{joined} | BLENDED across brand, non-brand and TOF: "
                  "guardrail 2 forbids leading with this figure. Brand carries "
                  "a small share of spend and most of the assumed value, so "
                  "this ratio tracks Brand conversion volume more than account "
                  "efficiency - quote the two subtotals below beside it")
        roas_fact(f"roas.brand.assumed_value.{tag}",
                  f"conversion_value.assumed.brand.{tag}",
                  f"spend.brand.{tag}", roas_window,
                  f"{joined}, Brand Core and its siblings only")
        roas_fact(f"roas.nonbrand.assumed_value.{tag}",
                  f"conversion_value.assumed.nonbrand.{tag}",
                  f"spend.nonbrand.{tag}", roas_window,
                  f"{joined}, non-brand Search and Performance Max only. TOF "
                  "(YouTube, Demand Gen) is excluded from the denominator: "
                  "guardrail 1 forbids judging it on BOFU CPA, so leaving its "
                  "spend in would be a correct number under a wrong label")
        # No roas.tof.*: guardrail 1 forbids judging TOF on a BOFU value
        # ratio, exactly as no cpa.tof.* is emitted above. Its spend and
        # assumed value are both emitted, so anyone decomposing the account
        # blend can still close the arithmetic without a labelled TOF ROAS.

    for cid in ever_spent:
        cpa_fact(f"cpa.campaign.{cid}.assumed_value.7d",
                 f"spend.campaign.{cid}.7d",
                 f"conversions.campaign.{cid}.7d",
                 f"{W7} joined to {WC7}",
                 f"{SC} joined to {SV} on campaign.id, campaign {cid}")

    # Per-campaign assumed-value ROAS, then the gap against the live target.
    TARGET_RULE = (
        "actual versus target. The actual is measured across a window and the "
        "target is a live setting, so the two can never share a window; the "
        "guard instead requires that the target did not CHANGE inside the "
        "window, read from the change log. The goal-basis caveat applies: our "
        "actual is 8-actions-only and Google manages the target against a "
        "broader goal set, so the gap is directional, not settled."
    )
    for cid in spending:
        cost = Decimal(str(fs.value(f"spend.campaign.{cid}.7d")))
        value = conv7.get(cid, [Decimal(0), Decimal(0)])[1]
        if cost == 0 or value == 0:
            continue
        value_id = f"conversion_value.assumed.campaign.{cid}.7d"
        fs.add(id=value_id, value=money(value),
               unit="currency_cad_assumed_value", scope=SV, window=WC7,
               source=CONV_7D, basis="8-action")
        roas_id = f"roas.campaign.{cid}.assumed_value.7d"
        related_fact(
            fs, roas_id, parents=(value_id, f"spend.campaign.{cid}.7d"),
            profile="cost-over-conversions", value=value / cost,
            unit="assumed_value_roas_ratio",
            scope=f"{SC} joined to {SV} on campaign.id, campaign {cid} | {VF8}",
            window=f"{WC7} joined to {W7}", basis="8-action", rule=CPA_RULE,
            scope_delta=CONV_CLAUSE,
        )
        target = targets.get(cid)
        target_id = f"target.target_roas.campaign.{cid}"
        if not target or target["kind"] != "target_roas":
            continue
        if cid in target_changed:
            fs.meta.setdefault("target_gap_suppressed", []).append({
                "campaign_id": cid, "campaign": names.get(cid),
                "reason": (
                    "the target ROAS was edited inside the change-event window, "
                    "so a gap measured across it compares the actual against "
                    "two different targets. Report the edit and its date "
                    "instead (guardrail 7)."
                ),
            })
            continue
        actual = value / cost
        related_fact(
            fs, f"target_gap.roas.campaign.{cid}.pct",
            parents=(roas_id, target_id), profile="actual-vs-target",
            value=((actual / target["value"] - 1) * Decimal(100)).quantize(
                Decimal("0.01")),
            unit="percent_vs_target",
            scope=(
                f"{SC} joined to {SV} on campaign.id, campaign {cid} | actual "
                f"assumed-value ROAS against the live target read from "
                f"{target['field']} | {VF8}"
            ),
            window=f"actual {WC7} joined to {W7}, against the target as of "
                   f"{as_of}",
            basis="8-action", rule=TARGET_RULE,
        )

    fs.add(id="cac.google_ads.real.usd", value=money(plan.cac_usd),
           unit="currency_usd",
           scope=(
               f"{plan_path.name}, 'CPA and CAC Calculations' tab, "
               f"{plan.cac_month}: real cost per real customer, the only "
               "revenue-side anchor this pack has. Quote it beside every "
               "assumed-value figure (VF-8)"
           ),
           window=plan.cac_month, source=plan_path.name, basis="n/a")


def add_wow(fs: FactSet, run: Run) -> None:
    """Week over week: every delta is a third fact."""
    ever_spent = run.ever_spent
    W7 = run.W7
    WP = run.WP
    WC7 = run.WC7
    WCP = run.WCP
    SC = run.SC
    SV = run.SV

    for key in ("account", "brand", "nonbrand", "tof"):
        delta_pair(fs, f"spend.{key}", f"spend.{key}.7d", f"spend.{key}.prior7d",
                   "currency_cad", "n/a", SC, W7, WP)
    # All four, not just account and non-brand. The 2026-08-24 report's
    # headline claim was that Brand had the largest conversion drop of the
    # three buckets; no brand or TOF delta fact existed to carry it.
    for key in ("account", "brand", "nonbrand", "tof"):
        delta_pair(fs, f"conversions.{key}", f"conversions.{key}.7d",
                   f"conversions.{key}.prior7d", "conversions_8_action",
                   "8-action", SV, WC7, WCP)
    # The ROAS numerator. Reading it beside the conversion delta is what shows
    # whether a bucket's conversions changed in number or in assigned value:
    # on 2026-08-24 non-brand's count moved -4.88% and its value -26.74%, which
    # is the whole reason its CPA improved while its ROAS fell.
    for key in ("account", "brand", "nonbrand", "tof"):
        delta_pair(fs, f"conversion_value.assumed.{key}",
                   f"conversion_value.assumed.{key}.7d",
                   f"conversion_value.assumed.{key}.prior7d",
                   "currency_cad_assumed_value", "8-action", SV, WC7, WCP)
    for base in ("cpa.account.blended.assumed_value",
                 "cpa.brand.assumed_value", "cpa.nonbrand.assumed_value",
                 "roas.account.assumed_value", "roas.brand.assumed_value",
                 "roas.nonbrand.assumed_value"):
        later, earlier = f"{base}.7d", f"{base}.prior7d"
        if later in fs and earlier in fs:
            a, b = fs.get(later), fs.get(earlier)
            delta_pair(fs, base, later, earlier, a.unit, "8-action", a.scope,
                       a.window, b.window)

    for cid in ever_spent:
        later, earlier = f"spend.campaign.{cid}.7d", f"spend.campaign.{cid}.prior7d"
        if Decimal(str(fs.value(earlier))) == 0:
            continue
        delta_pair(fs, f"spend.campaign.{cid}", later, earlier, "currency_cad",
                   "n/a", SC, W7, WP)


def add_impression_share(fs: FactSet, run: Run) -> None:
    """Impression share, both sentinels."""
    names = run.names
    cost7 = run.cost7
    costp = run.costp
    spending = run.spending
    W7 = run.W7
    WP = run.WP
    SC = run.SC

    GATE = (
        "GATED: not cleared to ship. Available impressions is held pending the "
        "account owner confirming one figure against their own Google Ads custom "
        "column (VF-11). Until then this figure may appear in an internal draft "
        "marked gated and must not go to the stakeholder."
    )
    usable = 0
    sentinel_hits = 0
    not_reported = 0
    for cid in spending:
        metrics = cost7.get(cid, {})
        impressions = int_of(metrics, "impressions")
        for field, label in (("searchImpressionShare", "impression_share"),
                             ("searchBudgetLostImpressionShare", "lost_is_budget"),
                             ("searchRankLostImpressionShare", "lost_is_rank"),
                             ("searchAbsoluteTopImpressionShare", "abs_top_is")):
            raw = metrics.get(field)
            reason = share_sentinel(raw)
            if reason:
                fs.meta["suppressed_shares"].append({
                    "pull": COST_7D, "campaign_id": cid,
                    "campaign": names.get(cid), "field": field,
                    "raw_value": raw, "reason": reason,
                })
                fs.add(id=f"{label}.campaign.{cid}.7d", value=None,
                       unit="ratio_impression_share", scope=SC, window=W7,
                       source=COST_7D, basis="n/a")
                continue
            fs.add(id=f"{label}.campaign.{cid}.7d", value=Decimal(str(raw)),
                   unit="ratio_impression_share", scope=SC, window=W7,
                   source=COST_7D, basis="n/a")

        # Two different reasons to suppress, counted separately. A sentinel is
        # a real share Google refused to disclose precisely; an absent field on
        # a Demand Gen or Video campaign is not a search auction at all. One
        # count covering both would read as six campaigns hiding a share.
        share = metrics.get("searchImpressionShare")
        if share is None:
            not_reported += 1
            continue
        if share_sentinel(share):
            sentinel_hits += 1
            continue
        usable += 1
        fs.add(id=f"available_impressions.gated.campaign.{cid}.7d",
               value=int(round(available_impressions(impressions, float(share)))),
               unit="impressions", scope=f"{SC} | {GATE}", window=W7,
               source=COST_7D, basis="n/a")
        fs.meta["gated"].append({
            "id": f"available_impressions.gated.campaign.{cid}.7d",
            "campaign": names.get(cid), "reason": GATE,
        })

    fs.add(id="impression_share.usable.7d.count", value=usable, unit="count",
           scope=SC, window=W7, source=COST_7D, basis="n/a")
    fs.add(id="impression_share.sentinel_suppressed.7d.count",
           value=sentinel_hits, unit="count",
           scope=f"{SC} | share returned as a sentinel bound, not a value",
           window=W7, source=COST_7D, basis="n/a")
    fs.add(id="impression_share.not_reported.7d.count", value=not_reported,
           unit="count",
           scope=(
               f"{SC} | the field is absent because the campaign is not in a "
               "search auction (Demand Gen, Video). Not a suppression"
           ),
           window=W7, source=COST_7D, basis="n/a")

    # The prior week is scanned for sentinels as well, even though no
    # per-campaign prior-week share fact is emitted. This account returns the
    # 0.9001 '> 90%' sentinel on search_rank_lost_impression_share in the prior
    # week only, so a scan of this week alone would leave a reader free to quote
    # "90.0% lost to rank" from the raw file as if it were a value.
    for cid, metrics in costp.items():
        for field in SHARE_FIELDS:
            if field not in metrics:
                continue
            reason = share_sentinel(metrics[field])
            if reason:
                fs.meta["suppressed_shares"].append({
                    "pull": COST_PRIOR, "campaign_id": cid,
                    "campaign": names.get(cid), "field": field,
                    "raw_value": metrics[field], "reason": reason,
                })

    prior_share_fields = set()
    for metrics in costp.values():
        prior_share_fields |= {f for f in SHARE_FIELDS if f in metrics}
    absent_prior = [f for f in SHARE_FIELDS
                    if f in {k for m in cost7.values() for k in m}
                    and f not in prior_share_fields]
    if absent_prior:
        fs.meta.setdefault("raw_pull_defects", []).append(
            f"{COST_PRIOR}: the prior-week pull's field mask omits "
            f"{', '.join(absent_prior)}, which {COST_7D} selects. No "
            "week-over-week comparison is possible on those metrics; the report "
            "must say so rather than omit the row."
        )

    brand_rank_now = cost7.get(BRAND_CORE_ID, {}).get(
        "searchRankLostImpressionShare")
    brand_rank_prior = costp.get(BRAND_CORE_ID, {}).get(
        "searchRankLostImpressionShare")
    BRAND_TRIGGER = (
        "guardrail 2: the ONLY admissible trigger for a Brand budget or bid "
        "recommendation is search_rank_lost_impression_share on Brand Core "
        "RISING week over week. Both weeks' values are quoted beside any such "
        "recommendation. A cheap Brand CPA is not evidence of headroom."
    )
    if not share_sentinel(brand_rank_now) and not share_sentinel(brand_rank_prior):
        fs.add(id="lost_is_rank.brand_core.prior7d",
               value=Decimal(str(brand_rank_prior)),
               unit="ratio_impression_share", scope=f"{SC} | {BRAND_TRIGGER}",
               window=WP, source=COST_PRIOR, basis="n/a")
        fs.add(id="lost_is_rank.brand_core.7d.trigger",
               value=Decimal(str(brand_rank_now)),
               unit="ratio_impression_share", scope=f"{SC} | {BRAND_TRIGGER}",
               window=W7, source=COST_7D, basis="n/a")
        delta_pair(fs, "lost_is_rank.brand_core", "lost_is_rank.brand_core.7d.trigger",
                   "lost_is_rank.brand_core.prior7d", "ratio_impression_share",
                   "n/a", f"{SC} | {BRAND_TRIGGER}", W7, WP)


def add_pacing(fs: FactSet, run: Run) -> None:
    """Real pacing against the read budget."""
    windows = run.windows
    scopes = run.scopes
    per_day = run.per_day
    plan = run.plan
    plan_path = run.plan_path

    mtd_window = windows[SPEND_MTD]
    mtd_scope = scopes[SPEND_MTD]
    days_elapsed = len(per_day)
    month = dt.date.fromisoformat(min(per_day))
    month_key = f"{month.year}-{month.month:02d}"
    if max(per_day)[:7] != month_key:
        raise BuildError(
            f"{SPEND_MTD} spans more than one calendar month "
            f"({month_key}..{max(per_day)[:7]}). Pacing is a single-month "
            "figure against a single month's stated budget; split the pull."
        )
    days_in_month = calendar.monthrange(month.year, month.month)[1]
    mtd_spend = sum(per_day.values(), Decimal(0))
    month_label = month.strftime("%B %Y")
    month_slug = f"{month.strftime('%B').lower()}_{month.year}"
    stated = plan.stated_budget(month_key)
    budget_cad = stated.cad          # None when the month has no stated split
    budget_id = f"budget.google_ads.{month_slug}.cad"

    if budget_cad is not None:
        BUDGET_SCOPE = (
            f"{plan_path.name}, 'Acme Software Google Ads' tab: the {month_label} "
            "Google Ads budget as stated by the account owner. READ, never derived "
            "- deriving it from Google Ads' share of the all-channel total produced "
            "CAD 97,315, wrong by 22% (VF-14, guardrail 8)"
        )
    else:
        BUDGET_SCOPE = (
            f"the {month_label} Google Ads budget: {stated.reason}"
        )
    fs.add(id=budget_id,
           value=None if budget_cad is None else money(budget_cad),
           unit="currency_cad",
           scope=BUDGET_SCOPE,
           window=f"{month.year}-{month.month:02d}-01..{month.year}-"
                  f"{month.month:02d}-{days_in_month}",
           source=plan_path.name, basis="n/a")
    fs.add(id="plan.mom_step_down.pct", value=plan.planned_step_down_pct,
           unit="percent_planned_change",
           scope=(
               f"{plan_path.name}: the PLANNED month-over-month budget step "
               "down. This is the plan and must NEVER be reported as a problem "
               "(guardrail 8). Overspend against the lower budget is a "
               "separate, real finding"
           ),
           window="Aug 2026 vs Jul 2026", source=plan_path.name, basis="n/a")

    fs.add(id="spend.account.mtd.cad", value=money(mtd_spend),
           unit="currency_cad", scope=mtd_scope, window=mtd_window,
           source=SPEND_MTD, basis="n/a")
    fs.add(id="pacing.days_elapsed", value=days_elapsed, unit="days",
           scope=mtd_scope, window=mtd_window, source=SPEND_MTD, basis="n/a")
    fs.add(id="pacing.days_in_month", value=days_in_month, unit="days",
           scope=f"calendar length of {month.year}-{month.month:02d}",
           window=mtd_window, source="calendar", basis="n/a")

    prorated_id = f"budget.google_ads.{month_slug}.prorated_to_date.cad"
    prorated = (
        None if budget_cad is None
        else budget_cad * Decimal(days_elapsed) / Decimal(days_in_month)
    )
    PRORATE_SCOPE = (
        f"{BUDGET_SCOPE}, pro-rated by day to the {days_elapsed} days the "
        f"spend pull covers: {budget_cad} x {days_elapsed} / "
        f"{days_in_month}. Real pacing is spend-to-date against budget "
        "pro-rated by day; the workbook's 'Pace' column compares yesterday's "
        "spend to the 7-day average, which is an acceleration check, not a "
        "budget check (guardrail 8)"
        if budget_cad is not None
        else f"{BUDGET_SCOPE} - so nothing to pro-rate"
    )
    fs.add(id=prorated_id,
           value=None if prorated is None else money(prorated),
           unit="currency_cad", scope=PRORATE_SCOPE,
           window=mtd_window, source=plan_path.name, basis="n/a")
    if prorated is not None:
        fs.meta["derivations"].append({
            "id": prorated_id,
            "parents": [budget_id, "pacing.days_elapsed",
                        "pacing.days_in_month"],
            "profile": "prorate",
            "rule": (
                "a single-parent rescaling of the read budget onto the spend pull's "
                "own window. Its window matches the spend fact so the pacing "
                "comparison bridges scope only."
            ),
        })

    PLAN_VS_ACTUAL = (
        "plan versus actual. The two parents differ on scope by construction "
        "and can never share one: a budget is a figure in the owner's workbook "
        "and spend is an account metric. That is a different failure class from "
        "the Aug-10 error, which was two figures that SHOULD have shared a "
        "scope. Window, unit and basis all match, and the pro-ration put both "
        "on the same days, so the comparison is sound."
    )
    if prorated is not None:
        related_fact(
            fs, "pacing.overspend_to_date.cad",
            parents=("spend.account.mtd.cad", prorated_id),
            profile="plan-vs-actual",
            value=money(mtd_spend - prorated), unit="currency_cad_delta",
            scope=f"{mtd_scope} | against: {PRORATE_SCOPE}", window=mtd_window,
            basis="n/a", rule=PLAN_VS_ACTUAL,
        )
        related_fact(
            fs, "pacing.spend_vs_prorated_budget.pct",
            parents=("spend.account.mtd.cad", prorated_id),
            profile="plan-vs-actual",
            value=(mtd_spend / prorated * Decimal(100)).quantize(Decimal("0.01")),
            unit="percent_of_prorated_budget",
            scope=f"{mtd_scope} | against: {PRORATE_SCOPE}", window=mtd_window,
            basis="n/a", rule=PLAN_VS_ACTUAL,
        )
    else:
        fs.add(id="pacing.overspend_to_date.cad", value=None,
               unit="currency_cad_delta",
               scope=f"{mtd_scope} | against: {PRORATE_SCOPE}",
               window=mtd_window, source=plan_path.name, basis="n/a")
        fs.add(id="pacing.spend_vs_prorated_budget.pct", value=None,
               unit="percent_of_prorated_budget",
               scope=f"{mtd_scope} | against: {PRORATE_SCOPE}",
               window=mtd_window, source=plan_path.name, basis="n/a")

    daily_average = mtd_spend / Decimal(days_elapsed)
    projected = daily_average * Decimal(days_in_month)
    remaining_days = days_in_month - days_elapsed
    fs.add(id="pacing.daily_average.cad", value=money(daily_average),
           unit="currency_cad",
           scope=f"{mtd_scope} | mean daily spend over {days_elapsed} days",
           window=mtd_window, source=SPEND_MTD, basis="n/a")
    fs.add(id="pacing.projected_full_month.cad", value=money(projected),
           unit="currency_cad",
           scope=(
               f"{mtd_scope} | run-rate projection: the {days_elapsed}-day mean "
               f"daily spend extended over all {days_in_month} days. A "
               "projection, not a measurement"
           ),
           window=f"{month.year}-{month.month:02d} projected from {mtd_window}",
           source=SPEND_MTD, basis="n/a")
    fs.add(id="pacing.projected_full_month_vs_budget.pct",
           value=(None if budget_cad is None else
                  (projected / budget_cad * Decimal(100)).quantize(Decimal("0.01"))),
           unit="percent_of_budget",
           scope=f"{mtd_scope} | against: {BUDGET_SCOPE}",
           window=f"{month.year}-{month.month:02d} projected from {mtd_window}",
           source=f"{SPEND_MTD} + {plan_path.name}", basis="n/a")
    fs.add(id="pacing.projected_overspend.cad",
           value=None if budget_cad is None else money(projected - budget_cad),
           unit="currency_cad_delta",
           scope=f"{mtd_scope} | against: {BUDGET_SCOPE}",
           window=f"{month.year}-{month.month:02d} projected from {mtd_window}",
           source=f"{SPEND_MTD} + {plan_path.name}", basis="n/a")
    if remaining_days > 0:
        left = None if budget_cad is None else budget_cad - mtd_spend
        fs.add(id="pacing.budget_left_per_remaining_day.cad",
               value=None if left is None else money(left / Decimal(remaining_days)),
               unit="currency_cad",
               scope=(
                   f"{BUDGET_SCOPE} minus spend to date, spread over the "
                   f"{remaining_days} days left in the month"
                   if budget_cad is not None else BUDGET_SCOPE
               ),
               window=mtd_window, source=f"{SPEND_MTD} + {plan_path.name}",
               basis="n/a")
        cut = (
            None if left is None
            else (Decimal(1) - (left / Decimal(remaining_days))
                  / daily_average) * Decimal(100)
        )
        fs.add(id="pacing.daily_cut_needed_to_land_on_budget.pct",
               value=None if cut is None else cut.quantize(Decimal("0.01")),
               unit="percent_reduction_required",
               scope=(
                   f"{BUDGET_SCOPE} | the cut to the current daily average "
                   f"needed to finish the month on budget across the remaining "
                   f"{remaining_days} days"
                   if budget_cad is not None else BUDGET_SCOPE
               ),
               window=mtd_window, source=f"{SPEND_MTD} + {plan_path.name}",
               basis="n/a")


def add_change_events(fs: FactSet, run: Run) -> None:
    """Change events and learning windows."""
    windows = run.windows
    scopes = run.scopes
    names = run.names
    channels = run.channels
    structure = run.structure
    change_rows = run.change_rows
    learning, spending, W7 = run.learning, run.spending, run.W7

    W14 = windows[CHANGES]
    S14 = scopes[CHANGES]
    fs.add(id="change_events.14d.count", value=len(change_rows), unit="count",
           scope=S14, window=W14, source=CHANGES, basis="n/a")
    budget_events = [r for r in change_rows
                     if (r.get("changeEvent") or {}).get("changeResourceType")
                     == "CAMPAIGN_BUDGET"]
    budget_campaigns = {(r["changeEvent"].get("campaign") or "").rsplit("/", 1)[-1]
                        for r in budget_events}
    fs.add(id="change_events.campaign_budget.event.count",
           value=len(budget_events), unit="count", scope=S14, window=W14,
           source=CHANGES, basis="n/a")
    fs.add(id="change_events.campaign_budget.campaign.count",
           value=len(budget_campaigns), unit="count", scope=S14, window=W14,
           source=CHANGES, basis="n/a")

    in_learning = [cid for cid in spending if learning["per_campaign"].get(cid)]
    fs.add(id="campaigns.with_spend.in_learning.7d.count",
           value=len(in_learning), unit="count",
           scope=(
               f"{S14} | campaigns that spent inside {W7} AND carry a bid, "
               "budget or target change inside the change-event window. "
               "Guardrail 7 applies this to efficiency, performance and "
               "bid/budget/target claims ONLY - never to a copy defect, a "
               "policy finding or a coverage gap, and never to account-level "
               "pacing, whose cause these very changes are"
           ),
           window=W14, source=f"{CHANGES} + {COST_7D}", basis="n/a")
    fs.meta["learning"]["applies_to"] = {
        "downgrade": ["CPA", "ROAS", "conversion rate", "volume move",
                      "underperforming", "bid/budget/target recommendation"],
        "does_not_downgrade": ["copy defect", "policy or compliance finding",
                               "coverage or structure gap",
                               "account-level budget pacing",
                               "impression-share and spend volume mechanics"],
        "learning_days": {"SEARCH": LEARNING_DAYS_SEARCH,
                          "PERFORMANCE_MAX": LEARNING_DAYS_PMAX},
    }
    fs.meta["learning"]["spending_campaigns_in_learning"] = [
            {"id": cid, "name": names.get(cid),
             "channel": channels.get(cid),
             "latest": max(e["date"] for e in learning["per_campaign"][cid]),
             "types": sorted({e["resource_type"]
                              for e in learning["per_campaign"][cid]})}
            for cid in in_learning
    ]
    fs.meta["learning"]["spending_campaigns_not_in_learning"] = [
        {"id": cid, "name": names.get(cid)}
        for cid in spending if cid not in in_learning
    ]


def add_scale_check(fs: FactSet, run: Run) -> None:
    """Self-check: every scale-bearing unit declares one."""
    trail = run.trail

    # Read off the UNIT, never the id or the magnitude. `citations.py` reads
    # the scale off the unit and refuses a unit that declares neither, so a
    # count called `...share...count` is fine and a share called `share` is not.
    SCALE_BEARING = ("percent", "pct", "ratio", "share", "roas", "rate")
    ambiguous = [
        f.id for f in fs
        if any(marker in f.unit.lower() for marker in SCALE_BEARING)
        and percentage_convention(f.unit) is None
    ]
    if ambiguous:
        raise BuildError(
            "these facts will be printed as percentages or ratios but their "
            "unit declares neither a percent (0-100) nor a ratio (0-1) scale, "
            f"so citations.py would refuse them as ambiguous_unit: {ambiguous}"
        )

    fs.meta["fact_count"] = len(fs)
    fs.meta["scope_trail"] = trail
    return fs


def build(raw_dir: Path, plan_path: Path) -> FactSet:
    """Read the pulls, then emit the facts, section by section."""
    fs, run = prepare(Path(raw_dir), plan_path)
    for section in (add_structure, add_volume, add_conversions,
                    add_efficiency, add_wow, add_impression_share,
                    add_pacing, add_change_events, add_scale_check):
        section(fs, run)
    fs.meta["fact_count"] = len(fs)
    fs.meta["scope_trail"] = run.trail
    return fs


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0])
    parser.add_argument("raw_dir", help="google-ads/outputs/raw/YYYY-MM-DD")
    parser.add_argument("--out", default=None,
                        help="output path (default <raw_dir>/weekly-review-facts.json)")
    parser.add_argument("--budget-plan", default=None,
                        help=f"path to {BUDGET_PLAN} (default: pack root)")
    args = parser.parse_args(argv)

    raw_dir = Path(args.raw_dir).resolve()
    plan_path = (Path(args.budget_plan) if args.budget_plan
                 else Path(__file__).resolve().parent.parent / BUDGET_PLAN)
    try:
        fs = build(raw_dir, plan_path)
    except (BuildError, FactsError, gaql.FilterMismatch, S.ManifestError) as err:
        print(f"REFUSED to write a facts file:\n{err}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else raw_dir / "weekly-review-facts.json"
    fs.write(out)
    print(f"wrote {len(fs)} facts to {out}")
    for line in fs.meta["scope_trail"]:
        print(f"  {line}")
    if fs.meta["suppressed_shares"]:
        print(f"  suppressed shares: {len(fs.meta['suppressed_shares'])} "
              "(sentinel or absent) - see meta.suppressed_shares")
    for defect in fs.meta.get("raw_pull_defects", []):
        print(f"  RAW PULL DEFECT: {defect}")
    for defect in fs.meta["shared_module_defects"]:
        print(f"  SHARED MODULE: {defect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
