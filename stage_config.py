"""
stage_config.py — 阶段 × 复杂度 × 模式 统一配置
================================================

本文件是 hooks/model_router/ 目录中 stage / operation / pattern / complexity
映射的**唯一数据源**。proxy.py、stage_show.py、stage_detector.py、stage CLI
均从此导入，确保所有组件展示和路由的模型一致。

修改流程：
  1. 只修改本文件的 STAGE_CONFIG / OPERATION_CONFIG / PATTERN_CONFIG /
     COMPLEXITY_CONFIG 字典
  2. 所有导入方自动同步，无需逐个修改

字段说明：
  emoji / label / desc — 展示与描述
  model               — 上游模型名
  base_url            — 上游 API 基础地址
  api_key_env         — 对应的环境变量名
  protocol            — "anthropic" | "openai"
  fb_*                — fallback 模型（同构字段）

────────────────────────────────────────────────────────────────────
2026-06-14 升级要点（按设计文档 V1.2 第 7/11 章）
────────────────────────────────────────────────────────────────────
- 默认模型策略对齐：
    MiniMax-M3      — 多数 simple/medium 任务的主模型
    DeepSeek-V4-Pro — 升级模型（plan/audit/decide 等高阶推理）
    DeepSeek-V4-Flash — 降级模型（brainstorm 等低成本场景）
- 新增 PATTERN_CONFIG（设计文档第 8 章）：Pattern Library 列表 + 默认流程 +
  默认复杂度，供 stage_detector 关键词识别 + 未来 Workflow Planner 使用。
- 新增 COMPLEXITY_CONFIG（设计文档第 9 章）：simple / medium / complex 阈值
  + 推荐策略，供 ~careful / ~quick 调整指令使用。
- Stage 表（设计文档第 7 章）按"默认 MiniMax-M3 / 升级 deepseek-v4-pro /
  降级 deepseek-v4-flash"统一规范。
────────────────────────────────────────────────────────────────────
"""

