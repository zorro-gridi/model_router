"""
post_tool_overhead.py — PostToolUse hook dispatch() 性能基线
==============================================================

Stage 8.2 性能验收：100 次 mock PostToolUse hook 调用，p99 < 5ms。

测量：
  - dispatch() 端到端延迟（包括 track + maybe_redecide）
  - 混合 7 种工具类型模拟真实 session
  - p50 / p95 / p99 / max 报告
  - p99 < 5ms → PASS，否则 FAIL

使用方式：
  python bench/post_tool_overhead.py
  python bench/post_tool_overhead.py --runs 500   # 自定义迭代次数
  python bench/post_tool_overhead.py --verbose     # 详细输出
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

# 确保可以 import hooks 模块
HOOKS_DIR = Path(__file__).resolve().parent.parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _init_state_file(project_root: str, sid: str) -> None:
    """预填充 model_router_state_<sid>.json —— 模拟已 decide + track 若干次的 session。"""
    claude_dir = Path(project_root) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / f"model_router_state_{sid}.json"

    data = {
        "version": "1.3",
        "session_id": sid,
        "decision": {
            "session_id": sid,
            "prompt_id": f"{sid[-8:]}-p0",
            "task_pattern": "feature",
            "task_complexity": "medium",
            "prompt_confidence": 0.65,
            "runtime_score": 15,
            "todo_score": 0,
            "final_model": "MiniMax-M3",
            "locked": False,
            "decision_source": "prompt",
            "last_update": int(time.time()),
        },
        "runtime_score": {
            "score": 15,
            "tool_count": 3,
            "tool_types": {"Read": 2, "Bash": 1},
        },
        "last_update": int(time.time()),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _reset_post_tool_globals() -> None:
    """重置 post_tool_handler 模块级单例（避免跨 benchmark run 状态泄漏）。"""
    import post_tool_handler
    from runtime_tracker import RuntimeTracker
    from todowrite_analyzer import TodoWriteAnalyzer
    post_tool_handler._tracker = RuntimeTracker()
    post_tool_handler._analyzer = TodoWriteAnalyzer()


# ── Mock events ──────────────────────────────────────────────────────────────

# 混合 7 种常见工具类型，模拟真实 session
MOCK_TOOL_EVENTS: list[dict] = [
    # Read — 最常见
    {
        "tool_name": "Read",
        "tool_input": {"file_path": "/project/src/app.py"},
    },
    {
        "tool_name": "Read",
        "tool_input": {"file_path": "/project/tests/test_app.py"},
    },
    {
        "tool_name": "Read",
        "tool_input": {"file_path": "/project/README.md"},
    },
    # Bash — git / ls / test
    {
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    },
    {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tests/ -x"},
    },
    {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
    },
    # Write
    {
        "tool_name": "Write",
        "tool_input": {"file_path": "/project/src/app.py", "content": "# hello"},
    },
    {
        "tool_name": "Write",
        "tool_input": {"file_path": "/project/tests/test_new.py", "content": "import unittest"},
    },
    # Edit
    {
        "tool_name": "Edit",
        "tool_input": {"file_path": "/project/src/app.py"},
    },
    # Grep
    {
        "tool_name": "Grep",
        "tool_input": {"pattern": "def test_", "path": "/project/tests/"},
    },
    # Glob
    {
        "tool_name": "Glob",
        "tool_input": {"pattern": "**/*.py"},
    },
    # TodoWrite — 每 15-20 次出现一次的"重要事件"
    {
        "tool_name": "TodoWrite",
        "tool_input": {
            "todos": [
                {"content": "implement user auth middleware", "status": "in_progress"},
                {"content": "write API endpoint tests", "status": "pending"},
                {"content": "update documentation", "status": "pending"},
            ]
        },
    },
    # WebFetch
    {
        "tool_name": "WebFetch",
        "tool_input": {"url": "https://docs.python.org/3/library/json.html"},
    },
    # WebSearch
    {
        "tool_name": "WebSearch",
        "tool_input": {"query": "python asyncio best practices"},
    },
]

# 生成 100 个事件的序列（循环取模 + TodoWrite 每 ~15 次出现）
def generate_event_sequence(n: int = 100) -> list[tuple[str, dict]]:
    """生成长度为 n 的事件序列，TodoWrite 约每 15 次出现一次。"""
    seq: list[tuple[str, dict]] = []
    for i in range(n):
        evt = MOCK_TOOL_EVENTS[i % len(MOCK_TOOL_EVENTS)]
        seq.append((str(uuid.uuid4()), evt))
    return seq


# ── Benchmark ────────────────────────────────────────────────────────────────

def run_benchmark(n_runs: int = 100, verbose: bool = False) -> dict:
    """运行 benchmark 并返回统计结果。

    Returns:
        dict with p50, p95, p99, max, mean, min, timings
    """
    from post_tool_handler import dispatch as _dispatch

    timings: list[float] = []

    for i in range(n_runs):
        # 每个 iteration 用独立的 temp project，避免跨 run state 累积
        tmpdir = tempfile.mkdtemp(prefix="bench_pt_")
        sid = f"bench-session-{uuid.uuid4().hex[:12]}"
        _init_state_file(tmpdir, sid)
        _reset_post_tool_globals()

        try:
            t0 = time.perf_counter()
            _dispatch(sid, tmpdir, MOCK_TOOL_EVENTS[i % len(MOCK_TOOL_EVENTS)])
            elapsed = (time.perf_counter() - t0) * 1000  # ms
            timings.append(elapsed)

            if verbose and (i < 5 or i >= n_runs - 5 or elapsed > 2.0):
                tag = ""
                if elapsed > 2.0:
                    tag = " ⚠️ SLOW"
                print(f"  [{i:3d}] {elapsed:7.3f} ms{tag}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    timings.sort()
    n = len(timings)

    def _percentile(p: float) -> float:
        """线性插值 percentile (p ∈ [0, 100])。"""
        idx = p / 100.0 * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return timings[lo] * (1 - frac) + timings[hi] * frac

    return {
        "n": n,
        "min": timings[0],
        "max": timings[-1],
        "mean": statistics.mean(timings),
        "p50": _percentile(50),
        "p95": _percentile(95),
        "p99": _percentile(99),
        "timings": timings,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="PostToolUse hook dispatch() 性能基线 — Stage 8.2"
    )
    parser.add_argument(
        "--runs", type=int, default=100,
        help="迭代次数 (default: 100)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="打印每次迭代的耗时",
    )
    args = parser.parse_args()

    print(f"PostToolUse dispatch() 性能基线 (Stage 8.2)")
    print(f"{'='*60}")
    print(f"  迭代次数: {args.runs}")
    print(f"  目标 p99: < 5.0 ms")
    print()

    result = run_benchmark(n_runs=args.runs, verbose=args.verbose)

    print(f"{'='*60}")
    print(f"  样本数: {result['n']}")
    print(f"  {'Min:':>6} {result['min']:8.3f} ms")
    print(f"  {'Mean:':>6} {result['mean']:8.3f} ms")
    print(f"  {'P50:':>6} {result['p50']:8.3f} ms")
    print(f"  {'P95:':>6} {result['p95']:8.3f} ms")
    print(f"  {'P99:':>6} {result['p99']:8.3f} ms")
    print(f"  {'Max:':>6} {result['max']:8.3f} ms")
    print()

    p99 = result["p99"]
    target = 5.0
    if p99 < target:
        print(f"  ✅ PASS: p99={p99:.3f} ms < {target} ms")
        return 0
    else:
        print(f"  ❌ FAIL: p99={p99:.3f} ms >= {target} ms")
        # 显示 top 10 最慢的
        slowest = sorted(result["timings"], reverse=True)[:10]
        print(f"  Top 10 最慢 (ms): {[f'{t:.3f}' for t in slowest]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
