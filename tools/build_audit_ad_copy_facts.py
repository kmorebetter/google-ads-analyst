#!/usr/bin/env python3
"""Build the `audit-ad-copy` facts file from saved raw JSON (VERIFICATION-RULES VF-15).

Usage:
    python3 tools/build_audit_ad_copy_facts.py google-ads/outputs/raw/2026-08-19
    python3 tools/build_audit_ad_copy_facts.py <raw_dir> --out /tmp/facts.json

Reads only the raw JSON already on disk. Makes no API calls. Writes
`<raw_dir>/audit-ad-copy-facts.json` unless `--out` says otherwise.

Scope literals, raw filenames and manifest reading live in `scopes.py`.

Five things about this script matter more than the arithmetic.

1. **Declared filters are validated per row, tri-state.** For each clause every
   row answers true, false, or unknown-because-the-field-was-not-saved. Any
   false aborts the run. All-unknown downgrades the clause to DECLARED and says
   so in the facts file's audit trail. A correct number under a stale label is
   the failure VF-15 exists to prevent.

2. **Temporality comes from `_manifest.json`, never from the data** (VF-17
   rule 5). Current-state facts carry `window: current-as-of-<date>` so the
   guard refuses to compare them against a windowed metric.

3. **`ad_group_ad_asset_view` is keyed on the four-part tuple**
   `(ad_group.id, ad.id, asset.id, field_type)`, equivalently on
   `ad_group_ad_asset_view.resource_name` (VF-4a). That pull carries no status
   filter, so one Ad entity appears under several ad groups: ad 620000000001
   returns 140 rows across 5 ad groups, and only 22 once restricted to one ad
   group and HEADLINE. Every asset fact id names both the ad group and the ad.

4. **Serve data is a window; the ad is a current state. They get intersected**
   (VF-4b). Only headlines on the ad right now are ranked. A live headline with
   zero serves is a dead slot, the actionable finding. An asset that served but
   is no longer on the ad is history and is never presented as a live problem.
   That is the exact error VF-17 exists to stop.

5. **The join key for ad-level rows is `adGroupAd.resourceName`, never
   `ad.id`.** Google reuses one ad id across ad groups.

Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asset_facts  # noqa: E402
import account_config  # noqa: E402
import gaql  # noqa: E402
import pulls  # noqa: E402
import scopes as S  # noqa: E402
from facts import FactSet, micros_to_currency  # noqa: E402

BuildError = pulls.BuildError
load_results = pulls.load_results
field_mask = pulls.field_mask
validate_clauses = pulls.validate_clauses
_status_is = pulls._status_is
assert_unique_join_key = pulls.assert_unique_join_key
ad_key = pulls.ad_key
ag_ad = pulls.ag_ad
InventoryScope = pulls.InventoryScope
read_inventory_scope = pulls.read_inventory_scope

INVENTORY = S.INVENTORY
METRICS = S.METRICS
CONVERSIONS = S.CONVERSIONS
ASSET_VIEW = S.ASSET_VIEW
VF5_FULL_FILTER = S.VF5_FULL_FILTER
UNFILTERED_AD_GROUP_FILTER = S.UNFILTERED_AD_GROUP_FILTER
PAUSED_AG_FILTER = S.PAUSED_AG_FILTER
ASSET_VIEW_BASIS = S.ASSET_VIEW_BASIS
EIGHT_ACTIONS = S.EIGHT_ACTIONS
ASSET_LABELS = S.ASSET_LABELS

#: An ad needs this many live headlines before a top-3 and a bottom-5 share can
#: be quoted without the two overlapping.
MIN_HEADLINES_FOR_SHARES = 8

#: `audit-ad-copy` step 5's selection rule for which ads get a serve table: the
#: top N servable RSAs by 30-day cost, plus any other servable RSA above the
#: threshold. The rule and the ad count it produces are both emitted as facts,
#: because the skill requires the report to print them beside the scope filter.
#: Since 2026-08-27 the same rule also decides which ads get PER-HEADLINE
#: facts at all: exhaustive per-headline emission (~6.4k facts) built the
#: dense value space both fabrication escapes hid in, so detail is emitted
#: for the tabled ads only and `--ad <id>` re-runs the builder for a deeper
#: dive. The two constants are read from `account-config.json`'s `thresholds`
#: block when present (`serve_table_top_n`, `serve_table_min_cost_cad`), so
#: the account file, not this module, is where the rule is tuned.


def _config_threshold(key: str, fallback):
    try:
        return account_config.threshold(key)
    except Exception:
        return fallback


SERVE_TABLE_TOP_N = int(_config_threshold("serve_table_top_n", 10))
#: Written with cents so it serialises as a currency float like every other
#: money fact, rather than as a bare integer a count could accidentally cite.
SERVE_TABLE_MIN_COST_CAD = Decimal(
    str(_config_threshold("serve_table_min_cost_cad", "2000.00"))
)

#: VF-6 detects a trademark restriction by **substring**, not by one topic
#: string: "every ad whose topic list contains TRADEMARK". The live example is
#: `TRADEMARK_IN_AD_TEXT_SPECIFICS`, but the class is what the rule names.
TRADEMARK_TOPIC_SUBSTRING = "TRADEMARK"


def policy_topics(row: dict) -> list:
    """Every policy topic string on one `ad_group_ad` row."""
    summary = (row["adGroupAd"].get("policySummary") or {})
    return [
        entry.get("topic")
        for entry in (summary.get("policyTopicEntries") or [])
        if entry.get("topic")
    ]


def has_trademark(row: dict) -> bool:
    """VF-6's test: does any topic on this ad contain TRADEMARK."""
    return any(TRADEMARK_TOPIC_SUBSTRING in topic for topic in policy_topics(row))


