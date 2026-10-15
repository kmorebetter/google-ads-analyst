#!/usr/bin/env python3
"""Tests for the `mine-search-terms` facts builder.

Run:  python3 tools/test_mine_search_terms_facts.py

Four of these tests exist because a specific thing went wrong, or would.

`TruncationTests`   the unfiltered cost query returns exactly 10,000 rows with a
                    `nextPageToken` and nothing in the response says "truncated".
                    Every total off it is a floor.
`ThresholdTests`    a previous run in this pack invented a threshold and shipped
                    an unstated subset of the terms it applied to.
`RowKeyTests`       keying on `criterion_id` alone mis-attributes CAD 7,127.70 of
                    one criterion's spend to the wrong ad group, and the answer
                    depends on dict insertion order, so two runs of identical
                    code over identical data can disagree while both mechanical
                    checkers stay green (VF-19).
`CategoryTests`     the whole ask. The decision must be the numbers. Every
                    category value is blanked, shuffled and rotated, and the
                    decisions must come back byte-identical.

Standard library only.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
import random
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

import build_mine_search_terms_facts as B  # noqa: E402
import facts as F  # noqa: E402

REPO = HERE.parent
AUG20 = TS.raw_or_fixture("2026-08-20")
#: True when AUG20 is the real pull rather than the synthetic fixture.
REAL_DATA = TS.using_real_data("2026-08-20")


def converted_90d(raw_dir=AUG20) -> set:
    """The 90-day clearance set `build()` applies, so a re-decide matches it."""
    path = raw_dir / B.DONOTNEGATE_90D
    if not path.exists():
        return set()
    return B.converted_in_90d_terms(json.loads(path.read_text(encoding="utf-8")))

#: One threshold set used across the tests. Not a default and not a
#: recommendation: the builder has no defaults, so a test must supply its own.
TEST_THRESHOLDS = {
    "min_spend_cad": Decimal("50"),
    "min_clicks": 3,
    "target_cpa_cad": Decimal("500"),
    "target_cpa_multiple": Decimal("3"),
}


def payload(rows: list, token: str | None = None) -> dict:
    data: dict = {"fieldMask": "campaign.id", "results": rows}
    if token:
        data["nextPageToken"] = token
    return {"success": True, "data": data}


def fake_row(i: int) -> dict:
    return {
        "campaign": {"id": "1"},
        "adGroup": {"id": "2"},
        "searchTermView": {"searchTerm": f"term {i}", "status": "NONE"},
        "metrics": {"costMicros": "1000000", "clicks": "1", "impressions": "1"},
        "segments": {
            "keyword": {
                "adGroupCriterion": "customers/1/adGroupCriteria/2~3",
                "info": {"text": "kw", "matchType": "BROAD"},
            }
        },
    }


# --------------------------------------------------------------------------


class TruncationTests(unittest.TestCase):
    """A pull returning exactly 10,000 rows is capped. Refuse to proceed."""

    def test_page_token_refuses(self):
        with self.assertRaises(B.TruncatedPullError) as ctx:
            B.assert_not_truncated("cost.json", payload([fake_row(0)], token="abc"))
        self.assertIn("nextPageToken", str(ctx.exception))
        self.assertIn("metrics.cost_micros > 0", str(ctx.exception))

    def test_exactly_the_page_cap_refuses_even_without_a_token(self):
        """The real failure mode: 10,000 rows and no token in what was saved."""
        rows = [fake_row(i) for i in range(B.PAGE_CAP_ROWS)]
        with self.assertRaises(B.TruncatedPullError) as ctx:
            B.assert_not_truncated("cost.json", payload(rows))
        self.assertIn(str(B.PAGE_CAP_ROWS), str(ctx.exception))

    def test_one_row_under_the_cap_passes(self):
        rows = [fake_row(i) for i in range(B.PAGE_CAP_ROWS - 1)]
        line = B.assert_not_truncated("cost.json", payload(rows))
        self.assertIn("no nextPageToken", line)

    def test_manifest_declaring_truncated_true_refuses(self):
        with self.assertRaises(B.TruncatedPullError):
            B.assert_not_truncated(
                "cost.json", payload([fake_row(0)]), declared_truncated=True
            )

    def test_manifest_disagreeing_with_the_response_refuses(self):
        """`truncated: false` beside a page token means the manifest is stale."""
        with self.assertRaises(B.TruncatedPullError) as ctx:
            B.assert_not_truncated(
                "cost.json",
                payload([fake_row(0)], token="abc"),
                declared_truncated=False,
            )
        self.assertIn("nextPageToken", str(ctx.exception))


    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_build_refuses_a_truncated_cost_pull_end_to_end(self):
        """Copy the real raw dir, cap the cost pull, and the build must refuse."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / AUG20.name
            dest.mkdir()
            for path in AUG20.glob("*.json"):
                (dest / path.name).write_bytes(path.read_bytes())
            raw = json.loads((dest / B.COST).read_text(encoding="utf-8"))
            raw["data"]["nextPageToken"] = "CAoQCg"
            (dest / B.COST).write_text(json.dumps(raw))
            thresholds = B.require_thresholds(dict(TEST_THRESHOLDS))
            with self.assertRaises(B.TruncatedPullError):
                B.build(dest, thresholds)


