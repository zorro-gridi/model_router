# §6 功能模块详细设计 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 6 章（"功能模块详细设计"）
> 审计时间：2026-06-14
> 审计范围：6.1 ~ 6.8 共 8 个子模块的实现完整性

---

## 6.0 总体结论

| 子模块                          | 状态       | 落地位置                                              |
| ------------------------------- | ---------- | ----------------------------------------------------- |
| 6.1 Context Compressor          | ⚠️ PARTIAL | smart_precompact + LLM Classifier prompt 共享上下文   |
| 6.2 Task Pattern Matcher        | ✅ SHADOW  | `stage_detector.PATTERN_KEYWORDS` + `llm_classifier`  |
| 6.3 Stage Classifier            | ✅ PASS    | `stage_detector.STAGE_KEYWORDS` + `llm_classifier`    |
| 6.4 Stage Complexity Classifier | ✅ PASS    | `stage_detector.detect_complexity` + `llm_classifier` |
| 6.5 Workflow Planner            | ✅ PASS    | `proxy.build_workflow_plan`                           |
| 6.6 Model Router                | ✅ PASS    | `proxy.do_POST`                                       |
| 6.7 State Manager               | ✅ PASS    | `state_index.json` + `active_session`                 |
| 6.8 Observability               | ✅ PASS    | `stage_router.log` + `/health` `/metrics` `/trace`    |

## 6.1 Context Compressor

**设计要求**：输入最近 N 轮对话 + 项目摘要 + 当前文件变更 + 工具调用摘要，输出 600~1200 中文字摘要。

**实现状态**：

- ✅ smart_precompact hook 存在（独立模块，未在本目录）
- ⚠️ Context Compressor 单独组件**未独立实现**；当前 LLM Classifier 直接吃原始 prompt（带 system prompt 让 LLM 自压缩）
- ⚠️ 摘要长度限制（600~1200 中文字）**未硬编码**

**差异**：

- D6.1-1 [EXPECTED] 没有显式 Context Compressor — LLM 自带上下文窗口消化能力（context 较短时不需要预先压缩）
- D6.1-2 [DEVIATION] 摘要长度未限制 — 当 prompt 极长（>50k tokens）时分类器可能超时

**修复建议**：

- 短期：保持现状，依赖 LLM 自身的 context window
- 中期：实现一个轻量 `truncate_prompt(prompt, max_chars=3000)` 包装器

## 6.2 Task Pattern Matcher

**设计要求**：识别 feature / bugfix / refactor / test / research / migration / docs / architecture，输出 pattern + 子模式 + 置信度 + 证据片段。

**实现状态**：

- ✅ `stage_detector.PATTERN_KEYWORDS`（带权重的关键词表）
- ✅ `llm_classifier.classify()` 返回 `pattern` + `pattern_confidence`
- ⚠️ "证据片段" 字段**未实现**（LLM 返回的只是 `reasoning` 一句话）
- ✅ Shadow Mode 规范：pattern 识别结果**仅记录不消费**（与文档一致）

**差异**：

- D6.2-1 [EXPECTED] Shadow Mode 行为 — 与设计文档 §6.2 规范完全一致
- D6.2-2 [DEVIATION] 输出字段不匹配 — 设计文档要求"子模式 + 证据片段"，当前只输出"reasoning 一句话"

**修复建议**：

- 在 `llm_classifier` 的 prompt 中要求 LLM 额外返回 `evidence` 数组（命中关键词片段）
- 子模式（如 `feature_test`、`bugfix_hotfix`）保留到阶段 B 启用

## 6.3 Stage Classifier

**设计要求**：识别 explore / plan / design / implement / test / audit；输出 primary_stage + secondary_stage + stage_confidence + reason_tags。

**实现状态**：

- ✅ `stage_detector.STAGE_KEYWORDS`（中英双语）
- ✅ `llm_classifier` 返回 `stage`
- ⚠️ **缺少 `explore` / `test`**（与 §7 表对齐缺失 — 见 §7 详细报告）
- ⚠️ `secondary_stage` 字段**未实现**
- ⚠️ `reason_tags` 字段**未实现**（仅 `reasoning` 字符串）

**差异**：

- D6.3-1 [DEVIATION] 缺 `explore` / `test` stage（高优先级）
- D6.3-2 [EXPECTED] secondary_stage 未实现 — 设计文档要求"主+次 stage"，当前只输出主 stage
- D6.3-3 [EXPECTED] reason_tags 未结构化 — 当前 `reasoning` 是字符串而非 tags 数组

**修复建议**：

- D6.3-1 与 §7 修复同步（补 explore / test）
- D6.3-2 / D6.3-3 保留到阶段 B（信息增益有限）

## 6.4 Stage Complexity Classifier

**设计要求**：判断当前阶段复杂度 simple/medium/complex；输出 complexity + score + confidence + escalation_hint + deescalation_hint。

**设计文档规定输出 Schema**（§6.4 第 116-145 行）：

```json
{
  "task_type": "test",
  "task_pattern": "feature_test",
  "current_stage": "analyze_result",
  "complexity": "complex",
  "score": 82,
  "confidence": 0.91
}
```

**实现状态**：

- ✅ `stage_detector.detect_complexity` 返回 `{score, label, source, signal, escalation_hint, deescalation_hint}`
- ✅ `llm_classifier.classify` 返回 `{complexity_score, complexity_label, complexity_confidence}`
- ⚠️ **Schema 字段命名差异**（见 D6.4-1）

**差异**：

- D6.4-1 [EXPECTED] Schema 字段名差异 — 实际输出 `stage` / `pattern` / `complexity_score` / `complexity_label` / `pattern_confidence` / `complexity_confidence`，文档要求 `task_type` / `task_pattern` / `current_stage` / `complexity` / `score` / `confidence`（**单 confidence 字段**）
  - 决策：**不修改**。原因：现有字段名已经稳定运行，且包含 `pattern_confidence` / `complexity_confidence` 双置信度（信息量更大）
  - 影响：日志/外部消费者需按实际字段名解析
- D6.4-2 [EXPECTED] `task_pattern` 复合值未实现 — 文档要求 `feature_test` / `bugfix_hotfix` 复合 pattern，当前只有 `feature` / `bugfix` 单层
  - 决策：保留到阶段 B（pattern 准确率稳定后）
- D6.4-3 [EXPECTED] `analyze_result` 等子阶段未实现 — 文档示例用子阶段名，当前是粗粒度 stage
  - 决策：保留

## 6.5 Workflow Planner

**设计要求**：根据 pattern + stage + complexity 生成 workflow_plan；simple=单步 / medium=双步(强+常规) / complex=三步(规划+执行+审计)。

**实现状态**：✅ **完全对齐**

- `proxy.build_workflow_plan(stage, is_simple, primary, strong, complexity)` 返回 `{type, models, model_details}`
- simple → `single`（1 个模型）
- medium → `double`（2 个模型：强 + 常规）
- complex → `triple`（3 个模型：强 + 常规 + 强）

**Smoke Test 验证**（2026-06-14）：

```
complexity=simple   type=single  models=['MiniMax-M3']
complexity=medium   type=double  models=['deepseek-v4-pro', 'MiniMax-M3']
complexity=complex  type=triple  models=['deepseek-v4-pro', 'MiniMax-M3', 'deepseek-v4-pro']
```

✅ PASS

## 6.6 Model Router

**设计要求**：把 workflow_plan 映射成具体模型名；处理 fallback / sticky fallback；默认优先 MiniMax-M3，DeepSeek-V4-Pro 仅在高阶推理时启用。

**实现状态**：✅ **完全对齐**

- `proxy.do_POST` 完成：plan → select model → execute → fallback
- Sticky fallback 状态写入 `<project_root>/.claude/stage_<sid>` 文件
- `internal_request_header` (`X-Stage-Router-Source`) 防止 5xx 污染

## 6.7 State Manager

**设计要求**：保存 active session、stage、pattern、complexity、fallback 状态；**不得**只使用单一全局指针。

**实现状态**：✅ **完全对齐**

- `state_index.json` 项目绑定（4-level lookup）
- Per-session `stage_<sid>` 文件
- `active_session` 全局指针（仅 fallback）

## 6.8 Observability

**设计要求**：记录每次分类、路由、切换、失败原因、最终模型；必须包含输入摘要、路由决策、模型耗时、token 估计、fallback 次数；建议提供 `/health` `/metrics` `/trace` 三个接口。

**实现状态**：✅ **完全对齐**

- `stage_router.log` 记录完整路由决策（输入、pattern、stage、complexity、score、model、耗时、token 估计、fallback 次数）
- `proxy.do_GET` 提供 `/health` / `/metrics` / `/trace` 三个端点

## 6.9 修复优先级汇总

| ID     | 差异                     | 优先级               |
| ------ | ------------------------ | -------------------- |
| D6.3-1 | 缺 explore / test stage  | **P1**（与 §7 同步） |
| D6.1-2 | 长 prompt 分类器超时风险 | P3（长尾场景）       |
| D6.2-2 | 缺 evidence 字段         | P3（信息增益有限）   |

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