def trademark_restricted_keys(servable: list) -> set:
    """`adGroupAd.resourceName` of every servable ad carrying a trademark topic.

    Trademark **only**. `FREE_DESKTOP_SOFTWARE` is a different restriction class
    and both `audit-ad-copy` step 4 and `campaign-guardrails` rule 6 keep it IN
    scope for copy and ad-strength work. Bundling the two is the exact mistake
    the `FreeTool` case exists to prevent.
    """
    return {ad_key(r) for r in servable if has_trademark(r)}


# --------------------------------------------------------------------------
# Inventory facts (current state)
# --------------------------------------------------------------------------


def headlines(row: dict) -> list[dict]:
    rsa = (row["adGroupAd"].get("ad") or {}).get("responsiveSearchAd") or {}
    return rsa.get("headlines") or []


def descriptions(row: dict) -> list[dict]:
    rsa = (row["adGroupAd"].get("ad") or {}).get("responsiveSearchAd") or {}
    return rsa.get("descriptions") or []


def add_counts(fs: FactSet, scope: InventoryScope, window: str) -> None:
    """VF-5: RSA counts at each filter level, plus what cannot be derived."""
    servable_scope = scope.scope

    for fact_id in (
        "rsa.servable.count",
        "rsa.filter_level.ad_and_ad_group_and_campaign_enabled.count",
    ):
        fs.add(
            id=fact_id,
            value=len(scope.servable),
            unit="count",
            scope=servable_scope,
            window=window,
            source=INVENTORY,
            basis="n/a",
        )

    # Levels 1 and 2 of the VF-5 table are never derivable from a saved raw dir:
    # the pull itself filtered campaign.status, so rows outside it were never
    # saved. Emitted as null facts with the reason. A missing fact reads as an
    # oversight; a null fact with a reason reads as the harness working.
    fs.add(
        id="rsa.filter_level.ad_enabled_only.count",
        value=None,
        unit="count",
        scope=S.unavailable(
            "wanted ad_group_ad.status=ENABLED AND ad.type=RESPONSIVE_SEARCH_AD "
            "with no ad_group.status and no campaign.status filter. The saved "
            f"{INVENTORY} pull filtered campaign.status, so rows outside it were "
            "never saved and the count cannot be recovered without a fresh pull."
        ),
        window=window,
        source=INVENTORY,
        basis="n/a",
    )
    fs.add(
        id="rsa.filter_level.ad_and_ad_group_enabled.count",
        value=None,
        unit="count",
        scope=S.unavailable(
            "wanted ad_group_ad.status=ENABLED AND ad_group.status=ENABLED "
            "AND ad.type=RESPONSIVE_SEARCH_AD with no campaign.status filter. "
            f"The saved {INVENTORY} pull filtered campaign.status, so the count "
            "cannot be recovered without a fresh pull."
        ),
        window=window,
        source=INVENTORY,
        basis="n/a",
    )

    if scope.unfiltered_proven:
        fs.add(
            id="rsa.unfiltered_ad_group_status.count",
            value=len(scope.rows),
            unit="count",
            scope=UNFILTERED_AD_GROUP_FILTER,
            window=window,
            source=INVENTORY,
            basis="n/a",
        )
        fs.add(
            id="rsa.in_paused_or_removed_ad_group.count",
            value=len(scope.non_servable),
            unit="count",
            scope=PAUSED_AG_FILTER,
            window=window,
            source=INVENTORY,
            basis="n/a",
        )
    else:
        reason = S.unavailable(
            "this pull returned no rows in a PAUSED or REMOVED ad group. That "
            "is indistinguishable from a pull that filtered ad_group.status, so "
            "labelling a count 'ad_group.status NOT FILTERED' would be a guess. "
            + " ".join(scope.notes)
        )
        for fact_id in (
            "rsa.unfiltered_ad_group_status.count",
            "rsa.in_paused_or_removed_ad_group.count",
        ):
            fs.add(
                id=fact_id,
                value=None,
                unit="count",
                scope=reason,
                window=window,
                source=INVENTORY,
                basis="n/a",
            )

    fs.add(
        id="rsa.servable.ad_group.count",
        value=len({r["adGroup"]["resourceName"] for r in scope.servable}),
        unit="count",
        scope=servable_scope + " (distinct ad groups)",
        window=window,
        source=INVENTORY,
        basis="n/a",
    )
    fs.add(
        id="rsa.servable.campaign.count",
        value=len({r["campaign"]["resourceName"] for r in scope.servable}),
        unit="count",
        scope=servable_scope + " (distinct campaigns)",
        window=window,
        source=INVENTORY,
        basis="n/a",
    )

    add_policy_facts(fs, scope, window)

    # Coverage and testing-gap figures the audit states at account level. Each
    # of these is a plausible headline number in the report, so each needs an id
    # a report can cite (VF-15). Without them the report has to do the counting
    # in prose, which is the habit this harness exists to break.
    per_ad_group = collections.Counter(
        r["adGroup"]["resourceName"] for r in scope.servable
    )
    fs.add(
        id="rsa.servable.ad_group.single_rsa.count",
        value=sum(1 for n in per_ad_group.values() if n == 1),
        unit="count",
        scope=(
            servable_scope
            + " (ad groups holding exactly one servable RSA, so no A/B test is"
            " running)"
        ),
        window=window,
        source=INVENTORY,
        basis="n/a",
    )
    fs.add(
        id="rsa.servable.ad.fewer_than_10_headlines.count",
        value=sum(1 for r in scope.servable if len(headlines(r)) < 10),
        unit="count",
        scope=servable_scope + " AND count(responsive_search_ad.headlines) < 10",
        window=window,
        source=INVENTORY,
        basis="n/a",
    )
    fs.add(
        id="rsa.servable.ad.fewer_than_3_descriptions.count",
        value=sum(1 for r in scope.servable if len(descriptions(r)) < 3),
        unit="count",
        scope=servable_scope + " AND count(responsive_search_ad.descriptions) < 3",
        window=window,
        source=INVENTORY,
        basis="n/a",
    )
    # The ad-strength count a recommendation can actually act on. The exclusion
    # is **trademark only**.
    #
    # This fact used to exclude TRADEMARK *or* FREE_DESKTOP_SOFTWARE, citing
    # VF-6. That was a scope the skills forbid: `audit-ad-copy` step 4 and
    # `campaign-guardrails` rule 6 both keep `FREE_DESKTOP_SOFTWARE` **in** scope
    # for copy and ad-strength work and say in terms that it is a different
    # restriction class which must never be bundled with the trademark cases.
    # The value did not move when this was fixed - the one such ad (`FreeTool`,
    # ad group 620000000002, ad 620000000003) has EXCELLENT strength, so it was
    # never in the POOR-or-AVERAGE population - which is exactly why the wrong
    # scope survived two runs. The next ad to pick up that topic would have
    # silently disappeared from a count a recommendation is built on.
    restricted = trademark_restricted_keys(scope.servable)
    fs.add(
        id="rsa.servable.ad_strength.poor_or_average.excluding_policy_restricted.count",
        value=sum(
            1
            for r in scope.servable
            if r["adGroupAd"].get("adStrength") in ("POOR", "AVERAGE")
            and ad_key(r) not in restricted
        ),
        unit="count",
        scope=(
            servable_scope
            + " AND ad_group_ad.ad_strength IN ('POOR','AVERAGE') AND NO topic in"
            " ad_group_ad.policy_summary.policy_topic_entries[] contains"
            " 'TRADEMARK'. Trademark is the ONLY exclusion: VF-6 puts trademark"
            " restrictions out of scope for ad-strength and copy work, while"
            " audit-ad-copy step 4 and campaign-guardrails rule 6 both keep"
            " FREE_DESKTOP_SOFTWARE and UNAPPROVED_SUBSTANCES IN scope as"
            " different restriction classes that are never bundled with the"
            " trademark cases (VF-6)"
        ),
        window=window,
        source=INVENTORY,
        basis="n/a",
    )

    for label, n in sorted(
        collections.Counter(
            r["adGroupAd"].get("adStrength", "ABSENT") for r in scope.servable
        ).items()
    ):
        fs.add(
            id=f"rsa.servable.ad_strength.{label.lower()}.count",
            value=n,
            unit="count",
            scope=servable_scope + f" AND ad_group_ad.ad_strength = '{label}'",
            window=window,
            source=INVENTORY,
            basis="n/a",
        )


