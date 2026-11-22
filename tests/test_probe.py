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
