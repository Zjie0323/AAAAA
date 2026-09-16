#!/bin/bash
# ============================================================
#  启动服务
#  用法: astock-realtime-start    (或 sudo ./ctl-start.sh)
# ============================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"   # 安装后由 install.sh 替换为内联配置
require_root
has_systemd || { err "未检测到 systemd，请使用 ctl-nohup.sh start"; exit 1; }

title "== 启动 ${APP_NAME} =="

if systemctl is-active --quiet "$APP_NAME"; then
    warn "服务已在运行中，无需重复启动"
    echo "  当前状态: $(systemctl is-active "$APP_NAME")"
else
    systemctl start "$APP_NAME"
    # 等待就绪，最多 HEALTH_TIMEOUT*2 秒
    for i in $(seq 1 $((HEALTH_TIMEOUT * 2))); do
        systemctl is-active --quiet "$APP_NAME" && break
        sleep 1
    done
    if systemctl is-active --quiet "$APP_NAME"; then
        info "启动成功"
    else
        err "启动失败，最近日志:"
        journalctl -u "$APP_NAME" -n 30 --no-pager
        exit 1
    fi
fi

echo ""
info "进程 PID: $(systemctl show -p MainPID --value "$APP_NAME")"
echo ""
title "验证地址:"
print_urls
echo ""
echo "查看日志: journalctl -u ${APP_NAME} -f"
echo "健康检查: ${APP_NAME}-check"
