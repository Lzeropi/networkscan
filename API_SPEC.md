# ScanWeb API 接口规范

> **适用版本**：v1.16.3（本文档随版本演进，接口契约变更必须同步更新本文件）
>
> 本文档描述 ScanWeb 局域网扫描服务的全部 HTTP 接口：请求格式、参数约束、响应结构、错误码与并发语义。供二次开发、脚本集成与前端联调使用。

---

## 一、通用约定

### 1.1 基础信息

| 项 | 值 |
|---|---|
| 服务端口 | `9203`（默认，`config.PORT`） |
| Base URL | `http://<主机IP>:9203` |
| 协议 | HTTP（局域网内明文，建议保持在可信网段） |
| 字符编码 | UTF-8 |
| 数据格式 | JSON（接口默认）；创建任务为表单（`POST /api/jobs`）；文件下载端点返回二进制流 |

### 1.2 响应包络

- **写操作与错误路径**统一返回 `{"ok": bool, "msg": "错误描述", ...业务字段}`。失败时 `ok=false` 且 `msg` 为可直接向用户展示的中文提示。
- **只读业务端点**（状态、列表、进度类）直接返回业务对象，不包 `ok` 字段（见各端点定义）。
- 错误提示均为中文、已截断防滥用（`str()[:60~300]`），可直接透传给用户。

### 1.3 HTTP 状态码语义

| 状态码 | 语义 | 典型场景 |
|---|---|---|
| 200 | 成功 | 常规成功；**特例**：`POST /api/admin/config` 路径不存在时的确认请求返回 `200 + ok=false + need_confirm=true`（见 3.7） |
| 302 | 跳转 | 创建任务成功跳转任务页；站点口令启用时未登录跳转 `/login` |
| 400 | 参数校验失败 | 参数超白名单、请求体不是 JSON 对象、设备不在列表等（fail-closed，早失败） |
| 401 | 管理端未登录 | PIN 会话缺失/过期；此时应引导用户重新输入 PIN |
| 403 | 权限拒绝 | 任务已锁定禁删/禁改、CSRF 校验失败、PIN 错误 |
| 404 | 资源不存在 | 任务名非法或不存在、页面文件名非法、qrcode 依赖未安装 |
| 409 | 并发冲突 | 设备忙、转换中（详见 1.5 并发语义汇总） |
| 413 | 资源超限 | PDF 页数超上限、预估合成内存超上限 |
| 429 | 触发防暴力锁定 | 连续 5 次口令/PIN 错误，锁定 60 秒（按 IP） |
| 500 | 服务端错误 | 扫描命令失败、磁盘权限异常等，`msg` 带原因 |

### 1.4 全局参数白名单

创建任务与参数修改共用（`scanner.py` 常量）：

| 参数 | 合法值 |
|---|---|
| `dpi` | `75 / 100 / 150 / 200 / 300 / 400 / 600 / 1200 / 2400`（宽兜底集） |
| `mode` | `Color / Gray / Lineart` |
| `format` | `png / jpg / webp` |
| `source` | `flatbed / adf` |

设备能力级校验：若 `scanimage -A` 探测到该设备的精确能力（`dpi_raw` / `modes`），则以**设备能力为白名单**收口；探测无结果时回退上表宽集兜底。

### 1.5 并发语义（409 场景汇总）

扫描仪为独占设备、转换在后台串行执行。并发协调规则：

| 操作 | 冲突场景 | 返回 |
|---|---|---|
| `POST scan / adf` | 他人正在扫描 | 409「设备忙」 |
| `POST scan / adf` | 后台转换队列水位满（默认 4 页） | 409「后台转换队列已满」 |
| `POST adf` | 同任务上一批 ADF 页转换未收敛 | 409「上一批 ADF 页面仍在转换中」 |
| `POST reorder` | 任务在扫描中 | 409「扫描进行中」 |
| `POST reorder` | 有页面在转换中 | 409「有页面正在转换」 |
| `GET raw / download.zip / download.pdf` | 目标页转换中（0 字节占位） | 409「有页面正在转换」 |
| `DELETE 任务`、`PUT params` | 任务锁被占 | **不返回 409**——请求排队等待锁释放后顺序执行（改参数永不被拒） |

### 1.6 鉴权体系（两层独立）

**站点口令（用户层）**

