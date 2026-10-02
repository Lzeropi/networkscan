# tests/test_tmp04.py — v1.15.2 锁分段验证
# 转换不再持 jlock：连续扫描不等转换、转换中 reorder/PDF/ZIP/raw 409、
# 转换中 DELETE 兜底、pending 登记生命周期、0 字节占位页启动清理
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


def _mk_job(letters=("A",)):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    with scanner._pending_lock:
        scanner._pending.clear()
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


class _SlowFx:
    """慢转换注入器：PIL 与 convert 双双挂起 timeout×，制造可控的『转换中』窗口。
    实现：把 _pnm_to_png 换成先置 pending 语义不变的慢函数——直接 monkeypatch
    _pnm_to_png 为 sleep（不真转文件），验证锁分段行为。"""

    def __init__(self, monkeypatch, sleep=1.5):
        self.sleep = sleep

    def __call__(self, src, dst):
        time.sleep(self.sleep)
        # 复制一个合法 PNG 到 dst（保证落盘段 os.replace 成功路径可测）
        import shutil as _sh
        _sh.copyfile(_png_path(os.path.dirname(src)), dst)


# ---------- 核心收益：扫描返回后 jlock 立即可用（不等转换） ----------
def test_scan_returns_before_convert_ends(monkeypatch, tmp_path):
    """扫描段结束（scan_flatbed 返回）时 jlock 必须已释放——转换还在跑也不挡。"""
    name = _mk_job()
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh",
                                     "#!/bin/sh\nsleep 0.3\nexit 0\n"))
    monkeypatch.setattr(scanner, "_pnm_to_png", _SlowFx(monkeypatch, sleep=2.0))

    t0 = time.time()
    r = scanner.scan_flatbed(name)
    dt = time.time() - t0
    assert r == "p002.png", "应返回文件名，实际 %r" % r
    assert dt < 1.5, "扫描段应在 scanimage(0.3s)+开销内返回，实际 %.2fs——jlock 仍被转换持有？" % dt
    # jlock 立即可获取（不等 2.0s 转换）
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False), "扫描返回后 jlock 必须立即可用"
    lk.release()
    assert scanner.scan_lock.acquire(blocking=False), "scan_lock 必须可用"
    scanner.scan_lock.release()
    # pending 已登记
    assert scanner.has_pending(name, "p002.png"), "转换中 pending 必须登记该页"
    # 落盘完成
    deadline = time.time() + 10
    while time.time() < deadline and scanner.get_state(name)["state"] not in ("done", "error"):
        time.sleep(0.1)
    assert not scanner.has_pending(name), "转换完成后 pending 必须清除"
    st = scanner.get_state(name)
    assert st["state"] == "done", "转换应成功落盘，实际 %s" % st
    fp = os.path.join(config.get_scan_root(), name, "p002.png")
    assert os.path.getsize(fp) > 0, "落盘后必须是真实 PNG（非 0 字节）"


# ---------- 转换中 reorder → 409 ----------
def test_reorder_during_convert_409(monkeypatch, tmp_path):
    name = _mk_job(["A"])
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh",
                                     "#!/bin/sh\nsleep 0.2\nexit 0\n"))
    monkeypatch.setattr(scanner, "_pnm_to_png", _SlowFx(monkeypatch, sleep=2.0))
    scanner.scan_flatbed(name)
    assert scanner.has_pending(name), "前置：转换中"

    c = app_mod.app.test_client()
    r = c.post("/api/jobs/" + name + "/reorder",
               json={"order": ["p002.png", "p001.png"], "delete": False})
    assert r.status_code == 409, "转换中 reorder 必须 409（防落盘目标名被打穿），实际 %s" % r.status_code
    # 等转换完成后再排 → 200（current 此时含 p001+p002 两页）
    deadline = time.time() + 10
    while time.time() < deadline and scanner.has_pending(name):
        time.sleep(0.1)
    r2 = c.post("/api/jobs/" + name + "/reorder",
                json={"order": ["p002.png", "p001.png"], "delete": False})
    assert r2.get_json().get("ok"), "转换完成后 reorder 应成功：%s" % r2.get_json()


# ---------- 转换中 DELETE → 200 + worker 兜底 ----------
def test_delete_during_convert_cleans_up(monkeypatch, tmp_path):
    name = _mk_job()
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh",
                                     "#!/bin/sh\nsleep 0.2\nexit 0\n"))
    monkeypatch.setattr(scanner, "_pnm_to_png", _SlowFx(monkeypatch, sleep=2.0))
    scanner.scan_flatbed(name)
    assert scanner.has_pending(name)

    c = app_mod.app.test_client()
    r = c.delete("/api/jobs/" + name)
    assert r.get_json().get("ok") is True, "转换中 DELETE 必须成功"
    assert not os.path.isdir(os.path.join(config.get_scan_root(), name)), "目录必须移除"
    # worker 兜底：等它跑完落盘段（isdir False → 清理），不得崩溃/不得重建目录
    deadline = time.time() + 10
    while time.time() < deadline and scanner.has_pending(name):
        time.sleep(0.1)
    assert not scanner.has_pending(name), "worker 兜底后 pending 必须清除"
    assert not os.path.isdir(os.path.join(config.get_scan_root(), name)), "不得重建已删目录"


