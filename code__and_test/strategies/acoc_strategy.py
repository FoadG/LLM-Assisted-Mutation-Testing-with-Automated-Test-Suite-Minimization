"""
strategies/acoc_strategy.py  —  استراتژی A
===========================================
تولید تست با روش ACOC (All Combinations of Classes).

ACOC = Cartesian Product تمام partition‌های هر پارامتر.
با سقف max_per_function از انفجار ترکیباتی جلوگیری می‌شود.

اصلاح نسخه ۲ — رفع Bias در islice:
  نسخه قبل از islice(product(...), max) استفاده می‌کرد که باعث می‌شد
  همیشه ترکیبات ابتدایی (اعداد اول پارتیشن‌ها) انتخاب شوند.
  اگر پارتیشن‌ها به ترتیب [[], [1], [1,2,3], [5,4,3], ...] بودند،
  ترکیبات انتهایی هرگز دیده نمی‌شدند.

  راه‌حل: اگر تعداد ترکیبات <= max بود، همه را برگردان.
           اگر بیشتر بود، با random.sample نمونه‌گیری تصادفی کن.
  محدودیت: برای product‌های خیلی بزرگ (>10000)، از sampling مستقیم
           با random.choices استفاده می‌شود تا از materialize کردن
           همه ترکیبات در RAM جلوگیری شود.

خروجی: [{"function": str, "inputs": {param: value, ...}}]
"""

from __future__ import annotations

import copy
import random
from itertools import product
from typing import Iterator
import logging

logger = logging.getLogger(__name__)


# آستانه برای sampling مستقیم (بدون materialize کردن همه ترکیبات)
_LARGE_PRODUCT_THRESHOLD = 10_000


def generate(config: dict, seed: int = 42) -> list[dict]:
    """
    برای هر تابع، Cartesian Product partition‌های پارامترها را می‌سازد.

    Args:
        config: dict کامل config.json
        seed:   random seed برای تکرارپذیری

    Returns:
        لیستی از TestRecord: [{"function": ..., "inputs": {...}}]
    """
    functions  = config.get("functions",  [])
    partitions = config.get("partitions", {})
    pool_cfg   = config.get("test_pool",  {})
    max_per_fn = pool_cfg.get("acoc_max_per_function", 200)

    rng   = random.Random(seed)
    tests: list[dict] = []

    for func in functions:
        fn_name = func["name"]
        params  = func.get("params", [])

        if not params:
            continue

        missing = [
            p["name"]
            for p in params
            if p.get("type") not in partitions
        ]
        if missing:
            logger.info(
                f"  [ACOC] هشدار: partition برای {missing} در "
                f"'{fn_name}' وجود ندارد — تابع رد می‌شود."
            )
            continue

        param_names = [p["name"]  for p in params]
        param_parts = [partitions[p["type"]] for p in params]

        # محاسبه تعداد کل ترکیب‌های ممکن
        total_possible = 1
        for part in param_parts:
            total_possible *= len(part)

        if total_possible <= max_per_fn:
            # همه ترکیبات — بدون bias
            combos = list(product(*param_parts))
        elif total_possible <= _LARGE_PRODUCT_THRESHOLD:
            # materialize می‌کنیم و sample می‌گیریم
            all_combos = list(product(*param_parts))
            combos = rng.sample(all_combos, max_per_fn)
            logger.info(
                f"  [ACOC] '{fn_name}': {total_possible} ترکیب ممکن، "
                f"random.sample → {max_per_fn}"
            )
        else:
            # محصول خیلی بزرگ — direct sampling (بدون materialize کردن همه)
            combos = [
                tuple(rng.choice(part) for part in param_parts)
                for _ in range(max_per_fn)
            ]
            logger.info(
                f"  [ACOC] '{fn_name}': {total_possible} ترکیب ممکن (خیلی بزرگ)، "
                f"direct random sampling → {max_per_fn}"
            )

        for combo in combos:
            tests.append({
                "function": fn_name,
                "inputs":   dict(zip(param_names, _deep_copy_combo(combo))),
            })

    return tests


def _deep_copy_combo(combo: tuple) -> tuple:
    """
    مقادیر tuple را deep copy می‌کند.
    ضروری برای ایزوله‌سازی لیست‌ها بین تست‌ها:
    bubble_sort([3,1,2]) آرایه را in-place مرتب می‌کند؛
    بدون deep copy، تمام تست‌هایی که همان شیء را share می‌کنند تغییر می‌بینند.
    """
    return tuple(copy.deepcopy(v) for v in combo)


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
    print(f"[ACOC] {len(pool)} تست تولید شد")

    # بررسی توزیع — باید یکنواخت‌تر از islice باشد
    from collections import Counter
    counts = Counter(t["function"] for t in pool)
    for fn, cnt in counts.items():
        print(f"  {fn}: {cnt} تست")

    # بررسی deep copy
    arr_tests = [t for t in pool if "arr" in t.get("inputs", {}) and t["inputs"]["arr"]]
    if arr_tests:
        t0 = arr_tests[0]
        t0["inputs"]["arr"].append(999)
        leaked = sum(
            1 for t in arr_tests[1:]
            if t["inputs"].get("arr") == t0["inputs"]["arr"]
        )
        print(f"  deep_copy check: {leaked} نشت (باید 0 باشد)")

    # تست bias: بررسی کن که ترکیبات انتهایی هم وجود دارند
    print("\n  نمونه ۵ تست اول:")
    for t in pool[:5]:
        fn  = t["function"]
        inp = t["inputs"]
        print(f"    {fn}({', '.join(f'{k}={v!r}' for k, v in inp.items())})")