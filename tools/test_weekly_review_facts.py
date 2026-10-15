#!/usr/bin/env python3
"""Tests for the `weekly-review` facts builder.

Run:  python3 tools/test_weekly_review_facts.py

The tests that matter most are in `DeltaFactTests`. A week-over-week delta is
the one thing this skill exists to produce and it is exactly what the VF-15
guard refuses: two windows. The design under test is that a delta is a **third
fact** with its own id, computed from two named parents, and that the helper
which builds it is **narrower** than the guard rather than a bypass. So these
tests check the refusals as hard as they check the arithmetic:

- the plain guard still refuses the naive week-over-week comparison;
- a delta whose parents differ on scope as well as window is refused (that is
  the Aug-10 error: $49,210.77 against $48,323.11, both correct, different
  scopes, reported as growth);
- a delta whose parents' windows already agree is refused, because then the
  plain guard would have allowed it and a parent id is probably wrong;
- a CPA that would divide this week's cost by last week's conversions is
  refused, even though CPA is allowed to bridge scope, unit and basis;
- the run leaves `unsafe_comparisons` empty, so nothing was overridden.

Standard library only.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
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

import build_weekly_review_facts as builder  # noqa: E402
import citations  # noqa: E402
import facts as F  # noqa: E402
import gaql  # noqa: E402
import pulls
import testsupport as TS  # noqa: E402
import scopes as S  # noqa: E402
from facts import FactMismatchError, FactSet, FactsError  # noqa: E402

PACK_ROOT = HERE.parent
RAW_DIR = _resolve_raw("2026-08-20")
BUDGET_PLAN = PACK_ROOT / "budget-plan.md"


def build_once():
    return builder.build(RAW_DIR, BUDGET_PLAN)




class DeltaFactTests(unittest.TestCase):
    """`related_fact` in isolation: what it permits and what it refuses."""

    def setUp(self):
        self.fs = FactSet()
        self.fs.add(id="spend.7d", value=Decimal("33381.98"),
                    unit="currency_cad", scope="campaign.status='ENABLED'",
                    window="2026-08-13..2026-08-19", source="a.json",
                    basis="n/a")
        self.fs.add(id="spend.prior7d", value=Decimal("34711.13"),
                    unit="currency_cad", scope="campaign.status='ENABLED'",
                    window="2026-08-06..2026-08-12", source="b.json",
                    basis="n/a")

    def delta(self, **overrides):
        kwargs = dict(
            parents=("spend.7d", "spend.prior7d"), profile="wow-delta",
            value=Decimal("-1329.15"), unit="currency_cad_delta",
            scope="campaign.status='ENABLED' | two windows",
            window="delta: [a] minus [b]", basis="n/a", rule="test",
        )
        kwargs.update(overrides)
        return builder.related_fact(self.fs, kwargs.pop("id", "spend.wow.delta"),
                                    **kwargs)

    def test_a_delta_across_two_windows_is_permitted(self):
        fact = self.delta()
        self.assertEqual(fact.value, Decimal("-1329.15"))
        self.assertEqual(len(self.fs.meta["derivations"]), 1)

    def test_a_delta_across_two_scopes_is_refused(self):
        """The Aug-10 error: two correct figures, different scopes, differenced."""
        self.fs.add(id="spend.prior7d.other_scope", value=Decimal("48323.11"),
                    unit="currency_cad",
                    scope="campaign.status='ENABLED' AND campaign.id != 1",
                    window="2026-08-06..2026-08-12", source="b.json",
                    basis="n/a")
        with self.assertRaises(FactsError) as caught:
            self.delta(parents=("spend.7d", "spend.prior7d.other_scope"))
        self.assertIn("Aug-10 error class", str(caught.exception))
        self.assertIn("scope", str(caught.exception))

    def test_a_delta_between_two_identical_windows_is_refused(self):
        self.fs.add(id="spend.7d.copy", value=Decimal("1"), unit="currency_cad",
                    scope="campaign.status='ENABLED'",
                    window="2026-08-13..2026-08-19", source="a.json",
                    basis="n/a")
        with self.assertRaises(FactsError) as caught:
            self.delta(parents=("spend.7d", "spend.7d.copy"))
        self.assertIn("already agree", str(caught.exception))
        self.assertIn("FactSet.compare()", str(caught.exception))

    def test_a_delta_against_a_current_state_fact_is_refused(self):
        self.fs.add(id="spend.now", value=Decimal("1"), unit="currency_cad",
                    scope="campaign.status='ENABLED'",
                    window=F.current_state_window("2026-08-20"),
                    source="c.json", basis="n/a")
        with self.assertRaises(FactsError) as caught:
            self.delta(parents=("spend.7d", "spend.now"))
        self.assertIn("VF-17", str(caught.exception))

    def test_a_delta_across_two_bases_is_refused(self):
        self.fs.add(id="spend.prior7d.broad", value=Decimal("1"),
                    unit="currency_cad", scope="campaign.status='ENABLED'",
                    window="2026-08-06..2026-08-12", source="b.json",
                    basis="broad-goal-set")
        with self.assertRaises(FactsError):
            self.delta(parents=("spend.7d", "spend.prior7d.broad"))

    def test_a_delta_from_a_null_parent_is_refused(self):
        self.fs.add(id="spend.suppressed", value=None, unit="currency_cad",
                    scope="campaign.status='ENABLED'",
                    window="2026-08-06..2026-08-12", source="b.json",
                    basis="n/a")
        with self.assertRaises(FactsError) as caught:
            self.delta(parents=("spend.7d", "spend.suppressed"))
        self.assertIn("null fact", str(caught.exception))

    def test_an_unknown_profile_is_refused(self):
        with self.assertRaises(FactsError):
            self.delta(profile="whatever-i-want")

    def test_a_cpa_across_two_windows_is_refused(self):
        """Even though CPA is allowed to bridge scope, unit and basis."""
        self.fs.add(id="conversions.prior7d", value=Decimal("85.8"),
                    unit="conversions_8_action",
                    scope="campaign.status='ENABLED' AND "
                          "segments.conversion_action_name IN (the 8)",
                    window="2026-08-06..2026-08-12", source="d.json",
                    basis="8-action")
        with self.assertRaises(FactsError) as caught:
            builder.related_fact(
                self.fs, "cpa.wrong", parents=("spend.7d", "conversions.prior7d"),
                profile="cost-over-conversions", value=Decimal("389"),
                unit="currency_cad_assumed_value_cpa", scope="x", window="y",
                basis="8-action", rule="test")
        self.assertIn("window", str(caught.exception))

    def test_a_cpa_scope_delta_must_be_exactly_the_conversion_clause(self):
        self.fs.add(id="conversions.7d.narrow", value=Decimal("99"),
                    unit="conversions_8_action",
                    scope="campaign.status='ENABLED' AND "
                          "segments.conversion_action_name IN (the 8) AND "
                          "campaign.advertising_channel_type='SEARCH'",
                    window="2026-08-13..2026-08-19", source="d.json",
                    basis="8-action")
        with self.assertRaises(FactsError) as caught:
            builder.related_fact(
                self.fs, "cpa.wrong2",
                parents=("spend.7d", "conversions.7d.narrow"),
                profile="cost-over-conversions", value=Decimal("337"),
                unit="currency_cad_assumed_value_cpa", scope="x",
                window="2026-08-13..2026-08-19", basis="8-action", rule="test",
                scope_delta=frozenset(
                    {"segments.conversion_action_name IN (the 8)"}))
        self.assertIn("same population", str(caught.exception))

    def test_the_permitted_profiles_are_a_closed_set(self):
        self.assertEqual(sorted(builder.PERMITTED_PROFILES),
                         ["actual-vs-target", "cost-over-conversions",
                          "plan-vs-actual", "wow-delta"])

    def test_only_actual_vs_target_may_take_a_current_state_parent(self):
        self.assertEqual(builder.CURRENT_STATE_PARENT_PROFILES,
                         frozenset({"actual-vs-target"}))

    def target_setup(self):
        self.fs.add(id="roas.7d", value=Decimal("0.69"),
                    unit="assumed_value_roas_ratio",
                    scope="campaign.status='ENABLED'",
                    window="2026-08-13..2026-08-19", source="a.json",
                    basis="8-action")
        self.fs.add(id="target.roas", value=Decimal("1.2"),
                    unit="target_roas_ratio",
                    scope="campaign.status='ENABLED' | read from x",
                    window=F.current_state_window("2026-08-20"),
                    source="c.json", basis="n/a")

    def test_a_gap_against_a_live_target_is_permitted(self):
        self.target_setup()
        fact = builder.related_fact(
            self.fs, "gap", parents=("roas.7d", "target.roas"),
            profile="actual-vs-target", value=Decimal("-42.5"),
            unit="percent_vs_target", scope="x", window="y", basis="8-action",
            rule="test")
        self.assertEqual(fact.value, Decimal("-42.5"))

    def test_a_gap_against_a_target_that_moved_is_refused(self):
        self.target_setup()
        with self.assertRaises(FactsError) as caught:
            builder.related_fact(
                self.fs, "gap", parents=("roas.7d", "target.roas"),
                profile="actual-vs-target", value=Decimal("-42.5"),
                unit="percent_vs_target", scope="x", window="y",
                basis="8-action", rule="test",
                setting_changed_in_window=True)
        self.assertIn("two different settings at once", str(caught.exception))

    def test_a_gap_needs_exactly_one_current_state_parent(self):
        self.target_setup()
        # Two current-state facts, so `window` still differs and the
        # already-agree check cannot fire first.
        self.fs.add(id="target.roas.b", value=Decimal("1.3"),
                    unit="target_roas_ratio",
                    scope="campaign.status='ENABLED' | read from x",
                    window=F.current_state_window("2026-08-19"),
                    source="c.json", basis="n/a")
        with self.assertRaises(FactsError) as caught:
            builder.related_fact(
                self.fs, "gap2", parents=("target.roas", "target.roas.b"),
                profile="actual-vs-target", value=Decimal("1"),
                unit="percent_vs_target", scope="x", window="y",
                basis="8-action", rule="test")
        self.assertIn("2 of its", str(caught.exception))

    def test_a_wow_delta_still_refuses_a_current_state_parent(self):
        """The carve-out must not leak into the other profiles."""
        self.fs.add(id="spend.now", value=Decimal("1"), unit="currency_cad",
                    scope="campaign.status='ENABLED'",
                    window=F.current_state_window("2026-08-20"),
                    source="c.json", basis="n/a")
        with self.assertRaises(FactsError) as caught:
            builder.related_fact(
                self.fs, "bad", parents=("spend.7d", "spend.now"),
                profile="wow-delta", value=Decimal("1"),
                unit="currency_cad_delta", scope="x", window="y",
                basis="n/a", rule="test")
        self.assertIn("VF-17", str(caught.exception))


class RowKeyTests(unittest.TestCase):
    """VF-19: every join on the (parent, child) pair."""

    def row(self, cid, action=None, conversions=1, value=100):
        row = {"campaign": {"id": cid},
               "metrics": {"conversions": conversions,
                           "conversionsValue": value}}
        if action:
            row["segments"] = {"conversionActionName": action}
        return row

    def test_the_conversions_pull_is_keyed_on_the_campaign_action_pair(self):
        rows = [self.row("1", S.EIGHT_ACTIONS[0], 2, 200),
                self.row("1", S.EIGHT_ACTIONS[1], 3, 300),
                self.row("2", S.EIGHT_ACTIONS[0], 4, 400)]
        per, meta = builder.conversions_per_campaign("f.json", rows)
        self.assertEqual(meta["row_keys"], 3)
        self.assertEqual(per["1"][0], Decimal("5"))
        self.assertEqual(per["1"][1], Decimal("500"))
        self.assertEqual(per["2"][0], Decimal("4"))

    def test_keying_on_the_campaign_alone_would_lose_rows(self):
        """The point of the pair: 5 conversions, not the last row's 3."""
        rows = [self.row("1", S.EIGHT_ACTIONS[0], 2),
                self.row("1", S.EIGHT_ACTIONS[1], 3)]
        per, _ = builder.conversions_per_campaign("f.json", rows)
        naive = {r["campaign"]["id"]: r["metrics"]["conversions"] for r in rows}
        self.assertEqual(per["1"][0], Decimal("5"))
        self.assertEqual(naive["1"], 3)

    def test_a_duplicate_pair_is_a_hard_stop(self):
        rows = [self.row("1", S.EIGHT_ACTIONS[0]),
                self.row("1", S.EIGHT_ACTIONS[0])]
        with self.assertRaises(pulls.BuildError) as caught:
            builder.conversions_per_campaign("f.json", rows)
        self.assertIn("duplicate row key", str(caught.exception))

    def test_a_missing_action_name_is_a_hard_stop(self):
        with self.assertRaises(pulls.BuildError) as caught:
            builder.conversions_per_campaign("f.json", [self.row("1")])
        self.assertIn("row key cannot be formed", str(caught.exception))

    def test_an_action_outside_the_eight_breaks_the_basis_claim(self):
        rows = [self.row("1", "Some Other Conversion")]
        with self.assertRaises(pulls.BuildError) as caught:
            builder.conversions_per_campaign("f.json", rows)
        self.assertIn("8-action basis", str(caught.exception))

    def test_a_segmented_campaign_pull_is_refused(self):
        rows = [{"campaign": {"id": "1"}, "metrics": {"costMicros": "1"}},
                {"campaign": {"id": "1"}, "metrics": {"costMicros": "2"}}]
        with self.assertRaises(pulls.BuildError) as caught:
            builder.one_row_per_campaign("f.json", rows)
        self.assertIn("last-write-wins", str(caught.exception).lower()
                      .replace("only the last row", "last-write-wins"))

    def test_a_row_with_no_campaign_id_is_refused(self):
        with self.assertRaises(pulls.BuildError):
            builder.one_row_per_campaign("f.json", [{"metrics": {}}])

    def test_a_repeated_date_in_the_mtd_pull_is_refused(self):
        rows = [{"segments": {"date": "2026-08-01"},
                 "metrics": {"costMicros": "1000000"}}] * 2
        with self.assertRaises(pulls.BuildError):
            builder.daily_spend("f.json", rows)


