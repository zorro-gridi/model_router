# Stage-Aware Model Router — 深度介绍

## 概述

Stage-Aware Model Router 是一个轻量级 HTTP 代理系统，为 Claude Code 添加**工作流阶段感知的智能模型路由**能力。它让用户在同一个会话中，根据当前任务类型（头脑风暴、方案设计、编码实现、代码审计等），自动切换到最合适的 AI 模型，无需手动切换 API 配置。

### 核心价值

| 问题                               | 解决方案                                           |
| ---------------------------------- | -------------------------------------------------- |
| 编码用主力模型，头脑风暴想省钱快速 | prompt 关键词自动检测，不同阶段路由到不同模型      |
| 上游 API 不稳定/限流               | 跨 provider 故障切换 + per-session sticky fallback |
| 不想改 Claude Code 源码            | 纯 Hook + HTTP 代理，零入侵                        |
| 多 session 互不干扰                | per-session 文件隔离                               |
| 用户想强制指定模型                 | `~model` 指令覆盖所有自动路由                      |

---

## 架构设计

### 三组件结构

```
┌────────────────────────────┐
│    UserPromptSubmit Hook    │  ← 分析 prompt，写入 stage/op/model 文件
│    (stage_detector.py)      │
└────────────┬───────────────┘
             │ 写文件
             ▼
┌────────────────────────────┐
│     文件系统 (File Store)   │  ← per-session 状态持久化
│   stage_<sid> / op_<sid>   │
│   model_<sid> / fallback   │
│   active_session (指针)     │
└────────────┬───────────────┘
             │ 读文件
             ▼
┌────────────────────────────┐
│    HTTP 代理 (proxy.py)     │  ← 拦截 API 请求，改写 model，转发
│    http://127.0.0.1:7878   │
└────────────┬───────────────┘
             │ 转发
             ▼
┌────────────────────────────┐
│   MiniMax / DeepSeek API   │
└────────────────────────────┘
```

### 为什么需要三个组件？

| 组件                           | 运行时机           | 有无 stdin/session_id      | 职责                                        |
| ------------------------------ | ------------------ | -------------------------- | ------------------------------------------- |
| **Hook** (`stage_detector.py`) | 每次 prompt 提交前 | ✅ 有 `session_id` + `cwd` | 分析 prompt 关键词，写入 per-session 文件   |
| **文件系统**                   | 持久化存储         | —                          | 传递决策结果，解耦 Hook 和 Proxy            |
| **Proxy** (`proxy.py`)         | 每个 API 请求      | ❌ 无 session_id 上下文    | 读文件决策，改写请求 body 中的 `model` 字段 |

**关键设计：Hook 和 Proxy 通过文件系统通信，互不阻塞。**

Hook 有 stdin 上下文（知道当前 session_id 和工作目录），Proxy 没有。所以 Hook 负责写文件并维护 `active_session` 指针，Proxy 只管读这个指针找到对应文件。

---

## 三路由维度

### 优先级：Model Override > Operation > Stage

```
用户 prompt
    │
    ├── 检测 ~model → 如果有 → 走 model override 路由 [最高优先级]
    │
    └── 检测 ~<op> / 关键词 → 如果有 → 走 op 路由 [覆盖 stage]
        │
        └── 检测 ~stage / 关键词 → 走 stage 路由 [默认]
```

### 维度一：Stage（阶段路由）

根据用户 prompt 中的关键词自动推断当前工作阶段，是**默认路由维度**。

| 阶段       | emoji | 检测关键词（部分）               | 主模型            | 备用模型          |
| ---------- | ----- | -------------------------------- | ----------------- | ----------------- |
| brainstorm | 💭    | 头脑风暴 / 创意 / idea / explore | deepseek-v4-flash | MiniMax-M3        |
| decide     | ⚖️    | 决策 / 权衡 / compare / 评估     | deepseek-v4-pro   | MiniMax-M3        |
| design     | 🏗️    | 设计 / 架构 / schema / 系统设计  | MiniMax-M3        | deepseek-v4-pro   |
| plan       | 📋    | 计划 / 拆分 / task list          | deepseek-v4-pro   | MiniMax-M3        |
| implement  | ⚙️    | 实现 / 写代码 / fix / 开发       | MiniMax-M3        | deepseek-v4-flash |
| audit      | 🔍    | 审计 / review / 安全 / 测试      | deepseek-v4-pro   | MiniMax-M3        |
| default    | 🔄    | 兜底                             | MiniMax-M3        | deepseek-v4-flash |

