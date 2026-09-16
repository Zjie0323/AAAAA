#!/bin/bash
# ============================================================
#  修改监听端口（轻量，不重装代码）
#  用法: sudo ./scripts/ctl-port.sh <新端口>
#        sudo ./scripts/ctl-port.sh            # 只查看当前端口
#
#  做四件事:
#    1) 校验端口合法性(1-65535)且未被其他进程占用
#    2) 更新 deploy.conf 中的 SERVE_PORT
#    3) 重新渲染 systemd 单元并 daemon-reload
#    4) 重启服务 + 健康检查，失败自动回滚到原端口
# ============================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_root

NEW_PORT="${1:-}"

# ---------- 无参数: 仅展示当前配置 ----------
if [ -z "$NEW_PORT" ]; then
    title "== ${APP_NAME} 当前端口配置 =="
    echo "  deploy.conf SERVE_PORT : $SERVE_PORT"
    if has_systemd && [ -f "/etc/systemd/system/${APP_NAME}.service" ]; then
        EFFECTIVE="$(grep -oP 'ExecStart=.*\bserver\.py\s+\K[0-9]+' \
                     "/etc/systemd/system/${APP_NAME}.service" 2>/dev/null || echo "未知")"
        echo "  systemd 单元实际端口     : $EFFECTIVE"
        if [ "$EFFECTIVE" != "$SERVE_PORT" ] && [ "$EFFECTIVE" != "未知" ]; then
            warn "两者不一致！单元内的端口是安装时注入的，需要重跑 install.sh 或本脚本才能同步"
        fi
    fi
    echo ""
    echo "  改端口: sudo $0 <新端口>"
    exit 0
fi

# ---------- 校验端口合法性 ----------
if ! [[ "$NEW_PORT" =~ ^[0-9]+$ ]]; then
    err "端口必须是数字，收到: $NEW_PORT"
    exit 1
fi
if [ "$NEW_PORT" -lt 1 ] || [ "$NEW_PORT" -gt 65535 ]; then
    err "端口范围必须是 1-65535，收到: $NEW_PORT"
    exit 1
fi
# 提示常见保留/冲突端口（不阻断，仅提醒）
case "$NEW_PORT" in
    22|80|443|3306|5432|6379|8080|8081|9090|9200|27017)
        warn "端口 $NEW_PORT 是常见服务端口，可能已被占用" ;;
esac

OLD_PORT="$SERVE_PORT"
if [ "$NEW_PORT" -eq "$OLD_PORT" ]; then
    warn "新端口与当前端口相同 ($OLD_PORT)，无需修改"
    exit 0
fi

title "== 修改端口: ${OLD_PORT} -> ${NEW_PORT} =="

# ---------- 检查新端口是否被占用 ----------
if command -v ss >/dev/null 2>&1; then
    if ss -lnt 2>/dev/null | grep -qE ":${NEW_PORT}\s"; then
        err "端口 $NEW_PORT 已被占用，请先释放："
        ss -lntp 2>/dev/null | grep -E ":${NEW_PORT}\s"
        exit 1
    fi
    info "新端口 $NEW_PORT 空闲可用"
elif command -v netstat >/dev/null 2>&1; then
    if netstat -lnt 2>/dev/null | grep -qE ":${NEW_PORT}\s"; then
        err "端口 $NEW_PORT 已被占用"
        netstat -lntp 2>/dev/null | grep -E ":${NEW_PORT}\s"
        exit 1
    fi
else
    warn "未找到 ss/netstat，跳过端口占用检查"
fi

# ---------- 定位 deploy.conf 并备份 ----------
CONF_FILE="$(_cfg_locate 2>/dev/null)" || {
    err "未找到 deploy.conf，无法修改"
    exit 1
}
cp -a "$CONF_FILE" "${CONF_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
info "已备份配置: ${CONF_FILE}.bak.*"

# ---------- 更新 deploy.conf ----------
if grep -qE '^[[:space:]]*SERVE_PORT=' "$CONF_FILE"; then
    sed -i -E "s|^[[:space:]]*SERVE_PORT=.*|SERVE_PORT=${NEW_PORT}|" "$CONF_FILE"
else
    echo "SERVE_PORT=${NEW_PORT}" >> "$CONF_FILE"
