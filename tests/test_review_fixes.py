import json
import pathlib
import unittest
from unittest import mock

from dealfinder.app import App
from dealfinder.rules import Unreadable, priced_per_unit, read_disk, read_ram
from dealfinder.sources import Listing
from tests import test_unreadable


class ReviewReaderFixes(unittest.TestCase):
    def test_system_and_disk_probes_never_regress_from_905799e(self):
        probes = json.loads((pathlib.Path(__file__).parent / "corpus" / "system-probes.json").read_text())
        for probe in probes:
            with self.subTest(title=probe["title"]):
                facts = read_disk(probe["title"], "used",
                                  category_id=probe.get("category_id"), aspects=probe.get("aspects"))
                verdict = ("rejected" if facts is None else "unreadable" if isinstance(facts, Unreadable)
                           else "qualifies" if facts.qualifies else "rejected")
                baseline = probe["baseline_verdict"]
                if probe["kind"] == "disk":
                    self.assertIn(verdict, {baseline, "qualifies"})
                elif baseline != "qualifies":
                    self.assertNotEqual(verdict, "qualifies")

    def test_one_pcs_uses_aspects_for_missing_form_factor(self):
        facts = read_disk("1PCS 12TB SAS HDD", "used", category_id="56083", aspects={"Form Factor": "3.5 in"})
        self.assertTrue(facts.qualifies)
        self.assertEqual((facts.capacity_tb, facts.count), (12, 1))

    def test_generic_hdd_titles_do_not_infer_form_factor(self):
        for title in ("12TB HDD", "16TB harddisk", "14TB hard drive", "16TB disk", "Seagate 14TB",
                      "Dell 12TB SAS HDD for PowerEdge R730xd", "HPE 12TB SAS HDD for ProLiant DL380"):
            with self.subTest(title=title):
                facts = read_disk(title, "used")
                self.assertIsInstance(facts, Unreadable)
                self.assertIn("form_factor", facts.missing)
                enriched = read_disk(title, "used", category_id="56083", aspects={"Form Factor": "3.5 in"})
                if " for " in title:
                    self.assertIsInstance(enriched, Unreadable)
                else:
                    self.assertTrue(enriched.qualifies)

    def test_baseline_drive_families_keep_title_form_factor(self):
        for name in ("Exos", "Ultrastar", "IronWolf", "WD Red", "WD Gold", "WD Purple", "SkyHawk",
                     "BarraCuda", "Toshiba MG08", "HC550", "WUH721616ALE6L4", "ST16000NM001G",
                     "MG08ACA16TE", "MG08SCA16TE"):
            with self.subTest(name=name):
                facts = read_disk(f"{name} 16TB HDD", "used")
                self.assertNotIsInstance(facts, Unreadable)
                self.assertIsNotNone(facts)
                self.assertTrue(facts.qualifies)
                self.assertFalse(read_disk(f'{name} 16TB HDD 2.5"', "used").qualifies)

    def test_model_only_titles_need_explicit_form_factor(self):
        for name in ("Toshiba N300", "Toshiba X300", "Toshiba MG Series", "ST16000DM001",
                     "HUH721212ALE600", "WUS721414BLE6L4", "WD160EDGZ", "HAS5300-16T",
                     "D4-VS07-12000", "D3-VS07-12000"):
            with self.subTest(name=name):
                title = f"{name} 16TB HDD"
                facts = read_disk(title, "used")
                self.assertIsInstance(facts, Unreadable)
                self.assertIn("form_factor", facts.missing)
                self.assertTrue(read_disk(title + ' 3.5"', "used").qualifies)
                enriched = read_disk(title, "used", category_id="56083", aspects={"Form Factor": "3.5 in"})
                if name in {"Toshiba N300", "Toshiba X300", "Toshiba MG Series"}:
                    self.assertIsInstance(enriched, Unreadable)
                else:
                    self.assertTrue(enriched.qualifies)

    def test_fallback_family_and_model_names_do_not_supply_form_factor(self):
        cases = [
            ("Synology DS918+ med 4x 12TB", {"aspects": {"Model": "WD Red Plus"}}),
            ("Dell PowerEdge R730xd 12x 12TB HDD server", {"aspects": {"MPN": "ST12000NM0027"}}),
        ]
        for title, fallback in cases:
            with self.subTest(title=title, fallback=fallback):
                facts = read_disk(title, "used", category_id="56083", **fallback)
                self.assertIsInstance(facts, Unreadable)
                self.assertIn("form_factor", facts.missing)
        for fallback in ({"aspects": {"MPN": "ST12000NM0027"}},
                         {"aspects": {"Model": "WD Red Plus 12TB"}},
                         {"aspects": {"Form Factor": "WD Red Plus"}}):
            with self.subTest(fallback=fallback):
                self.assertIsInstance(read_disk("12TB HDD", "used", category_id="56083", **fallback), Unreadable)

    def test_disk_description_never_supplies_or_changes_facts(self):
        app = object.__new__(App)
        app.fx, app.router = lambda _: 1, lambda *_: (0, 0)
        cases = [
            ("Toshiba 16TB HDD", description, ["form_factor"], None)
            for description in ('3.5"', '2.5"', "LFF", "SFF", "3.5GHz", "Brukt i 2,5 år",
                                "Diskene har gått i 3,5 år", "Pris 3,5k", '4-bay NAS for 3.5" disker')
        ] + [
            ('HDD 3.5"', description, ["capacity"], None)
            for description in ("Toshiba 16TB HDD", "ST16000NM002G", "4x 12TB WD Red")
        ] + [
            ("HDD", "12TB and 16TB HDD", ["capacity", "form_factor"], None),
            ("HDD", '12TB HDD 2.5" and 3.5"', ["capacity", "form_factor"], None),
            ('12TB HDD 3.5" lot', "4x 12TB HDD sold together", ["quantity"], None),
            ('12TB HDD 3.5"', "4x 12TB HDD sold together", [], (12, "3.5", 1)),
            ('WD Gold 12TB 3.5" HDD', 'Brukt i NAS med 2.5" SSD cache.', [], (12, "3.5", 1)),
            ('2x 12TB 3.5" HDD', '4x 256GB 2.5" SSD cache', [], (12, "3.5", 2)),
            ('WD WD160EDGZ 16TB 3.5" harddisk', 'Har også en 2,5" SSD til salgs.', [], (16, "3.5", 1)),
        ]
        for title, description, missing, expected in cases:
            with self.subTest(title=title, description=description):
                listing = Listing("finn", "disk-description", title, "https://www.finn.no/1",
                                  24000, "NOK", 0, "NOK", "used", None, description=description)
                score = app._score(type("Source", (), {"foreign": False})(), listing, "disk")
                self.assertEqual(score["missing"], missing)
                self.assertEqual(score["qualifies"], expected is not None)
                if expected is not None:
                    self.assertEqual(tuple(score["facts"][key] for key in ("capacity_tb", "form_factor", "count")),
                                     expected)

    def test_disk_aspects_require_a_form_factor_value_and_supported_key(self):
        factors = {
            "3.5": ('3.5"', '3,5"', "3.5 in", "3,5 inch", "3.5 tommer", "3,5 Zoll", "LFF", "8.9 cm", "8,9cm"),
            "2.5": ('2.5"', '2,5"', "2.5 in", "2,5 inch", "2.5 tommer", "2,5 Zoll", "SFF", "6.35 cm", "6,35cm"),
        }
        for factor, values in factors.items():
            for key in ("Form Factor", "form factor", "Formfaktor", "FORMFAKTOR"):
                for value in values:
                    with self.subTest(key=key, value=value):
                        facts = read_disk("Toshiba 16TB HDD", "used", category_id="56083", aspects={key: value})
                        self.assertNotIsInstance(facts, Unreadable)
                        self.assertEqual(facts.form_factor, factor)
                        self.assertEqual(facts.qualifies, factor == "3.5")
        for value in ("Desktop", "Tower", "2U", "Rackmount", "3.5", "2,5", "3.5GHz", "3,5 år",
                      "Pris 3,5k", '4-bay NAS for 3.5" disker', "3.5 internal drive", "2.5GbE", "13.5 in"):
            with self.subTest(value=value):
                self.assertIsInstance(read_disk("Toshiba 16TB HDD", "used",
                                                category_id="56083", aspects={"Form Factor": value}), Unreadable)
        for key in ("Type", "Model", "Compatible Drive Size", "Factor de forma"):
            with self.subTest(key=key):
                self.assertIsInstance(read_disk("Toshiba 16TB HDD", "used", category_id="56083", aspects={key: "3.5 in"}), Unreadable)

    def test_disk_lot_titles_cannot_borrow_aspect_form_factor(self):
        for title in ("4x 12TB HDD", "4x12TB HDD", "12TB HDD x 4", "12TB HDD x4", "4 stk 12TB HDD",
                      "12TB HDD lot", "kit of 4 12TB HDD", "12TB HDD set of 4", "12TB HDD 4 pcs available",
                      "12TB HDD 4x pris per stk", "12TB HDD 12 pieces"):
            with self.subTest(title=title):
                facts = read_disk(title, "used", category_id="56083", aspects={"Form Factor": "3.5 in"})
                self.assertIsInstance(facts, Unreadable)
                self.assertIn("form_factor", facts.missing)
        for title in ("1x 12TB HDD", "12TB HDD x 1", "1 stk 12TB HDD", "1PCS 12TB SAS HDD", "12TB HDD"):
            with self.subTest(title=title):
                facts = read_disk(title, "used", category_id="56083", aspects={"Formfaktor": "3,5 Zoll"})
                self.assertNotIsInstance(facts, Unreadable)
                self.assertTrue(facts.qualifies)
        for title in ("4x 12TB Exos", '4x 12TB HDD 3.5"'):
            with self.subTest(title=title):
                facts = read_disk(title, "used", category_id="56083", aspects={"Form Factor": "2.5 in"})
                self.assertTrue(facts.qualifies)
                self.assertEqual(facts.count, 4)

    def test_possible_title_counts_block_disk_aspect_form_factor(self):
        titles = (
            "NetApp DS4246 with 24 12TB drives", "NetApp DS4246 24 drives 12TB",
            "Server with 12 disks 12TB", "Synology DS918+ 4 12TB",
            "Synology DS918+ with four 12TB", "HP DL380 with 4 12TB",
            "Two 16TB HDD", "Pair of 16TB HDD", "16TB HDD pair",
            "Seagate 16TB HDD - Qty 4", "16TB HDD (2)",
        )
        for title in titles:
            with self.subTest(title=title):
                facts = read_disk(title, "used", category_id="56083", aspects={"Form Factor": "3.5 in"})
                self.assertIsInstance(facts, Unreadable)
                self.assertIn("form_factor", facts.missing)
        for count in ("two", "three", "four", "five", "six", "eight", "ten", "twelve", "pair", "dual",
                      "twin", "quad", "qty", "quantity", "lot", "set", "bundle", "stk", "pcs", "pieces",
                      "units", "to", "tre", "fire", "par", "zwei", "drei", "vier", "Stück",
                      "2", "10", "100", "999", "x2", "2x", "×2", "2×", "x100", "×999", ",2", "2."):
            with self.subTest(count=count):
                facts = read_disk(f"16TB HDD {count}", "used", category_id="56083",
                                  aspects={"Form Factor": "3.5 in"})
                self.assertIsInstance(facts, Unreadable)
                self.assertIn("form_factor", facts.missing)

    def test_disk_specification_numbers_are_not_possible_title_counts(self):
        for spec in ("12000GB", "12 TB", "12000 GB", "12Gb", "6G", "12Gbps", "12 Gb/s", "6 G",
                     "7200", "7200 RPM", "7.2K", "256MB", "256 MB", "2026", "Gen9",
                     "HAS5300-12T", "D3-VS07-12000", "123-456", "123/456", "005053485"):
            with self.subTest(spec=spec):
                facts = read_disk(f"12TB SAS HDD {spec}", "used", category_id="56083",
                                  aspects={"Form Factor": "3.5 in"})
                self.assertNotIsInstance(facts, Unreadable)
                self.assertTrue(facts.qualifies)

    def test_unknown_title_tokens_block_disk_aspect_form_factor(self):
        titles = (
            "Synology 12-bay 12TB HDD", "Synology DS918+ 4-bay 12TB",
            "NetApp 24-bay shelf 12TB drives", "Dell R730xd 12LFF 12TB",
            "16TB HDD QTY4", "16TB HDD 4-off", "16TB HDD 4off", "16TB HDD 2/4",
            "16TB HDD 4/4", "16TB HDD fünf", "16TB HDD seks", "16TB HDD dozen", "16TB HDD Paar",
            "16TB HDD with", "16TB HDD server", "16TB HDD Synology", "16TB HDD NetApp",
            "16TB HDD unexpected", "16TB HDD 2031", "16TB HDD 2014", "16TB HDD 12345",
            "16TB HDD 4-bay", "16TB HDD 12LFF", "16TB HDD 4-off 100%",
            "16TB HDD QTY40", "16TB HDD 12LFF4", "16TB HDD 4x12000GB", "16TB HDD two12",
            "Dell R730xd 12TB HDD", "HP DL380 12TB HDD", "HPE ML350 16TB HDD",
        )
        for title in titles:
            with self.subTest(title=title):
                facts = read_disk(title, "used", category_id="56083", aspects={"Form Factor": "3.5 in"})
                self.assertIsInstance(facts, Unreadable)
                self.assertIn("form_factor", facts.missing)

    def test_single_disk_title_vocabulary_allows_aspect_form_factor(self):
        titles = (
            "WD Ultrastar DC HC550 18TB 7200RPM 512MB SATA 6Gb/s",
            "Seagate Exos X18 ST18000NM000J 18TB 256MB 7.2K",
            "HGST HUH721212ALE604 12TB 7200 RPM 256MB SATA III",
            "Toshiba MG08ACA16TE 16TB 512e 7200rpm", "WD Red Pro 16TB 2023 NAS HDD",
            "HGST 12TB HDD SATA 3", "HGST 12TB-HDD", "HGST 12TB/HDD",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertTrue(read_disk(title, "used", category_id="56083",
                                          aspects={"Form Factor": "3.5 in"}).qualifies)
        for spec in ("6Gbit", "SATA3", "SAS3", "4Kn", "4KN", "100%", "Gen 9", "2015", "2030",
                     "0F38459", "01PG627", "4XB7A14104", "hot-swap", "grade A", "grade B"):
            with self.subTest(spec=spec):
                facts = read_disk(f"HGST 12TB HDD {spec}", "used", category_id="56083",
                                  aspects={"Form Factor": "3.5 in"})
                self.assertTrue(facts.qualifies)

    def test_live_aspect_form_factor_cases_keep_their_reader_verdicts(self):
        cases = json.loads((pathlib.Path(__file__).parent / "corpus" / "disks.json").read_text())
        ids = {"168585264459", "168578802021", "358900996033", "336692381103", "357877115510",
               "318719829013", "358538123586", "257771149667", "206562652792", "237120659368"}
        found = set()
        for case in cases:
            source_id = case.get("source_id", "").split("|")
            if len(source_id) < 2 or source_id[1] not in ids | {"820127468348"}:
                continue
            with self.subTest(title=case["title"]):
                facts = read_disk(case["title"], case["condition"], category_id=case["category_id"],
                                  aspects=case["aspects"])
                if source_id[1] == "820127468348":
                    self.assertIsInstance(facts, Unreadable)
                    self.assertEqual(facts.missing, ["form_factor"])
                else:
                    self.assertTrue(facts.qualifies)
                    found.add(source_id[1])
        self.assertEqual(found, ids)

    def test_conflicting_structured_disk_form_factors_stay_unreadable(self):
        for aspects in ({"Form Factor": ["3.5 in", "2.5 in"]},
                        {"Form Factor": "3.5 in", "Formfaktor": "2,5 Zoll"}):
            with self.subTest(aspects=aspects):
                facts = read_disk("Toshiba 16TB HDD", "used", category_id="56083", aspects=aspects)
                self.assertIsInstance(facts, Unreadable)
                self.assertEqual(facts.missing, ["form_factor"])

    def test_disk_aspects_require_internal_hdd_category_for_every_fact(self):
        for category in (None, "11211", "88754", "175706", 56083):
            for title, aspects, missing in [
                ('HDD 3.5"', {"Storage Capacity": "16TB"}, ["capacity"]),
                ('HDD 3.5"', {"MPN": "ST16000NM002G"}, ["capacity"]),
                ("16TB HDD", {"Form Factor": "3.5 in"}, ["form_factor"]),
            ]:
                with self.subTest(category=category, title=title, aspects=aspects):
                    facts = read_disk(title, "used", category_id=category, aspects=aspects)
                    self.assertIsInstance(facts, Unreadable)
                    self.assertEqual(facts.missing, missing)
            facts = read_disk('16TB HDD 3.5"', "used", category_id=category, aspects={"Type": "SSD"})
            self.assertTrue(facts.qualifies)

    def test_internal_hdd_category_allows_single_disk_aspects(self):
        for aspects in ({"Form Factor": "3.5 in"}, {"Formfaktor": "3,5 Zoll"}):
            with self.subTest(aspects=aspects):
                facts = read_disk("Toshiba 16TB HDD", "used", category_id="56083", aspects=aspects)
                self.assertTrue(facts.qualifies)
                self.assertEqual((facts.capacity_tb, facts.form_factor, facts.count), (16, "3.5", 1))

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

    def test_old_hgst_models_are_not_large_disks(self):
        for model, tb in [("HUH728080ALE600", 8), ("HUS726060ALE610", 6), ("HUS724040ALE640", 4)]:
            with self.subTest(model=model):
                facts = read_disk(f"{model} HDD 3.5\"", "used")
                self.assertEqual(facts.capacity_tb, tb)
                self.assertFalse(facts.qualifies)

    def test_unsupported_hgst_codes_do_not_guess_capacity(self):
        for model in ("HUH729999ALE600", "HUS721210ALE610"):
            self.assertIsInstance(read_disk(f"{model} HDD 3.5\"", "used"), Unreadable)

    def test_all_aspect_values_contribute_capacity_conflicts(self):
        for field in ("Hard Drives", "Drive selection"):
            with self.subTest(field=field):
                facts = read_disk("HPE 3.5\" HDD Upgrade Options", "used",
                                  category_id="56083", aspects={field: "HPE 2TB SATA HDD", "Storage Capacity": "12TB"})
                self.assertIsInstance(facts, Unreadable)
                self.assertIn("capacity", facts.missing)

    def test_all_small_variation_aspects_reject(self):
        facts = read_disk("HARD DRIVE 3.5\" 6TB 8TB 10TB 12TB 16TB", "used",
                          category_id="56083", aspects={"CAPACITY": "6TB", "Storage Capacity": "8 TB"})
        self.assertNotIsInstance(facts, Unreadable)
        self.assertFalse(facts.qualifies)

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

    def test_brand_and_capacity_do_not_establish_form_factor(self):
        for brand in ("Seagate", "Toshiba", "WD", "Western Digital", "HGST", "Hitachi"):
            with self.subTest(brand=brand):
                facts = read_disk(f"{brand} 14TB", "used")
                self.assertIsInstance(facts, Unreadable)
                self.assertEqual(facts.missing, ["form_factor"])
                self.assertIsNone(read_disk(f"{brand} 14TB SSD", "used"))

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

    def test_white_label_disk_is_not_a_sticker(self):
        self.assertTrue(read_disk("WD 12TB White Label HDD 3.5\"", "used").qualifies)
        for title in ("12TB HDD labels for caddies", "12TB caddy labels", "12TB HDD stickers"):
            self.assertIsNone(read_disk(title, "used"))

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
        listing = Listing("finn", "small", "2TB HDD", "https://www.finn.no/small", 1000, "NOK",
                          0, "NOK", "used", None)
        score = self.app._score(self.source, listing, "disk")
        self.assertFalse(score["qualifies"])
        self.assertGreater(score["landed_nok"], 0)
        listing.price = 0
        self.assertIsNone(self.app._score(self.source, listing, "disk")["landed_nok"])

    def test_foreign_shipping_rejection_does_not_depend_on_source_name(self):
        listing = Listing("another_foreign_source", "unknown", "SAS HDD", "https://example.com/item",
                          100, "GBP", None, None, "used", None)
        score = self.app._score(type("Source", (), {"foreign": True})(), listing, "disk")
        self.assertEqual(score["missing"], [])
        self.assertFalse(score["qualifies"])

    def test_disk_description_count_is_stock_only_when_priced_per_unit(self):
        listing = Listing("finn", "stock", '12TB HDD 3.5"', "https://www.finn.no/stock", 1000, "NOK",
                          0, "NOK", "used", None, description="Selger 4 stk. Pris per stk.")
        score = self.app._score(self.source, listing, "disk")
        self.assertTrue(score["qualifies"])
        self.assertEqual((score["facts"]["count"], score["facts"]["stock"]), (1, 4))

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
        for title, kind in [("16GB DDR4 RAM", "ram"), ("12TB HDD", "disk")]:
            with self.subTest(kind=kind):
                source = self.ebay(title=title, details=OSError("request refused"))
                listing, = source.search("query", kind)
                self.assertEqual(listing.stock, 1)
                if kind == "ram":
                    self.assertIsInstance(
                        read_ram(listing.title, listing.condition, aspects=listing.aspects), Unreadable)

    def test_ram_never_reads_stock_even_after_aspect_enrichment(self):
        for title in ("16GB DDR4 ECC RDIMM RAM", "16GB DDR4 ECC RAM"):
            with self.subTest(title=title):
                source = self.ebay(title=title, details={
                    "localizedAspects": [{"name": "RAM Form Factor", "value": "RDIMM"}],
                    "estimatedAvailabilities": [{"estimatedAvailableQuantity": 8}],
                })
                listing, = source.search("ddr4", "ram")
                self.assertTrue(read_ram(listing.title, listing.condition, aspects=listing.aspects).qualifies)
                self.assertEqual((listing.stock, listing.stock_read), (1, None))
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
