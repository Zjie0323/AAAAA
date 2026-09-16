#!/bin/bash
# ============================================================
#  停止服务
#  用法: astock-realtime-stop
# ============================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"   # 安装后由 install.sh 替换为内联配置
require_root
has_systemd || { err "未检测到 systemd，请使用 ctl-nohup.sh stop"; exit 1; }

title "== 停止 ${APP_NAME} =="

if ! systemctl is-active --quiet "$APP_NAME"; then
    warn "服务当前未运行 (状态: $(systemctl is-active "$APP_NAME" 2>/dev/null || echo unknown))"
    # 检查端口是否被游离进程占用
    if command -v ss >/dev/null 2>&1; then
        LEFTOVER="$(ss -lntp 2>/dev/null | grep ":${SERVE_PORT} " || true)"
        [ -n "$LEFTOVER" ] && { warn "但端口 ${SERVE_PORT} 仍被占用:"; echo "$LEFTOVER"; }
    fi
    exit 0
fi

systemctl stop "$APP_NAME"

# 等待进程真正退出（systemd 会按 KillSignal=SIGINT 优雅停止）
for i in $(seq 1 "$STOP_TIMEOUT"); do
    systemctl is-active --quiet "$APP_NAME" || break
    sleep 1
done

if systemctl is-active --quiet "$APP_NAME"; then
    err "服务在 ${STOP_TIMEOUT}s 内未停止，强制终止"
    systemctl kill -s SIGKILL "$APP_NAME" 2>/dev/null || true
    sleep 1
    if systemctl is-active --quiet "$APP_NAME"; then
        err "强制终止失败，请手动检查: ps aux | grep server.py"
        exit 1
    fi
    warn "已强制终止"
fi
info "服务已停止"

# 确认端口释放
sleep 1
if command -v ss >/dev/null 2>&1 && ss -lnt 2>/dev/null | grep -q ":${SERVE_PORT} "; then
    warn "端口 ${SERVE_PORT} 仍处于监听状态，可能有其他进程占用"
else
    info "端口 ${SERVE_PORT} 已释放"
fi
