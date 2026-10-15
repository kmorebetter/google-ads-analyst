#!/usr/bin/env python3
"""Build the `budget-optimize` facts file from saved raw JSON (VERIFICATION-RULES VF-15).

Usage:
    python3 tools/build_budget_optimize_facts.py google-ads/outputs/raw/2026-08-20
    python3 tools/build_budget_optimize_facts.py <raw_dir> --out /tmp/facts.json

Reads only the raw JSON already on disk plus the stated budget figures in
`budget-plan.md`. Makes no API calls. Writes
`<raw_dir>/budget-optimize-facts.json` unless `--out` says otherwise.

Six things about this script matter more than the arithmetic.

1. **The row key is the budget, not the campaign** (VF-19). `campaign_budget.id`
   is attached to several campaigns at once, so a budget total keyed on
   `campaign.id` counts the same pool once per campaign. Measured on
   2026-08-20: 6 budget ids carry 17 of the 36 enabled campaigns, and the
   campaign-keyed daily total reads CAD 7,900/day against a true CAD
   5,840.50/day. That is 1.3526x, an overstatement of 35.3%. Both figures are
   emitted, the wrong one under an id and a scope that say so, and the guard in
   `facts.py` refuses to relate them because their scopes differ.

2. **`campaign_budget.explicitly_shared` is False on all 35 rows that have a
   budget, and is therefore unusable here.** Sharing is detected by counting
   distinct `campaign_budget.id` values across campaigns, never by reading the
   flag. `refuse_campaign_keyed_total` is the hard stop that enforces point 1.

3. **The two-query rule** (VF-3). `metrics.cost_micros` cannot appear beside
   `segments.conversion_action_name`, so cost and 8-action conversions arrive in
   separate pulls and are joined on `campaign.id`, then aggregated to the budget.

4. **Budget figures are read, never derived** (`campaign-guardrails` rule 8).
   Stated monthly budgets come from `account-config.json` (drift-guarded
   against the owner's figures in `budget-plan.md` by
   `test_account_config.py`); August 2026 is CAD 54,400 (USD 40,000 at
   1.36). Deriving a figure from a share of the all-channel total once
   produced CAD 97,315, wrong by 22%. A month with no stated split builds
   with every budget-dependent pacing fact null, the reason attached - never
   another month's figure carried forward. Nothing in this script divides an
   all-channel total.

5. **Temporality comes from `_manifest.json`, never from the data** (VF-17
   rule 5). Current-state facts carry `window: current-as-of-<date>`.

6. **Efficiency facts exist only where they are admissible.** A cost-per-
   conversion or ROAS fact is emitted only for a budget cluster that clears the
   5-conversion floor (`campaign-guardrails` rule 5) and is not a top-of-funnel
   channel (rule 1). Below the floor the fact is absent, so a report cannot cite
   one. The floor is enforced by what gets written, not by prose.

Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import calendar
import collections
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_config  # noqa: E402
import builder_core as core  # noqa: E402
import gaql  # noqa: E402
import pulls  # noqa: E402
import scopes as S  # noqa: E402
from facts import FactSet, current_state_window, is_sentinel_share  # noqa: E402
from facts import micros_to_currency, money  # noqa: E402

BuildError = pulls.BuildError
load_results = pulls.load_results
field_mask = pulls.field_mask

# --------------------------------------------------------------------------
# The pulls this skill reads, and the scope label each one's facts carry
# --------------------------------------------------------------------------

# Canonical spellings live in `pull_specs.py` - the single source for which
# pulls exist; these aliases keep every existing reference working.
import pull_specs  # noqa: E402

CAMPAIGNS = pull_specs.CAMPAIGNS_CURRENT
COST_7D = pull_specs.COST_7D
COST_PRIOR7D = pull_specs.COST_PRIOR7D
CONV_7D = pull_specs.CONV_7D
CONV_PRIOR7D = pull_specs.CONV_PRIOR7D
SPEND_MTD = pull_specs.SPEND_MTD
CHANGES = pull_specs.CHANGE_EVENTS_14D

REQUIRED = (CAMPAIGNS, COST_7D, CONV_7D, SPEND_MTD, CHANGES)
OPTIONAL = (COST_PRIOR7D, CONV_PRIOR7D)

#: Normalised scope labels. Checked clause-for-clause against each pull's
#: declared filter in `_manifest.json` (VF-19a) by `verify_scope` below.
#: Canonical strings live in `pull_specs.py`.
ENABLED = pull_specs.ENABLED_CAMPAIGNS
ENABLED_8 = pull_specs.ENABLED_8
CHANGE_SCOPE = pull_specs.CHANGE_EVENTS_LABEL

SCOPE_FOR = {
    CAMPAIGNS: ENABLED,
    COST_7D: ENABLED,
    COST_PRIOR7D: ENABLED,
    CONV_7D: ENABLED_8,
    CONV_PRIOR7D: ENABLED_8,
    CHANGES: CHANGE_SCOPE,
}

#: `account-spend-mtd.json` applies no campaign or status filter at all. Its
#: only clause is the date range, and that absence is the point: the figure is
#: the customer-level total including paused and removed campaigns, which is why
#: it must never be compared against a sum over enabled campaigns.
SPEND_SCOPE_NOTE = (
    "account-spend-mtd.json applies no campaign or status filter. Its total is "
    "customer-level and includes paused and removed campaigns. That is what "
    "makes it the right instrument for pacing, and why it may not be compared "
    "against any sum over enabled campaigns only."
)

# --------------------------------------------------------------------------
# Read, never derive: the stated budget figures (campaign-guardrails rule 8)
# --------------------------------------------------------------------------

BUDGET_PLAN = "budget-plan.md"
USD_TO_CAD = account_config.fx_usd_to_cad()

#: Stated budgets live in `account-config.json` (drift-guarded against
#: `budget-plan.md` by `test_account_config.py`). A `MonthPlan` carries the run
#: month's figures - or their stated absence, with the reason. The old
#: single-month refusal (`STATED_BUDGET_MONTH`) is gone: a month with no
#: stated split BUILDS, and every budget-dependent pacing fact is null with
#: the reason attached, which is what "never carry another month's figure
#: forward" (campaign-guardrails rule 8) actually requires.
NO_SPLIT_REASON = account_config.no_split_reason()

#: The planned month-over-month step down. The plan, never a finding.
PLANNED_STEP_DOWN_PCT = account_config.planned_step_down_pct()
PLANNED_STEP_DOWN_WINDOW = account_config.planned_step_down_window()

#: Elasticity ceiling, from the owner's own projections (config-sourced,
#: drift-guarded against budget-plan.md). Only the stated bands iterate here;
#: band_3 is emitted explicitly as the null it is.
ELASTICITY = tuple(
    (name, usd, degradation, note)
    for name, usd, degradation, note in account_config.elasticity_bands()
    if usd is not None
)
ELASTICITY_SCOPE = account_config.elasticity_scope()
ELASTICITY_WINDOW = "2026 plan"

#: Real-money anchors so no assumed-value figure ships unanchored (VF-8).
_CAC = account_config.cac_anchor()
CAC_ANCHOR_MONTH = _CAC["month"]
CAC_ANCHOR_SLUG = _CAC["month_slug"]
CAC_ANCHOR_USD = _CAC["cac_usd"]
CPA_ANCHOR_USD = _CAC["cost_per_free_trial_usd"]
ANCHOR_SCOPE = (
    f"Google Ads, {_CAC['month_label']}, real customers and real free trials "
    "from the CPA and CAC Calculations tab via budget-plan.md"
)


@dataclass(frozen=True)
class MonthPlan:
    """The run month's calendar and stated-budget figures, from the config."""

    month: str                  # "2026-08"
    label: str                  # "August 2026"
    word: str                   # "August"
    slug: str                   # "august"
    days: int
    window: str                 # "2026-08-01..2026-08-31 (stated plan)"
    budget_cad: "Decimal | None"
    budget_usd: "Decimal | None"
    reason: "str | None"        # why there is no figure, when budget_cad is None

    @property
    def stated(self) -> bool:
        return self.budget_cad is not None


def month_plan(month: str) -> MonthPlan:
    """Look one month up in the config; unknown months refuse loudly."""
    try:
        stated = account_config.stated_budget(month)
    except account_config.UnknownMonthError as exc:
        raise BuildError(str(exc)) from exc
    first = dt.date.fromisoformat(month + "-01")
    days = calendar.monthrange(first.year, first.month)[1]
    return MonthPlan(
        month=month,
        label=first.strftime("%B %Y"),
        word=first.strftime("%B"),
        slug=first.strftime("%B").lower(),
        days=days,
        window=f"{month}-01..{month}-{days:02d} (stated plan)",
        budget_cad=stated.cad,
        budget_usd=stated.usd,
        reason=stated.reason,
    )


def budget_plan_scope(plan: MonthPlan) -> str:
    """The scope every stated-budget fact carries (VF-14, guardrail 8)."""
    if plan.stated:
        return (
            f"{plan.label} Google Ads monthly budget, read from the owner's "
            "Google Ads tab via budget-plan.md. NEVER derived from a share of "
            "the all-channel total - that derivation produced CAD 97,315, "
            "wrong by 22%"
        )
    return f"{plan.label} Google Ads monthly budget: {plan.reason}"

# --------------------------------------------------------------------------
# Thresholds and windows
# --------------------------------------------------------------------------

#: Thresholds read from `account-config.json` so the two skills that share a
#: gate (this one and weekly-review) can never disagree about its value.
CONSTRAINED_GATE = account_config.threshold("lost_is_budget_constrained_gate")
EXPOSURE_GATE = account_config.threshold("lost_is_budget_exposure_gate")
CONVERSION_FLOOR = account_config.threshold("conversion_floor")
LEARNING_DAYS_SEARCH = account_config.threshold("learning_days_search")
LEARNING_DAYS_PMAX = account_config.threshold("learning_days_pmax")
CHANGE_PULL_COVERAGE_DAYS = 14
AUG17 = "2026-08-17"

#: Channels that exist to build awareness, never judged on BOFU efficiency
#: (`campaign-guardrails` rule 1).
TOF_CHANNELS = account_config.tof_channels()

#: A CAMPAIGN change whose changed field list touches one of these is a bid or
#: target change, so it resets learning for a budget/target claim. A CAMPAIGN
#: change touching only a name or a tracking template does not.
TARGET_FIELD_MARKERS = (
    "targetRoas",
    "targetCpa",
    "biddingStrategyType",
    "targetImpressionShare",
    "targetSpend",
)

# --------------------------------------------------------------------------
# Campaign buckets. Read from this run's pull, never from a stored census.
# --------------------------------------------------------------------------

_NON_BRAND = re.compile(r"(?i)non[\s\-]?brand")
_BRAND = re.compile(r"(?i)brand")
_CONQUESTING = re.compile(
    "(?i)" + "|".join(re.escape(t) for t in account_config.conquest_tokens())
)

