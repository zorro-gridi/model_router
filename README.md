# Stage-Aware Model Router

Claude Code 阶段感知模型路由系统。根据当前工作流阶段和操作类型，自动将请求路由到最合适的模型，支持跨 provider 故障切换和 session 内状态持久化。

## 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Claude Code 进程                             │
│  ANTHROPIC_BASE_URL=http://127.0.0.1:7878                          │
│                                                                     │
│  ┌──────────────────┐     ┌──────────────────────────────────────┐ │
│  │ UserPromptSubmit  │     │  proxy.py (HTTP 代理, :7878)         │ │
│  │ Hook              │     │                                      │ │
│  │ ┌──────────────┐  │     │  1. 读 active_session → 找到         │ │
│  │ │ detect_stage │  │     │     当前 session 的 stage_<sid>      │ │
│  │ │ detect_op    │  │     │  2. 读 stage_<sid> → 关键词检测      │ │
│  │ │ detect_model │  │     │     或 op_<sid> → 操作类型覆盖       │ │
│  │ └──────────────┘  │     │     或 model_<sid> → 用户手动指定    │ │
│  │       ↓           │     │  3. 选模型，改写请求中的 model 字段   │ │
│  │ 写入 session 文件  │     │  4. 转发到上游 API                   │ │
│  │ (stage/op/model/  │     │  5. 故障时 sticky fallback           │ │
│  │  fallback_<sid>)  │     │  6. 改写响应的 model 字段遮罩内部    │ │
│  └──────────────────┘     │     别名，防止 CC 记录不识别的模型名  │ │
│                           └──────────┬───────────────────────────┘ │
│                                      │                             │
│  ┌──────────────────┐               ▼                              │
│  │ Stop Hook        │     ┌───────────────────────┐                │
│  │ stage_show.py    │     │ MiniMax / DeepSeek    │                │
│  │ 终端显示当前阶段  │     │ Anthropic 兼容 API    │                │
│  └──────────────────┘     └───────────────────────┘                │
└─────────────────────────────────────────────────────────────────────┘
```

**Hook 和代理解耦**：Hook 只负责写文件，代理读文件决策。两者通过文件系统通信，互不阻塞。

---

## V1.3 升级说明（2026-06-15）

V1.3 对路由系统进行了根本性重构：**从 stage 路由 → Task Pattern + Task Complexity 路由**，
决策链路从「每次 prompt 独立判断」升级为「prompt 先验 → runtime 累积 → 首次 TodoWrite 锁定」。

### V1.3 决策流程

```
                     UserPromptSubmit Hook
                            │
                            ▼
                  ┌─────────────────────┐
                  │ 1. ~model 检测      │  显式覆盖：~model ds-v4-pro / ~model mm3
                  │    model_alias 解析  │
                  └────────┬────────────┘
                           │
                           ▼
                  ┌─────────────────────┐
                  │ 2. LLM 分类         │  llm_classifier.classify(prompt)
                  │    → pattern +      │  或 V1 关键词 fallback
                  │      complexity     │
                  └────────┬────────────┘
                           │
                           ▼
                  ┌─────────────────────┐
                  │ 3. 保守偏置          │  模糊 prompt（"帮我"/"看看"）
                  │    simple → medium   │  → 强制至少 medium
                  └────────┬────────────┘
                           │
                           ▼
                  ┌─────────────────────┐
                  │ 4. decide()         │  DecisionRecord 生成
                  │    locked=False     │  首次只是"暂定"
                  └────────┬────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        PostToolUse   PostToolUse   PostToolUse
        (Read/Bash)   (Write/Edit)  (TodoWrite)
              │            │            │
              ▼            ▼            ▼
        runtime_score  runtime_score  runtime_score
        累积 +1~3      累积 +5~15     累积 +10~20
              │            │            │
              └────────────┼────────────┘
                           │
                           ▼
                  ┌─────────────────────┐
                  │ 5. maybe_redecide() │  每次 PostToolUse 后检查
                  │    runtime_score     │  - runtime_score > 70 → upgrade to complex
                  │    + todowrite       │  - TodoWrite implementation → lock
                  │    → 升级或锁        │
                  └────────┬────────────┘
                           │
                           ▼
                  ┌─────────────────────┐
                  │ 6. Decision Lock    │  locked=True 后：
                  │    决策终裁          │  - 新 prompt 不覆写模型
                  │    locked=True       │  - ~model 仍可显式切换
                  └─────────────────────┘
