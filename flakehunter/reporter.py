"""
flakehunter/reporter.py -- Phase 5: generate reports/report.html.

Single self-contained HTML file with:
  - Dark header with before/after headline numbers
  - Time-saved formula + calculation
  - One card per flaky test with status badge, verify result, collapsible diff
  - Footer: "Diagnosed and fixed by IBM Bob"
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from flakehunter.patcher import PatchResult


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_report(
    patch_results: list[PatchResult],
    hints_map: dict[str, list[str]],
    project_path: Path | str,
    output_dir: Path | str,
    *,
    before_flaky: int,
    before_pass_rate: float,
    after_flaky: int,
    after_pass_rate: float,
    detection_runs: int,
    ci_runs_per_day: int,
    minutes_per_rerun: int,
    dry_run: bool = False,
) -> Path:
    """Render the HTML report and return its path."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    fixed = [r for r in patch_results if r.status == "fixed"]
    suggestion = [r for r in patch_results if r.status == "suggestion_only"]

    # Time-saved formula
    # avg fail rate across flaky tests before patching
    if patch_results:
        avg_fail_rate = sum(
            (r.verify_runs - r.verify_passes) / r.verify_runs
            if r.verify_runs > 0 else 0.3
            for r in patch_results
        ) / len(patch_results)
    else:
        avg_fail_rate = 0.3

    ci_time_saved = (
        len(fixed) * ci_runs_per_day * avg_fail_rate * minutes_per_rerun * 22
    )
    dev_time_saved = len(fixed) * 4 + len(suggestion) * 1

    report_path = output_dir / "report.html"
    report_path.write_text(
        _render(
            patch_results=patch_results,
            hints_map=hints_map,
            project_path=Path(project_path),
            before_flaky=before_flaky,
            before_pass_rate=before_pass_rate,
            after_flaky=after_flaky,
            after_pass_rate=after_pass_rate,
            detection_runs=detection_runs,
            ci_runs_per_day=ci_runs_per_day,
            minutes_per_rerun=minutes_per_rerun,
            ci_time_saved=ci_time_saved,
            dev_time_saved=dev_time_saved,
            avg_fail_rate=avg_fail_rate,
            dry_run=dry_run,
            fixed_count=len(fixed),
            suggestion_count=len(suggestion),
        ),
        encoding="utf-8",
    )
    return report_path


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

_STATUS_BADGE = {
    "fixed":          ('<span class="badge badge-fixed">FIXED</span>', "#22c55e"),
    "suggestion_only":('<span class="badge badge-suggestion">SUGGESTION</span>', "#f59e0b"),
    "patch_failed":   ('<span class="badge badge-failed">FAILED</span>', "#ef4444"),
    "no_patch":       ('<span class="badge badge-nopatch">NO PATCH</span>', "#94a3b8"),
    "dry-run":        ('<span class="badge badge-nopatch">DRY RUN</span>', "#94a3b8"),
    "error":          ('<span class="badge badge-failed">ERROR</span>', "#ef4444"),
}


