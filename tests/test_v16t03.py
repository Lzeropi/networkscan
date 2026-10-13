# tests/test_v16t03.py — v1.16.0-tmp03：ADF 批量入队 + 换设备校验
# 覆盖：ADF 多页入队后由队列消化 → done（模拟无 ADF 设备）、
#       ADF 扫描段结束即释放 jlock/scan_lock（转换期 reorder 不被阻塞）、
#       done 判定看 pending 收敛（残留 PNM → error）、换设备 source 兼容校验
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402
import config          # noqa: E402
import device_probe    # noqa: E402
import jobs            # noqa: E402
import scanner         # noqa: E402
from PIL import Image  # noqa: E402


def _mk_job(params):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    scanner._reset_convert_for_test()
    return jobs.create("t", params)


def _fake_script(path, body):
    open(path, "w").write(body)
    os.chmod(path, 0o755)
    return path


def _ppm(path):
    Image.new("RGB", (8, 8), (10, 120, 210)).save(path, "PPM")
    return path


def _adf_scan_factory(tmp, pages=2, sleep=0.0):
    """假 scanimage：解析 --batch= 参数，往批目录写 pages 页真实 PPM（供 PIL 转换）。"""
    ppm = _ppm(str(tmp) + "/src.ppm")
    body = ('#!/bin/sh\npat=""\nfor a in "$@"; do\n case "$a" in --batch=*) pat="${a#--batch=}";; esac\ndone\n'
            '[ -z "$pat" ] && exit 0\ndir=$(dirname "$pat")\n')
    if sleep:
        body += "sleep %s\n" % sleep
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


# ---------- 1) ADF 多页入队 → 队列消化 → done ----------

def test_adf_enqueue_batch_done(monkeypatch, tmp_path):
    name = _mk_job({"source": "adf", "device": "epson:libusb:001:001",
                    "dpi": "150", "mode": "Gray", "crop": False,
                    "format": "png", "source_name": ""})   # source_name 空 → 跳过 fail-closed
    monkeypatch.setattr(scanner, "SCANIMAGE", _adf_scan_factory(tmp_path, pages=3))
    assert scanner.scan_adf(name) == "started"
    st = _wait_state(name)
    assert st["state"] == "done", "ADF 多页应全部转换后 done，实际 %s" % st
    base = os.path.join(config.get_scan_root(), name)
    pages = sorted(f for f in os.listdir(base) if f.endswith(".png"))
    assert pages == ["p001.png", "p002.png", "p003.png"], "3 页应全部产出，实际 %s" % pages
    assert jobs.load(name)["pages"] == 3, "meta.pages 应更新为 3"
    assert not [f for f in os.listdir(base) if f.endswith(".pnm")], "成功页不得残留 PNM"


# ---------- 2) ADF 扫描段结束即释放锁（转换期 reorder 不被阻塞） ----------

def test_adf_releases_locks_before_convert_done(monkeypatch, tmp_path):
    name = _mk_job({"source": "adf", "device": "epson:libusb:001:001",
                    "dpi": "150", "mode": "Gray", "crop": False,
                    "format": "png", "source_name": ""})
    monkeypatch.setattr(scanner, "SCANIMAGE", _adf_scan_factory(tmp_path, pages=2))

    def slow_convert(src, dst, fmt="png"):
        time.sleep(1.0)
        Image.new("RGB", (4, 4)).save(dst, "PNG")

    monkeypatch.setattr(scanner, "_convert_pnm", slow_convert)
    assert scanner.scan_adf(name) == "started"
    # 等入队（pending 出现）后，锁应已释放（转换仍需 1s 才完成）
    deadline = time.time() + 5
    while time.time() < deadline and scanner._pending_count() == 0:
        time.sleep(0.02)
    assert scanner._pending_count() > 0, "应已入队（转换进行中）"
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False), "ADF 转换期间 jlock 必须已释放（reorder/PDF 不被阻塞）"
    lk.release()
    assert scanner.scan_lock.acquire(blocking=False), "ADF 转换期间 scan_lock 必须已释放"
    scanner.scan_lock.release()
    _wait_state(name)


# ---------- 3) done 判定：转换失败页残留 → error（不看 scanimage 返回码） ----------

def test_adf_failed_page_reports_error(monkeypatch, tmp_path):
    name = _mk_job({"source": "adf", "device": "epson:libusb:001:001",
                    "dpi": "150", "mode": "Gray", "crop": False,
                    "format": "png", "source_name": ""})
    monkeypatch.setattr(scanner, "SCANIMAGE", _adf_scan_factory(tmp_path, pages=2))

    def flaky_convert(src, dst, fmt="png"):
        if "p002" in src:
            raise RuntimeError("模拟 p002 转换失败")
        Image.new("RGB", (4, 4)).save(dst, "PNG")

    monkeypatch.setattr(scanner, "_convert_pnm", flaky_convert)
    assert scanner.scan_adf(name) == "started"
    st = _wait_state(name)
    assert st["state"] == "error", "有页转换失败不得报 done，实际 %s" % st
    assert "转换失败" in st["msg"], "应提示转换失败并保留数据，实际 %r" % st["msg"]
    base = os.path.join(config.get_scan_root(), name)
    assert os.path.exists(os.path.join(base, "p002.pnm")), "失败页 PNM 必须保留"


# ---------- 4) 换设备：source 兼容校验 ----------

_DEVS = [
    {"name": "epson:libusb:001:001", "alias": "Epson ADF", "sources": ["Flatbed", "ADF"],
     "dpi_raw": ["150", "300"], "modes": ["Color", "Gray"]},
    {"name": "hpljm1005:libusb:002:002", "alias": "HP M1005", "sources": ["Flatbed"],
     "dpi_raw": ["150", "300", "600"], "modes": ["Color", "Gray"]},
]


def _mock_probe(monkeypatch, devs=None):
    monkeypatch.setattr(device_probe, "probe",
                        lambda force=False: (devs if devs is not None else _DEVS, False))


def test_change_device_to_non_adf_400(monkeypatch):
    """任务 source=adf，换到无 ADF 设备（HP M1005）→ 400 拒绝。"""
    _mock_probe(monkeypatch)
    name = _mk_job({"source": "adf", "device": "epson:libusb:001:001",
                    "dpi": "150", "mode": "Gray", "format": "png"})
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/" + name + "/params", json={"device": "hpljm1005:libusb:002:002"})
    assert r.status_code == 400, "无 ADF 设备不得用于 ADF 任务，实际 %s %s" % (r.status_code, r.get_json())
    assert jobs.load(name)["params"]["device"] == "epson:libusb:001:001", "被拒不得改动 meta"


def test_change_device_flatbed_ok(monkeypatch):
    """任务 source=flatbed，换到任意平板设备 → 200。"""
    _mock_probe(monkeypatch)
    name = _mk_job({"source": "flatbed", "device": "epson:libusb:001:001",
                    "dpi": "150", "mode": "Gray", "format": "png"})
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/" + name + "/params", json={"device": "hpljm1005:libusb:002:002"})
    assert r.status_code == 200, "平板任务换设备应放行，实际 %s %s" % (r.status_code, r.get_json())
    assert jobs.load(name)["params"]["device"] == "hpljm1005:libusb:002:002"