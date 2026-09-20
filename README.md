---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '62811e82-d0c7-400b-a55f-03fd4ead6731'
  PropagateID: '62811e82-d0c7-400b-a55f-03fd4ead6731'
  ReservedCode1: 'a1f5b640-8802-4e59-954f-65685c2c18dc'
  ReservedCode2: 'a1f5b640-8802-4e59-954f-65685c2c18dc'
---

# ScanWeb — 局域网网页扫描系统

把一台普通 USB 扫描仪（HP LaserJet M1005）变成**局域网共享扫描服务**：任何手机、平板、电脑打开浏览器即可扫描、预览、排序、下载，无需安装任何客户端软件。

| 项目信息 | 说明 |
|---|---|
| 当前版本 | v1.13 |
| 适配硬件 | hi3798mv100 机顶盒（ARM32/armhf）或其他 Linux 小主机 |
| 适配系统 | Ubuntu 20.04 (focal) / Python 3.8+ |
| 主要设备 | HP LaserJet M1005（其他 SANE 兼容扫描仪亦可） |
| 技术栈 | Flask + Waitress ｜ SANE scanimage ｜ ImageMagick convert ｜ Pillow |
| 前端形态 | 单页原生 HTML/CSS/JS，零外部依赖、零数据库、无 CDN 引用 |
| 默认端口 | 9203 |

> v1.13 更新：① 内置示例任务（输出目录为空时启动自动部署，带 🔒 锁定防清理）；② 默认端口改为 9203；③ CPU 温度优先读海思 /proc/msp/pm_cpu；④ 修复扫描报错——设备短名自动解析为完整名（hpljm1005: → hpljm1005:libusb:xxx:xxx，缓存 60 秒）+ 显式 --format=pnm 消除警告。
>
> v1.12 测试版修复记录：① 中文任务名 ZIP 下载报错（Content-Disposition 改为 RFC 5987 编码）；② 缩略图/原图缺失时返回 404 而非 500；③ 非法任务名/任务不存在统一返回 404。共 79 项端到端测试全部通过（排序 19 + 管理页 28 + 开关模式 10 + 功能 22）。

## 一、项目介绍

### 目的与作用

办公室或家庭中，扫描仪通常只能插在一台电脑上使用，其他设备要用就得拷来拷去。ScanWeb 把扫描仪接到一台低功耗 Linux 小主机（如闲置机顶盒）上，以 Web 服务形式共享给整个局域网：

- **多设备共享**：手机/平板/电脑浏览器直接访问 `http://<主机IP>:9203`，即可发起扫描并当场下载
- **零客户端**：扫描、预览、排序、打包全部在浏览器完成，移动端无需装任何 App
- **低成本**：一台闲置盒子 + 一台扫描仪即可搭建，整机功耗个位数瓦
- **数据不外传**：所有图片保存在本地磁盘，可对接 Samba 共享给局域网

### 工作原理

```
浏览器 ──HTTP──▶ Flask(Waitress:9203) ──调用──▶ scanimage(SANE) ──USB──▶ 扫描仪
                     │
                     ├── PNM 原始输出 → ImageMagick convert → PNG（+Pillow 生成缩略图）
                     └── 任务目录：<SCAN_ROOT>/<任务名>/p001.png、p002.png… + meta.json
```

hpljm1005 后端不支持 `--format=png`，扫描输出为 PNM 格式，需 convert 转 PNG 后进入任务目录（`p%03d.png` 连续编号）。

## 二、功能介绍

### 扫描功能

- **任务制管理**：每次扫描创建独立任务（备注 + 时间戳命名），历史任务卡片式展示
- **单张平板扫描**：点一下「扫描单页」扫一张，重复点击继续加页
- **ADF 连续扫描**：设备支持自动进纸时自动显示（M1005 不支持则隐藏整块区域）
- **高级选项**：分辨率（150/300/600 dpi）、色彩（彩色/灰度/黑白）、按 A4 裁边、多设备选择
- **设备能力自动探测**：启动时 `scanimage -A` 探测色彩模式与进纸源，前端动态隐藏不支持的选项

### 成果处理

- 缩略图墙预览、点击灯箱看大图、单页原图下载
- **扫码取件**（可选）：任务页生成二维码，手机扫码直达，无需输入地址
- **ZIP 打包下载**（流式传输，不受内存限制）
- **PDF 一键合成**（超 20 页自动拒绝，防 ARM32 内存 OOM）
- **页面顺序调整**：拖动图片，蓝框亮在哪张图上松手后就放到哪张图的位置，其余页面自动顺延；控制栏有「按钮排序」开关，开启后显示箭头按钮供触屏设备使用；保存后系统物理重排文件，PDF/ZIP 均按新顺序合并，无需重扫

