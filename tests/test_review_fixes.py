import unittest
from unittest import mock

from dealfinder.app import App
from dealfinder.rules import Unreadable, priced_per_unit, read_ram
from dealfinder.sources import Listing
from tests import test_unreadable


class ReviewReaderFixes(unittest.TestCase):
    def test_negated_module_types_do_not_reject_registered_memory(self):
        for description in ("Registered ECC. Ikke UDIMM", "Passer ikke i SODIMM laptop. Registered ECC",
                            "Registered ECC. Fungerer ikke i UDIMM hovedkort", "No SODIMM. Registered.",
                            "ECC RDIMM. Not unbuffered", "Registered ECC, not for SODIMM laptops",
                            "Registered ECC. Uten UDIMM", "ECC RDIMM. Uten støtte for UDIMM",
                            "Ikke LRDIMM, dette er RDIMM", "No load reduced modules. RDIMM"):
            with self.subTest(description=description):
                facts = read_ram("16GB DDR4 RAM", "used", description=description)
                self.assertIsNotNone(facts)
                self.assertNotIsInstance(facts, Unreadable)
                self.assertTrue(facts.qualifies)
                self.assertEqual(facts.type, "RDIMM")

    def test_negated_registered_type_does_not_override_load_reduced(self):
        facts = read_ram("16GB DDR4 RAM", "used", description="Not registered. ECC LRDIMM")
        self.assertTrue(facts.qualifies)
        self.assertEqual(facts.type, "LRDIMM")

    def test_module_negation_does_not_cross_sentences(self):
        for description in ("Registered. No UDIMM. SODIMM", "Registered. Uten UDIMM; unbuffered"):
            self.assertIsNone(read_ram("16GB DDR4 RAM", "used", description=description))

    def test_dell_generation_is_not_a_ram_total(self):
        for generation in ("13G", "14G"):
            with self.subTest(generation=generation):
                facts = read_ram(f"Dell {generation} 64GB DDR4 RDIMM", "used", description="2x32GB")
                self.assertNotIsInstance(facts, Unreadable)
                self.assertTrue(facts.qualifies)
                self.assertEqual((facts.sticks, facts.gb_per_stick), (2, 32))

    def test_description_total_agrees_with_title_stick_size_and_count(self):
        for description in ("Selger 4 stk, totalt 64GB.", "Totalt 64GB.", "4 stk 16GB, totalt 64GB."):
            with self.subTest(description=description):
                facts = read_ram("4x 16GB DDR4 2400 ECC RDIMM M393A2K40BB1", "used",
                                 description=description)
                self.assertNotIsInstance(facts, Unreadable)
                self.assertTrue(facts.qualifies)
                self.assertEqual((facts.sticks, facts.gb_per_stick), (4, 16))

    def test_description_totals_and_negations_cannot_change_title_facts(self):
        cases = [
            ("4x 32GB DDR4 ECC RDIMM", "128GB totalt", 4, 32),
            ("4x 32GB DDR4 ECC RDIMM", "Selger 4 stk = 128GB", 4, 32),
            ("4x 32GB DDR4 ECC RDIMM", "Sum 128GB", 4, 32),
            ("2x 16GB DDR4 ECC RDIMM", "32GB kit fra HP server", 2, 16),
            ("8x 16GB DDR4 ECC RDIMM", "Til sammen 128GB", 8, 16),
            ("4x 32GB DDR4 ECC RDIMM", "Fungerer i DDR4 servere, ikke DDR3.", 4, 32),
            ("4x 32GB DDR4 ECC RDIMM", "Fungerer ikke med DDR3", 4, 32),
            ("4x 16GB DDR4 2400 ECC RDIMM M393A2K40BB1", "Selger 4 stk, totalt 128GB.", 4, 16),
            ("4x 32GB DDR4 ECC RDIMM", "2x16GB DDR3 LRDIMM and 8x64GB DDR5 UDIMM", 4, 32),
        ]
        for title, description, sticks, gb in cases:
            with self.subTest(title=title, description=description):
                facts = read_ram(title, "used", description=description)
                self.assertNotIsInstance(facts, Unreadable)
                self.assertTrue(facts.qualifies)
                self.assertEqual((facts.sticks, facts.gb_per_stick, facts.ddr, facts.type),
                                 (sticks, gb, 4, "RDIMM"))

    def test_description_ram_pair_must_match_title_total(self):
        cases = [
            ("64GB DDR4 ECC RDIMM", "4x16GB", 4, 16),
            ("64GB DDR4 ECC RDIMM", "2x32GB", 2, 32),
            ("256GB DDR4 ECC RDIMM", "8x32GB", 8, 32),
            ("4x 32GB DDR4 ECC RDIMM", "2x64GB", 4, 32),
            ("8× 16 GB DDR4 ECC RDIMM", "2x64GB", 8, 16),
            ("32GB DDR4 ECC RDIMM - 12 stk", "2x64GB", 12, 32),
            ("32GB DDR4 ECC RDIMM", "Testet og fungerer", 1, 32),
            ("16GB DDR4 RDIMM RAM", "32GB DDR4 RDIMM RAM", 1, 16),
            ("32GB DDR4 ECC RDIMM", "Selger 12 stk", 1, 32),
        ]
        for title, description, sticks, gb in cases:
            with self.subTest(title=title, description=description):
                facts = read_ram(title, "used", description=description)
                self.assertNotIsInstance(facts, Unreadable)
                self.assertTrue(facts.qualifies)
                self.assertEqual((facts.sticks, facts.gb_per_stick), (sticks, gb))

    def test_description_ram_pair_disagreeing_with_title_total_is_unreadable(self):
        for title in ("64GB DDR4 ECC RDIMM", "256GB DDR4 ECC RDIMM"):
            with self.subTest(title=title):
                facts = read_ram(title, "used", description="2x16GB")
                self.assertIsInstance(facts, Unreadable)
                self.assertIn("gb_per_stick", facts.missing)

    def test_title_ram_count_does_not_borrow_description_size(self):
        facts = read_ram("DDR4 ECC RDIMM - 12 stk", "used", description="32GB DDR4 RDIMM")
        self.assertIsInstance(facts, Unreadable)
        self.assertIn("gb_per_stick", facts.missing)

    def test_description_fills_missing_ram_type_and_per_stick_price_overrides_stock(self):
        listing = Listing("finn", "per-stick", "16GB DDR4 RAM - 12 stk", "https://www.finn.no/1",
                          1000, "NOK", 0, "NOK", "used", None,
                          description="ECC RDIMM fra server. Pris er pr stk.")
        facts = read_ram(listing.title, listing.condition, description=listing.description)
        self.assertTrue(facts.qualifies)
        self.assertEqual((facts.gb_per_stick, facts.sticks, facts.ddr, facts.type), (16, 1, 4, "RDIMM"))
        app = object.__new__(App)
        app.fx, app.router = lambda _: 1, lambda *_: (0, 0)
        score = app._score(type("Source", (), {"foreign": False})(), listing, "ram")
        self.assertTrue(score["qualifies"])
        self.assertEqual(score["facts"]["sticks"], 1)

    def test_negated_compatibility_is_ignored_when_description_supplies_ddr(self):
        facts = read_ram("16GB RDIMM RAM", "used", description="DDR4. Not for DDR3.")
        self.assertTrue(facts.qualifies)
        self.assertEqual(facts.ddr, 4)

    def test_title_rank_inference_precedes_description_type(self):
        facts = read_ram("64GB DDR4 4Rx4 2400 ECC RAM", "used", description="RDIMM or UDIMM")
        self.assertTrue(facts.qualifies)
        self.assertEqual(facts.type, "LRDIMM")

    def test_negated_compatibility_mentions_do_not_contradict_ram_facts(self):
        for description in ("Ikke kompatibel med DDR3 hovedkort.", "not for DDR3",
                            "Passer ikke til DDR3 hovedkort.", "DDR4 RDIMM. Passer ikke DDR3."):
            with self.subTest(description=description):
                facts = read_ram("16GB DDR4 RDIMM RAM", "used", description=description)
                self.assertNotIsInstance(facts, Unreadable)
                self.assertTrue(facts.qualifies)
                self.assertEqual(facts.ddr, 4)

    def test_total_capacity_and_module_capacity_give_count(self):
        facts = read_ram("MTA9ASF1G72PZ DDR4 8GB ECC Server Reg 8/16/24/32 Gb", "used",
                         aspects={"Total Capacity": "16 GB", "Capacity per Module": "8 GB"})
        self.assertEqual((facts.gb_per_stick, facts.sticks), (8, 2))
        facts = read_ram("DDR4 RAM 16GB RDIMM", "used",
                         aspects={"Total Capacity": "24 GB", "Capacity per Module": "16 GB"})
        self.assertIsInstance(facts, Unreadable)

    def test_currency_free_unit_prices_match_only_the_listed_price(self):
        for text in ("For 3 brikker: 4200 kr (1400 per stk)", "1400 kr per stk", "1400 per stk"):
            self.assertTrue(priced_per_unit(text, 1400))
            self.assertFalse(priced_per_unit(text, 4200))
        self.assertFalse(priced_per_unit("32GB per modul", 32))

    def test_unbuffered_overrides_generic_registered_text_but_not_title_model(self):
        facts = read_ram("M393A2G40EB1 16GB DDR4 unbuffered RAM", "used")
        self.assertIsNotNone(facts)
        self.assertTrue(facts.qualifies)
        for description in ("Registrert eller bufret: Ikke-bufret", "Registered RAM. Unbuffered."):
            self.assertIsNone(read_ram("16GB DDR4 ECC RAM", "used", description=description))
            facts = read_ram("M393A2G40EB1 16GB DDR4 ECC RAM", "used", description=description)
            self.assertTrue(facts.qualifies)
            self.assertEqual(facts.type, "RDIMM")


