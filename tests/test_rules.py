"""Seam 2: the rule reader, checked against real listing texts."""
import json
import pathlib
import unittest

from dealfinder.rules import Unreadable, priced_per_unit, read_cpu, read_disk, read_heatsink, read_machine, read_ram

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
                self.check(case, read_disk(case["title"], case["condition"]))

    def test_machines(self):
        for case in json.loads((CORPUS / "machines.json").read_text()):
            with self.subTest(case["note"]):
                self.check(case, read_machine(case["title"], case["description"], case["condition"]))

    def test_cpus(self):
        for case in json.loads((CORPUS / "cpus.json").read_text()):
            with self.subTest(case["note"]):
                self.check(case, read_cpu(case["title"], case["condition"]))

    def test_ram(self):
        for case in json.loads((CORPUS / "ram.json").read_text()):
            with self.subTest(case["note"]):
                self.check(case, read_ram(case["title"], case["condition"]))

    def test_heatsinks(self):
        for case in json.loads((CORPUS / "heatsinks.json").read_text()):
            with self.subTest(case["note"]):
                self.check(case, read_heatsink(case["title"], case["condition"]))

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
