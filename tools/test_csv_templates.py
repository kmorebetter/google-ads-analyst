#!/usr/bin/env python3
"""The three bot-template engines, scored by the graded eval suite in evals/.

Fails if any eval threshold is missed, so the full suite keeps the bots honest."""
import sys, unittest, io, contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "tools"))
import run_evals as E  # noqa: E402
import scenarios as S  # noqa: E402
import csv_negatives as N  # noqa: E402
from decimal import Decimal  # noqa: E402


class EvalThresholds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):
            res = {"negatives": E.score_negatives(),
                   "audit": E.score_findings("audit", S.audit_scenarios(), "audit.md"),
                   "weekly": E.score_findings("weekly", S.weekly_scenarios(), "weekly.md"),
                   "bad_inputs": E.score_bad()}
        res["completed"] = 1.0 if not E.incomplete else 0.0
        cls.verdicts = E.verdicts(res)

    def test_every_threshold_passes(self):
        failed = {k: v for k, v in self.verdicts.items() if not v["pass"]}
        self.assertEqual(failed, {})


class NegativeRules(unittest.TestCase):
    T = N.Thresholds(Decimal("50"), 10, Decimal("120"), Decimal("3"))

    def row(self, term, camp="Search - Generic", clicks=20, cost="80", conv="0"):
        return N.Row(term, camp, "G", clicks, Decimal(cost), Decimal(conv))

    def test_misspelled_brand_is_kept_but_look_alikes_are_not(self):
        self.assertEqual(N.decide(self.row("acmee invoicing"), self.T, ["acme"])[0], "KEEP")
        self.assertEqual(N.decide(self.row("akme app"), self.T, ["acme"])[0], "KEEP")
        self.assertEqual(N.decide(self.row("acne cream"), self.T, ["acme"])[0], "NEGATE")

    def test_competitor_research_in_conquest_is_review_but_login_is_negative(self):
        c = "Search - Conquest - RivalCo"
        self.assertEqual(N.decide(self.row("rivalco pricing", c), self.T, (), (), ["rivalco"])[0], "REVIEW")
        self.assertEqual(N.decide(self.row("rivalco login", c), self.T, (), (), ["rivalco"])[0], "NEGATE")

    def test_one_word_term_is_review(self):
        self.assertEqual(N.decide(self.row("invoice"), self.T)[0], "REVIEW")

    def test_clearance_does_not_rescue_an_expensive_converter(self):
        rows = [self.row("quickbooks alternative", cost="1500", conv="2")]
        N.run(rows, self.T, clearance={"quickbooks alternative": (Decimal("3"), Decimal("3000"))})
        self.assertEqual(rows[0].decision, "CUT")
        rows = [self.row("billing app", cost="90", conv="0")]
        N.run(rows, self.T, clearance={"billing app": (Decimal("2"), Decimal("200"))})
        self.assertEqual(rows[0].decision, "CLEARED")


if __name__ == "__main__":
    unittest.main()