class ReviewScoringFixes(unittest.TestCase):
    def setUp(self):
        self.app = object.__new__(App)
        self.app.fx, self.app.router = lambda _: 1, lambda *_: (0, 0)
        self.source = type("Source", (), {"foreign": False})()

    def test_finn_total_uses_description_pair_for_ranked_capacity(self):
        listing = Listing("finn", "453469295", "64GB Minne (Prisen er for begge)",
                          "https://www.finn.no/recommerce/forsale/item/453469295", 2500, "NOK", 0,
                          "NOK", "used", None,
                          description="Type: DDR4 Størrelse: 64GB (2x32GB) ECC 2400 MHz m393a4k40bb1")
        score = self.app._score(self.source, listing, "ram")
        self.assertTrue(score["qualifies"])
        self.assertEqual((score["facts"]["sticks"], score["facts"]["gb_per_stick"]), (2, 32))

    def test_per_stick_ram_price_keeps_description_count_as_stock(self):
        for description in ("3x32GB DDR4 RDIMM. Pris per stk.",
                            "3 stk DDR4 RDIMM 32GB. For 3 brikker: 4200 kr (1400 per stk)"):
            with self.subTest(description=description):
                listing = Listing("finn", "per-stick-stock", "32GB DDR4 ECC RAM",
                                  "https://www.finn.no/per-stick-stock", 1400, "NOK", 0,
                                  "NOK", "used", None, description=description)
                score = self.app._score(self.source, listing, "ram")
                self.assertTrue(score["qualifies"])
                self.assertEqual((score["facts"]["sticks"], score["facts"]["gb_per_stick"]), (1, 32))

    def test_unmatched_per_stick_offer_does_not_override_lot_total_conflict(self):
        listing = Listing("finn", "whole-lot", "64GB DDR4 ECC RDIMM", "https://www.finn.no/whole-lot",
                          4200, "NOK", 0, "NOK", "used", None,
                          description="3x32GB. For 3 brikker: 4200 kr (1400 per stk)")
        score = self.app._score(self.source, listing, "ram")
        self.assertFalse(score["qualifies"])
        self.assertIn("gb_per_stick", score["missing"])

    def test_priced_rejection_keeps_landed_cost(self):
        listing = Listing("finn", "ddr3", "16GB DDR3 ECC RDIMM", "https://www.finn.no/ddr3", 1000, "NOK",
                          0, "NOK", "used", None)
        score = self.app._score(self.source, listing, "ram")
        self.assertFalse(score["qualifies"])
        self.assertGreater(score["landed_nok"], 0)
        listing.price = 0
        self.assertIsNone(self.app._score(self.source, listing, "ram")["landed_nok"])

    def test_foreign_shipping_rejection_does_not_depend_on_source_name(self):
        listing = Listing("another_foreign_source", "unknown", "DDR4 RAM", "https://example.com/item",
                          100, "GBP", None, None, "used", None)
        score = self.app._score(type("Source", (), {"foreign": True})(), listing, "ram")
        self.assertEqual(score["missing"], [])
        self.assertFalse(score["qualifies"])

    def test_tiered_per_stick_listing_does_not_rank_as_three_sticks(self):
        listing = Listing("finn", "tiered", "32GB DDR4 ECC RAM", "https://www.finn.no/tiered",
                          1400, "NOK", 0, "NOK", "used", None, description="Selger 3 stk DDR4 RDIMM 32GB. "
                          "For 1 brikke: 1500 kr For 2 brikker: 2900 kr (1450 per stk) "
                          "For 3 brikker: 4200 kr (1400 per stk)")
        score = self.app._score(self.source, listing, "ram")
        self.assertTrue(score["qualifies"])
        self.assertEqual(score["facts"]["sticks"], 1)


