"""
utils/coverage_filter.py
------------------------
فیلتر Mutant بر اساس پوشش کد با coverage.py.

خط‌هایی که توسط test pool پوشش داده می‌شوند را شناسایی می‌کند،
سپس Mutant‌هایی که روی خط‌های پوشش‌نیافته هستند را حذف می‌کند.

این کار تعداد Mutant‌های بی‌فایده را کاهش می‌دهد و
کیفیت mutation testing را بهبود می‌بخشد.

وابستگی: pip install coverage
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def get_covered_lines(
    source_file:   str,
    test_pool:     list[dict],
    source_code:   str,
    max_tests:     int = 200,
    timeout:       float = 30.0,
) -> Optional[set[int]]:
    """
    خط‌های پوشش‌داده‌شده توسط test_pool را برمی‌گرداند.

    از coverage.py برای اندازه‌گیری استفاده می‌کند.
    یک script موقت می‌سازد که توابع هدف را با ورودی‌های test_pool فراخوانی می‌کند.

    Args:
        source_file:  مسیر فایل هدف
        test_pool:    لیست تست‌ها برای اندازه‌گیری پوشش
        source_code:  کد منبع (برای استخراج نام توابع)
        max_tests:    حداکثر تعداد تست برای اندازه‌گیری (سرعت)
        timeout:      حداکثر زمان (ثانیه)

    Returns:
        set[int]: مجموعه شماره خط‌های پوشش‌داده‌شده
        None:     اگر اندازه‌گیری ناموفق بود
    """
    # بررسی نصب coverage
    try:
        import coverage as _cov  # noqa: F401
    except ImportError:
        logger.info("  [Coverage] coverage.py نصب نیست. اجرا کنید: pip install coverage")
        return None

    abs_source = os.path.abspath(source_file)
    if not os.path.exists(abs_source):
        logger.info(f"  [Coverage] فایل هدف پیدا نشد: {abs_source}")
        return None

    temp_dir = tempfile.mkdtemp(prefix="mutation_cov_")
    try:
        return _measure_coverage(
            abs_source, source_code, test_pool[:max_tests],
            temp_dir, timeout,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def filter_mutants_by_coverage(
    mutants:       list,           # list[MutantRecord]
    covered_lines: set[int],
    verbose:       bool = False,
) -> list:
    """
    Mutant‌هایی را که روی خط‌های پوشش‌نیافته هستند حذف می‌کند.

    Args:
        mutants:       لیست MutantRecord
        covered_lines: مجموعه شماره خط‌های پوشش‌داده‌شده
        verbose:       چاپ جزئیات

    Returns:
        لیست فیلترشده از Mutant‌ها
    """
    if not covered_lines:
        if verbose:
            logger.info("  [Coverage] خط پوشش‌داده‌شده‌ای پیدا نشد — فیلتر اعمال نشد")
        return mutants

    kept    = [m for m in mutants if m.line in covered_lines]
    removed = len(mutants) - len(kept)

    logger.info(f"  [Coverage] {removed} Mutant روی خط‌های پوشش‌نیافته حذف شد | "
          f"{len(kept)} باقی ماند")

    if verbose and removed > 0:
        by_line: dict[int, int] = {}
        for m in mutants:
            if m.line not in covered_lines:
                by_line[m.line] = by_line.get(m.line, 0) + 1
        for line, cnt in sorted(by_line.items()):
            logger.info(f"    خط {line}: {cnt} Mutant حذف شد")

    return kept


def prioritize_mutants_by_coverage(
    mutants:       list,
    covered_lines: set[int],
) -> list:
    """
    همه Mutant‌ها را نگه می‌دارد اما پوشش‌داده‌شده‌ها اول می‌آیند.
    برای سناریوهایی که نمی‌خواهید Mutant‌ها را حذف کنید.
    """
    covered   = [m for m in mutants if m.line in covered_lines]
    uncovered = [m for m in mutants if m.line not in covered_lines]
    return covered + uncovered


# ══════════════════════════════════════════════════════════════════════════════
# داخلی
# ══════════════════════════════════════════════════════════════════════════════

def _measure_coverage(
    abs_source:  str,
    source_code: str,
    test_pool:   list[dict],
    temp_dir:    str,
    timeout:     float,
) -> Optional[set[int]]:
    """اندازه‌گیری واقعی پوشش."""
    runner_path  = os.path.join(temp_dir, "_runner.py")
    cov_data     = os.path.join(temp_dir, ".coverage")
    report_path  = os.path.join(temp_dir, "cov.json")
    source_dir   = os.path.dirname(abs_source)

    # ساخت script runner
    func_calls = _build_func_calls(test_pool)
    runner_code = _build_runner_script(abs_source, source_dir, func_calls)

    with open(runner_path, "w", encoding="utf-8") as f:
        f.write(runner_code)

    # اجرا با coverage
    run_cmd = [
        sys.executable, "-m", "coverage", "run",
        f"--data-file={cov_data}",
        f"--source={abs_source}",
        runner_path,
    ]

    try:
        subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=source_dir,
        )
    except subprocess.TimeoutExpired:
        logger.info("  [Coverage] اندازه‌گیری timeout شد")
        return None
    except Exception as e:
        logger.info(f"  [Coverage] خطا در اجرا: {e}")
        return None

    # استخراج نتیجه با JSON report
    json_cmd = [
        sys.executable, "-m", "coverage", "json",
        f"--data-file={cov_data}",
        "-o", report_path,
    ]
    try:
        subprocess.run(
            json_cmd,
            capture_output=True,
            timeout=15,
            cwd=source_dir,
        )
    except Exception:
        return None

    if not os.path.exists(report_path):
        return None

    try:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    covered: set[int] = set()
    for filename, file_data in report.get("files", {}).items():
        if os.path.abspath(filename) == abs_source:
            covered.update(file_data.get("executed_lines", []))
            break

    if covered:
        logger.info(f"  [Coverage] {len(covered)} خط پوشش داده شده")
    return covered if covered else None


def _build_func_calls(test_pool: list[dict]) -> list[str]:
    """فراخوانی‌های تابع برای runner script."""
    calls = []
    for test in test_pool:
        fn     = test.get("function", "")
        inputs = test.get("inputs", {})
        if not fn:
            continue
        try:
            args = ", ".join(
                f"{k}={_safe_repr(v)}"
                for k, v in inputs.items()
            )
            calls.append(
                f"    try:\n"
                f"        import copy as _c\n"
                f"        {fn}(**_c.deepcopy({repr(inputs)}))\n"
                f"    except Exception:\n"
                f"        pass"
            )
        except Exception:
            pass
    return calls


def _build_runner_script(
    abs_source: str,
    source_dir: str,
    func_calls: list[str],
) -> str:
    """Runner script که توابع هدف را با ورودی‌های test_pool فراخوانی می‌کند."""
    calls_str = "\n".join(func_calls) if func_calls else "    pass"
    return (
        f"import sys, os\n"
        f"sys.path.insert(0, {repr(source_dir)})\n"
        f"\n"
        f"import importlib.util as _ilu\n"
        f"_spec = _ilu.spec_from_file_location('_target_', {repr(abs_source)})\n"
        f"_mod  = _ilu.module_from_spec(_spec)\n"
        f"_spec.loader.exec_module(_mod)\n"
        f"\n"
        f"for _name in dir(_mod):\n"
        f"    if not _name.startswith('_'):\n"
        f"        globals()[_name] = getattr(_mod, _name)\n"
        f"\n"
        f"if __name__ == '__main__':\n"
        f"{calls_str}\n"
    )


def _safe_repr(val: object) -> str:
    try:
        return repr(val)
    except Exception:
        return "'<unrepresentable>'"