def add_policy_facts(fs: FactSet, scope: InventoryScope, window: str) -> None:
    """Policy topic and approval-status counts, computed from THIS run's pull.

    **Never a hard-coded census.** `audit-ad-copy` step 4 spells out why: a
    census written into a skill file on 2026-08-19 (18 `APPROVED_LIMITED`, 8
    `UNAPPROVED_SUBSTANCES`) was already wrong against a re-pull the same
    afternoon (23 and 16). Policy review moves on Google's schedule. So this
    emits one fact per topic and one per approval status **actually observed**,
    which also satisfies step 4's catch-all: a restriction class this pack has
    never seen still gets a citable count rather than silence.

    The previous version looped over two hard-coded topic strings, which is why
    `UNAPPROVED_SUBSTANCES` and `APPROVED_LIMITED` had no fact and the skill had
    to tell report authors not to state either number.

    `any_trademark` is the one aggregate, and it is a **class** not a census:
    VF-6's exclusion test is "a topic containing TRADEMARK", so the number the
    exclusion actually rests on gets its own id.
    """
    servable_scope = scope.scope
    topic_counts: collections.Counter = collections.Counter()
    per_ad_topics: collections.Counter = collections.Counter()
    approval_counts: collections.Counter = collections.Counter()

    for row in scope.servable:
        topics = policy_topics(row)
        per_ad_topics[len(topics)] += 1
        for topic in set(topics):
            topic_counts[topic] += 1
        approval_counts[
            (row["adGroupAd"].get("policySummary") or {}).get(
                "approvalStatus", "ABSENT"
            )
        ] += 1

    topic_field = (
        "ad_group_ad.policy_summary.policy_topic_entries[].topic"
    )
    for topic, n in sorted(topic_counts.items()):
        fs.add(
            id=f"rsa.servable.policy_topic.{topic.lower()}.count",
            value=n,
            unit="count",
            scope=(
                f"{servable_scope} AND {topic_field} = '{topic}' (ads, not topic"
                " entries: an ad carrying the topic twice counts once. Computed"
                " from this run's own inventory pull, never a census carried"
                " from a skill file)"
            ),
            window=window,
            source=INVENTORY,
            basis="n/a",
        )

    fs.add(
        id="rsa.servable.policy_topic.any_trademark.count",
        value=sum(1 for r in scope.servable if has_trademark(r)),
        unit="count",
        scope=(
            f"{servable_scope} AND at least one {topic_field} CONTAINS"
            " 'TRADEMARK'. This is the VF-6 exclusion class itself, by substring"
            " and not by one topic string, so the count the exclusion rests on"
            " is citable directly"
        ),
        window=window,
        source=INVENTORY,
        basis="n/a",
    )
    fs.add(
        id="rsa.servable.policy_topic.distinct_topic.count",
        value=len(topic_counts),
        unit="count",
        scope=(
            f"{servable_scope} (distinct {topic_field} values observed in this"
            " run's pull)"
        ),
        window=window,
        source=INVENTORY,
        basis="n/a",
    )
    fs.add(
        id="rsa.servable.policy_topic.ad_with_any_topic.count",
        value=sum(n for slots, n in per_ad_topics.items() if slots >= 1),
        unit="count",
        scope=f"{servable_scope} AND the ad carries at least one policy topic",
        window=window,
        source=INVENTORY,
        basis="n/a",
    )
    fs.add(
        id="rsa.servable.policy_topic.ad_with_multiple_topics.count",
        value=sum(n for slots, n in per_ad_topics.items() if slots >= 2),
        unit="count",
        scope=f"{servable_scope} AND the ad carries two or more policy topics",
        window=window,
        source=INVENTORY,
        basis="n/a",
    )
    for status, n in sorted(approval_counts.items()):
        fs.add(
            id=f"rsa.servable.approval_status.{status.lower()}.count",
            value=n,
            unit="count",
            scope=(
                f"{servable_scope} AND"
                f" ad_group_ad.policy_summary.approval_status = '{status}'."
                " APPROVED_LIMITED is an approval status, not a defect: the ad"
                " runs with limits. Never report it as blocked or rejected"
                " (audit-ad-copy step 4)"
            ),
            window=window,
            source=INVENTORY,
            basis="n/a",
        )


