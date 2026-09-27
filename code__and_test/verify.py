"""
verify.py
=========
تست‌های واحد و یکپارچگی برای تمام ماژول‌های سیستم.
بدون وابستگی خارجی به جز PuLP.

اصلاحات نسخه ۳:
  ─ MutantRecord در test_ilp_solver و test_reporter: فیلد اجباری
    mutated_node_text="" اضافه شد (سه جا). بدون این اصلاح verify.py
    با TypeError کرش می‌کرد.
  ─ تست deep_copy در test_strategies: حالا واقعاً assert می‌کند.
    نسخه قبلی همیشه ok() می‌زد بدون هیچ بررسی‌ای (False Positive).
  ─ تمام open() بدون encoding: encoding="utf-8" اضافه شد.
    در Windows با locale غیر UTF-8ممکن بود خطا بدهد.

استفاده:
  python verify.py
  python verify.py -v    # verbose
"""

from __future__ import annotations

import ast
import atexit
import copy
import json
import os
import shutil
import sys
import tempfile

# ── M0 / S0 — Test hermeticity ────────────────────────────────────────────────
# verify.py's integration tests must NEVER write into the production output/
# directory, because tests/test_golden.py compares output/*.json against the
# captured golden fixtures. A shared output/ made pytest order-dependent.
# All report/artifact writes in this harness are redirected to an isolated,
# per-process temp directory that is removed at exit. Production code is
# unchanged (reporter.generate already honours config["project"]["output_dir"]).
#
# The directory is created LAZILY (first use, main process only). Creating it at
# import time would make every multiprocessing spawn worker — which re-imports
# this module — allocate its own temp dir, leaking dozens of directories.
_ISOLATED_OUT = None


def _isolated_out_dir() -> str:
    """Lazily create (once, in this process) the isolated temp output dir."""
    global _ISOLATED_OUT
    if _ISOLATED_OUT is None:
        _ISOLATED_OUT = tempfile.mkdtemp(prefix="mf_verify_out_")
        atexit.register(shutil.rmtree, _ISOLATED_OUT, ignore_errors=True)
    return _ISOLATED_OUT


def _isolated_cfg(cfg: dict) -> dict:
    """Return a copy of cfg whose project.output_dir points at the isolated
    temp dir, so reporter.generate writes there instead of production output/."""
    new = dict(cfg)
    new["project"] = {**cfg.get("project", {}), "output_dir": _isolated_out_dir()}
    return new

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── رنگ‌های ترمینال ──────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

VERBOSE = "-v" in sys.argv


# ══════════════════════════════════════════════════════════════════════════════
# چارچوب تست
# ══════════════════════════════════════════════════════════════════════════════

_passed = 0
_failed = 0
_errors: list[str] = []


def ok(name: str) -> None:
    global _passed
    _passed += 1
    if VERBOSE:
        print(f"  {GREEN}✓{RESET} {name}")


def fail(name: str, reason: str) -> None:
    global _failed
    _failed += 1
    _errors.append(f"{name}: {reason}")
    print(f"  {RED}✗{RESET} {name}")
    print(f"    {RED}{reason}{RESET}")


def section(title: str) -> None:
    print(f"\n{BOLD}{'─'*50}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'─'*50}{RESET}")


def assert_true(cond: bool, name: str, reason: str = "") -> None:
    if cond:
        ok(name)
    else:
        fail(name, reason or "شرط False است")


def assert_eq(a, b, name: str) -> None:
    if a == b:
        ok(name)
    else:
        fail(name, f"انتظار: {b!r} | دریافت: {a!r}")


