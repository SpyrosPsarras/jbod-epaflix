"""Seam 1: drive the app from outside with a fake Source network layer and a real Postgres."""
import base64
import datetime
import html as htmllib
import http.client
import json
import pathlib
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

import pgserver

from dealfinder.app import App, next_hunt_delay
from dealfinder.config import PICKUP_NOK_PER_KM
from dealfinder.costs import DailyFx, cost_breakdown
from dealfinder.rules import Unreadable, read_disk
from dealfinder.sources import EbaySource, FinnSource

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FIXTURE = json.loads((FIXTURES / "ebay_disk_search.json").read_text())
FINN = json.loads((FIXTURES / "finn.json").read_text())
FINN_DISK_QUERIES = ["exos 16tb", "ultrastar 16tb"]
RATES = {"NOK": 1.0, "GBP": 12.59, "USD": 9.43, "EUR": 11.0}


def _fee(price):
    """finn.no Trygg betaling (est.) on a Listing bought via Fiks ferdig: 29 NOK + 6% of the price."""
    return 29 + 0.06 * price


def _shipped(price):
    """Landed NOK of a finn.no Listing with free shipping (seller_pays_shipping): price + Trygg betaling."""
    return price + _fee(price)

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
        return {"access_token": "[REDACTED:Generic Password Field]", "expires_in": 7200}
    if "/item/" in url:
        return {}  # a Disk's item page without stock: one disk
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
    return HOSTILE if q == "hostile" else FIXTURE.get(q, {"itemSummaries": []})


def fake_router(lat, lon):
    """Road km/minutes from Sandefjord, recorded from OSRM for the fixture places."""
    if lat > 62:
        return 609.0, 547.0   # Trondheim area
    if lat < 58.5:
        return 215.0, 185.0   # Kristiansand
    return 119.0, 96.0        # Oslo area


def fake_finn_fetch(url):
    """Replays recorded finn.no data in the same shape the real pages carry it."""
    if "/search?" in url:
        docs = FINN["search"].get(url.split("?", 1)[1], [])
        blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
        return f"<html><script>{blob}</script></html>"
    item = FINN["items"].get(url.rstrip("/").rsplit("/", 1)[1], {"description": ""})
    paragraphs = "".join(f"<p>{htmllib.escape(line)}</p>" for line in (item["description"] or "").split("\n"))
    return f'<section data-testid="description"><div class="whitespace-pre-wrap">{paragraphs}</div></section>'


class HuntToPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
        sources = [EbaySource("id", "secret", fetch=fake_fetch), FinnSource(fetch=fake_finn_fetch, pause=0)]
        cls.app = App(cls.pg.get_uri(), sources, fx=RATES.__getitem__,
                      disk_queries={"ebay_uk": [*FIXTURE, "hostile"], "finn": FINN_DISK_QUERIES},
                      pause=0, router=fake_router)
        cls.server = cls.app.make_server("127.0.0.1", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.app.hunt()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
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

    def test_no_deals_without_earlier_weeks(self):
        _, html = self.get("/")
        self.assertGreater(len(self.rows()), 5)
        self.assertNotIn('data-deal="1"', html)

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
        self.assertIn('dealfinder_source_last_success_timestamp_seconds{source="ebay_uk"}', text)
        self.assertIn("dealfinder_hunt_duration_seconds", text)
        self.assertRegex(text, r"dealfinder_hunt_last_success_timestamp_seconds [1-9][\d.]+")


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
            {"id": 1, "heading": "4x 16GB DDR4 ECC RDIMM", "trade_type": "Til salgs", "price": {"amount": 5000},
             "coordinates": [63.4, 10.4], "canonical_url": "https://www.finn.no/recommerce/forsale/item/1"},
            {"id": 2, "heading": "4x 16GB DDR4 ECC RDIMM", "trade_type": "Til salgs", "price": {"amount": 9000},
             "canonical_url": "file:///etc/passwd"},
            {"id": 3, "heading": "4x 16GB DDR4 ECC RDIMM", "trade_type": "Til salgs", "price": {"amount": 7000},
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

        listings = {l.source_id: l for l in FinnSource(fetch=fetch, pause=0).search("rdimm", "ram")}
        self.assertEqual(sorted(listings), ["2", "3"])            # the string and the list-coordinates doc are skipped
        self.assertIsNone(listings["2"].description)              # a non-finn URL is never fetched
        self.assertIsNone(listings["3"].description)              # a dead page leaves the description empty
        self.assertFalse(any(u.startswith("file:") for u in fetched))


class RebuildItStock(unittest.TestCase):
    """A Rebuild IT finn ad its web shop shows sold out is dropped (so it goes Gone); the rest ship from the shop."""

    def test_sold_out_dropped_in_stock_ships(self):
        def ad(i, heading, price, shop="Rebuild IT"):
            return {"id": i, "heading": heading, "trade_type": "Til salgs", "price": {"amount": price},
                    "organisation_name": shop, "coordinates": {"lat": 59.8, "lon": 10.4}}
        docs = [ad(1, 'WD Gold Enterprise-Class 18TB 3.5" HDD', 3333),     # sold out in the shop
                ad(2, "WUH721818ALE6L4  WD Ultrastar 18TB", 3499),         # in stock (one of two variants)
                ad(3, "Seagate Exos 16TB", 800),                           # in stock, under the free-shipping total
                ad(4, "Toshiba MG08 16TB", 3000),                          # not in the shop: kept
                ad(5, 'WD Gold Enterprise-Class 18TB 3.5" HDD', 3333, None)]  # a private seller: untouched
        blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
        shop = [{"title": 'WD Gold Enterprise-Class 18TB 3.5" HDD', "variants": [{"available": False}]},
                {"title": 'WD Gold Enterprise-Class 18TB 3.5" HDD', "variants": [{"available": False}]},
                {"title": "wuh721818ale6l4 wd ultrastar 18TB", "variants": [{"available": False}, {"available": True}]},
                {"title": "Seagate Exos 16TB", "variants": [{"available": True}]}]
        pages = []

        def fetch(url):
            if "rebuildit.no" in url:
                pages.append(url)
                return json.dumps({"products": shop if url.endswith("page=1") else []})
            return f"<script>{blob}</script>" if "/search?" in url else ""

        source = FinnSource(fetch=fetch, pause=0)
        listings = {l.source_id: l for l in source.search("18tb", "ram")}
        self.assertEqual(sorted(listings), ["2", "3", "4", "5"])
        self.assertEqual((listings["2"].shipping, listings["2"].pickup_only), (0.0, False))
        self.assertEqual((listings["3"].shipping, listings["3"].pickup_only), (None, False))  # Fiks ferdig estimate
        self.assertFalse(listings["4"].pickup_only)
        self.assertTrue(listings["5"].pickup_only)
        source.search("16tb", "ram")
        self.assertEqual(len(pages), 2)  # page 1 and the empty page 2, read once for both searches
        # bought in the web shop: no Trygg betaling fee, shipped or not; a private seller shipping pays it
        costs = {i: cost_breakdown(listings[i], lambda c: 1.0, False, lambda lat, lon: (120, 80), "disk")[0]
                 for i in ("2", "3")}
        self.assertEqual(costs["2"]["total"], 3499)
        self.assertEqual(costs["3"]["total"], 865)  # 800 + the 65 NOK shipping estimate
        listings["5"].shipping = 0.0
        self.assertEqual(cost_breakdown(listings["5"], lambda c: 1.0, False, lambda lat, lon: (120, 80),
                                        "disk")[0]["finn_fee"], 228.98)  # 29 + 6% of 3,333

    def test_unreadable_shop_drops_nothing(self):
        doc = {"id": 1, "heading": "Exos 16TB", "trade_type": "Til salgs", "price": {"amount": 3000},
               "organisation_name": "Rebuild IT"}
        blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": [doc]}}}]}).encode()).decode()

        def fetch(url):
            if "rebuildit.no" in url:
                raise urllib.error.URLError("down")
            return f"<script>{blob}</script>"

        self.assertEqual([l.source_id for l in FinnSource(fetch=fetch, pause=0).search("exos", "ram")], ["1"])


class _BrokenSource:
    name, foreign = "broken", False

    def search(self, query, kind="disk"):
        raise RuntimeError("upstream page changed")


class _EmptySource:
    name, foreign = "empty", False

    def search(self, query, kind="disk"):
        return []


class _GatedSource(_EmptySource):
    """Holds a Hunt open until the test releases the gate."""
    name = "gated"

    def __init__(self):
        self.gate = threading.Event()

    def search(self, query, kind="disk"):
        self.gate.wait(30)
        return []


def _pg():
    return pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")


class SourceFaults(unittest.TestCase):
    """A failing or empty Source raises the banner while the other Sources still rank."""

    @classmethod
    def setUpClass(cls):
        cls.pg = _pg()
        sources = [EbaySource("id", "secret", fetch=fake_fetch), _BrokenSource(), _EmptySource()]
        cls.app = App(cls.pg.get_uri(), sources, fx=RATES.__getitem__,
                      disk_queries={"ebay_uk": list(FIXTURE), "broken": ["x"], "empty": ["x"]}, pause=0)
        cls.app.hunt()

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def test_banner_names_the_failing_and_the_empty_source(self):
        page = self.app.page()
        self.assertRegex(page, r'data-fault="broken">Source fault: <b>broken</b> failed: upstream page changed')
        self.assertRegex(page, r'data-fault="empty">Source fault: <b>empty</b> returned no Listings[^<]*It has no Listings yet')
        self.assertNotIn('data-fault="ebay_uk"', page)

    def test_healthy_source_still_ranks(self):
        self.assertGreater(len(re.findall(r'data-listing="[^"]+" data-nok-per-tb=', self.app.page())), 5)

    def test_hunt_records_start_end_and_per_source_result(self):
        last = self.app.store.last_hunt()
        self.assertLess(last["started"], last["finished"])
        self.assertEqual(sorted(last["detail"]), ["broken", "ebay_uk", "empty"])
        self.assertFalse(last["detail"]["broken"]["ok"])
        self.assertTrue(last["detail"]["ebay_uk"]["ok"])
        self.assertGreater(last["detail"]["ebay_uk"]["disk"], 5)

    def test_metrics_show_source_faults_and_counts(self):
        text = self.app.metrics()
        self.assertIn('dealfinder_source_up{source="broken"} 0', text)
        self.assertIn('dealfinder_source_up{source="empty"} 0', text)
        self.assertIn('dealfinder_source_up{source="ebay_uk"} 1', text)
        self.assertRegex(text, r'dealfinder_source_listings\{source="ebay_uk"\} [1-9]\d*')
        self.assertRegex(text, r"dealfinder_hunt_duration_seconds [\d.]+")


class HuntNowAndLock(unittest.TestCase):
    """'Hunt now' starts a Hunt; a second request while it runs is ignored with a message."""

    @classmethod
    def setUpClass(cls):
        cls.pg = _pg()
        cls.source = _GatedSource()
        cls.app = App(cls.pg.get_uri(), [cls.source], fx=RATES.__getitem__, disk_queries={"gated": ["x"]}, pause=0)
        cls.server = cls.app.make_server("127.0.0.1", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.source.gate.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.pg.cleanup()

    def post_hunt(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        try:
            conn.request("POST", "/hunt")
            resp = conn.getresponse()
            return resp.status, resp.getheader("Location")
        finally:
            conn.close()

    def test_second_request_is_ignored_while_a_hunt_runs(self):
        self.assertEqual(self.post_hunt(), (303, "/?hunt=started"))
        self.assertTrue(self.app.hunt_running())
        self.assertEqual(self.post_hunt(), (303, "/?hunt=busy"))
        with urllib.request.urlopen("http://127.0.0.1:%d/?hunt=busy" % self.server.server_address[1]) as r:
            page = r.read().decode()
        self.assertIn('data-notice="busy"', page)
        self.assertIn("A Hunt is running now.", page)
        self.source.gate.set()
        for _ in range(100):
            if not self.app.hunt_running():
                break
            threading.Event().wait(0.05)
        self.assertFalse(self.app.hunt_running())
        self.assertEqual(sorted(self.app.store.last_hunt()["detail"]), ["gated"])  # exactly one Hunt ran
        with self.app.store._conn() as c:
            self.assertEqual(len(c.execute("SELECT id FROM hunts").fetchall()), 1)


class Scheduler(unittest.TestCase):
    def test_next_hunt_is_due_one_interval_after_the_last(self):
        now = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(next_hunt_delay(None, now, 6 * 3600), 0)
        self.assertEqual(next_hunt_delay(now - datetime.timedelta(hours=7), now, 6 * 3600), 0)
        self.assertEqual(next_hunt_delay(now - datetime.timedelta(hours=2), now, 6 * 3600), 4 * 3600)

    def test_a_first_start_does_not_hunt(self):
        pg = _pg()
        try:
            app = App(pg.get_uri(), [_EmptySource()], fx=RATES.__getitem__, disk_queries={"empty": ["x"]}, pause=0)
            stop = threading.Event()
            worker = threading.Thread(target=app.run_scheduler, args=(stop,), kwargs={"interval": 60}, daemon=True)
            worker.start()
            threading.Event().wait(0.5)
            stop.set()
            worker.join(5)
            self.assertIsNone(app.store.last_hunt())
        finally:
            pg.cleanup()

    def test_a_restart_with_a_hunt_overdue_waits_one_interval(self):
        pg = _pg()
        try:
            app = App(pg.get_uri(), [_EmptySource()], fx=RATES.__getitem__, disk_queries={"empty": ["x"]}, pause=0)
            app.hunt()
            with app.store._conn() as c:
                c.execute("UPDATE hunts SET started = now() - interval '7 hours'")

            def hunts():
                with app.store._conn() as c:
                    return len(c.execute("SELECT id FROM hunts").fetchall())
            stop = threading.Event()
            worker = threading.Thread(target=app.run_scheduler, args=(stop,), kwargs={"interval": 1.5, "retry": 0.05},
                                      daemon=True)
            worker.start()
            threading.Event().wait(0.7)
            self.assertEqual(hunts(), 1)  # overdue, but a restart does not start a Hunt
            for _ in range(100):
                if hunts() == 2:
                    break
                threading.Event().wait(0.05)
            stop.set()
            worker.join(5)
            self.assertEqual(hunts(), 2)  # one interval after the restart
            self.assertFalse(worker.is_alive())
        finally:
            pg.cleanup()


class SchedulerSurvivesStoreErrors(unittest.TestCase):
    def test_scheduler_keeps_running_when_the_store_fails(self):
        pg = _pg()
        try:
            app = App(pg.get_uri(), [_EmptySource()], fx=RATES.__getitem__, disk_queries={"empty": ["x"]}, pause=0)
            calls = []

            def flaky():
                calls.append(1)
                raise OSError("postgres failover")
            app.store.last_started = flaky
            stop = threading.Event()
            worker = threading.Thread(target=app.run_scheduler, args=(stop,), kwargs={"retry": 0.05}, daemon=True)
            worker.start()
            for _ in range(100):
                if len(calls) >= 3:
                    break
                threading.Event().wait(0.05)
            self.assertTrue(worker.is_alive())   # survived the failures...
            self.assertGreaterEqual(len(calls), 3)  # ...and kept retrying
            stop.set()
            worker.join(5)
        finally:
            pg.cleanup()


class CrashedHuntReleasesTheLock(unittest.TestCase):
    def test_lock_is_released_after_a_crash(self):
        pg = _pg()
        try:
            app = App(pg.get_uri(), [], fx=RATES.__getitem__, pause=0)
            app.hunt = lambda: 1 / 0
            self.assertTrue(app.start_hunt())
            for _ in range(100):
                if not app.hunt_running():
                    break
                threading.Event().wait(0.05)
            self.assertFalse(app.hunt_running())
            self.assertTrue(app.start_hunt())  # the next Hunt can start
        finally:
            pg.cleanup()


class EmptySourceKeepsItsLastGoodListings(unittest.TestCase):
    """An empty Hunt from a Source keeps that Source's earlier Listings, as the banner says."""

    def test_banner_text_matches_what_is_shown(self):
        pg = _pg()
        try:
            state = {"items": FIXTURE}

            def fetch(url, headers=None, data=None):
                if "oauth2/token" in url:
                    return {"access_token": "t", "expires_in": 7200}
                q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
                return state["items"].get(q, {"itemSummaries": []})
            app = App(pg.get_uri(), [EbaySource("id", "secret", fetch=fetch)], fx=RATES.__getitem__,
                      disk_queries={"ebay_uk": list(FIXTURE)}, pause=0)
            app.hunt()
            before = len(re.findall("data-listing=", app.page()))
            state["items"] = {}  # the Source now returns nothing
            app.hunt()
            page = app.page()
            self.assertIn('data-fault="ebay_uk"', page)
            self.assertEqual(len(re.findall("data-listing=", page)), before)
            self.assertGreater(before, 5)
        finally:
            pg.cleanup()


class EbayMalformedItem(unittest.TestCase):
    def test_non_dict_item_is_skipped(self):
        def fetch(url, headers=None, data=None):
            if "oauth2/token" in url:
                return {"access_token": "t", "expires_in": 7200}
            return {"itemSummaries": ["not-an-item", FIXTURE["exos 16tb"]["itemSummaries"][0]]}
        self.assertEqual(len(EbaySource("id", "secret", fetch=fetch).search("q")), 1)


class PriceHistoryAndGone(unittest.TestCase):
    """A price drop gets an arrow; a vanished Listing is greyed out as Gone, then hidden after 7 days."""

    def test_drop_gone_and_expiry(self):
        pg = _pg()
        try:
            items = [it for v in FIXTURE.values() for it in v["itemSummaries"]
                     if (f := read_disk(it["title"], "used")) and not isinstance(f, Unreadable) and f.qualifies
                     and it.get("conditionId") == "3000" and it.get("shippingOptions")]
            stays, drops, leaves = items[0], json.loads(json.dumps(items[1])), items[2]
            state = {"items": [stays, drops, leaves]}

            def fetch(url, headers=None, data=None):
                if "oauth2/token" in url:
                    return {"access_token": "t", "expires_in": 7200}
                return {"itemSummaries": state["items"]}
            app = App(pg.get_uri(), [EbaySource("id", "secret", fetch=fetch)], fx=RATES.__getitem__,
                      disk_queries={"ebay_uk": ["q"]}, pause=0)
            app.hunt()
            old_price = float(drops["price"]["value"])
            drops["price"]["value"] = f"{old_price - 20:.2f}"
            state["items"] = [stays, drops]  # `leaves` sold
            app.hunt()

            page = app.page()
            row = lambda it: re.search(r'<tr data-listing="%s"[^>]*>.*?</tr>' % re.escape(htmllib.escape(it["itemId"])), page, re.S)
            self.assertIn('data-drop="1"', row(drops).group(0))
            self.assertIn(f"seller price was {old_price:,.0f} GBP", row(drops).group(0))
            self.assertNotIn('data-drop="1"', row(stays).group(0))
            self.assertIn('data-gone="1"', row(leaves).group(0))
            self.assertIn("last seller price", row(leaves).group(0))
            # Gone rows sort after live ones
            order = [m for m in re.findall(r'<tr data-listing="([^"]+)"', page)]
            self.assertEqual(order[-1], htmllib.escape(leaves["itemId"]))

            with app.store._conn() as c:
                self.assertEqual(c.execute("SELECT count(*) AS n FROM price_observations").fetchone()["n"], 5)
                c.execute("UPDATE listings SET last_seen = now() - interval '8 days' WHERE source_id = %s",
                          (leaves["itemId"],))
            self.assertNotIn(htmllib.escape(leaves["itemId"]), app.page())  # hidden after 7 days
        finally:
            pg.cleanup()


class SmrReclassification(unittest.TestCase):
    def test_hunt_rejects_a_previously_qualifying_disk(self):
        pg = _pg()
        try:
            item = _ebay_item("v1|smr|0", "WD 14TB 3.5 SATA HDD", 100, 10)

            def fetch(url, headers=None, data=None):
                if "oauth2/token" in url:
                    return {"access_token": "t", "expires_in": 7200}
                if "/item/" in url:
                    return {}
                return {"itemSummaries": [item]}

            app = App(pg.get_uri(), [EbaySource("id", "secret", fetch=fetch)], fx=RATES.__getitem__,
                      disk_queries={"ebay_uk": ["q"]}, ram_queries={}, pause=0)
            app.hunt()
            self.assertIn(item["itemId"], {r["source_id"] for r in app.store.best_disks()})
            item["title"] = "WD HSH721414ALE6M0 14TB 3.5 SATA HDD"

            app.hunt()

            with app.store._conn() as c:
                row = c.execute("SELECT qualifies, facts, missing FROM listings WHERE source_id = %s",
                                (item["itemId"],)).fetchone()
            self.assertIs(row["qualifies"], False)
            self.assertIs(row["facts"]["smr"], True)
            self.assertEqual(row["missing"], [])
            self.assertNotIn(item["itemId"], {r["source_id"] for r in app.store.best_disks()})
            self.assertNotIn('data-listing="v1|smr|0"', app.page())
            search = app.search_page("q", "disk")
            self.assertIn('data-result="v1|smr|0" data-qualifies="0"', search)
            self.assertIn("Rejected by the rules", search)
        finally:
            pg.cleanup()


class Routing(unittest.TestCase):

    def test_router_caches_osrm_and_falls_back_when_it_is_down(self):
        from dealfinder.costs import OsrmRouter
        pg = _pg()
        try:
            app = App(pg.get_uri(), [], fx=RATES.__getitem__, pause=0, router=lambda lat, lon: (0, 0))
            calls = []

            def osrm(url, headers=None, data=None):
                calls.append(url)
                return {"routes": [{"distance": 119000, "duration": 5760}]}
            router = OsrmRouter(app.store, fetch=osrm)
            self.assertEqual(router(59.9127, 10.7207), (119.0, 96.0))
            self.assertEqual(router(59.9127, 10.7207), (119.0, 96.0))
            self.assertEqual(len(calls), 1)  # second call served from the cache

            def down(url, headers=None, data=None):
                return {"routes": None}  # malformed reply, not just a network error
            km, minutes = OsrmRouter(app.store, fetch=down)(63.442, 10.43669)  # Trondheim, not cached
            self.assertGreater(km, 400)
            self.assertGreater(minutes, 120)  # still hidden as too far, no crash
        finally:
            pg.cleanup()


def _finn_app(docs, descriptions, router=fake_router):
    """An App over a fake finn.no that serves `docs` for every search and `descriptions` for item pages."""
    blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()

    def fetch(url):
        if "/search?" in url:
            return f"<script>{blob}</script>"
        text = descriptions[url.rsplit("/", 1)[1]]
        return f'<section data-testid="description"><p>{htmllib.escape(text)}</p></section>'
    pg = _pg()
    app = App(pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__, disk_queries={"finn": ["12tb"]},
              pause=0, router=router)
    return pg, app


def _doc(fid, heading, price, lat, lon, flags=()):
    return {"id": fid, "heading": heading, "trade_type": "Til salgs", "price": {"amount": price},
            "coordinates": {"lat": lat, "lon": lon}, "flags": list(flags), "location": "X",
            "canonical_url": f"https://www.finn.no/recommerce/forsale/item/{fid}"}


class FinnCoordinatesAreValidated(unittest.TestCase):
    def test_bad_coordinates_become_unknown_place(self):
        docs = [_doc(7, '12TB SATA HDD 3.5"', 5000, "abc", 10.7),
                _doc(8, '12TB SATA HDD 3.5"', 5000, float("nan"), 10.7)]
        pg, app = _finn_app(docs, {"7": "", "8": ""})
        try:
            app.hunt()
            self.assertTrue(app.store.last_hunt()["detail"]["finn"]["ok"])  # the Source did not abort
            unreadable = dict(re.findall(r'data-unreadable="([^"]+)" data-missing="([^"]*)"', app.page()))
            self.assertEqual(unreadable, {"7": "location", "8": "location"})
        finally:
            pg.cleanup()


def _ebay_item(iid, title, price, ship, pct="99.8", score=5000):
    it = {"itemId": iid, "title": title, "price": {"value": str(price), "currency": "GBP"}, "conditionId": "3000",
          "itemWebUrl": f"https://www.ebay.co.uk/itm/{iid}", "seller": {"username": "s", "feedbackPercentage": pct,
                                                                         "feedbackScore": score}}
    if ship is not None:
        it["shippingOptions"] = [{"shippingCost": {"value": str(ship), "currency": "GBP"}}]
    return it


class EbayDiskStockEndToEnd(unittest.TestCase):
    """Seam 1: an eBay Disk Listing with stock supplies several disks; the first unit pays the shipping,
    each more pays the item's extra-unit shipping, or the full shipping again when eBay does not state it."""

    @classmethod
    def setUpClass(cls):
        disk = 'Seagate Exos X16 16TB 3.5" SATA HDD'
        # eBay sorts by price: the item-call budget (patched to 8) goes to the cheapest qualifying Disks only
        items = ([_ebay_item("small", 'Seagate Exos 8TB 3.5" SATA HDD', 50, 20)]         # too small: no call
                 + [_ebay_item(i, disk, 190, 20) for i in "BCDEF"]                          # singles: 210 each
                 + [_ebay_item("A", disk, 200, 20),                                         # 220, then 205 each
                    _ebay_item("G", disk, 400, 20),                                         # "more than 10", 3 a buyer
                    _ebay_item("H", disk, 410, 20),                                         # item call fails
                    _ebay_item("I", disk, 420, 20)])                                        # over the budget
        ship = {"shippingCost": {"value": "20.00", "currency": "GBP"}}
        cls.details = {
            "A": {"estimatedAvailabilities": [{"estimatedAvailableQuantity": 10}],
                  "shippingOptions": [{**ship, "additionalShippingCostPerUnit": {"value": "5.00", "currency": "GBP"}}]},
            "G": {"estimatedAvailabilities": [{"availabilityThresholdType": "MORE_THAN", "availabilityThreshold": 10}],
                  "quantityLimitPerBuyer": 3, "shippingOptions": [ship]},
        }
        cls.calls = []

        def fetch(url, headers=None, data=None):
            cls.calls.append(url)
            if "oauth2/token" in url:
                return {"access_token": "t", "expires_in": 7200}
            if "/item/" in url:
                iid = url.rsplit("/", 1)[1]
                if iid == "H":
                    raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
                return cls.details.get(iid, {})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if q.get("category_ids") == ["11211"]:
                return {"itemSummaries": [_ebay_item("M", 'Dell PowerEdge R730xd 12x 3.5" LFF', 400, 80)]}
            return {"itemSummaries": items}

        cls.pg = _pg()
        with mock.patch("dealfinder.sources.EBAY_STOCK_LOOKUPS", 8):
            cls.app = App(cls.pg.get_uri(), [EbaySource("id", "secret", fetch=fetch)], fx=RATES.__getitem__,
                          disk_queries={"ebay_uk": ["exos 16tb"]}, pause=0,
                          router=fake_router)
            cls.app.hunt()
        cls.page, cls.first_hunt = cls.app.page(), list(cls.calls)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def stored(self, iid):
        with self.app.store._conn() as c:
            return c.execute("SELECT facts, costs, landed_nok FROM listings WHERE source_id = %s", (iid,)).fetchone()


    def test_more_than_is_its_threshold_capped_per_buyer_and_unknown_extra_shipping_is_charged_again(self):
        g = self.stored("G")
        self.assertEqual(g["facts"]["stock"], 3)
        self.assertAlmostEqual(g["costs"]["extra_unit"], float(g["landed_nok"]), places=2)
        self.assertEqual(self.stored("A")["facts"]["stock"], 10)
        self.assertEqual(self.stored("B")["facts"]["stock"], 1)
        self.assertNotIn("extra_unit", self.stored("B")["costs"])

    def test_item_calls_go_to_the_cheapest_qualifying_disks_and_a_failed_one_counts_one_disk(self):
        fetched = [u.rsplit("/", 1)[1] for u in self.first_hunt if "/item/" in u]
        self.assertEqual(sorted(fetched), sorted("BCDEFAGH"))
        self.assertEqual(self.stored("H")["facts"]["stock"], 1)
        self.assertEqual(self.stored("I")["facts"]["stock"], 1)
        self.assertIn('dealfinder_source_up{source="ebay_uk"} 1', self.app.metrics())

    def test_stock_is_read_once_a_day(self):
        self.calls.clear()
        with mock.patch("dealfinder.sources.EBAY_STOCK_LOOKUPS", 8):
            self.app.hunt()
        self.assertEqual([u.rsplit("/", 1)[1] for u in self.calls if "/item/" in u], ["H"])  # only the failed one
        self.assertEqual(self.stored("A")["facts"]["stock"], 10)

    def test_a_new_pod_does_not_read_stock_again(self):
        calls, fetch = [], self.app.sources[0]._fetch

        def counting(url, headers=None, data=None):
            calls.append(url)
            return fetch(url, headers, data)
        with mock.patch("dealfinder.sources.EBAY_STOCK_LOOKUPS", 8):
            app = App(self.pg.get_uri(), [EbaySource("id", "secret", fetch=counting)], fx=RATES.__getitem__,
                      disk_queries={"ebay_uk": ["exos 16tb"]}, pause=0,
                      router=fake_router)
            app.hunt()
        self.assertEqual(sorted(u.rsplit("/", 1)[1] for u in calls if "/item/" in u), ["H"])
        self.assertEqual(self.stored("A")["facts"]["stock"], 10)


class FinnDiskLotsEndToEnd(unittest.TestCase):
    """Seam 1: a title that names several disks ("4x 16TB") is one lot at its price; one priced per disk (title,
    description, or under DISK_MIN_NOK_PER_TB) is one disk, sold up to the stock its text states."""

    @classmethod
    def setUpClass(cls):
        ship = ["shipping_exists", "seller_pays_shipping"]
        disks = [_doc(10, '4x Seagate Exos X16 16TB 3.5" SATA', 7000, 60.4, 5.5, ship),       # a lot: 1,750 a disk
                 _doc(11, '4 stk Seagate Exos X16 16TB 3.5" SATA', 1900, 60.4, 5.5, ship),    # description: per disk
                 _doc(12, '5 stk Seagate Exos X16 16TB 3.5" SATA', 1300, 60.4, 5.5, ship)]    # 16 NOK/TB: per disk
        disks += [_doc(20 + i, 'Seagate Exos X16 16TB 3.5" SATA', 2000 + i, 60.4, 5.5, ship) for i in range(3)]
        descriptions = {"10": "Fire disker, selges samlet.", "11": "Fire like disker. Pris per stk.",
                        "12": "Fem disker."}

        def fetch(url):
            if "/search?" in url:
                q = urllib.parse.parse_qs(url.split("?", 1)[1])
                docs = disks if q["condition"] == ["3", "4"] else []
                blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
                return f"<script>{blob}</script>"
            return f'<section data-testid="description"><p>{descriptions.get(url.rsplit("/", 1)[1], "")}</p></section>'

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__,
                      disk_queries={"finn": ["exos"]}, pause=0, router=fake_router)
        cls.app.hunt()
        cls.page = cls.app.page()

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()


    def test_best_disks_price_a_lot_per_disk_and_say_it_is_a_lot(self):
        rows = dict(re.findall(r'data-listing="(\d+)" data-nok-per-tb="([\d.]+)"', self.page))
        self.assertAlmostEqual(float(rows["10"]), round(_shipped(7000) / 64, 2), places=2)
        self.assertAlmostEqual(float(rows["11"]), round(_shipped(1900) / 16, 2), places=2)
        self.assertAlmostEqual(float(rows["12"]), round(_shipped(1300) / 16, 2), places=2)
        self.assertIn("<td>4 &times; 16 TB</td>", self.page.split('data-listing="10"', 1)[1].split("</tr>", 1)[0])

    def test_a_text_priced_per_disk_sells_its_stated_stock(self):
        # "4 stk" priced per disk: 4 on sale, each after the first at its price + Trygg betaling (seller pays shipping)
        with self.app.store._conn() as c:
            row = c.execute("SELECT facts, costs FROM listings WHERE source_id = '11'").fetchone()
        self.assertEqual((row["facts"]["count"], row["facts"]["stock"]), (1, 4))
        self.assertAlmostEqual(row["costs"]["extra_unit"], _shipped(1900), places=2)


class RamEndToEnd(unittest.TestCase):
    """Seam 1: RAM Listings from eBay UK and finn.no land on the Best RAM tab at their Landed cost per GB."""

    @classmethod
    def setUpClass(cls):
        items = [_ebay_item("r1", "SK Hynix 32GB DDR4 2400MHz PC4-2400T ECC REG Server RAM DIMM HMA84GR7MFR4N-UH",
                            125.49, 3.98),
                 _ebay_item("r2", "Crucial 32GB 2x16GB DDR4-2133 RDIMM ECC Registered 288-Pin CT16G4RFD4213", 63.10, 2.70),
                 _ebay_item("r3", "Dell R730 server 128GB RAM", 150, 20),                                   # a server
                 _ebay_item("r4", "32GB DDR4 ECC RAM", 40, 0)]                                              # RDIMM?
        cls.filters = []

        def ebay(url, headers=None, data=None):
            if "oauth2/token" in url:
                return {"access_token": "t", "expires_in": 7200}
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            cls.filters.append((q.get("category_ids"), q["filter"][0]))
            return {"itemSummaries": items if q.get("category_ids") == ["11210"] else []}

        def finn(url):
            if "/search?" not in url:
                return ""  # an item page with no description
            q = urllib.parse.parse_qs(url.split("?", 1)[1])
            docs = [_doc(60, "Samsung 128GB (4x32GB) DDR4-3200MHz ECC RDIMM serverminne PC4-25600", 6000, 59.9, 10.7,
                         ["shipping_exists", "seller_pays_shipping"]),
                    _doc(61, "Samsung 32GB x 10 stk DDR4 RDIMM", 1600, 59.9, 10.7,             # priced per stick
                         ["shipping_exists", "seller_pays_shipping"]),
                    _doc(62, "10x Samsung 32GB DDR4 2666MHz ECC RDIMM", 1700, 59.9, 10.7),    # per stick, pickup
                    # "make an offer" (price 0): DDR3 is rejected (finn.no 477346469), DDR4 cannot be read
                    _doc(63, "16GB DDR3 ECC RDIMM PC3-14900R – SK Hynix & Samsung – 95 stk totalt", 0, 59.9, 10.7),
                    _doc(64, "Samsung 32GB DDR4 ECC RDIMM", 0, 59.9, 10.7)]
            docs = docs if q["q"] == ["rdimm"] and q["condition"] == ["3", "4"] else []
            blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
            return f"<script>{blob}</script>"

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [EbaySource("id", "secret", fetch=ebay), FinnSource(fetch=finn, pause=0)],
                      fx=RATES.__getitem__, disk_queries={"ebay_uk": []}, ram_queries={"ebay_uk": ["ddr4 ecc rdimm 32gb"], "finn": ["rdimm"]},
                      pause=0, router=fake_router)
        cls.app.hunt()
        cls.page = cls.app.page()
        cls.rows = [(i, float(n)) for i, n in re.findall(r'data-ram="([^"]+)" data-nok-per-gb="([\d.]+)"', cls.page)]

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def test_best_ram_shows_landed_nok_per_gb_sorted(self):
        one = (125.49 + 3.98) * RATES["GBP"] * 1.25 / 32          # eBay: price + shipping + VAT, one 32 GB stick
        pair = (63.10 + 2.70) * RATES["GBP"] * 1.25 / 32          # one Listing, 2 x 16 GB
        self.assertEqual([i for i, _ in self.rows], ["r2", "60", "61", "r1", "62"])
        rows = dict(self.rows)
        self.assertAlmostEqual(rows["r1"], one, places=1)
        self.assertAlmostEqual(rows["r2"], pair, places=1)
        self.assertAlmostEqual(rows["60"], _shipped(6000) / 128, places=1)  # finn: free shipping + fee, no VAT
        self.assertIn("<td>RDIMM</td><td>4 &times; 32 GB</td><td>3200 MT/s</td>", self.page)
        self.assertIn("<td>RDIMM</td><td>2 &times; 16 GB</td><td>2133 MT/s</td>", self.page)

    def test_ten_sticks_under_the_nok_per_gb_floor_count_as_one(self):
        # the floor reads the seller's price, 1600 / 320 = 5 NOK per GB; Landed is price + Trygg betaling
        self.assertAlmostEqual(dict(self.rows)["61"], _shipped(1600) / 32, places=1)
        row = self.page.split('data-ram="61"', 1)[1].split("</tr>", 1)[0]
        self.assertIn("<td>1 &times; 32 GB</td>", row)
        self.assertEqual(next(r["facts"]["sticks"] for r in self.app.store.best_ram() if r["source_id"] == "61"), 1)

    def test_a_pickup_trip_does_not_lift_a_per_stick_price_over_the_floor(self):
        # 1700 + 952 NOK trip = 2652 NOK = 8.3 NOK per GB over 320 GB, but 1700 / 320 = 5.3 is under the floor
        self.assertAlmostEqual(dict(self.rows)["62"], (1700 + 2 * 119 * PICKUP_NOK_PER_KM) / 32, places=1)
        row = self.page.split('data-ram="62"', 1)[1].split("</tr>", 1)[0]
        self.assertIn("<td>1 &times; 32 GB</td>", row)

    def test_server_in_a_ram_search_is_not_listed_and_unreadable_ram_is(self):
        self.assertNotIn("r3", dict(self.rows))
        self.assertNotIn('data-unreadable="r3"', self.page)
        self.assertIn('data-unreadable="r4" data-missing="type"', self.page)

    def test_make_an_offer_ram_is_rejected_when_its_facts_rule_it_out(self):
        self.assertNotIn('data-unreadable="63"', self.page)
        self.assertIn('data-unreadable="64" data-missing="price"', self.page)
        self.assertNotIn("63", dict(self.rows))

    def test_ebay_ram_search_uses_its_category_and_price_range(self):
        self.assertIn((["11210"], "buyingOptions:{FIXED_PRICE},deliveryCountry:NO,price:[5..1500],priceCurrency:GBP"),
                      self.filters)
        self.assertIn('dealfinder_listings{source="ebay_uk",kind="ram",state="qualified"} 2', self.app.metrics())

    def test_search_and_track_accept_ram(self):
        page = self.app.search_page("rdimm", "ram")
        self.assertIn('data-result="60" data-qualifies="1"', page)
        self.assertIn("<th>NOK per GB</th>", page)
        self.assertIn(f"<td>{_shipped(6000):,.0f}</td><td>{_shipped(6000) / 128:,.0f}</td>",
                      page.split('data-result="60"', 1)[1].split("</tr>", 1)[0])
        self.assertIn('<option value="ram">RAM</option>', self.page)
        self.assertIn('data-tracked="ram:rdimm"', self.page)


class FinnFeesAndPerUnitEndToEnd(unittest.TestCase):
    """Seam 1: a finn.no Listing bought via Fiks ferdig pays the per-kind shipping estimate and Trygg betaling; a
    pickup pays the trip and no fee; a price per unit in the description supplies one unit (finn.no 471846605)."""

    @classmethod
    def setUpClass(cls):
        oslo, fiks = (59.91, 10.72), ["shipping_exists"]  # buyer pays shipping: finn does not publish its price
        free = ["shipping_exists", "seller_pays_shipping"]
        rams = [_doc(80, "4x 16Gb DDR4 2400 ECC RDIMM", 800, *oslo, fiks),
                _doc(83, "2x 32GB DDR4-2400 ECC RDIMM", 1000, *oslo),                              # pickup only
                _doc(85, "128 GB (8 x 16 GB) Micron DDR4-2400 ECC RDIMM VLR", 5800, *oslo, free),   # live 477364211
                _doc(86, "Kingston 16GB DDR4 2666 ECC RDIMM", 500, *oslo, fiks),                   # one stick
                _doc(87, "2x 16GB DDR4-2400 ECC RDIMM", 700, *oslo)]                  # pickup only by flags
        cls.docs = {"rdimm": rams, "ddr4 ecc": rams,
                    "exos": [_doc(81, 'Seagate Exos X16 16TB 3.5" SATA', 2000, *oslo, fiks),
                             _doc(84, '12TB SATA HDD 3.5"', 5000, 59.3, 10.2, fiks)]}
        descriptions = {"80": "Bare 4 igjen 16Gb DDR4 2400 ECC RDIMN !!! Pris per srk.",
                        "85": "Kan også selges enkeltvis for 750kr per brikke. Spesifikasjoner per brikke: 16GB per modul.",
                        "87": "Gratis frakt i hele Norge."}
        cls.fetched = []

        def fetch(url):
            if "/search?" in url:
                q = urllib.parse.parse_qs(url.split("?", 1)[1])
                docs = cls.docs.get(q["q"][0], []) if q["condition"] == ["3", "4"] else []
                blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
                return f"<script>{blob}</script>"
            cls.fetched.append(url.rsplit("/", 1)[1])
            text = descriptions.get(url.rsplit("/", 1)[1], "")
            return f'<section data-testid="description"><p>{htmllib.escape(text)}</p></section>'

        def router(lat, lon):
            return (40.0, 30.0) if lat < 59.5 else fake_router(lat, lon)

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__,
                      disk_queries={"finn": ["exos"]}, ram_queries={"finn": ["rdimm", "ddr4 ecc"]}, pause=0,
                      router=router)
        cls.app.hunt()
        cls.page = cls.app.page()

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def row(self, attr, fid):
        return self.page.split(f'data-{attr}="{fid}"', 1)[1].split("</tr>", 1)[0]

    def test_per_unit_ram_reads_one_stick_and_pays_estimate_and_fee(self):
        # the owner's checkout was 800 + Trygg betaling 77 + Helthjem 38 = 915 for ONE stick; the estimate takes the
        # dearest small parcel, 65: 800 + 65 + (29 + 48) = 942, 1 x 16 GB, not 4 sticks at 19 NOK/GB
        landed = next(r["landed_nok"] for r in self.app.store.best_ram() if r["source_id"] == "80")
        self.assertAlmostEqual(float(landed), 942, places=2)
        self.assertIn('data-ram="80" data-nok-per-gb="58.88"', self.page)  # 942 / 16
        row = self.row("ram", 80)
        self.assertIn("<td>1 &times; 16 GB</td>", row)
        self.assertIn("800 + shipping (est.) 65 + Trygg betaling (est.) 77", row)

    def test_spec_text_and_single_unit_offers_in_a_description_keep_the_lot(self):
        # "750kr per brikke" is not the 5,800 price and "per brikke" / "16GB per modul" are spec text: 8 sticks
        self.assertIn("<td>8 &times; 16 GB</td>", self.row("ram", 85))
        per_gb = float(re.search(r'data-ram="85" data-nok-per-gb="([\d.]+)"', self.page)[1])
        self.assertAlmostEqual(per_gb, _shipped(5800) / 128, places=1)

    def test_only_multi_unit_parts_are_read_once_across_queries(self):
        self.assertEqual(sorted(self.fetched), ["80", "81", "83", "84", "85", "87"])

    def test_a_part_offering_free_shipping_in_its_text_is_shipped(self):
        # pickup only by finn's flags, but "Gratis frakt" in the text: 700 + Trygg betaling, no trip
        per_gb = float(re.search(r'data-ram="87" data-nok-per-gb="([\d.]+)"', self.page)[1])
        self.assertAlmostEqual(per_gb, _shipped(700) / 32, places=1)
        self.assertNotIn("pickup trip", self.row("ram", 87))

    def test_disk_with_fiks_ferdig_pays_the_small_parcel_estimate_and_fee(self):
        per_tb = float(re.search(r'data-listing="81" data-nok-per-tb="([\d.]+)"', self.page)[1])
        self.assertAlmostEqual(per_tb, (2000 + 65 + _fee(2000)) / 16, places=1)


    def test_near_pickup_pays_the_trip_and_no_fee(self):
        # pickup only: 1,000 + trip 952, no Trygg betaling
        per_gb = float(re.search(r'data-ram="83" data-nok-per-gb="([\d.]+)"', self.page)[1])
        self.assertAlmostEqual(per_gb * 64, 1000 + 952, places=1)
        self.assertNotIn("Trygg betaling", self.row("ram", 83))
        landed = next(float(r["landed_nok"]) for r in self.app.store.best_disks() if r["source_id"] == "84")
        self.assertAlmostEqual(landed, 5320, places=2)
        self.assertIn("pickup trip 320", self.row("listing", 84))


class SearchAndTrack(unittest.TestCase):
    """Seam 1: Search every Source now, Track the query, and the next Hunt runs it."""

    @classmethod
    def setUpClass(cls):
        cls.queries = []
        x20 = {"itemSummaries": [_ebay_item("v1|x20|0", 'Seagate Exos X20 20TB 3.5" SATA enterprise HDD', 250, 10)]}

        def ebay(url, headers=None, data=None):
            if "oauth2/token" in url:
                return {"access_token": "t", "expires_in": 7200}
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
            cls.queries.append(("ebay_uk", q))
            return x20 if q == "exos x20" else {"itemSummaries": []}

        def finn(url):
            if "/search?" not in url:
                return ""  # a qualifying Disk's item page: no text
            q = urllib.parse.parse_qs(url.split("?", 1)[1])
            cls.queries.append(("finn", q["q"][0]))
            docs = [_doc(99, 'Seagate Exos X20 20TB 3.5"', 3000, 59.9, 10.7, ["shipping_exists", "seller_pays_shipping"])]
            docs = docs if q["q"] == ["exos x20"] and q["condition"] == ["3", "4"] else []
            blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
            return f"<script>{blob}</script>"

        cls.pg = _pg()
        cls.sources = [EbaySource("id", "secret", fetch=ebay), FinnSource(fetch=finn, pause=0)]
        cls.app = App(cls.pg.get_uri(), cls.sources, fx=RATES.__getitem__, pause=0, router=fake_router)  # config seed
        cls.server = cls.app.make_server("127.0.0.1", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.pg.cleanup()

    def test_search_track_then_hunt(self):
        with urllib.request.urlopen(self.base + "/search?q=Exos+X20&kind=disk") as r:
            page = r.read().decode()
        results = dict(re.findall(r'data-result="([^"]+)" data-qualifies="(\d)"', page))
        self.assertEqual(results, {"v1|x20|0": "1", "99": "1"})  # both Sources, scored now
        self.assertEqual(self.app.store.last_hunt(), None)       # a Search is not a Hunt and saves nothing

        req = urllib.request.Request(self.base + "/track", data=b"q=Exos+X20&kind=disk", method="POST")
        with urllib.request.urlopen(req) as r:
            self.assertIn('data-tracked="disk:exos x20"', r.read().decode())

        self.queries.clear()
        self.app.hunt()
        self.assertIn(("ebay_uk", "exos x20"), self.queries)
        self.assertIn(("finn", "exos x20"), self.queries)
        shown = set(re.findall(r'data-listing="([^"]+)"', self.app.page()))
        self.assertTrue({"v1|x20|0", "99"} <= shown)

    def test_starting_set_is_seeded_once(self):
        tracked = {(r["query"], r["kind"]) for r in self.app.store.tracked()}
        for q in ("exos 12tb", "wd purple 12tb", "barracuda 12tb", "exos 14tb", "exos 16tb"):
            self.assertIn((q, "disk"), tracked)
        for q in ("ddr4 ecc rdimm 16gb", "ddr4 ecc rdimm 32gb", "ddr4 lrdimm 64gb", "ddr4 ecc", "rdimm"):
            self.assertIn((q, "ram"), tracked)
        before = len(self.app.store.tracked())
        App(self.pg.get_uri(), self.sources, fx=RATES.__getitem__, disk_queries={"finn": ["other"]}, pause=0,
            router=fake_router)  # a restart must not re-seed
        self.assertEqual(len(self.app.store.tracked()), before)

    def test_bad_input_is_rejected(self):
        for path in ("/search?q=&kind=disk", "/search?q=x&kind=car", "/search?q=x&kind=machine",
                     "/search?q=x&kind=cpu", "/search?q=x&kind=heatsink"):
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(err.exception.code, 400)
        for length in (b"-1", b"abc", b"5000", "\xb2".encode("latin-1"), "\u0663".encode()):  # raw header bytes
            conn = http.client.HTTPConnection(*self.server.server_address[:2], timeout=5)
            conn.putrequest("POST", "/track")
            conn.putheader("Content-Length", length)
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 400, length)
            conn.close()
        req = urllib.request.Request(self.base + "/track", data=b"q=a%00b&kind=disk", method="POST")
        with urllib.request.urlopen(req) as r:
            self.assertIn('data-tracked="disk:a b"', r.read().decode())


class TrackedQuerySeeding(unittest.TestCase):
    """Seam 1: starting Tracked queries per (kind, Source) group, and the migration that drops a removed Source."""

    def test_a_new_source_gets_its_starting_queries_on_an_already_seeded_database(self):
        pg = _pg()
        try:
            App(pg.get_uri(), [], fx=RATES.__getitem__, disk_queries={"finn": ["exos"]}, ram_queries={"finn": []},
                )
            app = App(pg.get_uri(), [], fx=RATES.__getitem__, disk_queries={"finn": ["other"], "ebay_uk": ["x20"]},
                      ram_queries={"finn": ["rdimm"]},
                      )
            got = {(r["query"], r["kind"], r["source"]) for r in app.store.tracked()}
            self.assertEqual(got, {("exos", "disk", "finn"), ("x20", "disk", "ebay_uk"),
                                   ("rdimm", "ram", "finn")})
        finally:
            pg.cleanup()

    def test_migration_removes_aliexpress_tracked_queries(self):
        pg = _pg()
        try:
            app = App(pg.get_uri(), [], fx=RATES.__getitem__)
            with app.store._conn() as c:
                c.execute("INSERT INTO tracked_queries (query, kind, source) VALUES ('exos', 'disk', 'aliexpress')")
            app.store.migrate()
            self.assertNotIn("aliexpress", {r["source"] for r in app.store.tracked()})
        finally:
            pg.cleanup()


class PriceHistory(unittest.TestCase):
    """Seam 1: two Hunts a week apart; the history page shows each week's lowest/median and the change."""

    @classmethod
    def setUpClass(cls):
        cls.disk_price = 1500
        ship = ["shipping_exists", "seller_pays_shipping"]

        def fetch(url):
            if "/search?" in url:
                q = urllib.parse.parse_qs(url.split("?", 1)[1])
                disks = [_doc(10 + i, 'Seagate Exos X16 16TB 3.5" SATA', cls.disk_price + 100 * i, 60.4, 5.5, ship)
                         for i in range(5)]
                rams = [_doc(50, "4x 32GB DDR4-2400 ECC RDIMM", 2000, 60.4, 5.5, ship)]
                docs = (rams if q["q"] == ["rdimm"] else disks) if q["condition"] == ["3", "4"] else []
                blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
                return f"<script>{blob}</script>"
            return '<section data-testid="description"></section>'

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__,
                      disk_queries={"finn": ["exos"]}, ram_queries={"finn": ["rdimm"]}, pause=0,
                      router=fake_router)
        cls.app.hunt()
        import psycopg
        with psycopg.connect(cls.pg.get_uri(), autocommit=True) as c:  # make Hunt 1 a week old
            c.execute("UPDATE price_observations SET seen_at = seen_at - interval '7 days'")
            c.execute("UPDATE hunts SET started = started - interval '7 days'")
            c.execute("UPDATE price_observations SET kind = NULL, units = NULL, "
                      "model = NULL")
        cls.app.store.migrate()
        cls.disk_price = 1200  # every seller drops the price by 300 NOK
        cls.app.hunt()
        cls.page = cls.app.history_page()
        cls.main = cls.app.page()  # before any test changes the shared database

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def cells(self, key):
        row = re.search(rf'<tr data-history="{re.escape(key)}">(.*?)</tr>', self.page).group(1)
        return re.findall(r"<td>([\d,]+) / ([\d,]+) <small>\((\d+)\)</small></td>", row), row

    def test_disk_weeks_show_lowest_median_and_change(self):
        (old, new), row = self.cells("16 TB|finn")
        # 5 disks, 1,500..1,900 NOK, then 1,200..1,600; free shipping + Trygg betaling, finn has no VAT; per TB = / 16
        self.assertEqual(old, (f"{_shipped(1500) / 16:,.0f}", f"{_shipped(1700) / 16:,.0f}", "5"))
        self.assertEqual(new, (f"{_shipped(1200) / 16:,.0f}", f"{_shipped(1400) / 16:,.0f}", "5"))
        self.assertIn(f"{(_shipped(1400) / _shipped(1700) - 1) * 100:+.0f}%", row)


    def test_parts_have_weekly_rows_per_unit_including_old_hunts(self):
        for key, landed, units in (("DDR4 RDIMM 32 GB|finn", _shipped(2000), 128),):
            (old, new), _ = self.cells(key)
            self.assertEqual(old, (f"{landed / units:,.0f}", f"{landed / units:,.0f}", "1"), key)
            self.assertEqual(new, old, key)

    def test_listings_under_the_lowest_price_of_earlier_weeks_are_deals(self):
        # 16 TB: 1,500..1,900 last week, 1,200..1,600 now. Last week's low is 1,500 (per TB, shipped); this week's own
        # prices are not history, or the low would be 1,200 and nothing would beat it
        page = self.main
        rows = dict(re.findall(r'<tr data-listing="(\d+)"([^>]*)>', page))
        self.assertEqual({i for i, attrs in rows.items() if 'data-deal="1"' in attrs}, {"10", "11", "12"})  # 1,200..1,400
        self.assertIn('data-drop="1" class="deal" data-deal="1"', rows["10"])  # a drop and a deal both show
        self.assertIn(f"That's a deal: under the lowest price of earlier weeks ({_shipped(1500) / 16:,.0f} NOK per TB)",
                      page)
        # one RAM Listing is too little history for a deal price
        self.assertEqual(page.count('data-deal="1"'), 3)

    def test_a_listing_that_stops_qualifying_keeps_its_history(self):
        import psycopg
        with psycopg.connect(self.pg.get_uri(), autocommit=True) as c:  # e.g. now "make an offer"
            c.execute("UPDATE listings SET qualifies = false, capacity_tb = NULL, facts = NULL WHERE source_id = '10'")
        page = self.app.history_page()  # rendered after the change; used to raise on a NULL capacity
        row = re.search(r'<tr data-history="16 TB\|finn">(.*?)</tr>', page).group(1)
        self.assertEqual(re.findall(r"<small>\((\d+)\)</small>", row), ["5", "5"])
        self.assertNotIn('data-history="None', page)

    def test_every_page_is_dark(self):
        for html in (self.page, self.app.page(), self.app.search_page("exos", "disk")):
            self.assertIn('<meta name="color-scheme" content="dark">', html)
            self.assertIn("background:#121212", html)

    def test_sections_are_tabs_with_sticky_headers(self):
        for html, labels in ((self.page, ["Disks, NOK per TB", "RAM, NOK per GB"]),
                             (self.app.page(), ["Best Disks", "Best RAM", "Could not read"])):
            self.assertEqual(re.findall(r'<label for="tab\d">([^<]+)</label>', html), labels)
            self.assertEqual(html.count("<section>"), len(labels))
            self.assertEqual(html.count('name="tab" id="tab0" checked'), 1)
            self.assertIn(f".tabs>input:nth-of-type({len(labels)}):checked~section:nth-of-type({len(labels)})", html)
            self.assertIn("th{position:sticky;top:0", html)
