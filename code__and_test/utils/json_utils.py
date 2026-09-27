"""
utils/json_utils.py
-------------------
مسئولیت:
  - safe_parse_json : استخراج و اعتبارسنجی JSON از خروجی خام LLM
  - deduplicate     : حذف تست‌های تکراری با hash-based comparison
  - test_hash       : هش یکتا برای یک TestRecord (public — برای feedback_loop)

اصلاحات نسخه ۳:
  ─ test_hash (قبلاً _test_hash) به public تبدیل شد تا feedback_loop بتواند
    new_tests را در برابر all_tests موجود فیلتر کند.
  ─ Single-quote JSON: LLM اغلب {'arr': [1,2]} می‌نویسد نه {"arr": [1,2]}
    json.loads فقط double quote می‌پذیرد → همه آن‌ها بی‌صدا drop می‌شدند.
    راه‌حل: fallback به ast.literal_eval برای دیکشنری‌های Python‌ای.
  ─ bracket-matching واقعی جایگزین regex غیر-greedy شده (نسخه قبل).
"""

from __future__ import annotations

import ast as _py_ast
import hashlib
import json
import re
from typing import Any, Optional


# ─────────────────────────────────────────────
# safe_parse_json
# ─────────────────────────────────────────────

def safe_parse_json(raw_text: str) -> Optional[list[dict]]:
    """
    JSON را از خروجی خام LLM استخراج و اعتبارسنجی می‌کند.

    LLM اغلب متن اضافه یا Single Quote می‌نویسد:
      "Here is the JSON: [{'arr': [1,2,3]}] Hope this helps!"

    راه‌حل ۴ مرحله‌ای:
      ۱. bracket-matching برای یافتن اولین  [...]  کامل
      ۲. bracket-matching برای یافتن اولین  {...}  کامل  → wrap در لیست
      ۳. parse کل متن (بعد از حذف code fence)
      ۴. fallback: ast.literal_eval برای دیکشنری‌های Python‌ای با single quote

    Args:
        raw_text: متن خام از LLM

    Returns:
        لیستی از dict ها یا None اگر parse ناموفق بود
    """
    if not raw_text or not raw_text.strip():
        return None

    # تلاش ۱: یافتن آرایه JSON با bracket-matching
    raw_array = _extract_by_bracket(raw_text, open_ch='[', close_ch=']')
    if raw_array is not None and isinstance(raw_array, list):
        validated = [d for d in raw_array if isinstance(d, dict)]
        if validated:
            return validated

    # تلاش ۲: یافتن object JSON با bracket-matching → wrap در لیست
    raw_obj = _extract_by_bracket(raw_text, open_ch='{', close_ch='}')
    if raw_obj is not None and isinstance(raw_obj, dict):
        return [raw_obj]

    # تلاش ۳: کل متن (بعد از strip و حذف code fence)
    result = _try_parse_whole(raw_text)
    if result:
        return result

    # تلاش ۴: ast.literal_eval — برای خروجی‌های Python‌ای با single quote
    # مثل: [{'arr': [1, 2, 3]}, {'arr': [4, 5]}]
    result = _try_literal_eval(raw_text)
    return result


def _extract_by_bracket(
    text:     str,
    open_ch:  str,
    close_ch: str,
) -> Optional[Any]:
    """
    اولین بلوک کامل open_ch...close_ch را با bracket-matching پیدا می‌کند.

    برخلاف regex غیر-greedy، این تابع نستینگ را به درستی رعایت می‌کند.
    مثال: در  '[{"arr": [1,2,3]}, {"x": 4}]'
          regex غیر-greedy  [1,2,3]  را برمی‌گرداند (اشتباه)
          این تابع کل  [{"arr":[1,2,3]},{"x":4}]  را برمی‌گرداند (درست)
    """
    start = text.find(open_ch)
    if start == -1:
        return None

    depth        = 0
    in_string    = False
    escape_next  = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape_next:
            escape_next = False
            continue

        if ch == '\\' and in_string:
            escape_next = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                candidate = text[start: i + 1]
                try:
                    return json.loads(candidate)
                except (json.JSONDecodeError, ValueError):
                    # شاید single-quote باشد
                    return _safe_literal(candidate)

    return None


def _try_parse_whole(text: str) -> Optional[list[dict]]:
    """کل متن را (بعد از strip و حذف code fence) parse می‌کند."""
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:json|python)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)] or None
        if isinstance(data, dict):
            return [data]
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _try_literal_eval(text: str) -> Optional[list[dict]]:
    """
    از ast.literal_eval برای دیکشنری‌های Python‌ای با single quote استفاده می‌کند.
    ایمن است: فقط literals پایتون (dict, list, str, int, float, bool, None) می‌پذیرد.
    هیچ کدی اجرا نمی‌کند.
    """
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:json|python)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    cleaned = cleaned.strip()

    for start_ch, end_ch in (('[', ']'), ('{', '}')):
        idx = cleaned.find(start_ch)
        if idx == -1:
            continue
        depth       = 0
        in_str      = False
        str_ch      = None
        esc         = False
        for i in range(idx, len(cleaned)):
            c = cleaned[i]
            if esc:
                esc = False
                continue
            if c == '\\' and in_str:
                esc = True
                continue
            if in_str:
                if c == str_ch:
                    in_str = False
                continue
            if c in ('"', "'"):
                in_str = True
                str_ch = c
                continue
            if c == start_ch:
                depth += 1
            elif c == end_ch:
                depth -= 1
                if depth == 0:
                    candidate = cleaned[idx: i + 1]
                    try:
                        data = _py_ast.literal_eval(candidate)
                        if isinstance(data, list):
                            validated = [d for d in data if isinstance(d, dict)]
                            if validated:
                                return validated
                        elif isinstance(data, dict):
                            return [data]
                    except (ValueError, SyntaxError):
                        pass
                    break

    return None


