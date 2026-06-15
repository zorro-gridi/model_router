"""
state_persistence.py — v1.3 持久化层：双写 + 兼容读
=======================================================

V1.3 §5 适配层：session_state_<sid>.json 双写 + 兼容读。

SessionStateStore 职责：
  - write(): 双写 — 新 session_state_<sid>.json + 旧 9 文件
  - read_new(): 读新格式
  - read_legacy(): 从旧 9 文件（v1.2）聚合读
  - migrate(): 旧→新 一次性迁移
  - MODEL_ROUTER_V13_WRITE env flag（默认 True，关闭则只写旧文件）

旧 9 文件（v1.2 遗留）：
  stage_<sid>（plain text）、model_<sid>（plain text）、
  pattern_<sid>（JSON）、complexity_<sid>（JSON）、batch_<sid>（JSON）、
  fallback_<sid>（plain text/json）、reqcnt_<sid>（JSON）、
  workflow_step_<sid>（JSON）、op_<sid>（JSON，已废弃）

原子写入：所有 write 均通过 .tmp + os.replace() 保证原子性。

设计约束：
  - 零依赖（除标准库）
  - 线程安全：os.replace() 是原子的，无需显式锁
  - 兼容读：旧消费方（proxy/stage_show）暂不感知新格式
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class SessionStateStore:
    """V1.3 持久化层 — 双写 + 兼容读。

    使用方式：
        store = SessionStateStore()
        store.write(sid, project_root, decision=rec, stage="implement",
                     pattern={...}, complexity={...})
        data = store.read_new(sid, project_root)   # → dict | None
        legacy = store.read_legacy(sid, project_root)  # → dict | None
        ok = store.migrate(sid, project_root)      # → True/False
    """

    VERSION = "1.3"

    # ── Feature Flag ──────────────────────────────────────────────────────

    @staticmethod
    def _is_enabled() -> bool:
        """MODEL_ROUTER_V13_WRITE flag：默认 True（开启双写）。"""
        flag = os.environ.get("MODEL_ROUTER_V13_WRITE", "1")
        return flag.lower() not in ("0", "false", "no", "off")

    # ── Path helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _new_file_path(sid: str, project_root: str) -> Path:
        return Path(project_root) / ".claude" / f"session_state_{sid}.json"

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
        """双写：新 session_state_<sid>.json + 旧 9 文件。

        Args:
            sid: Session ID。
            project_root: 项目根目录（包含 .claude/）。
            decision: DecisionRecord.to_dict() 或兼容 dict。
            **kwargs: 可选旧字段 — stage, model_override, pattern,
                      complexity, batch, fallback, reqcnt, workflow_step。
        """
        claude_dir = self._ensure_claude_dir(project_root)

        # ── 新格式数据 ──
        new_data: Dict[str, Any] = {
            "version": self.VERSION,
            "session_id": sid,
            "decision": decision or {},
            "last_update": int(time.time()),
        }

        optional_fields = (
            "stage",
            "model_override",
            "pattern",
            "complexity",
            "batch",
            "fallback",
            "reqcnt",
            "workflow_step",
        )
        for key in optional_fields:
            if key in kwargs and kwargs[key] is not None:
                new_data[key] = kwargs[key]

        # ── 旧文件双写（始终写，不依赖 flag） ──
        self._write_legacy_files(claude_dir, sid, **kwargs)

        # ── 新文件写（受 flag 控制） ──
        if self._is_enabled():
            new_path = claude_dir / f"session_state_{sid}.json"
            self._atomic_write(new_path, json.dumps(new_data, ensure_ascii=False, indent=2))

    def _write_legacy_files(self, claude_dir: Path, sid: str, **kwargs: Any) -> None:
        """写入旧格式文件（v1.2 兼容双写）。"""
        # Plain-text files
        if "stage" in kwargs and kwargs["stage"] is not None:
            self._atomic_write(
                claude_dir / f"stage_{sid}",
                f"{kwargs['stage']}\n",
            )

        if "model_override" in kwargs and kwargs["model_override"] is not None:
            self._atomic_write(
                claude_dir / f"model_{sid}",
                f"{kwargs['model_override']}\n",
            )

        # JSON files
        json_fields = (
            "pattern",
            "complexity",
            "batch",
            "fallback",
            "reqcnt",
            "workflow_step",
        )
        for key in json_fields:
            if key in kwargs and kwargs[key] is not None:
                self._atomic_write(
                    claude_dir / f"{key}_{sid}",
                    json.dumps(kwargs[key], ensure_ascii=False),
                )

    # ── Read ──────────────────────────────────────────────────────────────

    def read_new(self, sid: str, project_root: str) -> Optional[Dict[str, Any]]:
        """读取新格式 session_state_<sid>.json。

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
        """一次性迁移：旧 9 文件 → session_state_<sid>.json。

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

        new_path = claude_dir / f"session_state_{sid}.json"
        self._atomic_write(new_path, json.dumps(new_data, ensure_ascii=False, indent=2))
        return True
