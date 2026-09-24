import os
import re
import subprocess
import tempfile
import threading
import time

from PIL import Image

import jobs
import device_probe
from config import (CONVERT, DEVICE, SCAN_SOURCE,
                    SCANIMAGE, THUMB_SIZE, get_scan_root)

scan_lock = threading.Lock()          # 扫描仪全局独占锁
state = {}                            # job -> {"state": "scanning|done|error", "msg": str}
_scan_start_time = None               # 当前扫描开始时间戳（float）
_scan_job = None                      # 当前正在扫描的任务名

VALID_DPI = {"75", "150", "200", "300", "400", "600", "1200", "2400"}
VALID_MODE = {"Color", "Gray", "Lineart"}  # 完整模式集；实际支持由 scanimage -A 探测后前端动态过滤
# v1.14.4：超时提为常量（测试注入需要——真等 300s 不现实）
SCAN_CMD_TIMEOUT = 300        # 平板单页 scanimage 超时（秒）
CONVERT_CMD_TIMEOUT = 120     # 单页 PNM→PNG 转换超时（秒）
ADF_CMD_TIMEOUT = 3600        # ADF 批量扫描超时（秒）


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
    必须用 scanimage -L 列出的完整名。解析结果缓存 60 秒，USB 重插后自动刷新。
    注：UI 设备列表用 device_probe 300s 缓存，实际扫描解析用本 60s 缓存——
    刻意双生命周期（P2-15）：短缓存保证 USB 重插后扫描快速恢复，长缓存减少首页刷新探测。"""
    if not name or ":" not in name:
        return name                       # 无后缀名，原样返回
    backend, _, suffix = name.partition(":")
    if suffix.strip():                    # 已带完整路径（libusb:xxx），直接用
        return name
    cache = globals().get("_dev_cache")   # v1.14：按后端前缀分键缓存，多台同后端设备不串号（#15）
    now = time.time()
    if cache and backend in cache and cache[backend][0] > now:
        return cache[backend][1]
    try:
        out = subprocess.run([SCANIMAGE, "-L"], capture_output=True, timeout=15)
        for line in out.stdout.decode(errors="ignore").splitlines():
            # v1.14.1：与 device_probe 统一引号兼容正则（P0-2，含无引号格式）
            m = re.match(r"device\s+(?:[`']([^`']+)[`']|(\S+))", line.strip())
            full = (m.group(1) or m.group(2)) if m else None
            if full and full.startswith(backend + ":"):
                globals().setdefault("_dev_cache", {})[backend] = (now + 60, full)
                return full
    except (OSError, subprocess.SubprocessError):
        pass
    # v1.14.2（#14）：解析不到设备→失效 device_probe 长缓存，下次首页探测强制重刷，
    # USB 重插后 UI 与实际扫描解析恢复一致（双缓存失效钩子）
    try:
        import device_probe
        device_probe.invalidate()
    except ImportError:
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
    """把 ADF batch 产出的 .pnm 全部转成 .png；成功才删源（v1.14.1 P1-4：失败保留供重试）。
    v1.14.2（#3）：返回 (成功数, 失败文件列表)——scanimage 返回码 0 不代表转换全部成功，
    调用方据此判定，部分失败不再被误报为 done。"""
    p = jobs.validate(job)
    ok, failed = 0, []
    for f in os.listdir(p):
        m = re.fullmatch(r"p(\d+)\.pnm", f)      # v1.14.2（#10）：页码模型放宽为不限 3 位
        if m and int(m.group(1)) >= start:
            src = os.path.join(p, f)
            dst = os.path.join(p, f[:-4] + ".png")
            try:
                subprocess.run([CONVERT, src, dst], stderr=subprocess.PIPE,
                               timeout=CONVERT_CMD_TIMEOUT, check=True)
                os.remove(src)              # 转换成功才删 PNM（v1.14.1）
                ok += 1
            except (subprocess.CalledProcessError, OSError):
                failed.append(f)           # 失败保留 PNM 源数据，不再误删（v1.14.1 P1-4）
    return ok, failed


def _mk_thumb(job, fname):
    src = os.path.join(get_scan_root(), job, fname)
    dst = os.path.join(get_scan_root(), job, ".thumbs", fname[:-4] + ".jpg")
    # v1.14.4（P2）：显式 with 关闭句柄——PDF 路径 v1.14.1 已修，此处漏网；
    # ADF 一次几十页时避免 FD 短暂积压
    with Image.open(src) as im:
        im.thumbnail(THUMB_SIZE)
        im.convert("RGB").save(dst, quality=80)


def scan_flatbed(job):
    """平板单页扫描：scanimage 输出 PNM 到 /tmp，扫描阶段持有两把锁，
    扫描成功后由后台线程完成 convert+缩略图。

    v1.14.7：锁取得后立即进入统一 finally 生命周期；next_page_no/mkstemp
    等初始化步骤发生异常时也必须释放 scan_lock + job_lock，不能留下永久 busy。
    """
    global _scan_start_time, _scan_job
    p = _params(job)

    jlock = jobs.job_lock(job)
    scan_acquired = False
    handoff = False
    tmp = None
    out = None
    fname = None
    st = None

    jlock.acquire()
    try:
        if not scan_lock.acquire(blocking=False):
            busy_job = _scan_job or ""
            elapsed = int(time.time() - _scan_start_time) if _scan_start_time else 0
            raise RuntimeError("设备忙，%s 正在扫描（已用 %d 秒），请稍后再试" % (busy_job, elapsed))
        scan_acquired = True

        # 从取得两把锁开始，所有可能抛异常的初始化与扫描代码都在 finally 保护内。
        st = state.setdefault(job, {})
        st.update(state="scanning", msg="平板扫描中…")
        n = jobs.next_page_no(job)
        fname = f"p{n:03d}.png"
        out = os.path.join(get_scan_root(), job, fname)

        # v1.14.6：mkstemp 唯一化中转文件名——旧版可预测的 /tmp/scanweb_{job}_{n}.pnm 可被
        # 本地其他 shell 用户预创建同名 symlink，扫描写入跟随链接覆盖任意可写文件
        _fd, tmp = tempfile.mkstemp(prefix=f"scanweb_{job}_{n:03d}_", suffix=".pnm")
        os.close(_fd)

        # 预占位：先创建空的 .png 占位文件，前端能看到"正在转换"状态
        try:
            open(out, "wb").close()
        except OSError:
            pass

        scan_error = None
        try:
            _scan_start_time = time.time()
            _scan_job = job
            try:
                with open(tmp, "wb") as fh:
                    subprocess.run(_base_cmd(p), stdout=fh, stderr=subprocess.PIPE,
                                   timeout=SCAN_CMD_TIMEOUT, check=True)
            except subprocess.CalledProcessError as e:
                err = (e.stderr or b"").decode(errors="ignore").strip()
                scan_error = err[:300] or f"命令退出码 {e.returncode}"
            except subprocess.TimeoutExpired:
                scan_error = "扫描超时（300 秒），设备可能卡死"
            except OSError as e:
                scan_error = "扫描失败：%s" % str(e)[:250]
            except Exception as e:
                scan_error = "扫描异常：%s" % str(e)[:250]
        finally:
            _scan_job = None
            _scan_start_time = None
            scan_lock.release()
            scan_acquired = False

        if scan_error:
            _rm(tmp)
            tmp = None
            _rm(out)
            st.update(state="error", msg=scan_error)
            raise RuntimeError(scan_error)

        def _convert_worker():
            nonlocal tmp
            try:
                subprocess.run([CONVERT, tmp, out], stderr=subprocess.PIPE,
                               timeout=CONVERT_CMD_TIMEOUT, check=True)
                _mk_thumb(job, fname)
                meta = jobs.load(job)
                meta["pages"] = len(jobs.pages(job))
                jobs.save(job, meta)
                st.update(state="done", msg="扫描完成：%s" % fname)
                _rm(tmp)
                tmp = None
            except Exception:
                bak = tmp
                try:
                    dst = os.path.join(get_scan_root(), job, fname[:-4] + ".pnm")
                    os.replace(tmp, dst)
                    bak = dst
                    tmp = None
                except OSError:
                    pass
                st.update(state="error",
                          msg="后台转换失败：%s，原始数据已保留（%s），可用 convert 手动重转" % (fname, bak))
            finally:
                jlock.release()

        try:
            threading.Thread(target=_convert_worker, daemon=True).start()
            handoff = True
        except BaseException:
            _rm(tmp)
            tmp = None
            _rm(out)
            st.update(state="error", msg="后台转换线程启动失败")
            raise

        return fname

    except BaseException as e:
        if st is not None and st.get("state") == "scanning":
            st.update(state="error", msg="扫描初始化失败：%s" % str(e)[:250])
        raise
    finally:
        if scan_acquired:
            scan_lock.release()
            _scan_job = None
            _scan_start_time = None
        if not handoff:
            if tmp:
                _rm(tmp)
            jlock.release()


def scan_adf(job):
    """ADF 连续扫描：后台线程执行，进度/结果用 get_state 轮询。
    v1.14.1（P1-6）：主线程先探测设备忙——忙则立即返回 "busy"（API 层转 409），
    不再让前端收到 ok:true 之后才异步发现失败。锁由主线程获取、worker 释放。"""
    # v1.14.2（P0-2）：锁顺序统一 job_lock → scan_lock。主线程先拿生命周期锁再拿扫描仪锁，
    # 两锁到手才启动 worker——API 返回 started 时 jlock 已被持有，DELETE/reorder 必然排队，
    # 杜绝「返回 started 后、worker 拿锁前」任务被删的竞态窗口（锁主线程获取、worker 释放）
    jlock = jobs.job_lock(job)
    jlock.acquire()
    if not scan_lock.acquire(blocking=False):
        jlock.release()
        return "busy"

    def worker():
        global _scan_start_time, _scan_job
        # v1.14.5（P1-1）：st 前置 + _params 移入 try——_params 在 try 外时 meta.json 损坏/被删
        # （Samba 可写场景真实可能）会让 worker 线程直接死亡，try/finally 不进入，
        # scan_lock/job_lock 永久泄漏（实测复现：全部扫描 busy + 任务死锁，不重启无解）
        st = state.setdefault(job, {})
        try:
            p = _params(job)
            _scan_start_time = time.time()
            _scan_job = job
            st.update(state="scanning", msg="ADF 连续扫描中…")
            start = jobs.next_page_no(job)   # v1.14.1（P1-9）：max+1 防空洞编号冲突
            pat = os.path.join(get_scan_root(), job, "p%03d.pnm")
            source = p.get("source_name") or SCAN_SOURCE  # 优先用探测到的源名，其次配置回退
            # v1.14.1（P1-7）：fail-closed——探测不到设备能力时拒绝扫描，不再放行任意 source
            if source:
                cap = next((x for x in device_probe.probe()[0]
                            if x["name"] == p["device"] or x["name"].startswith(p["device"])), None)
                if cap is None or not cap.get("sources"):
                    st.update(state="error", msg="无法确认设备进纸源能力，已拒绝扫描（fail-closed）")
                    return
                if source not in cap["sources"]:
                    st.update(state="error", msg="进纸源不受设备支持：%s" % source)
                    return
            cmd = _base_cmd(p)
            if source:                      # 有值才传 --source（无 ADF 设备不传）
                cmd += ["--source", source]
            cmd += ["--batch=" + pat, f"--batch-start={start}"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=ADF_CMD_TIMEOUT)
                _ok_cnt, failed_pnms = _convert_pnms(job, start)   # PNM → PNG（v1.14.2 #3：返回转换统计）
                got = jobs.pages(job)          # 已转 png 的页
                new = [f for f in got if int(re.search(r"\d+", f).group()) >= start]   # v1.14.2（#10）：页码不限 3 位
                for f in new:
                    try:
                        _mk_thumb(job, f)
                    except Exception:
                        pass
                err = (r.stderr or "").strip()[:300]
                # v1.14.1（P1-5）：返回码非 0 不能报「完成」——部分成功也如实报异常
                if r.returncode != 0:
                    if new:
                        st.update(state="error",
                                  msg="scanimage 异常退出（返回码 %s），已保留 %d 页部分结果%s" %
                                      (r.returncode, len(new), ("；" + err) if err else ""))
                    else:
                        st.update(state="error", msg=err or "扫描失败（返回码 %s）" % r.returncode)
                    return
                # v1.14.2（#3）：转换完整性判定——scanimage 返回 0 但部分 PNM 转 PNG 失败时不能报「完成」
                if failed_pnms:
                    st.update(state="error",
                              msg="本次扫描 %d 页中 %d 页转换失败（%s），原始数据已保留可手动重转" %
                                  (len(new) + len(failed_pnms), len(failed_pnms),
                                   ", ".join(failed_pnms)[:200]))
                    return
                if not new:
                    st.update(state="error",
                              msg=err or "未扫描到任何页面，请检查进纸器")
                    return
                meta = jobs.load(job)
                meta["pages"] = len(got)
                jobs.save(job, meta)
                st.update(state="done", msg=f"连续扫描完成，本次 {len(new)} 页")
            except subprocess.TimeoutExpired:
                st.update(state="error", msg="扫描超时（超过 1 小时）")
            except Exception as e:
                st.update(state="error", msg=str(e)[:300])
        except Exception as e:   # v1.14.5（P1-1）：外层兜底——_params/next_page_no/meta 等内层 try 之外的异常
            st.update(state="error", msg=str(e)[:300])
        finally:
            _scan_job = None
            _scan_start_time = None
            scan_lock.release()
            jlock.release()          # v1.14.1（P1-3）

    threading.Thread(target=worker, daemon=True).start()
    return "started"


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
