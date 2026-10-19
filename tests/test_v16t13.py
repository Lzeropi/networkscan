# tests/test_v16t13.py — v1.16.3-tmp01：DeepSeek 十九轮报告 backlog P2×2 + P3×9 修复验证
# P2-1  _scan_job 清理收口 _scan_clear_current()（持 guard，防 scan-status 误报）
# P2-2  api_save_config 目录创建挪出 _scan_start_guard（guard 内不留慢 I/O）
# P3-1  _delete_locked pop 收 finally（rmtree 半途失败也不留孤儿条目）
# P3-2  jobs.create save 失败清理空目录
# P3-3  _pack_prog_start 存在即重建（拒绝登记不残留旧 done）
# P3-4  admin.js 单卡提交 + 后端 device_alias/scan_defaults merge
# P3-8  env_report Flask 版本改 importlib.metadata
# P3-9  pyvenv 改 sys.prefix
# P3-6  兜底 setTimeout 正则抗格式化（自证）
import ast
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin          # noqa: E402
import app as app_mod  # noqa: E402
import config         # noqa: E402
import jobs           # noqa: E402
import scanner        # noqa: E402


def _mk_job(params=None):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    scanner._reset_convert_for_test()
    with app_mod._pack_lock:
        app_mod._pack_progress.clear()
    return jobs.create("t", params or {})


# ==================== P2-1：清理当前扫描标记必须持 guard ====================

def test_scan_clear_current_holds_guard():
    """_scan_clear_current 必须在 _scan_start_guard 持有下清理——他线程持 guard
    时，清理必须阻塞（对称于启动段的互斥）。这正是收修竞态（旧收尾把新扫描启动
    段刚设的 _scan_job 清掉 → scan-status 误报空闲）的机制本体。
    反向验证：改回锁外赋值两行 → 本测试 FAIL（清理不等 guard）。"""
    scanner._scan_job = "jobX"
    scanner._scan_start_time = 123.0
    gate = threading.Event()

    def hold_guard():
        with scanner._scan_start_guard:
            gate.set()
            time.sleep(1.5)         # 持 guard 期间，清理必须仍在等

    t = threading.Thread(target=hold_guard, daemon=True)
    t.start()
    assert gate.wait(timeout=2)

    done = threading.Event()

    def do_clear():
        scanner._scan_clear_current()
        done.set()

    tc = threading.Thread(target=do_clear, daemon=True)
    tc.start()
    time.sleep(0.4)
    assert scanner._scan_job == "jobX", \
        "他线程持 guard 期间，清理不得提前完成（P2-1）"
    assert not done.wait(timeout=0.1)
    tc.join(timeout=5)
    assert scanner._scan_job is None and scanner._scan_start_time is None, \
        "guard 释放后清理必须完成"


