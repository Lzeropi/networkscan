# admin.py — v1.10 管理页（PIN 认证 / 环境依赖 / 设备信息 / 资源监控 / 配置 / 任务管理）
import functools
import hashlib
import os
import re
import subprocess
import sys
import threading
import time

from flask import Blueprint, jsonify, render_template, request, session

import jobs
import scanner
from config import (CONVERT, SCANIMAGE, VERSION, get_cleanup_cfg, get_scan_root,
                    load_admin_cfg, save_admin_cfg)

bp = Blueprint("admin", __name__)

# v1.14：scan_root 系统目录黑名单（防误填系统路径，#13）
_ROOT_BLACKLIST = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64",
                   "/proc", "/root", "/run", "/sbin", "/sys", "/usr",
                   "/var/lib", "/var/log", "/var/cache", "/var/spool")


def _root_blocked(path):
    np = os.path.normpath(path)
    for b in _ROOT_BLACKLIST:
        if np == b or np.startswith(b.rstrip("/") + "/"):
            return True
    return False

# ---------------- PIN 防暴力尝试 ----------------
_pin_fails = {"count": 0, "lock_until": 0.0}   # 全局计数（单人局域网工具，全局锁足够）
_PIN_MAX_FAILS = 5
_PIN_LOCK_SEC = 60


def _pin_locked():
    return time.time() < _pin_fails["lock_until"]


# ---------------- PIN 认证 ----------------
def _hash(pin):
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def admin_ok():
    return bool(session.get("admin_ok"))


def require_admin(f):
    @functools.wraps(f)
    def wrapper(*a, **kw):
        if not admin_ok():
            return jsonify(ok=False, msg="未登录或会话已过期，请输入 PIN"), 401
        return f(*a, **kw)
    return wrapper


@bp.get("/admin")
def admin_page():
    return render_template("admin.html")


@bp.get("/api/admin/status")
def api_status():
    cfg = load_admin_cfg()
    return jsonify(logged=admin_ok(), has_pin=bool(cfg["pin_hash"]), version=VERSION)


@bp.post("/api/admin/login")
def api_login():
    d = request.get_json(silent=True) or {}
    pin = str(d.get("pin", "")).strip()
    cfg = load_admin_cfg()
    if len(pin) < 4:
        return jsonify(ok=False, msg="PIN 至少 4 位"), 400
    # 防暴力：锁定期间直接拒绝（不校验、不提示剩余时间之外的信息）
    if _pin_locked():
        left = int(_pin_fails["lock_until"] - time.time()) + 1
        return jsonify(ok=False, msg="尝试过于频繁，请 %d 秒后再试" % left), 429
    if not cfg["pin_hash"]:
        # 首次使用：设置 PIN（需两次输入一致）
        if pin != str(d.get("confirm", "")).strip():
            return jsonify(ok=False, msg="两次输入不一致，请重试"), 400
        cfg["pin_hash"] = _hash(pin)
        save_admin_cfg(cfg)
        session["admin_ok"] = True
        return jsonify(ok=True, first=True)
    if _hash(pin) == cfg["pin_hash"]:
        _pin_fails["count"] = 0
        _pin_fails["lock_until"] = 0.0
        session["admin_ok"] = True
        return jsonify(ok=True)
    # PIN 错误：计数并按阈值锁定
    _pin_fails["count"] += 1
    if _pin_fails["count"] >= _PIN_MAX_FAILS:
        _pin_fails["lock_until"] = time.time() + _PIN_LOCK_SEC
        _pin_fails["count"] = 0
        return jsonify(ok=False, msg="连续错误次数过多，已锁定 1 分钟"), 429
    return jsonify(ok=False, msg="PIN 错误（已连续错 %d 次）" % _pin_fails["count"]), 403


@bp.post("/api/admin/logout")
def api_logout():
    session.pop("admin_ok", None)
    return jsonify(ok=True)


# ---------------- 资源监控（零依赖，读 /proc 与 /sys） ----------------
def _cpu_ticks():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    return sum(vals), idle


def cpu_usage(interval=0.15):
    """两次采样 /proc/stat 计算总体 CPU 使用率（%）。"""
    try:
        t1, i1 = _cpu_ticks()
        time.sleep(interval)
        t2, i2 = _cpu_ticks()
        dt = t2 - t1
        if dt <= 0:
            return 0.0
        return round((dt - (i2 - i1)) / dt * 100, 1)
    except (OSError, ValueError, IndexError):
        return -1


