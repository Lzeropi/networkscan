import json
import os
import re
import secrets
import shutil
import time

from config import get_cleanup_cfg, get_scan_root


class JobError(Exception):
    pass


JOB_RE = re.compile(r"\d{8}-\d{6}[\w\-]*")
PAGE_RE = re.compile(r"p\d{3}\.png")


def safe_slug(s, maxlen=30):
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "", str(s)).strip()[:maxlen]
    return s


def validate(job):
    if not job or not JOB_RE.fullmatch(job) or ".." in job:
        raise JobError("非法任务名")
    p = os.path.join(get_scan_root(), job)
    if not os.path.isdir(p):
        raise JobError("任务不存在")
    return p


def create(remark="", params=None):
    # v1.14：秒级时间戳 + 4 位随机后缀，防同秒并发创建同名任务互相覆盖
    name = time.strftime("%Y%m%d-%H%M%S") + "_" + secrets.token_hex(2)
    slug = safe_slug(remark)
    if slug:
        name += "_" + slug
    os.makedirs(os.path.join(get_scan_root(), name, ".thumbs"), exist_ok=True)
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
    with open(os.path.join(get_scan_root(), job, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def is_locked(job):
    """任务是否被锁定（锁定任务永不清理、不可删除；示例/重要任务可锁定保护）。"""
    return bool(load(job).get("locked", False))


def set_locked(job, locked):
    meta = load(job)
    meta["locked"] = bool(locked)
    save(job, meta)


def pages(job):
    p = validate(job)
    return sorted(f for f in os.listdir(p) if PAGE_RE.fullmatch(f))


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
    if is_locked(job):
        raise JobError("任务已锁定，不能删除")
    shutil.rmtree(validate(job))


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
            delete(name)
            deleted.append(name)
            return True
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
