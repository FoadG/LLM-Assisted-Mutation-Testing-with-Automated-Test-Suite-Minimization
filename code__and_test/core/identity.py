"""
core/identity.py — Stable Identity Layer (I3 substrate, V3 prerequisite M4.3)
=============================================================================
Derives STABLE, CONTENT-BASED identities for functions and mutants, to be used
later by incremental execution / partial cache reuse (V3: I3 keys, N10).

SAFETY (critical): this module is PURE and PASSIVE.
  - It computes identities ON DEMAND from existing MutantRecord fields.
  - It does NOT modify MutantRecord, mutation generation, ordering, the
    sequential `id`, the kill matrix, scoring, or any pipeline behavior.
  - It enables NO reuse. A wrong cache hit causes silent wrong scores, so M4.3
    deliberately only *creates* identities; nothing consumes them yet.

Design rationale:
  - The existing `MutantRecord.id` (e.g. "M0001") is POSITIONAL — it shifts when
    mutants are added/removed/reordered, so it is unsafe as a reuse key.
  - A reuse key must be POSITION-INDEPENDENT and change iff the mutation's
    meaning changes. We therefore hash the semantically-relevant fields
    (function, category, operators, mutated node text) plus the full mutant
    `code`. We deliberately EXCLUDE `id` (positional) and absolute `line`
    (shifts on unrelated edits) from the mutant identity.
  - Function identity hashes a normalized function source when available; a
    name-only fallback is clearly marked weak (documented), never silently used
    for reuse decisions.

Versioned so a future change to the scheme invalidates old keys cleanly.
"""

from __future__ import annotations

import ast
import hashlib
from typing import Any, Optional

# Bump this if the identity scheme ever changes — keys from a different version
# must never be treated as matching (prevents silent stale reuse across schemes).
IDENTITY_SCHEME_VERSION = 1

_SEP = "\x1f"  # unit separator; unambiguous field delimiter for hashing


def _sha(parts: list[str]) -> str:
    h = hashlib.sha256()
    h.update(str(IDENTITY_SCHEME_VERSION).encode("utf-8"))
    for p in parts:
        h.update(_SEP.encode("utf-8"))
        h.update(p.encode("utf-8"))
    return h.hexdigest()


def _normalize_source(src: str) -> str:
    """Best-effort canonicalization of source for a stable hash: parse → unparse
    (drops comments/formatting). Falls back to the raw string on parse failure.
    Used only for identity; never executed."""
    try:
        return ast.unparse(ast.parse(src))
    except (SyntaxError, ValueError, TypeError):
        return src


def function_identity(function_name: str, func_source: Optional[str] = None) -> str:
    """Stable identity for a function.

    If `func_source` is provided, the identity reflects the function's
    normalized source (edit-sensitive, reuse-safe). If omitted, a NAME-ONLY
    weak identity is returned — acceptable for grouping/labelling but NOT a
    sound reuse key on its own (documented; callers must not reuse on it)."""
    if func_source is None:
        return _sha(["fn-nameonly", function_name])
    return _sha(["fn", function_name, _normalize_source(func_source)])


def mutant_identity(mutant: Any) -> str:
    """Stable, position-independent identity for a mutant.

    Hashes the semantically-relevant fields + the full mutant code. Independent
    of the positional `id` and of other mutants' presence/order, so it is stable
    across reordering and unrelated edits — the property a reuse key needs.

    Pure: reads attributes, returns a hex digest, no side effects."""
    parts = [
        "mut",
        str(getattr(mutant, "function_name", "")),
        str(getattr(mutant, "category", "")),
        str(getattr(mutant, "operator_name", "")),
        str(getattr(mutant, "integration_op", "")),
        str(getattr(mutant, "original_op", "")),
        str(getattr(mutant, "mutated_op", "")),
        str(getattr(mutant, "mutated_node_text", "")),
        str(getattr(mutant, "code", "")),
    ]
    # N1.5 (neutral option): fold in the project-qualified attribution ONLY when
    # present. For single-file runs these are empty, so `parts` — and therefore
    # the hash — is byte-identical to the pre-N1.5 scheme (golden-safe). When
    # populated (N1.6 / N2), identities become project-stable and collision-proof
    # across modules. No IDENTITY_SCHEME_VERSION bump is needed precisely because
    # the empty case is unchanged.
    qn = str(getattr(mutant, "qualified_name", "") or "")
    mod = str(getattr(mutant, "module", "") or "")
    if qn:
        parts.append("qn:" + qn)
    if mod:
        parts.append("mod:" + mod)
    return _sha(parts)


def mutant_test_identity(mutant: Any, test_input_json: str) -> str:
    """Composite identity for one (mutant, test-input) execution cell.

    This is the granularity a future per-cell cache (I3) / incremental run (N10)
    would key on. PROVIDED ONLY; not consumed in M4 (no reuse is enabled)."""
    return _sha(["cell", mutant_identity(mutant), test_input_json])
