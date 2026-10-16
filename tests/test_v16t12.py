# tests/test_v16t12.py — v1.16.2-tmp02/tmp03：DeepSeek 十五/十六轮审查修复验证
# 十五轮（tmp02）：N1 页面枚举并发删除 / N1+O6 list_jobs 补捕 OSError /
#                 N2 state 复活竞态 / N3 dl_zip 生成器未消费 / N4 fd 双关
# 十六轮（tmp03）：tmp02 的 N2 修复自身引入 _JobLock 1:1 契约破坏（with jlock:
#                 提前弹 entry → 落盘孤儿锁）→ 独立 handle 修复 + entry 同一性/
#                 互斥不变量两项防回归
import os
import shutil
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402
import config          # noqa: E402
import jobs            # noqa: E402
import scanner         # noqa: E402


def _mk_job(params=None):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    scanner._reset_convert_for_test()
    with app_mod._pack_lock:
        app_mod._pack_progress.clear()
    return jobs.create("t", params or {})


def _mk_pnm(path, w=2, h=2, val=b"\x80"):
    """最小 P6 PNM（供 _run_convert_task 真实转换走通）"""
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (w, h) + val * (w * h * 3))


# ==================== N1：页面枚举对并发删除免疫 ====================

def test_iter_pages_survives_mid_delete(monkeypatch):
    """listdir 与 getsize 之间文件被删（Samba 外部或 DELETE 并发——pages() 不持
    jlock）时，_iter_page_files 必须把该文件当不存在跳过，不得抛
    FileNotFoundError 冒到首页/任务页 500。反向验证：恢复无保护 getsize → FAIL。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    open(os.path.join(base, "p001.png"), "wb").write(b"x")
    open(os.path.join(base, "p002.png"), "wb").write(b"yy")
    real_getsize = os.path.getsize

    def racy_getsize(fp):
        # 模拟 TOCTOU：stat 前页面已被外部删除
        if fp.endswith("p002.png"):
            raise FileNotFoundError(fp)
        return real_getsize(fp)

    monkeypatch.setattr(os.path, "getsize", racy_getsize)
    assert jobs.pages(name) == ["p001.png"], "并发删除的页面应被跳过而非抛异常"


def test_list_jobs_skips_oserror(monkeypatch):
    """任务目录半途被删时 pages() 抛 OSError——list_jobs 必须跳过该任务，
    不得让整页 500（原只捕 JobError）。"""
    name = _mk_job()

    def boom(*a, **k):
        raise OSError("目录已被并发删除")

    monkeypatch.setattr(jobs, "pages", boom)
    assert jobs.list_jobs() == [], "OSError 应跳过该任务而非传播"
    assert jobs.list_jobs(with_size=True) == []


# ==================== N2：state 复活竞态（jlock 内复查） ====================

def test_convert_isdir_judged_under_jlock(monkeypatch):
    """「isdir + setdefault」必须在 jlock 持有下执行（与持锁的 api_delete 互斥），
    否则窗口内 setdefault 会复活已删任务的空 state 条目（永不回收）。
    断言 isdir 调用瞬间任务锁处于 locked()——反向验证：恢复锁外三步 → FAIL。"""
    name = _mk_job()
    scanner.state.pop(name, None)   # 模拟重启后队列残留：state 无条目、目录在
    base = os.path.join(config.get_scan_root(), name)
    pnm = os.path.join(base, "q" + "0" * 23)     # 非页名（PAGE_RE 不匹配）
    _mk_pnm(pnm)
    out = os.path.join(base, "p001.png")
    task = (name, "p001.png", pnm, out, "png", None, False)

    real_isdir = os.path.isdir
    observed = []

    def spy_isdir(p):
        # v1.16.2-tmp04（十七轮 P3-1）：直接读 registry 不走 job_lock()——
        # 旧 spy 每 spy 一次 refs+1 却从不 release（靠 conftest 清空兜底才无后果），
        # 违反项目零泄漏纪律；纯观测不消耗 refs。
        e = jobs._job_locks.get(name)
        observed.append(bool(e and e["lock"].locked()))
        return real_isdir(p)

    monkeypatch.setattr(os.path, "isdir", spy_isdir)
    scanner._run_convert_task(task)
    assert observed and all(observed), \
        "isdir 判定必须在任务锁持有下进行（N2：与 api_delete 互斥）"


# ==================== N3：dl_zip 生成器未消费 → 三处收尾点幂等收敛 ====================

def test_dl_zip_unconsumed_close_releases():
    """Response 构造成功但生成器从未被迭代（waitress 在 body 前直接 close——
    客户端响应头后立即断开）时，未启动生成器 close() 不执行 finally：
    call_on_close 必须兜底释放 jlock + 收尾进度，任务才不会删不掉。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    open(os.path.join(base, "p001.png"), "wb").write(b"x" * 10)

    with app_mod.app.test_request_context("/job/%s/download.zip" % name):
        resp = app_mod.dl_zip(name)      # 直调 view：拿到 Response 但不迭代 gen
    resp.close()                          # 模拟 WSGI 关闭（从未消费生成器）

    # jlock 已释放：能立即重新 acquire
    h = jobs.job_lock(name)
    assert h.acquire(blocking=False), "call_on_close 必须兜底释放 jlock（N3）"
    h.release()
    # 进度条目已收尾
    with app_mod._pack_lock:
        rec = app_mod._pack_progress.get(name, {}).get("zip")
        assert rec is not None and rec.get("active") is False, \
            "call_on_close 必须收尾进度条目（active=False）"


# ==================== N4：webp 安全副本 fd 双重关闭 ====================

