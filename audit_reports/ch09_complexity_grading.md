# §9 复杂度分级规范 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 9 章（"复杂度分级规范"）
> 审计时间：2026-06-14
> 审计范围：`stage_detector.detect_complexity` + `stage_config.COMPLEXITY_LEVELS/THRESHOLDS` 与文档三档分级对齐
> 落地代码：
>
> - `/Users/zorro/.claude/hooks/model_router/stage_config.py:328-333` (COMPLEXITY_LEVELS/THRESHOLDS)
> - `/Users/zorro/.claude/hooks/model_router/stage_detector.py:998-1110` (detect_complexity)

---

## 9.1 设计文档要求

| 等级    | 分数   | 判定依据                                       | 推荐策略                               |
| ------- | ------ | ---------------------------------------------- | -------------------------------------- |
| simple  | 0~30   | 单文件、单步骤、需求明确、低风险、无跨模块依赖 | 直接由 MiniMax-M3 处理                 |
| medium  | 31~70  | 多步骤、轻度设计、跨少量文件、需要一定权衡     | 强模型给方案，常规模型执行             |
| complex | 71~100 | 跨模块/跨系统/高风险/需要审计或多轮验证        | 强模型规划 + 常规模型执行 + 强模型审计 |

**设计文档原则**：复杂度必须基于**当前阶段**来判断，不是整个任务的一次性静态标签。

## 9.2 当前实现

### 9.2.1 阈值配置（`stage_config.py`）

```python
COMPLEXITY_LEVELS = ("simple", "medium", "complex")
COMPLEXITY_THRESHOLDS = {
    "simple":  30,
    "medium":  70,
    "complex": 100,
}
```

✅ **完全对齐**：阈值 30 / 70 / 100 与文档一致。

### 9.2.2 评分逻辑（`stage_detector.detect_complexity`）

**实现思路（V1 启发式）**：

1. 基础分：若已识别 pattern，从 `PATTERN_BASE_SCORE` 起步；否则 medium=50
2. 关键词加权：扫描 `COMPLEXITY_KEYWORDS` 累加权重
3. 长度加成：>200 字加 5，>500 字加 10
4. "多个 / X 个" 加成 +5
5. 夹紧到 [0, 100]
6. confidence：信号数 0 → 0.3，≥3 → 0.85

**Pattern 基础分**：
| Pattern | 基础分 | 预期标签 | 文档默认复杂度 |
| --- | --- | --- | --- |
| feature | 50 | medium | medium ✅ |
| bugfix | 45 | medium | medium ✅ |
| refactor | 55 | medium | medium ✅ |
| test | 40 | medium | medium ✅ |
| research | 50 | medium | medium ✅ |
| migration | 75 | complex | complex ✅ |
| architecture | 80 | complex | complex ✅ |
| docs | 20 | simple | simple ✅ |
| audit | 70 | medium | —（文档未列） |

## 9.3 差异清单

### D9-1 [DEVIATION] 复杂度评估**未基于"当前 stage"**（设计原则违反）

- **文档原则（第 9 章第 252 行）**："复杂度必须基于当前阶段来判断，不是整个任务的一次性静态标签"
- **当前实现**：`detect_complexity(prompt, pattern)` 只接收 `prompt` 和 `pattern`，**不接收 stage**
- **后果**：
  - 同一个 prompt 在 explore / implement / audit 三个 stage 下评分相同
  - 违反"当前阶段感知"的复杂度评估
  - 但当前 `proxy.build_workflow_plan` 已经用 stage 修正 workflow 类型，所以"实际路由"未出错
- **建议修复**：
  ```python
  def detect_complexity(prompt, pattern=None, stage=None):
      base_score = PATTERN_BASE_SCORE.get(pattern, 50)
      # stage 加权（设计原则：基于当前阶段）
      stage_multiplier = {
          "explore": 0.7,    # 探索阶段通常简单
          "brainstorm": 0.8,
          "implement": 1.0,
          "design": 1.2,     # 设计阶段通常复杂
          "plan": 1.1,
          "audit": 1.3,      # 审计阶段通常复杂
          "decide": 1.1,
      }.get(stage, 1.0)
      score = base_score * stage_multiplier
      ...
  ```
- **风险等级**：P2（影响评分准确性，但不影响路由主路径）

### D9-2 [DEVIATION] `audit` pattern 基础分 70 映射到 medium 而非 complex

- **当前实现**：`audit` pattern base=70 → 触发 medium/complex 边界（≤70 = medium）
- **设计预期**：audit pattern 在 §8 中默认复杂度为 complex（但 §8 表实际未列出 audit，**审计 pattern 是实现侧扩展**）
- **建议修复**：
  - 方案 A：把 `audit` base 从 70 改到 75（确保落到 complex）
  - 方案 B：保留现状（边界值不重要）
- **风险等级**：P3（边界值，影响小）

### D9-3 [DEVIATION] 关键词权重硬编码（违反 §14 配置单源化）

- **文档 §14**：所有路由配置集中于 `stage_config.py`，**不允许硬编码在多个模块中**
- **当前实现**：`COMPLEXITY_KEYWORDS` 列表（共 30+ 条）硬编码在 `stage_detector.py:1011-1027`
- **建议修复**：迁移到 `stage_config.COMPLEXITY_KEYWORDS`，proxy / detector 共享
- **风险等级**：P2（违反单源化原则）

### D9-4 [DEVIATION] 长度加成阈值固定

- **文档**：无明确数值
- **当前实现**：>200 字 +5，>500 字 +10
- **影响**：长 prompt 不一定复杂（可能是用户附带的代码上下文）
- **建议修复**：把"长度加成"改为"内容多样性加成"（如代码块数量、关键词密度）
- **风险等级**：P3（粗略启发式可用）

### D9-5 [EXPECTED] V1 启发式限制

- **当前实现**：基于关键词 + 长度 + pattern 启发式评分
- **设计预期**：V1 启发式 → V2 LLM 分类器（§6.4）
- **结论**：✅ 与 §6.4 设计阶段一致
- **后续**：LLM 分类器已实现（`llm_classifier.classify`），可作为 V2 替换

## 9.4 验收结论

| 项目                     | 状态                                    |
| ------------------------ | --------------------------------------- |
| 阈值 30/70/100           | ✅ PASS                                 |
| Pattern 基础分与文档对齐 | ✅ PASS（audit 边界值 P3）              |
| 推荐策略映射到 workflow  | ✅ PASS（`build_workflow_plan` 已实现） |
| 基于当前 stage 评估      | ❌ FAIL（D9-1）                         |
| 关键词权重单源化         | ❌ FAIL（D9-3）                         |

## 9.5 修复优先级

1. **P2** — D9-1 让 `detect_complexity` 接收 stage 参数
2. **P2** — D9-3 关键词列表迁移到 `stage_config.py`
3. **P3** — D9-2 `audit` base 从 70 调到 75

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
