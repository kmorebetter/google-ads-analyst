#!/usr/bin/env python3
"""Build the `audit-extensions` facts file from saved raw JSON (VERIFICATION-RULES VF-15).

Usage:
    python3 tools/build_audit_extensions_facts.py google-ads/outputs/raw/2026-08-20
    python3 tools/build_audit_extensions_facts.py <raw_dir> --out /tmp/facts.json

Reads only the raw JSON already on disk. Makes no API calls. Writes
`<raw_dir>/audit-extensions-facts.json` unless `--out` says otherwise.

Four things about this script matter more than the arithmetic.

1. **`campaign_asset` returns one row per campaign-asset attachment, and this
   account shares assets across campaigns.** VF-19 in its extension form: the
   row key is `(campaign.id, asset.id, field_type)`, and the *count of rows* is
   not the count of assets. Measured 2026-08-20: 1,183 rows carry **98**
   distinct asset ids, and one sitelink asset is attached to **61** campaigns.
   Both figures are real and answer different questions, so this builder emits
   both under different `unit`s - `campaign_asset_link_rows` for coverage,
   `distinct_assets` for creative variety - and never one alone.

2. **`campaign_asset.status='ENABLED'` does NOT filter campaign status.** Same
   shape as the VF-5 bug. 43 of the 77 campaigns in the 2026-08-20 pull are not
   enabled, carrying 542 of its 1,183 rows. Every count here is intersected
   client-side against `campaigns-current.json` and every scope label says so.

3. **Account-level assets serve on a campaign that has none of its own.** This
   is the rule that decides whether a blank cell in the coverage grid is a gap
   or nothing at all, and it is `[verified]` from this account's own data, not
   assumed from documentation: 13 `(campaign, field_type)` pairs draw serve
   impressions in the 30-day pull while holding no campaign-level asset of that
   type, and the type exists at account level. The converse - that a
   campaign-level asset *replaces* rather than adds to the account-level set -
   is Google's documented behaviour and is **not** derivable from these pulls.
   It is labelled an assumption wherever it is used.

4. **`asset_field_type_view` attributes per field type, never per asset**, and
   its impressions fan out the same way `ad_group_ad_asset_view`'s do: an
   impression where a sitelink and a callout both showed is credited to both.
   Summing across field types gives 703,899 impressions against an account that
   served nothing like that many. So the per-field-type figures use
   `facts.NON_ADDITIVE_UNITS` members, and the fan-out total is emitted once,
   labelled as the fiction it is, so a report can name it rather than compute
   it. Per-field-type **cost** is deliberately not emitted at all: there is no
   non-additive currency unit for it, and it is the most misleading figure in
   the pull (VF-15a rule 6, fewer better facts). A single named campaign's own
   7-day total is a different thing - fully additive, not per-field-type - and
   IS emitted, but only for the campaign an absent_everywhere recommendation
   names as the one actually spending (see `with_spend.spend_cad.<id>` below).

Scope literals live in this module rather than `scopes.py`, which is read-only
for this skill. See the report note: they belong in `scopes.py` alongside
`VF5_FULL_FILTER`.

Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_config  # noqa: E402
import gaql  # noqa: E402
import pulls  # noqa: E402
import scopes as S  # noqa: E402
from facts import FactSet, micros_to_currency, money  # noqa: E402

BuildError = pulls.BuildError
load_results = pulls.load_results
field_mask = pulls.field_mask
validate_clauses = pulls.validate_clauses

# --------------------------------------------------------------------------
# Raw file names
# --------------------------------------------------------------------------

# Canonical spellings live in `pull_specs.py` - the single source for which
# pulls exist; these aliases keep every existing reference working.
import pull_specs

CAMPAIGN_ASSETS = pull_specs.CAMPAIGN_ASSETS_CURRENT
CUSTOMER_ASSETS = pull_specs.CUSTOMER_ASSETS_CURRENT
FIELD_TYPE_VIEW = pull_specs.FIELD_TYPE_VIEW_30D
CAMPAIGNS = pull_specs.CAMPAIGNS_CURRENT
CHANGE_EVENTS = pull_specs.CHANGE_EVENTS_14D

#: 7-day cost, used to RANK an absent_everywhere finding rather than pick by eye.
#: Optional: absent means the ranking facts are null, never guessed.
#:
#: Why it exists: `audit-extensions/SKILL.md`'s selection rule says an
#: absent_everywhere finding ships a table of every affected campaign and that a
#: run needing to rank inside it should use this pull and emit the rule as a
#: fact. The builder never read it, so on 2026-08-20 the report recommended a
#: 24-campaign price-asset rollout - and 23 of those 24 had spent nothing in two
#: weeks. The coverage number was right and the recommendation was wrong.
COST_7D = pull_specs.COST_7D

#: Every pull this builder reads. The three status-filtered ones get their
#: declared filter checked clause-for-clause (VF-19a); `asset_field_type_view`
#: declares only a date predicate, which is temporality and lives in `window`,
#: so its non-temporality clause set is empty and there is nothing to compare.
#: Recorded as declared-not-verified rather than forced through the comparison.
STATUS_FILTERED = (CAMPAIGN_ASSETS, CUSTOMER_ASSETS, CAMPAIGNS)

# --------------------------------------------------------------------------
# Scope literals
# --------------------------------------------------------------------------

PULL_CAMPAIGN_ASSET = pull_specs.CAMPAIGN_ASSET_LABEL
PULL_CUSTOMER_ASSET = pull_specs.CUSTOMER_ASSET_LABEL
PULL_CAMPAIGNS = pull_specs.ENABLED_CAMPAIGNS
PULL_FIELD_TYPE_VIEW = "segments.date DURING LAST_30_DAYS"

#: The client-side narrowing every campaign-level count needs. Point 2 above.
_CAMPAIGN_STATUS_JOIN = (
    " , then INNER JOINED client-side to campaigns-current.json on campaign.id "
    "(campaign.status=ENABLED) because campaign_asset applies NO campaign "
    "status filter of its own"
)

ENABLED_SCOPE = PULL_CAMPAIGN_ASSET + _CAMPAIGN_STATUS_JOIN
SEARCH_SCOPE = ENABLED_SCOPE + " AND campaign.advertising_channel_type=SEARCH"

#: Said inside every link-row scope. Without it, 410 sitelink rows reads as 410
#: sitelinks (point 1).
LINK_ROW_CAVEAT = (
    " . COUNT OF ATTACHMENTS, keyed on (campaign.id, asset.id, field_type) per "
    "VF-19: one asset attached to N campaigns contributes N. NOT a count of "
    "assets"
)

DISTINCT_CAVEAT = (
    " . COUNT OF DISTINCT asset.id values, deduplicated across campaigns per "
    "VF-19. A creative-variety measure, NOT a coverage measure"
)

#: The union of campaign-level and account-level assets of a type. A second row
#: key, because an asset can appear in both pulls: 24 of the 28 account-level
#: assets on 2026-08-20 are also attached at campaign level.
#: Named `ext.union.*` rather than `ext.account_wide.*` on purpose. One word
#: apart from `ext.account_level.*` and meaning the opposite of it is how a
#: correct figure gets a wrong attribution: `ext.account_level.price...` is 0,
#: which is what says price is NOT inheritable, while the union is 3. A draft
#: quoted the union as "the account level holds 3" and BOTH checkers passed it,
#: because the arithmetic was right and only the attribution was wrong - VF-19's
#: stated hazard exactly. Only `ext.account_level.*` answers "is this
#: inheritable?".
UNION_SCOPE_NOTE = (
    "Only `ext.account_level.*` answers \"is this field type inheritable on a "
    "campaign that has none of its own?\". `ext.union.*` counts both levels "
    "together and answers \"how much creative variety exists?\"."
)

UNION_SCOPE = (
    "UNION of distinct asset.id from campaign-assets-current.json ("
    + ENABLED_SCOPE
    + ") and customer-assets-current.json ("
    + PULL_CUSTOMER_ASSET
    + "), deduplicated across the two pulls per VF-19. " + UNION_SCOPE_NOTE
)

SERVE_SCOPE = (
    PULL_FIELD_TYPE_VIEW
    + " , then INNER JOINED client-side to campaigns-current.json on campaign.id "
    "(campaign.status=ENABLED). Keyed on (campaign.id, field_type)"
)

#: Said inside every serve scope. Point 4.
FAN_OUT_CAVEAT = (
    " . Google credits EVERY asset field type present in a serve with the whole "
    "ad's metrics, so these figures overlap each other. Never sum across field "
    "types and never treat one field type's impressions as a share of the "
    "account's"
)

#: Coverage of one field type on one campaign resolves to exactly one of three
#: states. A two-state grid reads every blank as a gap, and most blanks are not.
STATE_CAMPAIGN = "campaign_level"
STATE_INHERITED = "inherited_from_account"
STATE_ABSENT = "absent_everywhere"

#: The three field types with a documented Google serve minimum for a search
#: campaign, and the only ones this builder judges coverage on.
JUDGED_FIELD_TYPES = ("SITELINK", "CALLOUT", "STRUCTURED_SNIPPET")

#: Judged on a weaker footing: no Google minimum, so the gap is an opportunity
#: rather than a failed requirement. Kept separate from JUDGED_FIELD_TYPES so a
#: report cannot present the two on the same terms.
OPPORTUNITY_FIELD_TYPES = ("PRICE",)

#: Reported, never judged. PMax and asset-automation types with no search-campaign
#: minimum to judge against.
REPORT_ONLY_FIELD_TYPES = (
    "AD_IMAGE",
    "BUSINESS_NAME",
    "BUSINESS_LOGO",
    "LOGO",
    "LANDSCAPE_LOGO",
)

#: Guardrail 1. Extension coverage on these is reported, never judged: their
#: extension model is ad-group / asset-group level, which no pull here reads.
TOF_CHANNELS = account_config.tof_channels_ordered()


def _id(field_type: str) -> str:
    return field_type.lower()


# --------------------------------------------------------------------------
# Reading the pulls
# --------------------------------------------------------------------------


def load_account_level(raw_dir: Path) -> list[dict]:
    """`customer_asset` rows, tolerating an empty pull.

    `pulls.load_results` treats zero rows as a failed pull, which is right for
    every other file here. It is wrong for this one: an account with no
    account-level extensions is a real and important state - it means nothing is
    inherited, so every blank grid cell is a genuine gap - and refusing to build
    would turn the most severe finding this skill can make into a crash.
    """
    path = raw_dir / CUSTOMER_ASSETS
    if not path.exists():
        raise BuildError(
            f"{CUSTOMER_ASSETS}: missing from {raw_dir}. Without it, "
            "campaign-level and account-level cannot be told apart and every "
            "coverage finding would be wrong."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("success", True):
        raise BuildError(f"{CUSTOMER_ASSETS}: the saved pull records success=false")
    return list(((payload.get("data") or {}).get("results")) or [])


def campaign_index(rows: list[dict]) -> dict:
    """`{campaign.id: campaign}` from the undated enabled-campaign pull."""
    out = {}
    for row in rows:
        campaign = row.get("campaign") or {}
        cid = campaign.get("id")
        if not cid:
            raise BuildError(
                f"{CAMPAIGNS}: a row has no campaign.id, so no campaign-level "
                "count can be intersected against it."
            )
        out[str(cid)] = campaign
    return out


def link_rows(rows: list[dict]) -> list[tuple]:
    """`(campaign.id, asset.id, field_type)` per row - the VF-19 key."""
    out = []
    for row in rows:
        cid = (row.get("campaign") or {}).get("id")
        aid = (row.get("asset") or {}).get("id")
        ft = (row.get("campaignAsset") or {}).get("fieldType")
        if not (cid and aid and ft):
            raise BuildError(
                f"{CAMPAIGN_ASSETS}: a row is missing campaign.id, asset.id or "
                "campaignAsset.fieldType. Those three are the row key (VF-19); "
                "add them to the SELECT and re-pull."
            )
        out.append((str(cid), str(aid), ft))
    return out


def account_rows(rows: list[dict]) -> list[tuple]:
    """`(asset.id, field_type)` per account-level row."""
    out = []
    for row in rows:
        aid = (row.get("asset") or {}).get("id")
        ft = (row.get("customerAsset") or {}).get("fieldType")
        if not (aid and ft):
            raise BuildError(
                f"{CUSTOMER_ASSETS}: a row is missing asset.id or "
                "customerAsset.fieldType."
            )
        out.append((str(aid), ft))
    return out


def serve_rows(rows: list[dict]) -> list[tuple]:
    """`(campaign.id, field_type, impressions, clicks)` per serve row."""
    out = []
    for row in rows:
        cid = (row.get("campaign") or {}).get("id")
        ft = (row.get("assetFieldTypeView") or {}).get("fieldType")
        metrics = row.get("metrics") or {}
        if not (cid and ft):
            raise BuildError(
                f"{FIELD_TYPE_VIEW}: a row is missing campaign.id or "
                "assetFieldTypeView.fieldType."
            )
        out.append(
            (
                str(cid),
                ft,
                int(metrics.get("impressions") or 0),
                int(metrics.get("clicks") or 0),
            )
        )
    return out


def assert_unique_link_key(filename: str, keys: list[tuple]) -> str:
    """Row count against distinct-key count. Disagreement means segmented rows."""
    dupes = [k for k, n in collections.Counter(keys).items() if n > 1]
    if dupes:
        raise BuildError(
            f"{filename}: {len(dupes)} duplicate (campaign.id, asset.id, "
            f"field_type) keys (e.g. {dupes[0]}). The response is segmented; "
            "aggregate per key before counting, or every total double counts."
        )
    return (
        f"{filename}: {len(keys)} rows, {len(set(keys))} distinct "
        "(campaign.id, asset.id, field_type) keys - they agree, so no row is "
        "segmented (VF-19)"
    )


# --------------------------------------------------------------------------
# Coverage resolution
# --------------------------------------------------------------------------


def coverage_state(
    field_type: str, campaign_id: str, campaign_level: dict, account_level: set
) -> str:
    """Which of the three states one `(campaign, field_type)` cell is in.

    `campaign_level` maps campaign id to the set of field types attached to it.
    An account-level asset of that type serves where the campaign has none -
    verified from this account's own serve data, see the module docstring.
    """
    if field_type in campaign_level.get(campaign_id, set()):
        return STATE_CAMPAIGN
    if field_type in account_level:
        return STATE_INHERITED
    return STATE_ABSENT


# --------------------------------------------------------------------------
# Fact groups
# --------------------------------------------------------------------------


def add_scope_facts(
    fs: FactSet,
    keys: list[tuple],
    enabled: dict,
    window: str,
    campaigns_window: str,
) -> None:
    """What the pull returned, and what is left after the campaign-status join."""
    pull_campaigns = {k[0] for k in keys}
    kept = [k for k in keys if k[0] in enabled]
    fs.add(
        "ext.pull.campaign_asset.link_row.count",
        len(keys),
        "campaign_asset_link_rows",
        PULL_CAMPAIGN_ASSET + LINK_ROW_CAVEAT,
        window,
        CAMPAIGN_ASSETS,
        "n/a",
    )
    fs.add(
        "ext.pull.campaign_asset.campaign.count",
        len(pull_campaigns),
        "count",
        PULL_CAMPAIGN_ASSET,
        window,
        CAMPAIGN_ASSETS,
        "n/a",
    )
    fs.add(
        "ext.pull.campaign_asset.campaign.not_enabled.count",
        len(pull_campaigns - set(enabled)),
        "count",
        PULL_CAMPAIGN_ASSET
        + " , minus the campaigns present in campaigns-current.json "
        "(campaign.status=ENABLED). The defect: campaign_asset applies no "
        "campaign status filter",
        window,
        CAMPAIGN_ASSETS,
        "n/a",
    )
    fs.add(
        "ext.pull.campaign_asset.link_row.on_disabled_campaign.count",
        len(keys) - len(kept),
        "campaign_asset_link_rows",
        PULL_CAMPAIGN_ASSET
        + " AND campaign.status != ENABLED (client-side)"
        + LINK_ROW_CAVEAT,
        window,
        CAMPAIGN_ASSETS,
        "n/a",
    )
    fs.add(
        "ext.link_row.enabled.count",
        len(kept),
        "campaign_asset_link_rows",
        ENABLED_SCOPE + LINK_ROW_CAVEAT,
        window,
        CAMPAIGN_ASSETS,
        "n/a",
    )
    fs.add(
        "ext.distinct_asset.enabled.count",
        len({k[1] for k in kept}),
        "distinct_assets",
        ENABLED_SCOPE + DISTINCT_CAVEAT,
        window,
        CAMPAIGN_ASSETS,
        "n/a",
    )
    fs.add(
        "ext.enabled_campaign.count",
        len(enabled),
        "count",
        PULL_CAMPAIGNS,
        campaigns_window,
        CAMPAIGNS,
        "n/a",
    )
    fs.add(
        "ext.enabled_campaign.search.count",
        sum(
            1
            for c in enabled.values()
            if c.get("advertisingChannelType") == "SEARCH"
        ),
        "count",
        PULL_CAMPAIGNS + " AND campaign.advertising_channel_type=SEARCH",
        campaigns_window,
        CAMPAIGNS,
        "n/a",
    )


def add_sharing_facts(fs: FactSet, keys: list[tuple], enabled: dict, window: str) -> None:
    """The double-count hazard, stated as facts rather than left implicit."""
    kept = [k for k in keys if k[0] in enabled]
    per_asset = collections.defaultdict(set)
    for cid, aid, ft in kept:
        per_asset[(aid, ft)].add(cid)
    shared = [k for k, camps in per_asset.items() if len(camps) > 1]
    fs.add(
        "ext.shared_asset.multi_campaign.count",
        len(shared),
        "distinct_assets",
        ENABLED_SCOPE
        + " , grouped on (asset.id, field_type) and kept where the asset is "
        "attached to more than one campaign"
        + DISTINCT_CAVEAT,
        window,
        CAMPAIGN_ASSETS,
        "n/a",
    )
    fs.add(
        "ext.shared_asset.max_campaigns",
        max((len(c) for c in per_asset.values()), default=0),
        "count",
        ENABLED_SCOPE
        + " , the largest number of enabled campaigns any single "
        "(asset.id, field_type) is attached to",
        window,
        CAMPAIGN_ASSETS,
        "n/a",
    )


def add_field_type_facts(
    fs: FactSet,
    keys: list[tuple],
    enabled: dict,
    account: list[tuple],
    window: str,
    account_window: str,
) -> list[str]:
    """Per field type: link rows, distinct assets, account level, union."""
    kept = [k for k in keys if k[0] in enabled]
    rows_by_ft: dict = collections.Counter(k[2] for k in kept)
    assets_by_ft: dict = collections.defaultdict(set)
    for cid, aid, ft in kept:
        assets_by_ft[ft].add(aid)
    account_by_ft: dict = collections.defaultdict(set)
    for aid, ft in account:
        account_by_ft[ft].add(aid)

    trail = []
    # The judged and opportunity types are always emitted, at zero if need be.
    # `ext.account_level.price.distinct_asset.count` = 0 is the figure that says
    # price is not inheritable, and a report cannot cite a fact that only exists
    # when the account happens to own one.
    always = set(JUDGED_FIELD_TYPES) | set(OPPORTUNITY_FIELD_TYPES)
    for ft in sorted(set(rows_by_ft) | set(account_by_ft) | always):
        slug = _id(ft)
        fs.add(
            f"ext.link_row.enabled.{slug}.count",
            rows_by_ft.get(ft, 0),
            "campaign_asset_link_rows",
            ENABLED_SCOPE + f" AND campaign_asset.field_type={ft}" + LINK_ROW_CAVEAT,
            window,
            CAMPAIGN_ASSETS,
            "n/a",
        )
        fs.add(
            f"ext.distinct_asset.enabled.{slug}.count",
            len(assets_by_ft.get(ft, set())),
            "distinct_assets",
            ENABLED_SCOPE + f" AND campaign_asset.field_type={ft}" + DISTINCT_CAVEAT,
            window,
            CAMPAIGN_ASSETS,
            "n/a",
        )
        fs.add(
            f"ext.account_level.{slug}.distinct_asset.count",
            len(account_by_ft.get(ft, set())),
            "distinct_assets",
            PULL_CUSTOMER_ASSET + f" AND customer_asset.field_type={ft}" + DISTINCT_CAVEAT,
            account_window,
            CUSTOMER_ASSETS,
            "n/a",
        )
        fs.add(
            f"ext.union.{slug}.distinct_asset.count",
            len(assets_by_ft.get(ft, set()) | account_by_ft.get(ft, set())),
            "distinct_assets",
            UNION_SCOPE + f" AND field_type={ft}",
            window,
            f"{CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS}",
            "n/a",
        )
        if ft in assets_by_ft and rows_by_ft.get(ft, 0) != len(assets_by_ft[ft]):
            trail.append(
                f"{ft}: {rows_by_ft[ft]} attachments carry "
                f"{len(assets_by_ft[ft])} distinct assets. Quoting the "
                "attachment count as an asset count overstates variety by "
                f"{rows_by_ft[ft] - len(assets_by_ft[ft])}."
            )

    overlap = {aid for aid, _ in account} & {k[1] for k in kept}
    fs.add(
        "ext.account_level.also_campaign_attached.count",
        len(overlap),
        "distinct_assets",
        UNION_SCOPE
        + " , INTERSECTION rather than union: account-level assets that are also "
        "attached at campaign level. Adding the two pulls' counts double counts "
        "these",
        window,
        f"{CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS}",
        "n/a",
    )
    return trail


def add_coverage_facts(
    fs: FactSet,
    keys: list[tuple],
    enabled: dict,
    account: list[tuple],
    window: str,
    raw_dir: "Path | None" = None,
    cost_window: str | None = None,
) -> dict:
    """The three-state coverage grid, counted per field type over SEARCH."""
    campaign_level: dict = collections.defaultdict(set)
    for cid, _aid, ft in keys:
        if cid in enabled:
            campaign_level[cid].add(ft)
    account_types = {ft for _aid, ft in account}
    search = [
        cid
        for cid, c in enabled.items()
        if c.get("advertisingChannelType") == "SEARCH"
    ]

    grid: dict = {}
    for ft in JUDGED_FIELD_TYPES + OPPORTUNITY_FIELD_TYPES:
        slug = _id(ft)
        states = collections.Counter(
            coverage_state(ft, cid, campaign_level, account_types) for cid in search
        )
        grid[ft] = {
            cid: coverage_state(ft, cid, campaign_level, account_types)
            for cid in search
        }
        for state in (STATE_CAMPAIGN, STATE_INHERITED, STATE_ABSENT):
            fs.add(
                f"ext.coverage.search.{slug}.{state}.count",
                states.get(state, 0),
                "count",
                SEARCH_SCOPE
                + f" , resolved per campaign for field_type={ft} into one of "
                "three states: campaign_level, inherited_from_account "
                "(no campaign-level asset but the type exists at account level "
                "and serves - verified from asset-field-type-30d.json), "
                "absent_everywhere. This state is "
                + state,
                window,
                f"{CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS} + {CAMPAIGNS}",
                "n/a",
            )
    # Rank each absent_everywhere set by 7-day cost, per the skill's selection
    # rule, and emit the rule itself so no run can rank without declaring how.
    cost_path = (raw_dir / COST_7D) if raw_dir else None
    cost_by_campaign: dict = {}
    metrics_by_campaign: dict = {}
    if cost_path is not None and cost_path.exists():
        for row in (
            (json.loads(cost_path.read_text(encoding="utf-8")).get("data") or {})
            .get("results")
            or []
        ):
            cid = (row.get("campaign") or {}).get("id")
            if cid:
                m = row.get("metrics") or {}
                cost_by_campaign[cid] = int(m.get("costMicros") or 0)
                metrics_by_campaign[cid] = {
                    "clicks": int(m.get("clicks") or 0),
                    "impressions": int(m.get("impressions") or 0),
                }
    have_cost = bool(cost_by_campaign)
    fs.add(
        "ext.absent.ranking.selection_rule",
        "7-day cost descending, from campaign-cost-is-7d.json"
        if have_cost
        else None,
        "text",
        "the skill's selection rule: an absent_everywhere finding ships every "
        "affected campaign, and a run that ranks inside that table ranks by "
        "7-day cost and emits the rule. Never rank without emitting it"
        if have_cost
        else S.unavailable(
            f"{COST_7D} is absent from the raw dir, so no ranking is possible "
            "and none is guessed"
        ),
        window,
        COST_7D,
        "n/a",
    )
    for ft in JUDGED_FIELD_TYPES + OPPORTUNITY_FIELD_TYPES:
        slug = _id(ft)
        absent = [cid for cid in search if grid.get(ft, {}).get(cid) == STATE_ABSENT]
        spending = [cid for cid in absent if cost_by_campaign.get(cid, 0) > 0]
        for name, value, why in (
            (
                "with_spend.count",
                len(spending) if have_cost else None,
                "of the campaigns missing this field type everywhere, how many "
                "spent anything in the 7-day window. THIS is the actionable "
                "count; the absent_everywhere count includes dormant campaigns",
            ),
            (
                "zero_spend.count",
                len(absent) - len(spending) if have_cost else None,
                "campaigns missing this field type everywhere that spent "
                "nothing in the 7-day window. A coverage gap on a campaign that "
                "does not spend is structure to clean up, not performance work",
            ),
        ):
            fs.add(
                f"ext.coverage.search.{slug}.absent_everywhere.{name}",
                value,
                "count",
                (SEARCH_SCOPE + " joined to " + COST_7D + " on campaign.id. " + why)
                if have_cost
                else S.unavailable(f"{COST_7D} is absent, so {why[:40]}... is unknown"),
                window,
                f"{CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS} + {CAMPAIGNS} + {COST_7D}",
                "n/a",
            )
        # Named per-campaign 7-day totals for the spending campaigns only.
        # This is a single campaign's own total from COST_7D, already read for
        # ranking above - not a per-field-type figure and not summed across
        # anything, so none of the reasons the field-type serve tables refuse
        # cost apply here. Emitted because the with_spend campaign is exactly
        # the one a recommendation names, and "spending" with no number is a
        # missing fact per the skill's own rule (step 6).
        for cid in spending:
            m = metrics_by_campaign.get(cid, {})
            row_scope = (
                SEARCH_SCOPE + f" AND campaign.id={cid}, joined to {COST_7D} on "
                "campaign.id. The named campaign's own 7-day total - not a "
                "per-field-type figure, not summed across campaigns"
            )
            fs.add(
                f"ext.coverage.search.{slug}.absent_everywhere.with_spend.spend_cad.{cid}",
                money(micros_to_currency(cost_by_campaign.get(cid))),
                "currency_cad",
                row_scope,
                cost_window or window,
                f"{CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS} + {CAMPAIGNS} + {COST_7D}",
                "n/a",
            )
            fs.add(
                f"ext.coverage.search.{slug}.absent_everywhere.with_spend.clicks.{cid}",
                m.get("clicks"),
                "clicks",
                row_scope,
                cost_window or window,
                f"{CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS} + {CAMPAIGNS} + {COST_7D}",
                "n/a",
            )
            fs.add(
                f"ext.coverage.search.{slug}.absent_everywhere.with_spend.impressions.{cid}",
                m.get("impressions"),
                "impressions",
                row_scope,
                cost_window or window,
                f"{CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS} + {CAMPAIGNS} + {COST_7D}",
                "n/a",
            )

    fs.add(
        "ext.coverage.tof_campaign.count",
        sum(
            1
            for c in enabled.values()
            if c.get("advertisingChannelType") in TOF_CHANNELS
        ),
        "count",
        PULL_CAMPAIGNS
        + f" AND campaign.advertising_channel_type IN {TOF_CHANNELS}. Reported, "
        "never judged: guardrail 1, and their extension model is ad-group / "
        "asset-group level, which no pull here reads",
        window,
        CAMPAIGNS,
        "n/a",
    )
    return grid


def add_inheritance_facts(
    fs: FactSet,
    serves: list[tuple],
    keys: list[tuple],
    enabled: dict,
    account: list[tuple],
    window: str,
) -> list[str]:
    """The proof that account-level assets serve, and the residue they do not explain."""
    campaign_level: dict = collections.defaultdict(set)
    for cid, _aid, ft in keys:
        if cid in enabled:
            campaign_level[cid].add(ft)
    account_types = {ft for _aid, ft in account}

    proven, unexplained = [], []
    for cid, ft, impressions, _clicks in serves:
        if cid not in enabled or impressions <= 0:
            continue
        if ft in campaign_level.get(cid, set()):
            continue
        (proven if ft in account_types else unexplained).append((cid, ft))

    fs.add(
        "ext.inheritance.proven_pair.count",
        len(proven),
        "count",
        SERVE_SCOPE
        + " , kept where metrics.impressions > 0 AND the field type has NO "
        "campaign-level asset on that campaign AND the type exists at account "
        "level. Each row is one proof that an account-level asset serves where a "
        "campaign has none of its own"
        + FAN_OUT_CAVEAT,
        window,
        f"{FIELD_TYPE_VIEW} + {CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS}",
        "broad-goal-set",
    )
    fs.add(
        "ext.inheritance.unexplained_pair.count",
        len(unexplained),
        "count",
        SERVE_SCOPE
        + " , kept where metrics.impressions > 0 AND the field type has NO "
        "campaign-level asset AND is absent from the account level too. Neither "
        "current-state pull explains these, so a third source exists - "
        "ad-group-level assets or automatically created assets, neither of which "
        "this skill pulls"
        + FAN_OUT_CAVEAT,
        window,
        f"{FIELD_TYPE_VIEW} + {CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS}",
        "broad-goal-set",
    )
    by_ft = collections.Counter(ft for _cid, ft in unexplained)
    for ft, n in sorted(by_ft.items()):
        fs.add(
            f"ext.inheritance.unexplained_pair.{_id(ft)}.count",
            n,
            "count",
            SERVE_SCOPE
            + f" , field_type={ft}, serving with no campaign-level and no "
            "account-level asset on record"
            + FAN_OUT_CAVEAT,
            window,
            f"{FIELD_TYPE_VIEW} + {CAMPAIGN_ASSETS} + {CUSTOMER_ASSETS}",
            "broad-goal-set",
        )
    return [
        f"inheritance: {len(proven)} (campaign, field_type) pair(s) serve with no "
        f"campaign-level asset while the type exists at account level. That is "
        "the verified basis for treating a blank grid cell as inherited rather "
        "than missing.",
        f"inheritance: {len(unexplained)} pair(s) serve with no asset in either "
        f"current-state pull ({', '.join(f'{k} x{v}' for k, v in sorted(by_ft.items())) or 'none'}). "
        "Ad-group-level assets are out of scope for this skill and this is the "
        "visible consequence.",
    ]


def add_serve_facts(
    fs: FactSet, serves: list[tuple], enabled: dict, window: str
) -> None:
    """Per field type impressions and clicks. Never cost, never a cross-type sum."""
    kept = [s for s in serves if s[0] in enabled]
    agg: dict = collections.defaultdict(lambda: [0, 0, set()])
    for cid, ft, impressions, clicks in kept:
        agg[ft][0] += impressions
        agg[ft][1] += clicks
        agg[ft][2].add(cid)
    for ft, (impressions, clicks, campaigns) in sorted(agg.items()):
        slug = _id(ft)
        scope = SERVE_SCOPE + f" AND asset_field_type_view.field_type={ft}" + FAN_OUT_CAVEAT
        fs.add(
            f"ext.serve.{slug}.impressions",
            impressions,
            "impressions_asset_credited",
            scope,
            window,
            FIELD_TYPE_VIEW,
            "broad-goal-set",
        )
        fs.add(
            f"ext.serve.{slug}.clicks",
            clicks,
            "clicks_asset_credited",
            scope,
            window,
            FIELD_TYPE_VIEW,
            "broad-goal-set",
        )
        fs.add(
            f"ext.serve.{slug}.campaign.count",
            len(campaigns),
            "count",
            SERVE_SCOPE + f" AND asset_field_type_view.field_type={ft}",
            window,
            FIELD_TYPE_VIEW,
            "broad-goal-set",
        )
    fs.add(
        "ext.serve.field_type_sum.impressions",
        sum(v[0] for v in agg.values()),
        "impressions_asset_credited",
        SERVE_SCOPE
        + " , summed across ALL field types. THIS FIGURE IS A FAN-OUT ARTEFACT "
        "AND NOT AN IMPRESSION COUNT. It is emitted so a report can name it as "
        "the reason cross-field-type sums are forbidden, never as a total"
        + FAN_OUT_CAVEAT,
        window,
        FIELD_TYPE_VIEW,
        "broad-goal-set",
    )
    sitelink = agg.get("SITELINK", [0, 0, set()])[0]
    snippet = agg.get("STRUCTURED_SNIPPET", [0, 0, set()])[0]
    fs.add(
        "ext.serve.structured_snippet_vs_sitelink.percent",
        round(100.0 * snippet / sitelink, 2) if sitelink else None,
        "percent_of_sitelink_impressions",
        SERVE_SCOPE
        + " , STRUCTURED_SNIPPET impressions as a percentage of SITELINK "
        "impressions. Both are asset-credited under the same scope and window, so "
        "the ratio is like-for-like. The two nonzero campaign populations differ, "
        "so it is a comparison of how often each type shows, NOT a share of "
        "anything"
        + FAN_OUT_CAVEAT,
        window,
        FIELD_TYPE_VIEW,
        "broad-goal-set",
    )


def add_change_facts(fs: FactSet, raw_dir: Path, window: str) -> list[str]:
    """VF-17 rule 3. Extensions are actively edited; check before calling one absent."""
    path = raw_dir / CHANGE_EVENTS
    if not path.exists():
        for suffix in ("campaign_asset.count", "campaign_asset.campaign.count", "asset.count"):
            fs.add(
                f"ext.change_event.{suffix}",
                None,
                "count",
                S.unavailable(
                    f"{CHANGE_EVENTS} is absent from {raw_dir.name}, so VF-17 "
                    "rule 3 could not be checked for any campaign"
                ),
                window,
                CHANGE_EVENTS,
                "n/a",
            )
        return [
            f"{CHANGE_EVENTS}: ABSENT. No claim that a campaign lacks an "
            "extension may be made without it (VF-17 rule 3)."
        ]
    rows = load_results(raw_dir, CHANGE_EVENTS)
    events = [r.get("changeEvent") or {} for r in rows]
    campaign_asset = [e for e in events if e.get("changeResourceType") == "CAMPAIGN_ASSET"]
    asset = [e for e in events if e.get("changeResourceType") == "ASSET"]
    fs.add(
        "ext.change_event.campaign_asset.count",
        len(campaign_asset),
        "count",
        "change_event.change_date_time DURING LAST_14_DAYS AND "
        "change_event.change_resource_type=CAMPAIGN_ASSET",
        window,
        CHANGE_EVENTS,
        "n/a",
    )
    fs.add(
        "ext.change_event.campaign_asset.campaign.count",
        len({e.get("campaign") for e in campaign_asset if e.get("campaign")}),
        "count",
        "change_event.change_date_time DURING LAST_14_DAYS AND "
        "change_event.change_resource_type=CAMPAIGN_ASSET, counted as DISTINCT "
        "campaigns rather than events - a count is meaningless without its unit "
        "(guardrail 7)",
        window,
        CHANGE_EVENTS,
        "n/a",
    )
    fs.add(
        "ext.change_event.asset.count",
        len(asset),
        "count",
        "change_event.change_date_time DURING LAST_14_DAYS AND "
        "change_event.change_resource_type=ASSET",
        window,
        CHANGE_EVENTS,
        "n/a",
    )
    return [
        f"{CHANGE_EVENTS}: {len(campaign_asset)} CAMPAIGN_ASSET event(s) across "
        f"{len({e.get('campaign') for e in campaign_asset if e.get('campaign')})} "
        "distinct campaign(s), and "
        f"{len(asset)} ASSET event(s), inside 14 days. Extensions are being "
        "edited, so VF-17 rule 3 is a live risk here, not a formality."
    ]


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def build(raw_dir: Path) -> FactSet:
    manifest = S.Manifest(raw_dir)
    if manifest.pulled is None:
        raise BuildError(
            f"{Path(raw_dir).name} is not a YYYY-MM-DD directory, so the pull "
            "date cannot be read and no current-state window can be formed "
            "(VF-17 rule 4)."
        )

    # Consolidated validation: every contradiction in one pass, one raise. A
    # single contradiction re-raises the original exception unchanged; two or
    # more are listed together. Nothing is written on any failure.
    problems = pulls.ProblemLog()
    ca = problems.run(load_results, raw_dir, CAMPAIGN_ASSETS,
                      file=CAMPAIGN_ASSETS)
    cu = problems.run(load_account_level, raw_dir, file=CUSTOMER_ASSETS)
    fv = problems.run(load_results, raw_dir, FIELD_TYPE_VIEW,
                      file=FIELD_TYPE_VIEW)
    cp = problems.run(load_results, raw_dir, CAMPAIGNS, file=CAMPAIGNS)

    # Temporality is declared, never inferred (VF-17 rule 5).
    ca_window = manifest.window_for(CAMPAIGN_ASSETS, windowed_fallback=False)
    cu_window = manifest.window_for(CUSTOMER_ASSETS, windowed_fallback=False)
    cp_window = manifest.window_for(CAMPAIGNS, windowed_fallback=False)
    fv_window = manifest.window_for(FIELD_TYPE_VIEW, windowed_fallback=True)
    ce_window = manifest.window_for(CHANGE_EVENTS, windowed_fallback=True)
    cost7_window = manifest.window_for(COST_7D, windowed_fallback=True)

    trail: list[str] = []
    if not manifest.present:
        trail.append(
            f"{S.MANIFEST}: ABSENT from {raw_dir.name}. Temporality could not be "
            "declared for any file, so every window string says so (VF-17 "
            "rule 5)."
        )
    if cu is not None and not cu:
        trail.append(
            f"{CUSTOMER_ASSETS}: ZERO rows. Nothing is inherited, so every "
            "field type with no campaign-level asset is absent_everywhere on "
            "that campaign rather than covered from the account level."
        )
    for name, rows in (
        (CAMPAIGN_ASSETS, ca),
        (CUSTOMER_ASSETS, cu),
        (FIELD_TYPE_VIEW, fv),
        (CAMPAIGNS, cp),
    ):
        if rows is None:
            continue
        trail.append(manifest.audit_line(name))
        line = problems.run(
            manifest.check_row_count, name, len(rows), file=name
        )
        if line:
            trail.append(line)

    # VF-19a: clause-for-clause after normalising, not byte equality. Only the
    # three status-filtered pulls have a non-temporality clause to compare.
    for name, label in (
        (CAMPAIGN_ASSETS, PULL_CAMPAIGN_ASSET),
        (CUSTOMER_ASSETS, PULL_CUSTOMER_ASSET),
        (CAMPAIGNS, PULL_CAMPAIGNS),
    ):
        line = problems.run(manifest.verify_filter, name, label, file=name)
        if line:
            trail.append(line)
    trail.append(
        f"{FIELD_TYPE_VIEW}: declared filter "
        f"{manifest.declared_filter(FIELD_TYPE_VIEW)!r} carries only a date "
        "predicate, which is temporality and lives in `window` (VF-17). Its "
        "non-temporality clause set is empty, so there is nothing to compare "
        "clause-for-clause: the scope label stands DECLARED, not verified."
    )
    # Only this skill's own pulls. The `audit-ad-copy` builder loops over every
    # entry in the manifest, which on 2026-08-20 aborts the whole run on a pull
    # it never reads: `gaql.clauses` splits `segments.date BETWEEN '2026-08-01'
    # AND '2026-08-19'` on the AND into two clauses, so
    # `check_declared_temporality` reads the range as `segments.date BETWEEN
    # 2026-08-01` and calls the declared `window:2026-08-01..2026-08-19` a
    # different range. That is a defect in a read-only module (reported, not
    # fixed here); validating only the files this builder reads is the correct
    # scope regardless.
    for name in (CAMPAIGN_ASSETS, CUSTOMER_ASSETS, FIELD_TYPE_VIEW, CAMPAIGNS):
        if name not in manifest.files:
            continue
        line = problems.run(
            manifest.verify_declared_temporality, name, file=name
        )
        if line:
            trail.append(line)
    undeclared = manifest.undeclared_files()
    if undeclared:
        trail.append(
            f"{S.MANIFEST}: {len(undeclared)} raw pull(s) on disk are NOT "
            f"declared in it: {', '.join(undeclared)}. No fact may borrow a "
            "label from them (VF-17 rule 5)."
        )

    for name, rows, clause_list in (
        (CAMPAIGN_ASSETS, ca,
         [(PULL_CAMPAIGN_ASSET, pulls._status_is("campaignAsset", "ENABLED"))]),
        (CUSTOMER_ASSETS, cu,
         [(PULL_CUSTOMER_ASSET, pulls._status_is("customerAsset", "ENABLED"))]),
        (CAMPAIGNS, cp,
         [(PULL_CAMPAIGNS, pulls._status_is("campaign", "ENABLED"))]),
    ):
        if rows is None:
            continue
        lines = problems.run(validate_clauses, name, rows, clause_list,
                             file=name)
        if lines:
            trail += [f"{name}: {t}" for t in lines]

    problems.raise_if_any(raw_dir.name)
    keys = link_rows(ca)
    trail.append(assert_unique_link_key(CAMPAIGN_ASSETS, keys))
    account = account_rows(cu)
    serves = serve_rows(fv)
    serve_keys = [(cid, ft) for cid, ft, _i, _c in serves]
    if len(serve_keys) != len(set(serve_keys)):
        raise BuildError(
            f"{FIELD_TYPE_VIEW}: duplicate (campaign.id, field_type) keys. The "
            "response is segmented; aggregate per key before summing."
        )
    trail.append(
        f"{FIELD_TYPE_VIEW}: {len(serves)} rows, {len(set(serve_keys))} distinct "
        "(campaign.id, field_type) keys - they agree (VF-19)"
    )
    enabled = campaign_index(cp)

    # The whole point of the campaign-status join, stated once in the trail.
    pull_campaigns = {k[0] for k in keys}
    if pull_campaigns - set(enabled):
        trail.append(
            f"{CAMPAIGN_ASSETS}: {len(pull_campaigns - set(enabled))} of "
            f"{len(pull_campaigns)} campaigns in this pull are NOT in "
            f"{CAMPAIGNS}. `campaign_asset.status='ENABLED'` filters the "
            "attachment, not the campaign. Every count below is joined "
            "client-side to the enabled-campaign list and says so in its scope."
        )

    fs = FactSet(
        meta={
            "skill": "audit-extensions",
            "customer_id": account_config.customer_id(),
            "currency": "CAD",
            "raw_dir": str(raw_dir),
            "generator": "tools/build_audit_extensions_facts.py",
            "manifest_present": manifest.present,
            "row_key": (
                "(campaign.id, asset.id, field_type) for campaign_asset; "
                "(asset.id, field_type) for customer_asset; "
                "(campaign.id, field_type) for asset_field_type_view - VF-19"
            ),
            "link_rows_vs_distinct_assets": (
                "campaign_asset returns one row per attachment. An asset shared "
                "across campaigns contributes one row each, so the row count is "
                "a coverage measure and the distinct asset count is a variety "
                "measure. Both are emitted; neither may be stated alone."
            ),
            "inheritance": (
                "VERIFIED from asset-field-type-30d.json: account-level assets "
                "serve on campaigns holding none of their own. ASSUMED, from "
                "Google's documentation and not derivable here: a campaign-level "
                "asset of a type REPLACES the account-level set for that "
                "campaign rather than adding to it."
            ),
            "out_of_scope": (
                "ad_group_asset. No ad-group-level extension pull exists in this "
                "raw directory, so ad-group overrides are invisible here and "
                "PROMOTION serve data has no asset to trace to."
            ),
            "windows": {
                CAMPAIGN_ASSETS: ca_window,
                CUSTOMER_ASSETS: cu_window,
                CAMPAIGNS: cp_window,
                FIELD_TYPE_VIEW: fv_window,
                CHANGE_EVENTS: ce_window,
            },
            "inputs": {
                name: {
                    "rows": rows,
                    "field_mask": field_mask(raw_dir, name),
                    "declared_temporality": manifest.declared_temporality(name),
                    "declared_filter": manifest.declared_filter(name),
                }
                for name, rows in (
                    (CAMPAIGN_ASSETS, len(ca)),
                    (CUSTOMER_ASSETS, len(cu)),
                    (FIELD_TYPE_VIEW, len(fv)),
                    (CAMPAIGNS, len(cp)),
                )
            },
            "declared_filter_audit": trail,
        }
    )

    add_scope_facts(fs, keys, enabled, ca_window, cp_window)
    add_sharing_facts(fs, keys, enabled, ca_window)
    trail += add_field_type_facts(fs, keys, enabled, account, ca_window, cu_window)
    add_coverage_facts(fs, keys, enabled, account, ca_window, raw_dir, cost7_window)
    trail += add_inheritance_facts(fs, serves, keys, enabled, account, fv_window)
    add_serve_facts(fs, serves, enabled, fv_window)
    trail += add_change_facts(fs, raw_dir, ce_window)
    return fs


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("raw_dir", help="google-ads/outputs/raw/YYYY-MM-DD")
    parser.add_argument(
        "--out",
        default=None,
        help="output path (default: <raw_dir>/audit-extensions-facts.json)",
    )
    parser.add_argument("--print", action="store_true", help="print every fact")
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

    out = Path(args.out) if args.out else raw_dir / "audit-extensions-facts.json"
    fs.write(out)
    nulls = [f.id for f in fs if f.is_null]
    print(f"wrote {len(fs)} facts to {out}")
    print(f"{len(nulls)} deliberately null (unavailable) facts")
    for id in nulls:
        print(f"  - {id}")
    if args.print:
        for fact in fs:
            print(fact.describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
