import os
import shutil

SCAN_ROOT = os.environ.get("SCAN_ROOT", "/opt/smb_share/scan")
# 设备名只写后端前缀，不写 :libusb:xxx:xxx（重启后总线号会变导致失效）
DEVICE = os.environ.get("SCAN_DEVICE", "hpljm1005:")
SCAN_SOURCE = os.environ.get("SCAN_SOURCE", "ADF")    # ADF 源名称，用 scanimage -A 查看
BIND = os.environ.get("SCANWEB_BIND", "0.0.0.0")
PORT = int(os.environ.get("SCANWEB_PORT", "9230"))
TOKEN = os.environ.get("SCANWEB_TOKEN", "")           # 空 = 不启用登录
SECRET = os.environ.get("SCANWEB_SECRET", "scanweb-secret-change-me")

# 存储保护（0 = 关闭）：目录总配额（字节）、任务保留天数，超限自动删最旧任务
MAX_TOTAL_BYTES = int(os.environ.get("SCAN_MAX_TOTAL_BYTES", "0"))
MAX_AGE_DAYS = int(os.environ.get("SCAN_MAX_AGE_DAYS", "0"))

THUMB_SIZE = (300, 300)

# 外部命令绝对路径（systemd 环境下 PATH 不可靠，必须写死）
SCANIMAGE = os.environ.get("SCANIMAGE", shutil.which("scanimage") or "/usr/bin/scanimage")
CONVERT = os.environ.get("CONVERT_BIN", shutil.which("convert") or "/usr/bin/convert")
