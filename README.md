# Stage-Aware Model Router

Claude Code 阶段感知模型路由系统。根据当前工作流阶段，自动将 Claude Code 的请求路由到最合适的模型，实现智能降本增效。

## 支持的 Provider

| Provider                   | 端点                                 | 模型                                   |
| -------------------------- | ------------------------------------ | -------------------------------------- |
| MiniMax（Anthropic 协议）  | `https://api.minimaxi.com/anthropic` | `MiniMax-M3`                           |
| DeepSeek（Anthropic 协议） | `https://api.deepseek.com/anthropic` | `deepseek-v4-pro`, `deepseek-v4-flash` |

> **协议说明**：本代理端到端都是 **Anthropic Messages API 协议**——Claude Code 发出 Anthropic 格式请求，本地代理只做 `model` 字段改写 + `x-api-key` 注入，再透传到上游 Anthropic 兼容端点。不做 OpenAI 协议转换（除非显式开启，见"自定义模型映射"）。

## 架构

```
Claude Code
    │  ANTHROPIC_BASE_URL=http://127.0.0.1:7878
    ▼
proxy.py（本地代理，Anthropic 协议透传）
    │  读取 ~/.claude/stage
    ├─ brainstorm → DeepSeek deepseek-v4-flash       （cheap & fast）
    ├─ decide     → MiniMax MiniMax-M3               （deep reasoning）
    ├─ design     → MiniMax MiniMax-M3               （architecture）
    ├─ plan       → DeepSeek deepseek-v4-pro         （structured）
    ├─ implement  → DeepSeek deepseek-v4-pro         （workhorse）
    ├─ audit      → MiniMax MiniMax-M3               （strict review）
    └─ default    → DeepSeek deepseek-v4-pro         （fallback）

Hooks（与代理解耦，独立运行）
    UserPromptSubmit → stage_detector.py  → 写 ~/.claude/stage
    Stop             → stage_show.py      → 终端显示当前阶段
```

**关键设计**：Hook 和代理完全解耦。Hook 只负责写文件，代理读文件决策。两者通过 `~/.claude/stage` 这一个文件通信，互不阻塞。

## 安装

```bash
bash install.sh
```

安装脚本会：

1. 复制 hooks 到 `~/.claude/hooks/model_router/`
2. 复制 `stage` CLI 到 `~/.local/bin/`
3. 在 `~/.claude/settings.json` 中注册 `UserPromptSubmit` 和 `Stop` hook
4. 初始化 `~/.claude/stage` 为 `default`

## 配置 API Keys

`proxy.py` 和 `stage` CLI 启动时会自动从**本插件目录**下的 `.env` 文件加载环境变量，无需配置 `~/.zshrc`。

```bash
# 安装时已自动从 .env.example 复制为 .env（如不存在可手动复制）
cp ~/.claude/hooks/model_router/.env.example ~/.claude/hooks/model_router/.env
chmod 600 ~/.claude/hooks/model_router/.env   # 保护 API key

# 编辑填入真实 key
vim ~/.claude/hooks/model_router/.env
```

`.env` 文件格式（参考 `.env.example`）：

```bash
# MiniMax（https://api.minimaxi.com/anthropic）
MINIMAX_API_KEY=eyJ...

# DeepSeek（https://api.deepseek.com/anthropic）
DEEPSEEK_API_KEY=sk-...
```

`.env` 已被 `~/.claude/.gitignore`（`hooks/**/.env`）屏蔽，不会进 git。

> **加载优先级**：`shell 环境变量` > `.env`。如果两者都设了，shell 里的 export 优先。这让你可以在不修改 `.env` 的情况下临时切换 key。
>
> **启动校验**：`proxy.py` 启动时会检查所有 stage 需要的 key，缺一个就立即报错退出（不会跑起来后再 500）。同时日志会打印已加载 key 的**末 4 位**，方便确认配置是否正确。

最后让 CC 流量走本地代理（替换原来的直连）：

```bash
# ~/.zshrc 或本终端
export ANTHROPIC_BASE_URL="http://127.0.0.1:7878"
```

> **迁移提示**：如果你之前是 `ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic` 直连 MiniMax，需要把那个 key 从 `ANTHROPIC_AUTH_TOKEN` 复制到 `MINIMAX_API_KEY`（不同 stage 会用到不同的 key，互相隔离）。

## 启动

```bash
# 终端 1：启动代理
stage proxy

# 终端 2：启动 Claude Code（确保 ANTHROPIC_BASE_URL 已指向 127.0.0.1:7878）
claude
```

## 阶段切换

### 方式 1：CC 内自动检测（UserPromptSubmit Hook）

直接用自然语言，Hook 会自动识别关键词：