class ThresholdTests(unittest.TestCase):
    """The thresholds are the account owner's. No defaults, and a loud refusal."""

    def test_no_thresholds_refuses_and_names_all_four(self):
        with self.assertRaises(B.MissingThresholdError) as ctx:
            B.require_thresholds({})
        message = str(ctx.exception)
        for _, flag, _ in B.THRESHOLD_SPEC:
            self.assertIn(flag, message)
        self.assertIn("4 of 4", message)

    def test_each_missing_threshold_is_named_on_its_own(self):
        for attr, flag, _ in B.THRESHOLD_SPEC:
            supplied = dict(TEST_THRESHOLDS)
            supplied.pop(attr)
            with self.assertRaises(B.MissingThresholdError) as ctx:
                B.require_thresholds(supplied)
            message = str(ctx.exception)
            self.assertIn(flag, message)
            self.assertIn("1 of 4", message)

    def test_the_refusal_says_what_the_parameter_decides(self):
        with self.assertRaises(B.MissingThresholdError) as ctx:
            B.require_thresholds({})
        message = str(ctx.exception)
        self.assertIn("before a zero-conversion term may be negated", message)
        self.assertIn("before its zero is trusted", message)
        self.assertIn("NO Search campaign in this account carries", message)

    def test_zero_is_a_legal_threshold_and_none_is_not(self):
        """`0` is a choice. `None` is an absence. They must not be conflated."""
        supplied = dict(TEST_THRESHOLDS, min_spend_cad=Decimal("0"), min_clicks=0)
        thresholds = B.require_thresholds(supplied)
        self.assertEqual(thresholds.min_spend_cad, Decimal("0"))
        self.assertEqual(thresholds.min_clicks, 0)

    def test_a_negative_threshold_refuses(self):
        with self.assertRaises(B.MissingThresholdError):
            B.require_thresholds(dict(TEST_THRESHOLDS, min_clicks=-1))

    def test_a_zero_multiple_refuses_because_it_cuts_every_converter(self):
        with self.assertRaises(B.MissingThresholdError):
            B.require_thresholds(
                dict(TEST_THRESHOLDS, target_cpa_multiple=Decimal("0"))
            )

    def test_the_module_declares_no_default_anywhere(self):
        """A default would defeat the refusal, so assert none exists."""
        self.assertEqual(len(B.Thresholds.__dataclass_fields__), 4)
        import dataclasses

        for field in B.Thresholds.__dataclass_fields__.values():
            self.assertIs(field.default, dataclasses.MISSING, field.name)
            self.assertIs(field.default_factory, dataclasses.MISSING, field.name)

    @unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
    def test_the_cli_refuses_with_no_thresholds(self):
        code = B.main([str(AUG20)])
        self.assertEqual(code, 1)


@unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
class RowKeyTests(unittest.TestCase):
    """VF-19. Every join on the (parent, child) pair, proven on this pull."""

    @classmethod
    def setUpClass(cls):
        cls.cost = json.loads((AUG20 / B.COST).read_text(encoding="utf-8"))["data"][
            "results"
        ]

    def _pair_costs(self, rows):
        out: dict = {}
        for row in rows:
            pair = B.criterion_pair(row)
            if not pair:
                continue
            cost = F.micros_to_currency(row["metrics"]["costMicros"])
            out[pair] = out.get(pair, Decimal(0)) + cost
        return out

    def _bare_costs(self, rows, last_write_wins: bool):
        """What a `criterion_id`-keyed dict produces. The bug, written out."""
        out: dict = {}
        for row in rows:
            pair = B.criterion_pair(row)
            if not pair:
                continue
            _, criterion_id = pair
            cost = F.micros_to_currency(row["metrics"]["costMicros"])
            if criterion_id in out and not last_write_wins:
                out[criterion_id] = (out[criterion_id][0], out[criterion_id][1] + cost)
                continue
            previous = out.get(criterion_id, (pair[0], Decimal(0)))
            out[criterion_id] = (pair[0], previous[1] + cost)
        return out

    # Asserts a fact about this account, not builder behaviour.

    # TODO(fixture): behaviour test, not an account claim. It needs fixture
    # data this synthetic set does not yet carry. Enrich make_fixture.py and
    # drop this marker; do not leave it here as a permanent exemption.

    # TODO(fixture): behaviour test, not an account claim. It needs fixture
    # data this synthetic set does not yet carry. Enrich make_fixture.py and
    # drop this marker; do not leave it here as a permanent exemption.

    def test_the_term_key_is_the_four_part_tuple(self):
        row = self.cost[0]
        key = B.term_key(row)
        self.assertEqual(len(key), 4)
        self.assertEqual(key[0], row["campaign"]["id"])
        self.assertEqual(key[1], row["adGroup"]["id"])

    def test_match_type_adds_no_distinct_keys_so_the_join_is_sound(self):
        """The conversions pull has no match type, so the key cannot carry it.

        Asserted as an inequality rather than on today's equal counts: if match
        type ever splits a key, the four-part join silently merges two rows.
        """
        four = {B.term_key(r) for r in self.cost}
        five = {
            B.term_key(r)
            + (
                (((r.get("segments") or {}).get("keyword") or {}).get("info") or {})
                .get("matchType"),
            )
            for r in self.cost
        }
        self.assertEqual(len(four), len(five))
        self.assertEqual(len(four), len(self.cost))


@unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
class CategoryTests(unittest.TestCase):
    """The category is a label. It can never change a decision."""

    @classmethod
    def setUpClass(cls):
        cls.thresholds = B.require_thresholds(dict(TEST_THRESHOLDS))
        cls.fs, cls.rows, cls.csv, cls.lint = B.build(AUG20, cls.thresholds)

    def test_decide_cannot_see_the_term_text(self):
        """Structural, not behavioural: the input record has no text field."""
        fields = set(B.Numbers.__dataclass_fields__)
        self.assertEqual(
            fields,
            {
                "cost_cad",
                "clicks",
                "conversions_8action",
                # A boolean derived from the term text UPSTREAM, exactly like
                # term_account_conversions_8action. decide() still cannot read
                # the text itself, which is what this test is about.
                "converted_in_90d",
                "assumed_value",
                "term_account_conversions_8action",
                "status",
            },
        )
        for banned in ("search_term", "term", "keyword", "category", "campaign", "theme"):
            self.assertNotIn(banned, fields)

    def test_blanking_every_category_changes_no_decision(self):
        before = [(r.key, r.decision, r.reason) for r in self.rows]
        rows = copy.deepcopy(self.rows)
        for row in rows:
            row.category = ""
        B.apply_decisions(rows, self.thresholds, converted_90d())
        after = [(r.key, r.decision, r.reason) for r in rows]
        self.assertEqual(before, after)

    def test_shuffling_and_rotating_categories_changes_no_decision(self):
        before = [(r.key, r.decision) for r in self.rows]
        for seed in (1, 7, 19):
            rows = copy.deepcopy(self.rows)
            labels = [r.category for r in rows]
            random.Random(seed).shuffle(labels)
            for row, label in zip(rows, labels):
                row.category = label
            # Re-decide from the numbers with the categories deliberately wrong.
            account = B.account_conversions_by_term_text(rows)
            cleared = converted_90d()
            for row in rows:
                numbers = B.Numbers(
                    cost_cad=row.cost_cad,
                    clicks=row.clicks,
                    conversions_8action=row.conversions,
                    assumed_value=row.assumed_value,
                    term_account_conversions_8action=account[row.search_term],
                    status=row.status,
                    converted_in_90d=row.search_term.strip().lower() in cleared,
                )
                row.decision = B.decide(numbers, self.thresholds)
            self.assertEqual(before, [(r.key, r.decision) for r in rows])

    def test_setting_every_category_to_each_single_value_changes_no_decision(self):
        before = [(r.key, r.decision) for r in self.rows]
        for label in B.CATEGORIES:
            rows = copy.deepcopy(self.rows)
            for row in rows:
                row.category = label
            B.apply_decisions(rows, self.thresholds, converted_90d())
            for row in rows:
                row.category = label
            self.assertEqual(before, [(r.key, r.decision) for r in rows])

    def test_the_off_theme_categories_do_not_select_the_negatives(self):
        """The owner's two examples: a real buyer and someone discovering us.

        `free invoicing app for freelancers` labels `wrong intent` and
        `zoom`-style discovery labels `off-topic` or `too generic` under the old
        skill. Neither label may decide anything, so assert that rows carrying
        those labels appear on both sides of the decision.
        """
        judgement_labels = {"wrong intent", "off-topic", "too generic", "research-only"}
        labelled = [r for r in self.rows if r.category in judgement_labels]
        self.assertTrue(labelled)
        decisions = {r.decision for r in labelled}
        self.assertGreater(
            len(decisions),
            1,
            "every judgement-labelled row got the same decision, which would "
            "mean the label and the numbers are indistinguishable here",
        )
        kept = [r for r in labelled if r.decision not in B.CSV_DECISIONS]
        self.assertTrue(
            kept, "a judgement label must be able to survive as a KEEP"
        )


