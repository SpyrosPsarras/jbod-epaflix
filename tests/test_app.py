"""Seam 1: drive the app from outside with a fake Source network layer and a real Postgres."""
import base64
import html as htmllib
import json
import pathlib
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

import pgserver

from dealfinder.app import App
from dealfinder.costs import DailyFx
from dealfinder.sources import EbaySource, FinnSource

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FIXTURE = json.loads((FIXTURES / "ebay_disk_search.json").read_text())
FINN = json.loads((FIXTURES / "finn.json").read_text())
FINN_DISK_QUERIES = ["exos 16tb", "ultrastar 16tb"]
FINN_MACHINE_QUERIES = ["r730xd", "r730", "r740", "r720xd", "dl380 gen9", "supermicro server"]
RATES = {"NOK": 1.0, "GBP": 12.59, "USD": 9.43, "EUR": 11.0}

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


def fake_finn_fetch(url):
    """Replays recorded finn.no data in the same shape the real pages carry it."""
    if "/search?" in url:
        docs = FINN["search"].get(url.split("?", 1)[1], [])
        blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
        return f"<html><script>{blob}</script></html>"
    item = FINN["items"][url.rstrip("/").rsplit("/", 1)[1]]
    paragraphs = "".join(f"<p>{htmllib.escape(line)}</p>" for line in (item["description"] or "").split("\n"))
    return f'<section data-testid="description"><div class="whitespace-pre-wrap">{paragraphs}</div></section>'


class HuntToPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
        sources = [EbaySource("id", "secret", fetch=fake_fetch), FinnSource(fetch=fake_finn_fetch, pause=0)]
        cls.app = App(cls.pg.get_uri(), sources, fx=RATES.__getitem__,
                      disk_queries={"ebay_uk": [*FIXTURE, "hostile"], "finn": FINN_DISK_QUERIES},
                      machine_queries=FINN_MACHINE_QUERIES, pause=0)
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

    def machines(self):
        _, html = self.get("/")
        return [(i, float(n)) for i, n in re.findall(r'data-machine="([^"]+)" data-landed="([\d.]+)"', html)]

    def unreadable(self):
        _, html = self.get("/")
        return dict(re.findall(r'data-unreadable="([^"]+)" data-missing="([^"]*)"', html))

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
        qualified = re.search(r'dealfinder_listings\{source="ebay_uk",kind="disk",state="qualified"\} (\d+)', text)
        self.assertIsNotNone(qualified, text)
        self.assertGreater(int(qualified.group(1)), 5)
        self.assertIn("dealfinder_hunt_last_success_timestamp_seconds", text)


    def test_finn_machines_that_fit_the_rules_are_ranked_by_price(self):
        machines = self.machines()
        shown = {i for i, _ in machines}
        for fid in ("468970308", "473386139", "475664047"):  # Trondheim R730xd, Oslo R730xd, Nedenes R730
            self.assertIn(fid, shown)
        prices = [p for _, p in machines]
        self.assertEqual(prices, sorted(prices))

    def test_finn_machines_that_break_the_rules_are_not_ranked(self):
        shown = {i for i, _ in self.machines()}
        for fid in ("469749680",   # R720xd, 12th Gen
                    "426464076",   # R730 with a 2.5" backplane
                    "219121915",   # R730xd 24x 2.5"
                    "468742286",   # riser board for R740
                    "468806731"):  # Scania R730 toy truck
            self.assertNotIn(fid, shown)

    def test_unreadable_machines_are_listed_with_the_missing_facts(self):
        unreadable = self.unreadable()
        self.assertEqual(unreadable.get("476154464"), "bays_35")  # R740, "8x2TB SAS" of unknown size
        self.assertEqual(unreadable.get("455107692"), "bays_35")  # DL380 Gen9, never says LFF or SFF
        self.assertNotIn("468742286", unreadable)  # parts are not machines, so never "could not read"

    def test_finn_prices_carry_no_import_vat(self):
        _, html = self.get("/")
        for fid, landed in self.machines():
            doc = next(d for docs in FINN["search"].values() for d in docs if str(d["id"]) == fid)
            self.assertAlmostEqual(landed, float(doc["price"]["amount"]), places=2)


class DailyExchangeRate(unittest.TestCase):
    def test_fetches_once_per_currency_per_day(self):
        calls = []
        fx = DailyFx(fetch=lambda url: calls.append(url) or {"rates": {"NOK": 12.5}})
        self.assertEqual((fx("GBP"), fx("GBP"), fx("NOK")), (12.5, 12.5, 1.0))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class FinnSourceRobustness(unittest.TestCase):
    """One bad doc or one dead item page must not cost the rest of the finn.no Listings."""

    def test_malformed_doc_and_dead_item_page_are_skipped(self):
        docs = [
            "not-a-dict",
            {"id": 1, "heading": "Dell PowerEdge R730xd 12x LFF", "trade_type": "Til salgs", "price": {"amount": 5000},
             "coordinates": [63.4, 10.4], "canonical_url": "https://www.finn.no/recommerce/forsale/item/1"},
            {"id": 2, "heading": "Dell PowerEdge R730 8-Bay LFF", "trade_type": "Til salgs", "price": {"amount": 9000},
             "canonical_url": "file:///etc/passwd"},
            {"id": 3, "heading": "Dell PowerEdge R730xd 12x LFF", "trade_type": "Til salgs", "price": {"amount": 7000},
             "canonical_url": "https://www.finn.no/recommerce/forsale/item/3"},
        ]
        blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
        fetched = []

        def fetch(url):
            fetched.append(url)
            if "/search?" in url:
                return f"<script>{blob}</script>"
            if url.endswith("/3"):
                raise urllib.error.HTTPError(url, 404, "gone", {}, None)
            return '<section data-testid="description"><p>12x 3.5" LFF</p></section>'

        listings = {l.source_id: l for l in FinnSource(fetch=fetch, pause=0).search("r730", details=True)}
        self.assertEqual(sorted(listings), ["2", "3"])            # the string and the list-coordinates doc are skipped
        self.assertIsNone(listings["2"].description)              # a non-finn URL is never fetched
        self.assertIsNone(listings["3"].description)              # a dead page leaves the description empty
        self.assertFalse(any(u.startswith("file:") for u in fetched))
