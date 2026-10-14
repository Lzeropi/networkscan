import os
import queue
import re
import shutil
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

# v1.16.0-tmp05（T04-05，审查报告）：ADF 批次活跃标记——同一任务只允许一个
# ADF 批次（入口互斥），批次收尾前不得再启动新 ADF；平板/转换 worker 落盘时
# 凭此不置 done（避免与批次收尾状态交错覆盖）。_adf_batch_lock 保护字典。
_adf_batch = {}
_adf_batch_lock = threading.Lock()


def _adf_active(job):
    with _adf_batch_lock:
        return _adf_batch.get(job, False)

# v1.16.0-tmp02：转换队列化——全局单一常驻 worker + 有界水位，替代「每页一线程」。
# 动机：快设备（<14.5s/页）或 ADF 批量时每页一线程会并行 PIL 打满 ARM32 CPU、
# 且积压无上限。M1005（扫 19.2s > 转 14.5s）不积压，此改造为 ADF/快设备铺路。
# 队列元素：tuple(job, fname, pnm, out, fmt, root)——纯磁盘路径引用，不占内存。
_convert_queue = queue.Queue()
_convert_worker = None                 # 常驻 worker 线程（daemon）
_convert_worker_lock = threading.Lock()  # 幂等启动
MAX_PENDING_PNM = 4                    # v1.16.0-tmp04（P2-6，审查报告）：全局待转换水位。
# v1.16.0-tmp05（T04-06，审查报告）：不再是"平板专用准入"——平板超限拒扫（避免
# 扫完没人转的中间态）；ADF 批次入队也按此水位分批（等待时不持任务锁，无死锁）。
# convert_queue_len 状态字段暴露当前全局待转页数供监控。

VALID_DPI = {"75", "100", "150", "200", "300", "400", "600", "1200", "2400"}  # 宽兜底集（含 100：M1005 探测实报 75|100|150|200|300|600|1200）；精确收口靠 -A 探测 dpi_raw
VALID_MODE = {"Color", "Gray", "Lineart"}  # 完整模式集；实际支持由 scanimage -A 探测后前端动态过滤
# v1.16.0-tmp01：输出格式白名单（fail-closed）。TIFF 不上——浏览器 <img> 不支持，任务页预览会断。
VALID_FORMAT = {"png", "jpg", "webp"}
FMT_EXT = {"png": "png", "jpg": "jpg", "webp": "webp"}   # params.format → 文件扩展名映射
FMT_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
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


def pending_pages(job):
    """v1.16.0-tmp01：转换中页名快照（thumb long-poll 按 stem 匹配，多扩展名）。"""
    with _pending_lock:
        return tuple(_pending.get(job, ()))


def _pending_count():
    """全局转换中页数（排队 + 正在转换）——转换队列水位闸门。"""
    with _pending_lock:
        return sum(len(s) for s in _pending.values())


