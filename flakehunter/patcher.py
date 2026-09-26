"""
flakehunter/patcher.py -- Phase 4: apply, verify 50x, revert or keep.

For each DiagnosisResult that has new_contents:
  1. Back up original files in memory.
  2. Write new_contents into the real project.
  3. Run the test's whole file --verify-runs times (default 50) with
     pytest-randomly active; track the specific node_id's outcome from the
     JSON report.
  4. All passes -> status "fixed".  Any failure -> restore backups -> "suggestion_only".
  5. Write errors -> restore -> "patch_failed".
  6. No new_contents -> "no_patch".

Verification runs are parallelised across different test files via
ThreadPoolExecutor (each test file is independent).

After all patches, re-run the full suite (same --runs count) to capture
after_pass_rate and after_flaky_count for the report.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from flakehunter.diagnoser import DiagnosisResult
from flakehunter.runner import FlakySummary, run_suite, _make_relative


@dataclass
class PatchResult:
    node_id: str
    status: str          # "fixed" | "suggestion_only" | "patch_failed" | "no_patch" | "dry-run"
    verify_passes: int   # number of verification runs that passed
    verify_runs: int     # total verification runs attempted
    root_cause: str
    explanation: str
    diff_text: str | None
    diff_path: Path | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def patch_and_verify(
    diagnosis_results: list[DiagnosisResult],
    project_path: Path | str,
    *,
    verify_runs: int = 50,
    workers: int = 4,
) -> list[PatchResult]:
    """
    Apply each diagnosis patch, verify with pytest, revert on failure.

    Returns a PatchResult per DiagnosisResult, verified in parallel where
    each test file is independent.
    """
    project_path = Path(project_path).resolve()

    futures: dict = {}
    results: list[PatchResult] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for dr in diagnosis_results:
            future = pool.submit(
                _patch_one,
                dr=dr,
                project_path=project_path,
                verify_runs=verify_runs,
            )
            futures[future] = dr.node_id

        for future in as_completed(futures):
            node_id = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = PatchResult(
                    node_id=node_id,
                    status="patch_failed",
                    verify_passes=0,
                    verify_runs=0,
                    root_cause="error",
                    explanation=f"Unexpected error in patcher: {exc}",
                    diff_text=None,
                )
            results.append(result)

    return results


def run_after_suite(
    project_path: Path | str,
    runs: int,
) -> tuple[float, int]:
    """
    Re-run the full suite to measure post-patch quality.

    Returns (after_pass_rate, after_flaky_count).
    """
    project_path = Path(project_path).resolve()
    flaky_after, after_pass_rate = run_suite(project_path, runs=runs)
    return after_pass_rate, len(flaky_after)


# ---------------------------------------------------------------------------
# Per-test worker
# ---------------------------------------------------------------------------

def _patch_one(
    dr: DiagnosisResult,
    project_path: Path,
    verify_runs: int,
) -> PatchResult:
    """Apply one patch, verify, revert if needed. Thread-safe (different files)."""

    # -- No patch available --------------------------------------------------
    if dr.root_cause in ("dry-run",) or not dr.new_contents:
        return PatchResult(
            node_id=dr.node_id,
            status="no_patch" if dr.root_cause != "dry-run" else "dry-run",
            verify_passes=0,
            verify_runs=0,
            root_cause=dr.root_cause,
            explanation=dr.explanation,
            diff_text=dr.diff_text,
            diff_path=dr.diff_path,
        )

    # -- Back up originals (byte-identical, preserves line endings) ----------
    backups: dict[str, bytes] = {}
    for rel_path in dr.new_contents:
        original = project_path / rel_path
        if original.is_file():
            backups[rel_path] = original.read_bytes()

    # -- Apply patch ---------------------------------------------------------
    try:
        for rel_path, new_text in dr.new_contents.items():
            dest = project_path / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(new_text.encode("utf-8"))
    except OSError as exc:
        _restore(backups, project_path)
        return PatchResult(
            node_id=dr.node_id,
            status="patch_failed",
            verify_passes=0,
            verify_runs=0,
            root_cause=dr.root_cause,
            explanation=f"Failed to write patch: {exc}",
            diff_text=dr.diff_text,
            diff_path=dr.diff_path,
        )

    # -- Verify: run whole test file N times ---------------------------------
    test_file_rel = dr.node_id.split("::")[0]
    test_file_abs = project_path / test_file_rel

    passes = 0
    runs_done = 0

    for _ in range(verify_runs):
        outcome = _run_one_verify(test_file_abs, dr.node_id, project_path)
        runs_done += 1
        if outcome is True:
            passes += 1
        elif outcome is False:
            break   # fail fast on first failure

    all_passed = (passes == verify_runs)

    if all_passed:
        return PatchResult(
            node_id=dr.node_id,
            status="fixed",
            verify_passes=passes,
            verify_runs=runs_done,
            root_cause=dr.root_cause,
            explanation=dr.explanation,
            diff_text=dr.diff_text,
            diff_path=dr.diff_path,
        )
    else:
        _restore(backups, project_path)
        return PatchResult(
            node_id=dr.node_id,
            status="suggestion_only",
            verify_passes=passes,
            verify_runs=runs_done,
            root_cause=dr.root_cause,
            explanation=dr.explanation,
            diff_text=dr.diff_text,
            diff_path=dr.diff_path,
        )


# ---------------------------------------------------------------------------
# Single verification run
# ---------------------------------------------------------------------------

def _run_one_verify(
    test_file_abs: Path,
    node_id: str,
    project_path: Path,
) -> bool | None:
    """
    Run the whole test file once; return True/False for node_id's outcome.
    Returns None if the report could not be parsed (treated as failure).
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tmp:
        report_path = tmp.name

    cmd = [
        sys.executable, "-m", "pytest",
        str(test_file_abs),
        "--tb=no", "-q",
        "--json-report",
        f"--json-report-file={report_path}",
    ]
    subprocess.run(
        cmd,
        capture_output=True,
        cwd=str(project_path),
    )

    try:
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    finally:
        Path(report_path).unlink(missing_ok=True)

    report_root = Path(report.get("root", str(project_path)))

    for test in report.get("tests", []):
        raw_id = test.get("nodeid", "")
        rel_id = _make_relative(raw_id, report_root, project_path)
        if rel_id == node_id:
            return test.get("outcome", "") == "passed"

    return None  # test not found in report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _restore(backups: dict[str, bytes], project_path: Path) -> None:
    """Restore original file bytes exactly as they were before patching."""
    for rel_path, content in backups.items():
        try:
            (project_path / rel_path).write_bytes(content)
        except OSError:
            pass
