# §13 状态文件与并发规范 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 13 章（"状态文件与并发规范"）
> 审计时间：2026-06-14
> 审计范围：4 级 Project Binding 查找顺序 + state_index.json 结构 + active_session 兜底
> 落地代码：
>
> - `/Users/zorro/.claude/hooks/model_router/state_index.json`
> - `/Users/zorro/.claude/hooks/model_router/active_session`
> - `stage_detector.py` 中 per-session 文件路径解析

---

## 13.1 设计文档要求

**4 级路由查找顺序**：

1. **Level 1**: Project Binding（project_root 匹配）
2. **Level 2**: session_id（匹配）
3. **Level 3**: timestamp（匹配）
4. **Level 4**: active_session（兼容旧版）

**禁止**直接使用最近活跃会话作为默认逻辑（多窗口冲突）。

**新版 state_index.json 示例**：

```json
{
  "/project-a": {
    "session_id": "aaa",
    "stage": "design"
  },
  "/project-b": {
    "session_id": "bbb",
    "stage": "implement"
  }
}
```

## 13.2 当前实现

### 13.2.1 state_index.json（实际内容）

```json
{
  "/Users/zorro/.claude": {
    "session_id": "e694067c-a1a7-4154-9c49-d7fbf361f1fd",
    "stage": "design",
    "last_active": 1781421678
  }
}
```

✅ **结构对齐**：project_root → session info 结构与文档示例一致。

- 增加了 `last_active` 字段（文档未列出但合理，用于 timestamp 排序）

### 13.2.2 active_session 兜底

✅ 存在 `/Users/zorro/.claude/hooks/model_router/active_session` 指针文件，全局 fallback。

### 13.2.3 per-session 文件

`stage_detector.py` 中维护：

- `<cwd>/.claude/stage_<sid>` （per-session stage 状态）
- `<cwd>/.claude/model_<sid>` （model override）
- `<cwd>/.claude/op_<sid>` （op override）
- `<cwd>/.claude/pattern_<sid>` （pattern 记录）
- `<cwd>/.claude/fallback_<sid>` （sticky fallback）
- `<cwd>/.claude/complexity_<sid>` （complexity 调整）
- `<cwd>/.claude/batch_<sid>` （batch template）

## 13.3 差异清单

### D13-1 [DEVIATION] state_index.json Level 3 timestamp 查找未实现

- **文档 Level 3**：timestamp 匹配（最近活跃的 session 优先）
- **当前实现**：state_index.json 存了 `last_active` 字段，但 proxy/state_detector 中**未实现"按 last_active 排序选择最近 session"的逻辑**
- **后果**：
  - 同 project_root 下多 session 并发时，新 session 无法基于 timestamp 自动选择最近活跃
- **建议修复**：
  ```python
  # 4 级查找伪代码
  def find_state(project_root, session_id):
      # Level 1: project_root 命中
      if project_root in state_index:
          entry = state_index[project_root]
          if entry["session_id"] == session_id:
              return entry  # 完美匹配
      # Level 2: session_id 全局匹配（兼容同一 session 多 cwd 场景）
      for path, entry in state_index.items():
          if entry["session_id"] == session_id:
              return entry
      # Level 3: timestamp 最近活跃（同一 project_root 下新 session）
      candidates = [e for p, e in state_index.items()
                    if p == project_root or p.startswith(project_root)]
      if candidates:
          return max(candidates, key=lambda e: e.get("last_active", 0))
      # Level 4: active_session fallback
      return read_active_session()
  ```
- **风险等级**：P2（多 session 并发场景才暴露）

### D13-2 [DEVIATION] state_index.json 缺少 timestamp-only 索引

- **文档 §13**："Level 3 timestamp 匹配"
- **当前实现**：state_index.json 用 `project_root` 作为顶层 key，session 信息作为 value
- **后果**：
  - timestamp 索引是隐式的（按 `last_active` 排序）
  - 没有"按 timestamp 跨 project_root 找最近活跃 session"的显式逻辑
- **建议修复**：在 state_detector 中显式实现 D13-1 的 Level 3
- **风险等级**：P2

### D13-3 [PASS] Level 1 Project Binding

- **文档 Level 1**：project_root 匹配
- **当前实现**：✅ state_index.json 的 key 就是 project_root
- **结论**：PASS

### D13-4 [PASS] Level 2 session_id 匹配

- **文档 Level 2**：session_id 匹配
- **当前实现**：✅ state_index.json 的 value 含 `session_id`
- **结论**：PASS

### D13-5 [PASS] Level 4 active_session 兼容

- **文档 Level 4**：active_session 兼容旧版
- **当前实现**：✅ `active_session` 指针文件存在
- **结论**：PASS

### D13-6 [EXPECTED] per-session 文件路径

- **文档**：未明确指定 per-session 文件位置
- **当前实现**：`<cwd>/.claude/stage_<sid>` 等
- **结论**：✅ EXPECTED（业务侧合理选择）

## 13.4 验收结论

| Level                   | 文档要求          | 实现状态         |
| ----------------------- | ----------------- | ---------------- |
| Level 1 Project Binding | project_root 匹配 | ✅ PASS          |
| Level 2 session_id      | 匹配              | ✅ PASS          |
| Level 3 timestamp       | 匹配              | ❌ FAIL（D13-1） |
| Level 4 active_session  | 兼容旧版          | ✅ PASS          |

**4 级查找覆盖率**：3/4 = 75%

## 13.5 修复优先级

1. **P2** — D13-1 实现 Level 3 timestamp 查找（多 session 并发场景）

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
