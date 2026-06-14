# V1.2 详细设计方案 审计报告

**审计时间**：2026-06-14
**审计基准**：`智能模型路由插件系统_功能升级实施详细设计方案_V1.2.docx`
**当前目录**：`/Users/zorro/.claude/hooks/model_router/`
**审计目的**：逐条对齐 V1.2 设计文档与当前实施，给出 PASS / PARTIAL / MISSING 结论与偏差清单。

---

## 0. 总体结论

| 维度                                         | 状态        | 备注                                                                             |
| -------------------------------------------- | ----------- | -------------------------------------------------------------------------------- |
| 数据层（stage_config）                       | **PASS**    | STAGE/OPERATION/PATTERN/COMPLEXITY 四套配置均落地（见 `stage_config.py:48-322`） |
| 路由层（model_override > op > stage）        | **PASS**    | proxy.py `do_POST` 第 581-622 行实现 5 维优先级中前 3 维                         |
| Shadow Mode Pattern 识别                     | **PASS**    | stage*detector `detect_task_pattern`（213-298）+ pattern*<sid> JSON 落盘         |
| Stage Complexity Classifier                  | **MISSING** | COMPLEXITY_LEVELS 数据已就绪，但 `detect_complexity()` 函数未实现，proxy 不消费  |
| Workflow Planner（§6.5）                     | **MISSING** | 无 single/double/triple 编排；proxy 仅按单模型路由                               |
| 手动指令 ~careful / ~quick / ~batch / ~reset | **MISSING** | 仅 `~model` / `~stage` / `~pattern` 已实现                                       |
| Project Binding（§13）                       | **MISSING** | 仍依赖单点 `active_session` 指针，无 `state_index.json`                          |
| Observability（§6.8 / §15）                  | **PARTIAL** | /health 已实现，缺 /metrics、/trace；日志缺 pattern/complexity/score/confidence  |
| Stage × Complexity 新路由（阶段 C）          | **MISSING** | 当前仍走 op > stage 二维，未引入 complexity 维度                                 |

---

## 1. 逐章审计

### §2 设计原则

| 原则                                    | 状态    | 评估                                                                   |
| --------------------------------------- | ------- | ---------------------------------------------------------------------- |
| P1 Stage/Complexity/Pattern 三维分工    | PARTIAL | Stage+Pattern 已就绪，Complexity 仅数据未消费                          |
| P2 多维共同决定模型，不允许单维绝对覆盖 | PASS    | model_override > op > stage 已实现多级优先级                           |
| P3 默认低成本                           | PASS    | brainstorm/decide 用 deepseek-flash/pro，其余 MiniMax-M3（§11 已对齐） |
| P4 人工覆盖最高优先级                   | PASS    | `~model / ~m` 显式覆盖在所有自动路由之前                               |
| P5 分类器输出结构化                     | PARTIAL | Stage/Pattern 已结构化，Complexity 缺                                  |
| P6 可观测、可回放、可回滚               | PARTIAL | 日志可回放，缺 /metrics、/trace                                        |

### §4 核心概念

| 概念              | 状态                                                     |
| ----------------- | -------------------------------------------------------- |
| Task Pattern      | PASS（9 个 pattern，stage_config PATTERN_CONFIG）        |
| Stage             | PASS（7 个 stage，stage_config STAGE_CONFIG）            |
| Stage Complexity  | PARTIAL（数据已有，缺分类器）                            |
| Workflow Strategy | MISSING                                                  |
| Model Tier        | PASS（MiniMax-M3 / deepseek-v4-pro / deepseek-v4-flash） |

### §4.5 Context Compressor 边界

- **MISSING**：当前无独立 Context Compressor 模块。仅依赖 smart_precompact 压缩历史对话。
- 实际影响：当前 routing 决策不消费压缩后的 routing_context，复杂度分类器若引入则需要该模块的产出。

### §5 路由决策优先级（6 层）

| 优先级                                | 状态    | 实施位置               |
| ------------------------------------- | ------- | ---------------------- |
| 1. 人工模型覆盖 ~model / ~m           | PASS    | `proxy.py:582-594`     |
| 2. 强制流程覆盖（batch / 项目模板）   | MISSING | 无 batch 机制          |
| 3. 任务模式 Pattern                   | MISSING | 仅 Shadow 标注，未消费 |
| 4. Stage                              | PASS    | `proxy.py:603-608`     |
| 5. Stage Complexity 决定多阶段工作流  | MISSING | 无                     |
| 6. 模型成本与可用性 / sticky fallback | PASS    | `proxy.py:610-654`     |