```

### Decision Lock（决策锁定）

v1.3 的核心机制：**一次升级，锁后不变**。

| 属性           | 说明                                                                                               |
| -------------- | -------------------------------------------------------------------------------------------------- |
| **何时锁**     | ① runtime_score 触发升级（medium→complex / simple→medium）；② 首次 TodoWrite implementation 强信号 |
| **锁的效果**   | locked=True 后，新 UserPromptSubmit **不会**因 prompt 复杂度变化而切换模型                         |
| **锁的例外**   | `~model` 显式覆盖始终生效（用户手动指令 > lock）                                                   |
| **锁的持久化** | `decision.locked` 字段写入 `model_router_state_<sid>.json`                                         |
| **锁的语义**   | 「当前 session 的模型选择已确定，不再因 prompt 变化而切换」                                        |

**为什么需要 lock？** 避免「用户开始做复杂实施 → 触发升级到 deepseek-v4-pro → 中途问一个
简单问题 → 被降级回 MiniMax-M3 → 后续实施又需要切回去」的 ping-pong 效应。一旦判定任务
需要强模型，整个 session 保持该模型。

**locked 状态下 ~model 显式覆盖仍然生效**（`decision_source=explicit`，`locked` 保持 True），
因为用户显式指令不应被 lock 阻止。但无 ~model 的普通 prompt 不会触重重决策。

### 复杂度 → 模型路由表

V1.3 简化了路由表，不再按 stage 区分模型，仅保留复杂度维度：

| 复杂度  | 分数区间 | 模型              | 触发条件                                  |
| ------- | -------- | ----------------- | ----------------------------------------- |
| complex | 71~100   | `deepseek-v4-pro` | 跨模块重构 / 架构变更 / runtime 高分累积  |
| medium  | 31~70    | `MiniMax-M3`      | 多步骤任务 / 模糊 prompt 保守偏置（默认） |
| simple  | 0~30     | `MiniMax-M3`      | 单文件 / 单步骤 / 明确 trivial prompt     |

> **~model 显式覆盖优先级最高**，不受路由表影响。

### 文件布局变更

```
<project_root>/.claude/
├── model_router_state_<sid>.json  ← **v1.3 单文件持久化**（替代 9 个旧文件）
│   {
│     "version": "1.3",
│     "session_id": "...",
│     "decision": {              ← DecisionRecord（模型 + 复杂度 + lock 状态）
│       "final_model": "MiniMax-M3",
│       "task_complexity": "medium",
│       "locked": false,
│       "decision_source": "prompt"
│     },
│     "runtime_score": {...},    ← 工具使用累积分
│     "todowrite_signal": {...}, ← TodoWrite 强信号
│     "model_override": null,    ← ~model 显式覆盖
│     "stage": "implement"       ← 语义标签（不参与路由）
│   }
│
│ 旧文件（v1.2，已不再生成）：
│   stage_<sid>, op_<sid>, model_<sid>, pattern_<sid>,
│   complexity_<sid>, fallback_<sid>, batch_<sid>,
│   reqcnt_<sid>, workflow_step_<sid>
│   → migrate() 可一次性迁移到新格式
```

### `route_model` 字段更新链路（statusline 权威源）

`route_model` 是 `model_router_state_<sid>.json` 中**反映最近一次代理请求实际路由到的最终模型**的字段，由 `statusline.sh` 第三行读取并展示。该字段由 6 个写入点协同维护，保证文件创建后永不缺失、永不滞后。

#### 写入点一览

| #   | 触发时机                   | 写入模块                         | 写入策略                                        | 代码位置                                          |
| --- | -------------------------- | -------------------------------- | ----------------------------------------------- | ------------------------------------------------- |
| 1   | 文件**首次创建**           | `state_persistence.py`           | `route_model: null`（占位）                     | `state_persistence.py:120-122`                    |
| 2   | 首个 `UserPromptSubmit`    | `stage_detector.py`              | **初始化** `init_route_model`（见下）           | `stage_detector.py:896-912`                       |
| 3   | 每次 `proxy.py do_POST` 后 | `proxy.py`                       | **覆写** `actual_route_model`（sticky swap 后） | `proxy.py:1734,1858-1864`                         |
| 4   | fallback retry 成功        | `proxy.py`                       | **覆写** `actual_route_model = fb_model`        | `proxy.py:1771`                                   |
| 5   | 用户 `~model <name>`       | `stage_detector.py` + `proxy.py` | 双路径生效（见下）                              | `stage_detector.py:864-866`、`proxy.py:1514-1538` |
| 6   | 其它 `write()` 调用        | `decision_engine.py` 等          | **字段继承**保留旧值                            | `state_persistence.py:138-144`                    |

#### 字段初始化优先级（`init_route_model`）

`stage_detector.py` 在每个 `UserPromptSubmit` 末尾写入 `route_model`，优先级链如下（**任一档命中即停止，永不返回 null**）：

```
1. new_model                         # 用户 ~model 显式覆盖（最高）
2. decision.final_model              # LLM 分类器决策
3. STAGE_CONFIG[resolved_stage].model # 阶段默认主模型
4. "MiniMax-M3"                      # 硬编码兜底
```

#### 实际路由态追踪（`actual_route_model`）

`proxy.py` 在 `do_POST` 内追踪本次请求**实际使用**的模型（可能与 `stage` 默认不同）：

```python
# 初始：sticky swap 后的 model
actual_route_model = model

