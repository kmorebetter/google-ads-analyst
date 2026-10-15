"""Per-skill pull specifications - the single source for which pulls exist.

Why this module exists. On 2026-08-20 `mine-search-terms` shipped without its
mandatory 90-day clearance because the skill prose and the builder had drifted
apart about which pulls exist. `test_skill_builder_contract.py` was written to
catch that class by regexing SKILL.md against each builder's module constants -
a contract enforced in the wrong place, patched with waiver lists. This module
is the fix at the root: **one declarative spec per pull, per skill**, that the
builders import their filenames and filter labels from, the contract test
checks both the builders and the SKILL.md prose against, and
`tools/declare_pull.py` checks a hand- or tool-written `_manifest.json` entry
against. Adding or renaming a pull is one edit, here.

Since 2026-08-27 this module also carries the pack's **fact mandates**
(`MANDATED_FACTS`, below): the headline figures a skill's prose promises its
builder emits, checked in both directions by the contract test. The pull spec
answers "which files exist"; a mandate answers "which facts must exist" - the
bucket-ROAS defect proved the first cannot cover the second.

Two things deliberately NOT here:

- The `--existing-negatives` inputs to `mine-search-terms` have no fixed
  filenames (they are passed as paths) and so cannot be spec'd by name.
- The shared preflight pulls (`preflight-conventions-drift.json`,
  `preflight-conversion-values.json`) belong to `verification-protocol`, not to
  any one skill, and are listed in `SHARED_PREFLIGHT` for the tools that need
  to recognise them.

Import shape: this module imports nothing from the rest of the pack at module
level, so `scopes.py` (and every builder) can import filenames from it without
a cycle. `specs()` lazily imports `scopes` for the one filter literal that
lives there (`VF5_FULL_FILTER`).

Python 3 standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# --------------------------------------------------------------------------
# Filenames - the canonical spellings, one place
# --------------------------------------------------------------------------

# Shared across weekly-review, budget-optimize, and (partly) the two audit
# skills. One filename = one pull = same-day reuse across skills.
CAMPAIGNS_CURRENT = "campaigns-current.json"
COST_7D = "campaign-cost-is-7d.json"
COST_PRIOR7D = "campaign-cost-is-prior7d.json"
CONV_7D = "campaign-conversions-7d.json"
CONV_PRIOR7D = "campaign-conversions-prior7d.json"
SPEND_MTD = "account-spend-mtd.json"
CHANGE_EVENTS_14D = "change-events-14d.json"

# mine-search-terms
ST_COST_30D = "search-terms-cost-30d.json"
ST_CONV_30D = "search-terms-conversions-30d.json"
DONOTNEGATE_90D = "donotnegate-90d-8action.json"

# audit-extensions
CAMPAIGN_ASSETS_CURRENT = "campaign-assets-current.json"
CUSTOMER_ASSETS_CURRENT = "customer-assets-current.json"
FIELD_TYPE_VIEW_30D = "asset-field-type-30d.json"

# audit-ad-copy
ADCOPY_INVENTORY = "audit-ad-copy-rsa-inventory.json"
ADCOPY_METRICS_30D = "audit-ad-copy-rsa-metrics-30d.json"
ADCOPY_CONVERSIONS_30D = "audit-ad-copy-rsa-conversions-30d.json"
ADCOPY_ASSET_SERVES_30D = "audit-ad-copy-asset-serves-30d.json"
ADCOPY_CHANGE_EVENTS_14D = "audit-ad-copy-change-events-14d-detail.json"
ADCOPY_KEYWORD_QUALITY_CURRENT = "audit-ad-copy-keyword-quality-current.json"
ADCOPY_KEYWORD_COST_30D = "audit-ad-copy-keyword-cost-30d.json"

#: Declared in `verification-protocol`, inherited by every skill.
SHARED_PREFLIGHT = frozenset(
    {"preflight-conventions-drift.json", "preflight-conversion-values.json"}
)

# --------------------------------------------------------------------------
# Filter labels the builders share with their fact scopes
# --------------------------------------------------------------------------
# These are the normalised labels the builders compare clause-for-clause
# against each pull's declared GAQL (VF-19a) and write into fact `scope`
# strings. They lived as per-builder constants; the builders now import them
# from here so a label cannot drift between two skills reading the same pull.

ENABLED_CAMPAIGNS = "campaign.status=ENABLED"
ENABLED_8 = "campaign.status=ENABLED AND segments.conversion_action_name IN (the 8)"
CHANGE_EVENTS_LABEL = "change_event.change_date_time DURING LAST_14_DAYS"
ST_COST_LABEL = "metrics.cost_micros > 0"
ST_CONV_LABEL = "segments.conversion_action_name IN (the 8)"
CAMPAIGN_ASSET_LABEL = "campaign_asset.status=ENABLED"
CUSTOMER_ASSET_LABEL = "customer_asset.status=ENABLED"


# --------------------------------------------------------------------------
# The spec
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PullSpec:
    """One pull a skill's step 1 makes and its builder reads.

    `temporality` is the *kind* - "current" or "window" - because the spec
    cannot know concrete dates. `window_form` narrows a windowed pull: a bare
    GAQL token (`LAST_7_DAYS`) when the query uses `DURING`, or a human
    description starting "BETWEEN" when the query computes a range. Tools
    enforce the kind strictly and the token where one is pinned.

    `filter` is the normalised non-temporal filter label ("" = the pull
    applies none). `required_fields` are fieldMask spellings a builder is
    known to depend on - the ones whose omission broke a real run.
    """

    filename: str
    resource: str
    temporality: str  # "current" | "window"
    window_form: str = ""
    filter: str = ""
    required_fields: tuple = ()
    read_by_builder: bool = True
    optional: bool = False
    note: str = ""

    @property
    def is_current(self) -> bool:
        return self.temporality == "current"

    @property
    def is_windowed(self) -> bool:
        return self.temporality == "window"


_CACHE: "dict | None" = None


def specs() -> "dict[str, tuple[PullSpec, ...]]":
    """The per-skill pull specs. Lazy so scopes.py can import filenames above."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    import scopes as S  # lazy: scopes imports this module's filenames

    campaigns = PullSpec(
        filename=CAMPAIGNS_CURRENT,
        resource="campaign",
        temporality="current",
        filter=ENABLED_CAMPAIGNS,
        required_fields=(
            "campaign.id",
            "campaign.name",
            "campaign.biddingStrategyType",
        ),
        note=(
            "Undated structure - the only valid source for a current-state "
            "claim. Targets are read from the bidding-strategy sub-message; "
            "the legacy target field is selected as a control."
        ),
    )
    change_events = PullSpec(
        filename=CHANGE_EVENTS_14D,
        resource="change_event",
        temporality="window",
        window_form="LAST_14_DAYS",
        filter=CHANGE_EVENTS_LABEL,
        note=(
            "14 days is this resource's maximum window, and the query 400s "
            "without a LIMIT. No change_resource_type filter, ever - a "
            "CAMPAIGN-only filter missed CAMPAIGN_BUDGET events for two weeks."
        ),
    )
    cost_pair = (
        PullSpec(
            filename=COST_7D,
            resource="campaign",
            temporality="window",
            window_form="LAST_7_DAYS",
            filter=ENABLED_CAMPAIGNS,
            required_fields=(
                "campaign.id",
                "metrics.costMicros",
                "metrics.searchBudgetLostImpressionShare",
                "metrics.searchRankLostImpressionShare",
            ),
        ),
        PullSpec(
            filename=COST_PRIOR7D,
            resource="campaign",
            temporality="window",
            window_form="BETWEEN (the 7 days before this week's window)",
            filter=ENABLED_CAMPAIGNS,
            required_fields=(
                "campaign.id",
                "metrics.costMicros",
                "metrics.searchBudgetLostImpressionShare",
                "metrics.searchRankLostImpressionShare",
            ),
            note=(
                "Identical field list to the 7-day pull, or the omitted "
                "field's WoW row silently disappears (2026-08-20)."
            ),
        ),
    )
    conv_pair = (
        PullSpec(
            filename=CONV_7D,
            resource="campaign",
            temporality="window",
            window_form="LAST_7_DAYS",
            filter=ENABLED_8,
            required_fields=("campaign.id", "metrics.conversions"),
            note="Two-query rule (VF-3): joined to the cost pull on campaign.id.",
        ),
        PullSpec(
            filename=CONV_PRIOR7D,
            resource="campaign",
            temporality="window",
            window_form="BETWEEN (the 7 days before this week's window)",
            filter=ENABLED_8,
            required_fields=("campaign.id", "metrics.conversions"),
        ),
    )
    spend_mtd = PullSpec(
        filename=SPEND_MTD,
        resource="customer",
        temporality="window",
        window_form="BETWEEN (first of month .. yesterday)",
        filter="",
        required_fields=("segments.date", "metrics.costMicros"),
        note=(
            "No campaign or status filter, on purpose: pacing is against "
            "everything the account spent. Never differenced against an "
            "enabled-only sum."
        ),
    )

    weekly = campaigns, *cost_pair, *conv_pair, spend_mtd, change_events
    budget = (
        campaigns,
        cost_pair[0],
        replace(cost_pair[1], optional=True),
        conv_pair[0],
        replace(conv_pair[1], optional=True),
        spend_mtd,
        change_events,
    )

    mine = (
        PullSpec(
            filename=ST_COST_30D,
            resource="search_term_view",
            temporality="window",
            window_form="LAST_30_DAYS",
            filter=ST_COST_LABEL,
            required_fields=(
                "campaign.id",
                "adGroup.id",
                "adGroup.name",
                "searchTermView.searchTerm",
            ),
            note=(
                "adGroup.name is mandatory: Editor imports negatives by ad "
                "group NAME. No campaign filter - overlap cannot be seen from "
                "one campaign's slice. Truncation-checked (10,000-row cap)."
            ),
        ),
        PullSpec(
            filename=ST_CONV_30D,
            resource="search_term_view",
            temporality="window",
            window_form="LAST_30_DAYS",
            filter=ST_CONV_LABEL,
            note="Aggregate per term key BEFORE joining (segmented by action).",
        ),
        replace(campaigns, required_fields=()),
        change_events,
        PullSpec(
            filename=DONOTNEGATE_90D,
            resource="search_term_view",
            temporality="window",
            window_form="BETWEEN (90 days ago .. yesterday)",
            filter=ST_CONV_LABEL,
            optional=True,
            note=(
                "REQUIRED in effect: without it the builder emits zero CSV "
                "rows, by rule. Marked optional only because the build "
                "completes (with an empty CSV) to report that refusal. "
                "LAST_90_DAYS is not valid GAQL - BETWEEN form only."
            ),
        ),
    )

    extensions = (
        PullSpec(
            filename=CAMPAIGN_ASSETS_CURRENT,
            resource="campaign_asset",
            temporality="current",
            filter=CAMPAIGN_ASSET_LABEL,
            note=(
                "The status filters the ATTACHMENT, not the campaign - every "
                "count is joined client-side to campaigns-current.json."
            ),
        ),
        PullSpec(
            filename=CUSTOMER_ASSETS_CURRENT,
            resource="customer_asset",
            temporality="current",
            filter=CUSTOMER_ASSET_LABEL,
            note="Zero rows is a real answer: nothing is inherited.",
        ),
        PullSpec(
            filename=FIELD_TYPE_VIEW_30D,
            resource="asset_field_type_view",
            temporality="window",
            window_form="LAST_30_DAYS",
            filter="",
            note="Per field type, not per asset; impressions fan out (VF-4).",
        ),
        replace(campaigns, required_fields=()),
        change_events,
        replace(
            cost_pair[0],
            optional=True,
            note=(
                "Optional ranking input: an absent_everywhere table is "
                "ranked by 7-day cost when present; its absence is an "
                "unavailable-fact, not a refusal."
            ),
        ),
    )

    adcopy = (
        PullSpec(
            filename=ADCOPY_INVENTORY,
            resource="ad_group_ad",
            temporality="current",
            filter=S.VF5_FULL_FILTER,
            required_fields=("adGroupAd.resourceName",),
            note=(
                "Undated, no metrics - the only valid source for a "
                "current-state claim, and one side of the VF-4b intersect."
            ),
        ),
        PullSpec(
            filename=ADCOPY_METRICS_30D,
            resource="ad_group_ad",
            temporality="window",
            window_form="LAST_30_DAYS",
            filter=S.VF5_FULL_FILTER,
            required_fields=("adGroupAd.resourceName", "metrics.costMicros"),
            note="Never selected together with the repeated asset arrays.",
        ),
        PullSpec(
            filename=ADCOPY_CONVERSIONS_30D,
            resource="ad_group_ad",
            temporality="window",
            window_form="LAST_30_DAYS",
            filter=S.VF5_FULL_FILTER + " AND segments.conversion_action_name IN (the 8)",
            required_fields=("adGroup.id",),
            note="Two-query rule; ad_group.id is mandatory, name is not a substitute.",
        ),
        PullSpec(
            filename=ADCOPY_ASSET_SERVES_30D,
            resource="ad_group_ad_asset_view",
            temporality="window",
            window_form="LAST_30_DAYS",
            filter="ad_group_ad_asset_view.field_type IN (HEADLINE,DESCRIPTION)",
            optional=True,
            note=(
                "The resource rejected the canonical status filters on "
                "2026-08-19, which is why the VF-4b intersect against the "
                "inventory is not optional. Absent => per-headline facts are "
                "null with the reason."
            ),
        ),
        PullSpec(
            filename=ADCOPY_CHANGE_EVENTS_14D,
            resource="change_event",
            temporality="window",
            window_form="LAST_14_DAYS",
            filter=CHANGE_EVENTS_LABEL,
            read_by_builder=False,
            note=(
                "Documented in the skill (step 2), read by the operator, not "
                "yet by the builder - a tracked gap (MAINTENANCE 2026-08-19)."
            ),
        ),
        PullSpec(
            filename=ADCOPY_KEYWORD_QUALITY_CURRENT,
            resource="keyword_view",
            temporality="current",
            filter=(
                "campaign.status=ENABLED AND ad_group.status=ENABLED AND "
                "ad_group_criterion.status=ENABLED AND "
                "campaign.advertising_channel_type=SEARCH"
            ),
            read_by_builder=False,
            note="Documented (step 3a), not yet read by the builder - tracked gap.",
        ),
        PullSpec(
            filename=ADCOPY_KEYWORD_COST_30D,
            resource="keyword_view",
            temporality="window",
            window_form="LAST_30_DAYS",
            filter=(
                "campaign.status=ENABLED AND ad_group.status=ENABLED AND "
                "ad_group_criterion.status=ENABLED AND "
                "campaign.advertising_channel_type=SEARCH"
            ),
            read_by_builder=False,
            note="Documented (step 3b), not yet read by the builder - tracked gap.",
        ),
    )

    _CACHE = {
        "weekly-review": tuple(weekly),
        "budget-optimize": tuple(budget),
        "mine-search-terms": tuple(mine),
        "audit-extensions": tuple(extensions),
        "audit-ad-copy": tuple(adcopy),
    }
    return _CACHE