def _safe_literal(text: str) -> Optional[Any]:
    """ast.literal_eval با پوشش خطا."""
    try:
        return _py_ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


# ─────────────────────────────────────────────
# deduplicate
# ─────────────────────────────────────────────

def deduplicate(tests: list[dict]) -> list[dict]:
    """
    تست‌های تکراری را با hash-based comparison حذف می‌کند.

    دو تست یکسان محسوب می‌شوند اگر:
      - نام تابع یکسان باشد
      - ورودی‌ها (inputs) بعد از json.dumps با sort_keys یکسان باشند

    Args:
        tests: لیستی از TestRecord دیکشنری‌ها

    Returns:
        لیست بدون تکراری (ترتیب اول ظهور حفظ می‌شود)
    """
    seen:   set[str]   = set()
    unique: list[dict] = []

    for test in tests:
        key = test_hash(test)
        if key not in seen:
            seen.add(key)
            unique.append(test)

    return unique


# ─────────────────────────────────────────────
# test_hash  (public — قبلاً _test_hash بود)
# ─────────────────────────────────────────────

def test_hash(test: dict) -> str:
    """
    هش یکتا برای یک TestRecord می‌سازد.

    Public شده تا feedback_loop بتواند new_tests را در برابر
    all_tests موجود فیلتر کند و از اضافه‌شدن تست‌های تکراری جلوگیری کند.

    Args:
        test: dict با کلیدهای "function" و "inputs"

    Returns:
        رشته MD5 hex
    """
    normalized = {
        "function": test.get("function", ""),
        "inputs":   _deep_sort(test.get("inputs", {})),
    }
    serialized = json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                            default=str)
    return hashlib.md5(serialized.encode("utf-8")).hexdigest()


def _deep_sort(obj: Any) -> Any:
    """
    اشیاء تودرتو را به شکلی که قابل مقایسه باشند normalize می‌کند.
    لیست‌ها مرتب نمی‌شوند (ترتیب لیست برای آرایه‌های ورودی مهم است).
    """
    if isinstance(obj, dict):
        return {k: _deep_sort(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_deep_sort(i) for i in obj]
    return obj


# ─────────────────────────────────────────────
# helper: نمایش خوانا از TestRecord
# ─────────────────────────────────────────────

def test_to_str(test: dict) -> str:
    func   = test.get("function", "?")
    inputs = test.get("inputs",   {})
    return f"{func}({', '.join(f'{k}={v!r}' for k, v in inputs.items())})"


# ─────────────────────────────────────────────
# اجرای مستقل برای تست
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=== safe_parse_json ===")
    cases = [
        ('[{"arr": [1,2,3]}]',                              "آرایه تمیز"),
        ('Here is JSON: [{"x": 1}] Thanks!',                "آرایه با متن اضافه"),
        ('[{"arr": [1,2,3]}, {"arr": [4,5]}]',              "دو آیتم + نستینگ"),
        ('{"arr": [1,2]}',                                  "object تکی"),
        ('```json\n[{"a":1}]\n```',                         "code fence"),
        ("garbage text",                                     "garbage → None"),
        ('[1, 2, 3]',                                       "آرایه غیر-dict → None"),
        ('',                                                 "رشته خالی → None"),
        ('[{"arr":[1,2,3]},{"arr":[4,5,6]}]',               "دو آیتم تودرتو"),
        ("[{'arr': [1, 2, 3]}, {'arr': [4, 5]}]",           "single-quote Python"),
        ("{'key': 'value', 'num': 42}",                     "single-quote object"),
        ("[{'x': 1}, {'x': 2}]",                            "single-quote آرایه"),
    ]
    for text, label in cases:
        result = safe_parse_json(text)
        status = "✓" if (result is not None) == ("None" not in label) else "✗"
        print(f"  {status} [{label}]\n    → {result}")

    print("\n=== deduplicate ===")
    tests = [
        {"function": "bubble_sort",   "inputs": {"arr": [1, 2, 3]}},
        {"function": "bubble_sort",   "inputs": {"arr": [1, 2, 3]}},
        {"function": "bubble_sort",   "inputs": {"arr": [3, 2, 1]}},
        {"function": "binary_search", "inputs": {"arr": [1, 2, 3], "target": 2}},
    ]
    unique = deduplicate(tests)
    print(f"  ورودی: {len(tests)} → خروجی: {len(unique)} یکتا")
    for t in unique:
        print(f"    {test_to_str(t)}")

    print("\n=== test_hash (public) ===")
    t1 = {"function": "f", "inputs": {"arr": [1, 2, 3]}}
    t2 = {"function": "f", "inputs": {"arr": [1, 2, 3]}}
    t3 = {"function": "f", "inputs": {"arr": [3, 2, 1]}}
    print(f"  t1 hash: {test_hash(t1)}")
    print(f"  t2 hash: {test_hash(t2)}  (باید = t1)")
    print(f"  t3 hash: {test_hash(t3)}  (باید ≠ t1)")
    print(f"  t1==t2: {test_hash(t1) == test_hash(t2)}  (باید True)")
    print(f"  t1==t3: {test_hash(t1) == test_hash(t3)}  (باید False)")