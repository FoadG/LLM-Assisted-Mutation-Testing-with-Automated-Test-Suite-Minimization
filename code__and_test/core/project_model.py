"""
core/project_model.py — queryable project model (N1.4, V3)
==========================================================
Given a project root (or a single target file), discover Python modules, parse
each to an AST (cached, tolerant), expose a symbol table of qualified callables,
and provide the import/call graphs (delegated to core.graphs).

Built ONCE at startup and threaded read-only through the pipeline. In N1.4 it
has NO pipeline consumer yet (threading/attribution is N1.6); building it for a
single-file config yields a one-module model, so the V2 single-file path stays
behavior-neutral when this is eventually wired in.

Design fidelity: implements the frozen N1.4 design (discovery + AST cache +
symbol table + import/call graphs + qualified_name_at). The small ModuleInfo /
SymbolInfo dataclasses live here (NEW DESIGN DECISION within the approved
latitude) to keep the protected, stdlib-only core/contracts.py untouched and
avoid any import-graph-rule risk.

Qualified-name format: "dotted.module:Qualname" (colon separates module from the
in-module qualified name), matching the call-graph naming from N1.3.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core import graphs


# ──────────────────────────────────────────────────────────────────────────────
# Data shapes
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ModuleInfo:
    dotted_name: str
    path:        str
    parse_ok:    bool
    error:       str = ""


@dataclass(frozen=True)
class SymbolInfo:
    qualified_name: str            # "module:Qual"
    kind:           str            # "function" | "class" | "method"
    lineno:         int
    end_lineno:     int


_DEFAULT_EXCLUDE = ("__pycache__", "venv", ".venv", "site-packages")


# ──────────────────────────────────────────────────────────────────────────────
# Discovery helpers
# ──────────────────────────────────────────────────────────────────────────────
def _dotted_name(root: str, path: str) -> str:
    """Dotted module name for `path` relative to `root`, honoring packages."""
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(p for p in parts if p)


def _is_excluded_dir(name: str, exclude: Tuple[str, ...]) -> bool:
    return name in exclude or (name.startswith(".") and name not in (".",))


def discover_modules(root: str,
                     exclude: Tuple[str, ...] = _DEFAULT_EXCLUDE,
                     test_dir: Optional[str] = None) -> List[Tuple[str, str]]:
    """Return sorted (dotted_name, abspath) for *.py modules under `root`.

    Prunes excluded dir names, hidden dirs, and the configured test dir; does not
    follow symlinked directories out of the tree (followlinks=False)."""
    root = os.path.abspath(root)
    found: List[Tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # prune in place
        pruned = []
        for d in dirnames:
            if _is_excluded_dir(d, exclude):
                continue
            if test_dir and os.path.abspath(os.path.join(dirpath, d)) == os.path.abspath(os.path.join(root, test_dir)):
                continue
            pruned.append(d)
        dirnames[:] = pruned
        for f in filenames:
            if f.endswith(".py"):
                abspath = os.path.join(dirpath, f)
                found.append((_dotted_name(root, abspath), abspath))
    return sorted(found)


# ──────────────────────────────────────────────────────────────────────────────
# Symbol extraction (qualified)
# ──────────────────────────────────────────────────────────────────────────────
def _extract_symbols(module: str, tree: ast.AST) -> List[SymbolInfo]:
    syms: List[SymbolInfo] = []

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                q = f"{module}:" + ".".join(stack + [child.name])
                syms.append(SymbolInfo(q, "class", child.lineno,
                                       getattr(child, "end_lineno", child.lineno)))
                visit(child, stack + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if stack else "function"
                q = f"{module}:" + ".".join(stack + [child.name])
                syms.append(SymbolInfo(q, kind, child.lineno,
                                       getattr(child, "end_lineno", child.lineno)))
                visit(child, stack + [child.name])
            else:
                visit(child, stack)

    visit(tree, [])
    return syms


# ──────────────────────────────────────────────────────────────────────────────
# ProjectModel
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class ProjectModel:
    root:        str
    module_infos: List[ModuleInfo] = field(default_factory=list)
    _asts:       Dict[str, ast.AST] = field(default_factory=dict)
    _symbols:    Dict[str, List[SymbolInfo]] = field(default_factory=dict)
    _import_graph: Optional[graphs.ImportGraph] = None
    _call_graph:   Optional[graphs.CallGraph] = None

    # ── queries ──────────────────────────────────────────────────────────────
    def modules(self) -> List[str]:
        """Dotted names of successfully-parsed modules (sorted)."""
        return sorted(self._asts.keys())

    def all_module_infos(self) -> List[ModuleInfo]:
        return sorted(self.module_infos, key=lambda m: m.dotted_name)

    def module_for_path(self, path: str) -> Optional[ModuleInfo]:
        ap = os.path.abspath(path)
        for mi in self.module_infos:
            if os.path.abspath(mi.path) == ap:
                return mi
        return None

    def ast_for(self, module: str) -> Optional[ast.AST]:
        return self._asts.get(module)

    def symbols(self, module: str) -> List[SymbolInfo]:
        return list(self._symbols.get(module, []))

    def qualified_name_at(self, module: str, lineno: int) -> str:
        """Innermost qualified def enclosing `lineno`; 'module:<module>' if none.

        The qualified analogue of mutant_generator._line_to_func."""
        best: Optional[SymbolInfo] = None
        for s in self._symbols.get(module, []):
            if s.kind == "class":
                continue  # attribute mutants belong to functions/methods
            if s.lineno <= lineno <= s.end_lineno:
                if best is None or s.lineno > best.lineno:
                    best = s
        return best.qualified_name if best else f"{module}:<module>"

    def import_graph(self) -> graphs.ImportGraph:
        if self._import_graph is None:
            self._import_graph = graphs.build_import_graph(self._asts)
        return self._import_graph

    def call_graph(self) -> graphs.CallGraph:
        if self._call_graph is None:
            self._call_graph = graphs.build_call_graph(self._asts, self.import_graph())
        return self._call_graph


# ──────────────────────────────────────────────────────────────────────────────
# Construction
# ──────────────────────────────────────────────────────────────────────────────
def _parse_tolerant(path: str) -> Tuple[Optional[ast.AST], str]:
    try:
        with open(path, encoding="utf-8") as f:
            return ast.parse(f.read()), ""
    except (SyntaxError, ValueError, OSError, UnicodeDecodeError) as e:
        return None, f"{type(e).__name__}: {e}"


def _build_from_pairs(root: str, pairs: List[Tuple[str, str]]) -> ProjectModel:
    pm = ProjectModel(root=root)
    for dotted, path in pairs:
        tree, err = _parse_tolerant(path)
        ok = tree is not None
        pm.module_infos.append(ModuleInfo(dotted, path, ok, err))
        if ok:
            pm._asts[dotted] = tree
            pm._symbols[dotted] = _extract_symbols(dotted, tree)
    return pm


def build_project_model(config: dict) -> ProjectModel:
    """Build the model from config.

    - project.root present → discover modules under the root (honoring N1.1's
      project.discovery knobs).
    - else (single-file) → a ONE-module model from project.target_file, so the
      V2 path is reproduced exactly (dotted name = the file's basename stem).
    """
    project = (config or {}).get("project", {}) or {}
    root = project.get("root")

    if root:
        disc = project.get("discovery", {}) or {}
        exclude = tuple(disc.get("exclude", list(_DEFAULT_EXCLUDE)))
        test_dir = disc.get("test_dir")
        pairs = discover_modules(root, exclude=exclude, test_dir=test_dir)
        return _build_from_pairs(os.path.abspath(root), pairs)

    # single-file mode
    target_file = project.get("target_file", "target_code.py")
    dotted = os.path.splitext(os.path.basename(target_file))[0]
    base_root = os.path.abspath(os.path.dirname(target_file) or ".")
    return _build_from_pairs(base_root, [(dotted, os.path.abspath(target_file))])