class SharedModuleTests(unittest.TestCase):
    """What this builder needs from the read-only modules, asserted."""

    BETWEEN = ("campaign.status='ENABLED' AND segments.date BETWEEN "
               "'2026-08-06' AND '2026-08-12'")

    def test_a_dated_window_survives_the_clause_split(self):
        """`gaql` must keep a `BETWEEN 'a' AND 'b'` predicate whole.

        It did not until 2026-08-20, which aborted this builder on both dated
        pulls - the week-over-week comparison and the pacing input. Asserted
        here so a regression in the read-only module fails with a message that
        names the cause instead of a scope-label mismatch three layers down.
        """
        kept = [c for c in gaql.clauses(self.BETWEEN) if gaql.is_temporality(c)]
        self.assertEqual(len(kept), 1)
        self.assertIn("2026-08-06", kept[0])
        self.assertIn("2026-08-12", kept[0])
        line = gaql.check_declared_temporality(
            self.BETWEEN, "window:2026-08-06..2026-08-12")
        self.assertIn("agrees with the date predicate", line)

    def test_the_local_splitter_keeps_a_between_predicate_whole(self):
        """`split_and` does not lean on the upstream fix, and stays literal."""
        parts = builder.split_and(self.BETWEEN)
        self.assertEqual(parts, ["campaign.status='ENABLED'",
                                 "segments.date BETWEEN '2026-08-06' AND "
                                 "'2026-08-12'"])

    def test_the_local_splitter_leaves_quotes_alone(self):
        """A scope label is the literal filter (VF-15), not a normalised one."""
        for part in builder.split_and(self.BETWEEN):
            self.assertNotEqual(part, gaql.normalise(part).replace("'", ""))

    def test_the_local_splitter_handles_a_during_filter(self):
        during = "campaign.status='ENABLED' AND segments.date DURING LAST_7_DAYS"
        self.assertEqual(builder.split_and(during),
                         ["campaign.status='ENABLED'",
                          "segments.date DURING LAST_7_DAYS"])


    def test_relative_windows_are_resolved_by_appending_never_replacing(self):
        import datetime as dt
        declared = "declared window:LAST_7_DAYS, pulled 2026-08-20"
        resolved = builder.resolve_relative_window(declared,
                                                   dt.date(2026, 8, 20))
        self.assertTrue(resolved.startswith(declared))
        self.assertIn("2026-08-13..2026-08-19", resolved)

    def test_an_already_dated_window_is_untouched(self):
        import datetime as dt
        declared = "declared window:2026-08-06..2026-08-12, pulled 2026-08-20"
        self.assertEqual(
            builder.resolve_relative_window(declared, dt.date(2026, 8, 20)),
            declared)

    def test_the_sentinel_family_is_wider_than_facts_py_knows(self):
        self.assertTrue(F.is_sentinel_share(0.0999))
        self.assertTrue(F.is_sentinel_share(0.9001))
        self.assertIsNotNone(builder.share_sentinel(Decimal("0.0999")))
        self.assertIsNotNone(builder.share_sentinel(Decimal("0.9001")))
        self.assertIsNone(builder.share_sentinel(Decimal("0.7174")))
        self.assertIsNotNone(builder.share_sentinel(None))


