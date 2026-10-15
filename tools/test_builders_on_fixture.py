#!/usr/bin/env python3
"""Every builder, end to end, on committed synthetic data.

Run:  python3 tools/test_builders_on_fixture.py

Why this file exists
--------------------
Measured 2026-08-20: on a fresh clone **265 of 505 tests skip**, and
`test_budget_optimize_facts.py` executes **0 of its 71**, because runtime pulls
are gitignored and four of the five suites pin to a single date. No builder had
ever run against a second dataset.

Everything here runs on `tools/fixtures/2026-08-08/`, which is committed and
entirely synthetic, so **this module needs no account data and never skips.**

What it is for, and what it is not for. It asserts **behaviour that must hold on
any data**: the builders run, the guards fire, the refusals refuse, the labels
are right. It does **not** assert this account's numbers - that is the job of the
date-pinned suites, and duplicating it here would just pin a second set of
magic numbers (VF-20).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_audit_ad_copy_facts as AC  # noqa: E402
import build_audit_extensions_facts as AE  # noqa: E402
import build_budget_optimize_facts as BO  # noqa: E402
import build_mine_search_terms_facts as MST  # noqa: E402
import build_weekly_review_facts as WR  # noqa: E402
import make_fixture  # noqa: E402

FIXTURE = make_fixture.FIXTURE_ROOT
PLAN = HERE.parent / "budget-plan.md"

#: Thresholds for the fixture. Not defaults and not a recommendation: the
#: builder has none, so a caller must supply its own.
FIXTURE_THRESHOLDS = {
    "min_spend_cad": Decimal("100"),
    "min_clicks": 3,
    "target_cpa_cad": Decimal("400"),
    "target_cpa_multiple": Decimal("5"),
}


class FixtureIntegrityTests(unittest.TestCase):
    def test_the_fixture_is_committed_and_present(self):
        self.assertTrue(FIXTURE.is_dir(), f"missing fixture: {FIXTURE}")
        self.assertTrue((FIXTURE / "_manifest.json").exists())

    def test_the_fixture_is_current_with_its_generator(self):
        """A hand-edited fixture drifts from the generator and stops meaning anything."""
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / FIXTURE.name
            make_fixture.write(fresh)
            for path in sorted(fresh.glob("*.json")):
                committed = FIXTURE / path.name
                self.assertTrue(committed.exists(), f"{path.name} not committed")
                self.assertEqual(
                    committed.read_text(),
                    path.read_text(),
                    f"{path.name} differs from make_fixture.py output. "
                    "Run: python3 tools/make_fixture.py",
                )

    def test_the_fixture_announces_that_it_is_synthetic(self):
        """Nobody should ever mistake it for an account pull."""
        manifest = json.loads((FIXTURE / "_manifest.json").read_text())
        self.assertTrue(manifest.get("SYNTHETIC"))
        self.assertEqual(manifest["customer"], "9999999999")

    def test_the_fixture_carries_no_real_campaign_id(self):
        """This account's ids are 11 digits starting 2. The fixture's start 7."""
        blob = "".join(
            p.read_text() for p in sorted(FIXTURE.glob("*.json"))
        )
        self.assertNotIn("1234567890", blob, "real customer id in the fixture")
        for real in ("71000000001", "71000000002", "71000000004", "71000000005"):
            self.assertNotIn(real, blob, f"real campaign id {real} in the fixture")


class EveryBuilderRunsTests(unittest.TestCase):
    """The plainest possible assertion, and the one nothing else made."""

    def test_weekly_review_builds(self):
        fs = WR.build(FIXTURE, PLAN)
        self.assertGreater(len(fs), 0)
        self.assertEqual(fs.unsafe_comparisons, [])

    def test_budget_optimize_builds(self):
        fs = BO.build(FIXTURE)
        self.assertGreater(len(fs), 0)

    def test_every_builder_refuses_a_non_dated_raw_dir(self):
        """VF-17 rule 4: a raw dir whose name is not YYYY-MM-DD has no pull
        date, so no current-state window can be formed and no relative window
        token can be resolved. Every builder refuses; none writes facts off
        an undatable directory (audit-ad-copy, mine-search-terms and
        audit-extensions used to build anyway - final-review finding)."""
        import build_audit_ad_copy_facts as AAC
        import build_mine_search_terms_facts as MST
        import build_audit_extensions_facts as AE
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "notadate"
            shutil.copytree(FIXTURE, bad)
            for label, call in [
                ("weekly", lambda: WR.build(bad, PLAN)),
                ("budget", lambda: BO.build(bad)),
                ("audit-ad-copy", lambda: AAC.build(bad)),
                ("mine-search-terms",
                 lambda: MST.build(bad, MST.Thresholds(
                     min_spend_cad=1, min_clicks=1,
                     target_cpa_cad=100, target_cpa_multiple=2))),
                ("audit-extensions", lambda: AE.build(bad)),
            ]:
                with self.assertRaises(Exception, msg=label) as ctx:
                    call()
                self.assertIn("VF-17 rule 4", str(ctx.exception), label)

    def test_weekly_review_builds_a_september_month_with_null_pacing(self):
        """The September time bomb, defused for this builder too: a month with
        no stated Google Ads split builds, keeps its spend-side facts real,
        and writes every budget-dependent pacing fact null with the config's
        reason (VF-14, guardrail 8) - never August's figure carried forward."""
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "2026-09-08"
            shutil.copytree(FIXTURE, staged)
            spend = staged / "account-spend-mtd.json"
            payload = json.loads(spend.read_text(encoding="utf-8"))
            for row in payload["data"]["results"]:
                row["segments"]["date"] = row["segments"]["date"].replace(
                    "2026-08", "2026-09"
                )
            spend.write_text(json.dumps(payload), encoding="utf-8")
            mpath = staged / "_manifest.json"
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
            for entry in manifest["files"].values():
                for key in ("temporality", "filter"):
                    if isinstance(entry.get(key), str):
                        entry[key] = (
                            entry[key]
                            .replace("2026-08", "2026-09")
                            # Keep the prior week adjacent to the shifted
                            # current one, or the pair guard (correctly)
                            # refuses the restaged directory.
                            .replace("2026-07-25", "2026-08-25")
                            .replace("2026-07-31", "2026-08-31")
                        )
            mpath.write_text(json.dumps(manifest), encoding="utf-8")
            fs = WR.build(staged, PLAN)
            budget = fs.get("budget.google_ads.september_2026.cad")
            self.assertIsNone(budget.value)
            self.assertIn("no stated Google Ads split", budget.scope)
            self.assertIsNone(
                fs.get("budget.google_ads.september_2026.prorated_to_date.cad").value
            )
            self.assertIsNone(fs.get("pacing.overspend_to_date.cad").value)
            self.assertIsNotNone(fs.value("spend.account.mtd.cad"))
            self.assertIsNotNone(fs.value("pacing.projected_full_month.cad"))
            for fact in fs:
                if fact.id.startswith(("pacing.", "budget.google_ads.")):
                    self.assertNotIn(
                        "54400", str(fact.value).replace(",", ""), fact.id
                    )

    def test_audit_extensions_builds(self):
        fs = AE.build(FIXTURE)
        self.assertGreater(len(fs), 0)
        self.assertEqual(fs.unsafe_comparisons, [])

    def test_mine_search_terms_builds(self):
        fs, _rows, _csv, _lint = MST.build(
            FIXTURE, MST.require_thresholds(dict(FIXTURE_THRESHOLDS))
        )
        self.assertGreater(len(fs), 0)
        self.assertEqual(fs.unsafe_comparisons, [])

    def test_no_builder_emits_a_fact_without_a_scope_or_a_window(self):
        """Structural. A fact with no scope cannot be compared with anything."""
        for name, fs in self._all_factsets():
            for fact in fs:
                with self.subTest(builder=name, fact=fact.id):
                    self.assertTrue(str(fact.scope).strip())
                    self.assertTrue(str(fact.window).strip())

    def _all_factsets(self):
        yield "weekly_review", WR.build(FIXTURE, PLAN)
        yield "audit_ad_copy", AC.build(FIXTURE)
        yield "budget_optimize", BO.build(FIXTURE)
        yield "audit_extensions", AE.build(FIXTURE)
        fs, _r, _c, _l = MST.build(
            FIXTURE, MST.require_thresholds(dict(FIXTURE_THRESHOLDS))
        )
        yield "mine_search_terms", fs


