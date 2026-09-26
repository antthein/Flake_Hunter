"""
flakehunter/diagnoser.py -- Phase 3: Bob-powered diagnosis and patch generation.

For each flaky test:
  1. Copy the project into a fresh temp workspace (parallel-safe isolation).
  2. Write the full multi-line task into FLAKEHUNTER_TASK.md in the workspace.
  3. Invoke Bob with a short single-line prompt (no newlines, no shell-unsafe
     characters) so cmd.exe on Windows does not mangle it.
  4. Bob edits only the test file, then replies with a JSON summary.
  5. We compare ALL .py files in the workspace against the originals to detect
     any out-of-scope edits, capture new_contents before cleanup, compute a
     unified diff, and save it to reports/<test>.diff.

Bob output format (from pre-flight inspection):
  {"type":"result","status":"success","stats":{...},"last_message":"<text>"}
  last_message contains the JSON summary Bob wrote.

--dry-run: skips all Bob calls; returns DiagnosisResult with diff_text=None.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from flakehunter.runner import FlakySummary

_TASK_FILE = "FLAKEHUNTER_TASK.md"
_BOB_TIMEOUT = 300  # seconds per Bob subprocess


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DiagnosisResult:
    node_id: str
    diff_text: str | None           # unified diff, or None if dry-run / failed
    root_cause: str                 # timing | random | network | shared-state | other | dry-run | error
    explanation: str                # 2-sentence human summary from Bob
    files_changed: list[str]        # relative paths Bob edited (verified)
    new_contents: dict[str, str] = field(default_factory=dict)  # rel_path -> new text (for patcher)
    diff_path: Path | None = None   # set by diagnose() after saving the diff
    bob_raw: str = field(default="", repr=False)  # raw last_message from Bob


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_replay(replay_path: Path | str) -> dict[str, DiagnosisResult]:
    """
    Load a previously recorded Bob run from a JSON file.

    Returns a dict keyed by node_id so diagnose() can look up results.
    """
    data = json.loads(Path(replay_path).read_text(encoding="utf-8"))
    results: dict[str, DiagnosisResult] = {}
    for entry in data:
        dr = DiagnosisResult(
            node_id=entry["node_id"],
            diff_text=entry.get("diff_text"),
            root_cause=entry.get("root_cause", "other"),
            explanation=entry.get("explanation", ""),
            files_changed=entry.get("files_changed", []),
            new_contents=entry.get("new_contents", {}),
        )
        results[dr.node_id] = dr
    return results


def save_record(results: list[DiagnosisResult], record_path: Path | str) -> None:
    """Serialize DiagnosisResult list to JSON for future replay."""
    record_path = Path(record_path)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "node_id": r.node_id,
            "diff_text": r.diff_text,
            "root_cause": r.root_cause,
            "explanation": r.explanation,
            "files_changed": r.files_changed,
            "new_contents": r.new_contents,
        }
        for r in results
    ]
    record_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def diagnose(
    flaky: list[FlakySummary],
    hints_map: dict[str, list[str]],
    project_path: Path | str,
    *,
    workers: int = 4,
    max_cost: float = 1.0,
    dry_run: bool = False,
    output_dir: Path | str = Path("reports"),
    record_path: Path | str | None = None,
    replay_path: Path | str | None = None,
    progress_cb=None,  # callable(node_id, status_str) for live updates
) -> list[DiagnosisResult]:
    """
    Run Bob in parallel (one subprocess per flaky test) and return results.

    Each Bob call gets its own temp workspace copy so parallel edits never
    collide.  The real project is never touched during diagnosis.

    record_path: if set, serialize results to this JSON file after running.
    replay_path: if set, skip Bob entirely and load results from this file.
    progress_cb: optional callable(node_id, status) called as each result arrives.
    """
    project_path = Path(project_path).resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results: list[DiagnosisResult] = []

    if dry_run:
        for summary in flaky:
            results.append(DiagnosisResult(
                node_id=summary.node_id,
                diff_text=None,
                root_cause="dry-run",
                explanation="",
                files_changed=[],
            ))
        return results

    # ── Replay mode: load saved Bob results, skip actual Bob calls ───────────
    if replay_path is not None:
        replay_map = load_replay(replay_path)
        for summary in flaky:
            if summary.node_id in replay_map:
                dr = replay_map[summary.node_id]
                # Recompute diff_path if diff_text exists
                results.append(dr)
            else:
                results.append(DiagnosisResult(
                    node_id=summary.node_id,
                    diff_text=None,
                    root_cause="error",
                    explanation=f"node_id not found in replay file: {summary.node_id}",
                    files_changed=[],
                ))
            if progress_cb:
                progress_cb(summary.node_id, results[-1].root_cause)
        # Save diffs from replay
        for result in results:
            if result.diff_text and not result.diff_path:
                safe_name = re.sub(r"[^\w\-.]", "_", result.node_id) + ".diff"
                diff_path = output_dir / safe_name
                diff_path.write_text(result.diff_text, encoding="utf-8")
                result.diff_path = diff_path
        return results

    bob_exe = shutil.which("bob") or shutil.which("bob.cmd")
    if bob_exe is None:
        raise RuntimeError(
            "bob executable not found on PATH. "
            "Install Bob CLI and ensure it is on PATH."
        )

    futures = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for summary in flaky:
            future = pool.submit(
                _diagnose_one,
                summary=summary,
                hints=hints_map.get(summary.node_id, []),
                project_path=project_path,
                bob_exe=bob_exe,
                max_cost=max_cost,
            )
            futures[future] = summary.node_id

        for future in as_completed(futures):
            node_id = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = DiagnosisResult(
                    node_id=node_id,
                    diff_text=None,
                    root_cause="error",
                    explanation=f"Unexpected error during diagnosis: {exc}",
                    files_changed=[],
                )
            results.append(result)
            if progress_cb:
                progress_cb(node_id, result.root_cause)

    # Save diffs and set diff_path
    for result in results:
        if result.diff_text:
            safe_name = re.sub(r"[^\w\-.]", "_", result.node_id) + ".diff"
            diff_path = output_dir / safe_name
            diff_path.write_text(result.diff_text, encoding="utf-8")
            result.diff_path = diff_path

    # Record results if requested
    if record_path is not None:
        save_record(results, record_path)

    return results


# ---------------------------------------------------------------------------
# Per-test worker
# ---------------------------------------------------------------------------

def _diagnose_one(
    summary: FlakySummary,
    hints: list[str],
    project_path: Path,
    bob_exe: str,
    max_cost: float,
) -> DiagnosisResult:
    """
    Copy project to a temp workspace, invoke Bob, collect results, clean up.
    Returns a DiagnosisResult.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="flakehunter_"))
    temp_ws = temp_dir / "workspace"

    try:
        # ── 1. Isolate workspace ─────────────────────────────────────────────
        shutil.copytree(
            project_path,
            temp_ws,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", "reports", ".venv", ".git",
            ),
            dirs_exist_ok=False,
        )

        test_file_rel = summary.node_id.split("::")[0]   # e.g. "tests/test_promotions.py"
        hints_str = ", ".join(hints) if hints else "none"

        # ── 2. Write task file into workspace ────────────────────────────────
        task_text = _build_task(summary, test_file_rel, hints_str)
        (temp_ws / _TASK_FILE).write_text(task_text, encoding="utf-8")

        # ── 3. Run Bob with a short, safe single-line prompt ─────────────────
        short_prompt = (
            f"Read {_TASK_FILE} in this workspace and follow it exactly."
        )
        cmd = [
            bob_exe, "run", short_prompt,
            "--format", "json",
            "--trust",
            "--accept-license",
            "--max-cost", str(max_cost),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(temp_ws),
                timeout=_BOB_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return DiagnosisResult(
                node_id=summary.node_id,
                diff_text=None,
                root_cause="error",
                explanation=f"Bob timed out after {_BOB_TIMEOUT}s.",
                files_changed=[],
            )

        # ── 4. Parse Bob output ──────────────────────────────────────────────
        stderr_snippet = (proc.stderr or "")[:300].strip()
        root_cause, explanation, reported_files, bob_raw = _parse_bob_output(
            proc.stdout, summary.node_id
        )

        if proc.returncode != 0 and root_cause != "error":
            root_cause = "error"
            explanation = (
                f"Bob exited with code {proc.returncode}."
                + (f" stderr: {stderr_snippet}" if stderr_snippet else "")
            )

        # ── 5. Detect changed .py files by full scan of workspace ────────────
        changed_files, rogue_files = _detect_changed_files(
            project_path, temp_ws, test_file_rel
        )

        if rogue_files:
            # Bob edited files outside the test file — reject the patch
            return DiagnosisResult(
                node_id=summary.node_id,
                diff_text=None,
                root_cause="error",
                explanation=(
                    f"Bob edited files outside the test file: "
                    f"{', '.join(rogue_files)}"
                ),
                files_changed=[],
                bob_raw=bob_raw,
            )

        # ── 6. Capture new_contents before temp dir is deleted ───────────────
        new_contents: dict[str, str] = {}
        for rel in changed_files:
            edited = temp_ws / rel
            if edited.is_file():
                new_contents[rel] = edited.read_text(encoding="utf-8", errors="replace")

        # ── 7. Compute unified diff ──────────────────────────────────────────
        diff_text = _compute_diff(changed_files, project_path, temp_ws)

        return DiagnosisResult(
            node_id=summary.node_id,
            diff_text=diff_text,
            root_cause=root_cause,
            explanation=explanation,
            files_changed=changed_files,
            new_contents=new_contents,
            bob_raw=bob_raw,
        )

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Task file builder (replaces the inline prompt passed on the command line)
# ---------------------------------------------------------------------------

def _build_task(
    summary: FlakySummary,
    test_file_rel: str,
    hints_str: str,
) -> str:
    """Return the full task instructions written to FLAKEHUNTER_TASK.md."""
    return (
        "# FlakeHunter Task\n\n"
        "You are fixing a flaky pytest test. Work entirely inside this workspace.\n"
        "You MUST only edit the test file listed below.\n"
        "Do NOT modify app.py or any other file outside the test file.\n\n"
        f"**Test node:** {summary.node_id}\n"
        f"**Test file:** {test_file_rel}\n"
        f"**Hints:** {hints_str}\n"
        f"**Pass rate:** {summary.pass_count}/{summary.total_runs} runs\n\n"
        "Read the test file and the app source it imports (for context only).\n"
        "Fix the test so it becomes deterministic by editing ONLY the test file.\n"
        "Use monkeypatch, fixtures, or setup_function as needed.\n"
        "Do not add new dependencies.\n\n"
        "After editing, respond with ONLY valid JSON (no markdown fences, no extra text):\n"
        '{"root_cause": "<timing|random|network|shared-state|other>", '
        '"explanation": "<exactly 2 sentences describing root cause and fix>", '
        f'"files_changed": ["{test_file_rel}"]}}\n'
    )


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def _detect_changed_files(
    project_path: Path,
    temp_ws: Path,
    test_file_rel: str,
) -> tuple[list[str], list[str]]:
    """
    Compare every .py file in temp_ws against the original project.

    Returns:
      changed_files : list of rel paths that differ (should be just the test file)
      rogue_files   : changed files that are NOT the test file (out-of-scope edits)
    """
    changed: list[str] = []
    rogue: list[str] = []

    for edited_abs in temp_ws.rglob("*.py"):
        # Build the relative path (posix) from temp_ws root
        try:
            rel = edited_abs.relative_to(temp_ws).as_posix()
        except ValueError:
            continue

        original = project_path / rel
        if not original.is_file():
            # New file Bob created — treat as rogue
            rogue.append(rel)
            continue

        orig_text = original.read_text(encoding="utf-8", errors="replace")
        edit_text = edited_abs.read_text(encoding="utf-8", errors="replace")

        if orig_text != edit_text:
            changed.append(rel)
            if rel != test_file_rel:
                rogue.append(rel)

    return changed, rogue


# ---------------------------------------------------------------------------
# Bob output parser
# ---------------------------------------------------------------------------

def _parse_bob_output(
    stdout: str,
    node_id: str,
) -> tuple[str, str, list[str], str]:
    """
    Parse Bob's --format json stdout.

    Returns (root_cause, explanation, files_changed, raw_last_message).

    Bob output shape (from pre-flight):
      {"type":"result","status":"success","stats":{...},"last_message":"<text>"}

    last_message contains the JSON summary Bob was asked to produce.
    """
    try:
        envelope = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return "error", f"Bob returned non-JSON output: {stdout[:200]}", [], stdout

    if envelope.get("status") != "success":
        msg = envelope.get("last_message", "") or str(envelope)
        return "error", f"Bob returned status={envelope.get('status')!r}: {msg[:200]}", [], stdout

    last_message: str = envelope.get("last_message", "")
    inner_str = _strip_fences(last_message)

    try:
        payload = json.loads(inner_str)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"\{.*\}", inner_str, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group())
            except json.JSONDecodeError:
                payload = {}
        else:
            payload = {}

    root_cause = str(payload.get("root_cause", "other")).strip()
    explanation = str(payload.get("explanation", last_message[:300])).strip()
    files_changed = payload.get("files_changed", [])
    if not isinstance(files_changed, list):
        files_changed = []
    files_changed = [Path(f).as_posix() for f in files_changed]

    return root_cause, explanation, files_changed, last_message


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers if present."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

def _compute_diff(
    changed_files: list[str],
    project_path: Path,
    temp_ws: Path,
) -> str | None:
    """
    Compute a unified diff for each changed file.
    Returns the concatenated diff string, or None if nothing changed.
    """
    all_diffs: list[str] = []
    for rel in changed_files:
        original = project_path / rel
        edited   = temp_ws / rel

        if not original.is_file() or not edited.is_file():
            continue

        orig_lines = original.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        edit_lines = edited.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

        diff = list(difflib.unified_diff(
            orig_lines,
            edit_lines,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        ))
        if diff:
            all_diffs.extend(diff)

    return "".join(all_diffs) if all_diffs else None
