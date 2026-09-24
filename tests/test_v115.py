# tests/test_v115.py — v1.15 八轮审查收官版验证
# #2 invalidate 版本号（竞态根除）/ #1 worker 根快照（TOCTOU 后果消除）/
# #4 errno 提示细化 / #5 _iter_page_files 收口 / #3 注记见 jobs.cleanup docstring
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402,F401
import config          # noqa: E402,F401
import device_probe    # noqa: E402,F401
import jobs            # noqa: E402,F401
import scanner         # noqa: E402,F401


def _mk_job(letters, pages_meta=True):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    name = jobs.create("t")
    d = os.path.join(config.get_scan_root(), name)
    for i, c in enumerate(letters, 1):
        with open(os.path.join(d, "p%03d.png" % i), "wb") as f:
            f.write(c.encode())
    if pages_meta:
        m = jobs.load(name)
        m["pages"] = len(letters)
        jobs.save(name, m)
    return name


def _fake_script(path, body):
    open(path, "w").write(body)
    os.chmod(path, 0o755)
    return path


def _png_bytes():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


# ---------- #2：invalidate 版本号并发精确性（_ver_lock 串行自增） ----------
def test_invalidate_concurrent_version_exact(monkeypatch):
    monkeypatch.setattr(device_probe, "SCANIMAGE", "/nonexistent/scanimage")
    device_probe._cache.update(ts=0.0, data=None, ver=100, data_ver=100)
    N = 50
    errs = []

    def invalidater():
        try:
            device_probe.invalidate()
        except Exception as e:   # pragma: no cover
            errs.append(e)

    def prober():
        try:
            device_probe.probe()
        except Exception as e:   # pragma: no cover
            errs.append(e)

    threads = [threading.Thread(target=invalidater) for _ in range(N)] + \
              [threading.Thread(target=prober) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not errs, "#2：并发 invalidate/probe 不得抛异常"
    assert device_probe._cache["ver"] == 100 + N, \
        "#2：_ver_lock 下 50 并发 invalidate 必须精确自增 50（丢失即竞态未根除）"
    # 收敛断言：最后单次 probe 后，缓存记录本次进入时快照且命中
    device_probe.probe()
    v = device_probe._cache["ver"]
    device_probe.probe()
    assert device_probe._cache["data_ver"] == v, "#2：无新失效时 probe 应命中缓存"


# ---------- #5：_iter_page_files 收口后两函数语义分叉正确 ----------
def test_iter_page_files_equivalence():
    name = _mk_job(["A"])
    d = os.path.join(config.get_scan_root(), name)
    open(os.path.join(d, "p002.png"), "wb").close()          # 0 字节占位
    os.symlink("/etc/passwd", os.path.join(d, "p999.png"))  # symlink 幽灵页
    open(os.path.join(d, "note.txt"), "w").write("x")       # 非法名
    assert jobs.pages(name) == ["p001.png"], \
        "#5：pages 只回非空非链接页（0 字节占位对用户不可见）"
    assert jobs.raw_pages(name) == ["p001.png", "p002.png"], \
        "#5：raw_pages 保留 0 字节占位（编号防冲突）且过滤 symlink"
    # root 参数显式路径（worker 快照透传链）
    assert jobs.pages(name, config.get_scan_root()) == ["p001.png"], "#5/#1：root 透传等价"
    os.remove(os.path.join(d, "p999.png"))


# ---------- #4：open_page_fd errno 细化——ENOENT 提示不再千篇一律 ----------
def test_open_page_fd_enoent_hint():
    name = _mk_job(["A"])
    d = os.path.join(config.get_scan_root(), name)
    try:
        jobs.open_page_fd(d, "p777.png")
        raised = False
    except jobs.JobError as e:
        raised = True
        assert "不存在" in str(e), "#4：ENOENT 应提示「文件不存在」，实际：%s" % e
    assert raised, "#4：不存在页面必须抛 JobError"


# ---------- #1：validate/load/save 支持根快照透传（校验链完整保留） ----------
def test_jobs_root_param_validation_kept(tmp_path):
    name = _mk_job(["A"])
    root2 = str(tmp_path / "root2")
    os.makedirs(os.path.join(root2, name), exist_ok=True)
    # 正常：快照根下任务目录有效
    p = jobs.validate(name, root2)
    assert p == os.path.join(root2, name), "#1：validate(root) 应拼快照根"
    # 校验链保留：非法名仍拒
    try:
        jobs.validate("../etc", root2)
        assert False, "#1：非法名必须仍被拒绝"
    except jobs.JobError:
        pass
    # 校验链保留：symlink 任务目录仍拒
    ln = os.path.join(root2, "20200101-000000_ln")
    os.symlink("/etc", ln)
    try:
        jobs.validate("20200101-000000_ln", root2)
        assert False, "#1：symlink 任务目录必须仍被拒绝"
    except jobs.JobError:
        pass
    os.remove(ln)
    # 快照根下不存在任务仍拒
    try:
        jobs.validate("20200101-000000_nope", root2)
        assert False, "#1：快照根下不存在任务必须仍被拒绝"
    except jobs.JobError:
        pass


# ---------- #1：_mk_thumb / _convert_pnms 显式 root 参数 ----------
def test_mk_thumb_and_convert_pnms_root_param(tmp_path):
    png = _png_bytes()
    name = _mk_job(["A"])
    src_dir = os.path.join(config.get_scan_root(), name)
    root2 = str(tmp_path / "root2")
    d2 = os.path.join(root2, name)
    os.makedirs(os.path.join(d2, ".thumbs"), exist_ok=True)
    shutil.copytree(src_dir, d2, dirs_exist_ok=True)
    with open(os.path.join(d2, "p001.png"), "wb") as f:
        f.write(png)
    with open(os.path.join(d2, "p002.pnm"), "wb") as f:
        f.write(b"P4\n1 1\n\x00")
    scanner._mk_thumb(name, "p001.png", root2)
    assert os.path.exists(os.path.join(d2, ".thumbs", "p001.jpg")), \
        "#1：_mk_thumb(root) 缩略图必须落快照根"
    fake = _fake_script(str(tmp_path / "cvt.sh"), '#!/bin/sh\ncp "$1" "$2"\n')
    old = scanner.CONVERT
    scanner.CONVERT = fake
    try:
        ok, failed = scanner._convert_pnms(name, 1, root2)
        assert ok == 1 and not failed, "#1：_convert_pnms(root) 应转换快照根下 PNM"
        assert os.path.exists(os.path.join(d2, "p002.png")), "#1：PNG 必须落快照根"
        assert not os.path.exists(os.path.join(d2, "p002.pnm")), "#1：成功后 PNM 应删除"
        assert not os.path.exists(os.path.join(src_dir, "p002.png")), \
            "#1：不得写现取根（任务分裂即回归）"
    finally:
        scanner.CONVERT = old


# ---------- #1 核心链路：平板扫描 worker 根快照——转换期间改根，数据仍落旧根 ----------
def test_flatbed_root_snapshot_survives_change(tmp_path):
    png = _png_bytes()
    fx = str(tmp_path / "fx")
    os.makedirs(fx)
    with open(os.path.join(fx, "page.png"), "wb") as f:
        f.write(png)
    root_a = config.get_scan_root()
    root_b = str(tmp_path / "root_b")
    os.makedirs(root_b, exist_ok=True)
    name = _mk_job(["A"])
    sc = _fake_script(str(tmp_path / "scan.sh"), "#!/bin/sh\nsleep 0.2\nexit 0\n")
    # convert 慢 0.9s——保证主线程在转换完成前改根（复现 TOCTOU 窗口内保存）
    cv = _fake_script(str(tmp_path / "cvt.sh"),
                      '#!/bin/sh\nsleep 0.9\ncp "%s/page.png" "$2"\n' % fx)
    old_sc, old_cv = scanner.SCANIMAGE, scanner.CONVERT
    scanner.SCANIMAGE, scanner.CONVERT = sc, cv
    try:
        scanner.scan_flatbed(name)          # 拿锁→快照 root_a→扫描→启动 worker
        time.sleep(0.35)                     # worker 正在 convert sleep 中
        cfg = config.load_admin_cfg()
        cfg["scan_root"] = root_b            # 模拟 409 检查窗漏过的保存
        config.save_admin_cfg(cfg)
        deadline = time.time() + 8
        while time.time() < deadline:
            st = scanner.get_state(name)
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert st["state"] == "done", "#1：快照根下全链路应完成，实际 %s" % st
        assert os.path.exists(os.path.join(root_a, name, "p002.png")), \
            "#1：PNG 必须落扫描启动时快照根（旧根）"
        assert os.path.exists(os.path.join(root_a, name, ".thumbs", "p002.jpg")), \
            "#1：缩略图必须落快照根"
        assert not os.path.exists(os.path.join(root_b, name)), \
            "#1：新根不得出现本任务目录（分裂即回归）"
        meta = jobs.load(name, root_a)
        assert meta["pages"] >= 2, "#1：meta 更新也须用快照根（pages 计入新页）"
    finally:
        scanner.SCANIMAGE, scanner.CONVERT = old_sc, old_cv
