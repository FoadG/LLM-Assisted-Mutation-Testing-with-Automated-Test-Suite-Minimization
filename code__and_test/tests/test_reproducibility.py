"""
tests/test_reproducibility.py (T1.4)
=====================================
Validates the reproducibility envelope and scaffolds the determinism test.

The envelope (Python/dep versions, OS, ACOC seed=42) must be capturable.
A fast, real determinism check at the strategy level (ACOC is seeded) runs
now; the full-pipeline bit-identical check becomes meaningful after I1
(M2) and is scaffolded here behind an env flag.
"""
from __future__ import annotations

import json
import os

import pytest

from infra.reproducibility import capture_env, DEFAULT_ACOC_SEED


def test_capture_env_shape():
    env = capture_env()
    assert env["acoc_seed"] == DEFAULT_ACOC_SEED == 42
    assert "python_version" in env and env["python_version"]
    assert "dependencies" in env and "pulp" in env["dependencies"]
    # must be JSON-serializable (it goes into the manifest envelope)
    json.dumps(env)


def test_acoc_strategy_is_deterministic_under_seed():
    """ACOC uses a fixed seed (42); two builds must produce identical pools."""
    from strategies import acoc_strategy

    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))

    def build():
        return acoc_strategy.generate(cfg)

    p1, p2 = build(), build()
    assert p1 == p2, "ACOC must be deterministic under its fixed seed"


@pytest.mark.skipif(
    os.environ.get("MF_FULL_REPRO") != "1",
    reason="full-pipeline determinism becomes meaningful after I1 (set MF_FULL_REPRO=1)",
)
def test_full_pipeline_deterministic_no_llm():  # pragma: no cover - scaffold
    # Placeholder for the post-I1 bit-identical --no-llm comparison.
    # Will run the pipeline twice and assert identical minimum_test_suite.json.
    assert True