class DecisionTests(unittest.TestCase):
    """The negate/keep rules, one branch at a time."""

    def setUp(self):
        self.t = B.require_thresholds(dict(TEST_THRESHOLDS))

    def numbers(self, **kwargs):
        base = dict(
            cost_cad=Decimal("100"),
            clicks=10,
            conversions_8action=Decimal(0),
            assumed_value=Decimal(0),
            term_account_conversions_8action=Decimal(0),
            status="NONE",
        )
        base.update(kwargs)
        return B.Numbers(**base)

    def test_added_status_is_never_negated(self):
        """`rivalco app`: CAD 512.30, zero conversions, and our own keyword."""
        decision = B.decide(
            self.numbers(cost_cad=Decimal("512.30"), clicks=30, status="ADDED"), self.t
        )
        self.assertEqual(decision, B.NOT_ELIGIBLE_ADDED)
        self.assertNotIn(decision, B.CSV_DECISIONS)

    def test_excluded_status_is_never_negated(self):
        self.assertEqual(
            B.decide(self.numbers(status="EXCLUDED"), self.t), B.NOT_ELIGIBLE_EXCLUDED
        )

    def test_spend_without_clicks_is_held_not_negated(self):
        """CAD 398.40 on 2 clicks: the zero is not yet trustworthy."""
        self.assertEqual(
            B.decide(self.numbers(cost_cad=Decimal("398.40"), clicks=2), self.t),
            B.HOLD_THIN_CLICKS,
        )

    def test_spend_and_clicks_together_negate(self):
        self.assertEqual(
            B.decide(self.numbers(cost_cad=Decimal("419.68"), clicks=7), self.t),
            B.NEGATE_ZERO,
        )

    def test_below_the_spend_floor_is_kept_however_many_clicks(self):
        self.assertEqual(
            B.decide(self.numbers(cost_cad=Decimal("49.99"), clicks=900), self.t),
            B.KEEP_BELOW_FLOOR,
        )

    def test_a_converter_within_the_cut_line_is_kept(self):
        """`how to send an invoice`: CAD 176.25, one priority-plan conversion, CAD 1,700 assumed."""
        self.assertEqual(
            B.decide(
                self.numbers(
                    cost_cad=Decimal("176.25"),
                    clicks=1,
                    conversions_8action=Decimal(1),
                    assumed_value=Decimal(1700),
                    term_account_conversions_8action=Decimal(1),
                ),
                self.t,
            ),
            B.KEEP_CONVERTING,
        )

    def test_a_converter_above_the_cut_line_is_negated(self):
        self.assertEqual(
            B.decide(
                self.numbers(
                    cost_cad=Decimal("6000"),
                    clicks=50,
                    conversions_8action=Decimal(1),
                    term_account_conversions_8action=Decimal(1),
                ),
                self.t,
            ),
            B.NEGATE_OVER_CPA,
        )

    def test_the_cut_line_is_the_target_times_the_multiple(self):
        self.assertEqual(self.t.cut_cpa_cad, Decimal("1500"))
        just_under = B.decide(
            self.numbers(
                cost_cad=Decimal("1499.99"),
                conversions_8action=Decimal(1),
                term_account_conversions_8action=Decimal(1),
            ),
            self.t,
        )
        just_over = B.decide(
            self.numbers(
                cost_cad=Decimal("1500.01"),
                conversions_8action=Decimal(1),
                term_account_conversions_8action=Decimal(1),
            ),
            self.t,
        )
        self.assertEqual(just_under, B.KEEP_CONVERTING)
        self.assertEqual(just_over, B.NEGATE_OVER_CPA)

    def test_a_term_converting_elsewhere_is_never_negated_from_one_row(self):
        """The term-scoped safety check. `(campaign, term)` under-protects."""
        self.assertEqual(
            B.decide(
                self.numbers(
                    cost_cad=Decimal("1000"),
                    clicks=100,
                    conversions_8action=Decimal(0),
                    term_account_conversions_8action=Decimal("0.5"),
                ),
                self.t,
            ),
            B.KEEP_CONVERTS_ELSEWHERE,
        )

    def test_every_reason_is_numbers_and_names_no_category(self):
        for status in ("NONE", "ADDED", "EXCLUDED"):
            for cost, clicks, conv in (
                (Decimal("10"), 1, Decimal(0)),
                (Decimal("500"), 50, Decimal(0)),
                (Decimal("500"), 1, Decimal(0)),
                (Decimal("500"), 5, Decimal(2)),
                (Decimal("9000"), 5, Decimal(1)),
            ):
                n = self.numbers(
                    cost_cad=cost,
                    clicks=clicks,
                    conversions_8action=conv,
                    term_account_conversions_8action=conv,
                    status=status,
                )
                reason = B.why(n, self.t, B.decide(n, self.t))
                self.assertTrue(reason)
                for label in B.CATEGORIES:
                    self.assertNotIn(label, reason)


@unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
class BuildTests(unittest.TestCase):
    """End to end over the real raw directory."""

    @classmethod
    def setUpClass(cls):
        cls.t = B.require_thresholds(dict(TEST_THRESHOLDS))
        cls.fs, cls.rows, cls.csv, cls.lint = B.build(AUG20, cls.t)

    def value(self, id):
        return self.fs.value(id)

    # TODO(fixture): behaviour test, not an account claim. It needs fixture
    # data this synthetic set does not yet carry. Enrich make_fixture.py and
    # drop this marker; do not leave it here as a permanent exemption.

    # TODO(fixture): behaviour test, not an account claim. It needs fixture
    # data this synthetic set does not yet carry. Enrich make_fixture.py and
    # drop this marker; do not leave it here as a permanent exemption.

    # Asserts a fact about this account, not builder behaviour.

    def test_every_decision_has_a_count_and_a_spend_fact(self):
        for slug in (
            "negate_zero_conversion",
            "negate_over_cpa",
            "hold_clicks_below_floor",
            "keep_below_spend_floor",
            "keep_converting",
            "keep_converts_elsewhere",
            "not_eligible_added",
            "not_eligible_excluded",
        ):
            self.assertIn(f"st.decision.{slug}.row.count", self.fs)
            self.assertIn(f"st.decision.{slug}.spend.total", self.fs)
        counts = sum(
            self.value(f.id) for f in self.fs if f.id.startswith("st.decision.")
            and f.id.endswith(".row.count")
        )
        self.assertEqual(counts, self.value("st.pull.cost.row.count"))

    def test_the_thresholds_are_facts_so_the_report_must_state_them(self):
        self.assertEqual(self.value("st.threshold.min_spend_cad"), Decimal("50.00"))
        self.assertEqual(self.value("st.threshold.min_clicks"), 3)
        self.assertEqual(self.value("st.threshold.cut_cpa_cad"), Decimal("1500.00"))
        self.assertIn(
            "no default", self.fs.get("st.threshold.min_clicks").scope
        )

    def test_the_top_table_has_ten_ranks_and_every_column(self):
        self.assertEqual(self.value("st.top_table.selection_rule.top_n"), B.TOP_N)
        for rank in range(1, B.TOP_N + 1):
            for column in (
                "search_term",
                "keyword_text",
                "match_type",
                "campaign_name",
                "spend",
                "clicks",
                "conversions",
                "assumed_value_cpa",
                "assumed_value_roas",
                "decision",
                "why",
                "category",
            ):
                self.assertIn(f"st.top_table.rank_{rank}.{column}", self.fs)

    def test_the_top_table_is_ranked_by_spend_descending(self):
        spends = [
            self.value(f"st.top_table.rank_{r}.spend") for r in range(1, B.TOP_N + 1)
        ]
        self.assertEqual(spends, sorted(spends, reverse=True))

    def test_cpa_is_null_rather_than_zero_when_nothing_converted(self):
        for rank in range(1, B.TOP_N + 1):
            conv = self.value(f"st.top_table.rank_{rank}.conversions")
            cpa = self.value(f"st.top_table.rank_{rank}.assumed_value_cpa")
            if conv == 0:
                self.assertIsNone(cpa)
            else:
                self.assertIsNotNone(cpa)

    def test_every_cpa_and_roas_fact_is_labelled_assumed_value(self):
        """VF-8. The qualifier must be present, not the word ROAS banned.

        Scoped to facts that are themselves a CPA or ROAS figure.
        `target_cpa_multiple` is a dimensionless multiplier, not a CPA, so it is
        not in scope; the multiplier's product, `cut_cpa_cad`, is.
        """
        suffixes = ("_cpa", "_cpa_cad", "_roas", "assumed_value.total")
        checked = 0
        for fact in self.fs:
            if not fact.id.endswith(suffixes):
                continue
            checked += 1
            haystack = f"{fact.unit} {fact.scope}".lower()
            self.assertIn("assumed", haystack, fact.id)
        self.assertGreater(checked, 10)
        self.assertNotIn(
            "st.threshold.target_cpa_multiple",
            [f.id for f in self.fs if f.id.endswith(suffixes)],
        )

    def test_the_facts_file_stays_small_enough_to_check(self):
        """VF-15a rule 6. A fact per term is the failure mode, not the goal."""
        # Tight enough to trip on a fact-per-term regression: 1,681 terms would
        # blow this by an order of magnitude, and so would 70 more aggregates.
        self.assertLess(len(self.fs), 250)
        self.assertGreater(len(self.fs), 100)

    def test_the_guard_refuses_to_compare_a_windowed_fact_to_a_threshold(self):
        with self.assertRaises(F.FactMismatchError):
            self.fs.compare("st.threshold.min_spend_cad", "st.spend.total")

    # Asserts a fact about this account, not builder behaviour.

    # TODO(fixture): behaviour test, not an account claim. It needs fixture
    # data this synthetic set does not yet carry. Enrich make_fixture.py and
    # drop this marker; do not leave it here as a permanent exemption.

    # TODO(fixture): behaviour test, not an account claim. It needs fixture
    # data this synthetic set does not yet carry. Enrich make_fixture.py and
    # drop this marker; do not leave it here as a permanent exemption.

    def test_a_campaign_budget_event_alone_never_downgrades_a_term(self):
        change = json.loads((AUG20 / B.CHANGE_EVENTS).read_text(encoding="utf-8"))[
            "data"
        ]["results"]
        budget_only = [
            r
            for r in change
            if r["changeEvent"]["changeResourceType"] == "CAMPAIGN_BUDGET"
        ]
        ad_groups, campaigns, _ = B.criterion_change_scope(budget_only)
        self.assertEqual(ad_groups, set())
        self.assertEqual(campaigns, set())

    # TODO(fixture): behaviour test, not an account claim. It needs fixture
    # data this synthetic set does not yet carry. Enrich make_fixture.py and
    # drop this marker; do not leave it here as a permanent exemption.

    # TODO(fixture): behaviour test, not an account claim. It needs fixture
    # data this synthetic set does not yet carry. Enrich make_fixture.py and
    # drop this marker; do not leave it here as a permanent exemption.

    def test_the_planner_fact_is_null_and_says_it_cannot_pick(self):
        fact = self.fs.get("st.keyword_idea.planner.usable_for_selection")
        self.assertIsNone(fact.value)
        self.assertIn("never to pick one", fact.scope)
        self.assertIn("unrelated industries", fact.scope)

    def test_no_search_term_deep_link_appears_anywhere(self):
        """VF-13: the search-terms URL path is unverified. IDs only."""
        blob = json.dumps(
            [f.as_dict() for f in self.fs] + self.csv, default=str
        )
        for banned in ("ads.google.com", "ocid=", "http://", "https://"):
            self.assertNotIn(banned, blob)


