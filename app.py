import io
import os
import queue as q
import re
import subprocess
import threading
import zipfile

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template, request, send_file, session,
                   stream_with_context, url_for)
from waitress import serve

import jobs
import scanner
from config import BIND, CONVERT, MAX_PDF_PAGES, PORT, SCANIMAGE, SECRET, TOKEN

app = Flask(__name__)
app.secret_key = SECRET
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


@app.delete("/api/jobs/<job>")
def api_delete(job):
    if scanner.get_state(job)["state"] == "scanning":
        return jsonify(ok=False, msg="扫描进行中，请等待完成后再删除"), 409
    scanner.state.pop(job, None)
    jobs.delete(job)
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


if __name__ == "__main__":
    serve(app, host=BIND, port=PORT, threads=8)