def mem_info():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0])            # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        return {"total_mb": total // 1024, "used_mb": used // 1024,
                "percent": round(used / total * 100, 1) if total else 0}
    except (OSError, ValueError):
        return {"total_mb": 0, "used_mb": 0, "percent": 0}


def disk_info(path):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return {"total_gb": round(total / 1024 ** 3, 1), "used_gb": round(used / 1024 ** 3, 1),
                "free_gb": round(free / 1024 ** 3, 1),
                "percent": round(used / total * 100, 1) if total else 0}
    except OSError:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "percent": 0}


def cpu_temp():
    """CPU 温度（°C）；无传感器返回 None。
    优先读海思机顶盒的 /proc/msp/pm_cpu（Tsensor 行），通用 Linux 回退 /sys/class/thermal。"""
    # 海思方案：/proc/msp/pm_cpu 里有 "Tsensor: temperature = 59 degree"
    try:
        with open("/proc/msp/pm_cpu") as f:
            m = re.search(r"Tsensor:\s*temperature\s*=\s*(\d+)\s*degree", f.read())
        if m:
            t = int(m.group(1))
            if 0 < t < 120:
                return float(t)
    except (OSError, ValueError):
        pass
    # 通用回退：/sys/class/thermal 各 thermal_zone 取最高
    best = None
    base = "/sys/class/thermal"
    try:
        zones = sorted(z for z in os.listdir(base) if z.startswith("thermal_zone"))
    except OSError:
        return None
    for z in zones:
        try:
            with open(os.path.join(base, z, "temp")) as f:
                t = int(f.read().strip()) / 1000.0
            if 0 < t < 120 and (best is None or t > best):
                best = t
        except (OSError, ValueError):
            continue
    return round(best, 1) if best else None


def uptime_str():
    try:
        with open("/proc/uptime") as f:
            s = int(float(f.read().split()[0]))
        d, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        m, _ = divmod(r, 60)
        return "%d 天 %d 时 %d 分" % (d, h, m)
    except (OSError, ValueError, IndexError):
        return ""


