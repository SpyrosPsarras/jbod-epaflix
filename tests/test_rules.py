"""Seam 2: the rule reader, checked against real listing texts."""
import json
import pathlib
import unittest

from dealfinder.rules import Unreadable, priced_per_unit, read_disk, read_ram

CORPUS = pathlib.Path(__file__).parent / "corpus"


class RuleCorpus(unittest.TestCase):
    def check(self, case, got):
        exp = case["expect"]
        if exp.get("ignored"):
            self.assertIsNone(got)
        elif "unreadable" in exp:
            self.assertIsInstance(got, Unreadable, got)
            self.assertEqual(sorted(got.missing), sorted(exp["unreadable"]))
        else:
            self.assertIsNotNone(got)
            self.assertNotIsInstance(got, Unreadable, getattr(got, "missing", None))
            for field, value in exp.items():
                self.assertEqual(getattr(got, field), value, field)

    def test_disks(self):
        for case in json.loads((CORPUS / "disks.json").read_text()):
            with self.subTest(case["note"]):
                self.check(case, read_disk(case["title"], case["condition"],
                                           category_id=case.get("category_id"), aspects=case.get("aspects")))

    def test_smr_disk_titles_are_rejected(self):
        for title in (
                "WD HSH721414ALE6M0 14TB 7.2K LFF AF HOST MANAGED SMR SATA HDD *Read Description*",
                "HGST Ultrastar HC620 14TB SATA", "WD HSH721414ALE6M0 14TB 3.5 SATA",
                "HGST HSH721415 14TB HDD", "Seagate 14TB SMR 3.5 HDD",
                "14TB HM-SMR HDD", "14TB DM-SMR HDD", "14TB host managed HDD",
                "14TB host-managed HDD", "14TB host aware HDD", "14TB host-aware HDD",
                "14TB zoned HDD", "14TB HM_SMR HDD", "14TB hostmanaged HDD",
                "12TB / 14TB SMR HDD"):
            with self.subTest(title=title):
                facts = read_disk(title, "used")
                self.assertIsNotNone(facts)
                self.assertNotIsInstance(facts, Unreadable)
                self.assertIs(facts.smr, True)
                self.assertIs(facts.qualifies, False)

    def test_non_smr_disk_titles_still_qualify(self):
        for title in ("WD Red Plus 12TB CMR NAS HDD", "IronWolf 12TB 3.5 HDD non-SMR",
                      "12TB 3.5 HDD not SMR", "12TB 3.5 HDD no SMR", "12TB 3.5 HDD ikke SMR",
                      "12TB 3.5 HDD CMR not SMR", "12TB 3.5 HDD CMR / SMR",
                      "Seagate ST14000NM000E 14TB 3.5 HDD", "12TB 3.5 HDD SMR-free"):
            with self.subTest(title=title):
                facts = read_disk(title, "used")
                self.assertIs(facts.smr, False)
                self.assertIs(facts.qualifies, True)

    def test_smr_aspects_reject_only_in_disk_category(self):
        for value in ("SMR", "host managed", "host-managed", "host aware", "zoned",
                      "HSH721414ALE6M0", "Ultrastar DC HC620", ["SATA", "HM-SMR"],
                      "HM_SMR", "hostmanaged"):
            with self.subTest(value=value):
                aspects = {"Recording Technology": value}
                facts = read_disk("12TB 3.5 HDD", "used", category_id="56083", aspects=aspects)
                self.assertIs(facts.smr, True)
                self.assertIs(facts.qualifies, False)
                for category in (None, "11211", 56083):
                    self.assertTrue(read_disk("12TB 3.5 HDD", "used", category_id=category,
                                              aspects=aspects).qualifies)
        for value in ("CMR", "non-SMR", "not SMR", "no SMR", "ikke SMR", "SMR-free"):
            with self.subTest(value=value):
                self.assertTrue(read_disk("12TB 3.5 HDD", "used", category_id="56083",
                                          aspects={"Recording Technology": value}).qualifies)

    def test_ram(self):
        for case in json.loads((CORPUS / "ram.json").read_text()):
            with self.subTest(case["note"]):
                self.check(case, read_ram(case["title"], case["condition"], price=case.get("price"),
                                          description=case.get("description"), aspects=case.get("aspects")))

    def test_price_per_unit_in_a_description(self):
        for text, price, expect in (
                ("Bare 4 igjen 16Gb DDR4 2400 ECC RDIMN !!! Pris per srk.", 800, True),  # finn 471846605
                ("Pris pr. stk", 500, True), ("Stykkpris.", 500, True), ("Pris pr.", 500, True),
                ("2 300 kr per stk", 2300, True), ("390 kr per stk.", 390, True), ("pris pr stk 1.500,-", 1500, True),
                ("Kan også selges enkeltvis for 750kr per brikke", 5800, False),  # finn 477364211
                ("16GB per modul", 5800, False), ("Spesifikasjoner per brikke: 16GB", 5800, False),
                ("Pris per stk: 750", 5800, True),  # a "pris per stk" always counts, whatever number follows
                ("Passer HP ML350 Gen10.\nPris pr stk.", 800, True),  # finn 433624847: "10." is not an amount
                ("HP ML350 Gen10 pris per stk", 800, True), ("Selger 4 stk. (pris er per stk)", 650, True),  # 272306459
                ("Model 10\n390 kr per stk", 390, True), ("DDR4 2 300 kr per stk", 2300, True),
                ("Each stick tested", 500, False), ("Selges per stk", 500, False),
                ("Pris per brikke", 500, False)):
            with self.subTest(text):
                self.assertEqual(priced_per_unit(text, price), expect)


if __name__ == "__main__":
    unittest.main()
