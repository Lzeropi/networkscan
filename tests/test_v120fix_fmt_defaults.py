# tests/test_v120fix_fmt_defaults.py — v1.16.3 发布内修复：首页输出格式下拉应用管理页默认值
# 用户报告：管理页按设备设置的 format 默认值在扫描页（首页）创建任务时不生效。
# 根因（2026-10-22 定位）：updateDeviceOptions（app.js）只应用了 mode/dpi/crop 三项
# defaults，format 漏接——首页 #fmtDropdown 恒为模板初始 png。后端 /api/devices
# 透传正常（203 实测 defaults 完整含 format），纯前端应用侧遗漏（v1.16.0 历史遗留：
# 「能力 ∪ 管理页默认值」与「输出格式可选」同周期各自实现，未回接）。
# 三项均为 JS 运行时行为，项目无 node/DOM 测试依赖，以静态源断言固化防回归。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def _src(*paths):
    return open(os.path.join(os.path.dirname(config.__file__), *paths), encoding="utf-8").read()


def test_update_device_options_applies_format_default():
    """updateDeviceOptions 必须包含 format 默认值应用段：读取 dev.defaults.format、
    白名单命中时同步 hidden 提交值（fmtHidden.value = dvFmt）与选中高亮
    （item.classList.add("selected")）——缺任一即「值生效但显示停在 png」或
    「显示对了提交值没变」的半吊子状态。反向验证：删除该段 → 本测试 FAIL。"""
    js = _src("static", "app.js")
    body = js.split("function updateDeviceOptions", 1)[1].split("\n}", 1)[0] \
        if "function updateDeviceOptions" in js else js
    assert "fmtDropdown" in body, "updateDeviceOptions 内必须处理 #fmtDropdown（format 默认值应用）"
    assert "dev.defaults.format" in body, "必须读取 dev.defaults.format（管理页透传的默认值）"
    assert "fmtHidden.value = dvFmt" in body, "必须把默认格式写入 hidden 提交值"
    assert 'dd-item[data-value="' in body, "必须按 data-value 定位菜单项（白名单命中才应用）"
    assert 'classList.add("selected")' in body, "必须同步选中高亮（防值/显示不一致）"


def test_index_format_dropdown_is_still_static_whitelist():
    """首页 #fmtDropdown 结构保持静态白名单（png/jpg/webp 三项 data-value），
    应用层的白名单定位依赖该结构。若有人改模板结构需同步检查应用段。"""
    html = _src("templates", "index.html")
    assert 'id="fmtDropdown"' in html, "首页必须有输出格式下拉 #fmtDropdown"
    for v in ("png", "jpg", "webp"):
        assert 'data-value="%s"' % v in html, "首页格式菜单必须含 %s 项" % v
    assert 'name="format" value="png"' in html, "hidden 提交值初始为 png（默认回退位）"


def test_update_device_options_keeps_other_default_sections():
    """format 修复不得挤掉既有的 mode/dpi/crop 三个默认值应用段（回归护栏）。"""
    js = _src("static", "app.js")
    body = js.split("function updateDeviceOptions", 1)[1].split("\n}", 1)[0] \
        if "function updateDeviceOptions" in js else js
    assert "dv.mode" in body, "mode 默认值应用段必须保留（v1.16.0-tmp01）"
    assert "dv.dpi" in body, "dpi 默认值应用段必须保留（v1.16.0-tmp01）"
    assert "dev.defaults.crop" in body, "crop 默认值应用段必须保留"
