import io
import os
import queue as q
import re
import shutil
import sys
import threading
import zipfile
from urllib.parse import quote

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template, request, send_file, session,
                   stream_with_context, url_for)
from waitress import serve

import admin
import device_probe
import jobs
import scanner
import time
from config import BIND, CONVERT, MAX_PDF_MEM, MAX_PDF_PAGES, PORT, SCANIMAGE, SECRET, TOKEN
from config import get_scan_root

app = Flask(__name__)
app.secret_key = SECRET
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)  # v1.14：基础 CSRF 防护（#7）
app.register_blueprint(admin.bp)


@app.after_request
def _security_headers(resp):
    """v1.14.6：OWASP 基础响应头——防 clickjacking（管理页/登录被 iframe 嵌套钓鱼）、
    MIME 嗅探 XSS、Referer 泄漏。纯 HTTP 局域网部署不启用 Cookie Secure（无 TLS）。"""
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp
FNAME_RE = re.compile(r"p\d+\.(png|jpg)")   # v1.14.3（P2-3）：与 jobs.PAGE_RE 同步 p\d+——p1000 页面 raw/thumb 不再 404
ZIP_QUEUE_MAXSIZE = 16   # v1.14.3（P2）：提为常量供测试注入（满队列断开场景）


# ---------------- 登录（仅当设置 TOKEN 时启用） ----------------
# v1.14.1（P2-11）：TOKEN 登录防暴力，按 IP 计数（与 PIN 同策略：5 次/60 秒）
_token_fails = {}   # ip -> {"count": n, "lock_until": ts}；局域网 IP 数有限，不做淘汰
_token_fail_lock = threading.Lock()   # v1.14.4（P1）：并发锁——TTL 清理/计数读改写/pop 同步化
_TK_MAX_FAILS = 5
_TK_LOCK_SEC = 60


@app.before_request
def require_login():
    if not TOKEN or session.get("ok") or request.endpoint in ("login", "static", "architecture"):
        return
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not TOKEN:
        return redirect(url_for("index"))
    err = ""
    if request.method == "POST":
        # v1.14.3（P2-10）：顺手清理过期超过 1 小时的失败记录——字典不再只增不减
        # v1.14.4（P1）：整段持锁——TTL 清理/判定/计数读改写/成功 pop 原子化
        now = time.time()
        ok_login = False
        with _token_fail_lock:
            for k in [k for k, v in _token_fails.items() if v["lock_until"] and v["lock_until"] < now - 3600]:
                _token_fails.pop(k, None)
            rec = _token_fails.setdefault(request.remote_addr, {"count": 0, "lock_until": 0.0})
            if time.time() < rec["lock_until"]:
                err = "尝试过于频繁，请稍后再试"
            elif request.form.get("password", "") == TOKEN:
                _token_fails.pop(request.remote_addr, None)
                ok_login = True
            else:
                ok_login = False
                rec["count"] += 1
                if rec["count"] >= _TK_MAX_FAILS:
                    rec["lock_until"] = time.time() + _TK_LOCK_SEC
                    rec["count"] = 0
                err = "口令错误"
        if ok_login:
            session["ok"] = True
            return redirect(url_for("index"))
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
                           pairs=jobs.page_pairs(job),
                           from_admin=request.args.get("from") == "admin")


@app.route("/manual")
def manual():
    return render_template("manual.html")


@app.route("/admin/manual")
def admin_manual():
    return render_template("admin-manual.html")


@app.route("/architecture")
def architecture():
    return send_file(os.path.join(app.static_folder, "architecture.html"))


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
    r = scanner.scan_adf(job)
    if r == "busy":   # v1.14.1（P1-6）：设备忙同步告知，不再异步报错
        return jsonify(ok=False, msg="设备忙，请稍后再试"), 409
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
    if jobs.is_locked(job):   # v1.14：锁定任务双重防护（模板隐藏按钮 + 此处显式 403）
        return jsonify(ok=False, msg="任务已锁定（管理员保护），不能删除"), 403
    with jobs.job_lock(job):   # v1.14.1（P1-3）：锁内检查+删除原子化，消除 TOCTOU
        if scanner.get_state(job)["state"] == "scanning":
            return jsonify(ok=False, msg="扫描进行中，请等待完成后再删除"), 409
        scanner.state.pop(job, None)
        jobs._delete_locked(job)   # v1.14.3（P3-11）：本处已持锁，走无重入版本
    jobs.release_job_lock(job)   # v1.14.2（#11）：锁已空闲，回收条目防字典长期增长
    return jsonify(ok=True)