def spec_for(skill: str, filename: str) -> "PullSpec | None":
    for spec in specs().get(skill, ()):
        if spec.filename == filename:
            return spec
    return None


# --------------------------------------------------------------------------
# Fact mandates - the figures a skill's prose PROMISES its builder emits
# --------------------------------------------------------------------------
# Why. On 2026-08-27 weekly-review's step 5 mandated "non-brand assumed-value
# CPA and ROAS - the number the report leads with after pacing", and the
# builder emitted no non-brand ROAS at all. With no admissible subtotal to
# cite, the report fell back on the blended figure guardrail 2 exists to stop
# it leading with, and named the wrong cause for a real ROAS drop. The pull
# contract above could not see it: skill and builder agreed on every pull;
# they disagreed about a FACT. A mandate makes that promise machine-checkable
# in both directions: the id must exist in a build, and the promising sentence
# must still exist in the skill.
#
# Scope, honestly: mandates cover HEADLINE obligations - the figures a
# skill's prose names as the report's lead or as a required family - not
# every figure a report may cite. Adding one is a two-line spec edit here
# (ids + the sentence's anchor); the contract test picks it up unchanged.


@dataclass(frozen=True)
class FactMandate:
    """One prose obligation and the exact fact ids that keep it.

    `fact_ids` are exact ids (window-suffixed families are enumerated, so a
    mandate cannot silently cover less than it reads). `prose_anchor` is a
    case-insensitive, DOTALL regex that must match the skill's SKILL.md -
    delete the promising sentence and the mandate fails just as loudly as
    deleting the fact. `note` names the report obligation the mandate backs;
    it is the string the failure message prints.
    """

    fact_ids: tuple
    prose_anchor: str
    note: str


