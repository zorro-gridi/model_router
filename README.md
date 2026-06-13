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

## 核心概念

### 三路由维度（优先级从高到低）

| 维度             | 文件          | 设置方式                               | 说明                                |
| ---------------- | ------------- | -------------------------------------- | ----------------------------------- |
| ① Model Override | `model_<sid>` | `!model ds-v4-pro` / `用 mm3`          | 用户显式指定，完全覆盖其他维度      |
| ② Operation-type | `op_<sid>`    | prompt 关键词 (`写`/`search`/`review`) | 按操作类型微调，完全覆盖 stage 路由 |
| ③ Stage          | `stage_<sid>` | prompt 关键词 (`实现`/`架构`/`审计`)   | 默认路由维度                        |

三者的关系：**model override > op > stage**。op 和 model override 都是可选的——未检出时回退到 stage 路由。

### 阶段映射

| 阶段       | emoji | 主模型            | 备用模型        | 适用场景             |
| ---------- | ----- | ----------------- | --------------- | -------------------- |
| brainstorm | 💭    | deepseek-v4-flash | MiniMax-M3      | 快速发散，低成本探索 |
| decide     | ⚖️    | deepseek-v4-pro   | MiniMax-M3      | 深度推理，权衡分析   |
| design     | 🏗️    | MiniMax-M3        | deepseek-v4-pro | 系统架构，方案设计   |
| plan       | 📋    | deepseek-v4-pro   | MiniMax-M3      | 任务拆解，结构化输出 |
| implement  | ⚙️    | MiniMax-M3        | deepseek-v4-pro | 主力编码，工程实施   |
| audit      | 🔍    | deepseek-v4-pro   | MiniMax-M3      | 严格检查，安全审计   |
| default    | 🔄    | MiniMax-M3        | deepseek-v4-pro | 兜底默认             |

### 操作类型映射（第二维度）

| 操作     | emoji | 主模型            | 备用模型          | 说明                   |
| -------- | ----- | ----------------- | ----------------- | ---------------------- |
| write    | ✏️    | MiniMax-M3        | deepseek-v4-flash | 写入，便宜 fallback    |
| read     | 👁️    | MiniMax-M3        | deepseek-v4-pro   | 读取，稳 fallback      |
| search   | 🔎    | deepseek-v4-flash | MiniMax-M3        | 探索任务 fallback 升档 |
| refactor | 🔧    | MiniMax-M3        | deepseek-v4-pro   | 结构改动需稳妥推理     |

## Session 状态持久化（关键设计）

```
<project_root>/.claude/
├── stage_<session_id>        ← 当前阶段（纯文本，如 "implement"）
├── op_<session_id>           ← 操作类型覆盖（纯文本，可选）
├── model_<session_id>        ← 模型覆盖（纯文本，可选）
├── fallback_<session_id>     ← sticky fallback 标记（纯文本，可选）
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
| "拆一下任务" / "怎么做"       | plan       | deepseek-v4-pro   |
| "实现这个功能" / "修一下 bug" | implement  | MiniMax-M3        |
| "review 代码" / "安全检查"    | audit      | deepseek-v4-pro   |

操作类型关键词平行检测（不改变 stage，仅覆盖模型选择）：

| 你说               | 检测到的操作 | 路由覆盖          |
| ------------------ | ------------ | ----------------- |
| "把这个功能写一下" | write        | MiniMax-M3        |
| "看看这个文件"     | read         | MiniMax-M3        |
| "搜索一下这个接口" | search       | deepseek-v4-flash |
| "重构这个方法"     | refactor     | MiniMax-M3        |

### 方式 2：显式命令

CC 对话框内（优先级最高）：

```
/stage implement
/stage audit
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
```

### 方式 4：模型别名指令（最高优先级）

在 prompt 中任意位置指定模型，覆盖所有自动路由：

```
用 ds-v4-pro
!model ds-flash
use mm3
!m reset           # 清除覆盖，回到自动路由
```

支持的别名：`ds-v4-pro`, `ds-pro`, `ds-v4-flash`, `ds-flash`, `mm3`, `mm`, `sonnet`, `opus` 等。

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

没有"继承上一个 session 的状态"的设计——每个 session 是独立的。如果有跨 session 的偏好，请用 `settings.json` 的环境变量或 `!model` 指令在 prompt 中指定。

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
