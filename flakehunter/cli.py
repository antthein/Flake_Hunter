"""
flakehunter/cli.py -- entry point for the FlakeHunter CLI.

Commands
--------
  flakehunter run <project_path>   detect, diagnose, and fix flaky tests
  flakehunter reset-demo           restore demo_app from demo_originals/
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from flakehunter.diagnoser import diagnose
from flakehunter.runner import run_suite
from flakehunter.scanner import scan


# ── colour palette ──────────────────────────────────────────────────────────
def _red(s: str) -> str:    return click.style(s, fg="red",    bold=True)
def _yellow(s: str) -> str: return click.style(s, fg="yellow", bold=True)
def _green(s: str) -> str:  return click.style(s, fg="green",  bold=True)
def _cyan(s: str) -> str:   return click.style(s, fg="cyan")
def _dim(s: str) -> str:    return click.style(s, dim=True)
def _bold(s: str) -> str:   return click.style(s, bold=True)


# ── main group ───────────────────────────────────────────────────────────────

@click.group()
def main() -> None:
    """FlakeHunter — find, diagnose, and fix flaky pytest tests."""


# ── run ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.option("--runs",             default=20,    show_default=True, help="Detection re-run passes.")
@click.option("--workers",          default=4,     show_default=True, help="Parallel Bob subprocesses.")
@click.option("--max-cost",         default=1.0,   show_default=True, help="Per-Bob-call cost cap ($).")
@click.option("--dry-run",          is_flag=True,  help="Skip Bob calls; report hints only.")
@click.option("--output",           default="reports", show_default=True, type=click.Path(), help="Report output directory.")
@click.option("--ci-runs-per-day",  default=10,    show_default=True, help="CI runs/day (time-saved formula).")
@click.option("--minutes-per-rerun",default=5,     show_default=True, help="Minutes per re-run (time-saved formula).")
def run(
    project_path: str,
    runs: int,
    workers: int,
    max_cost: float,
    dry_run: bool,
    output: str,
    ci_runs_per_day: int,
    minutes_per_rerun: int,
) -> None:
    """Detect flaky tests in PROJECT_PATH and (eventually) fix them."""

    project = Path(project_path).resolve()
    click.echo()
    click.echo(_bold("===  FlakeHunter  ==="))
    click.echo(f"  Project : {_cyan(str(project))}")
    click.echo(f"  Runs    : {runs}   Workers: {workers}   Max-cost: ${max_cost}")
    if dry_run:
        click.echo("  " + _yellow("[!] --dry-run  (Bob calls will be skipped)"))
    click.echo()

    # ── Phase 1: detect ──────────────────────────────────────────────────────
    click.echo(_bold("Phase 1 - Re-run detection"))

    with click.progressbar(
        length=runs,
        label="  Running suite",
        bar_template="  %(label)s  %(bar)s  %(info)s",
        fill_char=click.style("█", fg="cyan"),
        empty_char=click.style("░", dim=True),
        width=40,
    ) as bar:
        # We need per-run progress, so call _single_run ourselves via a
        # wrapper that ticks the bar after each run.
        from flakehunter.runner import _single_run, FlakySummary

        counts: dict[str, dict[str, int]] = {}
        clean_runs = 0
        successful_runs = 0

        for i in range(runs):
            outcomes, skipped = _single_run(project)
            bar.update(1)
            if skipped:
                click.echo(
                    f"\n  {_yellow('[!]')}  Run {i + 1} produced no report "
                    "(collection error?), skipping.",
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

    suite_pass_rate = clean_runs / successful_runs if successful_runs > 0 else 0.0
    flaky = [
        FlakySummary(
            node_id=nid,
            pass_count=v["pass"],
            fail_count=v["fail"],
            total_runs=successful_runs,
        )
        for nid, v in counts.items()
        if v["pass"] > 0 and v["fail"] > 0
    ]

    clean_count = round(suite_pass_rate * successful_runs)
    click.echo(
        f"\n  Suite pass rate (all green): "
        f"{_green(f'{suite_pass_rate:.0%}')}  "
        f"{_dim(f'({clean_count}/{successful_runs} runs)')}"
    )

    if not flaky:
        click.echo("\n" + _green("[OK]  No flaky tests detected."))
        return

    click.echo(
        f"  Flaky tests found: {_red(str(len(flaky)))}\n"
    )

    # ── Phase 2: hints ───────────────────────────────────────────────────────
    click.echo(_bold("Phase 2 - Static hint scan"))
    hints_map = scan([s.node_id for s in flaky], project)
    click.echo(f"  Scanned {len(flaky)} test file(s).\n")

    # ── Summary table ────────────────────────────────────────────────────────
    click.echo(_bold("Flaky tests detected"))
    click.echo()

    # Column widths
    col_test  = max(len(s.node_id) for s in flaky)
    col_pass  = 6
    col_fail  = 6
    col_hints = 30

    header = (
        f"  {'TEST':<{col_test}}  {'PASS':>{col_pass}}  {'FAIL':>{col_fail}}  HINTS"
    )
    click.echo(_dim(header))
    click.echo(_dim("  " + "-" * (col_test + col_pass + col_fail + col_hints + 8)))

    for s in sorted(flaky, key=lambda x: x.fail_count, reverse=True):
        hints = ", ".join(hints_map.get(s.node_id, [])) or _dim("(none)")
        fail_str = _red(f"{s.fail_count}/{s.total_runs}")
        pass_str = _green(f"{s.pass_count}/{s.total_runs}")
        click.echo(
            f"  {s.node_id:<{col_test}}  {pass_str:>{col_pass+9}}  "
            f"{fail_str:>{col_fail+9}}  {_yellow(hints) if hints != _dim('(none)') else hints}"
        )

    # ── Phase 3: diagnose ────────────────────────────────────────────────────
    click.echo()
    click.echo(_bold("Phase 3 - Diagnosis & patch generation"))

    if dry_run:
        click.echo(_yellow("  [!] --dry-run: skipping Bob calls.\n"))
        diagnosis_results = diagnose(
            flaky, hints_map, project,
            workers=workers, max_cost=max_cost,
            dry_run=True, output_dir=output,
        )
    else:
        click.echo(
            f"  Spawning up to {workers} parallel Bob subprocess(es) "
            f"(max ${max_cost} each) ...\n"
        )
        diagnosis_results = diagnose(
            flaky, hints_map, project,
            workers=workers, max_cost=max_cost,
            dry_run=False, output_dir=output,
        )

    # Print per-test diagnosis summary
    for r in sorted(diagnosis_results, key=lambda x: x.node_id):
        status_str = {
            "dry-run": _dim("[dry-run]"),
            "error":   _red("[error]"),
        }.get(r.root_cause, _cyan(f"[{r.root_cause}]"))

        diff_info = _dim("no diff") if not r.diff_text else _green(f"diff saved -> {r.diff_path.name}")
        click.echo(f"  {r.node_id}")
        click.echo(f"    root cause : {status_str}")
        if r.explanation:
            click.echo(f"    explanation: {r.explanation}")
        click.echo(f"    diff       : {diff_info}")
        click.echo()

    click.echo(_dim("-" * 60))
    click.echo(
        _yellow("  [i]  Patch / verify / report not implemented yet.")
    )
    click.echo(_dim("-" * 60))
    click.echo()


# ── reset-demo ───────────────────────────────────────────────────────────────

@main.command("reset-demo")
def reset_demo() -> None:
    """Restore demo_app/ to its original flaky state from demo_originals/."""

    # Locate demo_originals/ relative to this file's package root
    here = Path(__file__).resolve().parent       # flakehunter/
    repo_root = here.parent                       # project root
    originals = repo_root / "demo_originals"
    demo_app  = repo_root / "demo_app"

    if not originals.is_dir():
        raise click.ClickException(
            f"demo_originals/ not found at {originals}. "
            "Are you running from the FlakeHunter repo?"
        )

    # Restore app.py
    src_app = originals / "app.py"
    dst_app = demo_app / "app.py"
    if src_app.is_file():
        shutil.copy2(src_app, dst_app)

    # Restore test files
    src_tests = originals / "tests"
    dst_tests = demo_app / "tests"
    dst_tests.mkdir(exist_ok=True)
    for src_file in src_tests.glob("*.py"):
        shutil.copy2(src_file, dst_tests / src_file.name)

    click.echo(_green("[OK]  Demo reset to original flaky state."))


if __name__ == "__main__":
    main()
