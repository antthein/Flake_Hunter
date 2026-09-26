"""
flakehunter/cli.py -- entry point for the FlakeHunter CLI.

Commands
--------
  flakehunter run <project_path>   detect, diagnose, fix, and report flaky tests
  flakehunter reset-demo           restore demo_app from demo_originals/
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from flakehunter.diagnoser import diagnose
from flakehunter.patcher import patch_and_verify, run_after_suite
from flakehunter.reporter import write_report
from flakehunter.runner import FlakySummary, _single_run
from flakehunter.scanner import scan


# ── colour helpers ───────────────────────────────────────────────────────────
def _red(s: str) -> str:    return click.style(s, fg="red",    bold=True)
def _yellow(s: str) -> str: return click.style(s, fg="yellow", bold=True)
def _green(s: str) -> str:  return click.style(s, fg="green",  bold=True)
def _cyan(s: str) -> str:   return click.style(s, fg="cyan")
def _dim(s: str) -> str:    return click.style(s, dim=True)
def _bold(s: str) -> str:   return click.style(s, bold=True)


# ── main group ───────────────────────────────────────────────────────────────

@click.group()
def main() -> None:
    """FlakeHunter -- find, diagnose, and fix flaky pytest tests."""


# ── run ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.option("--runs",              default=20,    show_default=True, help="Detection re-run passes.")
@click.option("--workers",           default=4,     show_default=True, help="Parallel Bob subprocesses.")
@click.option("--max-cost",          default=1.0,   show_default=True, help="Per-Bob-call cost cap ($).")
@click.option("--dry-run",           is_flag=True,  help="Skip Bob calls; still produce a report.")
@click.option("--output",            default="reports", show_default=True, type=click.Path(), help="Report output directory.")
@click.option("--verify-runs",       default=50,    show_default=True, help="Pytest passes for patch verification.")
@click.option("--ci-runs-per-day",   default=10,    show_default=True, help="CI runs/day (time-saved formula).")
@click.option("--minutes-per-rerun", default=5,     show_default=True, help="Minutes per CI re-run (time-saved formula).")
@click.option("--record",            default=None,  type=click.Path(), help="Save Bob results to this JSON file for later replay.")
@click.option("--replay-bob",        default=None,  type=click.Path(exists=True), help="Skip Bob; load results from this JSON file.")
def run(
    project_path: str,
    runs: int,
    workers: int,
    max_cost: float,
    dry_run: bool,
    output: str,
    verify_runs: int,
    ci_runs_per_day: int,
    minutes_per_rerun: int,
    record: str | None,
    replay_bob: str | None,
) -> None:
    """Detect, diagnose, and fix flaky tests in PROJECT_PATH."""

    project = Path(project_path).resolve()
    output_dir = Path(output)

    click.echo()
    click.echo(_bold("===  FlakeHunter  ==="))
    click.echo(f"  Project : {_cyan(str(project))}")
    click.echo(f"  Runs    : {runs}   Workers: {workers}   Max-cost: ${max_cost}   Verify: {verify_runs}x")
    if dry_run:
        click.echo("  " + _yellow("[!] --dry-run  (Bob calls will be skipped)"))
    click.echo()

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 1: Re-run detection
    # ─────────────────────────────────────────────────────────────────────────
    click.echo(_bold("Phase 1 - Re-run detection"))

    counts: dict[str, dict[str, int]] = {}
    clean_runs = 0
    successful_runs = 0

    with click.progressbar(
        length=runs,
        label="  Running suite",
        bar_template="  %(label)s  %(bar)s  %(info)s",
        fill_char=click.style("#", fg="cyan"),
        empty_char=click.style(".", dim=True),
        width=40,
    ) as bar:
        for i in range(runs):
            outcomes, skipped = _single_run(project)
            bar.update(1)
            if skipped:
                click.echo(
                    f"\n  {_yellow('[!]')} Run {i + 1} produced no report, skipping.",
                    err=True,
                )
                continue

            successful_runs += 1
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

    before_pass_rate = clean_runs / successful_runs if successful_runs > 0 else 0.0
    flaky: list[FlakySummary] = [
        FlakySummary(
            node_id=nid,
            pass_count=v["pass"],
            fail_count=v["fail"],
            total_runs=successful_runs,
        )
        for nid, v in counts.items()
        if v["pass"] > 0 and v["fail"] > 0
    ]

    clean_count = round(before_pass_rate * successful_runs)
    click.echo(
        f"\n  Suite pass rate (all green): "
        f"{_green(f'{before_pass_rate:.0%}')}  "
        f"{_dim(f'({clean_count}/{successful_runs} runs)')}"
    )

    if not flaky:
        click.echo("\n" + _green("[OK]  No flaky tests detected."))
        # Still produce an empty report
        _write_empty_report(output_dir, project, before_pass_rate, runs, ci_runs_per_day, minutes_per_rerun)
        return

    click.echo(f"  Flaky tests found: {_red(str(len(flaky)))}\n")

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 2: Static hint scan
    # ─────────────────────────────────────────────────────────────────────────
    click.echo(_bold("Phase 2 - Static hint scan"))
    hints_map = scan([s.node_id for s in flaky], project)
    click.echo(f"  Scanned {len(flaky)} test file(s).")

    # Print detection table
    click.echo()
    col_w = max(len(s.node_id) for s in flaky)
    click.echo(_dim(f"  {'TEST':<{col_w}}  {'PASS':>6}  {'FAIL':>6}  HINTS"))
    click.echo(_dim("  " + "-" * (col_w + 30)))
    for s in sorted(flaky, key=lambda x: x.fail_count, reverse=True):
        hints_str = ", ".join(hints_map.get(s.node_id, [])) or "(none)"
        click.echo(
            f"  {s.node_id:<{col_w}}  "
            f"{_green(f'{s.pass_count}/{s.total_runs}'):>15}  "
            f"{_red(f'{s.fail_count}/{s.total_runs}'):>14}  "
            f"{_yellow(hints_str)}"
        )
    click.echo()

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 3: Diagnosis (Bob)
    # ─────────────────────────────────────────────────────────────────────────
    click.echo(_bold("Phase 3 - Diagnosis & patch generation"))

    if dry_run:
        click.echo(_yellow("  [!] --dry-run: skipping Bob calls.\n"))

    if replay_bob:
        click.echo(_cyan(f"  [replay] loading Bob results from: {replay_bob}\n"))
    diagnosis_results = diagnose(
        flaky, hints_map, project,
        workers=workers,
        max_cost=max_cost,
        dry_run=dry_run,
        output_dir=output_dir,
        record_path=record,
        replay_path=replay_bob,
    )

    for r in sorted(diagnosis_results, key=lambda x: x.node_id):
        if r.root_cause in ("dry-run",):
            status_str = _dim("[dry-run]")
        elif r.root_cause == "error":
            status_str = _red("[error]")
        else:
            status_str = _cyan(f"[{r.root_cause}]")
        diff_info = _dim("no diff") if not r.diff_text else _green(f"diff -> {r.diff_path.name if r.diff_path else 'patch'}")
        click.echo(f"  {r.node_id}")
        click.echo(f"    root cause  : {status_str}")
        if r.explanation:
            click.echo(f"    explanation : {r.explanation[:120]}")
        click.echo(f"    diff        : {diff_info}")
        click.echo()

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 4: Patch & verify
    # ─────────────────────────────────────────────────────────────────────────
    click.echo(_bold("Phase 4 - Patch & verify"))

    if dry_run:
        click.echo(_yellow("  [!] --dry-run: skipping patch/verify.\n"))
        patch_results = patch_and_verify(
            diagnosis_results, project,
            verify_runs=0,
            workers=workers,
        )
        after_pass_rate = before_pass_rate
        after_flaky_count = len(flaky)
    else:
        click.echo(f"  Verifying patches ({verify_runs} runs each, whole-file, random order) ...\n")
        patch_results = patch_and_verify(
            diagnosis_results, project,
            verify_runs=verify_runs,
            workers=workers,
        )

        # Print per-test result
        for pr in sorted(patch_results, key=lambda x: x.node_id):
            icon = {
                "fixed":           _green("[FIXED]"),
                "suggestion_only": _yellow("[SUGGESTION]"),
                "patch_failed":    _red("[PATCH FAILED]"),
                "no_patch":        _dim("[NO PATCH]"),
                "dry-run":         _dim("[DRY RUN]"),
            }.get(pr.status, _dim(f"[{pr.status}]"))
            verify_str = f"{pr.verify_passes}/{pr.verify_runs}" if pr.verify_runs > 0 else "n/a"
            click.echo(f"  {pr.node_id}")
            click.echo(f"    {icon}  verify: {verify_str}")
            click.echo()

        # After suite
        click.echo(_bold("Phase 5 - After-patch suite run"))
        click.echo(f"  Re-running suite ({runs} passes) to measure improvement ...\n")
        after_pass_rate, after_flaky_count = run_after_suite(project, runs=runs)

        fixed_count = sum(1 for pr in patch_results if pr.status == "fixed")
        click.echo(
            f"  Suite pass rate after : {_green(f'{after_pass_rate:.0%}')}  "
            f"(was {before_pass_rate:.0%})"
        )
        click.echo(
            f"  Flaky tests remaining : "
            f"{_green(str(after_flaky_count)) if after_flaky_count == 0 else _yellow(str(after_flaky_count))}  "
            f"(was {len(flaky)})"
        )
        click.echo(f"  Fixed : {_green(str(fixed_count))}\n")

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 6: Report
    # ─────────────────────────────────────────────────────────────────────────
    click.echo(_bold("Phase 6 - Report"))
    report_path = write_report(
        patch_results=patch_results,
        hints_map=hints_map,
        project_path=project,
        output_dir=output_dir,
        flaky_summaries=flaky,
        before_flaky=len(flaky),
        before_pass_rate=before_pass_rate,
        after_flaky=after_flaky_count,
        after_pass_rate=after_pass_rate,
        detection_runs=successful_runs,
        ci_runs_per_day=ci_runs_per_day,
        minutes_per_rerun=minutes_per_rerun,
        dry_run=dry_run,
    )
    click.echo(f"  {_green('[OK]')} Report written to: {_cyan(str(report_path))}\n")


def _write_empty_report(
    output_dir: Path,
    project: Path,
    pass_rate: float,
    runs: int,
    ci_runs_per_day: int,
    minutes_per_rerun: int,
) -> None:
    """Write a minimal report when no flaky tests were found."""
    write_report(
        patch_results=[],
        hints_map={},
        project_path=project,
        output_dir=output_dir,
        flaky_summaries=[],
        before_flaky=0,
        before_pass_rate=pass_rate,
        after_flaky=0,
        after_pass_rate=pass_rate,
        detection_runs=runs,
        ci_runs_per_day=ci_runs_per_day,
        minutes_per_rerun=minutes_per_rerun,
        dry_run=False,
    )


# ── reset-demo ───────────────────────────────────────────────────────────────

@main.command("reset-demo")
def reset_demo() -> None:
    """Restore demo_app/ to its original flaky state from demo_originals/."""

    here = Path(__file__).resolve().parent
    repo_root = here.parent
    originals = repo_root / "demo_originals"
    demo_app  = repo_root / "demo_app"

    if not originals.is_dir():
        raise click.ClickException(
            f"demo_originals/ not found at {originals}. "
            "Are you running from the FlakeHunter repo?"
        )

    src_app = originals / "app.py"
    if src_app.is_file():
        shutil.copy2(src_app, demo_app / "app.py")

    src_tests = originals / "tests"
    dst_tests = demo_app / "tests"
    dst_tests.mkdir(exist_ok=True)
    for src_file in src_tests.glob("*.py"):
        shutil.copy2(src_file, dst_tests / src_file.name)

    click.echo(_green("[OK]  Demo reset to original flaky state."))


if __name__ == "__main__":
    main()
