---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '6d0a8b7b-56cb-4fd7-ab60-ce84387a1a4c'
  PropagateID: '6d0a8b7b-56cb-4fd7-ab60-ce84387a1a4c'
  ReservedCode1: '796fbd61-1c9a-49d1-a87a-3101a9edb111'
  ReservedCode2: '796fbd61-1c9a-49d1-a87a-3101a9edb111'
---

# ScanWeb — 局域网扫描仪网页端（HP M1005 适配）

平板 / ADF 扫描仪的局域网 Web 界面：创建任务 → 逐页或连续扫描 → 缩略图预览 → ZIP / PDF 一键下载。

**适配环境：Ubuntu 20.04 (focal) / ARM32 (armhf) / HP LaserJet M1005**

## 扫描流程（适配 hpljm1005 后端）

该后端不支持 `--format=png`，输出为 PNM 格式，因此流程为：

```
scanimage → /tmp/scanweb_<任务>_<页码>.pnm
    ↓ convert（ImageMagick，必需）
<SCAN_ROOT>/<任务>/p001.png
```

## 目录结构

```
scanweb/
├── app.py / scanner.py / jobs.py / config.py
├── templates/           base / index / job / login
├── static/              app.js / style.css
└── deploy/scanweb.service
```

扫描输出：`<SCAN_ROOT>/<YYYYMMDD-HHMMSS>[_备注]/p001.png ...`，缩略图在 `.thumbs/`，元数据 `meta.json`。

## 安装（focal + ARM32，常驻约 25MB）

focal 的 apt 源里没有 uv 包，用官方脚本安装（装完可删，不占运行空间）：

```bash
# 1. 系统依赖
sudo apt update
sudo apt install -y imagemagick curl python3

# 2. 安装 uv（官方脚本，装到 ~/.local/bin；root 用户则在 /root/.local/bin）
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 3. 部署项目
sudo mkdir -p /opt/scanweb && cd /opt/scanweb
# 把 tar.gz 解压到当前目录后：
uv venv --python /usr/bin/python3 .venv      # 复用系统 Python 3.8，省 ~80MB
uv sync --no-dev --python /usr/bin/python3
uv cache clean                                # 可选，释放 ~25MB 缓存
```

> 如果盒子访问外网困难，uv 也可用 pip 安装：`sudo apt install -y python3-pip && pip3 install uv`。

### 备用方案：ARM32 上 Pillow 编译失败时（推荐先用这个）

armhf 架构如果拉不到 Pillow 预编译 wheel，`uv sync` 会现场编译（需 gcc，2~5 分钟），
盒子内存小于 1GB 时可能编译失败或卡死。**改用系统自带的 ARM 版 Pillow，免编译：**

```bash
# 方案 A：继续用 uv，让 venv 复用系统已装的 Pillow（不需要编译）
sudo apt install -y python3-pil              # 系统自带 Pillow 7.2，已满足 pyproject 的 pillow>=7.0
uv venv --python /usr/bin/python3 --system-site-packages .venv   # 关键：创建时启用系统包
uv sync --no-dev --python /usr/bin/python3   # 系统 Pillow 已满足，uv 跳过安装

# 方案 B：完全不用 uv，纯 pip（更简单直观，Pillow 直接用系统的）
sudo apt install -y python3-pil python3-venv
cd /opt/scanweb
python3 -m venv .venv
.venv/bin/pip install --no-cache-dir flask waitress   # 不装 Pillow，用系统 7.2
```

> 注意：方案 A 的 `--system-site-packages` 必须在 `uv venv` 创建时指定才生效。
> 两个方案的 systemd `ExecStart` 都不用改，仍是 `.venv/bin/python app.py`。

## 运行权限与 systemd

```bash
sudo useradd -r -s /usr/sbin/nologin scanops 2>/dev/null || true
sudo usermod -aG lp,scanner scanops              # SANE 需要扫描设备权限
sudo chown -R scanops:scanops /opt/smb_share/scan
sudo chmod g+s /opt/smb_share/scan               # 新任务目录继承组，SMB 用户可读

sudo cp deploy/scanweb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scanweb
```

浏览器访问 `http://<盒子IP>:9230`。手机浏览器同样可用，界面已适配小屏。

> **注意端口冲突**：如果你的盒子上已装有 histb 源里的 `scanweb` 包（apt list 里显示 [installed]），
> 它可能已占用 9230 端口。先 `sudo systemctl stop scanweb-histb`（或类似服务名）停掉它，
> 或者把我们的端口改成 9231（改 service 里 Environment=SCANWEB_PORT=9231）。

## 配置（写进 service 的 Environment）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SCAN_ROOT` | /opt/smb_share/scan | 扫描输出根目录 |
| `SCANWEB_PORT` | 9230 | 监听端口 |
| `SCANWEB_TOKEN` | 空（不启用） | 访问口令，设置后需登录 |
| `SCAN_DEVICE` | hpljm1005: | 设备后端名。**只写前缀，不要带 `:libusb:001:002`**（重启后总线号会变导致找不到设备） |
| `SCAN_SOURCE` | ADF | ADF 源名称，部分设备叫 `Document Feeder`，用 `scanimage -A` 查 |
| `SCAN_MAX_TOTAL_BYTES` | 0 | 扫描目录总配额（字节），超限自动删最旧任务 |
| `SCAN_MAX_AGE_DAYS` | 0 | 任务保留天数，超龄自动删除 |

## 使用

1. 首页填备注（选填）→「创建并开始」。备注会拼进文件夹名：`20260909-103600_验收单`。
2. 任务页点「扫描一页（平板）」，每页完成后缩略图实时出现在图墙；点图看大图。
3. 「打包 ZIP」流式下载全部原图；「合成 PDF」合并成一份 PDF。
4. 高级选项里勾选「连续进纸」的任务会多出「开始连续扫描（ADF）」按钮，整叠纸一次扫完；
   平板任务任何时候都可用「扫描一页」补扫。
5. 高级选项里可勾选「按 A4 尺寸裁边」，等效于原脚本的 `-x 210 -y 297`。

## 常见问题

- 看不到设备：`sudo -u scanops scanimage -L` 验证权限；检查 lp/scanner 组。
- ADF 报错：M1005 没有自动进纸器，ADF 模式不可用，保持默认平板模式即可。
- 想用 SMB 直接访问：任务目录与普通扫描文件同目录同权限，无需额外配置。
- ARM32 上 Pillow 安装：优先用 `sudo apt install python3-pil`（系统自带 ARM 版，免编译），
  不要让它源码编译，详见上方「备用方案」。

> AI生成