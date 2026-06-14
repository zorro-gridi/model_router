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
        # V1.2 §11 例外（D11-1）：主模型 = deepseek-v4-pro（"高阶推理"语义），
        # 偏离"MiniMax-M3 作为默认基线"原则是经业务验证的优化（决策场景值得付推理成本），
        # 备=MiniMax-M3（pro 不可用时降级到基线）。
        "model":       "deepseek-v4-pro",
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
#
# 废弃原因（决策分析）：
#   write / read / search 只是"动作"，不是"任务属性"。
#   同一个 write 可以对应"写测试"、"写架构"、"写文档"、"写代码"，
#   但它们的复杂度天差地别——write 无法作为路由决策信号。
#
#   真正影响模型选择的是"任务类型 + 任务复杂度 + 当前阶段"：
#     测试任务内部就有：策略设计 → 用例生成 → 执行 → 结果分析 →
#     根因定位 → 回归验证，每一步复杂度都不同。
#   到这一步 write/read 已经没有决策价值。
#
#   同时，Complexity 分类器（设计文档 §6.4）的引入已吞掉 op 的原始职责
#   ——系统已经从"关键词动作路由"进化到"上下文复杂度路由"。
#
# 兼容策略：
#   OPERATION_CONFIG 保留为空 dict。所有消费方（proxy / stage_detector /
#   stage_show / stage CLI）通过 `if op in OPERATION_CONFIG` 自然退化到
#   不匹配分支，无需逐个修改条件判断。
#
#   原有四类 op 完整配置保留在下方的注释块中，便于：
#     - 事后追溯"曾经存在这个设计"
#     - 如果未来发现 Complexity 路由不如预期，可快速回退
#
# 原设计原则（已失效）：
#   op 完全覆盖 stage 路由（"我说 search 就是 search"）。
#   升级（2026-06-14）：op 默认主模型统一为 MiniMax-M3（设计文档第 11 章
#   默认策略 + 第 7 章 Stage 表），仅 fallback 视任务性质选择 deepseek。
#
# 原有 OPERATION_CONFIG（write / read / search / refactor）完整定义：
#   OPERATION_CONFIG: dict[str, dict] = {
#       "write": {
#           "emoji":       "✏️",
#           "label":       "写入",
#           "desc":        "主 MiniMax-M3，便宜 fallback",
#           "model":       "MiniMax-M3",
#           "base_url":    "https://api.minimaxi.com/anthropic",
#           "api_key_env": "MINIMAX_API_KEY",
#           "protocol":    "anthropic",
#           "fb_model":       "deepseek-v4-flash",
#           "fb_base_url":    "https://api.deepseek.com/anthropic",
#           "fb_api_key_env": "DEEPSEEK_API_KEY",
#           "fb_protocol":    "anthropic",
#       },
#       "read": {
#           "emoji":       "👁️",
#           "label":       "读取",
#           "desc":        "主 MiniMax-M3，稳 fallback",
#           "model":       "MiniMax-M3",
#           "base_url":    "https://api.minimaxi.com/anthropic",
#           "api_key_env": "MINIMAX_API_KEY",
#           "protocol":    "anthropic",
#           "fb_model":       "deepseek-v4-pro",
#           "fb_base_url":    "https://api.deepseek.com/anthropic",
#           "fb_api_key_env": "DEEPSEEK_API_KEY",
#           "fb_protocol":    "anthropic",
#       },
#       "search": {
#           "emoji":       "🔎",
#           "label":       "搜索",
#           "desc":        "主 MiniMax-M3，备 deepseek-v4-flash",
#           "model":       "MiniMax-M3",
#           "base_url":    "https://api.minimaxi.com/anthropic",
#           "api_key_env": "MINIMAX_API_KEY",
#           "protocol":    "anthropic",
#           "fb_model":       "deepseek-v4-flash",
#           "fb_base_url":    "https://api.deepseek.com/anthropic",
#           "fb_api_key_env": "DEEPSEEK_API_KEY",
#           "fb_protocol":    "anthropic",
#       },
#       "refactor": {
#           "emoji":       "🔧",
#           "label":       "重构",
#           "desc":        "主 MiniMax-M3，备 deepseek-v4-pro",
#           "model":       "MiniMax-M3",
#           "base_url":    "https://api.minimaxi.com/anthropic",
#           "api_key_env": "MINIMAX_API_KEY",
#           "protocol":    "anthropic",
#           "fb_model":       "deepseek-v4-pro",
#           "fb_base_url":    "https://api.deepseek.com/anthropic",
#           "fb_api_key_env": "DEEPSEEK_API_KEY",
#           "fb_protocol":    "anthropic",
#       },
#   }
# ═══════════════════════════════════════════════════════════════════════════