# 主模型失败 → fallback retry 成功后
if not _is_retriable(status):
    actual_route_model = fb_model
```

随后调用 `SessionStateStore.write(route_model=actual_route_model, ...)` 写回 statusline 读取的权威源。

#### 字段继承机制（兜底保护）

`state_persistence.py:138-144` 的 `optional_fields` 循环实现**写时继承**：

```python
for key in optional_fields:
    if key in kwargs and kwargs[key] is not None:    # 显式传 → 用新值
        new_data[key] = kwargs[key]
    elif key in existing:                            # 未传 → 从旧文件继承
        new_data[key] = existing[key]               # （不会清空 proxy 写过的值）
```

`decision_engine.maybe_redecide()` 写 `decision` 但不传 `route_model` 时，**自动继承**上一次 proxy 写入的值，不会误清空。

#### ~model 命令的双路径生效

- **路径 A（stage_detector）**：检测 prompt 中的 `~model <name>` → 解析为 `new_model` → 写入 `route_model=new_model`
- **路径 B（proxy）**：从请求 body 解析 `prompt_model_override`（`proxy.py:1514`）→ 直接作为 `model_override` 路由 → 请求后写回 `route_model=actual_route_model`

两条路径确保用户在 `~model ds-v4-pro` 的**当前回合**就立即生效，无一回合延迟。

#### ~model reset 行为

`~model reset` 是 `~model` 系列命令的"解除"指令，2026-06-16 行为变更后**同时**承担两件事：

| 目标                                                         | 实现方式                                                                  | 代码位置             |
| ------------------------------------------------------------ | ------------------------------------------------------------------------- | -------------------- |
| 1. 解除本回合的 `~model` 一次性覆盖                          | `prompt_is_reset=True` → `model_override = None`（本请求回到 stage 路由） | `proxy.py:1539-1545` |
| 2. 清除 **sticky fallback**（让后续请求回到正常 stage 路由） | `clear_fallback()` → 删除 `fallback_<sid>` 文件                           | `proxy.py:685-695`   |

**关键点**：`~model` 是一次性指令，**不写** `model_<sid>` 持久文件，但 `~model reset` 会清理 `fallback_<sid>`（持久文件）—— 这与 "~model 一次性" 不冲突，因为 fallback 是**模型不可用时**自动积累的副作用，需要用户显式清除。

`stage_detector` 端对 `~model reset` 同样保持 no-op（`stage_detector.py:600-603`），与 "~model 一次性" 语义对齐。

#### 时序保证

```
UserPromptSubmit (stage_detector)     ──→  创建文件 + 初始化 route_model
        ↓
proxy do_POST (实际请求)              ──→  用 actual_route_model 覆写
        ↓
PostToolUse (maybe_redecide)         ──→  只读 + 字段继承，**不创建**也**不清空**
```

- stage_detector **先于** proxy 第一次请求 → 文件必先存在
- maybe_redecide 在 line 212 有 `if not state: return None` 保护 → **不会在 stage_detector 之前创建文件**
- 一旦文件存在，**任何** `write()` 调用都不会把 `route_model` 写回 null

#### 验证

- 单元测试：`tests/test_state_persistence.py:200-407` 验证字段继承不会清空 `route_model`
- 状态展示：`statusline.sh:97-101` 读取 `route_model`，fallback 到 `fallback_<sid>` 原始文件兜底
- 知识库记录：`.knowledge-base/hooks.md` 同步了 statusline 错误的修复过程

`config/decision_weights.yaml` 集中管理复杂度判定参数（Stage 7 引入）：

```yaml
# config/decision_weights.yaml
version: "1.3"

# Pattern 关键词权重（LLM 不可用时 V1 fallback）
patterns:
  feature: { keywords: ["实现", "开发", "做一个", "新功能"], weight: 5 }
  bugfix: { keywords: ["修", "bug", "报错", "异常", "崩溃"], weight: 5 }
  refactor: { keywords: ["重构", "改结构", "拆分"], weight: 6 }
  test: { keywords: ["测试", "test", "覆盖率"], weight: 3 }
  research: { keywords: ["调研", "了解", "怎么实现", "对比"], weight: 4 }
  migration: { keywords: ["迁移", "升级", "兼容"], weight: 7 }
  architecture: { keywords: ["架构", "设计", "方案", "数据模型"], weight: 7 }
  docs: { keywords: ["文档", "注释", "说明"], weight: 2 }
  audit: { keywords: ["审计", "review", "检查", "安全"], weight: 6 }

