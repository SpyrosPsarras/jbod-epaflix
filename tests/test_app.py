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
from dealfinder.builds import _pick_stock
from dealfinder.config import PICKUP_NOK_PER_KM
from dealfinder.costs import DailyFx
from dealfinder.rules import Unreadable, read_disk
from dealfinder.sources import EbaySource, FinnSource

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FIXTURE = json.loads((FIXTURES / "ebay_disk_search.json").read_text())
FINN = json.loads((FIXTURES / "finn.json").read_text())
FINN_DISK_QUERIES = ["exos 16tb", "ultrastar 16tb"]
FINN_MACHINE_QUERIES = ["r730xd", "r730", "r740", "r720xd", "dl380 gen9", "supermicro server"]
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
        return {"access_token": "test-token", "expires_in": 7200}
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
    item = FINN["items"].get(url.rstrip("/").rsplit("/", 1)[1], {"description": ""})  # recorded: Machines only
    paragraphs = "".join(f"<p>{htmllib.escape(line)}</p>" for line in (item["description"] or "").split("\n"))
    return f'<section data-testid="description"><div class="whitespace-pre-wrap">{paragraphs}</div></section>'


class HuntToPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
        sources = [EbaySource("id", "secret", fetch=fake_fetch), FinnSource(fetch=fake_finn_fetch, pause=0)]
        cls.app = App(cls.pg.get_uri(), sources, fx=RATES.__getitem__,
                      disk_queries={"ebay_uk": [*FIXTURE, "hostile"], "finn": FINN_DISK_QUERIES},
                      machine_queries=FINN_MACHINE_QUERIES, pause=0, router=fake_router)
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

    def machines(self):
        _, html = self.get("/")
        return [(i, float(n)) for i, n in re.findall(r'data-machine="([^"]+)" data-landed="([\d.]+)"', html)]

    def unreadable(self):
        _, html = self.get("/")
        return dict(re.findall(r'data-unreadable="([^"]+)" data-missing="([^"]*)"', html))

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


    def test_finn_machines_that_fit_the_rules_are_ranked_by_price(self):
        machines = self.machines()
        shown = {i for i, _ in machines}
        for fid in ("473386139",   # Oslo R730xd, pickup 96 min
                    "475664047"):  # Kristiansand R730, pickup-only by flags but "Free Shipping" in the text
            self.assertIn(fid, shown)
        prices = [p for _, p in machines]
        self.assertEqual(prices, sorted(prices))

    def test_finn_machines_that_break_the_rules_are_not_ranked(self):
        shown = {i for i, _ in self.machines()}
        for fid in ("468970308",   # Trondheim R730xd: pickup-only, 547 min away, hidden
                    "474398976",   # Saksvik R730XD: pickup-only, far, hidden
                    "469749680",   # R720xd, 12th Gen
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

    def test_machine_landed_cost_adds_trip_and_every_penalty_without_vat(self):
        rows = dict(self.machines())
        # Oslo R730xd, 5,000 NOK: pickup 2 x 119 km x 4 = 952; single PSU 500; 12 bays, 0 caddies 1,200;
        # PERC H730 counts as HBA-capable (0); rails not stated 400. No VAT on a finn.no Listing.
        self.assertAlmostEqual(rows["473386139"], 5000 + 952 + 500 + 1200 + 400, places=2)
        # Kristiansand R730, 12,000 NOK: free shipping (0) but still bought via Fiks ferdig, so Trygg betaling 749;
        # 2 PSUs, 8 bays no caddies stated 800, controller not stated 500 (unknown is charged), rails not stated 400.
        self.assertAlmostEqual(rows["475664047"], 12000 + _fee(12000) + 800 + 500 + 400, places=2)
        _, html = self.get("/")
        self.assertIn("pickup trip 952", html)
        self.assertIn("rails (not stated) 400", html)
        self.assertIn("pickup Oslo (96 min)", html)


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

        listings = {l.source_id: l for l in FinnSource(fetch=fetch, pause=0).search("r730", "machine")}
        self.assertEqual(sorted(listings), ["2", "3"])            # the string and the list-coordinates doc are skipped
        self.assertIsNone(listings["2"].description)              # a non-finn URL is never fetched
        self.assertIsNone(listings["3"].description)              # a dead page leaves the description empty
        self.assertFalse(any(u.startswith("file:") for u in fetched))


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

    def test_scheduler_starts_a_hunt_when_none_has_run(self):
        pg = _pg()
        try:
            app = App(pg.get_uri(), [_EmptySource()], fx=RATES.__getitem__, disk_queries={"empty": ["x"]}, pause=0)
            stop = threading.Event()
            worker = threading.Thread(target=app.run_scheduler, args=(stop,), daemon=True)
            worker.start()
            for _ in range(100):
                if app.store.last_hunt():
                    break
                threading.Event().wait(0.05)
            stop.set()
            worker.join(5)
            self.assertIsNotNone(app.store.last_hunt())
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


class PenaltiesAndRouting(unittest.TestCase):
    def test_machine_with_every_penalty(self):
        from dealfinder.costs import machine_penalties
        # missing CPUs and RAM are no Penalty since #34: the Build buys them as Parts
        facts = {"bays_35": 12, "caddies_35": 4, "psu_count": 1, "controller": "raid", "rails": False,
                 "cpu": False, "ram_gb": 0}
        self.assertEqual(machine_penalties(facts), {"single_psu": 500, "caddies": 800, "raid_only": 500,
                                                    "no_rails": 400})
        unknown = {"bays_35": 8, "caddies_35": None, "psu_count": None, "controller": None, "rails": None,
                   "cpu": None, "ram_gb": None}
        self.assertEqual(machine_penalties(unknown), {"psu_unknown": 500, "caddies_unknown": 800,
                                                      "controller_unknown": 500, "rails_unknown": 400})
        clean = {"bays_35": 12, "caddies_35": 12, "psu_count": 2, "controller": "hba", "rails": True,
                 "cpu": True, "ram_gb": 256}
        self.assertEqual(machine_penalties(clean), {})

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
    app = App(pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__, disk_queries={"finn": []},
              machine_queries=["r730xd"], pause=0, router=router)
    return pg, app


def _doc(fid, heading, price, lat, lon, flags=()):
    return {"id": fid, "heading": heading, "trade_type": "Til salgs", "price": {"amount": price},
            "coordinates": {"lat": lat, "lon": lon}, "flags": list(flags), "location": "X",
            "canonical_url": f"https://www.finn.no/recommerce/forsale/item/{fid}"}


class PickupAndPenaltiesEndToEnd(unittest.TestCase):
    """Seam 1: a near pickup with every Penalty, far pickups hidden even when the text negates shipping."""

    @classmethod
    def setUpClass(cls):
        docs = [
            _doc(1, "Dell PowerEdge R730xd 12x LFF", 6000, 59.91, 10.72),   # Oslo, 96 min
            _doc(2, "Dell PowerEdge R730xd 12x LFF", 3000, 63.44, 10.43),   # Trondheim, 547 min
            _doc(3, "Dell PowerEdge R730xd 12x LFF", 3100, 63.44, 10.43),
            _doc(4, "Dell PowerEdge R730xd 12x LFF", 3200, 63.44, 10.43),
            _doc(5, "Dell PowerEdge R730xd 12x LFF", 3300, 63.44, 10.43),
        ]
        descriptions = {
            "1": "1x 750W PSU. PERC H710 RAID. 4x 3.5\" caddies. Rails følger ikke med. Ingen CPU. 32GB RAM.",
            "2": "Kun henting. Frakt kan ikke tilbys.",
            "3": "Ikke gratis frakt, hentes i Trondheim.",
            "4": "Fri frakt? Nei.",
            "5": "Gratis frakt i hele Norge.",                             # far, but ships free: shown
        }
        cls.pg, cls.app = _finn_app(docs, descriptions)
        cls.app.hunt()
        cls.rows = {i: p for i, p in re.findall(r'data-machine="([^"]+)" data-landed="([\d.]+)"', cls.app.page())}

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def test_near_pickup_with_every_penalty(self):
        # 6,000 + trip 2 x 119 x 4 = 952 + PSU 500 + 8 missing caddies 800 + RAID-only 500 + no rails 400;
        # "Ingen CPU" and 32 GB RAM are no Penalty since #34, the Build buys the CPUs and RAM as Parts
        self.assertAlmostEqual(float(self.rows["1"]), 6000 + 952 + 500 + 800 + 500 + 400, places=2)
        page = self.app.page()
        for part in ("pickup trip 952", "2nd PSU 500", "caddies 800", "HBA 500", "rails 400", "<td>32 GB</td>",
                     "<td>2 CPU, 128 GB, 2 HS</td>"):
            self.assertIn(part, page)

    def test_far_pickups_are_hidden_even_with_negated_shipping_words(self):
        for fid in ("2", "3", "4"):
            self.assertNotIn(fid, self.rows)

    def test_far_seller_with_free_shipping_is_shown_at_its_price(self):
        # 3,300, free shipping + Trygg betaling 227, 12 caddies not stated 1,200, PSU / controller / rails not stated
        # 500 + 500 + 400; CPU and RAM not stated are no Penalty since #34
        self.assertAlmostEqual(float(self.rows["5"]), _shipped(3300) + 1200 + 500 + 500 + 400, places=2)
        self.assertNotIn("CPUs (not stated)", self.app.page())
        self.assertNotIn("RAM to 128 GB", self.app.page())


class FinnCoordinatesAreValidated(unittest.TestCase):
    def test_bad_coordinates_become_unknown_place(self):
        docs = [_doc(7, "Dell PowerEdge R730xd 12x LFF", 5000, "abc", 10.7),
                _doc(8, "Dell PowerEdge R730xd 12x LFF", 5000, float("nan"), 10.7)]
        pg, app = _finn_app(docs, {"7": "", "8": ""})
        try:
            app.hunt()
            self.assertTrue(app.store.last_hunt()["detail"]["finn"]["ok"])  # the Source did not abort
            unreadable = dict(re.findall(r'data-unreadable="([^"]+)" data-missing="([^"]*)"', app.page()))
            self.assertEqual(unreadable, {"7": "location", "8": "location"})
        finally:
            pg.cleanup()


class BuildsEndToEnd(unittest.TestCase):
    """Seam 1: recorded-shape Listings produce the expected top Build, and the Ceiling hides the rest."""

    @classmethod
    def setUpClass(cls):
        full = "2x Xeon E5-2680 v4. 128GB RAM. 2x 750W PSU. Dell HBA330. 12x 3.5\" caddies. Rails included."
        # all docs share one place ("X"); Oslo pickups there share one 952 NOK trip
        machines = [_doc(1, "Dell PowerEdge R730xd 12x LFF", 6000, 59.91, 10.72),    # 6,952, every fact good
                    _doc(2, "Dell PowerEdge R730xd 12x LFF", 35000, 59.91, 10.72),   # 35,952 + disks > Ceiling
                    _doc(3, "Dell PowerEdge R730xd 12x LFF", 5000, 59.91, 10.72)]    # caddies not stated
        descriptions = {"1": full, "2": full, "3": "2x Xeon E5-2680 v4. 128GB RAM. 2x 750W PSU. Dell HBA330. Rails included."}
        ship = ["shipping_exists", "seller_pays_shipping"]
        disks = ([_doc(10 + i, 'Seagate Exos X16 16TB 3.5" SATA', 1500 + i, 60.4, 5.5, ship) for i in range(6)]
                 + [_doc(20 + i, 'Seagate Exos X24 24TB 3.5" SATA', 2600, 60.4, 5.5, ship) for i in range(4)]
                 # pickup at the Machines' place: 1,450 + trip 952 alone, 1,450 once the trip is driven
                 + [_doc(30 + i, 'Seagate Exos X16 16TB 3.5" SATA', 1450, 59.91, 10.72) for i in range(5)])

        def blob(docs):
            return base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()

        def fetch(url):
            if "/search?" in url:
                q = urllib.parse.parse_qs(url.split("?", 1)[1])
                docs = machines if q["q"] == ["r730xd"] else disks if q["q"] == ["exos"] else []
                return f"<script>{blob(docs if q['condition'] == ['3', '4'] else [])}</script>"
            text = descriptions.get(url.rsplit("/", 1)[1], "")
            return f'<section data-testid="description"><p>{htmllib.escape(text)}</p></section>'

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__,
                      disk_queries={"finn": ["exos"]}, machine_queries=["r730xd"], pause=0, router=fake_router)
        cls.app.hunt()
        cls.page = cls.app.page()
        cls.builds = re.findall(r'data-build="([^"]+)" data-score="([\d.]+)" data-landed="([\d.]+)"', cls.page)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def rows(self):
        return {b: float(landed) for b, _, landed in self.builds}

    def test_top_build_recounts_unknown_caddies_and_shares_the_pickup_trip(self):
        # Machine 3: 5,000 + trip 952 + caddies (not stated) for 5 disks + 1 boot 600, not the stored 1,200;
        # disks: 5 x 1,450 picked up on the same trip (7,250) beat shipped 1,500..1,504 (7,510) and 4 x 24 TB
        build, score, landed = self.builds[0]
        self.assertEqual(build, "3")
        self.assertAlmostEqual(float(landed), 5952 + 600 + 7250, places=2)
        self.assertAlmostEqual(float(score), (5952 + 600 + 7250) / 41.47, delta=1)
        self.assertIn("caddies (not stated) 600", self.page)
        self.assertIn("5 &times; 16 TB", self.page)

    def test_machine_with_every_fact_good_gets_no_caddy_penalty(self):
        # 2 CPUs and 128 GB stated, heatsinks come with the CPUs: no Parts needed, so none are on sale here
        self.assertAlmostEqual(self.rows()["1"], 6952 + 7250, places=2)
        self.assertIn("<td>nothing</td>", self.page)
        self.assertIn("+ Parts 0 +", self.page)

    def test_ceiling_hides_the_expensive_machine(self):
        self.assertEqual(sorted(self.rows()), ["1", "3"])

    def test_the_page_shows_the_hunts_ranking_and_a_restart_ranks_again(self):
        pattern = r'data-build="([^"]+)" data-score="([\d.]+)" data-landed="([\d.]+)"'
        with mock.patch.object(self.app.store, "build_parts", side_effect=AssertionError("a page load ranked")):
            self.assertEqual(re.findall(pattern, self.app.page()), self.builds)
            self.assertFalse(self.app.mark_bought("finn|nope"))  # Mark as bought reads the ranking too
        with self.app.store._conn() as c:
            c.execute("DELETE FROM ranking")
        self.assertEqual(re.findall(pattern, self.app.page()), [])
        App(self.pg.get_uri(), [], fx=RATES.__getitem__, pause=0, router=fake_router)  # a new pod ranks at start-up
        self.assertEqual(re.findall(pattern, self.app.page()), self.builds)

    def test_a_failed_ranking_keeps_the_last_builds_and_says_so(self):
        pattern = r'data-build="([^"]+)" data-score="([\d.]+)" data-landed="([\d.]+)"'
        with mock.patch("dealfinder.store.Store.build_parts", side_effect=RuntimeError("new rule broke")):
            app = App(self.pg.get_uri(), [], fx=RATES.__getitem__, pause=0, router=fake_router)  # a new pod
        page = app.page()
        self.assertEqual(re.findall(pattern, page), self.builds)
        self.assertIn('data-fault="ranking">Ranking the Builds failed (hunt 1: new rule broke)', page)
        self.assertIn("dealfinder_ranking_failed 1", app.metrics())
        app.rank(app.store.last_hunt()["id"])  # the next Hunt's ranking works
        self.assertNotIn('data-fault="ranking"', app.page())
        self.assertIn("dealfinder_ranking_failed 0", app.metrics())

    def test_details_link_every_listing_and_sort_by_any_column(self):
        for fid in (3, 30, 31, 32, 33, 34):
            self.assertIn(f"https://www.finn.no/recommerce/forsale/item/{fid}", self.page)
        for col in ("score", "landed", "usable", "machine", "disks", "sources"):
            self.assertIn(f'href="/?sort={col}"', self.page)
            self.assertIn('data-build="1"', self.app.page(sort=col))


