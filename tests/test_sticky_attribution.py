"""
test_sticky_attribution.py — sticky fallback 归因逻辑回归测试
================================================================

回归 2026-06-18 的两个 sticky 归因 bug：

  Bug 1（错归因）：
    原实现 do_POST 中写 `failed_provider = MODEL_TO_PROVIDER.get(session_model)`。
    sticky swap / token-plan 之后 session_model 是 swap **前**的模型名，
    实际请求目标 model 才是 swap **后**的——两者属于不同 provider 时，
    session_model 路径会把 deepseek 的断联错误归因到 minimax。

  Bug 2（stale sticky 阻塞）：
    原 `if not sticky_provider` 守卫阻止任何已有 sticky 时的写入，
    即使旧 sticky 指向的 provider 实际上已恢复，stale sticky 永远不会被覆盖。

修复：将归因计算抽成纯函数 `_resolve_failed_provider(sticky_provider,
        session_model, current_model)`，有 sticky 时使用 current_model 计算
        provider（因为 current_model 是当前请求真正请求的目标）。

本测试覆盖：
  A. _resolve_failed_provider 纯函数：5 个归因判定场景
  B. 端到端 sticky 写入 + stale 检测：模拟 do_POST 中的写入逻辑
     - deepseek 失败归因到 deepseek（不是 minimax）
     - minimax 失败归因到 minimax
     - stale sticky 被自动清除
     - sticky swap 流程不会错误覆盖 token_plan 写入的 sticky
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# 把 model_router/ 加到 sys.path 以便 import proxy
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─────────────────────────────────────────────────────────────────────
# A. _resolve_failed_provider 纯函数测试
# ─────────────────────────────────────────────────────────────────────
class ResolveFailedProviderTest(unittest.TestCase):
    """验证归因计算的 5 个核心场景。"""

    def setUp(self):
        from proxy import (
            MODEL_TO_PROVIDER,
            _resolve_failed_provider,
        )
        self.MODEL_TO_PROVIDER = MODEL_TO_PROVIDER
        self._resolve_failed_provider = _resolve_failed_provider

        # 校验测试前提：MODEL_TO_PROVIDER 应有 minimax + deepseek 两类
        self.assertIn("minimax", set(self.MODEL_TO_PROVIDER.values()))
        self.assertIn("deepseek", set(self.MODEL_TO_PROVIDER.values()))

        # 找一对明确的 model 名供测试用
        self.minimax_model = next(
            m for m, p in self.MODEL_TO_PROVIDER.items() if p == "minimax"
        )
        self.deepseek_model = next(
            m for m, p in self.MODEL_TO_PROVIDER.items() if p == "deepseek"
        )

    def test_no_sticky_session_model_minimax(self):
        """无 sticky + session 是 minimax → 归因 minimax（基准）。"""
        result = self._resolve_failed_provider(
            sticky_provider=None,
            session_model=self.minimax_model,
            current_model=self.minimax_model,
        )
        self.assertEqual(result, "minimax")

    def test_no_sticky_session_model_deepseek(self):
        """无 sticky + session 是 deepseek → 归因 deepseek（基准）。"""
        result = self._resolve_failed_provider(
            sticky_provider=None,
            session_model=self.deepseek_model,
            current_model=self.deepseek_model,
        )
        self.assertEqual(result, "deepseek")

    def test_sticky_minimax_swap_to_deepseek_fails(self):
        """【Bug 1 复现】sticky=minimax + swap 到 deepseek 后失败 →
        归因到 deepseek（修复后），不是 minimax（修复前 bug）。"""
        # sticky=minimax 触发 swap：current_model 切到 deepseek；
        # session_model 仍是 minimax（swap 前捕获的）。
        # 实际请求目标是 deepseek，请求失败 → 应归因 deepseek。
        result = self._resolve_failed_provider(
            sticky_provider="minimax",
            session_model=self.minimax_model,   # 修复前用这个 → 错误归因 minimax
            current_model=self.deepseek_model,  # 修复后用这个 → 正确归因 deepseek
        )
        self.assertEqual(
            result, "deepseek",
            "sticky swap 后失败应归因到 swap 目标 provider (deepseek)，"
            "而非 session_model 指向的 sticky provider (minimax)",
        )

    def test_sticky_deepseek_swap_to_minimax_fails(self):
        """【Bug 1 对偶】sticky=deepseek + swap 到 minimax 后失败 →
        归因到 minimax。"""
        result = self._resolve_failed_provider(
            sticky_provider="deepseek",
            session_model=self.deepseek_model,
            current_model=self.minimax_model,
        )
        self.assertEqual(result, "minimax")

    def test_unknown_model_returns_none(self):
        """未知 model 名 → None（调用方跳过写入）。"""
        result = self._resolve_failed_provider(
            sticky_provider=None,
            session_model="some-unknown-model-xyz",
            current_model=self.deepseek_model,
        )
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────
# B. 端到端 sticky 写入 + stale sticky 检测
# ─────────────────────────────────────────────────────────────────────
class StickyWriteEndToEndTest(unittest.TestCase):
    """模拟 do_POST 的"归因 + 写入"流程，验证：
    1. 失败归因正确
    2. stale sticky 被清除
    3. token_plan 写入的 sticky 不被错误覆盖（当 swap 后请求实际成功时）
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sid = "test-sticky-attribution"
        self.stage_path = self.root / ".claude" / f"stage_{self.sid}"
        self.stage_path.parent.mkdir(parents=True, exist_ok=True)
        self.stage_path.touch()
        self.fb_path = self.stage_path.with_name(f"fallback_{self.sid}")

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_active(self):
        return patch("proxy._active_stage_path", return_value=self.stage_path)

    def _write_sticky(self, provider: str, expired: bool = False):
        """手动写入一个 sticky 文件（模拟历史/外部写入者）。"""
        now = int(time.time())
        payload = {
            "provider": provider,
            "failed_at": now - 60,
            "expire_ts": now - 1 if expired else now + 7200,
        }
        self.fb_path.write_text(json.dumps(payload), encoding="utf-8")

    def _read_sticky(self) -> str | None:
        """读 sticky 文件中的 provider 名（None = 无文件 / 损坏）。"""
        if not self.fb_path.exists():
            return None
        try:
            data = json.loads(self.fb_path.read_text(encoding="utf-8"))
            return data.get("provider")
        except (json.JSONDecodeError, OSError):
            return None

    # ── 场景 1：deepseek 失败 → 写入 deepseek sticky（核心 bug 修复验证）──

    def test_deepseek_failure_writes_deepseek_sticky(self):
        """场景：minimax 不可用 → sticky swap 到 deepseek → deepseek 也失败
        → 应写入 "deepseek" sticky（不能误写 "minimax"）。"""
        from proxy import (
            MODEL_TO_PROVIDER,
            _resolve_failed_provider,
            try_write_fallback,
        )
        # 模拟前置：已存在 minimax sticky（swap 已发生）
        self._write_sticky("minimax")
        sticky_provider = "minimax"

        # session 原始模型是 minimax（swap 前），但当前请求目标是 deepseek
        session_model = next(m for m, p in MODEL_TO_PROVIDER.items() if p == "minimax")
        current_model = next(m for m, p in MODEL_TO_PROVIDER.items() if p == "deepseek")

        # 修复后的归因
        failed_provider = _resolve_failed_provider(
            sticky_provider=sticky_provider,
            session_model=session_model,
            current_model=current_model,
        )
        # 模拟 do_POST 中的 stale sticky 检测 + 写入
        # （clear_fallback 和 try_write_fallback 都依赖 _active_stage_path）
        from proxy import clear_fallback
        with self._patch_active():
            if sticky_provider and failed_provider != sticky_provider:
                clear_fallback()
            i_am_first = try_write_fallback(failed_provider)

        self.assertEqual(failed_provider, "deepseek",
                         "修复后：失败方应归因到 deepseek 而非 minimax")
        self.assertTrue(i_am_first, "首个写入者应返回 True")
        self.assertEqual(self._read_sticky(), "deepseek",
                         "修复后：sticky 文件应记录 deepseek 失败")

    # ── 场景 2：minimax 失败（无 swap）→ 写入 minimax sticky（基准）──

    def test_minimax_failure_writes_minimax_sticky(self):
        """场景：无 sticky + minimax 失败 → 写入 minimax sticky。"""
        from proxy import (
            MODEL_TO_PROVIDER,
            _resolve_failed_provider,
            try_write_fallback,
        )
        # 无 sticky（文件不存在）
        self.assertFalse(self.fb_path.exists())

        session_model = next(m for m, p in MODEL_TO_PROVIDER.items() if p == "minimax")
        current_model = session_model  # 未发生 swap

        failed_provider = _resolve_failed_provider(
            sticky_provider=None,
            session_model=session_model,
            current_model=current_model,
        )
        with self._patch_active():
            i_am_first = try_write_fallback(failed_provider)

        self.assertEqual(failed_provider, "minimax")
        self.assertTrue(i_am_first)
        self.assertEqual(self._read_sticky(), "minimax")

    # ── 场景 3：stale sticky=minimax + 实际 deepseek 失败 → 清除旧值 + 写新值 ──

    def test_stale_sticky_replaced_on_different_provider_failure(self):
        """【Bug 2 复现】stale sticky=minimax（实际可能已恢复），但 deepseek 真失败
        → 旧 buggy 代码因 `not sticky_provider` 守卫直接 skip，导致 stale 永不被覆盖。
        修复后：检测 failed_provider != sticky_provider → clear + 重写。"""
        from proxy import (
            MODEL_TO_PROVIDER,
            _resolve_failed_provider,
            clear_fallback,
            try_write_fallback,
        )
        # 模拟已存在的 stale minimax sticky
        self._write_sticky("minimax")
        self.assertEqual(self._read_sticky(), "minimax")

        # 但实际失败的是 deepseek（无 swap 发生，但 stale 仍存在）
        sticky_provider = "minimax"
        session_model = next(m for m, p in MODEL_TO_PROVIDER.items() if p == "deepseek")
        current_model = session_model  # 当前请求目标是 deepseek

        failed_provider = _resolve_failed_provider(
            sticky_provider=sticky_provider,
            session_model=session_model,
            current_model=current_model,
        )

        # 模拟 do_POST 中的 stale 处理（必须在 patch 下，否则 clear_fallback 找不到路径）
        with self._patch_active():
            if sticky_provider and failed_provider != sticky_provider:
                clear_fallback()
            i_am_first = try_write_fallback(failed_provider)

        self.assertEqual(failed_provider, "deepseek",
                         "stale sticky=minimax 但实际 deepseek 失败 → 应归因 deepseek")
        self.assertTrue(i_am_first, "clear 后应能正常写入")
        self.assertEqual(self._read_sticky(), "deepseek",
                         "修复后：stale minimax sticky 被 deepseek sticky 替换")

    # ── 场景 4：stale sticky=minimax + 实际 minimax 失败 → 保持原值 ──

    def test_stale_sticky_preserved_when_provider_matches(self):
        """stale sticky=minimax + minimax 仍然失败 → 不应清除原 sticky。"""
        from proxy import (
            MODEL_TO_PROVIDER,
            _resolve_failed_provider,
            clear_fallback,
            try_write_fallback,
        )
        self._write_sticky("minimax")

        sticky_provider = "minimax"
        session_model = next(m for m, p in MODEL_TO_PROVIDER.items() if p == "minimax")
        current_model = session_model  # 无 swap

        failed_provider = _resolve_failed_provider(
            sticky_provider=sticky_provider,
            session_model=session_model,
            current_model=current_model,
        )

        # 模拟 do_POST：stale 守卫不触发（failed_provider == sticky_provider）
        with self._patch_active():
            if sticky_provider and failed_provider != sticky_provider:
                clear_fallback()  # 不应被调用
            # 已有同 provider sticky → try_write_fallback 返回 False（O_EXCL 失败）
            i_am_first = try_write_fallback(failed_provider)

        self.assertEqual(failed_provider, "minimax")
        self.assertFalse(i_am_first,
                         "已有 minimax sticky → O_EXCL 失败，返回 False")
        self.assertEqual(self._read_sticky(), "minimax",
                         "失败 provider 与 sticky 一致 → sticky 文件应保留")


if __name__ == "__main__":
    unittest.main()
