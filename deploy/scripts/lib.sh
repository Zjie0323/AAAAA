#!/bin/bash
# ============================================================
#  公共函数库（被其他 ctl-*.sh 脚本 source）
#  不作为独立命令使用
# ============================================================

# 定位 deploy.conf：优先安装目录，其次脚本同级上级目录
# 定位 deploy.conf
#   脚本可能被 install.sh 装到 /usr/local/bin（此时同目录没有 ../deploy.conf），
#   因此优先找安装目录下的配置，再兜底内置默认值，确保命令在任何位置都能跑。
_cfg_locate() {
    for p in "/opt/astock-realtime/deploy.conf" \
             "/etc/astock-realtime.conf" \
             "$(dirname "${BASH_SOURCE[0]}")/../deploy.conf" \
             "$(dirname "${BASH_SOURCE[0]}")/../../deploy.conf"; do
        [ -f "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

if CFG_PATH="$(_cfg_locate)"; then
    # shellcheck source=/dev/null
    . "$CFG_PATH"
else
    # 兜底默认值：与 deploy.conf 保持一致，避免命令因找不到配置文件而失效
    APP_NAME="astock-realtime"
    INSTALL_DIR="/opt/astock-realtime"
    LOG_DIR="/var/log/astock-realtime"
    SERVE_HOST="0.0.0.0"
    SERVE_PORT=8000
    PYTHON_BIN=""
    RUN_USER="astock"
    RUN_GROUP="astock"
    STOP_TIMEOUT=10
    HEALTH_TIMEOUT=5
fi

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GRN}[INFO]${NC} $*"; }
warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
title() { echo -e "${BLU}$*${NC}"; }

# 要求 root
require_root() {
    [ "$(id -u)" -eq 0 ] || { err "需要 root 权限，请用 sudo 执行"; exit 1; }
}

# 获取本机对外 IP
local_ip() {
    local ip
    ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)"
    [ -z "$ip" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -z "$ip" ] && ip="127.0.0.1"
    echo "$ip"
}

# 打印验证地址
print_urls() {
    local ip; ip="$(local_ip)"
    echo "  本机    http://127.0.0.1:${SERVE_PORT}"
    echo "  局域网  http://${ip}:${SERVE_PORT}"
}

# 判断 systemd 可用
has_systemd() { command -v systemctl >/dev/null 2>&1; }
