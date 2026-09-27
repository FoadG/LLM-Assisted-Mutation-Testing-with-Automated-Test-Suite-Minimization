"""
tests/test_no_prints.py (T1.2)
===============================
Asserts no print() calls remain in PIPELINE module bodies. Excluded:
  - test files, gui.py, verify.py, target_code.py
  - code inside `if __name__ == "__main__":` standalone diagnostic blocks
    (those are CLI harnesses, not pipeline execution, and intentionally
    keep print()).
Pipeline code must log via logging.getLogger(__name__).
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PIPELINE = [
    "main.py",
    "core/mutant_generator.py", "core/test_pool_builder.py", "core/sandbox_executor.py",
    "core/ilp_solver.py", "core/feedback_loop.py", "core/reporter.py",
    "utils/cache_manager.py", "utils/config_loader.py", "utils/coverage_filter.py",
    "utils/history_tracker.py", "utils/pytest_exporter.py", "utils/json_utils.py",
    "strategies/acoc_strategy.py", "strategies/domain_strategy.py", "strategies/llm_strategy.py",
    "llm/client.py",
]


def _main_guard_lineno(tree: ast.AST) -> int:
    """Line of the top-level `if __name__ == '__main__':` guard, or a big number."""
    for node in tree.body:
        if isinstance(node, ast.If):
            t = node.test
            if (isinstance(t, ast.Compare)
                    and isinstance(t.left, ast.Name) and t.left.id == "__name__"):
                return node.lineno
    return 10**9


def _pipeline_prints(relpath: str) -> list[int]:
    src = open(os.path.join(ROOT, relpath), encoding="utf-8").read()
    tree = ast.parse(src)
    guard = _main_guard_lineno(tree)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
                and node.lineno < guard):
            hits.append(node.lineno)
    return hits


def test_no_print_in_pipeline_bodies():
    offenders = {f: _pipeline_prints(f) for f in PIPELINE if _pipeline_prints(f)}
    assert not offenders, f"print() found in pipeline bodies (use logging): {offenders}"