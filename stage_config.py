"""
stage_config.py — 阶段 × 复杂度 × 模式 统一配置
================================================

本文件是 hooks/model_router/ 目录中 stage / operation / pattern / complexity
映射的**唯一数据源**。proxy.py、stage_show.py、stage_detector.py、stage CLI
均从此导入，确保所有组件展示和路由的模型一致。

修改流程：
  1. 只修改本文件的 STAGE_CONFIG / PATTERN_CONFIG /
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

from __future__ import annotations

import copy
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════
# Stage 配置（设计文档第 7 章）
#
# 默认模型策略：
#   - explore/plan/design/implement/test/audit/default → MiniMax-M3 为主
#   - brainstorm/decide → 走更便宜的 deepseek 路径（发散与决策）
# 升级模型：deepseek-v4-pro（需要稳妥推理时）
# 降级模型：deepseek-v4-flash（成本敏感或主模型不可用时）
# ═══════════════════════════════════════════════════════════════════════════

_PLACEHOLDER_STAGE_CONFIG: dict[str, dict] = {
    "explore": {
        "emoji":       "🔎",
        "label":       "探索理解",
        "desc":        "读代码、追调用链、看日志、定位现状",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 升级：deepseek-v4-pro（深追调用链/理解复杂上下文时升级推理）
        "fb_model":       "deepseek-v4-pro",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入。
        # 顺序遍历即优先级；权重未使用，detect_stage 只判定包含关系。
        "keywords": [
            '读代码',
            '看代码',
            '理解',
            '追调用',
            '调用链',
            '看日志',
            '分析现状',
            '定位',
            '了解一下',
            '搞清楚',
            'read code',
            'understand',
            'trace',
            'investigate',
            'explore',
            '调研',
            '排查',
            '现状',
            '调用栈',
            '哪里调',
            '怎么实现的',
            '梳理',
        ],
    },
    "brainstorm": {
        "emoji":       "💭",
        "label":       "头脑风暴",
        "desc":        "快速发散，低成本探索",
        # V1.2 §11 例外（D11-1）：主模型 = flash（"低成本探索"语义），
        # 偏离"MiniMax-M3 作为默认基线"原则是经业务验证的优化，
        # 备=MiniMax-M3（flash 不可用时升级）。
        "model":       "deepseek-v4-flash",
        "base_url":    "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "protocol":    "anthropic",
        "fb_model":       "MiniMax-M3",
        "fb_base_url":    "https://api.minimaxi.com/anthropic",
        "fb_api_key_env": "MINIMAX_API_KEY",
        "fb_protocol":    "anthropic",
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入。
        # 顺序遍历即优先级；权重未使用，detect_stage 只判定包含关系。
        "keywords": [
            '头脑风暴',
            'brainstorm',
            '想法',
            '创意',
            'idea',
            '可能性',
            '方向',
            'possibilities',
            '脑暴',
            '随便想想',
        ],
    },
    "decide": {
        "emoji":       "⚖️",
        "label":       "决策分析",
        "desc":        "深度推理，权衡分析",
        # §16 核心原则（+ D11-1 修正 2026-06-17）：
        #   主模型 = deepseek-v4-flash（低成本基线），而非 deepseek-v4-pro。
        #
        #   原 D11-1 将 decide 固定为 pro，认为"决策场景值得付推理成本"。
        #   但这与 §16 复杂度主导原则冲突——simple/medium 决策任务不
        #   应强制走 pro。改为 flash 作为基线后：
        #     - complexity=simple  → flash（§16 覆盖，无额外成本）
        #     - complexity=medium  → flash（同上）
        #     - complexity=complex → deepseek-v4-pro（proxy.py §16 覆盖自动升级）
        #
        #   备=MiniMax-M3（flash 不可用时降级到基线）。
        "model":       "deepseek-v4-flash",
        "base_url":    "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "protocol":    "anthropic",
        "fb_model":       "MiniMax-M3",
        "fb_base_url":    "https://api.minimaxi.com/anthropic",
        "fb_api_key_env": "MINIMAX_API_KEY",
        "fb_protocol":    "anthropic",
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入。
        # 顺序遍历即优先级；权重未使用，detect_stage 只判定包含关系。
        "keywords": [
            '决策',
            '选择',
            'compare',
            '对比',
            '权衡',
            'trade-off',
            'pros and cons',
            '哪个好',
            '怎么选',
            'evaluate',
            '评估',
            'analysis',
            '分析',
        ],
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
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入。
        # 顺序遍历即优先级；权重未使用，detect_stage 只判定包含关系。
        "keywords": [
            '设计',
            '架构',
            'design',
            'architect',
            '方案',
            'schema',
            'structure',
            '模块',
            '接口',
            'interface',
            '系统设计',
            '数据模型',
        ],
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
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入。
        # 顺序遍历即优先级；权重未使用，detect_stage 只判定包含关系。
        "keywords": [
            '计划',
            'plan',
            '拆分',
            'breakdown',
            '步骤',
            'task list',
            'todo',
            'roadmap',
            '分解',
            'milestone',
            '任务清单',
            '怎么做',
        ],
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
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入。
        # 顺序遍历即优先级；权重未使用，detect_stage 只判定包含关系。
        "keywords": [
            '实现',
            '实施',
            'implement',
            '写代码',
            '开发',
            'develop',
            '写',
            '修',
            'build',
            'create',
            'fix',
            '修复',
            'add',
            '添加',
        ],
    },
    "test": {
        "emoji":       "🧪",
        "label":       "测试验证",
        "desc":        "写测试、跑测试、分析覆盖率、回归验证",
        "model":       "MiniMax-M3",
        "base_url":    "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol":    "anthropic",
        # 升级：deepseek-v4-pro（复杂测试用例设计、根因分析需稳妥推理）
        "fb_model":       "deepseek-v4-pro",
        "fb_base_url":    "https://api.deepseek.com/anthropic",
        "fb_api_key_env": "DEEPSEEK_API_KEY",
        "fb_protocol":    "anthropic",
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入。
        # §7 D7-2 修复：test 必须排在 audit 之前（避免被 audit 吞掉）。
        # 顺序遍历即优先级；权重未使用，detect_stage 只判定包含关系。
        "keywords": [
            '测试',                # §17 V17-4 修复 2026-06-14：高优先级泛匹配，先吃掉"分析测试失败"
            '跑测试',
            '跑一下测试',
            '跑用例',
            '写测试',
            '测试覆盖率',
            'unit test',
            '单元测试',
            '回归测试',
            'run test',
            'run tests',
            'run the test',
            'execute test',
            '覆盖率',
            '回归验证',
            'pytest',
            'jest',
            'mocha',
        ],
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
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入。
        # 顺序遍历即优先级；权重未使用，detect_stage 只判定包含关系。
        "keywords": [
            '审计',
            'audit',
            'review',
            '检查',
            'code review',
            '安全',
            'security',
            '漏洞',
            '验证',
            'verify',
            '质量',
            'quality',
            '检验',
        ],
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
# Operation-type 路由 — [已废弃 2026-06-14]
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

# V1.3 §5.1 Task Pattern 12 种（canonical 短标注，禁止附加详细描述）：
#   explore / architecture / feature / audit / implement / debug /
#   refactor / test / research / migration / docs / ops
# V1 旧名 bugfix → V1.3 debug（保留为兼容别名，不影响路由决策）。
_PLACEHOLDER_PATTERN_CONFIG: dict[str, dict] = {
    "explore": {
        "label":        "探索与调研",
        "default_flow": ["plan", "design"],
        "default_complexity": "simple",
        "primary_model": "MiniMax-M3",
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入此处。
        # 加权计票：每条 (关键词, 权重)；同 pattern 多个关键词命中时累加。
        "keywords": [
            ("explore", 3), ("调研", 3), ("了解一下", 2), ("搞清楚", 2),
            ("read code", 2), ("understand", 2), ("trace", 2), ("investigate", 2),
            ("看看", 1), ("分析现状", 2), ("梳理", 1),
        ],
    },
    "architecture": {
        "label":        "架构设计",
        "default_flow": ["explore", "plan", "design", "audit"],
        "default_complexity": "complex",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("架构", 3), ("architecture", 3), ("系统设计", 3), ("顶层设计", 3),
            ("整体方案", 2), ("技术选型", 2), ("模块划分", 3),
        ],
    },
    "feature": {
        "label":        "新功能需求",
        "default_flow": ["plan", "design", "implement", "test", "audit"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("新增功能", 3), ("添加功能", 3), ("加个功能", 2), ("新增字段", 2),
            ("新功能", 2), ("做一个", 1), ("实现一个", 1),
            ("new feature", 3), ("add feature", 3), ("implement feature", 3),
            ("support ", 1), ("支持 ", 1), ("加", 1),
        ],
    },
    "implement": {
        "label":        "功能实现",
        "default_flow": ["plan", "design", "implement", "test"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("实现", 3), ("实施", 3), ("写代码", 2), ("开发", 3),
            ("develop", 3), ("build", 2), ("create", 2),
            ("coding", 2), ("代码实现", 3), ("implement", 1),
        ],
    },
    "debug": {
        "label":        "调试异常",
        "default_flow": ["explore", "implement", "test"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("bug", 3), ("fix", 3), ("修复", 3), ("defect", 3),
            ("崩溃", 3), ("crash", 3), ("异常", 2), ("报错", 2), ("error", 2),
            ("故障", 3), ("debug", 3), ("修", 1),
        ],
    },
    "refactor": {
        "label":        "模块重构",
        "default_flow": ["explore", "design", "implement", "test", "audit"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("refactor", 3), ("重构", 3), ("整理", 2), ("优化结构", 3),
            ("restructure", 3), ("reorganize", 2), ("改结构", 3), ("清理", 1),
        ],
    },
    "test": {
        "label":        "测试相关",
        "default_flow": ["explore", "test", "audit"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("写测试", 3), ("补测试", 3), ("单元测试", 3), ("unit test", 3),
            ("integration test", 3), ("test case", 2), ("测试", 1),
        ],
    },
    "research": {
        "label":        "调查研究",
        "default_flow": ["explore", "plan", "design"],
        "default_complexity": "medium",
        "primary_model": "deepseek-v4-flash",
        "keywords": [
            ("调研", 3), ("research", 3), ("比较方案", 2), ("对比", 1),
            ("evaluate", 2), ("哪个好", 1), ("选哪个", 1), ("查一下", 1),
        ],
    },
    "migration": {
        "label":        "模块迁移",
        "default_flow": ["plan", "design", "implement", "test", "audit"],
        "default_complexity": "complex",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("migration", 3), ("migrate", 3), ("迁移", 3), ("迁到", 2),
            ("迁过去", 2), ("升级", 2), ("upgrade", 2),
            ("迁移到", 3), ("升级到", 2), ("无损迁移", 3),
        ],
    },
    "docs": {
        "label":        "文档处理",
        "default_flow": ["explore", "implement"],
        "default_complexity": "simple",
        "primary_model": "deepseek-v4-flash",
        "keywords": [
            ("写文档", 3), ("写说明", 3), ("readme", 3), ("comment", 2),
            ("注释", 1), ("注释一下", 2), ("documentation", 3), ("docs", 2),
        ],
    },
    "audit": {
        "label":        "审计系统功能",
        "default_flow": ["explore", "audit"],
        "default_complexity": "complex",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("code review", 3), ("安全审查", 3), ("安全审计", 3), ("security review", 3),
            ("审计", 3), ("漏洞", 2), ("vulnerability", 3), ("性能审查", 2),
        ],
    },
    "ops": {
        "label":        "运维、脚本、配置类任务",
        "default_flow": ["explore", "implement", "test"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("ci/cd", 3), ("pipeline", 3), ("workflow", 2),
            ("deploy", 3), ("部署", 3), ("发布", 2),
            ("script", 2), ("脚本", 2), ("cron", 3),
            ("config", 2), ("配置", 2), ("yaml", 2), ("toml", 2),
            ("env", 1), ("环境变量", 2),
        ],
    },
    # V1 旧名兼容别名（保留冗余）：V1 用 bugfix，V1.3 改用 debug。
    # llm_classifier._PATTERN_ALIASES 已在分类边界归一化 bugfix→debug，
    # 此处保留为 V1 旧记录（state_persistence、shadow 模式 raw 写入）的查找回退。
    "bugfix": {
        "label":        "缺陷修复",
        "default_flow": ["explore", "implement", "test"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("bug", 3), ("fix", 3), ("修复", 3), ("defect", 3),
            ("崩溃", 3), ("crash", 3), ("异常", 2), ("报错", 2), ("error", 2),
            ("修", 1),
        ],
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

# §17 架构简化（2026-06-17）：COMPLEXITY_KEYWORDS / PATTERN_BASE_SCORE /
# STAGE_COMPLEXITY_MULTIPLIER 已移除。Complexity 唯一来源是 LLM classifier，
# 辅以用户 ~careful/~quick 显式调档。

# ═══════════════════════════════════════════════════════════════════════════
# LLM 分类器配置（设计文档 §6.2 / §6.4 / §10 合并实现）
#
# 将原来三次独立的关键词分类（stage / pattern / complexity）合并为一次 LLM
# 调用。llm_classifier.py 读取此配置确定使用哪个模型做分类。
#
# 模型选择建议：
#   deepseek-v4-flash    — 推荐，成本更低、响应更快，分类任务不需要强模型
#   MiniMax-M3           — 备选，分类准确、稳定、速度快
#
# 调用方优先级：传入 config > 本配置 > llm_classifier.DEFAULT_CLASSIFIER_CONFIG
# ═══════════════════════════════════════════════════════════════════════════

_PLACEHOLDER_LLM_CLASSIFIER_CONFIG: dict[str, object] = {
    "model":       "deepseek-v4-flash",
    "base_url":    "https://api.deepseek.com/anthropic",
    "api_key_env": "DEEPSEEK_API_KEY",
    "protocol":    "anthropic",
    "fallback_model":       "MiniMax-M3",
    "fallback_base_url":    "https://api.minimaxi.com/anthropic",
    "fallback_api_key_env": "MINIMAX_API_KEY",
    "fallback_protocol":    "anthropic",
    "max_tokens":  512,
    "temperature": 0.0,
    "timeout":     15,
}

_PLACEHOLDER_MODEL_REGISTRY: dict[str, dict[str, str]] = {
    "MiniMax-M3": {
        "provider": "minimax",
        "base_url": "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "protocol": "anthropic",
    },
    "deepseek-v4-flash": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "protocol": "anthropic",
    },
    "deepseek-v4-pro": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "protocol": "anthropic",
    },
    "GPT-5.4": {
        "provider": "openai",
        "base_url": "https://api.openai.com",
        "api_key_env": "OPENAI_API_KEY",
        "protocol": "openai",
    },
    "GPT-5.4-Mini": {
        "provider": "openai",
        "base_url": "https://api.openai.com",
        "api_key_env": "OPENAI_API_KEY",
        "protocol": "openai",
    },
}

_PLACEHOLDER_DEFAULT_FALLBACK_PROVIDER: dict[str, str] = {
    "minimax": "deepseek",
    "deepseek": "minimax",
    "openai": "minimax",
}

_PLACEHOLDER_PROVIDER_COMPLEXITY_MODELS: dict[str, dict[str, str]] = {
    "minimax": {
        "simple": "MiniMax-M3",
        "medium": "MiniMax-M3",
        "complex": "MiniMax-M3",
    },
    "deepseek": {
        "simple": "deepseek-v4-flash",
        "medium": "deepseek-v4-pro",
        "complex": "deepseek-v4-pro",
    },
    "openai": {
        "simple": "GPT-5.4-Mini",
        "medium": "GPT-5.4",
        "complex": "GPT-5.4",
    },
}

_CONFIG_DIR = Path(__file__).resolve().parent / "config"
_MODELS_YAML_PATH = _CONFIG_DIR / "models.yaml"
_STAGES_YAML_PATH = _CONFIG_DIR / "stages.yaml"
_PATTERNS_YAML_PATH = _CONFIG_DIR / "patterns.yaml"
_LLM_CLASSIFIER_YAML_PATH = _CONFIG_DIR / "llm_classifier.yaml"


def _safe_load_yaml(path: Path) -> dict | None:
    try:
        import yaml
    except ImportError:
        return None
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _normalize_weighted_keywords(items) -> list[tuple[str, int]]:
    normalized: list[tuple[str, int]] = []
    if not isinstance(items, list):
        return normalized
    for item in items:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], str)
        ):
            normalized.append((item[0], int(item[1])))
    return normalized


def load_model_registry_bundle() -> tuple[
    dict[str, dict[str, str]],
    dict[str, str],
    dict[str, dict[str, str]],
]:
    data = _safe_load_yaml(_MODELS_YAML_PATH)
    if not data:
        return (
            copy.deepcopy(_PLACEHOLDER_MODEL_REGISTRY),
            dict(_PLACEHOLDER_DEFAULT_FALLBACK_PROVIDER),
            copy.deepcopy(_PLACEHOLDER_PROVIDER_COMPLEXITY_MODELS),
        )

    models = data.get("models")
    fallback_provider = data.get("default_fallback_provider")
    complexity_models = data.get("provider_complexity_models")
    if not isinstance(models, dict):
        models = copy.deepcopy(_PLACEHOLDER_MODEL_REGISTRY)
    if not isinstance(fallback_provider, dict):
        fallback_provider = dict(_PLACEHOLDER_DEFAULT_FALLBACK_PROVIDER)
    if not isinstance(complexity_models, dict):
        complexity_models = copy.deepcopy(_PLACEHOLDER_PROVIDER_COMPLEXITY_MODELS)

    normalized_models: dict[str, dict[str, str]] = {}
    for model_name, cfg in models.items():
        if not isinstance(model_name, str) or not isinstance(cfg, dict):
            continue
        provider = str(cfg.get("provider", "")).strip()
        base_url = str(cfg.get("base_url", "")).strip()
        api_key_env = str(cfg.get("api_key_env", "")).strip()
        protocol = str(cfg.get("protocol", "anthropic")).strip()
        if not (provider and base_url and api_key_env):
            continue
        if protocol not in {"anthropic", "openai"}:
            continue
        normalized_models[model_name] = {
            "provider": provider,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "protocol": protocol,
        }
    if not normalized_models:
        normalized_models = copy.deepcopy(_PLACEHOLDER_MODEL_REGISTRY)

    normalized_complexity: dict[str, dict[str, str]] = {}
    for provider, mapping in complexity_models.items():
        if not isinstance(provider, str) or not isinstance(mapping, dict):
            continue
        simple = mapping.get("simple")
        medium = mapping.get("medium")
        complex_model = mapping.get("complex")
        if not all(isinstance(v, str) for v in (simple, medium, complex_model)):
            continue
        if not all(v in normalized_models for v in (simple, medium, complex_model)):
            continue
        normalized_complexity[provider] = {
            "simple": simple,
            "medium": medium,
            "complex": complex_model,
        }
    if not normalized_complexity:
        normalized_complexity = copy.deepcopy(_PLACEHOLDER_PROVIDER_COMPLEXITY_MODELS)

    normalized_fallback: dict[str, str] = {}
    for provider, alt in fallback_provider.items():
        if not isinstance(provider, str) or not isinstance(alt, str):
            continue
        normalized_fallback[provider] = alt
    if not normalized_fallback:
        normalized_fallback = dict(_PLACEHOLDER_DEFAULT_FALLBACK_PROVIDER)

    return normalized_models, normalized_fallback, normalized_complexity


def load_stage_config(model_registry: dict[str, dict[str, str]]) -> dict[str, dict]:
    data = _safe_load_yaml(_STAGES_YAML_PATH)
    stages = data.get("stages") if data else None
    if not isinstance(stages, dict):
        return copy.deepcopy(_PLACEHOLDER_STAGE_CONFIG)

    loaded: dict[str, dict] = {}
    for stage_name, cfg in stages.items():
        if not isinstance(stage_name, str) or not isinstance(cfg, dict):
            continue
        model = cfg.get("model")
        fallback_model = cfg.get("fallback_model")
        if model not in model_registry or fallback_model not in model_registry:
            continue
        primary = model_registry[model]
        fallback = model_registry[fallback_model]
        keywords = cfg.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        loaded[stage_name] = {
            "emoji": str(cfg.get("emoji", "")),
            "label": str(cfg.get("label", stage_name)),
            "desc": str(cfg.get("desc", "")),
            "model": model,
            "base_url": primary["base_url"],
            "api_key_env": primary["api_key_env"],
            "protocol": primary["protocol"],
            "fb_model": fallback_model,
            "fb_base_url": fallback["base_url"],
            "fb_api_key_env": fallback["api_key_env"],
            "fb_protocol": fallback["protocol"],
            "keywords": [str(k) for k in keywords if isinstance(k, str)],
        }
    if "default" not in loaded:
        return copy.deepcopy(_PLACEHOLDER_STAGE_CONFIG)
    return loaded


def load_pattern_config() -> dict[str, dict]:
    data = _safe_load_yaml(_PATTERNS_YAML_PATH)
    patterns = data.get("patterns") if data else None
    if not isinstance(patterns, dict):
        return copy.deepcopy(_PLACEHOLDER_PATTERN_CONFIG)

    loaded: dict[str, dict] = {}
    for pattern_name, cfg in patterns.items():
        if not isinstance(pattern_name, str) or not isinstance(cfg, dict):
            continue
        flow = cfg.get("default_flow", [])
        if not isinstance(flow, list) or not all(isinstance(x, str) for x in flow):
            continue
        primary_model = cfg.get("primary_model")
        if not isinstance(primary_model, str):
            continue
        loaded[pattern_name] = {
            "label": str(cfg.get("label", pattern_name)),
            "default_flow": flow,
            "default_complexity": str(cfg.get("default_complexity", "medium")),
            "primary_model": primary_model,
            "keywords": _normalize_weighted_keywords(cfg.get("keywords", [])),
        }
    return loaded or copy.deepcopy(_PLACEHOLDER_PATTERN_CONFIG)


def load_llm_classifier_config(model_registry: dict[str, dict[str, str]]) -> dict[str, object]:
    data = _safe_load_yaml(_LLM_CLASSIFIER_YAML_PATH)
    classifier = data.get("classifier") if data else None
    if not isinstance(classifier, dict):
        return copy.deepcopy(_PLACEHOLDER_LLM_CLASSIFIER_CONFIG)
    model = classifier.get("model")
    if model not in model_registry:
        return copy.deepcopy(_PLACEHOLDER_LLM_CLASSIFIER_CONFIG)
    route = model_registry[model]
    fallback_model = classifier.get("fallback_model")
    fallback_route = model_registry.get(fallback_model) if isinstance(fallback_model, str) else None
    return {
        "model": model,
        "base_url": route["base_url"],
        "api_key_env": route["api_key_env"],
        "protocol": route["protocol"],
        "fallback_model": fallback_model if fallback_route else None,
        "fallback_base_url": fallback_route["base_url"] if fallback_route else None,
        "fallback_api_key_env": fallback_route["api_key_env"] if fallback_route else None,
        "fallback_protocol": fallback_route["protocol"] if fallback_route else None,
        "max_tokens": int(classifier.get("max_tokens", 512)),
        "temperature": float(classifier.get("temperature", 0.0)),
        "timeout": int(classifier.get("timeout", 15)),
    }


MODEL_REGISTRY, _YAML_DEFAULT_FALLBACK_PROVIDER, _YAML_PROVIDER_COMPLEXITY_MODELS = (
    load_model_registry_bundle()
)
STAGE_CONFIG: dict[str, dict] = load_stage_config(MODEL_REGISTRY)
PATTERN_CONFIG: dict[str, dict] = load_pattern_config()
LLM_CLASSIFIER_CONFIG: dict[str, object] = load_llm_classifier_config(MODEL_REGISTRY)

# ═══════════════════════════════════════════════════════════════════════════
# Workflow 角色模型（设计文档第 10 章算法 D10-5 修复 2026-06-14）
#
# 问题：原 build_workflow_plan 把 stage.fb_model 当 strong_model 用，
# 但 implement.fb_model = deepseek-v4-flash（弱模型），
# 导致 implement complex workflow = [flash, M3, flash] —— 违反"复杂任务用强模型"。
#
# 修复：定义全局 STRONG_MODEL / NORMAL_MODEL，build_workflow_plan 直接引用。
#
# 2026-06-15 策略调整：
#   - medium → 三模型编排（规划→执行→审计），models = [STRONG, NORMAL, STRONG]
#   - complex → 全程 strong model，models = [STRONG, STRONG, STRONG]，
#     避免 normal model 在复杂场景出错。
# ═══════════════════════════════════════════════════════════════════════════

STRONG_MODEL:  str = "deepseek-v4-pro"   # 复杂任务的规划/执行/审计模型（设计文档 §10）
NORMAL_MODEL: str = "MiniMax-M3"        # 常规模型（主力执行）

# ── Per-API-Request 动态分类间隔（设计文档 §6.2 / §6.4）──
# Hook (UserPromptSubmit) 每次都会分类；Proxy 在每次 API 请求时递增计数器，
# 计数器到达此阈值时触发一次 LLM 重新分类。
# 默认值 3：即每 3 次 CC API 请求重新分类一次。
# 可通过环境变量 STAGE_ROUTER_RECLASSIFY_INTERVAL 覆盖（覆盖点在 proxy.py 和
# stage_detector.py 中读取 os.environ）。
RECLASSIFY_INTERVAL: int = 3


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

# V1.3 §5.1 Task Pattern 中文 label 映射（与 llm_classifier.py 系统 prompt 对齐）
# 用于 statusline / shadow 模式显示。当 pattern 命中 V1.3 12 种之一时，
# 优先返回 V1.3 中文 label；命中 V1 旧名（bugfix 等）时回退到
# PATTERN_CONFIG 的 legacy label。缺失时返回 key 原文。
PATTERN_LABEL_V13: dict[str, str] = {
    "explore":      "探索与调研",
    "architecture": "架构设计",
    "feature":      "新功能需求",
    "audit":        "审计系统功能",
    "implement":    "功能实现",
    "debug":        "调试异常",
    "refactor":     "模块重构",
    "test":         "测试相关",
    "research":     "调查研究",
    "migration":    "模块迁移",
    "docs":         "文档处理",
    "ops":          "运维、脚本、配置类任务",
}


def get_pattern_label_v13(pattern: str) -> str:
    """返回 V1.3 风格的中文 label。

    优先级：V1.3 显式映射 > PATTERN_CONFIG 兜底 > pattern key 原文。

    Args:
        pattern: pattern key（如 "test"、"bugfix"、"explore"）。

    Returns:
        str: 中文 label 或 key 原文。
    """
    if not pattern:
        return ""
    if pattern in PATTERN_LABEL_V13:
        return PATTERN_LABEL_V13[pattern]
    # V1 旧 pattern 名（bugfix / feature 等）→ 走 PATTERN_CONFIG 找 legacy label
    cfg = PATTERN_CONFIG.get(pattern, {})
    return cfg.get("label", pattern) or pattern

# ═══════════════════════════════════════════════════════════════════════════
# 反向索引：model → (base_url, model, api_key_env, protocol)
# 从 STAGE_CONFIG 的主模型和备用模型收集。
# proxy.py 用于 sticky fallback：当主模型不可用后，直接从 fallback 模型名
# 反查出完整的路由配置（不再需要知道原 stage），也用于 model_override 路由。
# ═══════════════════════════════════════════════════════════════════════════

MODEL_TO_CONFIG: dict[str, tuple[str, str, str, str]] = {
    model_name: (
        cfg["base_url"],
        model_name,
        cfg["api_key_env"],
        cfg["protocol"],
    )
    for model_name, cfg in MODEL_REGISTRY.items()
}

# ═══════════════════════════════════════════════════════════════════════════
# Provider 级 fallback 定义（2026-06-16）
#
# 旧 sticky fallback 存储"具体的模型名"（如 deepseek-v4-flash），
# 导致降级提供方选定后无法按任务复杂度动态选模型。本质语义应是
# "provider 不可用"而非"model 不可用"——切换到替代 provider 后仍可
# 在该 provider 内部按 complexity 路由（如 deepseek: simple→flash,
# medium/complex→pro）。
#
# 数据结构：
#   MODEL_TO_PROVIDER         — model → provider（从 base_url 域名推导）
#   DEFAULT_FALLBACK_PROVIDER — 失败 provider → 替代 provider
#   PROVIDER_COMPLEXITY_MODELS — provider → {complexity → model}
#   KNOWN_PROVIDER_NAMES      — 已知 provider 名集合（校验/向后兼容用）
# ═══════════════════════════════════════════════════════════════════════════


def _build_model_to_provider() -> dict[str, str]:
    """从模型注册表推导 model→provider 映射。"""
    return {
        model_name: cfg["provider"]
        for model_name, cfg in MODEL_REGISTRY.items()
        if cfg.get("provider")
    }


MODEL_TO_PROVIDER: dict[str, str] = _build_model_to_provider()

DEFAULT_FALLBACK_PROVIDER: dict[str, str] = dict(_YAML_DEFAULT_FALLBACK_PROVIDER)

PROVIDER_COMPLEXITY_MODELS: dict[str, dict[str, str]] = copy.deepcopy(
    _YAML_PROVIDER_COMPLEXITY_MODELS
)

KNOWN_PROVIDER_NAMES: frozenset[str] = frozenset(
    set(MODEL_TO_PROVIDER.values())
    | set(DEFAULT_FALLBACK_PROVIDER.keys())
    | set(DEFAULT_FALLBACK_PROVIDER.values())
    | set(PROVIDER_COMPLEXITY_MODELS.keys())
)

# ═══════════════════════════════════════════════════════════════════════════
# §17 架构简化（2026-06-17）：STAGE_KEYWORDS / PATTERN_KEYWORDS 派生视图已移除。
# LLM classifier 是 stage / pattern / complexity 的唯一分类源。
# STAGE_CONFIG.keywords 和 PATTERN_CONFIG.keywords 字段保留为文档说明/LLM prompt 参考。
# ═══════════════════════════════════════════════════════════════════════════

# ── 公开 API 声明 ────────────────────────────────────────────────────────────

__all__ = [
    "STAGE_CONFIG",
    "PATTERN_CONFIG",
    "PATTERN_LABEL_V13",
    "get_pattern_label_v13",
    "_PLACEHOLDER_WEIGHTS",
    "load_yaml_weights",
    "get_weights",
    # ── Model tier ranking (config/model_tiers.yaml) ──
    "MODEL_TIERS",
    "load_model_tiers",
    "get_model_tier",
    "model_tier",
]

# ── Runtime Complexity Score 权重（V1.3 §7 硬编码兜底）────────────────────

# Stage 1 阶段在代码中硬编码；Stage 7 抽到 config/decision_weights.yaml。
# 本字典被 runtime_score.py 读取（Stage 2）；单测用 test_stage_config_weights.py
# 锁定形状契约，避免后续 stage 误改爆掉调用点。
_PLACEHOLDER_WEIGHTS: dict[str, dict[str, int]] = {
    "tool": {
        "Read": 2,
        "Edit": 4,
        "Write": 3,
        "MultiEdit": 5,
        "Grep": 3,
        "Glob": 2,
        "WebSearch": 4,
        "WebFetch": 3,
        "Bash": 2,
        "TodoWrite": 8,
        "Agent": 5,
        "NotebookEdit": 2,
        "TodoRead": 1,
    },
    "file_type": {
        ".py": 3,
        ".ts": 3,
        ".tsx": 3,
        ".js": 3,
        ".go": 3,
        ".rs": 3,
        ".java": 3,
        ".cpp": 3,
        ".c": 3,
        ".h": 3,
        ".swift": 3,
        ".yaml": 2,
        ".yml": 2,
        ".toml": 2,
        ".json": 1,
        ".md": 1,
        ".txt": 1,
        ".lock": 0,
        ".sum": 0,
    },
    "file_lines": {
        "small": 1,
        "medium": 2,
        "large": 3,
    },
    "runtime_signal": {
        "bash_nonzero_exit": 4,
        "grep_large_hits": 3,
        "search_large_hits": 3,
        "edit_retry_loop": 2,
        "test_failure": 5,
    },
}

# ── YAML 权重加载器（V1.3 §7 配置化）─────────────────────────────────────

import os as _os
from pathlib import Path as _Path

_WEIGHTS_YAML_PATH = _Path(__file__).resolve().parent / "config" / "decision_weights.yaml"


def load_yaml_weights() -> dict[str, dict[str, int]]:
    """加载 decision_weights.yaml → 注入 runtime_score 权重。

    启动时调用一次；YAML 缺失或损坏时降级为 _PLACEHOLDER_WEIGHTS 硬编码兜底。
    """
    try:
        import yaml as _yaml
    except ImportError:
        return _PLACEHOLDER_WEIGHTS

    if not _WEIGHTS_YAML_PATH.exists():
        return _PLACEHOLDER_WEIGHTS

    try:
        raw = _WEIGHTS_YAML_PATH.read_text(encoding="utf-8")
        data = _yaml.safe_load(raw)
    except Exception:
        return _PLACEHOLDER_WEIGHTS

    if not isinstance(data, dict):
        return _PLACEHOLDER_WEIGHTS

    # 验证顶层 key 结构与 _PLACEHOLDER_WEIGHTS 对齐
    expected_keys = {"tool", "file_type", "file_lines", "runtime_signal"}
    loaded: dict[str, dict[str, int]] = {}
    for key in expected_keys:
        section = data.get(key)
        if isinstance(section, dict):
            loaded[key] = {str(k): int(v) for k, v in section.items()}
        else:
            # 某个 section 缺失或格式错误 → 用硬编码兜底
            loaded[key] = _PLACEHOLDER_WEIGHTS.get(key, {})

    return loaded


# 模块级缓存：首次导入时加载一次
_YAML_WEIGHTS: dict[str, dict[str, int]] | None = None


def get_weights() -> dict[str, dict[str, int]]:
    """获取当前生效的权重（YAML 优先，降级硬编码）。"""
    global _YAML_WEIGHTS
    if _YAML_WEIGHTS is None:
        _YAML_WEIGHTS = load_yaml_weights()
    return _YAML_WEIGHTS

# ── Model Tier Ranking（config/model_tiers.yaml 配置化）────────────────

# 硬编码兜底：YAML 缺失或损坏时使用。
# 等级排序：deepseek-v4-pro(2) > MiniMax-M3(1) > deepseek-v4-flash(0)
# 与 config/model_tiers.yaml 保持同步。
_PLACEHOLDER_MODEL_TIERS: dict[str, int] = {
    "deepseek-v4-flash": 0,
    "MiniMax-M3":        1,
    "deepseek-v4-pro":   2,
    "GPT-5.4-Mini":      3,
    "GPT-5.4":           4,
}

_MODEL_TIERS_YAML_PATH = _Path(__file__).resolve().parent / "config" / "model_tiers.yaml"


def load_model_tiers() -> dict[str, int]:
    """加载 config/model_tiers.yaml → 返回 {model_name: tier_int}。

    启动时调用一次；YAML 缺失或损坏时降级为 _PLACEHOLDER_MODEL_TIERS 硬编码兜底。
    """
    try:
        import yaml as _yaml
    except ImportError:
        return dict(_PLACEHOLDER_MODEL_TIERS)

    if not _MODEL_TIERS_YAML_PATH.exists():
        return dict(_PLACEHOLDER_MODEL_TIERS)

    try:
        raw = _MODEL_TIERS_YAML_PATH.read_text(encoding="utf-8")
        data = _yaml.safe_load(raw)
    except Exception:
        return dict(_PLACEHOLDER_MODEL_TIERS)

    if not isinstance(data, dict):
        return dict(_PLACEHOLDER_MODEL_TIERS)

    models = data.get("models")
    if not isinstance(models, dict):
        return dict(_PLACEHOLDER_MODEL_TIERS)

    # 转换为 {model_name: int_tier}
    return {str(k): int(v) for k, v in models.items()}


# 模块级缓存：首次导入时加载一次
_MODEL_TIERS_CACHE: dict[str, int] | None = None


def _ensure_model_tiers() -> dict[str, int]:
    """确保 MODEL_TIERS 已加载（延迟加载，避免循环导入）。"""
    global _MODEL_TIERS_CACHE
    if _MODEL_TIERS_CACHE is None:
        _MODEL_TIERS_CACHE = load_model_tiers()
    return _MODEL_TIERS_CACHE


# 兼容属性访问：MODEL_TIERS 作为模块级"变量"（实际是函数调用）
# 消费方可直接 `from stage_config import MODEL_TIERS`，但注意这是函数返回值而非真变量。
# 推荐使用 get_model_tier() 或 model_tier() 函数。
MODEL_TIERS: dict[str, int] = _PLACEHOLDER_MODEL_TIERS  # 启动时占位，首次调用后更新


def get_model_tier(model_name: str) -> int:
    """返回模型的 capability tier 值（YAML 优先，降级硬编码兜底）。

    Args:
        model_name: 模型名（如 "deepseek-v4-pro"、"MiniMax-M3"）。

    Returns:
        int: tier 值（越大越强），未知模型返回 1（baseline）。
    """
    tiers = _ensure_model_tiers()
    return tiers.get(model_name, 1)  # 未知模型按 baseline (tier 1) 处理


# 简洁别名，与 statusline.sh 的 _model_tier() 命名对齐
model_tier = get_model_tier