def add_asset_label_counts(fs: FactSet, scope: InventoryScope, window: str) -> None:
    """Count RSA assets by performance_label, servable scope only.

    Source is the RSA definition inside `ad_group_ad`
    (`responsiveSearchAd.headlines[].assetPerformanceLabel`), not
    `ad_group_ad_asset_view`. Different resource, different scope, so these are
    a separate fact family from the asset-view label counts. Never merge them.
    """
    for asset_type, getter in (("headline", headlines), ("description", descriptions)):
        counts = collections.Counter()
        for row in scope.servable:
            for asset in getter(row):
                counts[asset.get("assetPerformanceLabel", "ABSENT")] += 1
        base_scope = (
            scope.scope
            + f" (assets of type {asset_type.upper()} defined on those ads, read"
            " from ad_group_ad.ad.responsive_search_ad, NOT"
            " ad_group_ad_asset_view)"
        )
        fs.add(
            id=f"rsa.servable.{asset_type}_asset.count",
            value=sum(counts.values()),
            unit="count",
            scope=base_scope,
            window=window,
            source=INVENTORY,
            basis="n/a",
        )
        for label in ASSET_LABELS:
            fs.add(
                id=(
                    f"rsa.servable.{asset_type}_asset.performance_label."
                    f"{label.lower()}.count"
                ),
                value=counts.get(label, 0),
                unit="count",
                scope=base_scope + f" AND asset_performance_label = '{label}'",
                window=window,
                source=INVENTORY,
                basis="n/a",
            )


# --------------------------------------------------------------------------
# Metrics facts (windowed)
# --------------------------------------------------------------------------