@unittest.skipUnless(AUG20.is_dir(), f"raw dir not present: {AUG20}")
class CsvTests(unittest.TestCase):
    """The deliverable. Correct columns, one negative per row, lint reported."""

    @classmethod
    def setUpClass(cls):
        cls.t = B.require_thresholds(dict(TEST_THRESHOLDS))
        cls.fs, cls.rows, cls.csv, cls.lint = B.build(AUG20, cls.t)

    def test_only_negate_decisions_reach_the_csv(self):
        self.assertTrue(self.csv)
        for row in self.csv:
            self.assertIn(row["Decision"], B.CSV_DECISIONS)

    # TODO(fixture): behaviour test, not an account claim. It needs fixture
    # data this synthetic set does not yet carry. Enrich make_fixture.py and
    # drop this marker; do not leave it here as a permanent exemption.

    def test_the_conversions_column_is_labelled_eight_action(self):
        """No conversion column may ship without its basis on the label.

        Substring, not membership. `assertNotIn("Conversions", CSV_HEADER)` is
        exact-membership on a tuple, so it passes even when a second, unlabelled
        conversion column exists under any other spelling. The 2026-08-10 file
        shipped the unrestricted ~20-action count beside a negate recommendation;
        a test that cannot catch that is worse than no test.
        """
        conversion_columns = [c for c in B.CSV_HEADER if "onversion" in c]
        self.assertEqual(conversion_columns, ["Conversions (8 actions only)"])
        for column in conversion_columns:
            self.assertIn("8 action", column)
        # And the planted regression fails.
        planted = B.CSV_HEADER + ("Conversions",)
        self.assertNotEqual(
            [c for c in planted if "onversion" in c],
            ["Conversions (8 actions only)"],
        )

    def test_the_csv_declares_the_level_its_negatives_apply_at(self):
        self.assertIn("Level", B.CSV_HEADER)
        for row in self.csv:
            self.assertEqual(row["Level"], B.CSV_LEVEL)

    def test_one_negative_per_row_after_dedup(self):
        seen = [tuple(r[c] for c in B.CSV_IDENTITY) for r in self.csv]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertGreater(self.fs.value("st.csv.duplicate_negative_collapsed.row.count"), 0)

    def test_a_collapsed_row_keeps_both_triggering_keywords_and_sums_the_cost(self):
        merged = [r for r in self.csv if " | " in r["Triggering Keyword"]]
        self.assertTrue(merged)
        for row in merged:
            keywords = row["Triggering Keyword"].split(" | ")
            match_types = row["Triggering Match Type"].split(" | ")
            self.assertEqual(len(keywords), len(match_types))
            source = [
                r
                for r in self.rows
                if r.campaign_id == row["Campaign ID"]
                and r.ad_group_id == row["Ad Group ID"]
                and r.search_term == row["Search Term"]
                and r.decision in B.CSV_DECISIONS
                and not r.learning_note
            ]
            self.assertEqual(
                Decimal(row["Cost (CAD)"]),
                F.money(sum((r.cost_cad for r in source), Decimal(0))),
            )

    def test_the_lint_catches_a_planted_over_length_keyword(self):
        bad = dict(self.csv[0], Keyword="x" * (B.KEYWORD_MAX_CHARS + 1))
        self.assertEqual(len(B.lint_csv([bad])["over_length"]), 1)
        self.assertEqual(len(B.lint_csv([self.csv[0]])["over_length"]), 0)

    def test_the_lint_catches_a_planted_comma_and_quote(self):
        comma = dict(self.csv[0], Keyword="free, cheap invoicing")
        quote = dict(self.csv[0], Keyword="what's an invoice")
        self.assertEqual(len(B.lint_csv([comma])["comma_in_keyword"]), 1)
        self.assertEqual(len(B.lint_csv([quote])["quote_char"]), 1)

    def test_the_lint_catches_a_planted_duplicate_negative(self):
        twice = [self.csv[0], dict(self.csv[0])]
        self.assertEqual(len(B.lint_csv(twice)["duplicate_identity"]), 2)

    def test_the_lint_catches_a_missing_column(self):
        short = {k: v for k, v in self.csv[0].items() if k != "Level"}
        problems = B.lint_csv([short])
        self.assertEqual(len(problems["wrong_width"]), 1)
        self.assertEqual(len(problems["missing_column"]), 1)

    def test_no_row_ships_without_an_ad_group_name(self):
        """Editor imports by ad group NAME, so a nameless row is unimportable.

        VF-20: this asserts the contract. Its predecessor asserted that EVERY
        row was missing the name, with the docstring "neither pull selects it"
        - which pinned a pull defect as expected behaviour, and failed the
        moment the pull was fixed on 2026-08-20.
        """
        self.assertEqual(self.fs.value("st.csv.lint.missing_ad_group_name.row.count"), 0)
        for row in self.csv:
            self.assertTrue(row["Ad Group"])
            self.assertNotEqual(row["Ad Group"], B.AD_GROUP_NAME_MISSING)

    def test_the_lint_still_catches_a_planted_nameless_row(self):
        """The detector has to keep working now that the pull is fixed."""
        nameless = dict(self.csv[0], **{"Ad Group": B.AD_GROUP_NAME_MISSING})
        self.assertEqual(len(B.lint_csv([nameless])["missing_ad_group_name"]), 1)

    # ----------------------------------------------------------------
    # The 90-day do-not-negate clearance. Step 1b's window was written as
    # LAST_30_DAYS, the pull stopped happening, and on 2026-08-20 `jotform`
    # returned to a negation list it had been removed from 15 days earlier.
    # These assert the guard, not this run's numbers.
    # ----------------------------------------------------------------

    def test_a_term_that_converted_in_90_days_is_never_negated(self):
        """The full-stop rule, tested directly on the decision function."""
        n = B.Numbers(
            cost_cad=Decimal("5000"), clicks=500,
            conversions_8action=Decimal(0), assumed_value=Decimal(0),
            term_account_conversions_8action=Decimal(0), status="NONE",
            converted_in_90d=True,
        )
        t = B.require_thresholds(dict(TEST_THRESHOLDS))
        self.assertEqual(B.decide(n, t), B.KEEP_CONVERTED_90D)
        # Same numbers without the 90-day hit would have been negated.
        self.assertEqual(
            B.decide(dataclasses.replace(n, converted_in_90d=False), t),
            B.NEGATE_ZERO,
        )

    def test_the_90_day_rule_outranks_the_over_cpa_cut(self):
        """A converting term over the cut line is still not a 90-day negate."""
        n = B.Numbers(
            cost_cad=Decimal("100000"), clicks=500,
            conversions_8action=Decimal(0), assumed_value=Decimal(0),
            term_account_conversions_8action=Decimal(0), status="NONE",
            converted_in_90d=True,
        )
        t = B.require_thresholds(dict(TEST_THRESHOLDS))
        self.assertNotIn("NEGATE", B.decide(n, t))

    def test_the_90_day_reader_ignores_zero_conversion_rows(self):
        got = B.converted_in_90d_terms({"data": {"results": [
            {"searchTermView": {"searchTerm": "converted"}, "metrics": {"conversions": 1}},
            {"searchTermView": {"searchTerm": "zeroed"}, "metrics": {"conversions": 0}},
            {"searchTermView": {"searchTerm": "MiXeD Case"}, "metrics": {"conversions": 0.5}},
        ]}})
        self.assertEqual(got, {"converted", "mixed case"})

    def test_a_missing_clearance_emits_no_csv_rows_at_all(self):
        """Absent guard means nothing ships. A refusal is a finding."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / AUG20.name
            shutil.copytree(AUG20, dest)
            (dest / B.DONOTNEGATE_90D).unlink()
            fs, _rows, out_csv, _lint = B.build(
                dest, B.require_thresholds(dict(TEST_THRESHOLDS))
            )
        self.assertEqual(out_csv, [])
        self.assertFalse(fs.value("st.donotnegate.90d.clearance_pulled"))
        self.assertIsNone(fs.value("st.donotnegate.90d.converting_term.count"))

    def test_the_clearance_is_read_from_the_raw_dir_without_a_flag(self):
        """A guard that needs remembering is a guard that gets forgotten."""
        self.assertTrue(self.fs.value("st.donotnegate.90d.clearance_pulled"))
        self.assertGreater(self.fs.value("st.donotnegate.90d.converting_term.count"), 0)

    # ----------------------------------------------------------------
    # --existing-negatives. Until 2026-08-20 the flag took file paths, never
    # opened them, and relabelled every row as checked. These assert that it
    # reads, compares, and removes - not that it merely sets a flag.
    # ----------------------------------------------------------------

    def _synthetic(self, rows):
        return B.existing_negatives([{"data": {"results": rows}}])

    @staticmethod
    def _row(*, campaign_id, ad_group_id, search_term):
        return B.TermRow(
            campaign_id=campaign_id, campaign_name="c",
            ad_group_id=ad_group_id, ad_group_name="a",
            search_term=search_term, keyword_text="k", match_type="BROAD",
            status="NONE", cost_cad=Decimal("500"), clicks=10, impressions=100,
            conversions=Decimal(0), assumed_value=Decimal(0), criterion=None,
        )

    def test_an_exact_negative_covers_only_the_identical_term(self):
        index = self._synthetic([
            {"campaign": {"id": "1"}, "adGroup": {"id": "2"},
             "adGroupCriterion": {"negative": True,
                                  "keyword": {"text": "zoom", "matchType": "EXACT"}}},
        ])
        self.assertTrue(B._covers("zoom", "EXACT", "zoom"))
        self.assertFalse(B._covers("zoom", "EXACT", "zoom meeting"))
        self.assertEqual(index["rows"], 1)

    def test_phrase_and_broad_coverage_follow_google_semantics(self):
        # PHRASE needs a contiguous run of words; BROAD needs all words in any order.
        self.assertTrue(B._covers("visit summary", "PHRASE", "after visit summary tpl"))
        self.assertFalse(B._covers("visit summary", "PHRASE", "summary of the visit"))
        self.assertTrue(B._covers("summary visit", "BROAD", "after visit summary tpl"))
        self.assertFalse(B._covers("summary invoice", "BROAD", "after visit summary"))

    def test_a_negative_on_another_ad_group_does_not_cover_this_row(self):
        """Ad-group negatives are scoped to their own ad group (VF-19 pair key)."""
        index = self._synthetic([
            {"campaign": {"id": "C"}, "adGroup": {"id": "OTHER"},
             "adGroupCriterion": {"negative": True,
                                  "keyword": {"text": "jotform", "matchType": "EXACT"}}},
        ])
        row = self._row(campaign_id="C", ad_group_id="MINE", search_term="jotform")
        self.assertIsNone(B.existing_coverage(row, index))

    def test_a_campaign_level_negative_covers_every_ad_group_in_it(self):
        index = self._synthetic([
            {"campaign": {"id": "C"},
             "campaignCriterion": {"negative": True,
                                   "keyword": {"text": "jotform", "matchType": "EXACT"}}},
        ])
        row = self._row(campaign_id="C", ad_group_id="ANY", search_term="jotform")
        self.assertIn("campaign level", B.existing_coverage(row, index) or "")

    def test_a_positive_keyword_is_not_read_as_a_negative(self):
        index = self._synthetic([
            {"campaign": {"id": "C"}, "adGroup": {"id": "A"},
             "adGroupCriterion": {"negative": False,
                                  "keyword": {"text": "jotform", "matchType": "EXACT"}}},
        ])
        self.assertEqual(index["rows"], 0)
        row = self._row(campaign_id="C", ad_group_id="A", search_term="jotform")
        self.assertIsNone(B.existing_coverage(row, index))

    def test_an_unrecognised_match_type_is_reported_not_guessed(self):
        index = self._synthetic([
            {"campaign": {"id": "C"}, "adGroup": {"id": "A"},
             "adGroupCriterion": {"negative": True,
                                  "keyword": {"text": "x", "matchType": "WHAT"}}},
        ])
        self.assertIn("WHAT", {str(m) for m in index["unrecognised_match_types"]})
        row = self._row(campaign_id="C", ad_group_id="A", search_term="x")
        self.assertIsNone(B.existing_coverage(row, index))

    def test_no_inventory_means_no_coverage_claim_at_all(self):
        row = self._row(campaign_id="C", ad_group_id="A", search_term="anything")
        self.assertIsNone(B.existing_coverage(row, None))

    def test_the_existing_negative_inventory_is_declared_unchecked(self):
        fact = self.fs.get("st.csv.existing_negative_inventory.checked")
        self.assertIsNone(fact.value)
        self.assertIn("NOT supplied", fact.scope)
        for row in self.csv:
            self.assertIn("NOT RUN", row["Existing Negative Check"])

    def test_a_written_csv_round_trips_with_the_right_column_count(self):
        import csv as csvmod

        with tempfile.TemporaryDirectory() as tmp:
            path = B.write_csv(Path(tmp) / "negatives.csv", self.csv)
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csvmod.reader(handle)
                header = next(reader)
                self.assertEqual(header, list(B.CSV_HEADER))
                count = 0
                for line in reader:
                    self.assertEqual(len(line), len(B.CSV_HEADER))
                    count += 1
                self.assertEqual(count, len(self.csv))


class SensitivityTests(unittest.TestCase):
    """The grid helps the owner choose. It never chooses."""



    def test_a_malformed_pair_refuses(self):
        with self.assertRaises(B.MissingThresholdError):
            B.parse_pairs(["250"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
