"""Build exec_phase1.json: deterministic Phase-1 dry-run fingerprints for executor TPC tests."""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys
import time


def _ast_fp(node, h):
    """Feed exact AST structure + DataType annotations (ignoring comments) into blake2b."""
    from sqlglot import exp
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, exp.Expr):
            h.update(b"E:")
            h.update(type(cur).__qualname__.encode())
            h.update(b";")
            if cur._type is not None:
                h.update(b"T:")
                stack.append(cur._type)
            for k in sorted(cur.args):
                v = cur.args[k]
                h.update(b"K:")
                h.update(k.encode())
                h.update(b"=")
                stack.append(v)
        elif isinstance(cur, list):
            h.update(b"L[")
            h.update(str(len(cur)).encode())
            h.update(b"]")
            for x in reversed(cur):
                stack.append(x)
        elif isinstance(cur, tuple):
            h.update(b"P(")
            h.update(str(len(cur)).encode())
            h.update(b")")
            for x in reversed(cur):
                stack.append(x)
        else:
            h.update(b"V:")
            h.update(type(cur).__name__.encode())
            h.update(b":")
            h.update(repr(cur).encode())
            h.update(b";")


def _plan_fp(plan, h):
    """Feed structural & scalar Step attributes (limit, offset, join side, distinct, etc.) deterministically."""
    sigs = []
    for s in plan.dag:
        rec = [
            type(s).__name__,
            s.name,
            repr(getattr(s, "limit", None)),
            repr(getattr(s, "offset", None)),
            sorted([type(d).__name__, d.name] for d in getattr(s, "dependencies", ())),
            len(getattr(s, "projections", ())),
            getattr(s, "condition", None) is not None,
        ]
        if hasattr(s, "joins"):
            rec.append(getattr(s, "source_name", None))
            rec.append(sorted(
                [k, j.get("side"), len(j.get("source_key", ())), len(j.get("join_key", ())), j.get("condition") is not None]
                for k, j in s.joins.items()
            ))
        if hasattr(s, "distinct"):
            op = getattr(s, "op", None)
            rec.append([op.__name__ if isinstance(op, type) else repr(op),
                        getattr(s, "left", None), getattr(s, "right", None), bool(s.distinct)])
        if hasattr(s, "group"):
            rec.append([getattr(s, "source", None), sorted(s.group.keys()), len(s.aggregations), len(s.operands)])
        if hasattr(s, "key"):
            rec.append(len(s.key))
        sigs.append(json.dumps(rec, separators=(",", ":")))
    sigs.sort()
    h.update(b"PLAN:")
    for sig in sigs:
        h.update(sig.encode())
        h.update(b"\n")


