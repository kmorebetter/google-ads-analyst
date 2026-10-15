#!/usr/bin/env python3
"""Tests for `build_audit_extensions_facts.py`.

Two kinds of test here.

**Synthetic fixtures** pin the behaviour that a real pull cannot demonstrate on
demand: the shared-asset double count, the campaign-status join, the
three-state coverage resolution, and the refusals. Each fixture is the smallest
raw directory that exercises one rule.

**Live-file tests** run against `google-ads/outputs/raw/2026-08-20` when it is
present and skip when it is not, so the suite still passes in a checkout
without the raw pulls. They are what proves the numbers in the skill's own
worked example, including that 410 sitelink rows were 410 *attachments*.

Run: `python3 tools/test_audit_extensions_facts.py`
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest

import pulls
import testsupport as TS
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_audit_extensions_facts as builder  # noqa: E402
import facts as F  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

def _resolve_raw(date):
    """Raw pull dir, or a definitely-absent path so skipUnless skips cleanly.

    Runtime pulls are gitignored, so a fresh clone has none. That is a skip,
    never a failure, and never an empty-data pass.
    """
    try:
        return pulls.raw_dir(date)
    except FileNotFoundError:
        return Path("/nonexistent/google-ads/outputs/raw") / date

LIVE_RAW = _resolve_raw("2026-08-20")


# --------------------------------------------------------------------------
# Fixture construction
# --------------------------------------------------------------------------


def _pull(field_mask: str, results: list) -> dict:
    return {"success": True, "data": {"fieldMask": field_mask, "results": results}}


def campaign_asset_row(campaign_id: str, asset_id: str, field_type: str) -> dict:
    return {
        "campaign": {"id": campaign_id},
        "asset": {"id": asset_id, "type": field_type},
        "campaignAsset": {"fieldType": field_type, "status": "ENABLED"},
    }


def customer_asset_row(asset_id: str, field_type: str) -> dict:
    return {
        "asset": {"id": asset_id, "type": field_type},
        "customerAsset": {"fieldType": field_type, "status": "ENABLED"},
    }


def campaign_row(campaign_id: str, name: str, channel: str = "SEARCH") -> dict:
    return {
        "campaign": {
            "id": campaign_id,
            "name": name,
            "status": "ENABLED",
            "advertisingChannelType": channel,
        }
    }


def serve_row(campaign_id: str, field_type: str, impressions: int, clicks: int) -> dict:
    return {
        "campaign": {"id": campaign_id},
        "assetFieldTypeView": {"fieldType": field_type},
        "metrics": {
            "impressions": str(impressions),
            "clicks": str(clicks),
            "costMicros": "0",
        },
    }


def write_fixture(
    tmp: Path,
    *,
    campaign_assets: list,
    customer_assets: list,
    campaigns: list,
    serves: list,
    change_events: list | None = None,
    declare: bool = True,
) -> Path:
    raw = tmp / "2026-08-20"
    raw.mkdir(parents=True, exist_ok=True)
    files = {
        builder.CAMPAIGN_ASSETS: (
            _pull("campaign.id,campaignAsset.fieldType,asset.id", campaign_assets),
            "campaign_asset.status='ENABLED'",
            "current-as-of-2026-08-20",
        ),
        builder.CUSTOMER_ASSETS: (
            _pull("customerAsset.fieldType,asset.id", customer_assets),
            "customer_asset.status='ENABLED'",
            "current-as-of-2026-08-20",
        ),
        builder.CAMPAIGNS: (
            _pull("campaign.id,campaign.name,campaign.status", campaigns),
            "campaign.status='ENABLED'",
            "current-as-of-2026-08-20",
        ),
        builder.FIELD_TYPE_VIEW: (
            _pull("assetFieldTypeView.fieldType,metrics.impressions", serves),
            "segments.date DURING LAST_30_DAYS",
            "window:LAST_30_DAYS",
        ),
    }
    if change_events is not None:
        files[builder.CHANGE_EVENTS] = (
            _pull("changeEvent.changeResourceType", change_events),
            "change_event.change_date_time DURING LAST_14_DAYS",
            "window:LAST_14_DAYS",
        )
    manifest = {"raw_dir": str(raw), "pulled": "2026-08-20", "files": {}}
    for name, (payload, filt, temporality) in files.items():
        (raw / name).write_text(json.dumps(payload), encoding="utf-8")
        if declare:
            manifest["files"][name] = {
                "rows": len(payload["data"]["results"]),
                "filter": filt,
                "temporality": temporality,
            }
    (raw / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return raw


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# --------------------------------------------------------------------------
# The double count. This is the test the whole builder exists for.
# --------------------------------------------------------------------------


class TestSharedAssetDoubleCount(FixtureCase):
    """One sitelink asset on three campaigns is three rows and one asset.

    A naive count of `campaign_asset` rows reports three sitelinks. That is the
    account-wide version of the error VF-19 describes, and it is what produced
    "410 sitelinks" from a pull holding far fewer.
    """

    def build(self):
        raw = write_fixture(
            self.tmp,
            campaign_assets=[
                campaign_asset_row("1", "900", "SITELINK"),
                campaign_asset_row("2", "900", "SITELINK"),
                campaign_asset_row("3", "900", "SITELINK"),
                campaign_asset_row("1", "901", "SITELINK"),
            ],
            customer_assets=[],
            campaigns=[
                campaign_row("1", "A"),
                campaign_row("2", "B"),
                campaign_row("3", "C"),
            ],
            serves=[serve_row("1", "SITELINK", 100, 10)],
        )
        return builder.build(raw)

    def test_the_naive_row_count_double_counts_the_shared_asset(self):
        """Proof of the hazard, computed the wrong way on purpose."""
        raw = self.tmp / "2026-08-20"
        self.build()
        rows = json.loads(
            (raw / builder.CAMPAIGN_ASSETS).read_text()
        )["data"]["results"]
        naive = sum(
            1 for r in rows if r["campaignAsset"]["fieldType"] == "SITELINK"
        )
        distinct = len(
            {
                r["asset"]["id"]
                for r in rows
                if r["campaignAsset"]["fieldType"] == "SITELINK"
            }
        )
        self.assertEqual(naive, 4)
        self.assertEqual(distinct, 2)
        self.assertGreater(
            naive,
            distinct,
            "the fixture must actually contain a shared asset or it proves nothing",
        )

    def test_the_builder_emits_both_counts_under_different_units(self):
        fs = self.build()
        rows = fs.get("ext.link_row.enabled.sitelink.count")
        assets = fs.get("ext.distinct_asset.enabled.sitelink.count")
        self.assertEqual(rows.value, 4)
        self.assertEqual(assets.value, 2)
        self.assertEqual(rows.unit, "campaign_asset_link_rows")
        self.assertEqual(assets.unit, "distinct_assets")

    def test_the_two_counts_can_never_be_compared_by_accident(self):
        """Different units, so the VF-15 guard refuses to relate them."""
        fs = self.build()
        with self.assertRaises(F.FactMismatchError):
            fs.compare(
                "ext.link_row.enabled.sitelink.count",
                "ext.distinct_asset.enabled.sitelink.count",
            )

    def test_each_scope_says_which_question_it_answers(self):
        fs = self.build()
        self.assertIn(
            "NOT a count of assets",
            fs.get("ext.link_row.enabled.sitelink.count").scope,
        )
        self.assertIn(
            "NOT a coverage measure",
            fs.get("ext.distinct_asset.enabled.sitelink.count").scope,
        )

    def test_the_sharing_facts_name_the_hazard_directly(self):
        fs = self.build()
        self.assertEqual(fs.value("ext.shared_asset.multi_campaign.count"), 1)
        self.assertEqual(fs.value("ext.shared_asset.max_campaigns"), 3)

    def test_a_shared_asset_is_counted_once_per_campaign_for_coverage(self):
        """Coverage is per campaign, so the shared asset covers all three."""
        fs = self.build()
        self.assertEqual(fs.value("ext.coverage.search.sitelink.campaign_level.count"), 3)
        self.assertEqual(fs.value("ext.coverage.search.sitelink.absent_everywhere.count"), 0)


# --------------------------------------------------------------------------
# The campaign-status join
# --------------------------------------------------------------------------


class TestCampaignStatusJoin(FixtureCase):
    """`campaign_asset.status='ENABLED'` does not filter the campaign."""

    def build(self):
        raw = write_fixture(
            self.tmp,
            campaign_assets=[
                campaign_asset_row("1", "900", "CALLOUT"),
                campaign_asset_row("99", "900", "CALLOUT"),
                campaign_asset_row("99", "901", "CALLOUT"),
            ],
            customer_assets=[],
            campaigns=[campaign_row("1", "A")],
            serves=[serve_row("1", "CALLOUT", 50, 5)],
        )
        return builder.build(raw)

    def test_rows_on_a_campaign_absent_from_the_enabled_list_are_excluded(self):
        fs = self.build()
        self.assertEqual(fs.value("ext.pull.campaign_asset.link_row.count"), 3)
        self.assertEqual(fs.value("ext.link_row.enabled.count"), 1)
        self.assertEqual(
            fs.value("ext.pull.campaign_asset.link_row.on_disabled_campaign.count"), 2
        )
        self.assertEqual(fs.value("ext.pull.campaign_asset.campaign.not_enabled.count"), 1)

    def test_the_scope_label_records_the_client_side_join(self):
        fs = self.build()
        scope = fs.get("ext.link_row.enabled.count").scope
        self.assertIn("INNER JOINED client-side to campaigns-current.json", scope)
        self.assertIn("NO campaign status filter", scope)

    def test_the_unjoined_and_joined_counts_are_not_comparable(self):
        fs = self.build()
        with self.assertRaises(F.FactMismatchError):
            fs.compare(
                "ext.pull.campaign_asset.link_row.count", "ext.link_row.enabled.count"
            )


# --------------------------------------------------------------------------
# Account-level versus campaign-level: the rule every finding rests on
# --------------------------------------------------------------------------


class TestAccountLevelInheritance(FixtureCase):
    def build(self):
        raw = write_fixture(
            self.tmp,
            campaign_assets=[campaign_asset_row("1", "900", "SITELINK")],
            customer_assets=[
                customer_asset_row("800", "CALLOUT"),
                customer_asset_row("900", "SITELINK"),
            ],
            campaigns=[campaign_row("1", "A"), campaign_row("2", "B")],
            serves=[
                serve_row("2", "CALLOUT", 500, 50),
                serve_row("1", "SITELINK", 900, 90),
                serve_row("2", "PROMOTION", 300, 30),
            ],
        )
        return builder.build(raw)

    def test_a_campaign_with_no_callout_of_its_own_is_inherited_not_absent(self):
        fs = self.build()
        self.assertEqual(fs.value("ext.coverage.search.callout.campaign_level.count"), 0)
        self.assertEqual(
            fs.value("ext.coverage.search.callout.inherited_from_account.count"), 2
        )
        self.assertEqual(fs.value("ext.coverage.search.callout.absent_everywhere.count"), 0)

    def test_a_type_at_neither_level_is_absent_everywhere(self):
        fs = self.build()
        self.assertEqual(fs.value("ext.coverage.search.price.absent_everywhere.count"), 2)
        self.assertEqual(fs.value("ext.coverage.search.price.inherited_from_account.count"), 0)

    def test_the_three_states_always_partition_the_search_campaigns(self):
        fs = self.build()
        total = fs.value("ext.enabled_campaign.search.count")
        for field_type in builder.JUDGED_FIELD_TYPES + builder.OPPORTUNITY_FIELD_TYPES:
            slug = field_type.lower()
            got = sum(
                fs.value(f"ext.coverage.search.{slug}.{state}.count")
                for state in (
                    builder.STATE_CAMPAIGN,
                    builder.STATE_INHERITED,
                    builder.STATE_ABSENT,
                )
            )
            self.assertEqual(got, total, f"{field_type} states do not partition")

    def test_serve_data_with_no_campaign_level_asset_is_a_proof_of_inheritance(self):
        fs = self.build()
        # campaign 2 serves CALLOUT with no campaign-level callout: 1 proof.
        self.assertEqual(fs.value("ext.inheritance.proven_pair.count"), 1)

    def test_serve_data_explained_by_neither_pull_is_reported_not_swallowed(self):
        fs = self.build()
        self.assertEqual(fs.value("ext.inheritance.unexplained_pair.count"), 1)
        self.assertEqual(fs.value("ext.inheritance.unexplained_pair.promotion.count"), 1)
        self.assertIn(
            "ad-group-level assets or automatically created assets",
            fs.get("ext.inheritance.unexplained_pair.count").scope,
        )

    def test_the_union_id_and_the_account_level_id_answer_different_questions(self):
        """The rename that stops a right number carrying a wrong attribution."""
        fs = self.build()
        self.assertEqual(fs.value("ext.account_level.price.distinct_asset.count"), 0)
        self.assertEqual(fs.value("ext.union.price.distinct_asset.count"), 0)
        # Sitelink is the case where the two genuinely differ in the live data.
        self.assertNotIn("ext.account_wide.sitelink.distinct_asset.count", fs.ids())
        self.assertIn("Only `ext.account_level.*` answers", builder.UNION_SCOPE_NOTE)

    def test_an_asset_at_both_levels_is_counted_once_in_the_union(self):
        fs = self.build()
        self.assertEqual(fs.value("ext.distinct_asset.enabled.sitelink.count"), 1)
        self.assertEqual(fs.value("ext.account_level.sitelink.distinct_asset.count"), 1)
        self.assertEqual(fs.value("ext.union.sitelink.distinct_asset.count"), 1)
        self.assertEqual(fs.value("ext.account_level.also_campaign_attached.count"), 1)


# --------------------------------------------------------------------------
# Field-type performance
# --------------------------------------------------------------------------


class TestFieldTypePerformance(FixtureCase):
    def build(self):
        raw = write_fixture(
            self.tmp,
            campaign_assets=[campaign_asset_row("1", "900", "SITELINK")],
            customer_assets=[],
            campaigns=[campaign_row("1", "A")],
            serves=[
                serve_row("1", "SITELINK", 1000, 100),
                serve_row("1", "CALLOUT", 800, 80),
                serve_row("1", "STRUCTURED_SNIPPET", 10, 1),
            ],
        )
        return builder.build(raw)

    def test_impressions_use_a_non_additive_unit(self):
        fs = self.build()
        for field_type in ("sitelink", "callout", "structured_snippet"):
            self.assertIn(
                fs.get(f"ext.serve.{field_type}.impressions").unit,
                F.NON_ADDITIVE_UNITS,
            )

    def test_the_guard_refuses_to_sum_across_field_types(self):
        """Two defences, and the scope one fires first.

        The per-field-type scopes differ (each names its own field type), so the
        VF-15 scope guard refuses before the non-additive check is reached. Both
        are wanted: the scope guard stops the cross-type sum, and the unit stops
        anyone who normalises the scopes from summing anyway.
        """
        fs = self.build()
        with self.assertRaises(F.FactMismatchError):
            fs.sum(["ext.serve.sitelink.impressions", "ext.serve.callout.impressions"])

    def test_the_unit_alone_blocks_a_sum_even_on_one_shared_scope(self):
        fs = self.build()
        unit = fs.get("ext.serve.sitelink.impressions").unit
        probe = F.FactSet()
        shared = dict(
            unit=unit,
            scope="one shared scope, on purpose",
            window="n/a",
            source="test",
            basis="broad-goal-set",
        )
        probe.add("a", 1, **shared)
        probe.add("b", 2, **shared)
        with self.assertRaises(F.NonAdditiveFactError):
            probe.sum(["a", "b"])

    def test_the_cross_type_sum_is_emitted_once_and_labelled_a_fiction(self):
        fs = self.build()
        fact = fs.get("ext.serve.field_type_sum.impressions")
        self.assertEqual(fact.value, 1810)
        self.assertIn("FAN-OUT ARTEFACT", fact.scope)

    def test_no_per_field_type_cost_fact_is_emitted(self):
        """There is no non-additive currency unit, so cost is not offered."""
        fs = self.build()
        self.assertEqual([i for i in fs.ids() if "cost" in i or "currency" in i], [])

    def test_the_snippet_to_sitelink_percentage_declares_its_scale(self):
        fs = self.build()
        fact = fs.get("ext.serve.structured_snippet_vs_sitelink.percent")
        self.assertEqual(fact.value, 1.0)
        self.assertEqual(F.percentage_convention(fact.unit), F.PERCENT_SCALE)

    def test_field_type_facts_declare_the_broad_goal_set_basis(self):
        fs = self.build()
        self.assertEqual(fs.get("ext.serve.sitelink.impressions").basis, "broad-goal-set")


# --------------------------------------------------------------------------
# Freshness, temporality, refusals
# --------------------------------------------------------------------------


class TestTemporalityAndRefusals(FixtureCase):
    def _minimal(self, **overrides):
        kwargs = dict(
            campaign_assets=[campaign_asset_row("1", "900", "SITELINK")],
            customer_assets=[customer_asset_row("800", "CALLOUT")],
            campaigns=[campaign_row("1", "A")],
            serves=[serve_row("1", "SITELINK", 10, 1)],
        )
        kwargs.update(overrides)
        return write_fixture(self.tmp, **kwargs)

    def test_current_state_facts_carry_current_as_of(self):
        fs = builder.build(self._minimal())
        for id in (
            "ext.link_row.enabled.count",
            "ext.distinct_asset.enabled.sitelink.count",
            "ext.coverage.search.price.absent_everywhere.count",
        ):
            self.assertTrue(
                F.is_current_state_window(fs.get(id).window),
                f"{id} window is {fs.get(id).window!r}",
            )

    def test_a_windowed_serve_fact_never_carries_current_as_of(self):
        fs = builder.build(self._minimal())
        window = fs.get("ext.serve.sitelink.impressions").window
        self.assertFalse(F.is_current_state_window(window))
        self.assertIn("2026-07", window)

    def test_the_guard_blocks_comparing_current_state_against_a_window(self):
        fs = builder.build(self._minimal())
        with self.assertRaises(F.FactMismatchError):
            fs.compare(
                "ext.distinct_asset.enabled.sitelink.count",
                "ext.serve.sitelink.campaign.count",
            )

    def test_temporality_is_read_from_the_manifest_not_inferred(self):
        raw = self._minimal()
        manifest = json.loads((raw / "_manifest.json").read_text())
        manifest["files"][builder.CAMPAIGN_ASSETS]["temporality"] = "current-as-of-2026-08-01"
        (raw / "_manifest.json").write_text(json.dumps(manifest))
        fs = builder.build(raw)
        self.assertEqual(
            fs.get("ext.link_row.enabled.count").window, "current-as-of-2026-08-01"
        )

    def test_an_undeclared_pull_says_so_rather_than_borrowing_a_label(self):
        raw = self._minimal(declare=False)
        fs = builder.build(raw)
        window = fs.get("ext.link_row.enabled.count").window
        self.assertIn("NOT declared", window)
        self.assertIn("VF-17 rule 5", window)

    def test_a_row_count_disagreeing_with_the_manifest_refuses_the_run(self):
        raw = self._minimal()
        manifest = json.loads((raw / "_manifest.json").read_text())
        manifest["files"][builder.CAMPAIGN_ASSETS]["rows"] = 99
        (raw / "_manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(builder.S.ManifestError):
            builder.build(raw)

    def test_a_declared_filter_contradicted_by_the_rows_refuses_the_run(self):
        raw = self._minimal()
        payload = json.loads((raw / builder.CAMPAIGN_ASSETS).read_text())
        payload["data"]["results"][0]["campaignAsset"]["status"] = "PAUSED"
        (raw / builder.CAMPAIGN_ASSETS).write_text(json.dumps(payload))
        with self.assertRaises(builder.BuildError):
            builder.build(raw)

    def test_a_declared_filter_naming_a_clause_the_query_lacks_refuses_the_run(self):
        raw = self._minimal()
        manifest = json.loads((raw / "_manifest.json").read_text())
        manifest["files"][builder.CAMPAIGN_ASSETS]["filter"] = "asset.type='SITELINK'"
        (raw / "_manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(builder.gaql.FilterMismatch):
            builder.build(raw)

    def test_a_segmented_campaign_asset_pull_refuses_the_run(self):
        raw = self._minimal(
            campaign_assets=[
                campaign_asset_row("1", "900", "SITELINK"),
                campaign_asset_row("1", "900", "SITELINK"),
            ]
        )
        manifest = json.loads((raw / "_manifest.json").read_text())
        manifest["files"][builder.CAMPAIGN_ASSETS]["rows"] = 2
        (raw / "_manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(builder.BuildError, "duplicate"):
            builder.build(raw)

    def test_a_missing_change_event_pull_nulls_the_facts_with_a_reason(self):
        fs = builder.build(self._minimal())
        fact = fs.get("ext.change_event.campaign_asset.count")
        self.assertIsNone(fact.value)
        self.assertIn("UNAVAILABLE", fact.scope)
        self.assertIn("VF-17 rule 3", fact.scope)

    def test_change_events_are_counted_as_events_and_as_campaigns(self):
        raw = self._minimal(
            change_events=[
                {"changeEvent": {"changeResourceType": "CAMPAIGN_ASSET", "campaign": "c/1"}},
                {"changeEvent": {"changeResourceType": "CAMPAIGN_ASSET", "campaign": "c/1"}},
                {"changeEvent": {"changeResourceType": "CAMPAIGN_ASSET", "campaign": "c/2"}},
                {"changeEvent": {"changeResourceType": "ASSET"}},
            ]
        )
        fs = builder.build(raw)
        self.assertEqual(fs.value("ext.change_event.campaign_asset.count"), 3)
        self.assertEqual(fs.value("ext.change_event.campaign_asset.campaign.count"), 2)
        self.assertEqual(fs.value("ext.change_event.asset.count"), 1)


# --------------------------------------------------------------------------
# The VF-15 contract, over every fact this builder writes
# --------------------------------------------------------------------------


class TestFactContract(FixtureCase):
    def setUp(self) -> None:
        super().setUp()
        self.fs = builder.build(
            write_fixture(
                self.tmp,
                campaign_assets=[
                    campaign_asset_row("1", "900", "SITELINK"),
                    campaign_asset_row("2", "900", "SITELINK"),
                ],
                customer_assets=[customer_asset_row("800", "CALLOUT")],
                campaigns=[campaign_row("1", "A"), campaign_row("2", "B", "VIDEO")],
                serves=[serve_row("1", "SITELINK", 10, 1)],
                change_events=[
                    {"changeEvent": {"changeResourceType": "CAMPAIGN_ASSET", "campaign": "c/1"}}
                ],
            )
        )

    def test_every_fact_carries_all_seven_fields(self):
        for fact in self.fs:
            self.assertTrue(fact.id)
            self.assertTrue(fact.unit)
            self.assertTrue(fact.scope)
            self.assertTrue(fact.window)
            self.assertTrue(fact.source)
            self.assertIn(fact.basis, F.LEGAL_BASES)

    def test_every_percent_unit_declares_its_scale(self):
        for fact in self.fs:
            if "percent" in fact.unit or "pct" in fact.unit:
                self.assertIsNotNone(
                    F.percentage_convention(fact.unit),
                    f"{fact.id} unit {fact.unit!r} does not say percent or ratio",
                )

    def test_no_scope_is_a_paraphrase_without_a_filter(self):
        for fact in self.fs:
            if fact.value is None:
                continue
            self.assertRegex(
                fact.scope,
                r"(status=ENABLED|segments\.date|change_event\.|UNION|field_type)",
                f"{fact.id} scope names no filter: {fact.scope!r}",
            )

    def test_the_meta_records_the_row_key_and_what_is_out_of_scope(self):
        self.assertIn("(campaign.id, asset.id, field_type)", self.fs.meta["row_key"])
        self.assertIn("ad_group_asset", self.fs.meta["out_of_scope"])
        self.assertIn("VERIFIED", self.fs.meta["inheritance"])
        self.assertIn("ASSUMED", self.fs.meta["inheritance"])

    def test_the_audit_trail_records_the_campaign_status_defect(self):
        """Only when the pull actually holds a non-enabled campaign."""
        fs = builder.build(
            write_fixture(
                self.tmp / "defect",
                campaign_assets=[
                    campaign_asset_row("1", "900", "SITELINK"),
                    campaign_asset_row("99", "900", "SITELINK"),
                ],
                customer_assets=[customer_asset_row("800", "CALLOUT")],
                campaigns=[campaign_row("1", "A")],
                serves=[serve_row("1", "SITELINK", 10, 1)],
            )
        )
        trail = " ".join(fs.meta["declared_filter_audit"])
        self.assertIn("filters the attachment, not the campaign", trail)

    def test_an_empty_account_level_pull_builds_and_says_nothing_is_inherited(self):
        fs = builder.build(
            write_fixture(
                self.tmp / "noaccount",
                campaign_assets=[campaign_asset_row("1", "900", "SITELINK")],
                customer_assets=[],
                campaigns=[campaign_row("1", "A")],
                serves=[serve_row("1", "SITELINK", 10, 1)],
            )
        )
        self.assertEqual(fs.value("ext.coverage.search.callout.absent_everywhere.count"), 1)
        self.assertEqual(fs.value("ext.coverage.search.callout.inherited_from_account.count"), 0)
        self.assertIn(
            "ZERO rows", " ".join(fs.meta["declared_filter_audit"])
        )

    def test_a_tof_campaign_is_counted_and_its_scope_says_it_is_not_judged(self):
        self.assertEqual(self.fs.value("ext.coverage.tof_campaign.count"), 1)
        self.assertIn("never judged", self.fs.get("ext.coverage.tof_campaign.count").scope)

    def test_the_facts_file_round_trips(self):
        out = self.tmp / "facts.json"
        self.fs.write(out)
        again = F.FactSet.load(out)
        self.assertEqual(len(again), len(self.fs))
        for fact in self.fs:
            self.assertEqual(again.get(fact.id).value, fact.value)


# --------------------------------------------------------------------------
# The live 2026-08-20 pull
# --------------------------------------------------------------------------




if __name__ == "__main__":
    unittest.main(verbosity=2)