def _run_convert_task(task):
    """常驻 worker 单页转换：PIL 无锁计算 → jlock 短锁落盘。
    与 v1.15.1-tmp04「每页一线程」版语义完全一致，仅去掉线程创建。
    单页异常全捕获——失败归档 PNM + 置该页 error，队列继续消化下一页。"""
    job, fname, pnm, out, fmt, root, quiet = task
    # v1.16.0-tmp06（DeepSeek P1）：任务已删时不得重建 state 条目——api_delete 已
    # pop，队列残留任务执行到 setdefault 会复活条目（长期运行内存缓涨）。
    st = state.get(job)
    if st is None:
        if os.path.isdir(os.path.join(root or get_scan_root(), job)):
            st = state.setdefault(job, {})
        else:
            st = {}      # 无主替身：写入不进 registry（页面已 404，无人可见）
    jlock = jobs.job_lock(job)
    # PIL 输出落点必须在任务目录内（同文件系统），os.replace 才能原子覆盖。
    tmp_png = out + ".partial"         # 任务目录内隐藏临时名（PAGE_RE 不匹配）
    try:
        _convert_pnm(pnm, tmp_png, fmt)      # 计算全程无锁
    except Exception:
        # v1.16.0-tmp03：源可能已在任务目录（ADF 页 pNNN.pnm）或 /tmp（平板临时）——
        # 两者归档目标同名。仅当源在别处时才 move。
        # v1.16.0-tmp04（P1-1，审查报告）：归档 move 失败时 /tmp 源是唯一可恢复数据，
        # 不得清理；源即归档（ADF）时 move 与删除都不需要——删源=删归档。
        sync_dst = os.path.join(root, job, fname.rsplit(".", 1)[0] + ".pnm")
        same = os.path.abspath(pnm) == os.path.abspath(sync_dst)
        moved = not same          # 待归档；move 成功后置 True（move 已转移，_rm 仅兜底）
        jlock.acquire()
        try:
            bak = sync_dst
            if not same:
                try:
                    shutil.move(pnm, sync_dst)   # /tmp 临时 → 归档进任务目录（跨文件系统安全）
                    moved = True
                except OSError:
                    bak = pnm                    # 归档失败：源留在 /tmp（唯一数据，绝不删）
                    moved = False
            if not quiet:
                st.update(state="error",
                          msg="转换失败：%s，原始数据已保留（%s）" % (fname, bak))
        finally:
            _rm(tmp_png)
            if not same and moved:
                _rm(pnm)          # 归档已成功转移，清 /tmp 残留（move 后通常已不存在）
            _pending_remove(job, fname)
            jlock.release()
        return
    # 落盘段：短锁（rename + thumb + meta + done）
    # v1.16.0-tmp05（T04-01，审查报告）：committed 标志——只有 os.replace 成功
    # （最终输出已提交）才允许删除源 PNM；提交前任何失败，源仍是唯一可恢复数据，
    # 必须保留（不能无条件 finally _rm(pnm)）。缩略图/meta 失败属提交后问题，
    # 不破坏已提交的最终图片，仅记录状态。
    committed = False
    jlock.acquire()
    try:
        if os.path.isdir(os.path.join(root, job)):  # DELETE 可能已删任务
            os.replace(tmp_png, out)    # 同文件系统，原子毫秒级
            committed = True
            try:
                _mk_thumb(job, fname, root)
                meta = jobs.load(job, root)
                meta["pages"] = len(jobs.pages(job, root))
                jobs.save(job, meta, root)
                if not quiet and not _adf_active(job):
                    # v1.16.0-tmp05（T04-05）：ADF 批次在跑/收尾时平板页落盘不置
                    # done——批次收尾统一写最终状态，避免交错覆盖
                    st.update(state="done", msg="扫描完成：%s" % fname)
            except Exception:
                if not quiet:
                    st.update(state="error",
                              msg="转换落盘完成但缩略图/元数据更新失败：%s，原始数据已保留" % fname)
        else:
            _rm(tmp_png)                # 任务已删：转换结果无主，清理
            _rm(pnm)                    # 源同样无主（任务目录已不存在，无恢复意义）
    except Exception:
        if not committed and not quiet:
            st.update(state="error",
                      msg="转换落盘失败：%s，原始数据已保留" % fname)
    finally:
        if not committed:
            _rm(tmp_png)                # 提交前失败：清理 .partial 残件（不涉原图）
        if committed:
            _rm(pnm)                    # 输出已提交（os.replace 成功），源可清
        _pending_remove(job, fname)
        jlock.release()


def _convert_loop():
    """常驻转换 worker 主循环：取任务 → 转换 → 标记完成。永不退出（daemon）。"""
    while True:
        task = _convert_queue.get()
        try:
            _run_convert_task(task)
        except Exception:
            pass                            # 兜底：单任务异常不得杀死 worker
        finally:
            _convert_queue.task_done()


def start_convert_worker():
    """幂等启动唯一常驻转换 worker（app 启动时调用；测试可重复调）。"""
    global _convert_worker
    with _convert_worker_lock:
        if _convert_worker is not None and _convert_worker.is_alive():
            return
        _convert_worker = threading.Thread(target=_convert_loop,
                                           name="convert-worker", daemon=True)
        _convert_worker.start()


def _enqueue_convert(job, fname, pnm, out, fmt, root, quiet=False):
    """登记 pending + 入队。顺序固定：先 _pending_add 后 put——worker 落盘后
    _pending_remove。协调语义（reorder/PDF/ZIP/raw 409、thumb long-poll）全不变。
    v1.16.0-tmp02：入队前惰性确保 worker 存活（生产由 main 启动，测试直接入队也稳）。
    v1.16.0-tmp03：quiet=True 时页级不置 state（ADF 批次由 worker 统一收尾，
    避免中途「done」误报——批次还有页未转就显示完成）。"""
    start_convert_worker()
    _pending_add(job, fname)
    _convert_queue.put((job, fname, pnm, out, fmt, root, quiet))


def _reset_convert_for_test():
    """v1.16.0-tmp02：测试隔离——清空队列未处理项 + 等在途转换收尾 + 清 pending。
    不杀 worker 线程（daemon，队列空时阻塞在 get()，无副作用）。"""
    try:
        while True:
            _convert_queue.get_nowait()
            _convert_queue.task_done()
    except queue.Empty:
        pass
    try:
        _convert_queue.join()          # 等在途任务收尾，防跨用例写入污染
    except Exception:
        pass
    with _pending_lock:
        _pending.clear()