def add_spend(
    fs: FactSet, scope: InventoryScope, met: list[dict], window: str
) -> dict:
    """Total RSA spend, at scopes that differ only by ad group status.

    Returns `(real_metrics, selected_keys)`: per-(ad group, ad) real
    impressions so the asset section can print an ad's real impressions beside
    its inflated serve-credit denominator, and the serve-table selection so the
    same section knows which ads get per-headline detail.
    """
    inv_keys = {ad_key(r) for r in scope.rows}
    orphans = [ad_key(r) for r in met if ad_key(r) not in inv_keys]
    if orphans:
        raise BuildError(
            f"{METRICS}: {len(orphans)} rows have no matching row in "
            f"{INVENTORY} (e.g. {orphans[0]}). The two pulls are not the same "
            "population, so scoping spend by ad group status is not sound."
        )

    def cost(rows) -> int:
        return sum(int(r["metrics"].get("costMicros", 0) or 0) for r in rows)

    servable_keys = scope.servable_keys
    servable_rows = [r for r in met if ad_key(r) in servable_keys]
    non_servable_rows = [r for r in met if ad_key(r) not in servable_keys]
    servable_scope = scope.scope

    fs.add(
        id="rsa.spend.servable.total",
        value=micros_to_currency(cost(servable_rows)),
        unit="currency_cad",
        scope=servable_scope,
        window=window,
        source=METRICS,
        basis="n/a",
    )
    fs.add(
        id="rsa.impressions.servable.total",
        value=sum(int(r["metrics"].get("impressions", 0) or 0) for r in servable_rows),
        unit="impressions",
        scope=servable_scope,
        window=window,
        source=METRICS,
        basis="n/a",
    )
    fs.add(
        id="rsa.clicks.servable.total",
        value=sum(int(r["metrics"].get("clicks", 0) or 0) for r in servable_rows),
        unit="clicks",
        scope=servable_scope,
        window=window,
        source=METRICS,
        basis="n/a",
    )
    fs.add(
        id="rsa.servable.ad.count_with_metrics",
        value=len(servable_rows),
        unit="count",
        scope=servable_scope + " AND present in the metrics pull",
        window=window,
        source=METRICS,
        basis="n/a",
    )

    if scope.unfiltered_proven:
        fs.add(
            id="rsa.spend.unfiltered_ad_group_status.total",
            value=micros_to_currency(cost(met)),
            unit="currency_cad",
            scope=UNFILTERED_AD_GROUP_FILTER,
            window=window,
            source=METRICS,
            basis="n/a",
        )
        fs.add(
            id="rsa.spend.in_paused_or_removed_ad_group.total",
            value=micros_to_currency(cost(non_servable_rows)),
            unit="currency_cad",
            scope=PAUSED_AG_FILTER,
            window=window,
            source=METRICS,
            basis="n/a",
        )
    else:
        reason = S.unavailable(
            "no row sits in a PAUSED or REMOVED ad group in this pull, so a "
            "spend figure under the unfiltered-ad-group scope cannot be "
            "distinguished from the servable one."
        )
        for fact_id in (
            "rsa.spend.unfiltered_ad_group_status.total",
            "rsa.spend.in_paused_or_removed_ad_group.total",
        ):
            fs.add(
                id=fact_id,
                value=None,
                unit="currency_cad",
                scope=reason,
                window=window,
                source=METRICS,
                basis="n/a",
            )

    # VF-6 / step 4: the spend sitting behind the trademark exclusion. Step 4
    # says the exclusion list carries names, IDs and topics only until this fact
    # exists, because a spend column with no fact behind it is an untraceable
    # figure. Trademark ONLY - FREE_DESKTOP_SOFTWARE stays in scope.
    trademark_keys = trademark_restricted_keys(scope.servable)
    trademark_rows = [r for r in servable_rows if ad_key(r) in trademark_keys]
    joined_source = (
        f"{METRICS} (joined to {INVENTORY} on adGroupAd.resourceName for the "
        "policy topic)"
    )
    fs.add(
        id="rsa.spend.policy_restricted.trademark.total",
        value=micros_to_currency(cost(trademark_rows)),
        unit="currency_cad",
        scope=(
            servable_scope
            + " AND at least one topic in"
            " ad_group_ad.policy_summary.policy_topic_entries[] CONTAINS"
            " 'TRADEMARK'. Trademark only: FREE_DESKTOP_SOFTWARE and"
            " UNAPPROVED_SUBSTANCES are different restriction classes that stay"
            " in scope and are never bundled here (VF-6)"
        ),
        window=window,
        source=joined_source,
        basis="n/a",
    )
    fs.add(
        id="rsa.spend.policy_restricted.trademark.ad.count",
        value=len(trademark_rows),
        unit="count",
        scope=(
            servable_scope
            + " AND at least one policy topic CONTAINS 'TRADEMARK' AND present"
            " in the metrics pull (the ads the spend figure above covers)"
        ),
        window=window,
        source=joined_source,
        basis="n/a",
    )

    real = {}
    for row in servable_rows:
        real[ag_ad(row)] = {
            "impressions": int(row["metrics"].get("impressions", 0) or 0),
            "clicks": int(row["metrics"].get("clicks", 0) or 0),
            "cost_micros": int(row["metrics"].get("costMicros", 0) or 0),
        }

    add_per_ad_spend(fs, servable_rows, servable_scope, window)
    selected = add_serve_table_selection(fs, servable_rows, servable_scope, window)
    return real, selected


