"""Regressions from the hands-on buyer test (fresh sample exports)."""
import unittest
from decimal import Decimal as D

import csv_negatives as N
import gads_csv as G


def row(term, camp, group, clicks, cost, conv):
    return N.Row(search_term=term, campaign=camp, ad_group=group, clicks=clicks,
                 cost=D(str(cost)), conversions=D(str(conv)))


T = N.Thresholds(D("50"), 10, D("120"), D("3"))


class BuyerRegressions(unittest.TestCase):
    def test_core_topic_cut_is_review_not_negative(self):
        d, why = N.decide(row("dental implants", "Search - Implants", "Implants", 48, 690, 1), T)
        self.assertEqual(d, "REVIEW")
        self.assertIn("landing page", why)

    def test_long_tail_off_topic_still_negated(self):
        d, _ = N.decide(row("cheap dental implants mexico", "Search - Implants", "Implants", 25, 310.5, 0), T)
        self.assertEqual(d, "NEGATE")

    def test_above_target_keep_says_watch(self):
        d, why = N.decide(row("invisalign price vancouver", "Search - Invisalign", "Invisalign", 31, 281.4, 1), T)
        self.assertEqual(d, "KEEP")
        self.assertIn("above your target", why)

    def test_brand_misspelling_ending_in_i(self):
        self.assertTrue(N.brand_hit("akmi invoice", ["acme"]))
        self.assertFalse(N.brand_hit("acne cream", ["acme"]))

    def test_comma_csv_date_line_parsed(self):
        raw = ('Search terms report\nSeptember 15, 2026 - October 14, 2026\n'
               'Search term,Campaign,Ad group,Clicks,Cost,Conversions\nx,C,G,1,1.00,0\n').encode()
        exp = G.read(raw, N.REQUIRED, N.OPTIONAL, "Search terms")
        self.assertEqual(exp.days, 30)


if __name__ == "__main__":
    unittest.main()
