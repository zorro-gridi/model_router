# §7 Stage 体系详细规范 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 7 章（"Stage 体系详细规范"）
> 审计时间：2026-06-14
> 审计范围：`stage_config.STAGE_CONFIG` 7 个 stage 与文档 6 个 stage 表格的逐项对齐
> 落地代码：`/Users/zorro/.claude/hooks/model_router/stage_config.py:48-145`

---

## 7.1 设计文档表格（6 个 stage）

| Stage     | 职责                                         | 默认模型   | 升级模型        | 降级模型          |
| --------- | -------------------------------------------- | ---------- | --------------- | ----------------- |
| explore   | 理解项目、读代码、追调用链、看日志、定位现状 | MiniMax-M3 | DeepSeek-V4-Pro | DeepSeek-V4-Flash |
| plan      | 拆解任务、规划步骤、定义范围、评估风险       | MiniMax-M3 | DeepSeek-V4-Pro | DeepSeek-V4-Flash |
| design    | 架构设计、接口设计、数据结构设计、方案权衡   | MiniMax-M3 | DeepSeek-V4-Pro | DeepSeek-V4-Flash |
| implement | 编码、修复、重构、迁移、补全逻辑             | MiniMax-M3 | DeepSeek-V4-Pro | DeepSeek-V4-Flash |
| test      | 写测试、跑测试、分析覆盖率、回归验证         | MiniMax-M3 | DeepSeek-V4-Pro | DeepSeek-V4-Flash |
| audit     | 代码审查、安全审查、性能审查、结果复核       | MiniMax-M3 | DeepSeek-V4-Pro | DeepSeek-V4-Flash |

## 7.2 当前实现（`stage_config.STAGE_CONFIG`）

| Stage      | 职责（desc）         | 主模型            | 备用模型          |
| ---------- | -------------------- | ----------------- | ----------------- |
| brainstorm | 快速发散，低成本探索 | deepseek-v4-flash | MiniMax-M3        |
| decide     | 深度推理，权衡分析   | deepseek-v4-pro   | MiniMax-M3        |
| design     | 系统架构，方案设计   | MiniMax-M3        | deepseek-v4-pro   |
| plan       | 任务拆解，结构化输出 | MiniMax-M3        | deepseek-v4-pro   |
| implement  | 主力编码，工程实施   | MiniMax-M3        | deepseek-v4-flash |
| audit      | 严格检查，安全审计   | MiniMax-M3        | deepseek-v4-pro   |
| default    | 兜底默认             | MiniMax-M3        | deepseek-v4-flash |

## 7.3 差异清单

### D7-1 [DEVIATION-P1] 缺少 `explore` stage

- **文档要求**：stage `explore`（理解项目、读代码、追调用链、看日志、定位现状）
- **当前实现**：❌ 不存在
- **影响范围**：
  1. LLM 分类器 prompt 中已包含 "brainstorm / decide / design / plan / implement / audit / default" 7 个值（不包含 explore）
  2. `stage_detector.STAGE_KEYWORDS` 不包含"读代码 / 调用链 / 理解"等 explore 关键词
  3. `PATTERN_CONFIG.*.default_flow` 大量引用 `"explore"`（feature 没有但 bugfix / refactor / test / research / architecture / docs / audit 都引用了），实际会**静默失效**（proxy 收到不存在的 stage 名会 fallback 到 default）
- **建议修复**：
  ```python
  STAGE_CONFIG["explore"] = {
      "emoji":       "🔎",
      "label":       "探索理解",
      "desc":        "读代码、追调用链、看日志、定位现状",
      "model":       "MiniMax-M3",
      "base_url":    "https://api.minimaxi.com/anthropic",
      "api_key_env": "MINIMAX_API_KEY",
      "protocol":    "anthropic",
      "fb_model":       "deepseek-v4-pro",
      "fb_base_url":    "https://api.deepseek.com/anthropic",
      "fb_api_key_env": "DEEPSEEK_API_KEY",
      "fb_protocol":    "anthropic",
  }
  ```
- **风险等级**：**P1**（高频任务"读代码"会被错误路由到 default）

### D7-2 [DEVIATION-P1] 缺少 `test` stage

- **文档要求**：stage `test`（写测试、跑测试、分析覆盖率、回归验证）
- **当前实现**：❌ 不存在
- **影响范围**：
  1. LLM 分类器不输出 `test`（识别"跑测试"会落到 audit 或 default）
  2. `PATTERN_CONFIG.test.default_flow` 引用 `["explore", "test", "audit"]` 中的 `test` 失效
  3. 用户输入"帮我跑测试"无法获得 `test` stage