class ReviewItemFixes(unittest.TestCase):
    ebay = test_unreadable.SourceEnrichment.ebay

    def test_refused_item_does_not_end_source(self):
        source = self.ebay(title="16GB DDR4 RAM", details=OSError("request refused"))
        listing, = source.search("query", "ram")
        self.assertIsInstance(read_ram(listing.title, listing.condition, aspects=listing.aspects), Unreadable)

    def test_ram_reads_its_item_only_when_the_title_is_unreadable(self):
        for title in ("16GB DDR4 ECC RDIMM RAM", "16GB DDR4 ECC RAM"):
            with self.subTest(title=title):
                source = self.ebay(title=title, details={
                    "localizedAspects": [{"name": "RAM Form Factor", "value": "RDIMM"}]})
                listing, = source.search("ddr4", "ram")
                self.assertTrue(read_ram(listing.title, listing.condition, aspects=listing.aspects).qualifies)
                self.assertEqual(sum("/item/" in url for url in self.calls), int("RDIMM" not in title))

    def test_search_does_not_reset_budget_in_same_thread(self):
        source = self.ebay()
        app = object.__new__(App)
        app.sources, app.fx, app.router = [source], lambda _: 1, lambda *_: (0, 0)
        with mock.patch("dealfinder.sources.EBAY_FACT_LOOKUPS", 1):
            source.start_hunt()
            source.search("ddr4", "ram")
            source._items.clear()
            rows, faults = app.search("ddr4", "ram")
        self.assertEqual(faults, [])
        self.assertEqual(rows[0]["missing"], ["type"])
        self.assertEqual(sum("/item/" in url for url in self.calls), 1)
