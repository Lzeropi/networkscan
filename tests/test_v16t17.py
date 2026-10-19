# tests/test_v16t17.py — v1.16.3-tmp05：纯代码审查报告（第 9 问）五项修复验证
# P2-1  <2 页删除分支幸存页重编号（p002/p005 删 p002 后 p005 收口到 p001）
# P3-1  order 非 array（int/str/dict/bool）→ 400 前置拦截（旧版 TypeError 500）
# P3-2  JSON 根非 dict（数组/字符串/数字/无效文本）→ 400 前置拦截（旧版
#       `or {}` 后 .get 抛 AttributeError 500；无效文本旧版 Werkzeug HTML 400）
# P3-4  api_scan 与 api_testscan 统一 except Exception（非 RuntimeError 冒
#       成 HTML 500 → 统一 JSON 500）
# P3-5  见 test_v16t10.py test_dl_with_progress_uses_iframe 增强断言
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402
import config          # noqa: E402
import jobs            # noqa: E402
import scanner         # noqa: E402


def _mk_job(params=None):
    for n in os.listdir(config.get_scan_root()):
        shutil.rmtree(os.path.join(config.get_scan_root(), n), ignore_errors=True)
    scanner.state.clear()
    scanner._reset_convert_for_test()
    with app_mod._pack_lock:
        app_mod._pack_progress.clear()
    return jobs.create("t", params or {})


def _put(name, fname, content):
    d = os.path.join(config.get_scan_root(), name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, fname), "wb") as f:
        f.write(content)


# ==================== P2-1：幸存页重编号 ====================

def test_reorder_keep1_renumbers_to_p001():
    """空洞编号场景（P2-1 核心用例）：p002/p005 删 p002 → 幸存 p005 必须收口到
    p001——旧版只删不改名，next_page_no 按 max+1 使下一页从 p006 起、p001-p004
    永缺。反向验证：删 _renumber_single_kept 调用 → 本测试 FAIL。"""
    name = _mk_job()
    _put(name, "p002.png", b"DEL-DOC")
    _put(name, "p005.png", b"KEPT-DOC")
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name,
               json={"order": ["p005.png"], "delete": True})
    assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
    # 幸存页改名 p001 且内容保持，被删页与旧名均消失
    assert jobs.pages(name) == ["p001.png"], \
        "幸存页应收口到 p001（P2-1），实际 %r" % jobs.pages(name)
    d = os.path.join(config.get_scan_root(), name)
    assert open(os.path.join(d, "p001.png"), "rb").read() == b"KEPT-DOC", "改名不得动内容"
    assert not os.path.exists(os.path.join(d, "p002.png"))
    assert not os.path.exists(os.path.join(d, "p005.png"))
    # 编号空洞已被收掉：下一页从 p002 起
    assert jobs.next_page_no(name) == 2, "收口后下一页应从 p002 起-gap 已消"


def test_reorder_keep1_already_p001_noop():
    """幸存页已是 p001（p001/p005 删 p005）→ 无需处理：p001 原样未动。"""
    name = _mk_job()
    _put(name, "p001.png", b"GOOD")
    _put(name, "p005.png", b"DEL")
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name,
               json={"order": ["p001.png"], "delete": True})
    assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
    d = os.path.join(config.get_scan_root(), name)
    assert jobs.pages(name) == ["p001.png"]
    assert open(os.path.join(d, "p001.png"), "rb").read() == b"GOOD", "p001 不得被动"


def test_reorder_keep1_preserves_extension():
    """jpg（及同理 webp）扩展名保留：p003.jpg/p007.jpg 删 p007 → p001.jpg。"""
    name = _mk_job()
    _put(name, "p003.jpg", b"JPGPAGE")
    _put(name, "p007.jpg", b"DEAD")
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name,
               json={"order": ["p003.jpg"], "delete": True})
    assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
    assert jobs.pages(name) == ["p001.jpg"], "jpg 幸存页应收口到 p001.jpg"
    d = os.path.join(config.get_scan_root(), name)
    assert open(os.path.join(d, "p001.jpg"), "rb").read() == b"JPGPAGE"


def test_reorder_keep1_thumb_follows():
    """缩略图同步跟随：.thumbs/p005.jpg → .thumbs/p001.jpg（derived cache，
    缺失可重建，但跟随可避免重建开销与前端占位窗口）。"""
    name = _mk_job()
    _put(name, "p002.png", b"D")
    _put(name, "p005.png", b"K")
    tdir = os.path.join(config.get_scan_root(), name, ".thumbs")
    os.makedirs(tdir, exist_ok=True)
    open(os.path.join(tdir, "p005.jpg"), "wb").write(b"THUMB")
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name,
               json={"order": ["p005.png"], "delete": True})
    assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
    assert os.path.exists(os.path.join(tdir, "p001.jpg")), "缩略图应跟随改名"
    assert open(os.path.join(tdir, "p001.jpg"), "rb").read() == b"THUMB"
    assert not os.path.exists(os.path.join(tdir, "p005.jpg"))


