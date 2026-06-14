# V1.2 设计文档审计报告索引

> 审计依据：`/Users/zorro/.claude/hooks/model_router/智能模型路由插件系统_功能升级实施详细设计方案_V1.2.docx`
> 审计时间：2026-06-14
> 审计范围：V1.2 文档全部 19 章
> 审计目录：`/Users/zorro/.claude/hooks/model_router/audit_reports/`

---

## 报告清单

| 章节                    | 报告                                                           | 状态                      |
| ----------------------- | -------------------------------------------------------------- | ------------------------- |
| §1-2                    | （概念性章节，无独立报告）                                     | ✅                        |
| §3                      | （合并到 §4 报告）                                             | ✅                        |
| §4 核心概念             | [ch04_core_concepts.md](./ch04_core_concepts.md)               | ❌ 缺 explore/test stage  |
| §4.5 Context Compressor | （合并到 §6 报告）                                             | ✅                        |
| §5 路由决策             | [ch05_routing_priority.md](./ch05_routing_priority.md)         | ❌ ~batch 未强制流程      |
| §6 功能模块             | [ch06_function_modules.md](./ch06_function_modules.md)         | ⚠️ PARTIAL                |
| §7 Stage 体系           | [ch07_stage_system.md](./ch07_stage_system.md)                 | ❌ **P0** 缺 explore/test |
| §8 Pattern Library      | [ch08_pattern_library.md](./ch08_pattern_library.md)           | ⚠️ flow 失效              |
| §9 复杂度分级           | [ch09_complexity_grading.md](./ch09_complexity_grading.md)     | ⚠️ 未基于 stage           |
| §10 路由算法            | [ch10_routing_algorithm.md](./ch10_routing_algorithm.md)       | ❌ strong_model 错误      |
| §11 默认模型            | [ch11_default_model.md](./ch11_default_model.md)               | ⚠️ brainstorm/decide 例外 |
| §12 手动指令            | [ch12_manual_commands.md](./ch12_manual_commands.md)           | ❌ ~batch 不强制          |
| §13 状态文件            | [ch13_state_files.md](./ch13_state_files.md)                   | ⚠️ Level 3 未实现         |
| §14 配置单源化          | [ch14_single_source_config.md](./ch14_single_source_config.md) | ⚠️ 关键词表分散           |
| §15 可观测性            | [ch15_observability.md](./ch15_observability.md)               | ⚠️ 缺部分指标             |
| §16 迁移实施            | [ch16_migration_plan.md](./ch16_migration_plan.md)             | ⚠️ D 阶段未完成           |
| §17 验收标准            | [ch17_acceptance_criteria.md](./ch17_acceptance_criteria.md)   | ❌ V17-3/4 未达成         |
| §18 风险对策            | [ch18_risks.md](./ch18_risks.md)                               | ❌ R18-3 无 rate limit    |

---

## P0 紧急修复清单（影响核心价值）

| ID    | 报告 | 差异                           | 影响                                      |
| ----- | ---- | ------------------------------ | ----------------------------------------- |
| D7-1  | §7   | 缺 `explore` stage             | "读代码"任务被错误路由到 default          |
| D7-2  | §7   | 缺 `test` stage                | "跑测试"任务被错误路由                    |
| D10-5 | §10  | strong_model 选择错误          | complex workflow 走弱模型（违反核心价值） |
| D10-4 | §10  | workflow 类型由 is_simple 决定 | complex 任务几乎不走 triple               |
| V17-3 | §17  | 复杂任务三步走未达成           | 验收标准 3 未通过                         |
| V17-4 | §17  | 测试任务两类复杂度识别未达成   | 验收标准 4 未通过                         |

---

## P1 高优先级修复清单