#: `account-conventions` calls these two one-off/legacy and says they need
#: review, so they are bucketed by id rather than by a name pattern that would
#: read `DD | Non Branded - Conquest ...` as conquesting.
LEGACY_CAMPAIGN_IDS = account_config.legacy_campaign_ids()


def bucket_of(campaign: dict) -> str:
    """Brand / non-brand / conquesting / PMax / TOF / legacy, from the name.

    `campaign-guardrails` rule 2 requires brand and non-brand subtotals to be
    reported separately, so the split has to exist before any subtotal does.
    `Search-NonBrand-T1 - tROAS` contains the word Brand and is not brand; the
    non-brand test therefore runs first.
    """
    if campaign["id"] in LEGACY_CAMPAIGN_IDS:
        return "legacy"
    channel = campaign.get("advertisingChannelType") or ""
    if channel in TOF_CHANNELS:
        return "tof"
    if channel == "PERFORMANCE_MAX":
        return "pmax"
    name = campaign.get("name") or ""
    if _CONQUESTING.search(name):
        return "conquesting"
    if _BRAND.search(name) and not _NON_BRAND.search(name):
        return "brand"
    return "nonbrand"


# --------------------------------------------------------------------------
# Scope verification, with one named workaround
# --------------------------------------------------------------------------

_BETWEEN_RANGE = re.compile(
    r"BETWEEN\s*'?(\d{4}-\d{2}-\d{2})'?\s+AND\s+'?(\d{4}-\d{2}-\d{2})'?",
    re.IGNORECASE,
)


def verify_temporality(manifest: S.Manifest, filename: str) -> str:
    """Check the manifest's `filter` and `temporality` fields agree.

    Not an inference (VF-17 rule 5 forbids that): both were declared by whatever
    ran the pull, and this only checks they say the same thing.

    The whole check is `gaql.check_declared_temporality` — its BETWEEN-range
    blindness was fixed 2026-08-20, so the local endpoint-comparison this
    function used to carry is retired. One remaining local behaviour: a
    mislabelled range is re-raised as a `BuildError`, because a manifest whose
    two fields disagree is this builder's refusal to write, not a filter
    mismatch to report onward.
    """
    declared_filter = manifest.declared_filter(filename) or ""
    declared = (manifest.declared_temporality(filename) or "").strip()
    try:
        return f"{filename}: " + gaql.check_declared_temporality(
            declared_filter, declared
        )
    except gaql.FilterMismatch as exc:
        if _BETWEEN_RANGE.search(declared_filter) and "different ranges" in str(exc):
            raise BuildError(f"{filename}: {exc}") from None
        raise


def verify_scope(manifest: S.Manifest, filename: str, label: str) -> str:
    """Clause-for-clause check of a declared filter against a scope label.

    VF-19a. Raises `gaql.FilterMismatch` on a missing or extra clause, because a
    right number under a wrong label is the failure VF-15 exists to prevent.
    """
    declared = manifest.declared_filter(filename)
    if not declared:
        return (
            f"{filename}: no declared filter in {S.MANIFEST}, so the scope "
            "label stands DECLARED, not verified (VF-19a)"
        )
    return f"{filename}: " + gaql.assert_semantic_match(filename, declared, label)


# --------------------------------------------------------------------------
# Row keys (VF-19): the budget is the key, the campaign is the attribution
# --------------------------------------------------------------------------


class Cluster:
    """One `campaign_budget.id` and every enabled campaign attached to it.

    The whole point of this class is that `daily_cad` belongs to the cluster and
    not to any campaign in it. Reading it per campaign and adding up is the
    fan-out this pack keeps paying for.
    """

    __slots__ = ("budget_id", "daily_cad", "campaigns", "explicitly_shared")

    def __init__(self, budget_id: str, daily_cad, explicitly_shared) -> None:
        self.budget_id = budget_id
        self.daily_cad = daily_cad
        self.explicitly_shared = explicitly_shared
        self.campaigns: list[dict] = []

    @property
    def shared(self) -> bool:
        return len(self.campaigns) > 1

    @property
    def campaign_ids(self) -> list:
        return [c["id"] for c in self.campaigns]

    @property
    def label(self) -> str:
        if self.shared:
            return f"budget {self.budget_id} ({len(self.campaigns)} campaigns)"
        return f"budget {self.budget_id} ({self.campaigns[0]['name']})"

    @property
    def buckets(self) -> list:
        return sorted({bucket_of(c) for c in self.campaigns})

    @property
    def is_pmax(self) -> bool:
        return any(
            (c.get("advertisingChannelType") or "") == "PERFORMANCE_MAX"
            for c in self.campaigns
        )

    @property
    def is_tof(self) -> bool:
        return any(
            (c.get("advertisingChannelType") or "") in TOF_CHANNELS
            for c in self.campaigns
        )


def build_clusters(rows: list[dict]) -> "dict[str, Cluster]":
    """Group enabled campaigns by `campaign_budget.id`. The only safe key.

    Refuses when two campaigns claim the same budget id with different amounts,
    which would mean the pull is segmented and neither amount can be trusted.
    """
    clusters: dict[str, Cluster] = {}
    for row in rows:
        campaign = row.get("campaign") or {}
        budget = row.get("campaignBudget") or {}
        budget_id = budget.get("id")
        if not budget_id:
            raise BuildError(
                f"{CAMPAIGNS}: campaign {campaign.get('id')} has no "
                "campaignBudget.id. That is the only safe row key for a budget "
                "figure; add campaign_budget.id to the SELECT and re-pull."
            )
        amount = micros_to_currency(budget.get("amountMicros"))
        existing = clusters.get(budget_id)
        if existing is None:
            clusters[budget_id] = existing = Cluster(
                budget_id, amount, budget.get("explicitlyShared")
            )
        elif existing.daily_cad != amount:
            raise BuildError(
                f"{CAMPAIGNS}: budget {budget_id} arrives with two different "
                f"amounts ({existing.daily_cad} and {amount}). The response is "
                "segmented; a budget figure cannot be read from it."
            )
        existing.campaigns.append(campaign)
    return clusters


def refuse_campaign_keyed_total(rows: list[dict]) -> None:
    """Hard stop on totalling daily budgets over campaign rows (VF-19).

    Called before any budget total is written. If the campaign rows carry fewer
    distinct budget ids than campaigns, a campaign-keyed sum double counts the
    shared pools and the only correct total is over distinct budget ids.
    """
    budget_ids = [
        (r.get("campaignBudget") or {}).get("id")
        for r in rows
        if (r.get("campaignBudget") or {}).get("amountMicros")
    ]
    distinct = len(set(budget_ids))
    if distinct < len(budget_ids):
        counts = collections.Counter(budget_ids)
        worst, n = counts.most_common(1)[0]
        raise BuildError(
            "REFUSED to total daily budgets over campaign rows. "
            f"{len(budget_ids)} campaigns carry only {distinct} distinct "
            f"campaign_budget.id values, so a campaign-keyed sum counts the "
            f"shared pools once per campaign (budget {worst} alone appears {n} "
            "times). Key on campaign_budget.id and attribute to campaigns "
            "afterwards (VF-19)."
        )


def sum_daily_budgets(clusters: "dict[str, Cluster]") -> Decimal:
    """The only correct daily budget total: one amount per distinct budget."""
    return sum(
        (c.daily_cad for c in clusters.values() if c.daily_cad is not None),
        Decimal(0),
    )


def campaign_keyed_daily_total(rows: list[dict]) -> Decimal:
    """The wrong total, computed on purpose so the report can name the gap.

    Never call this for a figure a reader will act on. Its fact id and scope
    both say it double counts, and `facts.py` refuses to relate it to the
    correct total because their scopes differ.
    """
    return sum(
        (
            micros_to_currency((r.get("campaignBudget") or {}).get("amountMicros"))
            for r in rows
            if (r.get("campaignBudget") or {}).get("amountMicros")
        ),
        Decimal(0),
    )


# --------------------------------------------------------------------------
# Per-campaign metric readers
# --------------------------------------------------------------------------


def metrics_by_campaign(rows: list[dict], filename: str) -> dict:
    """`campaign.id` -> metrics dict. Refuses a segmented response."""
    out: dict[str, dict] = {}
    for row in rows:
        cid = (row.get("campaign") or {}).get("id")
        if not cid:
            raise BuildError(f"{filename}: a row has no campaign.id")
        if cid in out:
            raise BuildError(
                f"{filename}: campaign {cid} appears more than once. The "
                "response is segmented; aggregate per campaign before summing."
            )
        out[cid] = row.get("metrics") or {}
    return out


def conversions_by_campaign(rows: list[dict], filename: str) -> dict:
    """`campaign.id` -> (conversions, value). Summed over the 8 action rows.

    This pull is segmented by `segments.conversion_action_name` by design, so
    several rows per campaign is correct here and the aggregation is the join.
    """
    conv: dict = collections.defaultdict(lambda: [Decimal(0), Decimal(0), 0])
    for row in rows:
        cid = (row.get("campaign") or {}).get("id")
        if not cid:
            raise BuildError(f"{filename}: a row has no campaign.id")
        metrics = row.get("metrics") or {}
        conv[cid][0] += Decimal(str(metrics.get("conversions", 0)))
        conv[cid][1] += Decimal(str(metrics.get("conversionsValue", 0)))
        conv[cid][2] += 1
    return dict(conv)


def cluster_cost(cluster: Cluster, cost: dict) -> Decimal:
    return sum(
        (
            micros_to_currency((cost.get(cid) or {}).get("costMicros", "0"))
            for cid in cluster.campaign_ids
        ),
        Decimal(0),
    )


def cluster_conversions(cluster: Cluster, conv: dict) -> Decimal:
    return sum(
        (conv[cid][0] for cid in cluster.campaign_ids if cid in conv), Decimal(0)
    )


def cluster_conversion_value(cluster: Cluster, conv: dict) -> Decimal:
    return sum(
        (conv[cid][1] for cid in cluster.campaign_ids if cid in conv), Decimal(0)
    )


def _max_share(cluster: Cluster, cost: dict, field: str):
    """The worst value of a share field across the cluster's campaigns.

    A share cannot be summed across campaigns - each has its own auction
    denominator - so the cluster's figure is the worst single campaign's, and
    the fact's scope says so.
    """
    values = [
        (cost.get(cid) or {}).get(field)
        for cid in cluster.campaign_ids
        if (cost.get(cid) or {}).get(field) is not None
    ]
    return max((Decimal(str(v)) for v in values), default=None)


# --------------------------------------------------------------------------
# Targets. The field that holds them is not the one whose name suggests it.
# --------------------------------------------------------------------------

