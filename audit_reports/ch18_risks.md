# §18 风险与对策 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 18 章（"风险与对策"）
> 审计时间：2026-06-14
> 审计范围：5 类风险在当前实现中的对位与对策落实

---

## 18.1 设计文档风险表（5 类）

| 风险         | 原因                              | 对策                                     |
| ------------ | --------------------------------- | ---------------------------------------- |
| 分类器误判   | LLM prompt 不稳定或上下文压缩过度 | 输出置信度与原因标签；低置信度走保守策略 |
| 路由过度复杂 | 策略过多导致维护困难              | 将配置单源化，所有规则从统一配置导出     |
| 高阶模型滥用 | 默认策略设置过强                  | 在策略层限制升级阈值与调用次数           |
| 并发冲突     | active_session 单点写冲突         | 使用会话索引与时间戳双机制               |
| 长期漂移     | 项目特征变化导致规则失效          | 按项目维度保留日志与模板覆盖能力         |

## 18.2 逐项审计

### R18-1 分类器误判

**对策要求**：输出置信度 + 原因标签；低置信度走保守策略

**实现状态**：

- ✅ `pattern_confidence` / `complexity_confidence` 双置信度输出
- ✅ `reasoning` 一句话原因
- ⚠️ **未结构化为 tags 数组**（D6.3-3）
- ✅ **V1 关键词 fallback 已就位**（LLM 失败时回退 V1 启发式）
- ⚠️ **置信度阈值未应用**（不区分高/低置信度都走同一路径）

**差异**：

- D18-1-1 [DEVIATION] 置信度阈值策略未实现 — 应有 `if confidence < 0.6: fall back to V1`
- 风险等级：P2

**建议修复**：

```python
# llm_classifier 调用方
result = classify(prompt)
if (result["pattern_confidence"] < 0.6 or
    result["complexity_confidence"] < 0.6):
    # 低置信度：fallback 到 V1 关键词
    result_v1 = stage_detector.classify_v1(prompt)
    result.update(result_v1)
    result["source"] = "v1_fallback"
```

### R18-2 路由过度复杂

**对策要求**：将配置单源化，所有规则从统一配置导出

**实现状态**：

- ✅ `stage_config.py` 是主配置源
- ✅ 派生视图（STAGE_MODELS / FALLBACK_MODELS / MODEL_TO_CONFIG）
- ❌ **STAGE_KEYWORDS / PATTERN_KEYWORDS / COMPLEXITY_KEYWORDS 仍在 stage_detector.py 独立维护**（D14-2 / D14-3 / D14-4）

**差异**：

- D18-2-1 [DEVIATION] 三个关键词表未单源化（与 D14-2 / D14-3 / D14-4 同根因）
- 风险等级：P2

**建议修复**：见 §14 报告 D14-2 / D14-3 / D14-4 修复路径

### R18-3 高阶模型滥用

**对策要求**：在策略层限制升级阈值与调用次数

**实现状态**：

- ✅ 默认 5/7 stage 是 MiniMax-M3（高阶模型未默认）
- ❌ **无升级阈值限制**（无 rate limit on deepseek-v4-pro）
- ❌ **无调用次数统计**（D15-1 未实现强模型占比指标）
- ⚠️ **无每日/每小时配额**（如 deepseek-v4-pro 单日最多 100 次）

**差异**：

- D18-3-1 [DEVIATION] 无 rate limit — 高阶模型可能被滥用
- 风险等级：P1（成本控制风险）

**建议修复**：

```python
# stage_config.py
STRONG_MODEL_LIMITS = {
    "deepseek-v4-pro": {
        "per_session_per_hour": 50,
        "per_project_per_day": 500,
    }
}

# proxy.py
if model == "deepseek-v4-pro":
    if not check_rate_limit(model, project_root, session_id):
        # 降级到 M3
        model = "MiniMax-M3"
        log_warn("rate_limited", model=model, ...)
```

### R18-4 并发冲突

**对策要求**：使用会话索引与时间戳双机制

**实现状态**：

- ✅ `state_index.json`（按 project_root 索引）
- ✅ Per-session `stage_<sid>` 文件
- ✅ `active_session` 兜底
- ⚠️ **Level 3 timestamp 查找未实现**（D13-1）

**差异**：

- D18-4-1 [DEVIATION] Level 3 timestamp 匹配未实现（与 D13-1 同根因）
- 风险等级：P2

**建议修复**：见 §13 报告 D13-1 修复路径

### R18-5 长期漂移

**对策要求**：按项目维度保留日志与模板覆盖能力

**实现状态**：

- ✅ `stage_router.log` 按 session 维度记录
- ✅ Stage override 机制（`~stage <name>`）
- ✅ Model override 机制（`~model <name>`）
- ⚠️ **项目维度聚合未实现**（D15-2）
- ⚠️ **项目级模板覆盖**（per-project PATTERN_CONFIG override）未实现

**差异**：

- D18-5-1 [DEVIATION] 项目级 PATTERN_CONFIG override 未实现
- 风险等级：P3（长期演进才暴露）

**建议修复**：

```python
# state_index.json 增加
{
  "/project-a": {
    "session_id": "...",
    "stage": "design",
    "pattern_overrides": {
      "feature": ["explore", "plan", "design", "implement", "test", "audit"]  # 项目 A 特有的 feature 流程
    }
  }
}
```

## 18.3 风险矩阵

| 风险               | 概率 | 影响 | 当前对策完整度                    |
| ------------------ | ---- | ---- | --------------------------------- |
| R18-1 分类器误判   | 中   | 中   | 70%（V1 fallback 已就位，缺阈值） |
| R18-2 路由过度复杂 | 中   | 中   | 70%（主表单源化，关键词表未）     |
| R18-3 高阶模型滥用 | 高   | 高   | 30%（无 rate limit）              |
| R18-4 并发冲突     | 低   | 中   | 80%（3/4 查找已实现）             |
| R18-5 长期漂移     | 低   | 中   | 60%（override 机制有，缺聚合）    |

## 18.4 验收结论

| 风险类别     | 对策落实                                |
| ------------ | --------------------------------------- |
| 分类器误判   | ⚠️ V1 fallback + 置信度输出，缺阈值策略 |
| 路由过度复杂 | ⚠️ 主表单源化，关键词表分散             |
| 高阶模型滥用 | ❌ 无 rate limit                        |
| 并发冲突     | ⚠️ 3/4 查找实现                         |
| 长期漂移     | ⚠️ override 机制有，缺项目级聚合        |

## 18.5 修复优先级

1. **P1** — D18-3-1 高阶模型 rate limit（成本控制 + 防止滥用）
2. **P2** — D18-1-1 置信度阈值策略
3. **P2** — D18-2-1 三个关键词表单源化
4. **P2** — D18-4-1 Level 3 timestamp 查找
5. **P3** — D18-5-1 项目级 PATTERN_CONFIG override

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
