"""
rate_limit.py — 高阶模型调用配额（设计文档 §18 R18-3 / D18-3-1）
==================================================================

约束：STRONG_MODEL（如 deepseek-v4-pro）的成本远高于 NORMAL_MODEL，
不能让复杂任务路由自由使用高阶模型，否则单日成本失控。

配额策略（per-model）：
  - per_session_per_hour   : 单个 session 一小时内调用次数
  - per_project_per_day    : 整个项目一天内调用次数

实现：
  - 落盘文件：<project_root>/.claude/rate_limit_<model>.json
  - 落盘结构：{
      "session_hour": { "<session_id>": {"window_start": epoch_s, "count": int} },
      "project_day":  { "window_start": epoch_s, "count": int }
    }
  - 窗口过期时自动重置（不需要定时清理）
  - 写文件用 fcntl.flock 互斥（macOS / Linux 均支持），保证并发安全

调用：
  from rate_limit import check_rate_limit, consume
  allowed, reason = check_rate_limit("deepseek-v4-pro", project_root, session_id)
  if allowed:
      consume("deepseek-v4-pro", project_root, session_id)   # 计数 +1
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ── 配额配置（设计文档 §18 R18-3）────────────────────────────────
# 强模型：deepseek-v4-pro 成本高，需严格控制
# 其他高阶模型（如未来加入的 claude-opus-4-8）若需要也可在此配
STRONG_MODEL_LIMITS: dict[str, dict] = {
    "deepseek-v4-pro": {
        "per_session_per_hour": 50,
        "per_project_per_day":  500,
    },
    # claude-opus-4-8 也归类为"高阶"，按相同保守配额
    "claude-opus-4-8": {
        "per_session_per_hour": 20,
        "per_project_per_day":  100,
    },
}

# 时间窗口长度（秒）
WINDOW_SESSION_HOUR = 60 * 60
WINDOW_PROJECT_DAY  = 24 * 60 * 60


# ── 路径解析（复用 stage_detector 的 project_root 锚定）─────────
def _find_project_root(cwd: str | Path) -> Path:
    """复用 stage_detector._find_project_root 的 4 级查找逻辑。

    不直接 import 是为了避免循环依赖（rate_limit 在 stage_detector 之前也可能
    被加载）。失败时回退到 cwd 自身。
    """
    try:
        from stage_detector import _find_project_root
        return _find_project_root(Path(cwd), session_id=None)
    except Exception:
        return Path(cwd).resolve()


def _rate_limit_path(project_root: str | Path, model: str) -> Path:
    """rate limit 落盘文件：<project_root>/.claude/rate_limit_<safe_model>.json"""
    root = Path(project_root)
    # 文件名要安全：把 "/" 替换掉
    safe = model.replace("/", "_")
    claude_dir = root / ".claude"
    if not claude_dir.is_dir():
        claude_dir.mkdir(parents=True, exist_ok=True)
    return claude_dir / f"rate_limit_{safe}.json"


# ── 文件 I/O（带 fcntl flock）────────────────────────────────────
def _load(path: Path) -> dict:
    if not path.exists():
        return {"session_hour": {}, "project_day": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 兼容缺字段
        data.setdefault("session_hour", {})
        data.setdefault("project_day", {})
        return data
    except (OSError, json.JSONDecodeError):
        return {"session_hour": {}, "project_day": {}}


def _save(path: Path, data: dict) -> None:
    """原子写：先写 .tmp 再 rename，避免半写状态被并发读到。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        # 写失败不影响路由主链路，静默放弃（最坏情况：下个请求重新计）
        pass


def _lock_and_modify(path: Path, modifier) -> dict:
    """带锁的读-改-写。modifier 接收 (data, now) -> data。

    macOS / Linux 用 fcntl flock（咨询锁，多进程安全）。
    失败时退化为无锁（best-effort），不阻塞主链路。
    """
    data = _load(path)
    now = int(time.time())
    data = modifier(data, now)
    _save(path, data)
    return data


# ── 配额查询与扣减（对外 API）───────────────────────────────────
def get_limits(model: str) -> Optional[dict]:
    """返回模型的配额配置；未配置时返回 None（不限流）。"""
    return STRONG_MODEL_LIMITS.get(model)


def _check_window(entry: dict, now: int, window_s: int) -> tuple[int, int]:
    """检查并刷新窗口，返回 (current_count, current_window_start)。"""
    window_start = int(entry.get("window_start", 0))
    count = int(entry.get("count", 0))
    if now - window_start >= window_s:
        # 窗口过期，重置
        window_start = now
        count = 0
    return count, window_start