### 内置操作手册

首页导航进入 `/manual`，覆盖创建任务、高级选项、扫描、排序、下载、清理策略等全部操作说明，新用户零门槛上手。

### 系统管理页（v1.10）

浏览器直接访问 `/admin`（首页导航**不放入口**），内置 PIN 码保护（首次访问引导设置）：

- **环境/依赖检测**：Python / SANE / ImageMagick / Pillow 版本，服务用户 lp/scanner 组成员检查（设备权限排障），存储目录可写性
- **扫描设备信息**：已连接设备列表、单张/连续（ADF）能力、分辨率实际范围、彩色/灰度/黑白支持；5 分钟缓存，手动「重新探测」；「测试扫描」真实扫一页验证设备可用
- **存储位置配置**：查看与修改图片保存路径，路径不存在时确认后自动创建，改后即时生效无需重启服务
- **自动清理策略**：任务数 / 保留天数 / 总占用 MB 三项任意组合（0 = 不启用），**任一超限即从最旧开始清理**；服务启动时、每小时后台、保存配置时各执行一次
- **任务锁定**：任意任务可设 🔒 锁定，锁定任务**永不被自动清理、不可删除**（示例/重要任务的保护机制）
- **宿主机监控**：CPU 使用率、CPU 温度、内存、磁盘占用进度条，每 30 秒自动刷新
- **任务管理**：全部任务的页数/大小/时间一览，删除（二次确认，锁定任务禁删）

## 三、安装部署

### 环境要求

| 组件 | 要求 | 用途 |
|---|---|---|
| 系统 | Ubuntu 20.04 / 任何 Linux（含 ARM32） | 运行环境 |
| Python | ≥ 3.8（系统自带 3.8 即可） | Web 服务 |
| SANE | `scanimage` | 驱动扫描仪 |
| ImageMagick | `convert` | PNM → PNG 转换 |
| Pillow | 7.x~（ARM32 无预编译包需源码编译） | 缩略图生成、PDF 合成 |

### 部署目录约定

- 服务目录：`/opt/network_scan_service`
- 扫描输出：`/opt/smb_share/scans`（默认值，可在管理页修改）
- 服务用户：`scanops`（加入 `lp`、`scanner` 组以获得 USB 设备访问权）
- systemd 服务名：`networkscan.service`

### 第一步：清理旧环境

> **目的**：避免旧代码、旧虚拟环境、旧依赖干扰新版本。

```bash
# 清空部署目录里的所有内容（含旧 .venv、旧压缩包、旧 service 等）
rm -rf /opt/network_scan_service/*
# 清理 uv 编译缓存（释放 90MB，含 Pillow 编译残留）
rm -rf /root/.cache/uv
# 验证磁盘
df -h /
```

### 第二步：上传并解压项目

> **目的**：把打包好的 ScanWeb 代码放到部署目录。

```bash
# 本机执行（传压缩包到盒子）
scp scanweb-v1.13.tar.gz root@192.168.1.203:/opt/network_scan_service/
# 盒子上执行
cd /opt/network_scan_service
tar xzf scanweb-v1.13.tar.gz --strip-components=1
rm -f scanweb-v1.13.tar.gz .python-version
```

### 第三步：安装编译依赖并安装 Python 包

> **目的**：Pillow 在 ARM32 无预编译 wheel，必须源码编译；`libjpeg-turbo8-dev` 提供编译所需的 JPEG 头文件，编译完成后可卸载以省空间。

```bash
# 1. 安装 Pillow 编译所需的 JPEG 开发库（仅 450KB，编译完可卸载）
apt update
apt install -y libjpeg-turbo8-dev
# 2. 安装 python3.8-venv（Ubuntu 20.04 默认不带，python3 -m venv 会报错）
apt install -y python3.8-venv
# 3. 创建 venv 并安装依赖
uv venv .venv
uv pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple flask waitress "pillow>=7.0,<9"
# 可选：安装 qrcode 启用任务页「扫码取件」功能（纯 Python 无编译，约 100KB）
uv pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple qrcode
# 4. 清理缓存
rm -rf /root/.cache/uv
# 5. 编译完成后可卸载开发库（不影响已编译的 Pillow 运行）
apt remove -y libjpeg-turbo8-dev && apt autoremove -y
```

<details>
<summary><b>遇到问题？点击展开排障记录（实测踩坑）</b></summary>

