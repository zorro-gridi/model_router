# 审计报告：第9章 — TodoWrite 分析策略

**审计日期**: 2026-06-15
**设计文档版本**: V1.3
**审计范围**: TodoWrite 分析策略 vs 实际实现

---

## 9.1 为什么 TodoWrite 权重高

**设计要求**: TodoWrite 出现在已完成初步理解、已形成执行规划、即将进入实质修改之前，此时系统拥有足够上下文做准确复杂度识别。

**实现状态**: ✅ 理解一致，权重已体现

| 检查点   | 状态 | 证据                                                                                    |
| -------- | ---- | --------------------------------------------------------------------------------------- |
| 权重最高 | ✅   | `_PLACEHOLDER_WEIGHTS["tool"]["TodoWrite"] = 8`（所有工具中最高）                       |
| 触发升级 | ✅   | `maybe_redecide()` 中 `is_implementation=True` → `todo_force_lock=True` → 强制 ≥ medium |
| 触发锁定 | ✅   | `todo_force_lock=True` 时 `need_lock=True`，写入 `locked=True`                          |

---

## 9.2 TodoWrite 分析关注点

**设计要求**: LLM 分析 TodoWrite 时重点看：todo 数量、每个 todo 是否独立、是否跨文件、是否跨模块、是否包含回归测试、是否包含迁移/兼容/重构、是否存在大量依赖前置步骤。

**实现状态**: ❌ 严重缩水

逐项对比：

| 设计要求                 | 实现方式                                             | 状态          |
| ------------------------ | ---------------------------------------------------- | ------------- |
| todo 数量                | `todowrite_analyzer.py` 统计 total/pending/completed | ✅ 关键词匹配 |
| 每个 todo 是否独立       | 未实现                                               | ❌            |
| 是否跨文件               | 未实现                                               | ❌            |
| 是否跨模块               | 未实现                                               | ❌            |
| 是否包含回归测试         | 未实现（关键词可能有"test"但非语义理解）             | ❌            |
| 是否包含迁移/兼容/重构   | 未实现（关键词可能有"migrate"/"refactor"但非语义）   | ❌            |
| 是否存在大量依赖前置步骤 | 未实现                                               | ❌            |

**实现 vs 设计对比**:

```
设计文档 §9.2 要求:
  LLM 分析 TodoWrite → 7 个分析维度 → 综合复杂度判断

实际实现:
  关键词匹配 → 判断 is_implementation → complexity_signal = min(pending/10, 1.0)
```

`complexity_signal` 的线性公式 `pending/10` 完全基于数量，不反映任务间的依赖关系、跨文件程度、测试/迁移等质量维度。

---

## 9.3 TodoWrite 触发后的策略

**设计要求**:

1. 立即分析 ✅
2. 与 Runtime Score 融合 ✅
3. 生成最终复杂度 ✅
4. 锁定模型 ✅
5. 这一步不应拖延 ✅

**实现状态**: ⚠️ 流程对齐，分析深度不足

| 步骤                  | 状态 | 证据                                                                    |
| --------------------- | ---- | ----------------------------------------------------------------------- |
| 立即分析              | ✅   | `post_tool_handler.py:65` 检测到 TodoWrite 立即调用 `_handle_todowrite` |
| 与 Runtime Score 融合 | ✅   | `maybe_redecide()` 取 max(current, runtime_label, todo_label)           |
| 生成最终复杂度        | ✅   | `maybe_redecide()` 返回新的 DecisionRecord                              |
| 锁定模型              | ✅   | `todo_force_lock=True` → `locked=True`                                  |
| 不拖延                | ✅   | 同一 PostToolUse 事件中同步完成分析+决策                                |

流程控制完全对齐设计。但"分析深度"不足导致"最终复杂度"的准确性依赖过于简陋的关键词匹配。

---

## 设计文档伪代码 vs 实际代码对比

**设计文档 §4.3 伪代码**:

```python
if tool_name == "TodoWrite" and is_first_todo_write:
    complexity = llm_analyze_todos(todos)  # LLM 深度分析
    if complexity == "COMPLEX":
        force_upgrade()
```

**实际代码**:

```python
# post_tool_handler.py:65
if tool_name == "TodoWrite":
    _handle_todowrite(sid, project_root, raw_event)
    # → todowrite_analyzer.analyze(todos)  # 纯关键词匹配，非 LLM
    # → maybe_redecide(todowrite_signal=signal)
    # → is_implementation → force medium + lock
```

**差异总结**:

1. `is_first_todo_write` 判断 → 未实现（每次都触发）
2. `llm_analyze_todos()` → 降级为 `analyze()` 关键词匹配
3. `force_upgrade()` → 降级为 `force medium`（非 complex）

---

## 总体评估

| 子章节               | 对齐度                                  |
| -------------------- | --------------------------------------- |
| 9.1 TodoWrite 权重高 | ✅ 100%                                 |
| 9.2 分析关注点       | ❌ 15% — 7 维度仅覆盖 1 个（todo 数量） |
| 9.3 触发后策略       | ⚠️ 75% — 流程对齐，分析深度不足         |

**综合评分**: 63% 对齐

**关键差异**:

1. **LLM 分析→关键词匹配**：这是设计与实现之间最大鸿沟之一
2. **7 个分析维度仅覆盖 1 个**（todo 数量），其余 6 个维度完全缺失
3. **`is_first_todo_write` 判断缺失**：每次 TodoWrite 都触发，可能导致多次分析
4. **升级力度不足**：设计文档的 `force_upgrade()`（暗示可能直达 complex）被降级为 `force medium`
