# tests/test_v16t01.py — v1.16.0-tmp01：输出格式可选（png/jpg/webp）+ 任务页参数修改 API
# 覆盖：_convert_pnm 三格式（魔数+PIL format）、有损无 ImageMagick 回退、
#       端到端 jpg/webp 扫描落盘、老参数兜底 png、PUT /params 正常/部分更新/
#       白名单 400/设备能力 400/404/扫描中 409、混格式任务 reorder/ZIP/PDF 兼容、
#       cleanup_empty_pages 多扩展名
import io
import os
import shutil
import sys
import threading
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402,F401
import config          # noqa: E402,F401
import device_probe    # noqa: E402,F401
import jobs            # noqa: E402,F401
import scanner         # noqa: E402,F401
from PIL import Image  # noqa: E402


# ---------- 工具 ----------

def _mk_job(params=None, letters=()):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    name = jobs.create("t", params or {})
    d = os.path.join(config.get_scan_root(), name)
    for i, c in enumerate(letters, 1):
        with open(os.path.join(d, "p%03d.png" % i), "wb") as f:
            f.write(c.encode())
    return name


def _fake_script(path, body):
    open(path, "w").write(body)
    os.chmod(path, 0o755)
    return path


def _ppm_path(tmp, color=(10, 120, 210)):
    """真实 PPM（scanimage --format pnm 同源格式）——走 PIL 真实解码路径"""
    p = str(tmp) + "/page.ppm"
    Image.new("RGB", (8, 8), color).save(p, "PPM")
    return p


def _fake_scanimage(tmp, ppm, sleep=0.2):
    """假 scanimage：cat 真实 PPM 到 stdout（被 subprocess 重定向进临时 .pnm）"""
    return _fake_script(str(tmp) + "/scan.sh",
                        '#!/bin/sh\nsleep %s\ncat "%s"\nexit 0\n' % (sleep, ppm))


def _wait_state(name, want=("done", "error"), timeout=10):
    deadline = time.time() + timeout
    st = {}
    while time.time() < deadline:
        st = scanner.get_state(name)
        if st["state"] in want:
            return st
        time.sleep(0.05)
    return st


# mock 设备探测列表（api_params 能力校验数据源）
_DEVS = [{
    "name": "hpljm1005:libusb:001:005", "alias": "HP M1005",
    "sources": ["Flatbed"],
    "dpi_raw": ["150", "300", "600"],
    "modes": ["Color", "Gray", "Lineart"],
}, {
    "name": "kyocera:libusb:002:003", "alias": "京瓷 P5021",
    "sources": ["Flatbed", "ADF"],
    "dpi_raw": ["150", "300"],
    "modes": ["Color"],
}]


def _mock_probe(monkeypatch, devs=None):
    monkeypatch.setattr(device_probe, "probe",
                        lambda force=False: (devs if devs is not None else _DEVS, False))


# ---------- 1) 参数兜底：老任务无 format → png ----------

def test_params_format_default_png():
    name = _mk_job()                                   # 无 format 字段（v1.15 老任务）
    p = scanner._params(name)
    assert p["format"] == "png", "老任务应兜底 png，实际 %r" % p["format"]
    m = jobs.load(name)
    m["params"]["format"] = "gif"                      # 非法值（历史损坏/手改）
    jobs.save(name, m)
    assert scanner._params(name)["format"] == "png", "非法格式必须 fail-closed 回 png"


# ---------- 2) _convert_pnm 三格式：魔数 + PIL format ----------

