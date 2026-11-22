# RWX Blast Radius

This repository selects and schedules SQLGlot tests for the Builders Buildathon. The Docker entry point reads the patch directory and test manifest under `/data` and writes one compact JSON object per commit.

## Run the official sample

Unpack the inner `blast-radius-sample.tar.gz` from the organizer's download into its own directory. Pass the directory containing `score.py` and `sample/` to Make.

```sh
make sample KIT=/absolute/path/to/unpacked-kit
```

This builds for linux/amd64 and uses the organizer's run flags, including the read-only filesystem and 64 MB temporary directory. The sample scorer prints simulated seconds and missed commits. The host-side scorer alone receives the public answers. For a practice entry, publish this repository on GitHub, then enter its URL and the output of `git rev-parse HEAD` on the track page.

## Measured results

| Public sample of 30 patches | Simulated seconds | Missed commits |
| --- | ---: | ---: |
| Organizer's starter | 1,210.470 | 0 |
| This entry | 94.373 | 0 |
| Minimum for the published failures | 94.351 | 0 |

The selector captured all 73 failing test/patch pairs. Measured under the organizer's Docker run flags, the container used about 8–9 CPU seconds on the sample, so the local total is about 102–103 seconds. On 76 further patches labeled locally with full test runs (function-body mutations, class-level and module-level constants, comments, fixtures, test files, renames, deletions and syntax errors) it also captured every failing test, and likewise on a second labeled set of 60 unusual changes (documentation and packaging edits, added, deleted and renamed files, binary fixtures, reshaped test modules, refactors), also when tests that no longer exist are counted as failing. On a third fully labeled set of 130 patches imitating the sample's own mutation operators, no failing test was missed (0 misses across all 296 labeled patches).

These checks ran the image built from this repository, which starts from the organizer's pinned linux/amd64 Python image and installs the organizer's package lock. The organizers' hidden scoring service was not part of this verification.

## How a decision is made

1. Apply each unified diff against the pinned revision in memory. Compare Python syntax and compiled code so a comment outside a string can take a different path from a comment inside a SQL literal. Docstring-only edits are classified separately (`docstring`) so only tests inspecting `__doc__` are affected. Check files independently when a patch changes several.
2. Match function edits to lines executed by each test. Take the union across edits. Include observed fixture readers, test setup, inherited classes, import-time execution, and direct readers of Python source. Fresh-process import checks guard class and module bindings. Unrecognized changes can select the whole suite.
3. For single-operator comparisons in a single non-equivalent function-body edit, inspect the operand relationships observed in the pristine test run (`values.pkl.gz`), including read-only structural hashes for `Expression` nodes, types, enums, and disjoint built-ins without invoking custom equality, along with per-test call-phase line execution to account for short-circuiting. A test can be removed when every observed input gives the same result under both expressions. Opaque values, missing observations and incomplete traces keep their candidates.
4. Before running full candidate probes, decide the expensive fixture-driven tests using precomputed query-level maps (`exec_phase1.json`, `slices.json`, and `opt_slices.json`):
   - For the executor TPC tests (`test_execute_tpcds`, `test_execute_tpch`, `test_optimized_tpch`), when the edit does not touch test code or any library lines executed during real TPC-DS row evaluation (`row_exec_lines`), run a Phase-1 dry-run over only the reachable queries against empty tables (or just `optimize()` for pure-optimizer passes) and compare deterministic fingerprints of the optimized AST, generated Python expressions, result columns, and transpiled DuckDB SQL against the pinned snapshot, stopping early if differing queries exceed the cost budget. Any queries whose Phase-1 fingerprints differ (or edits touching row-execution lines) fall back to running only those query slices with lazy CSV table loading for only the referenced tables, stopping at the first failure.
   - For `TestOptimizer.test_tpcds` and `TestOptimizer.test_tpch`, run only the reachable queries one at a time, stopping at the first mismatch or pool break.
   When a remaining candidate set (from Python edits or round-trip SQL fixtures such as `identity.sql`, `pretty.sql`, `partial.sql`) is cheap to check, run ordinary unittest methods against a temporary checkout containing the exact changes (skipping `TestExecutor.setUpClass` when none of the three TPC tests are in the probe, and aborting failing tests at their first failing `subTest` when pool breakage is impossible). Probes and import-state snapshots fork from pre-warmed `PYTHONHASHSEED=0` zygote processes (amortizing stdlib and `numpy`/`pandas`/`duckdb` imports across commits while keeping every probe isolated in its own process with a hard deadline and private result stream; the snapshot zygote imports neither `unittest` nor `logging`). When a full probe is too expensive, targeted probes can still decide the optimizer pool-cascade subset, standalone bottleneck tests, or a cheap subset excluding heavy probe blockers. Keep failures and inconclusive tests.