| 你说的关键词                   | 识别的阶段 | 路由到            |
| ------------------------------ | ---------- | ----------------- |
| "头脑风暴一下" / "想想方向"    | brainstorm | deepseek-v4-flash |
| "对比一下两个方案" / "权衡"    | decide     | MiniMax-M3        |
| "设计一下架构" / "数据模型"    | design     | MiniMax-M3        |
| "拆一下任务" / "怎么做"        | plan       | deepseek-v4-pro   |
| "实现这个功能" / "写代码"      | implement  | deepseek-v4-pro   |
| "review 一下代码" / "安全检查" | audit      | MiniMax-M3        |

### 方式 2：CC 内显式命令（优先级最高）

在 CC 对话框输入：

```
/stage implement
/stage audit
/stage brainstorm
```

### 方式 3：Shell 命令

```bash
stage implement   # 切换阶段
stage             # 查看当前阶段
stage status      # 查看代理状态
stage reset       # 重置为 default
stage log         # 实时查看路由日志
```

## 自定义模型映射

编辑 `proxy.py` 顶部的 `STAGE_MODELS` 字典，4 元组格式：

```python
STAGE_MODELS = {
    "brainstorm": (
        "https://api.deepseek.com/anthropic",   # base_url
        "deepseek-v4-flash",                    # model
        "DEEPSEEK_API_KEY",                     # 环境变量名
        "anthropic",                            # protocol: anthropic | openai
    ),
    # 如果想接 OpenAI 兼容 provider（如硅基流动），把 protocol 改成 "openai"：
    # "brainstorm": (
    #     "https://api.siliconflow.cn/v1",
    #     "Qwen/Qwen2.5-72B-Instruct",
    #     "SILICONFLOW_API_KEY",
    #     "openai",       # 自动做 Anthropic ↔ OpenAI 协议转换
    # ),
}
```

- `protocol="anthropic"`（默认）：透明转发，不做格式转换
- `protocol="openai"`：自动转换 Anthropic Messages ↔ OpenAI Chat Completions

## 文件说明

```
~/.claude/
├── stage                    ← 当前阶段（brainstorm/decide/design/plan/implement/audit/default）
├── stage-router.log         ← 路由日志
├── settings.json            ← CC 配置（含 Hook 注册）
└── hooks/
    └── model_router/
        ├── .env              ← API Keys（gitignored，自动加载）
        ├── .env.example      ← 模板
        ├── stage_detector.py ← UserPromptSubmit Hook：自动检测阶段
        ├── stage_show.py     ← Stop Hook：显示当前阶段
        └── proxy.py          ← 本地代理服务器

~/.local/bin/
└── stage                    ← 阶段管理 CLI
```

## 调试

```bash
# 测试 Hook 是否正常（模拟 CC 输入）
echo '{"prompt": "帮我实现一个登录功能"}' | python3 ~/.claude/hooks/model_router/stage_detector.py

# 测试代理连通性
curl http://127.0.0.1:7878/health
# → {"status": "ok", "stage": "...", "model": "...", "protocol": "anthropic"}

# 查看完整路由日志
stage log

# 干运行模式（只打印路由决策，不实际转发）
python3 ~/.claude/hooks/model_router/proxy.py --dry-run

# 模拟一次完整请求（不真正打到上游）
curl -X POST http://127.0.0.1:7878/v1/messages \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-api-key: dummy" \
  -d '{"model":"MiniMax-M3","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}'
```

## 故障排查

| 症状                                   | 排查                                                                                                                             |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `curl /health` 返回 connection refused | 代理未启动，跑 `stage proxy`                                                                                                     |
| 启动报 "缺少必需的 API key"            | 编辑 `~/.claude/hooks/model_router/.env` 填入 `MINIMAX_API_KEY` / `DEEPSEEK_API_KEY`（或 shell export）                          |
| 上游返回 401                           | 对应 stage 的 API key 未配置/错误，启动日志会打印已加载 key 的末 4 位可对照确认                                                  |
| 上游返回 404 / 模型不存在              | `STAGE_MODELS` 中的 model 名拼写错误，对照 provider 文档核对                                                                     |
| 上游返回 400 + 协议错误                | 99% 是 protocol 字段配错：base_url 是 `/anthropic` 路径就要写 `anthropic`，是 `/v1` 或 `/compatible-mode/v1` 之类的才写 `openai` |
| Hook 触发但 stage 不变                 | 看 `~/.claude/stage-router.log`，检查关键词匹配是否命中                                                                          |

## 成本估算

典型项目中各阶段占比：

| 阶段       | 占比 | 模型              | 相对成本（vs deepseek-v4-flash） |
| ---------- | ---- | ----------------- | -------------------------------: |
| brainstorm | 5%   | deepseek-v4-flash |                               1× |
| decide     | 5%   | MiniMax-M3        |                             ~15× |
| design     | 10%  | MiniMax-M3        |                             ~15× |
| plan       | 5%   | deepseek-v4-pro   |                              ~5× |
| implement  | 65%  | deepseek-v4-pro   |                              ~5× |
| audit      | 10%  | MiniMax-M3        |                             ~15× |

vs. 全程 MiniMax-M3：加权成本约为原来的 **~50%**（具体取决于 MiniMax 与 DeepSeek 的实际单价对比）。
