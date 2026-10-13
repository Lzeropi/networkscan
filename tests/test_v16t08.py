# tests/test_v16t08.py — v1.16.0-tmp08：DeepSeek 补充审查报告两项 P2 实测证实后的修复验证
# P2-1（验证 5 证实）：jobs.delete/cleanup 删除任务后 _pack_progress 残留——
#      修复 = _delete_locked 收口清理（所有删除路径汇合点）
# P2-2（验证 6 证实）：_convert_pnm PIL 主分支 Image.open(src) 跟随 symlink 读穿
#      （实测：外部图片经链接进转换链）——修复 = _open_no_follow（O_NOFOLLOW fd）
import os
import shutil
import sys

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
    return jobs.create("t", params or {})


def _ppm(path):
    Image.new("RGB", (8, 8), (10, 120, 210)).save(path, "PPM")
    return path


# ---------- P2-1：jobs.delete 路径清理打包进度（验证 5 复现过残留） ----------

def test_jobs_delete_clears_pack_progress():
    name = _mk_job()
    app_mod._pack_prog_update(name, "zip", done=1, total=1, active=False)
    app_mod._pack_prog_update(name, "pdf", done=7, total=7, active=False)
    assert name in app_mod._pack_progress, "前置：登记应存在"
    jobs.delete(name)                       # 非 api_delete 路径（验证 5 复现残留处）
    assert name not in app_mod._pack_progress, \
        "jobs.delete 后进度登记必须清除（DeepSeek 验证 5 实测的残留 bug）"


def test_cleanup_path_clears_pack_progress():
    name = _mk_job()
    app_mod._pack_prog_update(name, "zip", done=0, total=1, active=True)
    # 造超龄任务触发 cleanup 的超龄删除路径（max_jobs=0 语义是"不限制"，须用 max_age）
    meta = jobs.load(name)
    import time as _t
    old = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(_t.time() - 3 * 86400))
    meta["created"] = old
    jobs.save(name, meta)
    cfg = config.load_admin_cfg()
    cfg["cleanup"]["max_age_days"] = 1
    config.save_admin_cfg(cfg)
    deleted = jobs.cleanup(force=True)      # cleanup/try_delete 路径
    assert name in deleted, "前置：cleanup 超龄应删除该任务，实际 %r" % deleted
    assert name not in app_mod._pack_progress, "cleanup 删除后进度登记必须清除"


# ---------- P2-2：_convert_pnm 拒绝 symlink 源（验证 6 实测过读穿） ----------

def test_convert_pnm_rejects_symlink_main_branch():
    """验证 6 场景 A 复现：外部图片经 symlink 被 PIL 读穿进转换链。
    修复后：O_NOFOLLOW 抛 OSError → 转换失败 → 调用方走 PNM 保留路径，
    外部文件内容绝不出现在任务产物中。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    outside = os.path.join(os.path.dirname(base), "_outside_secret.png")
    Image.new("RGB", (16, 16), (255, 0, 0)).save(outside)
    link = os.path.join(base, "p001.pnm")
    os.symlink(outside, link)
    dst = os.path.join(base, "p001.png")
    raised = False
    try:
        scanner._convert_pnm(link, dst, "png")
    except Exception:
        raised = True                        # O_NOFOLLOW ELOOP 或回退链失败都算拒绝
    assert raised or not os.path.exists(dst), "symlink 源必须被拒绝（不得产出页面）"
    if os.path.exists(dst):
        with Image.open(dst) as im:
            px = im.convert("RGB").getpixel((0, 0))
        assert px != (255, 0, 0), "外部文件内容（红色）绝不得进任务产物（读穿）"
    assert os.path.islink(link), "链接本身保持原样（未被删/未跟随）"


def test_convert_pnm_normal_source_still_works():
    """安全修复不得误伤正常路径：真实 PNM 文件照常转换。"""
    name = _mk_job()
    base = os.path.join(config.get_scan_root(), name)
    pnm = _ppm(os.path.join(base, "p001.pnm"))
    dst = os.path.join(base, "p001.png")
    scanner._convert_pnm(pnm, dst, "png")
    assert os.path.exists(dst), "真实 PNM 源必须正常转换"
    with Image.open(dst) as im:
        assert im.size == (8, 8), "产物尺寸应与源一致"