# 复杂度阈值
complexity:
  thresholds:
    simple_max: 30
    medium_max: 70
    # > 70 → complex

  conservative_bias:
    enabled: true # 模糊 prompt → 强制 medium
    ambiguous_hints: # 模糊关键词
      - "帮我"
      - "看下"
      - "怎么"
      - "优化"
      - "重构"

# Runtime Score 事件权重
runtime_score:
  tool_weights:
    read: 1 # Read / Grep / Glob — 调研类
    write: 5 # Write / Edit — 实施类
    bash: 3 # Bash（git / test / 编译）
    todowrite: 10 # TodoWrite — 计划变更
    search: 2 # WebSearch / WebFetch

# TodoWrite 强信号
todowrite:
  implementation_keywords:
    - "implement"
    - "fix"
    - "refactor"
    - "build"
    - "add"
    - "create"
    - "write"
    - "debug"

# Decision Lock 阈值
decision_lock:
  runtime_upgrade_threshold: 70 # runtime_score > 70 → complex
  todowrite_triggers_lock: true # TodoWrite implementation → lock
```

**YAML 加载策略**：

- 启动时 `stage_config.py::load_yaml_weights()` 读取 YAML
- YAML 缺失或损坏 → 降级使用内置硬编码（`_PLACEHOLDER_WEIGHTS`）
- 修改 YAML 后需重启 proxy 生效
- 可选的 feature flag `MODEL_ROUTER_WEIGHTS_YAML`（默认启用）

### 新增模块

| 模块                           | 职责                                           | 阶段 |
| ------------------------------ | ---------------------------------------------- | ---- |
| `decision_engine.py`           | 决策核心：`decide()` + `maybe_redecide()`      | 1,5  |
| `state_persistence.py`         | 持久化层：`model_router_state_<sid>.json` 读写 | 3    |
| `runtime_score.py`             | Runtime Score 纯计算引擎（6 类事件）           | 2    |
| `runtime_tracker.py`           | PostToolUse hook：score 累积 + 持久化          | 4    |
| `todowrite_analyzer.py`        | TodoWrite 分析器：implementation 强信号检测    | 4    |
| `post_tool_handler.py`         | PostToolUse hook dispatcher                    | 4    |
| `session_state_machine.py`     | 7 态状态机（INITIAL→…→LOCKED）                 | 2    |
| `decision_lock.py`             | Decision Lock 并发控制                         | 1,2  |
| `config/decision_weights.yaml` | YAML 权重配置（可热更）                        | 7    |

### 已删除模块

- `workflow_orchestrator.py` — v1.3 不再编排多模型 workflow（Stage 6 删除）
- `OPERATION_CONFIG` — operation type 路由已废弃（Stage 7 删除）
- `WORKFLOW_PLANNER` — 多步编排已移除（Stage 6 删除）
- 旧 9 文件双写 — v1.3 仅写单文件 `model_router_state_<sid>.json`（Stage 7 删除）

### 从 v1.2 迁移

1. **自动迁移**：首次启动时 `state_persistence.migrate()` 自动将旧 9 文件聚合成新格式
2. **旧文件保留**：迁移后旧文件**不会自动删除**（安全起见），可手动清理
3. **proxy 透传**：proxy 读侧已切换为 `SessionStateStore.read_new()`，旧文件 fallback 仍保留
4. **stage CLI 兼容**：`stage show` 输出与 v1.2 视觉一致（用户零感知）

---

## 核心概念

### 路由维度（按设计文档 V1.2，v1.3 中已简化）

| 维度             | 文件            | 设置方式                                   | 是否影响路由   | 说明                                                |
| ---------------- | --------------- | ------------------------------------------ | -------------- | --------------------------------------------------- |
| ① Model Override | `model_<sid>`   | `~model ds-v4-pro` / `用 mm3`              | ✅ 是          | 用户显式指定，完全覆盖其他维度                      |
| ② Operation-type | `op_<sid>`      | prompt 关键词 (`写`/`search`/`review`)     | ✅ 是          | 按操作类型微调，覆盖 stage 路由                     |
| ③ Stage          | `stage_<sid>`   | prompt 关键词 (`实现`/`架构`/`审计`)       | ✅ 是          | 默认路由维度                                        |
| ④ Task Pattern   | `pattern_<sid>` | prompt 关键词 + `~pattern <name>` 显式指定 | ❌ Shadow Mode | 仅记录，不参与路由（Phase B 启用 Adaptive Routing） |
| ⑤ Complexity     | —               | `~careful` / `~quick`                      | 🟡 部分        | 调整主备模型优先级（待实现）                        |

四/五维度的关系：**model override > op > stage**。Pattern 与 Complexity **当前不影响路由**（Shadow Mode）。

> 💡 **三者可同时检测，互不冲突**。同一 prompt 中的 `~model`、`~stage`、`~<op>` 会各自独立命中，最终路由按上述优先级合并。例如 `~model ds-flash ~stage implement ~write` 会同时检测到 model=deepseek-v4-flash、stage=implement、op=write，最终走 model override（deepseek-v4-flash）。

> 💡 **`~` 命令不限制在 prompt 开头**，放在任意位置均可识别：
>
> ```
> ~model ds-flash 帮我实现这个功能    ← 开头
> 帮我实现这个功能 ~model ds-flash     ← 结尾
> 用 ds-v4-pro ~write 写一下这个      ← 中间
> ```
>
> 底层实现：三处正则均使用 `(?:^|\s)~` 前缀 + `.search()` 匹配，空格前缀确保不会误命中 `ignore~model` 这种写法。

### 阶段映射

| 阶段       | emoji | 主模型            | 备用模型          | 适用场景             |
| ---------- | ----- | ----------------- | ----------------- | -------------------- |
| brainstorm | 💭    | deepseek-v4-flash | MiniMax-M3        | 快速发散，低成本探索 |
| decide     | ⚖️    | deepseek-v4-pro   | MiniMax-M3        | 深度推理，权衡分析   |
| design     | 🏗️    | MiniMax-M3        | deepseek-v4-pro   | 系统架构，方案设计   |
| plan       | 📋    | MiniMax-M3        | deepseek-v4-pro   | 任务拆解，结构化输出 |
| implement  | ⚙️    | MiniMax-M3        | deepseek-v4-flash | 主力编码，工程实施   |
| audit      | 🔍    | MiniMax-M3        | deepseek-v4-pro   | 严格检查，安全审计   |
| default    | 🔄    | MiniMax-M3        | deepseek-v4-flash | 兜底默认             |

> **升级说明（2026-06-14，按设计文档 V1.2 第 7/11 章）**：
> 之前 `plan` 走 deepseek-v4-pro（贵），现在统一**默认走 MiniMax-M3**（主） +
> deepseek-v4-pro（升级）——大部分 plan 任务 mm3 够用，需要强推理时由 sticky
> fallback 切到 deepseek-v4-pro。同样 `audit` 也调整为主 mm3 / 备 pro。

### 操作类型映射（第二维度）

| 操作     | emoji | 主模型     | 备用模型          | 说明                |
| -------- | ----- | ---------- | ----------------- | ------------------- |
| write    | ✏️    | MiniMax-M3 | deepseek-v4-flash | 写入，便宜 fallback |
| read     | 👁️    | MiniMax-M3 | deepseek-v4-pro   | 读取，稳 fallback   |
| search   | 🔎    | MiniMax-M3 | deepseek-v4-flash | 搜索，和 write 一致 |
| refactor | 🔧    | MiniMax-M3 | deepseek-v4-pro   | 结构改动需稳妥推理  |

### 任务模式映射（第三维度，Shadow Mode）

> ⚠️ **本轮升级（2026-06-14）将 Pattern Library 接入 stage_detector，但保持
> Shadow Mode 状态**：检测结果会写入 `pattern_<sid>` + 在日志和 stage_show 中
> 显示，但 **不影响实际路由**。Phase B 阶段会通过 ROC 分析 + 准确率 ≥ 90%
> 后再开启 Adaptive Routing。

| Pattern        | 中文     | 默认流程                                    | 默认复杂度 | 主推模型          |
| -------------- | -------- | ------------------------------------------- | ---------- | ----------------- |
| `feature`      | 功能开发 | plan → design → implement → test → audit    | medium     | MiniMax-M3        |
| `bugfix`       | 缺陷修复 | explore → implement → test                  | medium     | MiniMax-M3        |
| `refactor`     | 结构重构 | explore → design → implement → test → audit | medium     | MiniMax-M3        |
| `test`         | 测试建设 | explore → test → audit                      | medium     | MiniMax-M3        |
| `research`     | 资料调研 | explore → plan → design                     | medium     | deepseek-v4-flash |
| `migration`    | 迁移改造 | plan → design → implement → test → audit    | complex    | MiniMax-M3        |
| `architecture` | 架构任务 | explore → plan → design → audit             | complex    | MiniMax-M3        |
| `docs`         | 文档编写 | explore → implement                         | simple     | deepseek-v4-flash |
| `audit`        | 代码审计 | explore → audit                             | complex    | MiniMax-M3        |

**显式指定**：`~pattern feature` / `~pattern bugfix` 等，识别后写 `pattern_<sid>`，置信度 `1.0`。
**关键词自动识别**：根据 prompt 中的关键词权重打分，最高分 pattern 胜出，置信度按 `score / (score + 4)` 归一化（典型值 0.4~0.8）。

### 复杂度等级（第四维度，部分影响）

| 等级    | 分数区间 | 含义              | 触发指令             |
| ------- | -------- | ----------------- | -------------------- |
| simple  | 0~30     | 单文件 / 单步骤   | `~quick`（降一档）   |
| medium  | 31~70    | 多步骤 / 轻度设计 | （默认）             |
| complex | 71~100   | 跨模块 / 高风险   | `~careful`（升一档） |

> 本轮升级已完成数据建模（`COMPLEXITY_LEVELS` / `shift_complexity()`），但
> proxy 路由尚未消费该信号——Phase B 接入。

## Session 状态持久化（关键设计）

```
<project_root>/.claude/
├── stage_<session_id>        ← 当前阶段（纯文本，如 "implement"）
├── op_<session_id>           ← 操作类型覆盖（纯文本，可选）
├── model_<session_id>        ← 模型覆盖（纯文本，可选）
├── fallback_<session_id>     ← sticky fallback 标记（纯文本，可选）
├── pattern_<session_id>      ← 任务模式标注（JSON，Shadow Mode 写入）
└── session_state_<sid>.json  ← 系统压缩状态（CC 原生）

