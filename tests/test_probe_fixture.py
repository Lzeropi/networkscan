# tests/test_probe_fixture.py — v1.14.1 设备探测真实 fixture 回归（P2-17 + P0-2）
# 样本来源：203 盒子 scanimage -L/-A 真实输出（2026-09-23 实测）+ 各后端已知输出格式
import os
import re
import sys
import tempfile

_TEST_ROOT = tempfile.mkdtemp(prefix="scanweb_fixture_")
os.environ["SCAN_ROOT"] = _TEST_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device_probe  # noqa: E402

# 203 真机原始输出（cat -A 验证过：反引号开头 + 单引号结尾）
REAL_203_L = ("device `hpljm1005:libusb:001:003' is a Hewlett-Packard LaserJet M1005 "
              "multi-function peripheral\n")

REAL_203_A = """Output format is not set, using pnm as a default.
scanimage: rounded value of br-x from 220 to 220
scanimage: rounded value of br-y from 330 to 330

All options specific to device `hpljm1005:libusb:001:003':
    --resolution 75|100|150|200|300|600|1200dpi [75]
        resolution
    --mode Gray|Color [Color]
        Selects the scan mode (e.g., lineart, monochrome, or color).
"""

DEV_RE = re.compile(r"device\s+(?:[`']([^`']+)[`']|(\S+))\s+is\s+a\s+(.*)")


def _names(text):
    return [m.group(1) or m.group(2) for m in DEV_RE.finditer(text)]


def test_203_real_output_matches():
    """203 真机输出必须匹配（防修复导致现网回归）。"""
    assert _names(REAL_203_L) == ["hpljm1005:libusb:001:003"]


def test_single_quote_backend():
    """epkowa 等后端单引号输出（旧版反引号正则会漏，P0-2 核心）。"""
    t = "device 'epkowa:net:192.168.1.5' is a EPSON WF-4830 Series"
    assert _names(t) == ["epkowa:net:192.168.1.5"]


def test_no_quote_backend():
    """pixma/airscan 无引号输出（两种旧正则都会漏）。"""
    assert _names("device pixma:04A917F2_076965 is a Canon PIXMA") == ["pixma:04A917F2_076965"]
    assert _names("device airscan:w10:HP:5E0A:23AB is a HP M209dw") == ["airscan:w10:HP:5E0A:23AB"]


def test_multi_device_lines():
    """多设备多行必须全部提取（连多台设备场景）。"""
    t = ("device `hp:libusb:001:001' is a HP A\n"
         "device 'epson:libusb:001:002' is a Epson B\n"
         "device `canon:libusb:001:003' is a Canon C")
    assert _names(t) == ["hp:libusb:001:001", "epson:libusb:001:002", "canon:libusb:001:003"]


def test_probe_uses_compat_regex(monkeypatch):
    """device_probe.probe() 整链路用兼容正则解析三种风格。"""
    class R:
        def __init__(self, out):
            self.stdout = out
            self.stderr = ""
    fake_output = ("device `hpljm1005:libusb:001:003' is a HP M1005\n"
                    "device 'epkowa:net:1.2.3.4' is a Epson\n"
                    "device pixma:04A917F2_076965 is a Canon\n")
    monkeypatch.setattr(device_probe, "SCANIMAGE", "/fake/scanimage")
    monkeypatch.setattr(device_probe.os.path, "exists", lambda p: True)
    monkeypatch.setattr(device_probe.subprocess, "run",
                        lambda cmd, **kw: R(fake_output) if "-L" in cmd else R("    --mode Gray|Color [Gray]\n"))
    device_probe._cache["data"] = None
    device_probe._cache["ts"] = 0.0
    devs, _ = device_probe.probe()
    names = [d["name"] for d in devs]
    assert "hpljm1005:libusb:001:003" in names, "203 反引号设备不能回归"
    assert "epkowa:net:1.2.3.4" in names, "单引号设备必须能探测到（P0-2）"
    assert "pixma:04A917F2_076965" in names, "无引号设备必须能探测到（P0-2）"
    device_probe._cache["data"] = None
    device_probe._cache["ts"] = 0.0


def test_203_real_a_output_parses():
    """203 真机 -A 输出能力解析回归（modes/dpi_raw）。"""
    mm = re.search(r"--mode\s+([^\[]+)\[", REAL_203_A)
    assert [s.strip() for s in mm.group(1).split("|")] == ["Gray", "Color"]
    mr = re.search(r"--resolution\s+([^\[]+)\[", REAL_203_A)
    dpi_raw = [d.strip().replace("dpi", "") for d in mr.group(1).split("|")]
    assert dpi_raw == ["75", "100", "150", "200", "300", "600", "1200"]
