#!/usr/bin/env python3
"""Tests for `citations.check_consistency` - the report-vs-metadata checks.

Both checks here were added after the 2026-08-28 live run, where Tier 2 caught
each of them by hand as a HIGH finding on a draft `citations.check()` had
already passed clean. These tests pin the two failures as they actually
occurred, so neither can come back silently:

- the facts file declared `conversion-integrity-check.json` in
  `meta.undeclared_pulls` while the report's footer reported none;
- the report stated its recommendations had been appended to the log when no
  append had happened.

Everything runs against synthetic fixtures in a tempdir. Run:
python3 tools/test_report_consistency.py
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import citations as C

PULLED = "2026-08-28"
UNDECLARED = "conversion-integrity-check.json"


class ConsistencyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    # -- fixtures ---------------------------------------------------------
    def write_report(self, body: str) -> str:
        path = self.dir / "report.md"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def write_facts(self, **meta) -> str:
        path = self.dir / "facts.json"
        base = {"skill": "weekly-review", "pulled": PULLED}
        base.update(meta)
        path.write_text(json.dumps({"meta": base, "facts": []}), encoding="utf-8")
        return str(path)

    def write_log(self, dates: "list[str]") -> str:
        path = self.dir / C.LOG_NAME
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date_recommended", "skill", "campaign", "metric",
                        "before_value", "recommendation", "reasoning",
                        "status", "after_value", "after_date"])
            for d in dates:
                w.writerow([d, "weekly-review", "acct", "pacing_pct",
                            "136.64", "hold", "over budget", "open", "", ""])
        return str(path)

    def kinds(self, problems) -> "list[str]":
        return [p.kind for p in problems]

    # -- undeclared pulls must be named -----------------------------------
    def test_undeclared_pull_never_named_is_flagged(self):
        report = self.write_report(
            "## Verification\n- undeclared in _manifest.json: none\n")
        facts = self.write_facts(undeclared_pulls=[UNDECLARED])
        found = C.check_consistency(report, facts)
        self.assertEqual(self.kinds(found), ["undeclared_pull_unnamed"])
        self.assertIn(UNDECLARED, found[0].detail)

    def test_undeclared_pull_named_anywhere_passes(self):
        report = self.write_report(
            f"Persisted to {UNDECLARED} - not a GAQL pull, so not in the "
            "manifest.\n")
        facts = self.write_facts(undeclared_pulls=[UNDECLARED])
        self.assertEqual(C.check_consistency(report, facts), [])

    def test_no_undeclared_pulls_is_silent(self):
        report = self.write_report("- undeclared in _manifest.json: none\n")
        self.assertEqual(
            C.check_consistency(report, self.write_facts(undeclared_pulls=[])),
            [])
        self.assertEqual(
            C.check_consistency(report, self.write_facts()), [])

    def test_every_unnamed_pull_is_reported_separately(self):
        report = self.write_report(f"names only {UNDECLARED} here\n")
        facts = self.write_facts(undeclared_pulls=[UNDECLARED, "other.json"])
        found = C.check_consistency(report, facts)
        self.assertEqual(self.kinds(found), ["undeclared_pull_unnamed"])
        self.assertIn("other.json", found[0].detail)

    # -- a claimed append must have happened -------------------------------
    def test_claimed_append_with_no_matching_row_is_flagged(self):
        report = self.write_report(
            f"All three are appended to outputs/{C.LOG_NAME} this run.\n")
        facts = self.write_facts()
        log = self.write_log(["2026-07-14"])  # an older run only
        found = C.check_consistency(report, facts, log)
        self.assertEqual(self.kinds(found), ["append_not_made"])
        self.assertIn(PULLED, found[0].detail)

    def test_claimed_append_that_happened_passes(self):
        report = self.write_report(
            f"All three are appended to outputs/{C.LOG_NAME} this run.\n")
        log = self.write_log(["2026-07-14", PULLED])
        self.assertEqual(
            C.check_consistency(report, self.write_facts(), log), [])

    def test_missing_log_under_a_claim_is_flagged(self):
        report = self.write_report(f"Logged to {C.LOG_NAME}.\n")
        missing = str(self.dir / "absent" / C.LOG_NAME)
        found = C.check_consistency(report, self.write_facts(), missing)
        self.assertEqual(self.kinds(found), ["append_not_made"])

    def test_append_check_is_opt_in(self):
        """No outputs dir, no finding - a dev box has nothing to verify."""
        report = self.write_report(
            f"All three are appended to outputs/{C.LOG_NAME} this run.\n")
        self.assertEqual(C.check_consistency(report, self.write_facts()), [])

    def test_log_mentioned_without_an_append_claim_is_not_a_claim(self):
        """Naming the file is not asserting a write to it."""
        report = self.write_report(
            f"The schema for {C.LOG_NAME} is described in budget-optimize "
            "step 8.\n")
        log = self.write_log(["2026-07-14"])
        self.assertEqual(
            C.check_consistency(report, self.write_facts(), log), [])

    def test_both_failures_are_reported_together(self):
        report = self.write_report(
            "- undeclared in _manifest.json: none\n"
            f"All three are appended to outputs/{C.LOG_NAME} this run.\n")
        facts = self.write_facts(undeclared_pulls=[UNDECLARED])
        log = self.write_log(["2026-07-14"])
        self.assertEqual(
            sorted(self.kinds(C.check_consistency(report, facts, log))),
            ["append_not_made", "undeclared_pull_unnamed"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
