# §14 配置文件规范 — 审计报告

> 审计依据：`/tmp/v12_design.md` 第 14 章（"配置文件规范"）
> 审计时间：2026-06-14
> 审计范围：所有路由配置集中在 `stage_config.py` 的单源化执行情况
> 落地代码：`/Users/zorro/.claude/hooks/model_router/stage_config.py`

---

## 14.1 设计文档要求

**核心原则**：建议所有路由配置集中于 `stage_config.py` 或等价的单一配置源，**其他模块只能派生读取，不可重复维护**。

**文档示例 STAGE_CONFIG 结构**：

```python
STAGE_CONFIG = {
    "explore": {
        "default_model": "MiniMax-M3",
        "upgrade_model": "DeepSeek-V4-Pro",
        "downgrade_model": "DeepSeek-V4-Flash",
        "keywords": ["理解", "追踪", "读代码", "调用链"],
        "weight": 0.8
    },
    ...
}
```

**文档示例 PATTERN_CONFIG 结构**：

```python
PATTERN_CONFIG = {
    "feature": {
        "default_flow": ["plan", "design", "implement", "test", "audit"],
        "default_complexity": "medium"
    }
}
```

## 14.2 当前实现（`stage_config.py`）

### 14.2.1 主配置源

| 字典                    | 内容                                                                | 文档对齐                                                                                              |
| ----------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `STAGE_CONFIG`          | 7 个 stage（brainstorm/decide/design/plan/implement/audit/default） | ⚠️ 字段命名差异（`model` / `fb_model` vs 文档 `default_model` / `upgrade_model` / `downgrade_model`） |
| `OPERATION_CONFIG`      | 空 dict（已废弃）                                                   | ✅                                                                                                    |
| `PATTERN_CONFIG`        | 9 个 pattern                                                        | ✅（多出 audit pattern，见 §8 D8-1）                                                                  |
| `COMPLEXITY_LEVELS`     | ("simple", "medium", "complex")                                     | ✅                                                                                                    |
| `COMPLEXITY_THRESHOLDS` | {simple: 30, medium: 70, complex: 100}                              | ✅                                                                                                    |
| `LLM_CLASSIFIER_CONFIG` | LLM 分类器配置                                                      | ✅                                                                                                    |

### 14.2.2 派生视图（stage_config.py:376-467）

✅ 派生视图（`STAGE_MODELS` / `FALLBACK_MODELS` / `STAGE_DISPLAY` / `PATTERN_FLOW` / `PATTERN_DEFAULT_COMPLEXITY` / `MODEL_TO_CONFIG`）全部基于主配置自动生成，**符合单源化**。

## 14.3 差异清单

### D14-1 [DEVIATION] STAGE_CONFIG 字段命名与文档不一致

- **文档示例**：
  - `default_model` / `upgrade_model` / `downgrade_model`
  - `keywords` / `weight`
- **当前实现**：
  - `model` / `fb_model` / `base_url` / `api_key_env` / `protocol` / `fb_base_url` / ...
  - 无 `keywords` / `weight` 字段
- **结论**：DEVIATION（字段命名差异）
- **设计溯源**：实现侧把"路由信息"（base_url / api_key_env / protocol）也塞进 STAGE_CONFIG，把 model 简化为 `model` / `fb_model`
- **影响**：
  - 字段命名不一致需要文档侧对齐
  - `keywords` 字段缺失（`stage_detector.STAGE_KEYWORDS` 仍是独立字典，**违反单源化**，见 D14-3）
- **建议修复**：
  - 方案 A：文档侧更新 §14 示例，使用 `model` / `fb_model` 命名
  - 方案 B：实现侧改为 `default_model` / `upgrade_model` / `downgrade_model`（破坏性变更，影响 proxy / stage_detector）
  - 推荐方案 A
- **风险等级**：P2（仅文档/命名差异）

### D14-2 [DEVIATION] STAGE_CONFIG 缺少 `keywords` / `weight` 字段

- **文档示例**：每个 stage 含 `keywords` 列表 + `weight` 浮点数
- **当前实现**：❌ STAGE_CONFIG 无 keywords / weight；关键词在 `stage_detector.STAGE_KEYWORDS`（独立字典）
- **后果**：
  - 关键词分类逻辑与 stage 配置**两套并行**，修改 stage 时容易遗漏关键词表
  - 违反单源化
