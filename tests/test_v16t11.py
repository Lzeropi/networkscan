# tests/test_v16t11.py — v1.16.1-tmp06/tmp07：DeepSeek 十一/十二轮审查修复验证
# 十一轮：P1-A 拒绝型条目惰性 GC（ts + _PACK_REJECT_TTL）/ P1-B worker 泄漏防回归
# 十二轮：P1-A 读时清（api_pack_progress 触发）/ P1-B 完成型条目也按 TTL 清 /
#         P1-C 取消不打印假错误 / TTL 边界严格化 / 线程差集名字过滤
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
    with app_mod._pack_lock:
        app_mod._pack_progress.clear()
    return jobs.create("t", params or {})


# ==================== GC：拒绝型/完成型条目按 TTL 清理 ====================

def test_pack_prog_reject_entry_expires():
    """409 拒绝条目（total=0/active=False）过期后应被 GC 清掉——防内存膨胀。
    v1.16.1-tmp08（十三轮 P2-E）：ts 以 _PACK_REJECT_TTL 为基准（不再硬编码 -999），
    改 TTL 时测试自动跟随。"""
    name = _mk_job()
    # 造拒绝型条目（模拟 dl_zip 409 路径写入），ts 手动拨到超 TTL
    app_mod._pack_prog_start(name, "zip", done=0, total=0, active=False,
                             msg="有页面正在转换，请稍后重试")
    with app_mod._pack_lock:
        app_mod._pack_progress[name]["zip"]["ts"] = \
            time.time() - app_mod._PACK_REJECT_TTL - 30
    # 触发 GC：任意一次新登记都会顺带清理
    app_mod._pack_prog_start(name, "pdf", done=0, total=1, active=True)
    with app_mod._pack_lock:
        assert "zip" not in app_mod._pack_progress.get(name, {}), \
            "过期拒绝条目应被 GC（P1-A）"
        # v1.16.1-tmp07（十二轮 P2-E）：补活跃条目不受 GC 误删断言
        assert "pdf" in app_mod._pack_progress.get(name, {}), \
            "GC 不得误删同任务活跃条目"


def test_pack_prog_finished_entry_expires():
    """v1.16.1-tmp07（十二轮 P1-B）：完成型条目（total>0/active=False）
    过期（ts 超 _PACK_FINISHED_TTL）后也应被 GC——旧版每用 N 个任务
    就永久驻留 N×2 条记录，此测试防"GC 只清拒绝型"的回归。
    v1.16.1-tmp08（十三轮 P2-D）：ts 以 _PACK_FINISHED_TTL 为基准
    （-TTL-100），改 TTL 时测试自动跟随，且与 60s 拒绝型 TTL 区分。"""
    name = _mk_job()
    app_mod._pack_prog_start(name, "zip", done=3, total=3, active=False)
    with app_mod._pack_lock:
        app_mod._pack_progress[name]["zip"]["ts"] = \
            time.time() - app_mod._PACK_FINISHED_TTL - 100
    app_mod._pack_prog_start("dummy-noexist", "pdf", done=0, total=1, active=True)
    with app_mod._pack_lock:
        recs = app_mod._pack_progress.get(name, {})
        assert "zip" not in recs, "过期完成型条目应被 GC（tmp07 P1-B）"


def test_pack_prog_gc_keeps_active_and_within_ttl():
    """活跃条目（active=True）永不清；TTL 内的拒绝/完成条目保留。"""
    name = _mk_job()
    # 活跃条目：active=True——即使 ts 远超任何 TTL 也保留
    app_mod._pack_prog_start(name, "zip", done=1, total=4, active=True)
    # 拒绝型条目拨到 TTL 内（60s 差 30s）：保留
    app_mod._pack_prog_start(name, "pdf", done=0, total=0, active=False, msg="转换中")
    # 完成型条目拨到 TTL 内（600s 差一半）：保留（前端兜底窗口内仍可读完成态）
    app_mod._pack_prog_start(name, "raw", done=2, total=2, active=False)
    with app_mod._pack_lock:
        app_mod._pack_progress[name]["zip"]["ts"] = \
            time.time() - app_mod._PACK_REJECT_TTL - 30
        app_mod._pack_progress[name]["pdf"]["ts"] = time.time() - 30
        app_mod._pack_progress[name]["raw"]["ts"] = time.time() - 300
    app_mod._pack_prog_start("dummy-noexist", "zip", done=0, total=1, active=True)
    with app_mod._pack_lock:
        recs = app_mod._pack_progress.get(name, {})
        assert "zip" in recs, "活跃条目（active=True）永不被 GC"
        assert "pdf" in recs, "TTL 内拒绝条目应保留（前端轮询窗口）"
        assert "raw" in recs, "TTL 内完成条目应保留（前端兜底窗口内）"


