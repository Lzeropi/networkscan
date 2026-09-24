# device_probe.py — v1.14 设备探测公共模块（app.py 与 admin.py 共用，单一缓存）
# 背景（#11）：此前普通页与管理页各有一套 scanimage -L/-A 解析实现，字段与超时不一致。
# 合并为超集字段：name/desc/modes/sources/dpis/dpi_raw/dpi/scan_type/error，
# app.js 依赖 dpis/modes/sources，admin.js 依赖 dpi_raw/desc/scan_type/error，此超集全覆盖。
import os
import re
import subprocess
import threading
import time

from config import SCANIMAGE

_cache = {"ts": 0.0, "data": None, "dirty": False}
_CACHE_SEC = 300          # 5 分钟缓存，与 v1.13 行为一致
_probe_lock = threading.Lock()   # v1.14.6：并发 miss 去重——避免 waitress 多线程同时跑多个 scanimage -L/-A


def invalidate():
    """v1.14.2（#14）：失效钩子——scanner._resolve_device 解析不到设备时调用，
    下次 probe() 强制重探。USB 重插后 UI 列表与实际扫描解析经此恢复一致。
    v1.14.8（C）：改置 dirty 标志而非直接清缓存——若持锁清理，扫描线程恰逢 probe
    在跑（-L/-A 各最长 25s）会阻塞等锁；置位后由 probe 锁内取走并强制重探，语义等价。"""
    _cache["dirty"] = True


def probe(force=False):
    """探测全部扫描设备及其能力。返回 (devs, cached)。
    force=True 时强制重新探测（管理页「重新探测」按钮）。
    探测失败记入单设备 error 字段（沿用管理页行为）；DPI 列表失败给空，
    前端 app.js/admin.js 各有 fallback 默认值。超时取 ARM 慢值 25s。"""
    with _probe_lock:   # v1.14.6：锁内二次查缓存——并发 miss 时首个线程探测完写缓存，
        # 等待线程复用结果，不再各自跑一遍 scanimage（waitress 8 线程上限下避免进程风暴）
        dirty, _cache["dirty"] = _cache["dirty"], False   # v1.14.8（C）：锁内取走失效标志
        now = time.time()
        if not force and not dirty and _cache["data"] is not None and now - _cache["ts"] < _CACHE_SEC:
            return _cache["data"], True
        devs = []
        if os.path.exists(SCANIMAGE):
            try:
                r = subprocess.run([SCANIMAGE, "-L"], capture_output=True, text=True, timeout=25)
                # v1.14.1：兼容三种引号风格（P0-2）——HP 为 `name'，epkowa 为 'name'，
                # pixma/airscan 可能无引号；写死任一种会导致其他后端设备列表为空
                for m in re.finditer(r"device\s+(?:[`']([^`']+)[`']|(\S+))\s+is\s+a\s+(.*)",
                                     r.stdout + r.stderr):
                    dev, desc = (m.group(1) or m.group(2)), (m.group(3) or "").strip()
                    info = {"name": dev, "desc": desc, "modes": [], "sources": [],
                            "dpis": [], "dpi": "", "dpi_raw": [], "scan_type": "未知", "error": ""}
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
                        info["scan_type"] = ("平板 + ADF 连续"
                                             if any("adf" in s.lower() for s in info["sources"])
                                             else "仅平板单张")
                        # 完整 DPI 列表（如 --resolution 75|100|150|200|300|600|1200dpi [75]）
                        mr = re.search(r"--resolution\s+([^\[]+)\[", ao)
                        if mr:
                            info["dpi_raw"] = [d.strip().replace("dpi", "")
                                               for d in mr.group(1).split("|")]
                            info["dpis"] = list(info["dpi_raw"])
                            info["dpi"] = (info["dpi_raw"][0] + "–" + info["dpi_raw"][-1] + " dpi"
                                          if info["dpi_raw"] else "")
                        else:
                            md = re.search(r"--resolution\s+(\d+)\.\.(\d+)", ao)
                            if md:
                                info["dpi"] = "%s–%s dpi" % (md.group(1), md.group(2))
                    except Exception as e:
                        info["error"] = str(e)[:120]
                    devs.append(info)
            except Exception:
                pass
        _cache["data"], _cache["ts"] = devs, now
        return devs, False