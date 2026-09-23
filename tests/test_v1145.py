# tests/test_v1145.py — v1.14.5 修复验证（ChatGPT 第六轮审查：2 P1 + 3 P2 + 2 P3 全修）
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402,F401
import config          # noqa: E402,F401
import jobs            # noqa: E402,F401
import scanner         # noqa: E402,F401


def _mk_job(letters, with_thumbs=False):
    """造任务：p001..pN 内容依次为 letters（byte per page）。"""
    import shutil
    for name in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), name), ignore_errors=True)
    scanner.state.clear()
    name = jobs.create("t")
    d = os.path.join(config.get_scan_root(), name)
    for i, c in enumerate(letters, 1):
        with open(os.path.join(d, "p%03d.png" % i), "wb") as f:
            f.write(c.encode())
        if with_thumbs:
            os.makedirs(os.path.join(d, ".thumbs"), exist_ok=True)
            with open(os.path.join(d, ".thumbs", "p%03d.jpg" % i), "wb") as f:
                f.write(("thumb-" + c).encode())
    m = jobs.load(name)
    m["pages"] = len(letters)
    jobs.save(name, m)
    return name


# ---------- P1-1：ADF _params 异常双锁泄漏根治 ----------
def test_adf_params_exception_releases_locks(monkeypatch):
    name = _mk_job(["A"])
    def boom(job):
        raise RuntimeError("boom-meta")
    monkeypatch.setattr(scanner, "_params", boom)
    assert scanner.scan_adf(name) == "started"
    for _ in range(60):               # 等 worker 死亡（daemon 线程）
        if scanner.scan_lock.acquire(blocking=False):
            scanner.scan_lock.release()
            break
        time.sleep(0.02)
    else:
        assert False, "P1-1：scan_lock 未释放（锁泄漏未修）"
    jl = jobs.job_lock(name)
    assert jl.acquire(blocking=False), "P1-1：job_lock 未释放"
    jl.release()
    st = scanner.get_state(name)
    assert st["state"] == "error", "P1-1：异常必须落 error，实际 %s" % st["state"]


# ---------- P1-2：open_page_fd O_NOFOLLOW 拒 symlink ----------
def test_open_page_fd_rejects_symlink():
    name = _mk_job(["A"])
    base = jobs.path(name)
    os.symlink("/etc/passwd", os.path.join(base, "p_link.png"))
    try:
        jobs.open_page_fd(base, "p_link.png")
        assert False, "P1-2：symlink 必须被 O_NOFOLLOW 拒绝"
    except jobs.JobError:
        pass


# ---------- P1-2：open_page_fd 拒非普通文件（FIFO/目录） ----------
def test_open_page_fd_rejects_non_regular():
    name = _mk_job(["A"])
    base = jobs.path(name)
    os.mkfifo(os.path.join(base, "fifo.png"))
    try:
        jobs.open_page_fd(base, "fifo.png")
        assert False, "P1-2：非普通文件必须拒绝"
    except jobs.JobError:
        pass


# ---------- P1-2：raw API 对 symlink 页 404（集成层） ----------
def test_raw_symlink_404():
    name = _mk_job(["A"], with_thumbs=True)
    base = jobs.path(name)
    os.remove(os.path.join(base, "p001.png"))
    os.symlink("/etc/passwd", os.path.join(base, "p001.png"))
    r = app_mod.app.test_client().get(f"/job/{name}/raw/p001.png")
    assert r.status_code == 404, "P1-2：raw 对 symlink 页必须 404，实际 %s" % r.status_code


# ---------- P2-1：不存在任务 reorder 不泄漏锁条目（B2 refs 自动回收） ----------
def test_reorder_missing_job_no_lock_leak():
    before = len(jobs._job_locks)
    client = app_mod.app.test_client()
    for i in range(50):
        client.post("/api/jobs/nonexist%03d_x/reorder", json={"order": ["p001.png"]})
    assert len(jobs._job_locks) == before, "P2-1：不存在任务 reorder 不应残留锁条目"


# ---------- P2-1：不存在任务 PDF 不泄漏锁条目（dl_pdf 是 with job_lock: path 模式） ----------
def test_pdf_missing_job_no_lock_leak():
    before = len(jobs._job_locks)
    client = app_mod.app.test_client()
    for i in range(50):
        client.get(f"/job/nonexist{i:03d}_x/download.pdf")
    assert len(jobs._job_locks) == before, "P2-1：不存在任务 PDF 不应残留锁条目"


# ---------- P2-2：refs 计数——引用未释放时 release_job_lock 不回收（waiter race 闭环） ----------
def test_job_lock_refs_not_collected_with_ref():
    name = jobs.create("t")
    lk = jobs.job_lock(name)            # refs=1（拿引用即计数，含尚未 acquire 的等待者）
    jobs.release_job_lock(name)         # refs>0 → 不应回收
    assert name in jobs._job_locks, "P2-2：引用未释放时不应回收（waiter race 未根治）"
    lk.abandon()                         # 放弃引用
    assert name not in jobs._job_locks, "P2-2：引用释放后应自动回收"


# ---------- P2-3：ZIP worker 外部删文件不再静默吞错（记 stderr） ----------
def test_zip_worker_logs_external_delete(capsys, monkeypatch):
    name = _mk_job(["A", "B"])
    import zipfile
    def boom_write(self, path, arcname=None):
        raise FileNotFoundError("外部删除")
    monkeypatch.setattr(zipfile.ZipFile, "write", boom_write)
    app_mod.app.test_client().get(f"/job/{name}/download.zip")
    err = capsys.readouterr().err
    assert "worker error" in err, "P2-3：外部删文件应记 stderr 日志，实际：%r" % err