#: skill -> its mandates. Only skills whose stakes are proven carry any yet.
MANDATED_FACTS: "dict[str, tuple[FactMandate, ...]]" = {
    "weekly-review": (
        FactMandate(
            fact_ids=(
                "spend.account.mtd.cad",
                "pacing.spend_vs_prorated_budget.pct",
                "pacing.overspend_to_date.cad",
                "pacing.projected_full_month.cad",
                "pacing.daily_cut_needed_to_land_on_budget.pct",
            ),
            prose_anchor=(
                r"pacing percentage, overspend-to-date, the projected"
                r"\s+full month.{0,80}?daily cut needed to land on\s+budget"
            ),
            note=(
                "step 4: real pacing leads the report - spend-to-date, the "
                "pacing percentage, overspend, the projection and the cut"
            ),
        ),
        FactMandate(
            fact_ids=(
                "cpa.nonbrand.assumed_value.7d",
                "cpa.nonbrand.assumed_value.prior7d",
                "roas.nonbrand.assumed_value.7d",
                "roas.nonbrand.assumed_value.prior7d",
            ),
            prose_anchor=(
                r"on assumed-value CPA/ROAS.{0,40}?the\s+number the report "
                r"leads with after pacing"
            ),
            note=(
                "step 5: non-brand assumed-value CPA and ROAS is the number "
                "the report leads with after pacing - the figure whose "
                "absence made the 2026-08-24 report name the wrong cause"
            ),
        ),
        FactMandate(
            fact_ids=(
                "spend.account.7d",
                "spend.brand.7d",
                "spend.nonbrand.7d",
                "spend.tof.7d",
                "conversion_value.assumed.brand.7d",
                "conversion_value.assumed.nonbrand.7d",
                "conversion_value.assumed.tof.7d",
            ),
            prose_anchor=(
                r"three subtotals are asserted to sum to the\s+account total"
            ),
            note=(
                "step 5: the three bucket subtotals exist separately and are "
                "asserted to sum to the account total"
            ),
        ),
        FactMandate(
            fact_ids=(
                "cpa.brand.assumed_value.7d",
                "cpa.brand.assumed_value.prior7d",
            ),
            prose_anchor=r"Brand Core[*]{0,2} on its own CPA",
            note="step 5: Brand Core is judged on its own CPA, never blended",
        ),
    ),
    "budget-optimize": (
        FactMandate(
            fact_ids=(
                "pacing.mtd.spend.cad",
                "pacing.budget_to_date.cad",
                "pacing.vs_budget_to_date.pct",
            ),
            prose_anchor=r"the answer[*]{0,2}\s*\(pacing, then what survives",
            note=(
                "step 9: the answer the report opens with - month-to-date "
                "spend against budget-to-date (null-with-reason months "
                "still emit the ids)"
            ),
        ),
        FactMandate(
            fact_ids=(
                "pacing.runrate_forecast.cad",
                "pacing.remaining.budget.cad",
                "pacing.remaining.allowed_daily.cad",
                "pacing.required_daily_cut.pct",
            ),
            prose_anchor=(
                r"run-rate forecast, budget left, allowed daily"
                r"\s+spend, the cut that lands on budget"
            ),
            note=(
                "step 9: the pacing detail - run-rate forecast, budget "
                "left, allowed daily spend, and the cut that lands on budget"
            ),
        ),
        FactMandate(
            fact_ids=(
                "idle.budget.count",
                "idle.campaign.count",
                "idle.daily_allocation.cad",
                "idle.share_of_daily_allocation.pct",
                "idle.campaigns_with_budget_or_target_change.count",
            ),
            prose_anchor=(
                r"idle allocation[*]{0,2}: clusters with zero cost"
                r"\s+in both windows"
            ),
            note=(
                "step 7: the idle-allocation family - pools carrying budget "
                "that buys nothing, named and sized"
            ),
        ),
    ),
}


