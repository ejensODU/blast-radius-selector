import dataclasses
import difflib
from pathlib import Path
import tempfile
import unittest

from impact import analyze, parse_patch
from values import OPS, _literal_replacement, _literal_unchanged, interpreted, refine, relation


def patch(name, before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile="a/" + name, tofile="b/" + name))


class ImpactTests(unittest.TestCase):
    def inspect(self, original, changed):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text(original, encoding="utf-8")
            return analyze(patch("module.py", original, changed), root)

    def test_comment_in_string_is_data(self):
        source = 'def f():\n    return """content\n# tested\n"""\n'
        self.assertEqual(self.inspect(source, source.replace("tested", "changed"))[0].kind, "body")

    def test_moved_class_and_unicode_binding(self):
        source = 'class A:\n    items = {"été": 1, "秋": 2}\n'
        self.assertEqual(self.inspect(source, "# comment\n" + source)[0].kind, "equivalent")
        item, = self.inspect(source, source.replace("2}", "3}"))
        self.assertEqual((item.kind, item.scope, item.names, item.keys),
                         ("binding", ("A",), {"items"}, {"秋"}))

    def test_short_circuit_header(self):
        source = "def f(a, b):\n    if (\n        a\n        and b\n    ):\n        return 1\n"
        item, = self.inspect(source, source.replace("and b", "or b"))
        self.assertTrue({3, 4} <= item.lines)

    def test_file_split_and_syntax_error(self):
        source = "def f():\n    return 1\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("a.py", "b.py"):
                (root / name).write_text(source)
            text = patch("a.py", source, source.replace("1", "2"))
            text += patch("b.py", source, source.replace("1", ")"))
            self.assertEqual([i.kind for i in analyze(text, root)], ["body", "unknown"])

    def test_deleting_nonlocal_widens_to_outer_function(self):
        source = (
            "def outer():\n"
            "    x = 1\n"
            "    def inner():\n"
            "        nonlocal x\n"
            "        x = 2\n"
            "    inner()\n"
            "    return x\n"
        )
        changed = source.replace("        nonlocal x\n", "")
        item, = self.inspect(source, changed)
        self.assertEqual(item.kind, "body")
        self.assertTrue({2, 6, 7} <= item.lines)


class ObservationTests(unittest.TestCase):
    def test_opaque_equality_is_never_invoked(self):
        class Opaque:
            def __eq__(self, other):
                raise AssertionError()
        self.assertIsNone(relation(Opaque(), None))

    def test_literal_bound_respects_entire_edit(self):
        source = "def f(x): return x < 5\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "m.py").write_text(source)
            def changed(expr):
                filepatch, = parse_patch(patch("m.py", source, source.replace("5", expr)))
                return _literal_replacement("m.py", root, filepatch)
            self.assertIsNotNone(changed("4"))
            self.assertIsNone(changed("4 or x()"))

    def test_integer_thresholds(self):
        for a in range(-3, 4):
            for old in range(-3, 4):
                for new in range(-3, 4):
                    for name, compare in OPS.items():
                        if not name.startswith('Is') and _literal_unchanged(name, relation(a, old), old, new):
                            self.assertEqual(compare(a, old), compare(a, new))

    def test_undefined_order(self):
        self.assertIsNone(interpreted("LtE", relation(None, None)))
        self.assertIsNone(interpreted("GtE", relation(object(), None)))

    def test_distinguishable_inputs(self):
        a, b = "sample".upper(), "SAMPLE".lower().upper()
        state = relation(a, b)
        self.assertFalse(interpreted("Is", state))
        self.assertTrue(interpreted("Eq", state))
        boundary = relation((1, 4), (1, 4))
        self.assertFalse(interpreted("Lt", boundary))
        self.assertTrue(interpreted("LtE", boundary))

    def test_nested_compare_scopes_skip_refinement(self):
        source = "class Box:\n    def __eq__(self, other):\n        return other is None\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "m.py").write_text(source)
            diff = patch("m.py", source, source.replace("is None", "is not None"))
            item, = analyze(diff, root)
            fp, = parse_patch(diff)
            self.assertIsNone(refine(item, root, fp, {"ids": ["t1"]}, profile={"ids": ["t1"]}))
            dialect_item = dataclasses.replace(item, scope=("_Dialect", "helper"))
            self.assertIsNone(refine(dialect_item, root, fp, {"ids": ["t1"]}, profile={"ids": ["t1"]}))


if __name__ == "__main__":
    unittest.main()
