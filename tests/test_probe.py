import os
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from probe import COMPLETE, _multiple_setups, _overlay, probe


class ProbeTests(unittest.TestCase):
    def test_changed_module_cannot_use_bytecode_of_equal_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "base"
            shadow = Path(directory) / "shadow"
            root.mkdir()
            shadow.mkdir()
            changed = root / "changed.py"
            sibling = root / "sibling.py"
            changed.write_text("VALUE = 1\n")
            sibling.write_text("VALUE = 3\n")
            cache_dir = root / "__pycache__"
            suffix = "." + sys.implementation.cache_tag + ".pyc"
            changed_cache = cache_dir / ("changed" + suffix)
            sibling_cache = cache_dir / ("sibling" + suffix)
            py_compile.compile(str(changed), cfile=str(changed_cache), doraise=True)
            py_compile.compile(str(sibling), cfile=str(sibling_cache), doraise=True)
            _overlay(root, shadow, {"changed.py": "VALUE = 2\n"})
            os.utime(
                shadow / "changed.py",
                ns=(changed.stat().st_atime_ns, changed.stat().st_mtime_ns),
            )
            self.assertFalse((shadow / "__pycache__" / changed_cache.name).exists())
            self.assertTrue((shadow / "__pycache__" / sibling_cache.name).is_symlink())
            stdout = subprocess.check_output(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "import changed, sibling; print(changed.VALUE, sibling.VALUE)",
                ],
                cwd=shadow,
                text=True,
                env={k: v for k, v in os.environ.items() if k != "PYTHONPYCACHEPREFIX"},
            )
            self.assertEqual(stdout.strip(), "2 3")

    def test_multiple_distinct_class_setups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text(
                "class A:\n    def setUpClass(cls): pass\nclass B: pass\n"
            )
            (root / "b.py").write_text("class C:\n    def setUpClass(cls): pass\n")
            first = ["a.py::A::test_one", "a.py::A::test_two"]
            self.assertFalse(_multiple_setups(root, first + ["a.py::B::test_three"]))
            self.assertTrue(_multiple_setups(root, first + ["b.py::C::test_three"]))

    def test_subtest_error_does_not_discard_sibling_passes_or_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "t.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self): self.assertEqual(1, 1)\n"
                "    def test_suberr(self):\n"
                "        for i in range(3):\n"
                "            with self.subTest(i=i):\n"
                "                if i == 0: raise ValueError('boom')\n"
            )
            ids = ["t.py::T::test_ok", "t.py::T::test_suberr"]
            passed = probe(root, {"x.py": "X = 1\n"}, ids, timeout=10.0, failfast_subtest=True)
            self.assertEqual(passed, {"t.py::T::test_ok"})
            self.assertTrue(bool(COMPLETE))

    def test_lazy_pool_failfast_skips_later_subtests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "ran.log"
            (root / "t.py").write_text(
                "import unittest\n"
                "from concurrent.futures import ProcessPoolExecutor, as_completed, wait\n"
                "def worker(i, path):\n"
                "    with open(path, 'a') as f: f.write(f'{i}\\n')\n"
                "    return i\n"
                "class T(unittest.TestCase):\n"
                "    def test_lazy(self):\n"
                f"        p = {str(marker)!r}\n"
                "        with ProcessPoolExecutor() as pool:\n"
                "            futs = [pool.submit(worker, i, p) for i in range(5)]\n"
                "            wait(futs)\n"
                "            for fut in as_completed(futs):\n"
                "                with self.subTest(fut=fut):\n"
                "                    val = fut.result()\n"
                "                    self.assertNotEqual(val, 1)\n"
            )
            passed = probe(root, {"x.py": "X = 1\n"}, ["t.py::T::test_lazy"], timeout=10.0, failfast_subtest=True)
            self.assertEqual(passed, set())
            self.assertEqual(marker.read_text().splitlines(), ["0", "1"])

    def test_fixture_slices_filters_only_target_rel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "t.py").write_text(
                "import unittest\n"
                "def load_sql_fixture_pairs(rel):\n"
                "    if rel == 'target.sql': return [(0, 'ok'), (1, 'bad'), (2, 'ok')]\n"
                "    return [(0, 'other')]\n"
                "class T(unittest.TestCase):\n"
                "    def test_sliced(self):\n"
                "        other = list(load_sql_fixture_pairs('other.sql'))\n"
                "        self.assertEqual(len(other), 1)\n"
                "        for _, val in load_sql_fixture_pairs('target.sql'):\n"
                "            self.assertEqual(val, 'ok')\n"
            )
            nid = "t.py::T::test_sliced"
            # Slicing to [0, 2] skips index 1 ('bad') and still checks other.sql
            passed = probe(
                root, {"x.py": "X = 1\n"}, [nid], timeout=10.0, failfast_subtest=True,
                fixture_slices={nid: {"rel": "target.sql", "indices": [0, 2], "size": 3}},
            )
            self.assertEqual(passed, {nid})
            # Wrong expected size conservatively fails the test
            passed_bad = probe(
                root, {"x.py": "X = 1\n"}, [nid], timeout=10.0, failfast_subtest=True,
                fixture_slices={nid: {"rel": "target.sql", "indices": [0], "size": 2}},
            )
            self.assertEqual(passed_bad, set())

    def test_per_test_cpu_cap_aborts_hang_and_keeps_sibling_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "t.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_a_hang(self):\n"
                "        with self.subTest(i=0):\n"
                "            x = 0\n"
                "            while True:\n"
                "                x += 1\n"
                "    def test_b_ok(self):\n"
                "        self.assertEqual(2 + 2, 4)\n"
            )
            hang_id = "t.py::T::test_a_hang"
            ok_id = "t.py::T::test_b_ok"
            passed = probe(
                root, {"x.py": "X = 1\n"}, [hang_id, ok_id], timeout=10.0,
                failfast_subtest=True, per_test_caps={hang_id: 0.25, ok_id: 2.0},
            )
            self.assertEqual(passed, {ok_id})
            self.assertTrue(bool(COMPLETE))
