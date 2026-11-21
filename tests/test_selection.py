import difflib
import tempfile
import unittest
from pathlib import Path

from selector import TestIndex, select


def patch(path, original, updated):
    return "".join(difflib.unified_diff(original.splitlines(True), updated.splitlines(True),
                                        "a/" + path, "b/" + path))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.root = Path(self.sandbox.name)
        self.text = "def f(x):\n    return x + 1\n"
        (self.root / "m.py").write_text(self.text)
        rows = [{"number": n, "id": f"s.py::C::test_{n}", "file": "s.py", "seconds": .01}
                for n in [10, 20, 30]]
        profile = {"ids": [rows[1]["id"], rows[0]["id"]], "incomplete": 0,
                   "lines": {"m.py": {2: 1}}, "reads": {"fixture.sql": 2}, "startup": {}, "startup_body": {},
                   "names": {}, "classes": {}, "startup_names": set()}
        self.index = TestIndex(rows, profile)

    def result(self, text):
        workers = select(text, self.root, self.index, 0, probes=False)
        return sorted(n for worker in workers for n in worker)

    def test_reordered_manifest_and_unprofiled_test(self):
        self.assertEqual(self.result(patch("m.py", self.text, self.text.replace("+ 1", "* 2"))), [20, 30])

    def test_startup_and_invalid_syntax(self):
        self.assertEqual(self.result(patch("m.py", self.text, "def !\n")), [10, 20, 30])
        self.index.profile["startup_body"]["m.py"] = {2}
        self.assertEqual(self.result(patch("m.py", self.text, self.text.replace("+ 1", "- 1"))),
                         [10, 20, 30])

    def test_equivalent_python_with_changed_fixture(self):
        edit = patch("m.py", self.text, "# a comment\n" + self.text)
        self.assertEqual(self.result(edit), [])
        self.assertEqual(self.result(edit + patch("fixture.sql", "SELECT 1\n", "SELECT 2\n")), [10, 30])
