# tests/test_v1142.py — v1.14.2 修复验证（ChatGPT 二次审计 18 项中的 16 项有效问题）
# 覆盖：P0-1 CSRF 接线由冒烟负责；P0-2 锁顺序（平板忙路径 + ADF started 持锁）、
# #3 ADF 转换完整性、#4 reorder 事务化、#5/#8 config 并发写+0600、#6/#7 ZIP 生命周期、
# #10 页码>999、#11 锁回收、#12 ID 碰撞重试、#14 探测缓存失效、#15 PNM 跨重启。
import io
import os
import sys
import tempfile
import threading
import time
import zipfile as zf

_TEST_ROOT = tempfile.mkdtemp(prefix="scanweb_v1142_")
os.environ["SCAN_ROOT"] = _TEST_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin  # noqa: E402,F401
import app as app_mod  # noqa: E402
import config  # noqa: E402
import jobs  # noqa: E402
import scanner  # noqa: E402


def _reset():
    import shutil
    for name in os.listdir(_TEST_ROOT):
        shutil.rmtree(os.path.join(_TEST_ROOT, name), ignore_errors=True)
    scanner.state.clear()


def _mk_job(npages=1, **meta_extra):
    name = jobs.create()
    d = os.path.join(config.get_scan_root(), name)
    for i in range(1, npages + 1):
        with open(os.path.join(d, "p%03d.png" % i), "wb") as f:
            f.write(b"page")
    m = jobs.load(name)
    m.update(meta_extra)
    m["pages"] = npages
    jobs.save(name, m)
    return name


