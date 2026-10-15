#!/usr/bin/env python3
"""Tests for the `budget-optimize` facts builder.

Run:  python3 tools/test_budget_optimize_facts.py

The tests that matter most are in SharedBudgetTests. They encode the fan-out
this account produces on every budget figure: `campaign_budget.id` is attached
to several campaigns at once, so a daily-budget total keyed on `campaign.id`
counts the same pool once per campaign. On the 2026-08-20 pull that reads CAD
7,900/day against a true CAD 5,840.50/day. Both numbers are individually
correct sums; only the key tells them apart. The builder must refuse the wrong
one, and `facts.py` must refuse to relate the two.

Standard library only.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest

import pulls
import testsupport as TS
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent

def _resolve_raw(date):
    """Raw pull dir, or a definitely-absent path so skipUnless skips cleanly.

    Runtime pulls are gitignored, so a fresh clone has none. That is a skip,
    never a failure, and never an empty-data pass.
    """
    try:
        return pulls.raw_dir(date)
    except FileNotFoundError:
        return Path("/nonexistent/google-ads/outputs/raw") / date

sys.path.insert(0, str(HERE))

import account_config as AC  # noqa: E402
import build_budget_optimize_facts as builder  # noqa: E402
import citations  # noqa: E402
import facts as F  # noqa: E402
import gaql  # noqa: E402
import scopes as S  # noqa: E402
from facts import FactMismatchError, FactSet  # noqa: E402

REPO = HERE.parent
AUG20 = TS.raw_or_fixture("2026-08-20")
#: True when AUG20 is the real pull rather than the synthetic fixture.
REAL_DATA = TS.using_real_data("2026-08-20")

#: Measured on the 2026-08-20 pull. If a re-pull changes these, the numbers in
#: `budget-optimize/SKILL.md` and in the builder's docstring change with them.
TRUE_DAILY_TOTAL = Decimal("5840.50")
CAMPAIGN_KEYED_DAILY_TOTAL = Decimal("7900.00")
SHARED_BUDGETS = 6
SHARED_CAMPAIGNS = 17


def _campaign_row(campaign_id, budget_id, micros, name="C", channel="SEARCH"):
    return {
        "campaign": {
            "id": campaign_id,
            "name": name,
            "status": "ENABLED",
            "advertisingChannelType": channel,
            "biddingStrategyType": "MAXIMIZE_CONVERSION_VALUE",
        },
        "campaignBudget": {
            "id": budget_id,
            "amountMicros": str(micros),
            "explicitlyShared": False,
        },
    }


@unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
class FixtureMixin:
    """Builds the facts file once for the whole module. It is not cheap."""

    fs: FactSet

    @classmethod
    def setUpClass(cls):
        if not AUG20.is_dir():
            raise unittest.SkipTest(f"{AUG20} is not present")
        cls.fs = builder.build(AUG20)
        cls.rows = builder.load_results(AUG20, builder.CAMPAIGNS)
        cls.clusters = builder.build_clusters(cls.rows)


# --------------------------------------------------------------------------
# The shared-budget fan-out. The reason this file exists.
# --------------------------------------------------------------------------


class SharedBudgetTests(unittest.TestCase, FixtureMixin):
    setUpClass = FixtureMixin.setUpClass

    def test_a_campaign_keyed_total_double_counts_a_shared_budget(self):
        """Three campaigns on one CAD 100/day pool is CAD 100, not CAD 300."""
        rows = [
            _campaign_row("1", "B1", 100_000_000),
            _campaign_row("2", "B1", 100_000_000),
            _campaign_row("3", "B1", 100_000_000),
        ]
        clusters = builder.build_clusters(rows)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(builder.sum_daily_budgets(clusters), Decimal("100"))
        self.assertEqual(builder.campaign_keyed_daily_total(rows), Decimal("300"))

    def test_the_builder_refuses_a_campaign_keyed_total(self):
        rows = [
            _campaign_row("1", "B1", 100_000_000),
            _campaign_row("2", "B1", 100_000_000),
        ]
        with self.assertRaises(builder.BuildError) as caught:
            builder.refuse_campaign_keyed_total(rows)
        message = str(caught.exception)
        self.assertIn("REFUSED", message)
        self.assertIn("campaign_budget.id", message)
        self.assertIn("VF-19", message)

    def test_the_refusal_fires_on_the_real_pull(self):
        with self.assertRaises(builder.BuildError):
            builder.refuse_campaign_keyed_total(self.rows)

    def test_the_refusal_does_not_fire_when_nothing_is_shared(self):
        rows = [
            _campaign_row("1", "B1", 100_000_000),
            _campaign_row("2", "B2", 100_000_000),
        ]
        builder.refuse_campaign_keyed_total(rows)  # must not raise

    # Asserts a fact about this account, not builder behaviour.

    # Asserts a fact about this account, not builder behaviour.

    def test_facts_py_refuses_to_relate_the_two_totals(self):
        """The scopes differ, so the guard blocks the comparison and the sum."""
        pair = (
            "budget.daily.total.by_budget.cad",
            "budget.daily.total.campaign_keyed_double_counted.cad",
        )
        with self.assertRaises(FactMismatchError):
            self.fs.compare(*pair)
        with self.assertRaises(FactMismatchError):
            self.fs.sum(list(pair))
        mismatches = [attr for attr, _, _ in self.fs.mismatches(*pair)]
        self.assertEqual(mismatches, ["scope"])

    def test_the_wrong_total_carries_its_warning_in_its_scope(self):
        scope = self.fs.get(
            "budget.daily.total.campaign_keyed_double_counted.cad"
        ).scope
        self.assertIn("WRONG", scope)
        self.assertIn("NEVER quote as a budget total", scope)

    # Asserts a fact about this account, not builder behaviour.

    def test_a_segmented_budget_amount_is_refused(self):
        rows = [
            _campaign_row("1", "B1", 100_000_000),
            _campaign_row("2", "B1", 250_000_000),
        ]
        with self.assertRaises(builder.BuildError) as caught:
            builder.build_clusters(rows)
        self.assertIn("two different", str(caught.exception))

    def test_a_row_with_no_budget_id_is_refused(self):
        rows = [_campaign_row("1", "B1", 100_000_000)]
        rows[0]["campaignBudget"].pop("id")
        with self.assertRaises(builder.BuildError) as caught:
            builder.build_clusters(rows)
        self.assertIn("only safe row key", str(caught.exception))

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_cluster_spend_is_summed_over_the_pool_not_one_campaign(self):
        """A shared pool's cost is the cluster's, so its facts must add up."""
        shared = [c for c in self.clusters.values() if c.shared]
        self.assertTrue(shared)
        cost = builder.metrics_by_campaign(
            builder.load_results(AUG20, builder.COST_7D), builder.COST_7D
        )
        for cluster in shared:
            per_campaign = sum(
                (
                    F.micros_to_currency((cost.get(cid) or {}).get("costMicros", "0"))
                    for cid in cluster.campaign_ids
                ),
                Decimal(0),
            )
            fact_id = f"cluster.{cluster.budget_id}.cost.cad"
            if fact_id in self.fs:
                self.assertEqual(F.money(per_campaign), F.money(self.fs.value(fact_id)))
                self.assertEqual(
                    self.fs.value(f"cluster.{cluster.budget_id}.campaign.count"),
                    len(cluster.campaigns),
                )

    def test_a_shared_pools_daily_budget_fact_is_the_pool_not_a_multiple(self):
        for cluster in self.clusters.values():
            if not cluster.shared or cluster.daily_cad is None:
                continue
            fact_id = f"cluster.{cluster.budget_id}.daily_budget.cad"
            if fact_id in self.fs:
                self.assertEqual(
                    F.money(self.fs.value(fact_id)), F.money(cluster.daily_cad)
                )


# --------------------------------------------------------------------------
# Read, never derive (campaign-guardrails rule 8)
# --------------------------------------------------------------------------


class BudgetFigureTests(unittest.TestCase, FixtureMixin):
    setUpClass = FixtureMixin.setUpClass

    def test_august_is_the_read_figure_not_a_derived_one(self):
        self.assertEqual(
            F.money(self.fs.value("pacing.august.budget.cad")), Decimal("54400.00")
        )
        self.assertEqual(
            F.money(self.fs.value("pacing.august.budget.usd")), Decimal("40000.00")
        )
        self.assertEqual(
            F.money(self.fs.value("pacing.august.budget.usd"))
            * Decimal(str(self.fs.value("pacing.fx.usd_to_cad.ratio"))),
            F.money(self.fs.value("pacing.august.budget.cad")),
        )
        scope = self.fs.get("pacing.august.budget.cad").scope
        self.assertIn("read from the owner's Google Ads tab", scope)
        self.assertIn("NEVER derived", scope)

    def test_the_wrong_derived_figure_appears_nowhere(self):
        """CAD 97,315 was the share-of-total derivation. Out by 22%."""
        for fact in self.fs:
            self.assertNotIn("97315", str(fact.value).replace(",", ""))
            self.assertNotIn("97,315", str(fact.value))

    def test_the_months_with_no_stated_split_are_null_not_derived(self):
        months = AC.months_with_no_stated_split()
        self.assertIn("2026-09", months)   # the September time bomb, defused
        for month in months:
            slug = builder.month_plan(month).slug
            fact = self.fs.get(f"pacing.{slug}.google_ads_budget.cad")
            self.assertIsNone(fact.value)
            self.assertIn("no stated Google Ads split", fact.scope)
            self.assertIn("Never derive", fact.scope)

    def test_the_planned_step_down_is_labelled_the_plan(self):
        fact = self.fs.get("pacing.planned_step_down_from_july.pct")
        self.assertEqual(Decimal(str(fact.value)), Decimal("12.5"))
        self.assertIn("THE PLAN, NOT A FINDING", fact.scope)


# --------------------------------------------------------------------------
# Real pacing
# --------------------------------------------------------------------------


class PacingTests(unittest.TestCase, FixtureMixin):
    setUpClass = FixtureMixin.setUpClass

    def test_pacing_is_spend_to_date_against_budget_prorated_by_day(self):
        spend = Decimal(str(self.fs.value("pacing.mtd.spend.cad")))
        days = self.fs.value("pacing.mtd.days.count")
        month_days = self.fs.value("pacing.august.days.count")
        budget = Decimal(str(self.fs.value("pacing.august.budget.cad")))
        expected = budget * days / month_days
        self.assertEqual(
            F.money(expected), F.money(self.fs.value("pacing.budget_to_date.cad"))
        )
        self.assertEqual(
            F.money(spend / expected * 100),
            F.money(self.fs.value("pacing.vs_budget_to_date.pct")),
        )

    # Asserts a fact about this account, not builder behaviour.

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_spend_to_date_is_recomputed_from_this_runs_pull(self):
        """Not restated from budget-plan.md, whose figure stops at Aug 18."""
        rows = builder.load_results(AUG20, builder.SPEND_MTD)
        total = sum(
            (
                F.micros_to_currency((r.get("metrics") or {}).get("costMicros"))
                for r in rows
            ),
            Decimal(0),
        )
        self.assertEqual(F.money(total), F.money(self.fs.value("pacing.mtd.spend.cad")))
        self.assertEqual(self.fs.value("pacing.mtd.days.count"), len(rows))
        self.assertNotEqual(
            F.money(self.fs.value("pacing.mtd.spend.cad")), Decimal("41960.52")
        )

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_a_duplicate_spend_date_is_refused(self):
        rows = builder.load_results(AUG20, builder.SPEND_MTD)
        doubled = rows + [copy.deepcopy(rows[0])]
        with self.assertRaises(builder.BuildError) as caught:
            builder.add_pacing(
                FactSet(), doubled, {"spend": "w"}, builder.month_plan("2026-08")
            )
        self.assertIn("appears twice", str(caught.exception))

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_a_month_with_no_stated_split_paces_null_with_reason(self):
        """September builds; its budget-dependent facts are null, never derived.

        The old contract refused the whole build, which meant the pack's budget
        skill stopped working on the first of any month without a stated split.
        The rule 8 protection survives in a stricter form: the spend-side facts
        are real, and every fact that would need a budget figure is null with
        the config's reason - so a September run can never pace September
        spend against August's figure, and a report citing the null fact says
        why the number is unavailable instead of dropping the line.
        """
        rows = builder.load_results(AUG20, builder.SPEND_MTD)
        september = copy.deepcopy(rows)
        for row in september:
            row["segments"]["date"] = row["segments"]["date"].replace(
                "2026-08", "2026-09"
            )
        plan = builder.spend_month_plan(september)
        self.assertEqual(plan.month, "2026-09")
        self.assertFalse(plan.stated)
        fs = FactSet()
        builder.add_pacing(fs, september, {"spend": "w"}, plan)
        # Budget-dependent: null, with the reason on the fact.
        for fact_id in (
            "pacing.september.budget.cad",
            "pacing.budget_to_date.cad",
            "pacing.vs_budget_to_date.pct",
            "pacing.budget_consumed.pct",
            "pacing.remaining.budget.cad",
            "pacing.required_daily_cut.pct",
        ):
            fact = fs.get(fact_id)
            self.assertIsNone(fact.value, fact_id)
        self.assertIn(
            "no stated Google Ads split",
            fs.get("pacing.september.budget.cad").scope,
        )
        # Spend-side: real figures from the pull, unaffected by the null budget.
        self.assertIsNotNone(fs.value("pacing.mtd.spend.cad"))
        self.assertIsNotNone(fs.value("pacing.runrate_forecast.cad"))
        self.assertIsNotNone(fs.value("pacing.last_7_days.avg_daily.cad"))
        # And no August figure leaked anywhere.
        for fact in fs:
            self.assertNotIn("54400", str(fact.value).replace(",", ""))

    def test_a_multi_month_spend_pull_is_refused(self):
        rows = builder.load_results(AUG20, builder.SPEND_MTD)
        mixed = copy.deepcopy(rows)
        mixed[-1]["segments"]["date"] = mixed[-1]["segments"]["date"].replace(
            "2026-08", "2026-09"
        )
        with self.assertRaises(builder.BuildError) as caught:
            builder.spend_month_plan(mixed)
        self.assertIn("REFUSED to pace", str(caught.exception))

    def test_a_month_the_config_does_not_know_refuses_loudly(self):
        rows = builder.load_results(AUG20, builder.SPEND_MTD)
        future = copy.deepcopy(rows)
        for row in future:
            row["segments"]["date"] = row["segments"]["date"].replace(
                "2026-08", "2027-01"
            )
        with self.assertRaises(builder.BuildError) as caught:
            builder.spend_month_plan(future)
        self.assertIn("knows nothing about month", str(caught.exception))

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_a_september_raw_dir_builds_with_null_pacing_facts(self):
        """The September time bomb, defused: a full build of a month with no
        stated split succeeds, writes real spend-side facts, and writes every
        budget-dependent fact null with the config's reason (rule 8)."""
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "2026-09-20"
            staged.mkdir()
            # REQUIRED only. The prior-week pulls are OPTIONAL and their
            # real August windows cannot be adjacent to a September current
            # week, so staging them would (correctly) trip the week-pair
            # guard - this test is about pacing, not WoW.
            for name in builder.REQUIRED:
                source = AUG20 / name
                if source.exists():
                    (staged / name).write_text(
                        source.read_text(encoding="utf-8"), encoding="utf-8"
                    )
            manifest = json.loads((AUG20 / S.MANIFEST).read_text(encoding="utf-8"))
            for name in builder.OPTIONAL:
                manifest["files"].pop(name, None)
            payload = json.loads(
                (staged / builder.SPEND_MTD).read_text(encoding="utf-8")
            )
            for row in payload["data"]["results"]:
                row["segments"]["date"] = row["segments"]["date"].replace(
                    "2026-08", "2026-09"
                )
            (staged / builder.SPEND_MTD).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            entry = manifest["files"][builder.SPEND_MTD]
            entry["temporality"] = "window:2026-09-01..2026-09-19"
            entry["filter"] = (
                "segments.date BETWEEN '2026-09-01' AND '2026-09-19'"
            )
            manifest["files"]["campaigns-current.json"]["temporality"] = (
                "current-as-of-2026-09-20"
            )
            (staged / S.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            fs = builder.build(staged)
            self.assertIsNone(fs.get("pacing.september.budget.cad").value)
            self.assertIn(
                "no stated Google Ads split",
                fs.get("pacing.september.budget.cad").scope,
            )
            self.assertIsNone(fs.get("pacing.budget_to_date.cad").value)
            self.assertIsNotNone(fs.value("pacing.mtd.spend.cad"))
            self.assertEqual(fs.value("pacing.september.days.count"), 30)
            for fact in fs:
                if fact.id.startswith("pacing."):
                    self.assertNotIn(
                        "54400", str(fact.value).replace(",", ""), fact.id
                    )

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_a_stated_month_paces_and_says_so(self):
        plan = builder.spend_month_plan(
            builder.load_results(AUG20, builder.SPEND_MTD)
        )
        self.assertEqual(plan.month, "2026-08")
        self.assertTrue(plan.stated)
        self.assertEqual(F.money(plan.budget_cad), Decimal("54400.00"))

    def test_the_account_spend_pull_declares_that_it_has_no_status_filter(self):
        note = self.fs.get("pacing.mtd.scope_note").value
        self.assertIn("no campaign or status filter", note)
        self.assertIn("paused and removed", note)

    def test_pacing_facts_are_not_comparable_to_enabled_only_totals(self):
        with self.assertRaises(FactMismatchError):
            self.fs.compare(
                "pacing.mtd.spend.cad", "budget.daily.total.by_budget.cad"
            )


# --------------------------------------------------------------------------
# Learning windows (campaign-guardrails rule 7)
# --------------------------------------------------------------------------


@unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
class LearningWindowTests(unittest.TestCase, FixtureMixin):
    setUpClass = FixtureMixin.setUpClass

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_only_budget_and_target_changes_are_collected(self):
        """A keyword or asset edit cannot downgrade a budget recommendation."""
        changes = builder.read_changes(builder.load_results(AUG20, builder.CHANGES))
        kinds = {kind for events in changes.values() for _, kind, _ in events}
        self.assertEqual(kinds, {"CAMPAIGN_BUDGET", "CAMPAIGN target/bid"})

    def test_a_criterion_change_is_not_collected(self):
        rows = [
            {
                "changeEvent": {
                    "campaign": "customers/1/campaigns/9",
                    "changeDateTime": "2026-08-18 10:00:00",
                    "changeResourceType": "AD_GROUP_CRITERION",
                    "changedFields": "keyword.text",
                }
            }
        ]
        self.assertEqual(builder.read_changes(rows), {})

    def test_a_campaign_name_change_is_not_a_target_change(self):
        rows = [
            {
                "changeEvent": {
                    "campaign": "customers/1/campaigns/9",
                    "changeDateTime": "2026-08-18 10:00:00",
                    "changeResourceType": "CAMPAIGN",
                    "changedFields": "name,trackingUrlTemplate",
                }
            }
        ]
        self.assertEqual(builder.read_changes(rows), {})

    # Asserts a fact about this account, not builder behaviour.

    def test_every_active_cluster_carries_a_change_date_and_a_change_type(self):
        """Rule 7: a downgrade missing either field is incomplete."""
        for fact in self.fs:
            if fact.id.endswith(".last_budget_or_target_change.date"):
                prefix = fact.id.rsplit(".", 2)[0]
                self.assertIn(f"{prefix}.last_budget_or_target_change.type", self.fs)
                self.assertTrue(str(fact.value))

    def test_a_null_cleared_fact_means_absence_only_never_a_found_change(self):
        """VF-20: assert the contract, not whatever this run happens to hold.

        A 28-day window cannot be *cleared* by a 14-day pull, because absence
        of a change in days 15 to 28 is unprovable. But a change found INSIDE
        the window settles the question outright, so a null must never appear
        on a cluster that has a change date. The predecessor of this test
        asserted at least one null existed, which pinned the line-898 ordering
        bug as expected behaviour.
        """
        self.assertEqual(self.fs.value("learning.pmax.window.days"), 28)
        self.assertEqual(self.fs.value("learning.change_event_pull.coverage.days"), 14)
        for fact in self.fs:
            if not fact.id.endswith(".learning_window.cleared"):
                continue
            prefix = fact.id.rsplit(".", 2)[0]
            date = self.fs.value(f"{prefix}.last_budget_or_target_change.date")
            found = date is not None and not str(date).startswith("none")
            if fact.value is None:
                self.assertFalse(
                    found,
                    f"{fact.id} is null while {prefix} carries change date "
                    f"{date!r}. A found change settles the window.",
                )
                self.assertIn("UNANSWERABLE", fact.scope)
                self.assertIn("ABSENCE", fact.scope)
            elif found:
                self.assertFalse(
                    fact.value,
                    f"{fact.id} claims a clear window while {prefix} carries "
                    f"change date {date!r}",
                )

    def test_every_constrained_cluster_is_in_learning_this_run(self):
        """The honest answer this run: no budget increase survives rule 7."""
        over = self.fs.value("constraint.over_20pct.budget.count")
        in_learning = self.fs.value("constraint.over_20pct.in_learning.budget.count")
        recommendable = self.fs.value(
            "constraint.over_20pct.recommendable.budget.count"
        )
        self.assertEqual(over, in_learning + recommendable)
        self.assertEqual(recommendable, 0)


# --------------------------------------------------------------------------
# Guardrails 1, 2 and 5
# --------------------------------------------------------------------------


@unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
class GuardrailTests(unittest.TestCase, FixtureMixin):
    setUpClass = FixtureMixin.setUpClass

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_no_efficiency_fact_exists_below_the_conversion_floor(self):
        """Rule 5 is enforced by absence, so a report cannot cite a thin CPA."""
        conv = builder.conversions_by_campaign(
            builder.load_results(AUG20, builder.CONV_7D), builder.CONV_7D
        )
        for cluster in self.clusters.values():
            conversions = builder.cluster_conversions(cluster, conv)
            has_cpa = f"cluster.{cluster.budget_id}.assumed_value_cpa.cad" in self.fs
            if conversions < builder.CONVERSION_FLOOR or cluster.is_tof:
                self.assertFalse(has_cpa, cluster.budget_id)
            else:
                self.assertTrue(has_cpa, cluster.budget_id)

    def test_top_of_funnel_channels_never_get_an_efficiency_fact(self):
        for cluster in self.clusters.values():
            if cluster.is_tof:
                self.assertNotIn(
                    f"cluster.{cluster.budget_id}.assumed_value_cpa.cad", self.fs
                )
                self.assertNotIn(
                    f"cluster.{cluster.budget_id}.assumed_value_roas.ratio", self.fs
                )

    def test_every_efficiency_fact_is_on_the_8_action_basis_and_says_assumed(self):
        found = 0
        for fact in self.fs:
            if ".assumed_value_" in fact.id:
                found += 1
                self.assertEqual(fact.basis, "8-action")
                self.assertIn("assumed", fact.scope.lower())
        self.assertGreater(found, 0)

    def test_a_real_money_anchor_exists_for_every_assumed_value_figure(self):
        self.assertIsNotNone(self.fs.value("anchor.real_cac.april_2026.cad"))
        self.assertIsNotNone(
            self.fs.value("anchor.real_cost_per_free_trial.april_2026.cad")
        )

    def test_brand_is_not_a_growth_lever_unless_rank_lost_is_rose(self):
        current = Decimal(
            str(self.fs.value("brand.core.rank_lost_is.current_week.ratio"))
        )
        prior = Decimal(str(self.fs.value("brand.core.rank_lost_is.prior_week.ratio")))
        met = self.fs.value("brand.core.growth_trigger_met")
        self.assertEqual(met, current > prior)
        self.assertFalse(met, "rank-lost IS fell, so no Brand recommendation")
        self.assertIn("ONLY admissible trigger", self.fs.get(
            "brand.core.rank_lost_is.current_week.ratio"
        ).scope)

    def test_both_brand_weeks_are_quoted_and_the_override_is_on_the_record(self):
        unsafe = self.fs.unsafe_comparisons
        matches = [
            r for r in unsafe
            if r["id_a"] == "brand.core.rank_lost_is.current_week.ratio"
        ]
        self.assertEqual(len(matches), 1)
        record = matches[0]
        self.assertEqual(record["id_b"], "brand.core.rank_lost_is.prior_week.ratio")
        self.assertEqual([m["attribute"] for m in record["mismatches"]], ["window"])
        self.assertIn("rule 2", record["justification"])

    def test_brand_and_nonbrand_subtotals_exist_separately(self):
        self.assertIn("subtotal.brand.cost.cad", self.fs)
        self.assertIn("subtotal.nonbrand.cost.cad", self.fs)
        with self.assertRaises(F.UnknownFactError):
            self.fs.get("subtotal.blended.cost.cad")

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_the_subtotals_reconcile_to_the_accounts_seven_day_cost(self):
        cost = builder.metrics_by_campaign(
            builder.load_results(AUG20, builder.COST_7D), builder.COST_7D
        )
        total = sum(
            (
                F.micros_to_currency(m.get("costMicros", "0"))
                for m in cost.values()
            ),
            Decimal(0),
        )
        buckets = [
            f
            for f in self.fs
            if f.id.startswith("subtotal.") and f.id.endswith(".cost.cad")
        ]
        subtotals = sum((Decimal(str(f.value)) for f in buckets), Decimal(0))
        # Each subtotal is rounded to the cent before it is stored, so the sum
        # may differ from the account total by up to half a cent per bucket.
        # Anything larger is a campaign in the wrong bucket or in none.
        tolerance = Decimal("0.005") * len(buckets)
        self.assertLessEqual(abs(F.money(total) - subtotals), tolerance)

    def test_nonbrand_named_campaigns_are_not_bucketed_as_brand(self):
        """`Search-NonBrand-T1 - tROAS` contains the word Brand."""
        self.assertEqual(
            builder.bucket_of(
                {"id": "x", "name": "Search-NonBrand-T1 - tROAS",
                 "advertisingChannelType": "SEARCH"}
            ),
            "nonbrand",
        )
        self.assertEqual(
            builder.bucket_of(
                {"id": "x", "name": "Search-Brand - tROAS",
                 "advertisingChannelType": "SEARCH"}
            ),
            "brand",
        )

    # Asserts a fact about this account, not builder behaviour.

    def test_elasticity_bands_exist_and_the_third_is_null(self):
        self.assertEqual(
            F.money(self.fs.value("elasticity.band_1.max_increase.usd_per_month")),
            Decimal("10000.00"),
        )
        self.assertEqual(
            F.money(self.fs.value("elasticity.band_2.max_increase.usd_per_month")),
            Decimal("20000.00"),
        )
        self.assertEqual(
            F.money(self.fs.value("elasticity.band_2.cpa_degradation.pct")),
            Decimal("12.00"),
        )
        band3 = self.fs.get("elasticity.band_3.max_increase.usd_per_month")
        self.assertIsNone(band3.value)
        self.assertIn("no data exists", band3.scope)


# --------------------------------------------------------------------------
# Scope, freshness and the contract
# --------------------------------------------------------------------------


class ContractTests(unittest.TestCase, FixtureMixin):
    setUpClass = FixtureMixin.setUpClass

    def test_the_two_query_rule_shows_in_every_efficiency_facts_source(self):
        for fact in self.fs:
            if ".assumed_value_" in fact.id:
                self.assertIn(builder.COST_7D, fact.source)
                self.assertIn(builder.CONV_7D, fact.source)
                self.assertIn("cannot share one query", fact.scope)

    # Asserts a fact about this account, not builder behaviour.

    def test_a_current_state_fact_cannot_be_compared_to_a_windowed_one(self):
        with self.assertRaises(FactMismatchError) as caught:
            self.fs.compare(
                "budget.daily.total.by_budget.cad",
                "pacing.mtd.spend.cad",
            )
        self.assertIn("window", str(caught.exception))

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_temporality_comes_from_the_manifest_and_is_never_inferred(self):
        manifest = S.Manifest(AUG20)
        for name in builder.REQUIRED:
            self.assertTrue(manifest.declared_temporality(name), name)
        self.assertTrue(manifest.is_current_state(builder.CAMPAIGNS))
        self.assertFalse(manifest.is_current_state(builder.COST_7D))

    def test_the_builder_refuses_a_raw_dir_with_no_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "2026-08-20"
            empty.mkdir()
            with self.assertRaises(builder.BuildError) as caught:
                builder.build(empty)
            self.assertIn("VF-17 rule 5", str(caught.exception))

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_the_builder_refuses_when_a_scope_label_drifts_from_the_query(self):
        manifest = S.Manifest(AUG20)
        with self.assertRaises(gaql.FilterMismatch) as caught:
            builder.verify_scope(
                manifest, builder.CAMPAIGNS, "campaign.status=PAUSED"
            )
        self.assertIn("VF-19a", str(caught.exception))
        # main() must surface it as a refusal, not a traceback, so the
        # exception has to be in the except clause it actually catches.
        import inspect
        clause = [
            line
            for line in inspect.getsource(builder.main).splitlines()
            if line.strip().startswith("except ")
        ]
        self.assertTrue(clause)
        self.assertTrue(any("gaql.FilterMismatch" in line for line in clause))

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_a_between_range_passes_the_shared_temporality_check(self):
        """The shared checker now reads a BETWEEN range.

        This test used to assert the opposite - it pinned a bug in
        `gaql.check_declared_temporality`, which substring-tested
        `2026-08-01..2026-08-19` against `BETWEEN 2026-08-01 AND 2026-08-19` and
        so failed every correctly-declared range. That justified a local
        re-implementation here. The shared function was fixed 2026-08-20, so the
        assertion is inverted: both paths must now agree.

        A test that pins a bug as expected behaviour outlives the bug and then
        blocks the fix. Assert the contract, not the defect.
        """
        manifest = S.Manifest(AUG20)
        line = builder.verify_temporality(manifest, builder.SPEND_MTD)
        self.assertIn("agrees", line)
        shared = gaql.check_declared_temporality(
            manifest.declared_filter(builder.SPEND_MTD),
            manifest.declared_temporality(builder.SPEND_MTD),
        )
        self.assertIn("agrees", shared)

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_a_mislabelled_between_range_is_refused(self):
        manifest = S.Manifest(AUG20)
        manifest.files = json.loads(json.dumps(manifest.files))
        manifest.files[builder.SPEND_MTD]["temporality"] = (
            "window:2026-08-01..2026-08-31"
        )
        with self.assertRaises(builder.BuildError) as caught:
            builder.verify_temporality(manifest, builder.SPEND_MTD)
        self.assertIn("different ranges", str(caught.exception))

    def test_the_impression_share_sentinel_is_suppressed_per_field(self):
        """VF-11. The lost-IS fields on the same row stay real."""
        suppressed = [
            f
            for f in self.fs
            if f.id.endswith(".impression_share.ratio")
            and f.value is None
            and "sentinel" in f.scope
        ]
        self.assertTrue(suppressed)
        for fact in suppressed:
            prefix = fact.id.rsplit(".", 2)[0]
            lost = self.fs.get(f"{prefix}.lost_is_budget.ratio")
            self.assertIsNotNone(lost.value, prefix)
            self.assertIn("not suppressed", fact.scope)

    def test_every_fact_declares_a_legal_basis_and_a_nonempty_scope(self):
        for fact in self.fs:
            self.assertIn(fact.basis, F.LEGAL_BASES, fact.id)
            self.assertTrue(fact.scope.strip(), fact.id)
            self.assertTrue(fact.window.strip(), fact.id)
            self.assertTrue(fact.source.strip(), fact.id)

    def test_every_percentage_unit_declares_its_scale(self):
        """VF-15a rule 3: a unit declaring neither scale is a failure."""
        for fact in self.fs:
            if fact.unit in ("percent", "ratio"):
                self.assertIsNotNone(F.percentage_convention(fact.unit), fact.id)

    def test_the_file_is_small_enough_for_a_citation_to_mean_something(self):
        """VF-15a rule 6. The 6,448-row file is what made coincidence likely."""
        self.assertLess(len(self.fs), 600)

    def test_the_facts_file_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget-optimize-facts.json"
            self.fs.write(path)
            reloaded = FactSet.load(path)
            self.assertEqual(len(reloaded), len(self.fs))
            self.assertEqual(sorted(reloaded.ids()), sorted(self.fs.ids()))
            self.assertEqual(
                len(reloaded.unsafe_comparisons), len(self.fs.unsafe_comparisons)
            )

    def test_citations_can_resolve_a_report_figure_written_from_the_file(self):
        """A cited figure is a lookup, not a value search (VF-15a)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            facts_path = tmp / "budget-optimize-facts.json"
            self.fs.write(facts_path)
            report = tmp / "report.md"
            over = self.fs.value("pacing.over_budget_to_date.pct")
            report.write_text(
                "August is running "
                f"{over}% over its budget to date "
                "[fact: pacing.over_budget_to_date.pct].\n",
                encoding="utf-8",
            )
            problems = citations.check(str(report), str(facts_path))
            self.assertEqual(problems, [], [p.kind for p in problems])

    def test_citations_catches_a_figure_that_does_not_match_its_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            facts_path = tmp / "budget-optimize-facts.json"
            self.fs.write(facts_path)
            report = tmp / "report.md"
            report.write_text(
                "August is running 12.5% over its budget to date "
                "[fact: pacing.over_budget_to_date.pct].\n",
                encoding="utf-8",
            )
            problems = citations.check(str(report), str(facts_path))
            self.assertTrue(problems)
            self.assertIn("value_mismatch", [p.kind for p in problems])


# --------------------------------------------------------------------------
# The Aug-17 change, now in the past
# --------------------------------------------------------------------------


class TargetTests(unittest.TestCase, FixtureMixin):
    setUpClass = FixtureMixin.setUpClass

    # Asserts a fact about this account, not builder behaviour.

    def test_the_target_reader_prefers_the_field_that_actually_holds_it(self):
        campaign = {
            "id": "1",
            "maximizeConversionValue": {"targetRoas": 1.4},
            "targetRoas": {},
        }
        kind, value, field = builder.read_target(campaign)
        self.assertEqual(kind, "target_roas")
        self.assertEqual(value, Decimal("1.4"))
        self.assertEqual(field, "campaign.maximizeConversionValue.targetRoas")
        self.assertEqual(builder.read_target({"id": "2"}), (None, None, None))

    def test_a_target_cpa_is_read_from_micros(self):
        kind, value, _ = builder.read_target(
            {"id": "1", "targetCpa": {"targetCpaMicros": "450000000"}}
        )
        self.assertEqual(kind, "legacy_target_cpa")
        self.assertEqual(value, Decimal("450"))

    def test_no_search_campaign_carries_a_target_cpa(self):
        """So a rule phrased as a multiple of target CPA has nothing to use."""
        self.assertEqual(
            self.fs.value("targets.target_cpa.search_channel.campaign.count"), 0
        )

    def test_every_target_fact_flags_the_goal_basis_mismatch(self):
        found = 0
        for fact in self.fs:
            if fact.id.startswith("campaign.") and fact.id.endswith(".value"):
                found += 1
                self.assertIn("account-default goal set", fact.scope)
        self.assertEqual(found, self.fs.value("targets.set.campaign.count"))

    # Asserts a fact about this account, not builder behaviour.

    # Asserts a fact about this account, not builder behaviour.

    def test_beating_target_is_direction_only_and_says_so(self):
        for fact in self.fs:
            if fact.id.endswith(".beating_target_on_8_actions"):
                self.assertIn("DIRECTION ONLY", fact.scope)
                self.assertIn("different bases", fact.scope)
                self.assertEqual(fact.basis, "8-action")

    def test_the_beating_flag_matches_the_two_underlying_facts(self):
        for fact in self.fs:
            if not fact.id.endswith(".beating_target_on_8_actions"):
                continue
            prefix = fact.id.rsplit(".", 1)[0]
            actual = Decimal(str(self.fs.value(f"{prefix}.assumed_value_roas.ratio")))
            target = Decimal(str(self.fs.value(f"{prefix}.target_roas.ratio")))
            self.assertEqual(fact.value, actual > target)

    # Asserts a fact about this account, not builder behaviour.

    # Asserts a fact about this account, not builder behaviour.

    def test_the_date_has_passed_and_no_countdown_fact_exists(self):
        self.assertTrue(self.fs.value("aug17.date_has_passed"))
        for fact in self.fs:
            self.assertNotIn("days_remaining", fact.id)
            self.assertNotIn("days_until", fact.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
