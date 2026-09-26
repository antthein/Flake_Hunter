# FlakeHunter — Hackathon Plan (v3)

## Goal
A one-day CLI tool that finds flaky tests in a Python/pytest project, diagnoses them, auto-patches them via IBM Bob (agent mode), and proves the fix. Outputs a Markdown report, an HTML report, and per-patch `.diff` files. Runs on Windows (no `patch` binary required).

---

## Project Structure

```
FlakeHunter/
├── demo_app/
│   ├── app.py                    # tiny sample app (counter, fake-network, random picker)
│   └── tests/
│       ├── test_stable.py        # ~8-10 always-passing tests
│       ├── test_flaky_sleep.py   # flaky: timing / time.sleep race
│       ├── test_flaky_random.py  # flaky: random without seed
│       ├── test_flaky_network.py # flaky: simulated ~30% timeout (no real internet)
│       └── test_flaky_state.py   # flaky: two tests share module-level state, order-dependent
│
├── demo_originals/               # git-committed snapshot of the flaky demo (never collected by pytest)
│   ├── app.py                    # original flaky app.py
│   └── tests/
│       ├── test_stable.py
│       ├── test_flaky_sleep.py
│       ├── test_flaky_random.py
│       ├── test_flaky_network.py
│       └── test_flaky_state.py
│
├── flakehunter/
│   ├── __init__.py
│   ├── cli.py                    # entry point — Click CLI
│   ├── runner.py                 # re-run detection (subprocess pytest, N passes)
│   ├── scanner.py                # static hint scanner (AST walk, ≤60 lines)
│   ├── diagnoser.py              # parallel Bob subprocesses (ThreadPoolExecutor)
│   ├── patcher.py                # difflib diff, backup/apply/verify-50/revert
│   └── reporter.py               # Markdown + self-contained HTML report
│
├── reports/                      # created at runtime
│   ├── report.md
│   ├── report.html
│   └── <test_name>.diff          # one per attempted patch
│
├── requirements.txt
├── pyproject.toml
└── PLAN.md                       # this file
```

---

## Part 1 — demo_app/

### `app.py`
Four functions used by the tests. No real network calls — everything is local.

- `increment(counter_dict, key)` — mutates a shared dict in-place; used by state tests
- `random_winner(items)` — returns `random.choice(items)` with no seed
- `fake_fetch(url)` — simulates a flaky HTTP call:
  - calls `random.random()`; if result < 0.30 raises `TimeoutError("simulated timeout")`
  - otherwise returns `{"status": 200}`
  - Fully offline and controllable
- `compute(x, y)` — pure arithmetic; used by stable tests

### `tests/test_stable.py` (~8-10 tests)
Pure, deterministic tests: arithmetic, string ops, list slicing, dict access, type checks.
Never fail. Exist to prove FlakeHunter doesn't false-positive on good tests.

### `tests/test_flaky_sleep.py` (1 test)
**Hint category:** `timing`

Records `time.time()`, calls `time.sleep(random.uniform(0.0, 0.15))`, asserts elapsed < 0.1 s.
Fails ~33% of runs (when sleep lands in the 0.1–0.15 s range).
**Target failure rate: ~33% of runs.**

### `tests/test_flaky_random.py` (1 test)
**Hint category:** `random`

Calls `random_winner(["a", "b", "c"])` and asserts the result is **in `["a", "b"]`**.
Fails ~33% of runs (when `"c"` is chosen).
**Target failure rate: ~33% of runs.**

### `tests/test_flaky_network.py` (1 test)
**Hint category:** `network`

Calls `fake_fetch("http://example.com/api")` and asserts `result["status"] == 200`.
`fake_fetch` raises `TimeoutError` ~30% of the time using `random.random()`.
No real internet access needed — fully self-contained.
**Target failure rate: ~30% of runs.**

### `tests/test_flaky_state.py` (2 tests in 1 file)
**Hint category:** `shared-state`

```python
COUNTER = {"value": 0}          # module-level mutable state

def test_state_increment():
    increment(COUNTER, "value")  # adds 1
    assert COUNTER["value"] == 1 # passes only if run first

def test_state_reset():
    assert COUNTER["value"] == 0 # passes only if run first
```

