---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'ca154f89-41b9-48bc-9c4b-f4afd2ac521c'
  PropagateID: 'ca154f89-41b9-48bc-9c4b-f4afd2ac521c'
  ReservedCode1: '3e1c8bd9-0b42-479d-9b28-62dffd60665c'
  ReservedCode2: '3e1c8bd9-0b42-479d-9b28-62dffd60665c'
---

# ScanWeb — 局域网扫描仪网页端（HP M1005 适配）

平板 / ADF 扫描仪的局域网 Web 界面：创建任务 → 逐页或连续扫描 → 缩略图预览 → ZIP / PDF 一键下载。

**适配环境：Ubuntu 20.04 (focal) / ARM32 (armhf) / Python 3.8 / HP LaserJet M1005**

## 扫描流程

hpljm1005 后端不支持 `--format=png`，输出为 PNM 格式，需 ImageMagick convert 转换：

```
scanimage → /tmp/scanweb_<任务>_<页码>.pnm → convert → <SCAN_ROOT>/<任务>/p001.png
```

## 目录结构

```
network_scan_service/
├── app.py / scanner.py / jobs.py / config.py   后端
├── pyproject.toml                              依赖声明
├── templates/    base / index / job / login / manual
├── static/       app.js / style.css
└── deploy/networkscan.service                  systemd 服务
```

扫描输出：`<SCAN_ROOT>/<YYYYMMDD-HHMMSS>[_备注]/p001.png ...`，缩略图在 `.thumbs/`，元数据 `meta.json`。

## 完整部署步骤

> 前置确认：以下依赖已在盒子上确认安装，无需额外操作：
> Python 3.8.10、scanimage 1.0.29、ImageMagick 6.9.10、uv 0.12.11、build-essential、python3-dev、python3-pip
>
> 需要额外安装（第三步会执行）：libjpeg-turbo8-dev（Pillow 编译依赖，450KB，编译完可卸载）

### 第一步：清理

```bash
# 清空部署目录里的所有内容（含旧 .venv、旧压缩包、旧 service 等）
rm -rf /opt/network_scan_service/*

# 清理 uv 编译缓存（释放 90MB，含 Pillow 10.4 编译残留）
uv cache clean

# 验证磁盘
df -h /
```

### 第二步：上传并解压项目

```bash
# 本机执行（传压缩包到盒子）
scp scanweb-v1.8.tar.gz root@192.168.1.203:/opt/network_scan_service/

# 盒子上执行
cd /opt/network_scan_service
tar xzf scanweb-v1.8.tar.gz --strip-components=1
rm -f .python-version
rm -f scanweb-v1.8.tar.gz
ls -R
```

### 第三步：安装编译依赖并安装 Python 包

```bash
# 1. 安装 Pillow 编译所需的 JPEG 开发库（仅 450KB，编译完可卸载）
apt install -y libjpeg-turbo8-dev

# 2. 创建 venv 并安装依赖
cd /opt/network_scan_service
uv venv --python /usr/bin/python3 .venv
uv sync --no-dev --python /usr/bin/python3

# 3. 清理缓存
uv cache clean

# 4. 编译完成后可卸载开发库（不影响已编译的 Pillow 运行）
apt remove -y libjpeg-turbo8-dev && apt autoremove -y
```

> Pillow 在 ARM32 上无预编译 wheel，需从源码编译（约 2~5 分钟），`libjpeg-turbo8-dev` 提供 JPEG 头文件。
> 如果 uv sync 超过 5 分钟无输出，Ctrl+C 后用 pip 方案：
> ```bash
> rm -rf .venv
> uv venv --python /usr/bin/python3 .venv
> .venv/bin/pip install --no-cache-dir flask waitress "pillow<9"
> uv cache clean
> apt remove -y libjpeg-turbo8-dev && apt autoremove -y
> ```

### 第四步：创建服务用户

```bash
useradd -r -s /usr/sbin/nologin scanops 2>/dev/null || true
usermod -aG lp,scanner scanops
chown -R scanops:scanops /opt/smb_share/scans
chmod g+s /opt/smb_share/scans
```

### 第五步：安装 systemd 服务

```bash
cp /opt/network_scan_service/deploy/networkscan.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now networkscan

systemctl status networkscan
journalctl -u networkscan -f
```

### 第六步：验证

浏览器访问 `http://192.168.1.203:9230`，能看到首页即部署成功。手机浏览器同样可用。

## 运维命令

```bash
systemctl status networkscan       # 查看状态
systemctl restart networkscan      # 重启（改配置后必须）
systemctl stop networkscan         # 停止
systemctl disable networkscan      # 取消开机自启
journalctl -u networkscan -f       # 实时日志
sudo -u scanops scanimage -L       # 验证扫描仪权限
```

## 配置项

编辑 `/etc/systemd/system/networkscan.service` 中的 `Environment=` 行，改后执行：
`systemctl daemon-reload && systemctl restart networkscan`

| 变量 | 默认 | 说明 |
|---|---|---|
| `SCAN_ROOT` | /opt/smb_share/scans | 扫描输出根目录 |
| `SCANWEB_PORT` | 9230 | 监听端口 |
| `SCANWEB_TOKEN` | 空（不启用） | 访问口令，设置后需登录 |
| `SCAN_DEVICE` | hpljm1005: | 设备后端名，只写前缀，不带 `:libusb:xxx:xxx` |
| `SCAN_SOURCE` | 空 | ADF 源名称。M1005 无 ADF 留空；换设备后用 `scanimage -A` 查看 |
| `SCAN_MAX_TOTAL_BYTES` | 0 | 扫描目录总配额（字节），超限自动删最旧任务 |
| `SCAN_MAX_AGE_DAYS` | 0 | 任务保留天数，超龄自动删除 |
| `SCANWEB_MAX_PDF_PAGES` | 20 | PDF 合成页数上限，防 ARM32 内存 OOM |

## 设备能力（root 实测）

| 项目 | 实测结果 |
|---|---|
| 设备 | `hpljm1005:libusb:001:003`（USB 已连接） |
| 分辨率 | 75/100/150/200/300/600/1200 dpi |
| 模式 | Gray、Color（无 Lineart） |
| 扫描区域 | 220mm × 330mm |
| ADF | 不支持（`scanimage -A` 无 `--source` 选项） |
| 输出格式 | 默认 PNM（convert 必需） |
| USB 权限 | `/dev/bus/usb/001/003` 属 `root:lp`，服务用户需加入 lp 组 |

## 使用说明

1. 首页填备注（选填）→「创建并开始」
2. 任务页点「扫描单页」扫描，完成后缩略图自动出现在图墙
3. 「打包 ZIP」流式下载全部原图；「合成 PDF」合并为 PDF（超 20 页提示用 ZIP）
4. 高级选项：扫描方式（自动探测设备能力动态显隐）、分辨率、色彩、设备
5. 首页右上角「操作手册」链接可查看完整使用说明

## 常见问题

- 看不到设备：`sudo -u scanops scanimage -L` 验证权限，检查 lp/scanner 组
- ADF 报错：M1005 无 ADF，保持平板模式
- Pillow 编译失败：需先装 `libjpeg-turbo8-dev` 再编译，详见第三步；如 uv sync 卡住用 pip 方案
- 删除任务提示"扫描进行中"：等待扫描完成后再删除

> AI生成