| ID      | 报告 | 差异                                |
| ------- | ---- | ----------------------------------- |
| D5-3    | §5   | ~batch 强制流程起点未实现           |
| D10-2   | §10  | ~batch 强制流程起点未实现（同根因） |
| D12-1   | §12  | ~batch 强制流程起点未实现（同根因） |
| D12-2   | §12  | ~stage 缺 explore / test（同根因）  |
| D16-D-1 | §16  | 阶段 D batch workflow 未启用        |
| D18-3-1 | §18  | 高阶模型无 rate limit（成本风险）   |
| V17-6   | §17  | 人工覆盖命令部分失效                |

---

## P2 中优先级修复清单（按章节）

| ID      | 报告 | 差异                                 |
| ------- | ---- | ------------------------------------ |
| D4-3    | §4   | Model Tier 命名大小写                |
| D5-1    | §5   | 路由 6 vs 7 层文档更新               |
| D6.1-2  | §6   | 长 prompt 分类器超时风险             |
| D6.2-2  | §6   | 缺 evidence 字段                     |
| D6.3-2  | §6   | secondary_stage 未实现               |
| D6.3-3  | §6   | reason_tags 未结构化                 |
| D9-1    | §9   | complexity 未基于 stage 评估         |
| D9-3    | §9   | COMPLEXITY_KEYWORDS 未单源化         |
| D11-1   | §11  | brainstorm/decide 主模型例外         |
| D11-2   | §11  | implement fb 选错（与 D10-5 同根因） |
| D12-3   | §12  | ~model 未校验合法 model              |
| D13-1   | §13  | Level 3 timestamp 查找未实现         |
| D14-1   | §14  | STAGE_CONFIG 字段命名差异            |
| D14-2   | §14  | STAGE_KEYWORDS 未单源化              |
| D14-3   | §14  | PATTERN_KEYWORDS 未单源化            |
| D14-4   | §14  | COMPLEXITY_KEYWORDS 未单源化         |
| D15-1   | §15  | 缺部分统计指标                       |
| D15-2   | §15  | 维度聚合未实现                       |
| D15-6   | §15  | prompt 日志脱敏                      |
| D16-A-1 | §16  | 准确率评估脚本缺失                   |
| D16-B-1 | §16  | 灰度开关缺失                         |
| D16-E-1 | §16  | prompt 迭代工具链缺失                |
| D18-1-1 | §18  | 置信度阈值策略未实现                 |
| D18-2-1 | §18  | 关键词表未单源化（与 D14 同根因）    |
| D18-4-1 | §18  | Level 3 timestamp（同 D13-1）        |

---

## P3 低优先级修复清单

| ID      | 报告 | 差异                                  |
| ------- | ---- | ------------------------------------- |
| D6.4-1  | §6   | Schema 字段名差异（业务侧不修改）     |
| D6.4-2  | §6   | task_pattern 复合值未实现             |
| D6.4-3  | §6   | analyze_result 子阶段未实现           |
| D9-2    | §9   | audit base=70 边界值                  |
| D9-4    | §9   | 长度加成粗略启发式                    |
| D11-5   | §11  | 模型命名大小写                        |
| D12-6   | §12  | ~m alias 文档化                       |
| D14-5   | §14  | LLM_CLASSIFIER_CONFIG 缺 enabled 开关 |
| D15-5   | §15  | /trace 端点文档化                     |
| D18-5-1 | §18  | 项目级 PATTERN_CONFIG override        |

---

## 同根因聚合（修复时一并处理）

### 簇 1：~batch 强制流程起点

涉及差异：**D5-3 / D10-2 / D12-1 / D16-D-1 / V17-6**
修复路径：在 `proxy.do_POST` 中加：

```python
if batch_template := read_batch_override(cwd, sid):
    if batch_template in PATTERN_CONFIG:
        forced_stage = PATTERN_CONFIG[batch_template]["default_flow"][0]
        stage = forced_stage
```

### 簇 2：缺 explore / test stage

涉及差异：**D7-1 / D7-2 / D8-2 / D12-2 / V17-4**
修复路径：在 `STAGE_CONFIG` 补 `explore` / `test`：