class CompleteBuildsEndToEnd(unittest.TestCase):
    """Seam 1: each Build buys the CPUs, RAM and heatsinks its Machine lacks from Part Listings; a Build that cannot
    be completed is hidden and counted per reason in /metrics."""

    OK = ' 2x 750W PSU. Dell HBA330. 12x 3.5" caddies. Rails included.'  # no Penalty: Machine NOK = its price

    @classmethod
    def setUpClass(cls):
        # free shipping: Landed NOK = price + Trygg betaling (_shipped); the picks are the same as before the fee
        ship = ["shipping_exists", "seller_pays_shipping"]
        oslo = (59.91, 10.72)  # every doc's place is "X"; pickups there cost a 952 NOK trip, driven once
        machines = [_doc(1, "Dell PowerEdge R730xd 12x LFF barebone", 3000, *oslo, ship),
                    _doc(2, "Dell PowerEdge R730xd 12x LFF", 4000, *oslo, ship),
                    _doc(3, "Dell PowerEdge R730xd 12x LFF", 4100, *oslo, ship),
                    _doc(4, "Dell PowerEdge R730xd 12x LFF", 4200, *oslo, ship),
                    _doc(5, "Dell PowerEdge R730xd 12x LFF", 4300, *oslo, ship),
                    _doc(6, "Dell PowerEdge R7415 12x LFF", 4400, *oslo, ship),       # 1 socket, AMD SP3
                    _doc(7, "Dell PowerEdge R730xd 12x LFF", 4500, *oslo),            # pickup, like CPU 44
                    _doc(8, "Dell PowerEdge R750 12x LFF", 5000, *oslo, ship),        # 15th Gen: no LGA4189 on sale
                    _doc(9, "Dell PowerEdge R760 12x LFF", 5000, *oslo, ship),        # 16th Gen: DDR5
                    _doc(17, "Dell PowerEdge R7425 12x LFF", 4600, *oslo, ship),      # 2 sockets, AMD SP3
                    _doc(18, "Dell PowerEdge R730xd 12x LFF", 4700, *oslo, ship)]     # LRDIMM installed
        cls.descriptions = {"1": "Barebone." + cls.OK, "2": "1x Xeon E5-2650 v4. 128GB RAM." + cls.OK,
                            "3": "2x Xeon E5-2680 v4. 4x16GB DDR4-2400 RAM." + cls.OK,
                            "4": "2x Xeon E5-2680 v4. 64GB RAM." + cls.OK, "5": "Ingen CPU. 2x HS. 128GB RAM." + cls.OK,
                            "6": "No CPU. 128GB RAM." + cls.OK, "7": "Ingen CPU. 2x HS. 128GB RAM." + cls.OK,
                            "8": "No CPU. 2x HS. 128GB RAM." + cls.OK, "9": "No CPU. 128GB RAM." + cls.OK,
                            "17": "No CPU. 2x HS. 128GB RAM." + cls.OK,
                            "18": "2x Xeon E5-2680 v4. 4x16GB DDR4-2400 LRDIMM." + cls.OK}
        cpus = [_doc(40, "Intel Xeon E5-2650 v4 CPU", 350, *oslo, ship),
                _doc(41, "Intel Xeon E5-2660 v4 CPU", 300, *oslo, ship),     # with 40: 650, but two models
                _doc(42, "2x Intel Xeon E5-2680 v4 CPU", 900, *oslo, ship),
                _doc(43, "AMD EPYC 7351P 16-Core SP3 CPU", 700, *oslo, ship),
                _doc(44, "2x Intel Xeon E5-2690 v4 CPU", 400, *oslo),         # pickup: 1,352, or 400 on a shared trip
                _doc(46, "2x AMD EPYC 7351P SP3 CPU", 1000, *oslo, ship),     # "P": single-socket only
                _doc(47, "2x AMD EPYC 7351 SP3 CPU", 1600, *oslo, ship),
                _doc(48, "Intel Xeon E5-2680 v4 CPU", 400, *oslo, ship)]      # with 42: 1,300 for 3 CPUs
        rams = [_doc(50, "4x 16GB DDR4-2400 ECC RDIMM", 1600, *oslo, ship),
                _doc(51, "8x 16GB DDR4-2133 ECC RDIMM", 2000, *oslo, ship),
                _doc(52, "2x 32GB DDR4-2400 ECC RDIMM", 900, *oslo, ship),   # with 53: 128 GB for 1,500, but mixed
                _doc(53, "2x 32GB DDR4-2400 ECC LRDIMM", 600, *oslo, ship),
                _doc(54, "16GB DDR4-2133 ECC RDIMM", 200, *oslo, ship),      # with 51: 2,200 for 9 sticks
                _doc(55, "4x 16GB DDR4-2400 ECC LRDIMM", 1800, *oslo, ship)]
        heatsinks = [_doc(60, "Dell PowerEdge R730 R730XD CPU Heatsink", 150, *oslo, ship),
                     _doc(61, "2 x Dell PowerEdge R730 R730XD CPU Heatsink", 250, *oslo, ship),
                     _doc(62, "Dell PowerEdge R7415 CPU Heatsink", 200, *oslo, ship)]
        disks = [_doc(10 + i, 'Seagate Exos X16 16TB 3.5" SATA', 1500 + i, 60.4, 5.5, ship) for i in range(5)]
        cls.docs = {"r730xd": machines, "exos": disks, "xeon": cpus, "rdimm": rams, "heatsink": heatsinks}

        def fetch(url):
            if "/search?" in url:
                q = urllib.parse.parse_qs(url.split("?", 1)[1])
                docs = cls.docs.get(q["q"][0], []) if q["condition"] == ["3", "4"] else []
                blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
                return f"<script>{blob}</script>"
            text = cls.descriptions.get(url.rsplit("/", 1)[1], "")  # Part item pages are fetched too
            return f'<section data-testid="description"><p>{htmllib.escape(text)}</p></section>'

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__,
                      disk_queries={"finn": ["exos"]}, machine_queries=["r730xd"], cpu_queries={"finn": ["xeon"]},
                      ram_queries={"finn": ["rdimm"]}, heatsink_queries={"finn": ["heatsink"]}, pause=0,
                      router=fake_router)
        cls.app.hunt()
        cls.page = cls.app.page()

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    DISKS = sum(_shipped(1500 + i) for i in range(5))  # 7,510 + Trygg betaling 596

    def expect(self, *nok, parts):
        """(Landed NOK, parts) as build() returns it, from the NOK of each Listing and the disks."""
        return round(sum(nok) + self.DISKS, 2), parts

    def build(self, mid):
        """(Landed NOK, {Part Listing: count used}) of the Build for Machine `mid`, None when hidden."""
        m = re.search(rf'<tr data-build="{mid}" data-score="[\d.]+" data-landed="([\d.]+)">(.*?)</tr>', self.page, re.S)
        return m and (float(m[1]), {p: int(n) for p, n in re.findall(r'data-part="(\d+)" data-count="(\d+)"', m[2])})

    def test_barebones_dual_socket_gets_two_same_model_cpus_a_full_set_and_two_heatsinks(self):
        # 3,000 + a pair of E5-2680 v4 900 (not 40 + 41: two models; not single 48 then pair 42: 1,300)
        # + 8x16GB DDR4-2133 2,000 (not 52 + 53: RDIMM with LRDIMM; not 54 then 51: 2,200) + a pair of heatsinks 250
        # + 5 disks 7,510
        self.assertEqual(self.build(1), self.expect(_shipped(3000), _shipped(900), _shipped(2000), _shipped(250),
                                                    parts={"42": 2, "51": 8, "61": 2}))
        machine, parts = _shipped(3000), _shipped(900) + _shipped(2000) + _shipped(250)
        self.assertIn(f"Total {machine + parts + self.DISKS:,.0f} NOK = Machine {machine:,.0f} + Parts {parts:,.0f}"
                      f" + Disks {self.DISKS:,.0f}", self.page)
        details = self.page.split('data-build="1"', 1)[1].split("</tr>", 1)[0]
        for fid in (42, 51, 61, 10, 14):
            self.assertIn(f"https://www.finn.no/recommerce/forsale/item/{fid}", details)
        self.assertIn(f'data-count="8" data-nok="{_shipped(2000):.2f}">RAM', details)

    def test_one_installed_cpu_gets_exactly_one_more_of_the_same_model(self):
        # "1x E5-2650 v4": one E5-2650 v4 350 (not the cheaper E5-2660 v4) and a heatsink for it 150
        self.assertEqual(self.build(2), self.expect(_shipped(4000), _shipped(350), _shipped(150),
                                                    parts={"40": 1, "60": 1}))

    def test_stated_sticks_are_topped_up_with_the_same_size_and_speed(self):
        # "4x16GB DDR4-2400": 4 more 16 GB 2400 sticks (1,600); not 2133 sticks nor the cheaper 2x 32GB
        self.assertEqual(self.build(3), self.expect(_shipped(4100), _shipped(1600), parts={"50": 4}))

    def test_a_stated_total_without_sticks_gets_a_full_new_set(self):
        self.assertEqual(self.build(4), self.expect(_shipped(4200), _shipped(2000), parts={"51": 8}))  # a new set

    def test_stated_heatsinks_are_not_bought_again(self):
        self.assertEqual(self.build(5), self.expect(_shipped(4300), _shipped(900), parts={"42": 2}))  # CPUs only

    def test_single_socket_machine_gets_one_cpu(self):
        self.assertEqual(self.build(6), self.expect(_shipped(4400), _shipped(700), _shipped(200),
                                                    parts={"43": 1, "62": 1}))

    def test_dual_socket_amd_gets_no_single_socket_p_cpus(self):
        # not the cheaper pair of 7351P (1,000)
        self.assertEqual(self.build(17), self.expect(_shipped(4600), _shipped(1600), parts={"47": 2}))

    def test_one_listing_covering_the_need_beats_a_cheaper_per_unit_start(self):
        # the pair 42 (900) alone, not single 48 (400) + pair 42 = 1,300 for 3 CPUs; same for the RAM kit 51
        self.assertEqual(self.build(5), self.expect(_shipped(4300), _shipped(900), parts={"42": 2}))
        self.assertEqual(self.build(4)[1], {"51": 8})

    def test_stated_lrdimm_sticks_are_topped_up_with_lrdimm(self):
        # not the cheaper RDIMM kit 50
        self.assertEqual(self.build(18), self.expect(_shipped(4700), _shipped(1800), parts={"55": 4}))

    def test_hidden_counts_come_from_the_last_hunt_and_survive_a_restart(self):
        app = App(self.pg.get_uri(), [], fx=RATES.__getitem__, pause=0, router=fake_router)  # a new pod, no Hunt
        app.store.build_parts = lambda: 1 / 0  # /metrics must not rank
        self.assertIn('dealfinder_builds_hidden{reason="no_cpu"} 1', app.metrics())
        self.assertIn('dealfinder_builds_hidden{reason="platform"} 1', app.metrics())

    def test_a_part_at_the_machine_pickup_place_shares_its_trip(self):
        # the pickup pair (400 + 952 trip) loses to the shipped pair (900 + fee 83) for a shipped Machine, and wins
        # at 400 once the Machine's own pickup trip is driven; a pickup pays no Trygg betaling
        self.assertEqual(self.build(7), self.expect(4500 + 952, 400, parts={"44": 2}))
        self.assertEqual(self.build(5)[1], {"42": 2})

    def test_uncompletable_and_unsupported_machines_are_hidden_and_counted(self):
        self.assertIsNone(self.build(8))  # no LGA4189 CPU on sale
        self.assertIsNone(self.build(9))  # 16th Gen
        metrics = self.app.metrics()
        for reason, n in (("no_cpu", 1), ("platform", 1), ("no_ram", 0), ("no_heatsink", 0), ("ceiling", 0)):
            self.assertIn(f'dealfinder_builds_hidden{{reason="{reason}"}} {n}', metrics)
        # a pair of LGA4189 CPUs on sale completes Machine 8 ("2x HS" stated) and takes it off the count
        self.docs["xeon"] = self.docs["xeon"] + [_doc(45, "2x Intel Xeon Gold 6330 CPU", 900, 59.91, 10.72,
                                                      ["shipping_exists", "seller_pays_shipping"])]
        try:
            self.app.hunt()
            self.assertIn('dealfinder_builds_hidden{reason="no_cpu"} 0', self.app.metrics())
            self.assertIn('data-build="8"', self.app.page())
        finally:
            self.docs["xeon"] = self.docs["xeon"][:-1]
            self.app.hunt()
        self.assertIn('dealfinder_builds_hidden{reason="no_cpu"} 1', self.app.metrics())

    def test_best_machines_show_needs_and_no_flat_cpu_ram_penalties(self):
        rows = dict(re.findall(r'data-machine="(\d+)" data-landed="([\d.]+)"', self.page))
        # barebones: no CPU/RAM Penalty in its Landed cost, only the price + Trygg betaling
        self.assertEqual(float(rows["1"]), _shipped(3000))
        for mid, needs in (("1", "2 CPU, 128 GB, 2 HS"), ("2", "1 CPU, 1 HS"), ("3", "64 GB"), ("5", "2 CPU"),
                           ("9", "unsupported platform")):
            row = self.page.split(f'data-machine="{mid}"', 1)[1].split("</tr>", 1)[0]
            self.assertIn(f"<td>{needs}</td>", row)
        for text in ("CPUs (not stated)", "RAM to 128 GB", "no_cpu", "ram_unknown"):
            self.assertNotIn(text, self.page)

    def test_mark_as_bought_records_the_parts(self):
        try:
            self.assertTrue(self.app.mark_bought("finn|1"))
            build = self.app.store.bought()["build"]
            self.assertEqual({(p["kind"], p["source_id"], p["count"]) for p in build["parts"]},
                             {("cpu", "42", 2), ("ram", "51", 8), ("heatsink", "61", 2)})
            self.assertAlmostEqual(build["landed_nok"], self.build(1)[0], places=2)
            self.assertIn("https://www.finn.no/recommerce/forsale/item/51", self.app.page())
        finally:
            with self.app.store._conn() as c:
                c.execute("DELETE FROM purchases")