- 由环境变量 `SCANWEB_TOKEN` 控制：**空 = 完全开放**（默认）；非空 = 除 `/login`、静态资源、架构图外全部请求需先登录。
- 登录：`POST /login`（表单 `password`），成功签发会话 cookie（`SameSite=Lax; HttpOnly`）。
- 防暴力：按 IP 连续 5 次错误锁定 60 秒（锁定期间返回提示，不泄露剩余次数以外的信息）。

**管理端 PIN（管理员层，与站点口令无关）**

- 全部 `/api/admin/*` 接口要求 PIN 会话（`require_admin`）。
- 登录签发会话 + **CSRF token**；此后所有**写操作**（POST/PUT/DELETE/PATCH）必须携带请求头 `X-CSRF-Token: <token>`（缺失或不匹配 → 403）。
- PIN 以 PBKDF2 哈希存储；首次登录即首次设置（需二次确认）。
- 防暴力：按 IP 连续 5 次错误锁定 60 秒 → 429。

### 1.7 标识符格式

| 标识 | 格式 | 示例 |
|---|---|---|
| 任务名 `job` | `\d{8}-\d{6}[\w\-]*`（时间戳前缀 + 备注转写） | `20261019-143025-验收单` |
| 页面文件 `fname` | `p\d+\.(png\|jpg\|webp)`，页号从 1 连续递增 | `p001.png` |
| 缩略图文件 | 恒为 `pNNN.jpg`（与页面扩展名无关） | `p001.jpg` |

排序/删除后页面**自动重新连续编号**（含删除至仅剩 1 页时幸存页收口为 `p001`）。

### 1.8 全局限额

| 限额 | 值 | 触发行为 |
|---|---|---|
| 后台转换队列水位 | 4 页（`MAX_PENDING_PNM`） | 拒绝新扫描 409 |
| PDF 合成页数上限 | 20 页（环境变量 `SCANWEB_MAX_PDF_PAGES`） | 413，请改用 ZIP |
| PDF 预估内存上限 | 512 MB（环境变量 `SCANWEB_MAX_PDF_MEM`） | 413 |
| 缩略图长轮询 | 20 秒 | 超时后由客户端 1s 后重试 |
| 打包进度条目驻留 | 拒绝型 60s / 完成型 600s | 内存自动 GC，无需清理接口 |

---

## 二、业务 API（普通用户端）

> 站点口令启用时需先完成站点登录（见 1.6）。

### 2.1 创建任务 `POST /api/jobs`

**注意：本接口为浏览器原生表单提交设计，使用 `application/x-www-form-urlencoded`（非 JSON）。**

请求字段（form）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `remark` | string | `""` | 备注名（进入任务文件夹名） |
| `source` | string | `flatbed` | 扫描来源，`flatbed` / `adf` |
| `source_name` | string | `""` | 进纸源设备名（ADF 时用） |
| `dpi` | string | `"150"` | 分辨率（见 1.4 白名单） |
| `mode` | string | `"Gray"` | 色彩（`Color`/`Gray`/`Lineart`） |
| `format` | string | `"png"` | 输出格式 png/jpg/webp |
| `device` | string | `""`（=默认设备） | 设备名，必须在探测列表内（前缀匹配允许） |
| `crop` | checkbox | 不勾 | 仅认 `1/true/on/yes` 为真，其余一律 false |

响应：`302` 跳转 `GET /job/<job>`（任务页）。

| 错误 | 说明 |
|---|---|
| 400 | 参数超白名单 / `source` 非法 / `device` 不在探测列表（不落盘无效任务） |

### 2.2 修改任务参数 `PUT /api/jobs/{job}/params`

请求体（JSON 对象，**字段可部分提交**，未提交字段沿用现值）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `device` | string | 须在探测列表；不能把非 ADF 设备用于 `source=adf` 任务 |
| `dpi` | string | 白名单 + 设备能力校验 |
| `mode` | string | 白名单 + 设备能力校验 |
| `format` | string | 白名单（png/jpg/webp），支持中途切换（混合格式任务合法） |
| `crop` | bool | **必须是布尔**（字符串 `"false"` 会被 400 拒绝而非误判为真） |

响应：`200 {"ok": true, "params": {…完整参数}}`

| 错误 | 说明 |
|---|---|
| 400 | 根不是 JSON 对象 / `crop` 非布尔 / format、设备、能力校验失败 |
| 404 | 任务不存在 |

语义：参数**对下一次扫描生效**；扫描/转换中允许提交（请求排队，扫描完成即生效，不返回 409）。

### 2.3 平板扫描 `POST /api/jobs/{job}/scan`

