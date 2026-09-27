"""
utils/config_loader.py
----------------------
مسئولیت:
  - خواندن config.json و اعتبارسنجی کلیدهای اجباری
  - fallback تعاملی: اگر بخشی از config ناقص بود از کاربر می‌پرسد
  - ذخیره‌ی پاسخ‌های کاربر در config برای اجراهای بعدی

اصلاحات نسخه ۲:
  ─ حالت غیرتعاملی (non-interactive/CI): اگر NON_INTERACTIVE=true باشد
    یا ورودی از ترمینال نباشد (stdin در CI/Docker)، به جای input()
    فوراً با پیام واضح خطا می‌دهد.
  ─ تابع load_config یک پارامتر non_interactive دریافت می‌کند.
  ─ تشخیص خودکار محیط CI: اگر stdin یک TTY نباشد → non-interactive.

استفاده:
  from utils.config_loader import load_config
  cfg = load_config("config.json")
  cfg = load_config("config.json", non_interactive=True)   # برای CI
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
import logging

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# کلیدهای اجباری
# ─────────────────────────────────────────────
REQUIRED_TOP_KEYS = [
    "project",
    "functions",
    "partitions",
    "mutation",
    "test_pool",
    "sandbox",
    "optimizer",
    "llm",
]

KNOWN_PARAM_TYPES = [
    "list_int",
    "sorted_list_int",
    "int",
    "float",
    "str",
    "bool",
]

KNOWN_FUNC_TYPES = ["sort", "search"]


# ─────────────────────────────────────────────
# تشخیص محیط CI
# ─────────────────────────────────────────────

def _is_ci_environment() -> bool:
    """
    بررسی می‌کند که آیا در محیط غیرتعاملی اجرا می‌شویم.

    شرایط تشخیص CI/غیرتعاملی:
      ۱. متغیر محیطی CI=true (GitHub Actions, Travis, Jenkins)
      ۲. متغیر محیطی NON_INTERACTIVE=true
      ۳. stdin یک TTY نیست (Docker، pipe، cron)
    """
    if os.environ.get("CI", "").lower() in ("true", "1", "yes"):
        return True
    if os.environ.get("NON_INTERACTIVE", "").lower() in ("true", "1", "yes"):
        return True
    if not sys.stdin.isatty():
        return True
    return False


# ─────────────────────────────────────────────
# توابع اصلی
# ─────────────────────────────────────────────

def load_config(path: str, non_interactive: bool = False) -> dict:
    """
    config.json را می‌خواند، اعتبارسنجی می‌کند، و در صورت ناقص بودن
    از طریق CLI از کاربر می‌پرسد (در صورتی که تعاملی باشیم).

    Args:
        path:            مسیر فایل config.json
        non_interactive: اگر True باشد، هیچ‌وقت input() نمی‌زند.
                         در محیط CI به صورت خودکار True می‌شود.

    Returns:
        dict: config کامل و معتبر

    Raises:
        SystemExit: اگر فایل پیدا نشود، JSON خراب باشد،
                    یا در حالت non-interactive config ناقص باشد
    """
    # تشخیص خودکار محیط CI
    if not non_interactive:
        non_interactive = _is_ci_environment()

    config = _read_json(path)
    _validate_required_keys(config, path)
    config = _ensure_functions_have_types(config, path, non_interactive)
    config = _ensure_partitions_exist(config, path)
    _validate_output_dir(config)
    _validate_project_root(config, path)
    return config


def save_config(config: dict, path: str) -> None:
    """config را روی دیسک ذخیره می‌کند."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    logger.info(f"  [config] ذخیره شد: {path}")


# ─────────────────────────────────────────────
# توابع داخلی
# ─────────────────────────────────────────────

