import io
import os
import queue as q
import re
import shutil
import subprocess
import threading
import zipfile

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template, request, send_file, session,
                   stream_with_context, url_for)
from waitress import serve

import admin
import jobs
import scanner
from config import BIND, CONVERT, MAX_PDF_PAGES, PORT, SCANIMAGE, SECRET, TOKEN

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
    abort(404)


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
    """按指定顺序物理重命名页面文件（两步重命名防冲突）。"""
    # 排序互斥：扫描进行中禁止排序，防止文件被同时操作
    if scanner.get_state(job)["state"] == "scanning":
        return jsonify(ok=False, msg="扫描进行中，请等待完成后再排序"), 409
    base = jobs.path(job)
    thumb_dir = os.path.join(base, ".thumbs")
    new_order = request.get_json().get("order", [])
    if not new_order:
        return jsonify(ok=False, msg="顺序列表为空"), 400
    # 校验：new_order 必须是当前页面文件的排列
    current = jobs.pages(job)
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
    tmp_map = {}
    for i, old_name in enumerate(current):
        tmp_name = f"_tmp_{i:03d}.png"
        os.rename(os.path.join(base, old_name), os.path.join(base, tmp_name))
        tmp_map[tmp_name] = old_name
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


@app.get("/api/devices")
def api_devices():
    """列出设备并探测每台设备支持的扫描模式和进纸源（scanimage -A）。"""
    try:
        r = subprocess.run([SCANIMAGE, "-L"], capture_output=True, text=True, timeout=15)
        dev_names = re.findall(r"device `([^`]+)`", r.stdout)
    except Exception:
        dev_names = []

    devs = []
    for dev in dev_names:
        info = {"name": dev, "modes": ["Color", "Gray"], "sources": []}
        try:
            a = subprocess.run([SCANIMAGE, "-A", "-d", dev],
                               capture_output=True, text=True, timeout=15)
            out = a.stdout + a.stderr
            # 解析 --mode 行：如 --mode Gray|Color [Color]
            m = re.search(r'--mode\s+([^\[]+)\[', out)
            if m:
                info["modes"] = [s.strip() for s in m.group(1).split("|")]
            # 解析 --source 行：如 --source Flatbed|ADF [Flatbed]
            s = re.search(r'--source\s+([^\[]+)\[', out)
            if s:
                info["sources"] = [src.strip() for src in s.group(1).split("|")]
        except Exception:
            pass  # 探测失败用默认值
        devs.append(info)
    return jsonify(devs=devs)


# ---------------- 文件 ----------------
@app.route("/job/<job>/raw/<fname>")
def raw(job, fname):
    if not FNAME_RE.fullmatch(fname) or not fname.endswith(".png"):
        abort(404)
    return send_file(os.path.join(jobs.path(job), fname))


@app.route("/job/<job>/thumb/<fname>")
def thumb(job, fname):
    if not FNAME_RE.fullmatch(fname) or not fname.endswith(".jpg"):
        abort(404)
    return send_file(os.path.join(jobs.path(job), ".thumbs", fname))


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
                    headers={"Content-Disposition": f'attachment; filename="{job}.zip"'})


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


if __name__ == "__main__":
    scanner.cleanup_tmp_pnms()           # 清理上次异常中断残留的 PNM 临时文件
    jobs.cleanup()                       # 启动时执行一次清理（锁定任务永不动）
    admin.start_cleanup_scheduler()      # 后台每小时检查一次
    serve(app, host=BIND, port=PORT, threads=8)