同步接口：等待 `scanimage` 扫描完成后返回（彩色 300dpi 约 19s），页面经后台队列转为图片。

响应：`200 {"ok": true, "file": "p001.png"}`（新页文件名；前端据此轮询缩略图就绪）

| 错误 | 说明 |
|---|---|
| 409 | 设备忙 / 转换队列满（含队列水位数值） |
| 500 | 扫描失败 / 超时（300 秒）/ 收尾异常，`msg` 带原因 |

### 2.4 ADF 连续扫描 `POST /api/jobs/{job}/adf`

要求任务创建时 `source=adf`（平板任务收到 400）。批量进纸后台扫描，前端轮询 `/api/jobs/{job}/status` 至终态。

响应：`200 {"ok": true}`

| 错误 | 说明 |
|---|---|
| 400 | 该任务创建时选择的是平板模式 |
| 409 | 设备忙 / 上一批 ADF 页仍在转换中（adf_busy） |

### 2.5 任务状态 `GET /api/jobs/{job}/status`

```json
{"state": "scanning", "msg": "平板扫描中…", "pages": 3}
```

| `state` | 含义 |
|---|---|
| `idle` | 无活动（含未扫过） |
| `scanning` | 平板扫描中 / ADF 连续扫描中 |
| `done` | 扫描完成（`msg` 带完成页名） |
| `error` | 失败（`msg` 带原因：超时/退出码/收尾异常等） |

`pages` 为磁盘实时页数（I/O 异常时兜底 0，不中断轮询）。

### 2.6 全局扫描状态 `GET /api/scan-status`

```json
{"busy": true, "job": "20261019-143025-x", "elapsed": 12, "convert_queue_len": 2}
```

`busy` = 是否有任务在扫描；`elapsed` = 已扫秒数；`convert_queue_len` = 后台转换队列页数。

### 2.7 页面排序 / 删除 `POST /api/jobs/{job}/reorder`

请求体（JSON 对象）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `order` | string[] | 必须是**字符串数组**：普通模式为全部页面的新顺序排列；删除模式为**保留**的页面列表 |
| `delete` | bool | `true` = 删除模式（`order` 中未列出的页面将被删除） |

校验链（fail-closed，逐级前置 400）：根必须是 JSON 对象 → `order` 必须是数组 → 元素必须全为字符串 → 不得重复 → 删除模式校验保留页均存在 / 普通模式校验恰为现有页面的排列（顺序可不同）。

响应：`200 {"ok": true}`（普通模式顺序未变时 `{"ok": true, "msg": "顺序未变化"}`）；删除模式 `{"ok": true, "deleted": N}`。

**删除语义**：待删页改名墓碑让位 → 保留页两步改名为连续编号 → 最后物理删除，任一步失败自动回滚（事务化）。**仅剩 1 页时幸存页收口为 `p001`**（缩略图同步），编号空洞不遗留。

| 错误 | 说明 |
|---|---|
| 400 | `请求体必须是 JSON 对象` / `顺序列表必须是数组` / `顺序列表元素必须为字符串文件名` / `顺序列表含重复页面` / `顺序列表为空` / 与实际页面不匹配 / 页面不存在 |
| 403 | 任务已锁定，不能删除页面（排序仍允许） |
| 409 | 扫描进行中 / 有页面正在转换 |

### 2.8 删除任务 `DELETE /api/jobs/{job}`

响应：`200 {"ok": true}`。转换中允许删除（后台 worker 有兜底）。

| 错误 | 说明 |
|---|---|
| 403 | 任务已锁定（管理员保护）；或锁获取窗口内被锁定 |
| 500 | 任务目录权限异常（提示管理员修复属主后重试） |

### 2.9 设备列表 `GET /api/devices?refresh=1`

`refresh=1` 强制重新探测（默认 300s 缓存；探测失败空数据也缓存 300s，用刷新自愈）。

```json
{"devs": [{
  "name": "hpljm1005:libusb:001:003", "desc": "...",
  "modes": ["Gray", "Color", "Lineart"], "dpis": ["75","100","150","200","300","600","1200"],
  "dpi_raw": ["75", ...], "dpi": "75–1200 dpi",
  "sources": ["Flatbed", "ADF"], "scan_type": "平板 + ADF 连续", "error": "",
  "alias": "办公室扫描仪", "defaults": {"dpi": "300", "mode": "Gray", "format": "png", "crop": false}
}]}
```

`alias` / `defaults` 来自管理页配置，逐设备附带。

