"""
test_sticky_recovery.py — sticky fallback TTL + 自动恢复端到端测试
==================================================================

E2E 验证 4 大场景：
  1. TTL 过期 → read_fallback 自动清理
  2. 并发原子写 → 仅 1 个 winner（10 线程 Barrier 同步）
  3. auto-recovery 闭环 → 探测成功 → 清除真实 sticky 文件
  4. leader election 跨进程 → 第二个进程非阻塞获取锁应失败

E2E 风格：
  - 用真实 tempfile 创建隔离 project_root（避免污染宿主 state）
  - proxy / health_checker 通过 sys.path 注入 hooks 目录
  - 跨进程测试用 subprocess 调独立 Python 脚本
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
HOOKS_DIR = THIS_DIR.parent.parent
HOOKS_DIR_STR = str(HOOKS_DIR)
sys.path.insert(0, HOOKS_DIR_STR)


def _setup():
    """建隔离 project_root（含 .claude/stage_<sid>）→ (root, sid, fb_path, stage_path)。"""
    root = tempfile.mkdtemp(prefix="mr-sticky-recovery-")
    root_path = Path(root)
    claude_dir = root_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    sid = f"e2e-sticky-{int(time.time())}-{os.getpid()}"
    stage_path = claude_dir / f"stage_{sid}"
    stage_path.touch()
    fb_path = claude_dir / f"fallback_{sid}"
    return root, sid, str(fb_path), str(stage_path)


# ─────────────────────────────────────────────────────────────────────
# E2E 场景 1: TTL 过期清理
# ─────────────────────────────────────────────────────────────────────
class TestTTLLifecycle(unittest.TestCase):
    """写一个 v3 JSON sticky（含 expire_ts），调 read_fallback：

    - 未到期 → 保留并返回 provider
    - 已到期 → unlink 并返回 None
    - expire_ts=0 → 视为无 TTL，不清理
    """

    def setUp(self):
        self.root, self.sid, self.fb_path, self.stage_path = _setup()
        from proxy import _active_stage_path, read_fallback
        # 把 _active_stage_path 指向我们临时 stage 文件
        self._patcher = patch(
            "proxy._active_stage_path", return_value=Path(self.stage_path)
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_expired_sticky_unlinked(self):
        from proxy import read_fallback
        Path(self.fb_path).write_text(json.dumps({
            "provider": "minimax",
            "failed_at": int(time.time()) - 10000,
            "expire_ts": int(time.time()) - 100,  # 已过期
        }), encoding="utf-8")
        self.assertIsNone(read_fallback())
        self.assertFalse(Path(self.fb_path).exists())

    def test_valid_sticky_preserved(self):
        from proxy import read_fallback
        Path(self.fb_path).write_text(json.dumps({
            "provider": "deepseek",
            "failed_at": int(time.time()) - 60,
            "expire_ts": int(time.time()) + 3600,
        }), encoding="utf-8")
        self.assertEqual(read_fallback(), "deepseek")
        self.assertTrue(Path(self.fb_path).exists())

    def test_zero_expire_ts_no_ttl(self):
        from proxy import read_fallback
        Path(self.fb_path).write_text(json.dumps({
            "provider": "minimax",
            "failed_at": int(time.time()) - 10000,
            "expire_ts": 0,  # 无 TTL
        }), encoding="utf-8")
        self.assertEqual(read_fallback(), "minimax")
        self.assertTrue(Path(self.fb_path).exists())


# ─────────────────────────────────────────────────────────────────────
# E2E 场景 2: 并发原子写
# ─────────────────────────────────────────────────────────────────────
class TestConcurrentAtomicWrite(unittest.TestCase):
    """20 个线程并发 try_write_fallback → 仅 1 个返回 True，文件内容一致。"""

    def setUp(self):
        self.root, self.sid, self.fb_path, self.stage_path = _setup()
        from proxy import _active_stage_path, try_write_fallback
        self._patcher = patch(
            "proxy._active_stage_path", return_value=Path(self.stage_path)
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_20_concurrent_writers_one_winner(self):
        from proxy import try_write_fallback
        n = 20
        barrier = threading.Barrier(n)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            ret = try_write_fallback("minimax")
            with results_lock:
                results.append(ret)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        true_count = sum(1 for r in results if r)
        self.assertEqual(true_count, 1, f"应仅 1 个 True，实际 {true_count}")
        # 文件存在且内容合法
        self.assertTrue(Path(self.fb_path).exists())
        data = json.loads(Path(self.fb_path).read_text(encoding="utf-8"))
        self.assertEqual(data["provider"], "minimax")
        self.assertIn("expire_ts", data)


# ─────────────────────────────────────────────────────────────────────
# E2E 场景 3: auto-recovery 闭环
# ─────────────────────────────────────────────────────────────────────
class TestAutoRecoveryLoop(unittest.TestCase):
    """模拟完整闭环：写 sticky → _run_probe_round(mock 探测成功) → sticky 被清。

    注意：state_index.json 不可写在 HOOK_DIR（影响其他实例），
    所以这里直接构造 _collect_probe_targets 内部用的元组并 mock。
    """

    def setUp(self):
        self.root, self.sid, self.fb_path, self.stage_path = _setup()
        Path(self.fb_path).write_text(json.dumps({
            "provider": "minimax",
            "failed_at": int(time.time()) - 10000,  # 远在 grace 之外
            "expire_ts": int(time.time()) + 100000,
        }), encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_recovery_clears_real_sticky(self):
        """探测成功 → 真实 sticky 文件被 unlink。"""
        target = ("minimax", Path(self.root), self.sid, time.time() - 100)
        with patch(
            "health_checker._collect_probe_targets", return_value=[target]
        ), patch(
            "health_checker._probe_provider", return_value=True
        ):
            from health_checker import _run_probe_round
            _run_probe_round()
        self.assertFalse(Path(self.fb_path).exists(), "探测成功 sticky 应被清除")

    def test_failed_probe_keeps_sticky(self):
        """探测失败 → sticky 保留。"""
        target = ("minimax", Path(self.root), self.sid, time.time() - 100)
        with patch(
            "health_checker._collect_probe_targets", return_value=[target]
        ), patch(
            "health_checker._probe_provider", return_value=False
        ):
            from health_checker import _run_probe_round
            _run_probe_round()
        self.assertTrue(Path(self.fb_path).exists(), "探测失败 sticky 应保留")

    def test_grace_period_protects_recent_sticky(self):
        """sticky 刚写入（< 30s）→ 即便探测成功也不清除。"""
        # 写一个"刚写"的 sticky（failed_at=5s 前）
        Path(self.fb_path).write_text(json.dumps({
            "provider": "minimax",
            "failed_at": int(time.time()) - 5,
            "expire_ts": int(time.time()) + 100000,
        }), encoding="utf-8")
        target = ("minimax", Path(self.root), self.sid, time.time() - 100)
        with patch(
            "health_checker._collect_probe_targets", return_value=[target]
        ), patch(
            "health_checker._probe_provider", return_value=True
        ):
            from health_checker import _run_probe_round
            _run_probe_round()
        self.assertTrue(
            Path(self.fb_path).exists(),
            "grace period 内 sticky 不应被清除",
        )


# ─────────────────────────────────────────────────────────────────────
# E2E 场景 4: leader election 跨进程
# ─────────────────────────────────────────────────────────────────────
HELD_LOCK_PROBE = r"""
import fcntl, os, sys, time
from pathlib import Path