# ---------------- 依赖检测 ----------------
def _cmd_ver(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        lines = (r.stdout or r.stderr).strip().splitlines()
        return lines[0][:100] if lines else "未知版本"
    except Exception:
        return None


def env_report():
    """环境/依赖检测：Python、SANE、ImageMagick、Pillow、用户组、目录可写。"""
    deps = [
        {"name": "Python", "ok": True, "ver": sys.version.split()[0]},
        {"name": "Flask", "ok": True, "ver": ""},
    ]
    try:
        import flask
        deps[1]["ver"] = flask.__version__ if hasattr(flask, "__version__") else getattr(flask, "__version__", "2.x")
    except Exception:
        pass
    deps.append({"name": "SANE scanimage", "ok": os.path.exists(SCANIMAGE),
                 "ver": _cmd_ver([SCANIMAGE, "--version"]) or "未安装"})
    deps.append({"name": "ImageMagick convert", "ok": os.path.exists(CONVERT),
                 "ver": _cmd_ver([CONVERT, "--version"]) or "未安装"})
    try:
        import PIL
        deps.append({"name": "Pillow", "ok": True, "ver": PIL.__version__})
    except Exception:
        deps.append({"name": "Pillow", "ok": False, "ver": "未安装"})

    user = ""
    groups = []
    scan_group_ok = False
    try:
        import grp
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        names = {grp.getgrgid(g).gr_name for g in os.getgroups()}
        names.add(grp.getgrgid(os.getgid()).gr_name)
        groups = sorted(names)
        scan_group_ok = bool(names & {"scanner", "lp"})
    except Exception:
        pass

    sr = get_scan_root()
    sr_exists = os.path.isdir(sr)
    sr_writable = sr_exists and os.access(sr, os.W_OK)
    return {"deps": deps, "user": user, "groups": groups,
            "scan_group_ok": scan_group_ok,
            "scan_root": sr, "scan_root_exists": sr_exists,
            "scan_root_writable": sr_writable}


# ---------------- 设备探测（5 分钟缓存，刷新按钮强制重探） ----------------
_dev_cache = {"ts": 0.0, "data": None}
_DEV_CACHE_SEC = 300


def probe_devices(force=False):
    now = time.time()
    if not force and _dev_cache["data"] is not None and now - _dev_cache["ts"] < _DEV_CACHE_SEC:
        return _dev_cache["data"], True
    devs = []
    cached = False
    if os.path.exists(SCANIMAGE):
        try:
            r = subprocess.run([SCANIMAGE, "-L"], capture_output=True, text=True, timeout=25)
            for m in re.finditer(r"device `([^']+)' is a (.+)", r.stdout + r.stderr):
                dev, desc = m.group(1), m.group(2).strip()
                info = {"name": dev, "desc": desc, "modes": [], "sources": [],
                        "dpi": "", "dpi_raw": [], "scan_type": "未知", "error": ""}
                try:
                    a = subprocess.run([SCANIMAGE, "-A", "-d", dev],
                                        capture_output=True, text=True, timeout=25)
                    ao = a.stdout + a.stderr
                    mm = re.search(r"--mode\s+([^\[]+)\[", ao)
                    if mm:
                        info["modes"] = [s.strip() for s in mm.group(1).split("|")]
                    ms = re.search(r"--source\s+([^\[]+)\[", ao)
                    if ms:
                        info["sources"] = [s.strip() for s in ms.group(1).split("|")]
                    has_adf = any("adf" in s.lower() for s in info["sources"])
                    info["scan_type"] = "平板 + ADF 连续" if has_adf else "仅平板单张"
                    # 完整 DPI 列表（如 --resolution 75|100|150|200|300|600|1200dpi [75]）
                    mr = re.search(r"--resolution\s+([^\[]+)\[", ao)
                    if mr:
                        info["dpi_raw"] = [d.strip().replace("dpi", "")
                                           for d in mr.group(1).split("|")]
                        info["dpi"] = info["dpi_raw"][0] + "–" + info["dpi_raw"][-1] + " dpi" if info["dpi_raw"] else ""
                    else:
                        md = re.search(r"--resolution\s+(\d+)\.\.(\d+)", ao)
                        if md:
                            info["dpi"] = "%s–%s dpi" % (md.group(1), md.group(2))
                except Exception as e:
                    info["error"] = str(e)[:120]
                devs.append(info)
            cached = True
        except Exception:
            pass
    _dev_cache["data"], _dev_cache["ts"] = devs, now
    return devs, cached


@bp.get("/api/admin/devices")
@require_admin
def api_devices():
    force = request.args.get("refresh") == "1"
    devs, cached = probe_devices(force=force)
    return jsonify(devs=devs, cached=cached, scanimage_ok=os.path.exists(SCANIMAGE))


# ---------------- 综合状态（刷新按钮调用；CPU 采样约 0.15s） ----------------
@bp.get("/api/admin/overview")
@require_admin
def api_overview():
    sr = get_scan_root()
    env = env_report()
    return jsonify(
        version=VERSION, uptime=uptime_str(),
        env=env,
        cpu={"percent": cpu_usage(), "temp": cpu_temp()},
        mem=mem_info(),
        disk_root=disk_info("/"),
        disk_scan=disk_info(sr),
        scan_root=sr,
        scan_dir_size=(jobs.dir_size(sr) if os.path.isdir(sr) else 0),
        cleanup=get_cleanup_cfg(),
    )


# ---------------- 配置读写 ----------------
@bp.get("/api/admin/config")
@require_admin
def api_get_config():
    cfg = load_admin_cfg()
    return jsonify(scan_root=get_scan_root(),
                   custom_root=cfg["scan_root"],
                   cleanup=cfg["cleanup"],
                   device_alias=cfg.get("device_alias", {}),
                   scan_defaults=cfg.get("scan_defaults", {}),
                   admin_cfg_path=os.path.basename(__import__("config").ADMIN_CFG_PATH))


@bp.post("/api/admin/config")
@require_admin
def api_save_config():
    d = request.get_json(silent=True) or {}
    cfg = load_admin_cfg()
    # 1) 扫描存储路径
    new_root = str(d.get("scan_root", "")).strip()
    if new_root:
        if not os.path.isabs(new_root):
            return jsonify(ok=False, msg="请输入绝对路径（以 / 开头）"), 400
        if _root_blocked(new_root):   # v1.14：拒绝系统目录（#13）
            return jsonify(ok=False, msg="不允许使用系统目录，请选择数据目录（如 /opt、/mnt、/home 下）"), 400
        current = get_scan_root()
        if os.path.normpath(new_root) != os.path.normpath(current):
            if not os.path.isdir(new_root):
                if not d.get("confirm"):
                    return jsonify(ok=False, need_confirm=True,
                                   msg="路径 %s 不存在，是否创建？" % new_root)
                try:
                    os.makedirs(new_root, exist_ok=True)
                    os.chmod(new_root, 0o755)   # 确保服务 umask 不影响 Samba 等其它用户读取
                except OSError as e:
                    return jsonify(ok=False, msg="创建失败：%s" % e), 400
            if not os.access(new_root, os.W_OK):
                return jsonify(ok=False,
                                msg="服务用户对 %s 无写入权限，请检查目录属主/权限" % new_root), 403
            cfg["scan_root"] = new_root
    # 2) 清理策略（0 = 不启用；任一条件超限即执行对应清理）
    if isinstance(d.get("cleanup"), dict):
        for k in ("max_jobs", "max_age_days", "max_total_mb"):
            if k in d["cleanup"]:
                try:
                    cfg["cleanup"][k] = max(0, int(str(d["cleanup"][k]).strip() or 0))
                except ValueError:
                    cfg["cleanup"][k] = 0
    # 3) 设备别名
    if "device_alias" in d:
        alias = d["device_alias"]
        if alias is None:
            cfg["device_alias"] = {}
        elif isinstance(alias, dict):
            # v1.14：别名滤除尖括号（前端 XSS 后端双保险，#12）
            cfg["device_alias"] = {k: re.sub(r"[<>]", "", str(v))[:50]
                                   for k, v in alias.items() if v}
    # 4) 扫描默认值（按设备存储：{ "设备名": {"dpi":"150","mode":"Gray","crop":false} }）
    if "scan_defaults" in d:
        sd = d["scan_defaults"]
        if sd is None:
            cfg["scan_defaults"] = {}
        elif isinstance(sd, dict):
            cleaned = {}
            for dev_name, dv in sd.items():
                if not isinstance(dv, dict):
                    continue
                cleaned[dev_name] = {
                    "dpi": str(dv.get("dpi", "150")),
                    "mode": str(dv.get("mode", "Gray")),
                    "crop": bool(dv.get("crop", False))
                }
            cfg["scan_defaults"] = cleaned
    save_admin_cfg(cfg)
    deleted = jobs.cleanup()
    return jsonify(ok=True, scan_root=get_scan_root(),
                   cleanup=cfg["cleanup"],
                   device_alias=cfg.get("device_alias", {}),
                   scan_defaults=cfg.get("scan_defaults", {}),
                   deleted=deleted)


# ---------------- 任务管理 ----------------
@bp.get("/api/admin/jobs")
@require_admin
def api_jobs():
    return jsonify(jobs=jobs.list_jobs(with_size=True))


@bp.post("/api/admin/jobs/<job>/lock")
@require_admin
def api_lock(job):
    d = request.get_json(silent=True) or {}
    locked = bool(d.get("locked"))
    jobs.set_locked(job, locked)
    return jsonify(ok=True, locked=jobs.is_locked(job))


@bp.delete("/api/admin/jobs/<job>")
@require_admin
def api_delete(job):
    if scanner.get_state(job)["state"] == "scanning":
        return jsonify(ok=False, msg="扫描进行中，请等待完成后再删除"), 409
    if jobs.is_locked(job):
        return jsonify(ok=False, msg="任务已锁定，请先解锁再删除"), 403
    jobs.delete(job)   # jobs.delete 内部亦有双保险
    return jsonify(ok=True)


# ---------------- 测试扫描 ----------------
@bp.post("/api/admin/testscan")
@require_admin
def api_testscan():
    if not os.path.exists(SCANIMAGE):
        return jsonify(ok=False, msg="scanimage 未安装，无法测试"), 400
    try:
        name = jobs.create("测试扫描", {"source": "flatbed", "dpi": "150", "mode": "Gray",
                                       "device": "", "crop": False, "source_name": ""})
    except Exception as e:
        return jsonify(ok=False, msg="创建测试任务失败：%s" % str(e)[:150]), 500
    try:
        scanner.scan_flatbed(name)
    except Exception as e:
        return jsonify(ok=False, msg="扫描失败：%s" % str(e)[:200], job=name), 500
    return jsonify(ok=True, job=name, pages=len(jobs.pages(name)))


# ---------------- 清理调度线程（每小时 + 外部可立即触发） ----------------
def start_cleanup_scheduler(interval=3600):
    def loop():
        while True:
            time.sleep(interval)
            try:
                jobs.cleanup()
            except Exception:
                pass
    t = threading.Thread(target=loop, daemon=True, name="cleanup-scheduler")
    t.start()
    return t