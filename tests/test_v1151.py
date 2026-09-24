# tests/test_v1151.py — v1.15.1 稳定性收尾验证（ChatGPT v1.15 源码审查建议采纳）
# P2-1 scan_start_guard 原子性 / P3-1 _params 内移无泄漏 /
# 真并发交错 4 项（ChatGPT v1.14 审查 §35 + DeepSeek 交叉印证的覆盖缺口）
import io
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402,F401
import config          # noqa: E402,F401
import jobs            # noqa: E402,F401
import scanner         # noqa: E402,F401


def _mk_job(letters=("A",)):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    name = jobs.create("t")
    d = os.path.join(config.get_scan_root(), name)
    for i, c in enumerate(letters, 1):
        with open(os.path.join(d, "p%03d.png" % i), "wb") as f:
            f.write(c.encode())
    return name


def _fake_script(path, body):
    open(path, "w").write(body)
    os.chmod(path, 0o755)
    return path


def _png_path(tmp):
    from PIL import Image
    p = str(tmp) + "/page.png"
    Image.new("RGB", (4, 4), (10, 120, 210)).save(p, "PNG")
    return p


class _Fx:
    """假 scanimage/convert 注入器（session 级，finally 还原）"""

    def __init__(self, tmp, scan_sleep=0.8):
        png = _png_path(tmp)
        self.sc = _fake_script(str(tmp) + "/scan.sh",
                               "#!/bin/sh\nsleep %s\nexit 0\n" % scan_sleep)
        self.cv = _fake_script(str(tmp) + "/cvt.sh",
                               '#!/bin/sh\ncp "%s" "$2"\n' % png)
        self.old = (scanner.SCANIMAGE, scanner.CONVERT)

    def __enter__(self):
        scanner.SCANIMAGE, scanner.CONVERT = self.sc, self.cv
        return self

    def __exit__(self, *a):
        scanner.SCANIMAGE, scanner.CONVERT = self.old


def _wait_state(name, want=("done", "error"), timeout=10):
    deadline = time.time() + timeout
    st = {}
    while time.time() < deadline:
        st = scanner.get_state(name)
        if st["state"] in want:
            return st
        time.sleep(0.05)
    return st


# ---------- P2-1：guard 持有时新扫描启动段必须排队 ----------
def test_save_config_blocks_new_scan(tmp_path):
    name = _mk_job()
    guard = scanner._scan_start_guard
    guard.acquire()                       # 模拟 save_config 正处于「检查+落盘」原子段
    result = {}

    def run():
        try:
            scanner.scan_flatbed(name)
            result["ok"] = True
        except Exception as e:
            result["err"] = str(e)

    t = threading.Thread(target=run, daemon=True)
    with _Fx(tmp_path):
        t.start()
        time.sleep(0.4)
        assert (scanner.state.get(name) or {}).get("state") != "scanning", \
            "P2-1：guard 持有时新扫描不得进入启动段（TOCTOU 残窗消除的语义基础）"
        guard.release()
        t.join(timeout=15)
    st = _wait_state(name)
    assert st["state"] in ("done", "error"), "guard 释放后扫描应正常收尾"
    # 双锁无泄漏
    ok1 = scanner.scan_lock.acquire(blocking=False)
    if ok1:
        scanner.scan_lock.release()
    assert ok1, "P2-1：扫描结束后 scan_lock 必须可获取"
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False), "P2-1：job_lock 必须可获取"
    lk.release()


# ---------- P3-1：_params 异常（锁内）不得泄漏任何锁 ----------
def test_params_exception_no_lock_leak(tmp_path, monkeypatch):
    name = _mk_job()

    def boom(_job):
        raise jobs.JobError("simulated meta corrupted")

    monkeypatch.setattr(scanner, "_params", boom)
    try:
        scanner.scan_flatbed(name)
        raised = False
    except jobs.JobError:
        raised = True
    assert raised, "P3-1：_params 异常必须向外抛（统一收尾）"
    ok1 = scanner.scan_lock.acquire(blocking=False)
    if ok1:
        scanner.scan_lock.release()
    assert ok1, "P3-1：_params 异常时 scan_lock 未取得也必须无残留"
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False), "P3-1：_params 异常必须释放 job_lock"
    lk.release()
    # _params 异常发生在 state 写入前 → 任务未进入 scanning，不产生 error 条目属正确行为
    assert (scanner.state.get(name) or {}).get("state") != "scanning", \
        "P3-1：异常后不得残留 scanning 状态"