def _fake_script(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


# ---------- P0-2：平板忙路径必须释放 job_lock ----------
def test_flatbed_busy_releases_joblock():
    _reset()
    name = jobs.create()
    assert scanner.scan_lock.acquire(blocking=False)
    try:
        try:
            scanner.scan_flatbed(name)
            raise AssertionError("设备忙必须抛 RuntimeError")
        except RuntimeError as e:
            assert "设备忙" in str(e)
        lk = jobs.job_lock(name)
        assert lk.acquire(blocking=False), "P0-2：忙路径必须立即释放 job_lock，不得泄漏"
        lk.release()
    finally:
        scanner.scan_lock.release()


# ---------- P0-2：扫描期间 DELETE 必须排队等待（job_lock 生命周期语义） ----------
def test_flatbed_delete_waits_during_scan(monkeypatch):
    _reset()
    name = jobs.create()
    fake = _fake_script(os.path.join(_TEST_ROOT, "_slow_scan"),
                        "#!/bin/sh\nsleep 0.4\nexit 0\n")
    monkeypatch.setattr(scanner, "SCANIMAGE", fake)
    monkeypatch.setattr(scanner, "CONVERT", "/nonexistent/convert")
    t = threading.Thread(target=scanner.scan_flatbed, args=(name,), daemon=True)
    t.start()
    time.sleep(0.1)                                  # 让扫描线程先取得双锁
    client = app_mod.app.test_client()
    t0 = time.time()
    rd = client.delete("/api/jobs/" + name)          # 应阻塞至扫描+转换结束
    elapsed = time.time() - t0
    assert elapsed > 0.2, "DELETE 必须等扫描结束，实际 %.2fs——锁顺序/生命周期被破坏" % elapsed
    assert rd.get_json().get("ok") is True
    t.join(timeout=5)
    assert not os.path.isdir(os.path.join(config.get_scan_root(), name)), "等待后删除必须成功"


# ---------- P0-2：ADF 返回 started 时 job_lock 必须已被持有 ----------
def test_adf_joblock_held_before_started_returns(monkeypatch):
    _reset()
    import device_probe
    name = jobs.create()
    meta = jobs.load(name)
    meta["params"] = {"source": "adf", "device": "nosuchdev:", "dpi": "150",
                      "mode": "Gray", "crop": False, "source_name": "ADF"}
    jobs.save(name, meta)
    device_probe._cache["data"] = []                 # 探测缓存注入空列表（fail-closed 场景）
    device_probe._cache["ts"] = 1e18
    try:
        assert scanner.scan_adf(name) == "started"
        lk = jobs.job_lock(name)
        assert not lk.acquire(blocking=False), \
            "P0-2：API 返回 started 时 job_lock 必须已被 worker 持有（否则删除竞态窗口仍在）"
        for _ in range(100):                         # 等 worker fail-closed 收尾
            if scanner.get_state(name)["state"] in ("error", "done"):
                break
            time.sleep(0.02)
        assert lk.acquire(blocking=False), "worker 结束后锁必须释放"
        lk.release()
    finally:
        device_probe._cache["data"] = None
        device_probe._cache["ts"] = 0.0


# ---------- #3：ADF 转换完整性——部分失败必须报 error ----------
def test_convert_pnms_returns_failure_stats(monkeypatch):
    _reset()
    name = jobs.create()
    d = os.path.join(config.get_scan_root(), name)
    for f in ("p001.pnm", "p002.pnm"):
        with open(os.path.join(d, f), "wb") as fh:
            fh.write(b"P5")
    fake = _fake_script(os.path.join(_TEST_ROOT, "_cv"),
                        '#!/bin/sh\ncase "$1" in *p002.pnm) exit 1;; esac\n: > "$2"\nexit 0\n')
    monkeypatch.setattr(scanner, "CONVERT", fake)
    ok, failed = scanner._convert_pnms(name, 1)
    assert ok == 1 and failed == ["p002.pnm"], "#3：必须返回转换统计而非静默吞掉"


def test_adf_partial_conversion_reports_error(monkeypatch):
    _reset()
    import device_probe
    name = jobs.create()
    scan_fake = _fake_script(os.path.join(_TEST_ROOT, "_adf_scan"), (
        '#!/bin/sh\npat=""\nfor a in "$@"; do\n'
        '  case "$a" in --batch=*) pat="${a#--batch=}";; esac\ndone\n'
        'dir=$(dirname "$pat")\n: > "$dir/p001.pnm"\n: > "$dir/p002.pnm"\nexit 0\n'))
    cv_fake = _fake_script(os.path.join(_TEST_ROOT, "_cv2"),
                           '#!/bin/sh\ncase "$1" in *p002.pnm) exit 1;; esac\n: > "$2"\nexit 0\n')
    monkeypatch.setattr(scanner, "SCANIMAGE", scan_fake)
    monkeypatch.setattr(scanner, "CONVERT", cv_fake)
    meta = jobs.load(name)
    meta["params"] = {"source": "adf", "device": "", "dpi": "150",
                      "mode": "Gray", "crop": False, "source_name": ""}   # 无 source：跳过 fail-closed
    jobs.save(name, meta)
    assert scanner.scan_adf(name) == "started"
    for _ in range(100):
        if scanner.get_state(name)["state"] in ("error", "done"):
            break
        time.sleep(0.02)
    st = scanner.get_state(name)
    assert st["state"] == "error", "#3：scanimage 返回 0 但 1 页转换失败，不能报 done——%s" % st["msg"]
    assert "转换失败" in st["msg"]
    d = os.path.join(config.get_scan_root(), name)
    assert os.path.exists(os.path.join(d, "p001.png")), "成功页应保留"
    assert os.path.exists(os.path.join(d, "p002.pnm")), "失败页 PNM 必须保留"


# ---------- #4：reorder 事务化——rename 中途失败恢复原状 ----------
def test_reorder_failure_recovers(monkeypatch):
    _reset()
    name = _mk_job(3)
    client = app_mod.app.test_client()
    real = os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (injected)")
        return real(src, dst)

    monkeypatch.setattr(app_mod.os, "rename", flaky)
    r = client.post("/api/jobs/%s/reorder" % name,
                    json={"order": ["p003.png", "p001.png", "p002.png"]})
    monkeypatch.undo()
    assert r.status_code == 500, "#4：中途失败必须 500"
    assert sorted(jobs.pages(name)) == ["p001.png", "p002.png", "p003.png"], \
        "#4：失败后必须反向恢复原状，不得留半完成状态"
    assert not [f for f in os.listdir(os.path.join(config.get_scan_root(), name))
                if f.startswith("_tmp_")], "#4：不得残留 _tmp_ 文件"


def test_reorder_delete_mode_failure_keeps_pages(monkeypatch):
    _reset()
    name = _mk_job(4)
    client = app_mod.app.test_client()
    real = os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (injected)")
        return real(src, dst)

    monkeypatch.setattr(app_mod.os, "rename", flaky)
    r = client.post("/api/jobs/%s/reorder" % name,
                    json={"order": ["p001.png", "p002.png", "p003.png"], "delete": True})
    monkeypatch.undo()
    assert r.status_code == 500
    assert len(jobs.pages(name)) == 4, \
        "#4：删除模式必须「先重编号成功再删」——重命名失败时待删页一页都不能丢"


# ---------- #5/#8：admin_config 并发写安全 + 0600 ----------
def test_config_concurrent_save():
    _reset()
    errs = []

    def worker(i):
        try:
            for _ in range(15):
                cfg = config.load_admin_cfg()
                cfg["cleanup"]["max_jobs"] = i
                config.save_admin_cfg(cfg)
        except Exception as e:                       # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, "#5：并发保存不得异常：%s" % errs[:3]
    import glob as g
    assert not g.glob(config.ADMIN_CFG_PATH + ".tmp*"), "#5：不得残留临时文件"
    cfg = config.load_admin_cfg()                    # 恢复默认，防污染其他测试
    cfg["cleanup"]["max_jobs"] = 0
    config.save_admin_cfg(cfg)


def test_config_saved_mode_600():
    cfg = config.load_admin_cfg()
    config.save_admin_cfg(cfg)
    assert os.stat(config.ADMIN_CFG_PATH).st_mode & 0o777 == 0o600, \
        "#8：配置含 PIN 哈希，落盘权限必须 0600"


# ---------- #6/#7：ZIP 下载生命周期 ----------
def test_zip_stream_valid_and_lock_released():
    _reset()
    name = _mk_job(3)
    client = app_mod.app.test_client()
    r = client.get("/job/%s/download.zip" % name)
    data = r.get_data()
    z = zf.ZipFile(io.BytesIO(data))
    assert sorted(z.namelist()) == ["p001.png", "p002.png", "p003.png"], "流式 ZIP 内容必须完整"
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False), "#6：流结束后 job_lock 必须释放"
    lk.release()


