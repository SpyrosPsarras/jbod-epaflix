"""Seam 2: the rule reader, checked against real listing texts."""
import json
import pathlib
import unittest

from dealfinder.rules import Unreadable, read_cpu, read_disk, read_machine

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


if __name__ == "__main__":
    unittest.main()
