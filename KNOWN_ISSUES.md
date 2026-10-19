# ScanWeb 已知遗留问题汇总（Known Issues）

> 本文档是 v1.16.4 及后续周期的 **backlog 实体清单**：记录截至 **v1.16.3 定版** 时「已评估、暂不修复」的遗留问题，逐条给出**问题等级、位置、评估依据与修复建议**。
>
> 作用：① 防止审查轮次重复提出/重复讨论同一问题；② 为后续迭代提供明确的 backlog 来源；③ 未列入本文档的历史问题视为已修复。新增挂起项在本文档补充，修复后从本文档移除并记入 README 版本历史。

## 等级说明

| 等级 | 含义 | 对发布的影响 |
|---|---|---|
| **P3** | 低风险：健壮性 / 体验 / 测试质量问题 | 不阻塞发布，建议择期修复 |
| **P4 / 边界** | 设计权衡或极端边缘场景 | 原则上不动，仅需求/触发条件出现时再评估 |

---

## 一、P3 级（挂起，建议 v1.16.4 优先评估）

### P3-1 `test_v16t18.py::test_load_overview_guards_non_200` 函数体定位脆弱

- **来源**：二十四轮审查（DeepSeek 审 tmp06）唯一新发现
- **位置**：`tests/test_v16t18.py` 第 56 行
- **描述**：`js.split("async function loadOverview", 1)[1].split("\n  }", 1)[0]` 依赖「loadOverview 函数内无 2 空格缩进的 `}`」这一**隐含前提**来切出函数体。当前函数内缩进结构满足（276 全绿、断言均命中），**测试当前有效**；但若未来在函数内加入 2 空格缩进的块（如函数体顶部加 `if (document.hidden) { ... }`），split 会在此**过早截断**，导致 `_status !== 200` 断言漏检或误报。
- **修复建议**：改为更紧的边界，例如 `split("async function loadDevices", 1)[0]`（定位下一个函数标记），或 `split("\n  }\n", 1)[0]`。
  - ⚠️ **勿用 `ast` 定位**：项目先例（`test_v16t10.py` 的 ast 用法）是解析 **Python** 源码的，`ast` 无法解析 JavaScript 文件——该建议不可行。
- **来源轮次**：二十四轮审查报告 P3-1（报告自认「非必须，可挂 v1.16.4」）。

### P3-2 `checkScanStatus` 无轮询（扫描占用横幅为加载时快照）

- **位置**：`static/app.js` → `checkScanStatus()`；`templates/index.html` 与 `templates/job.html` 只在页面加载时调用一次
- **描述**：README 描述为「展示当前是否有人在扫描」，实际是加载时快照——扫描结束横幅不自动消失、新扫描开始不自动出现（需刷新）。属体验/信息展示问题，非功能缺陷。
- **修复建议**：`setInterval(checkScanStatus, 3000)`，页面隐藏时暂停（`document.hidden` 判断）。可选。
- **入账轮次**：二十轮审查 P4-9 与二十三轮审查 P3-5 **两轮重复提出**，非新发现。

### P3-3 `loadDevices` 对非 200 响应误报「scanimage 未安装」

- **位置**：`static/admin.js` → `loadDevices()`；`templates/manual`
- **描述**：服务端 500（返回 HTML）时 `api()` 得 `{}`，`d.scanimage_ok` 为 `undefined`，用户看到「scanimage 未安装」的**误导提示**（实际是服务端错误）。
- **修复建议**：加 `if (d._status !== 200) { box.innerHTML = '<p class="status err">设备探测请求失败（HTTP '+d._status+'）</p>'; return; }`。
- **入账轮次**：二十三轮审查 P3（本轮已确认仍存在）。

### P3-4 `btoa(x.name)` 对非 ASCII 设备名抛 InvalidCharacterError

- **位置**：`static/admin.js`（设备列表 `const eid = btoa(x.name)...`，两处）
- **描述**：`btoa` 只接受 Latin-1，设备名含中文/Unicode 时抛 `InvalidCharacterError`，该设备卡渲染中断。SANE 设备名通常为 ASCII（`hpljm1005:libusb:001:003`），仅理论风险。
- **修复建议**：`btoa(unescape(encodeURIComponent(x.name)))` 或改用索引作 key。
- **入账轮次**：二十三轮审查 P3（第6项）。

## 2. P4 / 边界（挂起，原则上不动）

### B-1 `api_save_config` 的 `scan_defaults` merge 重新校验未提及设备的旧值

- **位置**：`admin.py` → `api_save_config` → `scan_defaults` 分支
- **描述**：`sd = {**cfg.get("scan_defaults", {}), **sd}` 后遍历**合并后全部设备**做能力校验。若某台**未提及**设备的旧记录是非法值（手改 `admin_config.json` / 历史版本遗留），整个保存请求被 400 拒绝——管理员无法通过 UI 修复（一提交就 400），只能手改或删配置文件。正常 UI 路径不可达（保存均经校验）。
- **修复建议**：仅对本次提交出现的设备做校验，未提及设备沿用旧值（信任已保存数据）；或对旧值清洗而非拒绝。若将来引入「配置迁移/导入」功能需先处理此边界。
- **入账轮次**：二十三轮边界项（报告自认非必须修），二十四轮确认认同。

### B-2 打包进度收尾（`_pack_prog_update`）失败后条目永久 active=True