def _ebay_item(iid, title, price, ship, pct="99.8", score=5000):
    it = {"itemId": iid, "title": title, "price": {"value": str(price), "currency": "GBP"}, "conditionId": "3000",
          "itemWebUrl": f"https://www.ebay.co.uk/itm/{iid}", "seller": {"username": "s", "feedbackPercentage": pct,
                                                                         "feedbackScore": score}}
    if ship is not None:
        it["shippingOptions"] = [{"shippingCost": {"value": str(ship), "currency": "GBP"}}]
    return it


class EbayMachinesAndWeakSellers(unittest.TestCase):
    """Seam 1: eBay UK Machines are read by the same rules; weak sellers pay +10%; no freight to Norway = excluded."""

    TEXT = "<p>2x Xeon E5-2680 v4</p><p>128GB RAM</p><p>2x 750W PSU</p><p>Dell HBA330</p><p>12x 3.5\" caddies</p><p>Rails included</p>"

    @classmethod
    def setUpClass(cls):
        items = [_ebay_item("1", 'Dell PowerEdge R730xd 12x 3.5" LFF', 400, 80),                    # strong seller
                 _ebay_item("2", 'Dell PowerEdge R730xd 12x 3.5" LFF', 380, 80, pct="97.5"),        # weak: < 98%
                 _ebay_item("3", 'Dell PowerEdge R730xd 12x 3.5" LFF', 300, None),                  # no freight to NO
                 _ebay_item("4", 'Dell PowerEdge R730xd 24x 2.5" SFF', 200, 80),                    # ruled out by title
                 _ebay_item("5", 'Dell PowerEdge R730xd 12x 3.5" LFF', 390, 80, score=49)]           # weak: < 50 ratings
        cls.calls = []

        def fetch(url, headers=None, data=None):
            cls.calls.append(url)
            if "oauth2/token" in url:
                return {"access_token": "t", "expires_in": 7200}
            if "/item/" in url:
                return {"description": cls.TEXT}
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return {"itemSummaries": items if q.get("category_ids") == ["11211"] else []}

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [EbaySource("id", "secret", fetch=fetch)], fx=RATES.__getitem__,
                      disk_queries={"ebay_uk": []}, machine_queries=["r730xd"], pause=0, router=fake_router)
        cls.app.hunt()
        cls.page = cls.app.page()
        cls.rows = {i: float(p) for i, p in re.findall(r'data-machine="([^"]+)" data-landed="([\d.]+)"', cls.page)}

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def test_strong_seller_machine_pays_freight_and_vat_and_no_penalty(self):
        # description says 2 PSUs, HBA330, 12 caddies, rails: every Penalty fact read from the eBay item text
        self.assertAlmostEqual(self.rows["1"], (400 + 80) * RATES["GBP"] * 1.25, places=1)

    def test_weak_seller_pays_ten_percent(self):
        base = (380 + 80) * RATES["GBP"] * 1.25
        self.assertAlmostEqual(self.rows["2"], base * 1.10, places=1)
        self.assertAlmostEqual(self.rows["5"], (390 + 80) * RATES["GBP"] * 1.25 * 1.10, places=1)
        self.assertIn("weak seller +10%", self.page)

    def test_machine_without_shipping_to_norway_is_excluded(self):
        self.assertNotIn("3", self.rows)
        self.assertNotIn('data-unreadable="3"', self.page)

    def test_item_text_fetched_only_when_the_title_does_not_rule_the_machine_out(self):
        fetched = [u.rsplit("/", 1)[1] for u in self.calls if "/item/" in u]
        self.assertEqual(sorted(fetched), ["1", "2", "5"])


