"""Booth limits at a venue: many phones share one Wi-Fi address, so limits must be per visitor.

  python3 -m unittest tests.test_booth -v      (needs fastapi and httpx)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRUEMENU_STATE", tempfile.mkdtemp(prefix="truemenu-booth-"))
os.environ.setdefault("TRUEMENU_QUOTE_SECRET", "test-secret-not-used-anywhere-else")

try:
    from fastapi.testclient import TestClient
    from truemenu import booth, db, server
except (ImportError, RuntimeError) as e:      # pragma: no cover  (newer starlette raises RuntimeError without its http client)
    raise unittest.SkipTest(f"fastapi test client not installed: {e}")

VENUE = {"x-forwarded-for": "18.0.0.1"}       # one public address for the whole room


class BoothVenueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        cls.c = TestClient(server.app)

    def setUp(self):
        booth._hits.clear()
        server._hits.clear()
        booth._status_cache["body"] = None

    def get_status(self, visitor=None):
        h = dict(VENUE, **({"x-visitor": visitor} if visitor else {}))
        return self.c.get("/demo/api/status", headers=h).status_code

    def test_room_of_phones_on_one_address(self):
        # 40 phones, each polling a minute's worth (every 5 s = 12 calls): 480 calls from one address
        codes = [self.get_status(f"visitor-{n:04d}-abcd") for n in range(40) for _ in range(12)]
        self.assertEqual(codes.count(200), len(codes))

    def test_one_visitor_is_still_limited(self):
        codes = [self.get_status("visitor-greedy-0001") for _ in range(70)]
        self.assertEqual(codes.count(200), 60)
        self.assertEqual(codes[-1], 429)
        self.assertEqual(self.get_status("visitor-polite-0002"), 200)     # others on the address are not affected

    def test_without_visitor_id_the_address_is_the_limit(self):
        codes = [self.get_status() for _ in range(65)]
        self.assertEqual(codes.count(200), 60)

    def test_bad_visitor_id_is_treated_as_none(self):
        codes = [self.get_status("x") for _ in range(61)]
        self.assertEqual(codes[-1], 429)

    def test_status_is_cached_but_events_show_at_once(self):
        first = self.c.get("/demo/api/status", headers=VENUE).json()
        booth._event("reset", "test event")
        second = self.c.get("/demo/api/status", headers=VENUE).json()
        self.assertNotEqual(first["events"], second["events"])
        self.assertEqual(second["events"][0]["text"], "test event")


if __name__ == "__main__":
    unittest.main()


class BoothRecheckEndToEndTest(unittest.TestCase):
    """The same six cases scripts/verify_live.py runs on the server, through the app in-process."""

    def test_every_change_after_the_quote_blocks_payment(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import verify_live
        from truemenu import feed
        c = TestClient(server.app)
        visitor = {"x-visitor": "verify-test-0001", **VENUE}

        def post(path, body):
            booth._hits.clear(); server._hits.clear()
            return c.post(path, json=body, headers=visitor).json()

        def make_stale(mid):                      # instead of waiting two sync intervals
            with db.connect() as con:
                con.execute("UPDATE sources SET last_pull_at='2000-01-01T00:00:00+00:00' WHERE merchant_id=?", (mid,))

        feed._save_overrides({"prices": {}, "status": {}, "paused_sources": [], "sold_out": []})
        res = verify_live.run(post, make_stale=make_stale, say=lambda *_: None)
        self.assertEqual([c for c, _, _ in res], verify_live.CASES)
        for case, ok, detail in res:
            self.assertTrue(ok, f"{case}: {detail}")
        self.assertEqual(feed._load_overrides()["sold_out"], [])          # overrides put back
