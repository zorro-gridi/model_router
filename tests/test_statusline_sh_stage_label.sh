#!/usr/bin/env bash
# test_statusline_sh_stage_label.sh — 回归 statusline.sh stage_*/stage_label/stage_model/stage_color
#
# 复现：statusline 第三行渲染 stage=test 时显示 "test → test" 这种 A→A 噪声。
# 根因：statusline.sh 的 stage_label() 等函数缺 test/explore 分支，
#       落到 `*) echo "$1"` 兜底，原样输出 stage key。
#
# 修复后：test → "测试验证"、explore → "探索理解"，且 emoji/color/model 都有专属分支。
#        unknown 仍走兜底分支原文输出，向后兼容。

set -u 2>/dev/null || true  # 允许 statusline.sh 内部用未声明的 BOLD 等
set +u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATUSLINE_SH="/Users/zorro/.claude/statusline.sh"

# 颜色变量 statusline.sh 内部使用，需先提供
CYAN=$'\033[36m';   MAGENTA=$'\033[35m'; BLUE=$'\033[34m'
WHITE=$'\033[97m';  GREEN=$'\033[32m';   YELLOW=$'\033[33m'
RED=$'\033[31m';    GRAY=$'\033[90m';    RESET=$'\033[0m'
DIM=$'\033[2m'
export CYAN MAGENTA BLUE WHITE GREEN YELLOW RED GRAY RESET DIM

# 抽出 statusline.sh 里的 4 个函数定义（line 102-145），source 到当前 shell
# 用 awk 精确切片避免 source 主流程触发的 `[: integer expected]` 报错
eval "$(awk '
  /^stage_emoji\(\)/   {p=1}
  /^complexity_color/{p=0}
  p {print}
' "$STATUSLINE_SH")"

# ── 测试 ──
fail=0
assert_eq() {
    local got="$1" want="$2" msg="$3"
    if [ "$got" = "$want" ]; then
        echo "  ✓ $msg"
    else
        echo "  ✗ $msg: got='$got' want='$want'"
        fail=$((fail+1))
    fi
}

echo "stage_label:"
assert_eq "$(stage_label test)"       "测试验证" "test → 测试验证"
assert_eq "$(stage_label explore)"    "探索理解" "explore → 探索理解"
assert_eq "$(stage_label brainstorm)" "头脑风暴" "brainstorm 不退化"
assert_eq "$(stage_label unknown_xyz)" "unknown_xyz" "未知 key 走兜底原文（兼容旧调用）"

echo "stage_emoji:"
assert_eq "$(stage_emoji test)"    "🧪" "test 配 🧪"
assert_eq "$(stage_emoji explore)" "🧭" "explore 配 🧭"
assert_eq "$(stage_emoji unknown_xyz)" "•" "未知 emoji 兜底"

echo "stage_model:"
assert_eq "$(stage_model test)"    "MiniMax-M3"  "test → MiniMax-M3"
assert_eq "$(stage_model explore)" "MiniMax-M3"  "explore → MiniMax-M3"
assert_eq "$(stage_model decide)"  "deepseek-v4-pro" "decide 不退化"

echo "stage_color:"
# 颜色变量是 ANSI 转义序列，含 \033。比对时把 \033 替换为 ESC 字面量再比
test_color=$(stage_color test | sed 's/\x1b/ESC/g')
assert_eq "$test_color" "ESC[33m" "test 配黄色"
test_color=$(stage_color explore | sed 's/\x1b/ESC/g')
assert_eq "$test_color" "ESC[34m" "explore 配蓝色"

echo
if [ "$fail" -eq 0 ]; then
    echo "ALL PASS"
    exit 0
else
    echo "FAIL: $fail assertion(s) failed"
    exit 1
fi