#: Where a target actually lives, per strategy. Read in this order.
#:
#: The first pull on 2026-08-20 selected only `campaign.target_roas.target_roas`,
#: the legacy field. It is populated for **zero** enabled campaigns in this
#: account, so the query succeeded, returned empty, and read as "no campaign has
#: a target" when 14 carry one on
#: `campaign.maximize_conversion_value.target_roas`. A wrong field that returns
#: nothing is more dangerous than one that errors, because nothing looks like an
#: answer. Always select both and report which one answered.
#: The four-field target table lives in `builder_core` — one home for the
#: knowledge of where a target actually lives, because the 2026-08-20
#: wrong-field incident is exactly what forked copies of this table invite.
TARGET_READERS = core.TARGET_READERS
read_target = core.read_target


def targets_by_campaign(rows: list[dict]) -> dict:
    out = {}
    for row in rows:
        campaign = row.get("campaign") or {}
        kind, value, field = read_target(campaign)
        if kind:
            out[campaign["id"]] = (kind, value, field)
    return out


def cluster_target(cluster: "Cluster", targets: dict):
    """The cluster's one target, or `(None, reason)` when it does not have one.

    A shared pool whose campaigns carry **different** targets has no single
    actual-versus-target figure, and inventing one by picking a campaign is the
    fan-out error in another costume. Refuse and say so.
    """
    found = {
        cid: targets[cid] for cid in cluster.campaign_ids if cid in targets
    }
    if not found:
        return None, None, "no campaign on this budget carries a set target"
    kinds = {kind for kind, _, _ in found.values()}
    values = {(kind, str(value)) for kind, value, _ in found.values()}
    if len(values) > 1:
        detail = ", ".join(
            f"{cid}={value}" for cid, (_, value, _) in sorted(found.items())
        )
        return (
            None,
            None,
            "the campaigns on this shared budget carry DIFFERENT targets "
            f"({detail}), so the pool has no single actual-versus-target figure. "
            "Compare per campaign or not at all",
        )
    kind = kinds.pop()
    value = next(iter(found.values()))[1]
    return kind, value, None


# --------------------------------------------------------------------------
# Change events (campaign-guardrails rule 7)
# --------------------------------------------------------------------------


def read_changes(rows: list[dict]) -> dict:
    """`campaign.id` -> list of (date, kind, changed_fields) for budget/target.

    Only the change types that can reset learning for a *budget or target*
    claim are collected: a `CAMPAIGN_BUDGET` amount change, or a `CAMPAIGN`
    change touching a bid strategy or a target. Rule 7 is explicit that the
    change must be matched to the claim, so a criterion or asset edit is not
    collected here and cannot downgrade a budget recommendation.
    """
    out: dict = collections.defaultdict(list)
    for row in rows:
        event = row.get("changeEvent") or {}
        cid = (event.get("campaign") or "").rsplit("/", 1)[-1]
        if not cid:
            continue
        kind = event.get("changeResourceType")
        fields = event.get("changedFields") or ""
        date = (event.get("changeDateTime") or "")[:10]
        if kind == "CAMPAIGN_BUDGET":
            out[cid].append((date, "CAMPAIGN_BUDGET", fields))
        elif kind == "CAMPAIGN" and any(m in fields for m in TARGET_FIELD_MARKERS):
            out[cid].append((date, "CAMPAIGN target/bid", fields))
    return dict(out)


def cluster_learning(cluster: Cluster, changes: dict):
    """The most recent budget/target change on any campaign in the cluster.

    Returns `(date, kind)` or `(None, None)`. A shared pool is one object: a
    budget change on any sibling resets the pool for all of them, which is the
    second reason the budget is the right key.
    """
    events = [e for cid in cluster.campaign_ids for e in changes.get(cid, [])]
    if not events:
        return None, None
    date, kind, _ = max(events)
    return date, kind


# --------------------------------------------------------------------------
# Fact groups
# --------------------------------------------------------------------------


def add_structure(fs: FactSet, rows, clusters, window, plan: MonthPlan) -> None:
    """Enabled campaigns, distinct budgets, and the shared-budget fan-out."""
    src, scope, basis = CAMPAIGNS, ENABLED, "n/a"
    campaigns = [r.get("campaign") or {} for r in rows]

    fs.add("campaign.enabled.count", len(campaigns), "count", scope, window, src, basis)
    by_channel = collections.Counter(
        c.get("advertisingChannelType") or "unknown" for c in campaigns
    )
    for channel, n in sorted(by_channel.items()):
        fs.add(
            f"campaign.enabled.channel.{channel.lower()}.count",
            n, "count", scope, window, src, basis,
        )
    by_bucket = collections.Counter(bucket_of(c) for c in campaigns)
    for bucket, n in sorted(by_bucket.items()):
        fs.add(
            f"campaign.enabled.bucket.{bucket}.count",
            n, "count", scope, window, src, basis,
        )
    overridden = sorted(
        c["id"] for c in campaigns if c["id"] in LEGACY_CAMPAIGN_IDS
    )
    fs.add(
        "campaign.bucket.hardcoded_override.ids",
        ", ".join(overridden) or "none",
        "text",
        f"{scope} - campaign ids bucketed by a STORED LIST in the builder rather "
        "than by this run's names and channels, because account-conventions "
        "calls them one-off/legacy. A stored census goes stale; if either "
        "campaign starts spending, its cost lands in subtotal.legacy and out of "
        "the bucket its name would give it. Re-read the list against "
        "account-conventions each run",
        window, src, basis,
    )
    fs.add(
        "campaign.bucket.hardcoded_override.count", len(overridden), "count",
        f"{scope} - how many campaigns this run bucketed from the stored legacy "
        "list rather than from their own name and channel",
        window, src, basis,
    )

    readable = [c for c in clusters.values() if c.daily_cad is not None]
    unreadable = [c for c in clusters.values() if c.daily_cad is None]
    shared = [c for c in clusters.values() if c.shared]

    fs.add("budget.distinct.count", len(clusters), "count", scope, window, src, basis)
    fs.add(
        "budget.readable_amount.count", len(readable), "count", scope, window, src, basis
    )
    fs.add(
        "budget.unreadable_amount.count",
        len(unreadable), "count", scope, window, src, basis,
    )
    fs.add("budget.shared.count", len(shared), "count", scope, window, src, basis)
    fs.add(
        "budget.shared.campaign.count",
        sum(len(c.campaigns) for c in shared), "count", scope, window, src, basis,
    )
    fs.add(
        "budget.explicitly_shared_flag.true.count",
        sum(1 for c in clusters.values() if c.explicitly_shared),
        "count",
        f"{scope} - campaign_budget.explicitly_shared as returned by the API",
        window, src, basis,
    )

    correct = sum_daily_budgets(clusters)
    correct_scope = (
        f"{scope} - keyed on campaign_budget.id, one amount per distinct "
        f"budget. A FLOOR, not a total, while {len(unreadable)} budget(s) "
        "return no amountMicros"
    )
    fs.add(
        "budget.daily.total.by_budget.cad",
        money(correct), "currency_cad", correct_scope, window, src, basis,
    )

    naive = campaign_keyed_daily_total(rows)
    naive_scope = (
        f"{scope} - keyed on campaign.id. WRONG: counts each of the "
        f"{len(shared)} shared pools once per campaign. NEVER quote as a "
        "budget total. Emitted only so a report can name the size of the error"
    )
    fs.add(
        "budget.daily.total.campaign_keyed_double_counted.cad",
        money(naive), "currency_cad", naive_scope, window, src, basis,
    )
    factor = (naive / correct) if correct else None
    fs.add(
        "budget.fanout.campaign_keyed.factor.ratio",
        None if factor is None else round(factor, 4),
        "ratio",
        "campaign-keyed daily budget total divided by budget-keyed daily budget "
        "total, both from " + CAMPAIGNS,
        window, src, basis,
    )
    fs.add(
        "budget.fanout.campaign_keyed.overstatement.pct",
        None if factor is None else money((factor - 1) * 100),
        "percent",
        "how much a campaign-keyed daily budget total overstates the true "
        "budget-keyed total, both from " + CAMPAIGNS,
        window, src, basis,
    )

    monthly = correct * plan.days
    fs.add(
        "budget.monthly.capacity.by_budget.cad",
        money(monthly),
        "currency_cad",
        f"{correct_scope}, multiplied by the {plan.days} days in {plan.label}",
        window, src, basis,
    )
    budget = plan.budget_cad
    fs.add(
        f"budget.monthly.capacity.vs_{plan.slug}_budget.pct",
        None if budget is None else money(monthly / budget * 100),
        "percent",
        (
            f"budget-keyed monthly daily-budget capacity against the CAD "
            f"{budget:,.0f} {plan.label} Google Ads budget read from "
            "budget-plan.md"
            if budget is not None
            else "budget-keyed monthly daily-budget capacity: " + str(plan.reason)
        ),
        window,
        f"{CAMPAIGNS} + {BUDGET_PLAN}",
        basis,
    )
    fs.add(
        f"budget.monthly.capacity.campaign_keyed.vs_{plan.slug}_budget.pct",
        None if budget is None else money(naive * plan.days / budget * 100),
        "percent",
        (
            f"{naive_scope}, against the CAD {budget:,.0f} {plan.word} budget. "
            "Shown only to size the fan-out error"
            if budget is not None
            else f"{naive_scope}. {plan.reason}"
        ),
        window,
        f"{CAMPAIGNS} + {BUDGET_PLAN}",
        basis,
    )


