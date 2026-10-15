#!/usr/bin/env python3
"""Tests for the facts-file harness.

Run:  python3 tools/test_facts.py

The tests that matter most are in GuardTests. They encode the actual Aug-10
regression from VERIFICATION-RULES VF-15: $49,210.77 including conquesting insertion ad
groups was compared against an Aug-5 figure of $48,323.11 excluding them, and
the difference was reported as growth. Both numbers were individually correct.
The guard must refuse that comparison on scope alone.

Standard library only.
"""

from __future__ import annotations

import json
import re
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

import asset_facts  # noqa: E402
import build_audit_ad_copy_facts as builder  # noqa: E402
import citations  # noqa: E402
import claims  # noqa: E402
import facts as F  # noqa: E402
import gaql  # noqa: E402
import pulls  # noqa: E402
import scopes as S  # noqa: E402
from facts import (  # noqa: E402
    DuplicateFactError,
    Fact,
    FactMismatchError,
    FactSet,
    FactsError,
    MissingJustificationError,
    NonAdditiveFactError,
    NullFactValueError,
)

REPO = HERE.parent
AUG10 = _resolve_raw("2026-08-10")
AUG19 = _resolve_raw("2026-08-19")

# The two scopes from the real regression, written out verbatim.
SCOPE_INCLUDING_CONQUESTING = (
    "keyword.match_type = 'BROAD' AND the ad group's RSA uses keyword "
    "insertion, INCLUDING the 2 conquesting insertion ad groups"
)
SCOPE_EXCLUDING_CONQUESTING = (
    "keyword.match_type = 'BROAD' AND the ad group's RSA uses keyword "
    "insertion, EXCLUDING conquesting (9 non-competitor ad groups only)"
)
WINDOW_30D = "2026-07-11..2026-08-09 (LAST_30_DAYS as of 2026-08-10)"


def regression_factset() -> FactSet:
    """The Aug-10 vs Aug-5 keyword-insertion spend figures.

    unit, window and basis all match. Only `scope` differs. That single label is
    the only thing that catches the error.
    """
    fs = FactSet(meta={"case": "VF-15 Aug-10 regression"})
    fs.add(
        id="insertion.spend.including_conquesting",
        value=Decimal("53453.02"),
        unit="currency_cad",
        scope=SCOPE_INCLUDING_CONQUESTING,
        window=WINDOW_30D,
        source="audit-ad-copy-keyword-qs-30d.json",
        basis="n/a",
    )
    fs.add(
        id="insertion.spend.excluding_conquesting",
        value=Decimal("48323.11"),
        unit="currency_cad",
        scope=SCOPE_EXCLUDING_CONQUESTING,
        window=WINDOW_30D,
        source="audit-ad-copy-2026-08-05.md section 2d",
        basis="n/a",
    )
    return fs


# ==========================================================================
# The guard
# ==========================================================================


class GuardTests(unittest.TestCase):
    def test_compare_refuses_two_facts_differing_only_in_scope(self):
        """The Aug-10 regression. Same unit, window, basis. Scope differs."""
        fs = regression_factset()
        a = fs.get("insertion.spend.including_conquesting")
        b = fs.get("insertion.spend.excluding_conquesting")
        # Prove the premise: everything except scope is identical.
        self.assertEqual(a.unit, b.unit)
        self.assertEqual(a.window, b.window)
        self.assertEqual(a.basis, b.basis)
        self.assertNotEqual(a.scope, b.scope)

        with self.assertRaises(FactMismatchError) as ctx:
            fs.compare(
                "insertion.spend.including_conquesting",
                "insertion.spend.excluding_conquesting",
            )
        msg = str(ctx.exception)
        # The error must name the attribute that mismatched...
        self.assertIn("scope mismatch", msg)
        # ...and show both values, so nobody has to go look them up.
        self.assertIn("INCLUDING the 2 conquesting insertion ad groups", msg)
        self.assertIn("EXCLUDING conquesting", msg)
        # ...and it must not have quietly produced the false delta.
        self.assertNotIn("5129.91", msg)

    def test_the_false_delta_is_never_returned(self):
        """The bug reported +$5,129.91 as growth. Nothing may return it."""
        fs = regression_factset()
        ids = (
            "insertion.spend.including_conquesting",
            "insertion.spend.excluding_conquesting",
        )
        for call in (
            lambda: fs.compare(*ids),
            lambda: fs.diff(*ids),
            lambda: fs.sum(ids),
        ):
            with self.assertRaises(FactMismatchError):
                call()

    def test_refuses_on_unit_window_and_basis_too(self):
        base = dict(
            value=10,
            unit="count",
            scope="scope X",
            window="n/a",
            source="s.json",
            basis="n/a",
        )
        for attr, other in (
            ("unit", "currency_cad"),
            ("scope", "scope Y"),
            ("window", "2026-08-01..2026-08-07"),
            ("basis", "8-action"),
        ):
            with self.subTest(attr=attr):
                fs = FactSet()
                fs.add(id="a", **base)
                fs.add(id="b", **{**base, attr: other})
                with self.assertRaises(FactMismatchError) as ctx:
                    fs.compare("a", "b")
                self.assertIn(f"{attr} mismatch", str(ctx.exception))

    def test_names_every_mismatched_attribute_not_just_the_first(self):
        fs = FactSet()
        fs.add(
            id="a", value=1, unit="count", scope="X", window="n/a",
            source="s.json", basis="n/a",
        )
        fs.add(
            id="b", value=1, unit="currency_cad", scope="Y",
            window="2026-08-01..2026-08-07", source="s.json", basis="8-action",
        )
        with self.assertRaises(FactMismatchError) as ctx:
            fs.compare("a", "b")
        msg = str(ctx.exception)
        for attr in ("unit", "scope", "window", "basis"):
            self.assertIn(f"{attr} mismatch", msg)
        self.assertIn("4 guarded attribute(s) mismatched", msg)

    def test_compare_permits_matching_facts_and_computes_the_delta(self):
        fs = FactSet()
        common = dict(
            unit="currency_cad",
            scope=SCOPE_EXCLUDING_CONQUESTING,
            window=WINDOW_30D,
            source="s.json",
            basis="n/a",
        )
        fs.add(id="now", value=Decimal("47054.97"), **common)
        fs.add(id="prior", value=Decimal("48323.11"), **common)
        result = fs.compare("now", "prior")
        self.assertEqual(result.delta, Decimal("-1268.14"))
        self.assertEqual(round(float(result.pct_change), 1), -2.6)
        self.assertEqual(fs.diff("now", "prior"), Decimal("-1268.14"))

    def test_sum_refuses_asset_credited_units(self):
        """VF-4 rule 1: never sum metrics across assets."""
        fs = FactSet()
        common = dict(
            unit="impressions_asset_credited",
            scope="ad_group_ad.ad.id=620000000004 (within this ad only)",
            window=WINDOW_30D,
            source="asset-view.json",
            basis="broad-goal-set",
        )
        fs.add(id="r1", value=2658, **common)
        fs.add(id="r2", value=0, **common)
        with self.assertRaises(NonAdditiveFactError) as ctx:
            fs.sum(["r1", "r2"])
        self.assertIn("VF-4 rule 1", str(ctx.exception))

    def test_sum_adds_facts_that_share_all_four_attributes(self):
        fs = FactSet()
        common = dict(
            unit="count", scope="same scope", window="n/a",
            source="s.json", basis="n/a",
        )
        fs.add(id="a", value=59, **common)
        fs.add(id="b", value=60, **common)
        self.assertEqual(fs.sum(["a", "b"]), Decimal(119))

    def test_null_valued_fact_refuses_arithmetic_with_a_clear_error(self):
        fs = FactSet()
        common = dict(
            unit="count", scope="same scope", window="n/a",
            source="s.json", basis="n/a",
        )
        fs.add(id="known", value=59, **common)
        fs.add(id="unavailable", value=None, **common)
        with self.assertRaises(NullFactValueError):
            fs.compare("known", "unavailable")
        with self.assertRaises(NullFactValueError):
            fs.sum(["known", "unavailable"])

    def test_unknown_id_raises_with_the_known_ids_listed(self):
        fs = FactSet()
        fs.add(
            id="rsa.servable.count", value=59, unit="count", scope="X",
            window="n/a", source="s.json", basis="n/a",
        )
        with self.assertRaises(F.UnknownFactError) as ctx:
            fs.get("rsa.servable.cout")
        self.assertIn("rsa.servable.count", str(ctx.exception))


