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
# v1.15.1-tmp04：转换中页登记——锁分段后转换不再持 jlock，reorder/PDF/ZIP/raw
# 需要凭此在 jlock 内识别"该页文件还是 0 字节占位"，避免打穿/混入产物。
# 锁序约定：jlock → _pending_lock（所有调用方一致，无反向嵌套，无死锁）。
# has_pending 必须在 jlock 内调用才原子：jlock 内转换无法落盘，检查结果稳定。
_pending = {}                         # {job: {fname, ...}}
_pending_lock = threading.Lock()
# v1.15.1（P2-1，ChatGPT v1.15 审查建议）：扫描启动闸门——只覆盖「快照+写 state」
# 毫秒级启动段，与 admin.save_config 的「检查+落盘」原子段互斥，消除保存瞬间穿插
# 新扫描的 TOCTOU 残窗（409 判定从此完整）。锁序：scan 侧 jlock→scan_lock→guard；
# admin 侧 guard→admin_cfg_lock——无交叉持有，无死锁。guard 从不覆盖扫描 body
# （最长 300s），保存配置永不等待扫描完成。
_scan_start_guard = threading.Lock()

VALID_DPI = {"75", "150", "200", "300", "400", "600", "1200", "2400"}
VALID_MODE = {"Color", "Gray", "Lineart"}  # 完整模式集；实际支持由 scanimage -A 探测后前端动态过滤
# v1.14.4：超时提为常量（测试注入需要——真等 300s 不现实）
SCAN_CMD_TIMEOUT = 300        # 平板单页 scanimage 超时（秒）
CONVERT_CMD_TIMEOUT = 120     # 单页 PNM→PNG 转换超时（秒）
ADF_CMD_TIMEOUT = 3600        # ADF 批量扫描超时（秒）


def _pending_add(job, fname):
    with _pending_lock:
        _pending.setdefault(job, set()).add(fname)


def _pending_remove(job, fname):
    with _pending_lock:
        s = _pending.get(job)
        if s:
            s.discard(fname)
            if not s:
                _pending.pop(job, None)


def has_pending(job, fname=None):
    """转换中页查询（必须在 jlock 内调用才原子）：fname=None → 该任务是否有
    任何转换中页；指定 fname → 该页是否转换中。reorder/PDF/ZIP/raw 落盘前凭此 409。"""
    with _pending_lock:
        s = _pending.get(job)
        if fname is None:
            return bool(s)
        return fname in (s or ())


