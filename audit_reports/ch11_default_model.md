# §11 默认模型策略 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 11 章（"默认模型策略"）
> 审计时间：2026-06-14
> 审计范围：`stage_config.STAGE_CONFIG` 7 个 stage 的 model 字段 + MODEL_TO_CONFIG 反向索引与文档三条策略对齐
> 落地代码：`/Users/zorro/.claude/hooks/model_router/stage_config.py:48-145`

---

## 11.1 设计文档要求（三条策略）

1. **MiniMax-M3 作为默认基线模型** — 承担大多数 simple/medium/执行密集任务
2. **DeepSeek-V4-Flash 作为快速低成本兜底** — 适合低价值、临时性、快速确认类任务
3. **DeepSeek-V4-Pro 作为高阶推理模型** — 只在规划、复杂审计、关键决策、复杂故障分析中启用
4. **禁止**把高阶模型设为无条件默认（否则破坏降本目标）

## 11.2 当前实现（按 stage 维度）

| Stage      | 主模型            | 备用模型          | 文档角色匹配             | 备注                       |
| ---------- | ----------------- | ----------------- | ------------------------ | -------------------------- |
| brainstorm | deepseek-v4-flash | MiniMax-M3        | ✅ 符合"低成本探索"      | 主=flash（兜底模型被前置） |
| decide     | deepseek-v4-pro   | MiniMax-M3        | ✅ 符合"高阶推理"        | 主=pro（高阶模型被前置）   |
| design     | MiniMax-M3        | deepseek-v4-pro   | ✅ 符合"基线 + 高阶备用" | 经典配置                   |
| plan       | MiniMax-M3        | deepseek-v4-pro   | ✅ 符合"基线 + 高阶备用" | 经典配置                   |
| implement  | MiniMax-M3        | deepseek-v4-flash | ✅ 符合"基线 + 快速兜底" | 经典配置                   |
| audit      | MiniMax-M3        | deepseek-v4-pro   | ✅ 符合"基线 + 高阶备用" | 经典配置                   |
| default    | MiniMax-M3        | deepseek-v4-flash | ✅ 符合"基线 + 快速兜底" | 经典配置                   |

**MODEL_TO_CONFIG 反向索引**（stage_config.py:457-467）：从 STAGE_CONFIG + OPERATION_CONFIG 的主/备模型收集，用于 sticky fallback 反查。

## 11.3 差异清单

### D11-1 [DEVIATION] `brainstorm` / `decide` 主模型违反"基线模型 = MiniMax-M3"原则

- **文档 §11 P1**："MiniMax-M3 作为默认基线模型，承担大多数 simple/medium/执行密集任务"
- **当前实现**：
  - `brainstorm.model = "deepseek-v4-flash"`（不是 MiniMax-M3）
  - `decide.model = "deepseek-v4-pro"`（不是 MiniMax-M3）
- **设计溯源**：实现侧把 brainstorm 视为"低成本探索"、decide 视为"高阶推理"做了业务优化
- **建议修复**：
  - 方案 A（推荐）：文档 §11 加注 "brainstorm/decide 是 V1.2 扩展，默认模型根据任务性质定制"
  - 方案 B：把 brainstorm 主模型改回 MiniMax-M3，备 flash
  - 方案 C：把 decide 主模型改回 MiniMax-M3，备 pro
- **风险等级**：低（业务侧已验证 brainstorm 用 flash / decide 用 pro 更经济；属"业务优化"）

### D11-2 [DEVIATION] `implement` stage 备用模型用 flash，complex workflow 失效（与 §10 D10-5 同根因）

- **文档 §11 P3**："DeepSeek-V4-Pro 作为高阶推理模型，只在规划、复杂审计、关键决策、复杂故障分析中启用"
- **当前实现**：
  - `implement.fb_model = "deepseek-v4-flash"`（不是 deepseek-v4-pro）
  - 后果：implement complex workflow = `[flash, M3, flash]`，**强模型从未出现**
- **建议修复**：在 `stage_config.py` 新增独立 `STRONG_MODEL` 常量，workflow 显式引用
- **风险等级**：P1（与 §10 D10-5 同根因）

### D11-3 [EXPECTED] `OPERATION_CONFIG` 为空（已废弃）

- **文档 §11**：未涉及 OPERATION_CONFIG
- **当前实现**：OPERATION_CONFIG = {}（已废弃，理由记录在 stage_config.py:148-232 注释中）
- **结论**：✅ EXPECTED（业务侧主动废弃）
- **理由**：write/read/search/refactor 是"动作"而非"任务属性"，与 §11 "任务级模型策略"不匹配

### D11-4 [PASS] 反向索引 MODEL_TO_CONFIG

- **文档 §11**：未涉及
- **当前实现**：✅ MODEL_TO_CONFIG 收集主 + 备模型，反查路由配置
- **结论**：PASS（基础设施类）

### D11-5 [DEVIATION] 模型命名大小写不一致（与 §4 D4-3 同根因）

- **文档 §11**：`DeepSeek-V4-Flash` / `DeepSeek-V4-Pro`（大写 V）
- **当前实现**：`deepseek-v4-flash` / `deepseek-v4-pro`（小写 v）
- **结论**：⚠️ 文档/显示层不一致
- **风险等级**：低

## 11.4 验收结论

| 文档要求                                 | 状态                                        |
| ---------------------------------------- | ------------------------------------------- |
| MiniMax-M3 作为默认基线                  | ⚠️ 5/7 stage 是基线；brainstorm/decide 例外 |
| DeepSeek-V4-Flash 作为低成本兜底         | ✅ PASS（brainstorm/impl/default 的 fb）    |
| DeepSeek-V4-Pro 作为高阶推理（按需启用） | ⚠️ 仅 design/plan/audit/think 备用          |
| 禁止高阶模型无条件默认                   | ✅ PASS（仅 decide 例外）                   |

## 11.5 修复优先级

1. **P1** — D11-2 / D10-5：新增独立 `STRONG_MODEL` 常量，workflow 显式引用
2. **P3** — D11-1 / D11-5：文档侧更新 §11 表，明确 brainstorm/decide 的特殊策略

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