OPERATION_CONFIG: dict[str, dict] = {}

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
        # §14 配置单源化（D14-2/3 修复 2026-06-14）：关键词从 stage_detector 迁入此处。
        # 加权计票：每条 (关键词, 权重)；同 pattern 多个关键词命中时累加。
        "keywords": [
            ("新增功能", 3), ("添加功能", 3), ("加个功能", 2), ("新增字段", 2),
            ("新功能", 2), ("做一个", 1), ("实现一个", 1),
            ("new feature", 3), ("add feature", 3), ("implement feature", 3),
            ("support ", 1), ("支持 ", 1), ("实现", 1), ("加", 1),
        ],
    },
    "bugfix": {
        "label":        "缺陷修复",
        "desc":         "修复缺陷",
        "default_flow": ["explore", "implement", "test"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("bug", 3), ("fix", 3), ("修复", 3), ("defect", 3),
            ("崩溃", 3), ("crash", 3), ("异常", 2), ("报错", 2), ("error", 2),
            ("修", 1),
        ],
    },
    "refactor": {
        "label":        "结构重构",
        "desc":         "结构重构",
        "default_flow": ["explore", "design", "implement", "test", "audit"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("refactor", 3), ("重构", 3), ("整理", 2), ("优化结构", 3),
            ("restructure", 3), ("reorganize", 2), ("改结构", 3), ("清理", 1),
        ],
    },
    "test": {
        "label":        "测试建设",
        "desc":         "写测试或分析测试结果",
        "default_flow": ["explore", "test", "audit"],
        "default_complexity": "medium",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("写测试", 3), ("补测试", 3), ("单元测试", 3), ("unit test", 3),
            ("integration test", 3), ("test case", 2), ("测试", 1),
        ],
    },
    "research": {
        "label":        "资料调研",
        "desc":         "资料调研/方案比较",
        "default_flow": ["explore", "plan", "design"],
        "default_complexity": "medium",
        "primary_model": "deepseek-v4-flash",
        "keywords": [
            ("调研", 3), ("research", 3), ("比较方案", 2), ("对比", 1),
            ("evaluate", 2), ("哪个好", 1), ("选哪个", 1), ("查一下", 1),
        ],
    },
    "migration": {
        "label":        "迁移改造",
        "desc":         "迁移/改造",
        "default_flow": ["plan", "design", "implement", "test", "audit"],
        "default_complexity": "complex",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("migration", 3), ("migrate", 3), ("迁移", 3), ("迁到", 2),
            ("迁过去", 2), ("升级", 2), ("upgrade", 2),
            ("迁移到", 3), ("升级到", 2), ("改造", 2),
        ],
    },
    "architecture": {
        "label":        "架构级任务",
        "desc":         "架构级任务",
        "default_flow": ["explore", "plan", "design", "audit"],
        "default_complexity": "complex",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("架构", 3), ("architecture", 3), ("系统设计", 3), ("顶层设计", 3),
            ("整体方案", 2), ("技术选型", 2), ("模块划分", 3),
        ],
    },
    "docs": {
        "label":        "文档编写",
        "desc":         "文档、说明、注释",
        "default_flow": ["explore", "implement"],
        "default_complexity": "simple",
        "primary_model": "deepseek-v4-flash",
        "keywords": [
            ("写文档", 3), ("写说明", 3), ("readme", 3), ("comment", 2),
            ("注释", 1), ("注释一下", 2), ("documentation", 3), ("docs", 2),
        ],
    },
    "audit": {
        "label":        "代码审计",
        "desc":         "代码审查、安全审查、性能审查",
        "default_flow": ["explore", "audit"],
        "default_complexity": "complex",
        "primary_model": "MiniMax-M3",
        "keywords": [
            ("code review", 3), ("安全审查", 3), ("安全审计", 3), ("security review", 3),
            ("审计", 3), ("漏洞", 2), ("vulnerability", 3), ("性能审查", 2),
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

# 关键词权重表（命中后累加；负权重为"明显简单"的反向信号）。
# §14 配置单源化（D9-3 修复 2026-06-14）：原本硬编码在 stage_detector.py，
# 现统一在 stage_config.py，stage_detector / proxy 通过派生读取。
COMPLEXITY_KEYWORDS: list[tuple[str, int]] = [
    # 高复杂度信号
    ("跨模块", 25), ("跨系统", 25), ("跨服务", 20), ("分布式", 20),
    ("迁移", 20), ("migration", 20), ("migrate", 20),
    ("架构", 25), ("architecture", 25), ("顶层设计", 30), ("系统设计", 25),
    ("性能审查", 20), ("安全审计", 20), ("安全审查", 20),
    ("重构", 15), ("refactor", 15), ("restructure", 15),
    ("审计", 15), ("audit", 15), ("code review", 15),
    ("分析测试失败", 20), ("失败原因", 15), ("排查", 10), ("根因", 15),
    ("方案对比", 15), ("比较方案", 15), ("调研", 10), ("research", 10),
    # 低复杂度信号（负权重）
    ("重命名", -15), ("rename", -15),
    ("改一行", -20), ("一行代码", -20), ("一行修复", -20),
    ("改个名字", -15), ("修个 typo", -20), ("typo", -20),
    ("确认一下", -10), ("快速确认", -10),
    ("简单", -5), ("就", -1),  # "就改一下" 类短句
]

# Pattern 基础分（PATTERN_BASE_SCORE）
PATTERN_BASE_SCORE: dict[str, int] = {
    "feature":      50,
    "bugfix":       45,
    "refactor":     55,
    "test":         40,
    "research":     50,
    "migration":    75,
    "architecture": 80,
    "docs":         20,
    "audit":        70,
}

# Stage 倍率（设计文档 §9 原则："复杂度必须基于当前阶段判断"）
# §9 D9-1 修复 2026-06-14：原 detect_complexity 不接 stage，导致同一 prompt
# 在 explore / implement / audit 三个 stage 下评分相同。
# 倍率语义：探索阶段通常简单（×0.7），设计/审计阶段通常复杂（×1.2~1.3）。
STAGE_COMPLEXITY_MULTIPLIER: dict[str, float] = {
    "explore":    0.7,   # 读代码/追调用链 → 通常简单
    "brainstorm": 0.8,   # 发散想法 → 偏简单
    "implement":  1.0,   # 编码 → 中性
    "decide":     1.1,   # 决策推理 → 偏复杂
    "plan":       1.1,   # 任务拆解 → 偏复杂
    "design":     1.2,   # 架构设计 → 通常复杂
    "audit":      1.3,   # 审计/审查 → 通常复杂
    "test":       1.0,   # 测试任务 → 中性（复杂度看具体子任务）
    "default":    1.0,   # 兜底
}

# ═══════════════════════════════════════════════════════════════════════════
# LLM 分类器配置（设计文档 §6.2 / §6.4 / §10 合并实现）
#
# 将原来三次独立的关键词分类（stage / pattern / complexity）合并为一次 LLM
# 调用。llm_classifier.py 读取此配置确定使用哪个模型做分类。
#
# 模型选择建议：
#   MiniMax-M3           — 推荐，分类准确、稳定、速度快
#   deepseek-v4-flash    — 备选，成本更低、响应更快
#
# 调用方优先级：传入 config > 本配置 > llm_classifier.DEFAULT_CLASSIFIER_CONFIG
# ═══════════════════════════════════════════════════════════════════════════

LLM_CLASSIFIER_CONFIG: dict[str, object] = {
    "model":       "MiniMax-M3",
    "base_url":    "https://api.minimaxi.com/anthropic",
    "api_key_env": "MINIMAX_API_KEY",
    "protocol":    "anthropic",
    "max_tokens":  512,
    "temperature": 0.0,
    "timeout":     15,
}

# ═══════════════════════════════════════════════════════════════════════════
# Workflow 角色模型（设计文档第 10 章算法 D10-5 修复 2026-06-14）
#
# 问题：原 build_workflow_plan 把 stage.fb_model 当 strong_model 用，
# 但 implement.fb_model = deepseek-v4-flash（弱模型），
# 导致 implement complex workflow = [flash, M3, flash] —— 违反"复杂任务用强模型"。
#
# 修复：定义全局 STRONG_MODEL / NORMAL_MODEL，build_workflow_plan 直接引用，
# 任何 stage 的 complex workflow 都真正走"强+常规+强"。
# ═══════════════════════════════════════════════════════════════════════════

STRONG_MODEL:  str = "deepseek-v4-pro"   # 复杂任务的规划/审计模型（设计文档 §10）
NORMAL_MODEL: str = "MiniMax-M3"        # 常规模型（主力执行）


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
    MODEL_TO_CONFIG.setdefault(
        c["model"],
        (c["base_url"], c["model"], c["api_key_env"], c["protocol"]),
    )
    MODEL_TO_CONFIG.setdefault(
        c["fb_model"],
        (c["fb_base_url"], c["fb_model"], c["fb_api_key_env"], c["fb_protocol"]),
    )

# ═══════════════════════════════════════════════════════════════════════════
# 关键词派生视图（§14 配置单源化 — D14-2/3/4 修复 2026-06-14）
#
# 原始 keywords 列表已合并进 STAGE_CONFIG（每 stage 的 `keywords` 字段）和
# PATTERN_CONFIG（每 pattern 的 `keywords` 字段）。stage_detector / 任何分类器
# 都从这里派生读取，避免硬编码副本。
#
# 派生约定：
#   STAGE_KEYWORDS   — list[(stage, list[str])]，顺序 = 优先级，detect_stage 用
#   PATTERN_KEYWORDS — dict[pattern, list[(kw, weight)]]，加权计票用
#   COMPLEXITY_KEYWORDS — list[(kw, weight)]，见上方 D9-3 定义
# ═══════════════════════════════════════════════════════════════════════════

# stage_detector.detect_stage() 用的优先级列表。
# 顺序与 STAGE_CONFIG 字典定义顺序保持一致（Python 3.7+ dict 保序）；
# 这样 explore → brainstorm → decide → design → plan → implement → test
# → audit → default 顺次尝试，文档要求的关键字优先级与原 stage_detector
# 完全一致。
STAGE_KEYWORDS: list[tuple[str, list[str]]] = [
    (stage, list(c.get("keywords", [])))
    for stage, c in STAGE_CONFIG.items()
    if c.get("keywords")  # 排除 default 等无关键词的 stage
]

# stage_detector.detect_task_pattern() 用的加权计票表。
# 直接把 PATTERN_CONFIG.keywords 拿出来（每个 pattern 一组 (关键词, 权重) 元组）。
PATTERN_KEYWORDS: dict[str, list[tuple[str, int]]] = {
    pattern: [(kw, int(w)) for kw, w in c.get("keywords", [])]
    for pattern, c in PATTERN_CONFIG.items()
}
