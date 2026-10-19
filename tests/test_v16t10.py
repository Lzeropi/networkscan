# tests/test_v16t10.py — v1.16.1-tmp01：DeepSeek 第六轮审查 × 遗留问题联合修复验证
# ① fd 泄漏对称性：_convert_pnm/_mk_thumb 的 mkstemp→fdopen 失败关 fd（_fdopen_w/_mkstemp_w）
# ② _pack_prog_update 不重建已删任务条目（gen finally 时序竞态）
# ③ api_delete 403/500 异常路径锁条目统一回收（不残留 _job_locks）
# ④ 架构图 EDGES 无同源同目标重复边（flask→scanimage 两条重合曲线）
# ⑤ SVG 导出 xesc 转义存在（& < > 防注入）
# ⑥ pyproject pillow 上限与实测部署一致（<13）
import os
import shutil
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402
import config          # noqa: E402
import jobs            # noqa: E402
import scanner         # noqa: E402
from PIL import Image  # noqa: E402


def _mk_job(params=None):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    scanner._reset_convert_for_test()
    with app_mod._pack_lock:
        app_mod._pack_progress.clear()
    return jobs.create("t", params or {})


def _ppm(path):
    Image.new("RGB", (8, 8), (10, 120, 210)).save(path, "PPM")
    return path


# ---------- ① fdopen 失败关 fd：_fdopen_w / _convert_pnm / _mk_thumb 三点 ----------

def test_fdopen_w_closes_fd_on_failure(monkeypatch):
    """_fdopen_w：fdopen 抛异常（EMFILE 模拟）时 fd 必须被关闭再上抛。"""
    closed = []
    real_fdopen = os.fdopen

    def boom(fd, *a, **kw):
        raise OSError(24, "Too many open files")   # EMFILE

    monkeypatch.setattr(os, "fdopen", boom)
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    try:
        scanner._fdopen_w(12345, "wb")
        raise AssertionError("应当上抛 OSError")
    except OSError:
        pass
    finally:
        monkeypatch.setattr(os, "fdopen", real_fdopen)
    assert 12345 in closed, "fdopen 失败必须关闭 fd（fd 泄漏对称性）"


def test_convert_pnm_safe_copy_no_fd_leak(monkeypatch, tmp_path):
    """_convert_pnm 回退分支：fdopen("wb") 失败（EMFILE）时 mkstemp 的 fd 必须被关闭。
    bad.pnm 使 PIL 主分支失败 → 进 ImageMagick 回退 → _mkstemp_w 得 _sfd →
    _fdopen_w(_sfd, "wb") 抛 OSError(24)。
    按模式区分失败点："rb"（_open_no_follow 读端）放行，"wb"（安全副本写端）失败。"""
    bad = str(tmp_path / "bad.pnm")
    open(bad, "w").write("NOT-A-PNM")
    dst = str(tmp_path / "out.png")
    real_fdopen = os.fdopen
    real_close = os.close
    fd_seen, closed = [], []

    def selective(fd, mode=None, *a, **kw):
        if mode == "wb":
            fd_seen.append(fd)
            raise OSError(24, "Too many open files")
        return real_fdopen(fd, mode, *a, **kw)

    monkeypatch.setattr(os, "fdopen", selective)

    def close_spy(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "close", close_spy)
    try:
        try:
            scanner._convert_pnm(bad, dst, "png")
            raise AssertionError("应当上抛 OSError")
        except OSError:
            pass
    finally:
        monkeypatch.setattr(os, "fdopen", real_fdopen)
        monkeypatch.setattr(os, "close", real_close)
    assert fd_seen, "未捕获 mkstemp fd（fdopen 未被调用？）"
    leaked = [fd for fd in fd_seen if fd not in closed]
    assert not leaked, "mkstemp→fdopen 失败路径 fd 泄漏：%s" % leaked


# ---------- ② _pack_prog_update 不重建已删任务条目 ----------

def test_pack_prog_update_does_not_recreate_deleted():
    """条目被 pop（任务删除）后 update 不重建——gen finally 时序竞态闭环。"""
    job = _mk_job()
    app_mod._pack_prog_start(job, "zip", done=0, total=3, active=True)
    # api_delete 路径的清理
    with app_mod._pack_lock:
        app_mod._pack_progress.pop(job, None)
    # gen finally 收尾（时序窗口内：release 后才走到）
    app_mod._pack_prog_update(job, "zip", active=False)
    assert job not in app_mod._pack_progress, \
        "已删任务的进度条目不应被收尾 update 重建（毫秒级竞态残留）"


