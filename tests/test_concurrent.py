# tests/test_concurrent.py — v1.14.1 并发与故障注入测试（P2-16）
import os
import sys
import tempfile
import threading

_TEST_ROOT = tempfile.mkdtemp(prefix="scanweb_conc_")
os.environ["SCAN_ROOT"] = _TEST_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin  # noqa: E402
import app as app_mod  # noqa: E402
import config  # noqa: E402
import jobs  # noqa: E402
import scanner  # noqa: E402


def _reset():
    import shutil
    for name in os.listdir(_TEST_ROOT):
        shutil.rmtree(os.path.join(_TEST_ROOT, name), ignore_errors=True)
    scanner.state.clear()


# ---------- job_lock 互斥语义（P1-3） ----------
def test_cleanup_skips_job_holding_lock():
    _reset()
    old, new = jobs.create("旧"), jobs.create("新")
    cfg = config.load_admin_cfg()
    cfg["cleanup"]["max_jobs"] = 1
    config.save_admin_cfg(cfg)
    lk = jobs.job_lock(new)
    assert lk.acquire(blocking=False)
    deleted = jobs.cleanup(force=True)
    assert old in deleted and new not in deleted, "持锁任务必须被保护，超限任务照删"
    lk.release()
    assert jobs.cleanup() == []       # 剩 1 个未超限，不应再删
    cfg["cleanup"]["max_jobs"] = 0
    config.save_admin_cfg(cfg)


def test_job_lock_mutual_exclusion():
    """两个线程抢同一任务锁，同一时刻只有一个能进入临界区。"""
    _reset()
    name = jobs.create()
    lk = jobs.job_lock(name)
    counter = {"in": 0, "max": 0}
    guard = threading.Lock()

    def worker():
        for _ in range(50):
            with lk:
                with guard:
                    counter["in"] += 1
                    counter["max"] = max(counter["max"], counter["in"])
                with guard:
                    counter["in"] -= 1

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert counter["max"] == 1, "生命周期锁必须互斥（并发进入=%d）" % counter["max"]


def test_concurrent_create_unique_names():
    """同秒并发创建，任务名必须全部唯一（#4 随机后缀 + 并发压力）。"""
    _reset()
    names, err = [], []

    def creator():
        try:
            names.append(jobs.create("并发"))
        except Exception as e:
            err.append(e)

    ts = [threading.Thread(target=creator) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not err, str(err)
    assert len(names) == len(set(names)), "并发创建出现重名"


# ---------- 故障注入（P2-16） ----------
def test_convert_pnms_keeps_source_on_failure(monkeypatch):
    """convert 失败时 PNM 必须保留（P1-4），不再误删用户扫描数据。"""
    _reset()
    name = jobs.create()
    d = os.path.join(config.get_scan_root(), name)
    with open(os.path.join(d, "p001.pnm"), "wb") as f:
        f.write(b"P5 fake pnm")
    # 注入故障：CONVERT 指向不存在的命令
    monkeypatch.setattr(scanner, "CONVERT", "/nonexistent/convert")
    scanner._convert_pnms(name, 1)
    assert os.path.exists(os.path.join(d, "p001.pnm")), "转换失败必须保留 PNM 源"
    assert not os.path.exists(os.path.join(d, "p001.png")), "失败不应产出 PNG"


def test_convert_pnms_removes_source_on_success(monkeypatch):
    """convert 成功时 PNM 删除、PNG 产出（防止只修不删走另一个极端）。"""
    _reset()
    name = jobs.create()
    d = os.path.join(config.get_scan_root(), name)
    with open(os.path.join(d, "p001.pnm"), "wb") as f:
        f.write(b"fake")
    # 注入假 convert：写一个目标 PNG 后退出 0
    fake = os.path.join(_TEST_ROOT, "_fake_convert")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\ncp /dev/null \"$2\" 2>/dev/null || : > \"$2\"\nexit 0\n")
    os.chmod(fake, 0o755)
    monkeypatch.setattr(scanner, "CONVERT", fake)
    scanner._convert_pnms(name, 1)
    assert not os.path.exists(os.path.join(d, "p001.pnm")), "成功必须删 PNM"
    assert os.path.exists(os.path.join(d, "p001.png")), "成功必须产出 PNG"


