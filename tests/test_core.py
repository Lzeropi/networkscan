# tests/test_core.py — v1.14 核心逻辑回归（pytest，无需真实扫描仪）
# 运行：在项目根目录执行 python -m pytest tests/ -v
import os
import sys
import tempfile

# 独立临时存储目录，避免污染真实扫描目录
_TEST_ROOT = tempfile.mkdtemp(prefix="scanweb_test_")
os.environ["SCAN_ROOT"] = _TEST_ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin  # noqa: E402
import config  # noqa: E402
import device_probe  # noqa: E402
import hashlib  # noqa: E402
import jobs  # noqa: E402
import re  # noqa: E402
import scanner  # noqa: E402


def _reset_root():
    import shutil
    for name in os.listdir(_TEST_ROOT):
        p = os.path.join(_TEST_ROOT, name)
        shutil.rmtree(p, ignore_errors=True)
    scanner.state.clear()


def _mk_job_with_pages():
    _reset_root()
    name = jobs.create()
    d = os.path.join(config.get_scan_root(), name)
    open(os.path.join(d, "p001.png"), "wb").write(b"\x89PNG fake data")  # 已完成页
    open(os.path.join(d, "p002.png"), "wb").close()                     # 0 字节转换中占位
    return name


# ---------- #4 任务 ID 同秒唯一 ----------
def test_job_id_unique_same_second():
    a = jobs.create("备注")
    b = jobs.create("备注")
    assert a != b, "同秒创建的两个任务名必须不同"
    # v1.14.2（#12）：后缀 4→6 位十六进制（24bit），碰撞概率再降 65536 倍
    assert re.fullmatch(r"\d{8}-\d{6}_[0-9a-f]{6}(_\S+)?", a), a


# ---------- #3 占位过滤与裸计数 ----------
def test_pages_filters_zero_byte():
    name = _mk_job_with_pages()
    assert jobs.pages(name) == ["p001.png"], "0 字节占位不能计入已完成页"
    assert jobs.raw_pages(name) == ["p001.png", "p002.png"], "裸计数必须包含占位"


def test_raw_pages_for_numbering():
    name = _mk_job_with_pages()
    assert len(jobs.raw_pages(name)) + 1 == 3, "扫描编号必须以 raw_pages 为准"


# ---------- 新1 锁定删除保护 ----------
def test_locked_job_delete_refused():
    name = _mk_job_with_pages()
    jobs.set_locked(name, True)
    try:
        jobs.delete(name)
        assert False, "锁定任务必须拒绝删除"
    except jobs.JobError:
        pass


# ---------- #2 cleanup 排除扫描中任务 ----------
def test_cleanup_skips_active_job():
    _reset_root()
    # 建 2 个任务，max_jobs=1 强制清理最旧；较新任务标记为扫描中
    old = jobs.create("较旧")
    new = jobs.create("较新")
    for name in (old, new):
        with open(os.path.join(config.get_scan_root(), name, "p001.png"), "wb") as f:
            f.write(b"\x89PNG data")
    cfg = config.load_admin_cfg()
    cfg["cleanup"]["max_jobs"] = 1
    config.save_admin_cfg(cfg)
    scanner.state[new] = {"state": "scanning", "msg": "扫描中"}
    deleted = jobs.cleanup()
    assert old in deleted, "超限的最旧任务应被清理"
    assert new not in deleted, "扫描中任务不能被清理"
    scanner.state.clear()
    # 剩 1 个任务未超 max_jobs=1，不应再删；active 保护解除后语义仍正确
    assert jobs.cleanup() == [], "未超限时不应清理"
    cfg["cleanup"]["max_jobs"] = 0
    config.save_admin_cfg(cfg)