class HeadlineFactsDietTests(unittest.TestCase):
    """The 2026-08-27 diet: per-headline detail only for the tabled ads.

    Exhaustive per-headline emission built the dense value space both
    fabrication escapes hid in, so `add_asset_facts` now emits per-ad detail
    only for the serve-table selection (plus `--ad`). The fixture's 13 RSAs
    select 11 (top 10 by cost + one above the CAD 2,000 floor), leaving ads
    12 and 13 processed but undetailed.
    """

    AD12, AD13 = "60000000012", "60000000013"

    @classmethod
    def setUpClass(cls):
        cls.default = AC.build(FIXTURE)
        cls.with_ad = AC.build(FIXTURE, detail_ad_ids=[cls.AD12])
        cls.full = AC.build(FIXTURE, detail_ad_ids=[cls.AD12, cls.AD13])

    @staticmethod
    def _detailed(fs, ad_id):
        return any(f".ad.{ad_id}.headline" in f.id for f in fs)

    def test_the_selection_and_the_counts_are_facts_not_silence(self):
        self.assertEqual(self.default.value("asset_view.per_ad_detail.ad.count"), 11)
        self.assertEqual(
            self.default.value("asset_view.per_ad_detail.excluded_ad.count"), 2
        )
        rule = self.default.value("asset_view.per_ad_detail.rule")
        self.assertIn("serve-table selection", rule)

    def test_detail_follows_the_serve_table_selection(self):
        self.assertTrue(self._detailed(self.default, "60000000001"))
        self.assertFalse(self._detailed(self.default, self.AD12))
        self.assertFalse(self._detailed(self.default, self.AD13))

    def test_the_fact_count_drops_sharply_against_full_emission(self):
        self.assertLess(len(self.default), len(self.full))

    def test_dash_dash_ad_adds_detail_for_the_named_ad_only(self):
        self.assertTrue(self._detailed(self.with_ad, self.AD12))
        self.assertFalse(self._detailed(self.with_ad, self.AD13))
        self.assertEqual(
            self.with_ad.value("asset_view.per_ad_detail.excluded_ad.count"), 1
        )
        self.assertIn("--ad", self.with_ad.value("asset_view.per_ad_detail.rule"))

    def test_selection_never_changes_an_account_level_number(self):
        """An undetailed ad is still processed: the totals must not move."""
        for fact_id in (
            "asset_view.dead_slot.total",
            "asset_view.dead_slot.ads_affected",
            "asset_view.ranked.headline.count",
            "asset_view.live_headline.slot.count",
            "asset_view.ranked.ad.count",
            "asset_view.removed_but_served.row.count",
        ):
            with self.subTest(fact=fact_id):
                self.assertEqual(
                    self.default.value(fact_id), self.full.value(fact_id)
                )

    def test_the_dead_slot_and_history_shapes_survive_on_the_fixture(self):
        """Ad 1 has one live headline with no serve row and one history row."""
        prefix = None
        for f in self.default:
            if f.id.endswith(".dead_slot.count") and ".ad.60000000001." in f.id:
                prefix = f.id
                self.assertEqual(f.value, 1)
        self.assertIsNotNone(prefix)
        self.assertEqual(
            self.default.value(
                "asset_view.adgroup.90000003.ad.60000000001.headline"
                ".removed_but_served.count"
            ),
            1,
        )


