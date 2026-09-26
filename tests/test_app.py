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

import pgserver

from dealfinder.app import App, next_hunt_delay
from dealfinder.costs import DailyFx
from dealfinder.rules import Unreadable, read_disk
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
        # Kristiansand R730, 12,000 NOK: free shipping (0), 2 PSUs, 8 bays no caddies stated 800,
        # controller not stated 500 (unknown is charged), rails not stated 400.
        self.assertAlmostEqual(rows["475664047"], 12000 + 800 + 500 + 400, places=2)
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

        listings = {l.source_id: l for l in FinnSource(fetch=fetch, pause=0).search("r730", details=True)}
        self.assertEqual(sorted(listings), ["2", "3"])            # the string and the list-coordinates doc are skipped
        self.assertIsNone(listings["2"].description)              # a non-finn URL is never fetched
        self.assertIsNone(listings["3"].description)              # a dead page leaves the description empty
        self.assertFalse(any(u.startswith("file:") for u in fetched))


class _BrokenSource:
    name, foreign, supports_machines = "broken", False, False

    def search(self, query, details=False):
        raise RuntimeError("upstream page changed")


class _EmptySource:
    name, foreign, supports_machines = "empty", False, False

    def search(self, query, details=False):
        return []


class _GatedSource(_EmptySource):
    """Holds a Hunt open until the test releases the gate."""
    name = "gated"

    def __init__(self):
        self.gate = threading.Event()

    def search(self, query, details=False):
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
        facts = {"bays_35": 12, "caddies_35": 4, "psu_count": 1, "controller": "raid", "rails": False}
        self.assertEqual(machine_penalties(facts),
                         {"single_psu": 500, "caddies": 800, "raid_only": 500, "no_rails": 400})
        unknown = {"bays_35": 8, "caddies_35": None, "psu_count": None, "controller": None, "rails": None}
        self.assertEqual(machine_penalties(unknown), {"psu_unknown": 500, "caddies_unknown": 800,
                                                      "controller_unknown": 500, "rails_unknown": 400})
        clean = {"bays_35": 12, "caddies_35": 12, "psu_count": 2, "controller": "hba", "rails": True}
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
            "1": "1x 750W PSU. PERC H710 RAID. 4x 3.5\" caddies. Rails følger ikke med.",
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
        # 6,000 + trip 2 x 119 x 4 = 952 + PSU 500 + 8 missing caddies 800 + RAID-only 500 + no rails 400
        self.assertAlmostEqual(float(self.rows["1"]), 6000 + 952 + 500 + 800 + 500 + 400, places=2)
        page = self.app.page()
        for part in ("pickup trip 952", "2nd PSU 500", "caddies 800", "HBA 500", "rails 400"):
            self.assertIn(part, page)

    def test_far_pickups_are_hidden_even_with_negated_shipping_words(self):
        for fid in ("2", "3", "4"):
            self.assertNotIn(fid, self.rows)

    def test_far_seller_with_free_shipping_is_shown_at_its_price(self):
        # 3,300, free shipping, 12 caddies not stated 1,200, PSU / controller / rails not stated 500 + 500 + 400
        self.assertAlmostEqual(float(self.rows["5"]), 3300 + 1200 + 500 + 500 + 400, places=2)


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
        full = "2x 750W PSU. Dell HBA330. 12x 3.5\" caddies. Rails included."
        # all docs share one place ("X"); Oslo pickups there share one 952 NOK trip
        machines = [_doc(1, "Dell PowerEdge R730xd 12x LFF", 6000, 59.91, 10.72),    # 6,952, every fact good
                    _doc(2, "Dell PowerEdge R730xd 12x LFF", 30000, 59.91, 10.72),   # 30,952 + disks > Ceiling
                    _doc(3, "Dell PowerEdge R730xd 12x LFF", 5000, 59.91, 10.72)]    # caddies not stated
        descriptions = {"1": full, "2": full, "3": "2x 750W PSU. Dell HBA330. Rails included."}
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
        self.assertAlmostEqual(self.rows()["1"], 6952 + 7250, places=2)

    def test_ceiling_hides_the_expensive_machine(self):
        self.assertEqual(sorted(self.rows()), ["1", "3"])

    def test_details_link_every_listing_and_sort_by_any_column(self):
        for fid in (3, 30, 31, 32, 33, 34):
            self.assertIn(f"https://www.finn.no/recommerce/forsale/item/{fid}", self.page)
        for col in ("score", "landed", "usable", "machine", "disks", "sources"):
            self.assertIn(f'href="/?sort={col}"', self.page)
            self.assertIn('data-build="1"', self.app.page(sort=col))


def _ebay_item(iid, title, price, ship, pct="99.8", score=5000):
    it = {"itemId": iid, "title": title, "price": {"value": str(price), "currency": "GBP"}, "conditionId": "3000",
          "itemWebUrl": f"https://www.ebay.co.uk/itm/{iid}", "seller": {"username": "s", "feedbackPercentage": pct,
                                                                         "feedbackScore": score}}
    if ship is not None:
        it["shippingOptions"] = [{"shippingCost": {"value": str(ship), "currency": "GBP"}}]
    return it


class EbayMachinesAndWeakSellers(unittest.TestCase):
    """Seam 1: eBay UK Machines are read by the same rules; weak sellers pay +10%; no freight to Norway = excluded."""

    TEXT = "<p>2x 750W PSU</p><p>Dell HBA330</p><p>12x 3.5\" caddies</p><p>Rails included</p>"

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