### 2.10 页面原图 `GET /job/{job}/raw/{fname}`

返回图片二进制（MIME 按扩展名：`image/png|jpeg|image/webp`）。文件名必须匹配 `p\d+\.(png|jpg|webp)`；符号链接与路径穿越被拒。404（不存在/非法名）、409（该页转换中）。

### 2.11 缩略图 `GET /job/{job}/thumb/{fname}.jpg`

返回 JPEG 二进制（恒 `.jpg` 后缀，对应任意格式页面）。**长轮询**：目标页转换中时请求挂起至多 20 秒，转换完成立即返回 200；超时未就绪由客户端 1s 后重试。不持任务锁，可并发。

### 2.12 打包进度 `GET /api/jobs/{job}/pack-progress`

```json
{"zip": {"kind": "zip", "done": 3, "total": 5, "active": true},
 "pdf": {"kind": "pdf", "done": 0, "total": 0, "active": false}}
```

`kind` 隔离：ZIP 与 PDF 进度互不覆盖，前端按需取。只读内存登记，可并发轮询（每次轮询顺带触发过期 GC）。

### 2.13 ZIP 下载 `GET /job/{job}/download.zip`

返回 `application/zip` 流。文件流式打包（不占内存）；下载期间任务删除/排序请求排队（锁语义），不产生截断包。进度经 2.12 轮询。409：有页面转换中。

### 2.14 PDF 下载 `GET /job/{job}/download.pdf`

返回 `application/pdf` 流。逐页合成、实时进度经 2.12 轮询。

| 错误 | 说明 |
|---|---|
| 409 | 有页面正在转换 |
| 413 | 页数 > 20（`SCANWEB_MAX_PDF_PAGES`）或预估合成内存超限（`SCANWEB_MAX_PDF_MEM`）——请改用 ZIP |

### 2.15 扫码取件 `GET /job/{job}/qrcode`

返回 PNG 二进制（指向任务页的二维码，`qrcode` 可选依赖，未安装时 404，前端自动隐藏入口）。任务不存在 → 404。

---

## 三、管理 API（`/api/admin/*`）

> 全部需 PIN 会话：未登录 `401 {"ok":false,"msg":"未登录或会话已过期…"}`；写操作（POST/PUT/DELETE/PATCH）另需请求头 `X-CSRF-Token`（不匹配 → 403）。

### 3.1 登录状态 `GET /api/admin/status`

```json
{"logged": false, "has_pin": true, "version": "1.16.3"}
```

已登录时额外返回 `"csrf": "<token>"`（前端取之用于写请求头）。

### 3.2 PIN 登录 `POST /api/admin/login`

请求：`{"pin": "1234", "confirm": "1234"}`（`confirm` 仅首次设置时需与 `pin` 一致）。

响应：`200 {"ok": true, "csrf": "<token>", "first": true}`（`first` 仅首次设置时出现）。

| 错误 | 说明 |
|---|---|
| 400 | PIN 少于 4 位 / 首次设置两次输入不一致 |
| 403 | PIN 错误（提示已连续错 N 次） |
| 429 | 连续 5 次错误锁定 60 秒（提示剩余秒数） |

### 3.3 登出 `POST /api/admin/logout`

响应：`200 {"ok": true}`

### 3.4 综合状态 `GET /api/admin/overview`

```json
{
  "version": "1.16.3", "uptime": "3 天 2 时",
  "env": {"deps": [{"name": "Python", "ok": true, "ver": "3.8.10", "desc": "运行时"}], "user": "scanops"},
  "cpu": {"percent": 23.5, "temp": 52},
  "mem": {"percent": 41, "used_mb": 1500, "total_mb": 3700},
  "disk_root": {"percent": 60, "free_gb": 12.3},
  "disk_scan": {"percent": 60, "free_gb": 12.3},
  "scan_root": "/opt/smb_share/scans",
  "scan_dir_size": 123456789,
  "cleanup": {"max_jobs": 0, "max_age_days": 30, "max_total_mb": 0}
}
```

CPU 温度：优先海思 `/proc/msp/pm_cpu`，通用回退 `/sys/class/thermal`（无传感器时 `temp: null`）。

### 3.5 设备探测 `GET /api/admin/devices?refresh=1`

```json
{"devs": [ …同 2.9（无 alias/defaults 附加） ], "cached": true, "scanimage_ok": true}
```

### 3.6 读配置 `GET /api/admin/config`

```json
{
  "scan_root": "/opt/smb_share/scans",       // 当前生效存储根（旧配置无自定义时为默认值）
  "custom_root": "",                          // 管理页显式自定义值（空=未自定义）
  "cleanup": {"max_jobs": 0, "max_age_days": 30, "max_total_mb": 0},
  "device_alias": {"hpljm1005:libusb:001:003": "办公室扫描仪"},
  "scan_defaults": {"hpljm1005:libusb:001:003": {"dpi": "300", "mode": "Gray", "format": "png", "crop": false}},
  "admin_cfg_path": "admin_config.json"
}
```

### 3.7 写配置 `POST /api/admin/config`

请求体（JSON 对象，各节可独立提交，配置加载-校验-保存原子事务，失败不留残迹）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `scan_root` | string | 绝对路径；系统目录黑名单拒绝；**不存在时先返回确认**：`200 {"ok": false, "need_confirm": true, "msg": "路径 … 不存在，是否创建？"}`，客户端带 `confirm: true` 重发即自动创建；有人扫描/转换时 409 |
| `confirm` | bool | 确认创建新路径 |
| `cleanup` | object | `{max_jobs, max_age_days, max_total_mb}`，0 = 不启用；任一超限即从最旧任务清理（锁定任务永不清） |
| `device_alias` | object | 设备名 → 别名；`null` = 全清 |
| `scan_defaults` | object | 设备名 → `{dpi, mode, format, crop}` 默认值；dpi/mode 按设备探测能力校验（探测无能力时回退白名单）；**空对象 `{}` = 重置该设备为出厂默认而非删除记录**；`null` = 全清 |

响应：`200 {"ok": true}`。

| 错误 | 说明 |
|---|---|
| 200+need_confirm | 路径不存在待确认（见上） |
| 400 | 路径非绝对 / 黑名单系统目录 / 创建失败 / scan_defaults 值非法 |
| 403 | 服务用户对目标路径无写权限 |
| 409 | 有任务正在扫描/转换，禁止改存储路径 |

### 3.8 任务列表 `GET /api/admin/jobs`

```json
{"jobs": [{"name": "20261019-143025-x", "remark": "验收单", "created": "…",
           "pages": 3, "locked": false, "size": 4561234}]}
```

最新在前；`size` 为任务目录字节数；目录被外部并发删除的任务自动跳过（不 500）。

### 3.9 锁定切换 `POST /api/admin/jobs/{job}/lock`

请求：`{"locked": true}`。响应：`200 {"ok": true, "locked": true}`（锁内回读，保证与并发操作最终一致）。锁定任务不可删除、不被自动清理。

### 3.10 删除任务 `DELETE /api/admin/jobs/{job}`

响应与错误同 2.8（403 锁定 / 500 权限异常）。

### 3.11 测试扫描 `POST /api/admin/testscan`

自动创建「测试扫描」任务（150dpi 灰度 PNG 平板扫描）并启动。

响应：`200 {"ok": true, "job": "<测试任务名>", "started": true}`。**只代表已启动**——最终成败由前端轮询任务 `state` 至终态判定。

| 错误 | 说明 |
|---|---|
| 400 | scanimage 未安装 |
| 409 | 设备忙 / 转换队列满 |
| 500 | 创建或扫描失败（附 `job` 便于事后清理） |

---

## 四、页面路由（非 API，集成参考）

| 路径 | 页面 |
|---|---|
| `GET /` | 首页（创建任务 + 历史任务列表，服务端渲染） |
| `GET/POST /login`、`GET /logout` | 站点口令登录/登出（仅 `SCANWEB_TOKEN` 非空时启用） |
| `GET /job/{job}` | 任务详情页 |
| `GET /manual`、`GET /admin/manual` | 用户操作手册 / 管理员操作手册 |
| `GET /admin` | 管理页（PIN 登录） |
| `GET /architecture` | 交互式架构图（免登录） |

---

## 五、约定与演进

- **文档同步义务**：任何接口的路径、参数、响应字段、错误码变更，须与本文件同步更新并随版本发布。
- **错误提示稳定性**：`msg` 供人阅读，脚本集成**不要**依赖 `msg` 精确文案做逻辑分支（可能随版本润色），应依赖状态码 + `ok` + 业务字段。
- **兼容性原则**：现有字段只增不删不改语义；废弃字段先在本文档标注 deprecated 再移除。
- 历史接口行为变更的完整时间线见 README 版本历史条目；已知挂起项见 `KNOWN_ISSUES.md`。

> AI生成
