# tests/test_v1148.py — v1.14.8 自审八修验证（A raw_pages 过滤 / B errno 区分 / C dirty 标志 /
# D 断言加强已在 test_v1147 内 / E 配置缓存 / F cleanup 节流 / G 改路径防呆 / J 死配置清理）
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402,F401
import config          # noqa: E402,F401
import device_probe    # noqa: E402,F401
import jobs            # noqa: E402,F401
import scanner         # noqa: E402,F401


def _mk_job(letters):
    import shutil
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    name = jobs.create("t")
    d = os.path.join(config.get_scan_root(), name)
    for i, c in enumerate(letters, 1):
        with open(os.path.join(d, "p%03d.png" % i), "wb") as f:
            f.write(c.encode())
    m = jobs.load(name)
    m["pages"] = len(letters)
    jobs.save(name, m)
    return name


# ---------- A：raw_pages 过滤 symlink（幽灵页不再使 next_page_no 跳号） ----------
def test_raw_pages_filters_symlink():
    name = _mk_job(["A", "B"])
    d = os.path.join(config.get_scan_root(), name)
    before = jobs.next_page_no(name)
    assert before == 3, "预期下一页 p003，实际 %s" % before
    os.symlink("/etc/passwd", os.path.join(d, "p999.png"))
    assert "p999.png" not in jobs.raw_pages(name), "A：raw_pages 必须过滤 symlink 页"
    assert "p999.png" not in jobs.pages(name), "pages 保持过滤"
    assert jobs.next_page_no(name) == 3, "A：幽灵页不得使编号跳到 1000"
    # 0 字节转换中占位仍保留（防编号冲突是 raw_pages 存在意义）
    open(os.path.join(d, "p005.png"), "wb").close()
    assert "p005.png" in jobs.raw_pages(name), "0 字节占位仍计入编号防冲突"


# ---------- B：open_page_fd 区分 errno（ELOOP 与 ENOENT 报错不同） ----------
def test_open_page_fd_errno_split():
    name = _mk_job(["A"])
    base = jobs.path(name)
    os.symlink("/etc/passwd", os.path.join(base, "p_link.png"))
    os.remove(os.path.join(base, "p001.png"))   # 制造 ENOENT
    try:
        jobs.open_page_fd(base, "p_link.png")
        assert False, "symlink 必须拒"
    except jobs.JobError as e:
        assert "符号链接" in str(e), "B：ELOOP 应报符号链接，实际 %s" % e
    try:
        jobs.open_page_fd(base, "p001.png")
        assert False, "不存在文件必须拒"
    except jobs.JobError as e:
        assert "符号链接" not in str(e), "B：ENOENT 不得误报符号链接，实际 %s" % e
        assert "读取失败" in str(e) or "I/O" in str(e), "B：应报 I/O 错误，实际 %s" % e


# ---------- C：invalidate 改 dirty 标志（不清缓存，probe 锁内消化） ----------
def test_invalidate_dirty_flag(monkeypatch):
    device_probe._cache.update(ts=time.time() - 1, data=[{"name": "old"}], dirty=False)
    device_probe.invalidate()
    assert device_probe._cache["data"] == [{"name": "old"}], "C：invalidate 不再直接清缓存"
    assert device_probe._cache["dirty"] is True, "C：置 dirty 标志"
    calls = []
    real = device_probe.subprocess.run
    monkeypatch.setattr(device_probe.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"stdout": "", "stderr": ""})())
    monkeypatch.setattr(device_probe.os.path, "exists", lambda p: True)
    devs, cached = device_probe.probe()
    assert devs == [], "C：dirty 使缓存强制重探（无设备环境返回空列表而非旧数据）"
    assert cached is False and calls, "C：dirty 必须触发真实探测"
    monkeypatch.setattr(device_probe.subprocess, "run", real)
    assert device_probe._cache["dirty"] is False, "C：probe 锁内取走并清零 dirty"


# ---------- E：admin 配置 mtime 缓存（副本独立 + mtime 变化自动重读） ----------
def test_admin_cfg_cache_independent_copies():
    a = config.load_admin_cfg()
    a["pin_hash"] = "mutated"
    a["cleanup"]["max_jobs"] = 99
    b = config.load_admin_cfg()
    assert b["pin_hash"] != "mutated", "E：命中缓存必须返回独立副本，mutator 不得污染"
    assert b["cleanup"]["max_jobs"] != 99, "E：嵌套 dict 也须深拷贝"


def test_admin_cfg_cache_mtime_invalidate():
    p = config.ADMIN_CFG_PATH
    open(p, "w").write('{"pin_hash": "x1"}')
    os.utime(p, (100, 100))                  # 固定旧 mtime
    assert config.load_admin_cfg()["pin_hash"] == "x1"
    time.sleep(0.01)
    open(p, "w").write('{"pin_hash": "x2"}')   # mtime 变
    assert config.load_admin_cfg()["pin_hash"] == "x2", "E：mtime 变化必须自动重读"
    os.remove(p)


# ---------- F：cleanup 60s 节流（force=True 立即执行） ----------
def test_cleanup_throttle():
    name = _mk_job(["A"])
    jobs._cleanup_last["ts"] = 0.0
    cfg = config.get_cleanup_cfg()
    cfg["max_jobs"] = 0                        # 未超限
    assert jobs.cleanup(force=True) == [], "force 立即执行"
    recent = time.time() - 5                   # 5 秒前执行过
    jobs._cleanup_last["ts"] = recent
    assert jobs.cleanup() == [], "节流窗口内直接返回空"
    assert jobs._cleanup_last["ts"] == recent, "F：节流命中不得刷新时间戳"
    jobs._cleanup_last["ts"] = 0.0


# ---------- G：有任务在扫时改存储路径 → 409 ----------
# ---------- G：有任务在扫时改存储路径 → 409 ----------
def test_save_config_root_blocked_while_scanning():
    _mk_job(["A"])
    import uuid
    new_root = "/tmp/v1148_newroot_%s" % uuid.uuid4().hex[:8]   # 唯一化——固定名会被上次运行残留目录污染（isdir=True 直接走保存分支）
    scanner.state.clear()
    scanner.state["20200101-000000_deadbeef"] = {"state": "scanning", "msg": "x"}
    c = app_mod.app.test_client()
    r0 = c.post("/api/admin/login", json={"pin": "1234", "confirm": "1234"})
    csrf = r0.get_json().get("csrf", "")
    r = c.post("/api/admin/config", json={"scan_root": "/tmp"},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409, "G：扫描中改路径应 409，实际 %s" % r.status_code
    scanner.state.clear()
    r2 = c.post("/api/admin/config", json={"scan_root": new_root},
                headers={"X-CSRF-Token": csrf})
    assert r2.get_json().get("need_confirm") is True, "G：不存在路径应先确认"
    r3 = c.post("/api/admin/config", json={"scan_root": new_root, "confirm": True},
                headers={"X-CSRF-Token": csrf})
    assert r3.get_json().get("ok") is True, "G：确认后无扫描时改路径应成功"
    assert config.get_scan_root() == new_root
    import shutil
    shutil.rmtree(new_root, ignore_errors=True)   # 清理：防下轮运行 isdir 命中残留


# ---------- J：死配置已清理 ----------
def test_dead_config_removed():
    assert not hasattr(config, "MAX_TOTAL_BYTES"), "J：MAX_TOTAL_BYTES 死配置应删除"
    assert not hasattr(config, "MAX_AGE_DAYS"), "J：MAX_AGE_DAYS 死配置应删除"
    assert hasattr(config, "MAX_PDF_PAGES"), "在用配置不得误删"
