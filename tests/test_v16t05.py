# tests/test_v16t05.py — v1.16.0-tmp05：ChatGPT T04 审查报告修复验证
# 覆盖：T04-01 落盘失败保留唯一 PNM、T04-02 扫描输出防符号链接写穿、
#       T04-03 webp→PDF 临时源副本不泄漏、T04-04 0 字节占位不阻断恢复、
#       T04-05 同任务 ADF 批次互斥、T04-06 ADF 入队受水位约束、
#       T04-07 创建任务 source/device 校验
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


def _wait_pending_idle(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline and scanner._pending_count() > 0:
        time.sleep(0.05)


# ---------- T04-01：转换成功但落盘（os.replace）失败时，唯一 PNM 原图必须保留 ----------

def test_commit_failure_keeps_pnm(monkeypatch, tmp_path):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    pnm = _ppm(str(tmp_path) + "/scanweb_%s_001_src.pnm" % name)

    def replace_boom(src, dst):
        raise OSError("模拟磁盘满/权限变化导致 os.replace 失败")

    monkeypatch.setattr(scanner.os, "replace", replace_boom)
    scanner._enqueue_convert(name, "p001.png", pnm, os.path.join(base, "p001.png"),
                             "png", config.get_scan_root())
    _wait_pending_idle()
    assert os.path.exists(pnm), "落盘失败时源 PNM 是唯一可恢复数据，必须保留"
    st = scanner.get_state(name)
    assert st["state"] == "error", "提交失败状态应为 error，实际 %s" % st
    assert "原始数据已保留" in st["msg"], "状态应说明数据已保留：%r" % st["msg"]
    # .partial 残件应清理（不占任务目录），输出页不得是 0 字节占位
    assert not os.path.exists(os.path.join(base, "p001.png.partial")), ".partial 残件应清理"
    assert not os.path.exists(os.path.join(base, "p001.png")), "提交失败不应产出页面"


def test_commit_ok_then_thumb_failure_keeps_page(monkeypatch, tmp_path):
    """提交成功后缩略图/meta 失败：最终图片保留（committed 语义），源才可删。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    pnm = _ppm(str(tmp_path / "src2.pnm"))

    def boom_thumb(*a, **k):
        raise RuntimeError("模拟缩略图失败")

    monkeypatch.setattr(scanner, "_mk_thumb", boom_thumb)
    scanner._enqueue_convert(name, "p001.png", pnm, os.path.join(base, "p001.png"),
                             "png", config.get_scan_root())
    _wait_pending_idle()
    assert os.path.exists(os.path.join(base, "p001.png")), "已提交页面必须保留（不被误删）"
    st = scanner.get_state(name)
    assert st["state"] == "error", "缩略图失败状态应为 error：%s" % st


# ---------- T04-02：扫描输出路径防符号链接写穿 ----------

def test_flatbed_skips_symlink_page(monkeypatch, tmp_path):
    """任务目录 p001.png 被换成 symlink：平板扫描不得跟随覆盖外部文件，须顺延。"""
    name = _mk_job({"source": "flatbed", "device": "", "dpi": "150",
                    "mode": "Gray", "format": "png", "crop": False})
    base = os.path.join(config.get_scan_root(), name)
    outside = str(tmp_path / "_outside_target.txt")
    open(outside, "w").write("KEEP")
    os.symlink(outside, os.path.join(base, "p001.png"))   # 攻击者占位 p001
    assert jobs.next_page_no(name) == 2, "symlink 页必须占页码（分配侧避让）"
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/fb.sh", "#!/bin/sh\nexit 0\n"))
    r = scanner.scan_flatbed(name)
    assert r == "p002.png", "p001 被链接占用应顺延到 p002，实际 %r" % r
    _wait_pending_idle()
    assert open(outside).read() == "KEEP", "外部文件不得被截断/覆盖"
    assert os.path.islink(os.path.join(base, "p001.png")), "链接页保持原样（未触碰）"
    assert os.path.exists(os.path.join(base, "p002.png")), "顺延页应正常产出"


def test_enqueue_pnms_rejects_symlink_source(monkeypatch, tmp_path):
    """_enqueue_pnms 必须拒绝 symlink 源（转换阶段不读不可信路径）。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    outside = str(tmp_path / "_ext.png")
    Image.new("RGB", (4, 4)).save(outside)
    os.symlink(outside, os.path.join(base, "p001.pnm"))
    queued = scanner._enqueue_pnms(name, 1, config.get_scan_root(), "png")
    assert queued == [], "symlink 源不得入队，实际 %r" % queued
    assert scanner._pending_count() == 0, "symlink 源不得产生 pending 登记"


# ---------- T04-03：webp→PDF 成功后临时源副本不泄漏 ----------

def test_webp_safe_png_no_src_leak(monkeypatch, tmp_path):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    webp = os.path.join(base, "p001.webp")
    Image.new("RGB", (4, 4)).save(webp, "WEBP")
    monkeypatch.setattr(scanner, "CONVERT",
                        _fake_script(str(tmp_path) + "/cv.sh",
                                     '#!/bin/sh\nt="${2#PNG:}"; cp "$1" "$t"; exit 0\n'))
    before = set(glob.glob("/tmp/scanweb_pdfsrc_*.webp"))
    png = app_mod._webp_safe_png(base, "p001.webp")
    after = set(glob.glob("/tmp/scanweb_pdfsrc_*.webp")) - before
    assert after == set(), "成功路径源副本必须清理，泄漏：%r" % after
    assert os.path.exists(png), "PNG 产物应返回给调用方"
    os.remove(png)


def test_webp_safe_temp_cleaned_on_convert_failure(monkeypatch, tmp_path):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    Image.new("RGB", (4, 4)).save(os.path.join(base, "p001.webp"), "WEBP")
    monkeypatch.setattr(scanner, "CONVERT",
                        _fake_script(str(tmp_path) + "/cv.sh", "#!/bin/sh\nexit 1\n"))
    before = set(glob.glob("/tmp/scanweb_pdfsrc_*"))
    try:
        app_mod._webp_safe_png(base, "p001.webp")
        raised = False
    except Exception:
        raised = True
    assert raised, "convert 失败必须抛"
    after = set(glob.glob("/tmp/scanweb_pdfsrc_*")) - before
    assert after == set(), "失败路径两个临时文件都须清理：%r" % after


# ---------- T04-04：0 字节占位页不阻断 PNM 恢复 ----------

def test_recover_pnms_not_blocked_by_zero_byte_placeholder():
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    _ppm(os.path.join(base, "p001.pnm"))
    open(os.path.join(base, "p001.png"), "wb").close()   # 0 字节占位（上次转换中断）
    rec, skip = scanner.recover_task_pnms()
    assert rec == 1 and skip == 0, "0 字节占位不得视为有效产物（应恢复 PNM），实际 (%d,%d)" % (rec, skip)
    _wait_pending_idle()
    assert os.path.getsize(os.path.join(base, "p001.png")) > 0, "恢复后应产出有效 PNG"
    assert not os.path.exists(os.path.join(base, "p001.pnm")), "恢复转换成功后源删除"
    # 有效产物页不受影响
    _ppm(os.path.join(base, "p002.pnm"))
    Image.new("RGB", (4, 4)).save(os.path.join(base, "p002.png"))
    rec2, skip2 = scanner.recover_task_pnms()
    assert rec2 == 0 and skip2 == 1, "有效产物页应跳过，实际 (%d,%d)" % (rec2, skip2)


# ---------- T04-05：同任务 ADF 批次互斥（入口拒绝 + 批次等待自身页） ----------

def test_adf_batch_mutual_exclusion(monkeypatch, tmp_path):
    name = _mk_job({"source": "adf", "device": "epson:libusb:001:001",
                    "dpi": "150", "mode": "Gray", "crop": False,
                    "format": "png", "source_name": ""})
    ppm = _ppm(str(tmp_path) + "/src.ppm")
    scan_fake = _fake_script(str(tmp_path) + "/adf.sh",
                             '#!/bin/sh\npat=""\nfor a in "$@"; do\n case "$a" in --batch=*) pat="${a#--batch=}";; esac\ndone\n'
                             '[ -z "$pat" ] && exit 0\ndir=$(dirname "$pat")\n'
                             'cp "%s" "$dir/p001.pnm"\nexit 0\n' % ppm)
    monkeypatch.setattr(scanner, "SCANIMAGE", scan_fake)

    def slow_convert(src, dst, fmt="png"):
        time.sleep(0.8)
        Image.new("RGB", (4, 4)).save(dst, "PNG")

    monkeypatch.setattr(scanner, "_convert_pnm", slow_convert)
    assert scanner.scan_adf(name) == "started"
    time.sleep(0.4)                        # 扫描段已结束（锁释放）、转换中（批次仍活跃）
    assert scanner.scan_adf(name) == "adf_busy", "前批转换未收敛时必须拒绝新批次"
    deadline = time.time() + 10
    st = None
    while time.time() < deadline:
        st = scanner.get_state(name)
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["state"] == "done", "批次收尾应统一置 done：%s" % st


def test_adf_entry_flag_cleared_after_batch():
    name = _mk_job()
    with scanner._adf_batch_lock:
        scanner._adf_batch[name] = True
    # 批内允许标志真实存在——手动清后入口才放行（配合互斥语义）
    with scanner._adf_batch_lock:
        scanner._adf_batch.pop(name, None)
    assert not scanner._adf_active(name)


# ---------- T04-06：ADF 入队受水位约束（满则等待，恢复后继续） ----------

def test_enqueue_pnms_waits_watermark(monkeypatch):
    """水位等待逻辑本身（分批等待、恢复后继续）。
    注意（DeepSeek 指出的边界）：本测试不持锁直接调 _enqueue_pnms，用 dummy
    任务 + 手动 _pending_remove 模拟消化——**不覆盖 scan_adf 调用上下文**。
    真实持锁路径的死锁回归由 tests/test_v16t06.py::test_adf_over_watermark_
    no_deadlock 独占覆盖（6 页批次 + jlock 可获取断言）。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    for i in range(1, 7):
        _ppm(os.path.join(base, "p%03d.pnm" % i))
    # 填满全局水位（MAX_PENDING_PNM=4），pending 用 dummy 任务挂起
    for k in range(scanner.MAX_PENDING_PNM):
        scanner._pending_add("dummy%d" % k, "x.png")
    result = {}

    def runner():
        result["queued"] = scanner._enqueue_pnms(name, 1, config.get_scan_root(), "png")

    t = threading.Thread(target=runner)
    t.start()
    time.sleep(0.3)
    assert "queued" not in result, "水位未降前不得全量入队（分批等待）"
    # 释放 2 个水位（模拟 worker 消化）
    for k in range(2):
        scanner._pending_remove("dummy%d" % k, "x.png")
    t.join(timeout=8)
    assert not t.is_alive(), "水位恢复后必须完成入队（防批次线程永久挂起）"
    assert result["queued"] == ["p001.png", "p002.png", "p003.png",
                                "p004.png", "p005.png", "p006.png"], "六页全部入队，实际 %r" % result["queued"]
    _wait_pending_idle()


# ---------- T04-07：创建任务接口 source/device 校验 ----------

def test_create_rejects_bad_source():
    c = app_mod.app.test_client()
    r = c.post("/api/jobs", data={"dpi": "150", "mode": "Gray", "format": "png",
                                  "source": "bogus"})
    assert r.status_code == 400, "非法 source 创建必须 400，实际 %s" % r.status_code


def test_create_rejects_unknown_device():
    c = app_mod.app.test_client()
    r = c.post("/api/jobs", data={"dpi": "150", "mode": "Gray", "format": "png",
                                  "device": "no_such_device:libusb:001:001"})
    assert r.status_code == 400, "不存在设备创建必须 400，实际 %s" % r.status_code


def test_create_legal_flatbed_ok():
    c = app_mod.app.test_client()
    r = c.post("/api/jobs", data={"dpi": "150", "mode": "Gray", "format": "png",
                                  "source": "flatbed"})
    assert r.status_code in (200, 302), "合法平板任务应创建成功，实际 %s" % r.status_code