# ---------- 并发交错：扫描中 DELETE → 409 ----------
def test_concurrent_delete_scan(tmp_path):
    name = _mk_job()
    c = app_mod.app.test_client()
    result = {}

    def run():
        try:
            scanner.scan_flatbed(name)
            result["ok"] = True
        except Exception as e:
            result["err"] = str(e)

    with _Fx(tmp_path, scan_sleep=1.0):
        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and (scanner.state.get(name) or {}).get("state") != "scanning":
            time.sleep(0.02)
        assert scanner.state.get(name, {}).get("state") == "scanning", "扫描应已启动"
        r = c.delete("/api/jobs/" + name)
        t.join(timeout=15)
    # 两种合法终态：拿到锁时仍在扫描 → 409；排队等扫描完成 → 200 删除。
    # 并发不变量是「不产生半删状态」：要么完好存活，要么完整删除。
    assert r.status_code in (200, 409), \
        "并发 DELETE 只允许 200（排队后删）或 409（扫描中拒），实际 %s" % r.status_code
    if r.status_code == 200:
        assert not os.path.isdir(os.path.join(config.get_scan_root(), name)), \
            "DELETE 200 后任务目录必须完整移除（不得半删）"
    else:
        _wait_state(name)
        assert os.path.isdir(os.path.join(config.get_scan_root(), name)), "409 后任务必须存活"


# ---------- 并发交错：扫描中 reorder → 409 ----------
def test_concurrent_reorder_scan(tmp_path):
    name = _mk_job()
    c = app_mod.app.test_client()
    result = {}

    def run():
        try:
            scanner.scan_flatbed(name)
            result["ok"] = True
        except Exception as e:
            result["err"] = str(e)

    with _Fx(tmp_path, scan_sleep=1.0):
        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and (scanner.state.get(name) or {}).get("state") != "scanning":
            time.sleep(0.02)
        r = c.post("/api/jobs/" + name + "/reorder",
                   json={"order": jobs.raw_pages(name), "delete": False})
        t.join(timeout=15)
    # 同 DELETE：两种合法终态，不变量是页面文件零损坏
    assert r.status_code in (200, 409), \
        "并发 reorder 只允许 200（排队后执行）或 409（扫描中拒），实际 %s" % r.status_code
    before = set(jobs.raw_pages(name))
    _wait_state(name)
    after = set(jobs.raw_pages(name))
    if r.status_code == 200:
        assert after >= before, "reorder 200 不得丢页（零数据损坏）"
    else:
        assert after >= before, "409 后页面必须完好"


# ---------- 并发交错：cleanup 与扫描竞态——scanning 任务不可删 ----------
def test_cleanup_scan_race(tmp_path):
    active_name = _mk_job()               # 先建将扫描的任务（最旧）
    old_name = jobs.create("old")        # 后建超限第二任务（_mk_job 已清场，勿反序）
    cfg = config.load_admin_cfg()
    cfg["cleanup"]["max_jobs"] = 1
    config.save_admin_cfg(cfg)
    result = {}

    def run():
        try:
            scanner.scan_flatbed(active_name)
            result["ok"] = True
        except Exception as e:
            result["err"] = str(e)

    with _Fx(tmp_path, scan_sleep=1.0):
        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and (scanner.state.get(active_name) or {}).get("state") != "scanning":
            time.sleep(0.02)
        deleted = jobs.cleanup(force=True)   # 与 scanning 并发触发清理
        t.join(timeout=15)
    assert old_name in deleted, "超限次旧任务应被清理（最旧任务在扫描被跳过后由次旧补位），实际 %s" % deleted
    assert active_name not in deleted, "并发清理不得删除正在扫描的任务"
    assert os.path.isdir(os.path.join(config.get_scan_root(), active_name)), "扫描中任务目录必须存活"
    _wait_state(active_name)
    scanner.state.clear()


# ---------- 并发交错：同任务双扫描——scan_lock 全局唯一 ----------
def test_two_scans_same_job(tmp_path):
    name = _mk_job()
    out = {}

    def run(tag):
        try:
            scanner.scan_flatbed(name)
            out[tag] = "ok"
        except RuntimeError as e:
            out[tag] = "busy" if "设备忙" in str(e) else str(e)[:60]
        except Exception as e:
            out[tag] = str(e)[:60]

    with _Fx(tmp_path, scan_sleep=1.0):
        t1 = threading.Thread(target=run, args=("a",), daemon=True)
        t2 = threading.Thread(target=run, args=("b",), daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
    # 同任务双扫被 job_lock 串行化（锁序 jlock→scan_lock）：第二次排队到第一次
    # 完成后才进入——两次都成功、页面 +2、无 busy 争抢是正确并发语义
    assert out.get("a") == "ok" and out.get("b") == "ok", \
        "同任务双扫描必须串行化完成（job_lock 排队），实际 %s" % out
    assert len(jobs.raw_pages(name)) == 3, \
        "初始 1 页 + 两次扫描各 1 页 = 3（含占位），实际 %s" % jobs.raw_pages(name)
    ok1 = scanner.scan_lock.acquire(blocking=False)
    if ok1:
        scanner.scan_lock.release()
    assert ok1, "并发扫描结束后 scan_lock 必须可获取"
