import os
import re
import subprocess
import threading
import time

from PIL import Image

import jobs
from config import (CONVERT, DEVICE, SCAN_SOURCE,
                    SCANIMAGE, THUMB_SIZE, get_scan_root)

scan_lock = threading.Lock()          # 扫描仪全局独占锁
state = {}                            # job -> {"state": "scanning|done|error", "msg": str}
_scan_start_time = None               # 当前扫描开始时间戳（float）
_scan_job = None                      # 当前正在扫描的任务名

VALID_DPI = {"75", "150", "200", "300", "400", "600", "1200", "2400"}
VALID_MODE = {"Color", "Gray", "Lineart"}  # 完整模式集；实际支持由 scanimage -A 探测后前端动态过滤


def _params(job):
    prm = jobs.load(job).get("params", {})
    dpi, mode = prm.get("dpi"), prm.get("mode")
    return {"dpi": dpi if dpi in VALID_DPI else "150",
            "mode": mode if mode in VALID_MODE else "Gray",
            "device": prm.get("device") or DEVICE,
            "crop": bool(prm.get("crop")),
            "source_name": prm.get("source_name", "")}


def _resolve_device(name):
    """把短设备名（如 'hpljm1005:'）解析为完整设备名（如 'hpljm1005:libusb:001:003'）。
    hi3798mv100 上 hpaio 后端不接受纯后缀名（open 报 Invalid argument），
    必须用 scanimage -L 列出的完整名。解析结果缓存 60 秒，USB 重插后自动刷新。"""
    if not name or ":" not in name:
        return name                       # 无后缀名，原样返回
    backend, _, suffix = name.partition(":")
    if suffix.strip():                    # 已带完整路径（libusb:xxx），直接用
        return name
    cache = globals().get("_dev_cache")
    now = time.time()
    if cache and cache[0] > now and cache[1].startswith(backend + ":"):
        return cache[1]                   # 缓存有效且同后端
    try:
        out = subprocess.run([SCANIMAGE, "-L"], capture_output=True, timeout=15)
        for line in out.stdout.decode(errors="ignore").splitlines():
            m = re.match(r"device [`']([^`']+)[`']", line.strip())
            if m and m.group(1).startswith(backend + ":"):
                globals()["_dev_cache"] = (now + 60, m.group(1))
                return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return name                           # 解析失败退回原名（由 scanimage 报错）


def _base_cmd(p):
    cmd = [SCANIMAGE, "-d", _resolve_device(p["device"]),
           "--format", "pnm",             # 显式指定格式，消除"Output format is not set"警告
           "--mode", p["mode"],
           "--resolution", p["dpi"]]
    if p["crop"]:                       # 可选：按 A4 毫米尺寸裁边（默认不裁）
        cmd += ["-x", "210", "-y", "297"]
    return cmd