class EbayDiskStockEndToEnd(unittest.TestCase):
    """Seam 1: an eBay Disk Listing with stock supplies several disks of one Build; the first unit pays the shipping,
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
            "M": {"description": EbayMachinesAndWeakSellers.TEXT},
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
                          disk_queries={"ebay_uk": ["exos 16tb"]}, machine_queries=["r730xd"], pause=0,
                          router=fake_router)
            cls.app.hunt()
        cls.page, cls.first_hunt = cls.app.page(), list(cls.calls)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def stored(self, iid):
        with self.app.store._conn() as c:
            return c.execute("SELECT facts, costs, landed_nok FROM listings WHERE source_id = %s", (iid,)).fetchone()

    def test_one_seller_with_stock_supplies_the_whole_build(self):
        # 5 from A: 220 + 4 x 205 = 1,040 GBP, not 5 singles at 210 = 1,050 GBP; the Machine 480 GBP; VAT on all
        gbp = RATES["GBP"] * 1.25
        row = re.search(r'<tr data-build="M" data-score="[\d.]+" data-landed="([\d.]+)">(.*?)</tr>', self.page, re.S)
        self.assertAlmostEqual(float(row[1]), (480 + 220 + 4 * 205) * gbp, places=1)
        self.assertEqual(re.findall(r'data-disk="(\w+)" data-count="(\d+)"', row[2]), [("A", "5")])
        self.assertIn("5 &times; 16 TB", row[2])
        self.assertIn(f"5 used, {(220 + 4 * 205) * gbp:,.0f} NOK", row[2])
        self.assertIn(f"each after the first {205 * gbp:,.0f}", row[2])

    def test_more_than_is_its_threshold_capped_per_buyer_and_unknown_extra_shipping_is_charged_again(self):
        g = self.stored("G")
        self.assertEqual(g["facts"]["stock"], 3)
        self.assertAlmostEqual(g["costs"]["extra_unit"], float(g["landed_nok"]), places=2)
        self.assertEqual(self.stored("A")["facts"]["stock"], 10)
        self.assertEqual(self.stored("B")["facts"]["stock"], 1)
        self.assertNotIn("extra_unit", self.stored("B")["costs"])

    def test_item_calls_go_to_the_cheapest_qualifying_disks_and_a_failed_one_counts_one_disk(self):
        fetched = [u.rsplit("/", 1)[1] for u in self.first_hunt if "/item/" in u]
        self.assertEqual(sorted(fetched), sorted("MBCDEFAGH"))
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
                      disk_queries={"ebay_uk": ["exos 16tb"]}, machine_queries=["r730xd"], pause=0,
                      router=fake_router)
            app.hunt()
        self.assertEqual(sorted(u.rsplit("/", 1)[1] for u in calls if "/item/" in u), ["H", "M"])  # M: its text
        self.assertEqual(self.stored("A")["facts"]["stock"], 10)


class FinnDiskLotsEndToEnd(unittest.TestCase):
    """Seam 1: a title that names several disks ("4x 16TB") is one lot at its price; one priced per disk (title,
    description, or under DISK_MIN_NOK_PER_TB) is one disk, sold up to the stock its text states."""

    @classmethod
    def setUpClass(cls):
        full = "2x Xeon E5-2680 v4. 128GB RAM. 2x 750W PSU. Dell HBA330. 12x 3.5\" caddies. Rails included."
        ship = ["shipping_exists", "seller_pays_shipping"]
        machines = [_doc(1, "Dell PowerEdge R730xd 12x LFF", 6000, 59.91, 10.72, ship)]
        disks = [_doc(10, '4x Seagate Exos X16 16TB 3.5" SATA', 7000, 60.4, 5.5, ship),       # a lot: 1,750 a disk
                 _doc(11, '4 stk Seagate Exos X16 16TB 3.5" SATA', 1900, 60.4, 5.5, ship),    # description: per disk
                 _doc(12, '5 stk Seagate Exos X16 16TB 3.5" SATA', 1300, 60.4, 5.5, ship)]    # 16 NOK/TB: per disk
        disks += [_doc(20 + i, 'Seagate Exos X16 16TB 3.5" SATA', 2000 + i, 60.4, 5.5, ship) for i in range(3)]
        descriptions = {"1": full, "10": "Fire disker, selges samlet.", "11": "Fire like disker. Pris per stk.",
                        "12": "Fem disker."}

        def fetch(url):
            if "/search?" in url:
                q = urllib.parse.parse_qs(url.split("?", 1)[1])
                docs = (machines if q["q"] == ["r730xd"] else disks) if q["condition"] == ["3", "4"] else []
                blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
                return f"<script>{blob}</script>"
            return f'<section data-testid="description"><p>{descriptions.get(url.rsplit("/", 1)[1], "")}</p></section>'

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__,
                      disk_queries={"finn": ["exos"]}, machine_queries=["r730xd"], pause=0, router=fake_router)
        cls.app.hunt()
        cls.page = cls.app.page()

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def test_the_lot_and_the_cheapest_single_make_the_build(self):
        # 5 disks: the lot of 4 (7,000) + the 1,300 single, not 5 singles at 1,300..2,002 (8,703 + fees)
        row = re.search(r'<tr data-build="1" data-score="[\d.]+" data-landed="([\d.]+)">(.*?)</tr>', self.page, re.S)
        self.assertAlmostEqual(float(row[1]), _shipped(6000) + _shipped(7000) + _shipped(1300), places=2)
        self.assertEqual(sorted(re.findall(r'data-disk="(\d+)" data-count="(\d+)"', row[2])), [("10", "4"), ("12", "1")])
        self.assertIn(f"4 used, {_shipped(7000):,.0f} NOK", row[2])

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


class PickStock(unittest.TestCase):
    """The Disk picker takes a Listing's extra units only after its first, and never more than its stock."""

    @staticmethod
    def row(nok, units=1, extra=None):
        return {"_nok": nok, "_trip": 0, "_place": None, "_units": units, "_extra": nok if extra is None else extra}

    def test_a_lot_is_taken_whole_before_dearer_singles(self):
        lot, singles = self.row(6000, units=4, extra=0), [self.row(2000) for _ in range(5)]
        picks, total, _ = _pick_stock([lot, *singles], 5, set())
        self.assertEqual(total, 6000 + 2000)
        self.assertEqual(sorted(n for _, n, _ in picks), [1, 4])

    def test_extra_units_follow_the_first_and_stock_is_a_limit(self):
        a, b, c = self.row(100, units=2, extra=40), self.row(90), self.row(95)
        picks, total, _ = _pick_stock([a, b, c], 4, set())
        self.assertEqual(total, 90 + 95 + 100 + 40)
        self.assertEqual(sorted(n for _, n, _ in picks), [1, 1, 2])
        self.assertIsNone(_pick_stock([a, b, c], 5, set()))