### §6 功能模块

| 模块                             | 状态    | 偏差                                              |
| -------------------------------- | ------- | ------------------------------------------------- |
| §6.1 Context Compressor          | MISSING | 无                                                |
| §6.2 Task Pattern Matcher        | PASS    | `stage_detector.py:213-298`                       |
| §6.3 Stage Classifier            | PASS    | `stage_detector.py:128-144`                       |
| §6.4 Stage Complexity Classifier | MISSING | 需补 `detect_complexity()`                        |
| §6.5 Workflow Planner            | MISSING | 需补 single/double/triple 编排                    |
| §6.6 Model Router                | PASS    | `proxy.py:570-666`（不含 workflow）               |
| §6.7 State Manager               | PARTIAL | active_session 单点，缺 state_index.json          |
| §6.8 Observability               | PARTIAL | /health 已实现，缺 /metrics、/trace、缺结构化字段 |

### §7 Stage 体系

- **PASS**：7 个 stage 全部对齐 V1.2 §7 表格（brainstorm/decide/design/plan/implement/audit/default）。
- 默认模型 MiniMax-M3、升级 deepseek-v4-pro、降级 deepseek-v4-flash，符合 §11 默认策略。
- 关键词识别见 `stage_detector.py:93-119`（待与 V1.2 §7 表格识别提示对照，本期未改）。

### §8 Pattern Library

- **PASS**：9 个 pattern 全部对齐 V1.2 §8 表格（feature/bugfix/refactor/test/research/migration/architecture/docs/audit），含 default_flow / default_complexity / primary_model 字段。

### §9 复杂度分级

- **PARTIAL**：COMPLEXITY*LEVELS、COMPLEXITY_THRESHOLDS、complexity_rank、shift_complexity 已在 `stage_config.py:305-322` 实现，但 `detect_complexity()` 函数、`complexity*<sid>` 落盘、proxy 消费均缺失。

### §10 路由决策算法

- 文档推荐"规则优先 + LLM 分类 + 置信度兜底"三段式。当前实现仅走了"规则 + 关键词分类"两段，未实现：
  - 步骤 5：评估当前 stage complexity
  - 步骤 6：生成 workflow plan
  - 步骤 7：路由到具体模型（按 plan.type 选模型序列）

### §11 默认模型策略

- **PASS**：
  - MiniMax-M3 作为默认基线（多数 stage 与 op）
  - DeepSeek-V4-Flash 作为 brainstorm 阶段 + 写/搜/降级场景
  - DeepSeek-V4-Pro 作为 decide/审计/读升级场景
  - 无无条件高阶模型，符合"不破坏降本目标"

### §12 手动指令

| 指令              | 状态        | 实施位置                                                                                 |
| ----------------- | ----------- | ---------------------------------------------------------------------------------------- |
| ~model / ~m       | PASS        | model_alias.detect_model_override                                                        |
| ~stage <name>     | PASS        | stage_detector EXPLICIT_PREFIX_RE                                                        |
| ~careful          | **MISSING** | 需实现（升档当前 complexity）                                                            |
| ~quick            | **MISSING** | 需实现（降档当前 complexity）                                                            |
| ~batch <template> | **MISSING** | 需实现（按 PATTERN*CONFIG.default_flow 写入 batch*<sid>）                                |
| ~reset            | **MISSING** | 当前 `stage reset` 仅清除 stage；需扩展为清除 model/op/pattern/fallback/complexity/batch |

### §13 状态文件与并发

- **MISSING**：仍使用单点 `active_session` 指针，未引入 `state_index.json`：
  ```json
  {
    "/Users/zorro/project-a": {
      "session_id": "aaa",
      "stage": "design",
      "last_active": 1234567890
    }
  }
  ```
- 风险：多窗口并发时 Project A / Project B 互相污染。

### §14 配置文件规范

- **PASS**：`stage_config.py` 是单源，所有消费方仅读取派生视图（`STAGE_MODELS`、`STAGE_DISPLAY` 等）。

