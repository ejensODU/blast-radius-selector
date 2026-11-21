import difflib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from impact import analyze, parse_patch
from symbols import build, scalar_mask


class ScalarTests(unittest.TestCase):
    def test_module_alias_and_local_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sqlglot").mkdir()
            (root / "tests").mkdir()
            path = "sqlglot/unit.py"
            code = "LIMIT_VALUE = 4\ndef read():\n    return LIMIT_VALUE\n"
            (root / path).write_text(code)
            result = build(root)
            self.assertEqual(result[path], {"LIMIT_VALUE": [3]})
            patch_text = "".join(
                difflib.unified_diff(
                    code.splitlines(True),
                    code.replace("= 4", "= 5").splitlines(True),
                    "a/" + path,
                    "b/" + path,
                )
            )
            change = parse_patch(patch_text)[0]
            impact = analyze(patch_text, root)[0]
            profile = dict(
                names={},
                reads={},
                incomplete=0,
                lines={path: {3: 8}},
                startup={},
                startup_names=set(),
            )
            with patch("symbols._load", return_value=result):
                self.assertEqual(scalar_mask(impact, change, root, profile), 8)
                self.assertIsNone(
                    scalar_mask(
                        impact,
                        change,
                        root,
                        profile | {"startup_names": {"LIMIT_VALUE"}},
                    )
                )
            (root / "tests/test_alias.py").write_text(
                "from sqlglot import unit\nconfig = vars(unit)\n"
            )
            self.assertNotIn(path, build(root))

    def test_import_time_alias_and_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sqlglot").mkdir()
            (root / "tests").mkdir()
            file = root / "sqlglot/aliases.py"
            for use in [
                "ALIAS = LOCAL_NUMBER",
                "def f(value=LOCAL_NUMBER): pass",
                "class Settings:\n    value = LOCAL_NUMBER",
                "REGISTRY = globals()",
            ]:
                file.write_text(
                    "LOCAL_NUMBER = 4\ndef value():\n    return LOCAL_NUMBER\n" + use
                )
                self.assertNotIn("sqlglot/aliases.py", build(root))
