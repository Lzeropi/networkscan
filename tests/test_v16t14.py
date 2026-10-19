# tests/test_v16t14.py — v1.16.3-tmp02：DeepSeek 二十轮审查七项修复验证
# P1-1  _release_once 的 jlock.release() 抛异常时进度仍收尾（try/finally）
# P1-2  reorder 两种模式统一前置去重校验（拒绝项 → 400 而非误导性 500）
# P1-3  scan_adf 线程创建失败回滚（_adf_batch/双锁，不永久 busy）
# P2-1  api_save_config 预创建目录在 _Reject 后补偿清理（仅空目录一级）
# P2-6  dl_zip call_on_close 抛异常时 jlock 兜底释放
# P2-4  env_report Pillow 版本改 importlib.metadata
# P3-3  _ROOT_BLACKLIST 补 /var/tmp
import os
import shutil
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin          # noqa: E402
import app as app_mod  # noqa: E402
import config         # noqa: E402
import jobs           # noqa: E402
import scanner        # noqa: E402

import pytest         # noqa: E402


def _mk_job(params=None):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    scanner._reset_convert_for_test()
    with app_mod._pack_lock:
        app_mod._pack_progress.clear()
    return jobs.create("t", params or {})


def _mk_pages(name, n=3):
    base = os.path.join(config.get_scan_root(), name)
    for i in range(1, n + 1):
        open(os.path.join(base, "p%03d.png" % i), "wb").write(b"x" * 10)


def _login_admin(c):
    r0 = c.post("/api/admin/login", json={"pin": "1234", "confirm": "1234"})
    csrf = r0.get_json().get("csrf", "")
    assert csrf, "登录响应必须携带 csrf token"
    return csrf


# ==================== P1-1：release 抛异常时进度仍收尾 ====================

def test_release_once_finalizes_progress_on_release_error(monkeypatch):
    """jlock.release() 抛 RuntimeError（双释放 bug 场景）时，_pack_prog_update
    必须仍执行（active=False）——否则条目永久 active=True，GC 永不清，前端按钮
    卡到 5 分钟兜底。反向验证：恢复顺序执行版本（无 finally）→ active 残留
    True → FAIL。"""
    name = _mk_job()
    _mk_pages(name, 1)

    with app_mod.app.test_request_context("/job/%s/download.zip" % name):
        resp = app_mod.dl_zip(name)          # gen 未消费；call_on_close 已注册

    def boom_release(self):
        raise RuntimeError("job_lock released without being held: %s" % self._job)

    monkeypatch.setattr(jobs._JobLock, "release", boom_release)
    # resp.close() → call_on_close → _release_once → jlock.release() 抛
    # → finally _pack_prog_update 仍执行 → 异常继续传播
    with pytest.raises(RuntimeError):
        resp.close()
    with app_mod._pack_lock:
        rec = app_mod._pack_progress.get(name, {}).get("zip")
        assert rec is not None and rec.get("active") is False, \
            "release 抛异常时进度也必须收尾（active=False，P1-1）"


# ==================== P1-2：reorder 去重前置校验 ====================

def test_reorder_rejects_duplicate_order():
    """删除模式 order 含重复项 → 400 明确报错且任务页面原样未动（旧版走到
    重编号第二次 rename → FileNotFoundError → 全量回滚 → 500「磁盘/权限异常」
    误导排查）。反向验证：删去重校验 → 本请求 500 → FAIL。"""
    name = _mk_job()
    _mk_pages(name, 3)
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name,
               json={"order": ["p001.png", "p001.png"], "delete": True})
    assert r.status_code == 400, "重复项应 400 拒绝，实际 %s" % r.status_code
    assert "重复" in (r.get_json().get("msg") or "")
    # 任务未动：3 页原样
    assert jobs.pages(name) == ["p001.png", "p002.png", "p003.png"]


def test_reorder_duplicate_protection_tolerates_malformed():
    """类型校验（tmp04 起）：恶意混入非字符串元素（嵌套 list）不再放行到
    「页面不存在」路径，而是被前置类型校验统一 400 拒绝——tmp02 的放行策略
    在 delete=False 路径会 sorted() 抛 TypeError → 500，本轮推翻（二十二审 #1）。
    反向验证：删类型校验 → delete=False 混合元素 500 → 新增测试 FAIL。"""
    name = _mk_job()
    _mk_pages(name, 2)
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name,
               json={"order": [["p001.png"]], "delete": True})
    assert r.status_code == 400, "非字符串页面名应被 400 拒绝（非 500）"
    assert "必须为字符串" in (r.get_json().get("msg") or "") or \
           "字符串" in (r.get_json().get("msg") or ""), \
        "应报到类型校验，实际 %r" % r.get_json().get("msg")
    assert jobs.pages(name) == ["p001.png", "p002.png"]


def test_reorder_normal_mode_still_works():
    """合法排序请求不受去重校验影响（无重复排列照常通过）。"""
    name = _mk_job()
    _mk_pages(name, 3)
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name,
               json={"order": ["p003.png", "p001.png", "p002.png"]})
    assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
    assert jobs.pages(name) == ["p001.png", "p002.png", "p003.png"]  # 已重编号


# ==================== P1-3：scan_adf 线程创建失败回滚 ====================

