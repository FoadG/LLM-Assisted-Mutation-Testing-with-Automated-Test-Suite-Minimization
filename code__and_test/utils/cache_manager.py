"""
utils/cache_manager.py
-----------------------
کش لایه برای kill matrix.

کلید کش: MD5 از (source_code + کد Mutant‌ها + ورودی تست‌ها)
ذخیره: JSON در output/cache/{key}.json

این کش از محاسبه مجدد kill matrix در اجراهای متوالی
بدون تغییر کد یا تست‌ها جلوگیری می‌کند.

جلوگیری از خرابی کش:
  - هر تغییر در source_code کلید را تغییر می‌دهد
  - هر تغییر در Mutant‌ها کلید را تغییر می‌دهد
  - هر تغییر در test_pool کلید را تغییر می‌دهد
  - فایل کش ناقص با try/except ایمن خوانده می‌شود
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class KillMatrixCache:
    """
    کش JSON-based برای kill matrix و نتایج اصلی.

    چرا JSON نه Pickle:
      Pickle می‌تواند کد دلخواه هنگام load اجرا کند.
      JSON فقط ساختارهای ساده را می‌پذیرد — ایمن‌تر.

    محدودیت:
      برای kill matrix‌های بسیار بزرگ (>50K cells) ممکن است
      JSON کند باشد. در این صورت به numpy binary یا msgpack مهاجرت کنید.
    """

    def __init__(self, cache_dir: str = "output/cache") -> None:
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    # ── کلید ─────────────────────────────────────────────────────────────────

    def compute_key(
        self,
        source_code: str,
        mutants:    list,         # list[MutantRecord]
        test_pool:  list[dict],
    ) -> str:
        """
        MD5 از (source_code + کد همه Mutant‌ها + ورودی همه تست‌ها).

        ترتیب Mutant‌ها: مرتب بر اساس id (تکرارپذیر)
        ترتیب تست‌ها: همان ترتیب test_pool (ترتیب مهم است)
        """
        h = hashlib.md5(usedforsecurity=False)
        h.update(source_code.encode("utf-8"))

        # Mutant‌ها را با ترتیب ثابت hash می‌کنیم
        for m in sorted(mutants, key=lambda x: x.id):
            h.update(m.code.encode("utf-8"))

        # تست‌ها را با ترتیب موجود hash می‌کنیم (ترتیب تأثیر دارد)
        for t in test_pool:
            try:
                serialized = json.dumps(t, sort_keys=True, ensure_ascii=False,
                                        default=str)
                h.update(serialized.encode("utf-8"))
            except (TypeError, ValueError):
                pass

        return h.hexdigest()

    # ── بارگذاری ─────────────────────────────────────────────────────────────

    def load(
        self,
        key: str,
    ) -> Optional[tuple[list[list[bool]], list[tuple]]]:
        """
        kill matrix و orig_results را از کش بارگذاری می‌کند.

        Returns:
            (kill_matrix, orig_results) یا None اگر کش وجود نداشت یا خراب بود
        """
        path = self._path(key)
        if not os.path.exists(path):
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # اعتبارسنجی ساختار
            if "kill_matrix" not in data or "orig_results" not in data:
                logger.info(f"  [Cache] ساختار کش نامعتبر — حذف: {key[:12]}...")
                self._safe_remove(path)
                return None

            kill_matrix  = data["kill_matrix"]
            orig_results = [tuple(r) for r in data["orig_results"]]

            # بررسی نوع داده
            if not isinstance(kill_matrix, list):
                self._safe_remove(path)
                return None

            logger.info(f"  [Cache] بارگذاری از کش: {key[:12]}... "
                  f"({len(kill_matrix)} تست × "
                  f"{len(kill_matrix[0]) if kill_matrix else 0} Mutant)")
            return kill_matrix, orig_results

        except (json.JSONDecodeError, KeyError, TypeError, IndexError) as e:
            logger.info(f"  [Cache] کش خراب ({key[:12]}...): {e} — حذف می‌شود")
            self._safe_remove(path)
            return None

    # ── ذخیره ─────────────────────────────────────────────────────────────────

    def save(
        self,
        key:          str,
        kill_matrix:  list[list[bool]],
        orig_results: list[tuple],
    ) -> None:
        """
        kill matrix و orig_results را در کش ذخیره می‌کند.

        orig_results[i] = (status: str, output: str|None)
        JSON فقط lists می‌پذیرد (نه tuples)، پس به list تبدیل می‌کنیم.
        """
        path = self._path(key)
        try:
            data = {
                "schema_version": 1,
                "key":            key,
                "created_at":     datetime.now().isoformat(),
                "n_tests":        len(kill_matrix),
                "n_mutants":      len(kill_matrix[0]) if kill_matrix else 0,
                "kill_matrix":    kill_matrix,
                "orig_results":   [list(r) for r in orig_results],
            }
            # ابتدا به فایل موقت بنویس، سپس جابجا کن (atomic write)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
            os.replace(tmp_path, path)
            logger.info(f"  [Cache] ذخیره شد: {key[:12]}... "
                  f"({data['n_tests']} × {data['n_mutants']})")
        except Exception as e:
            logger.info(f"  [Cache] ذخیره ناموفق: {e}")
            self._safe_remove(path + ".tmp")

    # ── حذف ─────────────────────────────────────────────────────────────────

    def invalidate(self, key: str) -> None:
        """یک کش entry را حذف می‌کند."""
        self._safe_remove(self._path(key))

    def clear_all(self) -> int:
        """همه فایل‌های کش را حذف می‌کند. تعداد حذف‌شده را برمی‌گرداند."""
        count = 0
        for fname in os.listdir(self.cache_dir):
            if fname.endswith(".json"):
                self._safe_remove(os.path.join(self.cache_dir, fname))
                count += 1
        return count

    def list_entries(self) -> list[dict]:
        """لیست همه کش entry‌ها با metadata."""
        entries = []
        for fname in os.listdir(self.cache_dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.cache_dir, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                entries.append({
                    "key":        data.get("key", fname[:-5])[:12] + "...",
                    "created_at": data.get("created_at", ""),
                    "n_tests":    data.get("n_tests",   0),
                    "n_mutants":  data.get("n_mutants", 0),
                    "file":       fname,
                })
            except Exception:
                pass
        return sorted(entries, key=lambda x: x["created_at"], reverse=True)

    # ── داخلی ─────────────────────────────────────────────────────────────────

    def _path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.json")

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass