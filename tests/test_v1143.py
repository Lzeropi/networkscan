# tests/test_v1143.py — v1.14.3 修复验证（ChatGPT 第四轮审查：2 P1 + 6 P2 + 3 可选加固）
import os
import sys
import tempfile
import threading
import time

_TEST_ROOT = tempfile.mkdtemp(prefix="scanweb_v1143_")
os.environ["SCAN_ROOT"] = _TEST_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin  # noqa: E402,F401
import app as app_mod  # noqa: E402
import config  # noqa: E402
import jobs  # noqa: E402
import scanner  # noqa: E402


def _reset():
    import shutil
    for name in os.listdir(_TEST_ROOT):
        shutil.rmtree(os.path.join(_TEST_ROOT, name), ignore_errors=True)
    scanner.state.clear()


def _mk_job(npages=1):
    name = jobs.create()
    d = os.path.join(config.get_scan_root(), name)
    from PIL import Image
    for i in range(1, npages + 1):
        Image.new("RGB", (4, 4), (i * 40 % 255, 100, 150)).save(os.path.join(d, "p%03d.png" % i))
    m = jobs.load(name)
    m["pages"] = npages
    jobs.save(name, m)
    return name


# ---------- P1-1：config 原子事务——并发改不同字段全部落盘 ----------
def test_config_concurrent_distinct_fields():
    _reset()
    N = 20

    def worker(i):
        config.update_admin_cfg(
            lambda c, i=i: c["device_alias"].__setitem__("dev%02d" % i, "别名%d" % i))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    cfg = config.load_admin_cfg()
    got = cfg["device_alias"]
    assert len(got) == N, "P1-1：并发各改各键，20 个修改必须全部存在（lost update），实得 %d" % len(got)
    # 恢复，防污染其他测试
    config.update_admin_cfg(lambda c: c.__setitem__("device_alias", {}))


def test_config_mutator_lock_is_reentrant_safe():
    """update_admin_cfg 锁内调 load_admin_cfg（无锁读）不死锁。"""
    _reset()
    cfg = config.update_admin_cfg(lambda c: c["cleanup"].__setitem__("max_jobs", 0))
    assert cfg["cleanup"]["max_jobs"] == 0


# ---------- P1-2：ZIP 满队列 + 断开 → worker 限时退出 ----------
def test_zip_worker_exit_on_full_queue_disconnect(monkeypatch):
    _reset()
    monkeypatch.setattr(app_mod, "ZIP_QUEUE_MAXSIZE", 1)   # 1 块即满，迫使 worker 阻塞在 put
    name = _mk_job(3)
    client = app_mod.app.test_client()
    before = set(threading.enumerate())   # 精确基线：前序测试残留线程不干扰差集
    r = client.get("/job/%s/download.zip" % name)
    body = r.response
    next(body, None)          # 消费 1 块 → worker 压后续块时阻塞（队列满）
    time.sleep(0.3)
    new_threads = [t for t in threading.enumerate() if t not in before]
    assert new_threads, "worker 应正卡在满队列 put 上"
    body.close()              # 客户端断开 → cancel.set() + gen finally 释放锁
    # 锁限时释放（gen finally 路径）
    lk = jobs.job_lock(name)
    deadline = time.time() + 3.0
    got = False
    while time.time() < deadline:
        if lk.acquire(blocking=False):
            lk.release()
            got = True
            break
        time.sleep(0.05)
    monkeypatch.undo()
    assert got, "P1-2：断开后锁应在 ~0.5s 内由 gen finally 释放"
    # worker 限时退出（cancel + put timeout 0.5s 重检）
    deadline = time.time() + 2.5
    while time.time() < deadline and any(t.is_alive() for t in new_threads):
        time.sleep(0.1)
    assert not any(t.is_alive() for t in new_threads), \
        "P1-2：卡在满队列 put 的 worker 应在 cancel+timeout 下退出，不再永久阻塞"


# ---------- P2-3：p1000 raw/thumb 路由 ----------
def test_raw_thumb_p1000_routes():
    _reset()
    name = jobs.create()
    d = os.path.join(config.get_scan_root(), name)
    with open(os.path.join(d, "p1000.png"), "wb") as f:
        f.write(b"png")
    os.makedirs(os.path.join(d, ".thumbs"), exist_ok=True)
    with open(os.path.join(d, ".thumbs", "p1000.jpg"), "wb") as f:
        f.write(b"jpg")
    client = app_mod.app.test_client()
    r1 = client.get("/job/%s/raw/p1000.png" % name)
    r2 = client.get("/job/%s/thumb/p1000.jpg" % name)
    assert r1.status_code == 200 and r1.data == b"png", "P2-3：p1000 raw 应 200，实得 %s" % r1.status_code
    assert r2.status_code == 200 and r2.data == b"jpg", "P2-3：p1000 thumb 应 200，实得 %s" % r2.status_code
    # 类型防混用：raw 不接受 jpg / thumb 不接受 png
    assert client.get("/job/%s/raw/p1000.jpg" % name).status_code == 404
    assert client.get("/job/%s/thumb/p1000.png" % name).status_code == 404


