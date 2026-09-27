"""
core/sandbox_executor.py  —  فاز ۳
=====================================
اجرای ایزوله تست‌ها روی کد اصلی و Mutant‌ها.
ساخت kill_matrix[n_tests × n_mutants].

اصلاحات نسخه ۴:
  ─ رفع Race Condition: reader.join(timeout + 2.0) به جای reader.join(2.0)
    مشکل: مقدار ثابت 2.0 با timeout > 2s در شرایط بار سنگین شکست می‌خورد.
    راه‌حل: زمان انتظار به timeout + 2.0 تغییر کرد تا همیشه کافی باشد.
  ─ اجرای موازی با ThreadPoolExecutor:
    جایگزین حلقه sequential در build_kill_matrix.
    هر thread یک Mutant را test می‌کند؛ run_safe داخل هر thread
    یک subprocess مجزا spawn می‌کند — GIL در هنگام انتظار آزاد است.
    نتیجه: speedup خطی تا max_workers برابر.
  ─ extend_kill_matrix هم موازی‌سازی شد.
  ─ config key جدید: sandbox.parallel_workers (پیش‌فرض: auto)

اصلاحات نسخه ۳:
  ─ رفع Resource Leak: q.close() + q.join_thread() پس از Timeout
  ─ RLIMIT_AS از 150MB به 512MB افزایش یافت

اصلاحات نسخه ۲:
  ─ رفع Deadlock: Thread خواننده جداگانه
  ─ محدودیت حافظه با resource.setrlimit
"""

from __future__ import annotations

import collections
import copy
import json
import multiprocessing
import os
import platform
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)


_TIMEOUT = ("TIMEOUT", None)
_CRASH   = ("CRASH",   None)

_MAX_MEMORY_BYTES = 512 * 1024 * 1024   # 512 MB

# ── I5.2: additional (non-cumulative) process resource caps ──────────────────
# These are safe to set ONCE on a long-lived pool worker because, unlike
# RLIMIT_CPU, they are not cumulative over the worker's lifetime:
#   FSIZE  — max bytes any single file the sandboxed code may write (contains
#            runaway/disk-fill writes). 64 MB is far above anything the test
#            harness legitimately writes (the mutated functions write nothing).
#   NOFILE — max open file descriptors (contains fd exhaustion). Clamped to the
#            existing hard limit so a non-root worker never tries to raise it.
# RLIMIT_CPU is intentionally NOT set on the pool worker: it is cumulative
# per-process and cannot be reset per job, so a single cap would sum CPU across
# all jobs and kill the worker mid-batch, corrupting the kill matrix. The
# per-job wall-clock timeout (owned by the parent dispatcher) remains the
# infinite-loop / CPU backstop, unchanged from M3.
_MAX_FSIZE_BYTES = 64 * 1024 * 1024     # 64 MB
_MAX_NOFILE      = 256                   # open file descriptors

# ── I1 (V2 M3): persistent warm worker pool tuning ───────────────────────────
_POOL_POLL  = 0.01   # parent result-queue poll interval (s)
_POOL_SLACK = 0.75   # IPC/scheduling slack added to per-job wall-timeout (s)


# ══════════════════════════════════════════════════════════════════════════════
# Worker — در پروسه ایزوله اجرا می‌شود
# ══════════════════════════════════════════════════════════════════════════════