def test_webp_safe_png_body_failure_no_double_close(monkeypatch):
    """copyfileobj 抛异常时 fd 已被 with 关闭（fdopen 移交）——except 分支不得再
    os.close(fd)：fd 号若被其他线程复用会误关他人 fd。反向验证：恢复无条件
    os.close(fd) → FAIL。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    open(os.path.join(base, "p001.webp"), "wb").write(b"x")   # 0x 占位即可，copy 被打断

    close_calls = []
    real_close = os.close

    def spy_close(fd):
        close_calls.append(fd)
        real_close(fd)

    def boom(*a, **k):
        raise RuntimeError("磁盘故障模拟")

    monkeypatch.setattr(app_mod.shutil, "copyfileobj", boom)
    monkeypatch.setattr(os, "close", spy_close)
    try:
        app_mod._webp_safe_png(base, "p001.webp")
        raise AssertionError("copyfileobj 抛错后应上抛")
    except RuntimeError:
        pass
    assert close_calls == [], \
        "fd 已由 with 关闭（fd_taken），except 分支不得二次 os.close（N4）"
    # 临时副本应被清理
    assert not [f for f in os.listdir("/tmp") if f.startswith("scanweb_pdfsrc_")], \
        "异常路径应清理 scanweb_pdfsrc_ 临时文件"


# ========== 十六轮：tmp02 N2 修复自身的 _JobLock 1:1 契约回归 ==========

def test_convert_task_entries_share_one_lock(monkeypatch):
    """【entry 同一性】_run_convert_task 快照复查（st 为 None 路径）期间及
    计算段中，同任务的所有 job_lock handle（worker 主 handle + 复查独立 handle
    + 并发他线程 handle）必须共享同一 registry entry——tmp02 曾误用
    with jlock:（主 handle），__exit__ 的 release() 把 refs 1→0 提前 pop，
    落盘段自此操作孤儿锁、他线程新建 entry 两锁并存互斥失效。
    反向验证：改回 with jlock: → 计算段他线程拿到新 entry → FAIL。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    pnm = os.path.join(base, "p001.pnm")
    _mk_pnm(pnm)
    out = os.path.join(base, "p001.png")
    root = config.get_scan_root()          # 产品代码 _enqueue_convert 恒传实际 root
    scanner.state.pop(name, None)   # 模拟重启后恢复任务首转（走快照复查路径）

    started = threading.Event()
    gate = threading.Event()
    entry_ids = []

    def slow_convert(src, dst, fmt="png"):
        started.set()               # 快照复查已结束（此时 registry entry 必须健在）
        gate.wait(timeout=10)
        with open(dst, "wb") as f:
            f.write(b"PNG")         # 内容无关紧要，落盘段只看 isdir/replace 路径

    monkeypatch.setattr(scanner, "_convert_pnm", slow_convert)

    def run():
        scanner._run_convert_task((name, "p001.png", pnm, out, "png", root, False))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert started.wait(timeout=5), "转换应已进入计算段"

    # v1.16.2-tmp04（十七轮 P3-2）：entry 存在性断言——快照复查段结束到计算段
    # 之间 entry 必须健在（坏版本此刻已被 with jlock: 提前 pop；仅 id 同一性
    # 断言存在临时插入再删的理论漏网）。
    assert name in jobs._job_locks, \
        "快照复查结束后 entry 必须仍留在 registry"

    # 计算段中：并发他线程取锁（模拟 reorder/delete/PDF 同期到达）——应拿到
    # 同一 entry 且锁此刻空闲（快照复查已释放），用毕归还
    concurrent = jobs.job_lock(name)
    assert concurrent.acquire(blocking=False), "快照复查结束后锁应空闲"
    entry_ids.append(id(concurrent._entry))
    concurrent.release()
    assert name in jobs._job_locks, \
        "计算段中 registry 条目必须健在（坏版本此刻已被 with jlock: 提前 pop）"
    gate.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert len(entry_ids) == 1      # 静态占位：并发 handle 只取一次


def test_convert_landing_blocks_until_lock_released(monkeypatch):
    """【互斥不变量】worker 落盘段的 jlock.acquire() 必须与并发他线程的
    job_lock(job) 真正互斥（同一把底层锁）——坏版本（孤儿锁）下 worker 会
    立即通过而不阻塞，两线程同时进入任务目录。反向验证：改回 with jlock:
    → worker 不等锁直接完成 → FAIL。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    pnm = os.path.join(base, "p001.pnm")
    _mk_pnm(pnm)
    out = os.path.join(base, "p001.png")
    root = config.get_scan_root()
    scanner.state.pop(name, None)

    started = threading.Event()
    gate = threading.Event()

    def slow_convert(src, dst, fmt="png"):
        started.set()
        gate.wait(timeout=10)
        with open(dst, "wb") as f:
            f.write(b"PNG")

    monkeypatch.setattr(scanner, "_convert_pnm", slow_convert)

    errs = []

    def run():
        try:
            scanner._run_convert_task((name, "p001.png", pnm, out, "png", root, False))
        except Exception as e:      # pragma: no cover
            errs.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert started.wait(timeout=5)

    # 他线程持锁 → 放行 worker 进落盘段 → worker 必须阻塞（同一把锁）
    h = jobs.job_lock(name)
    assert h.acquire(blocking=False), "计算段中锁应空闲（测试前提）"
    gate.set()
    t.join(timeout=2.0)
    assert t.is_alive(), \
        "坏版本（孤儿锁）互斥失效：worker 不等他线程锁就直接落盘了（十六轮 N2 回归）"
    h.release()                     # 归还锁
    t.join(timeout=10)
    assert not t.is_alive(), "锁释放后 worker 必须能完成落盘"
    assert not errs, "worker 不应抛异常：%r" % errs
    assert os.path.exists(out), "落盘产物应生成"