def test_pack_prog_update_updates_existing():
    """正常路径：条目存在时 update 照常生效（不破坏功能）。"""
    job = _mk_job()
    app_mod._pack_prog_start(job, "pdf", done=0, total=2, active=True)
    app_mod._pack_prog_update(job, "pdf", done=1)
    app_mod._pack_prog_update(job, "pdf", active=False)
    rec = app_mod._pack_progress[job]["pdf"]
    assert rec["done"] == 1 and rec["active"] is False


def test_pack_prog_update_no_entry_noop():
    """无任何条目时 update 是 no-op（不抛错、不创建）。"""
    job = _mk_job()
    app_mod._pack_prog_update(job, "zip", done=5, active=False)
    assert app_mod._pack_progress.get(job) is None


# ---------- ③ api_delete 异常路径锁回收（三路径统一 finally） ----------

def test_api_delete_403_path_lock_reclaimed(monkeypatch):
    """锁定 403 路径：请求结束后 _job_locks 条目回收，后续操作可用新锁。"""
    job = _mk_job()
    jobs.set_locked(job, True)

    client = app_mod.app.test_client()
    r = client.delete("/api/jobs/%s" % job)
    assert r.status_code == 403, "锁定任务删除应 403，实际 %s" % r.status_code
    # with 退出 refs 归零自动回收 + finally release_job_lock 兜底 → 条目不残留
    assert job not in jobs._job_locks, \
        "403 异常路径锁条目残留（_job_locks 膨胀）：%s" % job
    jobs.set_locked(job, False)
    r2 = client.delete("/api/jobs/%s" % job)
    assert r2.status_code == 200, "解锁后删除应成功，实际 %s" % r2.status_code


def test_api_delete_500_path_lock_reclaimed(monkeypatch):
    """目录权限异常 500 路径：锁条目同样回收。"""
    job = _mk_job()

    def fail_rmtree(p):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(jobs.shutil, "rmtree", fail_rmtree)
    client = app_mod.app.test_client()
    r = client.delete("/api/jobs/%s" % job)
    assert r.status_code == 500, "rmtree 失败应 500，实际 %s" % r.status_code
    assert job not in jobs._job_locks, \
        "500 异常路径锁条目残留（_job_locks 膨胀）：%s" % job


def test_api_delete_normal_path_lock_reclaimed():
    """正常路径回归：删除成功后锁条目不残留（防 finally 重构引入回归）。"""
    job = _mk_job()
    client = app_mod.app.test_client()
    r = client.delete("/api/jobs/%s" % job)
    assert r.status_code == 200
    assert job not in jobs._job_locks, "正常路径锁条目残留：%s" % job


# ---------- ④⑤ 架构图：无重复边 + xesc 转义（文件级断言） ----------

def test_architecture_no_duplicate_edge():
    """EDGES 无同源同目标重复（v1.16.1-tmp01 六轮 4.1：flask→scanimage 两条重合）。"""
    html = open(os.path.join(os.path.dirname(config.__file__),
                             "static", "architecture.html"), encoding="utf-8").read()
    import re
    body = html.split("const EDGES=", 1)[1].split("];", 1)[0]
    edges = re.findall(r'\{from:"(\w+)",to:"(\w+)",label:"[^"]*"\}', body)
    assert edges, "EDGES 解析失败"
    seen = set()
    for e in edges:
        assert e not in seen, "重复边（曲线完全重合、label 叠压）：%s" % (e,)
        seen.add(e)


def test_architecture_svg_export_escapes():
    """svgString 用 xesc 转义 label/detail（& < > 不再裸拼进 SVG）。"""
    html = open(os.path.join(os.path.dirname(config.__file__),
                             "static", "architecture.html"), encoding="utf-8").read()
    assert "xesc=n=>" in html or "xesc = s =>" in html or "xesc=s=>" in html, "缺 xesc 定义"
    assert "xesc(n.dataset.label)" in html and "xesc(n.dataset.detail)" in html, \
        "label/detail 拼接未走 xesc 转义"


# ---------- ⑥ pyproject pillow 约束 ----------