class WeekPairGuardTests(unittest.TestCase):
    """Anchored-window support (2026-08-27): both week forms accepted, and
    the pair must be equal-length, adjacent, and finished. The guard's first
    catch was the fixture itself - its prior week was the same seven days as
    the current one."""

    PULLED = __import__("datetime").date(2026, 8, 8)

    def _pair(self, cur, pri):
        import builder_core as core
        return core.verify_week_pair(cur, pri, self.PULLED, "cur.json", "pri.json")

    def test_a_valid_anchored_pair_passes(self):
        line = self._pair("window:2026-08-01..2026-08-07",
                          "window:2026-07-25..2026-07-31")
        self.assertIn("anchored", line)

    def test_a_valid_rolling_pair_passes(self):
        line = self._pair("window:LAST_7_DAYS",
                          "window:2026-07-25..2026-07-31")
        self.assertIn("rolling", line)

    def test_an_overlapping_pair_is_refused(self):
        import builder_core as core
        with self.assertRaises(core.BuildError) as caught:
            self._pair("window:2026-08-01..2026-08-07",
                       "window:2026-07-28..2026-08-03")
        self.assertIn("OVERLAP", str(caught.exception))

    def test_an_unequal_length_pair_is_refused(self):
        import builder_core as core
        with self.assertRaises(core.BuildError) as caught:
            self._pair("window:2026-08-01..2026-08-07",
                       "window:2026-07-18..2026-07-31")
        self.assertIn("equal-length", str(caught.exception))

    def test_a_relative_prior_week_is_the_mixed_form_contradiction(self):
        import builder_core as core
        with self.assertRaises(core.BuildError) as caught:
            self._pair("window:LAST_7_DAYS", "window:LAST_7_DAYS")
        self.assertIn("same trailing window", str(caught.exception))

    def test_an_unfinished_current_week_is_refused(self):
        import builder_core as core
        with self.assertRaises(core.BuildError) as caught:
            self._pair("window:2026-08-02..2026-08-08",
                       "window:2026-07-26..2026-08-01")
        self.assertIn("end before the pull date", str(caught.exception))

    def test_weekly_review_builds_on_an_anchored_fixture(self):
        """End to end: the fixture restaged with explicit BETWEEN windows on
        the current-week pulls builds cleanly - the anchored form the skills
        now prefer over rolling LAST_7_DAYS."""
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / FIXTURE.name
            shutil.copytree(FIXTURE, staged)
            mpath = staged / "_manifest.json"
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
            for name in ("campaign-cost-is-7d.json",
                         "campaign-conversions-7d.json"):
                entry = manifest["files"][name]
                entry["temporality"] = "window:2026-08-01..2026-08-07"
                entry["filter"] = entry["filter"].replace(
                    "segments.date DURING LAST_7_DAYS",
                    "segments.date BETWEEN '2026-08-01' AND '2026-08-07'",
                )
            mpath.write_text(json.dumps(manifest), encoding="utf-8")
            fs = WR.build(staged, PLAN)
            self.assertGreater(len(fs), 0)
            trail = " ".join(
                str(line)
                for key in ("scope_trail", "declared_filter_audit")
                for line in (fs.meta.get(key) or [])
            )
            self.assertIn("window pair OK (anchored)", trail)