def _worker(
    func_code:       str,
    func_name:       str,
    test_input_json: str,
    result_queue:    multiprocessing.Queue,
    cpu_limit:       int = 0,
) -> None:
    """
    کد تابع را در namespace تمیز اجرا می‌کند.
    این تابع در یک پروسه مجزا (spawn) اجرا می‌شود.

    cpu_limit > 0 یک سقف زمان-CPU (ثانیه) تعیین می‌کند تا mutant‌های
    دارای حلقه بی‌نهایت به‌جای مصرف کامل wall-clock timeout، سریع‌تر با
    SIGXCPU خاتمه یابند (پشتیبان wall-clock kill در run_safe).
    """
    if platform.system() in ("Linux", "Darwin"):
        try:
            import resource
            resource.setrlimit(
                resource.RLIMIT_AS,
                (_MAX_MEMORY_BYTES, _MAX_MEMORY_BYTES),
            )
            if cpu_limit and cpu_limit > 0:
                resource.setrlimit(
                    resource.RLIMIT_CPU, (cpu_limit, cpu_limit)
                )
        except Exception:
            pass

    try:
        namespace: dict[str, Any] = {}
        exec(compile(func_code, "<sandbox>", "exec"), namespace)   # noqa: S102

        func = namespace.get(func_name)
        if func is None:
            result_queue.put(("NO_FUNC", None))
            return

        raw_input  = json.loads(test_input_json)
        test_input = {k: copy.deepcopy(v) for k, v in raw_input.items()}

        result = func(**test_input)

        try:
            serialized = json.dumps(result, default=str, sort_keys=True)
        except (TypeError, ValueError):
            serialized = repr(result)

        result_queue.put(("OK", serialized))

    except MemoryError:
        result_queue.put(("CRASH", "MemoryError"))
    except Exception as e:
        result_queue.put(("CRASH", type(e).__name__))


# ══════════════════════════════════════════════════════════════════════════════
# run_safe — اجرای یک تست با timeout بدون Deadlock
# ══════════════════════════════════════════════════════════════════════════════

def run_safe(
    func_code:  str,
    func_name:  str,
    test_input: dict,
    timeout:    float = 2.0,
) -> tuple[str, Optional[str]]:
    """
    یک تست را روی func_code اجرا می‌کند.

    رفع Race Condition (نسخه ۴):
        reader.join(timeout + 2.0) به جای reader.join(2.0)
        مقدار قبلی 2.0 در timeout > 2s یا سیستم‌های پربار نادرست بود.
        الگو: بعد از p.join(timeout)، پروسه تمام کرده، queue نتیجه دارد.
        reader باید کافی زمان داشته باشد تا queue را drain کند.

    Returns:
        ("OK",      json_output)  — اجرای موفق
        ("TIMEOUT", None)         — timeout
        ("CRASH",   error_type)   — exception
        ("NO_FUNC", None)         — تابع پیدا نشد
        ("SERIAL",  None)         — ورودی serialize‌پذیر نیست
    """
    try:
        input_json = json.dumps(test_input, default=str)
    except (TypeError, ValueError):
        return ("SERIAL", None)

    ctx = multiprocessing.get_context("spawn")
    q   = ctx.Queue()
    # سقف زمان-CPU به‌عنوان پشتیبان wall-clock: حداقل ۱ ثانیه (واحد setrlimit
    # عدد صحیح است)، یا یک ثانیه بیش از timeout گرد-شده.
    cpu_limit = max(1, int(timeout) + 1)
    p   = ctx.Process(target=_worker,
                      args=(func_code, func_name, input_json, q, cpu_limit))

    result_holder: list[tuple] = []

    def _reader() -> None:
        try:
            result_holder.append(q.get(timeout=timeout + 1.0))
        except Exception:
            pass

    reader = threading.Thread(target=_reader, daemon=True)

    p.start()
    reader.start()

    p.join(timeout)

    if p.is_alive():
        p.kill()
        p.join(2.0)
        reader.join(1.0)
        try:
            q.close()
            q.join_thread()
        except Exception:
            pass
        return _TIMEOUT

    # ── اصلاح نسخه ۴: reader.join با زمان کافی ─────────────────────────────
    # پروسه تمام کرده — reader باید نتیجه را از queue بگیرد.
    # با timeout + 2.0 اطمینان می‌دهیم که حتی در سیستم‌های پربار کافی باشد.
    reader.join(timeout + 2.0)

    try:
        q.close()
        q.join_thread()
    except Exception:
        pass

    if result_holder:
        return result_holder[0]

    return _CRASH