def test_scan_funcs_no_bare_scan_job_assignment():
    """静态断言：scan_flatbed/scan_adf 函数体内不再存在 `_scan_job = None`
    清理式赋值（四处已全收口 _scan_clear_current）——防未来改动绕过收口；
    启动段的 `_scan_job = job`（设置新任务）是合法的，不在禁列。"""
    src = open(os.path.join(os.path.dirname(config.__file__), "scanner.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("scan_flatbed", "scan_adf"):
            for tgt in ast.walk(node):
                if (isinstance(tgt, ast.Assign) and len(tgt.targets) == 1
                        and isinstance(tgt.targets[0], ast.Name)
                        and tgt.targets[0].id == "_scan_job"
                        and isinstance(tgt.value, ast.Constant)
                        and tgt.value.value is None):
                    raise AssertionError(
                        "%s 内不得再直接清理 _scan_job = None（须走 _scan_clear_current）"
                        % node.name)


# ==================== P2-2：目录创建在 guard 外完成 ====================

def test_save_config_makedirs_outside_guard(monkeypatch):
    """confirm 建目录路径：他线程持 _scan_start_guard 期间发起保存，目录必须在
    guard 释放前就已创建（证明 makedirs 在 guard 外执行）；guard 释放后保存完成。
    反向验证：恢复 guard 内 makedirs（删前置段）→ 目录在 guard 释放前不存在 → FAIL。"""
    name = _mk_job()
    # 注意：不用 `with test_client()`——块级 app ctx 存于主线程 ContextVar，
    # 子线程请求后 pop 会 LookupError（与 test_v16t11 同用非 with 形式）。
    c = app_mod.app.test_client()
    csrf = _login_admin(c)
    new_root = os.path.join(os.path.dirname(config.get_scan_root()), "sub", "nroot")
    gate = threading.Event()
    release = threading.Event()

    def hold_guard():
        with scanner._scan_start_guard:
            gate.set()
            release.wait(timeout=10)

    t = threading.Thread(target=hold_guard, daemon=True)
    t.start()
    assert gate.wait(timeout=2)

    result = {}

    def do_save():
        try:
            r = c.post(
                "/api/admin/config",
                json={"scan_root": new_root, "confirm": True},
                headers={"X-CSRF-Token": csrf})
            result["resp"] = r.status_code
            result["body"] = r.get_json()
        except Exception:                           # pragma: no cover - 调试兜底
            import traceback
            result["exc"] = traceback.format_exc()[-500:]

    ts = threading.Thread(target=do_save, daemon=True)
    ts.start()
    # guard 仍被持有、保存请求仍在等 guard——目录应已建好（guard 外准备段）
    time.sleep(0.6)
    if not os.path.isdir(new_root):   # 诊断窗口：给 do_save 一点时间产生输出
        time.sleep(1.5)
    assert os.path.isdir(new_root), \
        "guard 被他人持有时，目录创建必须已完成（guard 外准备段，P2-2），线程输出：%r" % result
    assert not result, "guard 释放前保存不完成（update_admin_cfg 仍须等 guard）"
    release.set()
    t.join(timeout=5)
    ts.join(timeout=5)
    assert result.get("resp") == 200, "guard 释放后保存应成功：%r" % result
    assert os.path.normpath(config.get_scan_root()) == os.path.normpath(new_root)


def _login_admin(c):
    r0 = c.post("/api/admin/login", json={"pin": "1234", "confirm": "1234"})
    csrf = r0.get_json().get("csrf", "")
    assert csrf, "登录响应必须携带 csrf token"
    return csrf


# ==================== P3-1：rmtree 半途失败也不留孤儿条目 ====================

def test_delete_cleans_state_even_if_rmtree_fails(monkeypatch):
    """rmtree 抛 OSError（半途失败）时，state/_pack_progress 条目也必须被清
    （finally 语义）。反向验证：恢复 rmtree 后顺序执行（旧版）→ FAIL。"""
    name = _mk_job()
    scanner.state[name] = {"state": "done"}
    with app_mod._pack_lock:
        app_mod._pack_progress[name] = {"zip": {"kind": "zip", "active": False}}

    def boom(p):
        raise OSError("磁盘半途故障模拟")

    monkeypatch.setattr(jobs.shutil, "rmtree", boom)
    try:
        jobs.delete(name)
        raise AssertionError("rmtree 失败应上抛")
    except OSError:
        pass
    assert name not in scanner.state, \
        "rmtree 失败后 state 条目也必须清（P3-1 finally）"
    with app_mod._pack_lock:
        assert name not in app_mod._pack_progress, \
            "rmtree 失败后 _pack_progress 条目也必须清（P3-1 finally）"


# ==================== P3-2：create save 失败清理空目录 ====================

def test_create_cleans_dir_on_save_failure(monkeypatch):
    """save 抛 OSError 时，create 必须清理刚建的空任务目录再上抛。
    反向验证：恢复无清理版本 → FAIL（残留目录）。"""
    _mk_job()

    def boom(job, meta, root=None):
        raise OSError("磁盘满模拟")

    monkeypatch.setattr(jobs, "save", boom)
    before = set(os.listdir(config.get_scan_root()))
    try:
        jobs.create("新任务")
        raise AssertionError("save 失败应上抛")
    except OSError:
        pass
    after = set(os.listdir(config.get_scan_root()))
    assert before == after, "save 失败不得残留空任务目录（P3-2）"


# ==================== P3-3：_pack_prog_start 存在即重建 ====================

def test_pack_prog_start_rebuilds_fresh_entry():
    """【契约】_pack_prog_start 语义是"存在即重建"：任何未显式传入的字段必须
    恢复默认值，不得残留旧条目的 done/msg 等键。现有调用点恰好都显式传了
    done/total/active（update 恰好覆盖），但 msg 等未传键会残留、未来新调用点
    漏传键也会残留——本测试以"不带 kw 的最小调用"钉死重建契约。
    反向验证：恢复 setdefault+update 版本 → 残留 done=5/total=5/active=False → FAIL。"""
    name = _mk_job()
    # 老条目：done=5/total=5/active=False/带 msg（完成后/拒绝后的典型残留形态）
    app_mod._pack_prog_start(name, "zip", done=5, total=5, active=False, msg="旧提示")
    # 最小重建调用（不带 kw）——必须得到全新默认条目
    app_mod._pack_prog_start(name, "zip")
    with app_mod._pack_lock:
        rec = dict(app_mod._pack_progress[name]["zip"])
    assert rec["done"] == 0 and rec["total"] == 0 and rec["active"] is True, \
        "未传字段必须恢复默认（P3-3 重建契约），实际残留：%r" % rec
    assert "msg" not in rec, "旧 msg 不得残留（P3-3）"


# ==================== P3-4：后端单卡提交 merge 语义 ====================

def test_save_config_single_device_merge(monkeypatch):
    """前端按卡只提交当前设备键时，未提及设备已保存的别名/默认值必须保留
    （merge 而非全量替换）；空字符串别名清除该设备。反向验证：恢复全量替换 →
    设备 A 的别名被空提交清空 → FAIL。"""
    _mk_job()
    # 预置：设备 A 已有别名与默认值
    from config import load_admin_cfg
    cfg = load_admin_cfg()
    cfg["device_alias"] = {"devA:usb": "楼上一体机"}
    cfg["scan_defaults"] = {"devA:usb": {"dpi": "150", "mode": "Gray",
                                          "crop": False, "format": "png"}}
    from config import save_admin_cfg
    save_admin_cfg(cfg)

    c = app_mod.app.test_client()
    csrf = _login_admin(c)
    # 单卡提交设备 B（VALID 值，探测不可用场景走白名单放行）
    import device_probe
    device_probe._cache.update(data=[], ts=time.time(), ver=1, data_ver=1)
    r = c.post("/api/admin/config",
               json={"device_alias": {"devB:usb": "楼下扫描仪"},
                     "scan_defaults": {"devB:usb": {"dpi": "300", "mode": "Color",
                                                     "crop": True, "format": "webp"}}},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    out = r.get_json()
    assert out["device_alias"].get("devA:usb") == "楼上一体机", \
        "未提及设备的别名必须保留（P3-4 merge）"
    assert out["device_alias"].get("devB:usb") == "楼下扫描仪"
    assert out["scan_defaults"]["devA:usb"]["dpi"] == "150", \
        "未提及设备的默认值必须保留（P3-4 merge）"
    assert out["scan_defaults"]["devB:usb"]["dpi"] == "300"
    assert out["scan_defaults"]["devB:usb"]["format"] == "webp"
    # 空字符串别名＝清除该设备，另一设备不受影响
    r2 = c.post("/api/admin/config",
                json={"device_alias": {"devB:usb": ""}},
                headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 200
    out2 = r2.get_json()
    assert "devB:usb" not in out2["device_alias"], "空字符串应清除该设备别名"
    assert out2["device_alias"].get("devA:usb") == "楼上一体机", \
        "清除 B 不得影响 A"


# ==================== P3-8/9：env_report 取值方式 ====================

def test_env_report_flask_version_from_metadata():
    """Flask 版本号取自 importlib.metadata（弃用的 flask.__version__ 不再使用）。"""
    rep = admin.env_report()
    ver = rep["deps"][1]["ver"]
    import importlib.metadata as ilm
    assert ver == ilm.version("flask"), "Flask 版本应等于 importlib.metadata 报告值"


def test_env_report_pyvenv_is_sys_prefix():
    """pyvenv 直取 sys.prefix——不再对 flask.__file__ 五层 dirname 推断。"""
    rep = admin.env_report()
    assert rep["dep_paths"]["pyvenv"] == sys.prefix, \
        "pyvenv 应等于 sys.prefix（P3-9）"


# ==================== P3-6：兜底正则抗格式化（自证） ====================

def test_fallback_regex_tolerates_formatting():
    """宽松正则在被 prettier 等格式化的多行写法下仍能命中兜底毫秒数——
    旧逐字符正则会失配（假失败掩盖真实回归）。"""
    import re
    pat = r"setTimeout\(\s*\(\)\s*=>\s*\{[^{}]*\bstop\(\)[^{}]*\}\s*,\s*(\d{4,})\s*\)"
    # 原始写法
    m = re.search(pat, "setTimeout(() => { if (timer) stop(); }, 300000)")
    assert m and int(m.group(1)) == 300000
    # 格式化后的等价多行写法
    m2 = re.search(pat,
                   "setTimeout(\n  () => {\n    if (timer) stop();\n  },\n  300000\n)")
    assert m2 and int(m2.group(1)) == 300000, \
        "多行格式化写法必须同样命中（P3-6）"
