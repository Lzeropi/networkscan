# tests/test_v16t15.py — v1.16.3-tmp03：DeepSeek 二十一轮审查修复验证
# P2-1  非 _Reject 异常（磁盘满等）时预创建目录必须清理
# P2-2  makedirs 成功但 chmod 失败时预创建目录必须清理（含多级父目录）
# P2-3  多级父目录一并回收（加强版在 test_v16t14.py 的 sub2 断言）
# P3-1  _scan_clear_current(expected_job) 防误清其他任务标记；scan_flatbed 单次收口
# P3-2  scan_error 分支 jlock.release() 抛异常不掩盖真实扫描错误
import os
import shutil
import sys
import time as _t

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin            # noqa: E402
import app as app_mod   # noqa: E402
import config           # noqa: E402
import jobs             # noqa: E402
import scanner          # noqa: E402

import pytest           # noqa: E402


def _mk_job(params=None):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    scanner._reset_convert_for_test()
    with app_mod._pack_lock:
        app_mod._pack_progress.clear()
    return jobs.create("t", params or {})


def _login_admin(c):
    r0 = c.post("/api/admin/login", json={"pin": "1234", "confirm": "1234"})
    csrf = r0.get_json().get("csrf", "")
    assert csrf, "登录响应必须携带 csrf token"
    return csrf


def _fake_script(path, body):
    open(path, "w").write(body)
    os.chmod(path, 0o755)
    return path


def _prime_device_cache():
    """探测能力不可用（data 空）——让请求能走到 mutate 的后续校验/落盘，不被探测能力拦截。"""
    import device_probe
    device_probe._cache.update(data=[], ts=_t.time(), ver=1, data_ver=1)


# ==================== P2-1：非 _Reject 异常也清理预创建目录 ====================

def test_save_config_non_reject_exception_cleans_prep_dir(monkeypatch):
    """磁盘满等导致 save_admin_cfg 抛 OSError（非 _Reject）时，本次保存整体未生效，
    预创建目录（含多级父目录）必须同样清理——旧版只挂 `except _Reject`，500 后目录
    永久残留。反向验证：删掉 `except Exception` 的清理 → 目录残留 → FAIL。"""
    _mk_job()
    _prime_device_cache()
    c = app_mod.app.test_client()
    csrf = _login_admin(c)
    new_root = os.path.join(os.path.dirname(config.get_scan_root()), "subX", "nroot")
    assert not os.path.isdir(new_root), "测试前提：目标目录尚不存在"

    def boom_save(cfg):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(config, "_save_admin_cfg_unlocked", boom_save)
    r = c.post("/api/admin/config",
               json={"scan_root": new_root, "confirm": True},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 500, "save 抛 OSError 应 500，实际 %s" % r.status_code
    assert not os.path.isdir(new_root), \
        "非 _Reject 异常（磁盘满）也必须清理预创建目录（P2-1）"
    assert not os.path.isdir(os.path.dirname(new_root)), \
        "多级父目录同样清理（P2-1 + P2-3 联动）"


# ==================== P2-2：chmod 失败时预创建目录清理 ====================

def test_save_config_chmod_failure_cleans_prep_dir(monkeypatch):
    """makedirs 成功但 chmod 失败（目录刚建好、权限设置失败）时，预创建目录必须
    被清理。旧版 `_prep_created = True` 在 chmod 之后才置位——chmod 失败时目录已建
    却无标记，400 返回且残留。反向验证：恢复原顺序 → 目录残留 → FAIL。"""
    _mk_job()
    _prime_device_cache()
    c = app_mod.app.test_client()
    csrf = _login_admin(c)
    new_root = os.path.join(os.path.dirname(config.get_scan_root()), "subY", "newroot")
    assert not os.path.isdir(new_root), "测试前提：目标目录尚不存在"

    real_chmod = os.chmod
    hits = {"n": 0}

    def boom_chmod(path, mode, *a, **k):
        if os.path.normpath(path) == os.path.normpath(new_root):
            hits["n"] += 1
            raise PermissionError(13, "Permission denied (mock)")
        return real_chmod(path, mode, *a, **k)

    monkeypatch.setattr(os, "chmod", boom_chmod)
    r = c.post("/api/admin/config",
               json={"scan_root": new_root, "confirm": True},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400, "chmod 失败应 400，实际 %s" % r.status_code
    assert hits["n"] == 1, "chmod 应被拦截一次（预创建段），实际 %d" % hits["n"]
    assert not os.path.isdir(new_root), \
        "chmod 失败时预创建目录必须被清理（P2-2）"
    assert not os.path.isdir(os.path.dirname(new_root)), \
        "多级父目录同样清理（P2-2 + P2-3 联动）"


# ==================== P3-1：_scan_clear_current(expected_job) ====================

def test_scan_clear_current_expected_job_guards_own():
    """expected_job 参数只清自己的标记——scan_flatbed/scan_adf 完成且 scan_lock 已
    释放后，其他任务可能在毫秒窗口启动新扫描设新 _scan_job，此时无条件清理会误清新
    任务的标记（tmp01 持 guard 修复的残余缺口）。不传参（None）保持旧行为（无条件
    清理），保证既有调用/测试兼容。反向验证：去掉 expected_job 检查 → 误清 → FAIL。"""
    scanner._scan_job = "jobA"
    scanner._scan_start_time = 123.0
    # 其他任务 B 已接管：传 A 不应清 B
    scanner._scan_clear_current("jobB")
    assert scanner._scan_job == "jobA", "expected_job 不等时必须跳过清理（防误清其他任务）"
    assert scanner._scan_start_time == 123.0
    # 传匹配的任务 → 清自己的标记
    scanner._scan_clear_current("jobA")
    assert scanner._scan_job is None
    assert scanner._scan_start_time is None
    # 不传参（None）= 旧行为，无条件清理
    scanner._scan_job = "jobC"
    scanner._scan_clear_current()
    assert scanner._scan_job is None


def test_scan_flatbed_clear_current_single_call_with_job(monkeypatch, tmp_path):
    """scan_flatbed 的 _scan_clear_current 收口为 finally 单次调用，且传 expected_job=job
    （旧版成功路径 + except 兜底各一次）。反向验证：双调用版 → 计数 2 → FAIL。"""
    name = _mk_job()
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh",
                                     "#!/bin/sh\nexit 0\n"))

    real = scanner._scan_clear_current
    seen = {"n": 0, "args": []}

    def counting(*a, **k):
        seen["n"] += 1
        seen["args"].append(a)
        return real(*a, **k)

    monkeypatch.setattr(scanner, "_scan_clear_current", counting)
    r = scanner.scan_flatbed(name)
    assert r == "p001.png", "假 scanimage 成功应返回文件名，实际 %r" % r
    assert seen["n"] == 1, "scan_flatbed 的 _scan_clear_current 必须恰好调用一次（P3-1），实际 %d" % seen["n"]
    assert seen["args"] == [(name,)], \
        "收口调用必须传 expected_job=job（防误清其他任务），实际 %r" % (seen["args"],)


