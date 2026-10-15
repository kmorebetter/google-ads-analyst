"""Per-headline facts from `ad_group_ad_asset_view` (VERIFICATION-RULES VF-4, 4a, 4b).

This is the most dangerous pull in the pack, so the rules are enforced here in
code rather than left to a reader.

**VF-4a, the key.** Rows are keyed on `(ad_group.id, ad.id, asset.id,
field_type)`, equivalently on `ad_group_ad_asset_view.resource_name`. The pull
carries no status filter, so one Ad entity is returned under every ad group it is
linked to. Ad 620000000001 returns 140 rows across 5 ad groups; restricted to one
ad group and HEADLINE it is 22. Keying on `ad.id` alone silently multiplies rows
and, in the first version of this harness, crashed on a duplicate fact id. Every
fact id here names the ad group **and** the ad.

**VF-4b, the intersect.** Serve data is a window. The ad is a current state. A
30-day pull returns assets that served during the window including ones since
removed. Three outcomes, kept strictly apart:

  in inventory and serving  -> rank it
  in inventory, zero serves -> dead slot, the actionable finding
  serving, not in inventory -> history only, never a live problem

That last line is VF-17: a removed `GDRP` headline was reported as live on six
ads because a windowed serve pull still showed its impressions.

**The text join.** The inventory pull carries no asset IDs, only text, so the
intersect can only match on text. That is not the "never group by asset text" of
VF-4a: serve rows are grouped on the four-part tuple, and text is used solely to
ask "is this text still on the ad", inside one (ad group, ad). The build aborts
if two distinct asset IDs ever carry the same text inside one ad, because the
match would then be ambiguous.

**Never summed, except one controlled case.** Google credits every asset in a
serve with the whole ad's metrics, so asset impressions may not be added up
(VF-4 rule 1). The single exception is a within-ad denominator for a
share-of-serve percentage. It carries the unit `serve_credits_within_ad`, which
`FactSet.sum()` refuses, and its scope says it is not the ad's impressions. The
ad's real impressions are emitted beside it so the two can be seen together.

Python 3 standard library only.
"""

from __future__ import annotations

import collections
from pathlib import Path

import scopes as S
from facts import FactSet

HEADLINE = "HEADLINE"


class AssetError(Exception):
    """The asset pull cannot be scoped or joined honestly."""


# --------------------------------------------------------------------------
# Keys and indexes
# --------------------------------------------------------------------------


def four_part_key(row: dict) -> tuple:
    """`(ad_group.id, ad.id, asset.id, field_type)` — the VF-4a row key."""
    ag_id, ad_id = S.ag_ad_ids(row)
    return (
        ag_id,
        ad_id,
        row["asset"]["id"],
        row["adGroupAdAssetView"]["fieldType"],
    )


#: `(ad_group.id, ad.id)` — the unit of comparison (VF-4 rule 4).
ag_ad = S.ag_ad_ids


def text_of(row: dict) -> str:
    return ((row.get("asset") or {}).get("textAsset") or {}).get("text", "")


def impressions_of(row: dict) -> int:
    return int((row.get("metrics") or {}).get("impressions", 0) or 0)


def clicks_of(row: dict) -> int:
    return int((row.get("metrics") or {}).get("clicks", 0) or 0)


def label_of(row: dict) -> str:
    return (row.get("adGroupAdAssetView") or {}).get("performanceLabel", "ABSENT")


def assert_key_unique(filename: str, rows: list[dict]) -> None:
    """VF-4a. Refuse to proceed if the four-part key is not unique."""
    keys = [four_part_key(row) for row in rows]
    dupes = [k for k, n in collections.Counter(keys).items() if n > 1]
    if dupes:
        raise AssetError(
            f"{filename}: {len(dupes)} duplicate (ad_group.id, ad.id, asset.id, "
            f"field_type) keys, e.g. {dupes[0]}. The pull is segmented by "
            "something not in the key; aggregate before ranking."
        )
    names = [
        (row.get("adGroupAdAssetView") or {}).get("resourceName") for row in rows
    ]
    if all(names) and len(set(names)) != len(names):
        raise AssetError(
            f"{filename}: ad_group_ad_asset_view.resource_name is not unique."
        )


