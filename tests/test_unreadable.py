import base64
import json
import pathlib
import tempfile
import threading
import time
import unittest

import pgserver
import urllib.error
import urllib.parse
from unittest import mock

from dealfinder.app import App
from dealfinder.rules import Unreadable, read_disk, read_ram
from dealfinder.sources import EbaySource, FinnSource, Listing


class ReaderFacts(unittest.TestCase):
    def test_disk_link_speeds_do_not_add_capacities(self):
        for speed in ("12Gb", "12G", "12GB", "6Gb", "6Gb/s"):
            with self.subTest(speed=speed):
                facts = read_disk(f'12TB SAS {speed} HDD 3.5"', "used")
                self.assertNotIsInstance(facts, Unreadable)
                self.assertEqual(facts.capacity_tb, 12)
                self.assertTrue(facts.qualifies)

    def test_disk_capacity_spellings_and_model_fallback(self):
        for title, capacity in [('HDD 12000GB 3.5"', 12), ('HDD 16000GB 3.5"', 16), ("HDD 12TB3.5\"", 12),
                                ("HDD HAS5300-12T", 12), ("HDD ST16000NT001", 16),
                                ("HDD ST16000NM002G", 16), ("HDD ST12000NM0027", 12),
                                ("HDD WUH721818AL5204", 18), ("HDD WUH721816AL5204", 16),
                                ("HDD WUS721212BLE6L4", 12), ("HDD HUH721212ALE600", 12),
                                ("HDD WUH722020CLE6L4", 20), ("HDD ST16000NT001 10TB", 10)]:
            with self.subTest(title=title):
                facts = read_disk(title, "used", category_id="56083", aspects={"Form Factor": "3.5 in"})
                self.assertNotIsInstance(facts, Unreadable)
                self.assertEqual(facts.capacity_tb, capacity)

    def test_type_decoding_covers_registered_and_unbuffered_families(self):
        for model, expected in [("M393A2G40EB1", "RDIMM"), ("M386A8K40CM2", "LRDIMM"),
                                ("HMA82GR7AFR8N", "RDIMM"), ("HMAA8GL7AMR4N", "LRDIMM"),
                                ("MTA36ASF4G72PZ", "RDIMM"), ("MTA18ASF2G72PDZ", "RDIMM"),
                                ("MTA72ASS8G72LZ", "LRDIMM"), ("CT16G4RFD8266", "RDIMM"),
                                ("M391A2K43BB1", None), ("M378A2K43CB1", None),
                                ("M471A2K43CB1", None), ("M474A2K43CB1", None),
                                ("MTA4ATF51264AZ", None), ("MTA8ATF1G64HZ", None)]:
            with self.subTest(model=model):
                facts = read_ram(f"16GB DDR4 ECC RAM {model}", "used")
                if expected is None:
                    self.assertIsNone(facts)
                else:
                    self.assertNotIsInstance(facts, Unreadable)
                    self.assertEqual(facts.type, expected)
                    self.assertTrue(facts.qualifies)

    def test_64gb_quad_rank_2133_or_2400_is_load_reduced(self):
        for rank in ("4Rx4", "4DRx4"):
            for speed in (2133, 2400):
                facts = read_ram(f"64GB DDR4 {rank} PC4-{speed} ECC RAM", "used",
                                 aspects={"Form Factor": "RDIMM"})
                self.assertEqual(facts.type, "LRDIMM")
        self.assertIsInstance(read_ram("64GB DDR4 4Rx4 2666 ECC RAM", "used"), Unreadable)
        facts = read_ram("64GB DDR4 ECC RAM", "used", description="4Rx4 PC4-2400T")
        self.assertEqual(facts.type, "LRDIMM")

    def test_description_fills_facts_without_classifying_the_compatibility_text(self):
        facts = read_ram("32GB RAM", "used", description="32GB DDR4 ECC Registered RDIMM. "
                         "Passer servere og NAS med Xeon. Ikke for vanlige PC-er.")
        self.assertTrue(facts.qualifies)
        facts = read_ram("64GB Minne", "used", description="DDR4 64GB (2x32GB) M393A4K40BB1")
        self.assertEqual((facts.gb_per_stick, facts.sticks, facts.type), (32, 2, "RDIMM"))

    def test_conflicting_description_cannot_change_title_facts(self):
        for description in ("16GB DDR3 RDIMM", "32GB DDR4 RDIMM", "16GB DDR4 LRDIMM",
                            "16GB DDR4 RDIMM and 32GB DDR4 RDIMM"):
            with self.subTest(description=description):
                facts = read_ram("16GB DDR4 RDIMM RAM", "used", description=description)
                self.assertTrue(facts.qualifies)
                self.assertEqual((facts.gb_per_stick, facts.ddr, facts.type), (16, 4, "RDIMM"))

    def test_description_contradicting_itself_on_missing_ram_facts_is_unreadable(self):
        for title, description, missing in [
            ("DDR4 RDIMM RAM", "16GB or 32GB", "gb_per_stick"),
            ("16GB RDIMM RAM", "DDR3 or DDR4", "ddr"),
            ("16GB DDR4 RAM", "RDIMM or LRDIMM", "type"),
            ("16GB DDR4 RAM", "SODIMM / DIMM", "type"),
        ]:
            with self.subTest(title=title, description=description):
                facts = read_ram(title, "used", description=description)
                self.assertIsInstance(facts, Unreadable)
                self.assertIn(missing, facts.missing)

    def test_title_beats_aspects_and_part_number_beats_aspect_type(self):
        facts = read_disk("12TB 3.5\" HDD", "used", category_id="56083", aspects={"Storage Capacity": "8 TB", "Form Factor": "2.5 in"})
        self.assertTrue(facts.qualifies)
        facts = read_ram("16GB DDR4 RAM M393A2G40EB1", "used",
                         aspects={"Form Factor": "UDIMM", "Capacity per Module": "32GB", "Type": "DDR3"})
        self.assertEqual((facts.gb_per_stick, facts.ddr, facts.type), (16, 4, "RDIMM"))

    def test_part_numbers_in_item_facts_beat_seller_aspect_labels(self):
        facts = read_ram("16GB DDR4 ECC RAM", "used",
                         aspects={"MPN": "M393A2G40EB1", "RAM Form Factor": "UDIMM"})
        self.assertEqual(facts.type, "RDIMM")
        self.assertTrue(facts.qualifies)
        facts = read_disk("SAS Hard Drive", "used",
                          category_id="56083", aspects={"MPN": "ST16000NM002G", "Storage Capacity": "8 TB", "Form Factor": "3.5 in"})
        self.assertEqual(facts.capacity_tb, 16)
        self.assertTrue(facts.qualifies)

    def test_conflicting_aspects_and_variation_facts(self):
        facts = read_disk("6TB 8TB 12TB HDD lot", "used", category_id="56083", aspects={"Storage Capacity": "8 TB"})
        self.assertFalse(facts.qualifies)
        self.assertEqual(facts.capacity_tb, 8)
        self.assertIsInstance(read_disk("6TB 8TB 12TB HDD", "used",
                              category_id="56083", aspects={"Storage Capacity": "16 TB", "Capacity": "10 TB"}), Unreadable)
        self.assertIsInstance(read_ram("DDR4 16GB RAM", "used",
                              aspects={"RAM Form Factor": "RDIMM", "Form Factor": "LRDIMM"}), Unreadable)
        facts = read_ram("Server RAM", "used", aspects={"Capacity per Module": "16 GB", "Number of Modules": "4",
                                                      "RAM Form Factor": "RDIMM", "Type": "DDR4"})
        self.assertEqual((facts.gb_per_stick, facts.sticks, facts.type, facts.ddr), (16, 4, "RDIMM", 4))

    def test_a_capacity_alone_does_not_establish_a_hard_disk_form_factor(self):
        self.assertIsInstance(read_disk("12TB storage", "used"), Unreadable)

    def test_description_count_conflicts_stay_unreadable(self):
        facts = read_ram("2x16GB DDR4 RDIMM RAM", "used", description="4x16GB DDR4 RDIMM RAM")
        self.assertTrue(facts.qualifies)
        self.assertEqual(facts.sticks, 2)
        self.assertIsInstance(read_ram("DDR4 RDIMM RAM", "used",
                              description="2x16GB DDR4 RDIMM and 2x32GB DDR4 RDIMM"), Unreadable)
        self.assertIsInstance(read_ram("16GB DDR4 RDIMM RAM", "used",
                              description="2x16GB DDR4 RDIMM and 4x16GB DDR4 RDIMM"), Unreadable)

    def test_conflicting_form_factors_in_one_aspect_stay_unreadable(self):
        self.assertIsInstance(
            read_disk("HDD", "used", category_id="56083", aspects={"Storage Capacity": "12TB", "Form Factor": "2.5 in / 3.5 in"}),
            Unreadable)
        self.assertIsInstance(
            read_ram("DDR4 16GB RAM", "used", aspects={"RAM Form Factor": "RDIMM / LRDIMM"}), Unreadable)

    def test_aspects_reject_wrong_product_when_title_does_not_identify_one(self):
        for product in ("SSD", "NVMe SSD", "DVR", "Stickers", "Caddy"):
            self.assertIsNone(read_disk("Storage HDD 3.5\"", "used", category_id="56083", aspects={"Type": product}))
        for memory in ("UDIMM", "SODIMM"):
            self.assertIsNone(read_ram("16GB DDR4 RAM", "used", aspects={"RAM Form Factor": memory}))
        self.assertFalse(read_ram("16GB ECC RAM", "used", aspects={"Type": "DDR3", "Form Factor": "RDIMM"}).qualifies)


