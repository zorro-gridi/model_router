#!/usr/bin/env bash
# test_statusline_sh_state_index_fallback.sh — 回归 statusline.sh 的 state_index SID 定位
#
# 目标：
#   1. CWD/anchor 都 miss 时，statusline 能从 state_index.json 的 _by_session
#      或 legacy project_root -> {session_id} 映射中按 SID 找到正确项目目录。
#   2. active_session 即使指向别的 session，也不应把第三行 route_model 读错。

set -euo pipefail

ROOT_DIR="$(mktemp -d)"
trap 'rm -rf "$ROOT_DIR"' EXIT

TEST_HOME="$ROOT_DIR/home"
mkdir -p "$TEST_HOME/.claude/hooks/model_router/anchors"

PROJECT_OK="$ROOT_DIR/project-ok"
PROJECT_STALE="$ROOT_DIR/project-stale"
mkdir -p "$PROJECT_OK/.claude" "$PROJECT_STALE/.claude"

SID="sid-statusline-001"

printf '%s\n' 'decide' > "$PROJECT_OK/.claude/stage_${SID}"
printf '%s\n' 'default' > "$PROJECT_STALE/.claude/stage_${SID}"

cat > "$PROJECT_OK/.claude/model_router_state_${SID}.json" <<'JSON'
{
  "version": "1.3",
  "session_id": "sid-statusline-001",
  "route_model": "deepseek-v4-pro",
  "task_complexity": "complex",
  "pattern": {
    "prediction": "architecture",
    "confidence": 0.91
  }
}
JSON

cat > "$PROJECT_STALE/.claude/model_router_state_${SID}.json" <<'JSON'
{
  "version": "1.3",
  "session_id": "sid-statusline-001",
  "route_model": "MiniMax-M3",
  "task_complexity": "simple",
  "pattern": {
    "prediction": "docs",
    "confidence": 0.20
  }
}
JSON

printf '%s\n' "$PROJECT_STALE/.claude/stage_other-session" > "$TEST_HOME/.claude/hooks/model_router/active_session"

cat > "$TEST_HOME/.claude/hooks/model_router/state_index.json" <<JSON
{
  "_by_session": {
    "$SID": {
      "project_root": "$PROJECT_OK",
      "stage": "decide",
      "last_active": 999
    }
  },
  "$PROJECT_OK": {
    "session_id": "$SID",
    "stage": "decide",
    "last_active": 999
  }
}
JSON

INPUT=$(cat <<JSON
{
  "model": {"display_name": "Claude"},
  "workspace": {"current_dir": "$ROOT_DIR/outside"},
  "cost": {"total_cost_usd": 0, "total_duration_ms": 0},
  "context_window": {"used_percentage": 0},
  "session_id": "$SID"
}
JSON
)

OUTPUT=$(printf '%s' "$INPUT" | HOME="$TEST_HOME" bash /Users/zorro/.claude/statusline.sh)
LINE3=$(printf '%s\n' "$OUTPUT" | tail -n 1)
CLEAN_LINE3=$(printf '%s' "$LINE3" | perl -pe 's/\e\[[0-9;]*m//g')

fail=0
assert_contains() {
  local haystack="$1" needle="$2" msg="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "  ✓ $msg"
  else
    echo "  ✗ $msg: missing '$needle'"
    fail=$((fail+1))
  fi
}

echo "state_index sid fallback:"
assert_contains "$CLEAN_LINE3" "deepseek-v4-pro" "route_model 来自 state_index 指向的正确项目"
assert_contains "$CLEAN_LINE3" "架构级任务" "pattern 优先读 state.pattern"
assert_contains "$CLEAN_LINE3" "complex" "complexity 优先读 state.task_complexity"

if [[ "$CLEAN_LINE3" == *"🤖 MiniMax-M3"* || "$CLEAN_LINE3" == *"🔀 MiniMax-M3"* ]]; then
  echo "  ✗ 不应回退到 active_session 指向的 stale project"
  fail=$((fail+1))
else
  echo "  ✓ 未被 active_session stale 项目污染"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
else
  echo "FAIL: $fail assertion(s) failed"
  exit 1
fi
