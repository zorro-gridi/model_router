"""
stage_config.py — 阶段→模型映射 统一配置
==========================================

本文件是 hooks/model_router/ 目录中 stage ↔ model 映射的**唯一数据源**。
proxy.py、stage_show.py、stage_detector.py、stage CLI 均从此导入，
确保所有组件展示和路由的模型一致。

修改流程：
  1. 只修改本文件的 STAGE_CONFIG 字典
  2. 所有导入方自动同步，无需逐个修改

字段说明：
  emoji      — 终端展示图标
  label      — 阶段中文名
  desc       — 阶段功能描述（用于 stage CLI）
  model      — 上游模型名
  base_url   — 上游 API 基础地址
  api_key_env— 对应的环境变量名
  protocol   — "anthropic" | "openai"

────────────────────────────────────────────────────────────────────
Operation-type 路由（与 stage 并列的第二维度，2026-06-13 引入）
────────────────────────────────────────────────────────────────────
OPERATION_CONFIG 是与 STAGE_CONFIG 同构的 4 元组表（write/read/search/refactor），
用于按 prompt 操作类型微调模型选择。proxy.py 端在 stage 路由之上叠加：
  - 检出 op → 完全覆盖 stage 路由
  - 未检出 op → 走 stage 路由（与升级前行为一致）
base_url / api_key_env / protocol 字段与 STAGE_CONFIG 复用同 model 的现有值，
不重复硬编码字符串。
"""

# ═══════════════════════════════════════════════════════════════════════════
# 统一配置（唯一数据源）
#
# 每个 stage 配主模型 + 备用模型。备用模型选择跨 provider 的模型，
# 避免因同一 provider token 耗尽/网络不通导致备路也失败。
# ═══════════════════════════════════════════════════════════════════════════