```python
STAGE_CONFIG["explore"] = {
    "model": "MiniMax-M3", "fb_model": "deepseek-v4-pro",
    "emoji": "🔎", "label": "探索理解", "desc": "读代码、追调用链、看日志、定位现状",
    "base_url": "https://api.minimaxi.com/anthropic", "api_key_env": "MINIMAX_API_KEY",
    "protocol": "anthropic",
    "fb_base_url": "https://api.deepseek.com/anthropic", "fb_api_key_env": "DEEPSEEK_API_KEY",
    "fb_protocol": "anthropic",
}
STAGE_CONFIG["test"] = {
    "model": "MiniMax-M3", "fb_model": "deepseek-v4-pro",
    "emoji": "🧪", "label": "测试验证", "desc": "写测试、跑测试、分析覆盖率、回归验证",
    "base_url": "https://api.minimaxi.com/anthropic", "api_key_env": "MINIMAX_API_KEY",
    "protocol": "anthropic",
    "fb_base_url": "https://api.deepseek.com/anthropic", "fb_api_key_env": "DEEPSEEK_API_KEY",
    "fb_protocol": "anthropic",
}
```

同时：

- `llm_classifier.VALID_STAGES` 补 `explore` / `test`
- `stage_detector.STAGE_KEYWORDS` 补 explore / test 关键词
- LLM prompt 提示分类器允许输出 explore / test

### 簇 3：complex workflow + strong_model

涉及差异：**D10-4 / D10-5 / D11-2 / V17-3**
修复路径：

```python
# stage_config.py 新增
STRONG_MODEL = "deepseek-v4-pro"
NORMAL_MODEL = "MiniMax-M3"

# proxy.build_workflow_plan 改写
def build_workflow_plan(stage, complexity_label, ...):
    if complexity_label == "simple":
        return {"type": "single", "models": [STAGE_CONFIG[stage]["model"]]}
    elif complexity_label == "medium":
        return {"type": "double", "models": [STRONG_MODEL, STAGE_CONFIG[stage]["model"]]}
    else:  # complex
        return {"type": "triple", "models": [STRONG_MODEL, STAGE_CONFIG[stage]["model"], STRONG_MODEL]}
```

### 簇 4：单源化关键词表

涉及差异：**D14-2 / D14-3 / D14-4 / D18-2-1**
修复路径：把 `STAGE_KEYWORDS` / `PATTERN_KEYWORDS` / `COMPLEXITY_KEYWORDS` 全部迁移到 `stage_config.py`，`stage_detector.py` 改为派生读取。

### 簇 5：4 级查找 + 灰度开关

涉及差异：**D13-1 / D18-4-1**
修复路径：实现 `find_state(project_root, session_id)` 4 级查找完整逻辑（详见 §13 报告 D13-1）。

### 簇 6：可观测性指标 + rate limit

涉及差异：**D15-1 / D15-2 / D18-3-1**
修复路径：补齐 strong_model_ratio / complex_task_success_rate / retry_rate 指标；增加 per-session/per-project rate limit。

---

## 总结

**当前 V1.2 实施完成度**：

- §7 Stage 表：4/6 = 67%
- §8 Pattern 表：8/8 = 100%（含业务扩展）
- §10 路由算法：6/8 = 75%
- §13 4 级查找：3/4 = 75%
- §16 迁移进度：56%
- §17 验收标准：0/6 完全达成

**核心 P0 缺口（影响 V1.2 价值主张）**：

1. 缺 `explore` / `test` stage → 2 类高频任务路由错误
2. complex workflow + strong_model 失效 → "强模型规划 + 常规模型执行 + 强模型审计"未真正实现

**修复后预期**：

- Stage 表 9/9 完整
- Pattern 表 9/9 完整，default_flow 全部生效
- complex workflow 100% 走强模型
- 验收 6/6 全部达成

---

> 本索引由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
