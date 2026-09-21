---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'c810ebb9-56d8-46f6-9128-bcfea998ba5d'
  PropagateID: 'c810ebb9-56d8-46f6-9128-bcfea998ba5d'
  ReservedCode1: '134958b6-45d9-4058-8a22-05055338d960'
  ReservedCode2: '134958b6-45d9-4058-8a22-05055338d960'
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
├── templates/           base / index / job / login / manual
├── static/              app.js / style.css
└── deploy/networkscan.service
```

扫描输出：`<SCAN_ROOT>/<YYYYMMDD-HHMMSS>[_备注]/p001.png ...`，缩略图在 `.thumbs/`，元数据 `meta.json`。

## 完整部署步骤

> **前置确认**：以下依赖已在盒子上确认安装，无需额外操作：
> Python 3.8.10、scanimage 1.0.29、ImageMagick 6.9.10、uv 0.12.11、build-essential、python3-dev、python3-pip

### 第一步：清理旧残留

```bash
# 删除旧的 .venv（如果存在，可能指向错误的 Python 3.10）
rm -rf /opt/network_scan_service/.venv

# 清理 uv 之前误下载的独立 Python 3.10（释放 71MB）
rm -rf /root/.local/share/uv/python/cpython-3.10.21-linux-armv7-gnueabihf
rm -f /root/.local/share/uv/python/cpython-3.10-linux-armv7-gnueabihf

# 清理 uv 缓存
uv cache clean

# 验证磁盘释放
df -h /
```

### 第二步：上传并解压项目

```bash
# 在本机执行（将 v1.7 压缩包传到盒子）
scp scanweb-v1.7.tar.gz root@192.168.1.203:/opt/network_scan_service/

# 在盒子上执行
cd /opt/network_scan_service
tar xzf scanweb-v1.7.tar.gz --strip-components=1
rm -f .python-version          # 保险：v1.7 包内不含此文件，但防止残留导致 uv 下载独立 Python
rm -f scanweb-v1.7.tar.gz      # 解压完可删，减少占用

# 验证文件齐全（14个文件 + 2个目录）
ls -R
```

### 第三步：创建虚拟环境并安装依赖

```bash
cd /opt/network_scan_service

# 创建 venv（复用系统 Python 3.8，不下载独立 Python）
uv venv --python /usr/bin/python3 .venv

# 安装依赖（Pillow 在 ARM32 上需源码编译，约 2~5 分钟，耐心等待）
uv sync --no-dev --python /usr/bin/python3

# 清理 uv 缓存（释放空间）
uv cache clean
```

> **如果 Pillow 编译失败**（内存不足 OOM），使用备用方案：
> ```bash
> rm -rf .venv
> uv venv --python /usr/bin/python3 --system-site-packages .venv
> .venv/bin/pip install flask waitress "pillow<9"
> ```
> `pillow<9` 有 ARM32 预编译 wheel，不需要编译。

### 第四步：创建服务用户

```bash
# 创建系统用户（用于运行服务，不能登录）
useradd -r -s /usr/sbin/nologin scanops 2>/dev/null || true

# 加入 lp 和 scanner 组（SANE 需要 lp 组访问 USB 设备）
usermod -aG lp,scanner scanops

# 设置扫描输出目录权限
chown -R scanops:scanops /opt/smb_share/scans
chmod g+s /opt/smb_share/scans
```

### 第五步：安装 systemd 服务

```bash
cp /opt/network_scan_service/deploy/networkscan.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now networkscan

# 验证服务状态
systemctl status networkscan

# 查看实时日志（Ctrl+C 退出）
journalctl -u networkscan -f
```

### 第六步：验证

浏览器访问 `http://192.168.1.203:9230`，能看到首页即部署成功。
手机浏览器同样可用，界面已适配小屏。

## 运维命令

```bash
# 查看服务状态
systemctl status networkscan

# 重启服务（改了 Environment 后必须）
systemctl restart networkscan

# 停止 / 取消开机自启
systemctl stop networkscan
systemctl disable networkscan

# 查看实时日志
journalctl -u networkscan -f

# 验证扫描仪权限（以服务用户身份）
sudo -u scanops scanimage -L
```

## 配置（写进 service 的 Environment）

编辑 `/etc/systemd/system/networkscan.service` 中的 `Environment=` 行，改完后执行：
`systemctl daemon-reload && systemctl restart networkscan`

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
2. 任务页点「扫描单页」，每页完成后缩略图实时出现在图墙；点图看大图。
3. 「打包 ZIP」流式下载全部原图；「合成 PDF」合并成一份 PDF（超过 20 页时提示用 ZIP）。
4. 高级选项里勾选「连续进纸」的任务会多出「开始连续扫描（ADF）」按钮，整叠纸一次扫完；
   平板任务任何时候都可用「扫描单页」补扫。**M1005 无 ADF，保持默认平板模式即可。**
5. 高级选项里可勾选「按 A4 尺寸裁边」，等效于原脚本的 `-x 210 -y 297`。
6. 顶部导航栏右侧「操作手册」链接可查看完整使用说明。

## 常见问题

- 看不到设备：`sudo -u scanops scanimage -L` 验证权限；检查 lp/scanner 组。
  （已确认：root 下能看到设备，普通用户不在 lp 组则看不到）
- ADF 报错：M1005 没有自动进纸器，ADF 模式不可用，保持默认平板模式即可。
- 想用 SMB 直接访问：Samba 当前未配置 scans 共享段，需手动添加或直接用 Web 下载。
- ARM32 上 Pillow 安装：如果编译失败，见部署第三步的备用方案。
- 删除任务时提示"扫描进行中"：等待 ADF 连续扫描完成后再删除（已加并发保护）。

> AI生成