#!/bin/bash
# ============================================================
#  重启服务（并自动做健康检查）
#  用法: astock-realtime-restart
# ============================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"   # 安装后由 install.sh 替换为内联配置
require_root
has_systemd || { err "未检测到 systemd，请使用 ctl-nohup.sh restart"; exit 1; }

title "== 重启 ${APP_NAME} =="
systemctl restart "$APP_NAME"
info "已发出 restart 指令，等待就绪..."

for i in $(seq 1 $((HEALTH_TIMEOUT * 2))); do
    systemctl is-active --quiet "$APP_NAME" && break
    sleep 1
done

if ! systemctl is-active --quiet "$APP_NAME"; then
    err "重启后服务未进入 active 状态，最近日志:"
    journalctl -u "$APP_NAME" -n 30 --no-pager
    exit 1
fi
info "进程已就绪 (PID $(systemctl show -p MainPID --value "$APP_NAME"))"

# 调用健康检查脚本
echo ""
HEALTH_SCRIPT="$(dirname "${BASH_SOURCE[0]}")/ctl-check.sh"
if [ -f "$HEALTH_SCRIPT" ]; then
    bash "$HEALTH_SCRIPT"
else
    echo ""
    title "验证地址:"
    print_urls
fi