def eval_suite(kind, indices=None):
    """Compute deterministic Phase-1 fingerprints for TPC-DS / TPC-H / optimized TPC-H queries."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import Table, ensure_tables
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    if kind == "opt_tpch":
        sqls = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        want = range(len(sqls)) if indices is None else indices
        out = {}
        for i in want:
            _, sql, opt = sqls[i]
            a = transpile(sql, write="duckdb")[0]
            b = transpile(opt, write="duckdb")[0]
            out[i] = hashlib.blake2b(json.dumps([a, b]).encode(), digest_size=16).hexdigest()
        return out

    schema_dict = TPCDS_SCHEMA if kind == "tpcds" else TPCH_SCHEMA
    rel = "optimizer/tpc-ds/tpc-ds.sql" if kind == "tpcds" else "optimizer/tpc-h/tpc-h.sql"
    sqls = list(load_sql_fixture_pairs(rel))
    empty_tables = {}
    for tbl, cols in schema_dict.items():
        t = Table(columns=cols, rows=[])
        t._p1_tag = tbl
        empty_tables[tbl] = t
    schema = ensure_schema(schema_dict)
    want = range(len(sqls)) if indices is None else indices
    out = {}
    for i in want:
        meta, sql, _ = sqls[i]
        expr = pickle.loads(pickle.dumps(parse_one(sql)))
        if not meta.get("execute"):
            out[i] = {"exec": False, "py_fp": "noexec", "dk_fp": "", "tables": []}
            continue
        tnames = sorted({t.name for t in find_tables(expr)})
        tables = ensure_tables({name: empty_tables[name] for name in tnames})
        if tables.supported_table_args and tables.supported_table_args != schema.supported_table_args:
            raise RuntimeError("supported_table_args mismatch")
        scan_log = []
        orig_find = tables.find
        def logged_find(table, *args, _orig=orig_find, _log=scan_log, **kwargs):
            res = _orig(table, *args, **kwargs)
            _log.append([str(table), getattr(res, "_p1_tag", None), list(res.columns) if hasattr(res, "columns") else None])
            return res
        tables.find = logged_find
        optimized = optimize(expr, schema, leave_tables_isolated=True)
        optimized.sql(pretty=True)
        plan = Plan(optimized)
        str(plan)
        dag_sig = sorted(
            [type(s).__name__, s.name, sorted([type(d).__name__, d.name] for d in deps)]
            for s, deps in plan.dag.items()
        )
        pe = PythonExecutor(tables=tables)
        gen_log = []
        orig_gen = pe.generator.generate
        def logged_gen(e, _orig=orig_gen, _log=gen_log):
            s = _orig(e)
            _log.append(s)
            return s
        pe.generator.generate = logged_gen
        step_cols = []
        orig_table = pe.table
        def logged_table(expressions, _orig=orig_table, _log=step_cols):
            t = _orig(expressions)
            _log.append(list(t.columns))
            return t
        pe.table = logged_table
        result = pe.execute(plan)
        seen_sq = set()
        subplans = []
        while True:
            todo = sorted(k for k in pe._subquery_plans if k not in seen_sq)
            if not todo:
                break
            for k in todo:
                seen_sq.add(k)
                subplan, _ = pe._subquery_plans[k]
                subplans.append((k, subplan))
                pe.execute(subplan)
        rt = pickle.loads(pickle.dumps(result))
        ho = hashlib.blake2b(digest_size=16)
        ho.update(json.dumps(tnames).encode())
        _ast_fp(optimized, ho)
        opt_fp = ho.hexdigest()
        h = hashlib.blake2b(digest_size=16)
        h.update(json.dumps([tnames, dag_sig, list(rt.columns), sorted(scan_log), sorted(step_cols), sorted(gen_log)]).encode())
        _ast_fp(optimized, h)
        _plan_fp(plan, h)
        for k, sp in sorted(subplans, key=lambda kv: kv[0]):
            h.update(f"SUB:{k};".encode())
            _plan_fp(sp, h)
        py_fp = h.hexdigest()
        duck_sql = transpile(sql, write="duckdb")[0]
        dk_fp = hashlib.blake2b(duck_sql.encode(), digest_size=16).hexdigest()
        out[i] = {"exec": True, "opt_fp": opt_fp, "py_fp": py_fp, "dk_fp": dk_fp, "tables": sorted(set(tnames))}
    return out


def main():
    root = os.path.abspath(sys.argv[1])
    row_exec_path = sys.argv[2]
    dest = sys.argv[3]
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, root)
    row_exec = json.load(open(row_exec_path))
    t0 = time.process_time()
    tpcds_a = eval_suite("tpcds")
    tpcds_b = eval_suite("tpcds")
    assert tpcds_a == tpcds_b, "tpcds Phase-1 not deterministic!"
    tpch = eval_suite("tpch")
    opt_tpch = eval_suite("opt_tpch")
    data = {
        "row_exec_lines": row_exec["row_exec_lines"],
        "tpcds": [tpcds_a[i] for i in range(len(tpcds_a))],
        "tpch": [tpch[i] for i in range(len(tpch))],
        "opt_tpch": [opt_tpch[i] for i in range(len(opt_tpch))],
    }
    with open(dest, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"wrote {dest} ({os.path.getsize(dest)} bytes) in {time.process_time()-t0:.2f}s CPU; deterministic=True")


if __name__ == "__main__":
    main()
