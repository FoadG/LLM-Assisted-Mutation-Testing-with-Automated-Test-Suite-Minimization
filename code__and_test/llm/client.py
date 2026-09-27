"""
llm/client.py
-------------
مسئولیت:
  - تنها نقطه تماس با Anthropic API در کل پروژه
  - اگر مدل یا endpoint تغییر کرد فقط این فایل عوض می‌شود
  - retry خودکار در صورت خطای شبکه یا rate-limit
  - بازگرداندن متن خام (raw text) — parse در فازهای بالاتر

اصلاحات نسخه ۳:
  ─ model name در بلوک __main__ از claude-sonnet-4-20250514
    به claude-sonnet-4-6 اصلاح شد (هماهنگ با config.json).

اصلاحات نسخه ۲:
  ─ Circuit Breaker: اگر N بار پشت سر هم خطای سرور رخ داد،
    درخواست‌های بعدی فوراً رد می‌شوند (fail-fast) بدون انتظار.
  ─ Exponential backoff برای rate-limit (به جای linear)
  ─ جدا کردن خطاهای قابل retry (429, 5xx) از غیرقابل retry (4xx)

رابط ثابت برای بقیه سیستم:
  client = LLMClient(cfg["llm"])
  text   = client.call(prompt, temperature=0.85)
"""

import os
import time
import json
import urllib.request
import urllib.error
from typing import Optional
import logging

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# تنظیمات Circuit Breaker
# ──────────────────────────────────────────────
_CB_FAILURE_THRESHOLD = 5    # تعداد خطاهای متوالی برای باز کردن مدار
_CB_RECOVERY_TIMEOUT  = 120  # ثانیه‌ای که باید منتظر بماند قبل از تست مجدد