### 维度二：Operation（操作类型路由）

平行于 stage 的第二维度，用于微调模型选择。**检出 op 时完全覆盖 stage 路由**。

| 操作     | emoji | 检测关键词（部分）          | 主模型     | 备用模型          |
| -------- | ----- | --------------------------- | ---------- | ----------------- |
| write    | ✏️    | 写 / 更新 / 编辑 / 创建     | MiniMax-M3 | deepseek-v4-flash |
| read     | 👁️    | 读一下 / 查看 / 理解 / 解释 | MiniMax-M3 | deepseek-v4-pro   |
| search   | 🔎    | 搜索 / 查找 / 检索          | MiniMax-M3 | deepseek-v4-flash |
| refactor | 🔧    | 重构 / 整理 / 优化结构      | MiniMax-M3 | deepseek-v4-pro   |

### 维度三：Model Override（用户显式指定模型）

**最高路由优先级**。用户可以用 `~model` 指令或自然语言强制指定模型。

```
~model ds-v4-pro       ← 显式指令
~m mm3                  ← 短指令
用 deepseek-v4-flash    ← 自然中文
use mm3                ← 自然英语
~m reset               ← 清除覆盖，恢复自动路由
```

### 三者同时存在的示例

用户输入：

```
~model ds-flash 帮我 review 一下这个代码 ~audit
```

- `detect_model_override` 命中 → model = `deepseek-v4-flash`
- `detect_stage` 命中关键词 "review" → stage = `audit`
- `detect_operation` 未命中 → op = None

最终路由：model override = `deepseek-v4-flash`（最高优先级覆盖 audit 的模型选择）。

---

## 文件系统状态管理

### Per-Session 文件

每个 session 有自己独立的状态文件，互不干扰。

```
<project_root>/.claude/
├── stage_<session_id>      ← 当前阶段（纯文本）
├── op_<session_id>         ← 操作类型覆盖（可选）
├── model_<session_id>      ← 模型覆盖（可选）
├── fallback_<session_id>   ← sticky fallback 标记（可选）
└── session_state_<sid>.json ← CC 原生会话状态
```

**文件命名规则**：所有文件以 `stage_<sid>` 为基础，通过 `string.replace("stage_", "op_", 1)` 派生出 op/model/fallback 文件名，确保同目录。

### active_session 指针

Proxy 没有 stdin 上下文，无法知道当前 session_id。所以设计了一个**单文件指针**：

```
~/.claude/hooks/model_router/active_session
→ 内容: /Users/zorro/my-project/.claude/stage_a1b2c3d4
```

- **由 Hook 写入**：每次 UserPromptSubmit 触发时，更新指针指向当前 session 的 stage\_<sid> 完整路径
- **由 Proxy 读取**：每次 API 请求到达时，先读这个指针，拿到完整路径，再派生出 op/model/fallback 路径

### 项目根目录查找策略

当脚本（Hook / CLI）需要确定 session 文件放哪里时，按以下优先级向上遍历：

1. 存在 `stage_<sid>` 或 `session_state_<sid>.json` 的 `.claude/` 目录 → 其父目录即为项目根
2. `.claude/` 目录（跳过全局 `~/.claude`）
3. `.git/` toplevel
4. 兜底：`~/.claude`

---

## Sticky Fallback 机制

当主模型上游 API 返回可重试错误（401/402/403/429/5xx）时，Proxy 自动切换到备用模型。一旦备用模型成功，**该 session 从此固定使用备用模型**，避免每轮请求都重试已挂掉的主模型。

### 触发条件

```python
def _is_retriable(status: int) -> bool:
    return status in (401, 402, 403, 429) or (500 <= status < 600) or status == 0
```

- **纳入**：401（key 错切 provider）、402（余额不足）、429（限流）、5xx（服务端故障）、0（网络超时）
- **不纳入**：400（请求体格式错）、404（模型不存在）、422（参数错误）——切了也没用

### 执行流程

```
请求 → 主模型失败（可重试）
  → 切备用模型
    → 备用成功 → 写入 fallback_<sid> → 后续请求自动用备用
    → 备用也失败 → 返回错误给 CC（不做二级 fallback）
```

### 清除条件

以下任一操作会清除 sticky fallback：

- 用户切换 stage（`~stage implement`）
- 用户显式指定模型（`~model ds-v4-pro`）
- 用户清除模型覆盖（`~m reset`）

---

## 协议支持

### Anthropic 协议（默认）

仅改写请求 body 中的 `model` 字段，不做格式转换。适用于 MiniMax、DeepSeek 等 Anthropic Messages API 兼容端点。

### OpenAI 协议（opt-in）

自动做 Anthropic Messages ↔ OpenAI Chat Completions 格式转换。适用于硅基流动等 OpenAI 兼容端点。

### Thinking Block 处理

对于支持 extended thinking 的模型：

- **原生 Anthropic 端点**（`api.anthropic.com`）：保留完整 thinking 能力，不做任何降级
- **非原生端点**（MiniMax / DeepSeek）：删除顶层 `thinking` 参数（阻止上游进入 extended thinking 模式），但保留历史消息中的 `thinking` content block 原样透传（不转为 text，防止 signature 校验失败报 400）

### 响应模型名遮罩

Proxy 将上游响应体中的 `model` 字段改写为 CC 能识别的原始模型名（如 `MiniMax-M3`），避免 CC 记录内部别名（如 `deepseek-v4-flash`）后重启报 "not a model this version recognizes"。

---

## 命令参考

### CC Prompt 内指令（不限制位置）

| 指令                               | 作用                   | 示例                 |
| ---------------------------------- | ---------------------- | -------------------- |
| `~stage <name>`                    | 切换阶段               | `~stage implement`   |
| `~<write\|read\|search\|refactor>` | 切换操作类型           | `~read`              |
| `~model <alias>`                   | 指定模型（最高优先级） | `~model ds-v4-flash` |
| `~m <alias>`                       | 同 `~model` 短格式     | `~m mm3`             |
| `~m reset`                         | 清除模型覆盖           | `~m reset`           |

### Shell CLI

| 命令              | 作用              |
| ----------------- | ----------------- |
| `stage`           | 查看当前阶段和 op |
| `stage <name>`    | 手动设置阶段      |
| `stage op <name>` | 手动设置操作类型  |
| `stage op reset`  | 清除 op           |
| `stage op list`   | 列出可用 op       |
| `stage proxy`     | 启动代理          |
| `stage status`    | 查看代理健康状态  |
| `stage log`       | 实时查看路由日志  |
| `stage reset`     | 重置为 default    |

### 支持的模型别名

| 简写                                          | 完整模型名          |
| --------------------------------------------- | ------------------- |
| `ds-v4-pro` / `ds-pro` / `deepseek-pro`       | `deepseek-v4-pro`   |
| `ds-v4-flash` / `ds-flash` / `deepseek-flash` | `deepseek-v4-flash` |
| `mm3` / `mm-m3` / `minimax` / `mm`            | `MiniMax-M3`        |
| `sonnet` / `claude-sonnet`                    | `claude-sonnet-4-6` |
| `opus` / `claude-opus`                        | `claude-opus-4-8`   |

---

## 安装与启动

```bash
# 1. 安装
cd ~/.claude/hooks/model_router
bash install.sh

# 2. 配置 API Keys
vim .env
# MINIMAX_API_KEY=eyJ...
# DEEPSEEK_API_KEY=sk-...

# 3. 启动代理（终端 1）
stage proxy

# 4. 启动 Claude Code（终端 2）
export ANTHROPIC_BASE_URL=http://127.0.0.1:7878
claude
```

install.sh 会完成：

1. 复制 Hook 脚本到 `~/.claude/hooks/model_router/`
2. 创建 `stage` CLI 到 `~/.local/bin/stage`（符号链接）
3. 从 `.env.example` 创建 `.env`（如不存在）
4. 初始化全局后备阶段文件

---

## 配置文件唯一数据源原则

所有阶段/操作/模型映射集中在 **`stage_config.py`** 中。其他模块从这里导入派生视图：

```
stage_config.py
  ├── STAGE_CONFIG / OPERATION_CONFIG  ← 手写配置（唯一数据源）
  ├── STAGE_MODELS / OPERATION_MODELS  ← proxy.py 用
  ├── FALLBACK_MODELS                  ← proxy.py 用
  ├── STAGE_DISPLAY / OPERATION_DISPLAY ← stage_show.py 用
  ├── STAGE_DESC / OPERATION_DESC      ← stage CLI 用
  ├── STAGE_INFO / OPERATION_INFO      ← stage_detector.py 用
  └── MODEL_TO_CONFIG                  ← proxy.py 用（反向索引）
```

