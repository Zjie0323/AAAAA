#!/bin/bash
# ============================================================
#  卸载服务（默认保留代码与日志，仅移除 systemd 单元）
#  用法: sudo ./scripts/ctl-uninstall.sh          # 仅卸载服务
#        sudo ./scripts/ctl-uninstall.sh --purge  # 连代码/日志/用户一并清除
# ============================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"   # 安装后由 install.sh 替换为内联配置
require_root

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

title "== 卸载 ${APP_NAME} =="
if [ "$PURGE" -eq 1 ]; then
    warn "PURGE 模式：将删除 $INSTALL_DIR、$LOG_DIR 及用户 $RUN_USER"
    read -r -p "确认继续？输入 yes 继续: " ans
    [ "$ans" = "yes" ] || { echo "已取消"; exit 0; }
fi

# 1. 停止并禁用
if has_systemd; then
    # 先停定时器：否则卸载后盘后仍会自动跑刷新任务
    systemctl stop "${APP_NAME}-refresh.timer" 2>/dev/null || true
    systemctl disable "${APP_NAME}-refresh.timer" 2>/dev/null || true
    systemctl stop "$APP_NAME" 2>/dev/null || true
    systemctl disable "$APP_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/${APP_NAME}.service" \
          "/etc/systemd/system/${APP_NAME}-refresh.service" \
          "/etc/systemd/system/${APP_NAME}-refresh.timer"
    systemctl daemon-reload
    info "已移除 systemd 单元(含盘后刷新定时器)并 disable"
else
    warn "无 systemd，跳过单元清理"
fi

# 2. 移除命令
for cmd in start stop restart status check port uninstall refresh; do
    rm -f "/usr/local/bin/${APP_NAME}-${cmd}"
done
info "已移除 /usr/local/bin/${APP_NAME}-* 命令"

# 3. 清理 PID 残留
rm -f "/var/run/${APP_NAME}.pid" "/tmp/${APP_NAME}.pid" 2>/dev/null || true

if [ "$PURGE" -eq 1 ]; then
    SCREEN_DIR="$(dirname "$INSTALL_DIR")/astock-screen"
    rm -rf "$INSTALL_DIR" "$LOG_DIR"
    # 工具链目录含 kdata 日K缓存(约 22MB)，一并清除；空值/根路径做保护
    if [ -n "$SCREEN_DIR" ] && [ "$SCREEN_DIR" != "/" ]; then
        rm -rf "$SCREEN_DIR"
    fi
    info "已删除 $INSTALL_DIR、$LOG_DIR 与 $SCREEN_DIR"
    if id "$RUN_USER" >/dev/null 2>&1; then
        userdel "$RUN_USER" 2>/dev/null && info "已删除用户 $RUN_USER" || warn "用户删除失败（可能有进程占用）"
    fi
else
    info "已保留 $INSTALL_DIR / $LOG_DIR / $(dirname "$INSTALL_DIR")/astock-screen（如需彻底清理请加 --purge）"
fi

echo ""
info "卸载完成"
