# §10 路由决策算法规范 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 10 章（"路由决策算法规范"）
> 审计时间：2026-06-14
> 审计范围：决策顺序与算法伪代码在 `proxy.py` 中的实现一致性
> 落地代码：`/Users/zorro/.claude/hooks/model_router/proxy.py:984-1227` (do_POST)

---

## 10.1 设计文档要求

**决策顺序（8 步）**：

1. 检查 model override
2. 检查 batch / template 强制流程
3. 识别 task pattern
4. 识别当前 stage
5. 评估当前 stage complexity
6. 生成 workflow plan
7. 路由到具体模型
8. 执行 sticky fallback 与重试策略

**算法伪代码**：

```python
if model_override:
    return override_model
pattern = classify_pattern(context)
stage = classify_stage(context, pattern)
complexity = classify_complexity(context, pattern, stage)
plan = build_workflow(pattern, stage, complexity)
if plan.type == "single":
    model = select_single_model(stage, complexity)
elif plan.type == "double":
    model_seq = [strong_model, normal_model]
elif plan.type == "triple":
    model_seq = [strong_model, normal_model, strong_model]
return execute(plan, model_seq)
```

## 10.2 当前实现（`proxy.do_POST`）

实际执行顺序（proxy.py 第 984-1227 行）：

```python
def do_POST(self):
    # 1. 解析 body / 提取 prompt
    body = parse_request_body()
    prompt = extract_prompt(body)

    # 2. P1: prompt 中的 ~model / ~m / ~stage / ~careful / ~quick / ~batch / ~reset
    prompt_model_override = parse_prompt_overrides(prompt)

    # 3. P2: 持久化的 model override（state_index.json）
    model_override = state_index.get_model_override(project_root)

    # 4. P3: op 路由（OPERATION_CONFIG — 已废弃为空）
    op_model = OPERATION_MODELS.get(detected_op)  # 实际总是不命中

    # 5. P4+P5: stage 路由 + workflow 规划
    stage = detect_stage(prompt)
    complexity = detect_complexity(prompt, pattern=detected_pattern)
    workflow = build_workflow_plan(stage, is_simple, primary, strong, complexity)

    # 6. P6: sticky fallback（上一轮 fallback 写入的 sticky 状态）
    if sticky_fb_active:
        model = sticky_fb

    # 7. 执行
    response = call_upstream(model, prompt)

    # 8. Sticky fallback 写入
    if retriable_error(response):
        fallback_model = STAGE_CONFIG[stage].fb_model
        write_sticky(stage, fallback_model)
        response = call_upstream(fallback_model, prompt)
```

## 10.3 差异清单

### D10-1 [DEVIATION] 决策层数 7 vs 文档 8

- **文档 8 步**：1.override → 2.batch → 3.pattern → 4.stage → 5.complexity → 6.plan → 7.route → 8.sticky_fallback
- **实现 7 步**：1.prompt_override → 2.model_override → 3.op → 4.stage(+complexity) → 5.workflow → 6.sticky_fb → 7.fallback
- **差异点**：
  - 实现把"override" 拆成 prompt + 持久化双层
  - 实现把"complexity" 合并到 stage 路由（5 步合一）
  - 实现保留 op 层（与 P2 batch 重叠）
- **结论**：⚠️ DEVIATION（粒度不同），**逻辑等价**
- **建议修复**：在文档侧更新为 7 层实现模型（或在 `stage_router.log` 中加文档对齐的逻辑层级标签）
- **风险等级**：低

### D10-2 [DEVIATION] P2 batch / template 强制流程未实现（与 §5 D5-3 同根因）

- **文档 P2**：强制流程覆盖（~batch 跳过 stage 分类）
- **当前实现**：`~batch <pattern>` 仅设置 pattern，不强制 stage = pattern.default_flow[0]
- **影响**：用户 `~batch feature` 不会直接跳到 plan stage
- **建议修复**：
  ```python
  if prompt_batch := parse_batch(prompt):
      target_pattern = prompt_batch  # e.g. "feature"
      if target_pattern in PATTERN_CONFIG:
          # 强制 stage 到流程第一步
          forced_stage = PATTERN_CONFIG[target_pattern]["default_flow"][0]
          stage = forced_stage
  ```
- **风险等级**：P1（用户对"批量任务"语义的预期落空）

### D10-3 [EXPECTED] Pattern 步骤未影响路由

- **文档步骤 3**："识别 task pattern"
- **当前实现**：识别 pattern（关键词 + LLM）→ 写入 pattern\_<sid> 文件 + 日志 → **不进入路由决策**
- **结论**：✅ 与 §6.2 Shadow Mode 规范一致
- **后续**：阶段 B 启用

### D10-4 [DEVIATION] `select_single_model(stage, complexity)` 未独立实现