~/.claude/hooks/model_router/
└── active_session            ← **单文件指针**，指向当前活跃 session 的 stage_<sid> 完整路径
```

**每个 session 独立维护自己的状态文件**，互不干扰。这是 design principle：

- 新 session 的 stage 始终初始化为 `default`（从 prompt 关键词重新检测）
- Fallback 状态只影响自己所在的 session
- Model override 仅对当前 session 生效

### `active_session` 指针：为什么是单文件

`active_session` 是 **一个固定的单文件指针**，内容为当前最后活跃 session 的 `stage_<sid>` 文件的**完整绝对路径**。

```
~/.claude/hooks/model_router/active_session
→ 内容示例: /Users/zorro/my-project/.claude/stage_a1b2c3d4
```

之所以需要它是因为 **proxy.py 是 HTTP 服务器，没有 stdin 可以拿 session_id**。每次请求进来，proxy 必须知道"这次应该读哪个 session 的 stage 文件"，但请求体里不携带 session_id。所以设计了这个间接层：

1. **Hook（有 stdin）** 在 `UserPromptSubmit` 触发时，知道 session_id 和 cwd，写入 `active_session` 指针
2. **Proxy（无 stdin）** 每次请求读取 `active_session`，拿到完整路径，找到对应的 `stage_<sid>`

**多 session 场景的问题**：如果同时运行多个 Claude Code 窗口共用一个 proxy，`active_session` 会被不断覆盖——A 的请求可能误读 B 的 stage/fallback 配置。这是当前已知的设计限制。**建议：用 proxy 时只开一个 Claude Code 窗口**。

详见 [#active_session-多会话问题](#active_session-多会话问题)。

## 文件布局

```
~/.claude/
├── stage-router.log                 ← 路由日志
├── hooks/model_router/
│   ├── .env                         ← API Keys（gitignored，auto-loaded）
│   ├── .env.example                 ← 模板
│   ├── README.md                    ← 本文档
│   ├── install.sh                   ← 安装脚本
│   ├── proxy.py                     ← 本地代理服务器（HTTP :7878）
│   ├── stage                        ← 阶段管理 CLI 源（cp 到 ~/.local/bin/stage）
│   ├── stage_config.py              ← **唯一数据源**，所有组件从这里导入配置
│   ├── stage_detector.py            ← UserPromptSubmit Hook：自动检测 + 文件维护
│   ├── stage_show.py                ← Stop Hook：终端显示当前阶段/模型
│   ├── model_alias.py               ← 模型简称映射（ds-v4-pro → deepseek-v4-pro）
│   └── active_session               ← 单文件指针，指向当前 session 的 stage_<sid>

~/.local/bin/
└── stage                            ← 阶段管理 CLI
```

### 配置唯一数据源

所有阶段/操作/模型配置集中在 **`stage_config.py`** 的 `STAGE_CONFIG` 和 `OPERATION_CONFIG` 字典中。其他模块（proxy、stage_detector、stage_show、CLI）都从这导入派生视图：

```python
# stage_config.py 导出（自动生成，无需手动维护）
STAGE_MODELS        # proxy 用: stage → (base_url, model, key_env, protocol)
FALLBACK_MODELS     # proxy 用: stage → fallback (fb_base_url, fb_model, ...)
STAGE_DISPLAY       # stage_show 用: stage → (emoji, label, model)
STAGE_DESC          # CLI 用: stage → 格式化描述
STAGE_INFO          # stage_detector 用: stage → 描述
OPERATION_*         # 与上同构，但操作类型

MODEL_TO_CONFIG     # 反向索引: model_name → 完整路由配置
```

修改模型映射只改 `stage_config.py`，所有消费方自动同步。

## 安装

```bash
cd ~/.claude/hooks/model_router
bash install.sh
```

安装脚本会：

1. 复制 CLI 到 `~/.local/bin/stage`
2. 在 `~/.claude/settings.local.json` 注册 `UserPromptSubmit` 和 `Stop` hook
3. 从 `.env.example` 创建 `.env`（如不存在）

### 配置 API Keys

```bash
# 编辑 .env（已有则跳过）
vim ~/.claude/hooks/model_router/.env

