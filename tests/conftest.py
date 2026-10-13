# tests/conftest.py — v1.14.4（D）：测试隔离根治
# 背景：各测试模块曾在模块级自行设置 os.environ["SCAN_ROOT"]，但 config 只在首次
# import 时读环境变量——首个导入者决定全局，全量跑时所有模块共享同一目录（flaky
# 源头，v1.14.3 报告已记录）。根治：autouse fixture 每测试独立 SCAN_ROOT 与
# ADMIN_CFG_PATH，模块级 env 设置从此只作历史兼容、不再有实际作用。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
import jobs  # noqa: E402
import scanner  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    root = str(tmp_path / "scanroot")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(config, "SCAN_ROOT", root)
    # 管理配置也进沙箱——不再污染源码目录的 admin_config.json
    monkeypatch.setattr(config, "ADMIN_CFG_PATH", str(tmp_path / "admin_config.json"))
    yield
    scanner.state.clear()
    # v1.16.0-tmp02：清空转换队列水位与 pending（防跨用例串扰——常驻 worker 是模块级状态）
    scanner._reset_convert_for_test()
    # v1.16.0-tmp04：清 /tmp 测试残留 PNM——被 drain 的入队任务源文件仍留在磁盘，
    # 不清会污染后续 cleanup_tmp_pnms/归档类测试的计数
    import glob
    for f in glob.glob("/tmp/scanweb_*.pnm"):
        try:
            os.remove(f)
        except OSError:
            pass
    jobs._job_locks.clear()
    jobs._cleanup_last["ts"] = 0.0   # v1.14.8（F）：重置 cleanup 节流，防跨用例串扰
    import device_probe
    device_probe._cache.update(ts=0.0, data=None, ver=0, data_ver=-1)
    # v1.15（#2）：重置探测缓存版本号状态——旧用例手改 _cache 后不残留，防跨用例串扰
