import json
import os
import re
import secrets
import shutil
import threading
import time

from config import get_cleanup_cfg, get_scan_root


class JobError(Exception):
    pass


JOB_RE = re.compile(r"\d{8}-\d{6}[\w\-]*")
# v1.14.2（#10）：页码模型放宽为不限 3 位——p1000+ 也能被识别，不再「看不见就覆盖」
PAGE_RE = re.compile(r"p\d+\.png")


def _page_key(f):
    """v1.14.2（#10）：页序按数字比较——字符串排序在 p10 与 p2 之间会排错。"""
    return int(re.search(r"\d+", f).group())


def safe_slug(s, maxlen=30):
    # v1.14.1（P1-10）：过滤 "." —— JOB_RE 不含点号，否则备注含「..」会生成 validate 永拒的死角任务
    s = re.sub(r'[/\\:*?"<>|.\x00-\x1f]', "", str(s)).strip()[:maxlen]
    return s


def next_page_no(job):
    """v1.14.1（P1-9）：下一页编号 = 现有最大编号 + 1（len+1 在文件空洞时会冲突）。
    v1.14.2（#10）：页码解析不再限 3 位切片，p1000+ 也能算入 max。"""
    nums = [_page_key(f) for f in raw_pages(job)]
    return max(nums, default=0) + 1


# v1.14.1（P1-3）：任务生命周期锁——scan/convert/delete/reorder/cleanup 对同一任务互斥，
# 消除「检查 state → 执行操作」之间的 TOCTOU 竞态窗口。
# v1.14.2（#11）：锁条目随任务删除回收（release_job_lock），不再永久增长。
_job_locks = {}
_job_locks_guard = threading.Lock()


def job_lock(job):
    with _job_locks_guard:
        return _job_locks.setdefault(job, threading.Lock())


def release_job_lock(job):
    """v1.14.2（#11）：任务删除后回收锁条目。只在锁空闲（试探 acquire 成功）时回收；
    仍有线程持有则跳过，等下次删除时机——安全不破坏互斥。"""
    with _job_locks_guard:
        lock = _job_locks.get(job)
        if lock is None:
            return
        if lock.acquire(blocking=False):
            _job_locks.pop(job, None)
            lock.release()


def validate(job):
    if not job or not JOB_RE.fullmatch(job) or ".." in job:
        raise JobError("非法任务名")
    p = os.path.join(get_scan_root(), job)
    # v1.14.4（P1）：拒 symlink 任务目录——scan_root 为 Samba 共享时，LAN 可写用户
    # 可造 job -> 外部目录 链接，借 raw/thumb/PDF/ZIP 越权读服务用户可达的任意文件
    if os.path.islink(p):
        raise JobError("非法任务目录（符号链接）")
    if not os.path.isdir(p):
        raise JobError("任务不存在")
    return p


def check_page_safe(base, fname):
    """v1.14.4（P1）：页面文件安全校验——拒绝 symlink 并验证 realpath 在任务目录内，
    防 page -> 外部文件 链接越权读（供 raw/thumb/PDF 等文件读取入口调用）。"""
    p = os.path.join(base, fname)
    if os.path.islink(p):
        raise JobError("非法页面文件（符号链接）")
    real = os.path.realpath(p)
    if os.path.realpath(base) + os.sep not in real + os.sep:
        raise JobError("页面路径越界")
    return p