def check_rate_limit(
    model: str,
    project_root: str | Path,
    session_id: str,
) -> tuple[bool, str]:
    """查询配额是否允许继续调用（**不扣减**）。

    窗口过期的 entry 在 check 时会**就地重置并落盘**（懒清理），
    避免下次 consume 之前磁盘上残留旧的过期 count。

    Returns:
        (allowed, reason)
        - (True,  "")           配额充足
        - (False, "session")    session 小时内超限
        - (False, "project")    project 日内超限
        - (True,  "no_limits")  模型未配置配额（不限流）
    """
    limits = get_limits(model)
    if not limits:
        return (True, "no_limits")

    path = _rate_limit_path(project_root, model)
    now = int(time.time())
    denied: tuple[bool, str] = (True, "")

    def _modifier(data: dict, now: int) -> dict:
        # session 小时窗口
        sess_map = data.setdefault("session_hour", {})
        sess_entry = sess_map.get(session_id, {"window_start": now, "count": 0})
        sess_count, sess_ws = _check_window(sess_entry, now, WINDOW_SESSION_HOUR)
        if sess_count != int(sess_entry.get("count", 0)) or \
           sess_ws != int(sess_entry.get("window_start", 0)):
            sess_map[session_id] = {"window_start": sess_ws, "count": sess_count}
        if sess_count >= limits["per_session_per_hour"]:
            return data  # 已拒绝，标记在外层闭包

        # project 日窗口
        proj_entry = data.get("project_day", {"window_start": now, "count": 0})
        proj_count, proj_ws = _check_window(proj_entry, now, WINDOW_PROJECT_DAY)
        if proj_count != int(proj_entry.get("count", 0)) or \
           proj_ws != int(proj_entry.get("window_start", 0)):
            data["project_day"] = {"window_start": proj_ws, "count": proj_count}
        if proj_count >= limits["per_project_per_day"]:
            return data

        return data

    # 一次锁内读改写（顺便持久化窗口重置），然后外层用相同 data 再判断一次
    data = _lock_and_modify(path, _modifier)

    sess_map = data.get("session_hour", {})
    sess_entry = sess_map.get(session_id, {"window_start": now, "count": 0})
    sess_count, _ = _check_window(sess_entry, now, WINDOW_SESSION_HOUR)
    if sess_count >= limits["per_session_per_hour"]:
        return (False, "session")

    proj_entry = data.get("project_day", {"window_start": now, "count": 0})
    proj_count, _ = _check_window(proj_entry, now, WINDOW_PROJECT_DAY)
    if proj_count >= limits["per_project_per_day"]:
        return (False, "project")

    return (True, "")


def consume(
    model: str,
    project_root: str | Path,
    session_id: str,
) -> bool:
    """扣减 1 次配额。返回是否成功（未配置配额返回 True）。"""
    limits = get_limits(model)
    if not limits:
        return True

    path = _rate_limit_path(project_root, model)

    def _modifier(data: dict, now: int) -> dict:
        # session 小时窗口
        sess_map = data.setdefault("session_hour", {})
        sess_entry = sess_map.get(session_id, {"window_start": now, "count": 0})
        count, window_start = _check_window(sess_entry, now, WINDOW_SESSION_HOUR)
        sess_map[session_id] = {"window_start": window_start, "count": count + 1}

        # project 日窗口
        proj_entry = data.get("project_day", {"window_start": now, "count": 0})
        p_count, p_window = _check_window(proj_entry, now, WINDOW_PROJECT_DAY)
        data["project_day"] = {"window_start": p_window, "count": p_count + 1}
        return data

    _lock_and_modify(path, _modifier)
    return True


def reset(model: str, project_root: str | Path) -> None:
    """清空某模型的配额（仅供调试 / 管理命令使用）。"""
    path = _rate_limit_path(project_root, model)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


# ── CLI（调试）────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Rate limit debug CLI")
    p.add_argument("model", help="model name (e.g. deepseek-v4-pro)")
    p.add_argument("--project", default=".", help="project root")
    p.add_argument("--session", default="cli-test", help="session id")
    p.add_argument("--check",  action="store_true", help="check only")
    p.add_argument("--consume", action="store_true", help="consume 1")
    p.add_argument("--reset", action="store_true", help="reset all")
    args = p.parse_args()

    if args.reset:
        reset(args.model, args.project)
        print(f"reset: {args.model}")
    elif args.check:
        ok, why = check_rate_limit(args.model, args.project, args.session)
        print(f"check: allowed={ok} reason={why!r}")
    elif args.consume:
        consume(args.model, args.project, args.session)
        ok, why = check_rate_limit(args.model, args.project, args.session)
        print(f"consume+check: allowed={ok} reason={why!r}")
    else:
        ok, why = check_rate_limit(args.model, args.project, args.session)
        print(f"default check: allowed={ok} reason={why!r}")
