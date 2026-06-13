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
        "fb_model":       "deepseek-v4-pro",
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
        "fb_model":       "deepseek-v4-pro",
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
        "fb_model":       "deepseek-v4-pro",
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