- **文档算法**：complexity 决定 single/double/triple
- **当前实现**：
  - `build_workflow_plan(stage, is_simple, primary, strong, complexity)` 接收 complexity
  - 但**实际** plan 类型由 `is_simple` 参数决定（来自 stage_detector 的 simple/medium/complex 标签），**与 complexity_score 100 分制脱钩**
- **差异点**：
  - 文档：complexity 评分 0-100 → label 映射 → workflow 类型
  - 实现：is_simple boolean → workflow 类型（丢失了 medium/complex 的精细区分）
- **影响**：
  - 同一 stage 下 `is_simple=True` 走 single，`is_simple=False` 走 double（无视 medium / complex 区分）
  - triple workflow（规划+执行+审计）**几乎不走**（仅当 `complexity == "complex"` 显式调用时才走）
- **建议修复**：
  ```python
  def build_workflow_plan(stage, complexity_label, primary, strong):
      if complexity_label == "simple":
          type_ = "single"
      elif complexity_label == "medium":
          type_ = "double"
      else:  # complex
          type_ = "triple"
  ```
- **风险等级**：**P1**（文档要求"复杂任务三步走"是核心能力，当前几乎未触发）

### D10-5 [DEVIATION] `strong_model` / `normal_model` 选择硬编码

- **文档算法伪代码**：`model_seq = [strong_model, normal_model]`
- **当前实现**：
  ```python
  # proxy.py 第 560-585 行
  def build_workflow_plan(stage, is_simple, primary, strong, complexity):
      if complexity == "simple":
          models = [primary]  # 仅 primary
      elif complexity == "medium":
          models = [strong, primary]  # 强 + 常规
      else:  # complex
          models = [strong, primary, strong]  # 强 + 常规 + 强
  ```

  - `primary` = STAGE_CONFIG[stage]["model"]
  - `strong` = STAGE_CONFIG[stage]["fb_model"]（**备用模型被当作强模型用**）
- **差异点**：
  - 文档隐含 `strong_model` 是独立于 stage 配置的全局概念（如"DeepSeek-V4-Pro"）
  - 实现把"备用模型"等同于"强模型" — 巧合上 `design/plan/audit` 三个 stage 的 fb 确实是 deepseek-v4-pro（强模型），**但 implement 的 fb 是 deepseek-v4-flash**（弱模型）
- **后果**：
  - implement stage 的 complex workflow = `[deepseek-v4-flash, MiniMax-M3, deepseek-v4-flash]` — **强模型没有出现**
  - 这与文档"强模型规划 + 常规模型执行 + 强模型审计"不一致
- **建议修复**：
  - 方案 A：在 `stage_config.py` 新增 `STRONG_MODEL = "deepseek-v4-pro"`，workflow 显式引用
  - 方案 B：在 STAGE_CONFIG 中新增 `strong_model` 字段（与 `model` / `fb_model` 并列）
- **风险等级**：**P1**（implement 复杂任务的 workflow 走了弱模型，违反"复杂任务用强模型"的设计原则）

### D10-6 [PASS] sticky fallback 行为

- **文档**：执行 sticky fallback 与重试策略
- **实现**：✅ 5xx 错误 → fb_model → 写 sticky → 下次直接用 sticky
- **结论**：PASS

### D10-7 [EXPECTED] Complexity 评估失败时 fallback

- **文档**：无明确
- **实现**：当 `detect_complexity` 抛异常时，`stage_detector` 回退到 V1 关键词启发式
- **结论**：✅ 健壮性 OK

## 10.4 验收结论

| 文档步骤                     | 实现状态                          |
| ---------------------------- | --------------------------------- |
| 1. Model override            | ✅ PASS（双层都生效）             |
| 2. Batch / template 强制流程 | ❌ FAIL（D10-2）                  |
| 3. Pattern 识别              | ✅ PASS（Shadow）                 |
| 4. Stage 识别                | ✅ PASS                           |
| 5. Complexity 评估           | ⚠️ 未基于 stage（D9-1）           |
| 6. Workflow plan             | ⚠️ 走 is_simple 简化路径（D10-4） |
| 7. 路由到模型                | ⚠️ strong_model 选择错误（D10-5） |
| 8. Sticky fallback           | ✅ PASS                           |

## 10.5 修复优先级

1. **P0** — D10-5 strong_model 选择：让 complex workflow 真正用强模型（影响核心价值）
2. **P1** — D10-2 ~batch 强制流程起点
3. **P1** — D10-4 workflow 类型由 complexity_label 决定（不是 is_simple）
4. **P2** — D10-1 文档更新为 7 层实现

## 10.6 修复后预期

修复后 `build_workflow_plan` 应支持：

```python
build_workflow_plan(stage="implement", complexity="complex", ...)
# → type=triple, models=[strong_model, normal_model, strong_model]
# strong_model 始终是 deepseek-v4-pro（与 stage 无关）
# normal_model 始终是 MiniMax-M3（与 stage 无关）
```

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
