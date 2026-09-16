#!/bin/bash
# ============================================================
#  nohup 模式启停脚本（systemd 的备用方案）
#  适用: 容器内、无 systemd 的精简系统、或不改系统配置的场景
#  用法: ./ctl-nohup.sh {start|stop|restart|status|check|log}
#  自启: 若需开机自启，将本脚本 start 加入 /etc/rc.local 或 crontab @reboot
#        echo "@reboot $APP_DIR/scripts/ctl-nohup.sh start" | crontab -
# ============================================================
set -uo pipefail

# nohup 模式下允许直接指定 app 目录（无需 install.sh）
APP_DIR="${APP_DIR:-}"
if [ -z "$APP_DIR" ]; then
    for p in "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../app" \
             "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"; do
        [ -f "$p/server.py" ] && { APP_DIR="$(cd "$p" && pwd)"; break; }
    done
fi

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GRN}[INFO]${NC} $*"; }
warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
title() { echo -e "${BLU}$*${NC}"; }

[ -n "$APP_DIR" ] && [ -f "$APP_DIR/server.py" ] || {
    err "未找到 server.py，请设置 APP_DIR 环境变量或确认目录结构"
    err "示例: APP_DIR=/opt/astock-realtime/app $0 start"
    exit 1
}

NAME="${APP_NAME:-astock-realtime}"
PORT="${SERVE_PORT:-8000}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
PID_FILE="$LOG_DIR/${NAME}.pid"
LOG_FILE="$LOG_DIR/${NAME}.log"
PYTHON_BIN="${PYTHON_BIN:-}"

mkdir -p "$LOG_DIR"

detect_python() {
    if [ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ]; then echo "$PYTHON_BIN"; return 0; fi
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1 && \
           "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,7) else 1)' 2>/dev/null; then
            command -v "$c"; return 0
        fi
    done
    return 1
}

get_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid; pid="$(cat "$PID_FILE" 2>/dev/null)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && { echo "$pid"; return 0; }
    return 1
}

local_ip() {
    local ip
    ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)"
    [ -z "$ip" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -z "$ip" ] && ip="127.0.0.1"
    echo "$ip"
}

print_urls() {
    echo "  本机    http://127.0.0.1:${PORT}"
    echo "  局域网  http://$(local_ip):${PORT}"
}

do_start() {
    title "== 启动 ${NAME} (nohup 模式) =="
    local pid
    if pid="$(get_pid)"; then
        warn "服务已在运行, PID=$pid"
        return 0
    fi

    local py; py="$(detect_python)" || { err "未找到 Python 3.7+"; exit 1; }
    info "Python: $py"

    # 清理端口残留
    if command -v ss >/dev/null 2>&1; then
        local occupy
        occupy="$(ss -lntp 2>/dev/null | grep ":${PORT} " || true)"
        if [ -n "$occupy" ]; then
            warn "端口 $PORT 被占用:"
            echo "$occupy"
            err "请先释放端口再启动"
            exit 1
        fi
    fi

    cd "$APP_DIR" || exit 1
    # 用 setsid 脱离终端，避免 SSH 断开导致进程被杀
    PYTHONUNBUFFERED=1 nohup setsid "$py" "$APP_DIR/server.py" "$PORT" \
        >> "$LOG_FILE" 2>&1 &
    local newpid=$!
    echo "$newpid" > "$PID_FILE"

    sleep 2
    if kill -0 "$newpid" 2>/dev/null; then
        info "启动成功, PID=$newpid"
    else
        err "启动失败，日志末尾:"
        tail -20 "$LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
    echo ""
    title "验证地址:"; print_urls
    echo ""
    echo "查看日志: $0 log"
}

do_stop() {
    title "== 停止 ${NAME} (nohup 模式) =="
    local pid
    if ! pid="$(get_pid)"; then
        warn "服务未运行"
        rm -f "$PID_FILE"
        return 0
    fi
    kill -INT "$pid" 2>/dev/null || true
    local i
    for i in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        warn "优雅停止超时，强制终止"
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
    info "已停止 (PID $pid)"
}

do_status() {
    title "== ${NAME} 状态 (nohup 模式) =="
    local pid
    if pid="$(get_pid)"; then
        info "运行中  PID=$pid"
        echo "  启动时间: $(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//')"
        echo "  内存占用: $(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.1f MB", $1/1024}')"
        echo "  日志文件: $LOG_FILE"
    else
        warn "未运行"
    fi
    echo ""
    title "端口监听:"
    if command -v ss >/dev/null 2>&1; then
        ss -lntp 2>/dev/null | grep -E ":${PORT}\s" || echo "  端口 ${PORT} 未监听"
    fi
    echo ""
    title "验证地址:"; print_urls
}

do_check() {
    title "== ${NAME} 健康检查 (nohup 模式) =="
    local fail=0
    local pid
    if pid="$(get_pid)"; then
        echo -e "  ${GRN}[PASS]${NC} 进程运行中 PID=$pid"
    else
        echo -e "  ${RED}[FAIL]${NC} 进程未运行"; fail=1
    fi

    if command -v curl >/dev/null 2>&1; then
        local code; code="$(curl -sS --noproxy '*' -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" 2>/dev/null)"
        if [ "$code" = "200" ]; then
            echo -e "  ${GRN}[PASS]${NC} HTTP 主页 200"
            for ep in /api/limit_up /api/sectors; do
                local ec; ec="$(curl -sS --noproxy '*' -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}${ep}" 2>/dev/null)"
                [ "$ec" = "200" ] && echo -e "  ${GRN}[PASS]${NC} ${ep} 200" || { echo -e "  ${RED}[FAIL]${NC} ${ep} HTTP $ec"; fail=1; }
            done
        else
            echo -e "  ${RED}[FAIL]${NC} HTTP 主页返回 $code"; fail=1
        fi
    else
        warn "未安装 curl，跳过 HTTP 检查"
    fi

    echo ""
    title "验证地址:"; print_urls
    [ "$fail" -eq 0 ] || exit 1
}

case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)  do_status ;;
    check)   do_check ;;
    log)     tail -f "$LOG_FILE" ;;
    *)
        echo "用法: $0 {start|stop|restart|status|check|log}"
        echo ""
        echo "环境变量（可覆盖）:"
        echo "  APP_DIR     server.py 所在目录（默认自动探测）"
        echo "  SERVE_PORT  监听端口（默认 8000）"
        echo "  LOG_DIR     日志目录（默认 \$APP_DIR/logs）"
        exit 1
        ;;
esac