class LLMClient:
    """
    کلاینت با Circuit Breaker برای Anthropic Messages API.

    Circuit Breaker سه حالت دارد:
      CLOSED:    وضعیت عادی — همه درخواست‌ها عبور می‌کنند
      OPEN:      پس از N خطای متوالی — همه درخواست‌ها فوراً رد می‌شوند
      HALF-OPEN: پس از recovery_timeout — یک درخواست آزمایشی عبور می‌کند

    Args:
        llm_config: بخش "llm" از config.json
    """

    def __init__(self, llm_config: dict) -> None:
        self.model      = llm_config["model"]
        self.max_tokens = llm_config.get("max_tokens", 1000)
        self.api_base   = llm_config.get(
            "api_base", "https://api.anthropic.com/v1/messages"
        )
        self.api_key    = self._load_api_key()

        # وضعیت Circuit Breaker
        self._cb_failure_count:  int   = 0
        self._cb_open:           bool  = False
        self._cb_open_since:     float = 0.0

    # ──────────────────────────────────────────
    # رابط عمومی
    # ──────────────────────────────────────────

    def call(
        self,
        prompt:      str,
        temperature: float = 0.85,
        max_retries: int   = 3,
        retry_delay: float = 2.0,
    ) -> str:
        """
        یک prompt را به API می‌فرستد و متن خام پاسخ را برمی‌گرداند.

        Circuit Breaker:
          اگر مدار باز باشد (OPEN)، فوراً رشته خالی برمی‌گردد.
          پس از recovery_timeout، یک درخواست آزمایشی (HALF-OPEN) ارسال می‌شود.

        Args:
            prompt:      متن پرامپت
            temperature: خلاقیت (0.0 تا 1.0)
            max_retries: تعداد تلاش مجدد در صورت خطای قابل retry
            retry_delay: ثانیه‌های پایه برای exponential backoff

        Returns:
            متن پاسخ LLM یا رشته خالی در صورت خطا
        """
        # ── بررسی Circuit Breaker ─────────────────────────────────────────────
        if self._cb_open:
            elapsed = time.time() - self._cb_open_since
            if elapsed < _CB_RECOVERY_TIMEOUT:
                remaining = _CB_RECOVERY_TIMEOUT - elapsed
                logger.info(f"  [LLM] مدار باز است — {remaining:.0f}s مانده | رشته خالی")
                return ""
            else:
                # HALF-OPEN: یک درخواست آزمایشی
                logger.info("  [LLM] مدار نیمه‌باز — درخواست آزمایشی...")
                self._cb_open = False

        for attempt in range(1, max_retries + 1):
            try:
                raw  = self._post(prompt, temperature)
                text = self._extract_text(raw)
                # موفقیت → reset Circuit Breaker
                self._cb_failure_count = 0
                return text

            except _RateLimitError:
                # Exponential backoff برای rate-limit
                wait = retry_delay * (2 ** (attempt - 1))
                logger.info(f"  [LLM] rate-limit — انتظار {wait:.0f}s (تلاش {attempt}/{max_retries})")
                time.sleep(wait)

            except _ServerError as e:
                # خطاهای سرور (5xx) — قابل retry، در شمارنده CB ثبت می‌شود
                logger.info(f"  [LLM] خطای سرور: {e} (تلاش {attempt}/{max_retries})")
                self._cb_failure_count += 1
                self._check_circuit_breaker()
                if self._cb_open:
                    logger.info("  [LLM] مدار باز شد — ادامه بدون LLM")
                    return ""
                if attempt < max_retries:
                    time.sleep(retry_delay * attempt)

            except _ClientError as e:
                # خطاهای کلاینت (4xx غیر از 429) — غیرقابل retry
                logger.info(f"  [LLM] خطای کلاینت (غیرقابل retry): {e}")
                return ""

            except _APIError as e:
                # خطای شبکه یا دیگر خطاها — قابل retry
                logger.info(f"  [LLM] خطای شبکه: {e} (تلاش {attempt}/{max_retries})")
                self._cb_failure_count += 1
                self._check_circuit_breaker()
                if self._cb_open:
                    return ""
                if attempt < max_retries:
                    time.sleep(retry_delay)

            except Exception as e:
                logger.info(f"  [LLM] خطای غیرمنتظره: {e} (تلاش {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(retry_delay)

        logger.info("  [LLM] همه تلاش‌ها شکست خوردند — رشته خالی برگردانده می‌شود")
        return ""

    def reset_circuit_breaker(self) -> None:
        """Circuit Breaker را به وضعیت CLOSED برمی‌گرداند."""
        self._cb_failure_count = 0
        self._cb_open          = False
        self._cb_open_since    = 0.0
        logger.info("  [LLM] Circuit Breaker reset شد")

    @property
    def circuit_open(self) -> bool:
        """True اگر مدار فعلاً باز باشد."""
        return self._cb_open

    # ──────────────────────────────────────────
    # پیاده‌سازی داخلی
    # ──────────────────────────────────────────

    def _check_circuit_breaker(self) -> None:
        """وضعیت Circuit Breaker را بررسی و در صورت نیاز باز می‌کند."""
        if self._cb_failure_count >= _CB_FAILURE_THRESHOLD:
            if not self._cb_open:
                self._cb_open       = True
                self._cb_open_since = time.time()
                logger.info(
                    f"  [LLM] ⚡ Circuit Breaker OPEN پس از "
                    f"{self._cb_failure_count} خطای متوالی. "
                    f"بازیابی در {_CB_RECOVERY_TIMEOUT}s"
                )

    def _post(self, prompt: str, temperature: float) -> dict:
        """
        درخواست POST را می‌سازد و ارسال می‌کند.

        Raises:
            _RateLimitError: HTTP 429
            _ServerError:    HTTP 5xx
            _ClientError:    HTTP 4xx غیر از 429
            _APIError:       خطای شبکه
        """
        payload = json.dumps({
            "model":       self.model,
            "max_tokens":  self.max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "user", "content": prompt}
            ],
        }).encode("utf-8")

        req = urllib.request.Request(
            self.api_base,
            data=payload,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429:
                raise _RateLimitError(f"HTTP 429: {body}") from e
            elif 500 <= e.code < 600:
                raise _ServerError(f"HTTP {e.code}: {body}") from e
            else:
                raise _ClientError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise _APIError(f"Network error: {e.reason}") from e
        except TimeoutError as e:
            raise _APIError("Request timeout") from e

    @staticmethod
    def _extract_text(response: dict) -> str:
        """
        متن را از ساختار پاسخ API استخراج می‌کند.

        ساختار مورد انتظار:
          {"content": [{"type": "text", "text": "..."}]}
        """
        try:
            blocks = response.get("content", [])
            texts  = [b["text"] for b in blocks if b.get("type") == "text"]
            return "\n".join(texts).strip()
        except (KeyError, TypeError):
            return str(response)

    @staticmethod
    def _load_api_key() -> str:
        """
        API key را از متغیر محیطی می‌خواند.
        متغیر: ANTHROPIC_API_KEY

        Raises:
            SystemExit: اگر متغیر تنظیم نشده باشد
        """
        import sys
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            logger.error(
                "\n[ERROR] متغیر محیطی ANTHROPIC_API_KEY تنظیم نشده.\n"
                "  راهنما:\n"
                "    Linux/Mac: export ANTHROPIC_API_KEY='sk-ant-...'\n"
                "    Windows:   set ANTHROPIC_API_KEY=sk-ant-...\n"

            )
            sys.exit(1)
        return key


# ──────────────────────────────────────────────
# exception‌های داخلی
# ──────────────────────────────────────────────

class _RateLimitError(Exception):
    """HTTP 429 — نیاز به صبر بیشتر"""

class _ServerError(Exception):
    """HTTP 5xx — خطای سرور، قابل retry"""

class _ClientError(Exception):
    """HTTP 4xx (غیر از 429) — خطای کلاینت، غیرقابل retry"""

class _APIError(Exception):
    """خطاهای شبکه و سایر خطاهای غیرمنتظره"""


# ──────────────────────────────────────────────
# اجرای مستقل برای تست اتصال
# ──────────────────────────────────────────────
if __name__ == "__main__":
    # اصلاح: مدل به‌روز شد (هماهنگ با config.json)
    cfg = {
        "model":      "claude-sonnet-4-6",
        "max_tokens": 100,
        "api_base":   "https://api.anthropic.com/v1/messages",
    }

    client = LLMClient(cfg)
    print("[TEST] ارسال یک prompt ساده...")
    result = client.call(
        'Return only this JSON: [{"arr": [1, 2, 3]}]',
        temperature=0.0,
    )
    print(f"[TEST] پاسخ:\n{result}")
    print(f"[TEST] Circuit Breaker Open: {client.circuit_open}")