def create(remark="", params=None):
    # v1.14：秒级时间戳 + 随机后缀，防同秒并发创建同名任务互相覆盖
    # v1.14.2（#12）：后缀 16bit→24bit + 碰撞换名重试——原 exist_ok=True 碰撞时直接
    # 复用旧目录，save() 会覆盖旧任务 meta
    slug = safe_slug(remark)
    while True:
        name = time.strftime("%Y%m%d-%H%M%S") + "_" + secrets.token_hex(3)
        if slug:
            name += "_" + slug
        assert JOB_RE.fullmatch(name), name   # v1.14.1（P1-10）：生成值必须能被 validate 接受
        try:
            os.makedirs(os.path.join(get_scan_root(), name, ".thumbs"), exist_ok=False)
            break
        except FileExistsError:
            continue                          # v1.14.2（#12）：碰撞则重新随机，绝不复用旧目录
    meta = {"remark": slug, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pages": 0, "params": params or {}}
    save(name, meta)
    return name


def path(job):
    return validate(job)


def load(job):
    try:
        with open(os.path.join(validate(job), "meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise JobError(f"无法读取任务：{e}")


def save(job, meta):
    """v1.14.4（P2）：原子写——临时文件 + fsync + replace，断电/崩溃不再产半截 JSON
    （半截 meta 会让任务从 UI 直接消失）。"""
    p = os.path.join(get_scan_root(), job, "meta.json")
    tmp = "%s.tmp.%d" % (p, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def is_locked(job):
    """任务是否被锁定（锁定任务永不清理、不可删除；示例/重要任务可锁定保护）。"""
    return bool(load(job).get("locked", False))


def set_locked(job, locked):
    meta = load(job)
    meta["locked"] = bool(locked)
    save(job, meta)


def pages(job):
    """已完成页面（v1.14：过滤 0 字节占位文件，#3）。v1.14.2（#10）：数字序，不限 3 位。
    v1.14.4（P1）：过滤 symlink 页面——列表层根除链接页，ZIP/PDF/列表下游全安全。"""
    p = validate(job)
    fs = [f for f in os.listdir(p)
          if PAGE_RE.fullmatch(f) and not os.path.islink(os.path.join(p, f))
          and os.path.getsize(os.path.join(p, f)) > 0]
    return sorted(fs, key=_page_key)


def raw_pages(job):
    """裸页面文件列表（含 0 字节转换中占位，供扫描编号防冲突，v1.14 #3）。"""
    p = validate(job)
    return sorted((f for f in os.listdir(p) if PAGE_RE.fullmatch(f)), key=_page_key)


def page_pairs(job):
    p = validate(job)
    out = []
    for f in pages(job):
        t = os.path.join(p, ".thumbs", f[:-4] + ".jpg")
        out.append((f, f[:-4] + ".jpg" if os.path.exists(t) else None))
    return out


def list_jobs(with_size=False):
    """全部任务（最新在前）。with_size=True 时附带目录大小字节（管理页用）。"""
    out = []
    try:
        names = sorted(os.listdir(get_scan_root()), reverse=True)
    except OSError:
        return out
    for name in names:
        try:
            m = load(name)
            item = {"name": name, "remark": m.get("remark", ""),
                    "created": m.get("created", ""),
                    "pages": len(pages(name)),
                    "locked": bool(m.get("locked", False))}
            if with_size:
                item["size"] = dir_size(validate(name))
            out.append(item)
        except JobError:
            continue
    return out


def delete(job):
    """v1.14.3（P3-11）：公共删除入口自持任务锁——新调用点天然安全，无需调用者记加锁。
    已在锁内的内部路径（api_delete/cleanup 等）请调 _delete_locked()，防重入死锁。"""
    try:
        with job_lock(job):
            _delete_locked(job)
    finally:
        # v1.14.4（P3）：公共入口负责完整生命周期——锁条目同步回收，单独调用不残留
        release_job_lock(job)


def _delete_locked(job):
    if is_locked(job):
        raise JobError("任务已锁定，不能删除")
    shutil.rmtree(validate(job))
    # v1.14.3（P2-6）：删除成功同步清扫描状态——cleanup/管理删除路径同样不残留 state 条目，
    # scanner.state 不再随历史任务无限增长（此前只有用户 DELETE 一处 pop）
    try:
        import scanner   # 局部导入避循环（与 cleanup 同模式）
        scanner.state.pop(job, None)
    except ImportError:
        pass


def dir_size(p):
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def cleanup():
    """v1.10 清理策略（OR 组合：条数/天数/容量任一超限即执行对应清理；锁定任务永不动）。
    返回被删除的任务名列表（供管理页展示报告）。"""
    deleted = []
    cfg = get_cleanup_cfg()
    max_jobs, max_age_days, max_total_mb = cfg["max_jobs"], cfg["max_age_days"], cfg["max_total_mb"]
    if not any([max_jobs, max_age_days, max_total_mb]):
        return deleted

    def try_delete(name):
        try:
            if is_locked(name):
                return False
            jlock = job_lock(name)
            if not jlock.acquire(blocking=False):   # v1.14.1（P1-3）：扫描/转换持锁中，跳过不等待
                return False
            try:
                import scanner   # v1.14：局部导入避循环；正在扫描/转换的任务不清理（#2）
                if scanner.get_state(name)["state"] == "scanning":
                    return False
                _delete_locked(name)   # v1.14.3（P3-11）：本函数已持锁，走无重入版本
                deleted.append(name)
                return True
            finally:
                jlock.release()
                release_job_lock(name)   # v1.14.2（#11）：锁已空闲，回收条目
        except (JobError, OSError):
            return False

    # 1) 超龄清理（从最旧开始，锁定任务自动跳过）
    if max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        for j in reversed(list_jobs()):
            try:
                created_str = j["created"]
                if created_str and time.mktime(time.strptime(created_str, "%Y-%m-%d %H:%M:%S")) < cutoff:
                    try_delete(j["name"])
            except (JobError, ValueError):
                pass

    # 2) 超条数清理（未锁定任务数超限时从最旧开始删）
    if max_jobs > 0:
        live = [j["name"] for j in list_jobs() if not j["locked"]]
        for name in reversed(live):
            if len(live) <= max_jobs:
                break
            if try_delete(name):
                live.remove(name)

    # 3) 超容量清理（未锁定任务总大小超限时从最旧开始删）
    if max_total_mb > 0:
        limit = max_total_mb * 1024 * 1024
        live = [j for j in list_jobs(with_size=True) if not j["locked"]]
        total = sum(j["size"] for j in live)
        for j in reversed(live):
            if total <= limit:
                break
            if try_delete(j["name"]):
                total -= j["size"]

    return deleted
