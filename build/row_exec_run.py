"""Trace sqlglot library lines executed during TPC-DS PythonExecutor row execution (excluding generate)."""
from __future__ import annotations

import ast
import csv
import json
import os
import sys


def main() -> None:
    root = os.path.abspath(sys.argv[1])
    dest = os.path.abspath(sys.argv[2])
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, root)

    import tests.test_executor as te
    from sqlglot import find_tables, parse_one
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import Table, ensure_tables
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    tpcds_tables = {}
    for table, columns in TPCDS_SCHEMA.items():
        fn = f"{te.DIR_TPCDS}{table}.csv.gz"
        reader = csv.reader(te.open_file(fn), delimiter="|")
        rows, ctypes = [], []
        next(reader)
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tpcds_tables[table] = Table(columns=columns, rows=rows)

    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    prefix = os.path.join(root, "sqlglot") + os.sep
    strip = len(root.rstrip(os.sep)) + 1
    row_lines: set[tuple[str, int]] = set()
    in_gen = 0

    def on_line(code, line):
        fn = code.co_filename
        if not fn.startswith(prefix):
            return mon.DISABLE
        if in_gen > 0:
            return None
        row_lines.add((fn[strip:].replace(os.sep, "/"), line))
        return mon.DISABLE

    mon.use_tool_id(tool, "rowexec")
    mon.register_callback(tool, mon.events.LINE, on_line)
    try:
        schema = ensure_schema(TPCDS_SCHEMA)
        for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
            if not meta.get("execute"):
                continue
            expr = parse_one(sql)
            tables = ensure_tables({t.name: tpcds_tables[t.name] for t in find_tables(expr)})
            optimized = optimize(expr, schema, leave_tables_isolated=True)
            plan = Plan(optimized)
            pe = PythonExecutor(tables=tables)
            orig_gen = pe.generate

            def wrapped_gen(e, _orig=orig_gen):
                nonlocal in_gen
                in_gen += 1
                try:
                    return _orig(e)
                finally:
                    in_gen -= 1

            pe.generate = wrapped_gen
            mon.set_events(tool, mon.events.LINE)
            pe.execute(plan)
            mon.set_events(tool, 0)
    finally:
        mon.free_tool_id(tool)

    by_file: dict[str, list[int]] = {}
    for fn, ln in row_lines:
        by_file.setdefault(fn, []).append(ln)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump({fn: sorted(lns) for fn, lns in sorted(by_file.items())}, fh, separators=(",", ":"))


if __name__ == "__main__":
    main()
