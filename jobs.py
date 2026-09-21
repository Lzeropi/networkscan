import json
import os
import re
import shutil
import time

from config import SCAN_ROOT, MAX_TOTAL_BYTES, MAX_AGE_DAYS


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
    p = os.path.join(SCAN_ROOT, job)
    if not os.path.isdir(p):
        raise JobError("任务不存在")
    return p


def create(remark="", params=None):
    name = time.strftime("%Y%m%d-%H%M%S")
    slug = safe_slug(remark)
    if slug:
        name += "_" + slug
    os.makedirs(os.path.join(SCAN_ROOT, name, ".thumbs"), exist_ok=True)
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
    with open(os.path.join(SCAN_ROOT, job, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


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


def list_jobs():
    out = []
    try:
        names = sorted(os.listdir(SCAN_ROOT), reverse=True)
    except OSError:
        return out
    for name in names:
        try:
            m = load(name)
            out.append({"name": name, "remark": m.get("remark", ""),
                        "created": m.get("created", ""),
                        "pages": len(pages(name))})
        except JobError:
            continue
    return out


def delete(job):
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
    """先删超龄任务；仍超总配额则按时间从旧到新删。"""
    if MAX_AGE_DAYS > 0:
        cutoff = time.time() - MAX_AGE_DAYS * 86400
        for j in list_jobs():
            try:
                if os.path.getctime(validate(j["name"])) < cutoff:
                    delete(j["name"])
            except JobError:
                pass
    if MAX_TOTAL_BYTES > 0:
        total = dir_size(SCAN_ROOT)
        for j in reversed(list_jobs()):  # list_jobs 最新在前，倒序即从最旧删
            if total <= MAX_TOTAL_BYTES:
                break
            try:
                sz = dir_size(validate(j["name"]))
                delete(j["name"])
                total -= sz
            except JobError:
                pass