def mandates_for(skill: str) -> "tuple[FactMandate, ...]":
    return MANDATED_FACTS.get(skill, ())


def all_filenames() -> "dict[str, tuple[PullSpec, ...]]":
    """filename -> every spec that names it, across skills."""
    out: dict = {}
    for skill_specs in specs().values():
        for spec in skill_specs:
            out.setdefault(spec.filename, [])
            out[spec.filename].append(spec)
    return {name: tuple(entries) for name, entries in out.items()}


def read_filenames(skill: str) -> frozenset:
    """The filenames a skill's builder is expected to read."""
    return frozenset(
        s.filename for s in specs()[skill] if s.read_by_builder
    )


def documented_filenames(skill: str) -> frozenset:
    """Every filename the skill's SKILL.md must name - read or not."""
    return frozenset(s.filename for s in specs()[skill])


__all__ = [
    "PullSpec",
    "specs",
    "spec_for",
    "all_filenames",
    "read_filenames",
    "documented_filenames",
    "FactMandate",
    "MANDATED_FACTS",
    "mandates_for",
    "SHARED_PREFLIGHT",
    # filenames
    "CAMPAIGNS_CURRENT",
    "COST_7D",
    "COST_PRIOR7D",
    "CONV_7D",
    "CONV_PRIOR7D",
    "SPEND_MTD",
    "CHANGE_EVENTS_14D",
    "ST_COST_30D",
    "ST_CONV_30D",
    "DONOTNEGATE_90D",
    "CAMPAIGN_ASSETS_CURRENT",
    "CUSTOMER_ASSETS_CURRENT",
    "FIELD_TYPE_VIEW_30D",
    "ADCOPY_INVENTORY",
    "ADCOPY_METRICS_30D",
    "ADCOPY_CONVERSIONS_30D",
    "ADCOPY_ASSET_SERVES_30D",
    "ADCOPY_CHANGE_EVENTS_14D",
    "ADCOPY_KEYWORD_QUALITY_CURRENT",
    "ADCOPY_KEYWORD_COST_30D",
    # filter labels
    "ENABLED_CAMPAIGNS",
    "ENABLED_8",
    "CHANGE_EVENTS_LABEL",
    "ST_COST_LABEL",
    "ST_CONV_LABEL",
    "CAMPAIGN_ASSET_LABEL",
    "CUSTOMER_ASSET_LABEL",
]
