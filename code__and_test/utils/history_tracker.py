"""
utils/history_tracker.py
-----------------------
ثبت تاریخچه اجراها و محاسبه روند.

هر اجرا در output/history.json ذخیره می‌شود.
ساختار: لیستی از run records با timestamp و امتیاز.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)

_HISTORY_FILENAME = "history.json"
_MAX_HISTORY_ENTRIES = 500   # جلوگیری از رشد نامحدود فایل


def record_run(
    out_dir:             str,
    adjusted_score:      float,
    raw_score:           float,
    n_killed:            int,
    n_total:             int,
    n_suspected:         int,
    n_alive:             int,
    n_tests_pool:        int,
    n_min_tests:         int,
    runtime_seconds:     float,
    target_files:        list[str],
    model:               str = "",
    extra:               Optional[dict] = None,
) -> dict[str, Any]:
    """
    یک اجرا را در تاریخچه ثبت می‌کند.

    Args:
        out_dir:         پوشه خروجی
        adjusted_score:  امتیاز تنظیم‌شده (0-100)
        raw_score:       امتیاز خام (0-100)
        n_killed:        تعداد Mutant کشته‌شده
        n_total:         تعداد کل Mutant
        n_suspected:     تعداد suspected equivalent
        n_alive:         تعداد زنده‌مانده
        n_tests_pool:    تعداد کل تست‌های استخر
        n_min_tests:     تعداد تست‌های کمینه
        runtime_seconds: زمان اجرا (ثانیه)
        target_files:    فایل(‌های) هدف
        model:           نام مدل LLM
        extra:           داده‌های اضافی اختیاری

    Returns:
        entry: دیکشنری ثبت‌شده
    """
    entry: dict[str, Any] = {
        "timestamp":        datetime.now().isoformat(timespec="seconds"),
        "adjusted_score":   round(adjusted_score,  3),
        "raw_score":        round(raw_score,        3),
        "n_killed":         n_killed,
        "n_total":          n_total,
        "n_suspected":      n_suspected,
        "n_alive":          n_alive,
        "n_tests_pool":     n_tests_pool,
        "n_min_tests":      n_min_tests,
        "runtime_seconds":  round(runtime_seconds, 2),
        "target_files":     [str(f) for f in target_files],
        "model":            model,
    }
    if extra:
        for k, v in extra.items():
            if k not in entry:
                entry[k] = v

    history = load_history(out_dir)
    history.append(entry)

    # از رشد نامحدود جلوگیری می‌کنیم
    if len(history) > _MAX_HISTORY_ENTRIES:
        history = history[-_MAX_HISTORY_ENTRIES:]

    _save_history(out_dir, history)
    return entry


def load_history(out_dir: str) -> list[dict]:
    """تاریخچه را بارگذاری می‌کند. لیست خالی اگر فایل نبود."""
    path = os.path.join(out_dir, _HISTORY_FILENAME)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def get_trend(history: list[dict]) -> dict[str, Any]:
    """
    آمار روند از تاریخچه اجراها محاسبه می‌کند.

    Returns:
        dict: شامل runs، latest_score، best_score، worst_score،
              avg_score، scores (10 آخر)، trend (improving/declining/stable)
    """
    if not history:
        return {
            "runs":    0,
            "trend":   "no_data",
            "scores":  [],
        }

    scores = [h["adjusted_score"] for h in history if "adjusted_score" in h]
    if not scores:
        return {"runs": len(history), "trend": "no_data", "scores": []}

    n = len(scores)
    result: dict[str, Any] = {
        "runs":          n,
        "latest_score":  scores[-1],
        "best_score":    max(scores),
        "worst_score":   min(scores),
        "avg_score":     round(sum(scores) / n, 2),
        "scores":        scores[-10:],   # 10 آخر برای نمودار
        "timestamps":    [h.get("timestamp", "") for h in history[-10:]],
    }

    if n >= 2:
        delta = scores[-1] - scores[-2]
        if delta > 1.0:
            result["trend"] = "improving"
        elif delta < -1.0:
            result["trend"] = "declining"
        else:
            result["trend"] = "stable"
    else:
        result["trend"] = "insufficient_data"

    return result


def clear_history(out_dir: str) -> None:
    """تاریخچه را پاک می‌کند."""
    path = os.path.join(out_dir, _HISTORY_FILENAME)
    if os.path.exists(path):
        os.remove(path)


# ── داخلی ─────────────────────────────────────────────────────────────────────

def _save_history(out_dir: str, history: list[dict]) -> None:
    path = os.path.join(out_dir, _HISTORY_FILENAME)
    tmp  = path + ".tmp"
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except OSError as e:
        logger.info(f"  [History] خطا در ذخیره: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass