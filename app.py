import io
import os
import queue as q
import re
import shutil
import subprocess
import threading
import zipfile
from urllib.parse import quote

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template, request, send_file, session,
                   stream_with_context, url_for)
from waitress import serve

import admin
import jobs
import scanner
import time as _time
from config import BIND, CONVERT, MAX_PDF_PAGES, PORT, SCANIMAGE, SECRET, TOKEN
from config import get_scan_root

app = Flask(__name__)
app.secret_key = SECRET
app.register_blueprint(admin.bp)
FNAME_RE = re.compile(r"p\d{3}\.(png|jpg)")


# ---------------- 登录（仅当设置 TOKEN 时启用） ----------------
@app.before_request
def require_login():
    if not TOKEN or session.get("ok") or request.endpoint in ("login", "static"):
        return
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not TOKEN:
        return redirect(url_for("index"))
    err = ""
    if request.method == "POST":
        if request.form.get("password", "") == TOKEN:
            session["ok"] = True
            return redirect(url_for("index"))
        err = "口令错误"
    return render_template("login.html", err=err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.context_processor
def inject():
    return {"authed": bool(TOKEN)}


@app.errorhandler(jobs.JobError)
def job_error(e):
    return "任务不存在或任务名非法", 404


# ---------------- 页面 ----------------
@app.route("/")
def index():
    jobs.cleanup()
    return render_template("index.html", jobs=jobs.list_jobs(),
                           scanimage_ok=os.path.exists(SCANIMAGE),
                           convert_ok=os.path.exists(CONVERT))


@app.route("/job/<job>")
def job_page(job):
    meta = jobs.load(job)
    return render_template("job.html", job=job, meta=meta,
                           pairs=jobs.page_pairs(job))


@app.route("/manual")
def manual():
    return render_template("manual.html")


# ---------------- API ----------------
@app.post("/api/jobs")
def api_create():
    name = jobs.create(
        request.form.get("remark", ""),
        {         "source": request.form.get("source", "flatbed"),
         "source_name": request.form.get("source_name", ""),
         "dpi": request.form.get("dpi", "150"),
         "mode": request.form.get("mode", "Gray"),
         "device": request.form.get("device", ""),
         "crop": bool(request.form.get("crop"))})
    return redirect(url_for("job_page", job=name))


@app.post("/api/jobs/<job>/scan")
def api_scan(job):
    try:
        return jsonify(ok=True, file=scanner.scan_flatbed(job))
    except Exception as e:
        return jsonify(ok=False, msg=str(e)[:300]), 500


@app.post("/api/jobs/<job>/adf")
def api_adf(job):
    if jobs.load(job).get("params", {}).get("source", "flatbed") != "adf":
        return jsonify(ok=False, msg="该任务创建时选择的是平板模式"), 400
    scanner.scan_adf(job)
    return jsonify(ok=True)


@app.get("/api/jobs/<job>/status")
def api_status(job):
    return jsonify(scanner.get_state(job))


@app.get("/api/scan-status")
def api_scan_status():
    """全局扫描状态：当前是否有任务在扫描、扫了多久。供首页/任务页展示并发占用。"""
    return jsonify(scanner.get_scan_status())


@app.delete("/api/jobs/<job>")
def api_delete(job):
    if scanner.get_state(job)["state"] == "scanning":
        return jsonify(ok=False, msg="扫描进行中，请等待完成后再删除"), 409
    scanner.state.pop(job, None)
    jobs.delete(job)
    return jsonify(ok=True)


@app.post("/api/jobs/<job>/reorder")
def api_reorder(job):
    """按指定顺序物理重命名页面文件（两步重命名防冲突）。
    支持 delete=true：从 order 中省略的页面将被删除，剩余页面重新连续编号。"""
    # 排序互斥：扫描进行中禁止排序，防止文件被同时操作
    if scanner.get_state(job)["state"] == "scanning":
        return jsonify(ok=False, msg="扫描进行中，请等待完成后再排序"), 409
    body = request.get_json() or {}
    new_order = body.get("order", [])
    is_delete = body.get("delete", False)
    if not new_order:
        return jsonify(ok=False, msg="顺序列表为空"), 400
    base = jobs.path(job)
    thumb_dir = os.path.join(base, ".thumbs")
    current = jobs.pages(job)
    if is_delete:
        # 删除模式：new_order 是剩余文件（不需要等于 current 的排列）
        # 校验：new_order 中的文件必须都在 current 中
        for f in new_order:
            if f not in current:
                return jsonify(ok=False, msg="页面 %s 不存在" % f), 400
        # 删除不在 new_order 中的文件
        to_delete = [f for f in current if f not in new_order]
        for f in to_delete:
            try:
                os.remove(os.path.join(base, f))
            except OSError:
                pass
            old_thumb = os.path.join(thumb_dir, f[:-4] + ".jpg")
            if os.path.exists(old_thumb):
                try:
                    os.remove(old_thumb)
                except OSError:
                    pass
        # 更新 meta
        meta = jobs.load(job)
        meta["pages"] = len(new_order)
        jobs.save(job, meta)
        if len(new_order) < 2:
            # 只剩 0 或 1 页，无需重编号
            return jsonify(ok=True, deleted=len(to_delete))
        # 继续走重编号流程，target = new_order
        current = new_order[:]
        new_order = new_order[:]  # 保持不变，两步重命名为连续编号
    else:
        # 普通排序：new_order 必须是 current 的排列
        if sorted(new_order) != sorted(current):
            return jsonify(ok=False, msg="顺序列表与实际页面不匹配"), 400
        if new_order == current:
            return jsonify(ok=True, msg="顺序未变化")
    # 防御：清理可能残留的临时文件（上次异常中断遗留）
    for f in os.listdir(base):
        if f.startswith("_tmp_") and f.endswith(".png"):
            try:
                os.remove(os.path.join(base, f))
            except OSError:
                pass
    try:
        for f in os.listdir(thumb_dir):
            if f.startswith("_tmp_") and f.endswith(".jpg"):
                os.remove(os.path.join(thumb_dir, f))
    except OSError:
        pass
    # 第一步：全部重命名为临时名
    for i, old_name in enumerate(current):
        tmp_name = f"_tmp_{i:03d}.png"
        os.rename(os.path.join(base, old_name), os.path.join(base, tmp_name))
        # 缩略图同步
        old_thumb = os.path.join(thumb_dir, old_name[:-4] + ".jpg")
        if os.path.exists(old_thumb):
            os.rename(old_thumb, os.path.join(thumb_dir, f"_tmp_{i:03d}.jpg"))
    # 第二步：临时名 → 目标名（目标文件名 = 新位置编号，内容跟随新顺序）
    for i, new_name in enumerate(new_order):
        old_idx = current.index(new_name)          # 该页面在旧顺序中的位置
        tmp_key = f"_tmp_{old_idx:03d}.png"
        dst_name = f"p{i + 1:03d}.png"             # 新顺序第 i 位 → 文件名编号
        os.rename(os.path.join(base, tmp_key), os.path.join(base, dst_name))
        # 缩略图同步
        tmp_thumb = os.path.join(thumb_dir, f"_tmp_{old_idx:03d}.jpg")
        if os.path.exists(tmp_thumb):
            os.rename(tmp_thumb, os.path.join(thumb_dir, dst_name[:-4] + ".jpg"))
    return jsonify(ok=True)


# 设备探测缓存（5 分钟 TTL，与 admin 页共享）
_DEV_CACHE = {"data": None, "ts": 0}
_DEV_CACHE_SEC = 300

@app.get("/api/devices")
def api_devices():
    """列出设备并探测每台设备支持的扫描模式和进纸源（scanimage -A）。
    5 分钟服务端缓存，避免每次刷新页面都重新探测。"""
    now = _time.time()
    cached = False
    if _DEV_CACHE["data"] is not None and now - _DEV_CACHE["ts"] < _DEV_CACHE_SEC:
        cached = True
    else:
        try:
            r = subprocess.run([SCANIMAGE, "-L"], capture_output=True, text=True, timeout=15)
            dev_names = re.findall(r"device `([^']+)'", r.stdout)
        except Exception:
            dev_names = []

        devs = []
        for dev in dev_names:
            info = {"name": dev, "modes": ["Color", "Gray"], "sources": [], "dpis": ["150", "300", "600"]}
            try:
                a = subprocess.run([SCANIMAGE, "-A", "--format", "pnm", "-d", dev],
                                   capture_output=True, text=True, timeout=15)
                out = a.stdout + a.stderr
                m = re.search(r'--mode\s+([^\[]+)\[', out)
                if m:
                    info["modes"] = [s.strip() for s in m.group(1).split("|")]
                s = re.search(r'--source\s+([^\[]+)\[', out)
                if s:
                    info["sources"] = [src.strip() for src in s.group(1).split("|")]
                r2 = re.search(r'--resolution\s+([^\[]+)\[', out)
                if r2:
                    info["dpis"] = [d.strip().replace("dpi", "") for d in r2.group(1).split("|")]
            except Exception:
                pass
            devs.append(info)
        _DEV_CACHE["data"] = devs
        _DEV_CACHE["ts"] = now

    devs = _DEV_CACHE["data"] or []
    # 附加设备别名和按设备的扫描默认值（每次都读，可能随时改）
    try:
        from config import load_admin_cfg
        cfg = load_admin_cfg()
        aliases = cfg.get("device_alias", {})
        all_defaults = cfg.get("scan_defaults", {})
        for d in devs:
            d["alias"] = aliases.get(d["name"], "")
            d["defaults"] = all_defaults.get(d["name"], {})
    except Exception:
        pass
    return jsonify(devs=devs)


# ---------------- 文件 ----------------
@app.route("/job/<job>/raw/<fname>")
def raw(job, fname):
    if not FNAME_RE.fullmatch(fname) or not fname.endswith(".png"):
        abort(404)
    p = os.path.join(jobs.path(job), fname)
    if not os.path.exists(p):
        abort(404)
    return send_file(p)


@app.route("/job/<job>/thumb/<fname>")
def thumb(job, fname):
    if not FNAME_RE.fullmatch(fname) or not fname.endswith(".jpg"):
        abort(404)
    p = os.path.join(jobs.path(job), ".thumbs", fname)
    if not os.path.exists(p):
        abort(404)
    return send_file(p)


@app.route("/job/<job>/download.zip")
def dl_zip(job):
    base = jobs.path(job)
    files = [(f, os.path.join(base, f)) for f in jobs.pages(job)]
    if not files:
        abort(404)
    qu = q.Queue()
    DONE = object()

    def worker():
        class W:
            """非寻址流 Writer：tell 用计数器，seek 抛异常使 zipfile 走流模式。"""
            def __init__(self):
                self.pos = 0
            def write(self, b):
                self.pos += len(b)
                qu.put(b)
            def tell(self):
                return self.pos
            def seek(self, *a):
                raise OSError("not seekable")
            def flush(self):
                pass
        try:
            with zipfile.ZipFile(W(), "w", zipfile.ZIP_DEFLATED) as zf:
                for arc, path in files:
                    zf.write(path, arcname=arc)
        finally:
            qu.put(DONE)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            chunk = qu.get()
            if chunk is DONE:
                break
            yield chunk

    return Response(stream_with_context(gen()), mimetype="application/zip",
                    headers={"Content-Disposition":
                             f"attachment; filename=\"scan.zip\"; filename*=UTF-8''{quote(job + '.zip')}"})


@app.route("/job/<job>/download.pdf")
def dl_pdf(job):
    from PIL import Image
    base = jobs.path(job)
    files = jobs.pages(job)
    if not files:
        abort(404)
    if len(files) > MAX_PDF_PAGES:
        abort(413)
    imgs = [Image.open(os.path.join(base, f)).convert("RGB") for f in files]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{job}.pdf")


@app.route("/job/<job>/qrcode")
def job_qrcode(job):
    """扫码取件：生成指向本任务页的二维码（可选依赖 qrcode，未安装则返回 404，前端自动隐藏入口）。"""
    try:
        import qrcode
    except ImportError:
        abort(404)
    jobs.load(job)   # 校验任务存在
    url = request.url_root + "job/" + job
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


def deploy_examples():
    """启动时检查内置示例任务是否已在输出目录，不存在则复制。
    示例任务带 locked=True，不会被自动清理；用户可解锁后删除。"""
    root = get_scan_root()
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        return
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")
    if not os.path.isdir(src_dir):
        return
    for name in os.listdir(src_dir):
        s = os.path.join(src_dir, name)
        d = os.path.join(root, name)
        if os.path.isdir(s) and not os.path.exists(d):
            shutil.copytree(s, d)


if __name__ == "__main__":
    scanner.cleanup_tmp_pnms()           # 清理上次异常中断残留的 PNM 临时文件
    jobs.cleanup()                       # 启动时执行一次清理（锁定任务永不动）
    deploy_examples()                    # 输出目录为空时部署内置示例任务
    admin.start_cleanup_scheduler()      # 后台每小时检查一次
    serve(app, host=BIND, port=PORT, threads=8)
