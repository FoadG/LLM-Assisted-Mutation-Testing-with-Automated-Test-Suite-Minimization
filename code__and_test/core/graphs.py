"""
core/graphs.py — project graphs (N1, V3)
========================================
Pure, side-effect-free static analysis over module ASTs. Stdlib + `ast` only;
no third-party graph dependency.

N1.2 (this milestone) implements the IMPORT-DEPENDENCY GRAPH only:
  module → the in-project modules it imports (plus a record of external and
  unresolved imports). The best-effort static CALL GRAPH is added additively in
  N1.3; this module is structured so that addition touches nothing here.

CONSUMPTION: nothing in the pipeline imports this module yet. The N1.4
ProjectModel will call `build_import_graph` with the discovered {dotted_name →
AST} mapping. Keeping the builder a pure function of its inputs makes it fully
testable in isolation now, before discovery exists.

DETERMINISM: every collection returned is sorted, so repeated builds over the
same inputs produce byte-identical structures (a property later persistence /
incremental modes rely on).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ImportEdge:
    """One import occurrence in a module.

    importer:  dotted name of the module containing the import.
    target:    absolute dotted name of the imported module (best-effort for
               relative imports), or the raw external name when not in project.
    names:     the symbols pulled in (for `from x import a, b` → ("a","b");
               for `import x.y` → () since the bound name is the module itself).
    level:     relative-import level (0 = absolute, 1 = '.', 2 = '..', ...).
    resolved:  True iff `target` resolves to an in-project module.
    """
    importer: str
    target:   str
    names:    Tuple[str, ...]
    level:    int
    resolved: bool


@dataclass
class ImportGraph:
    """Adjacency view over ImportEdges. All accessors return sorted results."""
    edges: List[ImportEdge] = field(default_factory=list)
    all_modules: Tuple[str, ...] = ()

    def modules(self) -> List[str]:
        """All in-project modules (the full discovered set), not merely the ones
        that happen to contain imports."""
        if self.all_modules:
            return sorted(self.all_modules)
        return sorted({e.importer for e in self.edges})

    def successors(self, module: str) -> List[str]:
        """In-project modules imported by `module` (resolved targets only)."""
        return sorted({e.target for e in self.edges
                       if e.importer == module and e.resolved})

    def predecessors(self, module: str) -> List[str]:
        """In-project modules that import `module`."""
        return sorted({e.importer for e in self.edges
                       if e.target == module and e.resolved})

    def in_project_targets(self) -> List[str]:
        return sorted({e.target for e in self.edges if e.resolved})

    def external_targets(self) -> List[str]:
        return sorted({e.target for e in self.edges if not e.resolved})

    def to_dict(self) -> dict:
        """Deterministic, JSON-serializable snapshot."""
        return {
            "kind": "import_graph",
            "edges": [
                {
                    "importer": e.importer,
                    "target":   e.target,
                    "names":    list(e.names),
                    "level":    e.level,
                    "resolved": e.resolved,
                }
                for e in sorted(
                    self.edges,
                    key=lambda e: (e.importer, e.target, e.level, e.names),
                )
            ],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Extraction (single module)
# ──────────────────────────────────────────────────────────────────────────────
def extract_imports(importer: str, tree: ast.AST) -> List[Tuple[str, Tuple[str, ...], int]]:
    """Return raw (target, names, level) triples for one module AST.

    For `import a.b.c` → ("a.b.c", (), 0). For `import a as x` the target is the
    real module "a", not the alias. For `from a.b import c, d` →
    ("a.b", ("c","d"), 0). For `from . import x` → ("", ("x",), 1). For
    `from .mod import y` → ("mod", ("y",), 1). Resolution to absolute names
    happens in build_import_graph, which knows the package context.
    """
    out: List[Tuple[str, Tuple[str, ...], int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, (), 0))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((module, names, int(node.level or 0)))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Relative-import resolution
# ──────────────────────────────────────────────────────────────────────────────
def _resolve_relative(importer: str, module: str, level: int) -> str:
    """Best-effort absolute dotted name for a relative import.

    `importer` is the dotted name of the importing MODULE (e.g. "pkg.sub.mod").
    Its containing package is importer without the last component. For level L,
    ascend (L-1) further packages, then append `module` (may be empty).
    Returns "" if the ascent runs past the project root (caller treats as
    unresolved)."""
    if level <= 0:
        return module
    pkg_parts = importer.split(".")[:-1]          # package of the importer module
    ascend = level - 1
    if ascend > len(pkg_parts):
        return ""                                  # past the top — unresolvable
    base = pkg_parts[:len(pkg_parts) - ascend]
    parts = base + ([module] if module else [])
    return ".".join(p for p in parts if p)


def _is_in_project(target: str, module_names: set) -> bool:
    """A target is in-project if it equals a known module, or is a prefix
    package of one, or a known module is a prefix of it (importing a submodule
    or a symbol from a package module)."""
    if not target:
        return False
    if target in module_names:
        return True
    # package import: some known module lives under `target`
    if any(m == target or m.startswith(target + ".") for m in module_names):
        return True
    # symbol-from-module: `target` is a known module's parent path already
    # handled above; also handle target being a known module's ancestor.
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Graph builder
# ──────────────────────────────────────────────────────────────────────────────
def build_import_graph(modules: Dict[str, ast.AST]) -> ImportGraph:
    """Build the import-dependency graph from a {dotted_name → AST} mapping.

    Resolution rules (best-effort, documented):
      - `import a.b.c` → edge to "a.b.c"; resolved iff in-project (exact match or
        a package prefix of a known module).
      - `from M import n1, n2` → for each name, if "M.n2" is a known module the
        edge points at that submodule ("M.n2"); otherwise, if "M" is a known
        module, a single edge points at "M" (importing symbols from module M).
        If neither is in-project, one external edge to "M" is recorded.
      - Relative imports are resolved against the importer's package first
        (see _resolve_relative); if the ascent passes the root the edge is
        recorded unresolved.
      - External (third-party / stdlib) imports are recorded resolved=False and
        never expanded.
    """
    module_names = set(modules.keys())
    edges: List[ImportEdge] = []

    def add(importer: str, target: str, names: Tuple[str, ...], level: int) -> None:
        edges.append(ImportEdge(
            importer=importer, target=target, names=tuple(names),
            level=int(level), resolved=_is_in_project(target, module_names),
        ))

    for importer in sorted(modules.keys()):
        tree = modules[importer]
        for raw_target, names, level in extract_imports(importer, tree):
            base = _resolve_relative(importer, raw_target, level) if level > 0 else raw_target

            if not names:
                # plain `import x[.y.z]` (or `from . import` with empty module +
                # names handled below). Edge to the dotted module itself.
                add(importer, base, (), level)
                continue

            # `from BASE import n1, n2 ...` — prefer concrete submodule edges.
            matched_any = False
            for n in names:
                sub = f"{base}.{n}" if base else n
                if sub in module_names:
                    add(importer, sub, (n,), level)
                    matched_any = True
            # If BASE itself is a known module, symbols are pulled from it.
            if base and base in module_names:
                add(importer, base, tuple(names), level)
                matched_any = True
            if not matched_any:
                # Neither submodule nor module known: record one edge to BASE.
                # (resolved iff BASE is a package prefix of some known module.)
                add(importer, base, tuple(names), level)

    return ImportGraph(edges=edges, all_modules=tuple(sorted(module_names)))


# ══════════════════════════════════════════════════════════════════════════════
# N1.3 — best-effort static CALL GRAPH
# ══════════════════════════════════════════════════════════════════════════════
# Soundness bound (docx L120): a sound Python call graph is impossible (dynamic
# dispatch, decorators, getattr, duck typing). This builder is HEURISTIC and
# CONSERVATIVE: it resolves a call edge only when resolution is unambiguous, and
# records everything else as `unresolved` rather than guessing. Consumers (N5)
# must treat a missing/unresolved edge as "unknown", never as "unreachable".

@dataclass(frozen=True)
class CallEdge:
    """One call site inside a function.

    caller:          qualified name of the enclosing function "module:Qual"
                     (module-level call sites use "module:<module>").
    callee_repr:     textual callee as written ("helper", "obj.method", ...).
    callee_resolved: qualified "module:Qual" of the callee when unambiguously
                     resolved in-project, else "".
    kind:            "local" | "imported" | "attribute" | "unresolved".
    """
    caller:          str
    callee_repr:     str
    callee_resolved: str
    kind:            str


@dataclass
class CallGraph:
    edges: List[CallEdge] = field(default_factory=list)
    best_effort: bool = True   # always True — see soundness bound above

    def callers(self) -> List[str]:
        return sorted({e.caller for e in self.edges})

    def callees(self, caller: str) -> List[str]:
        """Resolved in-project callees of `caller` (qualified names)."""
        return sorted({e.callee_resolved for e in self.edges
                       if e.caller == caller and e.callee_resolved})

    def resolved_edges(self) -> List[CallEdge]:
        return [e for e in self.edges if e.callee_resolved]

    def unresolved_edges(self) -> List[CallEdge]:
        return [e for e in self.edges if not e.callee_resolved]

    def to_dict(self) -> dict:
        return {
            "kind": "call_graph",
            "best_effort": True,
            "edges": [
                {
                    "caller":          e.caller,
                    "callee_repr":     e.callee_repr,
                    "callee_resolved": e.callee_resolved,
                    "kind":            e.kind,
                }
                for e in sorted(
                    self.edges,
                    key=lambda e: (e.caller, e.callee_repr, e.callee_resolved, e.kind),
                )
            ],
        }


def _qualname(module: str, stack: List[str]) -> str:
    return f"{module}:" + (".".join(stack) if stack else "<module>")


def _collect_defs(module: str, tree: ast.AST):
    """Return (local_callables, methods_index_local).

    local_callables: simple name → qualified name, for module-LEVEL functions
                     and classes (the things a bare `name()` can reference).
    methods_index_local: method simple name → list of "module:Class.method"
                         qualified names defined in this module.
    """
    local_callables: Dict[str, str] = {}
    methods: Dict[str, List[str]] = {}

    # module-level callables
    for node in tree.body if hasattr(tree, "body") else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local_callables.setdefault(node.name, _qualname(module, [node.name]))

    # methods (any class, including nested) via a stacked walk
    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, stack + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if stack:  # inside a class → it's a method
                    methods.setdefault(child.name, []).append(
                        _qualname(module, stack + [child.name]))
                visit(child, stack + [child.name])
            else:
                visit(child, stack)

    visit(tree, [])
    return local_callables, methods


def _imported_bound_names(tree: ast.AST) -> Dict[str, Tuple[str, str]]:
    """bound simple name → (target_module, original_name) for `from M import n`.

    Uses the binding name (asname if present). Plain `import a.b` is not a bare
    callable binding (calls appear as attribute chains) and is skipped here.
    """
    out: Dict[str, Tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None and (node.level or 0) == 0:
            for a in node.names:
                bound = a.asname or a.name
                out[bound] = (node.module, a.name)
    return out


def build_call_graph(modules: Dict[str, ast.AST],
                     import_graph: Optional[ImportGraph] = None) -> CallGraph:
    """Best-effort static call graph over the {dotted_name → AST} mapping.

    Resolution:
      - bare `f()`        → module-local def, else an imported `from M import f`
                            whose target module M defines f, else unresolved.
      - `x.method()`      → if exactly ONE in-project class defines `method`,
                            resolve to it; otherwise unresolved (no guessing).
      - anything else     → unresolved.
    """
    # project-wide indexes
    local_by_module: Dict[str, Dict[str, str]] = {}
    methods_global: Dict[str, List[str]] = {}
    for m, tree in modules.items():
        local, methods = _collect_defs(m, tree)
        local_by_module[m] = local
        for name, quals in methods.items():
            methods_global.setdefault(name, []).extend(quals)

    edges: List[CallEdge] = []

    for module in sorted(modules.keys()):
        tree = modules[module]
        local = local_by_module[module]
        imported = _imported_bound_names(tree)

        def on_call(caller: str, call: ast.Call) -> None:
            fn = call.func
            if isinstance(fn, ast.Name):
                name = fn.id
                if name in local:
                    edges.append(CallEdge(caller, name, local[name], "local"))
                elif name in imported:
                    tgt_mod, orig = imported[name]
                    if tgt_mod in modules and orig in local_by_module[tgt_mod]:
                        edges.append(CallEdge(caller, name,
                                              local_by_module[tgt_mod][orig], "imported"))
                    else:
                        edges.append(CallEdge(caller, name, "", "imported"))
                else:
                    edges.append(CallEdge(caller, name, "", "unresolved"))
            elif isinstance(fn, ast.Attribute):
                repr_ = _attr_repr(fn)
                cands = methods_global.get(fn.attr, [])
                if len(cands) == 1:
                    edges.append(CallEdge(caller, repr_, cands[0], "attribute"))
                else:
                    edges.append(CallEdge(caller, repr_, "", "attribute"))
            else:
                edges.append(CallEdge(caller, _safe_unparse(fn), "", "unresolved"))

        _walk_call_sites(module, tree, [], on_call)

    return CallGraph(edges=edges, best_effort=True)


def _attr_repr(attr: ast.Attribute) -> str:
    """Textual 'obj.method' for an attribute callee (best-effort, no exec)."""
    parts: List[str] = [attr.attr]
    cur = attr.value
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<callee>"


def _walk_call_sites(module: str, node: ast.AST, stack: List[str], on_call) -> None:
    """Recurse the AST, attributing each ast.Call to the nearest enclosing
    function's qualified name (module-level calls → 'module:<module>')."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _walk_call_sites(module, child, stack + [child.name], on_call)
        else:
            if isinstance(child, ast.Call):
                on_call(_qualname(module, stack), child)
            _walk_call_sites(module, child, stack, on_call)