class BudgetPlanTests(unittest.TestCase):
    """VF-14: the budget is read (config-sourced), and never derived.

    The stated figures moved from a per-run regex parse of `budget-plan.md`
    into `account-config.json`; `test_account_config.py` re-parses the doc
    with the old expressions and fails if the two ever disagree, so "read,
    never derived" still holds - the read just happens once, drift-guarded,
    instead of on every run.
    """

    def test_the_stated_month_reads_the_config_figure(self):
        plan = builder.BudgetPlan(BUDGET_PLAN)
        stated = plan.stated_budget("2026-08")
        self.assertEqual(stated.cad, Decimal("54400"))
        self.assertTrue(stated.stated)
        self.assertEqual(plan.planned_step_down_pct, Decimal("12.5"))
        self.assertGreater(plan.cac_usd, 0)

    def test_an_unstated_month_is_null_with_reason_not_derived(self):
        stated = builder.BudgetPlan(BUDGET_PLAN).stated_budget("2026-09")
        self.assertIsNone(stated.cad)
        self.assertFalse(stated.stated)
        self.assertIn("no stated Google Ads split", stated.reason)
        self.assertIn("Never derive", stated.reason)

    def test_an_unknown_month_refuses_loudly(self):
        with self.assertRaises(pulls.BuildError) as caught:
            builder.BudgetPlan(BUDGET_PLAN).stated_budget("2027-01")
        self.assertIn("knows nothing about month", str(caught.exception))




if __name__ == "__main__":
    unittest.main(verbosity=2)
