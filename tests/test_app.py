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
