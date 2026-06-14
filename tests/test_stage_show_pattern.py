#!/usr/bin/env python3
"""
test_stage_show_pattern.py — stage_show.py 第三行 Task Pattern 渲染
==================================================================

复现并锁定 bug：原本直接打印 `pattern_data["prediction"]` 原文，
导致 statusline 显示 "test -> test" 这种 A->A 噪声（配置其实有
"测试建设" label，但 stage_show 没查表）。

测试用 subprocess 跑 stage_show.py 的 main()，
通过临时文件隔离 project_root + sid，确保不污染宿主 state。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
HOOK_DIR = THIS_DIR.parent
SCRIPT = HOOK_DIR / "stage_show.py"


def _run_stage_show(cwd: str, sid: str, pattern_payload: dict) -> str:
    """在隔离的 cwd + sid 下跑 stage_show.py，喂入 pattern JSON，返回 stderr 文本。"""
    # 写 pattern_<sid> 文件
    project_root = Path(cwd)
    (project_root / ".claude").mkdir(parents=True, exist_ok=True)
    pattern_file = project_root / ".claude" / f"pattern_{sid}"
    pattern_file.write_text(json.dumps(pattern_payload))

    # active_session 指针 → 指向我们自己的 stage_<sid> 路径
    # stage_<sid> 文件本身不需要（event 里给了 session_id+cwd，会优先用）
    active_session = HOOK_DIR / "active_session"
    active_session.write_text(str(project_root / ".claude" / f"stage_{sid}"))

    event = {
        "session_id": sid,
        "cwd": cwd,
        "hook_event_name": "Stop",
    }
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "PYTHONPATH": str(HOOK_DIR)},
        )
        return result.stderr
    finally:
        # 清理临时文件
        try:
            pattern_file.unlink()
        except FileNotFoundError:
            pass
        try:
            active_session.unlink()
        except FileNotFoundError:
            pass


class TestStageShowPatternLabel(unittest.TestCase):
    """验证 statusline 第三行能展示中文 label 而非裸 key。"""

    def test_known_pattern_shows_chinese_label(self):
        """test pattern 应显示 '测试建设'，而不是 'test -> test'。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = _run_stage_show(
                cwd=tmp,
                sid="t-pattern-known",
                pattern_payload={"prediction": "test", "confidence": 0.7, "ts": "2026-06-14"},
            )
        # 关键断言：第三行应包含中文 label
        self.assertIn("测试建设", out,
                      f"期望包含中文 label '测试建设'，实际输出:\n{out}")
        # 不应再出现 'test -> test' 这种 A->A 噪声
        self.assertNotIn("test -> test", out)

    def test_known_pattern_label_differs_from_key(self):
        """当 label != key 时，应同时显示 label 和 key 便于调试。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = _run_stage_show(
                cwd=tmp,
                sid="t-pattern-debug",
                pattern_payload={"prediction": "bugfix", "confidence": 0.85, "ts": "2026-06-14"},
            )
        # bugfix → "缺陷修复"
        self.assertIn("缺陷修复", out)
        # 调试时仍能看到 key
        self.assertIn("key=bugfix", out)

    def test_unknown_pattern_falls_back_to_raw_key(self):
        """未在 PATTERN_CONFIG 中的自定义 pattern key 应保留原文，不崩。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = _run_stage_show(
                cwd=tmp,
                sid="t-pattern-unknown",
                pattern_payload={"prediction": "custom_pattern_xyz", "confidence": 0.5, "ts": "2026-06-14"},
            )
        # label == key 时不重复显示，但仍能看见 key
        self.assertIn("custom_pattern_xyz", out)
        # 不应崩出 Python traceback
        self.assertNotIn("Traceback", out)

    def test_no_pattern_file_omits_line(self):
        """完全没有 pattern 文件时，第三行应被完全省略。"""
        with tempfile.TemporaryDirectory() as tmp:
            # active_session 也不写 → 全 fallback 路径
            out = _run_stage_show(
                cwd=tmp,
                sid="t-pattern-absent",
                pattern_payload={},  # 空 → read_pattern 走 fallback 返回 None
            )
            # 把空 dict 写入并清掉（防止 fallback 命中上一个测试残留）
            project_root = Path(tmp)
            (project_root / ".claude").mkdir(parents=True, exist_ok=True)
            pf = project_root / ".claude" / "pattern_t-pattern-absent"
            if pf.exists():
                pf.unlink()
            out = subprocess.run(
                [sys.executable, str(HOOK_DIR / "stage_show.py")],
                input=json.dumps({"session_id": "t-pattern-absent", "cwd": tmp}),
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "PYTHONPATH": str(HOOK_DIR)},
            ).stderr
        # 不应有 "模式:" 这行
        self.assertNotIn("📐 模式:", out)


if __name__ == "__main__":
    unittest.main()
