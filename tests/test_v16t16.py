# tests/test_v16t16.py — v1.16.3-tmp04：二十二审遗留清尾验证
# #1  reorder 元素类型前置校验（delete=False 混合类型不再 500）
# #5  scan_error 分支 jlock.release 失败记 stderr（journalctl 可查）
# #6  _prep_missing_dirs 用 lexists：broken symlink 不入追踪、不误删
import os
import shutil
import sys
import time as _t

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin            # noqa: E402
import app as app_mod   # noqa: E402
import jobs             # noqa: E402
import scanner          # noqa: E402


def _mk_job_real(letters=("A",)):
    import config
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    scanner._reset_convert_for_test()
    with app_mod._pack_lock:
        app_mod._pack_progress.clear()
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


# ==================== #1：reorder delete=False 混合类型 400 ====================

def test_reorder_delete_false_mixed_types_400(tmp_path):
    """delete=False（普通排序）传含非字符串元素的 order——旧版在
    sorted(new_order) != sorted(current) 比对处抛 TypeError → 500
    「磁盘/权限异常」误导排查（二十二审 #1 认定的唯一真实缺陷路径）；
    现 type 校验前置统一 400。反向验证：删类型校验 → sorted 抛 → 500 → FAIL。"""
    name = _mk_job_real(("A", "B"))
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name,
               json={"order": [["p001.png"], "p002.png"], "delete": False})
    assert r.status_code == 400, "混合类型 delete=False 必须 400（旧版 500），实际 %s" % r.status_code
    assert "字符串" in (r.get_json().get("msg") or ""), \
        "应报类型校验错误，实际 %r" % r.get_json().get("msg")
    # 页面未被任何方式动过
    assert jobs.pages(name) == ["p001.png", "p002.png"]
    # 合法请求不受影响（同请求改纯字符串排列应正常）
    r2 = c.post("/api/jobs/%s/reorder" % name,
                json={"order": ["p002.png", "p001.png"], "delete": False})
    assert r2.status_code == 200 and r2.get_json().get("ok"), r2.get_json()
    assert jobs.pages(name) == ["p001.png", "p002.png"]   # 已重编号回连续


# ==================== #5：scan_error 分支 release 失败记 stderr ====================

def test_scan_flatbed_release_failure_logs_stderr(monkeypatch, tmp_path, capsys):
    """scan_error 分支 jlock.release() 抛异常（双释放 bug 信号）被吞掉前必须
    记 stderr——旧版（tmp03）完全静默，锁泄漏只能靠后续全部 busy 间接观察
    （二十二审 #5）。反向验证：删 print → stderr 无记录 → FAIL。"""
    name = _mk_job_real()
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh",
                                     "#!/bin/sh\nexit 1\n"))

    def boom_release(self):
        raise RuntimeError("job_lock released without being held: %s" % self._job)

    monkeypatch.setattr(jobs._JobLock, "release", boom_release)
    try:
        scanner.scan_flatbed(name)
        raised = None
    except RuntimeError as e:
        raised = e
    # 真实 scan_error 照常上抛（tmp03 P3-2 语义不回退）
    assert raised is not None and "命令退出码" in str(raised), \
        "真实扫描错误必须上抛，实际 %r" % raised
    # #5 新增：stderr 有留痕（journalctl 可查）
    err = capsys.readouterr().err
    assert "[scan] jlock.release failed in scan_error path" in err, \
        "release 失败必须记 stderr（二十二审 #5），实际 stderr=%r" % err
    assert "released without being held" in err, "stderr 应含原始异常信息"


# ==================== #6：broken symlink 不入追踪、不误删 ====================

def test_prep_missing_dirs_broken_symlink_not_tracked(tmp_path):
    """broken symlink（指向已消失目标）的 exists=False、lexists=True——旧版
    （tmp03）会把它当「待创建层」纳入追踪列表（makedirs 随后 FileExistsError、
    清理 rmdir 对 symlink 抛 ENOTDIR，结局安全但链条绕）；lexists 直接判
    「已存在」不追踪（二十二审 #6）。反向验证：改回 exists → created 非空 → FAIL。"""
    link = os.path.join(str(tmp_path), "nroot")          # 父目录 tmp_path 已存在
    os.symlink(os.path.join(str(tmp_path), "gone"), link)
    assert not os.path.exists(link) and os.path.lexists(link), "测试前提：broken symlink"
    created = admin._prep_missing_dirs(link)
    assert created == [], "broken symlink 不得纳入追踪列表（lexists 判已存在），实际 %r" % created
    # 追踪为空 → 清理为 no-op，symlink 绝不会被触碰
    admin._cleanup_prep_dirs(created)
    assert os.path.lexists(link), "broken symlink 必须原样保留"


def test_save_config_broken_symlink_root_safe(monkeypatch, tmp_path):
    """端到端层：scan_root 指向 broken symlink 的目录——保存应 400（makedirs
    FileExistsError），且 symlink 原样保留绝不误删（二十二审 #6 行为保障）。"""
    import config
    import time as _time
    import device_probe
    _mk_job_real()
    device_probe._cache.update(data=[], ts=_time.time(), ver=1, data_ver=1)
    c = app_mod.app.test_client()
    r0 = c.post("/api/admin/login", json={"pin": "1234", "confirm": "1234"})
    csrf = r0.get_json().get("csrf", "")
    assert csrf

    link = os.path.join(str(tmp_path), "scanlink")       # 父目录 tmp_path 已存在
    target = os.path.join(str(tmp_path), "gone")
    os.symlink(target, link)

    r = c.post("/api/admin/config",
               json={"scan_root": link, "confirm": True},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400, "指向 broken symlink 的路径应 400，实际 %s" % r.status_code
    # symlink 原样保留：链仍存在且目标不变（绝不误删用户的 symlink）
    assert os.path.lexists(link), "broken symlink 不得被清理误删"
    assert os.readlink(link) == target, "symlink 指向不得被改变"
    # scan_root 未变
    assert os.path.normpath(config.get_scan_root()) != os.path.normpath(link)