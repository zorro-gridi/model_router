#!/usr/bin/env bash
# install.sh — model_router v1.3 一键安装
# 运行：bash install.sh
#
# v1.3 新特性：
#   - Task Pattern + Task Complexity 决策路由（替代 v1.2 stage 路由）
#   - Decision Lock：首次 TodoWrite 强信号后锁定模型选择
#   - YAML 权重配置（config/decision_weights.yaml）
#   - PostToolUse Hook 实时分数累积

set -e

HOOK_DIR="$HOME/.claude/hooks/model_router"
BIN_DIR="$HOME/.local/bin"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  model_router v1.3 安装"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. 创建目录
mkdir -p "$HOOK_DIR" "$HOOK_DIR/config" "$BIN_DIR"

# 2. 复制 Hook 脚本
# 注：若 SCRIPT_DIR == HOOK_DIR（如从已安装位置直接跑 install.sh），cp 会因
# 「源 == 目标」报错并被 set -e 终止；此时文件已在位，跳过即可。
if [ "$SCRIPT_DIR" = "$HOOK_DIR" ]; then
    echo "ℹ️  SCRIPT_DIR 与 HOOK_DIR 相同，跳过 Hook 文件复制（已就位）"
else
    # ── v1.2 遗留（保留兼容）──
    cp "$SCRIPT_DIR/stage_detector.py" "$HOOK_DIR/stage_detector.py"
    cp "$SCRIPT_DIR/stage_show.py"     "$HOOK_DIR/stage_show.py"
    cp "$SCRIPT_DIR/proxy.py"          "$HOOK_DIR/proxy.py"

    # ── v1.3 核心（决策引擎 + 状态机 + 持久化）──
    cp "$SCRIPT_DIR/decision_engine.py"      "$HOOK_DIR/decision_engine.py"
    cp "$SCRIPT_DIR/decision_lock.py"        "$HOOK_DIR/decision_lock.py"
    cp "$SCRIPT_DIR/state_persistence.py"    "$HOOK_DIR/state_persistence.py"
    cp "$SCRIPT_DIR/runtime_score.py"        "$HOOK_DIR/runtime_score.py"
    cp "$SCRIPT_DIR/session_state_machine.py" "$HOOK_DIR/session_state_machine.py"

    # ── v1.3 PostToolUse 工作线程 ──
    cp "$SCRIPT_DIR/post_tool_handler.py"   "$HOOK_DIR/post_tool_handler.py"
    cp "$SCRIPT_DIR/runtime_tracker.py"     "$HOOK_DIR/runtime_tracker.py"
    cp "$SCRIPT_DIR/todowrite_analyzer.py"  "$HOOK_DIR/todowrite_analyzer.py"

    # ── 原有支持模块（一并复制，确保完整）──
    cp "$SCRIPT_DIR/llm_classifier.py"  "$HOOK_DIR/llm_classifier.py"
    cp "$SCRIPT_DIR/model_alias.py"     "$HOOK_DIR/model_alias.py"
    cp "$SCRIPT_DIR/rate_limit.py"      "$HOOK_DIR/rate_limit.py"
    cp "$SCRIPT_DIR/stage_config.py"    "$HOOK_DIR/stage_config.py"

    chmod +x "$HOOK_DIR"/*.py
    echo "✅ Hook 脚本已复制 → $HOOK_DIR"
fi

# 2.5 复制 YAML 权重配置（v1.3 新增）
if [ -f "$SCRIPT_DIR/config/decision_weights.yaml" ]; then
    cp "$SCRIPT_DIR/config/decision_weights.yaml" "$HOOK_DIR/config/decision_weights.yaml"
    echo "✅ YAML 权重配置已复制 → $HOOK_DIR/config/decision_weights.yaml"
else
    echo "⚠️  未找到 config/decision_weights.yaml，将使用内置硬编码权重"
fi

# 3. 安装 stage CLI（符号链接，源更新即自动生效）
ln -sf "$SCRIPT_DIR/stage" "$BIN_DIR/stage"
echo "✅ stage CLI → $BIN_DIR/stage"

# 4. 注册 PostToolUse Hook（v1.3 新增）
# 自动在 ~/.claude/settings.json 中添加 model_router post_tool_handler hook 条目
# 注：此为 best-effort 操作 — jq 不可用或 settings.json 不存在时打印手动复制指引
PYTHON_BIN="/Users/zorro/miniconda3/envs/ts/bin/python"
SETTINGS_FILE="$HOME/.claude/settings.json"
POST_TOOL_CMD="$PYTHON_BIN $HOOK_DIR/post_tool_handler.py"

if [ -f "$SETTINGS_FILE" ] && command -v jq &>/dev/null; then
    # 检查是否已存在 model_router PostToolUse hook（幂等）
    if jq -e '.hooks.PostToolUse[]? | select(.hooks[].command | test("post_tool_handler"))' "$SETTINGS_FILE" > /dev/null 2>&1; then
        echo "ℹ️  PostToolUse hook (model_router) 已注册，跳过"
    else
        # 追加新 hook 条目（匹配所有 tool，不做过滤）
        jq --arg cmd "$POST_TOOL_CMD" '
          .hooks.PostToolUse += [{
            "matcher": "",
            "hooks": [{
              "type": "command",
              "command": $cmd
            }]
          }]
        ' "$SETTINGS_FILE" > "${SETTINGS_FILE}.tmp" && mv "${SETTINGS_FILE}.tmp" "$SETTINGS_FILE"
        echo "✅ PostToolUse hook 已注册 → $SETTINGS_FILE"
    fi
else
    echo "⚠️  无法自动注册 PostToolUse hook（jq 不可用或 settings.json 不存在）"
    echo "   请手动在 ~/.claude/settings.json 的 hooks.PostToolUse 数组中添加："
    echo ""
    echo '   {'
    echo '     "matcher": "",'
    echo '     "hooks": [{'
    echo '       "type": "command",'
    echo "       \"command\": \"$POST_TOOL_CMD\""
    echo '     }]'
    echo '   }'
    echo ""
fi

# 5. 初始化 .env（如果不存在）
if [ ! -f "$HOOK_DIR/.env" ]; then
    if [ -f "$SCRIPT_DIR/.env.example" ]; then
        cp "$SCRIPT_DIR/.env.example" "$HOOK_DIR/.env"
        chmod 600 "$HOOK_DIR/.env"
        echo "✅ .env 模板已复制到 $HOOK_DIR/.env（请编辑填入 key）"
        NEED_ENV_FILL=1
    else
        echo "⚠️  未找到 .env.example，请手动创建 $HOOK_DIR/.env"
    fi
else
    echo "ℹ️  $HOOK_DIR/.env 已存在，跳过"
fi


# 6. 检查 PATH
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    echo ""
    echo "⚠️  $BIN_DIR 不在 PATH 中，请添加到 ~/.zshrc 或 ~/.bashrc："
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# 7. 初始化阶段数据文件（v1.3 中 stage 降级为语义标签，不参与路由）
STAGE_DATA="$HOOK_DIR/current_stage"
mkdir -p "$HOOK_DIR"

OLD_STAGE="$HOME/.claude/stage"
if [ -f "$OLD_STAGE" ] && [ "$OLD_STAGE" != "$STAGE_DATA" ]; then
    cp "$OLD_STAGE" "$STAGE_DATA"
    rm -f "$OLD_STAGE"
    echo "ℹ️  老数据已迁移: $OLD_STAGE → $STAGE_DATA（$(cat "$STAGE_DATA")）"
elif [ ! -f "$STAGE_DATA" ]; then
    echo "default" > "$STAGE_DATA"
    echo "✅ 全局后备阶段初始化为: default → $STAGE_DATA"
else
    echo "ℹ️  全局后备阶段: $(cat "$STAGE_DATA") → $STAGE_DATA"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  model_router v1.3 安装完成！"
echo ""
if [ -n "$NEED_ENV_FILL" ]; then
echo "  1. ⚠️  编辑 $HOOK_DIR/.env 填入 API Keys："
echo "     MINIMAX_API_KEY=eyJ..."
echo "     DEEPSEEK_API_KEY=sk-..."
echo ""
echo "  2. 启动代理（新终端）："
else
echo "  1. 确认 $HOOK_DIR/.env 已配置 API Keys"
echo ""
echo "  2. 启动代理（新终端）："
fi
echo "     stage proxy"
echo ""
echo "  3. 配置 CC 使用代理（当前终端）："
echo "     export ANTHROPIC_BASE_URL=http://127.0.0.1:7878"
echo "     claude"
echo ""
echo "  v1.3 决策流程（无需手动干预）："
echo "     Prompt → LLM 分类 → 初始决策 → Runtime 累积 → TodoWrite Lock"
echo ""
echo "  手动控制："
echo "     ~model ds-v4-pro     ← 强制使用 deepseek-v4-pro（lock 不阻止）"
echo "     ~model mm3           ← 强制使用 MiniMax-M3"
echo "     stage                ← 查看当前状态"
echo "     stage status         ← 代理状态"
echo "     jq .decision $HOOK_DIR/../model_router_state_*.json  ← 查看决策记录"
echo ""
echo "  YAML 配置（可热更，重启 proxy 生效）："
echo "     $HOOK_DIR/config/decision_weights.yaml"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