def test_adf_busy_returns_busy_immediately():
    """设备被占时 scan_adf 同步返回 busy（P1-6），不再异步报错。"""
    _reset()
    name = jobs.create()
    assert scanner.scan_lock.acquire(blocking=False)
    try:
        assert scanner.scan_adf(name) == "busy", "忙时必须同步返回 busy"
    finally:
        scanner.scan_lock.release()


def test_adf_api_returns_409_when_busy():
    """API 层：忙时 POST adf 得 409（P1-6 链路验证）。"""
    _reset()
    name = jobs.create()
    meta = jobs.load(name)
    meta["params"] = {"source": "adf", "device": "", "dpi": "150",
                      "mode": "Gray", "crop": False, "source_name": "ADF"}
    jobs.save(name, meta)
    client = app_mod.app.test_client()
    assert scanner.scan_lock.acquire(blocking=False)
    try:
        r = client.post("/api/jobs/%s/adf" % name)
        assert r.status_code == 409 and r.get_json().get("ok") is False
    finally:
        scanner.scan_lock.release()


def test_adf_source_fail_closed():
    """探测不到设备能力时，带 source 的 ADF 扫描必须拒绝（P1-7 fail-closed）。"""
    import device_probe
    _reset()
    name = jobs.create()
    meta = jobs.load(name)
    meta["params"] = {"source": "adf", "device": "nosuchdev:", "dpi": "150",
                      "mode": "Gray", "crop": False, "source_name": "ADF"}
    jobs.save(name, meta)
    # 探测缓存注入：空设备列表（探测失败场景）
    device_probe._cache["data"] = []
    device_probe._cache["ts"] = 1e18
    assert scanner.scan_adf(name) == "started"   # 忙检查通过后 worker 启动
    import time
    for _ in range(50):     # 等 worker 收尾
        if scanner.get_state(name)["state"] in ("error", "done"):
            break
        time.sleep(0.02)
    st = scanner.get_state(name)
    assert st["state"] == "error" and "fail-closed" in st["msg"], st["msg"]
    device_probe._cache["data"] = None
    device_probe._cache["ts"] = 0.0


def test_safe_slug_rejects_dots():
    """备注含「..」不再生成不可访问任务（P1-10）。"""
    _reset()
    name = jobs.create("合同..最终版")
    import re
    assert re.fullmatch(jobs.JOB_RE, name), "生成名必须通过自己的 validator：%s" % name
    assert ".." not in name
    # 该任务可正常访问（validate 通过）
    assert jobs.pages(name) == []


# ---------- TOKEN 登录限流（P2-11） ----------
def test_token_login_rate_limit():
    app_mod.TOKEN = "test-token-123"      # 启用登录
    app_mod._token_fails.clear()
    try:
        c = app_mod.app.test_client()
        for i in range(5):
            r = c.post("/login", data={"password": "wrong"}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
        # 第 5 次错误后锁定：第 6 次即使密码正确也被限流页拒绝
        r = c.post("/login", data={"password": "test-token-123"},
                   environ_base={"REMOTE_ADDR": "10.0.0.9"})
        body = r.get_data(as_text=True)
        assert "尝试过于频繁" in body, "锁定后必须限流"
        # 另一 IP 不受影响
        c2 = app_mod.app.test_client()
        r2 = c2.post("/login", data={"password": "test-token-123"},
                     environ_base={"REMOTE_ADDR": "10.0.0.10"})
        assert r2.status_code == 302, "其他 IP 正确密码必须能登录"
    finally:
        app_mod.TOKEN = ""                 # 还原（其他测试默认无登录）
        app_mod._token_fails.clear()