def live_headlines(servable_inventory: list[dict]) -> dict:
    """`(ad_group.id, ad.id)` -> the headline texts on the ad right now."""
    out: dict = {}
    for row in servable_inventory:
        key = S.ag_ad_ids(row)
        rsa = (row["adGroupAd"].get("ad") or {}).get("responsiveSearchAd") or {}
        texts = [h.get("text", "") for h in (rsa.get("headlines") or [])]
        if key in out:
            raise AssetError(
                f"{key} appears twice in the inventory pull. One ad in one ad "
                "group is one row."
            )
        out[key] = texts
    return out


def headline_serves(asset_rows: list[dict]) -> dict:
    """`(ad_group.id, ad.id)` -> the HEADLINE serve rows for it."""
    out: dict = collections.defaultdict(list)
    for row in asset_rows:
        if (row.get("adGroupAdAssetView") or {}).get("fieldType") != HEADLINE:
            continue
        out[ag_ad(row)].append(row)
    return dict(out)


def assert_text_unambiguous(key: tuple, rows: list[dict]) -> None:
    """The text join is only sound while text is unique inside one ad."""
    counts = collections.Counter(text_of(r) for r in rows)
    ambiguous = {t: n for t, n in counts.items() if n > 1}
    if ambiguous:
        text, n = next(iter(ambiguous.items()))
        raise AssetError(
            f"ad group {key[0]} ad {key[1]}: headline text {text!r} appears on "
            f"{n} distinct asset IDs in the serve data. The inventory pull "
            "carries no asset IDs, so the current-state intersect can only "
            "match on text, and this makes that match ambiguous. Add asset IDs "
            "to the inventory pull before ranking this ad."
        )


# --------------------------------------------------------------------------
# Facts
# --------------------------------------------------------------------------


def _prefix(key: tuple) -> str:
    return f"asset_view.adgroup.{key[0]}.ad.{key[1]}.headline"


def _ad_scope(key: tuple) -> str:
    return (
        S.ASSET_RANKING_FILTER
        + f" AND ad_group.id={key[0]} AND ad_group_ad.ad.id={key[1]}"
        f" ({S.WITHIN_AD_ONLY})"
    )


def add_unavailable(fs: FactSet, expected_name: str, raw_dir: Path, window: str) -> None:
    """No asset pull on disk. Say so in facts rather than omitting them."""
    reason = S.unavailable(
        f"no asset pull in {raw_dir.name}. Expected {S.ASSET_VIEW_CANONICAL}, "
        "the one canonical name. Per-headline serve rankings cannot be "
        "recomputed from the other saved files. Re-pull it and re-run this "
        "script. Wanted scope: " + S.ASSET_RANKING_FILTER
    )
    for fact_id, unit in (
        ("asset_view.headline.serve_ranking", "impressions_asset_credited"),
        ("asset_view.per_ad_detail.rule", "text"),
        ("asset_view.per_ad_detail.ad.count", "count"),
        ("asset_view.per_ad_detail.excluded_ad.count", "count"),
        ("asset_view.serve_row.count", "count"),
        ("asset_view.ranked.ad.count", "count"),
        ("asset_view.ranked.headline.count", "count"),
        ("asset_view.live_headline.slot.count", "count"),
        ("asset_view.dead_slot.total", "count"),
        ("asset_view.dead_slot.ads_affected", "count"),
        ("asset_view.dead_slot.ads_clean", "count"),
        ("asset_view.removed_but_served.row.count", "count"),
        ("asset_view.fan_out.ad_id_in_multiple_ad_groups.count", "count"),
    ):
        fs.add(
            id=fact_id,
            value=None,
            unit=unit,
            scope=reason,
            window=window,
            source=f"{expected_name} (absent)",
            basis=S.ASSET_VIEW_BASIS,
        )
    for label in S.ASSET_LABELS:
        fs.add(
            id=f"asset_view.headline.performance_label.{label.lower()}.count",
            value=None,
            unit="count",
            scope=reason,
            window=window,
            source=f"{expected_name} (absent)",
            basis=S.ASSET_VIEW_BASIS,
        )


