# tests/test_v1146.py — v1.14.6 自审四修验证（reorder 语义 + 安全头 + mkstemp + probe 去重）
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402,F401
import device_probe    # noqa: E402,F401
import jobs            # noqa: E402,F401
import scanner         # noqa: E402,F401


# ---------- ① reorder：空 order 对不存在任务必须 404（此前 400） ----------
def test_reorder_empty_absent_job_404():
    r = app_mod.app.test_client().post("/api/jobs/nonexist_x/reorder",
                                       json={"order": []})
    assert r.status_code == 404, "P3：不存在任务 reorder（空 order）应 404，实际 %s" % r.status_code


# ---------- ② OWASP 安全响应头 ----------
def test_security_headers_present():
    r = app_mod.app.test_client().get("/")
    assert r.headers.get("X-Frame-Options") == "SAMEORIGIN", "缺 X-Frame-Options"
    assert r.headers.get("X-Content-Type-Options") == "nosniff", "缺 nosniff"
    assert r.headers.get("Referrer-Policy") == "same-origin", "缺 Referrer-Policy"


# ---------- ④ probe 并发去重：4 线程并发 miss 只跑一次 scanimage ----------
def test_probe_concurrent_single_scanimage(monkeypatch):
    calls = []
    real = device_probe.subprocess.run

    def fake_run(cmd, **kw):
        calls.append(cmd[0] if cmd else "")
        class R:
            stdout = 'device `fake1:` is a HP\n'
            stderr = ""
        return R()
    monkeypatch.setattr(device_probe.subprocess, "run", fake_run)
    monkeypatch.setattr(device_probe.os.path, "exists", lambda p: True)   # 环境无扫描仪也强制走探测分支
    device_probe.invalidate()   # 清缓存制造并发 miss
    out = []
    def hit():
        out.append(device_probe.probe()[0])
    ts = [threading.Thread(target=hit, daemon=True) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert len(calls) == 2, "并发 miss 只应探测一轮（-L + -A），实际 %s 次：%s" % (len(calls), calls)
    assert all(len(d) == 1 and d[0]["name"] == "fake1:" for d in out), out
    # 锁内写缓存：后续 probe 直接命中（不触发扫描）
    calls.clear()
    device_probe.probe()
    assert len(calls) == 0, "缓存命中不应再跑 scanimage"
    monkeypatch.setattr(device_probe.subprocess, "run", real)