5. An edit outside a plain function body (a class attribute, a module constant, a decorator, a default argument, a new or removed definition, an import) is judged by what it changes. The patched library is imported once in a fresh forked interpreter and every module global and class attribute is fingerprinted. Cells that differ from the pinned commit's snapshot are the edit's whole import-time effect, including values derived from it. A plain function-body edit on an import-time line whose only changed cell is the edited function itself passes through to line coverage and operand refinement. Each changed cell is mapped to the tests that read it: per-key reads for dictionaries, per-element reads for sets and frozensets, per-cell reads for wrapped scalar class attributes, attribute-name loads scoped to tests that imported that module otherwise, line coverage for functions, and the importing tests when a module no longer imports.
6. Tests that share the optimizer suite's class-level process pool are treated as one unit when the pool can break, because a worker that dies fails every later test using that pool. A worker dies only while unpickling what crosses the pool boundary; the lines that run there are recorded (`pool_lines.json`), so an edit inside a function body elsewhere cannot break the pool. For other edits a probe reports whether the pool broke.
7. A probe costs CPU seconds one for one, so it runs only when its measured cost (interpreter start, imports, class set-up, the tests) is below the scheduling time it is expected to save. Edited test code is never probed: a test run alone says nothing about state that its set-up or tear-down leaves for other tests.
8. Changes that are not an edit inside an existing library or fixture file are resolved per file (`structure.py`). A file no test opens, by the Python-level and system-call-level records in `fsdeps.json`, selects nothing; pytest's own configuration files are compared by their pytest tables. A deleted or renamed file selects its recorded readers; for a non-eager `sqlglot/` module it selects its recorded importers, line/file readers and untracked tests, and for a test module its own tests and every test that runs its code. An added file selects the tests that looked for that path, listed its folder, or tried to import that module name and failed (literal-only added `sqlglot/*.py` files hitting a failed import are judged by import-state diffing); names pytest or the interpreter pick up on their own (`conftest.py`, a package `__init__.py`, a module shadowing an installed one) select everything. Single-file Git binary patches (`literal` or `delta`) to TPC-H or TPC-DS `.csv.gz` fixtures are decoded in memory and validated against `TestExecutor.setUpClass` and the queries reading that table. A reshaped test module is compared unit by unit: a changed, removed or renamed test selects that test, a changed helper selects the tests that executed it, a change to a class body selects the class. Compiled code that differs only in the names of local variables (on code objects without frame readers such as `locals()`) or in redundant `pass` statements counts as unchanged.
9. A fixture that a test class loads once in `setUpClass` reaches a test only through the class attribute it is loaded into. `setup_reads.json` records, per test, which set-up attributes it reads and which positions of the loaded (meta, sql, expected) pairs it never inspects. The pinned loader is run on both versions of an edited fixture; a test that does not read the attribute, or inspects none of the positions that changed, is not selected. If the loader fails on the new text the whole class stays selected.
10. Try viable worker counts from one through eight. Assign the longest remaining test to the least-loaded worker. Choose the resulting schedule with the lowest makespan plus 2,000 ms per worker. Test identities come from the live manifest, so remapped test numbers work.

