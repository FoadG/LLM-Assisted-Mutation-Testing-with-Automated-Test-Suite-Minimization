"""
utils/pytest_exporter.py
------------------------
تولید فایل pytest قابل اجرا از minimum_test_suite.json.

برای هر تست در سوئیت کمینه:
  - تابع هدف را با ورودی‌های ذخیره‌شده فراخوانی می‌کند
  - خروجی مورد انتظار را با importlib محاسبه می‌کند
  - یک تابع pytest با assert مناسب تولید می‌کند

خروجی: test_generated_{timestamp}.py
استفاده: pytest test_generated_TIMESTAMP.py
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def export_pytest_file(
    suite_data:   dict,
    target_file:  str,
    output_path:  Optional[str] = None,
    project_root: Optional[str] = None,
) -> str:
    """
    فایل pytest را از داده‌های سوئیت تولید می‌کند.

    Args:
        suite_data:   محتوای minimum_test_suite.json
        target_file:  مسیر فایل هدف (برای محاسبه خروجی مورد انتظار)
        output_path:  مسیر خروجی (پیش‌فرض: auto-generated با timestamp)
        project_root: ریشه پروژه (برای import)

    Returns:
        مسیر فایل pytest تولید‌شده
    """
    abs_target = os.path.abspath(target_file)
    if project_root is None:
        project_root = os.path.dirname(abs_target)

    module_name = os.path.splitext(os.path.basename(abs_target))[0]
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")

    if output_path is None:
        output_dir  = os.path.dirname(abs_target)
        output_path = os.path.join(output_dir, f"test_generated_{timestamp}.py")

    tests = suite_data.get("tests", [])

    # محاسبه خروجی مورد انتظار با اجرای واقعی توابع
    expected_map = _compute_expected_outputs(tests, abs_target, project_root)

    # جمع‌آوری نام توابع
    func_names = sorted({t["function"] for t in tests if "function" in t})

    lines = _build_file_header(suite_data, module_name, func_names, project_root, timestamp)

    for test in tests:
        lines.extend(
            _build_test_function(test, expected_map, module_name)
        )

    content = "\n".join(lines)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"  [PytestExport] → {output_path}  ({len(tests)} تست)")
    return output_path


def export_from_file(
    suite_path:   str,
    target_file:  str,
    output_path:  Optional[str] = None,
    project_root: Optional[str] = None,
) -> str:
    """سوئیت را از فایل بارگذاری و فایل pytest را تولید می‌کند."""
    with open(suite_path, encoding="utf-8") as f:
        suite_data = json.load(f)
    return export_pytest_file(suite_data, target_file, output_path, project_root)


# ══════════════════════════════════════════════════════════════════════════════
# محاسبه خروجی مورد انتظار
# ══════════════════════════════════════════════════════════════════════════════

def _compute_expected_outputs(
    tests:        list[dict],
    target_file:  str,
    project_root: str,
) -> dict[str, Optional[str]]:
    """
    خروجی مورد انتظار برای هر تست را با اجرای واقعی توابع محاسبه می‌کند.

    Returns:
        dict: test_id → JSON string (یا None اگر اجرا ناموفق بود)
    """
    expected: dict[str, Optional[str]] = {}

    # بارگذاری ماژول هدف
    try:
        mod = _load_target_module(target_file, project_root)
    except Exception as e:
        logger.info(f"  [PytestExport] بارگذاری ماژول ناموفق: {e}")
        return {}

    for test in tests:
        test_id = test.get("test_id", "")
        func_name = test.get("function", "")
        inputs    = test.get("inputs",   {})

        func = getattr(mod, func_name, None)
        if func is None:
            expected[test_id] = None
            continue

        try:
            # deepcopy چون توابع sort مثل bubble_sort in-place تغییر می‌دهند
            inputs_copy = copy.deepcopy(inputs)
            result = func(**inputs_copy)
            expected[test_id] = json.dumps(result, default=str, sort_keys=True)
        except Exception:
            expected[test_id] = None

    return expected


def _load_target_module(target_file: str, project_root: str):
    """ماژول هدف را با importlib بارگذاری می‌کند."""
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    spec = importlib.util.spec_from_file_location("_pytest_export_target_", target_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"نمی‌توان ماژول را بارگذاری کرد: {target_file}")

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ══════════════════════════════════════════════════════════════════════════════
# ساخت محتوای فایل
# ══════════════════════════════════════════════════════════════════════════════

def _build_file_header(
    suite_data:   dict,
    module_name:  str,
    func_names:   list[str],
    project_root: str,
    timestamp:    str,
) -> list[str]:
    """هدر فایل pytest را می‌سازد."""
    imports_line = (
        f"from {module_name} import {', '.join(func_names)}"
        if func_names else
        "# No functions found"
    )

    return [
        '"""',
        f"Auto-generated pytest test file.",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Source hash: {suite_data.get('source_hash', 'unknown')}",
        f"Mutation score: {suite_data.get('mutation_score', 0)}%",
        f"Test count: {suite_data.get('test_count', 0)}",
        "",
        "Usage:",
        "    pytest " + f"test_generated_{timestamp}.py -v",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import copy",
        "import sys",
        "import os",
        "",
        f"# اطمینان از اینکه پوشه پروژه در path است",
        f"sys.path.insert(0, {repr(project_root)})",
        "",
        "# بارگذاری توابع هدف",
        imports_line,
        "",
        "",
    ]


def _build_test_function(
    test:         dict,
    expected_map: dict[str, Optional[str]],
    module_name:  str,
) -> list[str]:
    """یک تابع pytest برای یک تست می‌سازد."""
    test_id   = test.get("test_id",  "T0000")
    func_name = test.get("function", "unknown")
    inputs    = test.get("inputs",   {})
    kills     = test.get("kills",    [])
    expected  = expected_map.get(test_id)

    fn_safe      = test_id.lower().replace("-", "_")
    kills_comment = ", ".join(kills) if kills else "none"

    lines: list[str] = []
    lines.append(f"def test_{fn_safe}_{func_name}():")
    lines.append(f'    """')
    lines.append(f'    Test {test_id}: {func_name}')
    lines.append(f'    Kills: {kills_comment}')
    lines.append(f'    """')

    # تعریف متغیرهای ورودی
    for param, value in inputs.items():
        lines.append(f"    {param} = {repr(value)}")

    # فراخوانی با deepcopy برای list parameters (in-place safety)
    args_parts = []
    for param, value in inputs.items():
        if isinstance(value, list):
            args_parts.append(f"{param}=copy.deepcopy({param})")
        else:
            args_parts.append(f"{param}={param}")
    args_str = ", ".join(args_parts)

    lines.append(f"    result = {func_name}({args_str})")

    if expected is not None:
        try:
            exp_val = json.loads(expected)
            exp_repr = repr(exp_val)
            lines.append(f"    assert result == {exp_repr}, (")
            lines.append(
                f'        f"{func_name}({repr(inputs)}) → {{result!r}}, '
                f'expected {exp_repr}"'
            )
            lines.append(f"    )")
        except (json.JSONDecodeError, ValueError):
            lines.append(f"    # Expected (raw): {expected}")
            lines.append(f"    assert result is not None or isinstance(result, (int, float, bool, str))")
    else:
        lines.append(f"    # خروجی مورد انتظار موجود نیست — فقط بررسی می‌کنیم exception نداشت")
        lines.append(f"    assert True")

    lines.append("")
    lines.append("")
    return lines