def test_pack_prog_reject_entry_within_ttl_survives():
    """TTL 内的拒绝条目保留——前端 500ms 轮询窗口内必须能读到拒绝原因。
    v1.16.1-tmp07（十二轮 P2-A）：ts 明确拨到 now-30（TTL 差半），严格验证
    TTL 边界语义，而非依赖两次 start 间隔≈0 的偶然性。"""
    name = _mk_job()
    app_mod._pack_prog_start(name, "zip", done=0, total=0, active=False,
                             msg="有页面正在转换，请稍后重试")
    with app_mod._pack_lock:
        app_mod._pack_progress[name]["zip"]["ts"] = time.time() - 30
    app_mod._pack_prog_start("dummy-noexist", "zip", done=0, total=1, active=True)
    with app_mod._pack_lock:
        rec = app_mod._pack_progress.get(name, {}).get("zip")
    assert rec is not None and rec.get("msg"), "TTL 内拒绝条目应保留且带 msg"


def test_pack_prog_read_triggers_gc():
    """v1.16.1-tmp07（十二轮 P1-A）：读接口也触发 GC——用户不再发起任何下载
    （_pack_prog_start 不被调用）时，过期条目仍能被进度轮询回收。"""
    name = _mk_job()
    # 直接手工写入过期拒绝条目（不走 _pack_prog_start，避免其触发 GC 混淆验证目标）
    with app_mod._pack_lock:
        app_mod._pack_progress.setdefault(name, {})["zip"] = {
            "kind": "zip", "done": 0, "total": 0, "active": False,
            "ts": time.time() - app_mod._PACK_REJECT_TTL - 30,
            "msg": "有页面正在转换"}
    c = app_mod.app.test_client()
    r = c.get("/api/jobs/%s/pack-progress" % name)
    assert r.status_code == 200
    with app_mod._pack_lock:
        assert "zip" not in app_mod._pack_progress.get(name, {}), \
            "读接口（api_pack_progress）应触发 GC 清掉过期条目（tmp07 P1-A）"


def test_pack_prog_ts_not_exposed_to_api():
    """内部 ts 字段不得出现在 /pack-progress 响应 JSON 里。"""
    name = _mk_job()
    app_mod._pack_prog_start(name, "zip", done=1, total=2, active=True)
    c = app_mod.app.test_client()
    r = c.get("/api/jobs/%s/pack-progress" % name)
    body = r.get_json()
    assert "zip" in body
    assert "ts" not in body["zip"], "ts 是 GC 内部字段，不应进前端 JSON"


# ==================== worker 泄漏防回归 + 取消静默 ====================