# ==================== P3-2：scan_error 分支 release 失败不掩盖真实错误 ====================

def test_scan_flatbed_scan_error_keeps_real_reason(monkeypatch, tmp_path):
    """scan_error 分支 jlock.release() 抛异常（双释放 bug 信号）时，真实扫描错误
    （此处为假 scanimage 命令退出码）必须正常上抛——旧版「先 release 后置标志」会让
    release 异常落入外层兜底二次释放，日志中只剩「released without being held」、
    丢失用户真正需要的「扫描超时/设备故障」。反向验证：恢复原顺序 → msg 变 release
    错误 → FAIL。"""
    name = _mk_job()
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh",
                                     "#!/bin/sh\nexit 1\n"))

    def boom_release(self):
        raise RuntimeError("job_lock released without being held: %s" % self._job)

    monkeypatch.setattr(jobs._JobLock, "release", boom_release)
    with pytest.raises(RuntimeError) as ei:
        scanner.scan_flatbed(name)
    msg = str(ei.value)
    assert "命令退出码" in msg, "真实扫描错误必须呈现，实际 %r" % msg
    assert "released without" not in msg, "release 失败不得覆盖 scan_error，实际 %r" % msg
    # scan_lock 是模块级 threading.Lock，release 在 scan_error 分支之前已真实释放
    # （不受 _JobLock mock 影响）——必须可获取
    assert scanner.scan_lock.acquire(blocking=False), "scan_lock 必须已释放"
    scanner.scan_lock.release()
    # jlock 在此场景未释放：mock 的 _JobLock.release 抛异常＝锁确实没释放成功
    # （这正是「双释放 bug」信号本身）——锁泄漏与否取决于 release 是否成功，非本
    # 修复范围；本测试只验证「release 失败不得覆盖真实 scan_error」这一语义。
    # 状态已记为真实错误
    st = scanner.get_state(name)
    assert st["state"] == "error", "状态必须 error，实际 %s" % st


# ==================== P3-7：Pillow 元数据失败退回 __version__ 兜底 ====================

def test_env_report_pillow_falls_back_on_metadata_error(monkeypatch):
    """importlib.metadata.version 抛 PackageNotFoundError（editable 安装等非标准
    安装方式）时，Pillow 实际可用（import PIL 已成功）——不得误报「未安装」，
    退回 PIL.__version__。反向验证：删掉兜底 except → 落「未安装」→ FAIL。"""
    import PIL
    import importlib.metadata as ilm

    def boom_version(name):
        raise ilm.PackageNotFoundError(name)

    monkeypatch.setattr(ilm, "version", boom_version)
    rep = admin.env_report()
    pil = next(x for x in rep["deps"] if x["name"] == "Pillow")
    assert pil["ok"] is True, "PIL import 成功时不得误报未安装，实际 %r" % pil
    assert pil["ver"] == getattr(PIL, "__version__", "已安装"), \
        "元数据失败应退回 PIL.__version__ 兜底（P3-7），实际 %r" % pil