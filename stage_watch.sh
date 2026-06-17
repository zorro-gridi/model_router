#!/usr/bin/env bash
# stage_watch.sh — proxy.py 端口监控 + 崩溃自动重启
#
# 用法（通常由 stage CLI 调用，不直接用）：
#   stage_watch.sh <port>          # 启动 watcher（前台/后台看调用方式）
#   stage_watch.sh <port> stop     # 停掉本端口 watcher
#
# 设计要点（针对'会不会永远关不掉'）：
#   - 退出码 0 / SIGINT(130) / SIGTERM(143) → watcher 自身也退出，不再重启
#   - 退出码非 0 且非 130/143 → 退避 min(3*N, 60)s 后重启
#   - 连续 WATCH_MAX_RESTART 次崩溃后 watcher 主动放弃
#   - 用户 stop → SIGTERM → 等 STOP_TIMEOUT 秒 → SIGKILL 兜底
#
# 路径：
#   PID:  /tmp/stage_router_<port>.pid
#   LOG:  /tmp/stage_router_<port>.log
set -u

PORT="${1:-}"
ACTION="${2:-run}"   # run | stop

PROXY_SCRIPT="$HOME/.claude/hooks/model_router/proxy.py"
MAX_RESTART=20
BASE_COOLDOWN=3
MAX_COOLDOWN=60
STOP_TIMEOUT=3

if [[ -z "$PORT" ]] || ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
    echo "usage: $0 <port> [run|stop]" >&2
    exit 2
fi

PID_FILE="/tmp/stage_router_${PORT}.pid"
LOG_FILE="/tmp/stage_router_${PORT}.log"

# ── stop 子命令 ──
if [[ "$ACTION" == "stop" ]]; then
    if [[ ! -f "$PID_FILE" ]]; then
        echo "[watch] no watcher running for port $PORT"
        exit 0
    fi
    PID="$(cat "$PID_FILE" 2>/dev/null || echo 0)"
    if ! [[ "$PID" =~ ^[0-9]+$ ]] || [[ "$PID" -le 0 ]]; then
        echo "[watch] pid file corrupted, removing"
        rm -f "$PID_FILE"
        exit 0
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "[watch] pid $PID not alive, removing stale pid file"
        rm -f "$PID_FILE"
        exit 0
    fi
    echo "[watch] sending SIGTERM to process group $PID"
    # 杀整个进程组（-PID 形式把信号发给 PGID=watcher_pid 的所有进程，
    # 包含 watcher 自己 + 它后台跑的 proxy 子进程）
    kill -TERM -- -"$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null || true
    # 等 STOP_TIMEOUT 秒
    for _ in $(seq 1 $((STOP_TIMEOUT * 10))); do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "[watch] SIGTERM timeout, sending SIGKILL to process group"
        kill -KILL -- -"$PID" 2>/dev/null || kill -KILL "$PID" 2>/dev/null || true
        sleep 0.5
    fi
    rm -f "$PID_FILE"
    echo "[watch] watcher stopped (port $PORT)"
    exit 0
fi

# ── run 子命令（watcher 主循环）──
echo "[watch] starting watcher for port $PORT, pid=$$"
echo "$$" > "$PID_FILE"

# trap 用户主动停止 → 让 watcher 退出（不重启）
shutdown() {
    local sig="$1"
    echo "[watch] received $sig, shutting down (no restart)"
    # 把后台 proxy 也带走（如果还活着）
    if [[ "${PROXY_PID:-0}" -gt 0 ]] && kill -0 "$PROXY_PID" 2>/dev/null; then
        kill -TERM "$PROXY_PID" 2>/dev/null || true
        sleep 0.3
        kill -0 "$PROXY_PID" 2>/dev/null && kill -KILL "$PROXY_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    exit 0
}
trap 'shutdown SIGTERM' TERM
trap 'shutdown SIGINT'  INT

RESTART_COUNT=0

while true; do
    ATTEMPT=$((RESTART_COUNT + 1))
    echo "[watch] starting proxy on :$PORT (attempt $ATTEMPT/$MAX_RESTART) at $(date -u +%FT%TZ)"
    PROXY_PID=0
    python3 "$PROXY_SCRIPT" --port "$PORT" &
    PROXY_PID=$!

    # 等子进程或被中断
    wait "$PROXY_PID" 2>/dev/null
    EXIT_CODE=$?

    # 用户主动停：parent 收到信号 → trap 设了退出
    # proxy 自己被 SIGINT/SIGTERM 杀掉（父转发）：退出码 130/143/-15/-2
    if [[ $EXIT_CODE -eq 130 ]] || [[ $EXIT_CODE -eq 143 ]] \
       || [[ $EXIT_CODE -eq -15 ]] || [[ $EXIT_CODE -eq -2 ]]; then
        echo "[watch] proxy stopped by signal (exit=$EXIT_CODE), exiting watcher"
        rm -f "$PID_FILE"
        exit 0
    fi
    if [[ $EXIT_CODE -eq 0 ]]; then
        echo "[watch] proxy exited cleanly (exit=0), exiting watcher"
        rm -f "$PID_FILE"
        exit 0
    fi

    # 真崩溃
    RESTART_COUNT=$((RESTART_COUNT + 1))
    if [[ $RESTART_COUNT -ge $MAX_RESTART ]]; then
        echo "[watch] reached MAX_RESTART=$MAX_RESTART, giving up"
        rm -f "$PID_FILE"
        exit 1
    fi
    COOLDOWN=$((BASE_COOLDOWN * RESTART_COUNT))
    [[ $COOLDOWN -gt $MAX_COOLDOWN ]] && COOLDOWN=$MAX_COOLDOWN
    echo "[watch] proxy crashed (exit=$EXIT_CODE), restart in ${COOLDOWN}s ($RESTART_COUNT/$MAX_RESTART)"
    # 退避期间也能响应 TERM/INT（kill 不进 wait 循环，靠 trap 不可行 → 显式 sleep + 检测）
    STEPS=$((COOLDOWN * 10))
    for _ in $(seq 1 $STEPS); do
        sleep 0.1
    done
done
