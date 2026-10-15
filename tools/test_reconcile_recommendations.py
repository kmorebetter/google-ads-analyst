#!/usr/bin/env python3
"""Tests for `reconcile_recommendations.py` - the loop-closing tool.

Everything runs against synthetic fixtures built in a tempdir: a
recommendations log with the canonical 10-column schema (budget-optimize
step 8) and raw pulls in the same JSON shape the CLI saves. Dates are pinned
via the tool's hidden `--today` flag so ages are deterministic (VF-20: tests
assert the contract, and the contract includes "never guess, never
overwrite").

Run: python3 tools/test_reconcile_recommendations.py
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reconcile_recommendations as R

TODAY = "2026-08-27"
OBS_DIR = "2026-08-08"  # raw dir name doubles as the observation date

HEADER = R.BASE_COLUMNS
FULL_HEADER = R.BASE_COLUMNS + R.NEW_COLUMNS

COMMA_NAME = "Google - Acme Software - Search - Conquesting - RivalCo - US, CA, UK"


def base_row(**overrides) -> dict:
    row = {
        "date_recommended": "2026-08-05",
        "skill": "budget-optimize",
        "campaign": "Synthetic - Search - Generic - Features",
        "metric": "daily_budget",
        "before_value": "CAD 250.00/day",
        "recommendation": "increase to $400/day",
        "reasoning": "band 1",
        "status": "open",
        "after_value": "",
        "after_date": "",
    }
    row.update(overrides)
    return row


def campaigns_current(rows) -> dict:
    return {"action": "post", "data": {"fieldMask": "campaign.id", "results": rows}}


def campaign_row(cid, name, budget_id, budget_micros, target_roas=None):
    campaign = {"id": cid, "name": name, "status": "ENABLED"}
    if target_roas is not None:
        campaign["maximizeConversionValue"] = {"targetRoas": target_roas}
    return {
        "campaign": campaign,
        "campaignBudget": {"id": budget_id, "amountMicros": str(budget_micros),
                           "explicitlyShared": False},
    }


def change_event(cid, when, resource="CAMPAIGN_BUDGET", fields="amountMicros"):
    return {"changeEvent": {
        "campaign": f"customers/9/campaigns/{cid}",
        "changeDateTime": f"{when} 12:00:00.000000",
        "changeResourceType": resource,
        "resourceChangeOperation": "UPDATE",
        "changedFields": fields,
    }}


def metric_rows(cid, cost_micros=None, conversions=None, value=None):
    metrics = {}
    if cost_micros is not None:
        metrics["costMicros"] = str(cost_micros)
    if conversions is not None:
        metrics["conversions"] = conversions
        metrics["conversionsValue"] = value or 0
    return [{"campaign": {"id": cid}, "metrics": metrics}]


class Harness(unittest.TestCase):
    """Tempdir + helpers; every test builds its own outputs/raw pair."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.outputs = Path(self._tmp.name) / "outputs"
        self.raw = self.outputs / "raw" / OBS_DIR
        self.raw.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def write_log(self, rows, header=None):
        header = header or HEADER
        with (self.outputs / R.LOG_NAME).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(header)
            for row in rows:
                writer.writerow([row.get(col, "") for col in header])

    def read_log(self):
        with (self.outputs / R.LOG_NAME).open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            return header, [dict(zip(header, row)) for row in reader if row]

    def write_json(self, filename, payload):
        (self.raw / filename).write_text(json.dumps(payload), encoding="utf-8")

    def write_structure(self, extra_rows=()):
        rows = [
            campaign_row("70000000001", "Synthetic - Search - Brand - Core",
                         "80000001", 100_000_000, target_roas=20),
            campaign_row("70000000002", "Synthetic - Search - Generic - Features",
                         "80000002", 400_000_000, target_roas=1.2),
        ]
        rows.extend(extra_rows)
        self.write_json(R.CAMPAIGNS_CURRENT, campaigns_current(rows))

    def run_tool(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main([str(self.outputs), "--raw-dir", str(self.raw),
                           "--today", TODAY, *extra])
        return code, out.getvalue()


class DetectionTests(Harness):

    def test_applied_budget_change_with_change_event(self):
        self.write_log([base_row()])
        self.write_structure()
        self.write_json(R.CHANGE_EVENTS, campaigns_current(
            [change_event("70000000002", "2026-08-06")]))
        code, out = self.run_tool("--apply")
        self.assertEqual(code, 0)
        _, rows = self.read_log()
        self.assertEqual(rows[0]["status"], "applied")
        self.assertEqual(rows[0]["applied_date"], "2026-08-06")
        self.assertIn("CAD 250.00/day -> CAD 400.00/day", rows[0]["applied_evidence"])
        self.assertIn("change_event 2026-08-06 CAMPAIGN_BUDGET", rows[0]["applied_evidence"])
        self.assertIn("propose open -> applied .. 1", out)

    def test_applied_without_change_event_gets_on_or_before(self):
        self.write_log([base_row()])
        self.write_structure()  # no change-events file at all
        code, _ = self.run_tool("--apply")
        self.assertEqual(code, 0)
        _, rows = self.read_log()
        self.assertEqual(rows[0]["status"], "applied")
        self.assertEqual(rows[0]["applied_date"], f"on-or-before {OBS_DIR}")
        self.assertIn("no matching change_event", rows[0]["applied_evidence"])

    def test_equal_value_stays_open_and_long_open_is_surfaced(self):
        self.write_log([base_row(before_value="CAD 400.00/day")])
        self.write_structure()
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("still open ............... 1", out)
        self.assertIn("(1 long-open", out)          # 2026-08-05 -> 22 days
        self.assertIn("open 22 days", out)

    def test_unparseable_before_value_needs_review(self):
        self.write_log([base_row(before_value="see reasoning")])
        self.write_structure()
        code, out = self.run_tool("--apply")
        self.assertEqual(code, 0)
        _, rows = self.read_log()
        self.assertEqual(rows[0]["status"], "open")  # never auto-flipped
        self.assertIn("no parseable number", out)

    def test_overshoot_beyond_twice_the_recommended_delta_needs_review(self):
        # Recommendation said increase to $300/day (delta +50); the budget now
        # reads $400/day (delta +150, > 2x). Direction matches, but a move far
        # past the recommended figure more likely means a different
        # intervention - a human call, never an auto-flip.
        self.write_log([base_row(recommendation="increase to $300/day")])
        self.write_structure()
        code, out = self.run_tool("--apply")
        self.assertEqual(code, 0)
        _, rows = self.read_log()
        self.assertEqual(rows[0]["status"], "open")
        self.assertIn("overshooting the recommended", out)

    def test_opposite_direction_needs_review(self):
        # Recommendation said increase to 600; the budget was cut instead.
        self.write_log([base_row(before_value="CAD 500.00/day",
                                 recommendation="increase to $600/day")])
        self.write_structure()  # current 400 < before 500
        code, out = self.run_tool("--apply")
        self.assertEqual(code, 0)
        _, rows = self.read_log()
        self.assertEqual(rows[0]["status"], "open")
        self.assertIn("opposite to the recommendation", out)

    def test_unknown_campaign_needs_review(self):
        self.write_log([base_row(campaign="No Such Campaign")])
        self.write_structure()
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("campaign not found", out)

    def test_no_raw_dir_needs_review_not_a_crash(self):
        self.write_log([base_row()])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main([str(self.outputs), "--today", TODAY])
        self.assertEqual(code, 0)
        self.assertIn("no current-state pull provided", out.getvalue())

    def test_quoted_comma_campaign_name_round_trips(self):
        self.write_log([base_row(campaign=COMMA_NAME, before_value="CAD 100.00/day",
                                 recommendation="increase to $137.50/day")])
        self.write_structure(extra_rows=[
            campaign_row("70000000009", COMMA_NAME, "80000009", 137_500_000)])
        code, _ = self.run_tool("--apply")
        self.assertEqual(code, 0)
        header, rows = self.read_log()
        self.assertEqual(rows[0]["campaign"], COMMA_NAME)
        self.assertEqual(rows[0]["status"], "applied")
        # The file itself still lints: every row matches the header width.
        raw_lines = (self.outputs / R.LOG_NAME).read_text().splitlines()
        self.assertEqual(len(next(csv.reader([raw_lines[1]]))), len(header))

    def test_target_roas_read_from_the_submessage(self):
        self.write_log([base_row(campaign="Synthetic - Search - Brand - Core",
                                 metric="target_roas", before_value="8.3",
                                 recommendation="tighten tROAS from 8.3 to 20")])
        self.write_structure()  # Brand Core carries targetRoas 20
        code, _ = self.run_tool("--apply")
        self.assertEqual(code, 0)
        _, rows = self.read_log()
        self.assertEqual(rows[0]["status"], "applied")
        self.assertIn("target_roas 8.3 -> 20", rows[0]["applied_evidence"])


class SchemaAndSafetyTests(Harness):

    def test_migration_adds_columns_exactly_once(self):
        self.write_log([base_row()])
        self.write_structure()
        self.run_tool("--apply")
        header, _ = self.read_log()
        self.assertEqual(header, FULL_HEADER)
        self.run_tool("--apply")
        header, _ = self.read_log()
        self.assertEqual(header, FULL_HEADER)  # no duplicates on the second run

    def test_report_only_writes_nothing(self):
        self.write_log([base_row()])
        self.write_structure()
        before = (self.outputs / R.LOG_NAME).read_bytes()
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("report-only", out)
        self.assertEqual((self.outputs / R.LOG_NAME).read_bytes(), before)

    def test_apply_is_idempotent(self):
        self.write_log([base_row()])
        self.write_structure()
        self.write_json(R.CHANGE_EVENTS, campaigns_current(
            [change_event("70000000002", "2026-08-06")]))
        self.run_tool("--apply")
        first = (self.outputs / R.LOG_NAME).read_bytes()
        _, second_out = self.run_tool("--apply")
        self.assertEqual((self.outputs / R.LOG_NAME).read_bytes(), first)
        self.assertIn("propose open -> applied .. 0", second_out)

    def test_filled_cells_are_never_overwritten(self):
        row = base_row(status="applied", after_value="CAD 111.11/day",
                       after_date="2026-08-10")
        self.write_log([row], header=FULL_HEADER)
        self.write_structure()
        self.run_tool("--apply")
        _, rows = self.read_log()
        self.assertEqual(rows[0]["after_value"], "CAD 111.11/day")
        self.assertEqual(rows[0]["after_date"], "2026-08-10")

    def test_malformed_row_exits_1_and_names_the_row(self):
        (self.outputs / R.LOG_NAME).write_text(
            ",".join(HEADER) + "\n"
            + "2026-08-05,budget-optimize,Name, with, unquoted, commas,"
              "daily_budget,250,increase,why,open,,\n",
            encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = R.main([str(self.outputs), "--today", TODAY])
        self.assertEqual(code, 1)
        self.assertIn("line 2", err.getvalue())

    def test_missing_log_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = R.main([str(self.outputs), "--today", TODAY])
        self.assertEqual(code, 2)
        self.assertIn("not found", err.getvalue())


class BackfillTests(Harness):

    def test_applied_row_older_than_7_days_gets_after_value(self):
        row = base_row(status="applied", applied_date="2026-08-15")
        self.write_log([row], header=FULL_HEADER)
        self.write_structure()
        code, out = self.run_tool("--apply")
        self.assertEqual(code, 0)
        _, rows = self.read_log()
        self.assertEqual(rows[0]["after_value"], "CAD 400.00/day")
        self.assertEqual(rows[0]["after_date"], OBS_DIR)
        self.assertIn("after_value backfilled ... 1", out)

    def test_recently_applied_row_is_left_alone(self):
        row = base_row(status="applied", applied_date="2026-08-25")  # 2 days
        self.write_log([row], header=FULL_HEADER)
        self.write_structure()
        self.run_tool("--apply")
        _, rows = self.read_log()
        self.assertEqual(rows[0]["after_value"], "")


class OutcomeTests(Harness):

    def _applied_row(self):
        return base_row(status="applied", applied_date="2026-08-01",
                        after_value="CAD 400.00/day", after_date="2026-08-08")

    def _write_outcome_pulls(self, post_conversions=10, post_cost=400_000_000):
        cid = "70000000002"
        self.write_json(R.OUTCOME_FILES["cost_pre"],
                        campaigns_current(metric_rows(cid, cost_micros=500_000_000)))
        self.write_json(R.OUTCOME_FILES["cost_post"],
                        campaigns_current(metric_rows(cid, cost_micros=post_cost)))
        self.write_json(R.OUTCOME_FILES["conv_pre"],
                        campaigns_current(metric_rows(cid, conversions=10, value=3500)))
        self.write_json(R.OUTCOME_FILES["conv_post"],
                        campaigns_current(metric_rows(cid, conversions=post_conversions,
                                                      value=3500)))
        (self.raw / "_manifest.json").write_text(json.dumps({
            R.OUTCOME_FILES["cost_pre"]: {"temporality": "window:2026-07-18..2026-07-31"},
            R.OUTCOME_FILES["cost_post"]: {"temporality": "window:2026-08-01..2026-08-14"},
        }), encoding="utf-8")

    def test_missing_pulls_report_insufficient_and_csv_stays_blank(self):
        self.write_log([self._applied_row()], header=FULL_HEADER)
        self.write_structure()
        code, out = self.run_tool("--apply")
        self.assertEqual(code, 0)
        self.assertIn("insufficient_data: needs outcome-cost-pre14d.json", out)
        _, rows = self.read_log()
        self.assertEqual(rows[0]["outcome_verdict"], "")  # report-only verdict

    def test_cpa_improvement_is_a_verdict_with_windows(self):
        self.write_log([self._applied_row()], header=FULL_HEADER)
        self.write_structure()
        self._write_outcome_pulls()  # CPA 50 -> 40, -20%, beyond the 15% band
        code, out = self.run_tool("--apply")
        self.assertEqual(code, 0)
        _, rows = self.read_log()
        self.assertEqual(rows[0]["outcome_verdict"], "improved")
        self.assertIn("-20.0%", rows[0]["outcome_delta"])
        self.assertIn("assumed-value CPA", rows[0]["outcome_delta"])
        self.assertEqual(rows[0]["outcome_window"],
                         "pre 2026-07-18..2026-07-31 vs post 2026-08-01..2026-08-14")
        self.assertEqual(rows[0]["outcome_date"], OBS_DIR)
        self.assertIn("outcome verdicts ......... 1 proposed", out)

    def test_conversion_floor_blocks_a_verdict(self):
        self.write_log([self._applied_row()], header=FULL_HEADER)
        self.write_structure()
        self._write_outcome_pulls(post_conversions=3)  # below the floor of 5
        self.run_tool("--apply")
        _, rows = self.read_log()
        self.assertEqual(rows[0]["outcome_verdict"], "")

    def test_later_change_event_confounds_the_verdict(self):
        self.write_log([self._applied_row()], header=FULL_HEADER)
        self.write_structure()
        self._write_outcome_pulls()
        self.write_json(R.CHANGE_EVENTS, campaigns_current(
            [change_event("70000000002", "2026-08-10")]))  # after applied_date
        self.run_tool("--apply")
        _, rows = self.read_log()
        self.assertEqual(rows[0]["outcome_verdict"], "confounded: improved")


if __name__ == "__main__":
    unittest.main(verbosity=1)


class MetricVocabularyTests(Harness):
    """The closed `metric` vocabulary (added 2026-08-28).

    Before this, every metric outside `daily_budget`/`target_roas`/
    `target_cpa` produced one message - "not one this tool can read" - and
    landed in "needs review" beside real parse failures. On the live 08-28 log
    that was 6 of 9 rows, which turns the section into noise. Rows now split
    three ways: fixable at the write site (compound), never machine-checkable
    (manual), and genuinely unknown.
    """

    def test_manual_metric_is_not_needs_review(self):
        self.write_log([base_row(metric="negation_thresholds",
                                 before_value="none supplied")])
        self.write_structure()
        _, out = self.run_tool()
        self.assertIn("Judged by hand", out)
        self.assertIn("thresholds are supplied", out)
        self.assertNotIn("[1]", out.split("Judged by hand")[0])

    def test_manual_metric_reports_its_age(self):
        self.write_log([base_row(metric="ad_copy",
                                 date_recommended="2026-08-05")])
        self.write_structure()
        _, out = self.run_tool()
        self.assertIn("[open 22d]", out)  # 08-05 -> TODAY 08-27

    def test_manual_metric_needs_no_raw_dir(self):
        """It is dispositioned on the row alone - no current-state pull."""
        self.write_log([base_row(metric="keywords")])
        report = R.Report()
        R.detection_pass(self.read_log()[1], None, None, None, report,
                         R.date.fromisoformat(TODAY))
        self.assertEqual(len(report.manual_only), 1)
        self.assertEqual(report.needs_review, [])

    def test_compound_metric_is_malformed_not_unreadable(self):
        self.write_log([base_row(metric="daily_budget + target_roas")])
        self.write_structure()
        _, out = self.run_tool()
        self.assertIn("Malformed rows", out)
        self.assertIn("one row per metric", out)

    def test_compound_metric_survives_a_manual_component(self):
        self.write_log([base_row(metric="ad_copy + keywords")])
        self.write_structure()
        _, out = self.run_tool()
        self.assertIn("Malformed rows", out)
        self.assertNotIn("Judged by hand", out)

    def test_unknown_metric_names_the_vocabulary(self):
        self.write_log([base_row(metric="quality_score")])
        self.write_structure()
        _, out = self.run_tool()
        self.assertIn("Needs review", out)
        for name in R.READABLE_METRICS:
            self.assertIn(name, out)

    def test_readable_metrics_still_reconcile(self):
        """The split must not steal rows the tool could already read."""
        self.write_log([base_row()])
        self.write_structure()
        _, out = self.run_tool()
        self.assertNotIn("Judged by hand (", out.split("Dispositions")[0])
        self.assertIn("judged by hand ........... 0", out)
        self.assertIn("malformed rows ........... 0", out)

    def test_classify_metric_is_case_and_space_insensitive(self):
        self.assertEqual(R.classify_metric("  Daily_Budget ")[0], "readable")
        self.assertEqual(R.classify_metric("AD_COPY")[0], "manual")
        self.assertEqual(R.classify_metric("daily_budget + target_cpa")[0],
                         "compound")

    def test_every_manual_metric_carries_a_reason(self):
        for name, why in R.MANUAL_METRICS.items():
            self.assertTrue(why.strip(), f"{name} has no how-to-judge note")
            self.assertNotIn(name, R.READABLE_METRICS,
                             f"{name} is in both vocabularies")