def test_adf_thread_start_failure_rolls_back(monkeypatch):
    """Thread 构造/启动失败（OOM/OS 拒绝）时：_adf_batch 清除、scan_lock 与
    jlock 释放、异常上抛——旧版该任务永久 adf_busy + 全局扫描阻塞（仅重启
    可恢复）。反向验证：恢复裸 start → 锁不释放 → scan_lock 探测 False → FAIL。"""
    name = _mk_job()

    def boom_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", boom_start)
    with pytest.raises(RuntimeError):
        scanner.scan_adf(name)
    # 三项回滚断言
    assert not scanner._adf_batch.get(name), "批次标记必须回滚（否则永久 adf_busy）"
    assert scanner.scan_lock.acquire(blocking=False), "scan_lock 必须已释放（全局扫描不阻塞）"
    scanner.scan_lock.release()
    h = jobs.job_lock(name)
    assert h.acquire(blocking=False), "jlock 必须已释放（任务不锁死）"
    h.release()


# ==================== P2-1：_Reject 后预创建目录补偿清理 ====================

def test_save_config_reject_cleans_prep_dir(monkeypatch):
    """scan_root（confirm 创建新目录）+ scan_defaults（非法 format 触发 400）
    同请求：整体未生效 → 预创建的空目录必须清理。反向验证：恢复无清理 →
    目录残留 → FAIL。"""
    _mk_job()
    import time as _t
    import device_probe
    device_probe._cache.update(data=[], ts=_t.time(), ver=1, data_ver=1)

    c = app_mod.app.test_client()
    csrf = _login_admin(c)
    new_root = os.path.join(os.path.dirname(config.get_scan_root()), "sub2", "nroot")
    assert not os.path.isdir(new_root), "测试前提：目标目录尚不存在"
    r = c.post("/api/admin/config",
               json={"scan_root": new_root, "confirm": True,
                     "scan_defaults": {"devA": {"dpi": "150", "mode": "Gray",
                                                 "crop": False, "format": "exe"}}},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400, "非法 format 应 400（触发 _Reject）：%r" % r.get_json()
    assert not os.path.isdir(new_root), \
        "保存整体未生效时，预创建目录必须补偿清理（P2-1）"
    # v1.16.3-tmp03（二十一轮 P2-3）：多级父目录 sub2 也是本次 makedirs 递归新建的，
    # 必须一并回收——旧版只 rmdir 末级，sub2 会永久残留（用户看到失败路径下多了空目录）
    assert not os.path.isdir(os.path.dirname(new_root)), \
        "多级父目录（sub2）必须一并清理（P2-3），不得残留"
    assert os.path.normpath(config.get_scan_root()) != os.path.normpath(new_root), \
        "scan_root 不得被部分写入"


def test_save_config_success_keeps_prep_dir():
    """正常保存（全部校验通过）时预创建目录保留并被使用——清理逻辑不得误伤
    成功路径。"""
    _mk_job()
    import time as _t
    import device_probe
    device_probe._cache.update(data=[], ts=_t.time(), ver=1, data_ver=1)
    c = app_mod.app.test_client()
    csrf = _login_admin(c)
    new_root = os.path.join(os.path.dirname(config.get_scan_root()), "sub3", "okroot")
    r = c.post("/api/admin/config",
               json={"scan_root": new_root, "confirm": True},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.get_json()
    assert os.path.isdir(new_root), "成功路径目录必须保留并生效"


# ==================== P2-6：call_on_close 抛异常时 jlock 兜底 ====================

def test_call_on_close_failure_releases_lock(monkeypatch):
    """call_on_close 本身抛异常（极罕见）时：handed_to_gen 已置 True、外层
    finally 跳过、回调未注册——except 分支必须兜底 _release_once() 再上抛，
    否则 jlock 永久占用该任务（DELETE 死等）。
    反向验证：恢复裸调用 → jlock 泄漏（acquire False）→ FAIL。"""
    name = _mk_job()
    _mk_pages(name, 1)

    def boom_co(self, *a, **k):
        raise RuntimeError("call_on_close internal error")

    monkeypatch.setattr(app_mod.Response, "call_on_close", boom_co)
    with app_mod.app.test_request_context("/job/%s/download.zip" % name):
        with pytest.raises(RuntimeError):
            app_mod.dl_zip(name)
    h = jobs.job_lock(name)
    assert h.acquire(blocking=False), \
        "call_on_close 抛异常时 jlock 也必须被兜底释放（P2-6）"
    h.release()


# ==================== P2-4：Pillow 版本 importlib.metadata ====================

def test_env_report_pillow_from_metadata():
    rep = admin.env_report()
    pil = next(x for x in rep["deps"] if x["name"] == "Pillow")
    import importlib.metadata as ilm
    assert pil["ok"] and pil["ver"] == ilm.version("Pillow"), \
        "Pillow 版本应来自 importlib.metadata（P2-4），实际 %r" % pil


# ==================== P3-3：黑名单补 /var/tmp ====================

def test_var_tmp_blocked():
    assert admin._root_blocked("/var/tmp"), "/var/tmp 本体应被拦截"
    assert admin._root_blocked("/var/tmp/scans"), "/var/tmp 子路径应被拦截"
    # /srv 不拦（服务数据根，放扫描件属正当用法）
    assert not admin._root_blocked("/srv/scans"), "/srv 不应在黑名单（正当用法）"
