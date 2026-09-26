"""Seam 2: the rule reader, checked against real listing titles."""
import json
import pathlib
import unittest

from dealfinder.rules import Unreadable, read_disk

CORPUS = json.loads((pathlib.Path(__file__).parent / "corpus" / "disks.json").read_text())


class DiskRules(unittest.TestCase):
    def test_corpus(self):
        for case in CORPUS:
            with self.subTest(case["note"]):
                got = read_disk(case["title"], case["condition"])
                exp = case["expect"]
                if exp.get("ignored"):
                    self.assertIsNone(got)
                elif "unreadable" in exp:
                    self.assertIsInstance(got, Unreadable)
                    self.assertEqual(sorted(got.missing), sorted(exp["unreadable"]))
                else:
                    self.assertNotIsInstance(got, Unreadable, getattr(got, "missing", None))
                    for field, value in exp.items():
                        self.assertEqual(getattr(got, field), value, field)


if __name__ == "__main__":
    unittest.main()