# ══════════════════════════════════════════════════════════════════════════════
# کمک‌کننده موازی‌سازی
# ══════════════════════════════════════════════════════════════════════════════

def _test_mutant_parallel(
    mutant_code: str,
    func_name:   str,
    test_pool:   list[dict],
    orig_results: list[tuple],
    timeout:     float,
) -> list[bool]:
    """
    یک Mutant را در برابر همه تست‌های مرتبط اجرا می‌کند.
    برای استفاده درون ThreadPoolExecutor طراحی شده.

    Args:
        mutant_code:  کد Mutant (string)
        func_name:    نام تابع این Mutant
        test_pool:    کل استخر تست (فقط تست‌های func_name پردازش می‌شوند)
        orig_results: نتایج کد اصلی
        timeout:      timeout برای هر اجرا

    Returns:
        column: list[bool] با طول len(test_pool)
                column[i] = True اگر تست i این Mutant را کشته باشد
    """
    n_tests = len(test_pool)
    column  = [False] * n_tests

    for i, test in enumerate(test_pool):
        if test.get("function") != func_name:
            continue

        orig_r = orig_results[i]
        # نتایج UNKNOWN_FUNC یا SERIAL قابل مقایسه نیستند
        if orig_r[0] in ("UNKNOWN_FUNC", "SERIAL"):
            continue

        mut_r = run_safe(mutant_code, func_name, test.get("inputs", {}), timeout)
        if mut_r != orig_r:
            column[i] = True

    return column


def _get_parallel_workers(config: dict) -> int:
    """تعداد worker thread ها را از config یا CPU count تعیین می‌کند."""
    cfg = config.get("sandbox", {})
    workers = cfg.get("parallel_workers", None)
    if workers is not None:
        return max(1, int(workers))
    return min(4, os.cpu_count() or 1)


def _adaptive_timeout(
    orig_times:  list[float],
    sandbox_cfg: dict,
    hard_cap:    float,
) -> float:
    """
    تایم‌اوت تطبیقی برای اجرای Mutant‌ها محاسبه می‌کند.

    انگیزه (Perf-Fix):
        Mutant‌هایی که حلقه بی‌نهایت می‌سازند (مثل وارونه‌کردن به‌روزرسانی
        اندیس در binary_search) قبلاً کل timeout_seconds (پیش‌فرض ۲s) را
        برای هر تست مصرف می‌کردند. با تایم‌اوت تطبیقی که بر اساس کندترین
        اجرای *معمولِ* کد اصلی تنظیم می‌شود، این Mutant‌ها بسیار سریع‌تر
        خاتمه می‌یابند بدون آنکه Mutant‌های کندِ صحیح به‌اشتباه TIMEOUT شوند.

    مبنای محاسبه صدک ۹۰ زمان‌های اجرای کد اصلی است (نه max) تا یک spike
    منفرد در راه‌اندازی پروسه/زمان‌بندی، تایم‌اوت را به‌اشتباه بالا نبرد.

    فرمول:
        timeout = clamp(floor, multiplier × p90(orig_times), hard_cap)

    کلیدهای config (همگی اختیاری، سازگار با گذشته):
        sandbox.timeout_floor_seconds  (پیش‌فرض ۰٫۲۵)
        sandbox.timeout_multiplier     (پیش‌فرض ۸)
        sandbox.timeout_seconds        → hard_cap (سقف مطلق)
    """
    floor = float(sandbox_cfg.get("timeout_floor_seconds", 0.25))
    mult  = float(sandbox_cfg.get("timeout_multiplier",    8.0))
    cap   = float(hard_cap)

    times = sorted(t for t in orig_times if t and t > 0)
    if not times:
        base = floor
    else:
        # صدک ۹۰ — در برابر spike‌های منفرد راه‌اندازی پروسه مقاوم است
        k    = max(0, min(len(times) - 1, int(round(0.9 * (len(times) - 1)))))
        p90  = times[k]
        base = mult * p90

    # هرگز از سقف مطلق (timeout_seconds) فراتر نمی‌رود و هرگز زیر یک کف
    # منطقی نمی‌رود تا overhead راه‌اندازی پروسه پوشش داده شود.
    return max(0.001, min(cap, max(floor, base)))


