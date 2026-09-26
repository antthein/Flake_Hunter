"""
flakehunter/runner.py — Phase 1: re-run detection.

Runs the test suite N times with pytest-json-report, collects per-test
pass/fail counts, and flags any test that both passes and fails as flaky.
Also computes the full-suite pass rate (fraction of runs where every test passed).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FlakySummary:
    node_id: str       # relative to project root, e.g. "demo_app/tests/test_promotions.py::test_promo_winner_is_eligible"
    pass_count: int
    fail_count: int
    total_runs: int

    @property
    def pass_rate(self) -> str:
        return f"{self.pass_count}/{self.total_runs}"

    @property
    def fail_rate(self) -> str:
        return f"{self.fail_count}/{self.total_runs}"


def run_suite(
    project_path: Path | str,
    runs: int = 20,
    *,
    verbose: bool = False,
) -> tuple[list[FlakySummary], float]:
    """
    Run the test suite ``runs`` times and return flaky tests + suite pass rate.

    Returns
    -------
    flaky : list[FlakySummary]
        Tests that flipped between pass and fail across the runs.
    suite_pass_rate : float
        Fraction of runs in which every single test passed (0.0 – 1.0).
    """
    project_path = Path(project_path).resolve()

    # per-test outcome counters  {node_id: {"pass": int, "fail": int}}
    counts: dict[str, dict[str, int]] = {}
    clean_runs = 0  # runs where every test passed

    for i in range(runs):
        if verbose:
            print(f"  run {i + 1}/{runs} ...", end="\r", flush=True)

        outcomes = _single_run(project_path)
        if not outcomes:
            # pytest produced no parseable report (collection error etc.) — skip
            continue

        run_all_passed = True
        for node_id, passed in outcomes.items():
            entry = counts.setdefault(node_id, {"pass": 0, "fail": 0})
            if passed:
                entry["pass"] += 1
            else:
                entry["fail"] += 1
                run_all_passed = False

        if run_all_passed:
            clean_runs += 1

    if verbose:
        print()  # newline after \r progress

    suite_pass_rate = clean_runs / runs if runs > 0 else 0.0

    flaky: list[FlakySummary] = [
        FlakySummary(
            node_id=nid,
            pass_count=v["pass"],
            fail_count=v["fail"],
            total_runs=runs,
        )
        for nid, v in counts.items()
        if v["pass"] > 0 and v["fail"] > 0
    ]

    return flaky, suite_pass_rate


def _single_run(project_path: Path) -> dict[str, bool]:
    """
    Run pytest once and return {node_id: passed} for every collected test.
    node_ids are made relative to project_path.
    Returns an empty dict if the report cannot be parsed.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w"
    ) as tmp:
        report_path = tmp.name

    cmd = [
        sys.executable, "-m", "pytest",
        str(project_path),
        "--tb=no",
        "-q",
        "--json-report",
        f"--json-report-file={report_path}",
    ]

    subprocess.run(
        cmd,
        capture_output=True,
        cwd=project_path,
    )

    try:
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    finally:
        Path(report_path).unlink(missing_ok=True)

    outcomes: dict[str, bool] = {}
    for test in report.get("tests", []):
        raw_id: str = test.get("nodeid", "")
        # Make node_id relative to project_path
        rel_id = _make_relative(raw_id, project_path)
        when = test.get("outcome", "")
        outcomes[rel_id] = (when == "passed")

    return outcomes


def _make_relative(node_id: str, project_path: Path) -> str:
    """
    Strip the absolute project prefix from a node_id if present.

    pytest may emit either absolute paths or paths relative to cwd.
    We want paths relative to project_path for portability.
    """
    # node_id format: "path/to/file.py::test_name[param]"
    sep = "::"
    if sep in node_id:
        file_part, rest = node_id.split(sep, 1)
    else:
        file_part, rest = node_id, ""

    try:
        rel = Path(file_part).resolve().relative_to(project_path)
        file_part = rel.as_posix()
    except ValueError:
        # already relative or unresolvable — use as-is
        file_part = Path(file_part).as_posix()

    return f"{file_part}{sep}{rest}" if rest else file_part


# ---------------------------------------------------------------------------
# Quick CLI for manual testing:  python -m flakehunter.runner <path> [--runs N]
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="FlakeHunter — re-run detection")
    parser.add_argument("project_path", help="Path to the project under test")
    parser.add_argument("--runs", type=int, default=20, help="Number of re-runs (default: 20)")
    args = parser.parse_args()

    print(f"Running suite {args.runs} times against: {args.project_path}")
    flaky, suite_pass_rate = run_suite(args.project_path, runs=args.runs, verbose=True)

    print(f"\nSuite pass rate (all tests green): {suite_pass_rate:.0%} ({int(suite_pass_rate * args.runs)}/{args.runs} runs)\n")

    if not flaky:
        print("No flaky tests detected.")
        return

    # Print table
    col_w = max(len(s.node_id) for s in flaky)
    header = f"{'TEST':<{col_w}}  {'PASS':>6}  {'FAIL':>6}"
    print(header)
    print("-" * len(header))
    for s in sorted(flaky, key=lambda x: x.fail_count, reverse=True):
        print(f"{s.node_id:<{col_w}}  {s.pass_rate:>6}  {s.fail_rate:>6}")


if __name__ == "__main__":
    _main()