def add_cluster_facts(
    fs, clusters, cost, prior_cost, conv, changes, targets, windows
) -> list:
    """Per-cluster budget, spend, constraint, learning and efficiency facts.

    Emitted only for a cluster that spent or lost impression share in either
    week. An idle cluster gets the aggregate facts in `add_idle` instead, which
    is what keeps the file small enough for a citation to mean something.
    """
    trail = []
    cur_w, prior_w, cur_scope = windows["cost"], windows["prior"], ENABLED
    conv_w = windows["conv"]
    active, idle = [], []
    for cluster in clusters.values():
        spend_now = cluster_cost(cluster, cost)
        spend_prior = cluster_cost(cluster, prior_cost) if prior_cost else Decimal(0)
        lost_budget = _max_share(cluster, cost, "searchBudgetLostImpressionShare")
        if spend_now > 0 or spend_prior > 0 or (lost_budget or 0) > 0:
            active.append(cluster)
        else:
            idle.append(cluster)

    for cluster in sorted(active, key=lambda c: -cluster_cost(c, cost)):
        bid = cluster.budget_id
        prefix = f"cluster.{bid}"
        share_scope = (
            f"{cur_scope} - budget {bid}. A share has a per-campaign auction "
            "denominator and cannot be summed across campaigns, so this is the "
            "worst single campaign's value in the cluster"
        )
        fs.add(
            f"{prefix}.campaign.count",
            len(cluster.campaigns), "count", f"{cur_scope} - budget {bid}",
            windows["current_state"], CAMPAIGNS, "n/a",
        )
        fs.add(
            f"{prefix}.daily_budget.cad",
            money(cluster.daily_cad),
            "currency_cad",
            f"{cur_scope} - keyed on campaign_budget.id={bid}, read from "
            "campaign_budget.amount_micros, never derived",
            windows["current_state"], CAMPAIGNS, "n/a",
        )
        fs.add(
            f"{prefix}.cost.cad",
            money(cluster_cost(cluster, cost)), "currency_cad",
            f"{cur_scope} - summed over the {len(cluster.campaigns)} campaign(s) "
            f"on budget {bid}",
            cur_w, COST_7D, "n/a",
        )
        if prior_cost:
            fs.add(
                f"{prefix}.cost.prior_week.cad",
                money(cluster_cost(cluster, prior_cost)), "currency_cad",
                f"{cur_scope} - summed over the {len(cluster.campaigns)} "
                f"campaign(s) on budget {bid}",
                prior_w, COST_PRIOR7D, "n/a",
            )
        lost_budget = _max_share(cluster, cost, "searchBudgetLostImpressionShare")
        lost_budget_sentinel = (
            lost_budget is not None and is_sentinel_share(float(lost_budget))
        )
        fs.add(
            f"{prefix}.lost_is_budget.ratio",
            None if (lost_budget is None or lost_budget_sentinel)
            else round(lost_budget, 5),
            "ratio",
            share_scope
            + (
                "; SUPPRESSED: this row's impression-share metrics hit a "
                "0.0999/0.9001 clamp, so this figure is a bound, not a value "
                "(VF-11)"
                if lost_budget_sentinel
                else ""
            ),
            cur_w, COST_7D, "n/a",
        )
        lost_rank = _max_share(cluster, cost, "searchRankLostImpressionShare")
        lost_rank_sentinel = (
            lost_rank is not None and is_sentinel_share(float(lost_rank))
        )
        fs.add(
            f"{prefix}.lost_is_rank.ratio",
            None if (lost_rank is None or lost_rank_sentinel)
            else round(lost_rank, 5),
            "ratio",
            share_scope
            + (
                "; SUPPRESSED: this row's impression-share metrics hit a "
                "0.0999/0.9001 clamp, so this figure is a bound, not a value "
                "(VF-11)"
                if lost_rank_sentinel
                else ""
            ),
            cur_w, COST_7D, "n/a",
        )
        if prior_cost:
            lost_budget_prior = _max_share(
                cluster, prior_cost, "searchBudgetLostImpressionShare"
            )
            lost_budget_prior_sentinel = (
                lost_budget_prior is not None
                and is_sentinel_share(float(lost_budget_prior))
            )
            fs.add(
                f"{prefix}.lost_is_budget.prior_week.ratio",
                None if (lost_budget_prior is None or lost_budget_prior_sentinel)
                else round(lost_budget_prior, 5),
                "ratio",
                share_scope
                + (
                    "; SUPPRESSED: this row's impression-share metrics hit a "
                    "0.0999/0.9001 clamp, so this figure is a bound, not a "
                    "value (VF-11)"
                    if lost_budget_prior_sentinel
                    else ""
                ),
                prior_w, COST_PRIOR7D, "n/a",
            )
            if (
                lost_budget is not None
                and not lost_budget_sentinel
                and lost_budget_prior is not None
                and not lost_budget_prior_sentinel
            ):
                comparison = fs.compare_unsafe(
                    f"{prefix}.lost_is_budget.ratio",
                    f"{prefix}.lost_is_budget.prior_week.ratio",
                    "the two facts share unit, scope and basis and differ "
                    "only in window, which is the comparison itself, not a "
                    "scope error. Recorded here so the override is visible "
                    "(mirrors brand.core.rank_lost_is.wow_delta.ratio).",
                )
                fs.add(
                    f"{prefix}.lost_is_budget.wow_delta.ratio",
                    round(Decimal(str(comparison.delta)), 5), "ratio",
                    share_scope + ". Current week minus prior week, recorded "
                    "as a declared unsafe comparison because the windows "
                    "differ by design",
                    f"{prior_w} -> {cur_w}",
                    f"{COST_7D} + {COST_PRIOR7D}", "n/a",
                )
        share = _max_share(cluster, cost, "searchImpressionShare")
        sentinel = share is not None and is_sentinel_share(float(share))
        lost_is_note = (
            "The lost-IS fields on the same row are real and are not "
            "suppressed"
            if not (lost_budget_sentinel or lost_rank_sentinel)
            else "The lost-IS fields on the same row are suppressed "
            "independently and may or may not also be real this run - check "
            "each field's own scope, do not assume from this one"
        )
        fs.add(
            f"{prefix}.impression_share.ratio",
            None if (share is None or sentinel) else round(share, 5),
            "ratio",
            share_scope
            + (
                "; SUPPRESSED: search_impression_share returned the 0.0999 "
                "'< 10%' sentinel, so any figure from it is a floor, not a "
                f"value (VF-11). {lost_is_note}"
                if sentinel
                else ""
            ),
            cur_w, COST_7D, "n/a",
        )
        if prior_cost:
            share_prior = _max_share(cluster, prior_cost, "searchImpressionShare")
            sentinel_prior = (
                share_prior is not None and is_sentinel_share(float(share_prior))
            )
            fs.add(
                f"{prefix}.impression_share.prior_week.ratio",
                None if (share_prior is None or sentinel_prior)
                else round(share_prior, 5),
                "ratio",
                share_scope
                + (
                    "; SUPPRESSED: search_impression_share returned the 0.0999 "
                    "'< 10%' sentinel, so any figure from it is a floor, not a "
                    "value (VF-11)"
                    if sentinel_prior
                    else ""
                ),
                prior_w, COST_PRIOR7D, "n/a",
            )
            if (
                share is not None and not sentinel
                and share_prior is not None and not sentinel_prior
            ):
                is_comparison = fs.compare_unsafe(
                    f"{prefix}.impression_share.ratio",
                    f"{prefix}.impression_share.prior_week.ratio",
                    "the two facts share unit, scope and basis and differ "
                    "only in window, which is the comparison itself, not a "
                    "scope error. Recorded here so the override is visible "
                    "(mirrors brand.core.rank_lost_is.wow_delta.ratio).",
                )
                fs.add(
                    f"{prefix}.impression_share.wow_delta.ratio",
                    round(Decimal(str(is_comparison.delta)), 5), "ratio",
                    share_scope + ". Current week minus prior week, recorded "
                    "as a declared unsafe comparison because the windows "
                    "differ by design",
                    f"{prior_w} -> {cur_w}",
                    f"{COST_7D} + {COST_PRIOR7D}", "n/a",
                )
        conversions = cluster_conversions(cluster, conv)
        fs.add(
            f"{prefix}.conversions.count",
            round(conversions, 3), "conversions",
            f"{ENABLED_8} - summed over the 8 actions and over the "
            f"{len(cluster.campaigns)} campaign(s) on budget {bid}",
            conv_w, CONV_7D, "8-action",
        )
        date, kind = cluster_learning(cluster, changes)
        window_days = LEARNING_DAYS_PMAX if cluster.is_pmax else LEARNING_DAYS_SEARCH
        fs.add(
            f"{prefix}.last_budget_or_target_change.date",
            date or "none in the 14 days the change_event pull covers",
            "date",
            f"{CHANGE_SCOPE} - CAMPAIGN_BUDGET amount changes and CAMPAIGN bid "
            f"or target changes on the campaign(s) on budget {bid}",
            windows["changes"], CHANGES, "n/a",
        )
        fs.add(
            f"{prefix}.last_budget_or_target_change.type",
            kind or "none",
            "text",
            f"{CHANGE_SCOPE} - change_event.change_resource_type for the most "
            f"recent budget or target change on budget {bid}",
            windows["changes"], CHANGES, "n/a",
        )
        fs.add(
            f"{prefix}.learning_window.days",
            window_days, "days",
            "campaign-guardrails rule 7: 14 days Search, 28 days Performance "
            f"Max. Budget {bid} is "
            + ("Performance Max" if cluster.is_pmax else "Search"),
            windows["current_state"], CAMPAIGNS, "n/a",
        )
        if date is not None:
            # A change found INSIDE the window settles the question outright,
            # whatever the pull covers. Only an ABSENCE needs the full window,
            # so the coverage caveat below must never pre-empt a positive hit.
            fs.add(
                f"{prefix}.learning_window.cleared",
                False,
                "boolean",
                f"campaign-guardrails rule 7: budget {bid} carries a "
                f"{kind} change dated {date}, inside its {window_days}-day "
                "window. A change found inside the window proves the window is "
                "not clear regardless of how far back the pull reaches, so no "
                "coverage caveat applies",
                windows["changes"], CHANGES, "n/a",
            )
        elif cluster.is_pmax and window_days > CHANGE_PULL_COVERAGE_DAYS:
            fs.add(
                f"{prefix}.learning_window.cleared",
                None,
                "boolean",
                f"UNANSWERABLE: budget {bid} needs a {window_days}-day clean "
                f"window, no change was found, and the change_event pull covers "
                f"only {CHANGE_PULL_COVERAGE_DAYS} days, which is that "
                f"resource's maximum. Days {CHANGE_PULL_COVERAGE_DAYS + 1} to "
                f"{window_days} cannot be checked, so ABSENCE of a change "
                "cannot be proven",
                windows["changes"], CHANGES, "n/a",
            )
        else:
            fs.add(
                f"{prefix}.learning_window.cleared",
                date is None,
                "boolean",
                f"campaign-guardrails rule 7: no CAMPAIGN_BUDGET or bid/target "
                f"change on budget {bid} inside its {window_days}-day window, "
                f"checked against the full {CHANGE_PULL_COVERAGE_DAYS}-day pull",
                windows["changes"], CHANGES, "n/a",
            )

        # Efficiency, only where rules 1 and 5 admit it.
        admissible = conversions >= CONVERSION_FLOOR and not cluster.is_tof
        if admissible:
            spend = cluster_cost(cluster, cost)
            value = cluster_conversion_value(cluster, conv)
            eff_scope = (
                f"{cur_scope} joined to {ENABLED_8} on campaign.id, then "
                f"aggregated to campaign_budget.id={bid}. Cost and the 8-action "
                "segment cannot share one query (VF-3), so these are two pulls "
                "over the same window"
            )
            fs.add(
                f"{prefix}.assumed_value_cpa.cad",
                money(spend / conversions), "currency_cad",
                eff_scope
                + ". Cost per 8-action conversion. An 8-action conversion is a "
                "free-trial completion, not a customer, and its value is an "
                "assigned constant, so this is an assumed-value figure and must "
                "be labelled one (VF-8)",
                conv_w, f"{COST_7D} + {CONV_7D}", "8-action",
            )
            target_kind, target_value, why = cluster_target(cluster, targets)
            actual_roas = None if spend == 0 else value / spend
            if target_kind == "target_roas" and actual_roas is not None:
                fs.add(
                    f"{prefix}.target_roas.ratio", round(target_value, 4), "ratio",
                    f"{cur_scope} - the set target on every campaign on budget "
                    f"{bid}, read from this run's pull",
                    windows["current_state"], CAMPAIGNS, "n/a",
                )
                fs.add(
                    f"{prefix}.beating_target_on_8_actions",
                    bool(actual_roas > target_value), "boolean",
                    "DIRECTION ONLY, and the two numbers must be presented "
                    "separately: the actual is assumed-value ROAS on the 8 "
                    "actions, the target is managed by Google against the "
                    "broader account-default goal set, so the two are on "
                    "different bases and their difference is not a clean gap "
                    f"(campaign-guardrails rule 4). Budget {bid}",
                    conv_w, f"{COST_7D} + {CONV_7D} + {CAMPAIGNS}", "8-action",
                )
            else:
                fs.add(
                    f"{prefix}.target_roas.ratio", None, "ratio",
                    f"UNAVAILABLE for budget {bid}: "
                    + (why or f"the set target is a {target_kind}, not a ROAS"),
                    windows["current_state"], CAMPAIGNS, "n/a",
                )
                trail.append(
                    f"budget {bid}: no actual-versus-target figure. "
                    + (why or f"its target is a {target_kind}, not a ROAS.")
                )
            fs.add(
                f"{prefix}.assumed_value_roas.ratio",
                None if spend == 0 else round(value / spend, 4), "ratio",
                eff_scope
                + ". conversions_value divided by cost. An assumed-value "
                "figure: every one of the 8 actions carries "
                "always_use_default_value, so the numerator is assigned "
                "constants and not revenue. Label it assumed-value in the same "
                "sentence and quote a real-money anchor beside it (VF-2, VF-8)",
                conv_w, f"{COST_7D} + {CONV_7D}", "8-action",
            )
        elif cluster.is_tof:
            trail.append(
                f"budget {bid}: no efficiency fact emitted. Top-of-funnel "
                "channel, never judged on BOFU CPA or ROAS "
                "(campaign-guardrails rule 1)."
            )
        else:
            trail.append(
                f"budget {bid}: no efficiency fact emitted. "
                f"{round(conversions, 2)} 8-action conversion(s) in the window, "
                f"below the floor of {CONVERSION_FLOOR} "
                "(campaign-guardrails rule 5). Monitor note only."
            )
    return trail, active, idle