class CpusEndToEnd(unittest.TestCase):
    """Seam 1: CPU Listings from eBay UK and finn.no land on the Best CPUs tab at their Landed cost per CPU."""

    @classmethod
    def setUpClass(cls):
        items = [_ebay_item("c1", "Intel Xeon E5-2680 v4 SR2N7 2.4GHz 14 Core 28 Thread LGA2011-3 CPU", 19.36, 2.94),
                 _ebay_item("c2", "2x Intel Xeon Gold 6130 SR3B9 2.10GHz 22MB L3 Cache 16-Core CPU Processor", 31.90, 2.94),
                 _ebay_item("c3", "Dell PowerEdge R730 2x E5-2680 v4 128GB", 150, 20),                        # a server
                 _ebay_item("c4", "Matched Pair Intel Xeon E5-2690 V4 E5-2680 V4 E5-2660 V4 E5-2650V4 LGA2011-3 CPU",
                            22.79, 0)]                                                                      # which model?
        cls.filters = []

        def ebay(url, headers=None, data=None):
            if "oauth2/token" in url:
                return {"access_token": "t", "expires_in": 7200}
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            cls.filters.append((q.get("category_ids"), q["filter"][0]))
            return {"itemSummaries": items if q.get("category_ids") == ["164"] else []}

        def finn(url):
            if "/search?" not in url:
                return ""  # an item page with no description
            q = urllib.parse.parse_qs(url.split("?", 1)[1])
            docs = [_doc(50, "2 stk Intel Xeon E5-2690 v4, selges samlet eller hver for seg", 850, 59.9, 10.7,
                         ["shipping_exists", "seller_pays_shipping"])]
            docs = docs if q["q"] == ["xeon e5"] and q["condition"] == ["3", "4"] else []
            blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
            return f"<script>{blob}</script>"

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [EbaySource("id", "secret", fetch=ebay), FinnSource(fetch=finn, pause=0)],
                      fx=RATES.__getitem__, disk_queries={"ebay_uk": []}, machine_queries=["r730xd"],
                      cpu_queries={"ebay_uk": ["e5-2680 v4"], "finn": ["xeon e5"]}, pause=0, router=fake_router)
        cls.app.hunt()
        cls.page = cls.app.page()
        cls.rows = [(i, float(n)) for i, n in re.findall(r'data-cpu="([^"]+)" data-nok-per-cpu="([\d.]+)"', cls.page)]

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def test_best_cpus_show_landed_nok_per_cpu_sorted(self):
        one = (19.36 + 2.94) * RATES["GBP"] * 1.25               # eBay: price + shipping + VAT
        pair = (31.90 + 2.94) * RATES["GBP"] * 1.25 / 2          # one Listing, two CPUs
        self.assertEqual([i for i, _ in self.rows], ["c2", "c1", "50"])
        rows = dict(self.rows)
        self.assertAlmostEqual(rows["c1"], one, places=1)
        self.assertAlmostEqual(rows["c2"], pair, places=1)
        self.assertAlmostEqual(rows["50"], _shipped(850) / 2, places=2)   # finn: free shipping + fee, no VAT
        for text in ("<td>LGA3647</td><td>Gold 6130</td><td>2</td>", "<td>LGA2011-3</td><td>E5-2680 v4</td><td>1</td>"):
            self.assertIn(text, self.page)

    def test_server_in_a_cpu_search_is_not_listed_and_unreadable_cpus_are(self):
        self.assertNotIn("c3", dict(self.rows))
        self.assertNotIn('data-unreadable="c3"', self.page)
        self.assertIn('data-unreadable="c4" data-missing="model"', self.page)

    def test_ebay_cpu_search_uses_its_category_and_price_range(self):
        self.assertIn((["164"], "buyingOptions:{FIXED_PRICE},deliveryCountry:NO,price:[3..600],priceCurrency:GBP"),
                      self.filters)
        self.assertIn('dealfinder_listings{source="ebay_uk",kind="cpu",state="qualified"} 2', self.app.metrics())

    def test_search_and_track_accept_cpus(self):
        page = self.app.search_page("xeon e5", "cpu")
        self.assertIn('data-result="50" data-qualifies="1"', page)
        self.assertIn("<th>NOK per CPU</th>", page)
        order = re.findall(r'data-result="([^"]+)" data-qualifies="1"', page)
        self.assertEqual(order, ["c2", "c1", "50"])                  # ranked by NOK per CPU, like the tab
        self.assertIn(f"<td>{_shipped(850):,.0f}</td><td>{_shipped(850) / 2:,.0f}</td>",
                      page.split('data-result="50"', 1)[1].split("</tr>", 1)[0])
        self.assertIn('<option value="cpu">CPUs</option>', self.page)
        self.assertIn('data-tracked="cpu:e5-2680 v4"', self.page)


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
                      fx=RATES.__getitem__, disk_queries={"ebay_uk": []}, machine_queries=["r730xd"],
                      cpu_queries={"ebay_uk": []}, ram_queries={"ebay_uk": ["ddr4 ecc rdimm 32gb"], "finn": ["rdimm"]},
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
        self.assertEqual(next(r["facts"]["sticks"] for r in self.app.store.best_listings("ram") if r["source_id"] == "61"), 1)

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