def add_per_ad_spend(
    fs: FactSet, servable_rows: list, servable_scope: str, window: str
) -> None:
    """30-day cost for each servable ad, keyed on the (ad group, ad) pair.

    `audit-ad-copy` step 4 and step 5 both want a per-ad cost: step 4 for the
    exclusion table's spend column, step 5 to choose which ads get a serve
    table. Without a fact per ad the report has to divide or re-read the raw
    JSON in prose, which step 9 forbids.

    Keyed on the pair, never on `ad.id` (VF-19): 33 ad ids in this account
    appear under more than one ad group, so a bare-id key makes the figure
    depend on dict insertion order.
    """
    for row in servable_rows:
        ag_id, ad_id = ag_ad(row)
        fs.add(
            id=f"rsa.spend.servable.adgroup.{ag_id}.ad.{ad_id}.total",
            value=micros_to_currency(int(row["metrics"].get("costMicros", 0) or 0)),
            unit="currency_cad",
            scope=(
                servable_scope
                + f" AND ad_group.id={ag_id} AND ad_group_ad.ad.id={ad_id}"
                " (this one ad in this one ad group; keyed on the pair per VF-19)"
            ),
            window=window,
            source=METRICS,
            basis="n/a",
        )


def add_serve_table_selection(
    fs: FactSet, servable_rows: list, servable_scope: str, window: str
) -> set:
    """Step 5's selection rule, and the ad count it produces.

    The skill requires the report to print the rule and the resulting ad count
    beside the scope filter, so two runs select comparably and nobody picks the
    set by eye. Both are emitted here so neither is counted in prose. Returns
    the selected `(ad_group.id, ad.id)` keys - since 2026-08-27 they also
    decide which ads get per-headline facts (the headline-facts diet).
    """
    def cost_cad(row: dict) -> Decimal:
        return micros_to_currency(int(row["metrics"].get("costMicros", 0) or 0))

    ranked = sorted(
        servable_rows,
        key=lambda r: (-cost_cad(r), ad_key(r)),
    )
    top_n_keys = {ad_key(r) for r in ranked[:SERVE_TABLE_TOP_N]}
    above_threshold_keys = {
        ad_key(r) for r in servable_rows if cost_cad(r) > SERVE_TABLE_MIN_COST_CAD
    }
    selected = top_n_keys | above_threshold_keys

    rule = (
        f"the top {SERVE_TABLE_TOP_N} servable RSAs by 30-day cost, plus any "
        f"other servable RSA above CAD {SERVE_TABLE_MIN_COST_CAD} of 30-day "
        "cost (audit-ad-copy step 5)"
    )
    fs.add(
        id="rsa.serve_table.selected_ad.count",
        value=len(selected),
        unit="count",
        scope=f"{servable_scope} AND selected by {rule}",
        window=window,
        source=METRICS,
        basis="n/a",
    )
    fs.add(
        id="rsa.serve_table.selection_rule.top_n",
        value=SERVE_TABLE_TOP_N,
        unit="count",
        scope=(
            f"the N of {rule}. A parameter of the selection rule, not a"
            " measurement of the account"
        ),
        window=window,
        source=METRICS,
        basis="n/a",
    )
    fs.add(
        id="rsa.serve_table.selection_rule.min_cost_cad",
        value=SERVE_TABLE_MIN_COST_CAD,
        unit="currency_cad",
        scope=(
            f"the cost floor of {rule}. A parameter of the selection rule, not a"
            " measurement of the account"
        ),
        window=window,
        source=METRICS,
        basis="n/a",
    )
    fs.add(
        id="rsa.serve_table.selected_ad.above_threshold_only.count",
        value=len(above_threshold_keys - top_n_keys),
        unit="count",
        scope=(
            f"{servable_scope} AND 30-day cost > CAD {SERVE_TABLE_MIN_COST_CAD}"
            f" AND NOT in the top {SERVE_TABLE_TOP_N} by cost (the ads the"
            " threshold half of the rule adds)"
        ),
        window=window,
        source=METRICS,
        basis="n/a",
    )
    # The asset section keys on the (ad_group.id, ad.id) pair, not the
    # resource name, so the selection is returned in that shape.
    return {ag_ad(r) for r in servable_rows if ad_key(r) in selected}


def add_conversions(fs: FactSet, conv: list[dict], window: str) -> None:
    """Conversions on the 8-action basis (VF-1). Never in a query with spend (VF-3)."""
    names = {r["segments"]["conversionActionName"] for r in conv}
    stray = sorted(names - set(EIGHT_ACTIONS))
    if stray:
        raise BuildError(
            f"{CONVERSIONS}: contains conversion actions outside the VF-1 eight: "
            f"{stray}. A fact labelled basis='8-action' would be wrong."
        )
    # The scope is DECLARED, not verified, and it says so. This pull selected no
    # status field at all (its field mask is campaign.name, adGroup.name,
    # adGroupAd.ad.id, segments.conversionActionName, metrics.conversions,
    # metrics.conversionsValue) and `_manifest.json` does not declare the file,
    # so there is nothing to check the filter against. Writing
    # `ad_group.status=ENABLED` here to make it match VF5_FULL_FILTER would be
    # inventing a label the data cannot support - the same error class the rest
    # of this script exists to catch. The honest consequence is that no
    # conversion fact may be related to an ad-level fact: the guard refuses on
    # scope, which is the guard working.
    scope = (
        "DECLARED, NOT VERIFIED: ad_group_ad.status=ENABLED AND "
        "campaign.status=ENABLED AND ad.type=RESPONSIVE_SEARCH_AD "
        "AND segments.conversion_action_name IN (the 8 VF-1 actions). "
        "ad_group.status is NOT part of this claim, and no status field was "
        f"saved in {CONVERSIONS}, so not one clause here is checkable from the "
        f"saved rows and {S.MANIFEST} does not declare the file (VF-17 rule 5)"
    )
    total = sum(float(r["metrics"].get("conversions", 0) or 0) for r in conv)
    fs.add(
        id="rsa.conversions.eight_action.total",
        value=round(total, 2),
        unit="conversions",
        scope=scope,
        window=window,
        source=CONVERSIONS,
        basis="8-action",
    )
    fs.add(
        id="rsa.conversions.eight_action.action_count",
        value=len(names),
        unit="count",
        scope=scope + " (distinct actions with at least one conversion)",
        window=window,
        source=CONVERSIONS,
        basis="8-action",
    )
    # No conversion-value fact. VF-2: those are assigned constants for
    # value-based bidding, not observed revenue, so a bare "value" fact invites
    # exactly the misreading VF-8 forbids.


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build(raw_dir: Path, detail_ad_ids: "tuple | list" = ()) -> FactSet:
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
    # Runs first: it is the check that refuses a raw directory holding two
    # differently-named copies of the asset pull with different contents.
    asset_name = problems.run(manifest.asset_view_name)

    inv = problems.run(load_results, raw_dir, INVENTORY, file=INVENTORY)
    met = problems.run(load_results, raw_dir, METRICS, file=METRICS)
    conv = problems.run(load_results, raw_dir, CONVERSIONS, file=CONVERSIONS)

    # Temporality is declared, never inferred (VF-17 rule 5).
    inv_window = manifest.window_for(INVENTORY, windowed_fallback=False)
    met_window = manifest.window_for(METRICS, windowed_fallback=True)
    conv_window = manifest.window_for(CONVERSIONS, windowed_fallback=True)

    trail: list[str] = []
    if not manifest.present:
        trail.append(
            f"{S.MANIFEST}: ABSENT from {raw_dir.name}. Temporality could not be "
            "declared for any file, so every window string says so (VF-17 "
            "rule 5). Inferring it from the field mask is what got 2 of 6 files "
            "wrong on 2026-08-19."
        )
    for name, rows in ((INVENTORY, inv), (METRICS, met), (CONVERSIONS, conv)):
        if rows is None:
            continue
        trail.append(manifest.audit_line(name))
        line = problems.run(manifest.check_row_count, name, len(rows),
                            file=name)
        if line:
            trail.append(line)

    # VF-19a. The manifest stores executable GAQL; a fact scope stores the
    # normalised label. Byte equality between the two is impossible, and
    # demanding it cost a run. The test is clause-for-clause after normalising:
    # strip quotes, rewrite ad_group_ad.ad.type to ad.type. A missing or extra
    # clause is a hard fail; a spelling difference the normaliser accounts for
    # is not. `segments.date DURING ...` is temporality, which VF-17 records in
    # `window`, so it is held out of the scope comparison and checked against
    # the declared temporality instead.
    for name in (INVENTORY, METRICS):
        line = problems.run(manifest.verify_filter, name, VF5_FULL_FILTER,
                            file=name)
        if line:
            trail.append(line)
    for name in sorted(manifest.files):
        line = problems.run(manifest.verify_declared_temporality, name,
                            file=name)
        if line:
            trail.append(line)
    undeclared = manifest.undeclared_files()
    if undeclared:
        trail.append(
            f"{S.MANIFEST}: {len(undeclared)} raw pull(s) on disk are NOT "
            f"declared in it: {', '.join(undeclared)}. Neither their filter nor "
            "their temporality was declared, so no fact may borrow a label from "
            "them (VF-17 rule 5)."
        )
    trail += manifest.asset_view_notes

    inv_clause_lines = problems.run(
        validate_clauses,
        INVENTORY,
        inv,
        [
                ("ad_group_ad.status = 'ENABLED'", _status_is("adGroupAd", "ENABLED")),
                (
                    "ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'",
                    lambda r: "responsiveSearchAd" in (r["adGroupAd"].get("ad") or {}),
                ),
                (
                    "campaign.advertising_channel_type = 'SEARCH'",
                    lambda r: (
                        None
                        if (r.get("campaign") or {}).get("advertisingChannelType") is None
                        else r["campaign"]["advertisingChannelType"] == "SEARCH"
                    ),
                ),
                ("campaign.status = 'ENABLED'", _status_is("campaign", "ENABLED")),
        ],
        file=INVENTORY,
    ) if inv is not None else None
    if inv_clause_lines:
        trail += [f"{INVENTORY}: {t}" for t in inv_clause_lines]
    met_clause_lines = problems.run(
        validate_clauses,
        METRICS,
        met,
        [
            ("ad_group_ad.status = 'ENABLED'", _status_is("adGroupAd", "ENABLED")),
            ("campaign.status = 'ENABLED'", _status_is("campaign", "ENABLED")),
            (
                "every row carries metrics.cost_micros",
                lambda r: "costMicros" in (r.get("metrics") or {}),
            ),
        ],
        file=METRICS,
    ) if met is not None else None
    if met_clause_lines:
        trail += [f"{METRICS}: {t}" for t in met_clause_lines]
    if inv is not None:
        problems.run(assert_unique_join_key, INVENTORY, inv, file=INVENTORY)
    if met is not None:
        problems.run(assert_unique_join_key, METRICS, met, file=METRICS)

    problems.raise_if_any(str(raw_dir))
    scope = read_inventory_scope(inv)
    trail.append(f"{INVENTORY}: ad_group.status provenance: {scope.servable_provenance}")
    for note in scope.notes:
        trail.append(f"{INVENTORY}: NOTE: {note}")

    fs = FactSet(
        meta={
            "skill": "audit-ad-copy",
            "customer_id": account_config.customer_id(),
            "currency": "CAD",
            "raw_dir": str(raw_dir),
            "generator": "tools/build_audit_ad_copy_facts.py",
            "manifest_present": manifest.present,
            "join_key": "adGroupAd.resourceName (adGroupId~adId), never ad.id",
            "asset_row_key": "(ad_group.id, ad.id, asset.id, field_type) - VF-4a",
            "windows": {
                INVENTORY: inv_window,
                METRICS: met_window,
                CONVERSIONS: conv_window,
            },
            "inputs": {
                name: (
                    {
                        "rows": len(
                            json.loads(
                                (raw_dir / name).read_text(encoding="utf-8")
                            )["data"].get("results")
                            or []
                        ),
                        "field_mask": field_mask(raw_dir, name),
                        "declared_temporality": manifest.declared_temporality(name),
                        "declared_filter": manifest.declared_filter(name),
                    }
                    if (raw_dir / name).exists()
                    else "absent"
                )
                for name in (INVENTORY, METRICS, CONVERSIONS)
                + ((asset_name,) if asset_name else (S.ASSET_VIEW,))
            },
            "declared_filter_audit": trail,
        }
    )

    add_counts(fs, scope, inv_window)
    add_asset_label_counts(fs, scope, inv_window)
    real_metrics, serve_table_keys = add_spend(fs, scope, met, met_window)
    add_conversions(fs, conv, conv_window)

    if asset_name is None:
        asset_facts.add_unavailable(fs, S.ASSET_VIEW, raw_dir, met_window)
        trail.append(
            f"{S.ASSET_VIEW}: ABSENT. Per-headline facts emitted as null with "
            "the reason."
        )
    else:
        asset_rows = load_results(raw_dir, asset_name)
        trail.append(manifest.audit_line(asset_name))
        line = manifest.check_row_count(asset_name, len(asset_rows))
        if line:
            trail.append(line)
        asset_window = manifest.window_for(asset_name, windowed_fallback=True)
        try:
            trail += asset_facts.add_asset_facts(
                fs,
                asset_rows=asset_rows,
                servable_inventory=scope.servable,
                real_metrics=real_metrics,
                source=asset_name,
                metrics_source=METRICS,
                inventory_source=INVENTORY,
                window=asset_window,
                metrics_window=met_window,
                inventory_window=inv_window,
                min_headlines_for_shares=MIN_HEADLINES_FOR_SHARES,
                detail_keys=serve_table_keys,
                extra_ad_ids=frozenset(str(a) for a in detail_ad_ids),
            )
        except asset_facts.AssetError as exc:
            raise BuildError(str(exc)) from exc

    return fs


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("raw_dir", help="google-ads/outputs/raw/YYYY-MM-DD")
    parser.add_argument(
        "--out",
        default=None,
        help="output path (default: <raw_dir>/audit-ad-copy-facts.json)",
    )
    parser.add_argument(
        "--print", action="store_true", help="print every fact to stdout"
    )
    parser.add_argument(
        "--ad",
        action="append",
        default=[],
        metavar="AD_ID",
        help=(
            "also emit full per-headline detail for this ad.id (repeatable). "
            "The default detail set is the serve-table selection; a deeper "
            "dive re-runs the builder with --ad rather than doing prose "
            "arithmetic over the raw pull"
        ),
    )
    args = parser.parse_args(argv)

    raw_dir = Path(args.raw_dir)
    if not raw_dir.is_dir():
        print(f"not a directory: {raw_dir}", file=sys.stderr)
        return 2
    try:
        fs = build(raw_dir, detail_ad_ids=args.ad)
    except (BuildError, S.ManifestError, gaql.FilterMismatch) as exc:
        print(f"REFUSED to write a facts file.\n{exc}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else raw_dir / "audit-ad-copy-facts.json"
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