- **建议修复**：
  ```python
  STAGE_CONFIG["test"] = {
      "emoji":       "🧪",
      "label":       "测试验证",
      "desc":        "写测试、跑测试、分析覆盖率、回归验证",
      "model":       "MiniMax-M3",
      "base_url":    "https://api.minimaxi.com/anthropic",
      "api_key_env": "MINIMAX_API_KEY",
      "protocol":    "anthropic",
      "fb_model":       "deepseek-v4-pro",
      "fb_base_url":    "https://api.deepseek.com/anthropic",
      "fb_api_key_env": "DEEPSEEK_API_KEY",
      "fb_protocol":    "anthropic",
  }
  ```
- **风险等级**：**P1**（测试任务量大、识别失败会污染 audit 阶段统计）

### D7-3 [DEVIATION] 多了 `brainstorm` / `decide` / `default` 三个 stage

- **文档表格**：仅 6 个 stage，无 brainstorm / decide / default
- **当前实现**：多出 3 个 stage
- **设计溯源**：
  - `brainstorm` / `decide` 来自 §4.5 / §5 的"分类层输出 brainstorm/decide 维度" — 但 §7 Stage 表**未列出**
  - `default` 是实现侧的兜底值（LLM 分类器输出"无明显匹配"时使用）
- **影响**：
  1. LLM 分类器 prompt 把 brainstorm/decide 当成**合法 stage** — 这与 §7 Stage 表不一致
  2. `default` stage 永远不会从 LLM 输出（因为 LLM 被要求输出"无明显匹配"时打 "default" — 与 §7 表 6 个 stage 之外的 fallback 不一致）
- **建议修复**：
  - 方案 A：把 brainstorm/decide 视为 §4 的"分类维度"而非 §7 的"Stage"；LLM 输出后做映射（brainstorm → explore；decide → design）
  - 方案 B：在文档侧更新 §7 Stage 表，承认 brainstorm/decide 是业务扩展
  - 推荐方案 B（影响面小，与实现完全一致）
- **风险等级**：中（语义层不一致，但实际路由工作正常）

### D7-4 [DEVIATION] 默认模型策略不一致

- **文档要求**：6 个 stage **全部以 MiniMax-M3 为默认模型**
- **当前实现**：
  - `brainstorm`：默认 deepseek-v4-flash（不是 MiniMax-M3）
  - `decide`：默认 deepseek-v4-pro（不是 MiniMax-M3）
  - 其余 5 个：MiniMax-M3 ✅
- **设计溯源**：实现侧把 brainstorm 视为"低成本探索"、decide 视为"高阶推理"做了业务优化
- **建议修复**：
  - 方案 A（推荐）：在文档 §7 表中加一行说明 "brainstorm/decide 是 V1.2 扩展，默认模型根据任务性质定制"
  - 方案 B：把 brainstorm 默认改回 MiniMax-M3，备 deepseek-v4-flash
- **风险等级**：低（业务侧已经验证过 brainstorm 用 deepseek-v4-flash 更省成本）

## 7.4 验收结论

| 文档 Stage | 实现状态    |
| ---------- | ----------- |
| explore    | ❌ **缺失** |
| plan       | ✅ 存在     |
| design     | ✅ 存在     |
| implement  | ✅ 存在     |
| test       | ❌ **缺失** |
| audit      | ✅ 存在     |

**Stage 表覆盖率**：4/6 = 67%

## 7.5 修复优先级

1. **P0-紧急** — 补全 `explore` / `test` 两个 stage（影响"读代码"、"跑测试"两大高频任务路由）
2. **P1** — 文档侧更新 §7 Stage 表，明确 brainstorm / decide 是业务扩展

## 7.6 修复后预期

修复后 STAGE_CONFIG 应包含 9 个 stage：

```python
STAGE_CONFIG = {
    "explore":    {...},  # 新增
    "brainstorm": {...},
    "decide":     {...},
    "plan":       {...},
    "design":     {...},
    "implement":  {...},
    "test":       {...},  # 新增
    "audit":      {...},
    "default":    {...},
}
```

并同步修改：

- `llm_classifier` VALID_STAGES 增加 `explore` / `test`
- `stage_detector.STAGE_KEYWORDS` 补 explore/test 关键词
- LLM prompt 中提示分类器允许输出 explore / test

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