def _render(
    patch_results: list[PatchResult],
    hints_map: dict[str, list[str]],
    project_path: Path,
    before_flaky: int,
    before_pass_rate: float,
    after_flaky: int,
    after_pass_rate: float,
    detection_runs: int,
    ci_runs_per_day: int,
    minutes_per_rerun: int,
    ci_time_saved: float,
    dev_time_saved: float,
    avg_fail_rate: float,
    dry_run: bool,
    fixed_count: int,
    suggestion_count: int,
) -> str:

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cards_html = "\n".join(_card(r, hints_map) for r in sorted(patch_results, key=lambda r: r.node_id))

    formula_html = f"""
        <div class="formula-box">
          <div class="formula-title">Time Saved Formula</div>
          <code>CI time saved/month = fixed_tests &times; ci_runs_per_day &times; avg_fail_rate &times; minutes_per_rerun &times; 22 working_days</code>
          <br>
          <code>= {fixed_count} &times; {ci_runs_per_day} &times; {avg_fail_rate:.0%} &times; {minutes_per_rerun} &times; 22 = <strong>{ci_time_saved:.0f} minutes ({ci_time_saved/60:.1f} hrs)</strong></code>
          <br><br>
          <code>Developer time saved = (fixed &times; 4h) + (suggestions &times; 1h) = ({fixed_count} &times; 4) + ({suggestion_count} &times; 1) = <strong>{dev_time_saved:.0f} hrs</strong></code>
        </div>"""

    dry_banner = '<div class="dry-banner">[DRY RUN] Bob diagnosis and patching were skipped.</div>' if dry_run else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlakeHunter Report</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Segoe UI", system-ui, sans-serif; font-size: 14px; line-height: 1.6; background: #f1f5f9; color: #1e293b; }}

  /* header */
  .header {{ background: #0f172a; color: #f8fafc; padding: 32px 40px 24px; }}
  .header h1 {{ font-size: 1.8rem; font-weight: 700; letter-spacing: -0.5px; margin-bottom: 4px; }}
  .header .subtitle {{ color: #94a3b8; font-size: 0.85rem; }}
  .stats-row {{ display: flex; gap: 32px; margin-top: 24px; flex-wrap: wrap; }}
  .stat-card {{ background: #1e293b; border-radius: 8px; padding: 16px 24px; min-width: 150px; }}
  .stat-label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8; }}
  .stat-val {{ font-size: 2rem; font-weight: 700; margin-top: 2px; }}
  .stat-val.green {{ color: #4ade80; }}
  .stat-val.red {{ color: #f87171; }}
  .stat-val.amber {{ color: #fbbf24; }}
  .stat-arrow {{ color: #94a3b8; font-size: 1rem; align-self: center; padding-top: 18px; }}

  /* body */
  .body {{ max-width: 960px; margin: 0 auto; padding: 32px 24px 64px; }}
  .dry-banner {{ background: #fef3c7; border: 1px solid #f59e0b; border-radius: 6px; padding: 10px 16px; margin-bottom: 20px; color: #92400e; font-weight: 500; }}
  .section-title {{ font-size: 1.1rem; font-weight: 700; color: #0f172a; margin: 32px 0 12px; }}

  /* formula */
  .formula-box {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px 20px; margin-bottom: 28px; font-size: 0.82rem; }}
  .formula-title {{ font-weight: 700; margin-bottom: 8px; color: #334155; }}
  .formula-box code {{ background: #f8fafc; border-radius: 4px; padding: 2px 6px; display: inline-block; margin: 2px 0; color: #0f172a; }}

  /* cards */
  .card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; margin-bottom: 16px; overflow: hidden; }}
  .card-header {{ display: flex; align-items: center; gap: 12px; padding: 14px 20px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; flex-wrap: wrap; }}
  .card-title {{ font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.82rem; font-weight: 600; color: #1e293b; flex: 1; min-width: 0; word-break: break-all; }}
  .card-body {{ padding: 16px 20px; }}
  .meta-grid {{ display: grid; grid-template-columns: 120px 1fr; gap: 4px 12px; font-size: 0.82rem; margin-bottom: 12px; }}
  .meta-key {{ color: #64748b; font-weight: 500; }}
  .meta-val {{ color: #1e293b; }}
  .verify-bar {{ display: flex; align-items: center; gap: 8px; margin-top: 8px; }}
  .bar-track {{ flex: 1; height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 3px; }}
  .bar-fill.pass {{ background: #22c55e; }}
  .bar-fill.fail {{ background: #ef4444; }}
  .verify-label {{ font-size: 0.75rem; color: #64748b; white-space: nowrap; }}

  /* badges */
  .badge {{ display: inline-block; border-radius: 4px; padding: 2px 8px; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }}
  .badge-fixed {{ background: #dcfce7; color: #15803d; }}
  .badge-suggestion {{ background: #fef9c3; color: #a16207; }}
  .badge-failed {{ background: #fee2e2; color: #b91c1c; }}
  .badge-nopatch {{ background: #f1f5f9; color: #64748b; }}
  .hint-tag {{ display: inline-block; background: #eff6ff; color: #1d4ed8; border-radius: 4px; padding: 1px 7px; font-size: 0.72rem; margin: 2px; font-weight: 500; }}

  /* diff */
  details {{ margin-top: 12px; }}
  details summary {{ cursor: pointer; font-size: 0.8rem; font-weight: 600; color: #475569; user-select: none; }}
  details summary:hover {{ color: #0f172a; }}
  .diff-container {{ margin-top: 8px; background: #0f172a; border-radius: 6px; overflow: auto; max-height: 400px; }}
  pre.diff {{ font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.76rem; line-height: 1.5; padding: 12px 16px; white-space: pre; }}
  .diff-add {{ color: #4ade80; }}
  .diff-remove {{ color: #f87171; }}
  .diff-meta {{ color: #94a3b8; }}
  .diff-hunk {{ color: #7dd3fc; }}

  /* footer */
  .footer {{ text-align: center; padding: 24px; color: #94a3b8; font-size: 0.78rem; border-top: 1px solid #e2e8f0; margin-top: 32px; }}
</style>
</head>
<body>

<div class="header">
  <h1>FlakeHunter Report</h1>
  <div class="subtitle">Generated {now} &nbsp;|&nbsp; Project: {html.escape(str(project_path))} &nbsp;|&nbsp; Detection runs: {detection_runs}</div>
  <div class="stats-row">
    <div class="stat-card">
      <div class="stat-label">Flaky tests before</div>
      <div class="stat-val red">{before_flaky}</div>
    </div>
    <div class="stat-arrow">&rarr;</div>
    <div class="stat-card">
      <div class="stat-label">Flaky tests after</div>
      <div class="stat-val {'green' if after_flaky == 0 else 'amber'}">{after_flaky}</div>
    </div>
    <div style="width:1px;background:#334155;align-self:stretch;margin:0 8px;"></div>
    <div class="stat-card">
      <div class="stat-label">Suite pass rate before</div>
      <div class="stat-val red">{before_pass_rate:.0%}</div>
    </div>
    <div class="stat-arrow">&rarr;</div>
    <div class="stat-card">
      <div class="stat-label">Suite pass rate after</div>
      <div class="stat-val {'green' if after_pass_rate >= 0.9 else 'amber'}">{after_pass_rate:.0%}</div>
    </div>
    <div style="width:1px;background:#334155;align-self:stretch;margin:0 8px;"></div>
    <div class="stat-card">
      <div class="stat-label">CI time saved/month</div>
      <div class="stat-val green">{ci_time_saved:.0f} min</div>
    </div>
  </div>
</div>

<div class="body">
  {dry_banner}

  <div class="section-title">Time Saved Estimate</div>
  {formula_html}

  <div class="section-title">Test Results ({len(patch_results)} flaky tests found)</div>
  {cards_html}
</div>

<div class="footer">
  Diagnosed and fixed by <strong>IBM Bob</strong>
</div>

</body>
</html>"""


def _card(r: PatchResult, hints_map: dict[str, list[str]]) -> str:
    badge_html, _ = _STATUS_BADGE.get(r.status, _STATUS_BADGE["no_patch"])
    hints = hints_map.get(r.node_id, [])
    hints_html = " ".join(f'<span class="hint-tag">{html.escape(h)}</span>' for h in hints) or "<em>none</em>"

    # Short test name for display (last two path segments :: test_name)
    parts = r.node_id.rsplit("/", 1)
    short_name = parts[-1] if len(parts) > 1 else r.node_id

    # Verify bar
    if r.verify_runs > 0:
        pct = r.verify_passes / r.verify_runs
        bar_class = "pass" if pct == 1.0 else "fail"
        verify_html = f"""
        <div class="verify-bar">
          <div class="bar-track"><div class="bar-fill {bar_class}" style="width:{pct*100:.0f}%"></div></div>
          <span class="verify-label">{r.verify_passes}/{r.verify_runs} passed</span>
        </div>"""
    else:
        verify_html = ""

    # Diff block
    if r.diff_text:
        diff_lines_html = _colorize_diff(r.diff_text)
        diff_html = f"""
        <details>
          <summary>Show diff ({r.diff_path.name if r.diff_path else 'patch'})</summary>
          <div class="diff-container"><pre class="diff">{diff_lines_html}</pre></div>
        </details>"""
    else:
        diff_html = ""

    explanation_html = f'<div class="meta-key">Explanation</div><div class="meta-val">{html.escape(r.explanation)}</div>' if r.explanation else ""

    return f"""
  <div class="card">
    <div class="card-header">
      <span class="card-title" title="{html.escape(r.node_id)}">{html.escape(short_name)}</span>
      {badge_html}
    </div>
    <div class="card-body">
      <div class="meta-grid">
        <div class="meta-key">Full path</div><div class="meta-val" style="font-family:monospace;font-size:0.78rem">{html.escape(r.node_id)}</div>
        <div class="meta-key">Root cause</div><div class="meta-val">{html.escape(r.root_cause)}</div>
        {explanation_html}
        <div class="meta-key">Hints</div><div class="meta-val">{hints_html}</div>
      </div>
      {verify_html}
      {diff_html}
    </div>
  </div>"""


def _colorize_diff(diff_text: str) -> str:
    """Return HTML with colour classes for unified diff lines."""
    lines = []
    for line in diff_text.splitlines():
        escaped = html.escape(line)
        if line.startswith("+++") or line.startswith("---"):
            lines.append(f'<span class="diff-meta">{escaped}</span>')
        elif line.startswith("@@"):
            lines.append(f'<span class="diff-hunk">{escaped}</span>')
        elif line.startswith("+"):
            lines.append(f'<span class="diff-add">{escaped}</span>')
        elif line.startswith("-"):
            lines.append(f'<span class="diff-remove">{escaped}</span>')
        else:
            lines.append(escaped)
    return "\n".join(lines)
