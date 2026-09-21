---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'e9180874-611d-451b-9af8-a8f809532b2f'
  PropagateID: 'e9180874-611d-451b-9af8-a8f809532b2f'
  ReservedCode1: 'bad39349-6e28-415c-9d6c-d13bd9b876ac'
  ReservedCode2: 'bad39349-6e28-415c-9d6c-d13bd9b876ac'
---

# ScanWeb — 局域网扫描仪网页端（HP M1005 适配）

平板 / ADF 扫描仪的局域网 Web 界面：创建任务 → 逐页或连续扫描 → 缩略图预览 → ZIP / PDF 一键下载。

**适配环境：Ubuntu 20.04 (focal) / ARM32 (armhf) / Python 3.8 / HP LaserJet M1005**

## 扫描流程（适配 hpljm1005 后端）

该后端不支持 `--format=png`，输出为 PNM 格式，因此流程为：

```
scanimage → /tmp/scanweb_<任务>_<页码>.pnm
    ↓ convert（ImageMagick，必需）
<SCAN_ROOT>/<任务>/p001.png
```

## 目录结构

```
network_scan_service/
├── app.py / scanner.py / jobs.py / config.py
├── pyproject.toml
├── templates/           base / index / job / login
├── static/              app.js / style.css
└── deploy/scanweb.service
```

扫描输出：`<SCAN_ROOT>/<YYYYMMDD-HHMMSS>[_备注]/p001.png ...`，缩略图在 `.thumbs/`，元数据 `meta.json`。

## 安装（focal + ARM32 + Python 3.8）

### 前置条件

```bash
# 系统依赖（已确认 imagemagick 6.9.10、sane-utils 1.0.29 已装）
sudo apt update
sudo apt install -y imagemagick python3
```

### 方案一：uv 部署（推荐，常驻约 25MB）

盒子上 uv 已装在 `/usr/local/bin/uv`（v0.12.11）。如果重装系统，用官方脚本安装：
`curl -LsSf https://astral.sh/uv/install.sh | sh`

```bash
# 1. 部署项目到 /opt/network_scan_service
sudo mkdir -p /opt/network_scan_service && cd /opt/network_scan_service
# 把 tar.gz 解压到当前目录后：
# 删除 .python-version 文件（如果存在），避免 uv 下载独立 Python
rm -f .python-version

# 2. 创建 venv（复用系统 Python 3.8，省 ~71MB）
uv venv --python /usr/bin/python3 .venv

# 3. 安装依赖（Pillow 在 ARM32 上可能需要编译，见下方备用方案）
uv sync --no-dev --python /usr/bin/python3
uv cache clean                                # 可选，释放缓存
```

> **关键**：必须删除 `.python-version` 文件（里面写的 3.10 会导致 uv 下载独立 Python，
> 白白多占 71MB）。改用 `--python /usr/bin/python3` 强制复用系统 3.8。

### 方案二：纯 pip 部署（不依赖 uv）

```bash
sudo apt install -y python3-venv python3-pip
cd /opt/network_scan_service
python3 -m venv .venv
.venv/bin/pip install --no-cache-dir flask waitress pillow
```

> 注意：ARM32 上 Pillow 大概率没有预编译 wheel，pip 会从源码编译（需 `build-essential`，
> 编译 2~5 分钟，内存 <1GB 可能 OOM）。如果编译失败，用下方备用方案。

### 备用方案：Pillow 编译失败时（盒子上 apt-cache 查不到 python3-pil）

`python3-pil` 在此盒子的 apt 源里不可用，改用 `--system-site-packages` 复用系统已装的包，
或用 pip 指定旧版 Pillow（7.x 有 ARM32 的预编译 wheel）：

```bash
# 方案 A：uv + 系统包共享
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv sync --no-dev --python /usr/bin/python3
# 如果 uv 仍尝试装 Pillow，手动跳过：
# .venv/bin/pip install flask waitress  # 只装这两个，Pillow 用系统的

# 方案 B：pip 指定 Pillow 旧版（避免编译）
.venv/bin/pip install --no-cache-dir flask waitress "pillow<9"
```

## 运行权限与 systemd

```bash
# 创建服务用户并加入扫描仪权限组
sudo useradd -r -s /usr/sbin/nologin scanops 2>/dev/null || true
sudo usermod -aG lp,scanner scanops              # SANE 需要 lp 组访问 USB 设备

# 扫描输出目录（已存在 /opt/smb_share/scans，权限 rwxrwxrwx）
sudo chown -R scanops:scanops /opt/smb_share/scans
sudo chmod g+s /opt/smb_share/scans              # 新任务目录继承组，SMB 用户可读

# 启用 systemd 服务
sudo cp deploy/scanweb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scanweb
```

浏览器访问 `http://192.168.1.203:9230`。手机浏览器同样可用，界面已适配小屏。

> **端口状态**：已确认 9230 端口空闲，无冲突。histb 源的 scanweb-histb 包已被彻底移除，无残留。

## 配置（写进 service 的 Environment）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SCAN_ROOT` | /opt/smb_share/scans | 扫描输出根目录 |
| `SCANWEB_PORT` | 9230 | 监听端口 |
| `SCANWEB_TOKEN` | 空（不启用） | 访问口令，设置后需登录 |
| `SCAN_DEVICE` | hpljm1005: | 设备后端名。**只写前缀，不要带 `:libusb:001:003`**（重启后总线号会变导致找不到设备） |
| `SCAN_SOURCE` | 空 | ADF 源名称。M1005 无 ADF，留空不传 `--source`；换设备后用 `scanimage -A` 查看 |
| `SCAN_MAX_TOTAL_BYTES` | 0 | 扫描目录总配额（字节），超限自动删最旧任务 |
| `SCAN_MAX_AGE_DAYS` | 0 | 任务保留天数，超龄自动删除 |
| `SCANWEB_MAX_PDF_PAGES` | 20 | PDF 合成页数上限，超限拒绝（防 ARM32 内存 OOM） |

## 设备能力确认（root 实测）

| 项目 | 实测结果 |
|---|---|
| 设备 | `hpljm1005:libusb:001:003`（USB 已连接） |
| 分辨率 | 75/100/150/200/300/600/1200 dpi |
| 模式 | Gray、Color（**无 Lineart**） |
| 扫描区域 | 220mm × 330mm |
| ADF | **不支持**（`scanimage -A` 无 `--source` 选项） |
| 输出格式 | 默认 PNM（不支持 `--format=png`，convert 必需） |
| USB 权限 | `/dev/bus/usb/001/003` 属 `root:lp`，服务用户需加入 lp 组 |

## 使用

1. 首页填备注（选填）→「创建并开始」。备注会拼进文件夹名：`20260909-103600_验收单`。
2. 任务页点「扫描一页（平板）」，每页完成后缩略图实时出现在图墙；点图看大图。
3. 「打包 ZIP」流式下载全部原图；「合成 PDF」合并成一份 PDF（超过 20 页时提示用 ZIP）。
4. 高级选项里勾选「连续进纸」的任务会多出「开始连续扫描（ADF）」按钮，整叠纸一次扫完；
   平板任务任何时候都可用「扫描一页」补扫。**M1005 无 ADF，保持默认平板模式即可。**
5. 高级选项里可勾选「按 A4 尺寸裁边」，等效于原脚本的 `-x 210 -y 297`。

## 常见问题

- 看不到设备：`sudo -u scanops scanimage -L` 验证权限；检查 lp/scanner 组。
  （已确认：root 下能看到设备，普通用户不在 lp 组则看不到）
- ADF 报错：M1005 没有自动进纸器，ADF 模式不可用，保持默认平板模式即可。
- 想用 SMB 直接访问：Samba 当前未配置 scans 共享段，需手动添加或直接用 Web 下载。
- ARM32 上 Pillow 安装：如果编译失败，见上方「备用方案」，用 `--system-site-packages`
  或指定 `pillow<9` 安装旧版预编译 wheel。
- 删除任务时提示"扫描进行中"：等待 ADF 连续扫描完成后再删除（已加并发保护）。

> AI生成