class HeatsinksEndToEnd(unittest.TestCase):
    """Seam 1: heatsink Listings from eBay UK and finn.no land on the Heatsinks tab with the models they fit."""

    @classmethod
    def setUpClass(cls):
        items = [_ebay_item("h1", "Dell (YY2R8) PowerEdge R730, R730XD Heatsink (0YY2R8)", 20.00, 0),
                 _ebay_item("h2", "2 x Dell Poweredge R730 R730XD R7910 CPU Processor Cooling Heatsink YY2R8 0YY2R8",
                            36.00, 0),
                 _ebay_item("h3", "Dell R730 fan 0HR6C", 10, 2),                                        # a fan
                 _ebay_item("h4", "Dell R730 2x E5-2680 v4 2x heatsink 64GB", 100, 20),                 # a server
                 _ebay_item("h5", "Dell PowerEdge server CPU heatsink", 5, 0)]                          # which model?
        cls.filters = []

        def ebay(url, headers=None, data=None):
            if "oauth2/token" in url:
                return {"access_token": "t", "expires_in": 7200}
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            cls.filters.append((q.get("category_ids"), q["filter"][0]))
            return {"itemSummaries": items if "price:[2..150]" in q["filter"][0] else []}

        def finn(url):
            if "/search?" not in url:
                return ""  # an item page with no description
            q = urllib.parse.parse_qs(url.split("?", 1)[1])
            docs = [_doc(70, "2 stk Performance kjøleribbe til HP ML350 Gen10", 800, 59.9, 10.7,
                         ["shipping_exists", "seller_pays_shipping"])]
            docs = docs if q["q"] == ["kjøleribbe server"] and q["condition"] == ["3", "4"] else []
            blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
            return f"<script>{blob}</script>"

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [EbaySource("id", "secret", fetch=ebay), FinnSource(fetch=finn, pause=0)],
                      fx=RATES.__getitem__, disk_queries={"ebay_uk": []}, machine_queries=["r730xd"],
                      cpu_queries={"ebay_uk": []}, ram_queries={"ebay_uk": []},
                      heatsink_queries={"ebay_uk": ["r730 heatsink"], "finn": ["kjøleribbe server"]},
                      pause=0, router=fake_router)
        cls.app.hunt()
        cls.page = cls.app.page()
        cls.rows = [(i, float(n)) for i, n in
                    re.findall(r'data-heatsink="([^"]+)" data-nok-per-heatsink="([\d.]+)"', cls.page)]

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()

    def test_heatsinks_show_fits_count_and_landed_nok_sorted_per_heatsink(self):
        one = 20.00 * RATES["GBP"] * 1.25                         # eBay: price + free shipping + VAT
        self.assertEqual([i for i, _ in self.rows], ["h2", "h1", "70"])
        rows = dict(self.rows)
        self.assertAlmostEqual(rows["h1"], one, places=1)
        self.assertAlmostEqual(rows["h2"], 36.00 * RATES["GBP"] * 1.25 / 2, places=1)  # one Listing, 2 heatsinks
        self.assertAlmostEqual(rows["70"], _shipped(800) / 2, places=2)    # finn: free shipping + fee, no VAT
        self.assertIn(f"<td>R730, R730xd</td><td>1</td><td>ebay_uk</td><td>{one:,.0f}</td>", self.page)
        self.assertIn("<td>R730, R730xd, R7910</td><td>2</td>", self.page)
        self.assertIn("<td>ML350 Gen10</td><td>2</td>", self.page)

    def test_fan_and_server_in_a_heatsink_search_are_not_listed_and_unreadable_heatsinks_are(self):
        for iid in ("h3", "h4"):
            self.assertNotIn(iid, dict(self.rows))
            self.assertNotIn(f'data-unreadable="{iid}"', self.page)
        self.assertIn('data-unreadable="h5" data-missing="fits"', self.page)
        self.assertIn("Machine model it fits", self.page)

    def test_ebay_heatsink_search_uses_no_category_and_its_price_range(self):
        self.assertIn((None, "buyingOptions:{FIXED_PRICE},deliveryCountry:NO,price:[2..150],priceCurrency:GBP"),
                      self.filters)
        self.assertIn('dealfinder_listings{source="ebay_uk",kind="heatsink",state="qualified"} 2', self.app.metrics())

    def test_search_and_track_accept_heatsinks(self):
        page = self.app.search_page("kjøleribbe server", "heatsink")
        self.assertIn('data-result="70" data-qualifies="1"', page)
        self.assertIn("<th>NOK per heatsink</th>", page)
        self.assertIn(f"<td>{_shipped(800):,.0f}</td><td>{_shipped(800) / 2:,.0f}</td>",
                      page.split('data-result="70"', 1)[1].split("</tr>", 1)[0])
        self.assertIn('<option value="heatsink">Heatsinks</option>', self.page)
        self.assertIn('data-tracked="heatsink:r730 heatsink"', self.page)


