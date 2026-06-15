"""
decision_lock.py — v1.3 Decision Lock 内存原语
================================================

V1.3 §4.5 / §6.4：一次决策，整段锁定 — 当前 prompt 的路由结果一旦确定，
在下次 prompt 之前不再变化。

本模块提供**纯进程内**的锁原语：
  - `DecisionLock` — per-sid 的"是否已锁定"映射 + 已锁定的 record 引用
  - 线程安全（threading.Lock 保护内部 dict）
  - **不**持文件锁、**不**做跨进程同步（那是 `state_persistence` 的职责）
  - **不**做 I/O

设计要点：
  - `try_acquire(sid, record) -> bool` — 原子 CAS 语义；返回 True 即为 winner
  - `force_unlock(sid)` — 测试/异常路径手动重置
  - `is_locked(sid) / get(sid)` — 查询

Stage 1 范围：纯计算，零 I/O。
Stage 2 会在此基础上接入状态机的 `transition()` 校验。
"""

import threading
from typing import Any, Optional


class DecisionLock:
    """per-sid 的决策锁定原语（纯内存，线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, Any] = {}

    def try_acquire(self, sid: str, record: Any) -> bool:
        """尝试为 sid 锁定 record。返回 True 即为 winner。"""
        with self._lock:
            if sid in self._records:
                return False
            self._records[sid] = record
            return True

    def is_locked(self, sid: str) -> bool:
        return sid in self._records

    def get(self, sid: str) -> Optional[Any]:
        return self._records.get(sid)

    def force_unlock(self, sid: str) -> None:
        """强制重置 sid 的锁定状态（测试 / 异常恢复用）。"""
        with self._lock:
            self._records.pop(sid, None)