# ══════════════════════════════════════════════════════════════════════════════
# I1 — Persistent warm worker pool (V2 M3)
# ══════════════════════════════════════════════════════════════════════════════
#
# Replaces per-execution `spawn` with a pool of long-lived worker processes that
# consume (code, input) jobs. Each job executes with the EXACT semantics of
# `_worker` (fresh namespace per job → no cross-job state leak). Per-job
# wall-timeout + kill is owned by the PARENT (workers recycled on timeout /
# crash / OOM). Results are keyed by job-id (i, j) so completion order never
# affects the kill matrix — determinism is preserved. The legacy spawn-per-exec
# path remains available via config: sandbox.execution_backend = "legacy".

def _execute_job(func_code: str, func_name: str, input_json: str) -> tuple[str, Optional[str]]:
    """Run one job in a FRESH namespace. Byte-identical result semantics to
    `_worker` (OK / NO_FUNC / CRASH). TIMEOUT is enforced by the parent."""
    try:
        namespace: dict[str, Any] = {}
        exec(compile(func_code, "<sandbox>", "exec"), namespace)   # noqa: S102

        func = namespace.get(func_name)
        if func is None:
            return ("NO_FUNC", None)

        raw_input  = json.loads(input_json)
        test_input = {k: copy.deepcopy(v) for k, v in raw_input.items()}

        result = func(**test_input)
        try:
            serialized = json.dumps(result, default=str, sort_keys=True)
        except (TypeError, ValueError):
            serialized = repr(result)
        return ("OK", serialized)

    except MemoryError:
        return ("CRASH", "MemoryError")
    except Exception as e:
        return ("CRASH", type(e).__name__)


def _pool_worker_main(in_q, out_q, widx: int) -> None:
    """Long-lived worker loop. Sets the (non-cumulative) memory cap once, then
    processes jobs until a None sentinel. Module-level → picklable for spawn."""
    if platform.system() in ("Linux", "Darwin"):
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS,
                               (_MAX_MEMORY_BYTES, _MAX_MEMORY_BYTES))
            # I5.2: file-size cap (non-cumulative; contains runaway writes).
            try:
                resource.setrlimit(resource.RLIMIT_FSIZE,
                                   (_MAX_FSIZE_BYTES, _MAX_FSIZE_BYTES))
            except (ValueError, OSError, AttributeError):
                pass   # limit unsupported on this platform — skip, no crash
            # I5.2: open-fd cap (non-cumulative; contains fd exhaustion).
            # Clamp the soft limit to the existing hard limit so a non-root
            # worker never attempts to raise the hard limit.
            try:
                _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                _target = (_MAX_NOFILE if _hard == resource.RLIM_INFINITY
                           else min(_MAX_NOFILE, _hard))
                resource.setrlimit(resource.RLIMIT_NOFILE, (_target, _hard))
            except (ValueError, OSError, AttributeError):
                pass   # limit unsupported / cannot lower — skip, no crash
        except Exception:
            pass   # resource module unavailable (e.g. Windows) — rely on wall-timeout
    while True:
        try:
            job = in_q.get()
        except Exception:
            break
        if job is None:           # shutdown sentinel
            break
        job_id, func_code, func_name, input_json = job
        result = _execute_job(func_code, func_name, input_json)
        out_q.put((widx, job_id, result))