STAGE_CONFIG: dict[str, dict] = {
    "brainstorm": {
        "emoji":       "💭",
        "label":       "头脑风暴",
        "desc":        "快速发散，低成本探索",
        "model":       "deepseek-v4-flash",
        "base_url":    "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "protocol":    "anthropic",
        # 备用：MiniMax
        "fb_model":       "MiniMax-M3",
        "fb_base_url":    "https://api.minimaxi.com/anthropic",
        "fb_api_key_env": "MINIMAX_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "decide": {
        "emoji":       "⚖️",
        "label":       "决策分析",
        "desc":        "深度推理，权衡分析",
        "model":       "deepseek-v4-pro",
        "base_url":    "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "protocol":    "anthropic",
        "fb_model":       "MiniMax-M3",
        "fb_base_url":    "https://api.minimaxi.com/anthropic",
        "fb_api_key_env": "MINIMAX_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "design": {
        "emoji":       "🏗️",
        "label":       "方案设计",
        "desc":        "系统架构，方案设计",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 备用：deepseek-v4-flash（方案设计成本不敏感，降级到便宜模型）
        "fb_model":       "deepseek-v4-flash",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "plan": {
        "emoji":       "📋",
        "label":       "任务拆解",
        "desc":        "任务拆解，结构化输出",
        "model":       "deepseek-v4-pro",
        "base_url":    "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "protocol":    "anthropic",
        "fb_model":       "MiniMax-M3",
        "fb_base_url":    "https://api.minimaxi.com/anthropic",
        "fb_api_key_env": "MINIMAX_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "implement": {
        "emoji":       "⚙️",
        "label":       "工程实施",
        "desc":        "主力编码，工程实施",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 备用：deepseek-v4-flash（便宜，MiniMax 挂了降级到低成本模型也不心疼）
        "fb_model":       "deepseek-v4-flash",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "audit": {
        "emoji":       "🔍",
        "label":       "工程审计",
        "desc":        "严格检查，安全审计",
        "model":       "deepseek-v4-pro",
        "base_url":    "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "protocol":    "anthropic",
        "fb_model":       "MiniMax-M3",
        "fb_base_url":    "https://api.minimaxi.com/anthropic",
        "fb_api_key_env": "MINIMAX_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "default": {
        "emoji":       "🔄",
        "label":       "默认",
        "desc":        "兜底默认",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 备用：deepseek-v4-flash（兜底场景也用低成本模型）
        "fb_model":       "deepseek-v4-flash",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# 派生视图（为兼容各消费方现有代码的访问模式）
# ═══════════════════════════════════════════════════════════════════════════

# proxy.py 用：stage → (base_url, model, api_key_env, protocol)
STAGE_MODELS: dict[str, tuple[str, str, str, str]] = {
    stage: (c["base_url"], c["model"], c["api_key_env"], c["protocol"])
    for stage, c in STAGE_CONFIG.items()
}

# proxy.py 用：stage → 备用 (fb_base_url, fb_model, fb_api_key_env, fb_protocol)
FALLBACK_MODELS: dict[str, tuple[str, str, str, str]] = {
    stage: (c["fb_base_url"], c["fb_model"], c["fb_api_key_env"], c["fb_protocol"])
    for stage, c in STAGE_CONFIG.items()
}

# stage_show.py 用：stage → (emoji, label, model)
STAGE_DISPLAY: dict[str, tuple[str, str, str]] = {
    stage: (c["emoji"], c["label"], c["model"])
    for stage, c in STAGE_CONFIG.items()
}

# stage CLI 用：stage → 格式化描述行
STAGE_DESC: dict[str, str] = {
    stage: f"{c['emoji']} {c['model']:20s} — {c['desc']}"
    for stage, c in STAGE_CONFIG.items()
}

# stage_detector.py 用：stage → "阶段名 → 模型，简述"
STAGE_INFO: dict[str, str] = {
    stage: f"{c['label']}阶段 → {c['model']}，{c['desc']}"
    for stage, c in STAGE_CONFIG.items()
}

# ═══════════════════════════════════════════════════════════════════════════
# Operation-type 路由（与 stage 并列的第二维度）
#
# 设计原则：op 完全覆盖 stage 路由（"我说 search 就是 search"）。
# base_url / api_key_env / protocol 复用 STAGE_CONFIG 中同 model 的现有值。
# ═══════════════════════════════════════════════════════════════════════════

OPERATION_CONFIG: dict[str, dict] = {
    "write": {
        "emoji":       "✏️",
        "label":       "写入",
        "desc":        "主 MiniMax-M3，便宜 fallback",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 备用：deepseek-v4-flash（便宜，写错了也不心疼）
        "fb_model":       "deepseek-v4-flash",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "read": {
        "emoji":       "👁️",
        "label":       "读取",
        "desc":        "主 MiniMax-M3，稳 fallback",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 备用：deepseek-v4-pro（读不准的成本 > 写错的成本）
        "fb_model":       "deepseek-v4-pro",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "search": {
        "emoji":       "🔎",
        "label":       "搜索",
        "desc":        "主 deepseek-v4-flash，备 MiniMax-M3",
        "model":       "deepseek-v4-flash",
        "base_url":    "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "protocol":    "anthropic",
        # 备用：MiniMax-M3（探索任务 fallback 升档）
        "fb_model":       "MiniMax-M3",
        "fb_base_url":    "https://api.minimaxi.com/anthropic",
        "fb_api_key_env": "MINIMAX_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "refactor": {
        "emoji":       "🔧",
        "label":       "重构",
        "desc":        "主 MiniMax-M3，备 deepseek-v4-pro",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 备用：deepseek-v4-pro（结构改动需要稳妥的推理）
        "fb_model":       "deepseek-v4-pro",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# 派生视图（Operation-type，与 STAGE_* 镜像同构）
# ═══════════════════════════════════════════════════════════════════════════

# proxy.py 用：op → (base_url, model, api_key_env, protocol)
OPERATION_MODELS: dict[str, tuple[str, str, str, str]] = {
    op: (c["base_url"], c["model"], c["api_key_env"], c["protocol"])
    for op, c in OPERATION_CONFIG.items()
}

# proxy.py 用：op → 备用 (fb_base_url, fb_model, fb_api_key_env, fb_protocol)
OPERATION_FALLBACK_MODELS: dict[str, tuple[str, str, str, str]] = {
    op: (c["fb_base_url"], c["fb_model"], c["fb_api_key_env"], c["fb_protocol"])
    for op, c in OPERATION_CONFIG.items()
}

# stage_show.py 用：op → (emoji, label, model)
OPERATION_DISPLAY: dict[str, tuple[str, str, str]] = {
    op: (c["emoji"], c["label"], c["model"])
    for op, c in OPERATION_CONFIG.items()
}

# stage CLI 用：op → 格式化描述行
OPERATION_DESC: dict[str, str] = {
    op: f"{c['emoji']} {c['model']:20s} — {c['desc']}"
    for op, c in OPERATION_CONFIG.items()
}

# stage_detector.py 用：op → "操作类型 → 模型，简述"
OPERATION_INFO: dict[str, str] = {
    op: f"{c['label']}操作 → {c['model']}，{c['desc']}"
    for op, c in OPERATION_CONFIG.items()
}

# ═══════════════════════════════════════════════════════════════════════════
# 反向索引：model → (base_url, model, api_key_env, protocol)
# 从 STAGE_CONFIG + OPERATION_CONFIG 的主模型和备用模型收集。
# proxy.py 用于 sticky fallback：当主模型不可用后，直接从 fallback 模型名
# 反查出完整的路由配置（不再需要知道原 stage/op），也用于 model_override 路由。
# ═══════════════════════════════════════════════════════════════════════════

MODEL_TO_CONFIG: dict[str, tuple[str, str, str, str]] = {}
for c in STAGE_CONFIG.values():
    MODEL_TO_CONFIG[c["model"]] = (c["base_url"], c["model"], c["api_key_env"], c["protocol"])
    MODEL_TO_CONFIG[c["fb_model"]] = (c["fb_base_url"], c["fb_model"], c["fb_api_key_env"], c["fb_protocol"])
for c in OPERATION_CONFIG.values():
    # OPERATION_CONFIG 可能使用与 STAGE_CONFIG 相同的模型名，值应一致
    MODEL_TO_CONFIG.setdefault(c["model"], (c["base_url"], c["model"], c["api_key_env"], c["protocol"]))
    MODEL_TO_CONFIG.setdefault(c["fb_model"], (c["fb_base_url"], c["fb_model"], c["fb_api_key_env"], c["fb_protocol"]))
