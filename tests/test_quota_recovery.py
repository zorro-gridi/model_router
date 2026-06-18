"""
test_quota_recovery.py — minimax 配额恢复监控 单元测试
========================================================

覆盖：
  A. _quota_recovery_check() 双窗口联合检测
     - 双窗口均可用（interval≠2 AND weekly≠2）→ minimax 可路由
     - 上次不可路由 → 本次可路由 → 触发恢复清除
     - 仅一个窗口恢复（另一个仍耗尽）→ 不触发
     - 首次启动只记录基线，不触发
     - API 失败 → 静默跳过（fail-safe）
     - 无 API key → 跳过
     - status=3(unused) 不阻塞路由
  B. _is_minimax_sticky() 判断
     - JSON minimax sticky → True
     - JSON deepseek sticky → False
     - v2 纯文本 minimax → True
     - 损坏文件 → False
     - grace period 内 → False
  C. _clear_all_minimax_stickies() 扫描清理
     - 清除所有 session 的 minimax sticky
     - 不清除 deepseek sticky
     - grace period 保护
     - state_index.json 扫描
  D. get_quota_status() / clear_quota_state() 对外 API
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# 把 model_router/ 加到 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─────────────────────────────────────────────────────────────────────
# 测试辅助：构造 minimax token plan API 响应
# ─────────────────────────────────────────────────────────────────────
def _make_api_response(
    weekly_end_time: int | None = None,
    weekly_status: int = 1,
    interval_status: int = 1,
    weekly_remains_time: int = 18000 * 1000,  # 5h in ms
) -> dict:
    """构造 minimax /v1/token_plan/remains API 响应的 JSON dict。"""
    return {
        "model_remains": [
            {
                "model_name": "general",
                "current_interval_status": interval_status,
                "current_interval_remaining_percent": 50 if interval_status == 1 else 0,
                "current_weekly_status": weekly_status,
                "current_weekly_remaining_percent": 50 if weekly_status == 1 else 0,
                "weekly_end_time": weekly_end_time or int((time.time() + 86400) * 1000),
                "weekly_remains_time": weekly_remains_time,
                "weekly_boost_permille": 1000,
            },
            {
                "model_name": "video",
                "current_interval_status": 1,
                "current_interval_remaining_percent": 80,
                "current_weekly_status": 1,
                "current_weekly_remaining_percent": 80,
                "weekly_end_time": int((time.time() + 86400) * 1000),
                "weekly_remains_time": 5000000,
                "weekly_boost_permille": 1000,
            },
        ],
        "base_resp": {"status_code": 0, "status_msg": "ok"},
    }


# 固定一个"旧窗口"时间用于测试（2026-06-11 的某个 ms unix）
OLD_WEEKLY_END_TIME = 1781241600000

# ─────────────────────────────────────────────────────────────────────
# A. _quota_recovery_check() 双重触发 + 频率控制
# ─────────────────────────────────────────────────────────────────────
class QuotaRecoveryCheckTest(unittest.TestCase):
    """_quota_recovery_check() 双窗口联合检测（mock API 调用）。

    核心规则：minimax 可路由 ⇔ interval_status≠2 AND weekly_status≠2
    恢复触发 = 上次不可路由 → 本次可路由
    """

    def setUp(self):
        from health_checker import clear_quota_state as _clear
        _clear()

    def _patch_api(self, response: dict | None, fail_with: Exception | None = None):
        """构造 mock urlopen context manager。"""
        if fail_with:
            return patch(
                "health_checker.urllib.request.urlopen",
                side_effect=fail_with,
            )
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response).encode()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        return patch("health_checker.urllib.request.urlopen", return_value=mock_resp)

    def test_weekly_end_time_changed_triggers_recovery(self):
        """周窗口重置（end_time 变化）+ 双窗口均可用 → 触发恢复。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        # 基线：5h 正常、周窗口耗尽（不可路由）
        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = OLD_WEEKLY_END_TIME
            _QUOTA_STATE["last_weekly_status"] = 2
            _QUOTA_STATE["last_interval_status"] = 1
            _QUOTA_STATE["last_check_ts"] = 0.0

        new_end_time = OLD_WEEKLY_END_TIME + 604800000  # +7 天
        response = _make_api_response(
            weekly_end_time=new_end_time, weekly_status=1, interval_status=1,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies", return_value=3) as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_called_once()

    def test_weekly_status_recovered_triggers_recovery(self):
        """周窗口 2→1（5h 本就正常）→ 从不完全可用变为完全可用 → 触发恢复。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        same_end_time = OLD_WEEKLY_END_TIME
        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = same_end_time
            _QUOTA_STATE["last_weekly_status"] = 2   # 周窗口耗尽
            _QUOTA_STATE["last_interval_status"] = 1  # 5h 正常
            _QUOTA_STATE["last_check_ts"] = 0.0

        response = _make_api_response(
            weekly_end_time=same_end_time, weekly_status=1, interval_status=1,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies", return_value=2) as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_called_once()

    def test_interval_status_recovered_triggers_recovery(self):
        """5h 窗口 2→1（周本就正常）→ 触发恢复。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        same_end_time = OLD_WEEKLY_END_TIME
        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = same_end_time
            _QUOTA_STATE["last_weekly_status"] = 1   # 周正常
            _QUOTA_STATE["last_interval_status"] = 2  # 5h 耗尽
            _QUOTA_STATE["last_check_ts"] = 0.0

        response = _make_api_response(
            weekly_end_time=same_end_time, weekly_status=1, interval_status=1,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies", return_value=1) as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_called_once()

    def test_both_windows_recovered_triggers_recovery(self):
        """双窗口同时从耗尽恢复（interval 2→1 + weekly 2→1）→ 触发恢复。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = OLD_WEEKLY_END_TIME
            _QUOTA_STATE["last_weekly_status"] = 2
            _QUOTA_STATE["last_interval_status"] = 2  # 双窗口均耗尽
            _QUOTA_STATE["last_check_ts"] = 0.0

        response = _make_api_response(
            weekly_end_time=OLD_WEEKLY_END_TIME, weekly_status=1, interval_status=1,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies", return_value=5) as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_called_once()

    def test_only_interval_recovered_no_trigger(self):
        """仅 5h 恢复，周窗口仍耗尽 → 不可路由 → 不触发。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = OLD_WEEKLY_END_TIME
            _QUOTA_STATE["last_weekly_status"] = 2   # 周仍耗尽
            _QUOTA_STATE["last_interval_status"] = 2  # 5h 耗尽
            _QUOTA_STATE["last_check_ts"] = 0.0

        # 仅 5h 恢复，weekly 仍耗尽
        response = _make_api_response(
            weekly_end_time=OLD_WEEKLY_END_TIME, weekly_status=2, interval_status=1,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()

    def test_only_weekly_recovered_no_trigger(self):
        """仅周窗口恢复，5h 仍耗尽 → 不可路由 → 不触发。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = OLD_WEEKLY_END_TIME
            _QUOTA_STATE["last_weekly_status"] = 2   # 周耗尽
            _QUOTA_STATE["last_interval_status"] = 2  # 5h 耗尽
            _QUOTA_STATE["last_check_ts"] = 0.0

        # 仅 weekly 恢复，5h 仍耗尽
        response = _make_api_response(
            weekly_end_time=OLD_WEEKLY_END_TIME, weekly_status=1, interval_status=2,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()

    def test_no_trigger_same_state(self):
        """双窗口状态都不变 → 不触发。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        same_end_time = OLD_WEEKLY_END_TIME
        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = same_end_time
            _QUOTA_STATE["last_weekly_status"] = 1
            _QUOTA_STATE["last_interval_status"] = 1
            _QUOTA_STATE["last_check_ts"] = 0.0

        response = _make_api_response(
            weekly_end_time=same_end_time, weekly_status=1, interval_status=1,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()

    def test_first_run_no_trigger(self):
        """首次启动（无历史基线）→ 仅记录基线，不触发。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        with _QUOTA_STATE_LOCK:
            self.assertIsNone(_QUOTA_STATE["last_weekly_end_time"])
            self.assertIsNone(_QUOTA_STATE["last_weekly_status"])
            self.assertIsNone(_QUOTA_STATE["last_interval_status"])

        response = _make_api_response()

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()
        with _QUOTA_STATE_LOCK:
            self.assertIsNotNone(_QUOTA_STATE["last_weekly_end_time"])
            self.assertEqual(_QUOTA_STATE["last_weekly_status"], 1)
            self.assertEqual(_QUOTA_STATE["last_interval_status"], 1)

    def test_first_run_already_exhausted_no_trigger(self):
        """首次启动且已耗尽（双窗口均为 2）→ 只记录基线，不触发。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        response = _make_api_response(weekly_status=2, interval_status=2)

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()
        with _QUOTA_STATE_LOCK:
            self.assertEqual(_QUOTA_STATE["last_weekly_status"], 2)
            self.assertEqual(_QUOTA_STATE["last_interval_status"], 2)

    def test_api_failure_silent_skip(self):
        """API 调用失败 → 静默跳过，不触发清除。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = OLD_WEEKLY_END_TIME
            _QUOTA_STATE["last_weekly_status"] = 2
            _QUOTA_STATE["last_interval_status"] = 1
            _QUOTA_STATE["last_check_ts"] = 0.0

        with self._patch_api(None, fail_with=OSError("network unreachable")), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()
        with _QUOTA_STATE_LOCK:
            self.assertEqual(_QUOTA_STATE["last_weekly_end_time"], OLD_WEEKLY_END_TIME)
            self.assertEqual(_QUOTA_STATE["last_weekly_status"], 2)

    def test_no_api_key_skipped(self):
        """无 MINIMAX_API_KEY → 跳过检查。"""
        from health_checker import _quota_recovery_check

        with patch.dict(os.environ, {}, clear=True), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            os.environ.pop("MINIMAX_API_KEY", None)
            _quota_recovery_check()

        mock_clear.assert_not_called()

    def test_quota_check_interval_throttle(self):
        """QUOTA_CHECK_INTERVAL 内不重复调 API。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_check_ts"] = time.time() - 5

        response = _make_api_response()
        with self._patch_api(response) as mock_urlopen, \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}):
            _quota_recovery_check()

        mock_urlopen.assert_not_called()

    def test_disabled_by_env(self):
        """STAGE_ROUTER_QUOTA_RECOVERY_ENABLED=false → 跳过检查。"""
        from health_checker import _quota_recovery_check

        with patch.dict(os.environ, {"STAGE_ROUTER_QUOTA_RECOVERY_ENABLED": "false"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()

    def test_weekly_status_3_to_1_no_recovery(self):
        """每周 status 从 3(unused) → 1：本就非耗尽（status≠2），不触发恢复。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = OLD_WEEKLY_END_TIME
            _QUOTA_STATE["last_weekly_status"] = 3   # unused
            _QUOTA_STATE["last_interval_status"] = 1  # 5h 正常
            _QUOTA_STATE["last_check_ts"] = 0.0

        # 3→1：之前 (interval=1,weekly=3) → routable (neither is 2)
        # 现在 (interval=1,weekly=1) → routable
        # was_routable=True → 不触发
        response = _make_api_response(
            weekly_end_time=OLD_WEEKLY_END_TIME, weekly_status=1, interval_status=1,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()

    def test_interval_status_3_to_1_no_recovery(self):
        """5h status 从 3(unused) → 1：本就非耗尽，不触发恢复。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = OLD_WEEKLY_END_TIME
            _QUOTA_STATE["last_weekly_status"] = 1
            _QUOTA_STATE["last_interval_status"] = 3  # unused
            _QUOTA_STATE["last_check_ts"] = 0.0

        response = _make_api_response(
            weekly_end_time=OLD_WEEKLY_END_TIME, weekly_status=1, interval_status=1,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()

    def test_both_unchanged_exhausted_no_recovery(self):
        """双窗口均保持耗尽 → 不触发恢复。"""
        from health_checker import _quota_recovery_check, _QUOTA_STATE, _QUOTA_STATE_LOCK

        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = OLD_WEEKLY_END_TIME
            _QUOTA_STATE["last_weekly_status"] = 2
            _QUOTA_STATE["last_interval_status"] = 2
            _QUOTA_STATE["last_check_ts"] = 0.0

        response = _make_api_response(
            weekly_end_time=OLD_WEEKLY_END_TIME, weekly_status=2, interval_status=2,
        )

        with self._patch_api(response), \
             patch.dict(os.environ, {"MINIMAX_API_KEY": "sk-test"}), \
             patch("health_checker._clear_all_minimax_stickies") as mock_clear:
            _quota_recovery_check()

        mock_clear.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# B. _is_minimax_sticky() 单元测试
# ─────────────────────────────────────────────────────────────────────
class IsMinimaxStickyTest(unittest.TestCase):
    """_is_minimax_sticky() 各种格式/状态判断。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fb_path = self.root / "fallback_test-sid"
        self.fb_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_minimax_sticky_returns_true(self):
        payload = {
            "provider": "minimax",
            "failed_at": int(time.time()) - 3600,
            "expire_ts": int(time.time()) + 7200,
        }
        self.fb_path.write_text(json.dumps(payload), encoding="utf-8")
        from health_checker import _is_minimax_sticky
        self.assertTrue(_is_minimax_sticky(self.fb_path))

    def test_json_deepseek_sticky_returns_false(self):
        payload = {
            "provider": "deepseek",
            "failed_at": int(time.time()) - 3600,
            "expire_ts": int(time.time()) + 7200,
        }
        self.fb_path.write_text(json.dumps(payload), encoding="utf-8")
        from health_checker import _is_minimax_sticky
        self.assertFalse(_is_minimax_sticky(self.fb_path))

    def test_v2_text_minimax_returns_true(self):
        self.fb_path.write_text("minimax\n", encoding="utf-8")
        from health_checker import _is_minimax_sticky
        self.assertTrue(_is_minimax_sticky(self.fb_path))

    def test_v2_text_deepseek_returns_false(self):
        self.fb_path.write_text("deepseek\n", encoding="utf-8")
        from health_checker import _is_minimax_sticky
        self.assertFalse(_is_minimax_sticky(self.fb_path))

    def test_corrupt_json_returns_false(self):
        self.fb_path.write_text("{not valid json", encoding="utf-8")
        from health_checker import _is_minimax_sticky
        self.assertFalse(_is_minimax_sticky(self.fb_path))

    def test_empty_file_returns_false(self):
        self.fb_path.write_text("", encoding="utf-8")
        from health_checker import _is_minimax_sticky
        self.assertFalse(_is_minimax_sticky(self.fb_path))

    def test_grace_period_recent_sticky_returns_false(self):
        """failed_at 在 grace period 内 → 返回 False。"""
        payload = {
            "provider": "minimax",
            "failed_at": int(time.time()) - 5,  # 5s 前
            "expire_ts": int(time.time()) + 10000,
        }
        self.fb_path.write_text(json.dumps(payload), encoding="utf-8")
        from health_checker import _is_minimax_sticky
        self.assertFalse(_is_minimax_sticky(self.fb_path))

    def test_non_existent_file_returns_false(self):
        from health_checker import _is_minimax_sticky
        non_existent = self.root / "not_there"
        self.assertFalse(_is_minimax_sticky(non_existent))

    def test_unknown_text_provider_returns_false(self):
        self.fb_path.write_text("unknown_provider\n", encoding="utf-8")
        from health_checker import _is_minimax_sticky
        self.assertFalse(_is_minimax_sticky(self.fb_path))


# ─────────────────────────────────────────────────────────────────────
# C. _clear_all_minimax_stickies() 扫描清理
# ─────────────────────────────────────────────────────────────────────
class ClearAllMinimaxStickiesTest(unittest.TestCase):
    """_clear_all_minimax_stickies() 批量清除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 模拟多个 project/.claude/ 目录 + state_index
        self.project_a = self.root / "project_a"
        self.project_b = self.root / "project_b"
        self.project_c = self.root / "project_c"
        for proj in [self.project_a, self.project_b, self.project_c]:
            (proj / ".claude").mkdir(parents=True, exist_ok=True)

        self.old_ts = int(time.time()) - 3600

    def tearDown(self):
        self.tmp.cleanup()

    def _make_old_minimax_sticky(self, claude_dir: Path, sid: str) -> Path:
        fb = claude_dir / f"fallback_{sid}"
        payload = {
            "provider": "minimax",
            "failed_at": self.old_ts,
            "expire_ts": int(time.time()) + 10000,
        }
        fb.write_text(json.dumps(payload), encoding="utf-8")
        return fb

    def _make_old_deepseek_sticky(self, claude_dir: Path, sid: str) -> Path:
        fb = claude_dir / f"fallback_{sid}"
        payload = {
            "provider": "deepseek",
            "failed_at": self.old_ts,
            "expire_ts": int(time.time()) + 10000,
        }
        fb.write_text(json.dumps(payload), encoding="utf-8")
        return fb

    def _make_recent_minimax_sticky(self, claude_dir: Path, sid: str) -> Path:
        fb = claude_dir / f"fallback_{sid}"
        payload = {
            "provider": "minimax",
            "failed_at": int(time.time()) - 5,  # 5s 前，grace period 内
            "expire_ts": int(time.time()) + 10000,
        }
        fb.write_text(json.dumps(payload), encoding="utf-8")
        return fb

    def test_clears_all_minimax_stickies_across_sessions(self):
        """state_index.json 中多个 session 的 minimax sticky → 全部清除。"""
        # 每个 session 在独立的 project_root 下（匹配 state_index 的单 session 映射）
        fb1 = self._make_old_minimax_sticky(self.project_a / ".claude", "sid-a")
        fb2 = self._make_old_minimax_sticky(self.project_b / ".claude", "sid-b")
        fb3 = self._make_old_minimax_sticky(self.project_c / ".claude", "sid-c")

        # 构造 state_index.json：key=project_root, value={session_id, ...}
        state_index = {
            str(self.project_a): {"session_id": "sid-a", "stage": "dev"},
            str(self.project_b): {"session_id": "sid-b", "stage": "dev"},
            str(self.project_c): {"session_id": "sid-c", "stage": "dev"},
        }
        sidx_path = self.root / "state_index.json"
        sidx_path.write_text(json.dumps(state_index), encoding="utf-8")

        with patch("health_checker.HOOK_DIR", self.root):
            from health_checker import _clear_all_minimax_stickies
            cleared = _clear_all_minimax_stickies()

        # 所有 3 个 minimax sticky 应被清除
        self.assertFalse(fb1.exists())
        self.assertFalse(fb2.exists())
        self.assertFalse(fb3.exists())
        self.assertEqual(cleared, 3)

    def test_preserves_deepseek_stickies(self):
        """不清除 deepseek 等非 minimax 的 sticky。"""
        # 不同 project_root 各一个 session
        fb_mini = self._make_old_minimax_sticky(self.project_a / ".claude", "sid-a")
        fb_ds = self._make_old_deepseek_sticky(self.project_b / ".claude", "sid-b")

        state_index = {
            str(self.project_a): {"session_id": "sid-a", "stage": "dev"},
            str(self.project_b): {"session_id": "sid-b", "stage": "dev"},
        }
        sidx_path = self.root / "state_index.json"
        sidx_path.write_text(json.dumps(state_index), encoding="utf-8")

        with patch("health_checker.HOOK_DIR", self.root):
            from health_checker import _clear_all_minimax_stickies
            cleared = _clear_all_minimax_stickies()

        self.assertFalse(fb_mini.exists(), "minimax sticky 应被清除")
        self.assertTrue(fb_ds.exists(), "deepseek sticky 应保留")
        self.assertEqual(cleared, 1)

    def test_recent_sticky_preserved(self):
        """grace period 内的 minimax sticky → 保留。"""
        # 不同 project_root 各一个 session
        fb_old = self._make_old_minimax_sticky(self.project_a / ".claude", "sid-a")
        fb_recent = self._make_recent_minimax_sticky(self.project_b / ".claude", "sid-b")

        state_index = {
            str(self.project_a): {"session_id": "sid-a", "stage": "dev"},
            str(self.project_b): {"session_id": "sid-b", "stage": "dev"},
        }
        sidx_path = self.root / "state_index.json"
        sidx_path.write_text(json.dumps(state_index), encoding="utf-8")

        with patch("health_checker.HOOK_DIR", self.root):
            from health_checker import _clear_all_minimax_stickies
            cleared = _clear_all_minimax_stickies()

        self.assertFalse(fb_old.exists(), "旧 minimax sticky 应被清除")
        self.assertTrue(fb_recent.exists(), "grace period 内的 minimax sticky 应保留")
        self.assertEqual(cleared, 1)

    def test_no_state_index_returns_zero(self):
        """state_index.json 不存在 → 返回 0（无兜底扫描路径时）。"""
        with patch("health_checker.HOOK_DIR", self.root):
            from health_checker import _clear_all_minimax_stickies
            cleared = _clear_all_minimax_stickies()

        self.assertEqual(cleared, 0)

    def test_invalid_state_index_entry_skipped(self):
        """state_index.json 中无效条目 → 跳过。"""
        state_index = {
            "bad/key": "not_a_dict",
            "no/sid": {"stage": "dev"},  # 缺 session_id
        }
        sidx_path = self.root / "state_index.json"
        sidx_path.write_text(json.dumps(state_index), encoding="utf-8")

        with patch("health_checker.HOOK_DIR", self.root):
            from health_checker import _clear_all_minimax_stickies
            cleared = _clear_all_minimax_stickies()

        self.assertEqual(cleared, 0)


# ─────────────────────────────────────────────────────────────────────
# D. 对外 API 测试
# ─────────────────────────────────────────────────────────────────────
class QuotaStateAPITest(unittest.TestCase):
    """get_quota_status() / clear_quota_state() 对外接口。"""

    def setUp(self):
        from health_checker import clear_quota_state as _clear
        _clear()

    def test_get_quota_status_returns_defaults(self):
        from health_checker import get_quota_status
        status = get_quota_status()
        self.assertIsNone(status["last_weekly_end_time"])
        self.assertIsNone(status["last_weekly_status"])
        self.assertEqual(status["last_check_ts"], 0.0)

    def test_clear_quota_state_resets_all(self):
        from health_checker import (
            clear_quota_state, get_quota_status, _QUOTA_STATE, _QUOTA_STATE_LOCK,
        )
        with _QUOTA_STATE_LOCK:
            _QUOTA_STATE["last_weekly_end_time"] = 1234567890
            _QUOTA_STATE["last_weekly_status"] = 2
            _QUOTA_STATE["last_check_ts"] = time.time()

        clear_quota_state()
        status = get_quota_status()
        self.assertIsNone(status["last_weekly_end_time"])
        self.assertIsNone(status["last_weekly_status"])
        self.assertEqual(status["last_check_ts"], 0.0)

    def test_get_quota_status_returns_copy(self):
        """get_quota_status 返回的是 shallow copy，修改不影响内部状态。"""
        from health_checker import get_quota_status

        status1 = get_quota_status()
        status1["last_weekly_end_time"] = 99999

        status2 = get_quota_status()
        self.assertNotEqual(status2["last_weekly_end_time"], 99999)


if __name__ == "__main__":
    unittest.main()
