import io
import os
import queue as q
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
from config import BIND, CONVERT, MAX_PDF_MEM, MAX_PDF_PAGES, PORT, SCANIMAGE, SECRET, TOKEN, VERSION
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
FNAME_RE = re.compile(r"p\d+\.(png|jpg|webp)")   # v1.16.0-tmp01：格式可选；v1.14.3（P2-3）与 jobs.PAGE_RE 同步 p\d+
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
    return {"authed": bool(TOKEN), "ver": VERSION}


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
def _validate_scan_params(dpi, mode, fmt, source=None):
    """v1.16.0-tmp04（P2-8，审查报告）：创建任务/修改参数共享的基本校验
    （设备能力级校验仍由各入口按探测结果做）。返回错误消息或空串。
    v1.16.0-tmp05（T04-07，审查报告）：新增 source 枚举校验——创建接口不再
    接受任意来源字符串落盘（fail-closed）。"""
    if fmt not in scanner.VALID_FORMAT:
        return "不支持的输出格式：%s" % str(fmt)[:30]
    if dpi not in scanner.VALID_DPI or mode not in scanner.VALID_MODE:
        return "参数超出支持范围"
    if source is not None and source not in ("flatbed", "adf"):
        return "不支持的扫描来源：%s" % str(source)[:30]
    return ""


@app.post("/api/jobs")
def api_create():
    form = request.form
    # v1.16.0-tmp04（P2-8）：非法参数在创建时即拒（fail-closed），不再落盘后扫描才报错
    # v1.16.0-tmp05（T04-07，审查报告）：source 枚举校验（flatbed/adf）
    source = form.get("source", "flatbed")
    err = _validate_scan_params(form.get("dpi", "150"), form.get("mode", "Gray"),
                                form.get("format", "png"), source)
    if err:
        return jsonify(ok=False, msg=err), 400
    # v1.16.0-tmp05（T04-07）：device 有值时必须在探测列表（不存在即拒 400，
    # 不落盘无效任务）；空 device 保留默认设备回退语义
    device = form.get("device", "").strip()
    if device:
        devs, _cached = device_probe.probe()
        cap = next((x for x in devs
                    if x["name"] == device or x["name"].startswith(device)), None)
        if cap is None:
            return jsonify(ok=False, msg="设备不在已探测列表：%s" % device[:60]), 400
    # 表单布尔：checkbox 只在勾选时提交；仅认明确真值，其余一律 False（不与字符串 truthy 混淆）
    crop = form.get("crop", "").strip().lower() in ("1", "true", "on", "yes")
    name = jobs.create(
        form.get("remark", ""),
        {"source": source,
         "source_name": form.get("source_name", ""),
         "dpi": form.get("dpi", "150"),
         "mode": form.get("mode", "Gray"),
         "device": device,
         "crop": crop,
         "format": form.get("format", "png")})   # v1.16.0-tmp01：输出格式
    return redirect(url_for("job_page", job=name))