修改模型映射**只改 `stage_config.py`**，所有消费方自动同步。

---

## 设计决策（Key Decisions）

### 1. 为什么不用内置 switch 模型？

CC 虽然有 `/compact` 指令切换模型，但这是全量切换——所有请求都走同一个模型。Stage Router 能在同一次会话中根据任务类型自动切换，而且支持跨 provider 故障转移。

### 2. 为什么文件系统通信而不是 IPC？

- **无侵入**：Hook 和 Proxy 都是独立进程，无需修改 CC 源码
- **易调试**：`echo "implement" > stage_<sid>` 就能手动控制阶段
- **可观测**：`cat active_session` 就能知道当前状态
- **松耦合**：Hook 和 Proxy 谁挂了都不影响对方

### 3. 为什么 per-session 而不是全局？

多个 CC 窗口可能共用一个 proxy。如果全局一个 stage 文件，A 窗口切到 audit，B 窗口的编码请求会莫名其妙被路由到审计模型。per-session 隔离避免了这种交叉污染。

### 4. 为什么 `~` 前缀而非 `/`？

Claude Code 有大量内置 `/` 命令（`/help`, `/compact`, `/diff` 等）。用 `/stage` 可能和未来 CC 内置命令冲突。`~` 在 CC 中没有特殊含义，可安全使用。

### 5. 为什么 model override 不用文件？

如果 Proxy 端拿不到 session*id，那 model override 必须也走文件。所以才设计了 `model*<sid>`文件，让 Proxy 从`active_session` 指针派生读取。

### 6. 为什么关键词检测用 `in` 而非 NLP？

简单可靠。CC 场景下，用户说"帮我实现登录"里的"实现"足以触发 implement 阶段。不需要 NLP 分类器的复杂度和延迟。关键词列表见 `stage_detector.py` 中的 `STAGE_KEYWORDS` 和 `OPERATION_KEYWORDS`。

### 7. 为什么 staging 文件路径规则用 `replace("stage_", "op_")` 派生？

```
stage_<sid>  →  replace("stage_", "op_")  →  op_<sid>
             →  replace("stage_", "model_") →  model_<sid>
             →  replace("stage_", "fallback_") →  fallback_<sid>
```

所有派生文件共享同一目录，仅前缀替换。这个规则在 `stage_detector.py`、`proxy.py`、`stage_show.py`、`stage` CLI 中**完全一致地实现**。修改任一文件路径规则，只需改 `active_session` 指针的内容（指向新的 stage\_<sid> 路径），所有派生路径自动跟随。

---

## 调试

```bash
# 模拟 Hook 输入
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

# 查看 Hook 日志
cat /tmp/stage_detector.log
```

### /health 响应示例（有 model override）

```json
{
  "status": "ok",
  "model_override": "deepseek-v4-flash",
  "op": null,
  "stage": null,
  "model": "deepseek-v4-flash",
  "protocol": "anthropic",
  "fallback": "MiniMax-M3",
  "routing_source": "model=deepseek-v4-flash"
}
```

### /health 响应示例（走 stage 路由）

```json
{
  "status": "ok",
  "model_override": null,
  "op": null,
  "stage": "implement",
  "model": "MiniMax-M3",
  "protocol": "anthropic",
  "fallback": "deepseek-v4-flash",
  "routing_source": "stage=implement"
}
```

---

## 文件清单

```
~/.claude/hooks/model_router/
├── ARCHITECTURE.md           ← 本文档
├── README.md                 ← 快速入门文档
├── stage_config.py           ← 唯一数据源（模型映射配置）
├── stage_detector.py         ← UserPromptSubmit Hook（关键词检测 + 文件写入）
├── proxy.py                  ← HTTP 代理服务器（路由决策 + 请求转发）
├── stage_show.py             ← Stop Hook（终端显示当前路由状态）
├── stage                     ← shell CLI（阶段/op 管理）
├── model_alias.py            ← 模型简写映射（ds-v4-pro → deepseek-v4-pro）
├── install.sh                ← 安装脚本
├── .env                      ← API Keys（gitignored）
├── .env.example              ← API Keys 模板
├── active_session            ← 活跃 session 指针（hook 自动维护）
└── current_stage             ← 全局后备阶段名
```