# 指向健康检查 lock 路径
lock_path = Path(sys.argv[1])
hold_seconds = float(sys.argv[2])

fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("ACQUIRED", flush=True)
time.sleep(hold_seconds)
os.close(fd)
print("RELEASED", flush=True)
"""


class TestLeaderElectionCrossProcess(unittest.TestCase):
    """subprocess 持有健康检查 lock → 主进程 _try_acquire_leader_lock 应返回 False。

    注：必须在主进程起 health_checker 之前先抢锁，再 join 等待 subprocess 完成。
    干净起见，每个测试用独立的 lock_path（不污染真 HOOK_DIR/health_check.lock）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-leader-")
        self.lock_path = Path(self.tmp) / "health_check.lock"
        # 把 health_checker.HEALTH_LOCK_PATH 重定向到测试 lock
        import health_checker
        self._orig_lock_path = health_checker.HEALTH_LOCK_PATH
        health_checker.HEALTH_LOCK_PATH = self.lock_path
        # 也重置 _LEADER_FD（防止前一个测试残留）
        health_checker._LEADER_FD[0] = None

    def tearDown(self):
        import health_checker
        health_checker.HEALTH_LOCK_PATH = self._orig_lock_path
        health_checker._LEADER_FD[0] = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_main_process_blocked_while_subprocess_holds(self):
        """subprocess 持有锁 2s → 主进程非阻塞 flock 应失败。"""
        proc = subprocess.Popen(
            [sys.executable, "-c", HELD_LOCK_PROBE, str(self.lock_path), "2.0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # 等 subprocess 拿到锁
        line = proc.stdout.readline().strip()
        self.assertEqual(line, "ACQUIRED", f"subprocess 未拿到锁: {line!r}")

        # 主进程尝试非阻塞获取 → 应当失败
        from health_checker import _try_acquire_leader_lock
        self.assertFalse(_try_acquire_leader_lock(),
                         "subprocess 持有锁时主进程不应获取成功")

        # 等 subprocess 释放
        proc.wait(timeout=5)
        line = proc.stdout.readline().strip()
        self.assertEqual(line, "RELEASED")

        # 释放后主进程可获取
        self.assertTrue(_try_acquire_leader_lock())
        from health_checker import _release_leader_lock
        _release_leader_lock()


if __name__ == "__main__":
    # 因用了 patch，需要显式 import
    import unittest.mock  # noqa: F401
    unittest.main()
