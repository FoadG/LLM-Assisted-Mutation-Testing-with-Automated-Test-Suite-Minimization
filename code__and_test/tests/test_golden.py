"""
tests/test_golden.py (T0.3)
============================
Regression guard: a default --no-llm run must reproduce the captured V1
golden outputs field-for-field, ignoring volatile fields (timestamps) and
tolerating ADDITIVE fields (keys present now but absent in golden).

The golden fixtures were captured from V1 on target_code.py:
  killed=45/48, adjusted=100.0, min_test_count=4.

Fast path: always validates the golden fixtures' invariants and the
normalizer. Full comparison runs against output/*.json if a fresh run is
present (CI runs the pipeline, then this test). A 260s pipeline run is NOT
triggered from within pytest.
"""
from __future__ import annotations

import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "tests", "golden")
OUTPUT = os.path.join(ROOT, "output")

VOLATILE = {"generated_at"}


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _strip_volatile(obj):
    """Recursively drop volatile keys (timestamps)."""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [_strip_volatile(x) for x in obj]
    return obj


def assert_matches_golden(actual: dict, golden: dict):
    """
    Every key in GOLDEN must be present in ACTUAL with an equal (volatile-
    stripped) value. ACTUAL may contain ADDITIVE keys not in golden — those
    are ignored (governing rule: additive contracts only).
    """
    g = _strip_volatile(golden)
    a = _strip_volatile(actual)

    def cmp(gv, av, path):
        if isinstance(gv, dict):
            assert isinstance(av, dict), f"{path}: type mismatch"
            for k, sub in gv.items():
                assert k in av, f"{path}.{k} missing in actual (non-additive removal!)"
                cmp(sub, av[k], f"{path}.{k}")
        elif isinstance(gv, list):
            assert isinstance(av, list) and len(av) == len(gv), f"{path}: list mismatch"
            for i, (gi, ai) in enumerate(zip(gv, av)):
                cmp(gi, ai, f"{path}[{i}]")
        else:
            assert gv == av, f"{path}: {gv!r} != {av!r}"

    cmp(g, a, "<root>")


def test_golden_fixtures_present_and_valid():
    report = _load(os.path.join(GOLDEN, "mutation_report_v1.json"))
    suite = _load(os.path.join(GOLDEN, "minimum_test_suite_v1.json"))
    s = report["summary"]
    assert s["killed"] == 45 and s["total_mutants"] == 48
    assert s["adjusted_score"] == 100.0
    assert s["min_test_count"] == 4
    assert suite["test_count"] == 4
    baseline = open(os.path.join(GOLDEN, "baseline_runtime_seconds.txt")).read().strip()
    assert float(baseline) > 0


def test_normalizer_tolerates_additive_and_timestamp():
    golden = {"summary": {"killed": 45}, "generated_at": "X"}
    actual = {"summary": {"killed": 45, "n_equivalent": 0},  # additive field
              "generated_at": "Y", "manifest": {"run_id": "z"}}  # additive + volatile
    assert_matches_golden(actual, golden)  # must not raise


@pytest.mark.skipif(
    not os.path.exists(os.path.join(OUTPUT, "mutation_report.json")),
    reason="no fresh run in output/ — run `python main.py --no-llm` first",
)
def test_current_run_matches_golden():
    assert_matches_golden(
        _load(os.path.join(OUTPUT, "mutation_report.json")),
        _load(os.path.join(GOLDEN, "mutation_report_v1.json")),
    )
    assert_matches_golden(
        _load(os.path.join(OUTPUT, "minimum_test_suite.json")),
        _load(os.path.join(GOLDEN, "minimum_test_suite_v1.json")),
    )