def cleanup_empty_pages():
    """v1.15.1-tmp04：启动时清理崩溃残留的 0 字节占位页（转换中断遗留）。
    真页不可能是 0 字节（PNG 有文件头）；0 字节 + 非 symlink = 安全识别。
    v1.16.0-tmp01：正则扩展覆盖 jpg/webp 扩展名。"""
    try:
        root = get_scan_root()
        for name in os.listdir(root):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                fp = os.path.join(d, f)
                try:
                    if (re.fullmatch(r"p\d+\.(png|jpg|webp)", f) and not os.path.islink(fp)
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
            "format": prm.get("format") if prm.get("format") in VALID_FORMAT else "png",
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
    """v1.16.0-tmp02：启动恢复——/tmp 残留的 scanweb PNM（上次异常中断遗留）
    按文件名归属归档进任务目录 pNNN.pnm（与「转换失败保留 PNM」同语义，用户可
    手动重试/下载）；无主（任务已删/名非法）才删除。返回 (归档数, 删除数)。"""
    import glob
    recovered = removed = 0
    pat = re.compile(r"scanweb_(.+)_(\d+)_[A-Za-z0-9]+\.pnm$")
    for f in glob.glob("/tmp/scanweb_*.pnm"):
        dst = None
        m = pat.match(os.path.basename(f))
        if m:
            job, n = m.group(1), int(m.group(2))
            try:
                dst = os.path.join(jobs.validate(job), "p%03d.pnm" % n)  # 校验存在+名字安全
            except jobs.JobError:
                dst = None
        try:
            if dst:
                shutil.move(f, dst)          # 跨文件系统安全（/tmp tmpfs vs /opt 磁盘）
                recovered += 1
            else:
                os.remove(f)
                removed += 1
        except OSError:
            pass
    return recovered, removed


def recover_task_pnms():
    """v1.16.0-tmp04（P2-9，审查报告）：启动恢复——任务目录中遗留的 pNNN.pnm
    （ADF 批次进程中断 / 上次运行期未及转换）重新入队转换。
    幂等：已有对应格式产物页的跳过（不覆盖有效页面）；已在 pending 登记的跳过
    （防重复入队）；转换失败归档的 PNM 属「保留供重试」语义，同样会被再次入队
    ——转换仍失败则继续保留，不丢数据。返回 (恢复入队数, 跳过数)。"""
    recovered = skipped = 0
    try:
        root = get_scan_root()
        for name in os.listdir(root):
            d = os.path.join(root, name)
            if not os.path.isdir(d) or os.path.islink(d):
                continue
            try:
                job = jobs.validate(name)      # 存在性 + 名字安全（脏目录/链接拒）
            except jobs.JobError:
                continue
            try:
                fmt = _params(job).get("format", "png")
            except Exception:
                fmt = "png"
            ext = FMT_EXT.get(fmt, "png")
            for f in os.listdir(d):
                m = re.fullmatch(r"p(\d+)\.pnm", f)
                if not m or os.path.islink(os.path.join(d, f)):
                    continue
                fname = f[:-4] + "." + ext
                # v1.16.0-tmp05（T04-04，审查报告）：仅 os.path.exists 会误判——
                # 0 字节占位页存在即跳过，随后占位被清，PNM 永远不恢复。
                # 必须要求输出是「非链接 + 非 0 字节」的有效文件才视为已有产物。
                outp = os.path.join(d, fname)
                if (os.path.isfile(outp) and not os.path.islink(outp)
                        and os.path.getsize(outp) > 0):
                    skipped += 1               # 已有有效产物页：不覆盖
                    continue
                if has_pending(job, fname):
                    continue                   # 转换已在队列/进行中
                _enqueue_convert(job, fname, os.path.join(d, f),
                                 os.path.join(d, fname), fmt, root)
                recovered += 1
    except Exception:
        pass
    return recovered, skipped


def _open_no_follow(src):
    """v1.16.0-tmp08（DeepSeek 补充验证 6 实测证实）：O_NOFOLLOW 打开源文件——
    PIL 主分支 Image.open(src) 会跟随 symlink（实测：外部图片被读穿进转换链）。
    fd 交给 PIL 使用（可 seek）；链接源抛 OSError(ELOOP)，走 PNM 保留路径。
    v1.16.0-tmp09（DeepSeek M3）：fdopen 失败（如 EMFILE 内存压力）时关闭
    fd 再上抛——不留 fd 泄漏。
    v1.16.1-tmp02（DeepSeek 七轮 2.3）：fdopen 失败关 fd 的防护与 _fdopen_w
    语义完全重复——直接复用，消除双份实现漂移风险。"""
    fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    return _fdopen_w(fd, "rb")


def _mkstemp_w(path=None, prefix="", suffix=""):
    """v1.16.1-tmp01（DeepSeek 五轮 2.1/六轮同项）：mkstemp→fdopen 调用约定封装。
    历史背景：此前 _convert_pnm/_mk_thumb 的安全副本两处直接 os.fdopen(_sfd)，
    fdopen 抛异常（EMFILE 等）时 _sfd 永久泄漏。现调用方统一走本函数创建 +
    _fdopen_w 包装（失败关 fd 的防护在 _fdopen_w，不在本函数——
    v1.16.1-tmp03（DeepSeek 八轮 2.3）修正 docstring 此前误写"fd 由本函数关闭"）。
    v1.16.1-tmp02（DeepSeek 七轮 2.4）：删 set_inheritable 装饰代码——
    tempfile.mkstemp 自 Python 3.4 起默认 O_CLOEXEC，原调用幂等无功能作用。"""
    if path is None:
        return tempfile.mkstemp(prefix=prefix, suffix=suffix)
    return tempfile.mkstemp(dir=path, prefix=prefix, suffix=suffix)


def _fdopen_w(fd, *a, **kw):
    """v1.16.1-tmp01：fdopen 失败关 fd 再抛（fd 泄漏对称性）。"""
    try:
        return os.fdopen(fd, *a, **kw)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _convert_pnm(src, dst, fmt="png"):
    """PNM→PNG/JPEG/WebP：PIL 优先（ARM32 0.6s vs ImageMagick 11s），PIL 失败回退 ImageMagick。
    v1.51-tmp01：实测 203 ARM32 ImageMagick subprocess fork+exec 开销巨大（11s），
    PIL 同操作 0.6s（快 18x）。PIL 原生支持 PBM/PGM/PPM（scanimage --format pnm 输出）。
    v1.16.0-tmp01：格式分支——PNG 无损 / JPEG q85 / WebP q85（203 实测 300dpi 彩色：
    PNG 14.5s·4.4MB、JPEG 1.2s·0.2MB）。回退安全网仅 PNG：有损格式经 ImageMagick
    重转会二次有损，失败直接走调用方 PNM 保留路径。
    v1.16.0-tmp01-fix：203 盒子 Pillow 编译无 libwebp（features.webp=False）→
    webp 编码必挂。ImageMagick 含 libwebp 0.6.1 → webp 失败回退 CONVERT
    （从原始 PNM 直接编码，无二次有损；速度 ~11s/页 可接受）。"""
    try:
        with _open_no_follow(src) as fh, Image.open(fh) as im:
            if fmt == "jpg":
                im.convert("RGB").save(dst, format="JPEG", quality=85)
            elif fmt == "webp":
                im.convert("RGB").save(dst, format="WEBP", quality=85)
            else:
                im.save(dst, format="PNG")    # v1.15.1-tmp04：显式 PNG（.partial 扩展名 PIL 认不出）
    except Exception:
        cwd_kw = {"cwd": tempfile.gettempdir()}   # v1.16.0-tmp04（P3-10）：回退命令 cwd 固定 /tmp——
        # 假 convert 脚本把参数当输出名时不再把 -quality 等空文件落进仓库根（tmp03 包污染源）
        # v1.16.0-tmp06（DeepSeek P1）+ tmp08（补充验证 6）：src 安全化——入队后、worker
        # 执行前源可能被外部（Samba）替换成 symlink（TOCTOU）。主分支 PIL 经
        # _open_no_follow 拒绝链接；回退分支复制源同样 O_NOFOLLOW（旧 open(src,"rb")
        # 会跟随），ImageMagick 只接触副本。与 _webp_safe_png 同防线。
        # 副本名保留原文件名（basename），便于测试/日志定位是哪一页。
        # v1.16.0-tmp09（DeepSeek M1）：外层 try/except 兜底——源为 symlink 时
        # _open_no_follow 在复制段抛 ELOOP，旧结构 _rm(safe_src) 的 finally
        # 未覆盖该路径，每次泄漏一个空 .pnm 到 /tmp；现在任何异常都先清副本再上抛。
        _sfd, safe_src = _mkstemp_w(
            prefix="scanweb_cvsrc_%s_" % os.path.basename(src), suffix=".pnm")
        try:
            with _fdopen_w(_sfd, "wb") as _dst, _open_no_follow(src) as _src:
                shutil.copyfileobj(_src, _dst)
            try:
                if fmt == "webp":
                    # 盒子 Pillow 无 libwebp → ImageMagick（PNM→WEBP 一次编码，无损源）
                    # v1.16.0-tmp01-fix：dst 是 .partial 扩展名，ImageMagick 按输出扩展名定格式
                    # 会原样输出 PNM（实测 p013.webp 内容为 P6）——必须用 WEBP: 前缀强制格式
                    subprocess.run([CONVERT, safe_src, "-quality", "85", "WEBP:" + dst],
                                   stderr=subprocess.PIPE,
                                   timeout=CONVERT_CMD_TIMEOUT, check=True, **cwd_kw)
                elif fmt == "png":
                    # 仅 PNG 允许 ImageMagick 回退（v1.15.1 历史行为）
                    subprocess.run([CONVERT, safe_src, "PNG:" + dst], stderr=subprocess.PIPE,
                                   timeout=CONVERT_CMD_TIMEOUT, check=True, **cwd_kw)
                else:
                    raise                          # jpg 有损格式无回退意义，走 PNM 保留
                # 注意：elif 链必须互斥——曾因 PNG 回退行挂在 elif 后无条件执行，
                # webp 回退产出真 WebP 后被 PNG 回退覆盖成 PNG（盒子实测踩坑）
            finally:
                _rm(safe_src)
        except Exception:
            _rm(safe_src)
            raise


def _enqueue_pnms(job, start, root=None, fmt="png", wait=True):
    """v1.16.0-tmp03：ADF batch 产出的 .pnm 逐个入队（返回入队页数）。
    替代 v1.16.0-tmp02 前的同步 _convert_pnms——扫描段结束即释放双锁，
    转换由常驻 worker 串行消化，reorder/PDF 在转换期间不再被 jlock 阻塞。
    v1.15（#1）：root 传扫描启动时的根快照——本批 PNM 归档旧根，任务数据不分裂。
    root=None 时沿用 validate 现取（含名校验/symlink 拒）。
    v1.16.0-tmp05：
      - T04-02（审查报告）：symlink 源拒入队——scanimage 后目录可能被外部
        替换成链接，转换阶段不读不可信路径。
      - T04-06（审查报告）：按 MAX_PENDING_PNM 全局水位分批入队——等待时
        双锁已释放（扫描段结束），无"持 worker 必需锁等队列"的死锁；
        返回本批全部入队的 fname 列表，供批次收尾等待自身页（T04-05）。
      - wait=False：超时路径数据完整优先，全量直接入队（不阻塞批次收尾）。"""
    p = os.path.join(root, job) if root else jobs.validate(job)
    ext = FMT_EXT[fmt]                          # 目标扩展名随任务格式
    pages = []
    for f in os.listdir(p):
        m = re.fullmatch(r"p(\d+)\.pnm", f)      # 页码模型不限 3 位
        if m and int(m.group(1)) >= start and not os.path.islink(os.path.join(p, f)):
            pages.append(f)
    pages.sort(key=lambda x: int(re.search(r"\d+", x).group()))
    queued = []
    for f in pages:
        if wait:
            while _pending_count() >= MAX_PENDING_PNM:   # T04-06：水位等待（无锁，批次线程阻塞可接受）
                time.sleep(0.2)
        fname = f[:-4] + "." + ext
        _enqueue_convert(job, fname, os.path.join(p, f),
                         os.path.join(p, fname), fmt, root or get_scan_root(),
                         quiet=True)      # ADF 批次：页级不置 state，批次统一收尾
        queued.append(fname)
    return queued


def _mk_thumb(job, fname, root=None):
    """v1.15（#1）：root 传扫描启动时的根快照（worker 路径）——转换期间改存储路径
    不再使缩略图落新根（任务分裂）；缺省现取，非 worker 调用行为不变。"""
    base = root or get_scan_root()
    src = os.path.join(base, job, fname)
    dst = os.path.join(base, job, ".thumbs", fname.rsplit(".", 1)[0] + ".jpg")   # v1.16.0-tmp01：rsplit 剥扩展（[:-4] 对 .webp 会剩点）
    # v1.14.4（P2）：显式 with 关闭句柄——PDF 路径 v1.14.1 已修，此处漏网；
    # ADF 一次几十页时避免 FD 短暂积压
    try:
        # v1.16.0-tmp09（DeepSeek L2）：_open_no_follow 替换路径 Image.open——
        # os.replace 落盘后微秒级窗口内 src 仍可能被 Samba 替换成 symlink，
        # 与 P1-2/P1-3/_convert_pnm 同防线（fd 不跟随链接）
        with _open_no_follow(src) as fh, Image.open(fh) as im:
            im.thumbnail(THUMB_SIZE)
            im.convert("RGB").save(dst, quality=80)
    except Exception:
        # v1.16.0-tmp01：盒子 Pillow 无 webp 解码 → ImageMagick 生成 thumb
        # （PIL 生成的页才能走 PIL 分支，此处回退兼容 webp 页）
        # v1.16.0-tmp04（P3-10）：cwd 固定 /tmp——测试假脚本把 -resize 当输出名时不再落仓库根
        # v1.16.0-tmp09（DeepSeek L2）：回退分支先复制到受控副本——CONVERT 不接触
        # 任务目录原路径（symlink 防线与 _convert_pnm/_webp_safe_png 一致）
        _tfd, safe_thumb_src = _mkstemp_w(
            prefix="scanweb_thumbsrc_", suffix=".img")
        try:
            with _fdopen_w(_tfd, "wb") as _dst, _open_no_follow(src) as _src:
                shutil.copyfileobj(_src, _dst)
            try:
                subprocess.run([CONVERT, safe_thumb_src, "-resize", "%dx%d>" % THUMB_SIZE,
                                "-quality", "80", dst],
                               stderr=subprocess.PIPE, timeout=CONVERT_CMD_TIMEOUT, check=True,
                               cwd=tempfile.gettempdir())
            finally:
                _rm(safe_thumb_src)
        except Exception:
            _rm(safe_thumb_src)
            raise


def scan_flatbed(job):
    """平板单页扫描（v1.15.1-tmp04 锁分段 + v1.16.0-tmp02 队列化）：POST 同步等
    scanimage 完成返回文件名，PIL 计算由常驻 worker 排队处理，落盘段毫秒级短锁。

    连续扫描间隔只受 scanimage 物理极限（M1005 彩色 ~19s），PIL 转换与下一次
    扫描并行——用户两次点击间无需等 14.5s 的 PNG 压缩。

    主线程：jlock(阻塞) → scan_lock(非阻塞) → 水位检查 → scanimage → 入队 → 释放双锁
    转换 worker：PIL 计算(无锁) → jlock 短锁(落盘+thumb+meta+done) → 释放 jlock
    """
    jlock = jobs.job_lock(job)
    jlock.acquire()                    # 阻塞排队（等上一个扫描段释放）
    if not scan_lock.acquire(blocking=False):
        jlock.release()
        return "busy"
    # v1.16.0-tmp02：转换水位闸门——积压满则拒扫（避免「扫完没人转」的中间态）。
    # 检查在扫描段内（scan_lock 独占，同类检查无并发窗口），仅队列满时触发；
    # M1005（扫 19.2s > 转 14.5s）队列恒空，快设备/ADF 才有意义。
    if _pending_count() >= MAX_PENDING_PNM:
        scan_lock.release()
        jlock.release()
        return "convert_busy"

    global _scan_start_time, _scan_job
    st = state.setdefault(job, {})
    tmp = None
    out = None
    # v1.16.0-tmp04（审查报告·额外项）：锁所有权标志——入队/收尾段抛异常时外层
    # 兜底不得重复释放已释放的锁（重复 release 会 RuntimeError 并掩盖原始异常）。
    scan_lock_held = True
    job_lock_held = True
    try:
        with _scan_start_guard:
            root = get_scan_root()
            p = _params(job)
            _scan_start_time = time.time()
            _scan_job = job
            st.update(state="scanning", msg="平板扫描中…")

        n = jobs.next_page_no(job, root)
        fname = f"p{n:03d}.{FMT_EXT[p['format']]}"    # v1.16.0-tmp01：扩展名随任务格式
        out = os.path.join(root, job, fname)

        _fd, tmp = tempfile.mkstemp(prefix=f"scanweb_{job}_{n:03d}_", suffix=".pnm")
        os.close(_fd)

        # 预占位（编号占位；转换完成 os.replace 覆盖为真实 PNG）
        # v1.16.0-tmp05（T04-02，审查报告）：O_CREAT|O_EXCL 原子排他创建——
        # 绝不跟随已有 symlink 截断外部文件（旧 open(out,"wb") 会跟随链接）；
        # next_page_no 已让链接页占位，此处为 TOCTOU 双保险：冲突则顺延重试。
        created = False
        for _ in range(16):
            try:
                fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0))
                os.close(fd)
                created = True
                break
            except FileExistsError:
                n = jobs.next_page_no(job, root)
                fname = f"p{n:03d}.{FMT_EXT[p['format']]}"
                out = os.path.join(root, job, fname)
        if not created:
            raise RuntimeError("无法安全创建输出页（输出路径被持续占用）")

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

        # 扫描段结束 → scan_lock 立即释放（PIL 计算不需要任何锁）
        scan_lock.release()
        scan_lock_held = False
        _scan_job = None
        _scan_start_time = None

        if scan_error:
            _rm(tmp); tmp = None
            _rm(out)
            st.update(state="error", msg=scan_error)
            jlock.release()
            job_lock_held = False
            raise RuntimeError(scan_error)

        # v1.16.0-tmp02：登记 pending + 入队（PIL 计算由常驻 worker 串行消化）。
        # 入队失败兜底不出现——Queue 无界，水位闸门已在扫描前拦满。
        _enqueue_convert(job, fname, tmp, out, p["format"], root)
        jlock.release()                  # ←← 关键：扫描段结束即释放，不等转换
        job_lock_held = False
        return fname

    except Exception:
        # 统一兜底（v1.16.0-tmp04）：按持有标志清理，锁只释放一次，原始异常不被覆盖
        _scan_job = None
        _scan_start_time = None
        if scan_lock_held:
            scan_lock.release()
            scan_lock_held = False
        if st.get("state") != "error":       # scan_error 路径已带真实原因，不覆盖
            st.update(state="error", msg="扫描收尾异常")
        if tmp:
            _rm(tmp)
        if out:
            _rm(out)
        if job_lock_held:
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
    # v1.16.0-tmp05（T04-05，审查报告）：同任务 ADF 批次原子互斥——上一批仍在
    # 收尾（转换链未收敛）时拒绝再启动，避免两批交错更新同一 state[job]。
    # tmp08：检查+置位合并进同一锁临界区（外层 jlock 已保证，此处再收窄窗口）。
    with _adf_batch_lock:
        if _adf_batch.get(job):
            scan_lock.release()
            jlock.release()
            return "adf_busy"
        _adf_batch[job] = True

    def worker():
        global _scan_start_time, _scan_job
        # v1.14.5（P1-1）：st 前置 + _params 移入 try——_params 在 try 外时 meta.json 损坏/被删
        # （Samba 可写场景真实可能）会让 worker 线程直接死亡，try/finally 不进入，
        # scan_lock/job_lock 永久泄漏（实测复现：全部扫描 busy + 任务死锁，不重启无解）
        st = state.setdefault(job, {})
        released = False      # v1.16.0-tmp03：扫描段结束提前释放双锁（转换交给队列）
        root = None            # tmp06：guard 段前置默认值（finally 判活用它，防 _params 异常时未定义）
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
            # v1.16.0-tmp05（T04-02，审查报告）：scanimage 直接写任务目录——
            # 若目录中存在被替换成 symlink 的页面，batch 连续区间会跟随链接写穿
            # 外部文件。next_page_no 已让链接页占位（起始页避让），但区间中间
            # 仍可能撞上链接页：fail-closed 预检，有链接页即拒绝本次扫描。
            try:
                _links = [f for f in os.listdir(os.path.join(root, job))
                          if re.fullmatch(r"p\d+\.(png|jpg|webp|pnm)", f)
                          and os.path.islink(os.path.join(root, job, f))]
            except OSError:
                _links = []
            if _links:
                st.update(state="error",
                          msg="任务目录存在符号链接页（%s），为防外部文件被覆盖已拒绝扫描，请先清理" %
                              ", ".join(_links)[:200])
                return
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=ADF_CMD_TIMEOUT)
                err = (r.stderr or "").strip()[:300]
                # v1.16.0-tmp04（P1-4，审查报告）：返回码非 0 时也可能已生成部分 PNM——
                # 无论返回码先入队本批已扫页面（不转不显示 = 用户丢页），释放锁后
                # 等转换收敛，由批次汇总统一决定最终状态（scanimage 失败/转换失败/成功）。
                fmt = p.get("format", "png")
                # v1.16.0-tmp06（DeepSeek 审查 P0）：必须先释放双锁、再入队——
                # _enqueue_pnms 的水位等待若持锁，转换 worker 落盘（需 jlock）
                # 永远排不上，pending 不降 = 永久死锁（批次页数 > 水位必触发，
                # 复现：tests/test_v16t06.py::test_adf_over_watermark_no_deadlock）。
                # 释放后入队期间任务可能被 DELETE：listdir 由外层 except 兜底，
                # 已入队页由 _run_convert_task isdir 兜底清理，未入队 PNM 保留可恢复。
                scan_lock.release()
                jlock.release()
                released = True
                _scan_job = None
                _scan_start_time = None
                batch_pages = _enqueue_pnms(job, start, root, fmt)
                if not batch_pages:
                    st.update(state="error", msg=err or "未扫描到任何页面，请检查进纸器")
                    return
                # v1.16.0-tmp05（T04-05）：只等本批入队的页面收敛——其他批次/平板
                # 页的 pending 不掺入本批等待，各批独立汇总、互不覆盖状态
                while any(has_pending(job, f) for f in batch_pages):
                    time.sleep(0.2)
                pdir = os.path.join(root, job)
                ext = FMT_EXT[fmt]
                failed = []
                try:
                    # 残留 PNM（>=start）= 转换失败页（成功页 worker 已删源 + 落 ext 产物）
                    failed = [f for f in os.listdir(pdir)
                              if re.fullmatch(r"p(\d+)\.pnm", f) and int(re.search(r"\d+", f).group()) >= start]
                except OSError:
                    pass
                got = jobs.pages(job, root)
                new = [f for f in got if int(re.search(r"\d+", f).group()) >= start]
                # v1.14.1（P1-5）：返回码非 0 不能报「完成」——部分成功也如实报异常
                if r.returncode != 0:
                    if new:
                        st.update(state="error",
                                  msg="scanimage 异常退出（返回码 %s），已转换 %d 页部分结果%s" %
                                      (r.returncode, len(new), ("；" + err) if err else ""))
                    else:
                        st.update(state="error", msg=err or "扫描失败（返回码 %s）" % r.returncode)
                    return
                if failed:
                    st.update(state="error",
                              msg="本次扫描 %d 页中 %d 页转换失败（%s），原始数据已保留可手动重转" %
                                  (len(new) + len(failed), len(failed), ", ".join(failed)[:200]))
                    return
                if not new:
                    st.update(state="error", msg=err or "未扫描到任何页面，请检查进纸器")
                    return
                meta = jobs.load(job, root)
                meta["pages"] = len(got)
                jobs.save(job, meta, root)
                st.update(state="done", msg=f"连续扫描完成，本次 {len(new)} 页")
            except subprocess.TimeoutExpired:
                # v1.16.0-tmp04（审查报告）：超时时已生成的 PNM 也入队转换（数据完整性
                # 优先，不留「看得见文件却永不转换」的页）；锁由 finally 释放后 worker 消化
                try:
                    _enqueue_pnms(job, start, root, p.get("format", "png"), wait=False)
                except Exception:
                    pass
                st.update(state="error", msg="扫描超时（超过 1 小时），已扫页面将转换保留")
            except Exception as e:
                st.update(state="error", msg=str(e)[:300])
        except Exception as e:   # v1.14.5（P1-1）：外层兜底——_params/next_page_no/meta 等内层 try 之外的异常
            st.update(state="error", msg=str(e)[:300])
        finally:
            _scan_job = None
            _scan_start_time = None
            with _adf_batch_lock:
                _adf_batch.pop(job, None)     # v1.16.0-tmp05（T04-05）：批次结束清活跃标记
            # v1.16.0-tmp06（DeepSeek P1）：批次期间任务被删（tmp05 锁提前释放后
            # DELETE 可执行）——收尾不得在 state 留孤儿条目（内存缓涨）
            try:
                if root is not None and not os.path.isdir(os.path.join(root, job)):
                    state.pop(job, None)
            except OSError:
                pass
            # v1.16.0-tmp03：扫描段已入队并提前释放时不得二次释放（released 标志）
            if not released:
                scan_lock.release()
                jlock.release()      # v1.14.1（P1-3）

    threading.Thread(target=worker, daemon=True).start()
    return "started"


def get_state(job):
    st = dict(state.get(job) or {"state": "idle", "msg": ""})
    try:
        st["pages"] = len(jobs.pages(job))   # 实时从磁盘数页数
    except (jobs.JobError, OSError):
        # v1.16.0-tmp09（DeepSeek L3）：磁盘 I/O 异常也兜底为 0——前端 ADF 轮询
        # 不因 500 中断（旧版只捕 JobError）
        st.setdefault("pages", 0)
    return st


def get_scan_status():
    """全局扫描状态：当前是否有任务在扫描、扫了多久，及后台转换队列水位。
    v1.16.0-tmp02：新增 convert_queue_len（排队+在转页数），前端可显示「后台转换中 N 页」。"""
    qlen = _pending_count()
    if _scan_job and scan_lock.locked():
        elapsed = int(time.time() - _scan_start_time) if _scan_start_time else 0
        return {"busy": True, "job": _scan_job, "elapsed": elapsed, "convert_queue_len": qlen}
    return {"busy": False, "job": "", "elapsed": 0, "convert_queue_len": qlen}