def test_reorder_keep1_skip_when_target_exists(monkeypatch):
    """降级路径：目标 p001 因删除失败残留（Samba/权限等 OSError 被 _finish_delete
    吞掉）时必须跳过改名——绝不覆盖已有文件；幸存页保留原名，功能不受影响
    （列表/下载按文件名工作，仅编号回到不连续）。"""
    name = _mk_job()
    _put(name, "p001.png", b"ORIG-RESIDUE")
    _put(name, "p005.png", b"KEPT-DOC")
    real_remove = os.remove

    def flaky_remove(path):
        if path.endswith("p001.png"):
            raise OSError("simulated delete failure")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", flaky_remove)
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name,
               json={"order": ["p005.png"], "delete": True})
    assert r.status_code == 200 and r.get_json().get("ok"), r.get_json()
    d = os.path.join(config.get_scan_root(), name)
    # 两个文件都完好，幸存页未被覆盖成 p001
    assert open(os.path.join(d, "p001.png"), "rb").read() == b"ORIG-RESIDUE", \
        "残留 p001 不得被覆盖（数据安全优先于编号连续）"
    assert open(os.path.join(d, "p005.png"), "rb").read() == b"KEPT-DOC", \
        "改名跳过时幸存页必须原样保留"


# ==================== P3-1 / P3-2：reorder 入参类型防护 ====================

def test_reorder_non_list_order_400():
    """order 为整数/字符串/对象/布尔 → 400「顺序列表必须是数组」而非 TypeError
    500（P3-1）。反向验证：删 isinstance 前置校验 → 本测试 500 FAIL。"""
    name = _mk_job()
    _put(name, "p001.png", b"A")
    _put(name, "p002.png", b"B")
    c = app_mod.app.test_client()
    for bad in (123, "p001.png", {"0": "p001.png"}, True):
        r = c.post("/api/jobs/%s/reorder" % name,
                   json={"order": bad, "delete": True})
        assert r.status_code == 400, \
            "order=%r 应 400（非 500），实际 %s" % (bad, r.status_code)
        assert "数组" in (r.get_json().get("msg") or "")
    # 任务页面原样未动
    assert jobs.pages(name) == ["p001.png", "p002.png"]


def test_reorder_non_dict_body_400():
    """JSON 根非对象（数组/字符串/数字/无效文本）→ 400 JSON 而非 AttributeError
    500（P3-2）；无效 JSON 文本旧版为 Werkzeug HTML 400，统一为 JSON 400。"""
    name = _mk_job()
    _put(name, "p001.png", b"A")
    c = app_mod.app.test_client()
    # 合法 JSON 但非对象
    for bad in ([1, 2, 3], "x", 123, True):
        r = c.post("/api/jobs/%s/reorder" % name, json=bad)
        assert r.status_code == 400, \
            "body=%r 应 400（非 500），实际 %s" % (bad, r.status_code)
        assert "JSON 对象" in (r.get_json().get("msg") or "")
    # 无效 JSON 文本：也必须是 JSON 400（不再 HTML 400）
    r = c.post("/api/jobs/%s/reorder" % name, data="not-json{",
               content_type="application/json")
    assert r.status_code == 400
    j = r.get_json()
    assert j and j.get("ok") is False, "无效 JSON 也应返回 JSON 错误体"
    # 任务页面原样
    assert jobs.pages(name) == ["p001.png"]


def test_reorder_null_order_empty_400():
    """显式 order=null 等价缺省 → 「顺序列表为空」400（与旧版语义一致，
    不因类型校验收紧而误报「必须是数组」）。"""
    name = _mk_job()
    _put(name, "p001.png", b"A")
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/reorder" % name, json={"order": None})
    assert r.status_code == 400
    assert "为空" in (r.get_json().get("msg") or ""), \
        "null order 沿用空列表语义，实际 %r" % r.get_json().get("msg")


# ==================== P3-4：api_scan 异常口径统一 ====================

def test_api_scan_unexpected_exception_json_500(monkeypatch):
    """scan_flatbed 抛非 RuntimeError（旧版漏网口径）→ JSON 500 而非 Flask
    默认 HTML 500；与 api_testscan 的 except Exception 对齐。反向验证：
    恢复 except RuntimeError → 本测试 get_json() 为 None FAIL。"""
    name = _mk_job()
    _put(name, "p001.png", b"A")

    def boom(job):
        raise ValueError("boom-unexpected")

    monkeypatch.setattr(scanner, "scan_flatbed", boom)
    c = app_mod.app.test_client()
    r = c.post("/api/jobs/%s/scan" % name)
    assert r.status_code == 500
    j = r.get_json()
    assert j and j.get("ok") is False, "异常必须以 JSON 错误体返回（非 HTML 500）"
    assert "boom-unexpected" in (j.get("msg") or "")
