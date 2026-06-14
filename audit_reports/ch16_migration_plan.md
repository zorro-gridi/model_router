# §16 迁移实施方案 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 16 章（"迁移实施方案"）
> 审计时间：2026-06-14
> 审计范围：5 阶段迁移（阶段 A ~ E）的执行进度
> 历史参考：2026-06-14 commit a5b63ea "取消 op 操作的路由决策，注释掉相关代码并说明功能废弃的原因"

---

## 16.1 设计文档要求（5 阶段）

| 阶段       | 描述                                                                              | 状态                                                    |
| ---------- | --------------------------------------------------------------------------------- | ------------------------------------------------------- |
| **阶段 A** | 保留旧关键词路由，新增分类器影子模式（Shadow Mode），只记录不生效，用于对比正确率 | ✅ **已完成**                                           |
| **阶段 B** | 引入 Pattern 和 Complexity 但只在部分项目灰度启用                                 | ⚠️ **部分完成**（pattern 已在所有项目识别，灰度未实现） |
| **阶段 C** | 关闭旧 Op 覆盖逻辑，启用 Stage × Complexity 新路由                                | ✅ **已完成**（OPERATION_CONFIG = {}）                  |
| **阶段 D** | 上线 task templates 与 batch workflows，支持 feature/test/refactor 等标准流程     | ❌ **未完成**（batch 仅设置 pattern，未强制流程）       |
| **阶段 E** | 基于路由日志迭代训练提示词和规则，逐步提高自动化比率                              | ⚠️ **部分完成**（日志已升级，但 prompt 迭代未系统化）   |

## 16.2 阶段 A 验证

**设计要求**：保留旧关键词路由 + 新增分类器 Shadow Mode

**实现状态**：

- ✅ LLM 分类器已实现（`llm_classifier.py`，合并 stage + pattern + complexity）
- ✅ Shadow Mode：pattern 识别结果写入日志 + pattern\_<sid> 文件，**不进入路由决策**
- ✅ V1 关键词分类保留为 fallback（`stage_detector.STAGE_KEYWORDS`）
- ✅ 准确率统计未系统化（仅日志记录）

**差异**：

- D16-A-1 [EXPECTED] 准确率统计缺失 — 设计文档 §6.2 要求"Confusion Matrix"、"准确率 ≥ 90%" 评估，当前日志已记录但无统计脚本
- 建议：增加 `tools/eval_classifier.py` 计算 Shadow Mode 准确率

## 16.3 阶段 B 验证

**设计要求**：引入 Pattern 和 Complexity 但只在部分项目灰度启用

**实现状态**：

- ✅ Pattern 已在所有项目识别（无灰度开关）
- ✅ Complexity 已通过 `~careful` / `~quick` 手动调档
- ❌ **无灰度开关**（要么全开要么全关）

**差异**：

- D16-B-1 [DEVIATION] 灰度开关未实现 — 缺少 `state_index.json` 层面的 per-project pattern 启用开关
- 影响：无法按项目渐进式启用 pattern-driven routing
- 建议修复：在 `state_index.json` 增加 `"enable_pattern_routing": true/false` per-project
- 风险等级：P2

## 16.4 阶段 C 验证

**设计要求**：关闭旧 Op 覆盖逻辑，启用 Stage × Complexity 新路由

**实现状态**：

- ✅ OPERATION_CONFIG = {}（已废弃，理由记录在 stage_config.py:148-232 注释中）
- ✅ Stage 路由是主路径
- ✅ Complexity → workflow 规划已实现
- ✅ OPERATION_CONFIG 保留为注释块，便于回退

**差异**：

- D16-C-1 [PASS] 全部完成
- ✅ 阶段 C 100% 完成

## 16.5 阶段 D 验证

**设计要求**：上线 task templates 与 batch workflows，支持 feature/test/refactor 等标准流程

**实现状态**：

- ⚠️ PATTERN_CONFIG 9 个 pattern 的 default_flow 已定义
- ❌ **batch workflow 未真正启用**（`~batch <pattern>` 仅设置 pattern，不强制 stage = flow[0]）
- ❌ **无标准流程自动化**（用户需手动 `~stage <name>` 切换）

**差异**：

- D16-D-1 [DEVIATION] batch workflow 未实现（与 §5 D5-3 / §10 D10-2 / §12 D12-1 同根因）
- 风险等级：P1

## 16.6 阶段 E 验证

**设计要求**：基于路由日志迭代训练提示词和规则，逐步提高自动化比率

**实现状态**：

- ✅ `stage_router.log` 完整记录（升级了 session / pattern / complexity / stage 字段）
- ⚠️ **无 prompt 迭代系统**（仅每次手改）
- ⚠️ **无规则学习机制**（仅硬编码关键词）

**差异**：

- D16-E-1 [DEVIATION] prompt 迭代未系统化 — 无 `tools/optimize_prompts.py` 类工具
- 建议：
  - 增加 prompt 版本管理（`prompts/v1.json` / `prompts/v2.json`）
  - 增加离线评估脚本（用日志回放评估 prompt 改进）
- 风险等级：P2

## 16.7 验收结论

| 阶段             | 完成度 | 备注                              |
| ---------------- | ------ | --------------------------------- |
| A Shadow Mode    | 80%    | 缺准确率统计                      |
| B 灰度启用       | 50%    | pattern 全开，无灰度              |
| C 关闭旧 Op      | 100%   | ✅                                |
| D batch workflow | 20%    | pattern 表已定义，workflow 未启用 |
| E 持续迭代       | 30%    | 日志已升级，prompt 迭代无工具     |

**整体迁移进度**：(80 + 50 + 100 + 20 + 30) / 5 = **56%**

## 16.8 修复优先级

1. **P1** — D16-D-1 batch workflow 启用（与 §5 / §10 / §12 同根因）
2. **P2** — D16-B-1 灰度开关
3. **P2** — D16-A-1 准确率评估脚本
4. **P2** — D16-E-1 prompt 迭代工具链

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