@app.put("/api/jobs/<job>/params")
def api_params(job):
    """v1.16.0-tmp01：任务页参数修改（dpi/mode/format/crop/device）。
    jlock 内校验+写盘原子；dpi/mode 按目标设备能力白名单校验，
    format 白名单 fail-closed，device 须在已探测列表且能力兼容任务 source。

    v1.16.0-tmp01（用户反馈改进）：扫描/转换中允许改参数——参数本来就是
    「对下次扫描生效」语义（scan_flatbed 在 scanimage 前快照 _params），
    转换中改 format/dpi 不影响当前正在转换的页（混合格式已支持）。
    并发安全：PUT 与 convert_worker 落盘段都在 jlock 内读改写 meta.json
    （load→改→save 原子），jlock 串行化保证不丢更新；扫描段持 jlock 时
    PUT 阻塞排队，扫描完成即执行——无 409，改参数永不被拒。"""
    d = request.get_json(silent=True)
    # v1.16.0-tmp04（P2-8，审查报告）：JSON 根必须是对象——数组/字符串/数字/None
    # 进入后续 d.get() 会 500，fail-closed 直接 400
    if not isinstance(d, dict):
        return jsonify(ok=False, msg="请求体必须是 JSON 对象"), 400
    crop = d.get("crop", None)
    if crop is not None and not isinstance(crop, bool):
        return jsonify(ok=False, msg="crop 必须是布尔值"), 400   # "false" 字符串不再被误判为真
    with jobs.job_lock(job):
        try:
            meta = jobs.load(job)
        except jobs.JobError as e:
            return jsonify(ok=False, msg=str(e)[:150]), 404
        params = meta.setdefault("params", {})
        new_device = str(d.get("device") or params.get("device") or "").strip()
        new_dpi = str(d.get("dpi") or params.get("dpi") or "150").strip()
        new_mode = str(d.get("mode") or params.get("mode") or "Gray").strip()
        new_fmt = str(d.get("format") or params.get("format") or "png").strip()
        # 1) format 白名单（fail-closed）
        if new_fmt not in scanner.VALID_FORMAT:
            return jsonify(ok=False, msg="不支持的输出格式：%s" % new_fmt[:30]), 400
        # 2) device 存在性 + 能力校验（探测列表比对）
        devs, _cached = device_probe.probe()
        cap = next((x for x in devs
                    if x["name"] == new_device or x["name"].startswith(new_device)), None)
        if new_device and cap is None:
            return jsonify(ok=False, msg="设备不在已探测列表：%s" % new_device[:60]), 400
        source = params.get("source", "flatbed")
        if cap and source != "flatbed" and not any(
                "adf" in (s or "").lower() for s in cap.get("sources", [])):
            return jsonify(ok=False, msg="设备 %s 不支持 ADF，无法用于本任务" % new_device[:60]), 400
        # 3) dpi/mode 按设备能力白名单（探测失败无能力信息时放行 VALID 集合兜底）
        if cap and cap.get("dpi_raw") and new_dpi not in cap["dpi_raw"]:
            return jsonify(ok=False, msg="设备不支持 %s dpi" % new_dpi), 400
        if cap and cap.get("modes") and new_mode not in cap["modes"]:
            return jsonify(ok=False, msg="设备不支持 %s 模式" % new_mode), 400
        if new_dpi not in scanner.VALID_DPI or new_mode not in scanner.VALID_MODE:
            return jsonify(ok=False, msg="参数超出支持范围"), 400
        params.update(dpi=new_dpi, mode=new_mode, format=new_fmt,
                      crop=crop if crop is not None else bool(params.get("crop", False)),
                      device=new_device)
        jobs.save(job, meta)
    return jsonify(ok=True, params=params)


@app.post("/api/jobs/<job>/scan")
def api_scan(job):
    # v1.51-tmp03：同步返回——POST 等 scanimage -d 完成后返回文件名，
    # 前端据此显示占位 + pollThumbReady（彻底消除 status 轮询）
    try:
        r = scanner.scan_flatbed(job)
    except RuntimeError as e:
        return jsonify(ok=False, msg=str(e)[:300]), 500
    if r == "busy":
        return jsonify(ok=False, msg="设备忙，请稍后再试"), 409
    if r == "convert_busy":   # v1.16.0-tmp02：转换队列水位满，拒扫防无限堆积
        return jsonify(ok=False, msg="后台转换队列已满（%d 页待处理），请等转换完成后再扫描" %
                       scanner.MAX_PENDING_PNM), 409
    return jsonify(ok=True, file=r)


