#!/usr/bin/env bash
# install.sh — Stage-Aware Model Router 一键安装
# 运行：bash install.sh

set -e

HOOK_DIR="$HOME/.claude/hooks/model_router"
BIN_DIR="$HOME/.local/bin"
SETTINGS="$HOME/.claude/settings.local.json"
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

# 3. 安装 stage CLI
cp "$SCRIPT_DIR/stage" "$BIN_DIR/stage"
chmod +x "$BIN_DIR/stage"
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

# 4. 更新 ~/.claude/settings.local.json（项目级 / 本地 settings，不污染全局）
if [ ! -f "$SETTINGS" ]; then
    echo "{}" > "$SETTINGS"
fi

python3 - <<'PYEOF'
import json, os, sys
from pathlib import Path

settings_path = Path.home() / ".claude" / "settings.local.json"
# 关键：hook_dir 必须包含 model_router 子目录，与 cp 目标 HOOK_DIR 一致
hook_dir = Path.home() / ".claude" / "hooks" / "model_router"

try:
    settings = json.loads(settings_path.read_text())
except Exception:
    settings = {}

# 确保 hooks 键存在
settings.setdefault("hooks", {})

# UserPromptSubmit Hook：阶段检测
settings["hooks"]["UserPromptSubmit"] = [
    {
        "hooks": [
            {
                "type": "command",
                "command": f"python3 {hook_dir}/stage_detector.py"
            }
        ]
    }
]

# Stop Hook：每轮结束显示阶段
settings["hooks"]["Stop"] = [
    {
        "hooks": [
            {
                "type": "command",
                "command": f"python3 {hook_dir}/stage_show.py",
                "async": True
            }
        ]
    }
]

settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))
print(f"✅ settings.local.json 已更新: {settings_path}")
PYEOF

# 5. 检查 PATH
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    echo ""
    echo "⚠️  $BIN_DIR 不在 PATH 中，请添加到 ~/.zshrc 或 ~/.bashrc："
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# 6. 初始化阶段文件
echo "default" > "$HOME/.claude/stage"
echo "✅ 阶段初始化为: default"

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