def add_constraint_summary(fs, active, cost, conv, changes, windows) -> None:  # noqa: C901
    """How many clusters are constrained, and how many survive rule 7."""
    scope, cur_w = ENABLED, windows["cost"]
    over_gate, over_exposure, in_learning = [], [], []
    for cluster in active:
        lost = _max_share(cluster, cost, "searchBudgetLostImpressionShare")
        if lost is None:
            continue
        if lost > CONSTRAINED_GATE:
            over_gate.append(cluster)
        if lost > EXPOSURE_GATE:
            over_exposure.append(cluster)
            date, _ = cluster_learning(cluster, changes)
            if date is not None:
                in_learning.append(cluster)

    gate_scope = (
        f"{scope} - budget clusters whose worst campaign lost more than "
        "{gate}% of impression share to budget"
    )
    fs.add(
        "constraint.over_30pct.budget.count", len(over_gate), "count",
        gate_scope.format(gate=30), cur_w, COST_7D, "n/a",
    )
    fs.add(
        "constraint.over_20pct.budget.count", len(over_exposure), "count",
        gate_scope.format(gate=20), cur_w, COST_7D, "n/a",
    )
    fs.add(
        "constraint.over_20pct.in_learning.budget.count", len(in_learning), "count",
        "budget clusters over the 20% lost-IS-to-budget gate that also carry a "
        "CAMPAIGN_BUDGET or bid/target change inside their learning window, so "
        "campaign-guardrails rule 7 downgrades any budget recommendation on "
        "them to monitor",
        windows["changes"], f"{COST_7D} + {CHANGES}", "n/a",
    )
    fs.add(
        "constraint.over_20pct.recommendable.budget.count",
        len(over_exposure) - len(in_learning), "count",
        "budget clusters over the 20% gate that clear their learning window and "
        "could carry a budget recommendation before the other guardrails run",
        windows["changes"], f"{COST_7D} + {CHANGES}", "n/a",
    )
    wow_clusters = [
        c for c in active
        if f"cluster.{c.budget_id}.lost_is_budget.wow_delta.ratio" in fs
    ]
    wow_available = bool(wow_clusters)
    fs.add(
        "constraint.lost_is_budget.week_over_week.available", wow_available,
        "boolean",
        (
            f"{len(wow_clusters)} active budget cluster(s) have a real "
            "week-over-week lost-to-budget delta this run "
            f"({COST_PRIOR7D} selects metrics.search_budget_lost_impression_"
            "share for this pull), so 'is the budget constraint getting "
            "worse' can be assessed for those clusters"
            if wow_available
            else f"UNANSWERABLE: no active cluster has both weeks' "
            "metrics.search_budget_lost_impression_share this run"
        ),
        windows["prior"], COST_PRIOR7D, "n/a",
    )
    exposed = [
        c
        for c in over_exposure
        if f"cluster.{c.budget_id}.beating_target_on_8_actions" in fs
        and fs.value(f"cluster.{c.budget_id}.beating_target_on_8_actions")
    ]
    fs.add(
        "aug17.exposed.budget.count", len(exposed), "count",
        "budget clusters that are budget-limited above the 20% gate, carry a set "
        "ROAS target, clear the 5-conversion floor, and are beating that target "
        "on the 8 actions. This is the population the 2026-08-17 change pulls "
        "back toward the looser stated target. Direction only - the actual and "
        "the target are on different conversion bases",
        windows["conv"], f"{COST_7D} + {CONV_7D} + {CAMPAIGNS}", "8-action",
    )
    clearing = [c for c in active if cluster_conversions(c, conv) >= CONVERSION_FLOOR]
    fs.add(
        "floor.clusters_clearing_5_conversions.count", len(clearing), "count",
        f"{ENABLED_8} - budget clusters with at least {CONVERSION_FLOOR} "
        "8-action conversions in the window (campaign-guardrails rule 5)",
        windows["conv"], CONV_7D, "8-action",
    )
    fs.add(
        "floor.clusters_below_5_conversions.count", len(active) - len(clearing),
        "count",
        f"{ENABLED_8} - active budget clusters below the floor of "
        f"{CONVERSION_FLOOR} 8-action conversions, so monitor notes only",
        windows["conv"], CONV_7D, "8-action",
    )


def add_idle(fs, idle, clusters, changes, windows, plan: MonthPlan) -> None:
    """Allocation sitting on campaigns that have not served in two weeks.

    A spend and allocation observation with no conversion denominator, so the
    rule 5 floor does not apply to it, and rule 7's table has no row that
    downgrades a structural allocation finding.
    """
    scope, window, src = ENABLED, windows["current_state"], CAMPAIGNS
    allocated = sum(
        (c.daily_cad for c in idle if c.daily_cad is not None), Decimal(0)
    )
    total = sum_daily_budgets(clusters)
    idle_scope = (
        f"{scope} - budget clusters with zero cost in BOTH the current and the "
        "prior 7-day window, keyed on campaign_budget.id"
    )
    fs.add("idle.budget.count", len(idle), "count", idle_scope, window, src, "n/a")
    fs.add(
        "idle.campaign.count", sum(len(c.campaigns) for c in idle), "count",
        idle_scope, window, src, "n/a",
    )
    fs.add(
        "idle.daily_allocation.cad", money(allocated), "currency_cad",
        idle_scope + ", one amount per distinct budget", window, src, "n/a",
    )
    fs.add(
        "idle.monthly_allocation.cad", money(allocated * plan.days),
        "currency_cad",
        idle_scope + f", one amount per distinct budget, times {plan.days} days",
        window, src, "n/a",
    )
    fs.add(
        "idle.share_of_daily_allocation.pct",
        None if not total else money(allocated / total * 100), "percent",
        "idle daily allocation as a share of the budget-keyed daily allocation "
        "total, both from " + CAMPAIGNS,
        window, src, "n/a",
    )
    touched = sum(
        1 for c in idle for cid in c.campaign_ids if changes.get(cid)
    )
    fs.add(
        "idle.campaigns_with_budget_or_target_change.count", touched, "count",
        f"{CHANGE_SCOPE} - idle campaigns carrying any CAMPAIGN_BUDGET or "
        "bid/target change in the 14 days the pull covers. Zero means nothing "
        "recent explains the silence and no learning window applies",
        windows["changes"], CHANGES, "n/a",
    )


def spend_month_plan(spend_rows: list[dict]) -> MonthPlan:
    """One month per spend pull, and its stated-budget status (rule 8).

    Called before any fact is written. A pull spanning two months is refused -
    pacing has no meaning across a month boundary. A single month with no
    stated Google Ads split is NOT refused: the builder runs, and every
    budget-dependent pacing fact is null with the config's reason attached, so
    a September run never paces against August's figure and never derives one.
    A month the config says nothing about at all still refuses (see
    `account_config.stated_budget`).
    """
    months = sorted(
        {
            ((row.get("segments") or {}).get("date") or "")[:7]
            for row in spend_rows
        }
    )
    if len(months) != 1 or not months[0]:
        raise BuildError(
            f"REFUSED to pace. {SPEND_MTD} covers month(s) {months}; pacing is "
            "a single-month figure against a single month's stated budget. "
            "Split the pull by calendar month."
        )
    return month_plan(months[0])