@app.post("/api/jobs/<job>/reorder")
def api_reorder(job):
    """按指定顺序物理重命名页面文件（两步重命名防冲突）。
    支持 delete=true：从 order 中省略的页面将被删除，剩余页面重新连续编号。"""
    # v1.14.1（P1-3）：生命周期锁内执行检查+重命名，扫描/转换期间互斥
    with jobs.job_lock(job):
        return _reorder_body(job)


def _reorder_body(job):
    jobs.path(job)   # v1.14.6：先验证任务存在——空 order 等分支此前在任务不存在时误返 400
    # 排序互斥：扫描进行中禁止排序，防止文件被同时操作
    if scanner.get_state(job)["state"] == "scanning":
        return jsonify(ok=False, msg="扫描进行中，请等待完成后再排序"), 409
    body = request.get_json() or {}
    new_order = body.get("order", [])
    is_delete = body.get("delete", False)
    if is_delete and jobs.is_locked(job):   # v1.14：锁定任务禁删页，排序保留
        return jsonify(ok=False, msg="任务已锁定，不能删除页面"), 403
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
        # v1.14.2（#4）：待删页最后才物理删除——先重编号成功再删，中途失败不再丢数据
        to_delete = [f for f in current if f not in new_order]
        # 更新 meta（重命名不改页数，删除模式页数=保留数）
        if len(new_order) < 2:
            # 只剩 0 或 1 页，无需重编号，直接进入删除收尾
            _finish_delete(job, base, thumb_dir, to_delete, len(new_order))
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
    # 防御：清理可能残留的临时/墓也文件（上次异常中断遗留）
    for f in os.listdir(base):
        if (f.startswith("_tmp_") or f.startswith("_del_")) and f.endswith(".png"):
            try:
                os.remove(os.path.join(base, f))
            except OSError:
                pass
    try:
        for f in os.listdir(thumb_dir):
            if (f.startswith("_tmp_") or f.startswith("_del_")) and f.endswith(".jpg"):
                os.remove(os.path.join(thumb_dir, f))
    except OSError:
        pass
    # v1.14.4（P0）：删除模式 step0——待删页先改名墓也让出编号。此前模型里待删旧名
    # 一直占着 p001…，step2 给保留页分配新编号会与之碰撞，收尾按旧名删除会误删
    # 刚重命名的保留页数据（实测：A B C 删 p001 → B 被连带误删）。
    step0 = []   # 已完成的 (旧名, 墓也名)
    if is_delete:
        try:
            for i, dead_name in enumerate(to_delete):
                grave = f"_del_{i:03d}.png"
                os.rename(os.path.join(base, dead_name), os.path.join(base, grave))
                step0.append((dead_name, grave))
                dead_thumb = os.path.join(thumb_dir, dead_name[:-4] + ".jpg")
                if os.path.exists(dead_thumb):
                    try:
                        os.rename(dead_thumb, os.path.join(thumb_dir, grave[:-4] + ".jpg"))
                    except OSError:
                        pass   # v1.14.5（C2）：缩略图是 derived cache（_mk_thumb 随时重建、前端缺失有占位 fallback）——rename 失败不回滚页面事务（v1.14.4 评估不修重申）
        except OSError:
            for dead_name, grave in reversed(step0):
                try:
                    os.rename(os.path.join(base, grave), os.path.join(base, dead_name))
                except OSError:
                    pass
            return jsonify(ok=False, msg="删除准备失败，已恢复原状"), 500
    # v1.14.2（#4）：事务化两步重命名——任一步失败反向 rename 恢复原状，不再留半完成状态
    step1 = []   # 已完成的 (旧名, 临时名)
    try:
        for i, old_name in enumerate(current):
            tmp_name = f"_tmp_{i:03d}.png"
            os.rename(os.path.join(base, old_name), os.path.join(base, tmp_name))
            step1.append((old_name, tmp_name))
            # 缩略图同步（非关键数据，失败可重建，不阻塞页面重命名）
            old_thumb = os.path.join(thumb_dir, old_name[:-4] + ".jpg")
            if os.path.exists(old_thumb):
                try:
                    os.rename(old_thumb, os.path.join(thumb_dir, f"_tmp_{i:03d}.jpg"))
                except OSError:
                    pass
    except OSError:
        for old_name, tmp_name in reversed(step1):
            try:
                os.rename(os.path.join(base, tmp_name), os.path.join(base, old_name))
            except OSError:
                pass
        for dead_name, grave in reversed(step0):   # v1.14.4（P0）：连墓也一并撤回
            try:
                os.rename(os.path.join(base, grave), os.path.join(base, dead_name))
            except OSError:
                pass
        return jsonify(ok=False, msg="排序失败（磁盘/权限异常），已恢复原状"), 500
    step2 = []   # 已完成的 (临时名, 目标名)
    try:
        for i, new_name in enumerate(new_order):
            old_idx = current.index(new_name)          # 该页面在旧顺序中的位置
            tmp_key = f"_tmp_{old_idx:03d}.png"
            dst_name = f"p{i + 1:03d}.png"             # 新顺序第 i 位 → 文件名编号
            os.rename(os.path.join(base, tmp_key), os.path.join(base, dst_name))
            step2.append((tmp_key, dst_name))
            # 缩略图同步
            tmp_thumb = os.path.join(thumb_dir, f"_tmp_{old_idx:03d}.jpg")
            if os.path.exists(tmp_thumb):
                try:
                    os.rename(tmp_thumb, os.path.join(thumb_dir, dst_name[:-4] + ".jpg"))
                except OSError:
                    pass
    except OSError:
        # 反向恢复：先撤第二步（dst → tmp），再撤第一步（tmp → old），最后撤 step0 墓也
        for tmp_key, dst_name in reversed(step2):
            try:
                os.rename(os.path.join(base, dst_name), os.path.join(base, tmp_key))
            except OSError:
                pass
        for old_name, tmp_name in reversed(step1):
            try:
                os.rename(os.path.join(base, tmp_name), os.path.join(base, old_name))
            except OSError:
                pass
        for dead_name, grave in reversed(step0):
            try:
                os.rename(os.path.join(base, grave), os.path.join(base, dead_name))
            except OSError:
                pass
        return jsonify(ok=False, msg="排序失败（磁盘/权限异常），已恢复原状"), 500
    # v1.14.4（P0）：两步重命名全部成功后执行删除收尾——names=None 走墓也前缀，
    # 待删页在 step0 已改名让位，永不与新编号碰撞；<2 页分支（无 rename）才按原名删
    if is_delete:
        _finish_delete(job, base, thumb_dir, None, len(new_order))
    return jsonify(ok=True)


