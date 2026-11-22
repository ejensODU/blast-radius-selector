import difflib
import tempfile
import unittest
from pathlib import Path

from impact import Impact
from selector import TPC_CDS, TestIndex, _phase1_safe, _test_extra_ms, select, slice_plan
import structure


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

    def test_new_module_hook_selects_all_module_tests(self):
        tpath = self.root / "tests" / "test_demo.py"
        tpath.parent.mkdir(parents=True, exist_ok=True)
        old = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n"
        new = "def setUpModule():\n    raise RuntimeError()\n" + old
        tpath.write_text(old)
        rows = [{"number": 1, "id": "tests/test_demo.py::T::test_a", "file": "tests/test_demo.py", "seconds": 0.01}]
        profile = {"ids": [rows[0]["id"]], "incomplete": 0, "lines": {"tests/test_demo.py": {3: 1}},
                   "reads": {}, "startup": {}, "startup_body": {}, "names": {}, "classes": {}, "startup_names": set()}
        idx = TestIndex(rows, profile)
        gdiff = f"diff --git a/tests/test_demo.py b/tests/test_demo.py\n" + patch("tests/test_demo.py", old, new)
        plain, mask = structure.resolve(gdiff, idx, self.root, probes=False)
        self.assertEqual(plain, "")
        self.assertEqual(mask, 1)
        self.assertFalse(structure.LAST_UNAPPLIED)
        self.assertEqual(structure.LAST_UNAPPLIED_TEST_FILES, {"tests/test_demo.py"})

    def test_slice_plan_keeps_body_edit_in_same_file_as_world_fn_span(self):
        tid = "s.py::C::test_20"
        entry = {
            "file": "f.sql",
            "size": 3,
            "lines": {"sqlglot/a.py": {12: 0b001, 50: 0b100}},
            "setup": {},
            "outer": {},
        }
        impacts = [
            Impact("sqlglot/a.py", "binding", frozenset({10})),
            Impact("sqlglot/a.py", "body", frozenset({50})),
        ]
        got = slice_plan(tid, entry, impacts, {"sqlglot/a.py"}, {}, self.index, self.root,
                         world_fn_spans=[("sqlglot/a.py", (10, 15))])
        self.assertEqual(got, [0, 2])

    def test_phase1_safety_guards_and_unrelated_test_file(self):
        rows = [{"number": 1, "id": TPC_CDS, "file": "tests/test_executor.py", "seconds": 21.0}]
        profile = {"ids": [TPC_CDS], "incomplete": 0, "lines": {}, "reads": {},
                   "startup": {}, "startup_body": {}, "names": {}, "classes": {}, "startup_names": set()}
        idx = TestIndex(rows, profile)
        p1 = {"tpcds": [{}], "row_exec_lines": {"sqlglot/helper.py": [381], "sqlglot/planner.py": [23]}}
        # planner.py row-exec line or world change is blocked; body outside row-exec uses _plan_fp
        self.assertFalse(_phase1_safe(
            TPC_CDS, "tpcds", [Impact("sqlglot/planner.py", "body", frozenset({23}))],
            set(), None, [], [], False, True, idx, self.root, p1,
        ))
        self.assertFalse(_phase1_safe(
            TPC_CDS, "tpcds", [Impact("sqlglot/planner.py", "binding", frozenset({10}))],
            {"sqlglot/planner.py"}, 1, [("sqlglot/planner.py", "Step", "X")], [], False, True, idx, self.root, p1,
        ))
        self.assertTrue(_phase1_safe(
            TPC_CDS, "tpcds", [Impact("sqlglot/planner.py", "body", frozenset({282}))],
            set(), None, [], [], False, True, idx, self.root, p1,
        ))
        self.assertFalse(_phase1_safe(
            TPC_CDS, "tpcds", [Impact("sqlglot/executor/__init__.py", "body", frozenset({72}))],
            set(), None, [], [], False, True, idx, self.root, p1,
        ))
        self.assertFalse(_phase1_safe(
            TPC_CDS, "tpcds", [Impact("sqlglot/helper.py", "body", frozenset({381}))],
            set(), None, [], [], False, True, idx, self.root, p1,
        ))
        self.assertTrue(_phase1_safe(
            TPC_CDS, "tpcds",
            [Impact("sqlglot/parser.py", "body", frozenset({10})),
             Impact("tests/dialects/test_postgres.py", "body", frozenset({5}))],
            set(), None, [], [], False, True, idx, self.root, p1,
        ))

    def test_leaf_dialect_world_slicing_and_tiny_test_cost_cap(self):
        tid = "s.py::C::test_20"
        entry = {
            "file": "f.sql",
            "rel": "optimizer/simplify.sql",
            "size": 4,
            "lines": {"sqlglot/dialects/tsql.py": {10: 0b0101}},
            "setup": {},
            "outer": {},
        }
        impacts = [Impact("sqlglot/dialects/tsql.py", "binding", frozenset({10}))]
        got = slice_plan(
            tid, entry, impacts, {"sqlglot/dialects/tsql.py"}, {}, self.index, self.root,
            world_changed=[("sqlglot/dialects/tsql.py", "TSQL", "WEEK_OFFSET")],
            world_failed=[],
        )
        self.assertEqual(got, [0, 2])
        # Core file edit must NOT use leaf-dialect file slicing
        self.assertIsNone(slice_plan(
            tid, entry, [Impact("sqlglot/parser.py", "binding", frozenset({10}))],
            {"sqlglot/parser.py"}, {}, self.index, self.root,
            world_changed=[("sqlglot/parser.py", "Parser", "X")], world_failed=[],
        ))
        # Tiny-test phantom-noise cap suppresses fresh-process setup jitter while keeping heavy pool costs
        table = {"tests": {"t_tiny": 500, "t_pool": 9000}, "files": {"t": 100}}
        row = {"t_tiny": {"number": 1}, "t_pool": {"number": 2}}
        weights = {1: 5, 2: 2000}
        self.assertEqual(_test_extra_ms("t_tiny", table, weights=weights, row=row, pool_set={"t_pool"}), 32)
        self.assertEqual(_test_extra_ms("t_pool", table, weights=weights, row=row, pool_set={"t_pool"}), 9000 - 0)
