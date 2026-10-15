#!/usr/bin/env python3
"""Drift guards and integrity checks for `account-config.json`.

Run:  python3 tools/test_account_config.py

Why this file exists
--------------------
The account and calendar constants the builders used to bake in (campaign-ID
buckets, the 8 actions, stated monthly budgets, thresholds) now live in one
config file. That removes the September time bomb - a builder hard-coded to
August 2026 - but it creates a new place for drift: the config asserting a
figure `budget-plan.md` no longer states. So this suite re-parses the doc
with the same expressions the weekly-review builder used when it read the doc
directly, and fails loudly if the two ever disagree (VF-14: the budget is
READ, never derived - the read now happens here, once, instead of on every
run). Update the config and the doc together.

Standard library only; needs no account data, so it runs on a fresh clone.
"""

from __future__ import annotations

import re
import sys
import unittest
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import account_config as AC  # noqa: E402
import scopes as S  # noqa: E402

BUDGET_PLAN = HERE.parent / "budget-plan.md"

#: The exact expressions `build_weekly_review_facts.BudgetPlan` used while it
#: parsed the doc per run. Kept verbatim so this guard reads the same rows.
_BUDGET_ROW = re.compile(
    r"\|\s*August Google Ads budget\s*\|\s*\*\*CAD\s*([\d,]+)\*\*"
)
_STEP_DOWN = re.compile(r"Planned step down,\s*[-−]([\d.]+)%\s*vs Jul")
_CAC_ROW = re.compile(
    r"\|\s*(\w+ \d{4})\s*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|\s*\$([\d,]+\.\d{2})\s*\|"
)


class DocDriftGuards(unittest.TestCase):
    """`account-config.json` and `budget-plan.md` must state the same figures."""

    @classmethod
    def setUpClass(cls):
        cls.text = BUDGET_PLAN.read_text(encoding="utf-8")

    def test_the_august_budget_matches_the_doc(self):
        found = _BUDGET_ROW.search(self.text)
        self.assertIsNotNone(found, "the 'August Google Ads budget' row is gone")
        doc_cad = Decimal(found.group(1).replace(",", ""))
        self.assertEqual(AC.stated_budget("2026-08").cad, doc_cad)

    def test_the_august_usd_and_fx_are_consistent(self):
        stated = AC.stated_budget("2026-08")
        self.assertEqual(stated.usd * AC.fx_usd_to_cad(), stated.cad)

    def test_the_planned_step_down_matches_the_doc(self):
        step = _STEP_DOWN.search(self.text)
        self.assertIsNotNone(step, "the planned step down line is gone")
        self.assertEqual(AC.planned_step_down_pct(), Decimal(step.group(1)))

    def test_the_cac_anchor_matches_the_docs_latest_row(self):
        rows = _CAC_ROW.findall(self.text)
        self.assertTrue(rows, "the CAC table is gone")
        month_short, cac = rows[-1]
        anchor = AC.cac_anchor()
        self.assertEqual(anchor["month_short"], month_short)
        self.assertEqual(anchor["cac_usd"], Decimal(cac.replace(",", "")))

    def test_the_cost_per_free_trial_anchor_appears_in_the_doc(self):
        value = AC.cac_anchor()["cost_per_free_trial_usd"]
        self.assertIn(f"${value}", self.text)

    def test_the_elasticity_bands_appear_in_the_doc(self):
        for name, usd, degradation, _note in AC.elasticity_bands():
            if usd is None:
                continue
            self.assertIn(f"+${usd:,.0f}/month", self.text, name)
        band2 = dict(
            (name, degradation)
            for name, _usd, degradation, _note in AC.elasticity_bands()
        )["band_2"]
        self.assertIn(f"{band2}% CPA degradation", self.text)


class ConfigIntegrityTests(unittest.TestCase):
    """Internal consistency: one source, self-consistent."""

    def test_the_eight_actions_are_eight_and_scopes_serves_them(self):
        eight = AC.eight_actions()
        self.assertEqual(len(eight), 8)
        self.assertEqual(len(set(eight)), 8)
        self.assertEqual(S.EIGHT_ACTIONS, eight)

    def test_the_team_actions_are_a_subset_of_the_eight(self):
        self.assertTrue(set(AC.team_actions()) <= set(AC.eight_actions()))

    def test_brand_core_is_one_of_the_brand_ids(self):
        self.assertIn(AC.brand_core_campaign_id(), AC.brand_campaign_ids())

    def test_the_buckets_are_populated_and_disjoint(self):
        brand, tof = AC.brand_campaign_ids(), AC.tof_campaign_ids()
        legacy = AC.legacy_campaign_ids()
        self.assertTrue(brand and tof and legacy)
        self.assertFalse(brand & tof)
        self.assertFalse(brand & legacy)
        self.assertFalse(tof & legacy)

    def test_every_2026_month_from_july_is_known(self):
        for month in ("2026-07", "2026-08", "2026-09", "2026-10", "2026-11",
                      "2026-12"):
            AC.stated_budget(month)  # must not raise

    def test_september_is_explicitly_unstated_with_the_reason(self):
        stated = AC.stated_budget("2026-09")
        self.assertIsNone(stated.cad)
        self.assertIn("no stated Google Ads split", stated.reason)
        self.assertIn("Never derive", stated.reason)

    def test_an_unknown_month_raises_the_typed_error(self):
        with self.assertRaises(AC.UnknownMonthError):
            AC.stated_budget("2027-01")

    def test_thresholds_carry_the_documented_values(self):
        self.assertEqual(AC.threshold("lost_is_budget_constrained_gate"),
                         Decimal("0.30"))
        self.assertEqual(AC.threshold("lost_is_budget_exposure_gate"),
                         Decimal("0.20"))
        self.assertEqual(AC.threshold("conversion_floor"), 5)
        self.assertEqual(AC.threshold("learning_days_search"), 14)
        self.assertEqual(AC.threshold("learning_days_pmax"), 28)
        low, high = AC.threshold("conversion_integrity_band_pct")
        self.assertEqual((low, high), (Decimal("10"), Decimal("50")))
        # The audit-ad-copy serve-table selection rule reads these two; a
        # missing or malformed key silently reverts the builder to its local
        # fallback, so their presence and shape are contract.
        self.assertEqual(AC.threshold("serve_table_top_n"), 10)
        self.assertEqual(AC.threshold("serve_table_min_cost_cad"),
                         Decimal("2000"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
