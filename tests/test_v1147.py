# tests/test_v1147.py — v1.14.7 P1 修复验证
# 目标：验证 scan_flatbed 在取得 job_lock + scan_lock 后，初始化阶段任意异常
# 都不会留下永久锁死；尤其覆盖 v1.14.6 新增 mkstemp 路径。
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs  # noqa: E402
import scanner  # noqa: E402


def _mk_job():
    return jobs.create("v1147")


def _assert_locks_released(name):
    assert not scanner.scan_lock.locked(), "P1：初始化异常后 scan_lock 必须释放"
    # v1.14.8（D）：条目回收断言移到新建引用之前——原断言在自己 release 后恒真，
    # 未验证「异常收尾后条目已被自动回收」（v1.14.5 refs 机制的自然效果）
    assert name not in jobs._job_locks, "P1：异常收尾后锁 registry 应已自动回收（refs 归零）"
    handle = jobs.job_lock(name)
    try:
        assert handle.acquire(False), "P1：初始化异常后 job_lock 必须立即可获取"
        handle.release()
    except Exception:
        try:
            handle.abandon()
        except Exception:
            pass
        raise
    assert name not in jobs._job_locks, "P1：断言后条目应再次自动回收"


def test_flatbed_mkstemp_exception_releases_both_locks(monkeypatch):
    name = _mk_job()

    def boom(*args, **kwargs):
        raise OSError("simulated mkstemp failure")

    monkeypatch.setattr(scanner.tempfile, "mkstemp", boom)

    try:
        scanner.scan_flatbed(name)
        assert False, "mkstemp 异常必须向调用方报告"
    except OSError as e:
        assert "simulated mkstemp failure" in str(e)

    assert scanner.state[name]["state"] == "error"
    _assert_locks_released(name)


def test_flatbed_next_page_exception_releases_both_locks(monkeypatch):
    name = _mk_job()

    def boom(_job, _root=None):   # v1.15（#1）：next_page_no 增 root 参数
        raise jobs.JobError("simulated missing task during numbering")

    monkeypatch.setattr(jobs, "next_page_no", boom)

    try:
        scanner.scan_flatbed(name)
        assert False, "next_page_no 异常必须向调用方报告"
    except jobs.JobError as e:
        assert "simulated missing task" in str(e)

    assert scanner.state[name]["state"] == "error"
    _assert_locks_released(name)


def test_flatbed_init_exception_allows_later_scan_lock_acquire(monkeypatch):
    name = _mk_job()
    monkeypatch.setattr(jobs, "next_page_no", lambda _job, _root=None: (_ for _ in ()).throw(OSError("disk error")))

    try:
        scanner.scan_flatbed(name)
    except OSError:
        pass

    acquired = scanner.scan_lock.acquire(False)
    assert acquired, "初始化异常后下一次扫描必须能重新取得 scan_lock"
    if acquired:
        scanner.scan_lock.release()
    _assert_locks_released(name)
