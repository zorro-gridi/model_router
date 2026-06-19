"""
state_persistence.py — v1.3 持久化层（单写 + 兼容读）
=======================================================

V1.3 §5 适配层：model_router_state_<sid>.json 单文件持久化。

SessionStateStore 职责：
  - write(): 只写 model_router_state_<sid>.json
  - read_new(): 读新格式
  - read_legacy(): 从旧文件（v1.2）聚合读（向后兼容）
  - migrate(): 旧→新 一次性迁移

v1.3 已是唯一路径（不再需要 feature flag），write() 始终写入新格式。

原子写入：所有 write 均通过 .tmp + os.replace() 保证原子性。

设计约束：
  - 零依赖（除标准库）
  - 线程安全：os.replace() 是原子的，无需显式锁
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class SessionStateStore:
    """V1.3 持久化层 — 单文件写 + 兼容读。

    使用方式：
        store = SessionStateStore()
        store.write(sid, project_root, decision=rec, stage="implement",
                     pattern={...}, complexity={...})
        data = store.read_new(sid, project_root)   # → dict | None
        legacy = store.read_legacy(sid, project_root)  # → dict | None
        ok = store.migrate(sid, project_root)      # → True/False
    """

    VERSION = "1.3"

    # ── Path helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _new_file_path(sid: str, project_root: str) -> Path:
        return Path(project_root) / ".claude" / f"model_router_state_{sid}.json"

    @staticmethod
    def _ensure_claude_dir(project_root: str) -> Path:
        claude_dir = Path(project_root) / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        return claude_dir

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """原子写入：唯一临时名 + os.replace()。

        使用当前时间 + 线程 ID 生成唯一临时文件名，
        避免并发写入同一文件时 tmp 冲突导致 FileNotFoundError。
        """
        suffix = f".{os.getpid()}.{id(threading.current_thread())}.tmp"
        tmp_path = path.with_suffix(suffix)
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(str(tmp_path), str(path))
        finally:
            # 清理遗留 tmp（os.replace 成功后 tmp 不存在，此调用 no-op）
            try:
                tmp_path.unlink(missing_ok=True)  # type: ignore[attr-defined]
            except TypeError:
                # Python < 3.8 不支持 missing_ok
                if tmp_path.exists():
                    tmp_path.unlink()

    # ── Write ─────────────────────────────────────────────────────────────

    def write(
        self,
        sid: str,
        project_root: str,
        decision: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """写入 model_router_state_<sid>.json（v1.3 单文件持久化）。

        Args:
            sid: Session ID。
            project_root: 项目根目录（包含 .claude/）。
            decision: DecisionRecord.to_dict() 或兼容 dict。
            **kwargs: 可选附加字段 — stage, model_override, pattern,
                      complexity, batch, fallback, reqcnt, workflow_step。
        """
        claude_dir = self._ensure_claude_dir(project_root)
        new_path = claude_dir / f"model_router_state_{sid}.json"

        # 读取现有状态，保留其他组件写入的字段
        # （如 RuntimeTracker 写的 runtime_score、TodoWriteAnalyzer 写的
        #   todowrite_signal）。否则 create-from-scratch 会覆盖这些字段。
        existing: Dict[str, Any] = {}
        if new_path.exists():
            try:
                existing = json.loads(new_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}

        new_data: Dict[str, Any] = {
            "version": self.VERSION,
            "session_id": sid,
            "decision": decision or {},
            "last_update": int(time.time()),
        }

        # 新建文件（首次初始化）时写入默认路由字段为 None，
        # 后续由 proxy.py 在路由决策最终确定后回填。
        # 旧文件不强制补字段——保持向后兼容。
        if not existing:
            new_data["route_model"] = None
            new_data["task_complexity"] = None

        optional_fields = (
            "stage",
            "model_override",
            "pattern",
            "complexity",
            "batch",
            "fallback",
            "reqcnt",
            "workflow_step",
            "context_summary",  # V1.3 §11 Context Summary Injector
            "routing_reason",   # V1.3 §15.4 路由理由
            "route_model",      # 当前任务路由到的最终模型（proxy 写入）
            "task_complexity",  # 任务复杂度标签（proxy 写入）
            "current_prompt_id",  # V1.3 §4.2 当前 prompt ID（stage_detector 写入）
            # ── 2026-06-18 statusline v2：override→fallback 显示冲突 ──
            "pre_fallback_route_model",
            #   fallback 触发前的 route_model（sticky swap 后、fb_model 覆盖前）。
            #   statusline 用来在 fallback 标签里回指"原想跑的 model"，
            #   避免用 stage default model 误指代。proxy 端在 fallback 命中时写入。
            "override_degraded",
            #   bool。True = model_override 非空 + 本轮 fallback 被激活，
            #   即"用户指定的 override model 不可用、proxy 已切到备用 provider"。
            #   statusline 用来叠加 override + fallback 提示（规范 v2 §4）。
            # ── 2026-06-19 model tier：pre-computed tier for statusline ──
            "route_model_tier",
            #   int。route_model 的能力 tier（从 config/model_tiers.yaml 加载）。
            #   proxy 在每次请求时写入，statusline.sh 直接读取。
            "stage_model_tier",
            #   int。session 默认 model 的能力 tier（从 config/model_tiers.yaml 加载）。
            #   proxy 在每次请求时写入，statusline.sh 直接读取。
        )
        for key in optional_fields:
            if key in kwargs:
                # 显式传入（包括 None = 明确清除）→ 直接写。
                # 2026-06-18 修复：旧逻辑 `kwargs[key] is not None` 会把
                # fallback=None（sticky 已被 health_checker 清除）误跳过，
                # 转而从 existing 继承 stale 值，导致 statusline 永久误显
                # fallback 标签（详见 hooks.md § sticky 归因错误记录）。
                new_data[key] = kwargs[key]
            elif key in existing:
                # 未显式传入时从 existing 继承——避免 write() 调用方漏传
                # 关键字时误清掉 proxy 之前回填的 route_model/task_complexity。
                new_data[key] = existing[key]

        # 保留 existing 中 write() 不管理的字段（runtime_score、todowrite_signal 等）
        managed_keys = {"version", "session_id", "decision", "last_update"} | set(optional_fields)
        for key, value in existing.items():
            if key not in managed_keys and key not in new_data:
                new_data[key] = value

        self._atomic_write(new_path, json.dumps(new_data, ensure_ascii=False, indent=2))

    # ── Read ──────────────────────────────────────────────────────────────

    def read_new(self, sid: str, project_root: str) -> Optional[Dict[str, Any]]:
        """读取新格式 model_router_state_<sid>.json。

        Returns:
            dict 或 None（文件缺失 / JSON 损坏）。
        """
        new_path = self._new_file_path(sid, project_root)
        if not new_path.exists():
            return None
        try:
            return json.loads(new_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    def read_legacy(self, sid: str, project_root: str) -> Optional[Dict[str, Any]]:
        """从旧 9 文件聚合读取（v1.2 格式）。

        Returns:
            聚合 dict 或 None（没有任何旧文件存在）。
        """
        claude_dir = Path(project_root) / ".claude"
        if not claude_dir.exists():
            return None

        result: Dict[str, Any] = {}
        found_any = False

        # stage — plain text
        stage_path = claude_dir / f"stage_{sid}"
        if stage_path.exists():
            try:
                result["stage"] = stage_path.read_text(encoding="utf-8").strip()
                found_any = True
            except OSError:
                pass

        # model_override — plain text
        model_path = claude_dir / f"model_{sid}"
        if model_path.exists():
            try:
                result["model_override"] = model_path.read_text(encoding="utf-8").strip()
                found_any = True
            except OSError:
                pass

        # JSON files
        json_fields = (
            "pattern",
            "complexity",
            "batch",
            "fallback",
            "reqcnt",
            "workflow_step",
            "op",
        )
        for key in json_fields:
            path = claude_dir / f"{key}_{sid}"
            if path.exists():
                try:
                    result[key] = json.loads(path.read_text(encoding="utf-8"))
                    found_any = True
                except (json.JSONDecodeError, OSError):
                    pass
            else:
                result[key] = None

        return result if found_any else None

    # ── Migrate ───────────────────────────────────────────────────────────

    def migrate(self, sid: str, project_root: str) -> bool:
        """一次性迁移：旧 9 文件 → model_router_state_<sid>.json。

        Returns:
            True 迁移成功；False 无旧文件可迁移。
        """
        legacy = self.read_legacy(sid, project_root)
        if legacy is None:
            return False

        claude_dir = self._ensure_claude_dir(project_root)

        new_data: Dict[str, Any] = {
            "version": self.VERSION,
            "session_id": sid,
            "decision": {},
            "last_update": int(time.time()),
            "migrated_from": "v1.2",
        }

        # 复制所有旧字段
        for key, value in legacy.items():
            if value is not None:
                new_data[key] = value

        new_path = claude_dir / f"model_router_state_{sid}.json"
        self._atomic_write(new_path, json.dumps(new_data, ensure_ascii=False, indent=2))
        return True
