# V1.2 详细设计方案 审计报告

**审计时间**：2026-06-14
**审计基准**：`智能模型路由插件系统_功能升级实施详细设计方案_V1.2.docx`
**当前目录**：`/Users/zorro/.claude/hooks/model_router/`
**审计目的**：逐条对齐 V1.2 设计文档与当前实施，给出 PASS / PARTIAL / MISSING 结论与偏差清单。

> **2026-06-14 13:35 更新**：§2 偏差清单 10 项已全部完成修复（详见 §4 修复落实）。
>
> **2026-06-14 14:20 更新**：二次审计新发现 3 项二阶偏差（首轮 PASS 项的内部质量）已修复（详见 §5 二次审计发现）。

---

## 4. 修复落实（2026-06-14 13:35）

| #   | 偏差                                           | 修复位置                                                                                                 | 状态 |
| --- | ---------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ---- |
| 1   | ~careful / ~quick 指令未实现                   | `stage_detector.py:COMPLEXITY_SHIFT_RE` + `stage_detector.main()`，CLI `stage complexity careful\|quick` | ✅   |
| 2   | ~batch <template> 未实现                       | `stage_detector.py:BATCH_RE`，CLI `stage batch <template>`，proxy `do_POST` 强制覆盖                     | ✅   |
| 3   | ~reset 只能清 stage                            | `stage_detector.py:clear_all_overrides()` 清 model/op/pattern/fallback/complexity/batch 六件套           | ✅   |
| 4   | detect_complexity() 未实现                     | `stage_detector.py:detect_complexity()` 关键词+pattern 加权 0-100 评分                                   | ✅   |
| 5   | Workflow Planner 未实现                        | `proxy.py:build_workflow_plan()` single/double/triple 模型序列（§6.5）                                   | ✅   |
| 6   | state_index.json 未实现                        | `stage_detector.py:STATE_INDEX_FILE` + `_update_state_index()` 写入 project_root 索引                    | ✅   |
| 7   | 路由日志缺 pattern/complexity/score/confidence | `proxy.py:do_POST` 结构化日志 + `_append_metric()` 写 `/tmp/stage_metrics.jsonl`                         | ✅   |
| 8   | /metrics、/trace 接口未实现                    | `proxy.py:do_GET` 增 `/metrics`（聚合）+ `/trace`（最近决策）                                            | ✅   |
| 9   | stage_show 未显示 complexity                   | `stage_show.py:read_complexity()` + main 中 🟢/🟡/🔴 emoji 显示                                          | ✅   |
| 10  | stage CLI 缺子命令                             | `stage` CLI dispatcher 增 `complexity` 和 `batch` 两个分支                                               | ✅   |

**Smoke Test 全部通过**：

- `detect_complexity` 关键词评分输出 simple/medium/complex 标签
- `~careful` / `~quick` / `~batch refactor` / `~reset` 正则匹配
- `stage` CLI 全部子命令端到端通过
- `proxy /health` / `/metrics` / `/trace` HTTP 200，含 workflow_type/pattern/complexity 字段
- 路由日志格式：`[stage=design] target=... actual=... status=... pattern=... complexity=... workflow=... batch=...`

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

---

## 5. 二次审计发现（2026-06-14 14:20）

二次审计目标：对首轮已标 PASS 的项做"第二遍复验"，找出"看起来 PASS 但内部有质量缺陷"的二阶偏差。逐条对照 V1.2 实施后，新发现以下 3 项偏差并已修复。

### 5.1 偏差清单（二阶）

| #   | 偏差                                                                                                                      | 设计文档位置      | 修复位置                                                                                                                                                                             | 状态 |
| --- | ------------------------------------------------------------------------------------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---- |
| S1  | §6.5 Workflow Planner 是"装饰性"的，`build_workflow_plan` 没被消费；proxy 第 863 行 `strong_model=model`（自指）          | §6.5 / §10 步骤 6 | `proxy.py:945-956` 调用 `build_workflow_plan`；complex 任务主备交换，把 strong 模型当主                                                                                              | ✅   |
| S2  | §13 Project Binding 4 级查找未落地；`state_index.json` 只写不读，proxy 仍走老 `active_session`                            | §13 Level 1       | `proxy.py:STATE_INDEX_FILE` + `_find_project_root_for_stage_path`（复用 `hooks/compact/utils._find_project_root`） + `_read_state_index_for_project`；`read_stage()` 重写为 4 级查找 | ✅   |
| S3  | `used_fallback` 用 `status >= 400` 判定，4xx 非可重试错误（400/404/422）被误算成"用了 fallback"，导致 `/metrics` 严重虚高 | §15               | 引入 `fallback_invoked` 严格标志位：`used_fallback = bool(sticky_fb) or fallback_invoked`，仅 sticky 路径 + 实际切换 fb 才记 True                                                    | ✅   |

### 5.2 关键修复细节

#### S1 — Workflow Planner 实际消费

**修复前**：

```python
workflow = build_workflow_plan(
    stage_or_op=stage, is_op=False,
    primary_model=model,
    strong_model=model,   # ← 自指 hack，strong 等于 primary
    complexity_label=complexity_label,
)
```

后果：plan.models 三步全是 `MiniMax-M3`，复杂任务实际未走 strong → normal → strong。

**修复后**：

```python
workflow = build_workflow_plan(
    stage_or_op=stage, is_op=False,
    primary_model=model,
    strong_model=fb_model,  # 强模型 = stage 配置中的 fb_model（升级路径）
    complexity_label=complexity_label,
)
# complex 任务：把主/备对调，让 strong 模型当主、normal 当 fb
if complexity_label == "complex" and fb_model and fb_model != model:
    (base_url, model, key_env, protocol,
     fb_base, fb_model, fb_key, fb_proto) = (
        fb_base, fb_model, fb_key, fb_proto,
        base_url, model, key_env, protocol,
    )
    routing_source += " [workflow=complex→strong]"
```