- **建议修复**：

  ```python
  # stage_config.py 增加
  STAGE_CONFIG = {
      "explore": {
          "model":       "MiniMax-M3",
          "fb_model":    "deepseek-v4-pro",
          "keywords":    ["理解", "追踪", "读代码", "调用链", "定位"],
          "weight":      0.8,
          ...
      },
      ...
  }

  # stage_detector.py 改为派生
  STAGE_KEYWORDS = {
      stage: [(kw, c.get("weight", 1.0)) for kw in c.get("keywords", [])]
      for stage, c in STAGE_CONFIG.items()
  }
  ```

- **风险等级**：P2（违反单源化原则）

### D14-3 [DEVIATION] `stage_detector.STAGE_KEYWORDS` 独立维护（违反单源化）

- **文档 §14**："其他模块只能派生读取，不可重复维护"
- **当前实现**：`stage_detector.py:101-127`（STAGE_KEYWORDS，~30 条）和 `stage_detector.py:234-275`（PATTERN_KEYWORDS，~50 条）**独立硬编码**
- **建议修复**：合并到 STAGE_CONFIG / PATTERN_CONFIG
- **风险等级**：P2（与 D14-2 同根因）

### D14-4 [DEVIATION] `stage_detector.COMPLEXITY_KEYWORDS` 独立维护（与 §9 D9-3 同根因）

- **当前实现**：`stage_detector.py:1011-1027`（COMPLEXITY_KEYWORDS，~30 条）独立硬编码
- **建议修复**：迁移到 `stage_config.COMPLEXITY_KEYWORDS`
- **风险等级**：P2

### D14-5 [DEVIATION] `LLM_CLASSIFIER_CONFIG` 缺 `enabled` 开关

- **文档 §14**：未明确
- **当前实现**：LLM_CLASSIFIER_CONFIG 在 stage_config.py:348 定义
- **影响**：无法运行时关闭 LLM 分类器（只能改 .env）
- **建议修复**：增加 `"enabled": True` 字段
- **风险等级**：P3

### D14-6 [PASS] 派生视图（`STAGE_MODELS` / `FALLBACK_MODELS` / `MODEL_TO_CONFIG`）

- **文档 §14**：派生视图是单源化的标准模式
- **当前实现**：✅ stage_config.py:376-467 全部用 dict comprehension 派生
- **结论**：PASS

### D14-7 [PASS] COMPLEXITY_CONFIG / LLM_CLASSIFIER_CONFIG 集中在 stage_config.py

- **当前实现**：✅ 全部在 stage_config.py 顶层
- **结论**：PASS

## 14.4 验收结论

| 单源化项                                                     | 状态                       |
| ------------------------------------------------------------ | -------------------------- |
| STAGE_CONFIG 主表                                            | ✅ PASS（字段命名差异 P2） |
| PATTERN_CONFIG 主表                                          | ✅ PASS                    |
| COMPLEXITY_CONFIG 主表                                       | ✅ PASS                    |
| LLM_CLASSIFIER_CONFIG 主表                                   | ✅ PASS                    |
| 派生视图（STAGE_MODELS / FALLBACK_MODELS / MODEL_TO_CONFIG） | ✅ PASS                    |
| STAGE_KEYWORDS 派生                                          | ❌ FAIL（D14-2 / D14-3）   |
| PATTERN_KEYWORDS 派生                                        | ❌ FAIL（D14-3）           |
| COMPLEXITY_KEYWORDS 派生                                     | ❌ FAIL（D14-4）           |

## 14.5 修复优先级

1. **P2** — D14-2 / D14-3 / D14-4：把 stage_detector 三个关键词表迁移到 stage_config.py（单源化）
2. **P2** — D14-1：文档侧更新 §14 STAGE_CONFIG 示例字段命名

## 14.6 修复后预期

`stage_config.py` 应包含：

- STAGE_CONFIG（每 stage 含 model / fb_model / keywords / weight）
- PATTERN_CONFIG（每 pattern 含 default_flow / default_complexity / primary_model / keywords / weight）
- COMPLEXITY_KEYWORDS（共享给 stage_detector）
- LLM_CLASSIFIER_CONFIG
- 派生视图（STAGE_KEYWORDS / PATTERN_KEYWORDS / STAGE_MODELS / FALLBACK_MODELS / MODEL_TO_CONFIG）

`stage_detector.py` 应**只做派生读取**：

```python
from stage_config import STAGE_CONFIG, PATTERN_CONFIG, COMPLEXITY_KEYWORDS
STAGE_KEYWORDS = {s: c.get("keywords", []) for s, c in STAGE_CONFIG.items()}
PATTERN_KEYWORDS = {p: c.get("keywords", []) for p, c in PATTERN_CONFIG.items()}
```

---

> 本报告由 `chapter-by-chapter audit` 流程生成，供后续功能更新/修复时查阅。
