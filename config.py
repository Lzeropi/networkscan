import json
import os
import secrets
import shutil

SCAN_ROOT = os.environ.get("SCAN_ROOT", "/opt/smb_share/scans")
# 设备名只写后端前缀，不写 :libusb:xxx:xxx（重启后总线号会变导致失效）
DEVICE = os.environ.get("SCAN_DEVICE", "hpljm1005:")
SCAN_SOURCE = os.environ.get("SCAN_SOURCE", "")   # ADF 源名称（M1005 无 ADF，留空不传 --source）
BIND = os.environ.get("SCANWEB_BIND", "0.0.0.0")
PORT = int(os.environ.get("SCANWEB_PORT", "9203"))
TOKEN = os.environ.get("SCANWEB_TOKEN", "")           # 空 = 不启用登录
SECRET = os.environ.get("SCANWEB_SECRET") or secrets.token_hex(32)

# 存储保护（0 = 关闭）：目录总配额（字节）、任务保留天数，超限自动删最旧任务
MAX_TOTAL_BYTES = int(os.environ.get("SCAN_MAX_TOTAL_BYTES", "0"))
MAX_AGE_DAYS = int(os.environ.get("SCAN_MAX_AGE_DAYS", "0"))
MAX_PDF_PAGES = int(os.environ.get("SCANWEB_MAX_PDF_PAGES", "20"))  # PDF 合成页数上限，超限拒绝（防 OOM）
MAX_PDF_MEM = int(os.environ.get("SCANWEB_MAX_PDF_MEM", str(512 * 1024 * 1024)))  # v1.14：PDF 合成预估内存上限（字节，#5）

THUMB_SIZE = (300, 300)

# 外部命令绝对路径（systemd 环境下 PATH 不可靠，必须写死）
SCANIMAGE = os.environ.get("SCANIMAGE", shutil.which("scanimage") or "/usr/bin/scanimage")
CONVERT = os.environ.get("CONVERT_BIN", shutil.which("convert") or "/usr/bin/convert")

# ---------------- v1.10 管理页配置（admin_config.json，优先级高于环境变量） ----------------
VERSION = "1.14.1"
ADMIN_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_config.json")

# 默认值：scan_root 为空表示沿用环境变量/内置默认；三项清理策略 0 = 不启用
ADMIN_CFG_DEFAULT = {
    "pin_hash": "",                 # sha256(PIN)，空 = 尚未设置（首次访问 /admin 引导设置）
    "scan_root": "",                # 自定义扫描存储路径
    "device_alias": {},             # 设备别名映射 { "hpljm1005:libusb:001:003": "HP M1005" }
    "scan_defaults": {},            # 按设备存储扫描默认值 { "设备名": {"dpi":"150","mode":"Gray","crop":false} }
    "cleanup": {"max_jobs": 0, "max_age_days": 0, "max_total_mb": 0}
}


def load_admin_cfg():
    """读取管理配置文件；损坏/不存在时返回默认副本（不落盘）。"""
    try:
        with open(ADMIN_CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        out = dict(ADMIN_CFG_DEFAULT)
        out.update({k: cfg[k] for k in out if k in cfg})
        cl = dict(ADMIN_CFG_DEFAULT["cleanup"])
        raw = cfg.get("cleanup")
        if isinstance(raw, dict):
            cl.update({k: int(raw.get(k, 0)) for k in cl})
        out["cleanup"] = cl
        # scan_defaults 是按设备存储的字典，直接透传
        raw_sd = cfg.get("scan_defaults")
        if isinstance(raw_sd, dict):
            out["scan_defaults"] = raw_sd
        else:
            out["scan_defaults"] = {}
        return out
    except (OSError, ValueError, TypeError):
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in ADMIN_CFG_DEFAULT.items()}


def save_admin_cfg(cfg):
    """保存管理配置文件（原子写入：先写临时文件再替换）。"""
    tmp = ADMIN_CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ADMIN_CFG_PATH)


def get_scan_root():
    """运行时扫描存储路径：管理配置 > 环境变量 > 内置默认。"""
    return load_admin_cfg()["scan_root"] or SCAN_ROOT


def get_cleanup_cfg():
    """运行时清理策略（OR 组合，任一超限即执行对应清理）：0 = 不启用。"""
    return load_admin_cfg()["cleanup"]


def get_scan_defaults(device=None):
    """扫描页面默认值（按设备存储）。device 为空时返回全部。"""
    all_defaults = load_admin_cfg().get("scan_defaults", {})
    if device:
        return all_defaults.get(device, {"dpi": "150", "mode": "Gray", "crop": False})
    return all_defaults