fi
# 同步一份到安装目录（已安装命令从这里读配置）
if [ -f "$INSTALL_DIR/deploy.conf" ] && [ "$CONF_FILE" != "$INSTALL_DIR/deploy.conf" ]; then
    sed -i -E "s|^[[:space:]]*SERVE_PORT=.*|SERVE_PORT=${NEW_PORT}|" "$INSTALL_DIR/deploy.conf"
fi
info "deploy.conf 已更新为 SERVE_PORT=${NEW_PORT}"

# ---------- 重新渲染 systemd 单元 ----------
if has_systemd && [ -f "/etc/systemd/system/${APP_NAME}.service" ]; then
    UNIT="/etc/systemd/system/${APP_NAME}.service"
    cp -a "$UNIT" "${UNIT}.bak"
    # 只替换 ExecStart 末尾的端口号，其余保持原样（保留安装时的 Python 路径等）
    if grep -qE '^ExecStart=.*server\.py[[:space:]]+[0-9]+' "$UNIT"; then
        sed -i -E "s|^(ExecStart=.*server\.py[[:space:]]+)[0-9]+|\1${NEW_PORT}|" "$UNIT"
        info "systemd 单元已更新: ExecStart 端口 -> ${NEW_PORT}"
    else
        warn "未能识别 ExecStart 端口格式，请手动检查 $UNIT"
    fi
    systemctl daemon-reload

    # ---------- 重启并验证 ----------
    info "重启服务..."
    systemctl restart "$APP_NAME"
    for i in $(seq 1 $((HEALTH_TIMEOUT * 2))); do
        systemctl is-active --quiet "$APP_NAME" && break
        sleep 1
    done

    sleep 1
    HTTP_CODE=""
    if command -v curl >/dev/null 2>&1; then
        HTTP_CODE="$(curl -sS --noproxy '*' -m "$HEALTH_TIMEOUT" -o /dev/null \
                     -w '%{http_code}' "http://127.0.0.1:${NEW_PORT}/" 2>/dev/null)"
    fi

    if systemctl is-active --quiet "$APP_NAME" && [ "$HTTP_CODE" = "200" ]; then
        info "新端口 ${NEW_PORT} 验证通过 (HTTP 200)"
        rm -f "${UNIT}.bak"
    else
        err "新端口验证失败 (状态=$(systemctl is-active "$APP_NAME" 2>/dev/null), HTTP=${HTTP_CODE:-N/A})"
        warn "正在回滚到原端口 ${OLD_PORT} ..."
        cp -a "${UNIT}.bak" "$UNIT"
        sed -i -E "s|^[[:space:]]*SERVE_PORT=.*|SERVE_PORT=${OLD_PORT}|" "$CONF_FILE"
        [ -f "$INSTALL_DIR/deploy.conf" ] && \
            sed -i -E "s|^[[:space:]]*SERVE_PORT=.*|SERVE_PORT=${OLD_PORT}|" "$INSTALL_DIR/deploy.conf"
        systemctl daemon-reload
        systemctl restart "$APP_NAME"
        sleep 2
        rm -f "${UNIT}.bak"
        if systemctl is-active --quiet "$APP_NAME"; then
            warn "已回滚，服务运行在原端口 ${OLD_PORT}"
        else
            err "回滚后服务仍未启动，请检查: journalctl -u ${APP_NAME} -n 50"
        fi
        exit 1
    fi
else
    warn "未检测到 systemd 单元，仅更新了 deploy.conf"
    warn "nohup 模式请重启：APP_DIR=$INSTALL_DIR/app ./scripts/ctl-nohup.sh restart"
fi

# ---------- 完成提示 ----------
echo ""
title "== 完成 =="
echo "  端口: ${OLD_PORT} -> ${NEW_PORT}"
echo ""
title "  验证地址:"
echo "    本机    http://127.0.0.1:${NEW_PORT}"
echo "    局域网  http://$(local_ip):${NEW_PORT}"
echo ""
warn "记得同步防火墙规则（旧端口可一并关闭）:"
echo "  firewall-cmd --permanent --add-port=${NEW_PORT}/tcp"
echo "  firewall-cmd --permanent --remove-port=${OLD_PORT}/tcp"
echo "  firewall-cmd --reload"
echo ""
echo "  健康检查: ${APP_NAME}-check"