class FinnFeesAndPerUnitEndToEnd(unittest.TestCase):
    """Seam 1: a finn.no Listing bought via Fiks ferdig pays the per-kind shipping estimate and Trygg betaling; a
    pickup pays the trip and no fee; a price per unit in the description supplies one unit (finn.no 471846605)."""

    @classmethod
    def setUpClass(cls):
        oslo, fiks = (59.91, 10.72), ["shipping_exists"]  # buyer pays shipping: finn does not publish its price
        full = "2x Xeon E5-2680 v4. 128GB RAM. 2x 750W PSU. Dell HBA330. 12x 3.5\" caddies. Rails included."
        free = ["shipping_exists", "seller_pays_shipping"]
        rams = [_doc(80, "4x 16Gb DDR4 2400 ECC RDIMM", 800, *oslo, fiks),
                _doc(83, "2x 32GB DDR4-2400 ECC RDIMM", 1000, *oslo),                              # pickup only
                _doc(85, "128 GB (8 x 16 GB) Micron DDR4-2400 ECC RDIMM VLR", 5800, *oslo, free),   # live 477364211
                _doc(86, "Kingston 16GB DDR4 2666 ECC RDIMM", 500, *oslo, fiks),                   # one stick
                _doc(87, "2x 16GB DDR4-2400 ECC RDIMM", 700, *oslo)]                  # pickup only by flags
        cls.docs = {"rdimm": rams, "ddr4 ecc": rams,
                    "xeon": [_doc(88, "4 stk E5-2697v2", 650, *oslo, fiks)],                       # live 272306459
                    "kjøleribbe": [_doc(89, "2 stk Performance kjøleribbe til HP ML350 Gen10", 800, *oslo, free)],
                    "exos": [_doc(81, 'Seagate Exos X16 16TB 3.5" SATA', 2000, *oslo, fiks)],
                    "r730xd": [_doc(82, "Dell PowerEdge R730xd 12x LFF", 5000, *oslo, fiks),
                               _doc(84, "Dell PowerEdge R730xd 12x LFF", 5000, 59.3, 10.2, fiks)]}  # 60 km away
        descriptions = {"80": "Bare 4 igjen 16Gb DDR4 2400 ECC RDIMN !!! Pris per srk.", "82": full, "84": full,
                        "85": "Kan også selges enkeltvis for 750kr per brikke. Spesifikasjoner per brikke: 16GB per modul.",
                        "87": "Gratis frakt i hele Norge.", "88": "Selger 4 stk. (pris er per stk)",
                        "89": "Passer HP ML350 Gen10.\nPris pr stk."}  # live 433624847
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
            return (60.0, 50.0) if lat < 59.5 else fake_router(lat, lon)  # trip 2 x 60 x 4 = 480

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__,
                      disk_queries={"finn": ["exos"]}, machine_queries=["r730xd"], cpu_queries={"finn": ["xeon"]},
                      ram_queries={"finn": ["rdimm", "ddr4 ecc"]}, heatsink_queries={"finn": ["kjøleribbe"]}, pause=0,
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
        landed = next(r["landed_nok"] for r in self.app.store.best_listings("ram") if r["source_id"] == "80")
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

    def test_pris_per_stk_counts_one_unit_after_any_number(self):
        # "Gen10.\nPris pr stk." and "(pris er per stk)": one heatsink and one CPU, not 2 and 4
        for query, kind, fid in (("kjøleribbe", "heatsink", "89"), ("xeon", "cpu", "88")):
            rows, _ = self.app.search(query, kind)
            self.assertEqual(next(r["facts"]["count"] for r in rows if r["source_id"] == fid), 1, fid)

    def test_only_multi_unit_parts_are_read_once_across_queries(self):
        # Machines are always read; a Part only when its title names several units, and once for both RAM queries;
        # a qualifying Disk for its stock (81); the single stick 86 never
        self.assertEqual(sorted(self.fetched), ["80", "81", "82", "83", "84", "85", "87", "88", "89"])

    def test_a_part_offering_free_shipping_in_its_text_is_shipped(self):
        # pickup only by finn's flags, but "Gratis frakt" in the text: 700 + Trygg betaling, no trip
        per_gb = float(re.search(r'data-ram="87" data-nok-per-gb="([\d.]+)"', self.page)[1])
        self.assertAlmostEqual(per_gb, _shipped(700) / 32, places=1)
        self.assertNotIn("pickup trip", self.row("ram", 87))

    def test_disk_with_fiks_ferdig_pays_the_small_parcel_estimate_and_fee(self):
        per_tb = float(re.search(r'data-listing="81" data-nok-per-tb="([\d.]+)"', self.page)[1])
        self.assertAlmostEqual(per_tb, (2000 + 65 + _fee(2000)) / 16, places=1)

    def test_machine_with_fiks_ferdig_pays_the_heavy_parcel_estimate_and_fee(self):
        # trip 952 is dearer than 400 + fee 329, so the estimate; no Penalty (every fact stated)
        landed = float(re.search(r'data-machine="82" data-landed="([\d.]+)"', self.page)[1])
        self.assertAlmostEqual(landed, 5000 + 400 + _fee(5000), places=2)

    def test_near_pickup_pays_the_trip_and_no_fee(self):
        # pickup only: 1,000 + trip 952, no Trygg betaling
        per_gb = float(re.search(r'data-ram="83" data-nok-per-gb="([\d.]+)"', self.page)[1])
        self.assertAlmostEqual(per_gb * 64, 1000 + 952, places=1)
        self.assertNotIn("Trygg betaling", self.row("ram", 83))
        # Fiks ferdig, but a 480 NOK trip beats 400 + fee 329: picked up, no fee
        landed = float(re.search(r'data-machine="84" data-landed="([\d.]+)"', self.page)[1])
        self.assertAlmostEqual(landed, 5000 + 480, places=2)
        self.assertIn("pickup trip 480", self.row("machine", 84))


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
        for q in ("r730xd", "r740xd", "r730", "r740", "dl380 gen9", "dl380 gen10", "supermicro 12 bay"):
            self.assertIn((q, "machine"), tracked)
        for q in ("exos 14tb", "exos 16tb", "exos 18tb", "exos 20tb"):
            self.assertIn((q, "disk"), tracked)
        for q in ("e5-2680 v4", "xeon gold 6130", "epyc 7302", "xeon e5", "epyc"):
            self.assertIn((q, "cpu"), tracked)
        for q in ("ddr4 ecc rdimm 16gb", "ddr4 ecc rdimm 32gb", "ddr4 lrdimm 64gb", "ddr4 ecc", "rdimm"):
            self.assertIn((q, "ram"), tracked)
        for q in ("r730 heatsink", "r730xd heatsink", "r740 heatsink", "dl380 gen9 heatsink", "dl380 gen10 heatsink",
                  "kjøleribbe server", "kjøler hp"):
            self.assertIn((q, "heatsink"), tracked)
        before = len(self.app.store.tracked())
        App(self.pg.get_uri(), self.sources, fx=RATES.__getitem__, disk_queries={"finn": ["other"]}, pause=0,
            router=fake_router)  # a restart must not re-seed
        self.assertEqual(len(self.app.store.tracked()), before)

    def test_bad_input_is_rejected(self):
        for path in ("/search?q=&kind=disk", "/search?q=x&kind=car"):
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


