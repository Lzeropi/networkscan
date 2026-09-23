# tests/test_v1144.py — v1.14.4 修复验证（ChatGPT 第五轮审查：P0 reorder 数据丢失 + 3 P1 + P2 收尾）
import os
import sys
import tempfile
import threading
import time

_TEST_ROOT = tempfile.mkdtemp(prefix="scanweb_v1144_")
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


def _mk_job(letters, with_thumbs=False):
    """造任务：p001..pN 内容依次为 letters。"""
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


def _read_pages(d):
    out = {}
    for f in sorted(os.listdir(d)):
        if f.endswith(".png"):
            out[f[:-4]] = open(os.path.join(d, f), "rb").read().decode()
    return out


def _delete(client, name, keep_nums):
    r = client.post("/api/jobs/%s/reorder" % name,
                    json={"order": [f"p{k:03d}.png" for k in keep_nums], "delete": True})
    assert r.get_json().get("ok"), r.get_json()


# ---------- P0：删除模式成功路径内容回归（v1.14.3 的 68 测试全部没覆盖这里） ----------
def test_delete_first_page_keeps_content():
    _reset()
    client = app_mod.app.test_client()
    name = _mk_job(["A", "B", "C"])
    _delete(client, name, [2, 3])
    d = os.path.join(config.get_scan_root(), name)
    assert _read_pages(d) == {"p001": "B", "p002": "C"}, \
        "P0：删首页必须保留 B、C 且内容正确（v1.14.3 实测丢 B）"


def test_delete_middle_page_keeps_content():
    _reset()
    client = app_mod.app.test_client()
    name = _mk_job(["A", "B", "C"])
    _delete(client, name, [1, 3])
    d = os.path.join(config.get_scan_root(), name)
    assert _read_pages(d) == {"p001": "A", "p002": "C"}, "P0：删中间页内容必须正确"


def test_delete_last_page_keeps_content():
    _reset()
    client = app_mod.app.test_client()
    name = _mk_job(["A", "B", "C"])
    _delete(client, name, [1, 2])
    d = os.path.join(config.get_scan_root(), name)
    assert _read_pages(d) == {"p001": "A", "p002": "B"}, "P0：删尾页内容必须正确"


def test_delete_multiple_disjoint_keeps_content():
    _reset()
    client = app_mod.app.test_client()
    name = _mk_job(["A", "B", "C", "D"])
    _delete(client, name, [2, 4])
    d = os.path.join(config.get_scan_root(), name)
    assert _read_pages(d) == {"p001": "B", "p002": "D"}, "P0：删多页不连续内容必须正确"


def test_delete_with_thumbs_content_match():
    _reset()
    client = app_mod.app.test_client()
    name = _mk_job(["A", "B", "C"], with_thumbs=True)
    _delete(client, name, [2, 3])
    d = os.path.join(config.get_scan_root(), name)
    th = {}
    for f in sorted(os.listdir(os.path.join(d, ".thumbs"))):
        if f.endswith(".jpg"):
            th[f[:-4]] = open(os.path.join(d, ".thumbs", f), "rb").read().decode()
    assert th == {"p001": "thumb-B", "p002": "thumb-C"}, \
        "P0：缩略图必须与保留页一一对应（thumb-B 跟着 B 走）"


def test_delete_no_grave_leftover():
    _reset()
    client = app_mod.app.test_client()
    name = _mk_job(["A", "B", "C"])
    _delete(client, name, [2, 3])
    d = os.path.join(config.get_scan_root(), name)
    assert not [f for f in os.listdir(d) if f.startswith("_del_")], "P0：墓也文件不得残留"
    assert jobs.load(name)["pages"] == 2


# ---------- P1：平板扫描 TimeoutExpired 后 state 必须落 error ----------
def test_flatbed_timeout_state_error(monkeypatch):
    _reset()
    fake = os.path.join(_TEST_ROOT, "_stuck_scan")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\nsleep 5\nexit 0\n")
    os.chmod(fake, 0o755)
    monkeypatch.setattr(scanner, "SCANIMAGE", fake)
    monkeypatch.setattr(scanner, "SCAN_CMD_TIMEOUT", 0.3)   # 300s 提常量后测试可注入
    name = jobs.create()
    try:
        scanner.scan_flatbed(name)
        raise AssertionError("超时必须抛 RuntimeError")
    except RuntimeError as e:
        assert "超时" in str(e), e
    st = scanner.get_state(name)
    assert st["state"] == "error", "P1：TimeoutExpired 后不得永久卡 scanning，实际 %s" % st
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False), "P1：异常收尾必须释放 job_lock"
    lk.release()
    assert scanner.scan_lock.acquire(blocking=False), "P1：异常收尾必须释放 scan_lock"
    scanner.scan_lock.release()
    # 异常后 DELETE 不得被 409 死锁
    client = app_mod.app.test_client()
    rd = client.delete("/api/jobs/" + name)
    assert rd.get_json().get("ok") is True, "P1：超时后删除必须成功"


def test_flatbed_oserror_state_error(monkeypatch):
    _reset()
    monkeypatch.setattr(scanner, "SCANIMAGE", "/nonexistent/scanimage")   # OSError 路径
    name = jobs.create()
    try:
        scanner.scan_flatbed(name)
        raise AssertionError("OSError 必须抛 RuntimeError")
    except RuntimeError:
        pass
    st = scanner.get_state(name)
    assert st["state"] == "error", "P1：OSError 后 state 必须落 error，实际 %s" % st
    lk = jobs.job_lock(name)
    assert lk.acquire(blocking=False)
    lk.release()
    jobs.delete(name)