# ---------- 转换中 PDF/ZIP/raw → 409 ----------
def test_pdf_zip_raw_409_during_convert(monkeypatch, tmp_path):
    name = _mk_job(["A"])
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh",
                                     "#!/bin/sh\nsleep 0.2\nexit 0\n"))
    monkeypatch.setattr(scanner, "_pnm_to_png", _SlowFx(monkeypatch, sleep=2.0))
    scanner.scan_flatbed(name)
    assert scanner.has_pending(name)

    c = app_mod.app.test_client()
    rp = c.get("/job/" + name + "/download.pdf")
    assert rp.status_code == 409, "转换中 PDF 必须 409，实际 %s" % rp.status_code
    rz = c.get("/job/" + name + "/download.zip")
    assert rz.status_code == 409, "转换中 ZIP 必须 409，实际 %s" % rz.status_code
    rr = c.get("/job/" + name + "/raw/p002.png")
    assert rr.status_code == 409, "转换中该页 raw 必须 409，实际 %s" % rr.status_code
    # 其他已完成页 raw 不受影响
    rok = c.get("/job/" + name + "/raw/p001.png")
    assert rok.status_code == 200 and rok.data == b"A", "已完成页 raw 必须正常"


# ---------- 连续扫描不等转换（核心收益） ----------
def test_back_to_back_scans_no_convert_wait(monkeypatch, tmp_path):
    """第二次扫描的等待 = 第一次扫描段（0.3s），不含转换（2.0s）。"""
    name = _mk_job()
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh",
                                     "#!/bin/sh\nsleep 0.3\nexit 0\n"))
    monkeypatch.setattr(scanner, "_pnm_to_png", _SlowFx(monkeypatch, sleep=2.0))

    scanner.scan_flatbed(name)          # 第一次：返回时转换已开始（2s）
    t0 = time.time()
    r2 = scanner.scan_flatbed(name)      # 第二次：不得等第一次转换
    dt = time.time() - t0
    assert r2 == "p003.png", r2
    assert dt < 1.5, "第二次扫描不得等第一次转换（2s），实际 %.2fs" % dt
    # 两次转换都完成后 pending 清空
    deadline = time.time() + 10
    while time.time() < deadline and scanner.has_pending(name):
        time.sleep(0.1)
    assert not scanner.has_pending(name)
    st = scanner.get_state(name)
    assert st["state"] == "done", st


# ---------- thumb 转换中 long-poll：一次请求等转换完成，无 404 轮询 ----------
def test_thumb_longpoll_waits_for_convert(monkeypatch, tmp_path):
    name = _mk_job()
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh",
                                     "#!/bin/sh\nsleep 0.2\nexit 0\n"))
    monkeypatch.setattr(scanner, "_pnm_to_png", _SlowFx(monkeypatch, sleep=1.0))
    scanner.scan_flatbed(name)          # 转换中（1s）
    assert scanner.has_pending(name, "p002.png")

    c = app_mod.app.test_client()
    t0 = time.time()
    r = c.get("/job/" + name + "/thumb/p002.jpg")
    dt = time.time() - t0
    assert r.status_code == 200, "转换中请求 thumb 必须挂起等到完成返回 200，实际 %s" % r.status_code
    assert dt >= 0.8, "应等待转换完成（≥0.8s），实际 %.2fs——未挂起？" % dt
    assert len(r.data) > 0, "thumb 必须有效"


# ---------- 0 字节占位页启动清理 ----------
def test_cleanup_empty_pages(tmp_path, monkeypatch):
    name = _mk_job(["A"])
    d = os.path.join(config.get_scan_root(), name)
    # 造崩溃残留：0 字节占位页 + 正常页
    open(os.path.join(d, "p002.png"), "wb").close()
    with open(os.path.join(d, "p003.png"), "wb") as f:
        f.write(b"REAL")
    scanner.cleanup_empty_pages()
    files = sorted(os.listdir(d))
    assert "p002.png" not in files, "0 字节占位页必须被清理"
    assert "p001.png" in files and "p003.png" in files, "真实页不得误删"
    assert open(os.path.join(d, "p003.png"), "rb").read() == b"REAL"