class _WorkerPool:
    """Pool of long-lived spawn worker processes with parent-owned per-job
    wall-timeout + kill/recycle. Deterministic: results keyed by job-id."""

    def __init__(self, n_workers: int, timeout: float) -> None:
        self.ctx     = multiprocessing.get_context("spawn")
        self.timeout = float(timeout)
        self.n       = max(1, int(n_workers))
        self.out_q   = self.ctx.Queue()
        self.in_qs:  list = [None] * self.n
        self.procs:  list = [None] * self.n
        for widx in range(self.n):
            self._spawn(widx)

    def _spawn(self, widx: int) -> None:
        inq = self.ctx.Queue()
        p = self.ctx.Process(target=_pool_worker_main,
                             args=(inq, self.out_q, widx), daemon=True)
        p.start()
        self.in_qs[widx] = inq
        self.procs[widx] = p

    def _recycle(self, widx: int) -> None:
        """Hard-kill a worker (timeout/crash) and start a fresh replacement."""
        p = self.procs[widx]
        try:
            p.kill()
            p.join(1.0)
        except Exception:
            pass
        try:
            self.in_qs[widx].close()
        except Exception:
            pass
        self._spawn(widx)

    def run(self, jobs: list) -> dict:
        """jobs: list of (job_id, func_code, func_name, input_json).
        Returns {job_id: result_tuple}. Order-independent."""
        results: dict = {}
        pending = collections.deque(jobs)
        idle    = collections.deque(range(self.n))
        inflight: dict = {}      # widx -> (job_id, deadline)

        def dispatch() -> None:
            while idle and pending:
                widx = idle.popleft()
                job  = pending.popleft()
                self.in_qs[widx].put(job)
                inflight[widx] = (job[0], time.monotonic() + self.timeout + _POOL_SLACK)

        dispatch()
        while inflight:
            # 1) drain ALL available results first (a delivered result always
            #    wins over a timeout decision for the same job).
            drained = False
            while True:
                try:
                    widx, job_id, res = self.out_q.get_nowait()
                except queue.Empty:
                    break
                if widx in inflight and inflight[widx][0] == job_id:
                    results[job_id] = res
                    del inflight[widx]
                    idle.append(widx)
                    drained = True
                # else: stale result from a recycled worker → ignore

            # 2) timeout / dead-worker detection
            now = time.monotonic()
            timed_out = False
            for widx in list(inflight):
                job_id, deadline = inflight[widx]
                if now > deadline:
                    self._recycle(widx)
                    results[job_id] = _TIMEOUT
                    del inflight[widx]
                    idle.append(widx)
                    timed_out = True
                elif self.procs[widx] is not None and not self.procs[widx].is_alive():
                    # died without delivering a result (hard crash / OOM-kill)
                    self._recycle(widx)
                    results[job_id] = _CRASH
                    del inflight[widx]
                    idle.append(widx)
                    timed_out = True

            dispatch()
            if not drained and not timed_out and inflight:
                time.sleep(_POOL_POLL)
        return results

    def shutdown(self) -> None:
        for widx in range(self.n):
            try:
                self.in_qs[widx].put(None)
            except Exception:
                pass
        for p in self.procs:
            try:
                p.join(0.5)
            except Exception:
                pass
            try:
                if p.is_alive():
                    p.kill()
            except Exception:
                pass
        try:
            self.out_q.close()
        except Exception:
            pass