def add_pacing(fs, spend_rows, windows, plan: MonthPlan) -> list:
    """Real pacing: spend to date against budget pro-rated by day, in CAD.

    When `plan` carries no stated budget, the spend-side facts (month-to-date
    spend, day counts, run-rate projection in CAD, trailing-7-day average) are
    still real; every fact that needs the budget figure is null with the
    reason. No silent partials: the null facts exist, so a report citing them
    says why the figure is unavailable instead of dropping the line.
    """
    if plan.stated:
        trail = [
            f"{SPEND_MTD}: every row is in {plan.month}, whose Google Ads "
            "split is stated. Pacing allowed."
        ]
    else:
        trail = [
            f"{SPEND_MTD}: every row is in {plan.month}, which has NO stated "
            "Google Ads split. Spend-side facts written; every "
            "budget-dependent pacing fact is null with the reason."
        ]
    by_date = {}
    for row in spend_rows:
        date = (row.get("segments") or {}).get("date")
        if not date:
            raise BuildError(f"{SPEND_MTD}: a row has no segments.date")
        if date in by_date:
            raise BuildError(
                f"{SPEND_MTD}: date {date} appears twice. The response is "
                "segmented by something else too; aggregate per date first."
            )
        by_date[date] = micros_to_currency((row.get("metrics") or {}).get("costMicros"))

    dates = sorted(by_date)
    elapsed = len(dates)
    mtd = sum(by_date.values(), Decimal(0))
    window = windows["spend"]
    spend_scope = f"segments.date BETWEEN {dates[0]} AND {dates[-1]}"
    plan_scope = budget_plan_scope(plan)
    budget = plan.budget_cad          # None when the month has no stated split
    if plan.stated:
        derived_scope = (
            f"customer 1234567890, every campaign, no status filter; {plan.label} "
            f"spend for {dates[0]}..{dates[-1]} against the CAD {budget:,.0f} "
            "monthly budget read from budget-plan.md and pro-rated over "
            f"{plan.days} days"
        )
    else:
        derived_scope = (
            f"customer 1234567890, every campaign, no status filter; {plan.label} "
            f"spend for {dates[0]}..{dates[-1]}. {plan.reason}"
        )
    both = f"{SPEND_MTD} + {BUDGET_PLAN}"

    fs.add(
        f"pacing.{plan.slug}.budget.cad",
        None if budget is None else money(budget), "currency_cad",
        plan_scope, plan.window, BUDGET_PLAN, "n/a",
    )
    fs.add(
        f"pacing.{plan.slug}.budget.usd",
        None if plan.budget_usd is None else money(plan.budget_usd),
        "currency_usd", plan_scope, plan.window, BUDGET_PLAN, "n/a",
    )
    fs.add(
        "pacing.fx.usd_to_cad.ratio", USD_TO_CAD, "ratio",
        account_config.fx_note(),
        plan.window, BUDGET_PLAN, "n/a",
    )
    fs.add(
        "pacing.mtd.spend.cad", money(mtd), "currency_cad", spend_scope, window,
        SPEND_MTD, "n/a",
    )
    fs.add(
        "pacing.mtd.days.count", elapsed, "days", spend_scope, window, SPEND_MTD,
        "n/a",
    )
    fs.add(
        f"pacing.{plan.slug}.days.count", plan.days, "days", plan_scope,
        plan.window, BUDGET_PLAN, "n/a",
    )
    fs.add(
        "pacing.mtd.scope_note", SPEND_SCOPE_NOTE, "text", spend_scope, window,
        SPEND_MTD, "n/a",
    )

    budget_to_date = None if budget is None else budget * elapsed / plan.days
    fs.add(
        "pacing.budget_to_date.cad",
        None if budget_to_date is None else money(budget_to_date),
        "currency_cad", derived_scope, window, both, "n/a",
    )
    fs.add(
        "pacing.vs_budget_to_date.pct",
        None if budget_to_date is None else money(mtd / budget_to_date * 100),
        "percent", derived_scope, window, both, "n/a",
    )
    fs.add(
        "pacing.over_budget_to_date.pct",
        None if budget_to_date is None else money((mtd / budget_to_date - 1) * 100),
        "percent", derived_scope,
        window, both, "n/a",
    )
    fs.add(
        "pacing.month_elapsed.pct", money(Decimal(elapsed) / plan.days * 100),
        "percent", derived_scope, window, both, "n/a",
    )
    fs.add(
        "pacing.budget_consumed.pct",
        None if budget is None else money(mtd / budget * 100),
        "percent", derived_scope, window, both, "n/a",
    )
    forecast = mtd / elapsed * plan.days
    fs.add(
        "pacing.runrate_forecast.cad", money(forecast), "currency_cad",
        derived_scope + ", extended at the month-to-date daily average",
        window, both, "n/a",
    )
    fs.add(
        "pacing.runrate_forecast.vs_budget.pct",
        None if budget is None else money(forecast / budget * 100), "percent",
        derived_scope + ", extended at the month-to-date daily average",
        window, both, "n/a",
    )
    remaining_days = plan.days - elapsed
    remaining = None if budget is None else budget - mtd
    fs.add(
        "pacing.remaining.budget.cad",
        None if remaining is None else money(remaining), "currency_cad",
        derived_scope, window, both, "n/a",
    )
    fs.add(
        "pacing.remaining.days.count", remaining_days, "days", derived_scope,
        window, both, "n/a",
    )
    allowed = (
        remaining / remaining_days
        if (remaining is not None and remaining_days)
        else None
    )
    fs.add(
        "pacing.remaining.allowed_daily.cad",
        None if allowed is None else money(allowed), "currency_cad",
        derived_scope + ", budget left divided by days left", window, both, "n/a",
    )
    last7 = dates[-7:]
    avg7 = sum((by_date[d] for d in last7), Decimal(0)) / len(last7)
    fs.add(
        "pacing.last_7_days.avg_daily.cad", money(avg7), "currency_cad",
        f"segments.date BETWEEN {last7[0]} AND {last7[-1]}", window, SPEND_MTD,
        "n/a",
    )
    fs.add(
        "pacing.required_daily_cut.pct",
        None if (allowed is None or not avg7) else money((1 - allowed / avg7) * 100),
        "percent",
        derived_scope
        + ", cut needed against the last 7 days' average to land on budget",
        window, both, "n/a",
    )
    fs.add(
        "pacing.planned_step_down_from_july.pct", PLANNED_STEP_DOWN_PCT, "percent",
        "planned month-over-month reduction in the all-channel budget, August "
        "against July, from budget-plan.md. THE PLAN, NOT A FINDING: never "
        "report the reduction as a problem (campaign-guardrails rule 8). The "
        "overspend against the lower budget is the separate, real finding",
        PLANNED_STEP_DOWN_WINDOW, BUDGET_PLAN, "n/a",
    )
    for month in account_config.months_with_no_stated_split():
        slug = dt.date.fromisoformat(month + "-01").strftime("%B").lower()
        fs.add(
            f"pacing.{slug}.google_ads_budget.cad", None, "currency_cad",
            NO_SPLIT_REASON, f"{month[:4]} {slug}", BUDGET_PLAN, "n/a",
        )
    trail.append(
        f"{SPEND_MTD}: {elapsed} day(s) {dates[0]}..{dates[-1]}; pacing computed "
        f"from this run's pull, not restated from {BUDGET_PLAN}"
    )
    return trail


def add_brand(fs, clusters, cost, prior_cost, windows) -> list:
    """The only admissible Brand trigger: rank-lost IS rising week over week."""
    trail = []
    scope = ENABLED
    brand = [
        c
        for cluster in clusters.values()
        for c in cluster.campaigns
        if bucket_of(c) == "brand"
    ]
    core = [c for c in brand if "Brand - Core" in (c.get("name") or "")]
    if not core:
        trail.append(
            "brand: no campaign matched 'Brand - Core' in this run's pull, so "
            "the rule 2 trigger could not be evaluated."
        )
        return trail
    campaign = core[0]
    cid = campaign["id"]
    now = (cost.get(cid) or {}).get("searchRankLostImpressionShare")
    prior = (prior_cost.get(cid) or {}).get("searchRankLostImpressionShare") if prior_cost else None
    share_scope = (
        f"{scope} - metrics.search_rank_lost_impression_share on Brand Core, "
        f"campaign {cid}. The ONLY admissible trigger for a Brand budget or bid "
        "recommendation (campaign-guardrails rule 2)"
    )
    fs.add(
        "brand.core.rank_lost_is.current_week.ratio",
        None if now is None else round(Decimal(str(now)), 5), "ratio",
        share_scope, windows["cost"], COST_7D, "n/a",
    )
    fs.add(
        "brand.core.rank_lost_is.prior_week.ratio",
        None if prior is None else round(Decimal(str(prior)), 5), "ratio",
        share_scope, windows["prior"], COST_PRIOR7D, "n/a",
    )
    if now is None or prior is None:
        fs.add(
            "brand.core.growth_trigger_met", None, "boolean",
            "UNANSWERABLE: one of the two weeks' rank-lost impression share is "
            "absent, so 'rising week over week' cannot be evaluated. With the "
            "trigger unevaluable, no Brand recommendation is admissible",
            windows["prior"], f"{COST_7D} + {COST_PRIOR7D}", "n/a",
        )
        return trail
    comparison = fs.compare_unsafe(
        "brand.core.rank_lost_is.current_week.ratio",
        "brand.core.rank_lost_is.prior_week.ratio",
        "campaign-guardrails rule 2 requires this exact week-over-week "
        "comparison and requires quoting both weeks. The two facts share unit, "
        "scope and basis and differ only in window, which is the comparison "
        "itself, not a scope error. Recorded here so the override is visible.",
    )
    rising = comparison.delta > 0
    fs.add(
        "brand.core.rank_lost_is.wow_delta.ratio",
        round(Decimal(str(comparison.delta)), 5), "ratio",
        share_scope + ". Current week minus prior week, recorded as a declared "
        "unsafe comparison because the windows differ by design",
        f"{windows['prior']} -> {windows['cost']}",
        f"{COST_7D} + {COST_PRIOR7D}", "n/a",
    )
    fs.add(
        "brand.core.growth_trigger_met", bool(rising), "boolean",
        share_scope
        + ". True only when rank-lost impression share ROSE week over week. "
        "False means no Brand budget or bid recommendation is admissible on any "
        "ground, including cheap CPA, strong ROAS or lost impression share",
        f"{windows['prior']} -> {windows['cost']}",
        f"{COST_7D} + {COST_PRIOR7D}", "n/a",
    )
    trail.append(
        f"brand: rule 2 trigger {'MET' if rising else 'NOT met'} - Brand Core "
        f"rank-lost IS {prior} prior week, {now} current week."
    )
    return trail


def add_subtotals(fs, clusters, cost, conv, windows) -> None:
    """Brand and non-brand subtotals, separately. Never a blended lead figure."""
    by_bucket_cost: dict = collections.defaultdict(Decimal)
    by_bucket_conv: dict = collections.defaultdict(Decimal)
    for cluster in clusters.values():
        for campaign in cluster.campaigns:
            bucket = bucket_of(campaign)
            cid = campaign["id"]
            by_bucket_cost[bucket] += micros_to_currency(
                (cost.get(cid) or {}).get("costMicros", "0")
            ) or Decimal(0)
            if cid in conv:
                by_bucket_conv[bucket] += conv[cid][0]
    for bucket in sorted(set(by_bucket_cost) | set(by_bucket_conv)):
        fs.add(
            f"subtotal.{bucket}.cost.cad", money(by_bucket_cost[bucket]),
            "currency_cad",
            f"{ENABLED} - campaigns bucketed {bucket} by name and channel from "
            "this run's pull, per account-conventions. Reported as a subtotal, "
            "never blended with the others (campaign-guardrails rule 2)",
            windows["cost"], f"{COST_7D} + {CAMPAIGNS}", "n/a",
        )
        fs.add(
            f"subtotal.{bucket}.conversions.count",
            round(by_bucket_conv[bucket], 3), "conversions",
            f"{ENABLED_8} - campaigns bucketed {bucket} by name and channel "
            "from this run's pull",
            windows["conv"], f"{CONV_7D} + {CAMPAIGNS}", "8-action",
        )