def test_zip_response_construct_failure_no_worker_leak(monkeypatch):
    """v1.16.1-tmp06（十一轮 P1-B）：Response 构造失败后 worker 线程必须退出——
    tmp05 修复在 finally 补了 cancel.set()，但旧测试只断言 jlock 释放，
    把 cancel.set() 删掉也拦不住。本测试用线程数差集堵住该缺口：
    旧版 gen 从未被消费 → cancel 未 set → worker 永久卡 W.write 死循环。
    v1.16.1-tmp07（十二轮 P2-C）：差集加线程名过滤（本项目工作线程均为
    Thread-N 命名），进一步排除其他线程干扰的误报。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    for i in range(1, 5):   # 多页让 worker 有数据可写（进入 put 循环）
        Image.new("RGB", (4, 4)).save(os.path.join(base, "p%03d.png" % i))

    def boom(*a, **k):
        raise RuntimeError("模拟 Response OOM 构造失败")

    monkeypatch.setattr(app_mod, "Response", boom)
    before = set(threading.enumerate())
    c = app_mod.app.test_client()
    r = c.get("/job/" + name + "/download.zip")
    assert r.status_code == 500, "构造失败应 500，实际 %s" % r.status_code
    # cancel.set() 通知后 worker 应在 0.5s 重检周期内退出；放宽到 5s 防慢机 flaky
    deadline = time.time() + 5
    new_threads = []
    while time.time() < deadline:
        new_threads = [t for t in threading.enumerate()
                       if t not in before and t.name.startswith("Thread-")]
        if not new_threads:
            break
        time.sleep(0.1)
    assert not new_threads, \
        "Response 构造失败后 worker 必须退出（P1-B 回归）：残留 %r" % new_threads
    # jlock 释放仍被兜底（与 tmp09 H2 语义叠加验证）
    rd = c.delete("/api/jobs/" + name)
    assert rd.get_json().get("ok") is True, "Response 失败路径 jlock 不得泄漏"


def test_zip_cancel_no_error_log():
    """v1.16.1-tmp07（P1-C）：客户端断开引发的 cancel 是设计内取消，
    worker 异常打印必须有 cancel 守卫——否则 journalctl 混入
    "[zip] worker error: zip cancelled" 假错误噪音误导排障。
    v1.16.1-tmp08（十三轮 P2-A）：改用正则精确断言「守卫必须直接包住 print」——
    旧版 guard<errlog 只保证位置先后，守卫被挪到无关位置（不再包住打印）
    时仍会误通过。"""
    import re
    src = open(os.path.join(os.path.dirname(config.__file__), "app.py"),
               encoding="utf-8").read()
    body = src.split("def worker()", 1)[1].split("def gen()", 1)[0]
    # 守卫必须直接包住 print（同一 if 体），防守卫被挪走后断言仍过
    assert re.search(r'if not cancel\.is_set\(\):\s*\n\s*print\("\[zip\] worker error',
                     body), \
        "worker 的 cancel 守卫必须直接包住 error print（P2-A）"
    # 真异常（Samba 外部删文件等）仍应打印——print 存在即保证
    assert "[zip] worker error" in body, "worker 真异常仍应打印（不可静默）"


def test_frontend_fallback_lt_finished_ttl():
    """v1.16.1-tmp08（十三轮 P1-A）：前端 dlWithProgress 的 5 分钟兜底
    （static/app.js setTimeout 300000ms）必须 < 后端完成型 TTL
    （_PACK_FINISHED_TTL）——否则完成条目被 GC 过早清除、前端轮询后半段
    读到 p=undefined 卡"打包中"直到兜底才恢复。这是前后端隐式契约，
    防止单向修改（如把前端兜底改 15 分钟）破坏。"""
    import re
    js = open(os.path.join(os.path.dirname(config.__file__),
                           "static", "app.js"), encoding="utf-8").read()
    m = re.search(r"setTimeout\(\(\) => \{ if \(timer\) stop\(\); \}, (\d+)\)", js)
    assert m, "未找到 dlWithProgress 兜底 setTimeout"
    front_fallback_ms = int(m.group(1))
    assert front_fallback_ms / 1000 < app_mod._PACK_FINISHED_TTL, \
        "前端兜底 %d ms 必须 < 后端完成 TTL %d s（隐式契约）" % (
            front_fallback_ms, app_mod._PACK_FINISHED_TTL)


# ==================== v1.16.2-tmp01：scan_defaults 400 语义（十四轮 backlog P2-B/P2-C） ====================

def _login_admin(c):
    r0 = c.post("/api/admin/login", json={"pin": "1234", "confirm": "1234"})
    csrf = r0.get_json().get("csrf", "")
    assert csrf, "登录响应必须携带 csrf token"
    return csrf


def test_scan_defaults_unsupported_value_rejected_400(monkeypatch):
    """v1.16.2-tmp01（P2-C）：探测能力可用时，选设备不支持的 dpi/mode 不再静默回退
    探测列表首项（保存"成功"但值被悄悄改掉、UI 仍显示所选值），改 400 拒绝
    并告知可选值——与 api_params 的 400 语义对齐。"""
    _mk_job()
    import device_probe
    # 伪造探测结果：设备 hpljm1005: 能力已知（75|100|150dpi，Color|Gray）
    fake_devs = [{"name": "hpljm1005:", "desc": "HP LaserJet M1005", "modes": ["Color", "Gray"],
                  "sources": [], "dpis": ["75", "100", "150"], "dpi": "75-150 dpi",
                  "dpi_raw": ["75", "100", "150"], "scan_type": "仅平板单张", "error": ""}]
    monkeypatch.setattr(device_probe, "probe", lambda force=False: (fake_devs, False))
    c = app_mod.app.test_client()
    csrf = _login_admin(c)
    # dpi=600：探测能力不含 → 400（旧版静默回退成 75）
    r = c.post("/api/admin/config", json={
        "scan_defaults": {"hpljm1005:": {"dpi": "600", "mode": "Gray", "crop": False}}},
        headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400, "不支持的 dpi 应 400 拒绝，实际 %s" % r.status_code
    assert "不支持" in (r.get_json().get("msg") or ""), "400 应告知不支持原因"
    # mode=Lineart：能力不含 → 400
    r2 = c.post("/api/admin/config", json={
        "scan_defaults": {"hpljm1005:": {"dpi": "150", "mode": "Lineart", "crop": False}}},
        headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 400, "不支持的 mode 应 400 拒绝"
    # 合法值（150/Gray 在能力内）→ 保存成功
    r3 = c.post("/api/admin/config",
                json={"scan_defaults": {"hpljm1005:": {"dpi": "150", "mode": "Gray",
                                                        "crop": False, "format": "png"}},
                      }, headers={"X-CSRF-Token": csrf})
    assert r3.get_json().get("ok") is True, "能力内的合法值应保存成功"


def test_scan_defaults_probe_failed_passes_valid(monkeypatch):
    """v1.16.2-tmp01（P2-B 边界）：探测不可用（dpi_raw/modes 为空＝探测失败或未知设备）
    时放行 VALID 白名单兜底——与 api_params「无能力信息时放行 VALID 集合」同一条
    既有约定（app.py #3 注释）；前端此时已禁用保存按钮（P2-B），后端放行仅兜底
    非交互路径（旧配置迁移/外部 API 调用）。"""
    _mk_job()
    import device_probe
    # 探测失败：设备在列表但能力全空
    fake_devs = [{"name": "hpljm1005:", "desc": "HP", "modes": [], "sources": [],
                  "dpis": [], "dpi": "", "dpi_raw": [], "scan_type": "未知", "error": "timeout"}]
    monkeypatch.setattr(device_probe, "probe", lambda force=False: (fake_devs, False))
    c = app_mod.app.test_client()
    csrf = _login_admin(c)
    r = c.post("/api/admin/config", json={
        "scan_defaults": {"hpljm1005:": {"dpi": "150", "mode": "Gray", "crop": False}}},
        headers={"X-CSRF-Token": csrf})
    assert r.get_json().get("ok") is True, "探测不可用时 VALID 白名单值应放行"


def test_scan_defaults_out_of_valid_rejected_400(monkeypatch):
    """v1.16.2-tmp01：VALID 白名单外的值（探测不可用兜底路径也要拦）→ 400，
    旧版此路径静默落库非法值（扫描时被 _params 二次回退 150——"设置不生效"）。"""
    _mk_job()
    import device_probe
    fake_devs = [{"name": "hpljm1005:", "desc": "HP", "modes": [], "sources": [],
                  "dpis": [], "dpi": "", "dpi_raw": [], "scan_type": "未知", "error": ""}]
    monkeypatch.setattr(device_probe, "probe", lambda force=False: (fake_devs, False))
    c = app_mod.app.test_client()
    csrf = _login_admin(c)
    r = c.post("/api/admin/config", json={
        "scan_defaults": {"hpljm1005:": {"dpi": "12345", "mode": "Gray", "crop": False}}},
        headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400, "VALID 白名单外的 dpi 应 400 拒绝"
    # 合法 format 白名单外也要 400（旧版静默回退 png）
    r2 = c.post("/api/admin/config", json={
        "scan_defaults": {"hpljm1005:": {"dpi": "150", "mode": "Gray", "crop": False, "format": "tiff"}}},
        headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 400, "白名单外的 format 应 400 拒绝"


def test_admin_js_probe_failed_disables_save():
    """v1.16.2-tmp01（P2-B）：前端探测失败（dpi_raw/modes 为空）必须禁用保存按钮
    且不再伪造 fallback 档位（旧版 x.dpi_raw || ["75",...] 展示假能力诱导保存）。
    源码级静态断言——只查代码行（行首为语句），排除注释里的历史写法引用。"""
    import re
    js = open(os.path.join(os.path.dirname(config.__file__),
                           "static", "admin.js"), encoding="utf-8").read()
    # 旧 fallback 伪造写法必须已移除（仅代码行；注释中的历史引用不参与判定）
    code_lines = [ln for ln in js.splitlines()
                  if ln.strip() and not ln.strip().startswith("//")]
    code = "\n".join(code_lines)
    assert 'x.dpi_raw || ["75"' not in code, "DPI 选项不得伪造 fallback 档位（P2-B）"
    assert 'x.modes || ["Gray","Color"]' not in code, "模式选项不得伪造 fallback 档位（P2-B）"
    # dpiKnown/modeKnown 判定存在（空列表即探测失败）
    assert "const dpiKnown" in code and "const modeKnown" in code, \
        "必须有 dpi_known/mode_known 探测结果判定"
    # probeFailed 判定 + 禁用保存按钮逻辑存在
    assert "const probeFailed" in code and "disabled" in code, \
        "探测失败须判定 probeFailed 并禁用保存按钮"
    m = re.search(r"probeFailed\?' disabled[^']*'", code)
    assert m, "保存按钮必须有 probeFailed 条件禁用"
    # 探测失败时提示文案存在（引导用户重新探测）
    assert "重新探测" in code, "探测失败须提示用户点「重新探测」"
