# tests/test_v16t18.py — v1.16.3-tmp06：DeepSeek 二十三轮（首轮系统前端 JS 审查）三项修复验证
# P1    app.js updateDeviceOptions 的 const modes/dpis 在空数组兜底赋值时抛
#       TypeError（设备探测失败 + 无管理员默认值 → 菜单卡死）→ 改 let
# P2-1  admin.js ddBox 显示文字 cur 补 esc()（同函数 rawCur/items 均转义，唯 cur 遗漏）
# P2-2  admin.js loadOverview 非 200 响应前置返回（500 时 d.cpu 为 undefined，
#       旧版在 setBar 抛 TypeError 冻结仪表盘，30s 轮询每轮同点失败）
# 注：三项均为浏览器 JS 运行时行为；项目测试环境（开发机/203 盒）无 node/DOM 依赖，
# 行为级验证已在审查轮以 node 复现留档，本文件以静态源断言固化防回归。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def _src(*paths):
    return open(os.path.join(os.path.dirname(config.__file__), *paths), encoding="utf-8").read()


def test_update_device_options_uses_let_not_const():
    """P1：modes/dpis 声明必须是 let——探测失败（modes/dpis 空数组）且管理员未存
    该设备默认值时，`if (!modes.length) modes = [...]` 空数组兜底对 const 变量整体
    重新赋值抛 `TypeError: Assignment to constant variable`，updateDeviceOptions
    中断、色彩/分辨率菜单卡死。v1.16.0 引入（同文件 loadJobParams 同逻辑正确用
    let，当年遗漏）。反向验证：改回 const → 本测试 FAIL。"""
    js = _src("static", "app.js")
    body = js.split("function updateDeviceOptions", 1)[1].split("\n}", 1)[0]
    assert "let modes = (dev.modes && dev.modes.length)" in body, \
        "modes 声明必须是 let（const 在空数组兜底赋值时抛 TypeError 中断函数）"
    assert "let dpis = (dev.dpis && dev.dpis.length)" in body, \
        "dpis 声明必须是 let（同上）"
    assert "const modes = " not in body, "updateDeviceOptions 内不得残留 const modes 声明"
    assert "const dpis = " not in body, "updateDeviceOptions 内不得残留 const dpis 声明"


def test_ddbox_display_text_escaped():
    """P2-1：ddBox 显示文字 cur 必须走 esc()——同函数 rawCur/items 均已转义，唯
    cur 遗漏，违反 admin.js 顶部「所有动态拼接 insertAdjacentHTML 的值必须包
    esc()」的自身约定；正常保存值有后端白名单，风险面为手改 admin_config.json
    的存量脏值（admin_config.json 明确可手工编辑）。反向验证：去掉 esc(cur) →
    本测试 FAIL。"""
    js = _src("static", "admin.js")
    assert 'dd-toggle">' + "'+esc(cur)+'" in js, \
        "ddBox 的 dd-toggle 显示文字必须 esc() 转义（P2-1）"
    assert 'dd-toggle">' + "'+cur+'" + "<span" not in js, \
        "dd-toggle 不得拼接未转义的 cur（XSS 约定违规回归）"


def test_load_overview_guards_non_200():
    """P2-2：loadOverview 必须对非 200 响应前置返回——服务端 500 返回 HTML 时
    api() 得 {}（_status=500），旧版直接 d.cpu.percent 抛 TypeError，仪表盘冻结
    且 30s 自动刷新每轮在同一行失败。防御后保留旧值等下轮自愈（同类
    loadJobs/loadConfigs 本就有防御）。反向验证：删除 _status 检查 → FAIL。"""
    js = _src("static", "admin.js")
    body = js.split("async function loadOverview", 1)[1].split("\n  }", 1)[0]
    assert "if (d._status !== 200) return;" in body, \
        "loadOverview 必须在 _status!==200 时前置返回（防 d.cpu undefined TypeError）"
    # 401 原有语义保持：跳转登录而非静默保留旧值
    assert "if (d._status === 401) { location.reload(); return; }" in body, \
        "401 跳转登录的原有防御必须保留"