class GuardsFireOnUnseenDataTests(unittest.TestCase):
    """The three fixes of 2026-08-20, proven against data they never saw."""

    @classmethod
    def setUpClass(cls):
        cls.mst, _rows, cls.csv, _lint = MST.build(
            FIXTURE, MST.require_thresholds(dict(FIXTURE_THRESHOLDS))
        )
        cls.bo = BO.build(FIXTURE)

    def test_the_90_day_clearance_blocks_a_term_the_30_day_window_calls_waste(self):
        """The `jotform` failure, reproduced as a fixture row.

        `kept-by-90d` looks like a clean negate on the 30-day pull: CAD 500,
        25 clicks, zero conversions. The 90-day pull shows it converted. It must
        not reach the CSV, and `negate-me` - identical but for the 90-day hit -
        must.
        """
        shipped = {row["Keyword"] for row in self.csv}
        self.assertIn("negate-me", shipped)
        self.assertNotIn("kept-by-90d", shipped)
        self.assertTrue(self.mst.value("st.donotnegate.90d.clearance_pulled"))
        self.assertGreater(self.mst.value("st.decision.keep_converted_in_90d.row.count"), 0)

    def test_removing_the_clearance_empties_the_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / FIXTURE.name
            shutil.copytree(FIXTURE, dest)
            (dest / MST.DONOTNEGATE_90D).unlink()
            fs, _rows, out_csv, _lint = MST.build(
                dest, MST.require_thresholds(dict(FIXTURE_THRESHOLDS))
            )
        self.assertEqual(out_csv, [])
        self.assertFalse(fs.value("st.donotnegate.90d.clearance_pulled"))

    def test_existing_negatives_removes_a_covered_candidate(self):
        """The flag that used to accept paths and never open them."""
        payload = {"data": {"results": [{
            "campaign": {"id": make_fixture.NONBRAND},
            "adGroup": {"id": make_fixture.AG_NONBRAND},
            "adGroupCriterion": {
                "negative": True,
                "keyword": {"text": "negate-me", "matchType": "EXACT"},
            },
        }]}}
        fs, _rows, out_csv, _lint = MST.build(
            FIXTURE, MST.require_thresholds(dict(FIXTURE_THRESHOLDS)), [payload]
        )
        self.assertNotIn("negate-me", {r["Keyword"] for r in out_csv})
        self.assertEqual(fs.value("st.csv.already_covered.removed.row.count"), 1)
        self.assertTrue(fs.value("st.csv.existing_negative_inventory.checked"))

    def test_a_change_inside_the_pmax_window_reads_false_not_null(self):
        """The line-898 ordering bug, on a fixture row planted for it.

        The fixture gives Performance Max a budget change inside its 28-day
        window. `cleared` must be `False`. A `null` here means the coverage
        caveat pre-empted a found change again.
        """
        pmax = [
            f for f in self.bo
            if f.id.endswith(".learning_window.cleared")
            and self.bo.value(f.id.rsplit(".", 2)[0] + ".learning_window.days") == 28
        ]
        self.assertTrue(pmax, "the fixture no longer contains a PMax cluster")
        for fact in pmax:
            prefix = fact.id.rsplit(".", 2)[0]
            date = self.bo.value(f"{prefix}.last_budget_or_target_change.date")
            self.assertFalse(str(date).startswith("none"), "fixture lost its change row")
            self.assertIs(fact.value, False, f"{fact.id} should be False, got {fact.value!r}")