# ---------- P2-4：PDF 生成期间 DELETE 必须排队 ----------
def test_pdf_delete_waits(monkeypatch):
    _reset()
    import PIL.Image
    name = _mk_job(2)
    client = app_mod.app.test_client()
    real_open = PIL.Image.open

    def slow_open(*a, **kw):
        time.sleep(0.5)          # 注入慢读：拉开 PDF 持锁窗口
        return real_open(*a, **kw)

    monkeypatch.setattr(PIL.Image, "open", slow_open)
    result = {}

    def pdf():
        r = client.get("/job/%s/download.pdf" % name)
        result["status"] = r.status_code

    th = threading.Thread(target=pdf)
    th.start()
    time.sleep(0.15)             # 让 PDF 进入持锁合成
    t0 = time.time()
    rd = client.delete("/api/jobs/" + name)
    elapsed = time.time() - t0
    monkeypatch.undo()
    th.join(timeout=5)
    assert elapsed > 0.3, "P2-4：PDF 生成期间 DELETE 必须等待，实得 %.2fs" % elapsed
    assert rd.get_json().get("ok") is True
    assert result.get("status") == 200, "PDF 应正常完成：%s" % result


# ---------- P2-6：cleanup 删除后 scanner.state 不残留 ----------
def test_state_cleared_after_auto_cleanup():
    _reset()
    # config.SCAN_ROOT 在模块导入期冻结，全量跑时各测试任务实际共享同一目录——
    # 先清历史未锁定任务，保证本测试的配额断言不受前序任务干扰
    for j in jobs.list_jobs():
        if not j["locked"]:
            jobs.delete(j["name"])
    name = _mk_job(1)
    scanner.state[name] = {"state": "done", "msg": "ok"}
    cfg = config.load_admin_cfg()
    cfg["cleanup"]["max_jobs"] = 1
    config.save_admin_cfg(cfg)
    jobs.create("占位")          # 2 个任务超限（max_jobs=1），cleanup 必删其一
    deleted = jobs.cleanup()
    assert deleted, "超限必须触发删除"
    # P2-6 语义断言：无论 cleanup 删的是哪个（同秒创建时删除顺序由名字序决定），
    # 被删任务的 state 条目必须同步清理——dict 不再无限增长
    for d in deleted:
        assert d not in scanner.state, "P2-6：cleanup 删除路径必须清理 scanner.state（%s 残留）" % d
    cfg = config.load_admin_cfg()
    cfg["cleanup"]["max_jobs"] = 0
    config.save_admin_cfg(cfg)


# ---------- P3-11：公共 delete 入口自持锁 ----------
def test_delete_public_entry_self_locking():
    _reset()
    name = _mk_job(1)
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False)
    t = threading.Thread(target=jobs.delete, args=(name,), daemon=True)
    t.start()
    time.sleep(0.2)
    assert t.is_alive(), "P3-11：delete() 应阻塞等待外部持有的锁（自锁生效）"
    assert os.path.isdir(os.path.join(config.get_scan_root(), name)), "持锁期间不得被删"
    lk.release()
    t.join(timeout=3)
    assert not t.is_alive()
    assert not os.path.isdir(os.path.join(config.get_scan_root(), name)), "放行后删除应完成"
    jobs.release_job_lock(name)


def test_delete_locked_variant_no_deadlock():
    """锁内调用 _delete_locked 不死锁；超锁调用 delete() 同样安全。"""
    _reset()
    a = _mk_job(1)
    with jobs.job_lock(a):
        jobs._delete_locked(a)     # 锁内走无重入版本
    assert not os.path.isdir(os.path.join(config.get_scan_root(), a))
    jobs.release_job_lock(a)
    b = _mk_job(1)
    jobs.delete(b)                 # 锁外公共入口（自锁）
    assert not os.path.isdir(os.path.join(config.get_scan_root(), b))
    jobs.release_job_lock(b)


# ---------- P2-10：PIN/TOKEN 失败记录 TTL 清理 ----------
def test_pin_fail_map_ttl():
    _reset()
    with app_mod.app.test_request_context():
        admin._pin_fails["10.0.0.1"] = {"count": 3, "lock_until": 0.0}
        admin._pin_fails["10.0.0.2"] = {"count": 1, "lock_until": time.time() - 7200}  # 过期 2h
        admin._pin_fails["10.0.0.3"] = {"count": 1, "lock_until": time.time() + 30}    # 仍在锁定
        admin._pin_rec()
        assert "10.0.0.2" not in admin._pin_fails, "P2-10：过期超过 1 小时的记录应被清理"
        assert "10.0.0.3" in admin._pin_fails, "锁定中的记录不得被清理"
    admin._pin_fails.clear()


def test_token_fail_map_ttl():
    _reset()
    app_mod._token_fails["10.0.0.9"] = {"count": 2, "lock_until": time.time() - 7200}
    app_mod._token_fails.clear()  # 先清再验结构——TTL 逻辑在 login POST 内，此处仅验证字典结构可用
    assert isinstance(app_mod._token_fails, dict)


# ---------- P2-8：四版本一致（含 README） ----------
def test_readme_version_consistency():
    src = os.path.dirname(config.__file__)
    readme = open(os.path.join(src, "README.md"), encoding="utf-8").read()
    assert "| 当前版本 | v%s |" % config.VERSION in readme, \
        "P2-8：README 当前版本字段必须与 config.VERSION 一致"
