---
title: FlakeHunter
emoji: 🎯
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# FlakeHunter

**Your CI is red. Your code is fine. Your tests are flaky.**

FlakeHunter finds flaky tests in a Python/pytest project, sends **IBM Bob 2.0** to diagnose and fix each one **in parallel**, and keeps a fix **only if it passes every verification run**. Otherwise the fix is reverted automatically.

Built for the [IBM Bob 2.0 Hackathon](https://lablab.ai/ai-hackathons/ibm-bob-2-hackathon) (lablab.ai, Sept 2026).

## The problem

A flaky test passes and fails on the same code. Teams lose hours clicking "re-run", and they start ignoring red builds, so real bugs slip through. Finding the cause by hand is slow: the failure disappears as soon as you look at it.

## What FlakeHunter does

| Phase | What happens |
|---|---|
| 1. Detect | Runs the whole suite N times in random order (`pytest-randomly`). A test that both passed and failed is flaky. |
| 2. Hint | A static AST scan adds clues (`timing`, `random`, `network`, `shared-state`). Hints never decide anything; only re-runs do. |
| 3. Diagnose | One `bob run` agent per flaky test, **in parallel**, each in its **own temp copy** of the project. Bob may only edit that test file, and returns the root cause and a 2-sentence explanation. |
| 4. Verify | Each fix is applied and its test file is re-run 50× in random order. 50/50 means it is kept; any failure means it is reverted, byte-for-byte. |
| 5. Prove | The whole suite runs N more times to measure before vs after. |
| 6. Report | A self-contained HTML report: before/after, root causes, Bob's explanations, diffs, CI time saved. |

### Result on the demo shop app (real IBM Bob run, 2026-09-26)

| | Before | After |
|---|---|---|
| Flaky tests | 4 | **0** |
| CI runs fully green | 15% | **100%** |
| Bob fixes kept (50/50 verified) | | **4 / 4** |

Bob identified shared state leaking between tests, a random sleep exceeding an SLA, a simulated network timeout, and an unseeded random choice. Each fix was a minimal test-only change (`setup_function`, `monkeypatch`).

## Try it

**Web demo (hosted):** see the Application URL in the submission. It runs in **demo mode**: detection, patch application and verification run live on the server, while Bob's diagnoses are replayed from [`demo_data/bob_run.json`](demo_data/bob_run.json), a recording of the real IBM Bob 2.0 run above. That way the public page needs no API key.

**Locally:**

```bash
pip install -e .
flakehunter run demo_app --dry-run                      # detect + hints, no Bob calls
flakehunter run demo_app --runs 20 --max-cost 0.5       # full run with IBM Bob (needs Bob Shell + BOB_API_KEY)
flakehunter run demo_app --replay-bob demo_data/bob_run.json   # replay the recorded Bob run
flakehunter reset-demo                                  # restore the flaky demo tests
flakehunter serve                                       # web UI on http://127.0.0.1:8000
```

Set `FLAKEHUNTER_MODE=live` before `flakehunter serve` to call IBM Bob for real from the web UI.

## Safety by design

- Bob works in an isolated temp copy of the project. The real project is only touched by the patcher, and only with a verified fix.
- If Bob edits any file other than the test file, the fix is rejected.
- A fix is kept only on 50/50 verification passes in random order. Otherwise the original bytes are restored.
- `--max-cost` caps spending for each Bob call, and there is a 300 s timeout per Bob process.
- On Windows, `bob` is a `.cmd` file that truncates multi-line arguments. FlakeHunter writes the task to `FLAKEHUNTER_TASK.md` in the workspace and passes Bob a one-line prompt.

## How IBM Bob was used

1. **As the builder.** IBM Bob 2.0 (IDE, Plan and Agent modes) wrote FlakeHunter's core: the demo app, `runner.py`, `scanner.py`, `cli.py`, `diagnoser.py`, `patcher.py`, `reporter.py` and record/replay. See the git history.
2. **As the engine.** At runtime FlakeHunter calls Bob Shell (`bob run`, headless agent mode) once per flaky test, in parallel, to diagnose and fix it.

After the team's Bob credits ran out, the web UI (`pipeline.py`, `web.py`, `static/index.html`), the Dockerfile and this README were added with help from another AI coding assistant.

## Project layout

```
demo_app/          small shop app with 11 stable + 4 flaky tests (the target)
demo_originals/    pristine copy of the flaky demo (reset-demo, web runs)
demo_data/         recording of the real IBM Bob run, used by demo mode
flakehunter/       runner, scanner, diagnoser (Bob), patcher, reporter, pipeline, web
```

## License

MIT