class RefusalsRefuseTests(unittest.TestCase):
    """A refusal is a finding, so it has to actually happen."""

    def _without(self, filename):
        tmp = tempfile.mkdtemp()
        dest = Path(tmp) / FIXTURE.name
        shutil.copytree(FIXTURE, dest)
        (dest / filename).unlink()
        return dest

    def test_a_missing_manifest_is_never_silently_tolerated(self):
        """VF-17 rule 5, tested as the contract rather than as one mechanism.

        The pack has **two readings** of this rule and both are defensible.
        `weekly-review`, `budget-optimize` and `mine-search-terms` refuse
        outright: "no `_manifest.json`, no facts file." `audit-extensions`
        instead builds and stamps **every** window with an explicit
        "temporality NOT declared ... (VF-17 rule 5)". Arguably the second is
        more useful, since a labelled fact is more than no fact.

        What must never happen is the third option: proceeding as though
        temporality were declared. So that is what this asserts, and the
        divergence is recorded in MAINTENANCE.md rather than resolved by
        picking a winner here.
        """
        dest = self._without("_manifest.json")
        refusers = (
            ("weekly_review", lambda: WR.build(dest, PLAN)),
            ("budget_optimize", lambda: BO.build(dest)),
            ("mine_search_terms", lambda: MST.build(
                dest, MST.require_thresholds(dict(FIXTURE_THRESHOLDS)))),
        )
        for label, call in refusers:
            with self.subTest(builder=label, behaviour="refuses"):
                with self.assertRaises(Exception):
                    call()

        with self.subTest(builder="audit_extensions", behaviour="labels"):
            fs = AE.build(dest)
            self.assertGreater(len(fs), 0)
            undeclared = [f for f in fs if "NOT declared" in str(f.window)]
            self.assertEqual(
                len(undeclared),
                len(list(fs)),
                "every window must say the temporality was not declared; a "
                "single window that reads as declared is the failure mode",
            )
            for fact in undeclared:
                self.assertIn("VF-17", str(fact.window))

    def test_a_missing_required_pull_refuses(self):
        dest = self._without("campaigns-current.json")
        with self.assertRaises(Exception):
            BO.build(dest)

    def test_mine_search_terms_refuses_without_thresholds(self):
        with self.assertRaises(MST.MissingThresholdError):
            MST.require_thresholds({
                "min_spend_cad": None, "min_clicks": None,
                "target_cpa_cad": None, "target_cpa_multiple": None,
            })