class CapturedListings(unittest.TestCase):
    def test_every_captured_listing_verdict(self):
        app = object.__new__(App)
        app.fx, app.router = lambda _: 1, lambda *_: (0, 0)
        total = 0
        for kind, file in [("disk", "disks.json"), ("ram", "ram.json")]:
            for case in json.loads((pathlib.Path(__file__).parent / "corpus" / file).read_text()):
                if "listing_verdict" not in case:
                    continue
                total += 1
                with self.subTest(case["note"]):
                    listing = Listing(case["source"], case["source_id"], case["title"], case["url"], case["price"],
                                      "NOK", case["shipping"], "NOK", case["condition"], None,
                                      description=case.get("description"), aspects=case.get("aspects"),
                                      category_id=case.get("category_id"))
                    source = type("Source", (), {"foreign": case["source"] == "ebay_uk"})()
                    score = app._score(source, listing, kind)
                    verdict = ("rejected" if score is None else "qualifies" if score["qualifies"]
                               else "unreadable" if score["missing"] else "rejected")
                    self.assertEqual(verdict, case["listing_verdict"])
                    for field, value in case.get("listing_facts", {}).items():
                        self.assertEqual(score["facts"][field], value)
        self.assertEqual(total, 169)


    def test_finn_description_price_per_unit_keeps_one_ranked_stick(self):
        app = object.__new__(App)
        app.fx, app.router = lambda _: 1, lambda *_: (0, 0)
        cases = json.loads((pathlib.Path(__file__).parent / "corpus" / "ram.json").read_text())
        for case in cases:
            if case.get("source_id") not in {"455277766", "462654595", "462653276", "475607405"}:
                continue
            with self.subTest(case["source_id"]):
                listing = Listing("finn", case["source_id"], case["title"], case["url"], case["price"], "NOK",
                                  0, "NOK", "used", None, description=case["description"])
                score = app._score(type("Source", (), {"foreign": False})(), listing, "ram")
                self.assertTrue(score["qualifies"])
                self.assertEqual(score["facts"]["sticks"], 1)

    def test_no_shipping_is_rejected_even_when_the_product_facts_are_missing(self):
        app = object.__new__(App)
        for kind, title in [("disk", "SAS HDD"), ("ram", "DDR4 RAM")]:
            listing = Listing("ebay_uk", kind, title, "https://www.ebay.co.uk/item", 100, "GBP",
                              None, None, "used", None)
            score = app._score(type("Source", (), {"foreign": True})(), listing, kind)
            self.assertEqual(score["missing"], [])
            self.assertFalse(score["qualifies"])

    def test_priced_rejection_retains_the_cost_problem(self):
        app = object.__new__(App)
        app.fx, app.router = lambda _: 1, lambda *_: (0, 0)
        listing = Listing("finn", "small", "2TB HDD", "https://www.finn.no/small", 100, "NOK",
                          None, "NOK", "used", None, pickup_only=True)
        score = app._score(type("Source", (), {"foreign": False})(), listing, "disk")
        self.assertEqual(score["missing"], ["location"])
        self.assertFalse(score["qualifies"])


