# §5 路由决策总原则 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 5 章（"路由决策总原则"）
> 审计时间：2026-06-14
> 审计范围：6 层路由优先级在 `proxy.py` 中的实现顺序

---

## 5.1 设计文档要求（优先级 1~6）

| 优先级 | 决策因子            | 文档原文                          |
| ------ | ------------------- | --------------------------------- |
| P1     | 人工模型覆盖        | `~model / ~m`                     |
| P2     | 强制流程覆盖        | batch 模式 / 项目模板覆盖         |
| P3     | 任务模式（Pattern） | 选择默认流程                      |
| P4     | Stage 确认          | 当前工作位置                      |
| P5     | Stage Complexity    | 决定是否升级为多阶段工作流        |
| P6     | 模型成本与可用性    | sticky fallback / 限流 / 故障转移 |

## 5.2 实现侧决策顺序（`proxy.py` `do_POST` 中）

实际按以下顺序短路（找到第一项就 return）：

```
1. prompt_model_override       （在 prompt 中显式 ~model / ~m / ~reset 等）
2. model_override              （state_index.json 持久化的 model override）
3. op                          （OPERATION_CONFIG 路由 — 已废弃为空 dict）
4. stage                       （STAGE_CONFIG 路由 + sticky fallback 路径）
5. workflow                    （build_workflow_plan → multi-model sequence）
6. sticky_fb                   （上一轮 fallback 写入的 sticky 状态）
7. fallback                    （最终兜底到 STAGE_CONFIG.fb_model）
```

## 5.3 差异清单

### D5-1 [DEVIATION] 实际实现 7 层 vs 文档 6 层

- **文档定义**：6 层（model_override / batch / pattern / stage / complexity / sticky_fallback）
- **实现层数**：7 层
- **差异点**：
  1. 实现把 model_override 拆成 `prompt_model_override`（最高）+ `model_override`（持久化）
  2. 实现保留了 `op` 层（文档 P3 实质上替代了 op，但代码层没删除）
  3. `workflow` 是文档 P5 "Stage Complexity 决定多阶段" 的展开
  4. `sticky_fb` + `fallback` 实际是 P6 的细化
- **结论**：DEVIATION（拆分粒度不同），但**逻辑等价**。
- **建议修复**：
  - 在 `stage_router.log` 中按"逻辑层级"打标（prompt_override/model_override/op/stage/workflow/fallback），而不是按"代码分支"打标
  - 文档侧更新为 7 层实现模型，标注与 6 层的映射关系
- **风险等级**：低（不影响决策正确性，只影响日志可读性）

### D5-2 [EXPECTED] P3 Pattern 优先级未实际消费

- **文档 P3**：任务模式（Pattern）选择默认流程
- **当前实现**：`proxy.py` 读取 pattern，但**未用 pattern 影响路由决策**（Shadow Mode）
- **结论**：EXPECTED（与 §6.2 Shadow Mode 规范一致）
- **后续影响**：pattern 仅写入日志 + pattern\_<sid> 文件，不进路由
- **修复时机**：阶段 B（pattern 准确率 ≥ 90% 连续 7 天稳定）

### D5-3 [DEVIATION] P2 batch / template 流程覆盖未实现

- **文档 P2**：强制流程覆盖（batch 模式 / 项目模板覆盖）
- **当前实现**：~batch 指令**只设置 pattern**，不直接强制流程
- **差异点**：
  - 用户输入 `~batch feature`，proxy 把 pattern 设为 `feature`，但路由仍走 stage → workflow，不强制跳到 `feature.default_flow[0]`
  - 文档原意应该是 `~batch` 直接跳到流程第一步，绕过 stage 分类
- **结论**：DEVIATION（功能未完全实现）
- **建议修复**：在 proxy.py 中加一个 `if batch_template: stage = PATTERN_CONFIG[batch_template].default_flow[0]`
- **风险等级**：中（与文档语义不一致，~batch 当前等价于 `~stage <first>` 的弱化版）

## 5.4 验收结论

| 优先级                   | 状态      | 备注                           |
| ------------------------ | --------- | ------------------------------ |
| P1 模型覆盖              | ✅ PASS   | prompt + 持久化双层都生效      |
| P2 batch 覆盖            | ❌ FAIL   | 仅设置 pattern，不强制流程起点 |
| P3 Pattern 路由          | ⚠️ SHADOW | 按设计预期未消费               |
| P4 Stage 路由            | ✅ PASS   | 正常工作                       |
| P5 Complexity → workflow | ✅ PASS   | `build_workflow_plan` 已实现   |
| P6 sticky_fb + 故障转移  | ✅ PASS   | 5xx → fb → sticky 状态写入正常 |

## 5.5 修复优先级

1. **P1** — D5-3 ~batch 强制流程起点（影响用户对"批量任务"的预期）

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