def test_convert_pnm_three_formats(tmp_path):
    src = _ppm_path(tmp_path)
    # PNG
    dst = str(tmp_path / "o.png")
    scanner._convert_pnm(src, dst, "png")
    assert open(dst, "rb").read(8) == b"\x89PNG\r\n\x1a\n", "PNG 魔数不符"
    assert Image.open(dst).format == "PNG"
    # JPEG
    dst = str(tmp_path / "o.jpg")
    scanner._convert_pnm(src, dst, "jpg")
    assert open(dst, "rb").read(3) == b"\xff\xd8\xff", "JPEG 魔数不符"
    assert Image.open(dst).format == "JPEG"
    assert os.path.getsize(dst) > 0
    # WebP
    dst = str(tmp_path / "o.webp")
    scanner._convert_pnm(src, dst, "webp")
    head = open(dst, "rb").read(12)
    assert head[:4] == b"RIFF" and head[8:12] == b"WEBP", "WebP 魔数不符"
    assert Image.open(dst).format == "WEBP"
    # 体积对比只在真实扫描尺寸成立（8x8 纯色 PNG 反而更小），此处不比体积


# ---------- 3) 有损格式 PIL 失败必须 raise（无 ImageMagick 回退） ----------

def test_convert_pnm_lossy_no_magick_fallback(tmp_path, monkeypatch):
    src = _ppm_path(tmp_path)
    dst = str(tmp_path / "o.jpg")
    calls = []

    def boom(path):
        raise OSError("PIL simulated failure")

    monkeypatch.setattr(scanner.Image, "open", boom)
    real_run = scanner.subprocess.run

    def spy_run(cmd, **kw):
        calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(scanner.subprocess, "run", spy_run)
    try:
        scanner._convert_pnm(src, dst, "jpg")
        raise AssertionError("jpg 转换失败必须抛异常（走 PNM 保留路径）")
    except OSError:
        pass
    assert not calls, "有损格式 PIL 失败后不得回退 ImageMagick（二次有损），实际调了 %s" % calls
    assert not os.path.exists(dst), "失败时不得留下半截目标文件"
    # 对照：PNG 允许回退（历史行为回归保护）
    monkeypatch.setattr(scanner.subprocess, "run", real_run)
    cv = _fake_script(str(tmp_path) + "/cvt.sh", '#!/bin/sh\nt="${2#PNG:}"; echo x > "$t"\n')   # v1.16.0-tmp01-fix：回退命令带格式前缀 PNG:，脚本剥离
    old_cv = scanner.CONVERT
    scanner.CONVERT = cv
    try:
        dst2 = str(tmp_path / "o2.png")
        scanner._convert_pnm(src, dst2, "png")
        assert os.path.exists(dst2), "PNG 失败应回退 ImageMagick 落盘"
    finally:
        scanner.CONVERT = old_cv


# ---------- 3b) webp 回退 ImageMagick（203 盒子 Pillow 无 libwebp 编码） ----------

def test_convert_pnm_webp_magick_fallback(tmp_path, monkeypatch):
    """v1.16.0-tmp01-fix：盒子 Pillow 编译无 libwebp → webp 编码失败必须回退
    ImageMagick（从原始 PNM 一次编码，无二次有损）。"""
    src = _ppm_path(tmp_path)
    dst = str(tmp_path / "w.webp")
    calls = []

    def boom(path):
        raise OSError("PIL no webp encoder")

    monkeypatch.setattr(scanner.Image, "open", boom)

    def spy_run(cmd, **kw):
        calls.append(cmd)
        Image.new("RGB", (8, 8)).save(dst, "WEBP")   # mock convert 产出真 webp
        return None

    monkeypatch.setattr(scanner.subprocess, "run", spy_run)
    old_cv = scanner.CONVERT
    scanner.CONVERT = "convert"
    try:
        scanner._convert_pnm(src, dst, "webp")
    finally:
        scanner.CONVERT = old_cv
    assert calls, "webp 编码失败必须回退 ImageMagick"
    assert len(calls) == 1, "回退只能调用一次（曾因 PNG 回退无条件执行覆盖成 PNG），实际 %d 次" % len(calls)
    # v1.16.0-tmp06（DeepSeek P1）：回退改走安全副本（TOCTOU 防线）——副本从原始
    # PNM 字节复制、无二次有损语义保持；断言副本名含原文件名且非原路径本身
    assert os.path.basename(src) in calls[0][1] and calls[0][1].endswith(".pnm"), \
        "回退应从 PNM 源的安全副本编码（无二次有损），实际 %r" % calls[0][1]
    assert calls[0][1] != src, "回退不得直接读原路径（TOCTOU 防线，DeepSeek P1）"
    assert calls[0][-1] == "WEBP:" + dst, "回退必须显式 WEBP: 前缀（.partial 扩展名会被 ImageMagick 输出成 PNM）"
    assert open(dst, "rb").read(4) == b"RIFF", "回退产物必须是真 WebP"