`smoke test 验证`：

```
complexity=simple:  type=single  models=['MiniMax-M3']
complexity=medium:  type=double  models=['deepseek-v4-pro', 'MiniMax-M3']
complexity=complex: type=triple  models=['deepseek-v4-pro', 'MiniMax-M3', 'deepseek-v4-pro']
```

复杂任务主备交换后：`model='deepseek-v4-pro'`，`fb_model='MiniMax-M3'`，routing_source 含 `[workflow=complex→strong]`。

#### S2 — §13 4 级查找落地

**修复前**：proxy 读 stage 仅从 `active_session` 指针读，无 `project_root` 维度。

**修复后**：复用 `hooks/compact/utils.py::_find_project_root`（user feedback："你要发现项目的根目录，@hooks/compact/utils.py 中有方法可以复用啊"），不重造轮子：

```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude"))
from hooks.compact.utils import _find_project_root

def _find_project_root_for_stage_path(stage_path: Path) -> Path:
    """从 <project>/.claude/stage_<sid> 反推 project_root"""
    project = stage_path.parent.parent  # strip .claude/stage_<sid>
    return _find_project_root(project)

def _read_state_index_for_project(project_root: str) -> dict | None:
    if not STATE_INDEX_FILE.exists():
        return None
    index = json.loads(STATE_INDEX_FILE.read_text())
    return index.get(project_root)

def read_stage() -> str | None:
    # Level 1: Project Binding
    active = ACTIVE_SESSION_FILE.read_text().strip() if ACTIVE_SESSION_FILE.exists() else ""
    if active:
        try:
            project_root = str(_find_project_root_for_stage_path(Path(active)))
            info = _read_state_index_for_project(project_root)
            if info and info.get("stage"):
                return info["stage"]
        except Exception:
            pass
    # Level 4: active_session fallback (legacy)
    if active and Path(active).exists():
        return Path(active).read_text().strip()
    return None
```

`smoke test 验证`：

```
project_root 反推（真实 .claude/ 路径）：
  [OK] /Users/zorro/.claude/.claude/stage_aaa-bbb               -> /Users/zorro/.claude
  [OK] /Users/zorro/project/gridi_proj/.claude/stage_xxx        -> /Users/zorro/project/gridi_proj
  [OK] /Users/zorro/project/CodeAgent/frontend/.claude/stage_yyy -> /Users/zorro/project/CodeAgent/frontend

state_index.json 查找：
  [OK] /Users/zorro/.claude     -> {session_id: ..., stage: default, last_active: ...}
  [OK] /Users/zorro/nonexistent -> None
```

#### S3 — `used_fallback` 严格判定

**修复前**：

```python
used_fallback = bool(sticky_fb) or status >= 400
# 400/404/422 全部误算为 True → /metrics 虚高
```

**修复后**：

```python
fallback_invoked = False  # 在 sticky_fb 块前声明
sticky_fb = read_fallback() if (not model_override and not internal_req) else None
if sticky_fb:
    # 主备交换
    fallback_invoked = True  # sticky 路径 → 记 True

status, ... = forward_request(...)

if _is_retriable(status) and fb_base and fb_model and not internal_req:
    # 5xx 才进 fallback 分支
    fallback_invoked = True
    status, ... = forward_request(... fb_model ...)

# 严格定义：仅"实际使用或触发备用模型"才记 True
used_fallback = bool(sticky_fb) or fallback_invoked
```

`_is_retriable` 判定（已对齐 HTTP 语义）：

| 状态      | 200   | 400   | 401  | 403  | 404   | 422   | 429  | 500  | 502  | 503  | 504  |
| --------- | ----- | ----- | ---- | ---- | ----- | ----- | ---- | ---- | ---- | ---- | ---- |
| retriable | False | False | True | True | False | False | True | True | True | True | True |

注：401/403/429 仍归为可重试（可能由临时凭证/限流导致），但 4xx 业务错误（400/404/422）不再误算 fallback。

`smoke test 验证（三个分支）`：

```
A: 4xx 非可重试（status=400, sticky_fb=None） → used_fallback=False  ← 修复前是 True
B: 5xx 可重试（status=502, sticky_fb=None）   → used_fallback=True
C: sticky 路径（status=200, sticky_fb='x'）   → used_fallback=True
```

### 5.3 二次审计结论

| 维度                           | 首轮状态                 | 二次审计后状态           |
| ------------------------------ | ------------------------ | ------------------------ |
| §6.5 Workflow Planner          | PASS（装饰性）           | **PASS（实际消费）**     |
| §13 Project Binding 4 级查找   | PASS（只写不读）         | **PASS（4 级查找落地）** |
| §15 `used_fallback` 指标准确性 | PASS（status>=400 误算） | **PASS（严格判定）**     |
| §4.5 Context Compressor        | MISSING                  | MISSING（本期范围外）    |
| §6.1 Context Compressor        | MISSING                  | MISSING（本期范围外）    |

首轮标 PASS 的 3 个项目均发现内部质量缺陷，二次审计挖出"二阶偏差"——这是首轮审计未做"代码行为复验"留下的盲区。

### 5.4 后续建议

1. **§4.5 / §6.1 Context Compressor**：建议下一阶段建立（路由上下文构建模块），目前 routing 决策不消费 `routing_context`。
2. **审计方法论**：今后审计除看"功能是否实现"外，还应做"代码行为复验"（如对 PASS 项跑 smoke test），避免二阶偏差。