# 格式
MINIMAX_API_KEY=eyJ...
DEEPSEEK_API_KEY=sk-...
```

> Shell 环境变量优先级高于 `.env`，可用于临时覆盖。
> proxy 启动时会校验所有 stage 需要的 key，缺一个就报错退出。

### 启动代理

```bash
# 终端 1：启动代理
stage proxy

# 终端 2：启动 CC（确保 ANTHROPIC_BASE_URL 指向 proxy）
export ANTHROPIC_BASE_URL="http://127.0.0.1:7878"
claude
```

或者通过 `settings.json` 配置 `env.ANTHROPIC_BASE_URL`（全局生效）。

## 阶段切换

### 方式 1：自动关键词检测（UserPromptSubmit Hook）

直接用自然语言，Hook 自动识别：

| 你说                          | 识别的阶段 | 路由到            |
| ----------------------------- | ---------- | ----------------- |
| "头脑风暴一下" / "想想方向"   | brainstorm | deepseek-v4-flash |
| "对比两个方案" / "权衡"       | decide     | deepseek-v4-pro   |
| "设计一下架构" / "数据模型"   | design     | MiniMax-M3        |
| "拆一下任务" / "怎么做"       | plan       | MiniMax-M3        |
| "实现这个功能" / "修一下 bug" | implement  | MiniMax-M3        |
| "review 代码" / "安全检查"    | audit      | MiniMax-M3        |

操作类型关键词平行检测（不改变 stage，仅覆盖模型选择）：

| 你说               | 检测到的操作 | 路由覆盖   |
| ------------------ | ------------ | ---------- |
| "把这个功能写一下" | write        | MiniMax-M3 |
| "看看这个文件"     | read         | MiniMax-M3 |
| "搜索一下这个接口" | search       | MiniMax-M3 |
| "重构这个方法"     | refactor     | MiniMax-M3 |

任务模式关键词（Shadow Mode，不影响路由，仅记录）：

| 你说                  | 识别到的 pattern | 默认流程                            |
| --------------------- | ---------------- | ----------------------------------- |
| "新功能 / 做一个登录" | `feature`        | plan→design→implement→test→audit    |
| "修一个 bug / 报错"   | `bugfix`         | explore→implement→test              |
| "重构 / 改一下结构"   | `refactor`       | explore→design→implement→test→audit |
| "补测试 / 跑一下测试" | `test`           | explore→test→audit                  |
| "调研一下 / 了解一下" | `research`       | explore→plan→design                 |
| "把代码迁到 / 升级到" | `migration`      | plan→design→implement→test→audit    |
| "架构设计 / 顶层方案" | `architecture`   | explore→plan→design→audit           |
| "写个文档 / 注释"     | `docs`           | explore→implement                   |
| "代码审查 / 安全审计" | `audit`          | explore→audit                       |

### 方式 2：显式命令

CC 对话框内（优先级最高）：

```
~stage implement     # 切换阶段
~stage audit
~write               # 切换操作类型（覆盖 stage 路由）
~read                # 读取操作
~search              # 搜索操作
~refactor            # 重构操作

# ── Pattern / Complexity（Shadow Mode，仅记录不参与路由）──
~pattern feature     # 显式设定任务模式（confidence=1.0）
~pattern bugfix
~careful             # 复杂度升档（simple→medium→complex）
~quick               # 复杂度降档（complex→medium→simple）
```

### 方式 3：Shell CLI

```bash
stage              # 查看当前阶段和 op
stage implement    # 切换阶段
stage op write     # 手动设置操作类型覆盖
stage op reset     # 清除 op 覆盖
stage op list      # 列出可用 op
stage status       # 查看代理状态
stage log          # 实时路由日志
stage reset        # 重置为 default