def add_targets(fs, rows, targets, changes, windows) -> list:
    """Which targets are set, on which field, and where the legacy field lies."""
    trail = []
    scope, window, src = ENABLED, windows["current_state"], CAMPAIGNS
    campaigns = [r.get("campaign") or {} for r in rows]
    maximize = [
        c
        for c in campaigns
        if (c.get("biddingStrategyType") or "").startswith("MAXIMIZE_")
    ]
    fs.add(
        "targets.maximize_strategy.campaign.count", len(maximize), "count",
        f"{scope} AND campaign.bidding_strategy_type IN "
        "(MAXIMIZE_CONVERSIONS, MAXIMIZE_CONVERSION_VALUE)",
        window, src, "n/a",
    )
    fs.add(
        "targets.set.campaign.count", len(targets), "count",
        f"{scope} - campaigns carrying a set target on ANY of the four target "
        "fields. Read from this run's pull, never assumed from the name suffix",
        window, src, "n/a",
    )
    by_field = collections.Counter(field for _, _, field in targets.values())
    for container, leaf, kind, _ in TARGET_READERS:
        field = f"campaign.{container}.{leaf}"
        fs.add(
            f"targets.field.{kind}.populated.campaign.count",
            by_field.get(field, 0), "count",
            f"{scope} - campaigns whose target was read from {field}",
            window, src, "n/a",
        )
    legacy_roas = by_field.get("campaign.targetRoas.targetRoas", 0)
    if legacy_roas == 0:
        trail.append(
            "targets: the LEGACY field campaign.target_roas.target_roas is "
            "populated for ZERO enabled campaigns, while "
            "campaign.maximize_conversion_value.target_roas is populated for "
            f"{by_field.get('campaign.maximizeConversionValue.targetRoas', 0)}. "
            "Any logic anywhere in the pack that reads the legacy field is "
            "silently reading 'no target'. Reported, not fixed here."
        )
    search_target_cpa = [
        cid
        for cid, (kind, _, _) in targets.items()
        if kind.endswith("target_cpa")
        and next(
            (
                c.get("advertisingChannelType")
                for c in campaigns
                if c["id"] == cid
            ),
            None,
        )
        == "SEARCH"
    ]
    fs.add(
        "targets.target_cpa.search_channel.campaign.count",
        len(search_target_cpa), "count",
        f"{scope} AND campaign.advertising_channel_type=SEARCH - Search "
        "campaigns carrying a target CPA. Zero means any rule phrased as a "
        "multiple of target CPA has nothing to multiply on Search",
        window, src, "n/a",
    )
    for campaign in campaigns:
        cid = campaign["id"]
        if cid not in targets:
            continue
        kind, value, field = targets[cid]
        fs.add(
            f"campaign.{cid}.{kind}.value",
            value,
            "ratio" if kind.endswith("roas") else "currency_cad",
            f"{scope} - campaign {cid}, read from {field}. Google manages this "
            "target against the account-default goal set, which is broader than "
            "the 8 actions, so it is NOT on the same basis as any figure this "
            "skill computes (campaign-guardrails rule 4)",
            window, src, "n/a",
        )
    changed_aug17 = {
        cid
        for cid, events in changes.items()
        for date, kind, fields in events
        if date == AUG17 and ("targetRoas" in fields or "targetCpa" in fields)
    }
    fs.add(
        "targets.changed_on_2026_08_17.campaign.count", len(changed_aug17), "count",
        f"{CHANGE_SCOPE} - campaigns whose changed_fields on {AUG17} include a "
        "target. Every one of these is inside its learning window",
        windows["changes"], CHANGES, "n/a",
    )
    fs.add(
        "aug17.date_has_passed", True, "boolean",
        f"the target-based bidding change took effect {AUG17}. The "
        "days-remaining countdown and the pre-emptive tighten-before-it-lands "
        "recommendation are retired; the standing rule that target-tightening "
        "precedes a budget increase is not date-dependent and survives "
        "(campaign-guardrails rule 5)",
        window, BUDGET_PLAN, "n/a",
    )
    return trail


def add_learning_summary(fs, change_rows, changes, windows, clusters) -> None:
    """Rule 7 needs counts with their units stated. Events and campaigns differ.

    A `CAMPAIGN_BUDGET` change_event names exactly one campaign even when the
    budget it changed is shared (VF-19 again, this time in the change log): a
    shared pool's amount change generates one event tied to one sibling, so
    counting "distinct campaigns in the event rows" undercounts every campaign
    that shares the changed budget but was not the one named. The `.budgets.
    count` and `.affected_campaigns.count` facts below resolve each event's
    campaign back to its `campaign_budget.id` and then to every campaign that
    id covers, so the blast radius is not understated on this account.
    """
    window, src = windows["changes"], CHANGES
    fs.add(
        "learning.change_events.total.count", len(change_rows), "count",
        CHANGE_SCOPE, window, src, "n/a",
    )
    cid_to_bid = {
        cid: cluster.budget_id
        for cluster in clusters.values()
        for cid in cluster.campaign_ids
    }
    bid_to_cids = {
        cluster.budget_id: set(cluster.campaign_ids)
        for cluster in clusters.values()
    }

    def affected(cids) -> "tuple[set, set]":
        """(distinct budgets touched, every campaign those budgets cover)."""
        bids = {cid_to_bid[cid] for cid in cids if cid in cid_to_bid}
        campaigns: set = set()
        for bid in bids:
            campaigns |= bid_to_cids.get(bid, set())
        return bids, campaigns

    budget_events = [
        (cid, date)
        for cid, events in changes.items()
        for date, kind, _ in events
        if kind == "CAMPAIGN_BUDGET"
    ]
    fs.add(
        "learning.campaign_budget.events.count", len(budget_events), "count",
        f"{CHANGE_SCOPE} AND change_event.change_resource_type=CAMPAIGN_BUDGET "
        "- EVENTS, not campaigns",
        window, src, "n/a",
    )
    fs.add(
        "learning.campaign_budget.campaigns.count",
        len({cid for cid, _ in budget_events}), "count",
        f"{CHANGE_SCOPE} AND change_event.change_resource_type=CAMPAIGN_BUDGET "
        "- distinct CAMPAIGNS named in the event rows. Undercounts when a "
        "named campaign shares its budget with siblings the event stream did "
        "not separately name; see learning.campaign_budget.affected_"
        "campaigns.count for the true blast radius",
        window, src, "n/a",
    )
    all_bids, all_affected = affected({cid for cid, _ in budget_events})
    fs.add(
        "learning.campaign_budget.budgets.count", len(all_bids), "count",
        f"{CHANGE_SCOPE} AND change_event.change_resource_type=CAMPAIGN_BUDGET "
        "- distinct campaign_budget.id resolved from the named campaigns",
        window, src, "n/a",
    )
    fs.add(
        "learning.campaign_budget.affected_campaigns.count", len(all_affected),
        "count",
        f"{CHANGE_SCOPE} AND change_event.change_resource_type=CAMPAIGN_BUDGET "
        "- every enabled campaign sharing one of the budgets above, whether or "
        "not the event stream named it individually (VF-19)",
        window, src, "n/a",
    )
    aug17 = [(cid, date) for cid, date in budget_events if date == AUG17]
    fs.add(
        "learning.campaign_budget.on_2026_08_17.events.count", len(aug17), "count",
        f"{CHANGE_SCOPE} AND change_event.change_resource_type=CAMPAIGN_BUDGET "
        f"AND the change date is {AUG17} - EVENTS",
        window, src, "n/a",
    )
    fs.add(
        "learning.campaign_budget.on_2026_08_17.campaigns.count",
        len({cid for cid, _ in aug17}), "count",
        f"{CHANGE_SCOPE} AND change_event.change_resource_type=CAMPAIGN_BUDGET "
        f"AND the change date is {AUG17} - distinct CAMPAIGNS named in the "
        "event rows. Undercounts for the same reason as learning.campaign_"
        "budget.campaigns.count",
        window, src, "n/a",
    )
    aug17_bids, aug17_affected = affected({cid for cid, _ in aug17})
    fs.add(
        "learning.campaign_budget.on_2026_08_17.budgets.count", len(aug17_bids),
        "count",
        f"{CHANGE_SCOPE} AND change_event.change_resource_type=CAMPAIGN_BUDGET "
        f"AND the change date is {AUG17} - distinct campaign_budget.id "
        "resolved from the named campaigns",
        window, src, "n/a",
    )
    fs.add(
        "learning.campaign_budget.on_2026_08_17.affected_campaigns.count",
        len(aug17_affected), "count",
        f"{CHANGE_SCOPE} AND change_event.change_resource_type=CAMPAIGN_BUDGET "
        f"AND the change date is {AUG17} - every enabled campaign sharing one "
        "of those budgets, whether or not the event stream named it "
        "individually (VF-19)",
        window, src, "n/a",
    )
    fs.add(
        "learning.search.window.days", LEARNING_DAYS_SEARCH, "days",
        "campaign-guardrails rule 7, Search", window, src, "n/a",
    )
    fs.add(
        "learning.pmax.window.days", LEARNING_DAYS_PMAX, "days",
        "campaign-guardrails rule 7, Performance Max", window, src, "n/a",
    )
    fs.add(
        "learning.change_event_pull.coverage.days", CHANGE_PULL_COVERAGE_DAYS,
        "days",
        "the change_event resource's maximum lookback, so a 28-day Performance "
        "Max window can never be fully cleared from it",
        window, src, "n/a",
    )


