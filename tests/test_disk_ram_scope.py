import tempfile
import threading
import unittest
import urllib.request

import pgserver

from dealfinder.app import App
from dealfinder.store import Store
from dealfinder.sources import EbaySource, Listing


class DiskRamScope(unittest.TestCase):
    def setUp(self):
        self.pg = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
        self.addCleanup(self.pg.cleanup)

    def app(self):
        app = App(self.pg.get_uri(), [], fx=lambda _: 1, pause=0)
        self.addCleanup(app.store._pool.close)
        return app

    def test_existing_queries_gain_new_defaults_once_without_restoring_removed_queries(self):
        store = Store(self.pg.get_uri())
        self.addCleanup(store._pool.close)
        store.migrate()
        store.seed_tracked([("exos 14tb", "disk", "ebay_uk"), ("exos 16tb", "disk", "ebay_uk"),
                            ("exos", "disk", "finn")])
        with store._conn() as c:
            c.execute("DELETE FROM tracked_queries WHERE query = 'exos 14tb'")
        app = self.app()
        queries = {(r["query"], r["source"]) for r in app.store.tracked()}
        for query, source in (("exos 12tb", "ebay_uk"), ("wd purple 12tb", "ebay_uk"),
                              ("barracuda 12tb", "ebay_uk"), ("12tb", "finn"), ("purple", "finn")):
            self.assertIn((query, source), queries)
        self.assertNotIn(("exos 14tb", "ebay_uk"), queries)
        with store._conn() as c:
            c.execute("DELETE FROM tracked_queries WHERE query = 'wd purple 12tb'")
        self.app()
        self.assertNotIn("wd purple 12tb", [r["query"] for r in store.tracked()])

    def test_old_kinds_are_not_hunted_shown_or_counted(self):
        class Source:
            name = "finn"
            foreign = False

            def search(self, query, kind):
                if kind not in ("disk", "ram"):
                    raise AssertionError(kind)
                if kind == "disk":
                    return [Listing("finn", "current", '12TB SATA HDD 3.5"', "https://www.finn.no/current",
                                    2000, "NOK", 0, "NOK", "used", None)]
                return []

        app = self.app()
        app.sources = [Source()]
        with app.store._conn() as c:
            for kind in ("machine", "cpu", "heatsink"):
                c.execute("INSERT INTO tracked_queries (query, kind) VALUES ('old', %s)", (kind,))
                c.execute("INSERT INTO listings (source, source_id, kind, title, url, price, currency, missing) "
                          "VALUES ('finn', %s, %s, 'obsolete listing', 'https://example.com', 1, 'NOK', ARRAY['old'])",
                          (kind, kind))
        app.hunt()
        self.assertTrue(app.store.last_hunt()["ok"])
        with app.store._conn() as c:
            c.execute("UPDATE listings SET last_seen = now()")
        for page in (app.page(), app.history_page()):
            for text in ("Best Machines", "Best CPUs", "Heatsinks", "Builds", "Completed NOK", "obsolete listing",
                         "Machines, NOK", "CPUs, NOK", "Best Build"):
                self.assertNotIn(text, page)
        for text in ('kind="machine"', 'kind="cpu"', 'kind="heatsink"', "dealfinder_best_build", "dealfinder_builds_hidden"):
            self.assertNotIn(text, app.metrics())

    def test_purple_lot_is_ranked_per_12tb_disk(self):
        class Source:
            name = "finn"
            foreign = False

            def search(self, query, kind):
                if kind != "disk":
                    return []
                return [Listing("finn", "purple", "60 TB lagring | 5x 12TB Western Digital Purple disker | WD121PURZ",
                                "https://www.finn.no/purple", 10500, "NOK", 0, "NOK", "used", None)]

        app = self.app()
        app.sources = [Source()]
        app.hunt()
        row, = app.store.best_disks()
        self.assertEqual(row["count"], 5)
        self.assertEqual(row["capacity_tb"], 12)
        self.assertAlmostEqual(float(row["nok_per_tb"]), 185.98, places=2)

    def test_bought_stops_all_hunts_and_survives_restart(self):
        class Source:
            name = "finn"
            foreign = False

            def search(self, query, kind):
                raise AssertionError("Bought must stop every Source call")

        app = self.app()
        app.sources = [Source()]
        server = app.make_server("127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/healthz") as response:
            self.assertEqual(response.read(), b"ok\n")
        with urllib.request.urlopen(urllib.request.Request(base + "/buy", data=b"", method="POST")) as response:
            self.assertIn("Hunting has stopped for good", response.read().decode())
        self.assertFalse(app.mark_bought())
        self.assertFalse(app.start_hunt())
        app.hunt()
        restarted = self.app()
        restarted.sources = [Source()]
        restarted.hunt()
        self.assertFalse(restarted.start_hunt())
        self.assertIsNone(restarted.store.last_started())
        self.assertIn("dealfinder_bought 1\n", restarted.metrics())
        self.assertNotIn("Mark as bought", restarted.page())

    def test_legacy_bought_record_still_stops_hunts_without_build_sections(self):
        app = self.app()
        with app.store._conn() as c:
            c.execute("INSERT INTO purchases (build) VALUES ('{\"machine\": {\"title\": \"old machine\"}}')")
        self.assertFalse(app.start_hunt())
        self.assertIn("Hunting has stopped for good", app.page())
        self.assertNotIn("old machine", app.page())

    def test_disk_pickup_rules_keep_near_and_shipped_but_reject_far_or_unknown_places(self):
        app = self.app()
        app.router = lambda lat, lon: (100, 90) if lat < 60 else (600, 500)
        source = type("Source", (), {"foreign": False})()
        for lat, pickup, shipping, qualifies, missing, landed in (
                (59, True, None, True, [], 2800),
                (63, True, None, False, [], None),
                (None, True, None, False, ["location"], None),
                (63, False, 0, True, [], 2149)):
            with self.subTest(lat=lat, pickup=pickup):
                listing = Listing("finn", "disk", '12TB SATA HDD 3.5"', "https://www.finn.no/disk",
                                  2000, "NOK", shipping, "NOK", "used", None,
                                  lat=lat, lon=10 if lat is not None else None, pickup_only=pickup)
                scored = app._score(source, listing, "disk")
                self.assertEqual(scored["qualifies"], qualifies)
                self.assertEqual(scored["missing"], missing)
                self.assertEqual(scored["landed_nok"], landed)

    def test_weak_and_unknown_ebay_sellers_keep_the_surcharge_on_disks(self):
        app = self.app()
        for seller, expected in (({"feedbackPercentage": "99", "feedbackScore": 500}, 1500),
                                 ({"feedbackPercentage": "97", "feedbackScore": 500}, 1650),
                                 ({"feedbackPercentage": "99", "feedbackScore": 10}, 1650), ({}, 1650)):
            with self.subTest(seller=seller):
                source = EbaySource("id", "secret")
                listing = source._listing({"itemId": "disk", "title": '12TB SAS HDD 3.5"',
                                           "price": {"value": "100", "currency": "GBP"},
                                           "shippingOptions": [{"shippingCost": {"value": "20", "currency": "GBP"}}],
                                           "conditionId": "3000", "seller": seller})
                app.fx = lambda _: 10
                scored = app._score(source, listing, "disk")
                self.assertTrue(scored["qualifies"])
                self.assertEqual(scored["landed_nok"], expected)