def test_mk_thumb_webp_fallback(tmp_path, monkeypatch):
    """v1.16.0-tmp01-fix：盒子 Pillow 无 webp 解码 → 缩略图回退 ImageMagick。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    open(os.path.join(base, "p001.webp"), "wb").write(b"RIFFfake")   # 假 webp 页
    calls = []

    def boom(path):
        raise OSError("no webp decoder")

    monkeypatch.setattr(scanner.Image, "open", boom)

    def fake_run(cmd, **kw):
        calls.append(cmd)
        open(cmd[-1], "wb").write(b"\xff\xd8\xff")   # mock thumb jpg 产出
        return None

    monkeypatch.setattr(scanner.subprocess, "run", fake_run)
    old_cv = scanner.CONVERT
    scanner.CONVERT = "convert"
    try:
        scanner._mk_thumb(name, "p001.webp")
    finally:
        scanner.CONVERT = old_cv
    assert calls, "Pillow 打不开 webp 时应回退 ImageMagick 生成 thumb"
    assert os.path.getsize(os.path.join(base, ".thumbs", "p001.jpg")) > 0, \
        "webp 页缩略图应生成（统一 .jpg 命名）"


# ---------- 4/5) 端到端：jpg / webp 任务扫描全链路 ----------

def test_scan_e2e_jpg(tmp_path):
    name = _mk_job({"dpi": "150", "mode": "Color", "device": "", "format": "jpg"})
    old = scanner.SCANIMAGE
    scanner.SCANIMAGE = _fake_scanimage(tmp_path, _ppm_path(tmp_path))
    try:
        r = scanner.scan_flatbed(name)
        assert r == "p001.jpg", "jpg 任务应返回 .jpg 文件名，实际 %r" % r
        st = _wait_state(name)
        assert st["state"] == "done", "jpg 转换应成功，实际 %s" % st
        fp = os.path.join(config.get_scan_root(), name, "p001.jpg")
        assert open(fp, "rb").read(3) == b"\xff\xd8\xff", "落盘文件必须是真 JPEG"
        assert Image.open(fp).format == "JPEG"
        th = os.path.join(config.get_scan_root(), name, ".thumbs", "p001.jpg")
        assert os.path.getsize(th) > 0, "缩略图应生成（统一 .jpg 命名）"
        assert Image.open(th).format == "JPEG"
    finally:
        scanner.SCANIMAGE = old


def test_scan_e2e_webp(tmp_path):
    name = _mk_job({"dpi": "150", "mode": "Color", "device": "", "format": "webp"})
    old = scanner.SCANIMAGE
    scanner.SCANIMAGE = _fake_scanimage(tmp_path, _ppm_path(tmp_path))
    try:
        r = scanner.scan_flatbed(name)
        assert r == "p001.webp", "webp 任务应返回 .webp 文件名，实际 %r" % r
        st = _wait_state(name)
        assert st["state"] == "done", "webp 转换应成功，实际 %s" % st
        fp = os.path.join(config.get_scan_root(), name, "p001.webp")
        head = open(fp, "rb").read(12)
        assert head[:4] == b"RIFF" and head[8:12] == b"WEBP", "落盘文件必须是真 WebP"
        th = os.path.join(config.get_scan_root(), name, ".thumbs", "p001.jpg")
        assert os.path.getsize(th) > 0, "webp 页缩略图仍应转 JPEG（浏览器兼容）"
        assert Image.open(th).format == "JPEG"
    finally:
        scanner.SCANIMAGE = old


# ---------- 6/7) PUT /params：正常 + 部分更新 ----------

def test_put_params_ok(monkeypatch):
    _mock_probe(monkeypatch)
    name = _mk_job({"dpi": "150", "mode": "Gray", "device": "hpljm1005:libusb:001:005",
                    "format": "png", "source": "flatbed"})
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/" + name + "/params",
              json={"device": "kyocera:libusb:002:003", "dpi": "300",
                    "mode": "Color", "format": "jpg", "crop": True})
    assert r.status_code == 200, "合法参数应 200，实际 %s %s" % (r.status_code, r.get_json())
    d = r.get_json()
    assert d["ok"] and d["params"]["format"] == "jpg", "响应应含新参数：%s" % d
    p = jobs.load(name)["params"]
    assert (p["dpi"], p["mode"], p["format"], p["crop"]) == ("300", "Color", "jpg", True), \
        "meta 必须落盘新参数，实际 %s" % p


def test_put_params_partial_update(monkeypatch):
    """只传 format → 其余字段保持当前值（前端可能只发改动项）"""
    _mock_probe(monkeypatch)
    name = _mk_job({"dpi": "300", "mode": "Gray", "device": "hpljm1005:libusb:001:005",
                    "format": "png", "crop": True, "source": "flatbed"})
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/" + name + "/params", json={"format": "webp"})
    assert r.status_code == 200, "部分更新应 200，实际 %s" % r.status_code
    p = jobs.load(name)["params"]
    assert p["format"] == "webp", "format 应更新为 webp"
    assert (p["dpi"], p["mode"], p["crop"]) == ("300", "Gray", True), \
        "未传字段必须保持原值，实际 %s" % p


# ---------- 8-12) PUT /params：拒绝路径 ----------

def test_put_params_bad_format_400(monkeypatch):
    _mock_probe(monkeypatch)
    name = _mk_job({"dpi": "150", "mode": "Gray", "device": "hpljm1005:libusb:001:005",
                    "format": "png", "source": "flatbed"})
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/" + name + "/params", json={"format": "gif"})
    assert r.status_code == 400, "非白名单格式必须 400（fail-closed），实际 %s" % r.status_code
    assert jobs.load(name)["params"]["format"] == "png", "被拒请求不得改动 meta"


def test_put_params_bad_dpi_mode_400(monkeypatch):
    _mock_probe(monkeypatch)
    name = _mk_job({"dpi": "150", "mode": "Gray", "device": "hpljm1005:libusb:001:005",
                    "format": "png", "source": "flatbed"})
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/" + name + "/params", json={"dpi": "1200"})
    assert r.status_code == 400, "超 VALID_DPI 必须 400，实际 %s" % r.status_code
    r = c.put("/api/jobs/" + name + "/params", json={"mode": "Photo"})
    assert r.status_code == 400, "超 VALID_MODE 必须 400，实际 %s" % r.status_code


def test_put_params_dpi100_allowed(monkeypatch):
    """v1.16.0-tmp01 修复回归锁：M1005 探测实报 75|100|150|200|300|600|1200，
    VALID_DPI 曾漏 100 → PUT 400「参数超出支持范围」+ _params 偷改 100→150。
    设备能力含 100 时：PUT 必须 200 落盘 100，扫描快照不得偷改。"""
    devs = [{"name": "hpljm1005:libusb:001:005", "alias": "HP M1005",
             "sources": ["Flatbed"],
             "dpi_raw": ["75", "100", "150", "200", "300", "600", "1200"],
             "modes": ["Color", "Gray", "Lineart"]}]
    _mock_probe(monkeypatch, devs)
    name = _mk_job({"dpi": "150", "mode": "Gray", "device": "hpljm1005:libusb:001:005",
                    "format": "png", "source": "flatbed"})
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/" + name + "/params", json={"dpi": "100"})
    assert r.status_code == 200, "设备能力含 100dpi，PUT 必须 200，实际 %s %s" % (
        r.status_code, r.get_json())
    assert jobs.load(name)["params"]["dpi"] == "100", "100 必须原样落盘"
    assert scanner._params(name)["dpi"] == "100", "扫描快照不得偷改 100→150"


def test_put_params_device_not_probed_400(monkeypatch):
    _mock_probe(monkeypatch)
    name = _mk_job({"dpi": "150", "mode": "Gray", "device": "hpljm1005:libusb:001:005",
                    "format": "png", "source": "flatbed"})
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/" + name + "/params", json={"device": "ghost:libusb:009:009"})
    assert r.status_code == 400, "不在探测列表的设备必须 400，实际 %s" % r.status_code
    assert "探测" in r.get_json().get("msg", "") or "列表" in r.get_json().get("msg", "")


def test_put_params_device_capability_400(monkeypatch):
    _mock_probe(monkeypatch)
    name = _mk_job({"dpi": "150", "mode": "Gray", "device": "hpljm1005:libusb:001:005",
                    "format": "png", "source": "flatbed"})
    c = app_mod.app.test_client()
    # 京瓷 dpi_raw 无 600、modes 无 Gray
    r = c.put("/api/jobs/" + name + "/params",
              json={"device": "kyocera:libusb:002:003", "dpi": "600"})
    assert r.status_code == 400, "设备不支持 600dpi 必须 400，实际 %s" % r.status_code
    r = c.put("/api/jobs/" + name + "/params",
              json={"device": "kyocera:libusb:002:003", "mode": "Gray"})
    assert r.status_code == 400, "设备不支持 Gray 模式必须 400，实际 %s" % r.status_code


def test_put_params_adf_source_check(monkeypatch):
    _mock_probe(monkeypatch)
    name = _mk_job({"dpi": "150", "mode": "Gray", "device": "kyocera:libusb:002:003",
                    "format": "png", "source": "adf"})   # ADF 任务
    c = app_mod.app.test_client()
    # hpljm1005 仅 Flatbed → 用于 ADF 任务必须 400
    r = c.put("/api/jobs/" + name + "/params",
              json={"device": "hpljm1005:libusb:001:005"})
    assert r.status_code == 400, "无 ADF 设备用于 ADF 任务必须 400，实际 %s" % r.status_code


def test_put_params_404(monkeypatch):
    _mock_probe(monkeypatch)
    c = app_mod.app.test_client()
    r = c.put("/api/jobs/20260101-000000_deadbee/params", json={"format": "jpg"})
    assert r.status_code == 404, "不存在任务必须 404，实际 %s" % r.status_code


# ---------- 13) PUT /params：转换中允许改参数（对下次扫描生效，不影响当前页） ----------

def test_put_params_during_convert(tmp_path, monkeypatch):
    """v1.16.0-tmp01（用户反馈改进）：转换中改参数必须 200——
    参数本来就是「下次扫描生效」语义（scan_flatbed 在 scanimage 前快照），
    转换中的页已用旧参数，不冲突；jlock 串行化保证 meta 写安全。"""
    _mock_probe(monkeypatch)
    name = _mk_job({"dpi": "150", "mode": "Color", "device": "", "format": "png"})
    c = app_mod.app.test_client()
    out = {}

    def run():
        try:
            out["r"] = scanner.scan_flatbed(name)
        except Exception as e:
            out["err"] = str(e)[:80]

    # 慢转换制造 has_pending 窗口（PIL 8x8 极快会让 convert_worker 秒清 pending）
    class _SlowConv:
        def __init__(self, ppm, sleep=2.0):
            self.ppm, self.sleep = ppm, sleep
        def __call__(self, src, dst, fmt="png"):
            time.sleep(self.sleep)
            Image.open(self.ppm).save(dst, format="PNG")

    monkeypatch.setattr(scanner, "_convert_pnm", _SlowConv(_ppm_path(tmp_path)))
    old = scanner.SCANIMAGE
    scanner.SCANIMAGE = _fake_scanimage(tmp_path, _ppm_path(tmp_path), sleep=0.3)
    try:
        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and (scanner.state.get(name) or {}).get("state") != "scanning":
            time.sleep(0.02)
        # 等 scanimage 完成、scan_flatbed 释放 jlock、pending 登记
        deadline2 = time.time() + 5
        while time.time() < deadline2 and not scanner.has_pending(name):
            time.sleep(0.02)
        assert scanner.has_pending(name), "前置：转换中 pending 应已登记"
        # 转换中改格式 → 200 落盘（当前 p001.png 转换不受影响）
        r = c.put("/api/jobs/" + name + "/params", json={"format": "jpg"})
        assert r.status_code == 200, "转换中改参数应 200（对下次扫描生效），实际 %s %s" % (
            r.status_code, r.get_json())
        assert jobs.load(name)["params"]["format"] == "jpg", "转换中改参数应落盘"
        t.join(timeout=15)
        _wait_state(name)
        # 当前页仍是 png 格式（转换用旧快照参数），下次扫描才是 jpg
        st = scanner.get_state(name)
        assert st["state"] == "done", "当前页转换不受参数修改影响，实际 %s" % st
        assert os.path.exists(os.path.join(config.get_scan_root(), name, "p001.png")), \
            "当前转换页应为 png（快照格式），实际格式被改？"
        assert not scanner.has_pending(name), "转换应已收尾"
    finally:
        scanner.SCANIMAGE = old


# ---------- 14) 混格式任务：reorder 内容跟随 + ZIP/PDF 兼容 ----------

def test_mixed_reorder_zip_pdf(tmp_path):
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    colors = {"p001.png": (200, 10, 10), "p002.jpg": (10, 200, 10), "p003.webp": (10, 10, 200)}
    for fname, col in colors.items():
        fmt = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[fname.rsplit(".", 1)[-1]]
        Image.new("RGB", (8, 8), col).save(os.path.join(base, fname), format=fmt)
        Image.new("RGB", (8, 8), col).save(os.path.join(base, ".thumbs", fname[:-4] + ".jpg"),
                                          format="JPEG", quality=80)
    c = app_mod.app.test_client()
    # 反序 + 混格式：p002.jpg → 第 1 位，p003.webp → 第 2 位，p001.png → 第 3 位
    r = c.post("/api/jobs/" + name + "/reorder",
               json={"order": ["p002.jpg", "p003.webp", "p001.png"], "delete": False})
    assert r.status_code == 200, "混格式 reorder 应 200，实际 %s %s" % (r.status_code, r.get_json())
    # 内容跟随：p001.jpg 是原 p002 的绿色，p002.webp 是原 p003 的蓝色，p003.png 是原 p001 的红色
    def _px(fp):
        with Image.open(os.path.join(base, fp)).convert("RGB") as im:
            return im.getpixel((4, 4))
    def _near(px, want, tol=6):   # 有损压缩（JPEG/WebP q85）颜色允许 ±6 偏移
        return all(abs(a - b) <= tol for a, b in zip(px, want))
    px1, px2, px3 = _px("p001.jpg"), _px("p002.webp"), _px("p003.png")
    assert _near(px1, (10, 200, 10)), "p001 应为原 p002 内容（绿），实际 %s" % (px1,)
    assert _near(px2, (10, 10, 200)), "p002 应为原 p003 内容（蓝），实际 %s" % (px2,)
    assert _near(px3, (200, 10, 10)), "p003 应为原 p001 内容（红），实际 %s" % (px3,)
    # thumb 同步跟随
    with Image.open(os.path.join(base, ".thumbs", "p001.jpg")).convert("RGB") as im:
        assert _near(im.getpixel((4, 4)), (10, 200, 10)), "thumb 必须跟随页面重命名"
    # ZIP：条目顺序 = 新顺序，全部可解压
    rz = c.get("/job/" + name + "/download.zip")
    assert rz.status_code == 200, "混格式 ZIP 应 200，实际 %s" % rz.status_code
    zf = zipfile.ZipFile(io.BytesIO(rz.data))
    assert zf.namelist() == ["p001.jpg", "p002.webp", "p003.png"], \
        "ZIP 条目应为新顺序，实际 %s" % zf.namelist()
    assert zf.testzip() is None, "ZIP 内容必须完好"
    # PDF：PIL 统一合成，三格式混排可合并
    rp = c.get("/job/" + name + "/download.pdf")
    assert rp.status_code == 200, "混格式 PDF 应 200，实际 %s" % rp.status_code
    assert rp.data[:5] == b"%PDF-", "响应必须是 PDF 文件"
    assert len(rp.data) > 500, "PDF 应含三页真实内容（非空壳）"


# ---------- 16) webp 任务转换中请求 thumb：long-poll 按 stem 匹配（回归保护） ----------

def test_thumb_longpoll_webp(tmp_path, monkeypatch):
    """v1.16.0-tmp01：webp 页转换中请求缩略图应挂起等待后 200，
    不得因 pending 名是 p001.webp 而查 p001.png 导致立即 404（退化成每秒轮询）。"""
    name = _mk_job({"dpi": "150", "mode": "Color", "device": "", "format": "webp"})

    class _SlowWebp:
        def __init__(self, ppm, sleep=1.5):
            self.ppm, self.sleep = ppm, sleep

        def __call__(self, src, dst, fmt="png"):
            time.sleep(self.sleep)
            Image.open(self.ppm).convert("RGB").save(dst, format="WEBP", quality=85)

    monkeypatch.setattr(scanner, "_convert_pnm", _SlowWebp(_ppm_path(tmp_path)))
    old = scanner.SCANIMAGE
    scanner.SCANIMAGE = _fake_scanimage(tmp_path, _ppm_path(tmp_path), sleep=0.1)
    try:
        scanner.scan_flatbed(name)
        assert scanner.has_pending(name, "p001.webp"), "前置：webp 页应登记 pending"
        c = app_mod.app.test_client()
        t0 = time.time()
        r = c.get("/job/" + name + "/thumb/p001.jpg")
        dt = time.time() - t0
        assert r.status_code == 200, "转换中 thumb 请求应挂起后 200，实际 %s" % r.status_code
        assert dt >= 1.0, "应等待转换完成（挂起 ~%.2fs），实际立即返回 %.2fs" % (dt, dt)
        assert r.data[:3] == b"\xff\xd8\xff", "thumb 应为 JPEG"
        st = _wait_state(name)
        assert st["state"] == "done", "扫描应正常完成，实际 %s" % st
    finally:
        scanner.SCANIMAGE = old


# ---------- 15) cleanup_empty_pages：多扩展名 0 字节清理 ----------

def test_cleanup_empty_pages_multi_ext():
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    open(os.path.join(base, "p001.jpg"), "wb").close()      # 0 字节 jpg 占位
    open(os.path.join(base, "p002.webp"), "wb").close()     # 0 字节 webp 占位
    Image.new("RGB", (4, 4)).save(os.path.join(base, "p003.png"), "PNG")
    scanner.cleanup_empty_pages()
    left = sorted(f for f in os.listdir(base) if f.startswith("p"))
    assert left == ["p003.png"], "0 字节 jpg/webp 占位应清理、真页保留，实际 %s" % left


# ---------- 16) /api/devices?refresh=1：普通页手动重新探测 ----------

def test_api_devices_refresh_param(monkeypatch):
    """v1.16.0-tmp01：refresh=1 必须透传 probe(force=True)——首页/任务页
    「重新探测」按钮依赖（admin API 需 PIN 登录，普通页不能复用）"""
    calls = []

    def fake_probe(force=False):
        calls.append(force)
        return ([{"name": "hpljm1005:libusb:001:005", "modes": ["Gray"],
                  "sources": [], "dpis": [], "dpi": "", "dpi_raw": [],
                  "scan_type": "仅平板单张", "error": ""}], False)

    monkeypatch.setattr(device_probe, "probe", fake_probe)
    c = app_mod.app.test_client()
    assert c.get("/api/devices").status_code == 200
    assert calls[-1] is False, "默认请求不得强制探测"
    assert c.get("/api/devices?refresh=1").status_code == 200
    assert calls[-1] is True, "refresh=1 必须透传 force=True"