class CompareUnsafeTests(unittest.TestCase):
    def test_rejects_empty_or_whitespace_justification(self):
        fs = regression_factset()
        ids = (
            "insertion.spend.including_conquesting",
            "insertion.spend.excluding_conquesting",
        )
        for bad in ("", "   ", "\n\t", None):
            with self.subTest(justification=repr(bad)):
                with self.assertRaises(MissingJustificationError):
                    fs.compare_unsafe(*ids, bad)
        # Too short to be a real justification.
        with self.assertRaises(MissingJustificationError):
            fs.compare_unsafe(*ids, "fine")
        self.assertEqual(fs.unsafe_comparisons, [])

    def test_a_recorded_override_reaches_the_written_facts_file(self):
        """An override that escapes the output is the guard failing silently."""
        fs = regression_factset()
        justification = (
            "Quoted side by side as two different bases, never subtracted. "
            "See MAINTENANCE.md 2026-08-10."
        )
        result = fs.compare_unsafe(
            "insertion.spend.including_conquesting",
            "insertion.spend.excluding_conquesting",
            justification,
        )
        self.assertEqual(result.delta, Decimal("5129.91"))
        self.assertEqual(
            [m["attribute"] for m in result.mismatches], ["scope"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.json"
            fs.write(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            recorded = payload["unsafe_comparisons"]
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0]["justification"], justification)
            self.assertEqual(recorded[0]["mismatches"][0]["attribute"], "scope")
            self.assertEqual(recorded[0]["id_a"], "insertion.spend.including_conquesting")
            # And it survives a round trip, so a reader of the file sees it too.
            self.assertEqual(len(FactSet.load(path).unsafe_comparisons), 1)


# ==========================================================================
# Sentinel handling (VF-11)
# ==========================================================================


class SentinelTests(unittest.TestCase):
    def test_recognises_the_sentinel_and_the_real_values(self):
        self.assertTrue(F.is_sentinel_share(0.0999))
        self.assertTrue(F.is_sentinel_share(F.SEARCH_IMPRESSION_SHARE_SENTINEL))
        # Brand Core returns a real share; Performance Max and Generic -
        # Features both return the sentinel.
        self.assertFalse(F.is_sentinel_share(0.6954))
        self.assertFalse(F.is_sentinel_share(0.1))
        self.assertFalse(F.is_sentinel_share(None))

    def test_available_impressions_refuses_the_sentinel(self):
        with self.assertRaises(F.SentinelValueError) as ctx:
            F.available_impressions(34854, 0.0999)
        msg = str(ctx.exception)
        self.assertIn("floor, not a", msg)
        self.assertIn("VF-11", msg)

    def test_available_impressions_refuses_null_and_zero(self):
        with self.assertRaises(F.SentinelValueError):
            F.available_impressions(34854, None)
        with self.assertRaises(F.SentinelValueError):
            F.available_impressions(34854, 0.0)

    def test_available_impressions_computes_on_a_real_share(self):
        got = F.available_impressions(34854, 0.6954)
        self.assertAlmostEqual(got, 34854 / 0.6954, places=6)
        self.assertEqual(round(got), 50121)


# ==========================================================================
# Micros
# ==========================================================================


class MicrosTests(unittest.TestCase):
    def test_converts_exactly_with_no_float_drift(self):
        self.assertEqual(
            F.micros_to_currency("48213904417"), Decimal("48213.904417")
        )
        self.assertEqual(F.micros_to_currency(1_000_000), Decimal("1"))
        self.assertEqual(F.micros_to_currency(0), Decimal("0"))
        self.assertEqual(
            F.micros_to_currency(53_453_020_000), Decimal("53453.02")
        )
        self.assertIsNone(F.micros_to_currency(None))

    def test_accepts_the_string_form_the_api_actually_returns(self):
        # costMicros comes back as a JSON string.
        self.assertEqual(F.micros_to_currency("3300000006"), Decimal("3300.000006"))

    def test_money_rounds_half_up_only_for_printing(self):
        self.assertEqual(F.money(Decimal("48213.904417")), Decimal("48213.90"))
        self.assertEqual(F.money(Decimal("0.005")), Decimal("0.01"))
        self.assertEqual(F.money(Decimal("0")), Decimal("0.00"))
        self.assertIsNone(F.money(None))


# ==========================================================================
# Fact and FactSet mechanics
# ==========================================================================


class FactTests(unittest.TestCase):
    def test_scope_is_mandatory(self):
        with self.assertRaises(FactsError) as ctx:
            Fact(
                id="x", value=1, unit="count", scope="", window="n/a",
                source="s.json", basis="n/a",
            )
        self.assertIn("scope is mandatory", str(ctx.exception))

    def test_basis_must_be_one_of_the_three_legal_values(self):
        with self.assertRaises(FactsError):
            Fact(
                id="x", value=1, unit="count", scope="X", window="n/a",
                source="s.json", basis="20-action",
            )
        for basis in F.LEGAL_BASES:
            Fact(
                id="x", value=1, unit="count", scope="X", window="n/a",
                source="s.json", basis=basis,
            )

    def test_fields_are_exactly_those_vf15_specifies(self):
        fact = Fact(
            id="rsa.servable.count", value=61, unit="count", scope="X",
            window="n/a", source="audit-ad-copy-rsa-inventory.json", basis="n/a",
        )
        self.assertEqual(
            list(fact.as_dict()),
            ["id", "value", "unit", "scope", "window", "source", "basis"],
        )

    def test_duplicate_id_is_refused(self):
        fs = FactSet()
        common = dict(
            value=1, unit="count", scope="X", window="n/a",
            source="s.json", basis="n/a",
        )
        fs.add(id="a", **common)
        with self.assertRaises(DuplicateFactError):
            fs.add(id="a", **common)

    def test_write_load_round_trip_keeps_every_field_and_null_values(self):
        fs = FactSet(meta={"skill": "audit-ad-copy"})
        fs.add(
            id="rsa.servable.count", value=59, unit="count", scope="X",
            window="n/a", source="s.json", basis="n/a",
        )
        fs.add(
            id="rsa.spend.servable.total", value=Decimal("48213.904417"),
            unit="currency_cad", scope="X", window=WINDOW_30D,
            source="m.json", basis="n/a",
        )
        # Same scope as rsa.servable.count on purpose, so the null check is
        # what fires below rather than the scope guard.
        fs.add(
            id="rsa.filter_level.ad_enabled_only.count", value=None,
            unit="count", scope="X", window="n/a",
            source="s.json", basis="n/a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.json"
            fs.write(path)
            back = FactSet.load(path)
            self.assertEqual(len(back), 3)
            self.assertEqual(back.meta["skill"], "audit-ad-copy")
            self.assertEqual(back.value("rsa.servable.count"), 59)
            self.assertIsNone(back.value("rsa.filter_level.ad_enabled_only.count"))
            self.assertEqual(
                Decimal(str(back.value("rsa.spend.servable.total"))),
                Decimal("48213.904417"),
            )
            # A null fact still refuses arithmetic after a round trip.
            with self.assertRaises(NullFactValueError):
                back.compare(
                    "rsa.servable.count", "rsa.filter_level.ad_enabled_only.count"
                )


# ==========================================================================
# Regressions ported from the removed value-search checker (V2 Wave 2)
# ==========================================================================


class SoleCheckerPortedRegressionTests(unittest.TestCase):
    """Scenarios the removed `facts.verify_report` used to cover.

    The value-search checker was removed 2026-08-27 (it matched by
    coincidence; see citations.py's docstring). Every scenario below still
    describes a real risk, so each is re-expressed against the surviving
    checker. Scenarios NOT ported, with reasons: the skip-list corpus
    (duplicated by the masker corpus tests below) and percent-matches-either-
    way-round (that guess IS what was removed; the unit-scale tests here are
    its replacement).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        fs = FactSet()
        fs.add(
            id="rsa.servable.count", value=59, unit="count",
            scope=builder.VF5_FULL_FILTER, window="n/a",
            source="inv.json", basis="n/a",
        )
        fs.add(
            id="rsa.spend.servable.total", value=Decimal("48213.904417"),
            unit="currency_cad", scope=builder.VF5_FULL_FILTER,
            window=WINDOW_30D, source="met.json", basis="n/a",
        )
        fs.add(
            id="brand.search_impression_share", value=0.6954, unit="ratio",
            scope="campaign.name='Brand Core'", window=WINDOW_30D,
            source="camp.json", basis="n/a",
        )
        self.facts_path = self.dir / "facts.json"
        fs.write(self.facts_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _check(self, markdown: str):
        report = self.dir / "report.md"
        report.write_text(markdown, encoding="utf-8")
        return citations.check(str(report), str(self.facts_path))

    def test_a_cited_report_passes(self):
        out = self._check(
            "# Audit 2026-08-19\n\n"
            "59 servable RSAs [fact: rsa.servable.count] spent $48,213.90 "
            "[fact: rsa.spend.servable.total] in the window.\n\n"
            "Brand Core impression share 69.54% "
            "[fact: brand.search_impression_share].\n"
        )
        self.assertEqual([str(p) for p in out], [])

    def test_the_aug10_style_stale_figure_is_flagged(self):
        """A figure carried over from a prior run has no fact behind it.

        Under the old checker it traced to nothing; under this one it is
        uncited, and citing it would be a value_mismatch. Same protection,
        stricter form.
        """
        out = self._check("Keyword insertion spend grew to $49,210.77.\n")
        self.assertEqual([p.kind for p in out], ["uncited"])
        self.assertIn("49,210.77", out[0].detail)

    def test_rounded_printing_of_a_cited_full_precision_fact_passes(self):
        for shown in ("$48,213.90", "$48,213.904", "$48,213.904417",
                      "48,213.90"):
            with self.subTest(shown=shown):
                out = self._check(
                    f"Spend was {shown} [fact: rsa.spend.servable.total].\n"
                )
                self.assertEqual([str(p) for p in out], [])

    def test_a_number_hyphenated_into_a_word_is_not_a_figure(self):
        out = self._check(
            "The 30-day window, the 8-action basis and a 14-day learning "
            "window are words, not figures.\n"
        )
        self.assertEqual([str(p) for p in out], [])
        # But digits-hyphen-digits is still two figures, not a word.
        out = self._check("A 60-40 split.\n")
        self.assertEqual([p.kind for p in out], ["uncited"])
        self.assertIn("60", out[0].detail)

    def test_percent_scale_is_read_off_the_unit_never_guessed(self):
        """The replacement for either-way-round matching.

        The old checker accepted 69.54% against a fact holding 0.6954 OR
        69.54, doubling every percentage's matching surface — the 2026-08-19
        coincidence. Now the fact's unit declares the scale, and a wrong
        percentage is named against the fact's own scale.
        """
        ok = self._check(
            "Share was 69.54% [fact: brand.search_impression_share].\n"
        )
        self.assertEqual([str(p) for p in ok], [])
        plain = self._check(
            "Share was 0.6954 [fact: brand.search_impression_share].\n"
        )
        self.assertEqual([str(p) for p in plain], [])
        wrong = self._check(
            "Share was 71.2% [fact: brand.search_impression_share].\n"
        )
        self.assertEqual([p.kind for p in wrong], ["value_mismatch"])

    def test_null_facts_never_back_a_figure(self):
        fs = FactSet.load(self.facts_path)
        fs.add(
            id="unavailable", value=None, unit="count",
            scope="UNAVAILABLE: ...", window="n/a", source="s.json", basis="n/a",
        )
        fs.write(self.facts_path)
        cited = self._check("There were 0 of them [fact: unavailable].\n")
        # Two problems, both right: the null fact is named as unusable, and
        # the figure is left with no usable citation to vouch for it.
        self.assertEqual([p.kind for p in cited], ["value_mismatch", "uncited"])
        self.assertIn("null fact", cited[0].detail)
        uncited = self._check("There were 0 of them.\n")
        self.assertEqual([p.kind for p in uncited], ["uncited"])


# ==========================================================================
# The builder, against the real Aug-10 raw pull
# ==========================================================================




# ==========================================================================
# The builder, on synthetic raw dirs
# ==========================================================================


def write_pull(path: Path, field_mask: str, results: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "action": "search",
                "path": "/v22/customers/1234567890/googleAds:search",
                "resource": "customers_google_ads",
                "status": 200,
                "success": True,
                "data": {"fieldMask": field_mask, "results": results},
            }
        ),
        encoding="utf-8",
    )


def ad_row(ad_group_id: str, ad_id: str, *, ag_status: str | None = "ENABLED",
           ad_status: str = "ENABLED") -> dict:
    """One servable RSA with live headlines "A" and "B"."""
    row = {
        "campaign": {
            "id": "72000000007",
            "name": "Search - Generic - Core",
            "resourceName": "customers/1/campaigns/72000000007",
            "advertisingChannelType": "SEARCH",
        },
        "adGroup": {
            "id": ad_group_id,
            "name": f"AG {ad_group_id}",
            "resourceName": f"customers/1/adGroups/{ad_group_id}",
        },
        "adGroupAd": {
            "resourceName": f"customers/1/adGroupAds/{ad_group_id}~{ad_id}",
            "status": ad_status,
            "adStrength": "GOOD",
            "ad": {
                "id": ad_id,
                "responsiveSearchAd": {
                    "headlines": [
                        {"text": "A", "assetPerformanceLabel": "PENDING"},
                        {"text": "B", "assetPerformanceLabel": "GOOD"},
                    ],
                    "descriptions": [{"text": "D", "assetPerformanceLabel": "BEST"}],
                },
            },
            "policySummary": {"approvalStatus": "APPROVED"},
        },
    }
    if ag_status is not None:
        row["adGroup"]["status"] = ag_status
    return row


def metric_row(ad_group_id: str, ad_id: str, micros: int, impressions: int = 100) -> dict:
    return {
        "campaign": {"resourceName": "customers/1/campaigns/72000000007"},
        "adGroup": {"resourceName": f"customers/1/adGroups/{ad_group_id}"},
        "adGroupAd": {
            "resourceName": f"customers/1/adGroupAds/{ad_group_id}~{ad_id}",
            "ad": {"id": ad_id},
        },
        "metrics": {
            "costMicros": str(micros),
            "clicks": "10",
            "impressions": str(impressions),
        },
    }


def conversion_row(ad_group_id: str, ad_id: str) -> dict:
    return {
        "campaign": {"resourceName": "customers/1/campaigns/72000000007"},
        "adGroup": {"resourceName": f"customers/1/adGroups/{ad_group_id}"},
        "adGroupAd": {
            "resourceName": f"customers/1/adGroupAds/{ad_group_id}~{ad_id}",
            "ad": {"id": ad_id},
        },
        "segments": {"conversionActionName": "Pro Trial Signup"},
        "metrics": {"conversions": 2.5, "conversionsValue": 2625},
    }


def asset_view_row(ad_group_id: str, ad_id: str, text: str, impressions: int,
                   clicks: int, label: str, asset_id: str) -> dict:
    """One `ad_group_ad_asset_view` row, carrying the full VF-4a four-part key."""
    return {
        "campaign": {"id": "72000000007"},
        "adGroup": {"id": ad_group_id},
        "adGroupAd": {
            "resourceName": f"customers/1/adGroupAds/{ad_group_id}~{ad_id}",
            "ad": {"id": ad_id},
        },
        "adGroupAdAssetView": {
            "resourceName": (
                f"customers/1/adGroupAdAssetViews/{ad_group_id}~{ad_id}~"
                f"{asset_id}~HEADLINE"
            ),
            "fieldType": "HEADLINE",
            "performanceLabel": label,
        },
        "asset": {"id": asset_id, "textAsset": {"text": text}},
        "metrics": {"impressions": str(impressions), "clicks": str(clicks)},
    }


class SyntheticRawDir:
    """A minimal, valid raw dir under a temp path named like a real one."""

    #: The manifest records **executable GAQL**, verbatim - quoted literals and
    #: `ad_group_ad.ad.type` - because that is the audit trail (VF-19a). The
    #: fixture uses that spelling on purpose, so the clause-for-clause check has
    #: something real to normalise rather than comparing a label to itself.
    INVENTORY_GAQL = (
        "ad_group_ad.status='ENABLED' AND ad_group.status='ENABLED' "
        "AND campaign.status='ENABLED' "
        "AND ad_group_ad.ad.type='RESPONSIVE_SEARCH_AD' "
        "AND campaign.advertising_channel_type='SEARCH'"
    )

    def __init__(self, inv_rows, met_rows, conv_rows=None, asset_rows=None,
                 ag_status_selected=True, manifest_rows=None, pulled="2026-08-19",
                 inv_filter=None, inv_temporality=None, extra_files=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / pulled
        self.path.mkdir()
        inv_mask = (
            "campaign.id,campaign.advertisingChannelType,adGroup.id,"
            + ("adGroup.status," if ag_status_selected else "")
            + "adGroupAd.ad.id,adGroupAd.status,adGroupAd.adStrength,"
            "adGroupAd.ad.responsiveSearchAd.headlines"
        )
        write_pull(self.path / builder.INVENTORY, inv_mask, inv_rows)
        write_pull(
            self.path / builder.METRICS,
            "adGroupAd.ad.id,metrics.impressions,metrics.clicks,metrics.costMicros",
            met_rows,
        )
        write_pull(
            self.path / builder.CONVERSIONS,
            "adGroupAd.ad.id,segments.conversionActionName,metrics.conversions",
            conv_rows if conv_rows is not None else [conversion_row("1", "10")],
        )
        if asset_rows is not None:
            write_pull(
                self.path / builder.ASSET_VIEW,
                "adGroup.id,adGroupAd.ad.id,adGroupAdAssetView.fieldType,"
                "adGroupAdAssetView.performanceLabel,asset.id,"
                "asset.textAsset.text,metrics.impressions,metrics.clicks",
                asset_rows,
            )
        if manifest_rows is not None:
            # Temporality is declared by the pulling step, never inferred
            # (VF-17 rule 5). `manifest_rows` overrides a declared row count so
            # the disagreement path can be tested.
            declared = {
                builder.INVENTORY: {
                    "rows": len(inv_rows),
                    "temporality": inv_temporality or f"current-as-of-{pulled}",
                    "filter": inv_filter or self.INVENTORY_GAQL,
                },
                builder.METRICS: {
                    "rows": len(met_rows),
                    "temporality": "window:LAST_30_DAYS",
                    "filter": self.INVENTORY_GAQL
                    + " AND segments.date DURING LAST_30_DAYS",
                },
            }
            for name, rows in (manifest_rows or {}).items():
                declared.setdefault(name, {})["rows"] = rows
            (self.path / S.MANIFEST).write_text(
                json.dumps({"pulled": pulled, "files": declared}), encoding="utf-8"
            )
        for name, body in (extra_files or {}).items():
            (self.path / name).write_text(body, encoding="utf-8")

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        self._tmp.cleanup()


class BuilderValidationTests(unittest.TestCase):
    def test_a_stale_declared_filter_aborts_the_build(self):
        """A PAUSED ad in a pull declared as ENABLED-only must not be counted."""
        rows = [ad_row("1", "10"), ad_row("2", "20", ad_status="PAUSED")]
        with SyntheticRawDir(rows, [metric_row("1", "10", 1_000_000),
                                    metric_row("2", "20", 0)]) as raw:
            with self.assertRaises(builder.BuildError) as ctx:
                builder.build(raw)
            msg = str(ctx.exception)
            self.assertIn("ad_group_ad.status = 'ENABLED'", msg)
            self.assertIn("is FALSE for 1 of 2 rows", msg)

    def test_a_duplicated_join_key_aborts_the_build(self):
        rows = [ad_row("1", "10"), ad_row("1", "10")]
        with SyntheticRawDir(rows, [metric_row("1", "10", 1_000_000)]) as raw:
            with self.assertRaises(builder.BuildError) as ctx:
                builder.build(raw)
            self.assertIn("duplicate adGroupAd.resourceName", str(ctx.exception))

    def test_metrics_rows_with_no_inventory_row_abort_the_build(self):
        with SyntheticRawDir(
            [ad_row("1", "10")],
            [metric_row("1", "10", 1_000_000), metric_row("9", "99", 5_000_000)],
        ) as raw:
            with self.assertRaises(builder.BuildError) as ctx:
                builder.build(raw)
            self.assertIn("no matching row", str(ctx.exception))

    def test_one_ad_id_across_two_ad_groups_is_not_collapsed(self):
        """Google reuses ad ids across ad groups; the join key is the resource name."""
        rows = [ad_row("1", "10"), ad_row("2", "10")]
        mets = [metric_row("1", "10", 1_000_000), metric_row("2", "10", 3_000_000)]
        with SyntheticRawDir(rows, mets) as raw:
            fs = builder.build(raw)
            self.assertEqual(fs.value("rsa.servable.count"), 2)
            self.assertEqual(
                Decimal(str(fs.value("rsa.spend.servable.total"))), Decimal("4")
            )

    def test_a_conversion_action_outside_the_eight_aborts_the_build(self):
        conv = [conversion_row("1", "10")]
        conv[0]["segments"]["conversionActionName"] = "CTA Clicked"
        with SyntheticRawDir(
            [ad_row("1", "10")], [metric_row("1", "10", 0)], conv_rows=conv
        ) as raw:
            with self.assertRaises(builder.BuildError) as ctx:
                builder.build(raw)
            self.assertIn("outside the VF-1 eight", str(ctx.exception))

    def test_spend_splits_by_ad_group_status_when_the_pull_did_not_filter_it(self):
        rows = [ad_row("1", "10"), ad_row("2", "20", ag_status="PAUSED")]
        mets = [metric_row("1", "10", 2_000_000), metric_row("2", "20", 5_000_000)]
        with SyntheticRawDir(rows, mets) as raw:
            fs = builder.build(raw)
            self.assertEqual(fs.value("rsa.servable.count"), 1)
            self.assertEqual(fs.value("rsa.unfiltered_ad_group_status.count"), 2)
            self.assertEqual(
                Decimal(str(fs.value("rsa.spend.servable.total"))), Decimal("2")
            )
            self.assertEqual(
                Decimal(str(fs.value("rsa.spend.unfiltered_ad_group_status.total"))),
                Decimal("7"),
            )
            self.assertEqual(
                Decimal(str(
                    fs.value("rsa.spend.in_paused_or_removed_ad_group.total")
                )),
                Decimal("5"),
            )

    def test_a_corrected_pull_that_omits_ad_group_status_nulls_the_bug_scope(self):
        """The count must not be relabelled as 'ad_group.status NOT FILTERED'."""
        rows = [ad_row("1", "10", ag_status=None), ad_row("2", "20", ag_status=None)]
        mets = [metric_row("1", "10", 1_000_000), metric_row("2", "20", 1_000_000)]
        with SyntheticRawDir(rows, mets, ag_status_selected=False) as raw:
            fs = builder.build(raw)
            self.assertEqual(fs.value("rsa.servable.count"), 2)
            self.assertIsNone(fs.value("rsa.unfiltered_ad_group_status.count"))
            self.assertIsNone(fs.value("rsa.in_paused_or_removed_ad_group.count"))
            self.assertIn("DECLARED", fs.get("rsa.servable.count").scope)

    def test_a_pull_with_all_enabled_ad_groups_nulls_the_bug_scope(self):
        rows = [ad_row("1", "10"), ad_row("2", "20")]
        mets = [metric_row("1", "10", 1_000_000), metric_row("2", "20", 1_000_000)]
        with SyntheticRawDir(rows, mets) as raw:
            fs = builder.build(raw)
            self.assertEqual(fs.value("rsa.servable.count"), 2)
            self.assertIsNone(fs.value("rsa.unfiltered_ad_group_status.count"))
            self.assertIn(
                "indistinguishable",
                fs.get("rsa.unfiltered_ad_group_status.count").scope,
            )


class BuilderAssetViewFixtureTests(unittest.TestCase):
    """The ranking path on a small fixture, including the crash that VF-4a names.

    Ad 620000000001 really is linked to five ad groups in this account, and the
    first version of this harness keyed asset facts on `ad.id` alone and died
    with DuplicateFactError. Two of its real ad groups are used here.
    """

    AD = "620000000001"
    AG_A = "620000000008"
    AG_B = "620000000009"

    def setUp(self):
        inv = [ad_row(self.AG_A, self.AD), ad_row(self.AG_B, self.AD)]
        mets = [
            metric_row(self.AG_A, self.AD, 7_757_000_000, impressions=4000),
            metric_row(self.AG_B, self.AD, 1_000_000, impressions=50),
        ]
        # ad_row gives each ad the live headlines "A" and "B".
        assets = [
            asset_view_row(self.AG_A, self.AD, "A", 2658, 40, "GOOD", "111"),
            asset_view_row(self.AG_A, self.AD, "B", 0, 0, "PENDING", "222"),
            # Served during the window, no longer on the ad. History only.
            asset_view_row(
                self.AG_A, self.AD, "Secure & GDRP - Compliant", 524, 3, "LOW", "333"
            ),
            asset_view_row(self.AG_B, self.AD, "A", 90, 1, "LEARNING", "111"),
            asset_view_row(self.AG_B, self.AD, "B", 10, 0, "PENDING", "222"),
        ]
        self.raw = SyntheticRawDir(inv, mets, asset_rows=assets)
        self.fs = builder.build(self.raw.path)

    def tearDown(self):
        self.raw.__exit__()

    def _p(self, ag):
        return f"asset_view.adgroup.{ag}.ad.{self.AD}.headline"

    def test_one_ad_id_in_two_ad_groups_no_longer_collides(self):
        """The DuplicateFactError regression. Both ad groups get their own facts."""
        self.assertEqual(self.fs.value(f"{self._p(self.AG_A)}.rank_1.impressions"), 2658)
        self.assertEqual(self.fs.value(f"{self._p(self.AG_B)}.rank_1.impressions"), 90)
        self.assertEqual(self.fs.value("asset_view.ranked.ad.count"), 2)

    def test_no_asset_fact_id_is_keyed_on_ad_id_alone(self):
        """VF-4a. Every per-ad asset fact names the ad group as well as the ad."""
        good = re.compile(r"^asset_view\.adgroup\.(?P<ag>\d+)\.ad\.(?P<ad>\d+)\.")
        old_broken_shape = re.compile(r"^asset_view\.ad\.\d+\.")
        seen = 0
        for fact in self.fs:
            self.assertNotRegex(fact.id, old_broken_shape)
            m = good.match(fact.id)
            if not m:
                continue
            seen += 1
            self.assertIn(f"ad_group.id={m.group('ag')}", fact.scope)
            self.assertIn(f"ad_group_ad.ad.id={m.group('ad')}", fact.scope)
        self.assertGreater(seen, 0)

    def test_the_scope_names_both_ids_and_says_within_ad_only(self):
        scope = self.fs.get(f"{self._p(self.AG_A)}.rank_1.impressions").scope
        self.assertIn(f"ad_group.id={self.AG_A}", scope)
        self.assertIn(f"ad_group_ad.ad.id={self.AD}", scope)
        self.assertIn("ONLY within this one ad in this one ad group", scope)

    def test_a_removed_asset_is_history_never_a_rank(self):
        """VF-4b and VF-17. The GDRP headline must not be rankable."""
        prefix = self._p(self.AG_A)
        self.assertEqual(self.fs.value(f"{prefix}.removed_but_served.count"), 1)
        self.assertEqual(
            self.fs.value(f"{prefix}.removed_but_served.1.text"),
            "Secure & GDRP - Compliant",
        )
        self.assertEqual(
            self.fs.value(f"{prefix}.removed_but_served.1.impressions"), 524
        )
        history = self.fs.get(f"{prefix}.removed_but_served.1.text")
        self.assertIn("HISTORY ONLY", history.scope)
        self.assertIn("never be presented as a live problem", history.scope)
        # It appears in no rank fact anywhere in the file.
        for fact in self.fs:
            if ".rank_" in fact.id and fact.unit == "text":
                self.assertNotEqual(fact.value, "Secure & GDRP - Compliant")

    def test_a_live_headline_with_zero_serves_is_a_dead_slot_not_a_rank(self):
        prefix = self._p(self.AG_A)
        self.assertEqual(self.fs.value(f"{prefix}.live.count"), 2)
        self.assertEqual(self.fs.value(f"{prefix}.serve_row.count"), 3)
        self.assertEqual(self.fs.value(f"{prefix}.ranked.count"), 1)
        self.assertEqual(self.fs.value(f"{prefix}.dead_slot.count"), 1)
        self.assertEqual(self.fs.value(f"{prefix}.dead_slot.1.text"), "B")
        self.assertIn("DEAD SLOT", self.fs.get(f"{prefix}.dead_slot.1.text").scope)

    def test_serve_credits_are_a_within_ad_denominator_not_impressions(self):
        prefix = self._p(self.AG_B)
        credits = self.fs.get(f"{prefix}.serve_credits.total")
        self.assertEqual(credits.value, 100)
        self.assertEqual(credits.unit, "serve_credits_within_ad")
        self.assertIn("NOT this ad's impressions", credits.scope)
        with self.assertRaises(NonAdditiveFactError):
            self.fs.sum([f"{prefix}.serve_credits.total"])
        # The ad's real impressions sit beside it, and cannot be related to it.
        real = self.fs.get(f"asset_view.adgroup.{self.AG_B}.ad.{self.AD}.real_impressions")
        self.assertEqual(real.value, 50)
        self.assertEqual(real.unit, "impressions")
        self.assertLess(real.value, credits.value)
        with self.assertRaises(FactMismatchError):
            self.fs.compare(
                f"{prefix}.serve_credits.total",
                f"asset_view.adgroup.{self.AG_B}.ad.{self.AD}.real_impressions",
            )

    #: `asset_view.*` facts whose value comes from a pull other than the asset
    #: view, so `broad-goal-set` would be the wrong basis for them. The two live
    #: slot counts are read entirely from the undated inventory pull, and
    #: `real_impressions` from the ad-level metrics pull.
    NOT_FROM_THE_ASSET_PULL = (
        "real_impressions",
        ".live.count",
        "asset_view.live_headline.slot.count",
    )

    def test_asset_facts_carry_the_broad_goal_set_basis(self):
        for fact in self.fs:
            if not fact.id.startswith("asset_view."):
                continue
            if any(marker in fact.id for marker in self.NOT_FROM_THE_ASSET_PULL):
                continue
            self.assertEqual(fact.basis, "broad-goal-set", fact.id)

    def test_live_slot_counts_are_labelled_current_state_not_windowed(self):
        """VF-17 rule 4. A count of what is on the ad now is not a 30-day figure.

        These two facts are computed entirely from the undated inventory pull -
        the asset view contributes nothing to either value - and they used to
        carry the asset pull's 30-day window and filename. A scope reading "from
        the UNDATED inventory pull" beside a date-range window is
        self-contradictory, and it let the guard compare a current-state count
        against a windowed metric.
        """
        inventory_window = self.fs.get("rsa.servable.count").window
        asset_window = self.fs.get("asset_view.serve_row.count").window
        self.assertNotEqual(inventory_window, asset_window)
        for fact_id in (
            "asset_view.live_headline.slot.count",
            f"{self._p(self.AG_B)}.live.count",
        ):
            fact = self.fs.get(fact_id)
            self.assertEqual(fact.window, inventory_window, fact_id)
            self.assertEqual(fact.source, S.INVENTORY, fact_id)
            self.assertEqual(fact.basis, "n/a", fact_id)
            self.assertIn("UNDATED", fact.scope)

        # And the guard now refuses the subtraction that looks so inviting.
        with self.assertRaises(FactMismatchError):
            self.fs.compare(
                "asset_view.live_headline.slot.count", "asset_view.dead_slot.total"
            )

    def test_summing_asset_impressions_is_refused(self):
        prefix = self._p(self.AG_B)
        with self.assertRaises(NonAdditiveFactError):
            self.fs.sum(
                [f"{prefix}.rank_1.impressions", f"{prefix}.rank_2.impressions"]
            )

    def test_a_rank_in_one_ad_group_cannot_be_compared_to_the_other(self):
        """VF-4 rule 2. Same ad id, different ad group, different scope."""
        with self.assertRaises(FactMismatchError) as ctx:
            self.fs.compare(
                f"{self._p(self.AG_A)}.rank_1.impressions",
                f"{self._p(self.AG_B)}.rank_1.impressions",
            )
        self.assertIn("scope mismatch", str(ctx.exception))


class AssetJoinSafetyTests(unittest.TestCase):
    def test_duplicate_headline_text_in_one_ad_aborts_the_build(self):
        """The text join is only sound while text is unique inside one ad."""
        inv = [ad_row("1", "10")]
        mets = [metric_row("1", "10", 1_000_000)]
        assets = [
            asset_view_row("1", "10", "A", 100, 1, "GOOD", "111"),
            asset_view_row("1", "10", "A", 50, 0, "GOOD", "222"),
        ]
        with SyntheticRawDir(inv, mets, asset_rows=assets) as raw:
            with self.assertRaises(builder.BuildError) as ctx:
                builder.build(raw)
            msg = str(ctx.exception)
            self.assertIn("appears on 2 distinct asset IDs", msg)
            self.assertIn("ambiguous", msg)

    def test_a_duplicate_four_part_key_aborts_the_build(self):
        inv = [ad_row("1", "10")]
        mets = [metric_row("1", "10", 1_000_000)]
        assets = [
            asset_view_row("1", "10", "A", 100, 1, "GOOD", "111"),
            asset_view_row("1", "10", "A", 100, 1, "GOOD", "111"),
        ]
        with SyntheticRawDir(inv, mets, asset_rows=assets) as raw:
            with self.assertRaises(builder.BuildError) as ctx:
                builder.build(raw)
            self.assertIn("duplicate (ad_group.id, ad.id, asset.id", str(ctx.exception))


class ManifestTests(unittest.TestCase):
    def test_a_row_count_that_disagrees_with_the_manifest_aborts(self):
        inv = [ad_row("1", "10")]
        mets = [metric_row("1", "10", 1_000_000)]
        with SyntheticRawDir(inv, mets, manifest_rows={builder.INVENTORY: 99}) as raw:
            with self.assertRaises(S.ManifestError) as ctx:
                builder.build(raw)
            self.assertIn("declares 99 rows but the file holds 1", str(ctx.exception))

    def test_a_manifest_declares_current_state_and_windowed_temporality(self):
        inv = [ad_row("1", "10")]
        mets = [metric_row("1", "10", 1_000_000)]
        with SyntheticRawDir(inv, mets, manifest_rows={}) as raw:
            fs = builder.build(raw)
            self.assertEqual(
                fs.get("rsa.servable.count").window, "current-as-of-2026-08-19"
            )
            self.assertIn(
                "declared window:LAST_30_DAYS",
                fs.get("rsa.spend.servable.total").window,
            )

    def test_without_a_manifest_temporality_is_declared_undeclared(self):
        """VF-17 rule 5: never inferred. Say it is undeclared instead."""
        inv = [ad_row("1", "10")]
        mets = [metric_row("1", "10", 1_000_000)]
        with SyntheticRawDir(inv, mets) as raw:
            fs = builder.build(raw)
            inv_window = fs.get("rsa.servable.count").window
            self.assertIn("temporality NOT declared", inv_window)
            self.assertIn("VF-17", inv_window)
            self.assertFalse(F.is_current_state_window(inv_window))
            # A windowed pull still states its resolved date range.
            met_window = fs.get("rsa.spend.servable.total").window
            self.assertIn("2026-07-20..2026-08-18", met_window)
            self.assertIn("temporality NOT declared", met_window)


class Vf17GuardTests(unittest.TestCase):
    def test_current_state_windows_have_the_required_shape(self):
        self.assertEqual(F.current_state_window("2026-08-19"), "current-as-of-2026-08-19")
        self.assertTrue(F.is_current_state_window("current-as-of-2026-08-19"))
        self.assertFalse(F.is_current_state_window("2026-07-20..2026-08-18"))
        self.assertFalse(F.is_current_state_window("n/a"))
        with self.assertRaises(FactsError):
            F.current_state_window("last week")

    def test_a_windowed_metric_cannot_be_compared_to_a_current_state_fact(self):
        fs = FactSet()
        common = dict(unit="count", scope="same scope", source="s.json", basis="n/a")
        fs.add(id="now", value=61, window=F.current_state_window("2026-08-19"), **common)
        fs.add(id="over_30d", value=68, window="2026-07-20..2026-08-18", **common)
        with self.assertRaises(FactMismatchError) as ctx:
            fs.compare("now", "over_30d")
        msg = str(ctx.exception)
        self.assertIn("window mismatch", msg)
        self.assertIn("cannot answer a current-state question", msg)
        self.assertIn("VF-17", msg)
        self.assertIn("GDRP", msg)





# ==========================================================================
# citations.py — bare integers must name the fact they quote
# ==========================================================================


class CitationTests(unittest.TestCase):
    """Why this checker exists: value matching cannot police small integers.

    A 4,865-fact file contains 163 of the 201 integers between 0 and 200, so a
    wrong count matches something by coincidence. The regression test at the
    bottom of this class is the actual 2026-08-19 failure.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        fs = FactSet()
        common = dict(
            unit="count",
            scope="ad_group_ad_asset_view intersected with current inventory",
            window="2026-07-20..2026-08-18",
            source="asset-serves.json",
            basis="broad-goal-set",
        )
        fs.add(id="asset_view.dead_slot.total", value=126, **common)
        fs.add(id="asset_view.dead_slot.ads_affected", value=21, **common)
        fs.add(
            id="rsa.servable.count",
            value=61,
            unit="count",
            scope=builder.VF5_FULL_FILTER,
            window="current-as-of-2026-08-19",
            source="inventory.json",
            basis="n/a",
        )
        fs.add(
            id="rsa.spend.servable.total",
            value=Decimal("48213.904417"),
            unit="currency_cad",
            scope=builder.VF5_FULL_FILTER,
            window="2026-07-20..2026-08-18",
            source="metrics.json",
            basis="n/a",
        )
        fs.add(
            id="rsa.filter_level.ad_enabled_only.count",
            value=None,
            unit="count",
            scope="UNAVAILABLE: never pulled",
            window="n/a",
            source="inventory.json",
            basis="n/a",
        )
        fs.add(
            id="asset_view.adgroup.1.ad.2.headline.rank_1.text",
            value="AI Invoicing for Freelancers",
            unit="text",
            scope="within one ad",
            window="2026-07-20..2026-08-18",
            source="asset-serves.json",
            basis="broad-goal-set",
        )
        self.facts_path = self.dir / "facts.json"
        fs.write(self.facts_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _check(self, markdown: str):
        report = self.dir / "report.md"
        report.write_text(markdown, encoding="utf-8")
        return citations.check(str(report), str(self.facts_path))

    # -- the happy path ----------------------------------------------------

    def test_a_correct_citation_passes(self):
        out = self._check(
            "We found 126 dead headline slots "
            "[fact: asset_view.dead_slot.total].\n"
        )
        self.assertEqual([str(p) for p in out], [])

    def test_two_integers_on_one_line_need_two_citations(self):
        good = self._check(
            "126 dead slots [fact: asset_view.dead_slot.total] across 21 ads "
            "[fact: asset_view.dead_slot.ads_affected].\n"
        )
        self.assertEqual([str(p) for p in good], [])

    def test_money_and_percentages_are_no_longer_left_to_the_value_matcher(self):
        """The contract this test used to assert was falsified on 2026-08-19.

        It read `test_money_and_percentages_are_left_to_the_value_matcher` and
        asserted that this line passed clean. Value matching turned out to be a
        property of a small facts file, not of percentages: two fabricated share
        percentages coincided with real values on unrelated ads and passed both
        checkers. Money and percentages are citable claims now.
        """
        out = self._check("Spend was $48,213.90 and share 69.54%.\n")
        self.assertEqual([p.kind for p in out], ["uncited"])
        self.assertIn("48,213.90", out[0].detail)
        self.assertIn("69.54%", out[0].detail)

    # -- the three problem kinds -------------------------------------------

    def test_an_uncited_integer_is_flagged(self):
        out = self._check("There were 61 servable RSAs.\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "uncited")
        self.assertEqual(out[0].line, 1)
        self.assertIn("61", out[0].detail)

    def test_a_cited_id_that_does_not_exist_is_flagged(self):
        out = self._check("There were 61 servable RSAs [fact: rsa.servable.kount].\n")
        kinds = [p.kind for p in out]
        self.assertIn("unknown_id", kinds)
        unknown = next(p for p in out if p.kind == "unknown_id")
        self.assertIn("rsa.servable.kount", unknown.detail)
        self.assertIn("not in the facts file", unknown.detail)

    def test_a_cited_id_whose_value_disagrees_shows_both_values(self):
        out = self._check(
            "There were 99 servable RSAs [fact: rsa.servable.count].\n"
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "value_mismatch")
        self.assertIn("99", out[0].detail)
        self.assertIn("61", out[0].detail)
        self.assertIn("rsa.servable.count", out[0].detail)

    def test_a_null_fact_cannot_support_a_stated_number(self):
        out = self._check(
            "There were 223 RSAs at that filter level "
            "[fact: rsa.filter_level.ad_enabled_only.count].\n"
        )
        kinds = [p.kind for p in out]
        self.assertIn("value_mismatch", kinds)
        self.assertTrue(
            any("null fact" in p.detail for p in out),
            [p.detail for p in out],
        )

    def test_a_text_fact_cannot_support_a_stated_number(self):
        out = self._check(
            "It served 2658 times "
            "[fact: asset_view.adgroup.1.ad.2.headline.rank_1.text].\n"
        )
        self.assertTrue(any("not a number" in p.detail for p in out), out)

    # -- the maskers -------------------------------------------------------

    def test_dates_times_ordinals_years_and_object_ids_are_not_claims(self):
        for line in (
            "The owner removed it on Aug 14.",
            "The owner removed it on 14 August.",
            "Removed 2026-08-14 per the change log.",
            "Removed at 14:56 and again at 14:58.",
            "It went out on the 14th.",
            "Verified live in 2026.",
            "Ad 620000000010 in ad group 620000000011.",
        ):
            with self.subTest(line=line):
                self.assertEqual([str(p) for p in self._check(line + "\n")], [])

    def test_a_real_claim_beside_a_date_is_still_caught(self):
        out = self._check("On Aug 14 the owner removed 6 headlines.\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "uncited")
        self.assertIn("6", out[0].detail)

    def test_code_spans_and_fenced_blocks_are_skipped(self):
        out = self._check(
            "The filter was `ad_group.status = 5` and nothing else.\n"
            "```\nSELECT x FROM y WHERE metrics.clicks > 4242\n```\n"
        )
        self.assertEqual([str(p) for p in out], [])

    # -- wrapping and tables -----------------------------------------------

    def test_a_wrapped_sentence_keeps_its_citation(self):
        """Reports are hard-wrapped. Line-based checking flagged both of these."""
        out = self._check(
            "The account runs 61 servable RSAs [fact: rsa.servable.count] and\n"
            "126 dead slots [fact: asset_view.dead_slot.total] were found.\n"
        )
        self.assertEqual([str(p) for p in out], [])

    def test_a_blank_line_ends_a_block(self):
        out = self._check(
            "126 dead slots [fact: asset_view.dead_slot.total].\n"
            "\n"
            "There were 61 of them.\n"
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "uncited")
        self.assertEqual(out[0].line, 3)

    def test_each_table_row_is_checked_on_its_own(self):
        """A wrong cell must not borrow the row above's citation."""
        out = self._check(
            "| Figure | Value |\n"
            "| :- | -: |\n"
            "| Dead slots | 126 [fact: asset_view.dead_slot.total] |\n"
            "| Ads affected | 99 [fact: asset_view.dead_slot.total] |\n"
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].line, 4)
        self.assertIn("99", out[0].detail)

    def test_a_correct_table_passes(self):
        out = self._check(
            "| Dead slots | 126 [fact: asset_view.dead_slot.total] |\n"
            "| Ads affected | 21 [fact: asset_view.dead_slot.ads_affected] |\n"
        )
        self.assertEqual([str(p) for p in out], [])

    # -- the regression ----------------------------------------------------

    def test_the_99_dead_slots_regression(self):
        """The value matcher passes this; the citation checker must not.

        Reported 2026-08-19: a report claiming 99 dead headline slots against a
        real 126 was not flagged, because some unrelated fact in a 4,865-fact
        file happened to hold the value 99.
        """
        big = FactSet.load(self.facts_path)
        # Stand in for the coincidence: some unrelated fact holds 99.
        big.add(
            id="asset_view.adgroup.9.ad.9.headline.rank_12.impressions",
            value=99,
            unit="impressions_asset_credited",
            scope="within one unrelated ad",
            window="2026-07-20..2026-08-18",
            source="asset-serves.json",
            basis="broad-goal-set",
        )
        big.write(self.facts_path)
        report = self.dir / "report.md"
        report.write_text("We found 99 dead headline slots.\n", encoding="utf-8")

        # (The removed value-search checker was satisfied by the unrelated 99
        # — the bug that got it removed.) The citation checker rejects it,
        # uncited.
        out = citations.check(str(report), str(self.facts_path))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "uncited")

        # And with a citation, the wrong value is named against the real one.
        report.write_text(
            "We found 99 dead headline slots [fact: asset_view.dead_slot.total].\n",
            encoding="utf-8",
        )
        out = citations.check(str(report), str(self.facts_path))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "value_mismatch")
        self.assertIn("99", out[0].detail)
        self.assertIn("126", out[0].detail)

    def test_a_wrong_second_integer_cannot_ride_along_on_a_good_citation(self):
        """The per-integer rule. This passed under the first per-block matcher."""
        out = self._check(
            "126 dead slots across 99 ads [fact: asset_view.dead_slot.total].\n"
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "value_mismatch")
        self.assertIn("99", out[0].detail)





# ==========================================================================
# citations.py — percentages and decimals are citable claims too (VF-15a)
# ==========================================================================


class PercentageCitationTests(unittest.TestCase):
    """The second half of the coincidence problem, demonstrated 2026-08-19.

    VF-15a said value matching "handles percentages reliably". It does not. That
    was a property of a small facts file, not of percentages. The Aug-19 file
    grew to 6,448 rows, 1,504 of them per-rank CTR and share percentages, and a
    report draft's four fabricated share percentages split two and two: value
    matching caught two, and `10.6` and `9.6` coincided with real values on
    unrelated ads and passed both checkers clean.

    So percentages and decimals now carry `[fact: id]` like integers, and the
    scale a fact uses is read off its `unit` rather than guessed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        common = dict(
            scope="ad_group_ad_asset_view keyed per VF-4a, within one ad",
            window="2026-07-20..2026-08-18",
            source="asset-serves.json",
            basis="broad-goal-set",
        )
        fs = FactSet()
        fs.add(
            id="ad_a.bottom_5.share", value=9.2,
            unit="percent_of_ad_serve_credits", **common,
        )
        fs.add(
            id="ad_b.bottom_5.share", value=3.7,
            unit="percent_of_ad_serve_credits", **common,
        )
        fs.add(
            id="ad_a.top_3.share", value=68.2,
            unit="percent_of_ad_serve_credits", **common,
        )
        fs.add(
            id="ad_a.live.count", value=13, unit="count", **common,
        )
        # The two conventions, side by side, on the same underlying share.
        fs.add(
            id="brand.impression_share.ratio", value=0.52, unit="ratio",
            scope="campaign.name='Brand Core'", window="2026-07-20..2026-08-18",
            source="camp.json", basis="n/a",
        )
        fs.add(
            id="brand.impression_share.percent", value=52.0, unit="percent",
            scope="campaign.name='Brand Core'", window="2026-07-20..2026-08-18",
            source="camp.json", basis="n/a",
        )
        # A unit that names the quantity but not its scale. This is the case the
        # checker must refuse instead of trying both.
        fs.add(
            id="brand.impression_share.undeclared", value=0.52, unit="share",
            scope="campaign.name='Brand Core'", window="2026-07-20..2026-08-18",
            source="camp.json", basis="n/a",
        )
        # A unit that names BOTH conventions, so it declares neither.
        fs.add(
            id="brand.impression_share.both", value=0.52, unit="percent_ratio",
            scope="campaign.name='Brand Core'", window="2026-07-20..2026-08-18",
            source="camp.json", basis="n/a",
        )
        fs.add(
            id="rsa.spend.servable.total", value=Decimal("105264.517923"),
            unit="currency_cad", scope=builder.VF5_FULL_FILTER,
            window="2026-07-20..2026-08-18", source="metrics.json", basis="n/a",
        )
        fs.add(
            id="rsa.headlines.per_ad.mean", value=Decimal("126.4"),
            unit="count", scope=builder.VF5_FULL_FILTER,
            window="current-as-of-2026-08-19", source="inv.json", basis="n/a",
        )
        self.facts_path = self.dir / "facts.json"
        fs.write(self.facts_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _check(self, markdown: str):
        report = self.dir / "report.md"
        report.write_text(markdown, encoding="utf-8")
        return citations.check(str(report), str(self.facts_path))

    # -- the happy path ----------------------------------------------------

    def test_a_correctly_cited_percentage_passes(self):
        out = self._check(
            "The bottom 5 headlines take 9.2% of this ad's serve credits "
            "[fact: ad_a.bottom_5.share].\n"
        )
        self.assertEqual([str(p) for p in out], [])

    def test_a_correctly_cited_decimal_passes(self):
        out = self._check(
            "Servable spend over the window was CAD $105,264.52 "
            "[fact: rsa.spend.servable.total].\n"
        )
        self.assertEqual([str(p) for p in out], [])

    def test_an_integer_percentage_is_covered_too(self):
        """`50%` is a different regex branch from `50.0%`. Both are claims."""
        fs = FactSet.load(self.facts_path)
        fs.add(
            id="ad_a.half.share", value=50, unit="percent_of_ad_serve_credits",
            scope="within one ad", window="2026-07-20..2026-08-18",
            source="asset-serves.json", basis="broad-goal-set",
        )
        fs.write(self.facts_path)
        self.assertEqual(
            [str(p) for p in self._check("Half of it, 50% [fact: ad_a.half.share].\n")],
            [],
        )
        bare = self._check("Half of it, 50%.\n")
        self.assertEqual([p.kind for p in bare], ["uncited"])
        self.assertIn("50%", bare[0].detail)

    # -- the failures ------------------------------------------------------

    def test_an_uncited_percentage_is_flagged(self):
        out = self._check("The bottom 5 headlines take 9.2% of serve credits.\n")
        self.assertEqual([p.kind for p in out], ["uncited"])
        self.assertIn("9.2%", out[0].detail)

    def test_an_uncited_decimal_is_flagged(self):
        out = self._check("Servable spend was CAD $105,264.52 in the window.\n")
        self.assertEqual([p.kind for p in out], ["uncited"])
        self.assertIn("105,264.52", out[0].detail)

    def test_a_percentage_citing_a_fact_of_another_value_names_both(self):
        out = self._check(
            "The bottom 5 headlines take 10.6% [fact: ad_a.bottom_5.share].\n"
        )
        self.assertEqual([p.kind for p in out], ["value_mismatch"])
        self.assertIn("10.6%", out[0].detail)
        self.assertIn("9.2", out[0].detail)
        self.assertIn("ad_a.bottom_5.share", out[0].detail)

    # -- percent versus ratio ----------------------------------------------

    def test_a_percent_unit_fact_is_read_on_the_0_to_100_scale(self):
        self.assertEqual(
            [str(p) for p in self._check(
                "Impression share 52.0% [fact: brand.impression_share.percent].\n"
            )],
            [],
        )

    def test_a_ratio_unit_fact_is_read_on_the_0_to_1_scale(self):
        self.assertEqual(
            [str(p) for p in self._check(
                "Impression share 52.0% [fact: brand.impression_share.ratio].\n"
            )],
            [],
        )

    def test_the_two_conventions_are_not_interchangeable(self):
        """Trying both scales is what doubles the coincidence surface."""
        wrong_way = self._check(
            "Impression share 0.52% [fact: brand.impression_share.percent].\n"
        )
        self.assertEqual([p.kind for p in wrong_way], ["value_mismatch"])
        other_way = self._check(
            "Impression share 5200.0% [fact: brand.impression_share.ratio].\n"
        )
        self.assertEqual([p.kind for p in other_way], ["value_mismatch"])

    def test_a_figure_printed_without_a_percent_sign_uses_the_raw_value(self):
        """The mirror rule. Otherwise the guess comes back the other way."""
        self.assertEqual(
            [str(p) for p in self._check(
                "Impression share was 0.52 [fact: brand.impression_share.ratio].\n"
            )],
            [],
        )
        out = self._check(
            "Impression share was 0.52 [fact: brand.impression_share.percent].\n"
        )
        self.assertEqual([p.kind for p in out], ["value_mismatch"])

    def test_an_undeclared_unit_is_refused_out_loud(self):
        out = self._check(
            "Impression share 52.0% [fact: brand.impression_share.undeclared].\n"
        )
        self.assertEqual([p.kind for p in out], ["ambiguous_unit"])
        self.assertIn("'share'", out[0].detail)
        self.assertIn("percent", out[0].detail)
        self.assertIn("ratio", out[0].detail)
        self.assertIn("brand.impression_share.undeclared", out[0].detail)

    def test_a_unit_naming_both_conventions_declares_neither(self):
        out = self._check(
            "Impression share 52.0% [fact: brand.impression_share.both].\n"
        )
        self.assertEqual([p.kind for p in out], ["ambiguous_unit"])
        self.assertIn("percent_ratio", out[0].detail)

    def test_the_convention_table_lives_in_facts_py_only(self):
        """Structural: one definition of the scale, as with the exemptions."""
        self.assertIs(citations.percentage_convention, F.percentage_convention)
        self.assertEqual(
            F.percentage_convention("percent_of_ad_serve_credits"), F.PERCENT_SCALE
        )
        self.assertEqual(F.percentage_convention("ratio"), F.RATIO_SCALE)
        for undeclared in ("share", "rate", "ctr", "count", "", None):
            self.assertIsNone(F.percentage_convention(undeclared), undeclared)

    # -- rounding ----------------------------------------------------------

    def test_a_decimal_may_round_as_printed(self):
        for shown in ("105,264.52", "105,264.518", "105,264.517923"):
            with self.subTest(shown=shown):
                self.assertEqual(
                    [str(p) for p in self._check(
                        f"Spend was CAD ${shown} [fact: rsa.spend.servable.total].\n"
                    )],
                    [],
                )

    def test_an_integer_may_not_round(self):
        """The slack this module exists to remove must not come back.

        A report that prints `126` rounded nothing. Letting it stand for a fact
        holding `126.4` would hand back exactly the coincidence budget the
        citation rule takes away.
        """
        out = self._check(
            "There are 126 headlines [fact: rsa.headlines.per_ad.mean].\n"
        )
        self.assertEqual([p.kind for p in out], ["value_mismatch"])
        self.assertIn("126.4", out[0].detail)

    # -- wrapping ----------------------------------------------------------

    def test_a_wrapped_sentence_whose_citations_landed_on_their_own_line(self):
        """The module docstring promised wrapping support; this broke it.

        A line holding nothing but `[fact: ...]` tokens masks to whitespace, and
        whitespace used to end a claim block, so the sentence above it read as
        uncited. A block ends at structure - a blank line, a fence - never at
        masking.
        """
        out = self._check(
            "The bottom 5 headlines on this ad take 9.2% of its serve credits\n"
            "[fact: ad_a.bottom_5.share]\n"
            "and the top 3 take 68.2%\n"
            "[fact: ad_a.top_3.share].\n"
        )
        self.assertEqual([str(p) for p in out], [])

    def test_a_blank_line_still_ends_a_block(self):
        out = self._check(
            "The bottom 5 take 9.2% [fact: ad_a.bottom_5.share].\n"
            "\n"
            "[fact: ad_a.bottom_5.share]\n"
            "\n"
            "The top 3 take 68.2%.\n"
        )
        self.assertEqual([p.kind for p in out], ["uncited"])
        self.assertEqual(out[0].line, 5)

    def test_a_fence_still_ends_a_block(self):
        out = self._check(
            "The bottom 5 take 9.2% [fact: ad_a.bottom_5.share].\n"
            "```\n"
            "SELECT x FROM y\n"
            "```\n"
            "The top 3 take 68.2%.\n"
        )
        self.assertEqual([p.kind for p in out], ["uncited"])
        self.assertEqual(out[0].line, 5)

    # -- tables ------------------------------------------------------------

    def test_a_table_row_cannot_borrow_another_rows_percentage_citation(self):
        """The demonstrated failure was in table cells, two rows of them."""
        out = self._check(
            "| Ad | Bottom 5 |\n"
            "| :- | -: |\n"
            "| A | 9.2% [fact: ad_a.bottom_5.share] |\n"
            "| B | 9.2% |\n"
        )
        self.assertEqual([p.kind for p in out], ["uncited"])
        self.assertEqual(out[0].line, 4)
        self.assertIn("9.2%", out[0].detail)

    def test_a_table_row_citing_the_wrong_rows_fact_is_flagged(self):
        out = self._check(
            "| A | 9.2% [fact: ad_a.bottom_5.share] |\n"
            "| B | 9.2% [fact: ad_b.bottom_5.share] |\n"
        )
        self.assertEqual([p.kind for p in out], ["value_mismatch"])
        self.assertEqual(out[0].line, 2)
        self.assertIn("9.2%", out[0].detail)
        self.assertIn("3.7", out[0].detail)

    def test_a_correct_two_row_table_passes(self):
        out = self._check(
            "| A | 9.2% [fact: ad_a.bottom_5.share] |\n"
            "| B | 3.7% [fact: ad_b.bottom_5.share] |\n"
        )
        self.assertEqual([str(p) for p in out], [])

    def test_a_percentage_cell_beside_a_count_cell_uses_the_right_fact(self):
        """A row mixes units. Each figure must find its own citation."""
        good = self._check(
            "| A | 13 [fact: ad_a.live.count] | 9.2% "
            "[fact: ad_a.bottom_5.share] |\n"
        )
        self.assertEqual([str(p) for p in good], [])
        # The count fact declares no percentage scale, so it cannot stand in.
        bad = self._check("| A | 13 [fact: ad_a.live.count] | 9.2% |\n")
        self.assertEqual([p.kind for p in bad], ["ambiguous_unit"])
        self.assertIn("ad_a.live.count", bad[0].detail)

    # -- exemptions that must keep working ---------------------------------

    def test_the_existing_exemptions_survive_the_move_to_decimals(self):
        for line in (
            "Section 3.3 covers the per-headline serve report.",
            "See rule 6 and step 1a of campaign-guardrails.",
            "The pull ran at 14:56 on 2026-08-19, and again on Aug 14.",
            "Numbered 1.2.3 in the appendix.",
            "Google Ads API v21.1, appendix 1.2.3.",
            "The threshold is CAD 2,000 of 30-day cost.",
            "A `0.52` ratio and a `9.2%` share sit in code spans.",
            "It went out on the 14th, in 2026.",
            "Verified live in 2026.",
            "Ad 620000000010 in ad group 620000000011.",
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    [str(p) for p in self._check(line + "\n")], [], line
                )


    def test_a_version_shaped_decimal_with_no_pointer_word_is_still_a_claim(self):
        """Recorded, not fixed.

        `claims.py` does not exempt a word this pack never writes: exempting
        `version` would have made the checker greener on a claim nobody makes.
        The version forms the pack does write - `v21.1`, `1.2.3` - are already
        covered by WORD_WITH_NUMERAL and DOTTED_LABEL.
        """
        for line in ("Python 3.11 is the runtime.", "This is version 2.4."):
            with self.subTest(line=line):
                out = self._check(line + "\n")
                self.assertEqual([p.kind for p in out], ["uncited"], line)





# ==========================================================================
# claims.py — the one definition of "not a claim"
# ==========================================================================


class SharedNonClaimTests(unittest.TestCase):
    """One test per exemption. The names match `claims.NON_CLAIM_PATTERNS`.

    Every exemption here makes both checkers greener, so each one is asserted
    individually rather than in a lump. `CheckerAgreementTests` below then
    asserts the other half: that real claims are still caught.
    """

    def masked(self, line: str) -> str:
        return claims.mask_non_claims(line)

    def assertMasked(self, line: str, *digits: str):
        out = self.masked(line)
        for digit in digits:
            self.assertNotIn(
                digit, out, f"{digit!r} survived masking of {line!r} -> {out!r}"
            )
        self.assertEqual(len(out), len(line), "masking must preserve length")

    def assertKept(self, line: str, *digits: str):
        out = self.masked(line)
        for digit in digits:
            self.assertIn(
                digit, out, f"{digit!r} was masked out of {line!r} -> {out!r}"
            )

    def test_numeral_hyphenated_into_a_word(self):
        self.assertMasked("a 30-day window", "30")
        self.assertMasked("the 8-action basis", "8")
        self.assertMasked("a 14-day learning window", "14")
        self.assertMasked("2 new ads on Aug-14", "14")

    def test_a_hyphen_followed_by_a_digit_is_still_two_figures(self):
        """`facts.py` documented this and it must survive the move."""
        self.assertKept("a 60-40 split", "60", "40")

    def test_dotted_section_label_and_markdown_heading_number(self):
        self.assertMasked("### 3.3 Per-headline serve report", "3.3")
        self.assertMasked("### 4. Classify every policy restriction", "4")
        self.assertMasked("## 10 Verify, then present", "10")
        self.assertMasked("see 1.2.3 below", "1.2.3")

    def test_a_number_later_on_a_heading_line_is_still_a_claim(self):
        """Only the number that OPENS a heading is structure."""
        self.assertKept("### 3.3 We found 126 dead slots", "126")

    def test_ordered_list_marker(self):
        self.assertMasked("1. Account-level summary", "1")
        self.assertMasked("2) What changed since the last audit", "2")

    def test_label_words(self):
        for line, digits in (
            ("see section 5", ("5",)),
            ("campaign-guardrails rule 6", ("6",)),
            ("step 1a owns the mechanics", ("1a",)),
            ("Tier 2 verification is mechanical", ("2",)),
            ("the top 10 servable RSAs by cost", ("10",)),
            ("the bottom 5 take the rest", ("5",)),
            ("rank 3 is the swap candidate", ("3",)),
            ("step 3.3 says so", ("3.3",)),
        ):
            self.assertMasked(line, *digits)

    def test_label_words_are_deliberately_tight(self):
        """`note`, `row` and `point` read as prose and were left out."""
        self.assertKept("Note 2 ads were created", "2")
        self.assertKept("row 5 counts", "5")

    def test_gaql_and_window_tokens(self):
        self.assertMasked("segments.date DURING LAST_30_DAYS", "30")
        self.assertMasked("change_event DURING LAST_14_DAYS", "14")
        self.assertTrue(claims.GAQL_WINDOW_TOKEN.search("LAST_30_DAYS"))

    def test_dates_and_times_in_every_form(self):
        for line, digits in (
            ("changed on 2026-08-14", ("2026", "08", "14")),
            ("removed on Aug 14", ("14",)),
            ("removed on 14 August", ("14",)),
            ("at 14:56 and 14:58", ("56", "58")),
            ("the Aug 5/10 toolkit runs", ("5", "10")),
        ):
            self.assertMasked(line, *digits)

    def test_ordinals_and_years(self):
        self.assertMasked("fixed on the 14th", "14")
        self.assertMasked("in 2026 the account changed", "2026")

    def test_object_ids_by_context_by_parens_and_bare(self):
        self.assertMasked("ad group 620000000012", "620000000012")
        self.assertMasked("criterion 620000000013", "620000000013")
        self.assertMasked("ad 620000000001", "620000000001")
        self.assertMasked("campaign 72000000014", "72000000014")
        self.assertMasked("Templates (620000000011) spent money", "620000000011")
        self.assertMasked("resource 620000000008~620000000001", "620000000008")

    def test_the_bare_object_id_threshold_is_eight_digits(self):
        """Item 2: this account has 8-digit criterion ids.

        Lowering the threshold from 9 to 8 is free here. The largest
        integer-valued fact in the real 2026-08-19 file is 194,056 impressions,
        six digits, and every money figure carries decimals, so no legitimate
        figure in this pack reaches eight digits.
        """
        self.assertEqual(claims.BARE_OBJECT_ID_MIN_DIGITS, 8)
        self.assertMasked("10363430 is a criterion id", "10363430")
        self.assertKept("194056 impressions", "194056")

    def test_citation_tokens_are_not_claims(self):
        line = (
            "See [fact: asset_view.adgroup.620000000011.ad.620000000010."
            "headline.rank_1.impressions] for the figure."
        )
        self.assertMasked(line, "620000000011", "620000000010")

    def test_urls_inline_code_and_link_targets(self):
        self.assertMasked(
            "https://ads.google.com/aw/ads?ocid=100000001&adGroupId=620000000012",
            "100000001",
            "620000000012",
        )
        self.assertMasked("the count is `27 chars`", "27")
        self.assertMasked("[Templates](https://x/y?campaignId=72000000007)",
                          "72000000007")

    def test_both_fence_syntaxes_end_a_block(self):
        """`facts.py` honoured `~~~`, `citations.py` did not. Both now do."""
        for fence in ("```", "~~~"):
            lines = claims.masked_lines(f"before\n{fence}\n999 inside\n{fence}\nafter")
            self.assertEqual(
                [entry.masked for entry in lines],
                ["before", "", "", "", "after"],
                fence,
            )

    def test_real_claims_are_never_masked(self):
        """The blind spot is over-masking. This is the guard against it."""
        for line in (
            "We found 126 dead headline slots.",
            "The account runs 61 servable RSAs across 45 ad groups.",
            "878 live headline slots and 752 with an impression.",
            "126 dead slots across 99 ads",
            "16 ads carry UNAPPROVED_SUBSTANCES and 23 are APPROVED_LIMITED.",
            "5 servable RSAs are POOR or AVERAGE.",
            "21 ads hold at least one dead slot.",
        ):
            out = self.masked(line)
            for token in re.findall(r"\d+", line):
                self.assertIn(
                    token, out, f"{token!r} was masked out of {line!r} -> {out!r}"
                )


class CheckerCorpusTests(unittest.TestCase):
    """The masker corpus: real report wording, exempt and claiming.

    Historically these tests held two checkers to the same answer (they
    disagreed for one release — `30-day` passed one and failed the other,
    `### 3.3` the reverse — and no report wording passed both). The value
    search is gone; the corpus stays, because it is the strongest guard
    against an exemption regression in `claims.py` in either direction.
    """

    #: Every exemption class, in prose a real report would actually contain.
    NON_CLAIM_CORPUS = """\
### 3.3 Per-headline serve report

The audit covers a 30-day window on the 8-action basis, with a 14-day learning
window per campaign-guardrails rule 6 and Tier 2 verification per section 5.

### 4. Scope

The pull filtered segments.date DURING LAST_30_DAYS. Step 1a supplied the
undated inventory. The top 10 servable RSAs by cost get a table; the bottom 5
of each ranking are swap candidates.

Ad group 620000000012, criterion 620000000013, ad 620000000001 and campaign
72000000014 are the four ids in this example. Criterion 10363430 is the
shortest. Templates (620000000011) is the inline form.

The misspelled headline was removed on 2026-08-14 at 14:56, and again on Aug 14
at 14:58. By the 14th it was gone from all six. In 2026 the account is actively
managed. See the Aug 5/10 runs.

The link is https://ads.google.com/aw/ads?ocid=100000001&campaignId=72000000007
and the replacement is `27 chars` long.

1. First point.
2) Second point.

```
this fenced block holds 999999 and nobody checks it
```

~~~
this one too, 888888
~~~
"""

    #: Real claims about the account, deliberately uncited.
    CLAIM_CORPUS = """\
The account runs 61 servable RSAs across 45 ad groups. We found 126 dead
headline slots on 21 ads, out of 878 live slots, 752 of which took an
impression. 16 ads carry UNAPPROVED_SUBSTANCES.
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # A deliberately tiny facts file. Nothing in either corpus may pass by
        # matching a value in it.
        fs = FactSet()
        fs.add(
            id="rsa.servable.count",
            value=61,
            unit="count",
            scope=builder.VF5_FULL_FILTER,
            window="current-as-of-2026-08-19",
            source="inventory.json",
            basis="n/a",
        )
        self.facts_path = self.dir / "facts.json"
        fs.write(self.facts_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text: str) -> Path:
        path = self.dir / "report.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_checker_passes_the_whole_non_claim_corpus(self):
        report = self._write(self.NON_CLAIM_CORPUS)
        problems = citations.check(str(report), str(self.facts_path))
        self.assertEqual(
            [str(p) for p in problems], [], "citations.py flagged a non-claim"
        )

    def test_the_two_historically_disputed_forms_both_pass(self):
        """`### 3.3` and `30-day` — the wording the checker split over."""
        heading = self._write("### 3.3 Per-headline serve report\n")
        self.assertEqual(citations.check(str(heading), str(self.facts_path)), [])
        hyphen = self._write("The pull covers a 30-day window.\n")
        self.assertEqual(citations.check(str(hyphen), str(self.facts_path)), [])

    def test_the_checker_still_catches_the_real_claims(self):
        report = self._write(self.CLAIM_CORPUS)
        problems = citations.check(str(report), str(self.facts_path))
        flagged_by_citations = {
            int(m) for p in problems for m in re.findall(r"\b\d+\b", p.detail)
        }
        for wrong in (45, 126, 21, 878, 752, 16):
            self.assertIn(
                wrong, flagged_by_citations, f"citations.py missed {wrong}"
            )

    def test_the_vf15a_regression_still_fires(self):
        """The wrong integer riding along on a correct citation."""
        report = self._write(
            "126 dead slots across 99 ads [fact: rsa.servable.count].\n"
        )
        problems = citations.check(str(report), str(self.facts_path))
        self.assertTrue(
            any("99" in p.detail for p in problems),
            f"the uncited 99 rode along: {[str(p) for p in problems]}",
        )

    def test_a_thousands_separated_money_figure_makes_no_phantom_claim(self):
        """`CAD 2,000` used to produce an uncitable claim of `000`.

        The `(?<!CAD )` lookbehind rejected the whole `2,000`, and the engine
        then matched the `000` after the comma on its own. Step 5's selection
        rule states a CAD 2,000 threshold in prose, so that wording could not
        pass either.
        """
        report = self._write(
            "Any servable RSA above CAD 2,000 of 30-day cost gets a table.\n"
        )
        self.assertEqual(
            [str(p) for p in citations.check(str(report), str(self.facts_path))], []
        )

    def test_a_currency_prefixed_integer_needs_no_citation_but_a_decimal_does(self):
        """The line between a threshold the report chose and an account figure.

        `CAD 2,000` is step 5's own selection rule, stated in prose. There is no
        fact to cite because the account never produced it. `$48,213.90` is
        read out of the account, so it must name its fact.
        """
        threshold = self._write("The cut is CAD 2,000 of 30-day cost.\n")
        self.assertEqual(
            [str(p) for p in citations.check(str(threshold), str(self.facts_path))],
            [],
        )
        money = self._write("Total spend was $48,213.90.\n")
        problems = citations.check(str(money), str(self.facts_path))
        self.assertEqual([p.kind for p in problems], ["uncited"])
        self.assertIn("48,213.90", problems[0].detail)

    def test_a_serve_table_row_passes_when_the_rank_cell_says_rank(self):
        """Step 5's table has a Rank column, and a bare `1` there is a claim.

        A bare ordinal in a table cell is still flagged, on purpose: masking
        every small integer in a first cell would hide real counts, and
        over-masking is this module's failure mode. Writing the cell as
        `Rank 1` costs four characters and needs no exemption, because `rank` is
        already a pointer word. Backticks would work too but step 9 sanctions
        code spans for exactly two kinds of figure and a rank is neither.
        """
        header = "| Rank | Headline | Impressions |\n| :- | :- | -: |\n"
        bare = self._write(header + "| 1 | Templates | 61 [fact: rsa.servable.count] |\n")
        self.assertTrue(
            any("1" in p.detail for p in citations.check(str(bare), str(self.facts_path))),
            "a bare ordinal in a table cell should still be flagged",
        )
        named = self._write(
            header + "| Rank 1 | Templates | 61 [fact: rsa.servable.count] |\n"
        )
        self.assertEqual(
            [str(p) for p in citations.check(str(named), str(self.facts_path))], []
        )

    def test_no_module_defines_its_own_exemptions_or_regrows_a_checker(self):
        """Structural: one masker, one checker.

        `citations.py` must take its exemptions from `claims.py`, and
        `facts.py` must not regrow a report checker — the value search was
        removed 2026-08-27 because it matched by coincidence.
        """
        self.assertIs(citations.claims, claims)
        for module, banned in (
            (F, ("_LONG_ID_RE", "_ISO_DATE_RE", "_YEAR_RE", "_mask_line",
                 "verify_report", "Untraceable", "_FIGURE_RE")),
            (citations, ("_SKIP", "_DATEY", "_strip_code")),
        ):
            for name in banned:
                self.assertFalse(
                    hasattr(module, name),
                    f"{module.__name__}.{name} should not exist. Exemptions "
                    "live in claims.py; report checking lives in citations.py.",
                )


class RealObjectIdTests(unittest.TestCase):
    """Item 2. Real ids from customer 1234567890, at every length it uses.

    `citations.py` skipped runs of 9+ digits as object ids. This account has
    8-digit criterion ids, and `evidence-links` requires printing an id beside
    every keyword claim, so an 8-digit id was an uncitable integer claim and no
    compliant keyword table could be written.
    """

    #: Measured from `audit-ad-copy-keyword-cost-30d.json`, 2026-08-19: criterion
    #: id lengths run 8, 9, 10, 11, 12 and 13 digits.
    IDS = {
        8: "10363430",
        9: "126070036",
        11: "72000000014",
        12: "620000000001",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        fs = FactSet()
        fs.add(
            id="keyword.spend.servable.total",
            value=Decimal("12840.15"),
            unit="currency_cad",
            scope="ad_group_criterion.status=ENABLED",
            window="2026-07-20..2026-08-18",
            source="keyword-cost.json",
            basis="n/a",
        )
        self.facts_path = self.dir / "facts.json"
        fs.write(self.facts_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _check(self, markdown: str):
        path = self.dir / "report.md"
        path.write_text(markdown, encoding="utf-8")
        return citations.check(str(path), str(self.facts_path))

    def test_every_id_length_passes_in_context(self):
        for digits, value in sorted(self.IDS.items()):
            with self.subTest(digits=digits):
                problems = self._check(
                    f"The keyword sits on criterion {value} in that ad group.\n"
                )
                self.assertEqual([str(p) for p in problems], [])

    def test_the_evidence_links_inline_form_passes(self):
        """The money figure carries its id: a decimal is a citable claim now."""
        problems = self._check(
            "Spend was $12,840.15 [fact: keyword.spend.servable.total] on "
            f"`invoicing software` ({self.IDS[8]}) in ad group Generic Core "
            f"({self.IDS[12]}).\n"
        )
        self.assertEqual([str(p) for p in problems], [])

    def test_a_bare_id_of_eight_or_more_digits_is_not_a_claim(self):
        for digits, value in sorted(self.IDS.items()):
            with self.subTest(digits=digits):
                problems = self._check(f"| keyword | {value} |\n")
                self.assertEqual([str(p) for p in problems], [])


# ==========================================================================
# gaql.py — VF-19a, clause-for-clause instead of byte equality
# ==========================================================================


class Vf19aNormaliserTests(unittest.TestCase):
    """The manifest holds executable GAQL; a fact scope holds a label.

    Byte equality between the two is impossible, and `audit-ad-copy` demanded it
    until 2026-08-19. A run tried to satisfy both, correctly reported that it
    could not, and lost time to it.
    """

    INVENTORY_GAQL = (
        "ad_group_ad.status='ENABLED' AND ad_group.status='ENABLED' "
        "AND campaign.status='ENABLED' "
        "AND ad_group_ad.ad.type='RESPONSIVE_SEARCH_AD' "
        "AND campaign.advertising_channel_type='SEARCH'"
    )

    def test_the_normaliser_is_exactly_the_two_documented_steps(self):
        self.assertEqual(
            gaql.strip_quotes("ad_group_ad.status='ENABLED'"),
            "ad_group_ad.status=ENABLED",
        )
        self.assertEqual(
            gaql.strip_quotes(
                "ad_group_ad_asset_view.field_type IN ('HEADLINE','DESCRIPTION')"
            ),
            "ad_group_ad_asset_view.field_type IN (HEADLINE,DESCRIPTION)",
        )
        self.assertEqual(
            gaql.apply_aliases("ad_group_ad.ad.type='RESPONSIVE_SEARCH_AD'"),
            "ad.type='RESPONSIVE_SEARCH_AD'",
        )
        # And it does not touch a field that merely starts the same way.
        self.assertEqual(
            gaql.apply_aliases("ad_group_ad.ad.type_extra=X"),
            "ad_group_ad.ad.type_extra=X",
        )

    def test_the_two_spellings_are_semantically_equal(self):
        result = gaql.compare(self.INVENTORY_GAQL, S.VF5_FULL_FILTER)
        self.assertTrue(result.ok, result.describe())
        self.assertEqual(len(result.matched), 5)
        self.assertNotEqual(
            self.INVENTORY_GAQL, S.VF5_FULL_FILTER, "byte equality is impossible"
        )

    def test_the_date_clause_is_temporality_not_scope(self):
        """VF-17 puts the range in `window`, so it is held out of the diff."""
        result = gaql.compare(
            self.INVENTORY_GAQL + " AND segments.date DURING LAST_30_DAYS",
            S.VF5_FULL_FILTER,
        )
        self.assertTrue(result.ok, result.describe())
        self.assertEqual(result.temporality, ("segments.date DURING LAST_30_DAYS",))
        self.assertEqual(result.extra, ())

    def test_a_missing_clause_is_a_hard_fail(self):
        """The exact 2026-08-19 drift: the manifest declared four of five."""
        four = (
            "ad_group_ad.status='ENABLED' AND ad_group.status='ENABLED' "
            "AND campaign.status='ENABLED' "
            "AND ad_group_ad.ad.type='RESPONSIVE_SEARCH_AD'"
        )
        with self.assertRaises(gaql.FilterMismatch) as ctx:
            gaql.assert_semantic_match("inv", four, S.VF5_FULL_FILTER)
        self.assertIn("MISSING from the query", str(ctx.exception))
        self.assertIn("campaign.advertising_channel_type=SEARCH", str(ctx.exception))

    def test_an_extra_non_date_clause_is_a_hard_fail(self):
        """The date carve-out must not become a hole for any extra clause."""
        with self.assertRaises(gaql.FilterMismatch) as ctx:
            gaql.assert_semantic_match(
                "inv",
                self.INVENTORY_GAQL + " AND ad_group.status='PAUSED'",
                S.VF5_FULL_FILTER,
            )
        self.assertIn("EXTRA in the query", str(ctx.exception))
        self.assertIn("ad_group.status=PAUSED", str(ctx.exception))

    def test_a_date_clause_on_an_unexpected_field_is_still_extra(self):
        with self.assertRaises(gaql.FilterMismatch):
            gaql.assert_semantic_match(
                "inv",
                self.INVENTORY_GAQL + " AND metrics.impressions > 0",
                S.VF5_FULL_FILTER,
            )

    def test_temporality_cross_check_both_directions(self):
        gaql.check_declared_temporality(
            self.INVENTORY_GAQL, "current-as-of-2026-08-19"
        )
        gaql.check_declared_temporality(
            self.INVENTORY_GAQL + " AND segments.date DURING LAST_30_DAYS",
            "window:LAST_30_DAYS",
        )
        # The VF-17 rule 5 case a field mask got wrong: a change_event pull has
        # no segments.* or metrics.* field yet is windowed.
        gaql.check_declared_temporality(
            "change_event.change_date_time DURING LAST_14_DAYS",
            "window:LAST_14_DAYS",
        )
        with self.assertRaises(gaql.FilterMismatch):
            gaql.check_declared_temporality(
                self.INVENTORY_GAQL + " AND segments.date DURING LAST_30_DAYS",
                "current-as-of-2026-08-19",
            )
        with self.assertRaises(gaql.FilterMismatch):
            gaql.check_declared_temporality(
                self.INVENTORY_GAQL, "window:LAST_30_DAYS"
            )
        with self.assertRaises(gaql.FilterMismatch):
            gaql.check_declared_temporality(
                self.INVENTORY_GAQL + " AND segments.date DURING LAST_30_DAYS",
                "window:LAST_14_DAYS",
            )


class BuilderVf19aTests(unittest.TestCase):
    """The builder compares clause for clause and refuses on a real mismatch."""

    def _dir(self, **kwargs):
        return SyntheticRawDir(
            [ad_row("1", "10")],
            [metric_row("1", "10", 1_000_000)],
            manifest_rows={},
            **kwargs,
        )

    def test_the_gaql_and_the_label_spellings_both_build(self):
        with self._dir() as raw:
            fs = builder.build(raw)
        trail = fs.meta["declared_filter_audit"]
        self.assertTrue(
            any("clause(s) matched after normalising" in line for line in trail),
            trail,
        )

    def test_a_manifest_missing_a_canonical_clause_refuses_to_build(self):
        four = (
            "ad_group_ad.status='ENABLED' AND ad_group.status='ENABLED' "
            "AND campaign.status='ENABLED' "
            "AND ad_group_ad.ad.type='RESPONSIVE_SEARCH_AD'"
        )
        with self._dir(inv_filter=four) as raw:
            with self.assertRaises(gaql.FilterMismatch) as ctx:
                builder.build(raw)
        self.assertIn("MISSING from the query", str(ctx.exception))

    def test_a_manifest_with_an_extra_clause_refuses_to_build(self):
        with self._dir(
            inv_filter=SyntheticRawDir.INVENTORY_GAQL
            + " AND campaign.name LIKE '%Brand%'"
        ) as raw:
            with self.assertRaises(gaql.FilterMismatch) as ctx:
                builder.build(raw)
        self.assertIn("EXTRA in the query", str(ctx.exception))

    def test_a_manifest_whose_two_fields_disagree_refuses_to_build(self):
        with self._dir(inv_temporality="window:LAST_30_DAYS") as raw:
            with self.assertRaises(gaql.FilterMismatch):
                builder.build(raw)

    def test_the_cli_reports_a_mismatch_instead_of_writing(self):
        four = (
            "ad_group_ad.status='ENABLED' AND ad_group.status='ENABLED' "
            "AND campaign.status='ENABLED' "
            "AND ad_group_ad.ad.type='RESPONSIVE_SEARCH_AD'"
        )
        with self._dir(inv_filter=four) as raw:
            out = raw / "facts.json"
            self.assertEqual(builder.main([str(raw), "--out", str(out)]), 1)
            self.assertFalse(out.exists())


# ==========================================================================
# One canonical name for the asset pull
# ==========================================================================


class DuplicateAssetPullTests(unittest.TestCase):
    """Two names for one pull is a divergence waiting to happen (item 5).

    On 2026-08-19 `audit-ad-copy-asset-serves-30d.json` and
    `audit-ad-copy-asset-view-30d.json` sat in the raw directory sharing one
    md5, and `scopes.py` accepted either as an alias. Identical copies are
    harmless right up until they diverge, and then the builder and a human
    reader silently read different files.
    """

    def _rows(self):
        return (
            [ad_row("1", "10")],
            [metric_row("1", "10", 1_000_000)],
        )

    def _assets(self, impressions):
        return [
            asset_view_row("1", "10", "A", impressions, 5, "GOOD", "900"),
            asset_view_row("1", "10", "B", 10, 1, "PENDING", "901"),
        ]

    def test_the_canonical_name_is_the_only_one_preferred(self):
        self.assertEqual(S.ASSET_VIEW, S.ASSET_VIEW_CANONICAL)
        self.assertNotIn(S.ASSET_VIEW_CANONICAL, S.ASSET_VIEW_LEGACY_ALIASES)

    def test_an_identical_duplicate_builds_and_is_reported(self):
        inv, met = self._rows()
        with SyntheticRawDir(inv, met, asset_rows=self._assets(100)) as raw:
            body = (raw / S.ASSET_VIEW_CANONICAL).read_text(encoding="utf-8")
            (raw / S.ASSET_VIEW_LEGACY_ALIASES[0]).write_text(body, encoding="utf-8")
            fs = builder.build(raw)
        trail = " ".join(fs.meta["declared_filter_audit"])
        self.assertIn("byte-identical duplicate", trail)
        self.assertIn(S.ASSET_VIEW_LEGACY_ALIASES[0], trail)
        self.assertEqual(fs.value("asset_view.ranked.ad.count"), 1)

    def test_a_divergent_duplicate_refuses_to_build(self):
        inv, met = self._rows()
        with SyntheticRawDir(inv, met, asset_rows=self._assets(100)) as raw:
            write_pull(
                raw / S.ASSET_VIEW_LEGACY_ALIASES[0],
                "adGroup.id,adGroupAd.ad.id,adGroupAdAssetView.fieldType,"
                "adGroupAdAssetView.performanceLabel,asset.id,"
                "asset.textAsset.text,metrics.impressions,metrics.clicks",
                self._assets(999),
            )
            with self.assertRaises(S.ManifestError) as ctx:
                builder.build(raw)
        msg = str(ctx.exception)
        self.assertIn("contents DIFFER", msg)
        self.assertIn(S.ASSET_VIEW_CANONICAL, msg)
        self.assertIn("Refusing to write a facts file", msg)

    def test_the_cli_refuses_a_divergent_duplicate(self):
        inv, met = self._rows()
        with SyntheticRawDir(inv, met, asset_rows=self._assets(100)) as raw:
            write_pull(
                raw / S.ASSET_VIEW_LEGACY_ALIASES[0],
                "adGroup.id,adGroupAd.ad.id,adGroupAdAssetView.fieldType,"
                "adGroupAdAssetView.performanceLabel,asset.id,"
                "asset.textAsset.text,metrics.impressions,metrics.clicks",
                self._assets(999),
            )
            out = raw / "facts.json"
            self.assertEqual(builder.main([str(raw), "--out", str(out)]), 1)
            self.assertFalse(out.exists())

    def test_the_legacy_name_alone_is_read_and_flagged(self):
        inv, met = self._rows()
        with SyntheticRawDir(inv, met, asset_rows=self._assets(100)) as raw:
            (raw / S.ASSET_VIEW_CANONICAL).rename(
                raw / S.ASSET_VIEW_LEGACY_ALIASES[0]
            )
            fs = builder.build(raw)
        trail = " ".join(fs.meta["declared_filter_audit"])
        self.assertIn("LEGACY name", trail)


# ==========================================================================
# Scope labels that said something other than what was computed
# ==========================================================================




# ==========================================================================
# Policy classes the skills keep separate
# ==========================================================================




# ==========================================================================
# The facts the skills asked for and could not cite
# ==========================================================================





# --------------------------------------------------------------------------
# The 2026-08-21 checker hole. Two defects, one chain.
# --------------------------------------------------------------------------


class SpannedFigureTests(unittest.TestCase):
    """A code span next to a `[fact:]` citation is a claim, not an exemption.

    What happened. `claims.BARE_OBJECT_ID` matched 8+ digit runs, and the
    FRACTIONAL part of a long decimal is such a run, so `2.4134329359732405`
    masked to `2.` and both checkers compared the literal `2` against the fact.
    A correct figure looked wrong. The workaround was to wrap those figures in
    code spans - and code spans are exempt, which silently switched off value
    checking for every long decimal in the pack.

    Consequence, measured: a report shipped four fabricated table cells, one of
    them a wrong DECISION, every one citing a fact that said otherwise, and both
    checkers passed. A probe showed the hole was total - `42` cited to a fact
    holding `167.260587` also passed.

    These assert the contract from both ends, so neither half can regress alone.
    """

    def test_a_long_decimal_survives_masking(self):
        """The root cause. An object id never follows a decimal point."""
        for value in ("2.4134329359732405", "0.8486011700219589", "22.253361333652197"):
            with self.subTest(value=value):
                self.assertIn(value, claims.mask_non_claims(f"ROAS {value} tail"))

    def test_a_real_object_id_is_still_exempt(self):
        """The exemption has to keep working for what it was written for."""
        for value in ("620000000011", "72000000015", "72000000016"):
            with self.subTest(value=value):
                self.assertNotIn(value, claims.mask_non_claims(f"campaign {value}"))

    def test_a_spanned_figure_beside_a_citation_is_not_exempt(self):
        line = "| x | `167.008535` `[fact: some.id]` |"
        self.assertIn("167.008535", claims.mask_non_claims(line))

    def test_a_spanned_figure_with_no_citation_stays_exempt(self):
        """Rule thresholds, window tokens and ids must keep their exemption."""
        self.assertNotIn("5", claims.mask_non_claims("guardrail `5`'s floor of `5`"))
        self.assertNotIn("10000", claims.mask_non_claims("returned exactly `10000` rows"))

    def test_unspanning_preserves_column_offsets(self):
        """A reported line has to still line up with the file."""
        line = "| x | `167.008535` `[fact: some.id]` |"
        self.assertEqual(len(claims.unspan_cited_figures(line)), len(line))

if __name__ == "__main__":
    unittest.main(verbosity=2)