"""
flakehunter/scanner.py — Phase 2: static hint scanner.

For each flaky test, AST-walks its source file (and any local modules it
imports from the same project) to attach advisory hint labels.

Hint labels
-----------
timing       — time.sleep call or ``import time``
random       — random.* call or ``import random``
network      — network-related import, TimeoutError/ConnectionError raised or
               caught, or a called function whose name suggests network I/O
               (contains: api, fetch, client, request, http, url, socket)
shared-state — module-level name bound to a list or dict literal

Hints are advisory only.  They are never used to gate or skip detection.
"""

from __future__ import annotations

import ast
from pathlib import Path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan(
    flaky_node_ids: list[str],
    project_path: Path | str,
) -> dict[str, list[str]]:
    """
    Return ``{node_id: [hint, ...]}`` for every node_id in *flaky_node_ids*.

    Scans the test file and any local (same-project) modules it imports.
    """
    project_path = Path(project_path).resolve()
    result: dict[str, list[str]] = {}

    for node_id in flaky_node_ids:
        test_file = _resolve_file(node_id, project_path)
        if test_file is None:
            result[node_id] = []
            continue

        hints: set[str] = set()

        # Scan the test file itself
        _scan_file(test_file, hints)

        # Also scan local modules the test imports
        for mod_path in _local_imports(test_file, project_path):
            _scan_file(mod_path, hints)

        result[node_id] = sorted(hints)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_NETWORK_NAMES = {"api", "fetch", "client", "request", "http", "url", "socket"}
_NETWORK_IMPORTS = {"urllib", "requests", "httpx", "socket", "http", "aiohttp", "httplib2"}
_NETWORK_ERRORS = {"TimeoutError", "ConnectionError", "ConnectionResetError",
                   "ConnectionRefusedError", "OSError"}


def _resolve_file(node_id: str, project_path: Path) -> Path | None:
    """Turn ``tests/foo.py::test_bar`` into an absolute Path, or None."""
    file_part = node_id.split("::")[0]
    candidate = (project_path / file_part).resolve()
    return candidate if candidate.is_file() else None


def _scan_file(path: Path, hints: set[str]) -> None:
    """Walk the AST of *path* and add matching hint labels to *hints*."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return

    for node in ast.walk(tree):

        # ── imports ──────────────────────────────────────────────────────────
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [node.module or ""] if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
            )
            for name in names:
                root = (name or "").split(".")[0]
                if root == "time":
                    hints.add("timing")
                if root == "random":
                    hints.add("random")
                if root in _NETWORK_IMPORTS:
                    hints.add("network")

        # ── function calls ────────────────────────────────────────────────────
        elif isinstance(node, ast.Call):
            func_name = _call_name(node)
            if func_name:
                # time.sleep(...)
                if func_name in ("time.sleep", "sleep"):
                    hints.add("timing")
                # random.*
                if func_name.startswith("random.") or func_name == "random":
                    hints.add("random")
                # network-ish function names
                lower = func_name.lower()
                if any(kw in lower for kw in _NETWORK_NAMES):
                    hints.add("network")

        # ── raise / except: TimeoutError, ConnectionError … ──────────────────
        elif isinstance(node, ast.Raise):
            if node.exc is not None:
                exc_name = _exc_name(node.exc)
                if exc_name in _NETWORK_ERRORS:
                    hints.add("network")

        elif isinstance(node, ast.ExceptHandler):
            if node.type is not None:
                exc_name = _exc_name(node.type)
                if exc_name in _NETWORK_ERRORS:
                    hints.add("network")

        # ── module-level mutable assignments (list / dict literal) ───────────
        elif isinstance(node, ast.Assign):
            # Only flag top-level assignments (depth 1 in the module body)
            if _is_module_level(node, tree):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(
                        node.value, (ast.List, ast.Dict)
                    ):
                        hints.add("shared-state")


def _local_imports(test_file: Path, project_path: Path) -> list[Path]:
    """
    Return paths of local (same-project) modules imported by *test_file*.

    Only resolves simple ``import foo`` / ``from foo import bar`` where
    ``foo.py`` exists in the same directory or project root.
    """
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return []

    candidates: list[Path] = []
    search_dirs = [test_file.parent, project_path]

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (
                node.module
                if isinstance(node, ast.ImportFrom)
                else next((a.name for a in node.names), None)
            )
            if not mod:
                continue
            root_mod = mod.split(".")[0]
            for d in search_dirs:
                candidate = (d / f"{root_mod}.py").resolve()
                if candidate.is_file() and candidate != test_file:
                    candidates.append(candidate)
                    break

    return candidates


def _call_name(node: ast.Call) -> str | None:
    """Extract a dotted name string from a Call node's func, e.g. 'time.sleep'."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        parts: list[str] = []
        cur: ast.expr = node.func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _exc_name(node: ast.expr) -> str:
    """Extract the bare exception class name from a Name or Attribute node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_module_level(node: ast.AST, tree: ast.Module) -> bool:
    """Return True if *node* is a direct child of the module body."""
    return node in tree.body


# ---------------------------------------------------------------------------
# Quick CLI:  python -m flakehunter.scanner <project_path> [--runs N]
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    from flakehunter.runner import run_suite

    parser = argparse.ArgumentParser(description="FlakeHunter — static hint scanner")
    parser.add_argument("project_path", help="Path to the project under test")
    parser.add_argument("--runs", type=int, default=20, help="Detection runs (default: 20)")
    args = parser.parse_args()

    print(f"Detecting flaky tests ({args.runs} runs) …")
    flaky, suite_pass_rate = run_suite(args.project_path, runs=args.runs, verbose=True)

    if not flaky:
        print("No flaky tests detected.")
        return

    hints_map = scan([s.node_id for s in flaky], args.project_path)

    col_w = max(len(s.node_id) for s in flaky)
    print(f"\n{'TEST':<{col_w}}  {'PASS':>6}  {'FAIL':>6}  HINTS")
    print("-" * (col_w + 30))
    for s in sorted(flaky, key=lambda x: x.fail_count, reverse=True):
        hints = ", ".join(hints_map.get(s.node_id, [])) or "(none)"
        print(f"{s.node_id:<{col_w}}  {s.pass_rate:>6}  {s.fail_rate:>6}  {hints}")


if __name__ == "__main__":
    _main()