def _rm(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def cleanup_tmp_pnms():
    """服务启动时清理 /tmp 中残留的 scanweb PNM 临时文件（上次异常中断遗留）。"""
    import glob
    removed = 0
    try:
        for f in glob.glob("/tmp/scanweb_*.pnm"):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
    except Exception:
        pass
    return removed


def _convert_pnms(job, start):
    """把 ADF batch 产出的 .pnm 全部转成 .png 并删除原文件。"""
    p = jobs.validate(job)
    for f in os.listdir(p):
        m = re.fullmatch(r"p(\d{3})\.pnm", f)
        if m and int(m.group(1)) >= start:
            src = os.path.join(p, f)
            dst = os.path.join(p, f[:-4] + ".png")
            try:
                subprocess.run([CONVERT, src, dst], stderr=subprocess.PIPE,
                               timeout=120, check=True)
                os.remove(src)
            except (subprocess.CalledProcessError, OSError):
                _rm(src)  # 转换失败删掉 pnm 垃圾文件


def _mk_thumb(job, fname):
    src = os.path.join(get_scan_root(), job, fname)
    dst = os.path.join(get_scan_root(), job, ".thumbs", fname[:-4] + ".jpg")
    im = Image.open(src)
    im.thumbnail(THUMB_SIZE)
    im.convert("RGB").save(dst, quality=80)


def scan_flatbed(job):
    """平板单页扫描：scanimage 输出 PNM 到 /tmp 后立即释放锁，
    convert+缩略图在后台线程执行，不阻塞下一次扫描。
    扫描仪忙时立即返回提示，不阻塞等待。"""
    global _scan_start_time, _scan_job
    p = _params(job)
    if not scan_lock.acquire(blocking=False):
        busy_job = _scan_job or ""
        elapsed = int(time.time() - _scan_start_time) if _scan_start_time else 0
        raise RuntimeError("设备忙，%s 正在扫描（已用 %d 秒），请稍后再试" % (busy_job, elapsed))
    n = len(jobs.pages(job)) + 1          # 以磁盘实际页数为准，防编号冲突
    fname = f"p{n:03d}.png"
    out = os.path.join(get_scan_root(), job, fname)
    tmp = os.path.join("/tmp", f"scanweb_{job}_{n:03d}.pnm")

    # 预占位：先创建空的 .png 占位文件，前端能看到"正在转换"状态
    try:
        open(out, "wb").close()
    except OSError:
        pass

    # 阶段 1：scanimage 扫描（持锁）
    scan_error = None
    try:
        _scan_start_time = time.time()
        _scan_job = job
        try:
            with open(tmp, "wb") as fh:
                subprocess.run(_base_cmd(p), stdout=fh, stderr=subprocess.PIPE,
                               timeout=300, check=True)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode(errors="ignore").strip()
            scan_error = err[:300] or f"命令退出码 {e.returncode}"
            _rm(tmp)
            _rm(out)                       # 清空占位文件
    finally:
        _scan_job = None
        _scan_start_time = None
        scan_lock.release()

    if scan_error:
        raise RuntimeError(scan_error)

    # 阶段 2：后台转换 PNM → PNG + 缩略图（不持锁，不阻塞下一次扫描）
    def _convert_worker():
        try:
            subprocess.run([CONVERT, tmp, out], stderr=subprocess.PIPE,
                           timeout=120, check=True)
            _mk_thumb(job, fname)
            meta = jobs.load(job)
            meta["pages"] = len(jobs.pages(job))
            jobs.save(job, meta)
        except Exception:
            pass                           # 转换失败保留 PNM，下次启动时 cleanup_tmp_pnms 清理
        finally:
            _rm(tmp)

    threading.Thread(target=_convert_worker, daemon=True).start()
    return fname


def scan_adf(job):
    """ADF 连续扫描：后台线程执行，进度/结果用 get_state 轮询。"""
    def worker():
        global _scan_start_time, _scan_job
        p = _params(job)
        if not scan_lock.acquire(blocking=False):
            st = state.setdefault(job, {})
            busy_job = _scan_job or ""
            elapsed = int(time.time() - _scan_start_time) if _scan_start_time else 0
            st.update(state="error", msg="设备忙，%s 正在扫描（已用 %d 秒），请稍后再试" % (busy_job, elapsed))
            return
        try:
            _scan_start_time = time.time()
            _scan_job = job
            st = state.setdefault(job, {})
            st.update(state="scanning", msg="ADF 连续扫描中…")
            start = len(jobs.pages(job)) + 1
            pat = os.path.join(get_scan_root(), job, "p%03d.pnm")
            source = p.get("source_name") or SCAN_SOURCE  # 优先用探测到的源名，其次配置回退
            cmd = _base_cmd(p)
            if source:                      # 有值才传 --source（无 ADF 设备不传）
                cmd += ["--source", source]
            cmd += ["--batch=" + pat, f"--batch-start={start}"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                _convert_pnms(job, start)            # PNM → PNG（hpljm1005 后端不支持直接输出 PNG）
                got = jobs.pages(job)          # 已转 png 的页
                new = [f for f in got if int(f[1:4]) >= start]
                for f in new:
                    try:
                        _mk_thumb(job, f)
                    except Exception:
                        pass
                if not new:
                    st.update(state="error",
                              msg=(r.stderr or "").strip()[:300] or "未扫描到任何页面，请检查进纸器")
                    return
                meta = jobs.load(job)
                meta["pages"] = len(got)
                jobs.save(job, meta)
                st.update(state="done", msg=f"连续扫描完成，本次 {len(new)} 页")
            except subprocess.TimeoutExpired:
                st.update(state="error", msg="扫描超时（超过 1 小时）")
            except Exception as e:
                st.update(state="error", msg=str(e)[:300])
        finally:
            _scan_job = None
            _scan_start_time = None
            scan_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def get_state(job):
    st = dict(state.get(job) or {"state": "idle", "msg": ""})
    try:
        st["pages"] = len(jobs.pages(job))   # 实时从磁盘数页数
    except jobs.JobError:
        st.setdefault("pages", 0)
    return st


def get_scan_status():
    """全局扫描状态：当前是否有任务在扫描、扫了多久。供首页/任务页展示。"""
    if _scan_job and scan_lock.locked():
        elapsed = int(time.time() - _scan_start_time) if _scan_start_time else 0
        return {"busy": True, "job": _scan_job, "elapsed": elapsed}
    return {"busy": False, "job": "", "elapsed": 0}