def _build_columns_via_pool(
    mutants:      list,
    test_pool:    list[dict],
    orig_results: list[tuple],
    timeout:      float,
    max_workers:  int,
    config:       dict,
) -> list[Optional[list[bool]]]:
    """Warm-pool equivalent of the per-mutant `_test_mutant_parallel` loop.

    Produces `columns[j]` = list[bool] of length n_tests, applying the IDENTICAL
    kill predicate (`result != orig_results[i]`) and the IDENTICAL skip rules
    (function mismatch; orig UNKNOWN_FUNC/SERIAL). Result is deterministic.
    """
    n_tests   = len(test_pool)
    n_mutants = len(mutants)
    columns: list[Optional[list[bool]]] = [[False] * n_tests for _ in range(n_mutants)]

    jobs: list = []
    for j, m in enumerate(mutants):
        fn = m.function_name
        for i, test in enumerate(test_pool):
            if test.get("function") != fn:
                continue
            if orig_results[i][0] in ("UNKNOWN_FUNC", "SERIAL"):
                continue
            try:
                input_json = json.dumps(test.get("inputs", {}), default=str)
            except (TypeError, ValueError):
                continue   # SERIAL input → skip (column stays False), as legacy
            jobs.append(((i, j), m.code, fn, input_json))

    if not jobs:
        return columns

    # I5.1: obtain the executor from the factory (config-selected backend)
    # instead of hard-constructing WorkerPoolExecutor. With sandbox.executor
    # absent/"pool" (the default) this returns the SAME M3 WorkerPoolExecutor,
    # so jobs, result tuples, and timeout/recycle semantics are byte-identical.
    # This is the seam where the future ContainerExecutor (I5.4) plugs in,
    # "without touching callers".
    from core.executor import make_executor
    executor = make_executor(config, max_workers, timeout)
    try:
        executor.start()
        results = executor.submit_many(jobs)
    finally:
        executor.shutdown()

    for (i, j), res in results.items():
        if res != orig_results[i]:
            columns[j][i] = True
    return columns


# ══════════════════════════════════════════════════════════════════════════════
# build_kill_matrix — تابع اصلی فاز ۳ (نسخه موازی)
# ══════════════════════════════════════════════════════════════════════════════

