"""
core/reporter.py  —  فاز ۶
============================
تولید گزارش نهایی در دو فایل JSON + خلاصه console.

اصلاحات نسخه ۴:
  ─ _print_summary: از ilp_result.n_killed به جای شمارش status=='KILLED'
    استفاده می‌کند. این با امتیاز نمایش‌داده‌شده سازگار است.
  ─ پارامتر اختیاری orig_results اضافه شد — اگر ارائه شود،
    expected_output برای هر تست در minimum_test_suite.json ذخیره می‌شود.
    این برای تولید فایل pytest با assertion دقیق لازم است.
  ─ source_hash (MD5) برای قابلیت ردیابی نسخه حفظ شد.

اصلاحات نسخه ۵:
  ─ آستانه موفقیت از config (optimizer.target_score) خوانده می‌شود
    نه مقدار hardcode‌شده ۹۰. بنر ✅/⚠️ اکنون با هدف واقعی سازگار است.

خروجی:
  output/mutation_report.json     — گزارش کامل
  output/minimum_test_suite.json  — مجموعه تست کمینه
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Optional

from core.ilp_solver import ILPResult
import logging

logger = logging.getLogger(__name__)


def generate(
    mutants:       list,             # list[MutantRecord]
    all_tests:     list[dict],       # کل استخر تست
    kill_matrix:   list[list[bool]],
    ilp_result:    ILPResult,
    original_code: str,
    config:        dict,
    orig_results:  Optional[list[tuple]] = None,   # ← جدید: برای expected_output
    equivalence:   Optional[dict] = None,          # ← I4 (M2): additive annotation
    feedback:      Optional[dict] = None,          # ← I6 (M2): additive annotation
) -> None:
    """
    گزارش نهایی را می‌سازد و در output/ ذخیره می‌کند.

    Args:
        mutants:       لیست MutantRecord از فاز ۱
        all_tests:     کل استخر تست (بعد از همه فازها)
        kill_matrix:   ماتریس پوشش از فاز ۳
        ilp_result:    نتیجه ILP از فاز ۴ یا ۵
        original_code: کد اصلی هدف — هش MD5 آن در گزارش ذخیره می‌شود
        config:        dict کامل config.json
        orig_results:  (اختیاری) نتایج کد اصلی برای هر تست.
                       اگر ارائه شود، expected_output در سوئیت ذخیره می‌شود.
    """
    out_dir = config.get("project", {}).get("output_dir", "output/")
    os.makedirs(out_dir, exist_ok=True)

    target_score = float(config.get("optimizer", {}).get("target_score", 90.0))

    source_hash  = hashlib.md5(original_code.encode("utf-8"),
                               usedforsecurity=False).hexdigest()
    source_lines = len(original_code.splitlines())

    selected_idx  = ilp_result.selected_tests
    suspected_idx = ilp_result.suspected_indices
    n_tests       = len(all_tests)
    n_mutants     = len(mutants)

    selected_tests = [all_tests[i] for i in selected_idx if i < len(all_tests)]

    killed_mutants    = [m for m in mutants if m.status == "KILLED"]
    alive_mutants     = [m for m in mutants if m.status == "ALIVE"]
    suspected_mutants = [m for m in mutants if m.status == "SUSPECTED"]

    # ── تست‌های انتخابی با اطلاعات kills و expected_output ──────────────────
    min_suite_records = []
    for i in selected_idx:
        if i >= len(all_tests):
            continue
        test = all_tests[i]
        kills = [
            mutants[j].id
            for j in range(n_mutants)
            if i < len(kill_matrix) and j < len(kill_matrix[i]) and kill_matrix[i][j]
        ]
        record: dict = {
            "test_id":  f"T{i+1:04d}",
            "function": test.get("function"),
            "inputs":   test.get("inputs"),
            "kills":    kills,
        }

        # اضافه کردن expected_output اگر orig_results موجود باشد
        if orig_results is not None and i < len(orig_results):
            status, output = orig_results[i]
            if status == "OK" and output is not None:
                record["expected_output"] = output

        min_suite_records.append(record)

    # ── فایل ۱: mutation_report.json ─────────────────────────────────────────
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "target_file":  config.get("project", {}).get("target_file", "target_code.py"),
            "source_hash":  source_hash,
            "source_lines": source_lines,
        },
        "summary": {
            "raw_score":        round(ilp_result.raw_score,      2),
            "adjusted_score":   round(ilp_result.adjusted_score, 2),
            "total_mutants":    n_mutants,
            "killed":           ilp_result.n_killed,
            "suspected":        len(suspected_mutants),
            "alive":            len(alive_mutants),
            "total_tests_pool": n_tests,
            "min_test_count":   len(selected_tests),
        },
        "killed_mutants": [
            _mutant_brief(m) for m in killed_mutants
        ],
        "suspected_mutants": [
            _mutant_brief(m) for m in suspected_mutants
        ],
        "alive_mutants": [
            _mutant_brief(m) for m in alive_mutants
        ],
        "score_explanation": (
            "raw_score = killed / total × 100\n"
            "adjusted_score = killed / (total − suspected) × 100\n"
            "suspected: mutants that no test killed after all rounds "
            "(may be equivalent, but NOT guaranteed)"
        ),
    }

    # ── M2 additive analysis layer (I4 equivalence + I6 feedback) ────────────
    # Purely additive: appears only when the orchestrator supplies the data.
    # Does NOT touch `summary` (the frozen V1 metrics) — V1 callers that omit
    # these args (e.g. verify.py) produce byte-identical reports.
    if equivalence is not None or feedback is not None:
        analysis: dict = {}
        if equivalence is not None:
            analysis["equivalence"] = equivalence
        if feedback is not None:
            analysis["feedback"] = feedback
        report["analysis"] = analysis
        logger.info(
            "  [Reporter] analysis: equivalent=%s, survivors=%s",
            (equivalence or {}).get("count", 0),
            (feedback or {}).get("survivors", "n/a"),
        )

    report_path = os.path.join(out_dir, "mutation_report.json")
    _write_json(report, report_path)

    # ── فایل ۲: minimum_test_suite.json ──────────────────────────────────────
    suite = {
        "generated_at":   datetime.now().isoformat(timespec="seconds"),
        "source_hash":    source_hash,
        "test_count":     len(min_suite_records),
        "mutation_score": round(ilp_result.adjusted_score, 2),
        "tests":          min_suite_records,
    }

    suite_path = os.path.join(out_dir, "minimum_test_suite.json")
    _write_json(suite, suite_path)

    _print_summary(ilp_result, mutants, n_tests, selected_tests, target_score)


# ══════════════════════════════════════════════════════════════════════════════
# کمک‌کننده‌ها
# ══════════════════════════════════════════════════════════════════════════════

def _mutant_brief(m) -> dict:
    brief = {
        "id":            m.id,
        "category":      m.category,
        "operator":      m.operator_name,
        "function":      m.function_name,
        "line":          m.line,
        "original_line": m.original_line_text.strip(),
        "original_op":   m.original_op,
        "mutated_op":    m.mutated_op,
        "integration_op": m.integration_op,
    }
    # N1.6: additive qualified attribution — emitted only when populated, so the
    # single-file report keeps every pre-existing key/value unchanged.
    qn = getattr(m, "qualified_name", "")
    mod = getattr(m, "module", "")
    if qn:
        brief["qualified_name"] = qn
    if mod:
        brief["module"] = mod
    return brief


def _write_json(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"  [Reporter] → {path}")


def _print_summary(
    ilp_result:     ILPResult,
    mutants:        list,
    n_pool:         int,
    selected_tests: list,
    target_score:   float = 90.0,
) -> None:
    """
    خلاصه console را چاپ می‌کند.

    اصلاح نسخه ۴:
      از ilp_result.n_killed استفاده می‌کند نه شمارش status=='KILLED'.
      این با امتیاز نمایش‌داده‌شده سازگار است:
      ilp_result.n_killed = مبنای محاسبه raw_score و adjusted_score.

    اصلاح نسخه ۵:
      آستانه موفقیت از config (optimizer.target_score) خوانده می‌شود
      نه مقدار hardcode‌شده ۹۰. بنر ✅/⚠️ اکنون با هدف واقعی سازگار است.
    """
    n       = len(mutants)
    w       = 56
    n_alive = sum(1 for m in mutants if m.status == "ALIVE")

    logger.info(f"\n{'═'*w}")
    logger.info(f"  گزارش نهایی Mutation Testing")
    logger.info(f"{'─'*w}")
    logger.info(f"  {'Raw Mutation Score:':<26} {ilp_result.raw_score:.1f}%"
          f"  ({ilp_result.n_killed}/{ilp_result.n_total})")
    logger.info(f"  {'Adjusted Mutation Score:':<26} {ilp_result.adjusted_score:.1f}%"
          f"  ({ilp_result.n_killed}/{ilp_result.n_total - ilp_result.n_suspected})")
    logger.info(f"{'─'*w}")
    logger.info(f"  {'مجموع Mutant:':<26} {n}")
    # ── اصلاح: ilp_result.n_killed به جای شمارش status ──────────────────────
    logger.info(f"  {'  کشته‌شده:':<26} {ilp_result.n_killed}")
    logger.info(f"  {'  Suspected Equivalent:':<26} {ilp_result.n_suspected}")
    logger.info(f"  {'  زنده‌مانده:':<26} {n_alive}")
    logger.info(f"{'─'*w}")
    logger.info(f"  {'تست‌های استخر:':<26} {n_pool}")
    logger.info(f"  {'مجموعه تست کمینه:':<26} {len(selected_tests)}")

    if ilp_result.adjusted_score >= target_score:
        logger.info(f"\n  ✅  Score بالای {target_score:.0f}٪ — هدف تحقق یافت!")
    else:
        logger.info(f"\n  ⚠️  Score زیر {target_score:.0f}٪ ({ilp_result.adjusted_score:.1f}%)")
        logger.info(f"     پیشنهاد: تست‌های دستی برای Mutant‌های زنده اضافه کنید.")

    logger.info(f"{'═'*w}\n")