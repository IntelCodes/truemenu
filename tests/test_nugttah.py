"""Nugttah feed mapping, against sample data in the format requested from Nugttah.

  python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import json
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRUEMENU_STATE", tempfile.mkdtemp(prefix="truemenu-test-"))
os.environ.setdefault("TRUEMENU_QUOTE_SECRET", "test-secret-not-used-anywhere-else")
os.environ["NUGTTAH_FIXTURE"] = str(ROOT / "tests" / "fixtures" / "nugttah")
os.environ.pop("NUGTTAH_API_BASE", None)

from truemenu import config, db, nugttah, query  # noqa: E402

RIYADH = ZoneInfo("Asia/Riyadh")


def at(day, hm):
    # 2026-11-15 is a Sunday
    d = {"sun": 15, "mon": 16, "fri": 20, "sat": 21}[day]
    h, m = map(int, hm.split(":"))
    return datetime(2026, 11, d, h, m, tzinfo=RIYADH).isoformat()


class NugttahTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._db, cls._state = config.DB_PATH, config.STATE_DIR   # own database and sync state
        config.STATE_DIR = Path(tempfile.mkdtemp(prefix="truemenu-ng-"))
        config.DB_PATH = config.STATE_DIR / "truemenu.db"
        nugttah.sync_all()

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH, config.STATE_DIR = cls._db, cls._state

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
        self.assertEqual(sorted(r["name"] for r in out["results"]), ["Fattoush", "Grilled Chicken Box", "Grilled Chicken Plate"])

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
        self.assertEqual(out["results"][0]["freshness"]["merchant_data_as_of"], "2026-11-16T19:58:00.000Z")
        self.assertEqual(out["results"][0]["freshness"]["source"], "nugttah_api")

    def test_unknown_vat_quoted_at_the_price_charged(self):
        # Nugttah: when vat_inclusive is null, the price is what the customer sees and is charged.
        out = self.find(available_at=at("mon", "13:00"))
        kunafa = [r for r in out["results"] if r["name"] == "Kunafa Tray"][0]
        self.assertEqual((kunafa["price"]["amount"], kunafa["price"]["vat"]), (95.0, "unknown"))
        self.assertTrue(kunafa["quote_id"])
        with mock.patch.dict(os.environ, {"TRUEMENU_VAT_UNKNOWN": "refuse"}):
            kunafa = [r for r in self.find(available_at=at("mon", "13:00"))["results"] if r["name"] == "Kunafa Tray"][0]
        self.assertIsNone(kunafa["quote_id"])

    def test_paid_required_choice_is_not_quoted(self):
        out = self.find(available_at=at("mon", "13:00"))
        box = [r for r in out["results"] if r["name"] == "Grilled Chicken Box"][0]
        self.assertEqual(box["price"]["required_choices_add"], 4.0)
        self.assertIsNone(box["quote_id"])
        plate = [r for r in out["results"] if r["name"] == "Grilled Chicken Plate"][0]
        self.assertTrue(plate["quote_id"])              # its required sauce has a free option

    def test_deleted_and_blocked_category_items_not_offered(self):
        names = [r["name"] for r in self.find(available_at=at("mon", "13:00"))["results"]]
        self.assertNotIn("Garlic Bread", names)           # deleted: true
        self.assertNotIn("Shawarma Wrap", names)          # its category is not available

    def test_test_branch_skipped(self):
        with db.connect() as con:
            self.assertIsNone(con.execute("SELECT 1 FROM merchants WHERE merchant_id='ng-1003'").fetchone())
        self.assertEqual(nugttah.load_state()["excluded_branches"], ["Sample Cafe Test Branch"])

    def test_first_sync_stores_a_cursor(self):
        st = nugttah.load_state()
        self.assertEqual(st["cursor"], "c0")
        self.assertEqual(st["menu_queue"], [])
        self.assertTrue(st.get("first_sync_done_at"))


class NugttahChangesTest(unittest.TestCase):
    """The change feed: deltas update items without delisting the rest of the menu."""

    @classmethod
    def setUpClass(cls):
        cls._db, cls._state = config.DB_PATH, config.STATE_DIR
        config.STATE_DIR = Path(tempfile.mkdtemp(prefix="truemenu-ngc-"))
        config.DB_PATH = config.STATE_DIR / "truemenu.db"
        nugttah.sync_all()
        st = nugttah.load_state()
        st["cursor"] = "c1"                              # the next /changes page holds three changes
        nugttah.save_state(st)
        cls.events = nugttah.tick()

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH, config.STATE_DIR = cls._db, cls._state

    def find(self):
        return query.find_items({"near": "Riyadh", "limit": 25, "available_at": at("mon", "13:00")}, "test")

    def test_price_change_applied(self):
        r = [x for x in self.find()["results"] if x["name"] == "Grilled Chicken Plate"][0]
        self.assertEqual(r["price"]["amount"], 42.0)
        self.assertIn({"item_id": "ng-1001-9001", "event": "price", "from": 39.0, "to": 42.0}, self.events)

    def test_unavailable_item_removed_rest_kept(self):
        names = [x["name"] for x in self.find()["results"]]
        self.assertNotIn("Mixed Grill with Rice", names)
        self.assertIn("Fattoush", names)                 # a delta never delists items it does not mention

    def test_status_change_uses_server_time(self):
        with db.connect() as con:
            m = con.execute("SELECT pos_status, pos_status_at FROM merchants WHERE merchant_id='ng-1002'").fetchone()
        self.assertEqual((m["pos_status"], m["pos_status_at"]), ("online", "2026-11-18T12:00:20.000Z"))
        self.assertIn("Halloumi Sandwich", [x["name"] for x in self.find()["results"]])

    def test_older_record_ignored(self):
        r = [x for x in self.find()["results"] if x["name"] == "Fattoush"][0]
        self.assertEqual(r["price"]["amount"], 18.0)      # the 2026-11-01 copy at 1.00 SAR was older than ours

    def test_deleted_item_on_second_page(self):
        self.assertNotIn("Lamb Mandi", [x["name"] for x in self.find()["results"]])

    def test_cursor_advanced_and_feed_counts_as_fresh(self):
        self.assertEqual(nugttah.load_state()["cursor"], "c2")
        fr = [x for x in self.find()["results"] if x["name"] == "Fattoush"][0]["freshness"]
        self.assertEqual(fr["label"], "fresh")
        self.assertEqual(fr["merchant_data_as_of"], "2026-11-18T12:00:10.000Z")   # newest edit at that branch



class NugttahRecoveryTest(unittest.TestCase):
    """A rejected cursor starts a full resync; a branch missing from /branches leaves the pilot."""

    @classmethod
    def setUpClass(cls):
        cls._db, cls._state = config.DB_PATH, config.STATE_DIR
        config.STATE_DIR = Path(tempfile.mkdtemp(prefix="truemenu-ngr-"))
        config.DB_PATH = config.STATE_DIR / "truemenu.db"
        nugttah.sync_all()

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH, config.STATE_DIR = cls._db, cls._state

    def test_empty_hours_means_closed(self):
        # Nugttah: a branch with no opening hours is closed.
        hours = nugttah._hours([])
        m = {"tz": "Asia/Riyadh", "hours_json": json.dumps(hours), "pos_status": "online", "hours_verified": 1}
        self.assertFalse(query.branch_open(m, datetime.fromisoformat(at("mon", "13:00")))["open"])

    def test_rejected_cursor_resyncs(self):
        st = nugttah.load_state()
        st["cursor"] = "expired"
        nugttah.save_state(st)
        nugttah.tick()
        st = nugttah.load_state()
        self.assertEqual(st["cursor"], "c0")
        self.assertTrue(st.get("resync_done_at"))

    def test_branch_left_pilot(self):
        real = nugttah._get

        def without_1002(path, params=None):
            out = real(path, params)
            if path == "/branches":
                out = dict(out, data=[b for b in out["data"] if b["branch_id"] != "1002"])
            return out
        st = nugttah.load_state()
        events = []
        with mock.patch.object(nugttah, "_get", without_1002):
            nugttah._refresh_branches(st, events)
        self.assertIn({"merchant_id": "ng-1002", "event": "left_pilot", "count": 1}, events)


if __name__ == "__main__":
    unittest.main()
