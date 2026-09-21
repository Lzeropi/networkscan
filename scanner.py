import os
import re
import subprocess
import threading

from PIL import Image

import jobs
from config import (CONVERT, DEVICE, SCAN_ROOT, SCAN_SOURCE,
                    SCANIMAGE, THUMB_SIZE)

scan_lock = threading.Lock()          # 扫描仪全局独占锁
state = {}                            # job -> {"state": "scanning|done|error", "msg": str}

VALID_DPI = {"75", "150", "200", "300", "400", "600", "1200", "2400"}
VALID_MODE = {"Color", "Gray"}  # M1005 只支持这两种模式（scanimage -A 确认无 Lineart）


def _params(job):
    prm = jobs.load(job).get("params", {})
    dpi, mode = prm.get("dpi"), prm.get("mode")
    return {"dpi": dpi if dpi in VALID_DPI else "150",
            "mode": mode if mode in VALID_MODE else "Gray",
            "device": prm.get("device") or DEVICE,
            "crop": bool(prm.get("crop"))}


def _base_cmd(p):
    cmd = [SCANIMAGE, "-d", p["device"], "--mode", p["mode"],
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
    src = os.path.join(SCAN_ROOT, job, fname)
    dst = os.path.join(SCAN_ROOT, job, ".thumbs", fname[:-4] + ".jpg")
    im = Image.open(src)
    im.thumbnail(THUMB_SIZE)
    im.convert("RGB").save(dst, quality=80)


def scan_flatbed(job):
    """平板单页扫描：scanimage 输出 PNM 到 /tmp，convert 转 PNG 进任务目录。"""
    p = _params(job)
    with scan_lock:
        n = len(jobs.pages(job)) + 1          # 以磁盘实际页数为准，防编号冲突
        fname = f"p{n:03d}.png"
        out = os.path.join(SCAN_ROOT, job, fname)
        tmp = os.path.join("/tmp", f"scanweb_{job}_{n:03d}.pnm")
        try:
            with open(tmp, "wb") as fh:
                subprocess.run(_base_cmd(p), stdout=fh, stderr=subprocess.PIPE,
                               timeout=300, check=True)
            subprocess.run([CONVERT, tmp, out], stderr=subprocess.PIPE,
                           timeout=120, check=True)
            _mk_thumb(job, fname)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode(errors="ignore").strip()
            raise RuntimeError(err[:300] or f"命令退出码 {e.returncode}")
        except Exception:
            raise
        finally:
            _rm(tmp)
        meta = jobs.load(job)
        meta["pages"] = len(jobs.pages(job))
        jobs.save(job, meta)
        return fname


def scan_adf(job):
    """ADF 连续扫描：后台线程执行，进度/结果用 get_state 轮询。"""
    def worker():
        p = _params(job)
        with scan_lock:
            st = state.setdefault(job, {})
            st.update(state="scanning", msg="ADF 连续扫描中…")
            start = len(jobs.pages(job)) + 1
            pat = os.path.join(SCAN_ROOT, job, "p%d.pnm")
            cmd = _base_cmd(p)
            if SCAN_SOURCE:                    # 只在有值时传 --source（M1005 无 ADF，默认空）
                cmd += ["--source", SCAN_SOURCE]
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

    threading.Thread(target=worker, daemon=True).start()


def get_state(job):
    st = dict(state.get(job) or {"state": "idle", "msg": ""})
    try:
        st["pages"] = len(jobs.pages(job))   # 实时从磁盘数页数
    except jobs.JobError:
        st.setdefault("pages", 0)
    return st
