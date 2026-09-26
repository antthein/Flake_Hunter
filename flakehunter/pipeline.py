"""
flakehunter/pipeline.py -- the full detect -> hint -> diagnose -> verify -> report
pipeline as one function that emits progress events, for the web UI.

Every run works on a fresh temp copy of the project, so several visitors can run
at once and the source project is never modified.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

from flakehunter.diagnoser import diagnose
from flakehunter.patcher import patch_and_verify
from flakehunter.reporter import write_report
from flakehunter.runner import FlakySummary, _single_run
from flakehunter.scanner import scan

Emit = Callable[[str, dict], None]

_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "reports", ".venv", ".git")


def run_pipeline(
    source_project: Path,
    run_dir: Path,
    emit: Emit,
    *,
    runs: int = 15,
    verify_runs: int = 20,
    workers: int = 4,
    max_cost: float = 0.5,
    replay_path: Path | None = None,
    ci_runs_per_day: int = 10,
    minutes_per_rerun: int = 5,
) -> Path:
    """Run everything on a temp copy of *source_project*; return the report path."""
    work = Path(tempfile.mkdtemp(prefix="flakehunter_web_"))
    project = work / "project"
    shutil.copytree(source_project, project, ignore=_IGNORE)

    try:
        # -- Phase 1: detection ------------------------------------------------
        emit("phase", {"phase": "detect", "label": f"Running the test suite {runs} times"})
        counts: dict[str, dict[str, int]] = {}
        clean_runs = 0
        ok_runs = 0
        for i in range(runs):
            outcomes, skipped = _single_run(project)
            if skipped:
                emit("detect_run", {"i": i + 1, "n": runs, "all_passed": None})
                continue
            ok_runs += 1
            failed = [nid for nid, passed in outcomes.items() if not passed]
            for nid, passed in outcomes.items():
                entry = counts.setdefault(nid, {"pass": 0, "fail": 0})
                entry["pass" if passed else "fail"] += 1
            if not failed:
                clean_runs += 1
            emit("detect_run", {
                "i": i + 1, "n": runs, "all_passed": not failed,
                "tests": len(outcomes), "failed": failed,
            })

        before_pass_rate = clean_runs / ok_runs if ok_runs else 0.0
        flaky = [
            FlakySummary(nid, v["pass"], v["fail"], ok_runs)
            for nid, v in counts.items()
            if v["pass"] > 0 and v["fail"] > 0
        ]
        flaky.sort(key=lambda s: s.fail_count, reverse=True)

        # -- Phase 2: hints ----------------------------------------------------
        hints_map = scan([s.node_id for s in flaky], project)
        emit("detect_done", {
            "total_tests": len(counts),
            "pass_rate": before_pass_rate,
            "clean_runs": clean_runs,
            "runs": ok_runs,
            "flaky": [
                {"node_id": s.node_id, "pass": s.pass_count, "fail": s.fail_count,
                 "runs": s.total_runs, "hints": hints_map.get(s.node_id, [])}
                for s in flaky
            ],
        })

        if not flaky:
            emit("phase", {"phase": "report", "label": "No flaky tests found"})
            return _report([], hints_map, project, run_dir, flaky, before_pass_rate,
                           0, before_pass_rate, ok_runs, ci_runs_per_day, minutes_per_rerun)

        # -- Phase 3: Bob diagnoses in parallel ----------------------------------
        label = ("Replaying IBM Bob's recorded fixes" if replay_path
                 else f"IBM Bob is diagnosing {len(flaky)} tests in parallel")
        emit("phase", {"phase": "diagnose", "label": label})
        for s in flaky:
            emit("bob_start", {"node_id": s.node_id})

        def on_bob(node_id: str, _status: str, r) -> None:
            if replay_path:
                time.sleep(0.8)  # pace the replay so each card is readable
            emit("bob_done", {
                "node_id": node_id, "root_cause": r.root_cause,
                "explanation": r.explanation, "diff": r.diff_text or "",
            })
        results = diagnose(
            flaky, hints_map, project,
            workers=workers, max_cost=max_cost,
            output_dir=run_dir, replay_path=replay_path,
            progress_cb=on_bob,
        )

        # -- Phase 4: apply + verify ---------------------------------------------
        emit("phase", {"phase": "verify",
                       "label": f"Applying fixes and re-running each test file {verify_runs} times"})

        def on_verify(node_id: str, done: int, passes: int, total: int) -> None:
            emit("verify_progress", {"node_id": node_id, "done": done, "passes": passes, "total": total})

        patch_results = patch_and_verify(
            results, project, verify_runs=verify_runs, workers=workers, progress_cb=on_verify,
        )
        for pr in patch_results:
            emit("verify_done", {"node_id": pr.node_id, "status": pr.status,
                                 "passes": pr.verify_passes, "runs": pr.verify_runs})

        # -- Phase 5: after-run ----------------------------------------------------
        emit("phase", {"phase": "after", "label": f"Re-running the whole suite {runs} times"})
        after_clean = 0
        after_ok = 0
        after_counts: dict[str, dict[str, int]] = {}
        for i in range(runs):
            outcomes, skipped = _single_run(project)
            if skipped:
                continue
            after_ok += 1
            failed = [nid for nid, passed in outcomes.items() if not passed]
            for nid, passed in outcomes.items():
                entry = after_counts.setdefault(nid, {"pass": 0, "fail": 0})
                entry["pass" if passed else "fail"] += 1
            if not failed:
                after_clean += 1
            emit("after_run", {"i": i + 1, "n": runs, "all_passed": not failed})
        after_pass_rate = after_clean / after_ok if after_ok else 0.0
        after_flaky = sum(1 for v in after_counts.values() if v["pass"] and v["fail"])

        # -- Phase 6: report ----------------------------------------------------------
        emit("phase", {"phase": "report", "label": "Writing the report"})
        report = _report(patch_results, hints_map, project, run_dir, flaky, before_pass_rate,
                         after_flaky, after_pass_rate, ok_runs, ci_runs_per_day, minutes_per_rerun)
        fixed = sum(1 for pr in patch_results if pr.status == "fixed")
        emit("summary", {
            "before_flaky": len(flaky), "after_flaky": after_flaky,
            "before_pass_rate": before_pass_rate, "after_pass_rate": after_pass_rate,
            "fixed": fixed,
        })
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _report(patch_results, hints_map, project, run_dir, flaky, before_pass_rate,
            after_flaky, after_pass_rate, runs, ci_runs_per_day, minutes_per_rerun) -> Path:
    return write_report(
        patch_results=patch_results,
        hints_map=hints_map,
        project_path=project,
        output_dir=run_dir,
        flaky_summaries=flaky,
        before_flaky=len(flaky),
        before_pass_rate=before_pass_rate,
        after_flaky=after_flaky,
        after_pass_rate=after_pass_rate,
        detection_runs=runs,
        ci_runs_per_day=ci_runs_per_day,
        minutes_per_rerun=minutes_per_rerun,
    )