以下问题在 hi3798mv100 机顶盒（ARM32 / Ubuntu 20.04）实际部署时遇到过，记录于此供参考：

#### 问题 1：uv pip install 卡住不动（PyPI IPv6 连接超时）

**现象**：`uv pip install flask waitress "pillow>=7.0,<9"` 运行 12 分钟以上，只下载了 markupsafe 一个包，缓存不增长，CPU 几乎空闲。`ps` 显示 uv 进程在跑但没有进展。`ss -tnp` 显示 uv 连接的是 PyPI 的 IPv6 地址。

**原因**：uv 默认从 PyPI 官方源（pypi.org）下载，走 IPv6。盒子网络环境对 IPv6 长连接不稳定，导致下载卡死。

**解决**：加 `--index-url` 指定国内镜像源，强制走 IPv4：

```bash
uv pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple flask waitress "pillow>=7.0,<9"
```

> 清华镜像对 ARM32 设备速度快几十倍，Pillow 源码编译约 3-4 分钟即可完成。

#### 问题 2：uv 创建的 venv 里没有 pip

**现象**：`uv venv .venv` 创建的虚拟环境中没有 `pip` 命令，`.venv/bin/pip` 不存在，`.venv/bin/python -m pip` 也报 `No module named pip`。

**原因**：uv 的设计理念是自带包管理（`uv pip install`），默认不在 venv 中安装 pip。这导致想用 pip 作为备用方案时无法使用。

**解决**：改用系统 Python 的 `venv` 模块创建虚拟环境（自带 pip），或用 uv 直接安装（推荐）：

```bash
# 方案 A：用系统 venv（需要先装 python3.8-venv，见下一条）
rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install --no-cache-dir flask waitress "pillow>=7.0,<9"

# 方案 B：继续用 uv（推荐，更快）
uv venv .venv
uv pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple flask waitress "pillow>=7.0,<9"
```

#### 问题 3：python3 -m venv 报 ensurepip 不可用

**现象**：`python3 -m venv .venv` 报错：
```
The virtual environment was not created successfully because ensurepip is not available.
On Debian/Ubuntu systems, you need to install the python3-venv package.
```

**原因**：Ubuntu 20.04 默认不安装 `python3.8-venv` 包，`venv` 模块依赖它才能创建带 pip 的虚拟环境。

**解决**：先装 venv 包再建环境：

```bash
apt install -y python3.8-venv
python3 -m venv .venv
.venv/bin/pip --version    # 确认 pip 可用
```

> 如果直接用 `uv pip install` 安装则不需要这一步（uv 自带包管理），但建议装上以备排障时用 pip。

#### 问题 4：Pillow 源码编译耗时较长

**现象**：Pillow 在 ARM32 上没有预编译 wheel，`uv pip install "pillow>=7.0,<9"` 会下载源码并本地编译，耗时 3-5 分钟，期间 CPU 满载。

**原因**：ARM32 架构 PyPI 不提供 Pillow 的二进制 wheel，必须从 C 源码编译，J4125 级别的 CPU 需要几分钟。

**解决**：这是正常现象，耐心等待即可。确保已装 `libjpeg-turbo8-dev`（提供 JPEG 头文件），否则编译会失败。编译完成后可卸载该开发库。

```bash
# 编译前装
apt install -y libjpeg-turbo8-dev
# 编译后卸载（不影响已编译好的 Pillow）
apt remove -y libjpeg-turbo8-dev && apt autoremove -y
```

</details>

### 第四步：创建服务用户

> **目的**：以非 root 运行服务；加入 `lp`、`scanner` 组后才有权限访问 USB 扫描设备（实测 M1005 的 USB 设备节点属 `root:lp`）。

```bash
useradd -r -m -s /usr/sbin/nologin scanops
usermod -aG lp,scanner scanops
chown -R scanops:scanops /opt/network_scan_service
mkdir -p /opt/smb_share/scans && chown -R scanops:scanops /opt/smb_share/scans
# 验证设备可见（应列出 hpljm1005）
sudo -u scanops scanimage -L
```

### 第五步：安装 systemd 服务

> **目的**：开机自启、崩溃自动拉起。

```bash
cp deploy/networkscan.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now networkscan
```

### 第六步：验证

> **目的**：确认服务与设备全部就绪。

```bash
systemctl status networkscan --no-pager
curl -s http://127.0.0.1:9203/ | head -5
# 局域网任意设备浏览器打开 http://192.168.1.203:9203 即可使用
# 管理页：http://192.168.1.203:9203/admin（首次访问设置 PIN）
```

## 稳定性设计

