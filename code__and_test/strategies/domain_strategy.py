"""
strategies/domain_strategy.py  —  استراتژی C
=============================================
حالات ویژه دامنه‌محور برای الگوریتم‌های sort و search.

این تست‌ها حالاتی را پوشش می‌دهند که ACOC و LLM از دستشان می‌دهند:

Sort:
  - لیست خالی، یک عنصر، دو عنصر نامرتب
  - همه یکسان، تقریباً یکسان
  - کاملاً مرتب، کاملاً معکوس
  - اعداد منفی + مثبت، تکراری تصادفی
  - مرزهای عددی بزرگ

Search:
  - target در ابتدا، وسط، انتها
  - target وجود ندارد (قبل از مرز چپ، بعد از مرز راست، بین دو عنصر)
  - آرایه خالی، یک عنصر (پیدا / نپیدا)
  - همه عناصر یکسان
  - اعداد منفی + صفر
  - آرایه بزرگ (۱۰۰ عنصر)

خروجی: [{"function": str, "inputs": {...}}]
"""

from __future__ import annotations

import copy

# ── حالات ویژه sort ──────────────────────────────────────────────────────────
# هر آیتم یک مقدار arr است
_SORT_ARRAYS: list[list] = [
    [],                                        # خالی
    [1],                                       # یک عنصر
    [2, 1],                                    # دو عنصر نامرتب
    [1, 2],                                    # دو عنصر مرتب
    [1, 1, 1, 1],                              # همه یکسان
    [1, 2, 3, 4, 5],                           # کاملاً مرتب
    [5, 4, 3, 2, 1],                           # کاملاً معکوس
    [-3, 0, 2, 5],                             # منفی + مثبت
    [3, 1, 4, 1, 5, 9, 2, 6],                 # تکراری تصادفی
    [0, 0, 0, 0, 1],                           # تقریباً یکسان + یکی متفاوت
    [1, 0, 0, 0, 0],                           # یکی در ابتدا + بقیه صفر
    [-100, 0, 100],                            # مرزهای عددی
    [5, 5, 3, 3, 1, 1],                        # جفت‌های تکراری
    [10, -10, 5, -5, 0],                       # ترکیب منفی/مثبت/صفر
]

# ── حالات ویژه search ─────────────────────────────────────────────────────────
# هر آیتم یک dict است {arr, target}
_SEARCH_CASES: list[dict] = [
    # target در آرایه وجود دارد
    {"arr": [1, 3, 5, 7, 9],     "target": 5},    # وسط
    {"arr": [1, 3, 5, 7, 9],     "target": 1},    # اول
    {"arr": [1, 3, 5, 7, 9],     "target": 9},    # آخر
    {"arr": [2, 4, 6, 8, 10],    "target": 6},    # وسط (اندیس ۲)
    {"arr": [1, 2, 3, 4, 5],     "target": 2},    # دومی از اول

    # target در آرایه وجود ندارد
    {"arr": [1, 3, 5, 7, 9],     "target": 4},    # بین دو عنصر
    {"arr": [1, 3, 5, 7, 9],     "target": 0},    # قبل از مرز چپ
    {"arr": [1, 3, 5, 7, 9],     "target": 99},   # بعد از مرز راست
    {"arr": [2, 4, 6, 8],        "target": 5},    # فرد در آرایه زوج

    # آرایه خالی و تکی
    {"arr": [],                  "target": 1},    # خالی
    {"arr": [5],                 "target": 5},    # یک عنصر — پیدا
    {"arr": [5],                 "target": 3},    # یک عنصر — نپیدا

    # حالات ویژه
    {"arr": [1, 1, 1, 1, 1],    "target": 1},    # همه یکسان — پیدا
    {"arr": [1, 1, 1, 1, 1],    "target": 2},    # همه یکسان — نپیدا
    {"arr": [-5, -1, 0, 3, 7],  "target": 0},    # شامل منفی و صفر — پیدا
    {"arr": [-5, -1, 0, 3, 7],  "target": -1},   # شامل منفی — پیدا
    {"arr": [-5, -1, 0, 3, 7],  "target": 2},    # شامل منفی — نپیدا
    {"arr": list(range(1, 101)), "target": 50},   # آرایه ۱۰۰ عنصری
    {"arr": list(range(1, 101)), "target": 1},    # آرایه ۱۰۰ عنصری — اول
    {"arr": list(range(1, 101)), "target": 100},  # آرایه ۱۰۰ عنصری — آخر
    {"arr": list(range(1, 101)), "target": 101},  # آرایه ۱۰۰ عنصری — نپیدا
]


def generate(config: dict) -> list[dict]:
    """
    برای هر تابع، حالات ویژه دامنه‌محور را تولید می‌کند.

    Args:
        config: dict کامل config.json

    Returns:
        لیستی از TestRecord
    """
    tests: list[dict] = []

    for func in config.get("functions", []):
        fn_name  = func["name"]
        fn_type  = func.get("type", "")
        params   = func.get("params", [])

        if fn_type == "sort":
            arr_param = _find_param(params, ["arr", "array", "lst", "data", "nums"])
            if not arr_param and params:
                arr_param = params[0]["name"]
            if not arr_param:
                continue

            for arr_val in _SORT_ARRAYS:
                tests.append({
                    "function": fn_name,
                    "inputs":   {arr_param: copy.deepcopy(arr_val)},
                })

        elif fn_type == "search":
            arr_key    = _find_param(params, ["arr", "array", "lst", "data", "nums"])
            target_key = _find_param(params, ["target", "key", "x", "val", "value"])

            # fallback اگر نام‌های استاندارد پیدا نشد
            if not arr_key and len(params) >= 1:
                arr_key = params[0]["name"]
            if not target_key and len(params) >= 2:
                target_key = params[1]["name"]

            if not arr_key or not target_key:
                continue

            for case in _SEARCH_CASES:
                tests.append({
                    "function": fn_name,
                    "inputs": {
                        arr_key:    copy.deepcopy(case["arr"]),
                        target_key: case["target"],
                    },
                })

    return tests


def _find_param(params: list[dict], candidates: list[str]) -> str:
    """
    اولین پارامتری که نامش (به شکل lowercase) در candidates است را پیدا می‌کند.
    رشته خالی برمی‌گرداند اگر هیچ‌کدام پیدا نشد.
    """
    name_map = {p["name"].lower(): p["name"] for p in params}
    for c in candidates:
        if c in name_map:
            return name_map[c]
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# اجرای مستقل برای تست
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    with open("config.json", encoding="utf-8") as f:
        cfg = json.load(f)

    pool = generate(cfg)
    print(f"[Domain] {len(pool)} تست تولید شد")

    from collections import Counter
    counts = Counter(t["function"] for t in pool)
    for fn, cnt in counts.items():
        print(f"  {fn}: {cnt} تست")

    # نمونه ۵ تست اول از هر نوع
    for fn_type in ["bubble_sort", "binary_search"]:
        fn_tests = [t for t in pool if t["function"] == fn_type][:3]
        print(f"\n  نمونه {fn_type}:")
        for t in fn_tests:
            print(f"    {t['inputs']}")