"""
test_decision_lock.py — v1.3 Decision Lock 原语单测
=====================================================

V1.3 §4.5 / §6.4：一次决策，整段锁定 — 当前 prompt 的路由结果一旦确定，
在下次 prompt 之前不再变化。

`decision_lock` 是纯内存原语（per-process），不持文件锁，不跨进程。
文件级并发由 `state_persistence`（Stage 3）用 fcntl.flock 处理。

测试目标（TDD）：
  1. 未 lock 时 acquire_lock() 必须成功（首个 winner）
  2. 已 lock 时 acquire_lock() 必须短路返回 False
  3. force_unlock() 必须重置可再次获取
  4. 并发场景：同一 sid，多个线程同时抢锁 — 只有一个赢
  5. is_locked() 反映当前状态
  6. lock state 与具体 DecisionRecord 解耦 — 锁定只关心 sid + record 是否为 None
"""

import threading
import unittest
from unittest.mock import MagicMock

from decision_lock import DecisionLock


class TestSingleThreaded(unittest.TestCase):
    """单线程基本行为。"""

    def test_initially_unlocked(self):
        lock = DecisionLock()
        self.assertFalse(lock.is_locked("sid-1"))
        self.assertTrue(lock.try_acquire("sid-1", MagicMock()))

    def test_try_acquire_returns_true_when_unlocked(self):
        lock = DecisionLock()
        record = MagicMock()
        self.assertTrue(lock.try_acquire("sid-1", record))
        self.assertTrue(lock.is_locked("sid-1"))

    def test_try_acquire_returns_false_when_already_locked(self):
        lock = DecisionLock()
        first = MagicMock(name="first")
        second = MagicMock(name="second")
        self.assertTrue(lock.try_acquire("sid-1", first))
        # 已 lock → 必须短路
        self.assertFalse(lock.try_acquire("sid-1", second))

    def test_force_unlock_resets_state(self):
        lock = DecisionLock()
        lock.try_acquire("sid-1", MagicMock())
        lock.force_unlock("sid-1")
        self.assertFalse(lock.is_locked("sid-1"))
        # 重置后可重新获取
        self.assertTrue(lock.try_acquire("sid-1", MagicMock()))

    def test_different_sids_are_independent(self):
        lock = DecisionLock()
        self.assertTrue(lock.try_acquire("sid-1", MagicMock()))
        # sid-2 不应受 sid-1 锁定影响
        self.assertTrue(lock.try_acquire("sid-2", MagicMock()))
        self.assertTrue(lock.is_locked("sid-1"))
        self.assertTrue(lock.is_locked("sid-2"))

    def test_get_returns_stored_record(self):
        lock = DecisionLock()
        record = MagicMock(name="my-record")
        lock.try_acquire("sid-1", record)
        self.assertIs(lock.get("sid-1"), record)

    def test_get_returns_none_when_not_locked(self):
        lock = DecisionLock()
        self.assertIsNone(lock.get("sid-not-set"))


class TestConcurrency(unittest.TestCase):
    """并发场景：N 个线程同时抢锁，必须只有一个赢。"""

    def test_only_one_winner_under_concurrent_acquire(self):
        lock = DecisionLock()
        n_threads = 50
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker():
            ok = lock.try_acquire("sid-race", MagicMock())
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = sum(1 for r in results if r)
        losers = sum(1 for r in results if not r)
        self.assertEqual(winners, 1, f"expected exactly 1 winner, got {winners}")
        self.assertEqual(losers, n_threads - 1)
        self.assertTrue(lock.is_locked("sid-race"))

    def test_force_unlock_allows_retry(self):
        lock = DecisionLock()
        lock.try_acquire("sid-retry", MagicMock())
        lock.force_unlock("sid-retry")
        # 重新获取必须成功
        self.assertTrue(lock.try_acquire("sid-retry", MagicMock()))


if __name__ == "__main__":
    unittest.main()
