import base64
import json
import pathlib
import tempfile
import threading
import unittest

import pgserver
from unittest import mock

from dealfinder.app import App
from dealfinder.rules import Unreadable, read_ram
from dealfinder.sources import EbaySource, FinnSource, Listing


class ReaderFacts(unittest.TestCase):
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
        facts = read_ram("16GB DDR4 RAM M393A2G40EB1", "used",
                         aspects={"Form Factor": "UDIMM", "Capacity per Module": "32GB", "Type": "DDR3"})
        self.assertEqual((facts.gb_per_stick, facts.ddr, facts.type), (16, 4, "RDIMM"))

    def test_part_numbers_in_item_facts_beat_seller_aspect_labels(self):
        facts = read_ram("16GB DDR4 ECC RAM", "used",
                         aspects={"MPN": "M393A2G40EB1", "RAM Form Factor": "UDIMM"})
        self.assertEqual(facts.type, "RDIMM")
        self.assertTrue(facts.qualifies)

    def test_conflicting_aspects_and_variation_facts(self):
        self.assertIsInstance(read_ram("DDR4 16GB RAM", "used",
                              aspects={"RAM Form Factor": "RDIMM", "Form Factor": "LRDIMM"}), Unreadable)
        facts = read_ram("Server RAM", "used", aspects={"Capacity per Module": "16 GB", "Number of Modules": "4",
                                                      "RAM Form Factor": "RDIMM", "Type": "DDR4"})
        self.assertEqual((facts.gb_per_stick, facts.sticks, facts.type, facts.ddr), (16, 4, "RDIMM", 4))

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
            read_ram("DDR4 16GB RAM", "used", aspects={"RAM Form Factor": "RDIMM / LRDIMM"}), Unreadable)

    def test_aspects_reject_wrong_product_when_title_does_not_identify_one(self):
        for memory in ("UDIMM", "SODIMM"):
            self.assertIsNone(read_ram("16GB DDR4 RAM", "used", aspects={"RAM Form Factor": memory}))
        self.assertFalse(read_ram("16GB ECC RAM", "used", aspects={"Type": "DDR3", "Form Factor": "RDIMM"}).qualifies)


class CapturedListings(unittest.TestCase):
    def test_every_captured_listing_verdict(self):
        app = object.__new__(App)
        app.fx, app.router = lambda _: 1, lambda *_: (0, 0)
        total = 0
        for case in json.loads((pathlib.Path(__file__).parent / "corpus" / "ram.json").read_text()):
            if "listing_verdict" not in case:
                continue
            total += 1
            with self.subTest(case["note"]):
                listing = Listing(case["source"], case["source_id"], case["title"], case["url"], case["price"],
                                  "NOK", case["shipping"], "NOK", case["condition"], None,
                                  description=case.get("description"), aspects=case.get("aspects"))
                source = type("Source", (), {"foreign": case["source"] == "ebay_uk"})()
                score = app._score(source, listing, "ram")
                verdict = ("rejected" if score is None else "qualifies" if score["qualifies"]
                           else "unreadable" if score["missing"] else "rejected")
                self.assertEqual(verdict, case["listing_verdict"])
                for field, value in case.get("listing_facts", {}).items():
                    self.assertEqual(score["facts"][field], value)
        self.assertEqual(total, 55)


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
        listing = Listing("ebay_uk", "ram", "DDR4 RAM", "https://www.ebay.co.uk/item", 100, "GBP",
                          None, None, "used", None)
        score = app._score(type("Source", (), {"foreign": True})(), listing, "ram")
        self.assertEqual(score["missing"], [])
        self.assertFalse(score["qualifies"])

    def test_priced_rejection_retains_the_cost_problem(self):
        app = object.__new__(App)
        app.fx, app.router = lambda _: 1, lambda *_: (0, 0)
        listing = Listing("finn", "ddr3", "16GB DDR3 ECC RDIMM", "https://www.finn.no/ddr3", 100, "NOK",
                          None, "NOK", "used", None, pickup_only=True)
        score = app._score(type("Source", (), {"foreign": False})(), listing, "ram")
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

    def test_unreadable_ram_gets_one_cached_item_call(self):
        source = self.ebay()
        for _ in range(2):
            listing, = source.search("ddr4", "ram")
            facts = read_ram(listing.title, listing.condition, aspects=listing.aspects)
            self.assertTrue(facts.qualifies)
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
                app = App(pg.get_uri(), [source], fx=lambda _: 1, ram_queries={"ebay_uk": ["ddr4"]}, pause=0)
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
                                ram_queries={"ebay_uk": ["ddr4"]}, pause=0)
                self.addCleanup(restarted.store._pool.close)
                restarted.hunt()
                self.assertEqual(sum("/item/" in url for url in calls), 1)

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