def build_kill_matrix(
    original_code: str,
    mutants:       list,       # list[MutantRecord]
    test_pool:     list[dict],
    config:        dict,
    verbose:       bool = False,
) -> tuple[list[list[bool]], list[tuple]]:
    """
    ماتریس پوشش [n_tests × n_mutants] را می‌سازد.
    نسخه موازی با ThreadPoolExecutor.

    هر thread یک Mutant را test می‌کند.
    run_safe داخل هر thread یک subprocess spawn می‌کند.
    GIL در هنگام انتظار برای subprocess آزاد است → parallelism واقعی.

    Args:
        original_code: کد اصلی (string)
        mutants:       لیست MutantRecord
        test_pool:     لیست TestRecord
        config:        dict کامل config.json
        verbose:       چاپ پیشرفت کامل

    Returns:
        (kill_matrix, orig_results)
    """
    sandbox_cfg = config.get("sandbox", {})
    timeout     = sandbox_cfg.get("timeout_seconds", 2)
    max_workers = _get_parallel_workers(config)

    n_tests   = len(test_pool)
    n_mutants = len(mutants)

    logger.info("[PHASE-3] ساخت ماتریس پوشش...")
    logger.info(f"  {n_tests} تست × {n_mutants} Mutant "
          f"= {n_tests * n_mutants} اجرا | workers={max_workers}")

    if n_tests == 0 or n_mutants == 0:
        logger.info("[PHASE-3] ✓ ماتریس خالی (تست یا Mutant موجود نیست)\n")
        return [], []

    func_names = _get_func_names(config)

    # ── نتایج کد اصلی (sequential — ترتیب مهم است) ───────────────────────────
    # حین اجرای کد اصلی، کندترین زمان اجرا را اندازه می‌گیریم تا تایم‌اوت
    # تطبیقی برای Mutant‌ها محاسبه شود.
    orig_results: list[tuple] = []
    orig_times:   list[float] = []
    for i, test in enumerate(test_pool):
        fn = test.get("function", "")
        if fn not in func_names:
            orig_results.append(("UNKNOWN_FUNC", None))
            continue
        t0     = time.monotonic()
        result = run_safe(original_code, fn, test.get("inputs", {}), timeout)
        elapsed = time.monotonic() - t0
        if result[0] == "OK":
            orig_times.append(elapsed)
        orig_results.append(result)
        if verbose and i % 20 == 0:
            logger.info(f"  [ORIG] {i}/{n_tests} تست اصلی اجرا شد...")

    mutant_timeout = _adaptive_timeout(orig_times, sandbox_cfg, timeout)
    logger.info(f"  [ORIG] {len(orig_results)} نتیجه اصلی آماده "
          f"| تایم‌اوت تطبیقی Mutant: {mutant_timeout:.2f}s "
          f"(سقف {timeout}s)")

    # ── اجرای Mutant‌ها ───────────────────────────────────────────────────────
    # I1 (V2 M3): پیش‌فرض = استخر warm با پروسه‌های بلندعمر (process reuse) که
    # هزینهٔ spawn هر اجرا را حذف می‌کند. مسیر قدیمی (spawn-per-exec با
    # ThreadPoolExecutor) با execution_backend="legacy" در دسترس است.
    # I5.3: a single execution control-flow. Both the warm-pool and the legacy
    # spawn-per-exec backends now resolve to an Executor inside
    # _build_columns_via_pool (via make_executor), so build_kill_matrix no longer
    # duplicates the per-mutant ThreadPool loop. Backend choice lives entirely in
    # the config key sandbox.execution_backend ("pool" default | "legacy").
    backend = sandbox_cfg.get("execution_backend",
                              sandbox_cfg.get("executor", "pool"))

    kill:    list[list[bool]]           = [[False] * n_mutants for _ in range(n_tests)]
    columns: list[Optional[list[bool]]] = _build_columns_via_pool(
        mutants, test_pool, orig_results, mutant_timeout, max_workers, config
    )
    completed = sum(1 for c in columns if c is not None)
    if verbose:
        for j in range(n_mutants):
            col = columns[j]
            killed_by = sum(1 for v in col if v) if col else 0
            status = f"کشته‌شد ({killed_by})" if killed_by else "زنده ماند"
            logger.info(f"  [{mutants[j].id}] {mutants[j].operator_name} → {status}")
    else:
        total_killed_sofar = sum(
            1 for jj in range(n_mutants)
            if columns[jj] is not None and any(columns[jj])
        )
        logger.info(f"  [MATRIX] {completed}/{n_mutants} (100%) "
                    f"| کشته: {total_killed_sofar} | backend={backend}")

    # ── بازسازی ماتریس از ستون‌ها (مشترک بین backendها) ───────────────────────
    for i in range(n_tests):
        for j in range(n_mutants):
            col = columns[j]
            if col is not None:
                kill[i][j] = col[i]

    total_killed = sum(
        1 for j in range(n_mutants)
        if any(kill[i][j] for i in range(n_tests))
    )
    logger.info(f"[PHASE-3] ✓ ماتریس کامل | کشته: {total_killed}/{n_mutants}\n")

    return kill, orig_results


# ══════════════════════════════════════════════════════════════════════════════
# کمک‌کننده‌ها
# ══════════════════════════════════════════════════════════════════════════════

def _get_func_names(config: dict) -> set[str]:
    return {f["name"] for f in config.get("functions", [])}


def count_killed(kill_matrix: list[list[bool]]) -> int:
    """تعداد Mutant‌هایی که حداقل یک تست آن‌ها را کشته است."""
    if not kill_matrix or not kill_matrix[0]:
        return 0
    n_mutants = len(kill_matrix[0])
    return sum(
        1 for j in range(n_mutants)
        if any(kill_matrix[i][j] for i in range(len(kill_matrix)))
    )


def get_alive_indices(kill_matrix: list[list[bool]]) -> list[int]:
    """اندیس Mutant‌هایی که هیچ تستی آن‌ها را نکشته."""
    if not kill_matrix or not kill_matrix[0]:
        return []
    n_mutants = len(kill_matrix[0])
    return [
        j for j in range(n_mutants)
        if not any(kill_matrix[i][j] for i in range(len(kill_matrix)))
    ]