def add_elasticity(fs) -> None:
    """Sizing bands. Every increase recommendation must name the band it sits in."""
    for name, usd, degradation, note in ELASTICITY:
        fs.add(
            f"elasticity.{name}.max_increase.usd_per_month", money(usd),
            "currency_usd", f"{ELASTICITY_SCOPE} - {note}", ELASTICITY_WINDOW,
            BUDGET_PLAN, "n/a",
        )
        fs.add(
            f"elasticity.{name}.max_increase.cad_per_month",
            money(usd * USD_TO_CAD), "currency_cad",
            f"{ELASTICITY_SCOPE} - {note}, converted at the flat 1.36 rate",
            ELASTICITY_WINDOW, BUDGET_PLAN, "n/a",
        )
        fs.add(
            f"elasticity.{name}.cpa_degradation.pct", money(degradation), "percent",
            f"{ELASTICITY_SCOPE} - {note}", ELASTICITY_WINDOW, BUDGET_PLAN, "n/a",
        )
    fs.add(
        "elasticity.band_3.max_increase.usd_per_month", None, "currency_usd",
        "above the last stated band no data exists in the owner's projections. "
        "Do not recommend an increase in this band (campaign-guardrails rule 8)",
        ELASTICITY_WINDOW, BUDGET_PLAN, "n/a",
    )
    fs.add(
        f"anchor.real_cac.{CAC_ANCHOR_SLUG}.usd", money(CAC_ANCHOR_USD), "currency_usd",
        ANCHOR_SCOPE + ". Real cost per real customer, not an assumed value",
        f"{CAC_ANCHOR_MONTH}", BUDGET_PLAN, "n/a",
    )
    fs.add(
        f"anchor.real_cac.{CAC_ANCHOR_SLUG}.cad", money(CAC_ANCHOR_USD * USD_TO_CAD),
        "currency_cad", ANCHOR_SCOPE + ", converted at the flat 1.36 rate",
        f"{CAC_ANCHOR_MONTH}", BUDGET_PLAN, "n/a",
    )
    fs.add(
        f"anchor.real_cost_per_free_trial.{CAC_ANCHOR_SLUG}.usd", money(CPA_ANCHOR_USD),
        "currency_usd",
        ANCHOR_SCOPE + ". Real cost per real free trial, the closest real-money "
        "anchor for an 8-action cost-per-conversion figure",
        f"{CAC_ANCHOR_MONTH}", BUDGET_PLAN, "n/a",
    )
    fs.add(
        f"anchor.real_cost_per_free_trial.{CAC_ANCHOR_SLUG}.cad",
        money(CPA_ANCHOR_USD * USD_TO_CAD), "currency_cad",
        ANCHOR_SCOPE + ", converted at the flat 1.36 rate",
        f"{CAC_ANCHOR_MONTH}", BUDGET_PLAN, "n/a",
    )


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build(raw_dir: Path) -> FactSet:
    manifest = S.Manifest(raw_dir)
    trail: list[str] = []
    if not manifest.present:
        raise BuildError(
            f"{raw_dir}: no {S.MANIFEST}. Temporality is declared by whatever "
            "ran the pull and cannot be inferred (VF-17 rule 5), so no fact "
            "here could carry an honest window. Write the manifest first."
        )

    # Consolidated validation: every contradiction in one pass, one raise.
    # A single contradiction re-raises the original exception unchanged; two
    # or more are listed together, so a raw directory with several bad pulls
    # costs one build cycle to diagnose instead of one per defect. Nothing is
    # written on any failure.
    problems = pulls.ProblemLog()
    for name in REQUIRED:
        if not (raw_dir / name).exists():
            problems.record(
                BuildError(f"{name}: missing from {raw_dir}"), file=name
            )

    present = [n for n in REQUIRED + OPTIONAL
               if (raw_dir / n).exists() and not problems.broken(n)]
    loaded = {}
    for name in present:
        rows = problems.run(load_results, raw_dir, name, file=name)
        if rows is None:
            continue
        loaded[name] = rows
        trail.append(manifest.audit_line(name))
        line = problems.run(
            manifest.check_row_count, name, len(rows), file=name
        )
        if line:
            trail.append(line)
        line = problems.run(verify_temporality, manifest, name, file=name)
        if line:
            trail.append(line)
        if name in SCOPE_FOR:
            line = problems.run(
                verify_scope, manifest, name, SCOPE_FOR[name], file=name
            )
            if line:
                trail.append(line)
        else:
            trail.append(
                f"{name}: scope label is the declared date range only; the pull "
                "applies no campaign or status filter. " + SPEND_SCOPE_NOTE
            )
    for name in manifest.undeclared_files():
        trail.append(
            f"{name}: present on disk but ABSENT from {S.MANIFEST}. Not read by "
            "this builder."
        )

    as_of = manifest.pulled.isoformat() if manifest.pulled else None
    if not as_of:
        raise BuildError(
            f"{raw_dir.name} is not a YYYY-MM-DD directory, so a current-state "
            "window cannot be written (VF-17 rule 4)."
        )
    windows = {
        "current_state": current_state_window(as_of),
        "cost": manifest.window_for(COST_7D, windowed_fallback=False),
        "prior": manifest.window_for(COST_PRIOR7D, windowed_fallback=False),
        "conv": manifest.window_for(CONV_7D, windowed_fallback=False),
        "spend": manifest.window_for(SPEND_MTD, windowed_fallback=False),
        "changes": manifest.window_for(CHANGES, windowed_fallback=False),
    }
    if (not problems.broken(CAMPAIGNS)
            and not manifest.is_current_state(CAMPAIGNS)):
        problems.record(BuildError(
            f"{CAMPAIGNS} is declared "
            f"{manifest.declared_temporality(CAMPAIGNS)!r}, not current-as-of-*. "
            "Budgets, targets and structure are current-state claims and a "
            "windowed pull cannot answer them (VF-17 rule 1)."
        ))

    # WoW window pairs, where the prior-week pulls exist: equal length,
    # adjacent, finished; anchored or rolling, never a relative prior
    # (builder_core.verify_week_pair).
    for cur_name, pri_name in ((COST_7D, COST_PRIOR7D), (CONV_7D, CONV_PRIOR7D)):
        if pri_name not in loaded or problems.broken(cur_name) \
                or problems.broken(pri_name):
            continue
        line = problems.run(
            core.verify_week_pair,
            manifest.declared_temporality(cur_name) or "",
            manifest.declared_temporality(pri_name) or "",
            manifest.pulled, cur_name, pri_name,
            file=cur_name,
        )
        if line:
            trail.append(line)

    problems.raise_if_any(str(raw_dir))
    campaign_rows = loaded[CAMPAIGNS]
    clusters = build_clusters(campaign_rows)
    try:
        refuse_campaign_keyed_total(campaign_rows)
    except BuildError as exc:
        trail.append(
            "row keys (VF-19): campaign-keyed budget totals REFUSED for this "
            f"pull. {exc}"
        )
    else:
        trail.append(
            "row keys (VF-19): no budget id repeats across campaigns in this "
            "pull, so a campaign-keyed total would not have double counted. "
            "The budget-keyed total is still the one written."
        )

    cost = metrics_by_campaign(loaded[COST_7D], COST_7D)
    prior_cost = (
        metrics_by_campaign(loaded[COST_PRIOR7D], COST_PRIOR7D)
        if COST_PRIOR7D in loaded
        else {}
    )
    conv = conversions_by_campaign(loaded[CONV_7D], CONV_7D)
    changes = read_changes(loaded[CHANGES])
    targets = targets_by_campaign(campaign_rows)

    missing_cost = sorted(set(c["id"] for c in
                              [x.get("campaign") or {} for x in campaign_rows])
                          - set(cost))
    if missing_cost:
        trail.append(
            f"{COST_7D}: {len(missing_cost)} enabled campaign(s) absent from the "
            f"cost pull (e.g. {missing_cost[0]}). Treated as zero cost and named "
            "here rather than silently dropped."
        )
    orphan_conv = sorted(set(conv) - set(c["id"] for c in
                                         [x.get("campaign") or {} for x in campaign_rows]))
    if orphan_conv:
        trail.append(
            f"{CONV_7D}: {len(orphan_conv)} campaign(s) have conversions but are "
            f"not in the enabled-campaign pull (e.g. {orphan_conv[0]}). Their "
            "conversions are excluded from every cluster figure."
        )

    fs = FactSet(
        meta={
            "skill": "budget-optimize",
            "customer_id": account_config.customer_id(),
            "currency": "CAD",
            "raw_dir": str(raw_dir),
            "generator": "tools/build_budget_optimize_facts.py",
            "manifest_present": manifest.present,
            "row_key": (
                "campaign_budget.id for every budget figure, attributed to "
                "campaigns afterwards. NEVER campaign.id (VF-19)"
            ),
            "explicitly_shared_flag": (
                "unusable in this account: False on every campaign that has a "
                "budget, while 6 budget ids carry 17 campaigns. Sharing is "
                "detected by counting distinct campaign_budget.id values"
            ),
            "two_query_rule": (
                "cost and segments.conversion_action_name cannot share a query "
                "(VF-3). Joined on campaign.id, then aggregated to the budget"
            ),
            "budget_figures": (
                "read from budget-plan.md, never derived from a share of the "
                "all-channel total (campaign-guardrails rule 8)"
            ),
            "windows": windows,
            "inputs": {
                name: {
                    "rows": len(loaded[name]),
                    "field_mask": field_mask(raw_dir, name),
                    "declared_temporality": manifest.declared_temporality(name),
                    "declared_filter": manifest.declared_filter(name),
                }
                for name in present
            },
            "declared_filter_audit": trail,
        }
    )

    plan = spend_month_plan(loaded[SPEND_MTD])  # before the first fact
    add_structure(fs, campaign_rows, clusters, windows["current_state"], plan)
    cluster_trail, active, idle = add_cluster_facts(
        fs, clusters, cost, prior_cost, conv, changes, targets, windows
    )
    trail += cluster_trail
    add_constraint_summary(fs, active, cost, conv, changes, windows)
    add_idle(fs, idle, clusters, changes, windows, plan)
    trail += add_pacing(fs, loaded[SPEND_MTD], windows, plan)
    trail += add_brand(fs, clusters, cost, prior_cost, windows)
    add_subtotals(fs, clusters, cost, conv, windows)
    trail += add_targets(fs, campaign_rows, targets, changes, windows)
    add_learning_summary(fs, loaded[CHANGES], changes, windows, clusters)
    add_elasticity(fs)
    return fs


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("raw_dir", help="google-ads/outputs/raw/YYYY-MM-DD")
    parser.add_argument(
        "--out",
        default=None,
        help="output path (default: <raw_dir>/budget-optimize-facts.json)",
    )
    parser.add_argument(
        "--print", action="store_true", help="print every fact to stdout"
    )
    args = parser.parse_args(argv)

    raw_dir = Path(args.raw_dir)
    if not raw_dir.is_dir():
        print(f"not a directory: {raw_dir}", file=sys.stderr)
        return 2
    try:
        fs = build(raw_dir)
    except (BuildError, S.ManifestError, gaql.FilterMismatch) as exc:
        print(f"REFUSED to write a facts file.\n{exc}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else raw_dir / "budget-optimize-facts.json"
    fs.write(out)
    nulls = [f.id for f in fs if f.is_null]
    print(f"wrote {len(fs)} facts to {out}")
    print(f"{len(nulls)} deliberately null (unavailable) facts")
    for id in nulls:
        print(f"  - {id}")
    print(f"{len(fs.unsafe_comparisons)} declared unsafe comparison(s)")
    if args.print:
        for fact in fs:
            print(fact.describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