- systemd `Restart=always`：服务异常退出 3 秒后自动拉起
- 启动时自动清理 `/tmp` 中上次中断残留的 PNM 临时文件，防止小存储被占满
- 扫描仪全局独占锁（线程锁），平板与 ADF 不会同时抢设备
- PDF 页数上限（默认 20 页）防 ARM32 内存 OOM；ZIP 为流式生成不占内存

## 四、运维命令

```bash
systemctl status networkscan      # 状态
systemctl restart networkscan     # 重启
journalctl -u networkscan -f      # 看日志
ss -tlnp | grep 9203              # 确认端口监听
```

## 五、配置说明

### 环境变量（编辑 `/etc/systemd/system/networkscan.service` 的 `Environment=` 行，改后 `systemctl daemon-reload && systemctl restart networkscan`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SCAN_ROOT` | /opt/smb_share/scans | 扫描输出根目录（仅作初始默认，v1.10 起可在管理页运行时修改并持久化） |
| `SCANWEB_PORT` | 9203 | 监听端口 |
| `SCANWEB_TOKEN` | 空（不启用） | 全站访问口令，设置后需登录 |
| `SCAN_DEVICE` | hpljm1005: | 设备后端名，只写前缀，不带 `:libusb:xxx:xxx` |
| `SCAN_SOURCE` | 空 | ADF 源名称。M1005 无 ADF 留空；换设备后用 `scanimage -A` 查看 |
| `SCANWEB_MAX_PDF_PAGES` | 20 | PDF 合成页数上限，防 ARM32 内存 OOM |

> v1.10 起存储路径与三项清理策略（任务数 / 保留天数 / 总占用）改由管理页配置，持久化在服务目录 `admin_config.json`（优先级高于上述环境变量），保存后即时生效无需重启；PIN 以 SHA-256 存储，删除该文件即可重置 PIN 与全部管理配置。

### 目录结构

```
/opt/network_scan_service/
├── app.py            # Flask 路由与主入口（Waitress 启动）
├── admin.py          # v1.10 管理页：PIN/依赖检测/设备/监控/配置/任务管理
├── scanner.py        # scanimage 调用、PNM→PNG、缩略图、扫描状态
├── jobs.py           # 任务/页面文件管理、锁定、自动清理
├── config.py         # 环境变量与管理配置文件（admin_config.json）读取
├── templates/        # Jinja2 页面（首页/任务页/手册/管理页/登录）
├── static/           # app.js / admin.js / style.css（零外部依赖）
└── deploy/networkscan.service
```

## 六、设备能力（HP M1005 root 实测）

| 项目 | 实测结果 |
|---|---|
| 设备 | `hpljm1005:libusb:001:003`（USB 已连接） |
| 分辨率 | 75/100/150/200/300/600/1200 dpi |
| 模式 | Gray、Color（无 Lineart） |
| 扫描区域 | 220mm × 330mm |
| ADF | 不支持（`scanimage -A` 无 `--source` 选项） |
| 输出格式 | 默认 PNM（convert 必需） |
| USB 权限 | `/dev/bus/usb/001/003` 属 `root:lp`，服务用户需加入 lp 组 |

## 七、使用指南

1. 首页填备注（选填）→「创建并开始」，跳转任务页
2. 任务页点「扫描单页」扫描，完成后缩略图自动出现在图墙
3. 页序不对时点「调整顺序」拖拽调整（蓝框 = 松手落位），「保存顺序」生效
4. 「打包 ZIP」流式下载全部原图；「合成 PDF」合并为 PDF（超 20 页提示用 ZIP）
5. 首页右上角「操作手册」查看完整图文说明
6. 管理员访问 `/admin`：设备检测、测试扫描、存储路径、清理策略、任务锁定/删除、宿主机监控

## 八、常见问题

- **看不到设备**：`sudo -u scanops scanimage -L` 验证权限，检查 lp/scanner 组
- **ADF 报错**：M1005 无 ADF，保持平板模式
- **Pillow 编译失败**：需先装 `libjpeg-turbo8-dev` 再编译，详见第三步
- **uv 安装卡住不动**：PyPI IPv6 连接问题，加 `--index-url https://pypi.tuna.tsinghua.edu.cn/simple` 换清华镜像源，详见第三步排障记录
- **删除任务提示"扫描进行中"**：等待扫描完成后再删除
- **管理页忘记 PIN**：删除服务目录下 `admin_config.json` 后重启服务，重新设置
- **自动清理误删担心**：给重要任务上 🔒 锁定，锁定任务永不参与清理

> AI生成