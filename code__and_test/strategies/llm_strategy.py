"""
strategies/llm_strategy.py  —  استراتژی B
==========================================
تولید تست هدفمند با LLM، گروه‌بندی per-function × per-operator.

طراحی:
  ─ Mutant‌ها بر اساس (function_name × operator_name) گروه‌بندی می‌شوند
  ─ از هر گروه فقط یک نماینده انتخاب می‌شود → یک API call
  ─ پرامپت فقط diff خط اصلی vs جهش‌یافته است (بدون RAG، بدون راهنمای hardcode)
  ─ خروجی LLM با safe_parse_json اعتبارسنجی می‌شود
  ─ خطاهای LLM بی‌صدا رد می‌شوند (سیستم بدون LLM هم کار می‌کند)

خروجی: [{"function": str, "inputs": {...}}]
"""

from __future__ import annotations

import copy

from utils.json_utils import safe_parse_json
import logging

logger = logging.getLogger(__name__)


def generate(
    mutants:    list,      # list[MutantRecord]
    llm_client,            # LLMClient  (یا DummyLLM)
    config:     dict,
) -> list[dict]:
    """
    Args:
        mutants:    لیست MutantRecord از فاز ۱
        llm_client: نمونه LLMClient
        config:     dict کامل config.json

    Returns:
        لیستی از TestRecord: [{"function": str, "inputs": {...}}]
    """
    pool_cfg    = config.get("test_pool", {})
    temperature = pool_cfg.get("llm_temperature",             0.85)
    n_per_rep   = pool_cfg.get("llm_tests_per_representative", 4)
    enabled     = pool_cfg.get("llm_enabled",                 True)

    if not enabled:
        logger.info("  [LLM] llm_enabled=false — استراتژی B رد شد")
        return []

    if not mutants:
        return []

    tests: list[dict] = []

    # ── گروه‌بندی: (function_name × operator_name) → یک نماینده ─────────────
    # از هر ترکیب (تابع، نوع جهش) فقط یک Mutant به LLM فرستاده می‌شود
    # تا تعداد API call‌ها مدیریت شود.
    groups: dict[tuple[str, str], object] = {}
    for m in mutants:
        key = (m.function_name, m.operator_name)
        if key not in groups:
            groups[key] = m

    n_funcs = len(set(m.function_name for m in mutants))
    logger.info(
        f"  [LLM] {len(groups)} نماینده از {len(mutants)} Mutant "
        f"({n_funcs} تابع، {len(groups)} گروه operator)"
    )

    for (func_name, op_name), rep in groups.items():
        prompt = _build_prompt(rep, n_per_rep)

        raw = llm_client.call(prompt, temperature=temperature)
        if not raw:
            continue

        parsed = safe_parse_json(raw)
        if not parsed:
            logger.info(f"  [LLM] parse ناموفق: {func_name}/{op_name}")
            continue

        added = 0
        for item in parsed:
            if not isinstance(item, dict):
                continue
            tests.append({
                "function": func_name,
                "inputs":   _deep_copy_inputs(item),
            })
            added += 1

        if added == 0:
            logger.info(f"  [LLM] هیچ تست معتبری: {func_name}/{op_name}")

    logger.info(f"  [LLM] {len(tests)} تست تولید شد")
    return tests


def _build_prompt(mutant, n: int) -> str:
    """
    پرامپت را از روی diff کد می‌سازد.
    فقط یک خط اصلی و یک خط جهش‌یافته — بدون RAG، بدون راهنمای hardcode.
    """
    fn        = mutant.function_name
    orig_line = mutant.original_line_text.strip()

    # خط جهش‌یافته را از کد Mutant استخراج می‌کند
    mut_lines = mutant.code.splitlines()
    mut_line  = ""
    if 1 <= mutant.line <= len(mut_lines):
        mut_line = mut_lines[mutant.line - 1].strip()

    return (
        f"You are a precise software tester.\n\n"
        f"Function: {fn}\n"
        f"Original line {mutant.line}: {orig_line}\n"
        f"Mutated  line {mutant.line}: {mut_line}\n\n"
        f"Task: Generate exactly {n} Python dicts as inputs to '{fn}' "
        f"that will produce DIFFERENT outputs for the original vs the mutated code.\n\n"
        f"Rules:\n"
        f"  - Return ONLY a valid JSON array of dicts. No explanation, no markdown.\n"
        f"  - Each dict must have the correct parameter names for '{fn}'.\n"
        f"  - Make inputs diverse: different sizes, edge cases, boundary values.\n"
        f"  - For list parameters, include both empty and non-empty cases.\n\n"
        f"Example output: "
        f'[{{"arr": [3,1,2]}}, {{"arr": []}}, {{"arr": [5,5,1]}}]'
    )


def _deep_copy_inputs(item: dict) -> dict:
    """مقادیر را deep copy می‌کند تا بین تست‌ها share نشوند."""
    return {k: copy.deepcopy(v) for k, v in item.items()}