def test_zip_download_blocks_delete_and_releases_on_close():
    _reset()
    name = _mk_job(3)
    client = app_mod.app.test_client()
    r = client.get("/job/%s/download.zip" % name)
    body = r.response
    next(body, None)                                 # 启动消费（worker 开跑）
    result = {}

    def deleter():
        t0 = time.time()
        rd = client.delete("/api/jobs/" + name)
        result["elapsed"] = time.time() - t0
        result["json"] = rd.get_json()

    th = threading.Thread(target=deleter)
    th.start()
    time.sleep(0.3)
    assert th.is_alive(), "#6：下载流持有 job_lock 期间 DELETE 必须排队而非删目录"
    body.close()                                     # 模拟客户端断开
    th.join(timeout=5)
    assert not th.is_alive()
    assert result["json"].get("ok") is True, "断开后锁释放，DELETE 应完成：%s" % result
    assert not os.path.isdir(os.path.join(config.get_scan_root(), name))


# ---------- #10：页码模型超 999 ----------
def test_page_number_beyond_999():
    _reset()
    name = jobs.create()
    d = os.path.join(config.get_scan_root(), name)
    for n in (998, 999, 1000):
        with open(os.path.join(d, "p%d.png" % n), "wb") as f:
            f.write(b"x")
    assert jobs.pages(name) == ["p998.png", "p999.png", "p1000.png"], \
        "#10：p1000+ 必须被识别且按数字排序"
    assert jobs.next_page_no(name) == 1001, "#10：下一页编号必须看得到 p1000"


def test_page_sort_numeric():
    _reset()
    name = jobs.create()
    d = os.path.join(config.get_scan_root(), name)
    for n in (1, 2, 10):
        with open(os.path.join(d, "p%d.png" % n), "wb") as f:
            f.write(b"x")
    assert jobs.pages(name) == ["p1.png", "p2.png", "p10.png"], \
        "#10：数字序——字符串排序会把 p10 排在 p2 前"


# ---------- #11：锁条目回收 ----------
def test_job_lock_recycled_after_delete():
    _reset()
    name = jobs.create()
    jobs.job_lock(name)                              # 触发条目生成
    assert name in jobs._job_locks
    client = app_mod.app.test_client()
    rd = client.delete("/api/jobs/" + name)
    assert rd.get_json().get("ok") is True
    assert name not in jobs._job_locks, "#11：删除后锁条目应回收，防字典永久增长"


# ---------- #12：任务 ID 碰撞换名重试 ----------
def test_id_collision_retry(monkeypatch):
    _reset()
    import secrets
    seq = iter(["ab12", "ab12", "cd34"])
    monkeypatch.setattr(secrets, "token_hex", lambda n: next(seq))
    a = jobs.create("备注")
    b = jobs.create("备注")                          # 首次后缀碰撞 → 必须换名重试
    assert a != b, "#12：碰撞必须换名，不得复用旧目录"
    assert os.path.isdir(os.path.join(config.get_scan_root(), a))
    assert os.path.isdir(os.path.join(config.get_scan_root(), b))
    assert jobs.load(a)["remark"] == "备注", "#12：旧任务 meta 不得被覆盖"


# ---------- #14：探测缓存失效钩子 ----------
def test_device_probe_invalidate():
    import device_probe
    device_probe._cache["data"] = []
    device_probe._cache["ts"] = time.time()
    device_probe.invalidate()
    assert device_probe._cache["data"] is None and device_probe._cache["ts"] == 0.0, \
        "#14：invalidate 必须使下次 probe 强制重探"


# ---------- #15：转换失败 PNM 移入任务目录存活重启 ----------
def test_flatbed_failed_pnm_moved_into_job(monkeypatch):
    _reset()
    name = jobs.create()
    fake = _fake_script(os.path.join(_TEST_ROOT, "_fast_scan"), "#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(scanner, "SCANIMAGE", fake)
    monkeypatch.setattr(scanner, "CONVERT", "/nonexistent/convert")
    scanner.scan_flatbed(name)
    for _ in range(100):
        if scanner.get_state(name)["state"] == "error":
            break
        time.sleep(0.02)
    d = os.path.join(config.get_scan_root(), name)
    assert os.path.exists(os.path.join(d, "p001.pnm")), \
        "#15：失败 PNM 应移入任务目录（/tmp 会被启动清理删掉）"
    import glob
    assert not glob.glob("/tmp/scanweb_%s_*.pnm" % name), "#15：/tmp 不得残留"