def _finish_delete(job, base, thumb_dir, names=None, keep=0):
    """v1.14.4（P0）：删除收尾。names=None 时扫墓也前缀 _del_*（重编号流程，
    待删页已改名让位，防新编号碰撞误删保留页）；names 显式时按原名删（<2 页分支，
    无 rename 即无碰撞）。仅在重命名全部成功后调用。"""
    if names is None:
        names = [f for f in os.listdir(base) if f.startswith("_del_") and f.endswith(".png")]
    for f in names:
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
    meta = jobs.load(job)
    meta["pages"] = keep
    jobs.save(job, meta)


@app.get("/api/devices")
def api_devices():
    """列出设备并探测每台设备支持的扫描模式和进纸源（scanimage -A）。
    v1.14：探测逻辑合并到 device_probe.py，与管理页共用同一实现与缓存（#11）。"""
    devs, _cached = device_probe.probe()
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
    # v1.14.4（P1）：check_page_safe 拒 symlink + realpath containment，防链接页越权读
    try:
        base = jobs.path(job)
    except jobs.JobError:
        abort(404)
    # v1.14.4（B2 fd snapshot）：锁内完成安全校验并打开文件句柄后释放锁——
    # 已打开的 inode 不受后续 rename/删除影响（TOCTOU 消除），网络传输不占锁、
    # 不读入内存（替代 v1.14.3 的整文件读 RAM，消除大图并发下载内存放大）
    with jobs.job_lock(job):
        try:
            f = jobs.open_page_fd(base, fname)
        except (jobs.JobError, OSError):
            abort(404)
    return send_file(f, mimetype="image/png")


@app.route("/job/<job>/thumb/<fname>")
def thumb(job, fname):
    if not FNAME_RE.fullmatch(fname) or not fname.endswith(".jpg"):
        abort(404)
    # v1.14.4（P1）：同 raw——拒 symlink + containment；B2 fd snapshot 同 raw
    try:
        base = jobs.path(job)
    except jobs.JobError:
        abort(404)
    with jobs.job_lock(job):
        try:
            f = jobs.open_page_fd(base, os.path.join(".thumbs", fname))
        except (jobs.JobError, OSError):
            abort(404)
    return send_file(f, mimetype="image/jpeg")