def extend_kill_matrix(
    kill_matrix:   list[list[bool]],
    orig_results:  list[tuple],
    original_code: str,
    mutants:       list,
    new_tests:     list[dict],
    config:        dict,
) -> tuple[list[list[bool]], list[tuple]]:
    """
    ماتریس را با تست‌های جدید (از حلقه بازخورد) گسترش می‌دهد.
    تست‌های قدیمی مجدداً اجرا نمی‌شوند.
    از ThreadPoolExecutor برای موازی‌سازی Mutant‌ها استفاده می‌کند.
    """
    if not new_tests:
        return kill_matrix, orig_results

    sandbox_cfg = config.get("sandbox", {})
    timeout     = sandbox_cfg.get("timeout_seconds", 2)
    max_workers = _get_parallel_workers(config)
    n_mutants   = len(mutants)
    func_names  = _get_func_names(config)

    # ── نتایج اصلی برای تست‌های جدید ──────────────────────────────────────────
    new_orig: list[tuple] = []
    orig_times: list[float] = []
    for test in new_tests:
        fn = test.get("function", "")
        if fn not in func_names:
            new_orig.append(("UNKNOWN_FUNC", None))
        else:
            t0  = time.monotonic()
            res = run_safe(original_code, fn, test.get("inputs", {}), timeout)
            elapsed = time.monotonic() - t0
            if res[0] == "OK":
                orig_times.append(elapsed)
            new_orig.append(res)

    mutant_timeout = _adaptive_timeout(orig_times, sandbox_cfg, timeout)

    # ── ماتریس برای تست‌های جدید (موازی) ──────────────────────────────────────
    n_new  = len(new_tests)
    new_kill_rows: list[list[bool]] = [[False] * n_mutants for _ in range(n_new)]
    columns: list[Optional[list[bool]]] = [None] * n_mutants

    def _test_new_mutant(j: int) -> list[bool]:
        """یک Mutant را در برابر فقط تست‌های جدید اجرا می‌کند."""
        m      = mutants[j]
        col    = [False] * n_new
        fn     = m.function_name
        for ni, test in enumerate(new_tests):
            if test.get("function") != fn:
                continue
            orig_r = new_orig[ni]
            if orig_r[0] in ("UNKNOWN_FUNC", "SERIAL"):
                continue
            mut_r = run_safe(m.code, fn, test.get("inputs", {}), mutant_timeout)
            if mut_r != orig_r:
                col[ni] = True
        return col

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_j = {
            executor.submit(_test_new_mutant, j): j
            for j in range(n_mutants)
        }
        for future in as_completed(future_to_j):
            j = future_to_j[future]
            try:
                columns[j] = future.result()
            except Exception:
                columns[j] = [False] * n_new

    for ni in range(n_new):
        for j in range(n_mutants):
            col = columns[j]
            if col is not None:
                new_kill_rows[ni][j] = col[ni]

    combined_kill = kill_matrix + new_kill_rows
    combined_orig = orig_results + new_orig
    return combined_kill, combined_orig


# ══════════════════════════════════════════════════════════════════════════════
# اجرای مستقل — تست
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    code_ok  = "def f(x): return x + 1"
    code_mut = "def f(x): return x - 1"
    code_inf = "def f(x):\n    while True: pass"
    code_mem = "def f(x):\n    a=[]\n    while True: a.extend([0]*10000)"

    print("[TEST] run_safe...")
    r1 = run_safe(code_ok,  "f", {"x": 5})
    r2 = run_safe(code_mut, "f", {"x": 5})
    r3 = run_safe(code_inf, "f", {"x": 5}, timeout=1)
    r4 = run_safe(code_mem, "f", {"x": 1}, timeout=1)
    print(f"  اصلی:    {r1}")
    print(f"  Mutant:  {r2}")
    print(f"  Timeout: {r3}")
    print(f"  OOM:     {r4}")
    print(f"  کشته‌شد؟ {r1 != r2} (باید True)")
    print(f"  Timeout؟ {r3 == _TIMEOUT} (باید True)")