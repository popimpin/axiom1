import os
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1 import engines  # noqa: E402
from axiom1.engines import EngineError, _contract, table  # noqa: E402


def fake_engine(source, name="fake"):
    """A module built from source text, so the contract checker can be shown bad engines."""
    mod = types.ModuleType(f"axiom1.engines.{name}")
    path = Path(tempfile.mkdtemp()) / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    mod.__file__ = str(path)
    mod.EngineError = EngineError          # what `from ._base import EngineError` gives a real engine
    exec(compile(source, str(path), "exec"), mod.__dict__)
    import linecache
    linecache.updatecache(str(path))
    return mod


GOOD = '''
SPEC = {"name": "fake", "version": 1, "summary": "doubles", "functions": {
    "double": {"args": ["n"], "returns": "2n", "io": False,
               "examples": [{"args": {"n": 2}, "returns": 4}, {"args": {"n": "x"}, "refuses": "number"}]}}}
def double(n):
    """2n."""
    if not isinstance(n, int):
        raise EngineError("n must be a number")
    return 2 * n
'''


class EveryEngineMeetsTheContract(unittest.TestCase):
    def test_every_engine_in_the_package(self):
        mods = list(engines._modules())
        self.assertTrue(mods, "no engines found")
        for mod in mods:
            with self.subTest(engine=mod.__name__):
                self.assertEqual(_contract.problems(mod), [])


class TheContractCatchesBadEngines(unittest.TestCase):
    """Controls: the checker must be able to fail, or a clean report means nothing."""

    def test_a_good_engine_passes(self):
        self.assertEqual(_contract.problems(fake_engine(GOOD)), [])

    def test_a_wrong_example_is_caught(self):
        bad = GOOD.replace('"returns": 4', '"returns": 5')
        self.assertTrue(any("expected 5" in p for p in _contract.problems(fake_engine(bad))))

    def test_a_guess_where_a_refusal_was_promised_is_caught(self):
        bad = GOOD.replace('raise EngineError("n must be a number")', 'return 0')
        self.assertTrue(any("should refuse" in p for p in _contract.problems(fake_engine(bad))))

    def test_no_refusal_example_is_caught(self):
        bad = GOOD.replace(', {"args": {"n": "x"}, "refuses": "number"}', "")
        self.assertTrue(any("refuses" in p for p in _contract.problems(fake_engine(bad))))

    def test_a_crash_is_not_a_refusal(self):
        bad = GOOD.replace('raise EngineError("n must be a number")', 'raise TypeError("number")')
        self.assertTrue(any("crashed" in p for p in _contract.problems(fake_engine(bad))))

    def test_network_and_third_party_imports_are_caught(self):
        for line, word in (("import urllib.request", "outside the job"), ("import requests", "standard library")):
            with self.subTest(line=line):
                found = _contract.problems(fake_engine(line + "\n" + GOOD))
                self.assertTrue(any(word in p for p in found), found)

    def test_an_absolute_import_of_the_package_is_caught(self):
        # engines are copied into a job as `axiom_engines/`; `import axiom1` would not exist there
        found = _contract.problems(fake_engine("import axiom1.engines._base\n" + GOOD))
        self.assertTrue(any("'axiom1'" in p for p in found), found)

    def test_spec_args_must_match_the_signature(self):
        bad = GOOD.replace('"args": ["n"]', '"args": ["m"]')
        self.assertTrue(any("signature" in p for p in _contract.problems(fake_engine(bad))))

    def test_name_must_match_the_module(self):
        self.assertTrue(any("module name" in p for p in _contract.problems(fake_engine(GOOD, name="other"))))


class Registry(unittest.TestCase):
    def test_call_runs_a_pure_function(self):
        self.assertEqual(engines.call("table", "csv_text", {"columns": ["a"], "rows": [{"a": "1"}]}), "a\n1\n")

    def test_call_refuses_file_functions_unless_allowed(self):
        with self.assertRaisesRegex(EngineError, "process.py"):
            engines.call("table", "write_csv", {"path": "x.csv", "columns": ["a"], "rows": []})

    def test_unknown_names_list_what_exists(self):
        with self.assertRaisesRegex(EngineError, "table"):
            engines.call("nope", "x")
        with self.assertRaisesRegex(EngineError, "csv_text"):
            engines.call("table", "nope")

    def test_summary_lists_every_function(self):
        text = engines.summary()
        for fn in table.SPEC["functions"]:
            self.assertIn(f"table.{fn}(", text)


class TableFiles(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self.old)

    def test_write_then_read_round_trips(self):
        rows = [{"date": "2026-05-08", "title": "Kids' school play, gym"}, {"date": "2026-05-11", "title": None}]
        self.assertEqual(table.write_csv("out/cal.csv", ["date", "title"], rows), {"path": "out/cal.csv", "rows": 2})
        got = table.read_csv("out/cal.csv")
        self.assertEqual(got["columns"], ["date", "title"])
        self.assertEqual(got["rows"][0]["title"], "Kids' school play, gym")
        self.assertEqual(got["rows"][1]["title"], "")
        self.assertNotIn(b"\r", Path("out/cal.csv").read_bytes())

    def test_a_ragged_row_is_refused_not_padded(self):
        Path("in.csv").write_text("a,b\n1,2\n3\n", encoding="utf-8")
        with self.assertRaisesRegex(EngineError, "line 3"):
            table.read_csv("in.csv")

    def test_paths_cannot_leave_the_job(self):
        for path in ("../x.csv", "/tmp/x.csv", "C:\\x.csv", ""):
            with self.subTest(path=path), self.assertRaises(EngineError):
                table.write_csv(path, ["a"], [])

    def test_a_refused_table_writes_nothing(self):
        with self.assertRaises(EngineError):
            table.write_csv("t.csv", ["a"], [{"a": 1.5}])
        self.assertFalse(Path("t.csv").exists())

    def test_json_round_trip_with_decimal(self):
        table.write_json("t.json", {"total": Decimal("10.10")})
        self.assertEqual(Path("t.json").read_text(encoding="utf-8"), '{\n  "total": "10.10"\n}\n')


if __name__ == "__main__":
    unittest.main()
