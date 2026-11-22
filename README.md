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
| This entry | 96.839 | 0 |
| Minimum for the published failures | 94.351 | 0 |

The selector captured all 73 failing test/patch pairs. Measured under the organizer's Docker run flags, the container used about 9 CPU seconds on the sample, so the local total is about 106 seconds. On 76 further patches labeled locally with full test runs (function-body mutations, class-level and module-level constants, comments, fixtures, test files, renames, deletions and syntax errors) it also captured every failing test. A second set of unusual changes (documentation and packaging edits, added, deleted and renamed files, binary fixtures, reshaped test modules, refactors) is being labeled the same way; on the patches labeled so far no failing test was missed.

These checks ran the image built from this repository, which starts from the organizer's pinned linux/amd64 Python image and installs the organizer's package lock. The organizers' hidden scoring service was not part of this verification.

## How a decision is made

1. Apply each unified diff against the pinned revision in memory. Compare Python syntax and compiled code so a comment outside a string can take a different path from a comment inside a SQL literal. Check files independently when a patch changes several.
2. Match function edits to lines executed by each test. Take the union across edits. Include observed fixture readers, test setup, inherited classes, import-time execution, and direct readers of Python source. Fresh-process import checks guard class and module bindings. Unrecognized changes can select the whole suite.
3. For supported comparisons, inspect the relationships between operands observed in the pristine test run. A test can be removed when every observed input gives the same result under both expressions. Opaque values, missing observations and incomplete traces keep their candidates.
4. When a small candidate set is cheap to check, run ordinary unittest methods against a temporary checkout containing the exact changes. Keep failures and inconclusive tests. The subprocess has a deadline and its own result stream. The selector can still make decisions when a probe times out.
5. An edit outside a plain function body (a class attribute, a module constant, a decorator, a default argument, a new or removed definition, an import) is judged by what it changes. The patched library is imported once in a fresh interpreter and every module global and class attribute is fingerprinted. Cells that differ from the pinned commit's snapshot are the edit's whole import-time effect, including values derived from it. Each changed cell is mapped to the tests that read it: per-key reads for dictionaries, attribute-name loads otherwise, line coverage for functions, and the importing tests when a module no longer imports.
6. Tests that share the optimizer suite's class-level process pool are treated as one unit when the pool can break, because a worker that dies fails every later test using that pool. A worker dies only while unpickling what crosses the pool boundary; the lines that run there are recorded (`pool_lines.json`), so an edit inside a function body elsewhere cannot break the pool. For other edits a probe reports whether the pool broke.
7. A probe costs CPU seconds one for one, so it runs only when its measured cost (interpreter start, imports, class set-up, the tests) is below the scheduling time it is expected to save. Edited test code is never probed: a test run alone says nothing about state that its set-up or tear-down leaves for other tests.
8. Changes that are not an edit inside an existing library or fixture file are resolved per file (`structure.py`). A file no test opens, by the Python-level and system-call-level records in `fsdeps.json`, selects nothing; pytest's own configuration files are compared by their pytest tables. A deleted or renamed file selects its recorded readers, and for a test module its own tests and every test that runs its code. An added file selects the tests that looked for that path, listed its folder, or tried to import that module name and failed; names pytest or the interpreter pick up on their own (`conftest.py`, a package `__init__.py`, a module shadowing an installed one) select everything. A reshaped test module is compared unit by unit: a changed, removed or renamed test selects that test, a changed helper selects the tests that executed it, a change to a class body selects the class. Compiled code that differs only in the names of local variables or in redundant `pass` statements counts as unchanged.
9. Try viable worker counts from one through eight. Assign the longest remaining test to the least-loaded worker. Choose the resulting schedule with the lowest makespan plus 2,000 ms per worker. Test identities come from the live manifest, so remapped test numbers work.

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

Line coverage and dictionary reads are additionally recorded with every test in its own fresh interpreter (`build/isolated_lines_run.py`, `build/key_reads_run.py`) and merged (`build/merge_isolated_lines.py`, `build/merge_key_reads.py`). A single-process run credits code that is computed once and cached, such as the generator's dispatch table or a lazily imported dialect, only to the first test that triggers it. `worldfp.py` writes the import-state snapshot in `world.pkl.gz` (run it with `PYTHONHASHSEED=0`: fingerprints are the interpreter's tuple hashes, comparable between processes of one Python build only under a fixed seed; the selector checks this and falls back when the builds differ), and `pool_users.json` lists the tests that share a process pool. `build/pool_lines_one.py` records the library lines executed while unpickling at the pool boundary. `build/fsdeps/` traces every test file's process tree with strace and Python audit hooks: files opened, paths looked for and not found, folders listed, and module names whose import failed.

The coverage builder records both class setup and test execution. It runs process-pool work in the tracing process and preserves the pickle boundary for task arguments and results. The operand builder records supported comparisons while the original tests run. The scalar index checks references to locally consumed module constants across source and fixtures. Rebuilding these maps may take several minutes.

For selector regression tests, use Python 3.13 and run `make test`. The runtime switches `--no-probe` and `--debug` support local comparisons. The default entry point enables test probes. Diagnostics go to stderr.

## Provenance and license

The organizers supplied the base image digest, Python package lock, input contract, scoring formula and starter example of longest-first scheduling. SQLGlot and its upstream tests are MIT-licensed and fetched from the pinned revision during the build. Third-party Python packages are listed in `requirements.lock` and retain their licenses. The selector, scheduling code, profilers and regression tests were written for this entry with AI assistance. The entry is released under the MIT License in `LICENSE`.

[Official track and submission form](https://www.joinbuilders.org/buildathon/tracks/blast-radius) · [Pinned SQLGlot source](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa)
