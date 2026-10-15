#!/usr/bin/env python3
"""The CSV-upload path, on a committed synthetic export."""
import sys, tempfile, unittest
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import csv_negatives as C  # noqa: E402

EXPORT = HERE / "fixtures" / "csv" / "search-terms-export-synthetic.csv"
T = C.Thresholds(Decimal("50"), 10, Decimal("120"), Decimal("3"))


class CsvNegativesTests(unittest.TestCase):
    def setUp(self):
        self.rows = C.run(C.read_export(EXPORT.read_bytes()), T, ["acme"])
        self.by = {r.search_term: r.decision for r in self.rows}

    def test_title_and_total_rows_are_skipped(self):
        self.assertEqual(len(self.rows), 7)

    def test_decisions_follow_the_numbers(self):
        self.assertEqual(self.by["free invoice template word"], "NEGATE")
        self.assertEqual(self.by["invoice generator"], "HOLD")
        self.assertEqual(self.by["how to write an invoice"], "CUT")
        self.assertEqual(self.by["small business invoicing software"], "KEEP")
        self.assertEqual(self.by["cheap invoice"], "KEEP")

    def test_brand_and_added_terms_are_never_negated(self):
        self.assertEqual(self.by["acme login"], "KEEP")
        self.assertEqual(self.by["acme invoicing"], "KEEP")

    def test_utf16_tab_export_reads_the_same(self):
        text = EXPORT.read_text().replace(",", "\t")
        rows = C.read_export(text.encode("utf-16"))
        self.assertEqual(len(rows), 7)

    def test_missing_thresholds_refuse(self):
        self.assertEqual(C.main([str(EXPORT), "--min-spend", "50"]), 2)

    def test_not_an_export_refuses(self):
        with self.assertRaises(C.CsvInputError):
            C.read_export(b"a,b,c\n1,2,3\n")

    def test_outputs_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = C.main([str(EXPORT), "--min-spend", "50", "--min-clicks", "10",
                         "--target-cpa", "120", "--target-cpa-multiple", "3",
                         "--brand-token", "acme", "--out", tmp])
            self.assertEqual(rc, 0)
            neg = (Path(tmp) / "negatives.csv").read_text()
            self.assertIn("[free invoice template word]", neg)
            self.assertIn("[how to write an invoice]", neg)
            self.assertNotIn("acme", neg)
            self.assertTrue((Path(tmp) / "summary.md").exists())


if __name__ == "__main__":
    unittest.main()