With `pytest-randomly` changing test order each run, one of the two tests will fail depending
on execution order. **Target failure rate: ~50% of runs** (fails whenever `test_state_reset`
runs after `test_state_increment`).

**`pytest-randomly`** is added to `requirements.txt`. It randomises test collection order automatically.

---

### `demo_originals/` — snapshot for `reset-demo`

`demo_originals/` lives at the **project root** (not inside `demo_app/tests/`), so pytest
**never collects it**. It contains an exact copy of the original flaky files at scaffold time:

```
demo_originals/
├── app.py
└── tests/
    ├── test_stable.py
    ├── test_flaky_sleep.py
    ├── test_flaky_random.py
    ├── test_flaky_network.py
    └── test_flaky_state.py
```

`flakehunter reset-demo` copies these files back over `demo_app/app.py` and `demo_app/tests/*`,
restoring the full flaky demo without a git checkout. Both `app.py` and all test files are restored.

---

## Part 2 — flakehunter/

### `cli.py` — Click CLI

```
flakehunter run <project_path>
  --runs        N      # re-run passes for detection (default: 20)
  --workers     W      # parallel Bob subprocesses (default: 4)
  --max-cost    $      # per-Bob-call cost cap (default: 1.0)
  --dry-run            # skip Bob calls; report hints only, no patches
  --output      DIR    # where to write reports (default: ./reports)
  --ci-runs-per-day N  # for time-saved formula (default: 10)
  --minutes-per-rerun N# for time-saved formula (default: 5)

flakehunter reset-demo
  # Copies demo_originals/app.py  → demo_app/app.py
  # Copies demo_originals/tests/* → demo_app/tests/*
  # Prints "Demo reset to original flaky state."
```

Orchestrates phases in order: detect → hint → diagnose → patch → report.

---

### Phase 1 — `runner.py` — Re-run Detection

**Algorithm:**
1. Run `pytest <project_path> --tb=no -q --json-report --json-report-file=<tmp>` as a subprocess **N times** (default 20).
2. Collect pass/fail outcome per test node ID on each run by reading the JSON report.
3. A test is **flaky** if it has ≥1 PASS **and** ≥1 FAIL across the N runs.
4. Return a list of `FlakySummary(node_id, pass_count, fail_count)`.

**Key decisions:**
- Use `pytest-json-report` for reliable machine-readable output.
- `pytest-randomly` shuffles test order automatically on every run (no extra flag needed).
- Runs are **sequential** to avoid inter-test interference masking flakiness.
- All tests are always run — static hints never filter or skip a test.
- After all N runs, compute **full-suite pass rate before patching** (stored for the report header).

---

### Phase 2 — `scanner.py` — Static Hint Scanner

**Algorithm:**
1. For each flaky `node_id`, resolve its source file path from the node ID string.
2. `ast.parse()` the file and walk all nodes.
3. Attach hint labels for patterns found **anywhere in the file**:

| Hint label     | AST pattern to detect                                         |
|----------------|---------------------------------------------------------------|
| `timing`       | `time.sleep(...)` call or `import time`                       |
| `random`       | `random.*` call or `import random`                            |
| `network`      | import of `urllib`, `requests`, `httpx`, `socket`, or `http` |
| `shared-state` | module-level name assigned a `list` or `dict` literal         |

4. Return `hints: list[str]` per flaky test (empty list is valid).
5. **Hints are advisory only** — injected into the Bob prompt as context clues. They never gate, skip, or filter a test.

**Constraint:** ≤60 lines, stdlib only.

---

### Phase 3 — `diagnoser.py` — Bob-Powered Diagnosis & Patch (Windows-safe)

#### Workspace isolation
Each parallel Bob invocation gets its **own temporary copy** of the project:
```python
tempfile.mkdtemp() → temp_workspace
shutil.copytree(
    project_path, temp_workspace,
    ignore=shutil.ignore_patterns(
        "__pycache__", ".pytest_cache", "reports", ".venv", ".git"
    ),
    dirs_exist_ok=True,
)
```
Bob edits files **inside** the temp workspace. The real project is never touched during diagnosis.
After Bob finishes, `diagnoser.py` computes the diff and deletes the temp dir.