def assert_raises(exc_type, fn, name: str) -> None:
    try:
        fn()
        fail(name, f"استثنا {exc_type.__name__} منتظر بود اما نیامد")
    except exc_type:
        ok(name)
    except Exception as e:
        fail(name, f"استثنا اشتباه: {type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# تست‌های utils/json_utils
# ══════════════════════════════════════════════════════════════════════════════

def test_json_utils() -> None:
    section("utils/json_utils")
    from utils.json_utils import safe_parse_json, deduplicate, test_to_str, test_hash

    # safe_parse_json
    r = safe_parse_json('[{"arr": [1,2,3]}]')
    assert_true(r == [{"arr": [1, 2, 3]}], "parse آرایه تمیز")

    r = safe_parse_json('Here is JSON: [{"x": 1}] Thanks!')
    assert_true(r == [{"x": 1}], "parse با متن اضافه")

    r = safe_parse_json('{"k": "v"}')
    assert_true(r == [{"k": "v"}], "parse object تکی → list")

    r = safe_parse_json("```json\n[{\"a\":1}]\n```")
    assert_true(r == [{"a": 1}], "parse code fence")

    r = safe_parse_json("garbage text nothing json")
    assert_true(r is None, "parse garbage → None")

    r = safe_parse_json("")
    assert_true(r is None, "parse رشته خالی → None")

    r = safe_parse_json("[1, 2, 3]")
    assert_true(r is None, "parse آرایه غیر-dict → None")

    # deduplicate
    tests = [
        {"function": "f", "inputs": {"x": 1}},
        {"function": "f", "inputs": {"x": 1}},   # تکراری
        {"function": "f", "inputs": {"x": 2}},
        {"function": "g", "inputs": {"x": 1}},
    ]
    u = deduplicate(tests)
    assert_eq(len(u), 3, "deduplicate: تعداد یکتا")
    assert_eq(u[0], tests[0], "deduplicate: ترتیب حفظ می‌شود")

    # deep_copy در deduplicate (لیست‌ها نباید به اشتراک باشند)
    t1 = {"function": "f", "inputs": {"arr": [1, 2, 3]}}
    t2 = {"function": "f", "inputs": {"arr": [1, 2, 3]}}
    u2 = deduplicate([t1, t2])
    assert_eq(len(u2), 1, "deduplicate: لیست‌های یکسان → یکتا")

    # test_hash (public)
    h1 = test_hash({"function": "f", "inputs": {"x": 1}})
    h2 = test_hash({"function": "f", "inputs": {"x": 1}})
    h3 = test_hash({"function": "f", "inputs": {"x": 2}})
    assert_eq(h1, h2, "test_hash: تست‌های یکسان → هش یکسان")
    assert_true(h1 != h3, "test_hash: تست‌های مختلف → هش مختلف")

    # test_to_str
    t = {"function": "sort", "inputs": {"arr": [1, 2]}}
    s = test_to_str(t)
    assert_true("sort" in s and "arr" in s, "test_to_str فرمت")


# ══════════════════════════════════════════════════════════════════════════════
# تست‌های utils/config_loader
# ══════════════════════════════════════════════════════════════════════════════

def test_config_loader() -> None:
    section("utils/config_loader")
    from utils.config_loader import load_config

    # بارگذاری config معتبر
    cfg = load_config("config.json")
    assert_true(isinstance(cfg, dict), "load_config نوع dict برمی‌گرداند")
    assert_true("functions"  in cfg, "کلید functions موجود")
    assert_true("partitions" in cfg, "کلید partitions موجود")
    assert_true("mutation"   in cfg, "کلید mutation موجود")
    assert_true("optimizer"  in cfg, "کلید optimizer موجود")
    assert_true("llm"        in cfg, "کلید llm موجود")

    # تابع‌ها معتبرند
    for fn in cfg["functions"]:
        assert_true("name"   in fn, f"تابع {fn.get('name','?')} دارای name")
        assert_true("params" in fn, f"تابع {fn.get('name','?')} دارای params")
        for p in fn["params"]:
            assert_true(p["type"] in cfg["partitions"],
                        f"partition {p['type']} تعریف شده")

    # output_dir ساخته شده
    out = cfg.get("project", {}).get("output_dir", "output/")
    assert_true(os.path.isdir(out), f"output_dir {out!r} ساخته شد")

    # فایل ناموجود
    assert_raises(SystemExit, lambda: load_config("nonexistent.json"),
                  "load_config با فایل ناموجود → SystemExit")


# ══════════════════════════════════════════════════════════════════════════════
# تست‌های core/mutant_generator
# ══════════════════════════════════════════════════════════════════════════════

def test_mutant_generator() -> None:
    section("core/mutant_generator")
    from core.mutant_generator import (
        generate_all_mutants, MutantRecord,
        print_mutant_summary, save_mutants_json,
    )
    from utils.config_loader import load_config
    cfg = load_config("config.json")

    # اصلاح: encoding="utf-8" اضافه شد
    with open("target_code.py", encoding="utf-8") as _f:
        src = _f.read()

    # تولید Mutant
    mutants = generate_all_mutants(src, cfg["mutation"])
    assert_true(len(mutants) > 0, "حداقل یک Mutant تولید شد")
    assert_true(len(mutants) > 20, f"بیش از ۲۰ Mutant ({len(mutants)})")

    # MutantRecord معتبر
    for m in mutants:
        assert_true(isinstance(m, MutantRecord), f"{m.id} نوع MutantRecord")
        assert_true(m.id.startswith("M"),        f"{m.id} فرمت ID")
        assert_true(m.category in ("ARITH","REL","LOGICAL","INTEGRATION"),
                    f"{m.id} category معتبر")
        assert_true(m.line > 0,                   f"{m.id} line > 0")
        assert_true(len(m.code) > 0,              f"{m.id} code غیرخالی")
        assert_true(m.status == "ALIVE",          f"{m.id} status=ALIVE")
        # بررسی فیلد جدید
        assert_true(hasattr(m, "mutated_node_text"),
                    f"{m.id} دارای فیلد mutated_node_text")

    # اعتبارسنجی کد Mutant‌ها
    invalid = 0
    identical = 0
    for m in mutants:
        try:
            ast.parse(m.code)
        except SyntaxError:
            invalid += 1
        if m.code.strip() == src.strip():
            identical += 1

    assert_eq(invalid,   0, "هیچ Mutantی SyntaxError ندارد")
    assert_eq(identical, 0, "هیچ Mutantی با اصلی یکسان نیست")

    # دسته‌بندی‌ها
    from collections import Counter
    cats = Counter(m.category for m in mutants)
    assert_true(cats.get("ARITH",   0) > 0, "Mutant‌های ARITH وجود دارد")
    assert_true(cats.get("REL",     0) > 0, "Mutant‌های REL وجود دارد")
    assert_true(cats.get("INTEGRATION", 0) > 0, "Mutant‌های INTEGRATION وجود دارد")

    # بدون تکراری
    codes = [m.code for m in mutants]
    assert_eq(len(codes), len(set(codes)), "هیچ Mutantی تکراری نیست")

    # to_dict
    d = mutants[0].to_dict()
    for key in ("id","category","operator_name","function_name","line",
                "code","status","mutated_node_text"):
        assert_true(key in d, f"to_dict دارای کلید {key!r}")

    # ورودی خالی → ValueError
    assert_raises(ValueError, lambda: generate_all_mutants("", cfg["mutation"]),
                  "source_code خالی → ValueError")

    # کد نامعتبر → SyntaxError
    assert_raises(SyntaxError,
                  lambda: generate_all_mutants("def f(: pass", cfg["mutation"]),
                  "SyntaxError در کد نامعتبر")

    # print_mutant_summary بدون crash
    try:
        print_mutant_summary([])
        print_mutant_summary(mutants[:3])
        ok("print_mutant_summary بدون crash")
    except Exception as e:
        fail("print_mutant_summary", str(e))

    # save_mutants_json و load آن
    try:
        _mut_path = os.path.join(_isolated_out_dir(), "test_mutants.json")
        save_mutants_json(mutants, _mut_path)
        # اصلاح: encoding="utf-8" اضافه شد
        with open(_mut_path, encoding="utf-8") as _f:
            saved = json.load(_f)
        assert_eq(len(saved), len(mutants), "save_mutants_json تعداد درست")
        ok("save_mutants_json")
    except Exception as e:
        fail("save_mutants_json", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# تست‌های strategies
# ══════════════════════════════════════════════════════════════════════════════

def test_strategies() -> None:
    section("strategies")
    from strategies import acoc_strategy, domain_strategy
    from utils.config_loader import load_config
    from utils.json_utils import deduplicate
    cfg = load_config("config.json")

    # ACOC
    pool_a = acoc_strategy.generate(cfg)
    assert_true(len(pool_a) > 0, "ACOC: حداقل یک تست")
    for t in pool_a:
        assert_true("function" in t and "inputs" in t,
                    "ACOC: ساختار TestRecord معتبر")
        fn_names = {f["name"] for f in cfg["functions"]}
        assert_true(t["function"] in fn_names,
                    f"ACOC: function_name {t['function']!r} در config")

    # ACOC سقف max_per_function
    cfg2 = copy.deepcopy(cfg)
    cfg2["test_pool"]["acoc_max_per_function"] = 3
    pool_small = acoc_strategy.generate(cfg2)
    for fn in cfg["functions"]:
        count = sum(1 for t in pool_small if t["function"] == fn["name"])
        assert_true(count <= 3, f"ACOC سقف ۳ برای {fn['name']}: {count}")

    # Domain
    pool_c = domain_strategy.generate(cfg)
    assert_true(len(pool_c) > 0, "Domain: حداقل یک تست")
    for t in pool_c:
        assert_true("function" in t and "inputs" in t,
                    "Domain: ساختار معتبر")

    # dedup روی ترکیب
    merged = deduplicate(pool_a + pool_c)
    assert_true(len(merged) <= len(pool_a) + len(pool_c),
                "dedup ترکیب: حداکثر همان تعداد")

    # ── اصلاح: تست deep_copy واقعی با assert ──────────────────────────────
    # یک لیست مرجع از pool_a پیدا می‌کنیم
    ref_test  = None
    ref_arr   = None
    ref_index = -1

    for idx, t in enumerate(pool_a):
        for v in t["inputs"].values():
            if isinstance(v, list):
                ref_test  = t
                ref_arr   = list(v)   # کپی مستقل از مقدار اولیه
                ref_index = idx
                break
        if ref_test is not None:
            break

    # همه لیست‌ها را دستکاری می‌کنیم
    for t in pool_a + pool_c:
        for v in t["inputs"].values():
            if isinstance(v, list):
                v.append(999)

    if ref_test is not None and ref_arr is not None:
        # بررسی: آیا تغییر t[ref_index] به تست دیگری سرایت کرده؟
        other_leaked = False
        for idx, t in enumerate(pool_a):
            if idx == ref_index:
                continue
            for v in t["inputs"].values():
                if isinstance(v, list) and v == ref_test["inputs"].get(
                        list(ref_test["inputs"].keys())[0]):
                    # اگر همان آبجکت باشند، deep copy نبوده
                    if id(v) == id(list(ref_test["inputs"].values())[0]):
                        other_leaked = True
                        break

        # بررسی اصلی: مقدار اضافه‌شده باید فقط در ref_test باشد
        # pool_a[ref_index] تغییر کرده، اما pool_a[دیگری] نباید همان شیء باشد
        shared_objects = 0
        ref_input_obj = None
        for v in ref_test["inputs"].values():
            if isinstance(v, list):
                ref_input_obj = v
                break

        if ref_input_obj is not None:
            for idx, t in enumerate(pool_a):
                if idx == ref_index:
                    continue
                for v in t["inputs"].values():
                    if isinstance(v, list) and id(v) == id(ref_input_obj):
                        shared_objects += 1

        assert_eq(shared_objects, 0,
                  "deep_copy: هیچ تستی input یکسانی را share نمی‌کند")
    else:
        # اگر هیچ لیستی در pool نبود (نادر)، تست را pass می‌کنیم
        ok("deep_copy: هیچ لیستی برای بررسی یافت نشد (skip)")


# ══════════════════════════════════════════════════════════════════════════════
# تست‌های core/sandbox_executor
# ══════════════════════════════════════════════════════════════════════════════

def test_sandbox_executor() -> None:
    section("core/sandbox_executor")
    from core.sandbox_executor import (
        run_safe, build_kill_matrix, count_killed, get_alive_indices,
    )

    code_add = "def f(x, y): return x + y"
    code_sub = "def f(x, y): return x - y"   # Mutant: + → -
    code_inf = "def f(x, y):\n    while True: pass"
    code_err = "def f(x, y): return x / y"   # ممکن است ZeroDivisionError

    # اجرای موفق
    r = run_safe(code_add, "f", {"x": 3, "y": 4})
    assert_eq(r, ("OK", "7"), "run_safe: نتیجه درست")

    # مقایسه اصلی vs Mutant
    r_orig = run_safe(code_add, "f", {"x": 3, "y": 4})
    r_mut  = run_safe(code_sub, "f", {"x": 3, "y": 4})
    assert_true(r_orig != r_mut, "run_safe: اصلی vs Mutant فرق دارند")

    # Timeout
    r_to = run_safe(code_inf, "f", {"x": 1, "y": 1}, timeout=0.5)
    assert_eq(r_to, ("TIMEOUT", None), "run_safe: timeout")

    # CRASH
    r_cr = run_safe(code_err, "f", {"x": 1, "y": 0})
    assert_eq(r_cr[0], "CRASH", "run_safe: crash (ZeroDivisionError)")

    # تابع وجود ندارد
    r_nf = run_safe(code_add, "nonexistent", {"x": 1, "y": 2})
    assert_eq(r_nf[0], "NO_FUNC", "run_safe: تابع ناموجود → NO_FUNC")

    # in-place (sort) — ورودی نباید بین تست‌ها نشت کند
    code_sort = "def f(arr): arr.sort(); return arr"
    r1 = run_safe(code_sort, "f", {"arr": [3, 1, 2]})
    r2 = run_safe(code_sort, "f", {"arr": [3, 1, 2]})
    assert_eq(r1, r2, "run_safe: in-place sort، نتایج یکسان (no leak)")

    # build_kill_matrix کوچک
    from utils.config_loader import load_config
    from core.mutant_generator import generate_all_mutants
    cfg = load_config("config.json")

    # اصلاح: encoding="utf-8" اضافه شد
    with open("target_code.py", encoding="utf-8") as _f:
        src = _f.read()

    mutants = generate_all_mutants(src, cfg["mutation"])[:4]
    from strategies import acoc_strategy
    pool = acoc_strategy.generate(cfg)[:8]

    km, orig = build_kill_matrix(src, mutants, pool, cfg, verbose=False)
    assert_eq(len(km), len(pool),    "kill_matrix: تعداد ردیف‌ها = تعداد تست‌ها")
    assert_eq(len(km[0]), len(mutants), "kill_matrix: تعداد ستون‌ها = تعداد Mutant‌ها")
    assert_true(all(isinstance(v, bool) for row in km for v in row),
                "kill_matrix: همه مقادیر bool")

    # count_killed
    n_killed = count_killed(km)
    assert_true(n_killed >= 0 and n_killed <= len(mutants),
                f"count_killed: مقدار معقول ({n_killed})")

    # get_alive_indices
    alive = get_alive_indices(km)
    assert_true(all(isinstance(i, int) for i in alive),
                "get_alive_indices: همه int")


# ══════════════════════════════════════════════════════════════════════════════
# تست‌های core/ilp_solver
# ══════════════════════════════════════════════════════════════════════════════

def test_ilp_solver() -> None:
    section("core/ilp_solver")
    try:
        import pulp  # noqa
    except ImportError:
        print(f"  {YELLOW}⚠ PuLP نصب نیست — تست‌های ILP رد شد{RESET}")
        return

    from core.ilp_solver import solve, ILPResult, calc_current_score
    from core.mutant_generator import MutantRecord

    # اصلاح: mutated_node_text="" اضافه شد (فیلد اجباری بود و باعث TypeError می‌شد)
    def _fake_mutants(n: int) -> list:
        return [
            MutantRecord(
                id=f"M{i+1:03d}", category="ARITH",
                operator_name="Add_to_Sub", integration_op=None,
                function_name="f", line=1, original_line_text="",
                mutated_node_text="",          # ← اصلاح: فیلد اجباری اضافه شد
                original_op="Add", mutated_op="Sub", code="def f(): pass",
            )
            for i in range(n)
        ]

    # ماتریس نمونه: 4 تست، 3 Mutant
    km = [
        [True,  False, True ],  # t0 → M1, M3
        [False, True,  False],  # t1 → M2
        [True,  True,  False],  # t2 → M1, M2
        [False, False, False],  # t3 → هیچ
    ]
    muts = _fake_mutants(3)
    result = solve(km, copy.deepcopy(muts), [], verbose=False)

    assert_true(isinstance(result, ILPResult), "نوع ILPResult")
    assert_true(result.feasible, "feasible=True")
    assert_true(len(result.selected_tests) <= 4, "کمتر از ۴ تست انتخاب شد")
    assert_eq(result.n_total, 3, "n_total=3")
    assert_eq(result.n_suspected, 0, "suspected=0")
    assert_true(result.raw_score > 0, f"raw_score > 0 ({result.raw_score})")

    # تست با همه suspected
    km_zero = [[False]*3 for _ in range(3)]
    muts2   = _fake_mutants(3)
    r2 = solve(km_zero, copy.deepcopy(muts2), [], verbose=False)
    assert_eq(r2.n_suspected, 3, "همه suspected")
    assert_eq(r2.n_killed,    0, "killed=0")

    # ماتریس خالی
    r3 = solve([], _fake_mutants(0), [], verbose=False)
    assert_eq(r3.n_total, 0, "ماتریس خالی → n_total=0")

    # calc_current_score
    muts3 = _fake_mutants(3)
    raw, adj, n = calc_current_score(km, muts3)
    assert_true(0 <= raw <= 100, f"raw score محدوده ({raw})")
    assert_true(0 <= adj <= 100, f"adj score محدوده ({adj})")


# ══════════════════════════════════════════════════════════════════════════════
# تست‌های core/reporter
# ══════════════════════════════════════════════════════════════════════════════

def test_reporter() -> None:
    section("core/reporter")
    from core import reporter
    from core.ilp_solver import ILPResult
    from core.mutant_generator import MutantRecord
    from utils.config_loader import load_config
    cfg = load_config("config.json")
    cfg = _isolated_cfg(cfg)   # M0/S0: write report to isolated temp dir

    # اصلاح: mutated_node_text="" به هر دو MutantRecord اضافه شد
    muts = [
        MutantRecord(
            id="M001", category="ARITH", operator_name="Add_to_Sub",
            integration_op=None, function_name="f", line=1,
            original_line_text="x + y",
            mutated_node_text="x - y",     # ← اصلاح: فیلد اجباری اضافه شد
            original_op="Add", mutated_op="Sub",
            code="def f(x,y): return x-y", status="KILLED",
        ),
        MutantRecord(
            id="M002", category="REL", operator_name="Gt_to_Lt",
            integration_op=None, function_name="f", line=2,
            original_line_text="x > y",
            mutated_node_text="x < y",     # ← اصلاح: فیلد اجباری اضافه شد
            original_op="Gt", mutated_op="Lt",
            code="def f(x,y): return x<y", status="SUSPECTED",
        ),
    ]
    tests = [
        {"function": "f", "inputs": {"x": 1, "y": 2}},
        {"function": "f", "inputs": {"x": 3, "y": 4}},
    ]
    km = [[True, False], [False, False]]
    ilp = ILPResult(
        selected_tests=[0], suspected_indices=[1],
        raw_score=50.0, adjusted_score=100.0,
        n_killed=1, n_total=2, n_suspected=1, feasible=True,
    )

    try:
        reporter.generate(muts, tests, km, ilp, "def f(x,y): return x+y", cfg)
        ok("reporter.generate بدون crash")
    except Exception as e:
        fail("reporter.generate", str(e))

    # بررسی فایل‌های خروجی
    out = cfg.get("project", {}).get("output_dir", "output/")
    report_path = os.path.join(out, "mutation_report.json")
    suite_path  = os.path.join(out, "minimum_test_suite.json")

    assert_true(os.path.exists(report_path), "mutation_report.json ساخته شد")
    assert_true(os.path.exists(suite_path),  "minimum_test_suite.json ساخته شد")

    # اعتبارسنجی ساختار — اصلاح: encoding="utf-8" اضافه شد
    with open(report_path, encoding="utf-8") as _f:
        rpt = json.load(_f)
    assert_true("summary"          in rpt, "report دارای summary")
    assert_true("killed_mutants"   in rpt, "report دارای killed_mutants")
    assert_true("suspected_mutants" in rpt, "report دارای suspected_mutants")
    assert_true("generated_at"     in rpt, "report دارای generated_at")
    # بررسی فیلد جدید
    assert_true("source" in rpt,               "report دارای source")
    assert_true("source_hash" in rpt["source"], "report.source دارای source_hash")

    with open(suite_path, encoding="utf-8") as _f:
        suite = json.load(_f)
    assert_true("tests"          in suite, "suite دارای tests")
    assert_true("test_count"     in suite, "suite دارای test_count")
    assert_true("mutation_score" in suite, "suite دارای mutation_score")
    assert_true("source_hash"    in suite, "suite دارای source_hash")


# ══════════════════════════════════════════════════════════════════════════════
# تست end-to-end کامل (بدون LLM)
# ══════════════════════════════════════════════════════════════════════════════

def test_end_to_end() -> None:
    section("تست end-to-end (بدون LLM)")
    from utils.config_loader import load_config
    from core.mutant_generator import generate_all_mutants
    from core.test_pool_builder import build as build_pool
    from core.sandbox_executor import build_kill_matrix
    from core.ilp_solver import solve
    from core import reporter

    cfg = load_config("config.json")
    cfg = _isolated_cfg(cfg)   # M0/S0: write report to isolated temp dir
    cfg["test_pool"]["llm_enabled"] = False   # بدون LLM

    # اصلاح: encoding="utf-8" اضافه شد
    with open("target_code.py", encoding="utf-8") as _f:
        src = _f.read()

    class DummyLLM:
        def call(self, p, temperature=0.85): return ""

    # فاز ۱
    mutants = generate_all_mutants(src, cfg["mutation"])
    assert_true(len(mutants) > 0, "E2E: فاز ۱ موفق")

    # فاز ۲
    pool = build_pool(mutants, DummyLLM(), cfg)
    assert_true(len(pool) > 0, "E2E: فاز ۲ موفق")

    # فاز ۳ (نمونه کوچک)
    mini_m = mutants[:10]
    mini_p = pool[:15]
    km, orig = build_kill_matrix(src, mini_m, mini_p, cfg, verbose=False)
    assert_eq(len(km),    len(mini_p), "E2E: فاز ۳ ابعاد ماتریس")
    assert_eq(len(km[0]), len(mini_m), "E2E: فاز ۳ ابعاد ماتریس (ستون)")

    # فاز ۴
    import copy as _copy
    ilp = solve(km, _copy.deepcopy(mini_m), mini_p, verbose=False)
    assert_true(ilp.adjusted_score >= 0,  "E2E: فاز ۴ score معتبر")
    assert_true(len(ilp.selected_tests) <= len(mini_p),
                "E2E: فاز ۴ تعداد انتخابی ≤ pool")

    # فاز ۶
    try:
        reporter.generate(mini_m, mini_p, km, ilp, src, cfg)
        ok("E2E: فاز ۶ موفق")
    except Exception as e:
        fail("E2E: فاز ۶", str(e))

    # بررسی score با کل pool
    all_m = generate_all_mutants(src, cfg["mutation"])
    all_p = build_pool(all_m, DummyLLM(), cfg)
    km_full, _ = build_kill_matrix(src, all_m, all_p, cfg, verbose=False)
    ilp_full = solve(km_full, _copy.deepcopy(all_m), all_p, verbose=False)

    assert_true(ilp_full.adjusted_score >= 80,
                f"E2E کامل: Adjusted Score ≥ 80% ({ilp_full.adjusted_score:.1f}%)")
    assert_true(len(ilp_full.selected_tests) < len(all_p),
                f"E2E کامل: ILP کمینه‌سازی کرد "
                f"({len(ilp_full.selected_tests)} < {len(all_p)})")

    ok(f"E2E کامل: Score={ilp_full.adjusted_score:.1f}% | "
       f"MinTests={len(ilp_full.selected_tests)}/{len(all_p)}")


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print(f"\n{BOLD}{'═'*52}{RESET}")
    print(f"{BOLD}  Mutation Testing System — تست‌های واحد{RESET}")
    print(f"{BOLD}{'═'*52}{RESET}")

    test_json_utils()
    test_config_loader()
    test_mutant_generator()
    test_strategies()
    test_sandbox_executor()
    test_ilp_solver()
    test_reporter()
    test_end_to_end()

    # ── خلاصه ────────────────────────────────────────────────────────────────
    total = _passed + _failed
    print(f"\n{BOLD}{'═'*52}{RESET}")
    print(f"{BOLD}  نتیجه نهایی{RESET}")
    print(f"{'─'*52}")
    print(f"  کل تست‌ها:  {total}")
    print(f"  {GREEN}موفق:       {_passed}{RESET}")
    if _failed:
        print(f"  {RED}شکست:       {_failed}{RESET}")
        print(f"\n{RED}  تست‌های شکست‌خورده:{RESET}")
        for e in _errors:
            print(f"  {RED}  • {e}{RESET}")
    else:
        print(f"\n  {GREEN}{BOLD}✅ همه تست‌ها موفق!{RESET}")
    print(f"{BOLD}{'═'*52}{RESET}\n")

    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()