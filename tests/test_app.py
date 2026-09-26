"""Seam 1: drive the app from outside with a fake Source network layer and a real Postgres."""
import json
import pathlib
import re
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

import pgserver

from dealfinder.app import App
from dealfinder.costs import DailyFx
from dealfinder.sources import EbaySource

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "ebay_disk_search.json").read_text())
RATES = {"GBP": 12.59, "USD": 9.43, "EUR": 11.0}

# a qualifying Listing carrying hostile text, to prove the page escapes Source data
HOSTILE = {"itemSummaries": [{
    "itemId": "v1|hostile|0", "title": 'Seagate Exos X16 16TB 3.5" enterprise HDD <script>alert(1)</script>',
    "price": {"value": "100.00", "currency": "GBP"},
    "shippingOptions": [{"shippingCost": {"value": "10.00", "currency": "GBP"}}],
    "conditionId": "3000", "itemWebUrl": "javascript:alert(1)", "seller": {"username": "x"}}]}

# genuine-looking titles that the rules must keep off the page
NEVER_RANKED = re.compile(r"elements|brake|compatible|suitable for|fits? for|for (toshiba|seagate)\b|pcb", re.I)


def fake_fetch(url, headers=None, data=None):
    if "oauth2/token" in url:
        return {"access_token": "test-token", "expires_in": 7200}
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
    return HOSTILE if q == "hostile" else FIXTURE.get(q, {"itemSummaries": []})


class HuntToPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
        source = EbaySource("id", "secret", fetch=fake_fetch)
        cls.app = App(cls.pg.get_uri(), [source], fx=RATES.__getitem__, queries=[*FIXTURE, "hostile"], pause=0)
        cls.server = cls.app.make_server("127.0.0.1", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.app.hunt()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.pg.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.read().decode()

    def rows(self):
        status, html = self.get("/")
        self.assertEqual(status, 200)
        return [(i, float(n)) for i, n in re.findall(r'data-listing="([^"]+)" data-nok-per-tb="([\d.]+)"', html)]

    def all_listings(self):
        return [it for v in FIXTURE.values() for it in v["itemSummaries"]]

    def test_best_disks_sorted_by_nok_per_tb(self):
        rows = self.rows()
        self.assertGreater(len(rows), 5)
        values = [n for _, n in rows]
        self.assertEqual(values, sorted(values))

    def test_broken_external_relabelled_and_offtopic_listings_never_ranked(self):
        shown = {i for i, _ in self.rows()}
        for it in self.all_listings():
            if it.get("conditionId") == "7000" or NEVER_RANKED.search(it["title"]):
                self.assertNotIn(it["itemId"], shown, it["title"])

    def test_landed_cost_includes_shipping_vat_and_fx(self):
        item = next(it for it in FIXTURE["exos 16tb"]["itemSummaries"]
                    if it["title"].startswith("Seagate Exos X18") and it.get("conditionId") == "3000")
        ship = float(item["shippingOptions"][0]["shippingCost"]["value"])
        landed = (float(item["price"]["value"]) + ship) * RATES["GBP"] * 1.25
        rows = dict(self.rows())
        self.assertAlmostEqual(rows[item["itemId"]], round(landed / 16, 2), places=1)

    def test_page_escapes_source_text_and_blocks_non_https_links(self):
        _, html = self.get("/")
        self.assertIn("v1|hostile|0", html)  # the hostile Listing qualifies and is shown...
        self.assertNotIn("<script>alert(1)</script>", html)  # ...but escaped
        self.assertNotIn('href="javascript:', html)

    def test_metrics_count_qualified_and_unreadable(self):
        status, text = self.get("/metrics")
        self.assertEqual(status, 200)
        qualified = re.search(r'dealfinder_listings\{source="ebay_uk",state="qualified"\} (\d+)', text)
        self.assertIsNotNone(qualified, text)
        self.assertGreater(int(qualified.group(1)), 5)
        self.assertIn("dealfinder_hunt_last_success_timestamp_seconds", text)


class DailyExchangeRate(unittest.TestCase):
    def test_fetches_once_per_currency_per_day(self):
        calls = []
        fx = DailyFx(fetch=lambda url: calls.append(url) or {"rates": {"NOK": 12.5}})
        self.assertEqual((fx("GBP"), fx("GBP"), fx("NOK")), (12.5, 12.5, 1.0))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
