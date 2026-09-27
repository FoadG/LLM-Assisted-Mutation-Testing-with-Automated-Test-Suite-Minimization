"""
core/mutant_generator.py  —  فاز ۱
====================================
تولید تمام Mutant‌های ممکن از source code با تجزیه AST.

اصلاحات نسخه ۲:
  ─ _is_valid_mutant: کد اصلی هم یک‌بار unparse می‌شود — مقایسه عادلانه
    بدون تفاوت‌های فرمت‌بندی و کامنت
  ─ MutantRecord: فیلد mutated_node_text اضافه شد — خط جهش‌یافته مستقیماً
    از AST گره تغییریافته استخراج می‌شود (نه با شماره خط از unparse)
  ─ do_logical از config خوانده می‌شود

طراحی کلیدی:
  ─ هر دو پاس (جمع‌آوری و جهش) از همان NodeTransformer با DFS استفاده
    می‌کنند تا شمارنده دقیقاً یکسان باشد → هیچ mismatch وجود ندارد.
  ─ deepcopy(tree) قبل از هر ویزیت → کد اصلی هرگز تغییر نمی‌کند.
  ─ اعتبارسنجی کامل: کد Mutant باید parse‌پذیر و با اصلی متفاوت باشد.
  ─ هیچ فایلی روی دیسک نمی‌نویسد (فقط در RAM).

عملگرهای استاندارد:
  ARITH   — جایگزینی عملگرهای حسابی (+، -، *، /، //، %)
  REL     — جایگزینی عملگرهای رابطه‌ای (<، >، <=، >=، ==، !=)
  LOGICAL — جایگزینی عملگرهای منطقی (and ↔ or)

Integration Operators (فصل ۹ — بخش دوم):
  IPVR — کاهش آرگومان آخر range()  :  range(n) → range(n-1)
  IUOI — نفی شرط if/while          :  cond → not cond
  IORC — جابجایی عملوندهای compare  :  a op b → b op a
  ISMA — افزایش اندیس آرایه          :  arr[i] → arr[i+1]
  IMCD — جایگزینی len() با 0        :  len(x) → 0
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional
from core.contracts import MutantRecord  # re-export (T0.4): MutantRecord lives in core/contracts.py
import logging

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# نگاشت عملگرها  (تمام عملگرهای فصل ۹)
# ══════════════════════════════════════════════════════════════════════════════

ARITH_MAP: dict[type, list[type]] = {
    ast.Add:      [ast.Sub, ast.Mult],
    ast.Sub:      [ast.Add, ast.Mult],
    ast.Mult:     [ast.Add, ast.Sub],
    ast.FloorDiv: [ast.Add, ast.Sub],
    ast.Div:      [ast.Mult, ast.Sub],
    ast.Mod:      [ast.Add, ast.Mult],
}

REL_MAP: dict[type, list[type]] = {
    ast.Lt:    [ast.LtE, ast.Gt,  ast.GtE],
    ast.Gt:    [ast.GtE, ast.Lt,  ast.LtE],
    ast.LtE:   [ast.Lt,  ast.GtE],
    ast.GtE:   [ast.Gt,  ast.LtE],
    ast.Eq:    [ast.NotEq],
    ast.NotEq: [ast.Eq],
    ast.Is:    [ast.IsNot],
    ast.IsNot: [ast.Is],
}

LOGICAL_MAP: dict[type, list[type]] = {
    ast.And: [ast.Or],
    ast.Or:  [ast.And],
}

OP_NAMES: dict[type, str] = {
    ast.Add:      "Add",
    ast.Sub:      "Sub",
    ast.Mult:     "Mult",
    ast.Div:      "Div",
    ast.FloorDiv: "FloorDiv",
    ast.Mod:      "Mod",
    ast.Lt:       "Lt",
    ast.Gt:       "Gt",
    ast.LtE:      "LtE",
    ast.GtE:      "GtE",
    ast.Eq:       "Eq",
    ast.NotEq:    "NotEq",
    ast.Is:       "Is",
    ast.IsNot:    "IsNot",
    ast.And:      "And",
    ast.Or:       "Or",
    ast.Not:      "Not",
}


# ══════════════════════════════════════════════════════════════════════════════
# MutantRecord — ساختار داده یکتای این پروژه
# ══════════════════════════════════════════════════════════════════════════════

# MutantRecord moved to core/contracts.py (T0.4) and imported above.
# It is re-exported from this module so existing imports
# `from core.mutant_generator import MutantRecord` keep working.


# ══════════════════════════════════════════════════════════════════════════════
# _MutationEngine — هسته اصلی: جمع‌آوری + جهش در یک NodeTransformer
# ══════════════════════════════════════════════════════════════════════════════

class _MutationEngine(ast.NodeTransformer):
    """
    دو حالت کار:
      mode="collect" → فقط عملگرها را شمارش و ثبت می‌کند (بدون تغییر درخت)
      mode="mutate"  → فقط عملگر target_index را تغییر می‌دهد

    هر دو حالت از همان DFS post-order traversal استفاده می‌کنند،
    پس شمارنده کاملاً یکسان است — هیچ mismatch رخ نمی‌دهد.
    """

    def __init__(
        self,
        mode:            str,
        target_index:    int   = -1,
        replacement_cls: type  = None,
        do_arith:        bool  = True,
        do_rel:          bool  = True,
        do_logical:      bool  = False,
    ) -> None:
        self._mode       = mode
        self._target     = target_index
        self._rep        = replacement_cls
        self._do_arith   = do_arith
        self._do_rel     = do_rel
        self._do_logical = do_logical

        # خروجی collect
        # (idx, category, op_type, replacements, lineno, col_offset)
        self.collected: list[tuple] = []

        # خروجی mutate
        self.mutated_line: Optional[int] = None
        self.mutated_node_text: str = ""   # ← جدید: متن گره تغییریافته

        # شمارنده مشترک
        self._counter: int = 0

    # ─── BinOp ───────────────────────────────────────────────────────────────
    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)  # فرزندان اول (DFS post-order)
        if self._do_arith and type(node.op) in ARITH_MAP:
            self._process(node, "ARITH", type(node.op), ARITH_MAP[type(node.op)])
        return node

    # ─── Compare ─────────────────────────────────────────────────────────────
    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for pos, op in enumerate(node.ops):
            if self._do_rel and type(op) in REL_MAP:
                self._process(
                    node, "REL", type(op), REL_MAP[type(op)], op_pos=pos
                )
        return node

    # ─── BoolOp ──────────────────────────────────────────────────────────────
    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if self._do_logical and type(node.op) in LOGICAL_MAP:
            self._process(node, "LOGICAL", type(node.op), LOGICAL_MAP[type(node.op)])
        return node

    # ─── منطق مشترک ──────────────────────────────────────────────────────────
    def _process(
        self,
        node:     ast.AST,
        category: str,
        op_type:  type,
        reps:     list[type],
        op_pos:   int = 0,
    ) -> None:
        idx = self._counter
        self._counter += 1

        if self._mode == "collect":
            self.collected.append((
                idx,
                category,
                op_type,
                reps,
                getattr(node, "lineno",     0),
                getattr(node, "col_offset", 0),
            ))

        elif self._mode == "mutate" and idx == self._target:
            # جهش in-place — درخت قبلاً deepcopy شده، پس امن است
            if category == "ARITH":
                node.op = self._rep()
            elif category == "REL":
                # B-Fix: عملگر دقیقاً در همان موقعیتی که جمع‌آوری شد جهش می‌یابد.
                # نسخه قبلی همیشه اولین عملگر REL را تغییر می‌داد، پس در
                # مقایسه‌های زنجیره‌ای (a < b < c) عملگرهای داخلی هرگز جهش
                # نمی‌یافتند و mutant‌ها به‌صورت تکراری حذف می‌شدند.
                if 0 <= op_pos < len(node.ops):
                    node.ops[op_pos] = self._rep()
            elif category == "LOGICAL":
                node.op = self._rep()

            ast.fix_missing_locations(node)
            self.mutated_line = getattr(node, "lineno", None)

            # ← جدید: متن گره تغییریافته را از AST استخراج می‌کنیم
            try:
                self.mutated_node_text = ast.unparse(node)
            except Exception:
                self.mutated_node_text = ""


# ══════════════════════════════════════════════════════════════════════════════
# کمک‌کننده‌های عمومی
# ══════════════════════════════════════════════════════════════════════════════

def _build_func_map(tree: ast.AST) -> dict[int, str]:
    """شماره خط شروع هر تابع → نام آن تابع."""
    result: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[node.lineno] = node.name
    return result


def _line_to_func(lineno: int, func_map: dict[int, str]) -> str:
    """نزدیک‌ترین تابع بالاتر از lineno را برمی‌گرداند."""
    best = "<module>"
    for ln, name in sorted(func_map.items()):
        if ln <= lineno:
            best = name
    return best


def _src_line(source_lines: list[str], lineno: int) -> str:
    """محتوای خط lineno را بر می‌گرداند (۱-indexed)."""
    if 1 <= lineno <= len(source_lines):
        return source_lines[lineno - 1].rstrip()
    return ""


# ── اصلاح شده: مقایسه با نسخه normalize‌شده کد اصلی ──────────────────────────
_CANONICAL_CACHE: dict[str, str] = {}


def _canonical(code: str) -> str:
    """
    کد را parse و unparse می‌کند تا فرمت‌بندی استاندارد شود.
    نتیجه کش می‌شود تا در تولید تعداد زیادی Mutant، parse مکرر نشود.
    """
    if code not in _CANONICAL_CACHE:
        try:
            _CANONICAL_CACHE[code] = ast.unparse(ast.parse(code))
        except Exception:
            _CANONICAL_CACHE[code] = code.strip()
    return _CANONICAL_CACHE[code]


def _is_valid_mutant(mut_code: str, original_canonical: str) -> bool:
    """
    کد Mutant باید parse‌پذیر و با نسخه canonical اصلی متفاوت باشد.

    اصلاح: original_canonical باید قبلاً با _canonical() پردازش شده باشد.
    این جلوگیری می‌کند که تفاوت‌های فرمت‌بندی (مثل کامنت، فاصله اضافی)
    باعث شود یک Mutant هم‌ارز به اشتباه معتبر شناخته شود.
    """
    try:
        mut_canonical = ast.unparse(ast.parse(mut_code))
    except SyntaxError:
        return False

    return mut_canonical.strip() != original_canonical.strip()


def _code_hash(code: str) -> str:
    return hashlib.md5(code.encode("utf-8")).hexdigest()


def _match_node(node: ast.AST, lineno: int, col: int) -> bool:
    """بررسی تطابق گره AST با موقعیت مشخص."""
    return (
        getattr(node, "lineno",     -1) == lineno and
        getattr(node, "col_offset", -2) == col
    )


# ══════════════════════════════════════════════════════════════════════════════
# Integration Mutation Operators
# ══════════════════════════════════════════════════════════════════════════════

def _generate_integration_mutants(
    tree:              ast.AST,
    source_code:       str,
    original_canonical: str,   # ← اضافه شد
    source_lines:      list[str],
    enabled_ops:       list[str],
    func_map:          dict[int, str],
    counter:           list[int],
) -> list[MutantRecord]:
    """
    پنج نوع Integration Mutant تولید می‌کند.
    """
    result: list[MutantRecord] = []

    def _nid() -> str:
        counter[0] += 1
        return f"M{counter[0]:03d}"

    def _maybe_add(code: str, node_text: str, **kwargs) -> None:
        if _is_valid_mutant(code, original_canonical):
            result.append(MutantRecord(code=code, mutated_node_text=node_text, **kwargs))

    # ── IPVR: range(n) → range(n-1) ─────────────────────────────────────────
    if "IPVR" in enabled_ops:
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            ln  = getattr(node, "lineno",     0)
            col = getattr(node, "col_offset", 0)
            if ln == 0:
                continue

            func_id = ""
            if isinstance(node.func, ast.Name):
                func_id = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_id = node.func.attr
            if func_id != "range":
                continue

            mut = copy.deepcopy(tree)
            patched = False
            node_text = ""
            for mn in ast.walk(mut):
                if isinstance(mn, ast.Call) and _match_node(mn, ln, col):
                    last_arg = mn.args[-1]
                    new_arg = ast.BinOp(
                        left=copy.deepcopy(last_arg),
                        op=ast.Sub(),
                        right=ast.Constant(value=1),
                    )
                    ast.fix_missing_locations(new_arg)
                    mn.args[-1] = new_arg
                    patched = True
                    try:
                        node_text = ast.unparse(mn)
                    except Exception:
                        node_text = "range(n-1)"
                    break
            if not patched:
                continue

            try:
                code = ast.unparse(mut)
            except Exception:
                continue

            _maybe_add(
                code, node_text,
                id=_nid(),
                category="INTEGRATION",
                operator_name="range_arg_minus1",
                integration_op="IPVR",
                function_name=_line_to_func(ln, func_map),
                line=ln,
                original_line_text=_src_line(source_lines, ln),
                original_op="range(n)",
                mutated_op="range(n-1)",
                status="ALIVE",
            )

    # ── IUOI: نفی شرط if/while ───────────────────────────────────────────────
    if "IUOI" in enabled_ops:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.While)):
                continue
            ln  = getattr(node, "lineno",     0)
            col = getattr(node, "col_offset", 0)
            if ln == 0:
                continue

            mut = copy.deepcopy(tree)
            patched = False
            node_text = ""
            for mn in ast.walk(mut):
                if type(mn) is type(node) and _match_node(mn, ln, col):
                    new_test = ast.UnaryOp(
                        op=ast.Not(),
                        operand=copy.deepcopy(mn.test),
                    )
                    ast.fix_missing_locations(new_test)
                    mn.test = new_test
                    patched = True
                    try:
                        node_text = ast.unparse(new_test)
                    except Exception:
                        node_text = "not(cond)"
                    break
            if not patched:
                continue

            try:
                code = ast.unparse(mut)
            except Exception:
                continue

            _maybe_add(
                code, node_text,
                id=_nid(),
                category="INTEGRATION",
                operator_name="negate_condition",
                integration_op="IUOI",
                function_name=_line_to_func(ln, func_map),
                line=ln,
                original_line_text=_src_line(source_lines, ln),
                original_op="cond",
                mutated_op="not(cond)",
                status="ALIVE",
            )

    # ── IORC: جابجایی عملوندهای compare ─────────────────────────────────────
    if "IORC" in enabled_ops:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if len(node.comparators) != 1:
                continue
            if not isinstance(node.ops[0], (ast.Lt, ast.Gt, ast.LtE, ast.GtE)):
                continue
            ln  = getattr(node, "lineno",     0)
            col = getattr(node, "col_offset", 0)
            if ln == 0:
                continue

            mut = copy.deepcopy(tree)
            patched = False
            node_text = ""
            for mn in ast.walk(mut):
                if (isinstance(mn, ast.Compare)
                        and _match_node(mn, ln, col)
                        and len(mn.comparators) == 1):
                    L = copy.deepcopy(mn.left)
                    R = copy.deepcopy(mn.comparators[0])
                    mn.left            = R
                    mn.comparators[0]  = L
                    ast.fix_missing_locations(mn)
                    patched = True
                    try:
                        node_text = ast.unparse(mn)
                    except Exception:
                        node_text = "b op a"
                    break
            if not patched:
                continue

            try:
                code = ast.unparse(mut)
            except Exception:
                continue

            _maybe_add(
                code, node_text,
                id=_nid(),
                category="INTEGRATION",
                operator_name="swap_compare_operands",
                integration_op="IORC",
                function_name=_line_to_func(ln, func_map),
                line=ln,
                original_line_text=_src_line(source_lines, ln),
                original_op="a op b",
                mutated_op="b op a",
                status="ALIVE",
            )

    # ── ISMA: arr[i] → arr[i+1] ──────────────────────────────────────────────
    if "ISMA" in enabled_ops:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            if not isinstance(node.slice, (ast.Name, ast.BinOp, ast.Constant)):
                continue
            ln  = getattr(node, "lineno",     0)
            col = getattr(node, "col_offset", 0)
            if ln == 0:
                continue

            mut = copy.deepcopy(tree)
            patched = False
            node_text = ""
            for mn in ast.walk(mut):
                if isinstance(mn, ast.Subscript) and _match_node(mn, ln, col):
                    new_sl = ast.BinOp(
                        left=copy.deepcopy(mn.slice),
                        op=ast.Add(),
                        right=ast.Constant(value=1),
                    )
                    ast.fix_missing_locations(new_sl)
                    mn.slice = new_sl
                    patched = True
                    try:
                        node_text = ast.unparse(mn)
                    except Exception:
                        node_text = "arr[i+1]"
                    break
            if not patched:
                continue

            try:
                code = ast.unparse(mut)
            except Exception:
                continue

            _maybe_add(
                code, node_text,
                id=_nid(),
                category="INTEGRATION",
                operator_name="index_plus1",
                integration_op="ISMA",
                function_name=_line_to_func(ln, func_map),
                line=ln,
                original_line_text=_src_line(source_lines, ln),
                original_op="arr[i]",
                mutated_op="arr[i+1]",
                status="ALIVE",
            )

    # ── IMCD: len(x) → 0 ─────────────────────────────────────────────────────
    if "IMCD" in enabled_ops:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "len"):
                continue
            ln  = getattr(node, "lineno",     0)
            col = getattr(node, "col_offset", 0)
            if ln == 0:
                continue

            class _LenReplacer(ast.NodeTransformer):
                def __init__(self_, tln: int, tcol: int) -> None:
                    self_.tln  = tln
                    self_.tcol = tcol
                    self_.done = False

                def visit_Call(self_, n: ast.Call) -> ast.AST:
                    self_.generic_visit(n)
                    if (not self_.done
                            and isinstance(n.func, ast.Name)
                            and n.func.id == "len"
                            and _match_node(n, self_.tln, self_.tcol)):
                        self_.done = True
                        return ast.fix_missing_locations(ast.Constant(value=0))
                    return n

            mut      = copy.deepcopy(tree)
            replacer = _LenReplacer(ln, col)
            mut      = replacer.visit(mut)
            if not replacer.done:
                continue

            try:
                code = ast.unparse(mut)
            except Exception:
                continue

            _maybe_add(
                code, "0",
                id=_nid(),
                category="INTEGRATION",
                operator_name="len_to_zero",
                integration_op="IMCD",
                function_name=_line_to_func(ln, func_map),
                line=ln,
                original_line_text=_src_line(source_lines, ln),
                original_op="len(x)",
                mutated_op="0",
                status="ALIVE",
            )

    return result


# ══════════════════════════════════════════════════════════════════════════════
# تابع اصلی عمومی
# ══════════════════════════════════════════════════════════════════════════════

def generate_all_mutants(
    source_code:     str,
    mutation_config: dict,
    verbose:         bool = False,
    project_model=None,
    module_name:     str = None,
) -> list[MutantRecord]:
    """
    تمام Mutant‌های ممکن را از source_code تولید می‌کند.

    Args:
        source_code:     کد Python کامل (string)
        mutation_config: بخش "mutation" از config.json
        verbose:         چاپ پیشرفت

    Returns:
        list[MutantRecord] — بدون تکراری، اعتبارسنجی‌شده

    Raises:
        ValueError:  source_code خالی است
        SyntaxError: کد parse‌پذیر نیست
    """
    if not source_code or not source_code.strip():
        raise ValueError("source_code خالی است.")

    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        raise SyntaxError(
            f"خطای parse در source_code:\n  خط {e.lineno}: {e.msg}\n"
            "مطمئن شوید کد Python معتبر است."
        ) from e

    # ── نسخه canonical کد اصلی برای مقایسه عادلانه ──────────────────────────
    # این کار از false-negative در _is_valid_mutant جلوگیری می‌کند
    # (تفاوت‌های فرمت‌بندی مثل کامنت، فاصله اضافی)
    original_canonical = ast.unparse(tree)

    source_lines = source_code.splitlines()
    func_map     = _build_func_map(tree)

    if verbose:
        logger.info(f"  [MutantGen] parse OK — {len(source_lines)} خط | "
              f"توابع: {list(func_map.values())}")

    do_arith   = mutation_config.get("arithmetic_operators", True)
    do_rel     = mutation_config.get("relational_operators", True)
    do_logical = mutation_config.get("logical_operators",    False)

    # ── پاس ۱: جمع‌آوری ──────────────────────────────────────────────────────
    collector = _MutationEngine(
        mode="collect",
        do_arith=do_arith,
        do_rel=do_rel,
        do_logical=do_logical,
    )
    collector.visit(copy.deepcopy(tree))
    entries = collector.collected

    if verbose:
        from collections import Counter
        cats           = Counter(e[1] for e in entries)
        total_possible = sum(len(e[3]) for e in entries)
        logger.info(f"  [MutantGen] عملگرها: {dict(cats)} | جهش‌های ممکن: {total_possible}")

    mutants: list[MutantRecord] = []
    counter: list[int] = [0]

    def _nid() -> str:
        counter[0] += 1
        return f"M{counter[0]:03d}"

    # ── پاس ۲: تولید Mutant ──────────────────────────────────────────────────
    for (idx, category, op_type, reps, lineno, col) in entries:
        func_name = _line_to_func(lineno, func_map)
        src_line  = _src_line(source_lines, lineno)
        orig_name = OP_NAMES.get(op_type, op_type.__name__)

        for rep_cls in reps:
            rep_name = OP_NAMES.get(rep_cls, rep_cls.__name__)

            engine = _MutationEngine(
                mode="mutate",
                target_index=idx,
                replacement_cls=rep_cls,
                do_arith=do_arith,
                do_rel=do_rel,
                do_logical=do_logical,
            )
            mut_tree = engine.visit(copy.deepcopy(tree))

            if engine.mutated_line is None:
                if verbose:
                    logger.info(f"  [WARN] mutated_line=None idx={idx} rep={rep_name}")
                continue

            try:
                mut_code = ast.unparse(mut_tree)
            except Exception as e:
                if verbose:
                    logger.info(f"  [SKIP] unparse idx={idx}: {e}")
                continue

            if not _is_valid_mutant(mut_code, original_canonical):
                continue

            mutants.append(MutantRecord(
                id=_nid(),
                category=category,
                operator_name=f"{orig_name}_to_{rep_name}",
                integration_op=None,
                function_name=func_name,
                line=engine.mutated_line,
                original_line_text=src_line,
                mutated_node_text=engine.mutated_node_text,  # ← جدید
                original_op=orig_name,
                mutated_op=rep_name,
                code=mut_code,
            ))

    # ── Integration Operators ─────────────────────────────────────────────────
    enabled_int = mutation_config.get("integration_operators", [])
    if enabled_int:
        int_mutants = _generate_integration_mutants(
            tree=tree,
            source_code=source_code,
            original_canonical=original_canonical,  # ← اضافه شد
            source_lines=source_lines,
            enabled_ops=enabled_int,
            func_map=func_map,
            counter=counter,
        )
        mutants.extend(int_mutants)
        if verbose:
            logger.info(f"  [MutantGen] Integration Mutants: {len(int_mutants)}")

    # ── حذف تکراری ───────────────────────────────────────────────────────────
    seen:   set[str]           = set()
    unique: list[MutantRecord] = []
    for m in mutants:
        h = _code_hash(m.code)
        if h not in seen:
            seen.add(h)
            unique.append(m)

    removed = len(mutants) - len(unique)
    if verbose and removed:
        logger.info(f"  [MutantGen] {removed} Mutant تکراری حذف شد")
    if verbose:
        logger.info(f"  [MutantGen] ✓ مجموع نهایی: {len(unique)} Mutant")

    # ── N1.6: observational qualified attribution ─────────────────────────────
    # When a ProjectModel is supplied (built once in the parent process), fill
    # MutantRecord.module / .qualified_name from each mutant's line. function_name
    # stays bare. No execution/scoring effect: the model is consulted here in the
    # parent only and is NEVER passed to spawn workers (they receive code strings).
    _apply_qualified_attribution(unique, project_model, module_name)

    return unique


def _apply_qualified_attribution(mutants: list[MutantRecord],
                                 project_model, module_name: str = None) -> None:
    """Populate module / qualified_name on each mutant, in place.

    No-op when project_model is None. Resolves the module name from the explicit
    `module_name` argument, else from a single-module model (the V2 single-file
    shape); when the model has multiple modules and none was specified, the
    fields are left empty (multi-file attribution is handled in N2)."""
    if project_model is None:
        return
    try:
        mods = project_model.modules()
    except Exception:
        return
    mod = module_name or (mods[0] if len(mods) == 1 else None)
    if not mod:
        return
    for m in mutants:
        try:
            m.module = mod
            m.qualified_name = project_model.qualified_name_at(mod, m.line)
        except Exception:
            # attribution is best-effort/observational; never break generation
            m.module = m.module or ""
            m.qualified_name = m.qualified_name or ""


# ══════════════════════════════════════════════════════════════════════════════
# ابزارهای گزارش‌دهی
# ══════════════════════════════════════════════════════════════════════════════

def print_mutant_summary(mutants: list[MutantRecord]) -> None:
    if not mutants:
        logger.info("  [MutantGen] هیچ Mutantی تولید نشد.")
        return

    from collections import Counter
    cat  = Counter(m.category      for m in mutants)
    func = Counter(m.function_name for m in mutants)
    ops  = Counter(m.operator_name for m in mutants)
    w    = 58

    logger.info(f"\n{'─'*w}")
    logger.info(f"  مجموع Mutant: {len(mutants)}")
    logger.info("  بر اساس دسته:")
    for k, v in sorted(cat.items()):
        logger.info(f"    {k:<18}: {v}")
    logger.info("  بر اساس تابع:")
    for k, v in sorted(func.items()):
        logger.info(f"    {k:<24}: {v}")
    logger.info("  پرتکرارترین عملگرها (top-8):")
    for k, v in ops.most_common(8):
        logger.info(f"    {k:<30}: {v}")
    logger.info(f"{'─'*w}")


def save_mutants_json(mutants: list[MutantRecord], path: str) -> None:
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([m.to_dict() for m in mutants], f, ensure_ascii=False, indent=2)
    logger.info(f"  [MutantGen] {len(mutants)} Mutant → {path}")


# ══════════════════════════════════════════════════════════════════════════════
# اجرای مستقل
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import ast as _ast

    _cfg_path    = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    _target_path = sys.argv[2] if len(sys.argv) > 2 else "target_code.py"

    with open(_cfg_path, encoding="utf-8") as _f:
        _cfg = json.load(_f)
    with open(_target_path, encoding="utf-8") as _f:
        _source = _f.read()

    print(f"[PHASE-1] تولید Mutant: {_target_path}\n")
    _mutants = generate_all_mutants(
        source_code=_source,
        mutation_config=_cfg["mutation"],
        verbose=True,
    )

    print_mutant_summary(_mutants)

    print("\n[PHASE-1] اعتبارسنجی همه Mutant‌ها...")
    _ok = _fail = 0
    for _m in _mutants:
        try:
            _ast.parse(_m.code)
            if _m.code.strip() == _source.strip():
                print(f"  [FAIL] {_m.id}: کد با اصلی یکسان!")
                _fail += 1
            else:
                _ok += 1
        except SyntaxError as _e:
            print(f"  [FAIL] {_m.id} SyntaxError: {_e}")
            _fail += 1
    print(f"  نتیجه: {_ok} معتبر | {_fail} شکست")

    print("\n[PHASE-1] ۵ Mutant نمونه:")
    for _m in _mutants[:5]:
        print(f"\n  {_m}")
        print(f"    خط اصلی       : {_m.original_line_text.strip()}")
        print(f"    گره تغییریافته: {_m.mutated_node_text}")

    _out = _cfg.get("project", {}).get("output_dir", "output/")
    save_mutants_json(_mutants, os.path.join(_out, "mutants.json"))
    print(f"\n[PHASE-1] ✓ {len(_mutants)} Mutant آماده برای فاز ۲")