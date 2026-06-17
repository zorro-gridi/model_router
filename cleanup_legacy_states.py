#!/usr/bin/env python3
"""一次性归档 ~/.claude/.claude/ 残留状态文件。

绝对不动当前 session 81c5ac33-7ca5-4088-b870-a6c3c8895c41 的任何文件
（其下 stage_/op_/model_/complexity_/pattern_/fallback_/model_router_state_/
session_state_*.json/.lock 均跳过）。

操作：
  - 移动所有非当前 session 的 <prefix>_<sid> 状态文件到
    ~/.claude/.claude_legacy_<ts>/ 子目录
  - 保留 ~/.claude/.claude/backups/ 目录不动
  - 不触碰非状态文件（如果有）
  - dry-run 模式：只列出会被移动的文件

使用：
  python cleanup_legacy_states.py            # 实际执行
  python cleanup_legacy_states.py --dry-run  # 预览
  python cleanup_legacy_states.py --list     # 列出当前 session 受保护文件
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOME_CLAUDE = Path.home() / ".claude"
SOURCE_DIR = HOME_CLAUDE / ".claude"
PROTECTED_SID = "81c5ac33-7ca5-4088-b870-a6c3c8895c41"

# 状态文件名前缀（与 stage_detector._stage_file_path / state_persistence 命名一致）
STATE_PREFIXES = (
    "stage_",
    "op_",
    "model_",
    "complexity_",
    "pattern_",
    "fallback_",
    "model_router_state_",
    "session_state_",
)


def is_state_file(name: str) -> bool:
    return any(name.startswith(p) for p in STATE_PREFIXES)


def belongs_to_current_session(name: str) -> bool:
    if not is_state_file(name):
        return False
    # 匹配 <prefix>_<PROTECTED_SID> 或 <prefix>_<PROTECTED_SID>.json
    return PROTECTED_SID in name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印要移动的文件，不实际执行")
    parser.add_argument("--list", action="store_true", help="列出当前 session 受保护的文件")
    args = parser.parse_args()

    if not SOURCE_DIR.is_dir():
        print(f"❌ 源目录不存在: {SOURCE_DIR}", file=sys.stderr)
        return 1

    # 列出所有受保护文件
    protected = sorted(
        n for n in SOURCE_DIR.iterdir()
        if n.is_file() and belongs_to_current_session(n.name)
    )

    if args.list:
        print(f"当前 session ({PROTECTED_SID}) 受保护文件（{len(protected)} 个）：")
        for p in protected:
            print(f"  - {p.name}")
        return 0

    # 列出待归档文件（非受保护的状态文件）
    to_move = sorted(
        p for p in SOURCE_DIR.iterdir()
        if p.is_file() and is_state_file(p.name) and not belongs_to_current_session(p.name)
    )

    # backups 目录跳过
    backups = SOURCE_DIR / "backups"
    if backups.is_dir():
        print(f"⚠️  跳过 backups/ 目录：{backups}")

    if not to_move:
        print("✅ 没有需要归档的 legacy 文件")
        return 0

    print(f"待归档文件数：{len(to_move)}")
    print(f"受保护文件数（当前 session）：{len(protected)}")
    for p in protected:
        print(f"  🔒 {p.name}")

    if args.dry_run:
        print("\n[DRY-RUN] 以下文件将被移动：")
        for p in to_move:
            print(f"  → {p.name}")
        return 0

    # 实际归档
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = HOME_CLAUDE / f".claude_legacy_{ts}"
    archive_dir.mkdir(parents=False, exist_ok=False)
    print(f"\n📦 归档目录：{archive_dir}")

    moved = 0
    failed = 0
    for p in to_move:
        try:
            shutil.move(str(p), str(archive_dir / p.name))
            moved += 1
        except OSError as e:
            print(f"❌ 移动失败 {p.name}: {e}", file=sys.stderr)
            failed += 1

    print(f"\n✅ 移动成功：{moved} / 失败：{failed}")
    print(f"📁 归档位置：{archive_dir}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
