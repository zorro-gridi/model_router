"""
model_alias.py — 用户自定义模型指令别名系统
=============================================
维护模型简称 → 规范模型名的映射，并提供自然语言关键词识别和
标准指令格式（~model / ~m）解析。

用户可在任意 prompt 中指定模型，优先于 op/stage 自动路由：
  ~model ds-v4-pro      → 显式指令
  ~m mm3                 → 短指令
  use deepseek-v4-flash  → 自然英语
  用 mm3                  → 自然中文
  ~model reset           → 清除覆盖，回到自动路由

本文件是别名映射的**唯一数据源**。新增简称只需修改 MODEL_ALIASES 字典。
"""

import re
from typing import Optional

# ── 模型简称 → 规范模型名 ───────────────────────────────────────────

MODEL_ALIASES: dict[str, str] = {
    # DeepSeek 系列
    "ds-v4-pro":        "deepseek-v4-pro",
    "ds-pro":           "deepseek-v4-pro",
    "deepseek-pro":     "deepseek-v4-pro",
    "ds-v4-flash":      "deepseek-v4-flash",
    "ds-flash":         "deepseek-v4-flash",
    "deepseek-flash":   "deepseek-v4-flash",
    "ds-v3":            "deepseek-v3",
    "ds-r1":            "deepseek-r1",
    "ds":               "deepseek-v4-pro",       # 默认指向 pro
    "deepseek":         "deepseek-v4-pro",       # 默认指向 pro

    # MiniMax 系列
    "mm3":              "MiniMax-M3",
    "minimax":          "MiniMax-M3",
    "mm-m3":            "MiniMax-M3",
    "mm":               "MiniMax-M3",            # 默认指向 M3

    # Claude 系列（原生 Anthropic）
    "sonnet":           "claude-sonnet-4-6",
    "claude-sonnet":    "claude-sonnet-4-6",
    "opus":             "claude-opus-4-8",
    "claude-opus":      "claude-opus-4-8",
    "haiku":            "claude-haiku-4-5-20251001",
    "claude-haiku":     "claude-haiku-4-5-20251001",
}


# ── 已知的有效规范模型名（从 stage_config 各配置收集中生成，供校验用）──

def _collect_known_models() -> set[str]:
    """从 STAGE_CONFIG 收集所有已配置的模型名。
    延迟导入避免循环依赖——本模块在 stage_config.py 之前也可能被加载。
    """
    models: set[str] = set()
    try:
        from stage_config import STAGE_CONFIG
        for c in STAGE_CONFIG.values():
            models.add(c["model"])
            models.add(c["fb_model"])
    except ImportError:
        pass
    # Claude 原生模型（即使没在 stage_config 中出现也要认）
    models.update({
        "claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5-20251001",
    })
    return models


KNOWN_MODEL_NAMES: set[str] = _collect_known_models()


# ── 正则：显式指令（~model / ~m，最高优先级）─────────────────────

# ~model <alias> 或 ~m <alias>
MODEL_OVERRIDE_PREFIX_RE = re.compile(
    r"(?:^|\s)~(?:model|m)\s+(\S+)",
    re.IGNORECASE,
)

# 显式指令的 reset/default 关键词
MODEL_RESET_WORDS = frozenset({"reset", "default", "auto", "clear", "off"})


# ── 正则：自然语言模式（中/英）────────────────────────────────────

# 匹配 "use/用/使用/切换到/switch to <alias>" 模式
# 支持位于 prompt 任意位置（非行首）
NATURAL_MODEL_RE = re.compile(
    r"(?:^|\s)(?:use|用|使用|切换(?:到|至)?|switch\s*(?:to)?)\s+(\S+)",
    re.IGNORECASE,
)


# ── 解析函数 ──────────────────────────────────────────────────────

def resolve_model(raw: str) -> Optional[str]:
    """将用户输入的简称或全名解析为规范模型名。

    Args:
        raw: 用户输入的原始字符串（如 "ds-v4-pro", "mm3", "MiniMax-M3"）

    Returns:
        规范模型名，无法识别时返回 None
    """
    if not raw:
        return None
    # 1. 别名查表（大小写不敏感）
    lower = raw.lower()
    if lower in MODEL_ALIASES:
        return MODEL_ALIASES[lower]
    # 2. 以规范名逐字匹配（大小写不敏感）
    for name in KNOWN_MODEL_NAMES:
        if name.lower() == lower:
            return name
    return None


def detect_model_override(prompt: str) -> tuple[Optional[str], bool]:
    """从用户 prompt 中检测模型覆盖指令。

    返回 (canonical_model_name, is_reset)。
    - is_reset=True:  用户要求清除覆盖，回到自动路由
    - 其它:            canon 为模型名或 None（无指令）

    优先级: 显式 ~model 指令 > 自然语言模式
    """
    canon, is_reset, _unknown = parse_model_override(prompt)
    return (canon, is_reset)


def parse_model_override(prompt: str) -> tuple[Optional[str], bool, Optional[str]]:
    """从用户 prompt 中检测模型覆盖指令，返回 (canonical, is_reset, unknown_alias)。

    - canonical:        解析出的规范模型名；无指令或未识别时为 None
    - is_reset:         用户是否要求清除覆盖（`~model reset`）
    - unknown_alias:    用户输入但无法识别的 alias（仅显式 `~model <name>` 时设置；
                        自然语言模式不识别时**不**回传，避免误报）

    设计文档 §12 D12-3：未识别的 alias 必须显式提示用户，不能静默失效。
    调用方拿到 unknown_alias 后应记 warning / 返回 400 / 在响应中提示合法 model 列表。
    """
    if not prompt:
        return (None, False, None)

    stripped = prompt.strip()
    prompt_lower = stripped.lower()

    # ── 1. 显式指令（最高优先级）──────────────────────────────
    m = MODEL_OVERRIDE_PREFIX_RE.search(stripped)
    if m:
        raw = m.group(1).strip()
        # 检查 reset/default/auto/clear/off 关键词
        if raw.lower() in MODEL_RESET_WORDS:
            return (None, True, None)  # is_reset
        canon = resolve_model(raw)
        if canon:
            return (canon, False, None)
        # 未识别 → 返回 None + 原始 alias（供调用方给 warning，修复 §12 D12-3 静默失效）
        return (None, False, raw)

    # ── 2. 自然语言模式 ─────────────────────────────────────
    # 注意：只在 ~model 未命中时才走自然语言，避免 "~model" 被
    # 自然语言正则误匹配。
    for m_nat in NATURAL_MODEL_RE.finditer(prompt_lower):
        raw = m_nat.group(1).strip()
        canon = resolve_model(raw)
        if canon:
            return (canon, False, None)

    return (None, False, None)
