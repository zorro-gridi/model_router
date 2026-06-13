#!/usr/bin/env bash
# install.sh — Stage-Aware Model Router 一键安装
# 运行：bash install.sh

set -e

HOOK_DIR="$HOME/.claude/hooks/model_router"
BIN_DIR="$HOME/.local/bin"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Stage-Aware Model Router 安装"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. 创建目录
mkdir -p "$HOOK_DIR" "$BIN_DIR"

# 2. 复制 Hook 脚本
# 注：若 SCRIPT_DIR == HOOK_DIR（如从已安装位置直接跑 install.sh），cp 会因
# 「源 == 目标」报错并被 set -e 终止；此时文件已在位，跳过即可。
if [ "$SCRIPT_DIR" = "$HOOK_DIR" ]; then
    echo "ℹ️  SCRIPT_DIR 与 HOOK_DIR 相同，跳过 Hook 文件复制（已就位）"
else
    cp "$SCRIPT_DIR/stage_detector.py" "$HOOK_DIR/stage_detector.py"
    cp "$SCRIPT_DIR/stage_show.py"     "$HOOK_DIR/stage_show.py"
    cp "$SCRIPT_DIR/proxy.py"          "$HOOK_DIR/proxy.py"
    chmod +x "$HOOK_DIR/stage_detector.py" "$HOOK_DIR/stage_show.py" "$HOOK_DIR/proxy.py"
    echo "✅ Hook 脚本已复制 → $HOOK_DIR"
fi

# 3. 安装 stage CLI（符号链接，源更新即自动生效）
# 源和数据文件分离：
#   源（CLI）    = $HOOK_DIR/stage                ← Python 脚本，ln -s 到 $BIN_DIR/stage
#   数据         = $HOOK_DIR/current_stage        ← 全局后备阶段名
#                  $HOOK_DIR/stage_<session_id>   ← 分 session 阶段名
#                  $HOOK_DIR/active_session       ← 活跃 session 指针
ln -sf "$SCRIPT_DIR/stage" "$BIN_DIR/stage"
echo "✅ stage CLI → $BIN_DIR/stage"

# 3.5 初始化 .env（如果不存在）
# 注：这里源 (.env.example) 和目标 (.env) 文件名不同，
# 即使 SCRIPT_DIR == HOOK_DIR 也不会触发 cp "identical" 错误。
if [ ! -f "$HOOK_DIR/.env" ]; then
    if [ -f "$SCRIPT_DIR/.env.example" ]; then
        cp "$SCRIPT_DIR/.env.example" "$HOOK_DIR/.env"
        chmod 600 "$HOOK_DIR/.env"   # 仅当前用户可读，保护 API key
        echo "✅ .env 模板已复制到 $HOOK_DIR/.env（请编辑填入 key）"
        NEED_ENV_FILL=1
    else
        echo "⚠️  未找到 .env.example，请手动创建 $HOOK_DIR/.env"
    fi
else
    echo "ℹ️  $HOOK_DIR/.env 已存在，跳过"
fi


# 5. 检查 PATH
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    echo ""
    echo "⚠️  $BIN_DIR 不在 PATH 中，请添加到 ~/.zshrc 或 ~/.bashrc："
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# 6. 初始化阶段数据文件
# 分 session 管理：
#   stage_<session_id>  — per-session 阶段文件（由 stage_detector.py hook 自动创建）
#   active_session      — 活跃 session 指针（由 hook 自动维护）
#   current_stage       — 全局后备（本脚本初始化）
STAGE_DATA="$HOOK_DIR/current_stage"
mkdir -p "$HOOK_DIR"

# 一次性迁移：把老路径 ~/.claude/stage 的内容迁到新文件
OLD_STAGE="$HOME/.claude/stage"
if [ -f "$OLD_STAGE" ] && [ "$OLD_STAGE" != "$STAGE_DATA" ]; then
    cp "$OLD_STAGE" "$STAGE_DATA"
    rm -f "$OLD_STAGE"
    echo "ℹ️  老数据已迁移: $OLD_STAGE → $STAGE_DATA（$(cat "$STAGE_DATA")）"
elif [ ! -f "$STAGE_DATA" ]; then
    # 新安装 / 数据文件丢失：初始化为 default
    echo "default" > "$STAGE_DATA"
    echo "✅ 全局后备阶段初始化为: default → $STAGE_DATA"
else
    echo "ℹ️  全局后备阶段: $(cat "$STAGE_DATA") → $STAGE_DATA"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  安装完成！使用方式："
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
echo "  4. 在 CC 内切换阶段（自动检测 or 手动）："
echo "     /stage implement    ← 手动切换"
echo "     （或直接说中文关键词，自动识别）"
echo ""
echo "  5. 在 shell 中查看状态："
echo "     stage               ← 当前阶段"
echo "     stage status        ← 代理状态"
echo "     stage audit         ← 手动切换阶段"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
