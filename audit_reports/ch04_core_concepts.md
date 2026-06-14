# §4 核心概念定义 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 4 章（"核心概念定义"）
> 审计时间：2026-06-14
> 审计范围：Task Pattern / Stage / Stage Complexity / Workflow Strategy / Model Tier 在实现中的字段映射

---

## 4.1 设计文档要求

| 概念              | 设计文档定义                                                             |
| ----------------- | ------------------------------------------------------------------------ |
| Task Pattern      | feature / bugfix / refactor / test / audit / research / migration / docs |
| Stage             | explore / plan / design / implement / test / audit                       |
| Stage Complexity  | simple / medium / complex                                                |
| Workflow Strategy | 单模型 / 双模型 / 三模型                                                 |
| Model Tier        | MiniMax-M3 / DeepSeek-V4-Flash / DeepSeek-V4-Pro                         |

## 4.2 实现侧字段映射

| 概念              | 落地位置                                                   | 字段                                                                                                  |
| ----------------- | ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Task Pattern      | `stage_config.PATTERN_CONFIG`                              | 9 个 pattern：feature / bugfix / refactor / test / research / migration / architecture / docs / audit |
| Stage             | `stage_config.STAGE_CONFIG`                                | 7 个 stage：brainstorm / decide / design / plan / implement / audit / default                         |
| Stage Complexity  | `stage_config.COMPLEXITY_LEVELS` + `COMPLEXITY_THRESHOLDS` | simple(0-30) / medium(31-70) / complex(71-100)                                                        |
| Workflow Strategy | `proxy.build_workflow_plan()`                              | type: single / double / triple                                                                        |
| Model Tier        | `STAGE_CONFIG[*].model`                                    | MiniMax-M3 / deepseek-v4-pro / deepseek-v4-flash                                                      |

## 4.3 差异清单

### D4-1 [EXPECTED] Pattern 数量差异

- **设计文档**：8 个 pattern（feature / bugfix / refactor / test / audit / research / migration / docs）
- **当前实现**：9 个 pattern（在文档 8 个基础上新增 `architecture`）
- **结论**：EXPECTED。`architecture` 是实现侧业务扩展（专门处理"架构级任务"），与设计文档 §8 末尾"高阶决策任务"语义吻合。
- **后续影响**：无。proxy.py 走 Shadow Mode 暂不消费 pattern 字段。

### D4-2 [DEVIATION] Stage 数量与命名差异

- **设计文档**：6 个 stage（explore / plan / design / implement / test / audit）
- **当前实现**：7 个 stage（brainstorm / decide / design / plan / implement / audit / default）
- **差异点**：
  1. 实现侧**缺少** `explore` / `test` 两个文档要求的 stage
  2. 实现侧**多出** `brainstorm` / `decide` / `default` 三个扩展 stage
- **结论**：DEVIATION（见 §7 章节审计详细分析）
- **建议修复方向**：
  - 方案 A：补全 `explore` / `test` 两个 stage（删除 `default`，让"无匹配"自动落到 explore）
  - 方案 B：保留 brainstorm/decide 业务扩展，并新增 explore/test，把 `default` 改名
- **后续影响**：
  - LLM 分类器 prompt 仍按 brainstorm/decide 训练（影响 stage 字段输出）
  - stage_detector 关键词表缺 explore/test 关键词
  - PATTERN_CONFIG 中 `default_flow` 仍引用 `explore`（实际 stage 表里没有，会失效）
- **风险等级**：中（语义层缺口，但 Stage 路由主路径仍能工作；缺 explore 意味着"读代码、追调用链"任务被错误路由）

### D4-3 [DEVIATION] Model Tier 命名差异

- **设计文档**：`DeepSeek-V4-Flash` / `DeepSeek-V4-Pro`（大写 V）
- **当前实现**：`deepseek-v4-flash` / `deepseek-v4-pro`（小写 v）
- **结论**：DEVIATION（命名规范不一致），但**实际不影响路由**（MiniMax API 接受小写）。
- **建议修复**：保持小写，与 API 服务端实际 model name 一致；如要严格对齐文档，需要确认上游 API 是否接受大写 V。
- **风险等级**：低（仅文档/显示层）

## 4.4 验收结论

| 概念              | 状态    | 备注                                            |
| ----------------- | ------- | ----------------------------------------------- |
| Task Pattern      | ✅ PASS | 多出 architecture 是业务扩展                    |
| Stage             | ❌ FAIL | 缺 explore/test，多出 brainstorm/decide/default |
| Stage Complexity  | ✅ PASS | 阈值与文档完全对齐                              |
| Workflow Strategy | ✅ PASS | single/double/triple 已实现                     |
| Model Tier        | ⚠️ WARN | 命名大小写与文档不一致，但不影响功能            |

## 4.5 修复优先级

1. **P1** — §7 Stage 表补全 `explore` / `test`（影响"读代码"、"跑测试"两类高频任务）
2. **P2** — §4.3 Model Tier 命名规范化（影响跨系统对接）

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