### §15 日志 / 指标 / 可观测

- **PARTIAL**：
  - `proxy.py:375-396` 路由日志字段：阶段、原模型、目标、provider、protocol、msgs、thinking_param、thinking_blocks、hdrs
  - **缺**：pattern、complexity、score、confidence、token 估计、fallback 次数（每次分类都需记录）
  - /health 已实现；/metrics、/trace 未实现
  - 缺按项目/会话/pattern 维度聚合统计

### §16 迁移实施

- 当前实际为阶段 A 状态：保留关键词路由，Pattern/Complexity 处于 Shadow 标注期。
- 阶段 B（引入 Pattern + Complexity 部分灰度）尚未启动。
- 阶段 C（关闭 Op 覆盖、启用新路由）尚未启动。

### §17 验收标准

| 标准                                      | 状态                                      |
| ----------------------------------------- | ----------------------------------------- |
| 简单任务 token 下降                       | 未量化（需 /metrics 落地）                |
| 中等任务返工率下降                        | 未量化                                    |
| 复杂任务稳定触发 strong → normal → strong | 未实现（缺 Workflow Planner）             |
| 测试任务区分"写测试"与"分析测试结果"      | 未实现（缺 complexity 分类器）            |
| 多窗口不再互相污染                        | 未实现（缺 state_index.json）             |
| 人工覆盖生效且优先级明确                  | PASS（~model / ~stage / ~pattern 已实现） |

### §18 风险

- 主要未化解风险：分类器误判（缺复杂度分类器）、路由过度复杂（缺 workflow 编排层）、并发冲突（缺 state_index.json）。

---

## 2. 偏差清单（按修复优先级排序）

| #   | 偏差                                           | 设计文档位置 | 修复建议                                                                                            |
| --- | ---------------------------------------------- | ------------ | --------------------------------------------------------------------------------------------------- |
| 1   | ~careful / ~quick 指令未实现                   | §12          | stage*detector 新增前缀检测 + 写 complexity*<sid>                                                   |
| 2   | ~batch <template> 未实现                       | §12          | stage*detector 解析，按 PATTERN_CONFIG.default_flow 写 batch*<sid>                                  |
| 3   | ~reset 只能清 stage，未清全部 override         | §12          | 新增全量清除函数（model/op/pattern/fallback/complexity/batch）                                      |
| 4   | detect_complexity() 函数未实现                 | §6.4         | stage_detector 关键词+长度+pattern 加权评分 0-100                                                   |
| 5   | Workflow Planner 未实现                        | §6.5         | proxy 端按 complexity 选模型序列：simple=单模型，medium=normal+strong，complex=strong+normal+strong |
| 6   | state_index.json 未实现                        | §13          | HOOK_DIR/state_index.json，proxy 优先按 project_root 查找                                           |
| 7   | 路由日志缺 pattern/complexity/score/confidence | §15          | proxy.py 路由决策前读 pattern/complexity，日志补字段                                                |
| 8   | /metrics、/trace 接口未实现                    | §6.8 / §15   | do_GET 增路由，写入 /tmp/stage_metrics.jsonl                                                        |
| 9   | stage_show 未显示 complexity                   | §15          | 读 complexity\_<sid>，按 §6.4 schema 打印                                                           |
| 10  | stage CLI 缺 complexity/batch/reset 子命令     | §12          | main() 增 elif 分支                                                                                 |

---

## 3. 修复计划（Task #9 子任务）

1. ~careful / ~quick / ~batch / ~reset 手动指令（stage_detector.py + stage CLI）
2. detect_complexity() 函数（stage_detector.py）
3. state_index.json 维护 + proxy 端 project_root 优先查找
4. proxy.py 消费 pattern + complexity，按 complexity 选模型（workflow 编排）
5. 路由日志结构化（pattern/complexity/score/confidence/token/fallback_count）
6. /health 扩展 + /metrics、/trace 接口
7. stage_show 追加 complexity 显示
8. 完整 smoke test + git 提交

> 全部修复后，本审计报告的 PARTIAL/MISSING 项应转 PASS；§17 验收标准中"复杂任务三步编排"和"多窗口不污染"两条将首次具备达成条件。
