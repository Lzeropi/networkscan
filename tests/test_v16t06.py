# tests/test_v16t06.py — v1.16.0-tmp06：DeepSeek 审查修复验证
# P0：ADF 批次超水位死锁（_enqueue_pnms 在双锁内等水位 → worker 无法落盘降
#     pending → 永久死锁，批次 > MAX_PENDING_PNM - 已有pending 页必触发）
# P0：dl_zip pages() 异常路径锁泄漏
# P1：任务删除后 worker 不得重建 state 条目；admin 遍历 state 抗并发修改
# 新功能：ZIP/PDF 打包进度提示
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


def _adf_scan_factory(tmp, pages=2):
    ppm = _ppm(str(tmp) + "/src.ppm")
    body = ('#!/bin/sh\npat=""\nfor a in "$@"; do\n case "$a" in --batch=*) pat="${a#--batch=}";; esac\ndone\n'
            '[ -z "$pat" ] && exit 0\ndir=$(dirname "$pat")\n')
    for i in range(1, pages + 1):
        body += 'cp "%s" "$dir/p%03d.pnm"\n' % (ppm, i)
    body += "exit 0\n"
    return _fake_script(str(tmp) + "/adf.sh", body)


def _wait_state(name, want=("done", "error"), timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = scanner.get_state(name)
        if st["state"] in want:
            return st
        time.sleep(0.05)
    return scanner.get_state(name)


# ---------- P0-1：ADF 批次超水位不得死锁 ----------
# 死锁机理（tmp05 引入）：scan_adf worker 在释放 jlock/scan_lock **之前**调用
# _enqueue_pnms；批次第 MAX+1 页起水位等待 → 等待需要 pending 下降 → pending
# 下降需要转换 worker 落盘 → 落盘需要 jlock → jlock 被批次自己持有 → 死锁。
# 修复后：先释放双锁再入队等待——本测试断言「pending 涨到水位后 jlock 必须可得」。

def test_adf_over_watermark_no_deadlock(monkeypatch, tmp_path):
    name = _mk_job({"source": "adf", "device": "epson:libusb:001:001",
                    "dpi": "150", "mode": "Gray", "crop": False,
                    "format": "png", "source_name": ""})
    total = scanner.MAX_PENDING_PNM + 2          # 6 页 > 水位 4
    monkeypatch.setattr(scanner, "SCANIMAGE", _adf_scan_factory(tmp_path, pages=total))

    def slow_convert(src, dst, fmt="png"):
        time.sleep(0.6)
        Image.new("RGB", (4, 4)).save(dst, "PNG")

    monkeypatch.setattr(scanner, "_convert_pnm", slow_convert)
    assert scanner.scan_adf(name) == "started"
    # 等入队推进到水位（修复前：第 5 页起 _enqueue_pnms 持锁死等）
    deadline = time.time() + 10
    while time.time() < deadline and scanner._pending_count() < scanner.MAX_PENDING_PNM:
        time.sleep(0.05)
    assert scanner._pending_count() >= scanner.MAX_PENDING_PNM, "批次应已入队到水位"
    # 核心断言：水位满、批次仍在入队时，jlock 必须已释放（不持锁等水位）
    lk = jobs.job_lock(name)
    ok = lk.acquire(blocking=False)
    if ok:
        lk.release()
    assert ok, "P0 死锁：批次入队等待水位时不得持有 jlock（worker 无法落盘 = 永久卡死）"
    # 批次应正常收尾（修复后锁已放，worker 可转换、pending 下降、批次 done）
    st = _wait_state(name, timeout=30)
    assert st["state"] == "done", "超水位批次应正常收尾 done，实际 %s" % st
    base = os.path.join(config.get_scan_root(), name)
    got = [f for f in os.listdir(base) if f.endswith(".png")]
    assert len(got) == total, "全部 %d 页应转换完成，实际 %d" % (total, len(got))


# ---------- P0-2：dl_zip 的 pages() 异常路径锁不得泄漏 ----------

def test_zip_pages_error_releases_lock(monkeypatch, tmp_path):
    """DeepSeek P0：jobs.pages 抛 JobError（任务目录被 Samba 外部删除）时，
    dl_zip 必须释放 jlock——否则同任务后续 DELETE 永久阻塞。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    Image.new("RGB", (4, 4)).save(os.path.join(base, "p001.png"))
    # dl_zip 内 jobs.pages 一次调用即抛（模拟任务目录已被 Samba 外部删除）
    def flaky_pages(j, root=None):
        raise jobs.JobError("模拟任务目录被外部删除")

    monkeypatch.setattr(jobs, "pages", flaky_pages)
    c = app_mod.app.test_client()
    r = c.get("/job/" + name + "/download.zip")
    assert r.status_code == 404, "模拟外部删除后 zip 应 404，实际 %s" % r.status_code
    # 关键：锁已释放——DELETE 不能被 jlock 卡死
    rd = c.delete("/api/jobs/" + name)
    assert rd.get_json().get("ok") is True, "pages 异常路径后 DELETE 必须可执行（锁未泄漏）"


# ---------- P1：任务删除后转换残留不得重建 state 条目 ----------
def test_delete_during_convert_no_state_rebuild(monkeypatch, tmp_path):
    name = _mk_job()
    monkeypatch.setattr(scanner, "SCANIMAGE",
                        _fake_script(str(tmp_path) + "/scan.sh", "#!/bin/sh\nexit 0\n"))

    def slow_convert(src, dst, fmt="png"):
        time.sleep(0.8)
        Image.new("RGB", (4, 4)).save(dst, "PNG")

    monkeypatch.setattr(scanner, "_convert_pnm", slow_convert)
    scanner.scan_flatbed(name)
    assert scanner.has_pending(name)
    c = app_mod.app.test_client()
    r = c.delete("/api/jobs/" + name)
    assert r.get_json().get("ok") is True
    # 等 worker 跑完落盘段（isdir False → 清理）
    deadline = time.time() + 10
    while time.time() < deadline and scanner.has_pending(name):
        time.sleep(0.1)
    assert not scanner.has_pending(name), "worker 兜底后 pending 必须清除"
    assert name not in scanner.state, "任务删除后 worker 残留不得重建 state 条目（内存缓涨）"


# ---------- 新功能：打包进度登记与查询 API（按 kind 隔离，ZIP/PDF 不互相覆盖） ----------
def test_pack_progress_api_flow():
    name = _mk_job()
    c = app_mod.app.test_client()
    r = c.get("/api/jobs/" + name + "/pack-progress")
    assert r.get_json() == {}, "无打包记录时应返回空结构"
    app_mod._pack_prog_update(name, "pdf", done=2, total=7, active=True)
    r = c.get("/api/jobs/" + name + "/pack-progress")
    p = r.get_json()
    assert p["pdf"]["done"] == 2 and p["pdf"]["total"] == 7 and p["pdf"]["active"] is True
    # ZIP 与 PDF 同时登记，互不覆盖（tmp07 修复的并发污染）
    app_mod._pack_prog_update(name, "zip", done=1, total=7, active=True)
    r = c.get("/api/jobs/" + name + "/pack-progress")
    p = r.get_json()
    assert p["zip"]["done"] == 1 and p["pdf"]["done"] == 2, "ZIP/PDF 进度必须各自独立：%s" % p
    # 一个完成只收尾自己，不影响另一个
    app_mod._pack_prog_update(name, "zip", active=False)
    r = c.get("/api/jobs/" + name + "/pack-progress")
    p = r.get_json()
    assert p["zip"]["active"] is False and p["pdf"]["active"] is True, \
        "ZIP 收尾不得影响 PDF：%s" % p
    # 任务删除清全部
    c.delete("/api/jobs/" + name)
    r = c.get("/api/jobs/" + name + "/pack-progress")
    assert r.get_json() == {}, "任务删除后进度应清空"
