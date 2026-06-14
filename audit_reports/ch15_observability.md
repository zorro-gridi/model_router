# §15 日志、指标与可观测性 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 15 章（"日志、指标与可观测性"）
> 审计时间：2026-06-14
> 审计范围：每次分类/路由/fallback 的日志完整性 + /health /metrics /trace 三个接口
> 落地代码：
>
> - `/Users/zorro/.claude/hooks/model_router/stage_router.log`（日志）
> - `proxy.py:1229+` (do_GET)
> - `stage_detector.py:1067+` (log 写入逻辑)

---

## 15.1 设计文档要求

**每次分类必须记录**：

- 输入摘要
- pattern
- stage
- complexity
- score
- 置信度
- 最终模型
- 耗时
- token 估计

**每次 fallback 必须记录**：

- 错误码
- 主模型
- 备用模型
- 是否写入 sticky 状态

**必须提供的统计指标**：

- 模型调用量
- 强模型占比
- 平均每任务 token
- 复杂任务成功率
- 返工率

**建议按维度聚合**：项目维度 / 会话维度 / pattern 维度

## 15.2 当前实现

### 15.2.1 `stage_router.log` 日志格式

✅ 已升级日志格式（2026-06-14 commit 33b494e），每条路由记录新增：

- `session`（会话 ID）
- `task_pattern`（识别出的 pattern）
- `task_complexity`（complexity 标签）
- `task_stage`（最终 stage）

### 15.2.2 三个 HTTP 接口

| 端点       | 实现位置        | 文档要求 | 状态 |
| ---------- | --------------- | -------- | ---- |
| `/health`  | proxy.py do_GET | 健康检查 | ✅   |
| `/metrics` | proxy.py do_GET | 指标查询 | ✅   |
| `/trace`   | proxy.py do_GET | 路由追踪 | ✅   |

### 15.2.3 指标收集（proxy.py:1184-1221）

✅ Structured metrics + routing log：

- 路由决策全过程
- 耗时统计
- token 估计
- fallback 次数

## 15.3 差异清单

### D15-1 [DEVIATION] 文档要求的统计指标部分缺失

- **文档 §15 P2**："必须提供统计指标：模型调用量、强模型占比、平均每任务 token、复杂任务成功率、返工率"
- **当前实现**：
  - ✅ 模型调用量（metrics 端点）
  - ✅ 耗时 / token 估计（log 字段）
  - ❌ **强模型占比**（未统计 deepseek-v4-pro 调用占比）
  - ❌ **复杂任务成功率**（无 success/failure 标记）
  - ❌ **返工率**（无 retry 计数）
- **建议修复**：
  ```python
  # proxy.py metrics 端点
  metrics = {
      "model_call_count": {...},  # 已有
      "strong_model_ratio": ...,  # 新增
      "avg_tokens_per_task": ...,  # 已有
      "complex_task_success_rate": ...,  # 新增
      "retry_rate": ...,  # 新增
  }
  ```
- **风险等级**：P2（统计指标缺失）

### D15-2 [DEVIATION] 维度聚合未实现

- **文档 §15 P3**："建议按项目维度、会话维度、pattern 维度分别聚合"
- **当前实现**：metrics 端点只输出全局统计
- **建议修复**：
  ```python
  metrics = {
      "by_project": {
          "/project-a": {
              "call_count": 100,
              "strong_ratio": 0.3,
              ...
          },
          ...
      },
      "by_pattern": {
          "feature": {...},
          "bugfix": {...},
      },
  }
  ```
- **风险等级**：P2

### D15-3 [PASS] 输入摘要记录

- **文档**：必须记录输入摘要
- **当前实现**：✅ stage_router.log 含 prompt 摘要 + 完整 prompt
- **结论**：PASS

### D15-4 [PASS] Fallback 详细记录

- **文档**：错误码 / 主模型 / 备用模型 / sticky 状态
- **当前实现**：✅ log 记录完整
- **结论**：PASS

### D15-5 [DEVIATION] `/trace` 端点语义不明确

- **文档 §15**：建议 /trace 接口
- **当前实现**：✅ 实现 /trace，但**未明确返回结构**
- **建议修复**：
  - 明确 /trace 返回结构（最近 N 条路由决策的完整 trace）
  - 支持按 session_id / project_root 过滤
- **风险等级**：P3

### D15-6 [EXPECTED] 日志脱敏

- **文档**：未明确
- **当前实现**：prompt 完整写入 log（**未脱敏**）
- **结论**：⚠️ 隐私风险（如 prompt 含 API key / 密码）
- **建议修复**：增加 prompt 摘要 + 敏感字段（password / api_key / token）脱敏
- **风险等级**：P2（合规风险）

## 15.4 验收结论

| 文档要求                                             | 状态             |
| ---------------------------------------------------- | ---------------- |
| 每次分类记录输入摘要                                 | ✅ PASS          |
| 每次分类记录 pattern/stage/complexity/score/置信度   | ✅ PASS          |
| 每次分类记录最终模型/耗时/token                      | ✅ PASS          |
| 每次 fallback 记录错误码/主模型/备用模型/sticky 状态 | ✅ PASS          |
| /health /metrics /trace 三个接口                     | ✅ PASS          |
| 统计指标：模型调用量                                 | ✅ PASS          |
| 统计指标：强模型占比                                 | ❌ FAIL          |
| 统计指标：平均每任务 token                           | ✅ PASS          |
| 统计指标：复杂任务成功率                             | ❌ FAIL          |
| 统计指标：返工率                                     | ❌ FAIL          |
| 维度聚合（项目/会话/pattern）                        | ❌ FAIL（D15-2） |

## 15.5 修复优先级

1. **P2** — D15-1 补齐统计指标（强模型占比 / 复杂任务成功率 / 返工率）
2. **P2** — D15-2 增加维度聚合（项目 / 会话 / pattern）
3. **P2** — D15-6 prompt 日志脱敏
4. **P3** — D15-5 /trace 端点文档化

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
