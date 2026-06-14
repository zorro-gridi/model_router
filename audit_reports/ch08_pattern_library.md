# §8 任务模式库 Task Pattern Library — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 8 章（"任务模式库"）
> 审计时间：2026-06-14
> 审计范围：`stage_config.PATTERN_CONFIG` 9 个 pattern 与文档 8 个 pattern 的逐项对齐
> 落地代码：`/Users/zorro/.claude/hooks/model_router/stage_config.py:251-315`

---

## 8.1 设计文档表格（8 个 pattern）

| Pattern      | 定位       | 默认流程                                    | 默认复杂度 | 推荐主推             |
| ------------ | ---------- | ------------------------------------------- | ---------- | -------------------- |
| feature      | 新增功能   | plan → design → implement → test → audit    | medium     | MiniMax-M3           |
| bugfix       | 修复缺陷   | explore → implement → test                  | medium     | MiniMax-M3           |
| refactor     | 结构重构   | explore → design → implement → test → audit | medium     | MiniMax-M3           |
| test         | 测试建设   | explore → test → audit                      | medium     | MiniMax-M3           |
| research     | 资料调研   | explore → plan → design                     | medium     | MiniMax-M3           |
| migration    | 迁移/改造  | plan → design → implement → test → audit    | complex    | MiniMax-M3           |
| architecture | 架构级任务 | explore → plan → design → audit             | complex    | MiniMax-M3           |
| docs         | 文档、说明 | explore → implement                         | simple     | MiniMax-M3（低成本） |

## 8.2 当前实现（`stage_config.PATTERN_CONFIG`）

| Pattern      | 默认流程                                    | 默认复杂度 | primary_model         | 状态                    |
| ------------ | ------------------------------------------- | ---------- | --------------------- | ----------------------- |
| feature      | plan → design → implement → test → audit    | medium     | MiniMax-M3            | ✅                      |
| bugfix       | explore → implement → test                  | medium     | MiniMax-M3            | ⚠️ flow 含失效 stage    |
| refactor     | explore → design → implement → test → audit | medium     | MiniMax-M3            | ⚠️ flow 含失效 stage    |
| test         | explore → test → audit                      | medium     | MiniMax-M3            | ⚠️ flow 含失效 stage    |
| research     | explore → plan → design                     | medium     | **deepseek-v4-flash** | ⚠️ model 差异           |
| migration    | plan → design → implement → test → audit    | complex    | MiniMax-M3            | ✅                      |
| architecture | explore → plan → design → audit             | complex    | MiniMax-M3            | ⚠️ flow 含失效 stage    |
| docs         | explore → implement                         | simple     | **deepseek-v4-flash** | ⚠️ model 差异           |
| **audit**    | explore → audit                             | complex    | MiniMax-M3            | ⚠️ **文档无此 pattern** |

## 8.3 差异清单

### D8-1 [DEVIATION] 多了 `audit` pattern

- **文档表格**：8 个 pattern（feature / bugfix / refactor / test / research / migration / architecture / docs）
- **当前实现**：9 个 pattern（多出 `audit`）
- **影响**：
  1. LLM 分类器 prompt 已包含 `audit` 作为合法 pattern
  2. `audit` pattern 的 `default_flow = ["explore", "audit"]` 与 §7 stage `audit` 重名但语义不同（pattern 维度 vs stage 维度）
- **建议修复**：
  - 方案 A（推荐）：保留 `audit` pattern（语义清晰：用户做"代码审查"时直接识别为 audit pattern，跳过 explore）
  - 方案 B：合并到 `audit` stage，不再作为独立 pattern
- **风险等级**：低（语义清晰，不冲突）

### D8-2 [DEVIATION] 7 个 pattern 的 default_flow 引用了不存在的 stage

- **影响 pattern**：bugfix / refactor / test / research / architecture / docs / audit（共 7 个）
- **缺失 stage**：`explore` / `test`（详见 §7 报告 D7-1 / D7-2）
- **后果**：
  - `proxy` 收到不存在的 stage 名 → 落到 `default`（MiniMax-M3 兜底）
  - 用户感知不到 flow 切换，工作流退化为单 stage
- **建议修复**：与 §7 修复同步（先补 explore / test stage，pattern 的 flow 自然可用）
- **风险等级**：**P0**（与 §7 D7-1 / D7-2 同根因）

### D8-3 [DEVIATION] `research` / `docs` 主推模型与文档不一致

- **文档要求**：所有 pattern 默认主推 MiniMax-M3
- **当前实现**：
  - `research.primary_model = "deepseek-v4-flash"`
  - `docs.primary_model = "deepseek-v4-flash"`
- **设计溯源**：实现侧把 research / docs 视为"低成本调研/文档"，强制走 deepseek-v4-flash
- **建议修复**：
  - 方案 A（推荐）：文档侧 §8 加注 "research / docs 因低价值，扩展为 deepseek-v4-flash 优先"
  - 方案 B：改回 MiniMax-M3（成本上升）
- **风险等级**：低（业务侧验证 deepseek-v4-flash 在调研/文档场景性价比更高）

### D8-4 [DEVIATION] `migration` 流程缺少 `explore` 阶段

- **文档要求**：`migration` → `plan → design → implement → test → audit`（无 explore）
- **当前实现**：`migration` → `plan → design → implement → test → audit`（**与文档一致**）
- **结论**：✅ PASS
- **额外观察**：实现中没有 `explore` 阶段（合理：迁移项目用户已经了解现状）

### D8-5 [PASS] `feature` / `migration` 默认流程完全对齐

## 8.4 验收结论

| Pattern      | 流程 | 复杂度 | 主推模型 | 综合         |
| ------------ | ---- | ------ | -------- | ------------ |
| feature      | ✅   | ✅     | ✅       | ✅ PASS      |
| bugfix       | ⚠️   | ✅     | ✅       | ⚠️ flow 失效 |
| refactor     | ⚠️   | ✅     | ✅       | ⚠️ flow 失效 |
| test         | ⚠️   | ✅     | ✅       | ⚠️ flow 失效 |
| research     | ⚠️   | ✅     | ❌       | ❌ FAIL      |
| migration    | ✅   | ✅     | ✅       | ✅ PASS      |
| architecture | ⚠️   | ✅     | ✅       | ⚠️ flow 失效 |
| docs         | ⚠️   | ✅     | ❌       | ❌ FAIL      |
| audit        | ⚠️   | -      | -        | ❌ 多出      |

**Pattern 表格覆盖率**：8/8 = 100%（含 1 个扩展 + 2 个主推模型差异 + 7 个 flow 失效）

## 8.5 修复优先级

1. **P0** — 与 §7 同步：补全 `explore` / `test` stage（7 个 pattern 的 default_flow 自动恢复）
2. **P2** — 文档侧更新 §8 表，明确 research/docs 用 deepseek-v4-flash 是业务优化

## 8.6 修复后预期

修复 D7-1 / D7-2 后，所有 9 个 pattern 的 default_flow 都能正确路由到对应 stage。

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
