
"""
gui.py — رابط گرافیکی وب برای سیستم Mutation Testing
======================================================
یک وب‌اپلیکیشن Flask با رابط گرافیکی کامل برای کنترل
و نظارت بر سیستم Mutation Testing.

ویژگی‌ها:
  - مدیریت تنظیمات (ویرایش config.json)
  - ویرایش کد هدف (target_code.py)
  - اجرای pipeline با استریم real-time
  - نمایش نتایج با نمودار و جدول
  - دانلود گزارش‌ها

نصب وابستگی:
  pip install flask

اجرا:
  python gui.py
  سپس مرورگر را باز کنید: http://localhost:5000
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ─── Flask ────────────────────────────────────────────────────────────────────
try:
    from flask import Flask, Response, jsonify, request, send_file
except ImportError:
    print("[ERROR] Flask نصب نیست. اجرا کنید: pip install flask")
    sys.exit(1)

# ─── مسیرهای پروژه ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
CONFIG_PATH  = PROJECT_ROOT / "config.json"
TARGET_PATH  = PROJECT_ROOT / "target_code.py"
OUTPUT_DIR   = PROJECT_ROOT / "output"
MAIN_PY      = PROJECT_ROOT / "main.py"

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ─── وضعیت اجرا (thread-safe) ─────────────────────────────────────────────────
_lock       = threading.Lock()
_run_state  = {
    "running":    False,
    "phase":      None,
    "started_at": None,
    "ended_at":   None,
    "exit_code":  None,
    "args":       [],
}
_process:    subprocess.Popen | None = None
_log_queue:  queue.Queue = queue.Queue(maxsize=2000)


# ══════════════════════════════════════════════════════════════════════════════
# صفحه اصلی
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html; charset=utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# API — Config
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/config", methods=["GET"])
def get_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return jsonify({"ok": True, "data": json.load(f)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def save_config():
    try:
        data = request.get_json(force=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — Target Code
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/target", methods=["GET"])
def get_target():
    try:
        with open(TARGET_PATH, encoding="utf-8") as f:
            return jsonify({"ok": True, "code": f.read()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/target", methods=["POST"])
def save_target():
    try:
        body = request.get_json(force=True)
        code = body.get("code", "")
        with open(TARGET_PATH, "w", encoding="utf-8") as f:
            f.write(code)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# API — اجرا
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/run", methods=["POST"])
def start_run():
    global _process
    with _lock:
        if _run_state["running"]:
            return jsonify({"ok": False, "error": "پروسه‌ای در حال اجرا است"}), 409

    body    = request.get_json(force=True) or {}
    no_llm  = body.get("no_llm",  False)
    verbose = body.get("verbose", False)

    # ساخت آرگومان‌ها
    cmd = [sys.executable, str(MAIN_PY),
           "--config", str(CONFIG_PATH),
           "--target", str(TARGET_PATH)]
    if no_llm:
        cmd.append("--no-llm")
    if verbose:
        cmd.append("--verbose")

    def _run():
        global _process
        with _lock:
            _run_state["running"]    = True
            _run_state["phase"]      = "آماده‌سازی..."
            _run_state["started_at"] = datetime.now().isoformat()
            _run_state["ended_at"]   = None
            _run_state["exit_code"]  = None
            _run_state["args"]       = cmd

        # پاک‌سازی صف قدیمی
        while not _log_queue.empty():
            try:
                _log_queue.get_nowait()
            except queue.Empty:
                break

        _log_queue.put({"type": "start", "text": f"▶ شروع اجرا: {' '.join(cmd)}"})
        _log_queue.put({"type": "info",  "text": f"📁 پوشه پروژه: {PROJECT_ROOT}"})

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
            with _lock:
                _process = proc

            for line in proc.stdout:
                stripped = line.rstrip()
                if not stripped:
                    continue
                msg_type = "log"
                if "[ERROR]" in stripped or "Error" in stripped or "خطا" in stripped:
                    msg_type = "error"
                elif "[WARN]" in stripped or "هشدار" in stripped:
                    msg_type = "warn"
                elif "✓" in stripped or "موفق" in stripped or "DONE" in stripped:
                    msg_type = "success"
                elif "[PHASE-" in stripped:
                    msg_type = "phase"
                    # استخراج شماره فاز
                    import re
                    m = re.search(r"\[PHASE-(\d+)\]", stripped)
                    if m:
                        with _lock:
                            _run_state["phase"] = f"فاز {m.group(1)}"
                try:
                    _log_queue.put_nowait({"type": msg_type, "text": stripped})
                except queue.Full:
                    pass

            proc.wait()
            exit_code = proc.returncode

        except Exception as e:
            exit_code = -1
            _log_queue.put({"type": "error", "text": f"[GUI] خطا: {e}"})

        with _lock:
            _run_state["running"]   = False
            _run_state["phase"]     = None
            _run_state["ended_at"]  = datetime.now().isoformat()
            _run_state["exit_code"] = exit_code
            _process = None

        status = "✅ موفق" if exit_code == 0 else f"❌ خطا (کد {exit_code})"
        _log_queue.put({"type": "done", "text": f"▪ اجرا تمام شد: {status}"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop_run():
    global _process
    with _lock:
        proc = _process
    if proc and proc.poll() is None:
        proc.kill()
        _log_queue.put({"type": "warn", "text": "⏹ اجرا توسط کاربر متوقف شد"})
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "هیچ پروسه‌ای در حال اجرا نیست"})


@app.route("/api/status", methods=["GET"])
def get_status():
    with _lock:
        return jsonify({"ok": True, "data": dict(_run_state)})


# ══════════════════════════════════════════════════════════════════════════════
# API — SSE استریم لاگ
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/stream")
def stream():
    def generate():
        while True:
            try:
                msg = _log_queue.get(timeout=25)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# API — نتایج
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/results/mutants")
def get_mutants():
    path = OUTPUT_DIR / "mutants.json"
    if not path.exists():
        return jsonify({"ok": False, "error": "فایل mutants.json یافت نشد"})
    try:
        with open(path, encoding="utf-8") as f:
            return jsonify({"ok": True, "data": json.load(f)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/results/report")
def get_report():
    path = OUTPUT_DIR / "mutation_report.json"
    if not path.exists():
        return jsonify({"ok": False, "error": "گزارش موجود نیست. ابتدا pipeline را اجرا کنید."})
    try:
        with open(path, encoding="utf-8") as f:
            return jsonify({"ok": True, "data": json.load(f)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/results/suite")
def get_suite():
    path = OUTPUT_DIR / "minimum_test_suite.json"
    if not path.exists():
        return jsonify({"ok": False, "error": "مجموعه تست کمینه موجود نیست."})
    try:
        with open(path, encoding="utf-8") as f:
            return jsonify({"ok": True, "data": json.load(f)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/results/download/<filename>")
def download_result(filename: str):
    safe = {"mutation_report.json", "minimum_test_suite.json", "mutants.json"}
    if filename not in safe:
        return jsonify({"error": "فایل مجاز نیست"}), 403
    path = OUTPUT_DIR / filename
    if not path.exists():
        return jsonify({"error": "فایل یافت نشد"}), 404
    return send_file(path, as_attachment=True, download_name=filename)


# ══════════════════════════════════════════════════════════════════════════════
# API — API Key
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/env/apikey", methods=["GET"])
def check_apikey():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return jsonify({"ok": True, "set": bool(key), "prefix": key[:12] + "..." if key else ""})


@app.route("/api/env/apikey", methods=["POST"])
def set_apikey():
    body = request.get_json(force=True) or {}
    key  = body.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "کلید خالی است"}), 400
    os.environ["ANTHROPIC_API_KEY"] = key
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# صفحه HTML کامل
# ══════════════════════════════════════════════════════════════════════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mutation Testing System — رابط گرافیکی</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root {
  --bg-base:    #080c18;
  --bg-panel:   #0d1224;
  --bg-card:    #111828;
  --bg-hover:   #1a2338;
  --bg-input:   #0a1020;
  --border:     #1e2d4a;
  --border-lit: #2a4070;
  --text-pri:   #e2e8f8;
  --text-sec:   #8899bb;
  --text-dim:   #445577;
  --cyan:       #00d4ff;
  --cyan-dim:   #007899;
  --green:      #00e87a;
  --green-dim:  #006633;
  --amber:      #ffaa00;
  --amber-dim:  #664400;
  --red:        #ff4466;
  --red-dim:    #661122;
  --purple:     #aa66ff;
  --purple-dim: #440088;
  --font-ui:   'IBM Plex Sans Arabic', sans-serif;
  --font-code: 'JetBrains Mono', monospace;
  --r-sm:  6px;
  --r-md:  10px;
  --r-lg:  16px;
  --sh:    0 4px 24px rgba(0,0,0,0.6);
  --sh-glow-c: 0 0 20px rgba(0,212,255,0.15);
  --sh-glow-g: 0 0 20px rgba(0,232,122,0.15);
  --transition: 0.2s cubic-bezier(0.4,0,0.2,1);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;font-family:var(--font-ui);background:var(--bg-base);color:var(--text-pri);font-size:14px;line-height:1.6;overflow:hidden}

/* ── Layout ─────────────────────────────────────────────────────────────── */
#app{display:grid;grid-template-columns:220px 1fr;grid-template-rows:56px 1fr;height:100vh;gap:0}
#topbar{grid-column:1/-1;background:var(--bg-panel);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:16px;z-index:10}
#sidebar{background:var(--bg-panel);border-left:1px solid var(--border);display:flex;flex-direction:column;padding:12px 0;overflow-y:auto}
#main{overflow-y:auto;padding:0}

/* ── Topbar ─────────────────────────────────────────────────────────────── */
.logo{font-family:var(--font-code);font-size:13px;font-weight:700;color:var(--cyan);letter-spacing:.05em;white-space:nowrap}
.logo span{color:var(--text-dim)}
.topbar-right{margin-right:auto;display:flex;align-items:center;gap:12px}
.pill{display:inline-flex;align-items:center;gap:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:100px;padding:4px 12px;font-size:12px;color:var(--text-sec)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:var(--text-dim)}
.pill.running .dot{background:var(--cyan);animation:pulse 1.2s infinite}
.pill.success .dot{background:var(--green)}
.pill.error   .dot{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
#apikey-badge{cursor:pointer;transition:var(--transition)}
#apikey-badge:hover{border-color:var(--cyan);color:var(--cyan)}
.btn{display:inline-flex;align-items:center;gap:7px;padding:7px 16px;border-radius:var(--r-sm);border:1px solid transparent;font-family:var(--font-ui);font-size:13px;font-weight:500;cursor:pointer;transition:var(--transition);white-space:nowrap}
.btn-primary{background:var(--cyan);color:#000;border-color:var(--cyan)}
.btn-primary:hover{background:#00eeff;box-shadow:0 0 16px rgba(0,212,255,0.4)}
.btn-danger{background:transparent;color:var(--red);border-color:var(--red)}
.btn-danger:hover{background:var(--red);color:#fff}
.btn-ghost{background:transparent;color:var(--text-sec);border-color:var(--border)}
.btn-ghost:hover{background:var(--bg-hover);color:var(--text-pri);border-color:var(--border-lit)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-sm{padding:5px 12px;font-size:12px}
.icon{font-size:15px}

/* ── Sidebar ─────────────────────────────────────────────────────────────── */
.nav-section{padding:0 12px 4px;font-size:10px;font-weight:600;letter-spacing:.12em;color:var(--text-dim);text-transform:uppercase;margin-top:8px}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 16px;cursor:pointer;border-radius:var(--r-sm);margin:1px 8px;font-size:13px;color:var(--text-sec);transition:var(--transition);border:1px solid transparent}
.nav-item:hover{background:var(--bg-hover);color:var(--text-pri)}
.nav-item.active{background:rgba(0,212,255,0.1);color:var(--cyan);border-color:rgba(0,212,255,0.2)}
.nav-item .ni-icon{font-size:16px;min-width:20px;text-align:center}
.nav-item .ni-badge{margin-right:auto;background:var(--cyan);color:#000;border-radius:100px;font-size:10px;font-weight:700;padding:1px 7px;min-width:20px;text-align:center}
.sidebar-footer{margin-top:auto;padding:12px;border-top:1px solid var(--border)}
.sidebar-version{font-size:11px;color:var(--text-dim);text-align:center;font-family:var(--font-code)}

/* ── Tabs ─────────────────────────────────────────────────────────────── */
.tab-page{display:none;padding:24px;min-height:calc(100vh - 56px)}
.tab-page.active{display:block}

/* ── Cards ─────────────────────────────────────────────────────────────── */
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--r-lg);padding:20px;transition:var(--transition)}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.card-title{font-size:13px;font-weight:600;color:var(--text-sec);letter-spacing:.06em;text-transform:uppercase;display:flex;align-items:center;gap:8px}
.card-title .ct-icon{font-size:16px}

/* ── Dashboard ─────────────────────────────────────────────────────────── */
.dash-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
.stat-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--r-md);padding:16px 20px;position:relative;overflow:hidden;transition:var(--transition)}
.stat-card::before{content:'';position:absolute;top:0;right:0;width:3px;height:100%;background:var(--accent,var(--cyan))}
.stat-card:hover{border-color:var(--border-lit);transform:translateY(-1px)}
.stat-label{font-size:11px;font-weight:600;letter-spacing:.08em;color:var(--text-dim);text-transform:uppercase;margin-bottom:6px}
.stat-val{font-family:var(--font-code);font-size:28px;font-weight:700;color:var(--accent,var(--cyan));line-height:1}
.stat-sub{font-size:11px;color:var(--text-dim);margin-top:4px}
.stat-card.green{--accent:var(--green)}
.stat-card.amber{--accent:var(--amber)}
.stat-card.red{--accent:var(--red)}
.stat-card.purple{--accent:var(--purple)}

/* ── Phase Timeline ─────────────────────────────────────────────────────── */
.phases-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:24px}
.phase-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--r-md);padding:14px;text-align:center;position:relative;transition:var(--transition);cursor:default}
.phase-card.active{border-color:var(--cyan);box-shadow:var(--sh-glow-c)}
.phase-card.done{border-color:var(--green);box-shadow:var(--sh-glow-g)}
.phase-card.error{border-color:var(--red)}
.phase-num{font-family:var(--font-code);font-size:11px;font-weight:700;color:var(--text-dim);margin-bottom:4px}
.phase-icon{font-size:22px;margin-bottom:4px;display:block}
.phase-name{font-size:11px;font-weight:600;color:var(--text-sec);line-height:1.3}
.phase-status{position:absolute;top:8px;left:8px;width:8px;height:8px;border-radius:50%;background:var(--text-dim)}
.phase-card.active .phase-status{background:var(--cyan);animation:pulse 1s infinite}
.phase-card.done  .phase-status{background:var(--green)}
.phase-card.error .phase-status{background:var(--red)}

/* ── Run Panel ─────────────────────────────────────────────────────────── */
.run-options{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.toggle-opt{display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;font-size:13px;color:var(--text-sec)}
.toggle-opt input[type=checkbox]{width:16px;height:16px;accent-color:var(--cyan)}

/* ── Log Panel ─────────────────────────────────────────────────────────── */
#log-panel{background:var(--bg-input);border:1px solid var(--border);border-radius:var(--r-md);height:420px;overflow-y:auto;font-family:var(--font-code);font-size:12px;padding:12px;scroll-behavior:smooth}
.log-line{padding:1px 0;border-bottom:1px solid rgba(255,255,255,0.02)}
.log-line.type-phase{color:var(--cyan);font-weight:500}
.log-line.type-success{color:var(--green)}
.log-line.type-error{color:var(--red)}
.log-line.type-warn{color:var(--amber)}
.log-line.type-info{color:var(--purple)}
.log-line.type-start,.log-line.type-done{color:var(--cyan);font-weight:700;border-top:1px solid var(--border);margin-top:4px;padding-top:4px}
.log-line.type-log{color:var(--text-sec)}

/* ── Config Editor ─────────────────────────────────────────────────────── */
.config-two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.form-group{margin-bottom:16px}
.form-label{display:block;font-size:12px;font-weight:600;color:var(--text-sec);margin-bottom:6px;letter-spacing:.04em}
.form-input{width:100%;background:var(--bg-input);border:1px solid var(--border);border-radius:var(--r-sm);padding:8px 12px;color:var(--text-pri);font-family:var(--font-ui);font-size:13px;outline:none;transition:var(--transition)}
.form-input:focus{border-color:var(--cyan);box-shadow:0 0 0 2px rgba(0,212,255,0.1)}
.form-input[type=number]{font-family:var(--font-code)}
textarea.form-input{resize:vertical;font-family:var(--font-code);font-size:12px;line-height:1.6}
.form-check{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-sec);cursor:pointer}
.form-check input[type=checkbox]{width:15px;height:15px;accent-color:var(--cyan)}
.section-divider{border:none;border-top:1px solid var(--border);margin:20px 0}
.config-section-title{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--cyan);margin-bottom:14px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.tag-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.tag{display:inline-flex;align-items:center;gap:6px;background:var(--bg-hover);border:1px solid var(--border);border-radius:100px;padding:3px 10px;font-size:12px;font-family:var(--font-code);color:var(--text-sec)}
.tag.active{background:rgba(0,212,255,0.1);border-color:var(--cyan);color:var(--cyan)}
.tag{cursor:pointer;transition:var(--transition)}
.tag:hover{border-color:var(--border-lit)}

/* ── Code Editor ─────────────────────────────────────────────────────────── */
#code-editor{width:100%;height:500px;background:var(--bg-input);border:1px solid var(--border);border-radius:var(--r-md);padding:16px;color:#cdd9e5;font-family:var(--font-code);font-size:13px;line-height:1.7;outline:none;resize:vertical;tab-size:4;direction:ltr;text-align:left}
#code-editor:focus{border-color:var(--cyan)}
.editor-toolbar{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.editor-info{font-size:11px;color:var(--text-dim);font-family:var(--font-code)}

/* ── Results ─────────────────────────────────────────────────────────────── */
.results-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
.score-display{text-align:center;padding:24px}
.score-ring{position:relative;width:140px;height:140px;margin:0 auto 12px}
.score-ring canvas{position:absolute;top:0;left:0}
.score-number{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-family:var(--font-code);font-size:28px;font-weight:700}
.score-label{font-size:12px;color:var(--text-dim);margin-top:4px}

/* ── Mutants Table ─────────────────────────────────────────────────────── */
.table-wrap{overflow-x:auto}
.mtable{width:100%;border-collapse:collapse;font-size:12px}
.mtable th{background:var(--bg-input);color:var(--text-dim);font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:8px 12px;text-align:right;border-bottom:1px solid var(--border);white-space:nowrap}
.mtable td{padding:8px 12px;border-bottom:1px solid rgba(255,255,255,0.04);color:var(--text-sec);vertical-align:middle;font-family:var(--font-code);font-size:11px}
.mtable tr:hover td{background:var(--bg-hover)}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:100px;font-size:10px;font-weight:700;white-space:nowrap}
.badge-killed{background:rgba(0,232,122,0.12);color:var(--green);border:1px solid rgba(0,232,122,0.3)}
.badge-alive{background:rgba(255,68,102,0.12);color:var(--red);border:1px solid rgba(255,68,102,0.3)}
.badge-suspected{background:rgba(255,170,0,0.12);color:var(--amber);border:1px solid rgba(255,170,0,0.3)}
.badge-arith{background:rgba(0,212,255,0.1);color:var(--cyan);border:1px solid rgba(0,212,255,0.2)}
.badge-rel{background:rgba(170,102,255,0.1);color:var(--purple);border:1px solid rgba(170,102,255,0.2)}
.badge-int{background:rgba(255,170,0,0.1);color:var(--amber);border:1px solid rgba(255,170,0,0.2)}

/* ── Filter Bar ─────────────────────────────────────────────────────────── */
.filter-bar{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
.filter-chip{padding:4px 12px;border-radius:100px;border:1px solid var(--border);background:transparent;color:var(--text-sec);font-size:11px;cursor:pointer;transition:var(--transition)}
.filter-chip:hover{border-color:var(--border-lit);color:var(--text-pri)}
.filter-chip.active{background:rgba(0,212,255,0.1);border-color:var(--cyan);color:var(--cyan)}
.search-box{background:var(--bg-input);border:1px solid var(--border);border-radius:var(--r-sm);padding:5px 12px;color:var(--text-pri);font-family:var(--font-code);font-size:12px;outline:none;width:200px;transition:var(--transition);direction:ltr}
.search-box:focus{border-color:var(--cyan)}

/* ── Min Test Suite ─────────────────────────────────────────────────────── */
.suite-item{background:var(--bg-input);border:1px solid var(--border);border-radius:var(--r-sm);padding:10px 14px;margin-bottom:8px;transition:var(--transition)}
.suite-item:hover{border-color:var(--border-lit)}
.suite-item-header{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.suite-id{font-family:var(--font-code);font-size:11px;font-weight:700;color:var(--cyan)}
.suite-fn{font-size:12px;color:var(--text-sec)}
.suite-kills{display:flex;gap:4px;flex-wrap:wrap}
.kill-chip{font-family:var(--font-code);font-size:10px;padding:1px 7px;background:rgba(0,232,122,0.08);border:1px solid rgba(0,232,122,0.2);border-radius:100px;color:var(--green)}
.suite-input{font-family:var(--font-code);font-size:11px;color:var(--text-dim);direction:ltr;text-align:left;margin-top:4px;word-break:break-all}

/* ── Modal ─────────────────────────────────────────────────────────────── */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:100;align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
.modal{background:var(--bg-panel);border:1px solid var(--border-lit);border-radius:var(--r-lg);padding:28px;width:440px;max-width:90vw;box-shadow:var(--sh)}
.modal-title{font-size:15px;font-weight:600;color:var(--text-pri);margin-bottom:16px;display:flex;align-items:center;gap:8px}
.modal-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}

/* ── Misc ─────────────────────────────────────────────────────────────── */
.empty-state{text-align:center;padding:48px;color:var(--text-dim)}
.empty-state .es-icon{font-size:40px;margin-bottom:12px;display:block;opacity:.4}
.empty-state .es-text{font-size:13px}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.row-between{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.mt-8{margin-top:8px}
.mt-16{margin-top:16px}
.mt-24{margin-top:24px}
.text-sm{font-size:12px}
.text-dim{color:var(--text-dim)}
.text-code{font-family:var(--font-code)}
.text-green{color:var(--green)}
.text-red{color:var(--red)}
.text-amber{color:var(--amber)}
.text-cyan{color:var(--cyan)}
.gap-grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.gap-grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
.progress-bar{height:6px;background:var(--bg-input);border-radius:100px;overflow:hidden;margin-top:4px}
.progress-fill{height:100%;border-radius:100px;background:var(--cyan);transition:width 0.5s ease}
.progress-fill.green{background:var(--green)}
.progress-fill.red{background:var(--red)}
.progress-fill.amber{background:var(--amber)}
.scroll-x{overflow-x:auto}
hr.divider{border:none;border-top:1px solid var(--border);margin:20px 0}
</style>
</head>
<body>
<div id="app">

<!-- ═══ TOPBAR ═══════════════════════════════════════════════════════════ -->
<header id="topbar">
  <div class="logo">🧬 <span>Mutation</span>Testing<span>.gui</span></div>
  <div class="topbar-right">
    <div id="run-status" class="pill">
      <span class="dot"></span>
      <span id="run-status-text">آماده</span>
    </div>
    <div id="apikey-badge" class="pill" onclick="openApiKeyModal()" title="کلیک برای تنظیم API Key">
      🔑 <span id="apikey-text">بررسی...</span>
    </div>
    <button class="btn btn-primary btn-sm" id="btn-run-top" onclick="quickRun()">
      <span class="icon">▶</span> اجرای کامل
    </button>
    <button class="btn btn-danger btn-sm" id="btn-stop-top" onclick="stopRun()" disabled>
      <span class="icon">⏹</span> توقف
    </button>
  </div>
</header>

<!-- ═══ SIDEBAR ══════════════════════════════════════════════════════════ -->
<nav id="sidebar">
  <div class="nav-section">اصلی</div>
  <div class="nav-item active" onclick="showTab('dashboard')">
    <span class="ni-icon">📊</span> داشبورد
  </div>
  <div class="nav-item" onclick="showTab('run')">
    <span class="ni-icon">⚡</span> اجرا و لاگ
  </div>
  <div class="nav-section">پیکربندی</div>
  <div class="nav-item" onclick="showTab('config')">
    <span class="ni-icon">⚙️</span> تنظیمات
  </div>
  <div class="nav-item" onclick="showTab('code')">
    <span class="ni-icon">📝</span> کد هدف
  </div>
  <div class="nav-section">نتایج</div>
  <div class="nav-item" onclick="showTab('results')">
    <span class="ni-icon">🎯</span> نتایج
    <span class="ni-badge" id="score-badge" style="display:none">—</span>
  </div>
  <div class="nav-item" onclick="showTab('mutants')">
    <span class="ni-icon">🦠</span> Mutant‌ها
    <span class="ni-badge" id="mutants-badge" style="display:none">0</span>
  </div>
  <div class="nav-item" onclick="showTab('suite')">
    <span class="ni-icon">✅</span> مجموعه تست کمینه
  </div>
  <div class="sidebar-footer">
    <div class="sidebar-version">v2.0 — mutation-testing</div>
  </div>
</nav>

<!-- ═══ MAIN ══════════════════════════════════════════════════════════════ -->
<main id="main">

<!-- ─── داشبورد ──────────────────────────────────────────────────────── -->
<div class="tab-page active" id="tab-dashboard">
  <div class="row-between" style="margin-bottom:20px">
    <h1 style="font-size:18px;font-weight:600;color:var(--text-pri)">داشبورد — نمای کلی سیستم</h1>
    <button class="btn btn-ghost btn-sm" onclick="refreshDashboard()">🔄 به‌روزرسانی</button>
  </div>

  <!-- آمار کلی -->
  <div class="dash-grid" id="dash-stats">
    <div class="stat-card" style="--accent:var(--cyan)">
      <div class="stat-label">Adjusted Score</div>
      <div class="stat-val" id="stat-adj-score">—</div>
      <div class="stat-sub">امتیاز تنظیم‌شده</div>
    </div>
    <div class="stat-card green">
      <div class="stat-label">Mutant‌های کشته‌شده</div>
      <div class="stat-val" id="stat-killed">—</div>
      <div class="stat-sub">از کل <span id="stat-total">—</span> Mutant</div>
    </div>
    <div class="stat-card amber">
      <div class="stat-label">Suspected Equivalent</div>
      <div class="stat-val" id="stat-suspected">—</div>
      <div class="stat-sub">احتمالاً معادل</div>
    </div>
    <div class="stat-card" style="--accent:var(--purple)">
      <div class="stat-label">تست‌های کمینه</div>
      <div class="stat-val" id="stat-min-tests">—</div>
      <div class="stat-sub">از استخر <span id="stat-pool">—</span> تست</div>
    </div>
  </div>

  <!-- فازها -->
  <div class="card" style="margin-bottom:20px">
    <div class="card-header">
      <div class="card-title"><span class="ct-icon">🔁</span> فازهای pipeline</div>
    </div>
    <div class="phases-grid">
      <div class="phase-card" id="ph-1"><span class="phase-status"></span><div class="phase-num">فاز ۱</div><span class="phase-icon">🧬</span><div class="phase-name">تولید Mutant</div></div>
      <div class="phase-card" id="ph-2"><span class="phase-status"></span><div class="phase-num">فاز ۲</div><span class="phase-icon">🧪</span><div class="phase-name">استخر تست</div></div>
      <div class="phase-card" id="ph-3"><span class="phase-status"></span><div class="phase-num">فاز ۳</div><span class="phase-icon">📊</span><div class="phase-name">ماتریس پوشش</div></div>
      <div class="phase-card" id="ph-4"><span class="phase-status"></span><div class="phase-num">فاز ۴</div><span class="phase-icon">🧮</span><div class="phase-name">ILP — کمینه‌سازی</div></div>
      <div class="phase-card" id="ph-5"><span class="phase-status"></span><div class="phase-num">فاز ۵</div><span class="phase-icon">🔄</span><div class="phase-name">حلقه بازخورد</div></div>
      <div class="phase-card" id="ph-6"><span class="phase-status"></span><div class="phase-num">فاز ۶</div><span class="phase-icon">📋</span><div class="phase-name">گزارش نهایی</div></div>
    </div>
  </div>

  <!-- اجرای سریع + اطلاعات پروژه -->
  <div class="gap-grid-2">
    <div class="card">
      <div class="card-header">
        <div class="card-title"><span class="ct-icon">⚡</span> اجرای سریع</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:12px">
        <label class="toggle-opt"><input type="checkbox" id="dash-no-llm"> اجرا بدون LLM (سریع‌تر)</label>
        <label class="toggle-opt"><input type="checkbox" id="dash-verbose"> خروجی verbose</label>
        <div class="row" style="margin-top:4px">
          <button class="btn btn-primary" id="btn-run-dash" onclick="quickRun()"><span class="icon">▶</span> شروع pipeline</button>
          <button class="btn btn-danger" id="btn-stop-dash" onclick="stopRun()" disabled><span class="icon">⏹</span> توقف</button>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <div class="card-title"><span class="ct-icon">📁</span> اطلاعات پروژه</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:8px;font-size:12px">
        <div><span class="text-dim">کد هدف: </span><span class="text-code text-cyan" id="proj-target">—</span></div>
        <div><span class="text-dim">توابع: </span><span id="proj-functions">—</span></div>
        <div><span class="text-dim">Score هدف: </span><span class="text-code text-green" id="proj-target-score">—</span></div>
        <div><span class="text-dim">مدل LLM: </span><span class="text-code" id="proj-model">—</span></div>
        <div><span class="text-dim">دورهای بازخورد: </span><span class="text-code" id="proj-feedback">—</span></div>
        <div style="margin-top:8px">
          <div class="text-dim" style="font-size:11px;margin-bottom:4px">امتیاز فعلی</div>
          <div class="progress-bar"><div class="progress-fill" id="score-progress" style="width:0%"></div></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ─── اجرا و لاگ ────────────────────────────────────────────────────── -->
<div class="tab-page" id="tab-run">
  <div class="row-between" style="margin-bottom:20px">
    <h1 style="font-size:18px;font-weight:600">اجرا و لاگ real-time</h1>
    <div class="row">
      <button class="btn btn-ghost btn-sm" onclick="clearLog()">🗑 پاک‌سازی لاگ</button>
      <button class="btn btn-ghost btn-sm" onclick="downloadLog()">💾 دانلود لاگ</button>
    </div>
  </div>

  <div class="card" style="margin-bottom:16px">
    <div class="card-header">
      <div class="card-title"><span class="ct-icon">🚀</span> گزینه‌های اجرا</div>
    </div>
    <div class="run-options">
      <label class="toggle-opt"><input type="checkbox" id="run-no-llm"> بدون LLM</label>
      <label class="toggle-opt"><input type="checkbox" id="run-verbose"> verbose</label>
      <button class="btn btn-primary" id="btn-run-main" onclick="startRun()"><span class="icon">▶</span> اجرای pipeline</button>
      <button class="btn btn-danger" id="btn-stop-main" onclick="stopRun()" disabled><span class="icon">⏹</span> توقف اجرا</button>
      <span class="text-dim text-sm" id="run-timer"></span>
    </div>
  </div>

  <div class="card">
    <div class="card-header">
      <div class="card-title"><span class="ct-icon">📟</span> خروجی لاگ</div>
      <div id="log-line-count" class="text-dim text-sm">0 خط</div>
    </div>
    <div id="log-panel"></div>
  </div>
</div>

<!-- ─── تنظیمات ──────────────────────────────────────────────────────── -->
<div class="tab-page" id="tab-config">
  <div class="row-between" style="margin-bottom:20px">
    <h1 style="font-size:18px;font-weight:600">تنظیمات سیستم</h1>
    <div class="row">
      <button class="btn btn-ghost btn-sm" onclick="loadConfig()">🔄 بارگذاری مجدد</button>
      <button class="btn btn-primary btn-sm" onclick="saveConfig()">💾 ذخیره تنظیمات</button>
    </div>
  </div>

  <div id="config-save-msg" style="display:none;margin-bottom:16px" class="card" style="padding:10px 16px">
    <span class="text-green">✅ تنظیمات با موفقیت ذخیره شد</span>
  </div>

  <div class="config-two-col">
    <!-- ستون چپ -->
    <div>
      <div class="card" style="margin-bottom:16px">
        <div class="config-section-title">📁 پروژه</div>
        <div class="form-group">
          <label class="form-label">فایل هدف (target_file)</label>
          <input type="text" class="form-input" id="cfg-target-file" placeholder="target_code.py">
        </div>
        <div class="form-group">
          <label class="form-label">پوشه خروجی (output_dir)</label>
          <input type="text" class="form-input" id="cfg-output-dir" placeholder="output/">
        </div>
      </div>

      <div class="card" style="margin-bottom:16px">
        <div class="config-section-title">🤖 LLM</div>
        <div class="form-group">
          <label class="form-label">مدل (model)</label>
          <input type="text" class="form-input text-code" id="cfg-llm-model" placeholder="claude-sonnet-4-20250514">
        </div>
        <div class="form-group">
          <label class="form-label">حداکثر توکن (max_tokens)</label>
          <input type="number" class="form-input" id="cfg-llm-tokens" min="100" max="8000">
        </div>
        <div class="form-group">
          <label class="form-label">API Base URL</label>
          <input type="text" class="form-input text-code" id="cfg-llm-base">
        </div>
      </div>

      <div class="card">
        <div class="config-section-title">📦 Sandbox</div>
        <div class="form-group">
          <label class="form-label">Timeout (ثانیه)</label>
          <input type="number" class="form-input" id="cfg-timeout" min="1" max="60">
        </div>
        <div class="form-group">
          <label class="form-check">
            <input type="checkbox" id="cfg-multiprocess">
            استفاده از multiprocessing
          </label>
        </div>
      </div>
    </div>

    <!-- ستون راست -->
    <div>
      <div class="card" style="margin-bottom:16px">
        <div class="config-section-title">🧬 جهش (Mutation)</div>
        <div class="form-group">
          <label class="form-check">
            <input type="checkbox" id="cfg-mut-arith">
            عملگرهای حسابی (ARITH: +, -, *, /)
          </label>
        </div>
        <div class="form-group">
          <label class="form-check">
            <input type="checkbox" id="cfg-mut-rel">
            عملگرهای رابطه‌ای (REL: &lt;, &gt;, ==, !=)
          </label>
        </div>
        <div class="form-group">
          <label class="form-check">
            <input type="checkbox" id="cfg-mut-logical">
            عملگرهای منطقی (LOGICAL: and, or)
          </label>
        </div>
        <div class="form-group">
          <label class="form-label">عملگرهای Integration (کلیک برای فعال/غیرفعال)</label>
          <div class="tag-list" id="int-ops-tags">
            <span class="tag" data-op="IPVR" onclick="toggleIntOp('IPVR')">IPVR <small>range-1</small></span>
            <span class="tag" data-op="IUOI" onclick="toggleIntOp('IUOI')">IUOI <small>not cond</small></span>
            <span class="tag" data-op="IORC" onclick="toggleIntOp('IORC')">IORC <small>swap ops</small></span>
            <span class="tag" data-op="ISMA" onclick="toggleIntOp('ISMA')">ISMA <small>arr[i+1]</small></span>
            <span class="tag" data-op="IMCD" onclick="toggleIntOp('IMCD')">IMCD <small>len→0</small></span>
          </div>
        </div>
      </div>

      <div class="card" style="margin-bottom:16px">
        <div class="config-section-title">🎯 بهینه‌ساز (Optimizer)</div>
        <div class="form-group">
          <label class="form-label">Score هدف (%)</label>
          <input type="number" class="form-input" id="cfg-target-score" min="0" max="100" step="0.5">
        </div>
        <div class="form-group">
          <label class="form-label">حداکثر دور بازخورد</label>
          <input type="number" class="form-input" id="cfg-feedback-rounds" min="0" max="10">
        </div>
        <div class="form-group">
          <label class="form-label">حداکثر Mutant per round</label>
          <input type="number" class="form-input" id="cfg-feedback-mutants" min="1" max="50">
        </div>
        <div class="form-group">
          <label class="form-label">حداکثر تست جدید per mutant</label>
          <input type="number" class="form-input" id="cfg-feedback-new" min="1" max="10">
        </div>
      </div>

      <div class="card">
        <div class="config-section-title">🧪 استخر تست (Test Pool)</div>
        <div class="form-group">
          <label class="form-label">حداکثر ACOC per function</label>
          <input type="number" class="form-input" id="cfg-acoc-max" min="10" max="1000">
        </div>
        <div class="form-group">
          <label class="form-label">دمای LLM</label>
          <input type="number" class="form-input" id="cfg-llm-temp" min="0" max="1" step="0.05">
        </div>
        <div class="form-group">
          <label class="form-label">تعداد تست LLM per representative</label>
          <input type="number" class="form-input" id="cfg-llm-per-rep" min="1" max="20">
        </div>
        <div class="form-group">
          <label class="form-check">
            <input type="checkbox" id="cfg-llm-enabled">
            LLM فعال باشد
          </label>
        </div>
      </div>
    </div>
  </div>

  <!-- توابع -->
  <div class="card" style="margin-top:16px">
    <div class="card-header">
      <div class="config-section-title" style="margin:0">⚡ توابع (Functions)</div>
    </div>
    <div id="functions-editor" style="margin-top:12px">
      <div class="text-dim text-sm">در حال بارگذاری...</div>
    </div>
    <div style="margin-top:12px;padding:12px;background:var(--bg-input);border-radius:var(--r-sm);border:1px solid var(--border)">
      <div class="form-label">ویرایش پیشرفته — JSON خام</div>
      <textarea class="form-input" id="cfg-raw-json" rows="8" style="direction:ltr;text-align:left;font-size:11px"></textarea>
      <div class="row" style="margin-top:8px">
        <button class="btn btn-ghost btn-sm" onclick="applyRawJson()">اعمال JSON</button>
        <button class="btn btn-ghost btn-sm" onclick="refreshRawJson()">🔄 به‌روزرسانی</button>
      </div>
    </div>
  </div>
</div>

<!-- ─── کد هدف ───────────────────────────────────────────────────────── -->
<div class="tab-page" id="tab-code">
  <div class="row-between" style="margin-bottom:20px">
    <h1 style="font-size:18px;font-weight:600">ویرایش کد هدف</h1>
    <div class="row">
      <button class="btn btn-ghost btn-sm" onclick="loadTargetCode()">🔄 بارگذاری</button>
      <button class="btn btn-primary btn-sm" onclick="saveTargetCode()">💾 ذخیره</button>
    </div>
  </div>
  <div class="card">
    <div class="editor-toolbar">
      <span class="editor-info" id="code-file-path">📄 target_code.py</span>
      <span class="editor-info" id="code-line-count" style="margin-right:auto"></span>
      <button class="btn btn-ghost btn-sm" onclick="formatCode()">🔧 قالب‌بندی</button>
    </div>
    <textarea id="code-editor" spellcheck="false" oninput="onCodeChange()" onkeydown="handleTab(event)"></textarea>
    <div id="code-save-msg" style="display:none;margin-top:10px" class="text-green text-sm">✅ کد با موفقیت ذخیره شد</div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-header">
      <div class="card-title"><span class="ct-icon">ℹ️</span> راهنما</div>
    </div>
    <div style="font-size:12px;color:var(--text-sec);line-height:1.8">
      <p>• توابعی که در <strong class="text-cyan">config.json</strong> تعریف کرده‌اید باید اینجا پیاده‌سازی شوند.</p>
      <p>• پس از ذخیره کد، <strong class="text-cyan">pipeline را مجدداً اجرا کنید</strong> تا Mutant‌های جدید تولید شوند.</p>
      <p>• می‌توانید هر تابع Python استاندارد بنویسید — سیستم به‌صورت خودکار AST را تحلیل می‌کند.</p>
      <p>• از <strong class="text-code">Tab</strong> برای indent استفاده کنید. ذخیره: <strong class="text-code">Ctrl+S</strong></p>
    </div>
  </div>
</div>

<!-- ─── نتایج ─────────────────────────────────────────────────────────── -->
<div class="tab-page" id="tab-results">
  <div class="row-between" style="margin-bottom:20px">
    <h1 style="font-size:18px;font-weight:600">نتایج تحلیل جهش</h1>
    <div class="row">
      <button class="btn btn-ghost btn-sm" onclick="loadResults()">🔄 به‌روزرسانی</button>
      <button class="btn btn-ghost btn-sm" onclick="downloadReport('mutation_report.json')">📥 دانلود گزارش</button>
    </div>
  </div>

  <div id="results-empty" class="card">
    <div class="empty-state">
      <span class="es-icon">📊</span>
      <div class="es-text">هنوز گزارشی موجود نیست. <br>ابتدا pipeline را اجرا کنید.</div>
    </div>
  </div>

  <div id="results-content" style="display:none">
    <!-- Score Cards -->
    <div class="dash-grid" id="result-stats"></div>

    <!-- نمودارها -->
    <div class="gap-grid-2" style="margin-top:16px;margin-bottom:16px">
      <div class="card">
        <div class="card-header"><div class="card-title"><span class="ct-icon">🎯</span> امتیاز جهش</div></div>
        <div style="position:relative;height:220px">
          <canvas id="score-chart"></canvas>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title"><span class="ct-icon">🦠</span> توزیع Mutant‌ها</div></div>
        <div style="position:relative;height:220px">
          <canvas id="mutant-dist-chart"></canvas>
        </div>
      </div>
    </div>

    <!-- جزئیات -->
    <div class="gap-grid-2" style="margin-bottom:16px">
      <div class="card">
        <div class="card-header"><div class="card-title"><span class="ct-icon">📈</span> امتیاز خام vs تنظیم‌شده</div></div>
        <canvas id="score-compare-chart" height="160"></canvas>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title"><span class="ct-icon">ℹ️</span> توضیحات</div></div>
        <div id="result-explanation" style="font-size:12px;color:var(--text-sec);line-height:1.8"></div>
      </div>
    </div>

    <div class="row" style="gap:8px">
      <button class="btn btn-ghost btn-sm" onclick="downloadReport('mutation_report.json')">📥 mutation_report.json</button>
      <button class="btn btn-ghost btn-sm" onclick="downloadReport('minimum_test_suite.json')">📥 minimum_test_suite.json</button>
      <button class="btn btn-ghost btn-sm" onclick="downloadReport('mutants.json')">📥 mutants.json</button>
    </div>
  </div>
</div>

<!-- ─── Mutant‌ها ────────────────────────────────────────────────────── -->
<div class="tab-page" id="tab-mutants">
  <div class="row-between" style="margin-bottom:20px">
    <h1 style="font-size:18px;font-weight:600">لیست Mutant‌ها</h1>
    <button class="btn btn-ghost btn-sm" onclick="loadMutants()">🔄 به‌روزرسانی</button>
  </div>

  <div class="card">
    <div class="filter-bar">
      <span class="filter-chip active" data-filter="all" onclick="filterMutants('all',this)">همه</span>
      <span class="filter-chip" data-filter="KILLED" onclick="filterMutants('KILLED',this)">🟢 کشته</span>
      <span class="filter-chip" data-filter="ALIVE" onclick="filterMutants('ALIVE',this)">🔴 زنده</span>
      <span class="filter-chip" data-filter="SUSPECTED" onclick="filterMutants('SUSPECTED',this)">🟡 Suspected</span>
      <span class="filter-chip" data-filter="ARITH" onclick="filterMutants('ARITH',this)">ARITH</span>
      <span class="filter-chip" data-filter="REL" onclick="filterMutants('REL',this)">REL</span>
      <span class="filter-chip" data-filter="INTEGRATION" onclick="filterMutants('INTEGRATION',this)">INT</span>
      <input class="search-box" type="text" placeholder="جستجو..." id="mutant-search" oninput="filterMutants(null,null)">
    </div>
    <div class="table-wrap">
      <table class="mtable">
        <thead>
          <tr>
            <th>ID</th><th>وضعیت</th><th>دسته</th><th>عملگر</th><th>تابع</th><th>خط</th><th>خط اصلی</th>
          </tr>
        </thead>
        <tbody id="mutants-tbody">
          <tr><td colspan="7" style="text-align:center;padding:24px;color:var(--text-dim)">در حال بارگذاری...</td></tr>
        </tbody>
      </table>
    </div>
    <div id="mutants-count" class="text-dim text-sm" style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border)"></div>
  </div>
</div>

<!-- ─── مجموعه تست کمینه ─────────────────────────────────────────────── -->
<div class="tab-page" id="tab-suite">
  <div class="row-between" style="margin-bottom:20px">
    <h1 style="font-size:18px;font-weight:600">مجموعه تست کمینه</h1>
    <div class="row">
      <button class="btn btn-ghost btn-sm" onclick="loadSuite()">🔄 به‌روزرسانی</button>
      <button class="btn btn-ghost btn-sm" onclick="downloadReport('minimum_test_suite.json')">📥 دانلود</button>
    </div>
  </div>
  <div class="card" style="margin-bottom:16px">
    <div id="suite-summary" class="row" style="gap:24px">
      <div><span class="text-dim text-sm">تعداد تست: </span><span class="text-code text-cyan" id="suite-count">—</span></div>
      <div><span class="text-dim text-sm">امتیاز: </span><span class="text-code text-green" id="suite-score">—</span></div>
      <div><span class="text-dim text-sm">تولید‌شده: </span><span class="text-code" id="suite-date">—</span></div>
    </div>
  </div>
  <div id="suite-list">
    <div class="empty-state"><span class="es-icon">✅</span><div class="es-text">هنوز مجموعه تست کمینه‌ای موجود نیست.</div></div>
  </div>
</div>

</main><!-- end #main -->
</div><!-- end #app -->

<!-- ═══ API KEY MODAL ═══════════════════════════════════════════════════════ -->
<div class="modal-overlay" id="apikey-modal">
  <div class="modal">
    <div class="modal-title">🔑 تنظیم API Key آنتروپیک</div>
    <p class="text-dim text-sm" style="margin-bottom:16px">کلید در متغیر محیطی ANTHROPIC_API_KEY ذخیره می‌شود (فقط برای این session).</p>
    <div class="form-group">
      <label class="form-label">ANTHROPIC_API_KEY</label>
      <input type="password" class="form-input text-code" id="apikey-input" placeholder="sk-ant-...">
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost btn-sm" onclick="closeApiKeyModal()">انصراف</button>
      <button class="btn btn-primary btn-sm" onclick="setApiKey()">ذخیره کلید</button>
    </div>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
let _config     = null;
let _allMutants = [];
let _mutFilter  = 'all';
let _scoreChart = null, _distChart = null, _compareChart = null;
let _sse        = null;
let _logLines   = 0;
let _runStart   = null;
let _timerInterval = null;
let _statusInterval = null;

// ═══════════════════════════════════════════════════════════════════════════
// Navigation
// ═══════════════════════════════════════════════════════════════════════════
function showTab(name) {
  document.querySelectorAll('.tab-page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  // mark nav item
  document.querySelectorAll('.nav-item').forEach(n => {
    if (n.getAttribute('onclick') === `showTab('${name}')`) n.classList.add('active');
  });
  // lazy load
  if (name === 'results')  loadResults();
  if (name === 'mutants')  loadMutants();
  if (name === 'suite')    loadSuite();
  if (name === 'config')   loadConfig();
  if (name === 'code')     loadTargetCode();
}

// ═══════════════════════════════════════════════════════════════════════════
// Config
// ═══════════════════════════════════════════════════════════════════════════
async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const j = await r.json();
    if (!j.ok) return;
    _config = j.data;
    populateConfigForm(_config);
    refreshRawJson();
  } catch(e) { console.error(e); }
}

function populateConfigForm(cfg) {
  const proj = cfg.project || {};
  const llm  = cfg.llm     || {};
  const sb   = cfg.sandbox  || {};
  const mut  = cfg.mutation || {};
  const opt  = cfg.optimizer|| {};
  const pool = cfg.test_pool|| {};

  setVal('cfg-target-file', proj.target_file || '');
  setVal('cfg-output-dir',  proj.output_dir  || '');
  setVal('cfg-llm-model',   llm.model        || '');
  setVal('cfg-llm-tokens',  llm.max_tokens   || 1000);
  setVal('cfg-llm-base',    llm.api_base     || '');
  setVal('cfg-timeout',     sb.timeout_seconds || 2);
  setChk('cfg-multiprocess',sb.use_multiprocessing !== false);
  setChk('cfg-mut-arith',   mut.arithmetic_operators !== false);
  setChk('cfg-mut-rel',     mut.relational_operators !== false);
  setChk('cfg-mut-logical', mut.logical_operators === true);
  setVal('cfg-target-score',opt.target_score || 90);
  setVal('cfg-feedback-rounds', opt.feedback_max_rounds || 3);
  setVal('cfg-feedback-mutants',opt.feedback_max_mutants_per_round || 10);
  setVal('cfg-feedback-new',    opt.feedback_max_new_tests_per_mutant || 2);
  setVal('cfg-acoc-max',  pool.acoc_max_per_function || 200);
  setVal('cfg-llm-temp',  pool.llm_temperature       || 0.85);
  setVal('cfg-llm-per-rep',pool.llm_tests_per_representative || 4);
  setChk('cfg-llm-enabled', pool.llm_enabled !== false);

  // integration ops tags
  const enabled = mut.integration_operators || [];
  document.querySelectorAll('#int-ops-tags .tag').forEach(t => {
    t.classList.toggle('active', enabled.includes(t.dataset.op));
  });
}

function toggleIntOp(op) {
  const tag = document.querySelector(`[data-op="${op}"]`);
  tag.classList.toggle('active');
}

function buildConfigFromForm() {
  const cfg = _config ? JSON.parse(JSON.stringify(_config)) : {};
  cfg.project = cfg.project || {};
  cfg.project.target_file = getVal('cfg-target-file');
  cfg.project.output_dir  = getVal('cfg-output-dir');

  cfg.llm = cfg.llm || {};
  cfg.llm.model      = getVal('cfg-llm-model');
  cfg.llm.max_tokens = parseInt(getVal('cfg-llm-tokens')) || 1000;
  cfg.llm.api_base   = getVal('cfg-llm-base');

  cfg.sandbox = cfg.sandbox || {};
  cfg.sandbox.timeout_seconds      = parseInt(getVal('cfg-timeout')) || 2;
  cfg.sandbox.use_multiprocessing  = getChk('cfg-multiprocess');

  cfg.mutation = cfg.mutation || {};
  cfg.mutation.arithmetic_operators = getChk('cfg-mut-arith');
  cfg.mutation.relational_operators = getChk('cfg-mut-rel');
  cfg.mutation.logical_operators    = getChk('cfg-mut-logical');
  cfg.mutation.integration_operators = [...document.querySelectorAll('#int-ops-tags .tag.active')].map(t=>t.dataset.op);

  cfg.optimizer = cfg.optimizer || {};
  cfg.optimizer.target_score                      = parseFloat(getVal('cfg-target-score')) || 90;
  cfg.optimizer.feedback_max_rounds               = parseInt(getVal('cfg-feedback-rounds')) || 3;
  cfg.optimizer.feedback_max_mutants_per_round    = parseInt(getVal('cfg-feedback-mutants')) || 10;
  cfg.optimizer.feedback_max_new_tests_per_mutant = parseInt(getVal('cfg-feedback-new')) || 2;

  cfg.test_pool = cfg.test_pool || {};
  cfg.test_pool.acoc_max_per_function        = parseInt(getVal('cfg-acoc-max')) || 200;
  cfg.test_pool.llm_temperature              = parseFloat(getVal('cfg-llm-temp')) || 0.85;
  cfg.test_pool.llm_tests_per_representative = parseInt(getVal('cfg-llm-per-rep')) || 4;
  cfg.test_pool.llm_enabled                  = getChk('cfg-llm-enabled');

  return cfg;
}

async function saveConfig() {
  const cfg = buildConfigFromForm();
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cfg),
    });
    const j = await r.json();
    if (j.ok) {
      _config = cfg;
      refreshRawJson();
      flashMsg('config-save-msg');
    } else alert('خطا: ' + j.error);
  } catch(e) { alert('خطای شبکه: ' + e); }
}

function refreshRawJson() {
  const cfg = _config ? buildConfigFromForm() : {};
  document.getElementById('cfg-raw-json').value = JSON.stringify(cfg, null, 2);
}

function applyRawJson() {
  try {
    const raw = document.getElementById('cfg-raw-json').value;
    const cfg = JSON.parse(raw);
    _config = cfg;
    populateConfigForm(cfg);
    alert('JSON اعمال شد. برای ذخیره روی دیسک، دکمه "ذخیره تنظیمات" را بزنید.');
  } catch(e) { alert('JSON نامعتبر: ' + e.message); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Target Code
// ═══════════════════════════════════════════════════════════════════════════
async function loadTargetCode() {
  try {
    const r = await fetch('/api/target');
    const j = await r.json();
    if (j.ok) {
      document.getElementById('code-editor').value = j.code;
      updateCodeInfo();
    }
  } catch(e) { console.error(e); }
}

async function saveTargetCode() {
  const code = document.getElementById('code-editor').value;
  try {
    const r = await fetch('/api/target', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code}),
    });
    const j = await r.json();
    if (j.ok) flashMsg('code-save-msg');
    else alert('خطا: ' + j.error);
  } catch(e) { alert('خطا: ' + e); }
}

function onCodeChange() { updateCodeInfo(); }
function updateCodeInfo() {
  const code  = document.getElementById('code-editor').value;
  const lines = code.split('\n').length;
  document.getElementById('code-line-count').textContent = lines + ' خط';
}
function handleTab(e) {
  if (e.key === 'Tab') {
    e.preventDefault();
    const el = e.target, s = el.selectionStart;
    el.value = el.value.substring(0, s) + '    ' + el.value.substring(el.selectionEnd);
    el.selectionStart = el.selectionEnd = s + 4;
  }
  if (e.ctrlKey && e.key === 's') { e.preventDefault(); saveTargetCode(); }
}
function formatCode() { alert('برای قالب‌بندی، ابزار Black یا autopep8 را نصب کنید.'); }

// ═══════════════════════════════════════════════════════════════════════════
// Run
// ═══════════════════════════════════════════════════════════════════════════
async function quickRun() {
  const noLlm  = document.getElementById('dash-no-llm')?.checked || false;
  const verbose = document.getElementById('dash-verbose')?.checked || false;
  showTab('run');
  await _doRun(noLlm, verbose);
}

async function startRun() {
  const noLlm  = document.getElementById('run-no-llm').checked;
  const verbose = document.getElementById('run-verbose').checked;
  await _doRun(noLlm, verbose);
}

async function _doRun(noLlm, verbose) {
  try {
    const r = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({no_llm: noLlm, verbose}),
    });
    const j = await r.json();
    if (!j.ok) { alert('خطا: ' + j.error); return; }
    startTimer();
    connectSSE();
  } catch(e) { alert('خطا: ' + e); }
}

async function stopRun() {
  try {
    await fetch('/api/stop', {method: 'POST'});
  } catch(e) {}
}

// ═══════════════════════════════════════════════════════════════════════════
// SSE — Log Streaming
// ═══════════════════════════════════════════════════════════════════════════
function connectSSE() {
  if (_sse) { _sse.close(); _sse = null; }
  _sse = new EventSource('/api/stream');
  const panel = document.getElementById('log-panel');
  
  _sse.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.ping) return;
    if (msg.type === 'done' || msg.type === 'start') {
      if (msg.type === 'done') {
        stopTimer();
        setTimeout(() => { loadResults(); loadMutants(); loadSuite(); refreshDashboard(); }, 1500);
      }
    }
    appendLog(panel, msg);
  };
  
  _sse.onerror = () => {
    if (_sse) { _sse.close(); _sse = null; }
    // Reconnect after 2s if there's an active run
    setTimeout(async () => {
      const st = await getStatus();
      if (st && st.running) connectSSE();
    }, 2000);
  };
}

function appendLog(panel, msg) {
  const div = document.createElement('div');
  div.className = `log-line type-${msg.type || 'log'}`;
  div.textContent = msg.text || '';
  panel.appendChild(div);
  panel.scrollTop = panel.scrollHeight;
  _logLines++;
  document.getElementById('log-line-count').textContent = _logLines + ' خط';
}

function clearLog() {
  document.getElementById('log-panel').innerHTML = '';
  _logLines = 0;
  document.getElementById('log-line-count').textContent = '0 خط';
}

function downloadLog() {
  const text = [...document.querySelectorAll('.log-line')].map(l=>l.textContent).join('\n');
  const blob = new Blob([text], {type: 'text/plain'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = 'mutation_log_' + new Date().toISOString().slice(0,19).replace(/:/g,'-') + '.txt';
  a.click();
}

// ═══════════════════════════════════════════════════════════════════════════
// Status Polling
// ═══════════════════════════════════════════════════════════════════════════
async function getStatus() {
  try {
    const r = await fetch('/api/status');
    const j = await r.json();
    return j.data;
  } catch { return null; }
}

async function pollStatus() {
  const st = await getStatus();
  if (!st) return;
  
  const pill = document.getElementById('run-status');
  const txt  = document.getElementById('run-status-text');
  const btnRun  = document.querySelectorAll('[id^=btn-run]');
  const btnStop = document.querySelectorAll('[id^=btn-stop]');
  
  if (st.running) {
    pill.className = 'pill running';
    txt.textContent = st.phase || 'در حال اجرا...';
    btnRun.forEach(b => b.disabled = true);
    btnStop.forEach(b => b.disabled = false);
    updatePhaseUI(st.phase);
  } else {
    if (st.exit_code === 0) { pill.className = 'pill success'; txt.textContent = 'موفق ✓'; }
    else if (st.exit_code !== null) { pill.className = 'pill error'; txt.textContent = 'خطا ✗'; }
    else { pill.className = 'pill'; txt.textContent = 'آماده'; }
    btnRun.forEach(b => b.disabled = false);
    btnStop.forEach(b => b.disabled = true);
  }
}

function updatePhaseUI(phase) {
  if (!phase) return;
  const map = {'فاز 1': 1, 'فاز 2': 2, 'فاز 3': 3, 'فاز 4': 4, 'فاز 5': 5, 'فاز 6': 6};
  const n = parseInt(phase.replace(/\D/g, '')) || 0;
  for (let i = 1; i <= 6; i++) {
    const el = document.getElementById('ph-' + i);
    if (!el) continue;
    el.className = 'phase-card' + (i < n ? ' done' : i === n ? ' active' : '');
  }
}

function startTimer() {
  _runStart = Date.now();
  if (_timerInterval) clearInterval(_timerInterval);
  _timerInterval = setInterval(() => {
    const elapsed = Math.floor((Date.now() - _runStart) / 1000);
    const m = Math.floor(elapsed / 60), s = elapsed % 60;
    document.getElementById('run-timer').textContent = `⏱ ${m}:${String(s).padStart(2,'0')}`;
  }, 1000);
}

function stopTimer() {
  if (_timerInterval) { clearInterval(_timerInterval); _timerInterval = null; }
}

// ═══════════════════════════════════════════════════════════════════════════
// Results
// ═══════════════════════════════════════════════════════════════════════════
async function loadResults() {
  try {
    const r = await fetch('/api/results/report');
    const j = await r.json();
    if (!j.ok) {
      document.getElementById('results-empty').style.display = '';
      document.getElementById('results-content').style.display = 'none';
      return;
    }
    const data = j.data;
    document.getElementById('results-empty').style.display = 'none';
    document.getElementById('results-content').style.display = '';
    
    const s = data.summary;
    // Stats
    const adjColor = s.adjusted_score >= 90 ? 'green' : s.adjusted_score >= 70 ? 'amber' : 'red';
    document.getElementById('result-stats').innerHTML = `
      <div class="stat-card ${adjColor}">
        <div class="stat-label">Adjusted Score</div>
        <div class="stat-val">${s.adjusted_score.toFixed(1)}%</div>
        <div class="stat-sub">امتیاز تنظیم‌شده</div>
      </div>
      <div class="stat-card green">
        <div class="stat-label">کشته‌شده</div>
        <div class="stat-val">${s.killed}</div>
        <div class="stat-sub">از ${s.total_mutants} Mutant</div>
      </div>
      <div class="stat-card amber">
        <div class="stat-label">Suspected</div>
        <div class="stat-val">${s.suspected}</div>
        <div class="stat-sub">احتمالاً معادل</div>
      </div>
      <div class="stat-card" style="--accent:var(--purple)">
        <div class="stat-label">تست کمینه</div>
        <div class="stat-val">${s.min_test_count}</div>
        <div class="stat-sub">از ${s.total_tests_pool} تست</div>
      </div>
    `;
    
    // Update score badge
    const badge = document.getElementById('score-badge');
    badge.textContent = s.adjusted_score.toFixed(0) + '%';
    badge.style.display = '';
    badge.style.background = s.adjusted_score >= 90 ? 'var(--green)' : s.adjusted_score >= 70 ? 'var(--amber)' : 'var(--red)';

    // Explanation
    document.getElementById('result-explanation').innerHTML = `
      <p>📌 <strong>Raw Score:</strong> ${s.raw_score}% — نسبت کشته‌شدگان به کل</p>
      <p>📌 <strong>Adjusted Score:</strong> ${s.adjusted_score}% — نسبت کشته‌شدگان به (کل − suspected)</p>
      <p style="margin-top:8px">🎯 هدف: ${_config?.optimizer?.target_score || 90}%</p>
      <p>📅 تولید: ${data.generated_at?.replace('T',' ') || '—'}</p>
    `;
    
    // Charts
    renderScoreChart(s);
    renderDistChart(data);
    renderCompareChart(s);
    
    // Update dashboard stats
    updateDashStats(s, data);
    
  } catch(e) { console.error(e); }
}

function renderScoreChart(s) {
  const ctx = document.getElementById('score-chart').getContext('2d');
  if (_scoreChart) _scoreChart.destroy();
  const score = s.adjusted_score;
  const color = score >= 90 ? '#00e87a' : score >= 70 ? '#ffaa00' : '#ff4466';
  _scoreChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      datasets: [{
        data: [score, 100 - score],
        backgroundColor: [color, '#1a2338'],
        borderWidth: 0,
        circumference: 270,
        rotation: 225,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '78%',
      plugins: {
        legend: {display: false},
        tooltip: {enabled: false},
      }
    },
    plugins: [{
      id: 'centerText',
      afterDraw(chart) {
        const {ctx, chartArea: {top,bottom,left,right}} = chart;
        const cx = (left+right)/2, cy = (top+bottom)/2;
        ctx.save();
        ctx.fillStyle = color;
        ctx.font = 'bold 28px JetBrains Mono, monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(score.toFixed(1) + '%', cx, cy - 8);
        ctx.fillStyle = '#445577';
        ctx.font = '11px IBM Plex Sans Arabic, sans-serif';
        ctx.fillText('Adjusted Score', cx, cy + 18);
        ctx.restore();
      }
    }]
  });
}

function renderDistChart(data) {
  const ctx = document.getElementById('mutant-dist-chart').getContext('2d');
  if (_distChart) _distChart.destroy();
  const s = data.summary;
  _distChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['کشته‌شده', 'Suspected', 'زنده'],
      datasets: [{
        data: [s.killed, s.suspected, s.alive],
        backgroundColor: ['#00e87a', '#ffaa00', '#ff4466'],
        borderColor: '#0d1224', borderWidth: 2,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'bottom',
          labels: {color: '#8899bb', font: {size: 11}, padding: 12, boxWidth: 12}
        }
      }
    }
  });
}

function renderCompareChart(s) {
  const ctx = document.getElementById('score-compare-chart').getContext('2d');
  if (_compareChart) _compareChart.destroy();
  _compareChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Raw Score', 'Adjusted Score'],
      datasets: [{
        data: [s.raw_score, s.adjusted_score],
        backgroundColor: ['rgba(0,212,255,0.3)', 'rgba(0,232,122,0.3)'],
        borderColor: ['#00d4ff', '#00e87a'],
        borderWidth: 2, borderRadius: 6,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        y: {min: 0, max: 100, ticks: {color: '#445577', callback: v => v + '%'}, grid: {color: '#1e2d4a'}},
        x: {ticks: {color: '#8899bb'}, grid: {display: false}}
      },
      plugins: {legend: {display: false}}
    }
  });
}

function updateDashStats(s, data) {
  document.getElementById('stat-adj-score').textContent = s.adjusted_score.toFixed(1) + '%';
  document.getElementById('stat-killed').textContent    = s.killed;
  document.getElementById('stat-total').textContent     = s.total_mutants;
  document.getElementById('stat-suspected').textContent = s.suspected;
  document.getElementById('stat-min-tests').textContent = s.min_test_count;
  document.getElementById('stat-pool').textContent      = s.total_tests_pool;
  const bar = document.getElementById('score-progress');
  const pct = Math.min(s.adjusted_score, 100);
  bar.style.width = pct + '%';
  bar.className = 'progress-fill' + (pct >= 90 ? ' green' : pct >= 70 ? ' amber' : ' red');
}

// ═══════════════════════════════════════════════════════════════════════════
// Mutants Table
// ═══════════════════════════════════════════════════════════════════════════
async function loadMutants() {
  try {
    const r = await fetch('/api/results/mutants');
    const j = await r.json();
    if (!j.ok) {
      document.getElementById('mutants-tbody').innerHTML = '<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--text-dim)">' + j.error + '</td></tr>';
      return;
    }
    _allMutants = j.data;
    document.getElementById('mutants-badge').textContent = _allMutants.length;
    document.getElementById('mutants-badge').style.display = '';
    renderMutantsTable();
  } catch(e) { console.error(e); }
}

function filterMutants(filter, el) {
  if (filter) _mutFilter = filter;
  if (el) {
    document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
  }
  renderMutantsTable();
}

function renderMutantsTable() {
  const search = document.getElementById('mutant-search').value.toLowerCase();
  let data = _allMutants;
  
  if (_mutFilter !== 'all') {
    if (['KILLED','ALIVE','SUSPECTED'].includes(_mutFilter)) {
      data = data.filter(m => m.status === _mutFilter);
    } else {
      data = data.filter(m => m.category === _mutFilter);
    }
  }
  if (search) {
    data = data.filter(m =>
      (m.id||'').toLowerCase().includes(search) ||
      (m.operator_name||'').toLowerCase().includes(search) ||
      (m.function_name||'').toLowerCase().includes(search) ||
      (m.original_line_text||'').toLowerCase().includes(search)
    );
  }
  
  const catBadge = cat => {
    if (cat === 'ARITH') return '<span class="badge badge-arith">ARITH</span>';
    if (cat === 'REL')   return '<span class="badge badge-rel">REL</span>';
    return '<span class="badge badge-int">INT</span>';
  };
  const stBadge = st => {
    if (st === 'KILLED')    return '<span class="badge badge-killed">🟢 کشته</span>';
    if (st === 'ALIVE')     return '<span class="badge badge-alive">🔴 زنده</span>';
    if (st === 'SUSPECTED') return '<span class="badge badge-suspected">🟡 Suspected</span>';
    return `<span class="badge">${st}</span>`;
  };

  const rows = data.map(m => `
    <tr>
      <td class="text-cyan">${m.id}</td>
      <td>${stBadge(m.status)}</td>
      <td>${catBadge(m.category)}</td>
      <td title="${m.operator_name}">${(m.operator_name||'').replace(/_/g,' ')}</td>
      <td class="text-code">${m.function_name || ''}</td>
      <td class="text-code">${m.line || ''}</td>
      <td title="${m.original_line_text||''}" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(m.original_line_text||'')}</td>
    </tr>
  `).join('');
  
  document.getElementById('mutants-tbody').innerHTML = rows || '<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--text-dim)">نتیجه‌ای یافت نشد</td></tr>';
  document.getElementById('mutants-count').textContent = `نمایش ${data.length} از ${_allMutants.length} Mutant`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Suite
// ═══════════════════════════════════════════════════════════════════════════
async function loadSuite() {
  try {
    const r = await fetch('/api/results/suite');
    const j = await r.json();
    if (!j.ok) return;
    const d = j.data;
    document.getElementById('suite-count').textContent = d.test_count || 0;
    document.getElementById('suite-score').textContent = (d.mutation_score || 0) + '%';
    document.getElementById('suite-date').textContent  = (d.generated_at || '').replace('T',' ');
    
    const list = document.getElementById('suite-list');
    if (!d.tests || !d.tests.length) {
      list.innerHTML = '<div class="empty-state"><span class="es-icon">✅</span><div class="es-text">مجموعه تست خالی است.</div></div>';
      return;
    }
    list.innerHTML = d.tests.map(t => `
      <div class="suite-item">
        <div class="suite-item-header">
          <span class="suite-id">${t.test_id}</span>
          <span class="suite-fn">${t.function || ''}</span>
          <div class="suite-kills">
            ${(t.kills||[]).map(k => `<span class="kill-chip">${k}</span>`).join('')}
          </div>
        </div>
        <div class="suite-input">${JSON.stringify(t.inputs || {})}</div>
      </div>
    `).join('');
  } catch(e) { console.error(e); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Dashboard
// ═══════════════════════════════════════════════════════════════════════════
async function refreshDashboard() {
  // project info from config
  if (!_config) await loadConfig();
  if (_config) {
    const proj  = _config.project  || {};
    const opt   = _config.optimizer || {};
    const llm   = _config.llm       || {};
    document.getElementById('proj-target').textContent       = proj.target_file || '—';
    document.getElementById('proj-functions').textContent    = (_config.functions||[]).map(f=>f.name).join(', ') || '—';
    document.getElementById('proj-target-score').textContent = (opt.target_score || 90) + '%';
    document.getElementById('proj-model').textContent        = llm.model || '—';
    document.getElementById('proj-feedback').textContent     = opt.feedback_max_rounds || '—';
  }
  await loadResults();
  await checkApiKey();
}

// ═══════════════════════════════════════════════════════════════════════════
// API Key
// ═══════════════════════════════════════════════════════════════════════════
async function checkApiKey() {
  try {
    const r = await fetch('/api/env/apikey');
    const j = await r.json();
    const txt = document.getElementById('apikey-text');
    if (j.set) {
      txt.textContent = j.prefix + ' ✓';
      document.getElementById('apikey-badge').style.borderColor = 'var(--green)';
      document.getElementById('apikey-badge').style.color = 'var(--green)';
    } else {
      txt.textContent = 'تنظیم نشده ⚠';
      document.getElementById('apikey-badge').style.borderColor = 'var(--amber)';
      document.getElementById('apikey-badge').style.color = 'var(--amber)';
    }
  } catch(e) {}
}

function openApiKeyModal() {
  document.getElementById('apikey-modal').classList.add('open');
  document.getElementById('apikey-input').focus();
}
function closeApiKeyModal() { document.getElementById('apikey-modal').classList.remove('open'); }

async function setApiKey() {
  const key = document.getElementById('apikey-input').value.trim();
  if (!key) return;
  try {
    const r = await fetch('/api/env/apikey', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key}),
    });
    const j = await r.json();
    if (j.ok) { closeApiKeyModal(); checkApiKey(); }
    else alert('خطا: ' + j.error);
  } catch(e) { alert('خطا: ' + e); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Download
// ═══════════════════════════════════════════════════════════════════════════
function downloadReport(filename) {
  window.open('/api/results/download/' + filename, '_blank');
}

// ═══════════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════════
function setVal(id, val) { const el = document.getElementById(id); if (el) el.value = val; }
function getVal(id) { const el = document.getElementById(id); return el ? el.value : ''; }
function setChk(id, val) { const el = document.getElementById(id); if (el) el.checked = val; }
function getChk(id) { const el = document.getElementById(id); return el ? el.checked : false; }
function escHtml(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function flashMsg(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = '';
  setTimeout(() => el.style.display = 'none', 3000);
}

// ═══════════════════════════════════════════════════════════════════════════
// Keyboard Shortcuts
// ═══════════════════════════════════════════════════════════════════════════
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeApiKeyModal();
});

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════
async function init() {
  await refreshDashboard();
  connectSSE();
  pollStatus();
  _statusInterval = setInterval(pollStatus, 2000);
}

init();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# نقطه ورود
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import webbrowser, threading

    # ایجاد پوشه output اگر وجود ندارد
    OUTPUT_DIR.mkdir(exist_ok=True)

    port = int(os.environ.get("GUI_PORT", 5000))
    host = os.environ.get("GUI_HOST", "127.0.0.1")

    print("╔══════════════════════════════════════════════════╗")
    print("║   Mutation Testing System — رابط گرافیکی        ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║   آدرس: http://{host}:{port:<29}  ║")
    print(f"║   پروژه: {str(PROJECT_ROOT):<40}  ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # چک API Key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠️  هشدار: ANTHROPIC_API_KEY تنظیم نشده.")
        print("   از طریق رابط گرافیکی (آیکون 🔑) یا:")
        print("   export ANTHROPIC_API_KEY='sk-ant-...'")
        print()

    # باز کردن مرورگر
    def open_browser():
        time.sleep(1.2)
        webbrowser.open(f"http://{host}:{port}")

    threading.Thread(target=open_browser, daemon=True).start()

    app.run(host=host, port=port, debug=False, threaded=True)