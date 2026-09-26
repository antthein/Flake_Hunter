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

    Scans the test file fully, then scans only the bodies of the specific
    symbols (functions/classes) imported from local modules — not the whole
    module — to avoid false hints from unrelated code (e.g. module-level
    imports in app.py that the test never exercises).
    """
    project_path = Path(project_path).resolve()
    result: dict[str, list[str]] = {}

    for node_id in flaky_node_ids:
        test_file = _resolve_file(node_id, project_path)
        if test_file is None:
            result[node_id] = []
            continue

        hints: set[str] = set()

        # Scan the test file itself (full walk)
        _scan_file(test_file, hints)

        # Scan only the imported symbol bodies from local modules
        for mod_path, symbol_names in _local_imports(test_file, project_path).items():
            _scan_symbols(mod_path, symbol_names, hints)

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
    """Walk the full AST of *path* and add matching hint labels to *hints*."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return

    for node in ast.walk(tree):
        _check_node(node, tree, hints, module_level=True)


def _check_node(
    node: ast.AST,
    tree: ast.Module,
    hints: set[str],
    *,
    module_level: bool,
) -> None:
    """Inspect a single AST node and add any matching hints."""

    # ── imports ───────────────────────────────────────────────────────────
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
            if func_name in ("time.sleep", "sleep"):
                hints.add("timing")
            if func_name.startswith("random.") or func_name == "random":
                hints.add("random")
            lower = func_name.lower()
            if any(kw in lower for kw in _NETWORK_NAMES):
                hints.add("network")

    # ── raise / except: TimeoutError, ConnectionError … ──────────────────
    elif isinstance(node, ast.Raise):
        if node.exc is not None:
            if _exc_name(node.exc) in _NETWORK_ERRORS:
                hints.add("network")

    elif isinstance(node, ast.ExceptHandler):
        if node.type is not None:
            if _exc_name(node.type) in _NETWORK_ERRORS:
                hints.add("network")

    # ── module-level mutable assignments (list / dict literal) ───────────
    elif module_level and isinstance(node, ast.Assign):
        if _is_module_level(node, tree):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(
                    node.value, (ast.List, ast.Dict)
                ):
                    hints.add("shared-state")


def _local_imports(test_file: Path, project_path: Path) -> dict[Path, set[str]]:
    """
    Return ``{module_path: {symbol_name, ...}}`` for local modules imported by
    *test_file*.

    For ``from app import add_stock, compute`` this yields
    ``{Path(".../app.py"): {"add_stock", "compute"}}``.
    For ``import app`` (bare module import) the symbol set is empty, which
    signals that the whole module body should be scanned.

    Only resolves modules whose ``.py`` file exists in the same directory as
    the test or at the project root.
    """
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return {}

    result: dict[Path, set[str]] = {}
    search_dirs = [test_file.parent, project_path]

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue

        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # collect the names being imported: `from app import foo, bar`
            symbols = {alias.name for alias in node.names if alias.name != "*"}
        else:
            mod = next((a.name for a in node.names), "")
            symbols = set()  # bare `import app` — no specific symbols

        root_mod = mod.split(".")[0]
        if not root_mod:
            continue

        for d in search_dirs:
            candidate = (d / f"{root_mod}.py").resolve()
            if candidate.is_file() and candidate != test_file:
                entry = result.setdefault(candidate, set())
                entry.update(symbols)
                break

    return result


def _scan_symbols(mod_path: Path, symbol_names: set[str], hints: set[str]) -> None:
    """
    Scan only the bodies of *symbol_names* in *mod_path*.

    If *symbol_names* is empty (bare ``import mod``), falls back to scanning
    the whole file.
    """
    if not symbol_names:
        _scan_file(mod_path, hints)
        return

    try:
        tree = ast.parse(mod_path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return

    for node in tree.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.ClassDef):
            name = node.name

        if name in symbol_names:
            # Walk only this top-level definition's subtree
            for child in ast.walk(node):
                _check_node(child, tree, hints, module_level=False)


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
    """
    Extract the bare exception class name from a node.

    Handles:
      - ``Name``           : ``TimeoutError``
      - ``Attribute``      : ``socket.timeout``
      - ``Call``           : ``TimeoutError("msg")``  ← unwrap to the func
    """
    if isinstance(node, ast.Call):
        return _exc_name(node.func)
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
