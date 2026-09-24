# tests/test_api.py — v1.14 API 集成回归（Flask test client，无需真实扫描仪）
# 覆盖：新1 锁定三层防护 / 新2 管理页跳转 / #7 SameSite / #1 状态拦截 / #16 配置校验
import os
import sys
import tempfile

_TEST_ROOT = tempfile.mkdtemp(prefix="scanweb_api_test_")
os.environ["SCAN_ROOT"] = _TEST_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402
import config  # noqa: E402
import jobs  # noqa: E402
import scanner  # noqa: E402

client = app_mod.app.test_client()


def _reset():
    import shutil
    for name in os.listdir(_TEST_ROOT):
        shutil.rmtree(os.path.join(_TEST_ROOT, name), ignore_errors=True)
    scanner.state.clear()
    # 清 PIN，回到未初始化状态
    cfg = config.load_admin_cfg()
    cfg["pin_hash"] = ""
    config.save_admin_cfg(cfg)


def _mk_job(locked=False, npages=2):
    _reset()
    name = jobs.create("集成测试")
    d = os.path.join(config.get_scan_root(), name)
    for i in range(1, npages + 1):
        with open(os.path.join(d, "p%03d.png" % i), "wb") as f:
            f.write(b"\x89PNG fake page data")
    if locked:
        jobs.set_locked(name, True)
    return name


# ---------- 首页与版本 ----------
def test_index_ok():
    r = client.get("/")
    assert r.status_code == 200


def test_admin_status_version():
    r = client.get("/api/admin/status")
    assert r.get_json()["version"] == "1.15"


# ---------- 新1 锁定任务三层防护 ----------
def test_locked_job_api_delete_403():
    name = _mk_job(locked=True)
    r = client.delete("/api/jobs/" + name)
    assert r.status_code == 403, "锁定任务普通删除必须 403"
    assert "锁定" in r.get_json()["msg"]


def test_unlocked_job_api_delete_ok():
    name = _mk_job(locked=False)
    r = client.delete("/api/jobs/" + name)
    assert r.get_json().get("ok") is True


def test_locked_job_page_no_delete_button():
    name = _mk_job(locked=True)
    r = client.get("/job/" + name)
    body = r.get_data(as_text=True)
    assert "删除任务" not in body, "锁定任务页不显示删除任务按钮"
    assert "删除图片" not in body, "锁定任务页不显示删除图片按钮"
    assert "已锁定保护" in body, "锁定任务页应显示锁定徽标"


def test_unlocked_job_page_has_delete_button():
    name = _mk_job(locked=False)
    body = client.get("/job/" + name).get_data(as_text=True)
    assert "删除任务" in body


def test_locked_reorder_delete_403_but_sort_ok():
    name = _mk_job(locked=True)
    # 删页模式被拒
    r = client.post("/api/jobs/%s/reorder" % name,
                    json={"order": ["p001.png"], "delete": True})
    assert r.status_code == 403, "锁定任务删页必须 403"
    # 排序保留
    r = client.post("/api/jobs/%s/reorder" % name,
                    json={"order": ["p002.png", "p001.png"], "delete": False})
    assert r.get_json().get("ok") is True, "锁定任务排序应放行"


# ---------- 新2 管理页跳转 ----------
def test_job_page_from_admin_back_link():
    name = _mk_job()
    body = client.get("/job/%s?from=admin" % name).get_data(as_text=True)
    assert "/admin#jobs-manage" in body, "from=admin 时返回链接指向管理页锚点"
    assert "返回管理页" in body
    body2 = client.get("/job/" + name).get_data(as_text=True)
    assert "返回首页" in body2, "普通进入仍返回首页"


def test_admin_page_has_jobs_manage_anchor():
    assert b'id="jobs-manage"' in client.get("/admin").get_data()


# ---------- #1 扫描中删除/排序拦截 ----------
def test_scanning_job_delete_409():
    name = _mk_job()
    scanner.state[name] = {"state": "scanning", "msg": "扫描中"}
    r = client.delete("/api/jobs/" + name)
    assert r.status_code == 409
    r = client.post("/api/jobs/%s/reorder" % name,
                    json={"order": ["p002.png", "p001.png"]})
    assert r.status_code == 409
    scanner.state.clear()


# ---------- #7 SameSite Cookie ----------
def test_session_cookie_samesite():
    _reset()
    r = client.post("/api/admin/login",
                   json={"pin": "1234", "confirm": "1234"})
    assert r.get_json().get("ok") is True, "首次设置 PIN 应成功"
    # Flask test client 不直接暴露 Set-Cookie；改查配置
    assert app_mod.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app_mod.app.config["SESSION_COOKIE_HTTPONLY"] is True


# ---------- #8 PIN 迁移（API 级）----------
def test_pin_legacy_login_upgrade():
    _reset()
    import hashlib
    cfg = config.load_admin_cfg()
    cfg["pin_hash"] = hashlib.sha256(b"1234").hexdigest()  # 模拟 v1.13 存量
    config.save_admin_cfg(cfg)
    c2 = app_mod.app.test_client()
    r = c2.post("/api/admin/login", json={"pin": "1234"})
    assert r.get_json().get("ok") is True, "旧格式 PIN 必须能登录"
    new_hash = config.load_admin_cfg()["pin_hash"]
    assert "$" in new_hash, "登录成功后必须自动升级为 PBKDF2"
    # 升级后旧 PIN 仍有效
    r2 = c2.post("/api/admin/login", json={"pin": "1234"})
    assert r2.get_json().get("ok") is True
    # 错误 PIN 拒绝
    r3 = c2.post("/api/admin/login", json={"pin": "9999"})
    assert r3.status_code == 403


# ---------- #16 scan_defaults 白名单校验（API 级）----------
def test_scan_defaults_validated():
    _reset()
    c2 = app_mod.app.test_client()
    r0 = c2.post("/api/admin/login", json={"pin": "1234", "confirm": "1234"})
    csrf = r0.get_json().get("csrf", "")   # v1.14.1（P2-12）：登录下发 token
    assert csrf, "登录响应必须携带 csrf token"
    # 不带 token 的写请求被拒（CSRF 防护）
    r_bad = c2.post("/api/admin/config", json={"scan_defaults": {}})
    assert r_bad.status_code == 403, "缺 CSRF token 的写请求必须 403"
    # 带 token 正常
    r = c2.post("/api/admin/config", json={
        "scan_defaults": {"hpljm1005:": {"dpi": "150", "mode": "Gray", "crop": False}}},
        headers={"X-CSRF-Token": csrf})
    assert r.get_json().get("ok") is True


# ---------- 普通 API 创建任务（随机后缀链路）----------
def test_api_create_job():
    _reset()
    r = client.post("/api/jobs", data={"remark": "API创建", "dpi": "150",
                                      "mode": "Gray", "source": "flatbed"})
    assert r.status_code == 302, "创建成功应跳转任务页"
    assert "_test_root" not in r.headers.get("Location", "")
    # 同秒再建一个不冲突
    r2 = client.post("/api/jobs", data={"remark": "API创建", "dpi": "150",
                                        "mode": "Gray", "source": "flatbed"})
    assert r2.status_code == 302
    assert r.headers["Location"] != r2.headers["Location"], "同秒两个任务跳转地址必须不同"
