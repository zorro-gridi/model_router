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

    # OpenAI GPT 系列
    "gpt54":            "GPT-5.4",
    "gpt-5.4":          "GPT-5.4",
    "gpt54-mini":       "GPT-5.4-Mini",
    "gpt-5.4-mini":     "GPT-5.4-Mini",
    "gpt-mini":         "GPT-5.4-Mini",
}


# ── 已知的有效规范模型名（从 stage_config 各配置收集中生成，供校验用）──

def _collect_known_models() -> set[str]:
    """从 STAGE_CONFIG 收集所有已配置的模型名。
    延迟导入避免循环依赖——本模块在 stage_config.py 之前也可能被加载。
    """
    models: set[str] = set()
    try:
        from stage_config import MODEL_TO_CONFIG
        models.update(MODEL_TO_CONFIG.keys())
    except ImportError:
        pass
    # Claude 原生模型（即使没在 stage_config 中出现也要认）
    models.update({
        "claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5-20251001",
    })
    return models


KNOWN_MODEL_NAMES: set[str] = _collect_known_models()


# ── 正则：显式指令（~model / ~m，最高优先级）─────────────────────

# ~model <alias> 或 ~m <alias>，可选追加 `0`/`1` 持久化标志（2026-06-18）。
# group(1) = alias，group(2) = 可选 `0`/`1`。
# 注意：alias 部分用 `[^~\s]+`（不允许空格），让后续可能追加的 `0`/`1` 留给 group(2)。
# 若用 `\S+` 贪婪匹配 + `re.search` 找最长，alias 后的数字 flag 会被 alias "吞掉"。
MODEL_OVERRIDE_PREFIX_RE = re.compile(
    r"(?:^|\s)~(?:model|m)\s+([^~\s]+)(?:\s+([01]))?\b",
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
    canon, is_reset, _unknown, _persist = parse_model_override(prompt)
    return (canon, is_reset)


def parse_model_override(prompt: str) -> tuple[Optional[str], bool, Optional[str], bool]:
    """从用户 prompt 中检测模型覆盖指令，返回 (canonical, is_reset, unknown_alias, persist)。

    - canonical:        解析出的规范模型名；无指令或未识别时为 None
    - is_reset:         用户是否要求清除覆盖（`~model reset`）
    - unknown_alias:    用户输入但无法识别的 alias（仅显式 `~model <name>` 时设置；
                        自然语言模式不识别时**不**回传，避免误报）
    - persist:          是否将该覆盖写入 model_<sid> 让整个 session 持续生效。
                        - True  = 持久化（默认行为）：整个 session 路由都受此覆盖，
                                  直到用户 `~model reset` 才解除。
                        - False = 一次性（one-shot）：仅本请求使用，下一回合回到
                                  自动 stage 路由。

    持久化开关语法（2026-06-18 引入，~model <alias> 后追加数字）：
        ~model mm3         → persist=True（默认 = session 持续）
        ~model mm3 1       → persist=True（显式声明）
        ~model mm3 0       → persist=False（一次性不写盘）
        ~model mm3 一次    → 暂不支持（数字是最小集，扩展留作未来）

    设计文档 §12 D12-3：未识别的 alias 必须显式提示用户，不能静默失效。
    调用方拿到 unknown_alias 后应记 warning / 返回 400 / 在响应中提示合法 model 列表。
    """
    if not prompt:
        return (None, False, None, False)

    stripped = prompt.strip()
    prompt_lower = stripped.lower()

    # ── 1. 显式指令（最高优先级）──────────────────────────────
    m = MODEL_OVERRIDE_PREFIX_RE.search(stripped)
    if m:
        raw = m.group(1).strip()
        # 剥离尾随标点符号，避免文档/指令文本中的 placeholder
        # （如 `~model <model_alias>: 表示...`）的冒号被当作 alias 的一部分
        raw = raw.rstrip(':.,;)')
        if not raw:
            return (None, False, None, False)
        # 跳过角括号占位符（如 <model_alias>），这是文档/指令文本而非实际 alias
        if raw.startswith('<') or raw.startswith('['):
            return (None, False, None, False)
        # 检查 reset/default/auto/clear/off 关键词
        if raw.lower() in MODEL_RESET_WORDS:
            return (None, True, None, False)  # is_reset
        # ── 持久化开关解析（2026-06-18）────────────────────────
        # 0/1 标志已在正则 group(2) 里抽出（见 MODEL_OVERRIDE_PREFIX_RE）。
        # 默认 persist=True（= session 持续有效），仅当显式 0 时才转 one-shot。
        # 这样写避免 `\S+` 贪婪匹配把 `0`/`1` 吞进 alias，
        # 也避免后续多余 token 干扰（多余的字符如果用户在 alias 后面写了
        # 不构成 0/1 的东西，persist 仍然走默认 True，符合最小惊讶原则）。
        persist = True
        if m.group(2) is not None:
            persist = (m.group(2) == "1")
        canon = resolve_model(raw)
        if canon:
            return (canon, False, None, persist)
        # 未识别 → 返回 None + 原始 alias（供调用方给 warning，修复 §12 D12-3 静默失效）
        return (None, False, raw, persist)

    # ── 2. 自然语言模式 ─────────────────────────────────────
    # 注意：只在 ~model 未命中时才走自然语言，避免 "~model" 被
    # 自然语言正则误匹配。
    # 自然语言模式不接 persist 标志——只用「use mm3」这种说法的用户大概率是
    # 一次性意图，强行持久化反而违背直觉。
    for m_nat in NATURAL_MODEL_RE.finditer(prompt_lower):
        raw = m_nat.group(1).strip()
        canon = resolve_model(raw)
        if canon:
            return (canon, False, None, False)

    return (None, False, None, False)


# ── Provider 指令解析（provider 级 fallback，2026-06-16）─────────

PROVIDER_OVERRIDE_PREFIX_RE = re.compile(
    r"(?:^|\s)~(?:provider|prov)\s+(\S+)",
    re.IGNORECASE,
)

PROVIDER_RESET_WORDS = frozenset({"reset", "clear", "default", "auto", "off"})


def detect_provider_override(prompt: str) -> tuple[Optional[str], bool]:
    """从用户 prompt 中检测 provider 覆盖指令。

    返回 (provider_name, is_reset)。
    - is_reset=True:  用户要求清除 provider 级 sticky fallback
    - 其它:           provider_name 为 provider 名或 None（无指令 / 未识别）

    目前仅支持 ~provider reset（清除 sticky fallback），
    不支持 ~provider <name> 主动设置 provider（未来可扩展）。
    """
    if not prompt:
        return (None, False)

    stripped = prompt.strip()
    m = PROVIDER_OVERRIDE_PREFIX_RE.search(stripped)
    if not m:
        return (None, False)

    raw = m.group(1).strip()
    # 剥离尾随标点符号，避免文档/指令文本中的 placeholder
    raw = raw.rstrip(':.,;)')
    if not raw:
        return (None, False)
    if raw.lower() in PROVIDER_RESET_WORDS:
        return (None, True)  # is_reset

    # 未来扩展：~provider <name> 主动设置 provider
    # canon = resolve_provider(raw)
    # if canon:
    #     return (canon, False)
    return (None, False)