#### Bob invocation
Resolve the Bob executable once at startup:
```python
BOB_EXE = shutil.which("bob") or shutil.which("bob.cmd")
```
Spawn one subprocess per flaky test, all in **parallel** via `ThreadPoolExecutor(max_workers=workers)`:
```
bob run "<prompt>" \
  --workspace <temp_workspace> \
  --format json \
  --trust \
  --accept-license \
  --max-cost 1.0
```

#### Bob prompt — test-file-only constraint
Bob must **only edit the test file** for its assigned flaky test. `app.py` must never be modified.
This prevents parallel Bob processes from overwriting each other's changes to shared app files.

Fix strategy per category (in the prompt):
- **timing**: use `unittest.mock.patch` or `monkeypatch` to control `time.sleep` in the test
- **random**: seed `random` in the test or use `monkeypatch` to patch `random.choice`
- **network**: mock `fake_fetch` via `monkeypatch` in the test so it never raises
- **shared-state**: add a `setup_function` or fixture in the test file that resets the shared state before each test

```
You are fixing a flaky pytest test. Work entirely inside the workspace provided.
You MUST only edit the test file listed below. Do NOT modify app.py or any other file.

Test node: <node_id>
Test file: <relative_path_to_test_file>
Hints: <hint labels>
Pass rate: <pass_count>/<N> runs

Read the test file and the app source it imports (for context only — do not edit app.py).
Fix the test so it becomes deterministic by editing ONLY the test file.
Use monkeypatch, fixtures, or setup_function as needed.

After editing, respond with ONLY valid JSON (no markdown fences):
{
  "root_cause": "<one of: timing | random | network | shared-state | other>",
  "explanation": "<exactly 2 sentences describing the root cause and your fix>",
  "files_changed": ["<relative path to test file only>"]
}
```

#### Bob output parsing
**Before writing the parser, run one live `bob run --format json` test** to inspect real stdout shape. The parser must handle the actual JSON envelope Bob returns. Code against the real shape, not an assumed shape.

If `--dry-run` is set, skip all Bob calls; return `DiagnosisResult` with `diff_text=None`, `root_cause="dry-run"`, `explanation=""`.

#### Diff computation (no `patch` binary needed)
After Bob edits files in the temp workspace:
1. For each path in `files_changed`, compare original file (from project) vs edited file (from temp workspace) using `difflib.unified_diff()`.
2. Concatenate all per-file diffs into one unified diff string.
3. Save to `reports/<sanitized_node_id>.diff` unconditionally.
4. Return `DiagnosisResult(node_id, diff_text, root_cause, explanation, files_changed)`.

---

### Phase 4 — `patcher.py` — Apply, Verify 50/50, Revert or Keep (Windows-safe)

No `patch` binary used anywhere. All file operations are pure Python.

**For each `DiagnosisResult` with non-None `diff_text`:**

1. **Backup originals:**
   ```python
   backups = {}
   for rel_path in result.files_changed:
       backups[rel_path] = Path(project / rel_path).read_text(encoding="utf-8")
   ```

2. **Apply patch** by copying changed files from the temp workspace into the real project:
   ```python
   for rel_path in result.files_changed:
       shutil.copy(temp_workspace / rel_path, project / rel_path)
   ```
   If any copy fails → record `status=patch_failed`, restore backups, skip verification.

3. **Verify:** Run the **entire test file** (not just the single node ID) 50 times sequentially
   with `pytest-randomly` active so order varies on every pass:
   ```
   pytest <path_to_test_file> --tb=no -q --json-report --json-report-file=<tmp>
   ```
   From each JSON report, extract the outcome of `<node_id>` specifically.
   Count passes and failures for that node across all 50 runs.

   > **Why the whole file?** Order-dependent tests (shared-state category) can only be proven
   > fixed by running all tests in the file together so `pytest-randomly` can exercise all orderings.
   > Running a single node ID in isolation would always pass and give a false positive.