class SourceEnrichment(unittest.TestCase):
    def ebay(self, title="16GB DDR4 ECC RAM", details=None, ship=True):
        self.calls = []
        summary = {"itemId": "v1|1|0", "title": title, "itemWebUrl": "https://www.ebay.co.uk/itm/1",
                   "price": {"value": "100", "currency": "GBP"}, "conditionId": "3000"}
        if ship:
            summary["shippingOptions"] = [{"shippingCost": {"value": "10", "currency": "GBP"}}]

        def fetch(url, headers=None, data=None):
            self.calls.append(url)
            if "oauth2/token" in url:
                return {"access_token": "t", "expires_in": 7200}
            if "/item/" in url:
                if isinstance(details, Exception):
                    raise details
                return details or {"localizedAspects": [{"name": "RAM Form Factor", "value": "RDIMM"}]}
            return {"itemSummaries": [summary]}
        return EbaySource("id", "secret", fetch=fetch)

    def test_disk_aspects_from_a_system_category_cannot_rank_a_system(self):
        source = self.ebay("Synology DS918+ NAS 48TB", {
            "categoryId": "175706",
            "localizedAspects": [{"name": "Form Factor", "value": "3.5 in"}],
            "estimatedAvailabilities": [{"estimatedAvailableQuantity": 4}],
        })
        listing, = source.search("hard drive")
        app = object.__new__(App)
        app.fx, app.router = lambda _: 1, lambda *_: (0, 0)
        score = app._score(source, listing, "disk")
        self.assertFalse(score["qualifies"])
        self.assertEqual(score["missing"], ["form_factor"])
        self.assertEqual((listing.stock, listing.stock_read), (1, None))
        restarted = self.ebay("Synology DS918+ NAS 48TB", OSError("must use cache"))
        restarted.seed_items([(listing.source_id, listing.item_read, listing.item_details)])
        listing, = restarted.search("hard drive")
        score = app._score(restarted, listing, "disk")
        self.assertFalse(score["qualifies"])
        self.assertEqual(score["missing"], ["form_factor"])
        self.assertEqual(sum("/item/" in url for url in self.calls), 0)

    def test_unreadable_ram_gets_one_cached_item_call(self):
        source = self.ebay()
        for _ in range(2):
            listing, = source.search("ddr4", "ram")
            facts = read_ram(listing.title, listing.condition, aspects=listing.aspects)
            self.assertTrue(facts.qualifies)
        self.assertEqual(sum("/item/" in url for url in self.calls), 1)

    def test_newly_readable_disk_reuses_its_item_for_stock(self):
        source = self.ebay("Hard Drive 3.5\"", {"categoryId": "56083",
                                             "localizedAspects": [{"name": "Storage Capacity", "value": "16 TB"}],
                                             "estimatedAvailabilities": [{"estimatedAvailableQuantity": 4}]})
        listing, = source.search("hard drive")
        self.assertTrue(read_disk(listing.title, listing.condition, category_id=listing.category_id, aspects=listing.aspects).qualifies)
        self.assertEqual(listing.stock, 4)
        self.assertEqual(sum("/item/" in url for url in self.calls), 1)

    def test_extra_calls_are_bounded_across_queries_in_one_hunt(self):
        source = self.ebay()
        with mock.patch("dealfinder.sources.EBAY_FACT_LOOKUPS", 1):
            source.start_hunt()
            first, = source.search("ddr4", "ram")
            source._items.clear()
            second, = source.search("ecc", "ram")
        self.assertTrue(read_ram(first.title, first.condition, aspects=first.aspects).qualifies)
        self.assertIsInstance(read_ram(second.title, second.condition, aspects=second.aspects), Unreadable)
        self.assertEqual(sum("/item/" in url for url in self.calls), 1)

    def test_another_search_thread_does_not_reset_the_hunt_budget(self):
        source = self.ebay()
        with mock.patch("dealfinder.sources.EBAY_FACT_LOOKUPS", 1):
            source.start_hunt()
            source.search("ddr4", "ram")
            source._items.clear()
            thread = threading.Thread(target=source.start_hunt)
            thread.start()
            thread.join()
            listing, = source.search("ddr4", "ram")
        self.assertIsInstance(read_ram(listing.title, listing.condition, aspects=listing.aspects), Unreadable)
        self.assertEqual(sum("/item/" in url for url in self.calls), 1)

    def test_malformed_aspects_leave_one_listing_unreadable(self):
        for aspect in ({"name": 3, "value": "RDIMM"}, {"name": "RAM Form Factor", "value": {"type": "RDIMM"}}):
            source = self.ebay(details={"localizedAspects": [aspect]})
            listing, = source.search("ddr4", "ram")
            self.assertIsInstance(read_ram(listing.title, listing.condition, aspects=listing.aspects), Unreadable)

    def test_readable_ram_and_missing_shipping_do_not_spend_extra_calls(self):
        for title, ship in [("16GB DDR4 ECC RDIMM RAM", True), ("16GB DDR4 RAM", False)]:
            source = self.ebay(title, ship=ship)
            source.search("ddr4", "ram")
            self.assertEqual(sum("/item/" in url for url in self.calls), 0)

    def test_failed_item_does_not_end_source_and_consumes_extra_budget(self):
        source = self.ebay(details=OSError("offline"))
        with mock.patch("dealfinder.sources.EBAY_FACT_LOOKUPS", 1):
            source.start_hunt()
            listing, = source.search("ddr4", "ram")
            source.search("ddr4", "ram")
        self.assertIsInstance(read_ram(listing.title, listing.condition, aspects=listing.aspects), Unreadable)
        self.assertEqual(sum("/item/" in url for url in self.calls), 1)

    def test_cached_item_facts_expire_after_a_day(self):
        source = self.ebay()
        source.search("ddr4", "ram")
        at, details = source._items["v1|1|0"]
        source._items["v1|1|0"] = (at - 86401, details)
        source.search("ddr4", "ram")
        self.assertEqual(sum("/item/" in url for url in self.calls), 2)

    def test_seeded_item_facts_are_reused_after_restart(self):
        source = self.ebay()
        listing, = source.search("ddr4", "ram")
        restarted = self.ebay()
        restarted.seed_items([("v1|1|0", listing.item_read, listing.item_details)])
        listing, = restarted.search("ddr4", "ram")
        self.assertTrue(read_ram(listing.title, listing.condition, aspects=listing.aspects).qualifies)
        self.assertEqual(sum("/item/" in url for url in self.calls), 0)

    def test_hunt_persists_item_cache_including_still_unreadable_ram(self):
        for aspects in ([{"name": "RAM Form Factor", "value": "RDIMM"}], []):
            with self.subTest(aspects=aspects):
                pg = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
                self.addCleanup(pg.cleanup)
                source = self.ebay(details={"localizedAspects": aspects})
                app = App(pg.get_uri(), [source], fx=lambda _: 1, disk_queries={},
                          ram_queries={"ebay_uk": ["ddr4"]}, pause=0)
                self.addCleanup(app.store._pool.close)
                app.hunt()
                known = app.store.item_read("ebay_uk")
                self.assertEqual(len(known), 1)
                self.assertEqual(known[0][2]["localizedAspects"], aspects)
                with app.store._conn() as conn:
                    saved = conn.execute("SELECT facts, item_cache FROM listings").fetchone()
                if aspects:
                    self.assertNotIn("_ebay_item", saved["facts"])
                else:
                    self.assertIsNone(saved["facts"])
                self.assertEqual(saved["item_cache"]["details"]["localizedAspects"], aspects)
                calls = self.calls
                restarted = App(pg.get_uri(), [EbaySource("id", "secret", fetch=source._fetch)], fx=lambda _: 1,
                                disk_queries={}, ram_queries={"ebay_uk": ["ddr4"]}, pause=0)
                self.addCleanup(restarted.store._pool.close)
                restarted.hunt()
                self.assertEqual(sum("/item/" in url for url in calls), 1)

    def test_persisted_stock_item_also_supplies_aspects_after_restart(self):
        pg = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
        self.addCleanup(pg.cleanup)
        details = {"categoryId": "56083",
                   "localizedAspects": [{"name": "Storage Capacity", "value": "16 TB"}],
                   "estimatedAvailabilities": [{"estimatedAvailableQuantity": 4}]}
        source = self.ebay("12TB HDD", details)
        app = App(pg.get_uri(), [source], fx=lambda _: 1, disk_queries={"ebay_uk": ["hdd"]},
                  ram_queries={}, pause=0)
        self.addCleanup(app.store._pool.close)
        app.hunt()
        restarted_source = self.ebay("Hard Drive 3.5\"", details)
        restarted = App(pg.get_uri(), [restarted_source], fx=lambda _: 1,
                        disk_queries={"ebay_uk": ["hdd"]}, ram_queries={}, pause=0)
        self.addCleanup(restarted.store._pool.close)
        restarted.hunt()
        with restarted.store._conn() as conn:
            saved = conn.execute("SELECT facts, qualifies FROM listings").fetchone()
        self.assertTrue(saved["qualifies"])
        self.assertEqual((saved["facts"]["capacity_tb"], saved["facts"]["stock"]), (16, 4))
        self.assertEqual(sum("/item/" in url for url in self.calls), 0)
        self.assertEqual(restarted.store.item_read("ebay_uk")[0][2]["categoryId"], "56083")

    def test_old_stock_persistence_seeds_the_same_item_cache(self):
        pg = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
        self.addCleanup(pg.cleanup)
        source = self.ebay('12TB HDD 3.5"')
        app = App(pg.get_uri(), [source], fx=lambda _: 1, disk_queries={"ebay_uk": ["hdd"]},
                  ram_queries={}, pause=0)
        self.addCleanup(app.store._pool.close)
        app.hunt()
        legacy = {"stock_read": time.time(), "stock": 4, "extra_shipping": 2}
        with app.store._conn() as conn:
            conn.execute("UPDATE listings SET item_cache = NULL, facts = facts || %s::jsonb",
                         (json.dumps(legacy),))
        restarted_source = self.ebay('12TB HDD 3.5"', OSError("must use persisted stock"))
        restarted = App(pg.get_uri(), [restarted_source], fx=lambda _: 1,
                        disk_queries={"ebay_uk": ["hdd"]}, ram_queries={}, pause=0)
        self.addCleanup(restarted.store._pool.close)
        listing, = restarted_source.search("hdd")
        self.assertEqual((listing.stock, listing.extra_shipping), (4, 2))
        self.assertEqual(sum("/item/" in url for url in self.calls), 0)

    def test_finn_disk_description_fetching_keeps_baseline_unit_price_and_stock_rules(self):
        for title, should_fetch in [('12TB HDD 3.5"', True), ('4x 12TB HDD 3.5"', True),
                                    ('HDD 3.5"', False), ("12TB HDD", False)]:
            with self.subTest(title=title):
                doc = {"id": 1, "heading": title, "trade_type": "Til salgs", "price": {"amount": 1000},
                       "flags": ["shipping_exists"]}
                calls = []

                def fetch(url):
                    calls.append(url)
                    if "/search?" in url:
                        data = {"queries": [{"state": {"data": {"docs": [doc]}}}]}
                        return "<script>" + base64.b64encode(json.dumps(data).encode()).decode() + "</script>"
                    return '<section data-testid="description"><p>Selger 4 stk. Pris per stk.</p></section>'

                listing, = FinnSource(fetch=fetch, pause=0).search("hdd")
                self.assertEqual(sum("/item/" in url for url in calls), int(should_fetch))
                if should_fetch:
                    app = object.__new__(App)
                    app.fx, app.router = lambda _: 1, lambda *_: (0, 0)
                    score = app._score(type("Source", (), {"foreign": False})(), listing, "disk")
                    self.assertTrue(score["qualifies"])
                    self.assertEqual((score["facts"]["count"], score["facts"]["stock"]), (1, 4))
                else:
                    self.assertIsNone(listing.description)

    def test_finn_fetches_description_for_title_missing_type(self):
        doc = {"id": 1, "heading": "16GB DDR4 ECC RAM", "trade_type": "Til salgs", "price": {"amount": 1000},
               "flags": ["shipping_exists"]}
        calls = []

        def fetch(url):
            calls.append(url)
            if "/search?" in url:
                data = {"queries": [{"state": {"data": {"docs": [doc]}}}]}
                return "<script>" + base64.b64encode(json.dumps(data).encode()).decode() + "</script>"
            return "<section data-testid=\"description\"><p>16GB DDR4 RDIMM. Pris per stk.</p></section>"
        source = FinnSource(fetch=fetch, pause=0)
        listing, = source.search("ddr4", "ram")
        self.assertTrue(read_ram(listing.title, listing.condition, description=listing.description).qualifies)
        source.search("ecc", "ram")
        self.assertEqual(sum("/item/" in url for url in calls), 1)
