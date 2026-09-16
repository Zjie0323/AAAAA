#!/bin/bash
# ============================================================
#  查看服务状态（含资源占用、端口、验证地址、最近日志）
#  用法: astock-realtime-status
# ============================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"   # 安装后由 install.sh 替换为内联配置
has_systemd || { err "未检测到 systemd，请使用 ctl-nohup.sh status"; exit 1; }

title "== ${APP_NAME} 服务状态 =="
systemctl status "$APP_NAME" --no-pager -l 2>/dev/null || true

echo ""
title "== 概要 =="
ACTIVE="$(systemctl is-active "$APP_NAME" 2>/dev/null || echo unknown)"
ENABLED="$(systemctl is-enabled "$APP_NAME" 2>/dev/null || echo unknown)"
PID="$(systemctl show -p MainPID --value "$APP_NAME" 2>/dev/null || echo 0)"
echo "  运行状态  : $ACTIVE"
echo "  开机自启  : $ENABLED"
echo "  主进程 PID: $PID"

if [ "$PID" != "0" ] && [ -d "/proc/$PID" ]; then
    echo "  进程启动于: $(ps -o lstart= -p "$PID" 2>/dev/null | sed 's/^ *//')"
    echo "  内存占用  : $(ps -o rss= -p "$PID" 2>/dev/null | awk '{printf "%.1f MB", $1/1024}')"
    echo "  CPU 占用  : $(ps -o %cpu= -p "$PID" 2>/dev/null | sed 's/^ *//')%"
    echo "  累计 CPU  : $(ps -o time= -p "$PID" 2>/dev/null | sed 's/^ *//')"
fi

echo ""
title "== 端口监听 =="
if command -v ss >/dev/null 2>&1; then
    ss -lntp 2>/dev/null | grep -E ":${SERVE_PORT}\s" || warn "端口 ${SERVE_PORT} 未处于监听状态"
elif command -v netstat >/dev/null 2>&1; then
    netstat -lntp 2>/dev/null | grep -E ":${SERVE_PORT}\s" || warn "端口 ${SERVE_PORT} 未处于监听状态"
else
    warn "未找到 ss/netstat，跳过端口检查"
fi

echo ""
title "== 验证地址 =="
print_urls

echo ""
title "== 最近 10 行日志 =="
journalctl -u "$APP_NAME" -n 10 --no-pager 2>/dev/null | tail -10 || echo "  (无日志)"
echo ""
echo "完整日志: journalctl -u ${APP_NAME} -f"