def test_pyproject_pillow_range():
    """pillow 上限与实测部署（12.3.0）一致：>=7.0,<13。
    v1.16.1-tmp02（DeepSeek 七轮 2.1）：pyproject 与 README 双向一致——
    tmp01 只改 pyproject 漏了 README 6 处 <9（用户按 README 装会得到 8.x，
    与 pyproject 声明脱钩），当时无一致性测试所以 207 项全绿也没拦住。
    v1.16.1-tmp03（DeepSeek 八轮 2.2）：改遍历式断言——tmp01 漏 README、
    tmp02 漏 admin-manual.html，同类漏改连犯两次，根因是断言靠人工列举
    文件名。改为「任何含 pillow 约束的文档都不得残留旧上限」，不再点名。"""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    src = os.path.dirname(config.__file__)
    with open(os.path.join(src, "pyproject.toml"), "rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    pillow = next(d for d in deps if d.lower().startswith("pillow"))
    assert "<13" in pillow and ">=7" in pillow, \
        "pillow 约束与实测部署错配（12.3.0 实测通过，14 移除 getdata）：%s" % pillow
    readme = open(os.path.join(src, "README.md"), encoding="utf-8").read()
    assert 'pillow>=7.0,<13' in readme, "README 安装命令未同步 pillow <13"
    # v1.16.1-tmp03：依赖表格行同样封上限（7.x~ 无上界声明会误导装到未测版本）
    assert "7.x~" not in readme, "README 依赖表格 Pillow 行残留无上限写法（7.x~）"
    # v1.16.1-tmp03（八轮 2.2）：遍历 README + 全部模板——HTML 转义（&lt;9）
    # 与裸文本（<9）两种形态都拦；含 pillow 的文档必须出现新上限 <13
    import glob
    docs = [os.path.join(src, "README.md")] + \
        glob.glob(os.path.join(src, "templates", "*.html"))
    stale_forms = ("pillow>=7.0,<9", "pillow>=7.0,&lt;9",
                   "pillow>&gt;7.0,<9", "pillow>&gt;7.0,&lt;9")
    for doc in docs:
        if not os.path.isfile(doc):
            continue
        body = open(doc, encoding="utf-8").read()
        if "pillow" not in body.lower():
            continue                       # 不含 pillow 约束的文档跳过
        for stale in stale_forms:
            assert stale not in body, \
                "%s 残留旧约束 %s（按此装会得到 8.x，与 pyproject 脱钩）" % (
                    os.path.basename(doc), stale)
        fresh = ("pillow>=7.0,<13" in body) or ("pillow>=7.0,&lt;13" in body) \
            or ("≥ 7.0, < 13" in body)
        assert fresh, "%s 含 pillow 但未见新上限 <13" % os.path.basename(doc)


# ==================== v1.16.1-tmp04：DeepSeek 第九轮审查 P1 修复验证 ====================
# P1-1：_JobLock.release 误释放时 refs 不递减（防提前回收 waiter race 变体）
# P1-2：dl_zip worker DONE 投递 while 重试（慢客户端不丢 DONE）
# P1-4：409 拒绝路径同步写进度登记（前端按钮 500ms 恢复+提示）
# P2-1：admin 保存配置遍历 state 用 dict() 原子快照

# ---------- P1-1：误 release 不递减 refs ----------

def test_joblock_bad_release_keeps_refs():
    """未持锁调 release（RuntimeError 路径）——refs 不得递减，条目不提前回收。
    注：threading.Lock 任何线程都能 release 被持有的锁（lk1 acquire 后 lk2
    release 是合法操作），只有 release 空闲锁才 RuntimeError——本用例两 handle
    均不 acquire，直接误释放。"""
    name = "20261013-000000_badrel_t"
    lk1 = jobs.job_lock(name)          # refs=1
    lk2 = jobs.job_lock(name)          # refs=2
    try:
        lk1.release()                  # 底层锁空闲 → RuntimeError（误释放）
        raise AssertionError("释放空闲锁应上抛 RuntimeError")
    except RuntimeError:
        pass
    with jobs._job_locks_guard:
        e = jobs._job_locks.get(name)
        assert e is not None and e["refs"] == 2, \
            "误 release 后 refs 不应递减（P1-1：防条目提前回收），实际 refs=%r" % (
                e["refs"] if e else "条目已消失")
    # 正常路径清理：abandon 归零回收（M4 防膨胀目标不受影响）
    lk1.abandon()                      # refs=1
    lk2.abandon()                      # refs=0 → pop
    assert name not in jobs._job_locks, "abandon 归零后条目应回收"


# ---------- P1-2：DONE 投递为 while 重试（源码级断言 + 行为） ----------

def test_dl_zip_done_retry_loop(monkeypatch):
    """worker finally 的 DONE 投递是 while 重试——put 超时不再静默丢失。
    行为验证：小队列 + 不消费，worker 投递必须反复重试直至 cancel。"""
    src = open(os.path.join(os.path.dirname(config.__file__), "app.py"),
               encoding="utf-8").read()
    body = src.split("def dl_zip", 1)[1].split("def _webp_safe_png", 1)[0]
    assert "while not cancel.is_set():" in body, "DONE 投递应为 while 重试循环（P1-2）"
    assert "qu.put(DONE, timeout=5)" not in body, "旧版单次 5s 超时投递应已移除"
    assert "except q.Full:" in body, "队列满应继续重试而非放弃"


# ---------- P1-4：409 拒绝写进度登记 ----------

def test_dl_zip_409_registers_progress():
    """转换中（has_pending）时 dl_zip 409 且登记 active=False+msg——
    前端轮询 500ms 内即可恢复按钮并提示（不再页面灰 5 分钟/跳 JSON）。"""
    name = _mk_job()
    # 造一个 pending 页（模拟转换中）
    with scanner._pending_lock:
        scanner._pending.setdefault(name, set()).add("p001.png")
    c = app_mod.app.test_client()
    r = c.get("/job/%s/download.zip" % name)
    assert r.status_code == 409, "转换中下载应 409，实际 %s" % r.status_code
    p = app_mod._pack_progress.get(name, {}).get("zip")
    assert p is not None and p["active"] is False, \
        "409 必须同步写登记（P1-4）：实际 %r" % p
    assert p.get("msg"), "登记应含拒绝原因 msg"
    # 轮询接口返回该登记（前端按钮据此立即恢复）
    pr = c.get("/api/jobs/%s/pack-progress" % name)
    assert pr.get_json()["zip"]["active"] is False
    # 清理
    with scanner._pending_lock:
        scanner._pending.pop(name, None)


def test_dl_pdf_409_registers_progress():
    """PDF 合成同理：409 拒绝时写 kind=pdf 登记含 msg。"""
    name = _mk_job()
    with scanner._pending_lock:
        scanner._pending.setdefault(name, set()).add("p001.png")
    c = app_mod.app.test_client()
    r = c.get("/job/%s/download.pdf" % name)
    assert r.status_code == 409
    p = app_mod._pack_progress.get(name, {}).get("pdf")
    assert p is not None and p["active"] is False and p.get("msg")
    with scanner._pending_lock:
        scanner._pending.pop(name, None)


def test_dl_with_progress_uses_iframe():
    """前端下载走隐藏 iframe（不再 location.href 导航 409 JSON 页面）。
    v1.16.3-tmp05（P3-5，纯代码审查）：追加断言——不复用单例 iframe（替换
    src 会中断前一个进行中的下载），且必须有延迟清理防 DOM 无限增长。"""
    js = open(os.path.join(os.path.dirname(config.__file__),
                           "static", "app.js"), encoding="utf-8").read()
    body = js.split("async function dlWithProgress", 1)[1].split("\n}", 1)[0]
    assert "location.href = url" not in body, "应已移除 location.href 导航（P1-4）"
    assert 'document.createElement("iframe")' in body, "应改 iframe 触发下载"
    assert "stop(p.msg)" in body, "轮询应识别登记的 msg 并提示"
    assert 'getElementById("dl_frame")' not in body, \
        "不应复用单例 iframe（P3-5：替换 src 会中断首个进行中的下载）"
    assert "f.remove()" in body, \
        "独立 iframe 应有延迟清理（P3-5：防 DOM 无限增长）"


# ---------- P2-1：admin state 遍历用 dict() 原子快照 ----------

def test_admin_save_config_uses_dict_snapshot():
    """api_save_config 遍历 scanner.state 用 dict()（原子快照，防迭代中插键）。"""
    src = open(os.path.join(os.path.dirname(config.__file__), "admin.py"),
               encoding="utf-8").read()
    assert "list(scanner.state.values())" not in src, \
        "list(values()) 迭代中插键仍会 RuntimeError（P2-1）"
    assert "dict(scanner.state)" in src, "应改 dict() 原子整表浅拷贝"


# ---------- P1-3：thumb 长轮询上限 20s（源码级断言） ----------

def test_thumb_longpoll_timeout_capped():
    """thumb 长轮询 deadline 20s——8 线程池下 ADF 多页并发缩略图不再吊死应用。
    v1.16.1-tmp07（十二轮 P2-F）：改 ast 按函数名定位——不再依赖
    "def thumb(job, fname):" 字面量与 def dl_zip 定义顺序（签名加类型注解或
    重排函数定义会让旧 string split 静默 IndexError 而非明确断言失败）。"""
    import ast
    src = open(os.path.join(os.path.dirname(config.__file__), "app.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    thumb = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "thumb"), None)
    assert thumb is not None, "app.py 中未找到 thumb 函数"
    found = False
    for node in ast.walk(thumb):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "deadline"
                        for t in node.targets)
                and isinstance(node.value, ast.BinOp)
                and isinstance(node.value.op, ast.Add)
                and isinstance(node.value.right, ast.Constant)
                and node.value.right.value == 20):
            found = True
            break
    assert found, "thumb 长轮询应 20s 封顶（P1-3）"