A 300-second miss penalty makes broad schedules worthwhile for ambiguous changes. Coverage is a measurement of a specific execution. Dynamic imports, reflective attributes, order-dependent tests, and process differences can make an unseen change reach beyond the recorded observations.

## Reproduce the profiles

The committed data files describe the unmodified SQLGlot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. They contain test IDs, source locations, observed operand relations and dependency information. Their builders take the upstream source tree as input. The checkout, sample patches and answers are separate from the submission repository.

After `make build`, regenerate the maps in a writable directory on the host. The normal competition build uses the committed maps directly.

```sh
mkdir -p profiles
docker run --rm --platform linux/amd64 -v "$PWD/profiles:/out" rwx-blast-radius \
  python /app/build_map.py /opt/sqlglot /out/map.pkl.gz
docker run --rm --platform linux/amd64 -v "$PWD/profiles:/out" rwx-blast-radius \
  python /app/values.py /opt/sqlglot /out/values.pkl.gz
docker run --rm --platform linux/amd64 -v "$PWD/profiles:/out" rwx-blast-radius \
  python /app/symbols.py /opt/sqlglot /out/scalars.json
```

Line coverage and container/attribute reads (dictionaries, sets, frozensets, and scalar class attributes via non-data descriptors) are additionally recorded with every test in its own fresh interpreter (`build/isolated_lines_run.py`, `build/key_reads_run.py`, `build/track.py`) and merged (`build/merge_isolated_lines.py`, `build/merge_key_reads.py`). A single-process run credits code that is computed once and cached, such as the generator's dispatch table or a lazily imported dialect, only to the first test that triggers it. `worldfp.py` writes the import-state snapshot in `world.pkl.gz` (run it with `PYTHONHASHSEED=0`: fingerprints are the interpreter's tuple hashes, comparable between processes of one Python build only under a fixed seed; the selector checks this and falls back when the builds differ), and `pool_users.json` lists the tests that share a process pool. `build/class_setup_run.py` records the set-up attribute and pair-position usage of step 9, by watching attribute reads and by re-running each test with one position replaced by an object that reports any use. `build/probe_cost_run.py` times a fresh-interpreter probe of every test alone (`probe_cost.json`), which is what step 7 prices a probe with. `build/slice_one.py` and `build/slices_run.py` record per-query line coverage and timings for the fixture-driven executor tests (`slices.json`), and `build/build_opt_slices.py` does the same for `TestOptimizer.test_tpcds` and `test_tpch` (`opt_slices.json`). `build/row_exec_run.py` traces the library lines executed during real TPC-DS row evaluation, and `build/build_phase1.py` records the deterministic Phase-1 dry-run fingerprints (`exec_phase1.json`). `build/pool_lines_one.py` records the library lines executed while unpickling at the pool boundary. `build/fsdeps/` traces every test file's process tree with strace and Python audit hooks: files opened, paths looked for and not found, folders listed, and module names whose import failed.

The coverage builder records both class setup and test execution. It runs process-pool work in the tracing process and preserves the pickle boundary for task arguments and results. The operand builder records supported comparisons while the original tests run. The scalar index checks references to locally consumed module constants across source and fixtures. Rebuilding these maps may take several minutes.

For selector regression tests, use Python 3.13 and run `make test`. The runtime switches `--no-probe` and `--debug` support local comparisons. The default entry point enables test probes. Diagnostics go to stderr.

## Provenance and license

The organizers supplied the base image digest, Python package lock, input contract, scoring formula and starter example of longest-first scheduling. SQLGlot and its upstream tests are MIT-licensed and fetched from the pinned revision during the build. Third-party Python packages are listed in `requirements.lock` and retain their licenses. The selector, scheduling code, profilers and regression tests were written for this entry with AI assistance. The entry is released under the MIT License in `LICENSE`.

[Official track and submission form](https://www.joinbuilders.org/buildathon/tracks/blast-radius) · [Pinned SQLGlot source](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa)