4. **Decision:**
   - All 50 passes for `<node_id>` → **keep the patch**. `status=fixed`.
   - Any failure → **revert**: write `backups[rel_path]` back to disk for every changed file. `status=suggestion_only`.

5. Return `PatchResult(node_id, status, diff_path, root_cause, explanation)`.

**After all patches are evaluated**, compute **full-suite pass rate after patching** (stored for the report header).

---

### Phase 5 — `reporter.py` — Markdown + HTML Report

Generates two files in `<output>/`.

#### Report header fields
```
Generated: <ISO timestamp>
Project:   <project_path>
Detection runs: N

Flaky tests found:    X  →  after patching: Y remaining
Full-suite pass rate: A%  →  after patching: B%

Estimated developer time saved: ~Z hours
  Formula: (fixed_count × 4 h) + (suggestion_count × 1 h)
  Assumptions: 4 h to manually find+fix a flaky test; 1 h to apply a suggestion.
  Configurable via --ci-runs-per-day and --minutes-per-rerun (shown if provided).
```

If `--ci-runs-per-day` and `--minutes-per-rerun` are given, also show:
```
  CI time saved per day: fixed_count × ci_runs_per_day × minutes_per_rerun minutes
```

#### `report.md` structure:
```markdown
# FlakeHunter Report
...header fields...

## Summary
| Test | Pass/N | Fail/N | Hints | Root Cause | Outcome |
|------|--------|--------|-------|------------|---------|
| ...  |  14/20 |  6/20  | timing | timing | ✅ fixed |

## Details
### <test_node_id>
- **Hints:** timing
- **Root cause:** timing
- **Explanation:** The sleep duration is random and can exceed the assertion threshold. The fix patches time.sleep in the test using monkeypatch so the duration is always deterministic.
- **Outcome:** fixed
- **Diff:** [view patch](test_name.diff)
```

#### `report.html`
- Single self-contained `.html` file — inline CSS, zero external dependencies.
- Same content rendered as styled HTML table.
- Color-coded rows: 🟢 green = `fixed`, 🟡 yellow = `suggestion_only`, 🔴 red = `patch_failed`, ⚪ grey = no diff / dry-run.
- Each row has a collapsible `<details><summary>Show diff</summary><pre>...</pre></details>` block.
- Report header block at the top with before/after stats, formula, and estimated time saved.

---

## Data Flow

```
cli.py
  │
  ├─► runner.py        → list[FlakySummary]  +  before_pass_rate
  │
  ├─► scanner.py       → dict[node_id → list[str]]  (hints)
  │
  ├─► diagnoser.py     → list[DiagnosisResult]
  │    ├── per test: mkdtemp → copytree (ignore: __pycache__/.git/.venv/reports)
  │    │             → bob subprocess → parse JSON → difflib diff → rmtree
  │    └── all tests run in parallel via ThreadPoolExecutor
  │
  ├─► patcher.py       → list[PatchResult]   +  after_pass_rate
  │    ├── backup originals (in-memory)
  │    ├── apply by file copy (no patch binary)
  │    ├── verify 50× by running WHOLE TEST FILE with pytest-randomly
  │    │   and tracking node_id outcome from JSON report
  │    └── revert if any failure (restore from in-memory backup)
  │
  └─► reporter.py      → reports/report.md
                          reports/report.html
                          reports/<test>.diff
```

---

## `requirements.txt`

```
click>=8.1
pytest>=7.0
pytest-json-report>=1.5
pytest-randomly>=3.12
```

No binary dependencies. Works on Windows, macOS, Linux.

---

## `pyproject.toml`

```toml
[project]
name = "flakehunter"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "click>=8.1",
    "pytest>=7.0",
    "pytest-json-report>=1.5",
    "pytest-randomly>=3.12",
]

[project.scripts]
flakehunter = "flakehunter.cli:main"

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
```

---

## `reset-demo` command

`flakehunter reset-demo` restores the demo to its original flaky state without a git checkout.