# ---------- P1：PIN 限流并发计数准确 ----------
def test_pin_rate_limit_concurrent():
    _reset()
    config.update_admin_cfg(lambda c: c.__setitem__("pin_hash", admin._hash("9999")))
    errs = []
    codes = []
    lock = threading.Lock()

    def worker():
        try:
            c = app_mod.app.test_client()
            r = c.post("/api/admin/login", json={"pin": "0000"})
            with lock:
                codes.append(r.status_code)
        except Exception as e:                       # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, "P1：并发限流不得抛异常：%s" % errs[:3]
    assert 429 in codes, "P1：20 个并发错误 PIN 必须触发锁定（429），实得 %s" % sorted(set(codes))
    assert set(codes) <= {403, 429}, "P1：仅应出现 403/429，实得 %s" % sorted(set(codes))
    with admin._pin_fail_lock:
        assert len(admin._pin_fails) >= 1
    admin._pin_fails.clear()
    config.update_admin_cfg(lambda c: c.__setitem__("pin_hash", ""))


# ---------- P1：TOKEN 限流并发（真实 /login POST） ----------
def test_token_rate_limit_concurrent_and_ttl():
    _reset()
    monkey_cfg = app_mod.app.test_client()
    # 注入过期与锁定条目，验证真实 POST 触发 TTL 清理
    app_mod._token_fails["10.0.0.2"] = {"count": 1, "lock_until": time.time() - 7200}   # 过期 2h
    app_mod._token_fails["10.0.0.3"] = {"count": 1, "lock_until": time.time() + 30}    # 锁定中
    old_token = app_mod.TOKEN
    app_mod.TOKEN = "secret-1144"   # 启用 TOKEN 登录（require_login 对 login 端点放行）
    try:
        errs, codes = [], []
        lock = threading.Lock()

        def worker():
            try:
                c = app_mod.app.test_client()
                r = c.post("/login", data={"password": "wrong"})
                with lock:
                    codes.append(r.status_code)
            except Exception as e:                   # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=worker) for _ in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errs, "P1：并发 TOKEN 限流不得抛异常：%s" % errs[:3]
        # 锁定后 POST 返回 200（login 页面带错误信息）——以字典状态断言
        locked = [r for r in app_mod._token_fails.values() if r["lock_until"] > time.time()]
        assert locked, "P1：12 个并发错误口令必须触发锁定"
        assert "10.0.0.2" not in app_mod._token_fails, "TTL：过期 2 小时条目应被真实 POST 清理"
        assert "10.0.0.3" in app_mod._token_fails, "TTL：锁定中条目不得清理"
    finally:
        app_mod.TOKEN = old_token
        app_mod._token_fails.clear()


# ---------- P1：symlink 越权拒绝 ----------
def test_symlink_task_dir_rejected():
    _reset()
    client = app_mod.app.test_client()
    secret = os.path.join(_TEST_ROOT, "_secret")
    os.makedirs(secret, exist_ok=True)
    with open(os.path.join(secret, "p001.png"), "wb") as f:
        f.write(b"SECRET")
    name = jobs.create("真任务")
    # 恶意任务目录：symlink 指向外部
    evil = "20260923-235959_evil"
    os.symlink(secret, os.path.join(config.get_scan_root(), evil))
    r = client.get("/job/%s/raw/p001.png" % evil)
    assert r.status_code == 404, "P1：symlink 任务目录必须拒绝访问，实得 %s" % r.status_code
    assert b"SECRET" not in (r.data or b""), "P1：不得读出外部文件内容"
    assert evil not in [j["name"] for j in jobs.list_jobs()], "P1：symlink 任务不得进列表"
    os.remove(os.path.join(config.get_scan_root(), evil))
    rd = client.delete("/api/jobs/" + name)
    assert rd.get_json().get("ok") is True


def test_symlink_page_rejected():
    _reset()
    client = app_mod.app.test_client()
    name = jobs.create("t")
    d = os.path.join(config.get_scan_root(), name)
    secret_file = os.path.join(_TEST_ROOT, "_secret.dat")
    with open(secret_file, "wb") as f:
        f.write(b"TOPSECRET")
    with open(os.path.join(d, "p001.png"), "wb") as f:
        f.write(b"normal")
    os.symlink(secret_file, os.path.join(d, "p002.png"))
    assert "p002.png" not in jobs.pages(name), "P1：symlink 页面不得进列表"
    r = client.get("/job/%s/raw/p002.png" % name)
    assert r.status_code == 404, "P1：symlink 页面 raw 必须拒绝"
    r1 = client.get("/job/%s/raw/p001.png" % name)
    assert r1.status_code == 200 and r1.data == b"normal", "正常页不受影响"
    jobs.delete(name)


# ---------- P2：meta 原子写 ----------
def test_meta_atomic_no_tmp_leftover():
    _reset()
    name = _mk_job(["A"])
    d = os.path.join(config.get_scan_root(), name)
    jobs.save(name, jobs.load(name))
    assert not [f for f in os.listdir(d) if f.startswith("meta.json.tmp")], "P2：原子写不得残留 tmp"
    assert jobs.load(name)["pages"] == 1


# ---------- P3：公共 delete 入口锁条目回收 ----------
def test_public_delete_recycles_lock_entry():
    _reset()
    name = _mk_job(["A"])
    jobs.delete(name)
    assert name not in jobs._job_locks, "P3：公共 delete() 应完整回收锁条目"
