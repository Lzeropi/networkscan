# tests/test_v16t02.py — v1.16.0-tmp02：转换队列化
# 覆盖：常驻 worker FIFO 串行、水位闸门 convert_busy → API 409、
#       单页转换失败不阻塞后续页、重启恢复归档（无主才删）、
#       scan_status convert_queue_len、test 隔离复位
import os
import shutil
import sys
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


def _png_bytes(size=8):
    import io
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (10, 120, 210)).save(buf, "PNG")
    return buf.getvalue()


def _mk_pnm(path):
    """真实 PPM 源文件（走 PIL 真实解码路径）。"""
    Image.new("RGB", (8, 8), (10, 120, 210)).save(path, "PPM")
    return path


def _wait_idle(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline and scanner._pending_count() > 0:
        time.sleep(0.05)


# ---------- 1) FIFO 串行：worker 按入队顺序逐页转换 ----------

def test_convert_queue_fifo_serial(monkeypatch):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    order = []

    def fake_convert(src, dst, fmt="png"):
        order.append(os.path.basename(src))
        time.sleep(0.05)                       # 制造可观测的处理窗口
        with open(dst, "wb") as f:
            f.write(_png_bytes())

    monkeypatch.setattr(scanner, "_convert_pnm", fake_convert)
    for i in (1, 2, 3):
        pnm = _mk_pnm(os.path.join(base, "in%d.pnm" % i))
        scanner._enqueue_convert(name, "p%03d.png" % i, pnm,
                                 os.path.join(base, "p%03d.png" % i), "png", config.get_scan_root())
    _wait_idle()
    assert order == ["in1.pnm", "in2.pnm", "in3.pnm"], "常驻 worker 必须 FIFO 串行，实际 %s" % order
    assert scanner._pending_count() == 0, "全部转换完成后水位必须归零"


# ---------- 2) 水位闸门：满则 scan_flatbed 返回 convert_busy ----------

def test_convert_busy_when_queue_full():
    name = _mk_job()
    for i in range(scanner.MAX_PENDING_PNM):
        scanner._pending_add(name, "p%03d.png" % i)
    r = scanner.scan_flatbed(name)             # 检查在 scanimage 前 → 无需 fake scanimage
    assert r == "convert_busy", "水位满必须拒扫防无限堆积，实际 %r" % r
    # 双锁必须已释放（拒扫路径不得泄漏锁）
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False), "convert_busy 后 jlock 必须可用"
    lk.release()
    assert scanner.scan_lock.acquire(blocking=False), "convert_busy 后 scan_lock 必须可用"
    scanner.scan_lock.release()


# ---------- 3) convert_busy → API 409（与设备忙 409 区分文案） ----------

def test_convert_busy_api_409():
    name = _mk_job()
    for i in range(scanner.MAX_PENDING_PNM):
        scanner._pending_add(name, "p%03d.png" % i)
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/" + name + "/scan")
    assert r.status_code == 409, "水位满 API 必须 409，实际 %s" % r.status_code
    msg = (r.get_json() or {}).get("msg", "")
    assert "队列" in msg, "文案应说明转换队列已满，实际 %r" % msg


# ---------- 4) 单页失败不阻塞后续页 ----------

def test_convert_failure_does_not_block_queue(monkeypatch):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    seen = []

    def fake_convert(src, dst, fmt="png"):
        seen.append(os.path.basename(src))
        if "bad" in src:
            raise RuntimeError("模拟转换失败")
        with open(dst, "wb") as f:
            f.write(_png_bytes())

    monkeypatch.setattr(scanner, "_convert_pnm", fake_convert)
    scanner._enqueue_convert(name, "p001.png", _mk_pnm(os.path.join(base, "bad1.pnm")),
                             os.path.join(base, "p001.png"), "png", config.get_scan_root())
    scanner._enqueue_convert(name, "p002.png", _mk_pnm(os.path.join(base, "good2.pnm")),
                             os.path.join(base, "p002.png"), "png", config.get_scan_root())
    _wait_idle()
    assert seen == ["bad1.pnm", "good2.pnm"], "失败页不得卡死队列，后续页仍须处理，实际 %s" % seen
    assert scanner._pending_count() == 0, "失败页也必须 from finally 清 pending"
    assert os.path.getsize(os.path.join(base, "p002.png")) > 0, "后续页应正常落盘"
    # 失败页 PNM 归档保留（数据不丢）
    assert os.path.exists(os.path.join(base, "p001.pnm")), "失败页 PNM 必须归档进任务目录"


# ---------- 5) 重启恢复：/tmp 残留 PNM 按归属归档 ----------

def test_cleanup_tmp_pnms_archives_owned():
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    # 模拟崩溃遗留：/tmp/scanweb_{job}_{n}_{rand}.pnm
    tmpf = "/tmp/scanweb_%s_007_abcd1234.pnm" % name
    with open(tmpf, "wb") as f:
        f.write(b"P6\n1 1\n255\n\x00\x00\x00")
    recovered, removed = scanner.cleanup_tmp_pnms()
    assert recovered == 1 and removed == 0, "有主 PNM 应归档，实际 (%d,%d)" % (recovered, removed)
    assert os.path.exists(os.path.join(base, "p007.pnm")), "应归档为任务目录 pNNN.pnm"
    assert not os.path.exists(tmpf), "/tmp 源文件应被移走"


# ---------- 6) 重启恢复：无主 PNM 才删 ----------

def test_cleanup_tmp_pnms_removes_orphan():
    _mk_job()
    tmpf = "/tmp/scanweb_ghost-no-such-job-xyz_001_deadbeef.pnm"
    with open(tmpf, "wb") as f:
        f.write(b"P6\n1 1\n255\n\x00\x00\x00")
    recovered, removed = scanner.cleanup_tmp_pnms()
    assert removed >= 1, "无主 PNM 必须删除"
    assert not os.path.exists(tmpf)


# ---------- 7) scan_status 暴露转换队列水位 ----------

def test_scan_status_convert_queue_len(monkeypatch):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)

    def slow_convert(src, dst, fmt="png"):
        time.sleep(0.4)
        with open(dst, "wb") as f:
            f.write(_png_bytes())

    monkeypatch.setattr(scanner, "_convert_pnm", slow_convert)
    scanner._enqueue_convert(name, "p001.png", _mk_pnm(os.path.join(base, "a.pnm")),
                             os.path.join(base, "p001.png"), "png", config.get_scan_root())
    scanner._enqueue_convert(name, "p002.png", _mk_pnm(os.path.join(base, "b.pnm")),
                             os.path.join(base, "p002.png"), "png", config.get_scan_root())
    st = scanner.get_scan_status()
    assert st["convert_queue_len"] >= 1, "队列水位应 >0，实际 %s" % st
    _wait_idle()
    assert scanner.get_scan_status()["convert_queue_len"] == 0, "转换完应归零"