"""
core/contracts.py — Protected Domain Core (T0.4)
=================================================
The stable, versioned data contracts that flow between every pipeline phase.

ARCHITECTURE RULE (§2.5 / §2.9): this module is the domain core. It imports
ONLY the Python standard library and is depended upon by everything above it.
It must never import a sibling component (mutant_generator, sandbox_executor,
strategies, llm, infra, ...). The import-graph CI test enforces this.

GOVERNING RULE: additive contracts only. Fields are never removed or renamed.
New status values and new OPTIONAL fields may be added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

# ── Type aliases for the contracts shared across phases ──────────────────────
# A single test case: {"function": str, "inputs": dict, ["source_strategy": str]}
TestRecord = dict[str, Any]

# kill_matrix[test_index][mutant_index] -> bool (test i kills mutant j)
KillMatrix = list[list[bool]]

# orig_results[test_index] -> (status, serialized_output_or_None)
#   status in {"OK","TIMEOUT","CRASH","NO_FUNC","SERIAL","UNKNOWN_FUNC"}
OrigResult = Tuple[str, Optional[str]]
OrigResults = list[OrigResult]

# ── Mutant status values (additive) ─────────────────────────────────────────
# Existing consumers compare against the string literals below. EQUIVALENT is
# an ADDITIVE value introduced for I4; code that only checks ALIVE/KILLED/
# SUSPECTED continues to work (an EQUIVALENT mutant is simply "not killed").
STATUS_ALIVE = "ALIVE"
STATUS_KILLED = "KILLED"
STATUS_SUSPECTED = "SUSPECTED"
STATUS_EQUIVALENT = "EQUIVALENT"  # ← additive (I4); reserved, not yet produced

VALID_STATUSES = frozenset(
    {STATUS_ALIVE, STATUS_KILLED, STATUS_SUSPECTED, STATUS_EQUIVALENT}
)


@dataclass
class MutantRecord:
    """
    Canonical mutant representation passed between all phases.

    Moved here from core/mutant_generator.py in T0.4 and re-exported there so
    every existing `from core.mutant_generator import MutantRecord` keeps
    working unchanged.

    `status` transitions ALIVE -> KILLED | SUSPECTED | EQUIVALENT during
    phases 3-5. `EQUIVALENT` is additive (I4) and currently unused by the
    pipeline; existing literal comparisons remain valid.
    """
    id:                  str
    category:            str           # ARITH | REL | LOGICAL | INTEGRATION
    operator_name:       str           # e.g. "Gt_to_Lt"
    integration_op:      Optional[str] # IPVR | IUOI | IORC | ISMA | IMCD | None
    function_name:       str
    line:                int
    original_line_text:  str
    mutated_node_text:   str
    original_op:         str
    mutated_op:          str
    code:                str           # full mutant source (string)
    status:              str  = STATUS_ALIVE
    tried:               list = field(default_factory=list)
    # N1.5: optional project-qualified attribution. Default empty so single-file
    # runs are byte-identical (these stay "" until populated in N1.6). When set,
    # they make the mutant identity project-stable / collision-proof across files.
    qualified_name:      str  = ""      # e.g. "pkg.mod:Class.method"
    module:              str  = ""      # e.g. "pkg.mod"

    def to_dict(self) -> dict:
        d = {
            "id":                  self.id,
            "category":            self.category,
            "operator_name":       self.operator_name,
            "integration_op":      self.integration_op,
            "function_name":       self.function_name,
            "line":                self.line,
            "original_line_text":  self.original_line_text,
            "mutated_node_text":   self.mutated_node_text,
            "original_op":         self.original_op,
            "mutated_op":          self.mutated_op,
            "code":                self.code,
            "status":              self.status,
            "tried":               self.tried,
        }
        # Emitted only when populated -> single-file report bytes unchanged.
        if self.qualified_name:
            d["qualified_name"] = self.qualified_name
        if self.module:
            d["module"] = self.module
        return d

    def __repr__(self) -> str:
        return (
            f"Mutant({self.id} | {self.operator_name} | "
            f"line {self.line} | {self.function_name} | {self.status})"
        )