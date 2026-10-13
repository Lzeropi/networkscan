# tests/test_v16t04.py — v1.16.0-tmp04：ChatGPT 审查报告修复验证
# 覆盖：P1-1 归档失败保 /tmp 源、P1-2 PNM 占页码、P1-3 webp→PDF 安全副本、
#       P1-4 ADF 非零退出先入队部分页、P2-5 cleanup 查 pending、P2-7 raw MIME、
#       P2-8 JSON/crop 严格校验、P2-9 任务目录 PNM 启动恢复（幂等）、
#       额外项：入队异常后锁不重复释放
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


def _wait_state(name, want=("done", "error"), timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = scanner.get_state(name)
        if st["state"] in want:
            return st
        time.sleep(0.05)
    return scanner.get_state(name)


# ---------- P1-1：归档失败时 /tmp 源绝不删（原图是唯一数据） ----------

def test_archive_failure_keeps_tmp_source(monkeypatch, tmp_path):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    pnm = _ppm(str(tmp_path) + "/scanweb_%s_001_testab.pnm" % name)

    def boom(src, dst, fmt="png"):
        raise RuntimeError("模拟转换失败")

    def move_boom(src, dst):
        raise OSError("模拟归档失败（如任务目录磁盘满）")

    monkeypatch.setattr(scanner, "_convert_pnm", boom)
    monkeypatch.setattr(scanner.shutil, "move", move_boom)
    scanner._enqueue_convert(name, "p001.png", pnm, os.path.join(base, "p001.png"),
                             "png", config.get_scan_root())
    _wait_pending_idle()
    assert os.path.exists(pnm), "归档失败时 /tmp 源是唯一可恢复数据，必须保留"
    st = scanner.get_state(name)
    assert st["state"] == "error" and pnm in st["msg"], "状态应指向实际保留路径：%s" % st


# ---------- P1-2：任务目录 PNM 占页码（防 ADF 转换期再扫描覆盖） ----------

def test_next_page_no_counts_pnm():
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    with open(os.path.join(base, "p001.png"), "wb") as f:
        f.write(b"x")
    _ppm(os.path.join(base, "p003.pnm"))
    assert jobs.next_page_no(name) == 4, "p003.pnm 必须占页码，下一页应为 4（防覆盖）"


def test_flatbed_after_adf_pnm_no_conflict(monkeypatch, tmp_path):
    """ADF 已产 p001.pnm（转换中），同任务再平板扫描必须分到 p002。"""
    name = _mk_job({"source": "flatbed", "device": "", "dpi": "150",
                    "mode": "Gray", "format": "png", "crop": False})
    base = os.path.join(config.get_scan_root(), name)
    _ppm(os.path.join(base, "p001.pnm"))
    scanner._pending_add(name, "p001.png")       # 模拟该页转换中

    def slow_convert(src, dst, fmt="png"):
        time.sleep(0.8)
        Image.new("RGB", (4, 4)).save(dst, "PNG")

    monkeypatch.setattr(scanner, "_convert_pnm", slow_convert)
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/fb.sh", "#!/bin/sh\nexit 0\n"))
    r = scanner.scan_flatbed(name)
    assert r == "p002.png", "转换中的 p001 之后必须分配 p002，实际 %r（页码覆盖=丢页）" % r


# ---------- P1-3：webp→PDF 走安全句柄副本（symlink 拒绝） ----------

def test_pdf_webp_symlink_rejected(monkeypatch):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    outside = os.path.join(os.path.dirname(base), "_outside.webp")
    Image.new("RGB", (4, 4)).save(outside, "WEBP")
    os.symlink(outside, os.path.join(base, "p001.webp"))
    m = jobs.load(name)
    m["pages"] = 1
    jobs.save(name, m)
    c = app_mod.app.test_client()
    r = c.get("/job/" + name + "/download.pdf")
    assert r.status_code in (403, 404), "symlink 页必须被安全检查拒绝，不得 500/合成，实际 %s" % r.status_code
    assert r.status_code != 200


# ---------- P1-4：ADF 非零退出也入队已生成页面 ----------

def test_adf_nonzero_exit_enqueues_partial_pages(monkeypatch, tmp_path):
    name = _mk_job({"source": "adf", "device": "epson:libusb:001:001",
                    "dpi": "150", "mode": "Gray", "crop": False,
                    "format": "png", "source_name": ""})
    ppm = _ppm(str(tmp_path) + "/src.ppm")
    scan_fake = _fake_script(str(tmp_path) + "/adf.sh",
                             '#!/bin/sh\npat=""\nfor a in "$@"; do\n case "$a" in --batch=*) pat="${a#--batch=}";; esac\ndone\n'
                             '[ -z "$pat" ] && exit 0\ndir=$(dirname "$pat")\n'
                             'cp "%s" "$dir/p001.pnm"\ncp "%s" "$dir/p002.pnm"\nexit 3\n' % (ppm, ppm))
    monkeypatch.setattr(scanner, "SCANIMAGE", scan_fake)
    assert scanner.scan_adf(name) == "started"
    st = _wait_state(name)
    assert st["state"] == "error", "非零返回码不得报 done：%s" % st
    assert "scanimage 异常退出" in st["msg"], "应如实报扫描异常，实际 %r" % st["msg"]
    base = os.path.join(config.get_scan_root(), name)
    assert os.path.exists(os.path.join(base, "p001.png")), "已扫页面必须入队转换（不转=前端丢页）"
    assert os.path.exists(os.path.join(base, "p002.png")), "部分结果两页都要转换保留"
    assert not os.path.exists(os.path.join(base, "p001.pnm")), "转换成功后源应删除"


# ---------- P2-5：转换中任务不被自动清理 ----------

def test_cleanup_skips_pending_job():
    import config as _cfg
    name = _mk_job()                      # 任务 A（超限应删的那个）
    time.sleep(1.05)                      # 任务名秒级时间戳——跨秒确保排序稳定（A 最旧）
    other = jobs.create("keep")            # 任务 B（持 pending，应保护）
    cfg = _cfg.load_admin_cfg()
    cfg["cleanup"]["max_jobs"] = 1        # 超限触发清理
    _cfg.save_admin_cfg(cfg)
    scanner._pending_add(other, "p001.png")   # 该页转换中（state 可能已 done）
    scanner.state[other] = {"state": "done", "msg": "扫描完成"}   # 模拟同批前一页已置 done
    deleted = jobs.cleanup(force=True)
    assert other not in deleted, "仍有页在转换的任务不得被清理（丢转换中页面）"
    assert os.path.isdir(os.path.join(config.get_scan_root(), other)), "pending 任务目录必须保留"
    assert name in deleted, "无 pending 的超限任务应正常清理"
    scanner._pending_remove(other, "p001.png")
    time.sleep(1.05)                       # 跨秒：确保 other 是最旧、newer 最新
    jobs.create("newer")                   # 再造超限（max_jobs=1，现 2 个任务）
    deleted = jobs.cleanup(force=True)
    assert other in deleted, "pending 清空后可正常清理"


# ---------- P2-7：原图下载 MIME 按扩展名 ----------

def test_raw_mime_by_extension():
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    Image.new("RGB", (4, 4)).save(os.path.join(base, "p001.png"))
    Image.new("RGB", (4, 4)).save(os.path.join(base, "p002.jpg"))
    Image.new("RGB", (4, 4)).save(os.path.join(base, "p003.webp"))
    c = app_mod.app.test_client()
    for f, want in (("p001.png", "image/png"),
                    ("p002.jpg", "image/jpeg"),
                    ("p003.webp", "image/webp")):
        r = c.get("/job/%s/raw/%s" % (name, f))
        assert r.status_code == 200 and r.headers["Content-Type"] == want, \
            "%s 应返回 %s，实际 %s" % (f, want, r.headers.get("Content-Type"))


# ---------- P2-8：JSON 对象与布尔严格校验 ----------

def test_params_json_root_type_rejected():
    name = _mk_job()
    c = app_mod.app.test_client()
    for body in ([1, 2], "x", 42, None):
        r = c.put("/api/jobs/" + name + "/params", json=body)
        assert r.status_code == 400, "非对象 JSON 必须 400 不 500，实际 %s（body=%r）" % (r.status_code, body)


def test_params_crop_strict_bool():
    name = _mk_job()
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/" + name + "/params", json={"crop": "false"})
    assert r.status_code == 400, "字符串 'false' 不得被误判为真，实际 %s" % r.status_code
    r = c.put("/api/jobs/" + name + "/params", json={"crop": False})
    assert r.status_code == 200 and jobs.load(name)["params"]["crop"] is False


def test_create_rejects_bad_format():
    c = app_mod.app.test_client()
    r = c.post("/api/jobs", data={"dpi": "150", "mode": "Gray", "format": "bmp"})
    assert r.status_code == 400, "非法格式应在创建时拒绝，实际 %s" % r.status_code


# ---------- P2-9：任务目录 PNM 启动恢复（幂等） ----------

def test_recover_task_pnms_idempotent():
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    _ppm(os.path.join(base, "p001.pnm"))          # 无产物 → 应恢复
    _ppm(os.path.join(base, "p002.pnm"))
    Image.new("RGB", (4, 4)).save(os.path.join(base, "p002.png"))   # 已有产物 → 跳过
    rec, skip = scanner.recover_task_pnms()
    assert rec == 1 and skip == 1, "应恢复 1 页跳过 1 页，实际 (%d,%d)" % (rec, skip)
    _wait_pending_idle()
    assert os.path.exists(os.path.join(base, "p001.png")), "恢复的 PNM 应转换出产物"
    assert os.path.exists(os.path.join(base, "p002.pnm")), "已有产物页的 PNM 保留不覆盖"
    # 幂等：再跑一次无新可恢复项
    rec2, _ = scanner.recover_task_pnms()
    assert rec2 == 0, "重复恢复不得重复入队，实际 %d" % rec2


# ---------- 额外项：入队异常后锁不重复释放 ----------

def test_flatbed_enqueue_exception_releases_locks_once(monkeypatch, tmp_path):
    name = _mk_job()
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/fb.sh", "#!/bin/sh\nexit 0\n"))
    orig_enqueue = scanner._enqueue_convert

    def boom(*a, **k):
        raise RuntimeError("模拟入队失败")

    monkeypatch.setattr(scanner, "_enqueue_convert", boom)
    try:
        scanner.scan_flatbed(name)
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "入队异常必须向上抛（不吞）"
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False), "入队异常后 jlock 不得泄漏/重复释放"
    lk.release()
    assert scanner.scan_lock.acquire(blocking=False), "入队异常后 scan_lock 不得泄漏"
    scanner.scan_lock.release()
    # 下一次扫描仍可正常执行（恢复原函数，不动 conftest 的 SCAN_ROOT patch）
    monkeypatch.setattr(scanner, "_enqueue_convert", orig_enqueue)
    r = scanner.scan_flatbed(name)
    assert r == "p001.png", "异常恢复后扫描应正常，实际 %r" % r