@app.post("/api/jobs/<job>/adf")
def api_adf(job):
    if jobs.load(job).get("params", {}).get("source", "flatbed") != "adf":
        return jsonify(ok=False, msg="该任务创建时选择的是平板模式"), 400
    r = scanner.scan_adf(job)
    if r == "busy":   # v1.14.1（P1-6）：设备忙同步告知，不再异步报错
        return jsonify(ok=False, msg="设备忙，请稍后再试"), 409
    if r == "adf_busy":   # v1.16.0-tmp05（T04-05）：同任务上一批 ADF 转换未收敛
        return jsonify(ok=False, msg="上一批 ADF 页面仍在转换中，请等待完成后再扫描"), 409
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
    # v1.15.1-tmp04：不再检查 state==scanning——能拿到 jlock 即扫描段已结束
    # （扫描段全程持 jlock）；转换中删除允许，convert_worker 落盘段 isdir 兜底。
    try:
        with jobs.job_lock(job):   # v1.14.1（P1-3）：锁内检查+删除原子化，消除 TOCTOU
            scanner.state.pop(job, None)
            with _pack_lock:                   # tmp06：顺带清打包进度登记
                _pack_progress.pop(job, None)
            try:
                jobs._delete_locked(job)   # v1.14.3（P3-11）：本处已持锁，走无重入版本
            except jobs.JobError as e:
                # v1.16.0-tmp09（DeepSeek M2）：外部 is_locked 检查与 jlock 获取之间
                # 任务被管理员锁定时，_delete_locked 抛 JobError 落到全局 404——
                # 语义错误（任务存在且被锁），应 403
                return jsonify(ok=False, msg=str(e)[:150]), 403
            except OSError as e:
                # v1.16.0-tmp01：任务目录权限异常（如历史 root 属主）时给可行动提示，不白屏 500
                return jsonify(ok=False, msg="删除失败：任务目录权限异常（%s），请管理员修复目录权限后重试" % str(e)[:80]), 500
    finally:
        # v1.16.1-tmp01（DeepSeek 六轮 2.3）：正常/403/500 三条路径统一兑底——
        # 旧版只在正常路径调（403/500 提前 return 不经过），语义不一致；
        # 与 jobs.delete 公共入口同模式（幂等，refs 自动回收后为 no-op 孤儿兑底）
        jobs.release_job_lock(job)
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
    # v1.15.1-tmp04：转换中禁止排序（落盘段会 os.replace 到旧名，reorder 改名会打穿）
    if scanner.has_pending(job):
        return jsonify(ok=False, msg="有页面正在转换，请等待完成后再排序"), 409
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
    # v1.16.0-tmp01：临时/墓也名携带页面原扩展名（png/jpg/webp）
    for f in os.listdir(base):
        if (f.startswith("_tmp_") or f.startswith("_del_")) and f.endswith((".png", ".jpg", ".webp")):
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
                grave = f"_del_{i:03d}." + dead_name.rsplit(".", 1)[-1]   # v1.16.0-tmp01：保留原 ext
                os.rename(os.path.join(base, dead_name), os.path.join(base, grave))
                step0.append((dead_name, grave))
                dead_thumb = os.path.join(thumb_dir, dead_name.rsplit(".", 1)[0] + ".jpg")   # v1.16.0-tmp01：rsplit 剥扩展
                if os.path.exists(dead_thumb):
                    try:
                        os.rename(dead_thumb, os.path.join(thumb_dir, grave.rsplit(".", 1)[0] + ".jpg"))
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
            tmp_name = f"_tmp_{i:03d}." + old_name.rsplit(".", 1)[-1]   # v1.16.0-tmp01：保留原 ext
            os.rename(os.path.join(base, old_name), os.path.join(base, tmp_name))
            step1.append((old_name, tmp_name))
            # 缩略图同步（非关键数据，失败可重建，不阻塞页面重命名）
            old_thumb = os.path.join(thumb_dir, old_name.rsplit(".", 1)[0] + ".jpg")   # v1.16.0-tmp01：rsplit 剥扩展
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
            _ext = new_name.rsplit(".", 1)[-1]          # v1.16.0-tmp01：ext 随页面走
            tmp_key = f"_tmp_{old_idx:03d}.{_ext}"
            dst_name = f"p{i + 1:03d}.{_ext}"           # 新顺序第 i 位 → 文件名编号（ext 保留）
            os.rename(os.path.join(base, tmp_key), os.path.join(base, dst_name))
            step2.append((tmp_key, dst_name))
            # 缩略图同步
            tmp_thumb = os.path.join(thumb_dir, f"_tmp_{old_idx:03d}.jpg")
            if os.path.exists(tmp_thumb):
                try:
                    os.rename(tmp_thumb, os.path.join(thumb_dir, dst_name.rsplit(".", 1)[0] + ".jpg"))   # v1.16.0-tmp01：rsplit 剥扩展
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
        # v1.16.0-tmp01：墓也前缀扫描覆盖全部扩展名
        names = [f for f in os.listdir(base) if f.startswith("_del_") and f.endswith((".png", ".jpg", ".webp"))]
    for f in names:
        try:
            os.remove(os.path.join(base, f))
        except OSError:
            pass
        old_thumb = os.path.join(thumb_dir, f.rsplit(".", 1)[0] + ".jpg")   # v1.16.0-tmp01：rsplit 剥扩展
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
    v1.14：探测逻辑合并到 device_probe.py，与管理页共用同一实现与缓存（#11）。
    v1.16.0-tmp01：?refresh=1 强制重新探测——普通页「重新探测」按钮用
    （admin API 需 PIN 登录，普通页不能复用；探测失败空数据缓存 300s 的自愈入口）。"""
    devs, _cached = device_probe.probe(force=request.args.get("refresh") == "1")
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
    if not FNAME_RE.fullmatch(fname):
        abort(404)
    # v1.16.0-tmp01：mimetype 按扩展名（png/jpg/webp）
    mime = scanner.FMT_MIME.get(fname.rsplit(".", 1)[-1], "image/png")
    # v1.14.4（P1）：check_page_safe 拒 symlink + realpath containment，防链接页越权读
    try:
        base = jobs.path(job)
    except jobs.JobError:
        abort(404)
    # v1.14.4（B2 fd snapshot）：锁内完成安全校验并打开文件句柄后释放锁——
    # 已打开的 inode 不受后续 rename/删除影响（TOCTOU 消除），网络传输不占锁、
    # 不读入内存（替代 v1.14.3 的整文件读 RAM，消除大图并发下载内存放大）
    with jobs.job_lock(job):
        # v1.15.1-tmp04：该页转换中（0 字节占位）→ 409，避免下载到空文件
        if scanner.has_pending(job, fname):
            abort(409)
        try:
            f = jobs.open_page_fd(base, fname)
        except (jobs.JobError, OSError):
            abort(404)
    return send_file(f, mimetype=mime)   # v1.16.0-tmp04（P2-7）：用已算的按扩展名 MIME，不再固定 image/png


@app.route("/job/<job>/thumb/<fname>")
def thumb(job, fname):
    if not FNAME_RE.fullmatch(fname) or not fname.endswith(".jpg"):
        abort(404)
# v1.14.4（P1）：同 raw——拒 symlink + containment；B2 fd snapshot 同 raw
    try:
        base = jobs.path(job)
    except jobs.JobError:
        abort(404)
    stem = fname.rsplit(".", 1)[0]   # v1.16.0-tmp01：thumb 恒 .jpg；页面扩展名任意，pending 按 stem 匹配
    # v1.15.1-tmp04：转换中页挂起等待（long-poll，不持锁）——前端 1 次请求
    # 等转换完成直接 200，不再产生每秒一次的 404 轮询。上限 120s 防异常挂死。
    # v1.16.0-tmp01：页面可能 png/jpg/webp，pending 登记的是真实页面名——
    # 写死 "+.png" 会使 jpg/webp 任务 long-poll 失效（退化成每秒 404 轮询）。
    # v1.16.1-tmp04（DeepSeek 九轮 P1-3）：上限 120→20s——waitress 8 线程下，
    # ADF 多页任务浏览器并发加载 N 张缩略图会各挂 1 线程，120s 足以吊死整个
    # 线程池（应用假死）；20s + 前端 1s 重试已兼容（pollThumbReady 非 200 即
    # 1s 后重试），周期性释放让状态接口始终有机会插入。M1005 无 ADF 本无风险，
    # 此为开源 ADF 设备用户的防御。
    deadline = time.time() + 20
    while time.time() < deadline:
        if not any(p.rsplit(".", 1)[0] == stem for p in scanner.pending_pages(job)):
            break
        time.sleep(0.5)
    with jobs.job_lock(job):
        try:
            f = jobs.open_page_fd(base, os.path.join(".thumbs", fname))
        except (jobs.JobError, OSError):
            abort(404)
    return send_file(f, mimetype="image/jpeg")


# ---------------- 打包进度（v1.16.0-tmp06 新功能） ----------------
# PDF 合成 / ZIP 打包对多页大任务是长耗时操作（300dpi 彩色 PIL 逐页处理 + 流式压缩），
# 此前点击按钮后页面无任何反馈。登记表供前端轮询显示「打包中 n/m」。
# v1.16.0-tmp07（DeepSeek 排查 P3）：登记按 kind 隔离——同一任务并发 ZIP+PDF 时
# 各自独立进度（旧版单条共用 job 键被互相覆盖，先点 ZIP 再点 PDF 后 ZIP 按钮轮询
# 失配、永不恢复）。结构 {job: {kind: {kind,done,total,active}}}。
_pack_progress = {}
_pack_lock = threading.Lock()

# v1.16.1-tmp06（DeepSeek 十一轮 P1-A）：409 拒绝型条目（done=0/total=0/active=False，
# dl_zip/dl_pdf 转换中拒绝下载时写入）此前无任何回收路径——用户不重试也不删任务则永久
# 驻留 _pack_progress，长期运行+频繁 409 下内存只增不减（唯一随时间恶化的缺陷）。
# v1.16.1-tmp07（十二轮 P1-A/P1-B）扩展：
# ① 完成型条目（total>0/active=False，正常下载完成后写入）此前也永久驻留——每用 N 个任务
#    就驻留 N×2 条记录；现同样按 TTL 清理（保留期 > 前端 5 分钟兜底，前端早已 stop 轮询）；
# ② 触发点从仅 _pack_prog_start 扩展到 api_pack_progress 读时清——不重试也不删任务时
#    拒绝条目也能被下次任意轮询清掉。
# v1.16.1 定版（十四轮 P2-C）：两档 TTL 相差 10 倍的分档理由——拒绝条目只需覆盖
# "前端下一次轮询（≤500ms）读到 msg"，60s 是宽松保险；完成条目须覆盖"前端 5 分钟
# 兜底窗口"全段（600s 有余量），否则前端轮询后半段读不到条目、按钮卡到兜底才恢复。
_PACK_REJECT_TTL = 60    # 拒绝型条目存活秒数——前端 500ms 轮询在 TTL 内必能读到拒绝原因
# v1.16.1-tmp08（十三轮 P1-A）：完成型 TTL 与前端兜底是隐式契约——本值必须
# > static/app.js dlWithProgress 里 setTimeout(300000)=5 分钟硬兜底，否则前端
# 轮询后半段读不到完成条目（p=undefined → if(!p) return），按钮卡"打包中"直到
# 兜底才恢复。改前端兜底时必须同步审视本值（test_v16t11.py 有
# test_frontend_fallback_lt_finished_ttl 静态断言守护）。
_PACK_FINISHED_TTL = 600 # 完成型条目存活秒数（10 分钟）—— 前端 5 分钟兜底早已 stop，展示完成态远够


def _pack_prog_gc(now):
    """清理过期非活跃条目（须在 _pack_lock 内调用）。
    v1.16.1-tmp07：不再区分拒绝/完成——任何 active=False 且 ts 超对应 TTL 都清；
    仅 active=True（进行中）条目永不清。"""
    for job_key in list(_pack_progress.keys()):
        kinds = _pack_progress[job_key]
        for kind_key in list(kinds.keys()):
            rec = kinds[kind_key]
            if rec.get("active") is not False:
                continue   # 进行中条目永不清
            ttl = _PACK_REJECT_TTL if rec.get("total") == 0 else _PACK_FINISHED_TTL
            if now - rec.get("ts", now) > ttl:
                kinds.pop(kind_key, None)
        if not kinds:
            _pack_progress.pop(job_key, None)


def _pack_prog_start(job, kind, **kw):
    """登记/重建进度条目（下载开始时调用）。
    v1.16.1-tmp06：写 ts 供惰性 GC 判定过期；登记前顺带 GC 过期条目。
    v1.16.1-tmp07：now 取一次（原双重 time.time() 合并）。"""
    now = time.time()
    with _pack_lock:
        _pack_prog_gc(now)
        rec = _pack_progress.setdefault(job, {}).setdefault(
            kind, {"kind": kind, "done": 0, "total": 0, "active": True})
        rec.update(kw)
        rec["ts"] = now


def _pack_prog_update(job, kind, **kw):
    """v1.16.1-tmp01（DeepSeek 六轮 2.2）：仅更新已存在条目，不重建——
    gen finally 的 jlock.release() 与本调用之间存在时序窗口：api_delete 拿到
    锁后 pop 掉 _pack_progress[job]，随后收尾 update(active=False) 走 setdefault
    会重建已删任务的条目（毫秒级竞态残留）。改为条目不存在即忽略（任务已删，
    进度条目本就不该再出现）。
    v1.16.1-tmp06：update 同步刷新 ts——条目仍在被写入即仍活跃，防未来 GC
    条件演化时误删。"""
    with _pack_lock:
        rec = _pack_progress.get(job, {}).get(kind)
        if rec is None:
            return
        rec.update(kw)
        rec["ts"] = time.time()


@app.get("/api/jobs/<job>/pack-progress")
def api_pack_progress(job):
    """打包进度查询（不持任务锁——只读内存登记，合成/打包进行中可并发响应）。
    返回 {zip: {...}, pdf: {...}}，前端按 kind 精确取各自进度，互不干扰。
    v1.16.1-tmp07（十二轮 P1-A）：读时清——任何进度轮询都顺带触发 GC，
    即使不再发起新下载也能回收过期条目（前端 500ms 轮询即天然节流）。"""
    with _pack_lock:
        _pack_prog_gc(time.time())
        src = _pack_progress.get(job) or {}
        # v1.16.1-tmp06：ts 为 GC 内部字段，不进前端 JSON
        out = {k: {kk: vv for kk, vv in dict(v).items() if kk != "ts"}
               for k, v in src.items()}
    return jsonify(out)


@app.route("/job/<job>/download.zip")
def dl_zip(job):
    base = jobs.path(job)
    # v1.14.2（#6）：下载全程持任务生命周期锁——期间 delete/reorder 排队等待而非产生截断/损坏包
    jlock = jobs.job_lock(job)
    jlock.acquire()
    # v1.16.0-tmp06（DeepSeek P0）：锁移交 gen 前的任何提前返回/异常（409/404/
    # pages 抛 JobError）都由 finally 兜底释放——旧代码手动 release 漏了
    # jobs.pages 的异常路径（任务目录被 Samba 外部删除时锁永久泄漏）。
    # v1.16.1-tmp05（DeepSeek 十轮 P1-B）：cancel 提前到 try 前定义——Response(...)
    # 构造抛异常时 gen 从未被消费、其 finally 不执行，worker 会永久卡在 W.write 的
    # while True 死循环（daemon 线程泄漏）；必须在构造失败的兜底路径显式 cancel.set()
    # 通知 worker 退出。提前定义还保证 409/404 提前返回时 finally 引用 cancel 不 NameError。
    cancel = threading.Event()
    handed_to_gen = False
    # v1.16.2-tmp02（十五轮 N3）：三收尾点（gen finally / 构造失败 finally /
    # call_on_close）收敛为单次执行入口——锁保护确保 jlock.release() 绝不双调
    # （threading.Lock 二次 release 不报错但会误关他线程刚 acquire 的锁）。
    _fin_lock = threading.Lock()
    _fin_done = [False]

    def _release_once():
        with _fin_lock:
            if _fin_done[0]:
                return
            _fin_done[0] = True
        cancel.set()   # v1.16.1-tmp05（P1-B）：通知 worker 退出，防 W.write 永久循环
        jlock.release()
        _pack_prog_update(job, "zip", active=False)

    try:
        # v1.15.1-tmp04：转换中页是 0 字节占位，混入 ZIP 会产生损坏文件
        # v1.16.1-tmp04（DeepSeek 九轮 P1-4）：拒绝时同步写登记（active=False + msg）——
        # 前端轮询下次（≤500ms）即见 active=False 恢复按钮并提示，不再页面灰 5 分钟
        if scanner.has_pending(job):
            _pack_prog_start(job, "zip", done=0, total=0, active=False,
                             msg="有页面正在转换，请稍后重试")
            return jsonify(ok=False, msg="有页面正在转换，请等待完成后再下载"), 409
        try:
            files = [(f, os.path.join(base, f)) for f in jobs.pages(job)]
        except jobs.JobError:
            abort(404)
        if not files:
            abort(404)
        _pack_prog_start(job, "zip", done=0, total=len(files), active=True)
        qu = q.Queue(maxsize=ZIP_QUEUE_MAXSIZE)   # v1.14.1（P1-8）：背压——慢客户端时压缩线程阻塞，防队列无限吃内存
        DONE = object()

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
                    for i, (arc, path) in enumerate(files, 1):
                        if cancel.is_set():
                            break
                        zf.write(path, arcname=arc)
                        _pack_prog_update(job, "zip", done=i)   # tmp06：打包进度（逐文件）
            except Exception as e:
                # v1.14.5（B3）：Samba 外部删除/替换文件会让 zipfile.write 抛错——不再静默吞，
                # 记 stderr（journalctl 可见），消费者收到截断流后会重试。应用 job_lock 约束不了外部客户端
                # v1.16.1-tmp07（十二轮 P1-C）：cancel 引发的 OSError("zip cancelled") 是设计内
                # 取消（客户端断开），不是错误——cancel 已 set 时不打印，避免 journalctl 假错误噪音
                if not cancel.is_set():
                    print("[zip] worker error: %s" % e, file=sys.stderr)
            finally:
                # v1.16.1-tmp04（DeepSeek 九轮 P1-2）：DONE 必须送达或等到 cancel——
                # 旧版 put(timeout=5) 超时 pass，慢客户端（队列持续满 5s+）时 DONE
                # 丢失、gen 永久阻塞 qu.get()、jlock 永不释放（任务删不掉，致命偶发）。
                # 正常路径重试直到送达；cancel（客户端断开，gen 已走）才放弃。
                while not cancel.is_set():
                    try:
                        qu.put(DONE, timeout=0.5)   # v1.14.2（#7）：消费者已走则不投递，防 put 永久阻塞
                        break
                    except q.Full:
                        continue

        threading.Thread(target=worker, daemon=True).start()

        def gen():
            try:
                while True:
                    chunk = qu.get()
                    if chunk is DONE:
                        break
                    yield chunk
            finally:
                # 正常结束/客户端断开（GeneratorExit）统一走幂等收尾
                # （v1.14.2 #6 释放生命周期锁 + tmp05 cancel + tmp06 进度收口的原三行）
                _release_once()

        # v1.16.0-tmp09（DeepSeek H2）：Response 先构造成功、再交接锁——旧序
        # handed_to_gen=True 在构造前置位，若 Response(...) 抛异常（OOM/header
        # 异常），gen 从未被调、finally 见 True 不释放 → jlock 永久泄漏
        resp = Response(stream_with_context(gen()), mimetype="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename=\"scan.zip\"; filename*=UTF-8''{quote(job + '.zip')}"})
        handed_to_gen = True
        # v1.16.2-tmp02（十五轮 N3）：构造成功但生成器从未被消费（waitress 在
        # 迭代 body 前就 close——客户端在响应头后立即断开）时，未启动的生成器
        # close() 不执行 finally，cancel/jlock 无人释放：worker 每 0.5s 空转
        # 永不退出、jlock 永久占用该任务（DELETE 死等）。call_on_close 兜底：
        # WSGI 关闭响应时统一走幂等收尾（gen 已收尾则 no-op）。
        resp.call_on_close(_release_once)
        return resp
    finally:
        if not handed_to_gen:
            _release_once()


def _webp_safe_png(base, fname):
    """v1.16.0-tmp04（P1-3，审查报告）：webp 页安全转 PNG。
    open_page_fd（O_NOFOLLOW + realpath 校验）打开后复制到受控临时文件，
    ImageMagick 只接触副本——任务目录可被 Samba 写入时，symlink/路径替换
    不再绕过安全检查。返回临时 PNG 路径（调用方用毕删除）。"""
    fd, safe_src = tempfile.mkstemp(prefix="scanweb_pdfsrc_", suffix=".webp")
    png = safe_src + ".png"
    fd_taken = False   # v1.16.2-tmp02（十五轮 N4）：fdopen 移交成功后 fd 归 dst 所有，
    try:               # body 失败时 with 已关 fd——except 不得再 close（防 fd 号被
        with jobs.open_page_fd(base, fname) as src:   # 其他线程复用后误关他人 fd）
            dst = os.fdopen(fd, "wb")
            fd_taken = True
            with dst:
                shutil.copyfileobj(src, dst)
    except Exception:
        if not fd_taken:   # 仅 fdopen 失败（fd 未被接管）时才需要手动关闭
            try:
                os.close(fd)
            except OSError:
                pass
        for p in (png, safe_src):
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    try:
        subprocess.run([scanner.CONVERT, safe_src, "PNG:" + png],
                       capture_output=True, timeout=scanner.CONVERT_CMD_TIMEOUT,
                       check=True, cwd=tempfile.gettempdir())
        # v1.16.0-tmp05（T04-03，审查报告）：转换成功源副本不再需要——调用方只
        # 删除返回的 PNG，若此处不删 safe_src，重复生成 PDF 会持续泄漏 .webp
        os.remove(safe_src)
        return png
    except Exception:
        for p in (png, safe_src):
            try:
                os.remove(p)
            except OSError:
                pass
        raise


@app.route("/job/<job>/download.pdf")
def dl_pdf(job):
    from PIL import Image
    # v1.14.3（P2-4）：读取+合成全程持任务锁——期间 delete/reorder 排队，PDF 页面内容一致；
    # 合成完即释放，网络传输不持锁（PDF 一次生成到内存，最适合此方案）
    with jobs.job_lock(job):
        # v1.15.1-tmp04：转换中页是 0 字节占位，混入 PDF 会产生空白页
        # v1.16.1-tmp04（DeepSeek 九轮 P1-4）：拒绝时同步写登记，前端按钮立即恢复
        if scanner.has_pending(job):
            _pack_prog_start(job, "pdf", done=0, total=0, active=False,
                             msg="有页面正在转换，请稍后重试")
            return jsonify(ok=False, msg="有页面正在转换，请等待完成后再下载 PDF"), 409
        base = jobs.path(job)
        files = jobs.pages(job)
        if not files:
            abort(404)
        if len(files) > MAX_PDF_PAGES:
            abort(413)
        # v1.16.0-tmp07：进度总量 = 页数（用户直觉），不再用估算+合成 2N 步
        # （7 页显示 14 步对用户无意义）；估算阶段是快速内存检查、不占步数
        _pack_prog_start(job, "pdf", done=0, total=len(files), active=True)
        try:
            # v1.14：按像素估算合成内存（Pillow 惰性读头不载位图），超限拒绝防 ARM 盒子 OOM（#5）
            # v1.16.0-tmp01：webp 页经 ImageMagick 转临时 PNG 再估（盒子 Pillow 无 libwebp 解码）
            approx = 0
            for f in files:
                with jobs.open_page_fd(base, f) as fh:
                    try:
                        with Image.open(fh) as im:
                            approx += im.width * im.height * 3
                    except Exception:
                        if not f.endswith(".webp"):
                            raise
                        # v1.16.0-tmp04（P1-3）：安全副本转换（不再裸路径交 ImageMagick）
                        tmp = _webp_safe_png(base, f)
                        try:
                            with Image.open(tmp) as im:
                                approx += im.width * im.height * 3
                        finally:
                            os.remove(tmp)
            if approx > MAX_PDF_MEM:
                abort(413)
            imgs = []
            for i, f in enumerate(files, 1):   # v1.14.1（P2-14）+ v1.14.5（P1-2）：with 显式关闭文件句柄；open_page_fd O_NOFOLLOW 防页面被外部替换为链接越权读
                if f.endswith(".webp"):
                    # 盒子 Pillow 无 webp 解码 → ImageMagick 转临时 PNG 合成
                    # v1.16.0-tmp04（P1-3）：安全副本转换（不再裸路径交 ImageMagick）
                    tmp = _webp_safe_png(base, f)
                    try:
                        with Image.open(tmp) as src:
                            imgs.append(src.convert("RGB"))
                    finally:
                        os.remove(tmp)
                else:
                    with jobs.open_page_fd(base, f) as fh, Image.open(fh) as src:
                        imgs.append(src.convert("RGB"))
                _pack_prog_update(job, "pdf", done=i)   # tmp07：合成阶段逐页推进
            buf = io.BytesIO()
            imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
        finally:
            _pack_prog_update(job, "pdf", active=False)   # tmp06：413/异常/完成统一收尾
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


def _start_dev_cache_refresher():
    """v1.51-tmp03：后台线程每 240s 刷新 _dev_cache（TTL 300s 的 80%），
    确保扫描时缓存不过期——消除 scanimage -L 的 12s 冷启动。
    scanimage -L 不占 scan_lock，与正在进行的扫描不冲突。"""
    from config import DEVICE
    def _loop():
        while True:
            try:
                scanner._resolve_device(DEVICE)
            except Exception:
                pass
            time.sleep(240)
    threading.Thread(target=_loop, daemon=True).start()


if __name__ == "__main__":
    scanner.cleanup_tmp_pnms()           # v1.16.0-tmp02：残留 PNM 按归属归档进任务目录（无主才删）
    scanner.start_convert_worker()       # v1.16.0-tmp02：启动唯一常驻转换 worker
    # v1.16.0-tmp05（T04-04，审查报告）：先清 0 字节占位页再恢复 PNM——
    # 若顺序颠倒，恢复逻辑看到占位文件存在即跳过，占位随后被清，PNM 永不恢复
    scanner.cleanup_empty_pages()        # v1.15.1-tmp04：清理 0 字节占位页（转换中断残留）
    scanner.recover_task_pnms()         # v1.16.0-tmp04（P2-9）+tmp05（T04-04）：遗留 PNM 重新入队（幂等，拒 0 字节输出）
    jobs.cleanup()                       # 启动时执行一次清理（锁定任务永不动）
    deploy_examples()                    # 输出目录为空时部署内置示例任务
    admin.start_cleanup_scheduler()      # 后台每小时检查一次
    _start_dev_cache_refresher()         # v1.51-tmp03：保活 _dev_cache
    serve(app, host=BIND, port=PORT, threads=8)
