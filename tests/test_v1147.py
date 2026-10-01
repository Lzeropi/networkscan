# tests/test_v1147.py — v1.14.7 P1 修复验证（v1.51-tmp02 适配异步 scan_flatbed）
# 异步化后 scan_flatbed 返回 "started"（不抛异常），异常在 worker 内写 state=error
# 测试改为轮询 state 确认 error + 双锁释放
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs  # noqa: E402
import scanner  # noqa: E402


def _mk_job():
    return jobs.create("v1147")


def _wait_state(name, want=("done", "error"), timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = scanner.state.get(name, {})
        if st.get("state") in want:
            return st
        time.sleep(0.05)
    return {}


def _assert_locks_released(name):
    assert not scanner.scan_lock.locked(), "P1：异常后 scan_lock 必须释放"
    assert name not in jobs._job_locks, "P1：锁 registry 应已回收"
    handle = jobs.job_lock(name)
    try:
        assert handle.acquire(False), "P1：job_lock 必须立即可获取"
        handle.release()
    except Exception:
        handle.abandon()
        raise


def test_flatbed_mkstemp_exception_releases_both_locks(monkeypatch):
    name = _mk_job()
    def boom(*args, **kwargs):
        raise OSError("simulated mkstemp failure")
    monkeypatch.setattr(scanner.tempfile, "mkstemp", boom)

    r = scanner.scan_flatbed(name)
    assert r == "started", "异步扫描应返回 started"
    st = _wait_state(name)
    assert st.get("state") == "error", "mkstemp 异常应写 error，实际 %s" % st.get("state")
    _assert_locks_released(name)


def test_flatbed_next_page_exception_releases_both_locks(monkeypatch):
    name = _mk_job()
    def boom(_job, _root=None):
        raise jobs.JobError("simulated missing task during numbering")
    monkeypatch.setattr(jobs, "next_page_no", boom)

    r = scanner.scan_flatbed(name)
    assert r == "started"
    st = _wait_state(name)
    assert st.get("state") == "error", "next_page_no 异常应写 error"
    _assert_locks_released(name)


def test_flatbed_init_exception_allows_later_scan_lock_acquire(monkeypatch):
    name = _mk_job()
    monkeypatch.setattr(jobs, "next_page_no", lambda _job, _root=None: (_ for _ in ()).throw(OSError("disk error")))

    r = scanner.scan_flatbed(name)
    assert r == "started"
    _wait_state(name)
    acquired = scanner.scan_lock.acquire(False)
    assert acquired, "异常后 scan_lock 必须可重新获取"
    if acquired:
        scanner.scan_lock.release()
    _assert_locks_released(name)