**Implementation:**
1. Copy `demo_originals/app.py` → `demo_app/app.py`
2. Copy every file in `demo_originals/tests/` → `demo_app/tests/`
3. Print "Demo reset to original flaky state."

`demo_originals/` is committed to git at scaffold time and never modified by FlakeHunter.
Because it lives at the project root (not under `demo_app/tests/`), pytest never collects it.

---

## Bob Output Parser — Pre-flight Step

**Before implementing the JSON parser**, run this one-off command and capture real stdout:
```
bob run "Hello, respond with JSON: {\"status\": \"ok\"}" --format json --trust --accept-license
```
Inspect the structure (top-level keys, nesting, how text content is embedded).
Code the parser in `diagnoser.py` against the **actual** output shape, not an assumed shape.

---

## One-Day Build Order

| # | Task | Est. |
|---|------|------|
| 1 | Scaffold folders, `pyproject.toml`, `requirements.txt`, `git init` + first commit | 20 min |
| 2 | Write `demo_app/app.py` + all test files + `demo_originals/` snapshot | 35 min |
| 3 | `runner.py` — re-run detection loop + `before_pass_rate` | 45 min |
| 4 | `scanner.py` — AST hint scanner | 30 min |
| 5 | `cli.py` — Click wiring (`run` + `reset-demo`), `--dry-run`, `--max-cost 1.0` default | 30 min |
| 6 | Pre-flight: run `bob run --format json` and inspect output shape | 15 min |
| 7 | `diagnoser.py` — temp workspace isolation (with ignore patterns), Bob subprocess, JSON parser, difflib diff, ThreadPoolExecutor | 75 min |
| 8 | `patcher.py` — backup / apply-by-copy / verify-whole-file-50× / revert + `after_pass_rate` | 50 min |
| 9 | `reporter.py` — Markdown + HTML (header stats, formula, root cause, explanation, diff blocks) | 50 min |
|10 | End-to-end smoke test with `--dry-run` | 20 min |
|11 | End-to-end test with real Bob (credits) | 30 min |
| **Total** | | **~6.5 hr** |

---

## Key Design Constraints (do not drift from these)

- **Static hints are advisory only** — re-run detection is always the source of truth for flakiness.
- **No `patch` binary** — apply changes by file copy (Python only). Revert by restoring in-memory string backups.
- **Workspace isolation** — each parallel Bob call gets its own `tempfile.mkdtemp()` copy of the project (with `shutil.ignore_patterns("__pycache__", ".pytest_cache", "reports", ".venv", ".git")`). The real project is never touched during diagnosis.
- **Bob edits only the test file** for its assigned test — never `app.py` or any other file. This is enforced by the prompt and verified by checking `files_changed`.
- **Bob executable** resolved via `shutil.which("bob") or shutil.which("bob.cmd")`.
- **ThreadPoolExecutor** for parallelism (not ProcessPoolExecutor) — subprocesses are already the unit of work.
- **`--dry-run`** skips ALL Bob calls; detect + hint + report still run normally.
- **Default `--max-cost 1.0`** per Bob call.
- **Verification runs the whole test file 50×** (not just the single node ID) so order-dependent tests are exercised with varying orderings via `pytest-randomly`. The node ID's outcome is tracked from the JSON report.
- Patch kept **only on 50/50**; otherwise file is auto-reverted from in-memory backup.
- Every attempted diff saved to `reports/<test>.diff` regardless of outcome.
- **HTML report is fully self-contained** — no CDN, no external CSS or JS.
- **Pre-flight Bob output inspection** is a mandatory step before writing the JSON parser.
- **`demo_originals/`** lives at project root (never collected by pytest); **`reset-demo`** restores both `app.py` and all test files.
- **`git init`** + first commit after scaffold (step 1 of build order).
- Flaky test target failure rates: sleep ~33%, random ~33%, network ~30%, shared-state ~50%. Sleeps ≤ 0.15 s. No real internet.
- Time-saved formula and assumptions are **explicit and shown in the report**. CI rate and minutes-per-rerun are configurable flags.