@app.route("/job/<job>/download.zip")
def dl_zip(job):
    base = jobs.path(job)
    # v1.14.2（#6）：下载全程持任务生命周期锁——期间 delete/reorder 排队等待而非产生截断/损坏包
    jlock = jobs.job_lock(job)
    jlock.acquire()
    files = [(f, os.path.join(base, f)) for f in jobs.pages(job)]
    if not files:
        jlock.release()
        abort(404)
    qu = q.Queue(maxsize=ZIP_QUEUE_MAXSIZE)   # v1.14.1（P1-8）：背压——慢客户端时压缩线程阻塞，防队列无限吃内存
    DONE = object()
    cancel = threading.Event()   # v1.14.2（#7）：客户端断开时唤醒 worker 退出，防 daemon 线程永久阻塞

    def worker():
        class W:
            """非寻址流 Writer：tell 用计数器，seek 抛异常使 zipfile 走流模式。"""
            def __init__(self):
                self.pos = 0
            def write(self, b):
                # v1.14.3（P1-2）：满队列 put 超时重检 cancel——客户端断开后 worker 最多 0.5s 退出，
                # 不再永久阻塞在无 timeout 的 put 上（v1.14.2 只给 DONE 加了 timeout，此处是漏网点）
                while True:
                    if cancel.is_set():
                        raise OSError("zip cancelled")   # 消费者已断开，中止压缩
                    try:
                        qu.put(b, timeout=0.5)
                        self.pos += len(b)
                        return
                    except q.Full:
                        continue
            def tell(self):
                return self.pos
            def seek(self, *a):
                raise OSError("not seekable")
            def flush(self):
                pass
        try:
            with zipfile.ZipFile(W(), "w", zipfile.ZIP_DEFLATED) as zf:
                for arc, path in files:
                    if cancel.is_set():
                        break
                    zf.write(path, arcname=arc)
        except Exception as e:
            # v1.14.5（B3）：Samba 外部删除/替换文件会让 zipfile.write 抛错——不再静默吞，
            # 记 stderr（journalctl 可见），消费者收到截断流后会重试。应用 job_lock 约束不了外部客户端
            print("[zip] worker error: %s" % e, file=sys.stderr)
        finally:
            if not cancel.is_set():
                try:
                    qu.put(DONE, timeout=5)   # v1.14.2（#7）：消费者已走则不投递，防 put 永久阻塞
                except q.Full:
                    pass

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        try:
            while True:
                chunk = qu.get()
                if chunk is DONE:
                    break
                yield chunk
        finally:
            cancel.set()          # 正常结束或客户端断开（GeneratorExit）都通知 worker 停止
            jlock.release()       # v1.14.2（#6）：流结束/中断才释放生命周期锁

    return Response(stream_with_context(gen()), mimetype="application/zip",
                    headers={"Content-Disposition":
                             f"attachment; filename=\"scan.zip\"; filename*=UTF-8''{quote(job + '.zip')}"})


@app.route("/job/<job>/download.pdf")
def dl_pdf(job):
    from PIL import Image
    # v1.14.3（P2-4）：读取+合成全程持任务锁——期间 delete/reorder 排队，PDF 页面内容一致；
    # 合成完即释放，网络传输不持锁（PDF 一次生成到内存，最适合此方案）
    with jobs.job_lock(job):
        base = jobs.path(job)
        files = jobs.pages(job)
        if not files:
            abort(404)
        if len(files) > MAX_PDF_PAGES:
            abort(413)
        # v1.14：按像素估算合成内存（Pillow 惰性读头不载位图），超限拒绝防 ARM 盒子 OOM（#5）
        approx = 0
        for f in files:
            with jobs.open_page_fd(base, f) as fh, Image.open(fh) as im:
                approx += im.width * im.height * 3
        if approx > MAX_PDF_MEM:
            abort(413)
        imgs = []
        for f in files:   # v1.14.1（P2-14）+ v1.14.5（P1-2）：with 显式关闭文件句柄；open_page_fd O_NOFOLLOW 防页面被外部替换为链接越权读
            with jobs.open_page_fd(base, f) as fh, Image.open(fh) as src:
                imgs.append(src.convert("RGB"))
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