def cleanup_empty_pages():
    """v1.15.1-tmp04：启动时清理崩溃残留的 0 字节占位页（转换中断遗留）。
    真页不可能是 0 字节（PNG 有文件头）；0 字节 + 非 symlink = 安全识别。"""
    try:
        root = get_scan_root()
        for name in os.listdir(root):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                fp = os.path.join(d, f)
                try:
                    if (re.fullmatch(r"p\d+\.png", f) and not os.path.islink(fp)
                            and os.path.getsize(fp) == 0):
                        os.remove(fp)
                except OSError:
                    pass
    except Exception:
        pass


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
    必须用 scanimage -L 列出的完整名。解析结果缓存 300 秒（v1.51-tmp03：60→300，
    用户换纸/看结果常超 60s 导致每次扫描重跑 -L 耗 12s），USB 重插后自动刷新。
    注：UI 设备列表用 device_probe 300s 缓存，实际扫描解析用本 300s 缓存——
    刻意双生命周期（P2-15）：缓存统一 300s，后台保活线程每 240s 刷新确保不过期。"""
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
                globals().setdefault("_dev_cache", {})[backend] = (now + 300, full)
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


def _pnm_to_png(src, dst):
    """PNM→PNG：PIL 优先（ARM32 0.6s vs ImageMagick 11s），PIL 失败回退 ImageMagick。
    v1.51-tmp01：实测 203 ARM32 ImageMagick subprocess fork+exec 开销巨大（11s），
    PIL 同操作 0.6s（快 18x）。PIL 原生支持 PBM/PGM/PPM（scanimage --format pnm 输出）。
    回退保安全网：PIL 偶遇异常格式 → ImageMagick 兜底 → 仍失败由调用方保留 PNM 源。"""
    try:
        with Image.open(src) as im:
            im.save(dst)
    except Exception:
        subprocess.run([CONVERT, src, dst], stderr=subprocess.PIPE,
                       timeout=CONVERT_CMD_TIMEOUT, check=True)


def _convert_pnms(job, start, root=None):
    """把 ADF batch 产出的 .pnm 全部转成 .png；成功才删源（v1.14.1 P1-4：失败保留供重试）。
    v1.14.2（#3）：返回 (成功数, 失败文件列表)——scanimage 返回码 0 不代表转换全部成功，
    调用方据此判定，部分失败不再被误报为 done。
    v1.15（#1）：root 传扫描启动时的根快照——转换期间管理员改存储路径时，
    本批 PNM 仍归档到启动时的旧根，任务数据不再分裂（TOCTOU 后果消除）。
    root=None 时沿用 validate 现取（含名校验/symlink 拒），供非 worker 调用。
    v1.51-tmp01：PIL 转换优先（_pnm_to_png），ImageMagick 回退。"""
    p = os.path.join(root, job) if root else jobs.validate(job)
    ok, failed = 0, []
    for f in os.listdir(p):
        m = re.fullmatch(r"p(\d+)\.pnm", f)      # v1.14.2（#10）：页码模型放宽为不限 3 位
        if m and int(m.group(1)) >= start:
            src = os.path.join(p, f)
            dst = os.path.join(p, f[:-4] + ".png")
            try:
                _pnm_to_png(src, dst)
                os.remove(src)              # 转换成功才删 PNM（v1.14.1）
                ok += 1
            except Exception:
                failed.append(f)           # 失败保留 PNM 源数据（v1.14.1 P1-4）
    return ok, failed


def _mk_thumb(job, fname, root=None):
    """v1.15（#1）：root 传扫描启动时的根快照（worker 路径）——转换期间改存储路径
    不再使缩略图落新根（任务分裂）；缺省现取，非 worker 调用行为不变。"""
    base = root or get_scan_root()
    src = os.path.join(base, job, fname)
    dst = os.path.join(base, job, ".thumbs", fname[:-4] + ".jpg")
    # v1.14.4（P2）：显式 with 关闭句柄——PDF 路径 v1.14.1 已修，此处漏网；
    # ADF 一次几十页时避免 FD 短暂积压
    with Image.open(src) as im:
        im.thumbnail(THUMB_SIZE)
        im.convert("RGB").save(dst, quality=80)


def scan_flatbed(job):
    """平板单页扫描（v1.15.1-tmp04 锁分段）：POST 同步等 scanimage 完成返回文件名，
    PIL 计算在后台无锁线程进行，落盘段毫秒级短锁。

    连续扫描间隔只受 scanimage 物理极限（M1005 彩色 ~19s），PIL 转换与下一次
    扫描并行——用户两次点击间无需等 14.5s 的 PNG 压缩。

    主线程：jlock(阻塞) → scan_lock(非阻塞) → scanimage → 释放双锁 → 返回文件名
    转换线程：PIL 计算(无锁) → jlock 短锁(落盘+thumb+meta+done) → 释放 jlock
    """
    jlock = jobs.job_lock(job)
    jlock.acquire()                    # 阻塞排队（等上一个扫描段释放）
    if not scan_lock.acquire(blocking=False):
        jlock.release()
        return "busy"

    global _scan_start_time, _scan_job
    st = state.setdefault(job, {})
    tmp = None
    out = None
    try:
        with _scan_start_guard:
            root = get_scan_root()
            p = _params(job)
            _scan_start_time = time.time()
            _scan_job = job
            st.update(state="scanning", msg="平板扫描中…")

        n = jobs.next_page_no(job, root)
        fname = f"p{n:03d}.png"
        out = os.path.join(root, job, fname)

        _fd, tmp = tempfile.mkstemp(prefix=f"scanweb_{job}_{n:03d}_", suffix=".pnm")
        os.close(_fd)

        # 预占位（编号占位；转换完成 os.replace 覆盖为真实 PNG）
        try:
            open(out, "wb").close()
        except OSError:
            pass

        scan_error = None
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

        # 扫描段结束 → 双锁立即全释放（PIL 计算不需要任何锁）
        scan_lock.release()
        _scan_job = None
        _scan_start_time = None

        if scan_error:
            _rm(tmp); tmp = None
            _rm(out)
            st.update(state="error", msg=scan_error)
            jlock.release()
            raise RuntimeError(scan_error)

        # 转换中登记（reorder/PDF/ZIP/raw 落盘前凭此 409）
        _pending_add(job, fname)
        # 后台转换：PIL 计算无锁 → 落盘段短锁（rename+thumb+meta+done）
        _tmp, _out, _fname, _root = tmp, out, fname, root

        def convert_worker():
            tmp_png = _tmp[:-4] + ".png"        # /tmp 同名 PNG（PIL 输出落点）
            try:
                _pnm_to_png(_tmp, tmp_png)      # 14.5s 计算全程无锁
            except Exception:
                # 转换失败：PNM 归档进任务目录（需短锁）
                jlock.acquire()
                try:
                    bak = _tmp
                    try:
                        dst = os.path.join(_root, job, _fname[:-4] + ".pnm")
                        os.replace(_tmp, dst)
                        bak = dst
                    except OSError:
                        pass
                    st.update(state="error",
                              msg="转换失败：%s，原始数据已保留（%s）" % (_fname, bak))
                finally:
                    _rm(tmp_png)
                    _rm(_tmp)
                    _pending_remove(job, _fname)
                    jlock.release()
                return
            # 落盘段：短锁（rename + thumb + meta + done）
            jlock.acquire()
            try:
                if os.path.isdir(os.path.join(_root, job)):  # DELETE 可能已删任务
                    os.replace(tmp_png, _out)
                    _mk_thumb(job, _fname, _root)
                    meta = jobs.load(job, _root)
                    meta["pages"] = len(jobs.pages(job, _root))
                    jobs.save(job, meta, _root)
                    st.update(state="done", msg="扫描完成：%s" % _fname)
                else:
                    _rm(tmp_png)                # 任务已删：转换结果无主，清理
            except Exception:
                st.update(state="error",
                          msg="转换落盘失败：%s，原始数据已保留" % _fname)
            finally:
                _rm(_tmp)
                _pending_remove(job, _fname)
                jlock.release()

        threading.Thread(target=convert_worker, daemon=True).start()
        jlock.release()                  # ←← 关键：扫描段结束即释放，不等转换
        return fname

    except RuntimeError:
        raise
    except Exception as e:
        # 兜底：scan_lock 释放前的异常（_params / mkstemp 等）
        _scan_job = None
        _scan_start_time = None
        scan_lock.release()
        st.update(state="error", msg=str(e)[:300])
        if tmp:
            _rm(tmp)
        if out:
            _rm(out)
        jlock.release()
        raise


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
            # v1.15.1（P2-1）：启动闸门——[快照+写 state] 与 save_config「检查+落盘」互斥
            # （毫秒级，ADF 批量 body 在 guard 外）；v1.15（#1）：worker 全程根快照——
            # 扫描期间改存储路径不再分裂任务数据（本批完整落旧根）
            with _scan_start_guard:
                root = get_scan_root()
                p = _params(job)
                _scan_start_time = time.time()
                _scan_job = job
                st.update(state="scanning", msg="ADF 连续扫描中…")
            start = jobs.next_page_no(job, root)   # v1.14.1（P1-9）：max+1 防空洞编号冲突
            pat = os.path.join(root, job, "p%03d.pnm")
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
                _ok_cnt, failed_pnms = _convert_pnms(job, start, root)   # PNM → PNG（v1.14.2 #3：返回转换统计）
                got = jobs.pages(job, root)          # 已转 png 的页
                new = [f for f in got if int(re.search(r"\d+", f).group()) >= start]   # v1.14.2（#10）：页码不限 3 位
                for f in new:
                    try:
                        _mk_thumb(job, f, root)
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
                meta = jobs.load(job, root)
                meta["pages"] = len(got)
                jobs.save(job, meta, root)
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
