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
    jobs._job_locks.clear()
    jobs._cleanup_last["ts"] = 0.0   # v1.14.8（F）：重置 cleanup 节流，防跨用例串扰