def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        _fatal(
            f"فایل config پیدا نشد: {path}\n"
            f"  راهنما: یک فایل config.json بسازید یا مسیر را درست کنید."
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        _fatal(f"فایل config.json ساختار JSON معتبر ندارد:\n  {e}")


def _validate_required_keys(config: dict, path: str) -> None:
    missing = [k for k in REQUIRED_TOP_KEYS if k not in config]
    if missing:
        _fatal(
            f"کلیدهای زیر در config.json وجود ندارند: {missing}\n"
            f"  فایل: {path}"
        )


def _ensure_functions_have_types(
    config: dict,
    config_path: str,
    non_interactive: bool,
) -> dict:
    """
    اگر تابعی نوع param یا نوع function (sort/search) نداشت:
      - در حالت تعاملی: از کاربر می‌پرسد
      - در حالت non-interactive: فوراً خطای واضح می‌دهد
    """
    changed = False

    for func in config.get("functions", []):
        fn_name = func.get("name", "?")

        if "type" not in func or func["type"] not in KNOWN_FUNC_TYPES:
            if non_interactive:
                _fatal(
                    f"نوع تابع '{fn_name}' در config.json مشخص نیست.\n"
                    f"  لطفاً فیلد 'type' را با یکی از {KNOWN_FUNC_TYPES} تنظیم کنید.\n"
                    f"  محیط non-interactive: امکان پرسش از کاربر وجود ندارد."
                )
            func["type"] = _ask_function_type(fn_name)
            changed = True

        for param in func.get("params", []):
            if "type" not in param or param["type"] not in KNOWN_PARAM_TYPES:
                if non_interactive:
                    _fatal(
                        f"نوع پارامتر '{param.get('name','?')}' در تابع '{fn_name}' "
                        f"در config.json مشخص نیست.\n"
                        f"  لطفاً فیلد 'type' را با یکی از {KNOWN_PARAM_TYPES} تنظیم کنید.\n"
                        f"  محیط non-interactive: امکان پرسش از کاربر وجود ندارد."
                    )
                param["type"] = _ask_param_type(fn_name, param["name"])
                changed = True

    if changed:
        _prompt_save(config, config_path, non_interactive)

    return config


def _ensure_partitions_exist(config: dict, config_path: str) -> dict:
    """
    بررسی می‌کند که هر نوع پارامتر در partitions تعریف شده باشد.
    """
    partitions = config.get("partitions", {})

    for func in config.get("functions", []):
        fn_name = func.get("name", "?")
        for param in func.get("params", []):
            ptype = param.get("type", "")
            pname = param.get("name", "?")

            if ptype not in partitions:
                _fatal(
                    f"partition برای نوع '{ptype}' (پارامتر '{pname}' "
                    f"در تابع '{fn_name}') در config.json تعریف نشده.\n"
                    f"  لطفاً زیر کلید 'partitions' آن را اضافه کنید."
                )

            part = partitions[ptype]
            if not isinstance(part, list) or len(part) == 0:
                _fatal(
                    f"partition برای نوع '{ptype}' باید یک لیست غیرخالی باشد.\n"
                    f"  مقدار فعلی: {part!r}"
                )

    return config


def _validate_output_dir(config: dict) -> None:
    out = config.get("project", {}).get("output_dir", "output/")
    os.makedirs(out, exist_ok=True)


# ─────────────────────────────────────────────
# N1.1: optional project-root configuration
# ─────────────────────────────────────────────
# Defaults for the discovery knobs the N1.4 ProjectModel will consume. They are
# stored on the config ONLY when project.root is present; when root is absent
# this validator is a strict no-op, so the V2 single-file path (and the golden
# result) is byte-identical. (Knob names/location were a delegated NEW DESIGN
# DECISION in the approved N1 design; realized here under project.discovery.)
_DISCOVERY_DEFAULTS = {
    "include":  ["**/*.py"],
    "exclude":  ["__pycache__", ".*", "venv", ".venv", "site-packages"],
    "test_dir": "tests",
}


def _validate_project_root(config: dict, path: str) -> None:
    """Validate and normalize the OPTIONAL project.root block.

    No-op when project.root is absent (preserves the single-file path exactly).
    When present: root must be an existing directory; discovery knobs are
    type-checked and missing ones filled with _DISCOVERY_DEFAULTS. Nothing
    consumes these values yet — discovery itself arrives in N1.4."""
    project = config.get("project", {})
    if not isinstance(project, dict) or "root" not in project:
        return  # single-file mode — untouched

    root = project.get("root")
    if not isinstance(root, str) or not root.strip():
        _fatal(f"project.root باید یک مسیر رشته‌ای معتبر باشد.\n  فایل: {path}")
    if not os.path.isdir(root):
        _fatal(
            f"project.root یک پوشه‌ی موجود نیست: {root}\n"
            f"  راهنما: مسیر ریشه‌ی پروژه را درست کنید.\n  فایل: {path}"
        )

    disc = project.get("discovery", {})
    if not isinstance(disc, dict):
        _fatal(f"project.discovery باید یک شیء (dict) باشد.\n  فایل: {path}")
    for list_key in ("include", "exclude"):
        if list_key in disc and not isinstance(disc[list_key], list):
            _fatal(f"project.discovery.{list_key} باید لیست باشد.\n  فایل: {path}")
    if "test_dir" in disc and not isinstance(disc["test_dir"], str):
        _fatal(f"project.discovery.test_dir باید رشته باشد.\n  فایل: {path}")

    for k, default in _DISCOVERY_DEFAULTS.items():
        disc.setdefault(k, list(default) if isinstance(default, list) else default)
    project["discovery"] = disc
    config["project"] = project


# ─────────────────────────────────────────────
# CLI helpers (فقط در حالت تعاملی)
# ─────────────────────────────────────────────

def _ask_function_type(func_name: str) -> str:
    logger.info(f"\n  [config] نوع تابع '{func_name}' مشخص نیست.")
    while True:
        try:
            ans = input(f"    نوع (یکی از {KNOWN_FUNC_TYPES})؟  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _fatal("ورودی منقطع شد.")
        if ans in KNOWN_FUNC_TYPES:
            return ans
        logger.info(f"    لطفاً یکی از {KNOWN_FUNC_TYPES} را وارد کنید.")


def _ask_param_type(func_name: str, param_name: str) -> str:
    logger.info(f"\n  [config] نوع پارامتر '{param_name}' در تابع '{func_name}' مشخص نیست.")
    logger.info(f"    گزینه‌ها: {', '.join(KNOWN_PARAM_TYPES)}")
    while True:
        try:
            ans = input("    نوع را وارد کنید > ").strip()
        except (EOFError, KeyboardInterrupt):
            _fatal("ورودی منقطع شد.")
        if ans in KNOWN_PARAM_TYPES:
            return ans
        logger.info(f"    باید یکی از: {KNOWN_PARAM_TYPES}")


def _prompt_save(config: dict, path: str, non_interactive: bool) -> None:
    if non_interactive:
        return
    try:
        ans = input("\n  config تکمیل شد. ذخیره در فایل؟ [Y/n] > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans in ("", "y", "yes", "بله"):
        save_config(config, path)


# ─────────────────────────────────────────────
# helper عمومی
# ─────────────────────────────────────────────

def _fatal(msg: str) -> None:
    logger.error(f"\n[ERROR] {msg}\n")
    sys.exit(1)


# ─────────────────────────────────────────────
# اجرای مستقل برای تست
# ─────────────────────────────────────────────
if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    # non_interactive از آرگومان یا متغیر محیطی
    ni = "--ci" in sys.argv or "--non-interactive" in sys.argv
    cfg = load_config(path, non_interactive=ni)
    print("\n[OK] config بارگذاری شد.")
    print(f"  توابع:      {[f['name'] for f in cfg['functions']]}")
    print(f"  partition‌ها: {list(cfg['partitions'].keys())}")
    print(f"  هدف Score:  {cfg['optimizer']['target_score']}%")
    ci_env = _is_ci_environment()
    print(f"  محیط CI:    {ci_env}")