def add_account_facts(
    fs: FactSet,
    *,
    asset_rows: list[dict],
    source: str,
    window: str,
    inventory_source: str,
    inventory_window: str,
    totals: dict,
) -> None:
    all_headline_rows = [
        r
        for r in asset_rows
        if (r.get("adGroupAdAssetView") or {}).get("fieldType") == HEADLINE
    ]
    by_ad_id: dict = collections.defaultdict(set)
    for row in asset_rows:
        ag_id, ad_id = S.ag_ad_ids(row)
        by_ad_id[ad_id].add(ag_id)
    fan_out = {k: len(v) for k, v in by_ad_id.items() if len(v) > 1}

    fs.add(
        id="asset_view.serve_row.count",
        value=len(all_headline_rows),
        unit="count",
        scope=S.ASSET_PULL_FILTER + " AND field_type='HEADLINE' (BEFORE any join)",
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    fs.add(
        id="asset_view.serve_row.ad_group.count",
        value=len({S.ag_ad_ids(r)[0] for r in asset_rows}),
        unit="count",
        scope=S.ASSET_PULL_FILTER + " (distinct ad groups, BEFORE any join)",
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    # VF-4a evidence, kept as a fact so the fan-out is visible not folklore.
    fs.add(
        id="asset_view.fan_out.ad_id_in_multiple_ad_groups.count",
        value=len(fan_out),
        unit="count",
        scope=(
            S.ASSET_PULL_FILTER
            + " (distinct ad_group_ad.ad.id values returned under more than one "
            "ad_group.id - proof that ad.id alone is not a key, VF-4a)"
        ),
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    fs.add(
        id="asset_view.fan_out.max_ad_groups_per_ad_id",
        value=max(fan_out.values()) if fan_out else 0,
        unit="count",
        scope=(
            S.ASSET_PULL_FILTER
            + " (the largest number of ad groups any one ad_group_ad.ad.id "
            "appears under, VF-4a)"
        ),
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )

    # Counting dead slots across ads is NOT the forbidden sum of VF-4 rule 1.
    # Rule 1 forbids adding per-asset *metrics* (impressions, clicks,
    # conversions), because Google credits every asset in a serve with the whole
    # ad's numbers, so the total overstates reality by ~3.4x. These facts count
    # slots, not credited metrics: each live headline contributes exactly 1 to
    # the tally regardless of how it served, so there is nothing to inflate.
    # They carry unit `count`, never anything ending `_asset_credited`, and
    # FactSet.sum() therefore has no reason to block them.
    why_counting_is_safe = (
        ". A COUNT of slots, not a sum of credited metrics: each live headline "
        "contributes 1 whatever its serve count, so the VF-4 rule 1 inflation "
        "cannot apply. Never add this to, or compare it against, an "
        "impressions figure"
    )
    joined = S.ASSET_RANKING_FILTER
    for fact_id, value, scope_suffix in (
        (
            # Not "pairs ranked". This is every servable (ad_group.id, ad.id)
            # pair the intersect processed, including ads where every live
            # headline turned out to be a dead slot. On 2026-08-19 that was 6 of
            # the 61, so a label reading "ranked" overstated by 6.
            "asset_view.ranked.ad.count",
            totals["ads"],
            " (distinct (ad_group.id, ad.id) pairs PROCESSED by the intersect -"
            " every servable ad, including ads on which no live headline took a"
            " single impression. NOT a count of ads that produced a ranking)",
        ),
        # The canonical account-level dead-slot facts. A report's headline
        # number is one of these two, so they use the same `dead_slot`
        # vocabulary as the per-ad facts. Without that, a report author looking
        # for the aggregate searched for "dead_slot", found only per-ad ids, and
        # concluded the aggregate did not exist.
        (
            "asset_view.dead_slot.total",
            totals["dead_slots"],
            " AND metrics.impressions = 0 (DEAD SLOTS: headline slots on the ad"
            " now that took ZERO IMPRESSIONS in the window - the actionable"
            " finding, VF-4b. A slot with a serve row of zero impressions counts"
            " here; on 2026-08-19 every dead slot was of that kind, and none was"
            " missing a serve row)" + why_counting_is_safe,
        ),
        (
            "asset_view.dead_slot.ads_affected",
            totals["dead_slot_ads"],
            " AND metrics.impressions = 0 (distinct ads holding at least one"
            " dead slot)" + why_counting_is_safe,
        ),
        (
            "asset_view.dead_slot.ads_clean",
            totals["ads"] - totals["dead_slot_ads"],
            " (ads on which every live headline took AT LEAST ONE IMPRESSION."
            " Not 'served at least once': a slot can hold a serve row and still"
            " have zero impressions, and those ads are counted as affected, not"
            " clean)" + why_counting_is_safe,
        ),
        (
            # The label used to read "with at least one serve row". That is a
            # different population and it was wrong by 126. Verified 2026-08-19:
            # all 878 live headline slots HAVE a serve row, so a
            # serve-row-based count would be 878. 752 is the slots with at
            # least one IMPRESSION (878 - 126 dead). Anyone who read this fact
            # as serve-row coverage was wrong by exactly the dead-slot total.
            "asset_view.ranked.headline.count",
            totals["ranked"],
            " AND metrics.impressions > 0 (live headline slots that took AT"
            " LEAST ONE IMPRESSION in the window. NOT 'has a serve row': on"
            " 2026-08-19 all 878 live slots had a serve row and only 752 had an"
            " impression. Do not subtract this from"
            " asset_view.live_headline.slot.count - that fact is current state"
            " and this one is a window/current-state join, so the guard refuses"
            " the comparison. Quote asset_view.dead_slot.total instead)"
            + why_counting_is_safe,
        ),
    ):
        fs.add(
            id=fact_id,
            value=value,
            unit="count",
            scope=joined + scope_suffix,
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )

    # A current-state fact, so it is labelled as one. Its value is read entirely
    # from the undated inventory pull - the asset pull contributes nothing to it -
    # and it used to carry the asset pull's 30-day window and filename. That is a
    # VF-17 rule 4 violation: a scope saying "read from the undated current
    # inventory" beside a date-range window is self-contradictory, and the guard
    # could not then refuse to compare it against a windowed metric.
    fs.add(
        id="asset_view.live_headline.slot.count",
        value=totals["live_slots"],
        unit="count",
        scope=(
            S.VF5_FULL_FILTER
            + " (headline slots defined on those ads right now, counted from"
            " ad_group_ad.ad.responsive_search_ad.headlines in the UNDATED"
            " inventory pull. The asset pull contributes nothing to this figure)"
            + why_counting_is_safe
        ),
        window=inventory_window,
        source=inventory_source,
        basis="n/a",
    )

    fs.add(
        id="asset_view.removed_but_served.row.count",
        value=totals["removed"],
        unit="count",
        scope=(
            S.ASSET_PULL_FILTER
            + " AND field_type='HEADLINE' AND the (ad_group.id, ad.id) is in the "
            "servable inventory AND the text is NOT on the ad now. "
            + S.HISTORY_ONLY
        ),
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    # Same defect as `ranked.headline.count`, in a second family, off by the same
    # 126. These are counted over `ranked_rows`, which is filtered to
    # impressions > 0, so they cover the 752 ranked headlines and not the 878
    # joined slots the bare `joined` scope describes. The six values sum to
    # exactly `asset_view.ranked.headline.count`.
    #
    # Three populations share the words "headline performance label" and they are
    # all different. Say which one this is, every time:
    #   this family     752 - live slots with at least one impression
    #   the joined set  878 - every live slot, ranked or dead
    #   step 5 rule 5   4,120 raw HEADLINE serve rows, account-wide, no join
    for label in S.ASSET_LABELS:
        fs.add(
            id=f"asset_view.headline.performance_label.{label.lower()}.count",
            value=totals["labels"].get(label, 0),
            unit="count",
            scope=(
                joined
                + f" AND metrics.impressions > 0 AND"
                f" ad_group_ad_asset_view.performance_label = '{label}'"
                " (context only, never the ranking key - VF-4 rule 5). The"
                " population is the RANKED live headlines - those with AT LEAST"
                " ONE IMPRESSION - so these six counts sum to"
                " asset_view.ranked.headline.count and NOT to"
                " asset_view.live_headline.slot.count. The dead slots' labels are"
                " counted nowhere. This is also a different population from the"
                " account-wide HEADLINE label census, which is over every raw"
                " serve row with no join at all"
            ),
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )


def add_ad_facts(
    fs: FactSet,
    key: tuple,
    *,
    live_texts: list[str],
    serve_rows: list[dict],
    real: dict | None,
    source: str,
    metrics_source: str,
    inventory_source: str,
    window: str,
    metrics_window: str,
    inventory_window: str,
    min_headlines_for_shares: int,
) -> dict:
    """Rank one ad's live headlines and record its dead slots and its history."""
    assert_text_unambiguous(key, serve_rows)
    prefix = _prefix(key)
    scope = _ad_scope(key)
    live_set = set(live_texts)
    by_text = {text_of(r): r for r in serve_rows}

    ranked_rows = [by_text[t] for t in live_texts if t in by_text]
    dead_texts = [t for t in live_texts if t not in by_text or impressions_of(by_text[t]) == 0]
    # A live headline with a serve row of zero impressions is a dead slot too,
    # so it must not also be ranked.
    ranked_rows = [r for r in ranked_rows if impressions_of(r) > 0]
    removed_rows = [r for r in serve_rows if text_of(r) not in live_set]

    ranked_rows.sort(key=lambda r: (-impressions_of(r), text_of(r)))

    # Current state, not a window. Read from the undated inventory pull, so it
    # carries the inventory's window and filename (VF-17 rule 4).
    fs.add(
        id=f"{prefix}.live.count",
        value=len(live_texts),
        unit="count",
        scope=(
            S.VF5_FULL_FILTER
            + f" AND ad_group.id={key[0]} AND ad_group_ad.ad.id={key[1]}"
            " (headline slots on the ad right now, counted from"
            " ad_group_ad.ad.responsive_search_ad.headlines in the UNDATED"
            " inventory pull)"
        ),
        window=inventory_window,
        source=inventory_source,
        basis="n/a",
    )
    fs.add(
        id=f"{prefix}.serve_row.count",
        value=len(serve_rows),
        unit="count",
        scope=(
            S.ASSET_PULL_FILTER
            + f" AND field_type='HEADLINE' AND ad_group.id={key[0]} AND "
            f"ad_group_ad.ad.id={key[1]} (BEFORE the current-state intersect, so "
            "this can exceed the live slot count - VF-4b)"
        ),
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    fs.add(
        id=f"{prefix}.ranked.count",
        value=len(ranked_rows),
        unit="count",
        scope=scope + " AND metrics.impressions > 0",
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )

    credits = sum(impressions_of(r) for r in ranked_rows)
    fs.add(
        id=f"{prefix}.serve_credits.total",
        value=credits,
        unit="serve_credits_within_ad",
        scope=scope + ". " + S.DENOMINATOR_CAVEAT,
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    # The ad's real impressions, from the ad-level metrics pull, so nobody reads
    # the serve-credit total as an impression count. Different unit and scope, so
    # the guard refuses to relate the two.
    if real is not None:
        fs.add(
            id=f"asset_view.adgroup.{key[0]}.ad.{key[1]}.real_impressions",
            value=real["impressions"],
            unit="impressions",
            scope=(
                S.VF5_FULL_FILTER
                + f" AND ad_group.id={key[0]} AND ad_group_ad.ad.id={key[1]}"
                " (the ad's OWN impressions from the ad-level metrics pull. "
                "Always lower than the serve-credit total above, because every "
                "asset in a serve is credited with the whole ad's metrics - "
                "VF-4)"
            ),
            window=metrics_window,
            source=metrics_source,
            basis="n/a",
        )

    for rank, row in enumerate(ranked_rows, start=1):
        stem = f"{prefix}.rank_{rank}"
        fs.add(
            id=f"{stem}.impressions",
            value=impressions_of(row),
            unit="impressions_asset_credited",
            scope=scope,
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )
        fs.add(
            id=f"{stem}.clicks",
            value=clicks_of(row),
            unit="clicks_asset_credited",
            scope=scope,
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )
        fs.add(
            id=f"{stem}.text",
            value=text_of(row),
            unit="text",
            scope=scope,
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )
        fs.add(
            id=f"{stem}.asset_id",
            value=row["asset"]["id"],
            unit="text",
            scope=scope + " (asset.id, the third part of the VF-4a key)",
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )
        fs.add(
            id=f"{stem}.performance_label",
            value=label_of(row),
            unit="text",
            scope=scope + " (context only, never the ranking key - VF-4 rule 5)",
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )
        # CTR within one ad. VF-4 rule 3 forbids per-asset conversions, CPA,
        # ROAS and conversion value but explicitly permits CTR inside a single
        # ad, and numerator and denominator here come from the same serve row.
        # `audit-ad-copy` step 5 bans the table's CTR column until this fact
        # exists, because dividing the two in the report body is the prose
        # arithmetic step 9 forbids.
        fs.add(
            id=f"{stem}.ctr",
            value=round(100.0 * clicks_of(row) / impressions_of(row), 2),
            unit="percent_ctr_within_ad",
            scope=(
                scope
                + ". clicks / impressions on this one serve row, as a"
                " percentage. Both numbers are asset-credited, so this is the"
                " CTR of the ad serves this asset was present in, not the"
                " asset's own CTR. Comparable only against another rank in THIS"
                " ad (VF-4 rules 2, 3 and 4)"
            ),
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )
        # Share of this ad's live serve credits. `audit-ad-copy` step 8 bans a
        # per-rank serve share until this fact exists. The denominator is the
        # same ad's own credit total, which is the one controlled sum VF-4
        # rule 1 permits.
        fs.add(
            id=f"{stem}.share",
            # `credits` is the sum over `ranked_rows`, every one of which has
            # impressions > 0, so it is always positive inside this loop. No
            # null branch: a null that cannot occur is noise in a file where
            # every other null carries a reason.
            value=round(100.0 * impressions_of(row) / credits, 1),
            unit="percent_of_ad_serve_credits",
            scope=(
                scope
                + ". This rank's serve credits as a percentage of THIS ad's live"
                " serve-credit total. Not a share of impressions: the"
                " denominator is inflated by VF-4 rule 1 credit-to-every-asset,"
                " and both parts are inflated the same way, which is what makes"
                " the ratio meaningful inside one ad and meaningless outside it"
            ),
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )

    # Share of serve inside this one ad. Legitimate only because the denominator
    # and the parts come from the same ad.
    if credits > 0 and len(ranked_rows) >= min_headlines_for_shares:
        top3 = sum(impressions_of(r) for r in ranked_rows[:3])
        bottom5 = sum(impressions_of(r) for r in ranked_rows[-5:])
        for fact_id, part, label in (
            (f"{prefix}.top_3.share", top3, "top 3 live headlines by serves"),
            (f"{prefix}.bottom_5.share", bottom5, "bottom 5 live headlines by serves"),
        ):
            fs.add(
                id=fact_id,
                value=round(100.0 * part / credits, 1),
                unit="percent_of_ad_serve_credits",
                scope=(
                    scope
                    + f". The {label}, as a percentage of this ad's live serve "
                    "credits. Not a share of impressions"
                ),
                window=window,
                source=source,
                basis=S.ASSET_VIEW_BASIS,
            )

    # Dead slots: on the ad now, no serves. The actionable finding.
    fs.add(
        id=f"{prefix}.dead_slot.count",
        value=len(dead_texts),
        unit="count",
        scope=(
            scope
            + " AND metrics.impressions = 0 (DEAD SLOT: on the ad now and not "
            "serving. This is the actionable finding - VF-4b)"
        ),
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    for i, text in enumerate(sorted(dead_texts), start=1):
        fs.add(
            id=f"{prefix}.dead_slot.{i}.text",
            value=text,
            unit="text",
            scope=scope + " AND metrics.impressions = 0 (DEAD SLOT)",
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )

    # Removed but served: history only. Never a live problem.
    fs.add(
        id=f"{prefix}.removed_but_served.count",
        value=len(removed_rows),
        unit="count",
        scope=(
            S.ASSET_PULL_FILTER
            + f" AND field_type='HEADLINE' AND ad_group.id={key[0]} AND "
            f"ad_group_ad.ad.id={key[1]} AND the text is NOT on the ad now. "
            + S.HISTORY_ONLY
        ),
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    for i, row in enumerate(
        sorted(removed_rows, key=lambda r: (-impressions_of(r), text_of(r))), start=1
    ):
        history_scope = (
            S.ASSET_PULL_FILTER
            + f" AND field_type='HEADLINE' AND ad_group.id={key[0]} AND "
            f"ad_group_ad.ad.id={key[1]} AND asset.id={row['asset']['id']} AND "
            "the text is NOT on the ad now. " + S.HISTORY_ONLY
        )
        fs.add(
            id=f"{prefix}.removed_but_served.{i}.text",
            value=text_of(row),
            unit="text",
            scope=history_scope,
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )
        fs.add(
            id=f"{prefix}.removed_but_served.{i}.impressions",
            value=impressions_of(row),
            unit="impressions_asset_credited",
            scope=history_scope,
            window=window,
            source=source,
            basis=S.ASSET_VIEW_BASIS,
        )

    return {
        "live": len(live_texts),
        "serve_rows": len(serve_rows),
        "ranked": len(ranked_rows),
        "dead": len(dead_texts),
        "removed": len(removed_rows),
        "credits": credits,
        "labels": collections.Counter(label_of(r) for r in ranked_rows),
    }


def add_asset_facts(
    fs: FactSet,
    *,
    asset_rows: list[dict],
    servable_inventory: list[dict],
    real_metrics: dict,
    source: str,
    metrics_source: str,
    inventory_source: str,
    window: str,
    metrics_window: str,
    inventory_window: str,
    min_headlines_for_shares: int,
    detail_keys: "set | None" = None,
    extra_ad_ids: frozenset = frozenset(),
) -> list[str]:
    """Every asset fact. Returns lines for the facts file's audit trail.

    The headline-facts diet (2026-08-27): when `detail_keys` is given, per-ad
    and per-rank facts are emitted only for those `(ad_group.id, ad.id)` keys
    (the serve-table selection) plus any ad whose `ad.id` is in
    `extra_ad_ids` (the builder's repeatable `--ad` flag). Every ad is still
    PROCESSED - the account totals, the dead-slot census and the
    text-collision integrity check cover the full servable set - but detail
    for an unselected ad is computed and discarded rather than written.
    Exhaustive emission built a facts file dense enough for fabricated
    numbers to match by coincidence (both 2026-08 escapes), and VF-15a rule 6
    already said: do not emit a fact per entity. The counts of covered and
    excluded ads are emitted as facts, so the cap is never silent.
    `detail_keys=None` emits everything (the pre-diet behaviour)."""
    assert_key_unique(source, asset_rows)
    live = live_headlines(servable_inventory)
    serves = headline_serves(asset_rows)

    totals = {
        "ads": 0,
        "ranked_ads": 0,
        "live_slots": 0,
        "ranked": 0,
        "dead_slots": 0,
        "dead_slot_ads": 0,
        "removed": 0,
        "labels": collections.Counter(),
    }
    no_serve_keys = []
    detailed, undetailed = [], []
    for key in sorted(live):
        rows = serves.get(key, [])
        if not rows:
            no_serve_keys.append(key)
        emit = (
            detail_keys is None
            or key in detail_keys
            or str(key[1]) in extra_ad_ids
        )
        (detailed if emit else undetailed).append(key)
        # An unselected ad is processed identically - same integrity checks,
        # same summary feeding the account totals - into a discarded FactSet,
        # so selection can never change an account-level number.
        summary = add_ad_facts(
            fs if emit else FactSet(meta={}),
            key,
            live_texts=live[key],
            serve_rows=rows,
            real=real_metrics.get(key),
            source=source,
            metrics_source=metrics_source,
            inventory_source=inventory_source,
            window=window,
            metrics_window=metrics_window,
            inventory_window=inventory_window,
            min_headlines_for_shares=min_headlines_for_shares,
        )
        totals["ads"] += 1
        totals["ranked_ads"] += 1 if summary["ranked"] else 0
        totals["live_slots"] += summary["live"]
        totals["ranked"] += summary["ranked"]
        totals["dead_slots"] += summary["dead"]
        totals["dead_slot_ads"] += 1 if summary["dead"] else 0
        totals["removed"] += summary["removed"]
        totals["labels"].update(summary["labels"])

    add_account_facts(
        fs,
        asset_rows=asset_rows,
        source=source,
        window=window,
        inventory_source=inventory_source,
        inventory_window=inventory_window,
        totals=totals,
    )

    if detail_keys is None:
        detail_rule = (
            "every servable ad (no selection was passed; pre-diet behaviour)"
        )
    else:
        detail_rule = (
            "the serve-table selection (audit-ad-copy step 5: top N servable "
            "RSAs by 30-day cost plus any above the cost floor - the exact N "
            "and floor are facts: rsa.serve_table.selection_rule.*)"
            + (
                f", plus {len(extra_ad_ids)} ad id(s) named with --ad"
                if extra_ad_ids
                else ""
            )
        )
    detail_scope = (
        S.ASSET_RANKING_FILTER
        + " (the headline-facts diet: per-ad and per-rank facts exist only "
        "for these ads; account totals and the dead-slot census still cover "
        "every servable ad. A deeper dive re-runs the builder with "
        "--ad <ad_id>, never prose arithmetic over the raw pull)"
    )
    fs.add(
        id="asset_view.per_ad_detail.rule",
        value=detail_rule,
        unit="text",
        scope=detail_scope,
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    fs.add(
        id="asset_view.per_ad_detail.ad.count",
        value=len(detailed),
        unit="count",
        scope=detail_scope + ". Ads WITH per-headline detail in this file",
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )
    fs.add(
        id="asset_view.per_ad_detail.excluded_ad.count",
        value=len(undetailed),
        unit="count",
        scope=(
            detail_scope
            + ". Servable ads PROCESSED for the account totals but holding no "
            "per-headline facts here - re-run with --ad to detail one"
        ),
        window=window,
        source=source,
        basis=S.ASSET_VIEW_BASIS,
    )

    trail = [
        f"{source}: four-part key (ad_group.id, ad.id, asset.id, field_type) "
        f"VERIFIED unique on all {len(asset_rows)} rows (VF-4a)",
        f"{source}: intersected with the undated inventory pull on "
        f"(ad_group.id, ad.id, headline text) per VF-4b: {totals['ads']} "
        f"servable ads processed of which {totals['ranked_ads']} produced at "
        f"least one ranked headline, {totals['live_slots']} live headline slots, "
        f"{totals['dead_slots']} dead slots across {totals['dead_slot_ads']} "
        f"ads, {totals['removed']} serve rows excluded as already removed",
    ]
    if no_serve_keys:
        trail.append(
            f"{source}: {len(no_serve_keys)} servable ads have no HEADLINE serve "
            "row at all; every live slot on them counts as a dead slot"
        )
    trail.append(
        f"{source}: per-headline detail emitted for {len(detailed)} ad(s), "
        f"withheld for {len(undetailed)} (headline-facts diet; rule: "
        f"{detail_rule})"
    )
    return trail