# ---------- #15 设备解析缓存分键 ----------
def test_resolve_cache_keyed_by_backend(monkeypatch):
    calls = []

    class R:
        stdout = ("device `hpljm1005:libusb:001:003' is a HP\n"
                  "device `epkowa:net:192.168.1.5' is a Epson\n").encode()
        stderr = b""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return R()

    monkeypatch.setattr(scanner.subprocess, "run", fake_run)
    scanner._dev_cache = {}
    assert scanner._resolve_device("hpljm1005:") == "hpljm1005:libusb:001:003"
    assert scanner._resolve_device("epkowa:") == "epkowa:net:192.168.1.5"
    n_before = len(calls)
    # 第二轮全命中缓存，不再调 scanimage（且不串号）
    assert scanner._resolve_device("hpljm1005:") == "hpljm1005:libusb:001:003"
    assert scanner._resolve_device("epkowa:") == "epkowa:net:192.168.1.5"
    assert len(calls) == n_before, "缓存命中后不应再探测"
    scanner._dev_cache = {}


# ---------- #13 scan_root 黑名单 ----------
def test_root_blacklist():
    for p in ("/", "/etc", "/etc/foo", "/usr", "/usr/local/data",
              "/var/lib", "/opt/../etc", "/proc", "/sys"):
        assert admin._root_blocked(p), p
    for p in ("/opt/smb_share/scans", "/mnt/data", "/home/u/scans",
              "/srv", "/media/disk"):
        assert not admin._root_blocked(p), p


# ---------- #8 PIN PBKDF2 + 旧格式迁移 ----------
def test_pin_pbkdf2_and_legacy():
    legacy = hashlib.sha256(b"1234").hexdigest()
    assert admin._verify("1234", legacy)          # 旧格式通过
    assert not admin._verify("9999", legacy)      # 旧格式拒绝
    new = admin._hash("1234")
    assert "$" in new and admin._verify("1234", new)
    assert not admin._verify("9999", new)
    assert admin._hash("1234") != admin._hash("1234"), "每次加盐不同"


# ---------- #17 source 白名单（探测匹配逻辑）----------
def test_adf_source_whitelist():
    device_probe._cache["data"] = [{
        "name": "epkowa:net:1.2.3.4", "sources": ["ADF", "Flatbed"],
        "modes": ["Gray"], "dpi_raw": ["150"], "dpis": ["150"],
        "desc": "", "dpi": "", "scan_type": "", "error": ""}]
    device_probe._cache["ts"] = 1e18
    p = {"device": "epkowa:", "source_name": "EVIL_SOURCE"}
    cap = next((x for x in device_probe.probe()[0]
                if x["name"] == p["device"] or x["name"].startswith(p["device"])), None)
    assert cap and "EVIL_SOURCE" not in cap["sources"], "非法 source 必须不在设备支持列表"
    device_probe._cache["data"] = None
    device_probe._cache["ts"] = 0.0


# ---------- #10 版本一致性 ----------
def test_version_consistency():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    with open(os.path.join(os.path.dirname(config.__file__), "pyproject.toml"), "rb") as f:
        ver = tomllib.load(f)["project"]["version"]
    assert config.VERSION == "1.14.6"
    assert ver == config.VERSION, "pyproject 版本必须与 config.VERSION 一致"
    # v1.14.3（P2-8）+ v1.14.4（P3-12）+ v1.14.5（P3）：四版本一致断言——README「当前版本」字段曾漏改、
    # 发布包命名也曾跨版本残留，固化成测试防再犯
    src = os.path.dirname(config.__file__)
    readme = open(os.path.join(src, "README.md"), encoding="utf-8").read()
    assert "| 当前版本 | v%s |" % ver in readme, "README 当前版本字段与代码版本不一致"
    assert "scanweb-v%s.tar.gz" % ver in readme, "README 发布包命名与代码版本不一致"
    # v1.14.5（P3）：真正验实际发布包文件名——之前只查 README 字符串，README=v1.14.4 而实包
    # 是 v1.14.3 的打包事故仍会漏过。包内跑 pytest 无 tar 时静默跳过（不破坏包内测试）
    from pathlib import Path
    pkgs = list(Path(src).resolve().parent.parent.glob("scanweb-v*.tar.gz"))
    if pkgs and not any(p.name == "scanweb-v%s.tar.gz" % ver for p in pkgs):
        raise AssertionError("存在发布包但无 scanweb-v%s.tar.gz（实际包名与版本不一致）" % ver)