class BucketRoasFixtureTests(unittest.TestCase):
    """Fixture equivalents of the 2026-08-27 bucket-ROAS regression tests.

    The real-data versions live in test_weekly_review_facts.RealRunTests and
    skip on a machine without the gitignored pulls, so this class re-exercises
    the core behaviours on the committed fixture. The fixture's synthetic
    campaign ids are deliberately outside the real account's ranges, so they
    are not in account-config's bucket sets and an unpatched build puts every
    campaign in "nonbrand"; the patch below assigns the fixture's Brand and
    TOF campaigns to their intended buckets for this class only.
    """

    @classmethod
    def setUpClass(cls):
        import unittest.mock as mock
        patches = [
            mock.patch.object(WR, "BRAND_CAMPAIGN_IDS",
                              frozenset({make_fixture.BRAND})),
            mock.patch.object(WR, "TOF_CAMPAIGN_IDS",
                              frozenset({make_fixture.TOF})),
        ]
        for p in patches:
            p.start()
            cls.addClassCleanup(p.stop)
        cls.fs = WR.build(FIXTURE, PLAN)

    def value(self, fact_id):
        return Decimal(str(self.fs.value(fact_id)))

    def test_the_bucket_values_sum_to_the_account_value(self):
        for tag in ("7d", "prior7d"):
            parts = sum(
                self.value(f"conversion_value.assumed.{k}.{tag}")
                for k in ("brand", "nonbrand", "tof"))
            self.assertEqual(
                parts,
                self.value(f"conversion_value.assumed.account.{tag}"), tag)

    def test_a_nonbrand_roas_exists_and_is_value_over_spend(self):
        for tag in ("7d", "prior7d"):
            fact_id = f"roas.nonbrand.assumed_value.{tag}"
            self.assertIn(fact_id, self.fs)
            self.assertEqual(
                self.value(fact_id),
                self.value(f"conversion_value.assumed.nonbrand.{tag}")
                / self.value(f"spend.nonbrand.{tag}"))

    def test_a_brand_roas_exists_and_is_value_over_spend(self):
        for tag in ("7d", "prior7d"):
            fact_id = f"roas.brand.assumed_value.{tag}"
            self.assertIn(fact_id, self.fs)
            self.assertEqual(
                self.value(fact_id),
                self.value(f"conversion_value.assumed.brand.{tag}")
                / self.value(f"spend.brand.{tag}"))

    def test_tof_spend_is_outside_the_nonbrand_roas_denominator(self):
        value = self.value("conversion_value.assumed.nonbrand.7d")
        spend = self.value("spend.nonbrand.7d")
        tof = self.value("spend.tof.7d")
        self.assertGreater(tof, 0)
        actual = self.value("roas.nonbrand.assumed_value.7d")
        self.assertEqual(actual, value / spend)
        self.assertNotEqual(actual, value / (spend + tof))

    def test_there_is_no_roas_fact_for_tof(self):
        for tag in ("7d", "prior7d"):
            self.assertNotIn(f"roas.tof.assumed_value.{tag}", self.fs)
        self.assertIn("conversion_value.assumed.tof.7d", self.fs)
        self.assertIn("spend.tof.7d", self.fs)

    def test_the_blended_roas_scope_warns_it_tracks_brand_volume(self):
        scope = self.fs.get("roas.account.assumed_value.7d").scope
        self.assertIn("BLENDED", scope)
        self.assertIn("guardrail 2", scope)

    def test_every_bucket_has_a_conversion_delta_fact(self):
        for key in ("account", "brand", "nonbrand", "tof"):
            self.assertIn(f"conversions.{key}.wow.delta", self.fs, key)

    def test_the_bucket_roas_deltas_are_third_facts(self):
        for base in ("roas.brand.assumed_value", "roas.nonbrand.assumed_value"):
            self.assertEqual(
                self.value(f"{base}.wow.delta"),
                self.value(f"{base}.7d") - self.value(f"{base}.prior7d"), base)


if __name__ == "__main__":
    unittest.main(verbosity=2)