class MarkAsBought(unittest.TestCase):
    """Seam 1: Mark as bought records the Build and its Listings, and no Hunt calls a Source again."""

    @classmethod
    def setUpClass(cls):
        cls.calls = []
        full = "2x Xeon E5-2680 v4. 128GB RAM. 2x 750W PSU. Dell HBA330. 12x 3.5\" caddies. Rails included."
        ship = ["shipping_exists", "seller_pays_shipping"]
        machines = [_doc(1, "Dell PowerEdge R730xd 12x LFF", 6000, 59.91, 10.72)]
        disks = [_doc(10 + i, 'Seagate Exos X16 16TB 3.5" SATA', 1500 + i, 60.4, 5.5, ship) for i in range(5)]

        def fetch(url):
            cls.calls.append(url)
            if "/search?" in url:
                q = urllib.parse.parse_qs(url.split("?", 1)[1])
                docs = (machines if q["q"] == ["r730xd"] else disks) if q["condition"] == ["3", "4"] else []
                blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
                return f"<script>{blob}</script>"
            return f'<section data-testid="description"><p>{htmllib.escape(full)}</p></section>'

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__,
                      disk_queries={"finn": ["exos"]}, machine_queries=["r730xd"], pause=0, router=fake_router)
        cls.server = cls.app.make_server("127.0.0.1", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.app.hunt()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.pg.cleanup()

    def post(self, path, body=b""):
        with urllib.request.urlopen(urllib.request.Request(self.base + path, data=body, method="POST")) as r:
            return r.url, r.read().decode()

    def test_mark_as_bought_then_no_hunt_calls_a_source(self):
        page = self.app.page()
        self.assertIn('action="/buy"', page)
        self.assertIn('data-copy="finn|1"', page)  # Copy ID copies the same key the /buy form posts
        self.assertIn('<details><summary title="More">&#9662;</summary><form method="post" action="/buy"', page)  # buy sits in the dropdown
        self.assertIn("?hunt=gone", self.post("/buy", b"machine=finn%7C999")[0])  # not a shown Build: nothing kept
        self.assertIsNone(self.app.store.bought())

        url, page = self.post("/buy", b"machine=finn%7C1")
        self.assertIn("?hunt=bought", url)
        build = self.app.store.bought()["build"]
        self.assertEqual(build["machine"]["source_id"], "1")
        self.assertEqual(sorted(d["source_id"] for d in build["disks"]), ["10", "11", "12", "13", "14"])
        # pickup Machine 6,952 (no fee) + 5 shipped disks 7,510 + Trygg betaling on each
        self.assertAlmostEqual(build["landed_nok"], 6952 + sum(_shipped(p) for p in range(1500, 1505)), places=2)
        self.assertIn('data-bought="1"', page)                    # the page shows the bought Build...
        self.assertIn("https://www.finn.no/recommerce/forsale/item/14", page)
        self.assertNotIn('action="/buy"', page)                    # ...and offers no more buttons
        self.assertIn('data-copy="finn|1"', page)                  # Copy ID stays after the purchase
        self.assertNotIn('action="/hunt"', page)

        self.calls.clear()
        stop = threading.Event()
        scheduler = threading.Thread(target=self.app.run_scheduler, kwargs={"stop": stop, "interval": 0, "retry": 0.05})
        scheduler.start()                                          # a scheduled Hunt is due at once
        threading.Event().wait(0.5)
        stop.set()
        scheduler.join(5)
        self.assertIn("?hunt=bought", self.post("/hunt")[0])      # Hunt now does nothing
        self.app.hunt()                                            # nor does a direct Hunt
        self.assertEqual(self.calls, [])
        self.assertIn("dealfinder_bought 1", self.app.metrics())
        self.assertFalse(self.app.mark_bought("finn|1"))           # bought once, for good
        self.assertFalse(self.app.store.record_purchase({"x": 1}))  # the table itself refuses a second row


class TrackedQuerySeeding(unittest.TestCase):
    """Seam 1: starting Tracked queries per (kind, Source) group, and the migration that drops a removed Source."""

    def test_a_new_source_gets_its_starting_queries_on_an_already_seeded_database(self):
        pg = _pg()
        try:
            App(pg.get_uri(), [], fx=RATES.__getitem__, disk_queries={"finn": ["exos"]}, machine_queries=["r730xd"],
                cpu_queries={"finn": []}, ram_queries={"finn": []},
                heatsink_queries={"finn": []})  # a database from before Parts
            app = App(pg.get_uri(), [], fx=RATES.__getitem__, disk_queries={"finn": ["other"], "ebay_uk": ["x20"]},
                      machine_queries=["r740"], cpu_queries={"finn": ["xeon e5"]}, ram_queries={"finn": ["rdimm"]},
                      heatsink_queries={"finn": ["heatsink"]})
            got = {(r["query"], r["kind"], r["source"]) for r in app.store.tracked()}
            self.assertEqual(got, {("exos", "disk", "finn"), ("r730xd", "machine", ""), ("x20", "disk", "ebay_uk"),
                                   ("xeon e5", "cpu", "finn"), ("rdimm", "ram", "finn"),
                                   ("heatsink", "heatsink", "finn")})
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
        full = "2x Xeon E5-2680 v4. 128GB RAM. 2x 750W PSU. Dell HBA330. 12x 3.5\" caddies. Rails included."
        ship = ["shipping_exists", "seller_pays_shipping"]

        def fetch(url):
            if "/search?" in url:
                q = urllib.parse.parse_qs(url.split("?", 1)[1])
                disks = [_doc(10 + i, 'Seagate Exos X16 16TB 3.5" SATA', cls.disk_price + 100 * i, 60.4, 5.5, ship)
                         for i in range(5)]
                parts = {"r730xd": [_doc(1, "Dell PowerEdge R730xd 12x LFF", 6000, 59.91, 10.72)],
                         "xeon": [_doc(40, "2x Intel Xeon E5-2680 v4 CPU", 1000, 60.4, 5.5, ship)],
                         "rdimm": [_doc(50, "4x 32GB DDR4-2400 ECC RDIMM", 2000, 60.4, 5.5, ship)],
                         "heatsink": [_doc(60, "2 x Dell PowerEdge R730 CPU Heatsink", 300, 60.4, 5.5, ship)]}
                docs = parts.get(q["q"][0], disks) if q["condition"] == ["3", "4"] else []
                blob = base64.b64encode(json.dumps({"queries": [{"state": {"data": {"docs": docs}}}]}).encode()).decode()
                return f"<script>{blob}</script>"
            return f'<section data-testid="description"><p>{htmllib.escape(full)}</p></section>'

        cls.pg = _pg()
        cls.app = App(cls.pg.get_uri(), [FinnSource(fetch=fetch, pause=0)], fx=RATES.__getitem__,
                      disk_queries={"finn": ["exos"]}, machine_queries=["r730xd"], cpu_queries={"finn": ["xeon"]},
                      ram_queries={"finn": ["rdimm"]}, heatsink_queries={"finn": ["heatsink"]}, pause=0,
                      router=fake_router)
        cls.app.hunt()
        import psycopg
        with psycopg.connect(cls.pg.get_uri(), autocommit=True) as c:  # make Hunt 1 a week old
            c.execute("UPDATE price_observations SET seen_at = seen_at - interval '7 days'")
            c.execute("UPDATE hunts SET started = started - interval '7 days'")
            # Hunt 1 as recorded before Parts had history: no kind, and no key or units for a Part
            c.execute("UPDATE price_observations SET kind = NULL, units = NULL, "
                      "model = CASE WHEN source_id = '1' THEN model END")
        cls.app.store.migrate()  # keys the old Part observations from their Listings
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

    def test_machine_model_row_and_best_build_per_week(self):
        (old, new), _ = self.cells("R730xd|finn")
        self.assertEqual(old[0], "6,952")                         # 6,000 + pickup trip 952
        self.assertEqual(len(re.findall("data-best-week=", self.page)), 2)
        landed = 6952 + sum(_shipped(p) for p in (1200, 1300, 1400, 1500, 1600))  # 14,517: disks pay Trygg betaling
        self.assertIn(f"{landed / 41.47:,.0f}", self.page)  # this week's Score
        metrics = self.app.metrics()
        self.assertRegex(metrics, r"dealfinder_best_build_score 350\.\d+")  # 14,517 / 41.47 TiB
        self.assertIn(f"dealfinder_best_build_landed_nok {landed:g}", metrics)

    def test_a_hunt_that_shows_no_build_zeroes_the_best_build_gauges(self):
        import psycopg
        hunt = self.app.store.start_hunt()
        try:
            self.app.store.set_best_build(hunt, None, {"ceiling": 3})  # every Build over the Ceiling
            metrics = self.app.metrics()
            self.assertIn("dealfinder_best_build_score 0\n", metrics)
            self.assertIn("dealfinder_best_build_landed_nok 0\n", metrics)
            self.assertEqual(len(re.findall("data-best-week=", self.app.history_page())), 2)  # history keeps its weeks
        finally:  # the class shares one database
            with psycopg.connect(self.pg.get_uri(), autocommit=True) as c:
                c.execute("DELETE FROM hunts WHERE id = %s", (hunt,))

    def test_parts_have_weekly_rows_per_unit_including_old_hunts(self):
        for key, landed, units in (("E5-2680 v4|finn", _shipped(1000), 2), ("DDR4 RDIMM 32 GB|finn", _shipped(2000), 128),
                                   ("R730|finn", _shipped(300), 2)):
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
        # one Listing per Machine model or Part is too little history for a deal price
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
        for html, labels in ((self.page, ["Best Build per week", "Disks, NOK per TB", "Machines, NOK", "CPUs, NOK per CPU",
                                          "RAM, NOK per GB", "Heatsinks, NOK per heatsink"]),
                             (self.app.page(), ["Builds", "Best Disks", "Best Machines", "Best CPUs", "Best RAM",
                                              "Heatsinks", "Could not read"])):
            self.assertEqual(re.findall(r'<label for="tab\d">([^<]+)</label>', html), labels)
            self.assertEqual(html.count("<section>"), len(labels))
            self.assertEqual(html.count('name="tab" id="tab0" checked'), 1)
            self.assertIn(f".tabs>input:nth-of-type({len(labels)}):checked~section:nth-of-type({len(labels)})", html)
            self.assertIn("th{position:sticky;top:0", html)