# ── Pattern 维度（Shadow Mode）──
stage pattern                # 查看当前 session 识别的 pattern
stage pattern feature        # 显式设定 pattern（confidence=1.0）
stage pattern reset          # 清除 pattern 标注
stage pattern list           # 列出全部可用 pattern
```

### 方式 4：模型别名指令（最高优先级，一次性）

在 prompt 中任意位置指定模型，**仅对当前请求生效**，下一次提交（不再带 `~model`）则回到自动路由：

```
用 ds-v4-pro
~model ds-flash
use mm3
~m reset           # 本次 reset（一次性覆盖下，no-op 形式存在）
```

支持的别名：`ds-v4-pro`, `ds-pro`, `ds-v4-flash`, `ds-flash`, `mm3`, `mm`, `sonnet`, `opus` 等。

> **2026-06-16 行为变更**：`~model` 不再写入 `model_<sid>` 持久文件，避免用户在 prompt 里随手
> 写了 `~model` 后忘了清、导致整个 session 都被钉死在某个模型上。语义与 `~stage` / `~<op>` 对齐
> （都是「本次会话指令」）。需要长期使用某模型：在 `settings.json` 里配环境变量，或每次
> prompt 都带 `~model`。

## Sticky Fallback

当上游 API 返回 429/5xx 或超时时，proxy 自动尝试备用模型。一旦备用模型成功，该 session 会**持久化 fallback 状态**——后续请求固定使用对应 stage 的备用模型。

```python
# fallback_<sid> 文件标记 session 已降级
# 请求流程变成: stage_<sid> → fallback_<sid> 存在 → 用备用模型
```

- **触发条件**：请求失败且 retry 后在备用模型上成功
- **持久化**：写入 `fallback_<sid>` 文件，session 内所有后续请求走备用
- **清除时机**：stage 切换、model override、op 覆盖时自动清除
- **遮罩**：proxy 改写响应体中的 `model` 字段为 CC 认知的模型名，防止 CC 记录不识别的别名后重启报 warning

## API 协议支持

两种协议模式，`stage_config.py` 中通过 `protocol` 字段控制：

| protocol            | 说明                                                  | 适用端点           |
| ------------------- | ----------------------------------------------------- | ------------------ |
| `anthropic`（默认） | 透明转发，仅改写 model 字段                           | `*/anthropic` 路径 |
| `openai`（opt-in）  | 自动转换 Anthropic Messages ↔ OpenAI Chat Completions | `*/v1` 路径        |

### Thinking block 处理

对于支持 extended thinking 的模型：

- **Anthropic 协议**：去除顶层的 `thinking` 参数（非原生 Anthropic 端点不支持），但保留 `type: thinking` 的 content block 原样透传
- **OpenAI 协议**：去除 `thinking` 参数 + 将 `thinking` block 转为 `text` block

## 调试

```bash
# 测试 Hook（模拟 CC 输入）
echo '{"prompt": "帮我实现登录", "session_id": "test", "cwd": "/tmp"}' \
  | python3 ~/.claude/hooks/model_router/stage_detector.py

# 代理健康检查
curl http://127.0.0.1:7878/health

# 实时路由日志
stage log

# 干运行
python3 ~/.claude/hooks/model_router/proxy.py --dry-run

# 查看日志文件
tail -f ~/.claude/stage-router.log
```

## FAQ

### `active_session` 多会话问题

`active_session` 是单文件指针。**多窗口共用 proxy 时会被覆盖**。

| 使用模式            | 影响                    | 建议                     |
| ------------------- | ----------------------- | ------------------------ |
| 单窗口              | ✅ 完美                 | 默认工作模式             |
| 多窗口（不同项目）  | ⚠️ session 路由可能错乱 | 每个项目开独立的 proxy   |
| Workflow 并行 agent | ⚠️ 同上                 | 确保 Workflow 中模型一致 |

设计选择的原因：proxy 是 HTTP 服务器，请求体不带 session_id。如果要彻底解决，需要让 CC 的每次请求携带 session_id（需要修改 CC 或通过请求 header 传递），未来可以考虑。

### 新 session 的 stage 从哪来？

**始终从你的 prompt 关键词检测**。新 session 初始化为 `default`，在第一个 `UserPromptSubmit` 触发时再次检测。

没有"继承上一个 session 的状态"的设计——每个 session 是独立的。如果有跨 session 的偏好，请用 `settings.json` 的环境变量或 `~model` 指令在 prompt 中指定。

### 模型名不识别 warning

如果你看到 `Session model XXXXX could not be restored (not a model this version of Claude Code recognizes)`，这是 proxy 的 `_rewrite_response_model()` 没有覆盖到——请检查 proxy 中该 session 的原始模型名是否已映射到一个 CC 能识别的显示名。

### 端口被占用

```bash
lsof -i :7878  # 找到旧进程
kill <PID>
stage proxy    # 重新启动
```

## 故障排查

| 症状                                   | 排查                                                      |
| -------------------------------------- | --------------------------------------------------------- |
| `curl /health` 返回 connection refused | 代理未启动，跑 `stage proxy`                              |
| 启动报"缺少必需的 API key"             | 编辑 `.env` 填入 key 或 shell export                      |
| 上游 401                               | 对应 key 错误/未配置，日志会打印末 4 位                   |
| 上游 404 / 模型不存在                  | `stage_config.py` 中 model 名拼写错误                     |
| Hook 触发但 stage 不变                 | 查看 `stage-router.log`，检查关键词是否命中               |
| proxy 重连失败                         | 检查 `active_session` 文件是否存在、内容是否正确          |
| 多窗口下路由混乱                       | 同一 proxy 只配一个 CC 窗口，或检查 `active_session` 内容 |