# ═══════════════════════════════════════════════════════════════════════════
# Stage 配置（设计文档第 7 章）
#
# 默认模型策略：
#   - explore/plan/design/implement/test/audit/default → MiniMax-M3 为主
#   - brainstorm/decide → 走更便宜的 deepseek 路径（发散与决策）
# 升级模型：deepseek-v4-pro（需要稳妥推理时）
# 降级模型：deepseek-v4-flash（成本敏感或主模型不可用时）
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
        # 升级：deepseek-v4-pro（架构设计要稳妥推理）
        "fb_model":       "deepseek-v4-pro",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "plan": {
        "emoji":       "📋",
        "label":       "任务拆解",
        "desc":        "任务拆解，结构化输出",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 升级：deepseek-v4-pro（拆解复杂任务需要强推理）
        "fb_model":       "deepseek-v4-pro",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
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
        # 降级：deepseek-v4-flash（便宜，MiniMax 挂了降级到低成本模型也不心疼）
        "fb_model":       "deepseek-v4-flash",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "audit": {
        "emoji":       "🔍",
        "label":       "工程审计",
        "desc":        "严格检查，安全审计",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 升级：deepseek-v4-pro（审计需要稳妥推理）
        "fb_model":       "deepseek-v4-pro",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
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
        # 降级：deepseek-v4-flash（兜底场景也用低成本模型）
        "fb_model":       "deepseek-v4-flash",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# Operation-type 路由（与 stage 并列的第二维度）
#
# 设计原则：op 完全覆盖 stage 路由（"我说 search 就是 search"）。
# 升级（2026-06-14）：op 默认主模型统一为 MiniMax-M3（设计文档第 11 章
# 默认策略 + 第 7 章 Stage 表），仅 fallback 视任务性质选择 deepseek。
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
        # 降级：deepseek-v4-flash（便宜，写错了也不心疼）
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
        # 升级：deepseek-v4-pro（读不准的成本 > 写错的成本）
        "fb_model":       "deepseek-v4-pro",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
    "search": {
        "emoji":       "🔎",
        "label":       "搜索",
        "desc":        "主 MiniMax-M3，备 deepseek-v4-flash",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 降级：deepseek-v4-flash（和 write 一致，便宜 fallback）
        "fb_model":       "deepseek-v4-flash",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
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
        # 升级：deepseek-v4-pro（结构改动需要稳妥的推理）
        "fb_model":       "deepseek-v4-pro",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# Pattern Library（设计文档第 8 章）
#
# 任务模式是 Workflow Planner 的核心输入。本轮进入 Shadow Mode：
#   - stage_detector 识别 pattern 后只写入 pattern_<sid> 文件 + 日志
#   - proxy 暂不消费 pattern（保持现有 model_override > op > stage 路由）
#   - 阶段 B 通过 ROC 分析 + 准确率 ≥ 90% 后再启用 Adaptive Routing
#
# 字段：
#   default_flow     — 默认阶段序列（list[str]）
#   default_complexity— 默认阶段复杂度（simple/medium/complex）
#   primary_model    — Pattern 主推模型（文档第 11 章策略的细化）
# ═══════════════════════════════════════════════════════════════════════════

PATTERN_CONFIG: dict[str, dict] = {
    "feature": {
        "label":        "功能开发",
        "desc":         "新增功能",
        "default_flow": ["plan", "design", "implement", "test", "audit"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
    },
    "bugfix": {
        "label":        "缺陷修复",
        "desc":         "修复缺陷",
        "default_flow": ["explore", "implement", "test"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
    },
    "refactor": {
        "label":        "结构重构",
        "desc":         "结构重构",
        "default_flow": ["explore", "design", "implement", "test", "audit"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
    },
    "test": {
        "label":        "测试建设",
        "desc":         "写测试或分析测试结果",
        "default_flow": ["explore", "test", "audit"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
    },
    "research": {
        "label":        "资料调研",
        "desc":         "资料调研/方案比较",
        "default_flow": ["explore", "plan", "design"],
        "default_complexity": "medium",
        "primary_model": "deepseek-v4-flash",
    },
    "migration": {
        "label":        "迁移改造",
        "desc":         "迁移/改造",
        "default_flow": ["plan", "design", "implement", "test", "audit"],
        "default_complexity": "complex",
        "primary_model": "MiniMax-M3",
    },
    "architecture": {
        "label":        "架构级任务",
        "desc":         "架构级任务",
        "default_flow": ["explore", "plan", "design", "audit"],
        "default_complexity": "complex",
        "primary_model": "MiniMax-M3",
    },
    "docs": {
        "label":        "文档编写",
        "desc":         "文档、说明、注释",
        "default_flow": ["explore", "implement"],
        "default_complexity": "simple",
        "primary_model": "deepseek-v4-flash",
    },
    "audit": {
        "label":        "代码审计",
        "desc":         "代码审查、安全审查、性能审查",
        "default_flow": ["explore", "audit"],
        "default_complexity": "complex",
        "primary_model": "MiniMax-M3",
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# Complexity 配置（设计文档第 9 章）
#
# 复杂度按 0~100 分数映射到 simple/medium/complex：
#   simple    — 0~30   单文件、单步骤、需求明确
#   medium    — 31~70  多步骤、轻度设计
#   complex   — 71~100 跨模块/跨系统/高风险
#
# 用于 ~careful（升档）/ ~quick（降档）指令调整。
# ═══════════════════════════════════════════════════════════════════════════

COMPLEXITY_LEVELS: tuple[str, ...] = ("simple", "medium", "complex")
COMPLEXITY_THRESHOLDS: dict[str, int] = {
    "simple":  30,
    "medium":  70,
    "complex": 100,
}


def complexity_rank(level: str) -> int:
    """返回复杂度等级的数字序号（用于 ~careful/~quick 升档降档）。"""
    return COMPLEXITY_LEVELS.index(level) if level in COMPLEXITY_LEVELS else 1


def shift_complexity(current: str, delta: int) -> str:
    """在 simple/medium/complex 之间升/降档，超界时夹紧。"""
    idx = complexity_rank(current) + delta
    idx = max(0, min(idx, len(COMPLEXITY_LEVELS) - 1))
    return COMPLEXITY_LEVELS[idx]


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

# Pattern 派生视图
PATTERN_FLOW: dict[str, list[str]] = {
    p: c["default_flow"] for p, c in PATTERN_CONFIG.items()
}
PATTERN_DEFAULT_COMPLEXITY: dict[str, str] = {
    p: c["default_complexity"] for p, c in PATTERN_CONFIG.items()
}
PATTERN_PRIMARY_MODEL: dict[str, str] = {
    p: c["primary_model"] for p, c in PATTERN_CONFIG.items()
}
PATTERN_INFO: dict[str, str] = {
    p: f"{c['label']} → 默认流程 {'→'.join(c['default_flow'])}（{c['default_complexity']}）"
    for p, c in PATTERN_CONFIG.items()
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
    MODEL_TO_CONFIG.setdefault(c["model"], (c["base_url"], c["model"], c["api_key_env"], c["protocol"])
    MODEL_TO_CONFIG.setdefault(c["fb_model"], (c["fb_base_url"], c["fb_model"], c["fb_api_key_env"], c["fb_protocol"])