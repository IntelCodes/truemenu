"""Nugttah feed mapping, against sample data in the format requested from Nugttah.

  python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRUEMENU_STATE", tempfile.mkdtemp(prefix="truemenu-test-"))
os.environ.setdefault("TRUEMENU_QUOTE_SECRET", "test-secret-not-used-anywhere-else")
os.environ["NUGTTAH_FIXTURE"] = str(ROOT / "tests" / "fixtures" / "nugttah")
os.environ.pop("NUGTTAH_API_BASE", None)

from truemenu import config, nugttah, query  # noqa: E402

RIYADH = ZoneInfo("Asia/Riyadh")


def at(day, hm):
    # 2026-11-15 is a Sunday
    d = {"sun": 15, "mon": 16, "fri": 20, "sat": 21}[day]
    h, m = map(int, hm.split(":"))
    return datetime(2026, 11, d, h, m, tzinfo=RIYADH).isoformat()


class NugttahTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._db = config.DB_PATH                     # own database, so other test files are unaffected
        config.DB_PATH = Path(tempfile.mkdtemp(prefix="truemenu-ng-")) / "truemenu.db"
        nugttah.pull()

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH = cls._db

    def find(self, **kw):
        p = {"near": "Riyadh", "limit": 25}
        p.update(kw)
        return query.find_items(p, "test")

    def test_branches_and_prices_mapped(self):
        out = self.find(available_at=at("mon", "13:00"))
        names = {r["name"]: r for r in out["results"]}
        r = names["Grilled Chicken Plate"]
        self.assertEqual(r["price"]["currency"], "SAR")
        self.assertEqual(r["price"]["amount"], 39.0)
        self.assertEqual(r["price"]["usd_equivalent"], 10.4)
        self.assertEqual(r["name_ar"], "صحن دجاج مشوي")
        self.assertEqual(r["calories"], 720)
        self.assertEqual(r["merchant"]["area"], "Al Olaya, Riyadh")
        self.assertTrue(r["quote_id"])

    def test_vat_added_when_price_excludes_it(self):
        out = self.find(available_at=at("mon", "13:00"))
        mandi = [r for r in out["results"] if r["name"] == "Lamb Mandi"][0]
        self.assertEqual(mandi["price"]["amount"], 69.0)

    def test_unavailable_item_not_offered(self):
        out = self.find(available_at=at("mon", "13:00"))
        self.assertNotIn("Hummus", [r["name"] for r in out["results"]])

    def test_paused_branch_not_offered(self):
        out = self.find(available_at=at("mon", "13:00"))
        self.assertNotIn("Halloumi Sandwich", [r["name"] for r in out["results"]])
        self.assertGreater(out["dropped"]["closed"], 0)

    def test_split_shifts_and_after_midnight(self):
        self.assertEqual(self.find(available_at=at("mon", "17:00"))["count"], 0)      # between shifts
        self.assertGreater(self.find(available_at=at("mon", "23:30"))["count"], 0)    # evening shift
        self.assertGreater(self.find(available_at=at("sun", "01:30"))["count"], 0)    # Saturday's shift, after midnight
        self.assertEqual(self.find(available_at=at("fri", "12:00"))["count"], 0)      # Friday opens 13:30

    def test_budget_in_dollars_uses_the_peg(self):
        out = self.find(available_at=at("mon", "13:00"), budget_usd_max=11)
        self.assertEqual(sorted(r["name"] for r in out["results"]), ["Fattoush", "Grilled Chicken Plate"])

    def test_pork_tag_from_jurisdiction(self):
        out = self.find(available_at=at("mon", "13:00"), exclude=["pork"])
        tag = out["results"][0]["tags"]["contains_pork"]
        self.assertEqual((tag["value"], tag["source"]), (False, "jurisdiction:SA"))

    def test_coverage_lists_riyadh(self):
        cov = self.find(available_at=at("mon", "13:00"))["coverage"]
        self.assertTrue(cov["supported"])
        self.assertIn("Al Olaya, Riyadh", cov["covered_areas"])

    def test_freshness_reports_merchant_data_time(self):
        out = self.find(available_at=at("mon", "13:00"))
        self.assertEqual(out["results"][0]["freshness"]["merchant_data_as_of"], "2026-11-16T19:55:00Z")
        self.assertEqual(out["results"][0]["freshness"]["source"], "nugttah_api")


if __name__ == "__main__":
    unittest.main()