- **位置**：`app.py` → `dl_zip._release_once`
- **描述**：收尾 `_pack_prog_update(active=False)` 若自身抛异常（`_pack_progress` 数据结构被破坏、`_pack_lock` 长期持有等），条目永久 active=True（GC 只清 active=False），前端按钮卡到 5 分钟兜底。属极端场景（双失败链在正常 Python 下几乎不可能），且已 stderr 留痕可排障。
- **修复建议**：暂不动（已留痕）。若未来强化可加「active 超时兜底清理」。
- **入账轮次**：二十三轮边界项。

### B-3 `api_params` 的 `d.get("dpi") or params.get("dpi")` falsy 值被吞（P4）

- **位置**：`app.py:214`
- **描述**：用 `or` 合并时，`dpi=0` 或 `mode=""` 等 falsy 值被吞，落到 `params` 或默认值——但业务上 dpi/mode 白名单本就排除了这些值，实际影响几乎为零。
- **修复建议**：若需精确区分「未提供」与「提供了非法 falsy」，改用显式 `None` 判断。非必须。
- **入账轮次**：二十三轮纯代码审查 P4-1。

### B-4 `scan_flatbed` 的 `jlock.acquire()` 无 timeout

- **位置**：`scanner.py` → `scan_flatbed` 入口
- **描述**：`jlock.acquire()` 阻塞无超时——若前一个扫描/转换长期持锁（理论异常），扫描请求永久排队。正常路径锁均及时释放。
- **修复建议**：可加 timeout + 超时 409。当前不动。
- **入账轮次**：二十三轮纯代码审查 P4-2。

### B-5 `scan_adf` 的 pending 轮询无超时上限

- **位置**：`scanner.py` → `scan_adf` → `while any(has_pending(job, f) for f in batch_pages)`
- **描述**：轮询等待批次页转换收敛，无超时上限——若某页转换永久失败，会死等。正常路径转换 worker 必有终态。
- **修复建议**：加超时上限或失败页放行。当前不动。
- **入账轮次**：二十三轮纯代码审查 P4-3。

### B-6 `_webp_safe_png` 异常路径未提取 stderr

- **位置**：`app.py` → `_webp_safe_png`
- **描述**：ImageMagick 异常路径未提取 stderr，仅记 generic 错误。排障信息略缺，非功能缺陷。
- **修复建议**：异常时附 `e.stderr` 片段。低优先。
- **入账轮次**：二十三轮纯代码审查 P4-4。

### B-7 `dl_pdf` 内存峰值口径与 `MAX_PDF_MEM` 不同

- **位置**：`app.py` → `dl_pdf`（`approx > MAX_PDF_MEM`）
- **描述**：估算口径与实际峰值存在差异，可能误判 413。属保守/激进之间的口径选择。
- **修复建议**：需实测校准，非必须。
- **入账轮次**：二十三轮纯代码审查 P4-5。

### B-8 `pollThumbReady` 注释「60 秒」与实际可能不符

- **位置**：`static/app.js` / `app.py` 注释
- **描述**：注释口径与实际重试窗口（可能 21 分钟）不一致，纯文档问题，不影响功能。
- **修复建议**：更新注释。低优先。
- **入账轮次**：二十三轮纯代码审查 P4-6。

### B-9 `except TypeError: _dup = False` 已成死代码（防御深度保留）

- **位置**：`app.py` → `_reorder_body` 去重校验
- **描述**：tmp04 元素类型前置校验后，`set(new_order)` 理论上恒不抛 TypeError，此分支成为死代码。**保留**作为防御深度——防未来改动类型校验时静默回退 500（代码注释已说明）。
- **修复建议**：不动（防御深度，注释完备）。
- **入账轮次**：二十三轮纯代码审查 P4-7。

### B-10 `api_delete` 并发删除时返回 403（应 404）

- **位置**：`app.py` → `api_delete`
- **描述**：并发删除（两个请求）时第二个可能 403 而非 404，语义轻微偏差，无实际危害。
- **修复建议**：区分「已锁定」与「已不存在」。非必须。
- **入账轮次**：二十三轮纯代码审查 P4-8。

### B-11 `api_create` / `api_params` device 前缀匹配静默匹配

- **位置**：`app.py` → `api_create` / `api_params`
- **描述**：`x["name"] == device or x["name"].startswith(device)` —— 前缀匹配可能静默匹配非精确设备（如输入 `hpl` 匹配到 `hpljm1005...`），宽松但可控。
- **修复建议**：需精确匹配或显式提示。非必须。
- **入账轮次**：二十三轮纯代码审查 P4-10。

### B-12 `_JobLock` 双 release 时 refs 不递减（已知权衡）

- **位置**：`jobs.py` → `_JobLock`
- **描述**：重复 release（同 handle 二次）只抛 RuntimeError 不递减 refs——有意设计（防误递减导致 registry 条目被提前回收）。**明确接受的权衡**，不修。
- **修复建议**：不动（权衡合理，注释完备）。
- **入账轮次**：历轮已确认，二十三轮纯代码审查 P4-11 重申。

---

## 历史已修复项（不再 backlog）

v1.16.3 周期（tmp01~tmp06）内已修复并验证的问题不计入本文档，详见 README 版本历史条目。本文档仅收录「截至 v1.16.3 定版仍待处理」的遗留问题。

> AI生成
