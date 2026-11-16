"""Regression test for the cold-start boundary failure (docs/COLD_START_TEST.md).

A user asked for lunch near Harvard Square. TrueMenu covers only Kendall Square, returned an
empty list with no explanation, and the agent searched Kendall Square instead and decided to pay.
find_items must now say plainly when the requested area has no data.

  python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TRUEMENU_STATE"] = tempfile.mkdtemp(prefix="truemenu-test-")
os.environ.setdefault("TRUEMENU_QUOTE_SECRET", "test-secret-not-used-anywhere-else")

from truemenu import feed, query  # noqa: E402

AT = "2026-11-16T12:30"   # Monday lunch, every covered restaurant open
ASK = {"budget_usd_max": 20, "exclude": ["pork"], "diet": ["low_carb"], "available_at": AT}


class CoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        feed.pull()

    def test_uncovered_area_is_reported(self):
        out = query.find_items({**ASK, "near": "Harvard Square"}, "test")
        self.assertEqual(out["results"], [])
        cov = out["coverage"]
        self.assertIs(cov["supported"], False)
        self.assertEqual(cov["requested_area"], "Harvard Square")
        self.assertEqual(cov["covered_areas"], ["Kendall Square"])
        self.assertIn("not covered", cov["message"])
        self.assertIn("Do not search another area", cov["message"])
        self.assertIn("Do not pay", cov["message"])

    def test_covered_area_still_returns_quoted_dishes(self):
        out = query.find_items({**ASK, "near": "Kendall Square"}, "test")
        self.assertIs(out["coverage"]["supported"], True)
        self.assertGreater(out["count"], 0)
        self.assertTrue(any(r["quote_id"] for r in out["results"]))
        self.assertTrue(all(r["merchant"]["area"] == "Kendall Square" for r in out["results"]))

    def test_case_and_partial_area_names_match(self):
        self.assertIs(query.find_items({**ASK, "near": "kendall"}, "test")["coverage"]["supported"], True)

    def test_missing_area_is_flagged_not_hidden(self):
        cov = query.find_items(ASK, "test")["coverage"]
        self.assertIsNone(cov["supported"])
        self.assertIn("No area given", cov["message"])

    def test_synthetic_area_not_listed_by_default(self):
        cov = query.find_items({**ASK, "near": "Harvard Square"}, "test")["coverage"]
        self.assertNotIn("synthetic", " ".join(cov["covered_areas"]).lower())


if __name__ == "__main__":
    unittest.main()
