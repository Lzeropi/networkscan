# tests/test_v16t09.py — v1.16.0-tmp09：DeepSeek 第四轮审查（109 项代码级）修复验证
# H1：api_testscan 检查 busy/convert_busy（不再误报成功）
# H2：dl_zip Response 构造失败不泄漏 jlock（resp 先构造再交接）
# H3：jobs.save tmp 名含线程 ID（同进程并发写不碰撞）
# M1：_convert_pnm 回退分支 safe_src 不泄漏（symlink 源拒绝路径）
# M2：api_delete TOCTOU 锁定 → 403 而非 404
# M3：_open_no_follow fdopen 失败关 fd
import glob
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402
import config          # noqa: E402
import jobs            # noqa: E402
import scanner         # noqa: E402
from PIL import Image  # noqa: E402


def _mk_job(params=None):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    scanner._reset_convert_for_test()
    return jobs.create("t", params or {})


def _ppm(path):
    Image.new("RGB", (8, 8), (10, 120, 210)).save(path, "PPM")
    return path


def _fake_script(path, body):
    open(path, "w").write(body)
    os.chmod(path, 0o755)
    return path


# ---------- H1：api_testscan 忙时如实返回（不再误报 started） ----------

def test_testscan_busy_reports_409(monkeypatch, tmp_path):
    _mk_job()
    import admin
    # admin 从 config import 复制了 SCANIMAGE 值——patch admin 模块属性
    monkeypatch.setattr(admin, "SCANIMAGE",
                        _fake_script(str(tmp_path / "si.sh"), "#!/bin/sh\nexit 0\n"))
    # scan_lock 被外部持有 → scan_flatbed 返回 "busy" → api_testscan 应 409
    scanner.scan_lock.acquire()
    try:
        import admin
        # @require_admin 已在定义时包裹——取 __wrapped__ 原函数直调（绕过登录态，
        # 单测 scan 返回值分支语义本身）
        raw = admin.api_testscan.__wrapped__
        with app_mod.app.test_request_context("/api/admin/testscan", method="POST"):
            body, status = raw()
        assert status == 409, "busy 必须如实 409，实际 %s" % status
        assert "忙" in body.get_json()["msg"], "提示应说明设备忙：%r" % body.get_json()
    finally:
        scanner.scan_lock.release()


# ---------- H2：dl_zip Response 构造失败 → 锁必须释放 ----------

def test_zip_response_construct_failure_releases_lock(monkeypatch):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    Image.new("RGB", (4, 4)).save(os.path.join(base, "p001.png"))
    boom_calls = {"n": 0}

    def boom(*a, **k):
        boom_calls["n"] += 1
        raise RuntimeError("模拟 Response OOM 构造失败")

    monkeypatch.setattr(app_mod, "Response", boom)
    c = app_mod.app.test_client()
    r = c.get("/job/" + name + "/download.zip")
    assert boom_calls["n"] == 1, "Response 构造应被触发"
    assert r.status_code == 500, "构造失败应 500，实际 %s" % r.status_code
    # 核心：锁必须被 finally 释放——后续 DELETE 不被卡死
    rd = c.delete("/api/jobs/" + name)
    assert rd.get_json().get("ok") is True, "H2：Response 失败路径 jlock 不得泄漏"


# ---------- H3：jobs.save 同进程并发写不碰撞 ----------

def test_save_concurrent_threads_no_collision():
    name = _mk_job()
    errs = []

    def writer(i):
        try:
            for k in range(30):
                meta = jobs.load(name)
                meta["pages"] = k
                meta["writer"] = i
                jobs.save(name, meta)
        except Exception as e:
            errs.append(e)

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # 并发 120 次 save 后 meta.json 必须仍是合法 JSON（旧版同 pid tmp 碰撞可损坏）
    assert not errs, "并发 save 不得抛异常：%r" % errs[:2]
    meta = jobs.load(name)
    assert isinstance(meta.get("pages"), int), "meta.json 并发写后仍应合法可读"
    # 无 .tmp 残留
    d = os.path.join(config.get_scan_root(), name)
    assert not [f for f in os.listdir(d) if f.startswith("meta.json.tmp")], "tmp 残留"


# ---------- M1：_convert_pnm symlink 源拒绝路径不泄漏 safe_src ----------

def test_convert_pnm_symlink_no_safe_src_leak():
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    outside = os.path.join(os.path.dirname(base), "_outside.png")
    Image.new("RGB", (4, 4)).save(outside)
    link = os.path.join(base, "p001.pnm")
    os.symlink(outside, link)
    before = set(glob.glob("/tmp/scanweb_cvsrc_*"))
    try:
        scanner._convert_pnm(link, os.path.join(base, "p001.png"), "png")
        raised = False
    except Exception:
        raised = True
    assert raised, "symlink 源必须被拒绝"
    after = set(glob.glob("/tmp/scanweb_cvsrc_*")) - before
    assert after == set(), "M1：拒绝路径不得泄漏 safe_src 副本，泄漏 %r" % after


# ---------- M2：api_delete TOCTOU 锁定 → 403 ----------

def test_delete_locked_tocu_to_403(monkeypatch):
    name = _mk_job()
    # 模拟外部检查时未锁、jlock 内 _delete_locked 时已锁（TOCTOU 窗口）
    real_is_locked = jobs.is_locked
    calls = {"n": 0}

    def flaky_locked(job):
        calls["n"] += 1
        # 第 1 次（api_delete 外部检查）未锁；第 2+ 次（_delete_locked 内部）已锁
        return calls["n"] >= 2

    monkeypatch.setattr(jobs, "is_locked", flaky_locked)
    c = app_mod.app.test_client()
    r = c.delete("/api/jobs/" + name)
    assert r.status_code == 403, "TOCTOU 锁定应 403（旧版误落 404），实际 %s" % r.status_code
    assert "锁定" in r.get_json()["msg"]
    monkeypatch.setattr(jobs, "is_locked", real_is_locked)


# ---------- M3：_open_no_follow fdopen 失败关 fd ----------

def test_open_no_follow_closes_fd_on_fdopen_failure(monkeypatch):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    real_pnm = _ppm(os.path.join(base, "p001.pnm"))
    opened_fds = []

    real_open = os.open

    def spy_open(path, flags, *a, **k):
        fd = real_open(path, flags, *a, **k)
        if "p001.pnm" in str(path):
            opened_fds.append(fd)
        return fd

    def boom_fdopen(fd, mode, *a, **k):
        raise OSError("模拟 fdopen 失败（EMFILE 边界）")

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fdopen", boom_fdopen)
    raised = False
    try:
        scanner._open_no_follow(real_pnm)
    except OSError:
        raised = True
    assert raised, "fdopen 失败应上抛"
    # fd 已被关闭：对已关 fd 再 close 应抛 EBADF
    if opened_fds:
        fd = opened_fds[0]
        try:
            os.close(fd)
            leaked = True   # 关成功了说明 fd 未被关（泄漏）
        except OSError:
            leaked = False  # 已关，正常
        assert not leaked, "M3：fdopen 失败后 fd 必须被关闭，不得泄漏"
