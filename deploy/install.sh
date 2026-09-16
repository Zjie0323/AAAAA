#!/bin/bash
# ============================================================
#  A股实时看板 · 服务器安装脚本 (systemd 模式)
#  用法: sudo ./install.sh
#  幂等: 可重复执行，会覆盖旧版本代码但保留配置
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$SCRIPT_DIR/deploy.conf"

if [ ! -f "$CONF" ]; then
    echo "[ERROR] 未找到配置文件: $CONF" >&2
    exit 1
fi
# shellcheck source=deploy.conf
. "$CONF"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; NC='\033[0m'
info() { echo -e "${GRN}[INFO]${NC} $*"; }
warn() { echo -e "${YLW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ---------- 0. 前置检查 ----------
[ "$(id -u)" -eq 0 ] || { err "需要 root 权限，请用: sudo $0"; exit 1; }

if ! command -v systemctl >/dev/null 2>&1; then
    err "系统未安装 systemd，本脚本不适用。请改用 scripts/ctl-nohup.sh"
    exit 1
fi

# ---------- 1. 探测 Python 解释器 ----------
detect_python() {
    if [ -n "${PYTHON_BIN:-}" ] && [ -x "$PYTHON_BIN" ]; then
        echo "$PYTHON_BIN"; return 0
    fi
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            # 必须 >= 3.7
            if "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,7) else 1)' 2>/dev/null; then
                command -v "$c"; return 0
            fi
        fi
    done
    return 1
}

PY="$(detect_python)" || {
    err "未找到 Python 3.7+ 解释器。"
    echo "  CentOS/RHEL:  sudo yum install -y python3"
    echo "  Ubuntu/Debian: sudo apt-get install -y python3"
    exit 1
}
PYVER="$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"
info "Python: $PY (版本 $PYVER)"
if [ -x /usr/bin/python3 ] && [ "$PY" != "/usr/bin/python3" ]; then
    warn "systemd 单元内将写入绝对路径 $PY ，确保开机时可用"
fi

# ---------- 2. 创建运行用户 ----------
if id "$RUN_USER" >/dev/null 2>&1; then
    info "运行用户已存在: $RUN_USER"
else
    # -r 系统账号, -s nologin 禁止交互登录
    if getent group "$RUN_GROUP" >/dev/null 2>&1; then
        useradd -r -s /sbin/nologin -g "$RUN_GROUP" "$RUN_USER" 2>/dev/null || \
        useradd -r -s /usr/sbin/nologin -g "$RUN_GROUP" "$RUN_USER"
    else
        groupadd -r "$RUN_GROUP" 2>/dev/null || true
        useradd -r -s /sbin/nologin -g "$RUN_GROUP" "$RUN_USER" 2>/dev/null || \
        useradd -r -s /usr/sbin/nologin -g "$RUN_GROUP" "$RUN_USER"
    fi
    info "已创建运行用户: $RUN_USER (禁止登录)"
fi

# ---------- 3. 创建目录并部署代码 ----------
mkdir -p "$INSTALL_DIR" "$LOG_DIR"
info "部署目录: $INSTALL_DIR"

if [ ! -f "$SCRIPT_DIR/app/server.py" ]; then
    err "缺少 app/server.py，安装包不完整"
    exit 1
fi
install -m 0755 -o "$RUN_USER" -g "$RUN_GROUP" "$SCRIPT_DIR/app/server.py" "$INSTALL_DIR/server.py"
install -m 0644 -o "$RUN_USER" -g "$RUN_GROUP" "$SCRIPT_DIR/app/index.html" "$INSTALL_DIR/index.html"
install -m 0644 -o "$RUN_USER" -g "$RUN_GROUP" "$CONF" "$INSTALL_DIR/deploy.conf"
info "已部署: server.py / index.html / deploy.conf"

# ---------- 3.5 部署竞价名单工具链（/api/bid_watch 的真正数据来源）----------
# server.py 以 ../astock-screen/ 为基准目录：既 import reco_engine(六维分/赚钱效应分/
# 买点门控)，也读取 gen_tomorrow.py 产出的 tomorrow_watch.json。
# 工具链仅依赖 Python 标准库，上游为东方财富(涨停专题池)与腾讯(指数分时/日K)。
SCREEN_DIR="$(dirname "$INSTALL_DIR")/astock-screen"
if [ -d "$SCRIPT_DIR/app/screen" ]; then
    mkdir -p "$SCREEN_DIR"
    # cp -a 保留权限与时间；该目录同时是代码目录与数据目录
    cp -a "$SCRIPT_DIR/app/screen/." "$SCREEN_DIR/"
    MISSING=""
    for f in refresh_watch.py gen_tomorrow.py live_limit.py limit_up.py em.py reco_engine.py; do
        [ -f "$SCREEN_DIR/$f" ] || MISSING="$MISSING $f"
    done
    if [ -n "$MISSING" ]; then
        warn "工具链缺少:$MISSING —— 竞价名单刷新会失败"
    fi
    [ -f "$SCREEN_DIR/limit_up.json" ] || \
        warn "缺少 limit_up.json 基因池 —— 首次刷新会自动重算(较慢, 属正常)"
    KDATA_N=0
    if [ -d "$SCREEN_DIR/kdata" ]; then
        KDATA_N="$(find "$SCREEN_DIR/kdata" -name '*.json' 2>/dev/null | wc -l)"
    fi
    info "已部署竞价名单工具链: $SCREEN_DIR (日K缓存 $KDATA_N 个)"
    # 该目录服务进程需写入: live_raw.json / tomorrow_watch.json / 历史副本 / HTML
    chown -R "$RUN_USER:$RUN_GROUP" "$SCREEN_DIR"
else
    warn "缺少 app/screen/ —— /api/bid_watch 将返回 502（行情功能不受影响）"
fi

# 定盘快照 _bid_picks_YYYYMMDD.json 由服务进程直接写在 $INSTALL_DIR，
# 因此安装目录也必须归运行用户所有 —— 否则 9:25 定盘会静默落盘失败
# (界面照常显示，但快照不落盘、重启后丢失)。
chown -R "$RUN_USER:$RUN_GROUP" "$INSTALL_DIR"

# 日志目录属主（systemd 内以 $RUN_USER 运行，需要写权限）
chown -R "$RUN_USER:$RUN_GROUP" "$LOG_DIR"
chmod 0755 "$LOG_DIR"

# ---------- 4. 清理端口残留（避免旧实例占位）----------
if command -v ss >/dev/null 2>&1; then
    OLD_PIDS="$(ss -lntp 2>/dev/null | awk -v p=":$SERVE_PORT" '$4 ~ p {print $NF}' \
                | grep -oP 'pid=\K[0-9]+' | sort -u || true)"
    if [ -n "$OLD_PIDS" ]; then
        warn "端口 $SERVE_PORT 被占用，PID: $(echo $OLD_PIDS | tr '\n' ' ')"
        warn "若为旧版本服务，请先执行: systemctl stop $APP_NAME"
    fi
fi

# ---------- 5. 生成并安装 systemd 单元 ----------
UNIT_SRC="$SCRIPT_DIR/systemd/${APP_NAME}.service.template"
[ -f "$UNIT_SRC" ] || { err "缺少单元模板: $UNIT_SRC"; exit 1; }

UNIT_TMP="$(mktemp)"
sed -e "s|INSTALL_DIR_PLACEHOLDER|$INSTALL_DIR|g" \
    -e "s|LOG_DIR_PLACEHOLDER|$LOG_DIR|g" \
    -e "s|RUN_USER_PLACEHOLDER|$RUN_USER|g" \
    -e "s|RUN_GROUP_PLACEHOLDER|$RUN_GROUP|g" \
    -e "s|PYTHON_BIN_PLACEHOLDER|$PY|g" \
    -e "s|SERVE_PORT_PLACEHOLDER|$SERVE_PORT|g" \
    -e "s|STOP_TIMEOUT_PLACEHOLDER|$STOP_TIMEOUT|g" \
    "$UNIT_SRC" > "$UNIT_TMP"

install -m 0644 "$UNIT_TMP" "/etc/systemd/system/${APP_NAME}.service"
rm -f "$UNIT_TMP"
info "已安装单元: /etc/systemd/system/${APP_NAME}.service"

# ---------- 5.5 安装竞价名单刷新单元（盘后自动生成次日名单）----------
# 主服务只负责"读"名单；名单的"生成"是独立的一次性任务，由 timer 每交易日
# 15:10 触发，避免把网络抓取与计算压进常驻服务进程。
REFRESH_UNIT="$SCRIPT_DIR/systemd/${APP_NAME}-refresh.service.template"
REFRESH_TIMER="$SCRIPT_DIR/systemd/${APP_NAME}-refresh.timer.template"
if [ -f "$REFRESH_UNIT" ] && [ -f "$REFRESH_TIMER" ]; then
    for pair in "$REFRESH_UNIT:${APP_NAME}-refresh.service" \
                "$REFRESH_TIMER:${APP_NAME}-refresh.timer"; do
        TPL="${pair%%:*}"; DST="${pair##*:}"
        TMPU="$(mktemp)"
        sed -e "s|INSTALL_DIR_PLACEHOLDER|$INSTALL_DIR|g" \
            -e "s|SCREEN_DIR_PLACEHOLDER|$SCREEN_DIR|g" \
            -e "s|LOG_DIR_PLACEHOLDER|$LOG_DIR|g" \
            -e "s|RUN_USER_PLACEHOLDER|$RUN_USER|g" \
            -e "s|RUN_GROUP_PLACEHOLDER|$RUN_GROUP|g" \
            -e "s|PYTHON_BIN_PLACEHOLDER|$PY|g" \
            -e "s|APP_NAME_PLACEHOLDER|$APP_NAME|g" \
            "$TPL" > "$TMPU"
        install -m 0644 "$TMPU" "/etc/systemd/system/$DST"
        rm -f "$TMPU"
    done
    info "已安装刷新单元: ${APP_NAME}-refresh.{service,timer} (每交易日 15:10)"
else
    warn "缺少刷新单元模板 —— 竞价名单将不会自动生成"
fi

# ---------- 6. 安装启停脚本到 /usr/local/bin ----------
# 安装到 /usr/local/bin 后，脚本同目录不再有 lib.sh / deploy.conf，
# 因此把配置以绝对路径内联注入，确保命令在任意位置、任意用户下都能执行。
for s in ctl-start.sh ctl-stop.sh ctl-restart.sh ctl-status.sh ctl-check.sh ctl-port.sh ctl-uninstall.sh; do
    SRC="$SCRIPT_DIR/scripts/$s"
    [ -f "$SRC" ] || continue
    CMD_NAME="${s#ctl-}"; CMD_NAME="${CMD_NAME%.sh}"
    TMPF="$(mktemp)"
    # 注入: 显式指定配置路径 + 内联 lib.sh（函数库）
    {
        sed -e "s|^source \"\$(dirname \"\${BASH_SOURCE\[0\]}\")/lib.sh\".*|LIB_OVERRIDE=\"$INSTALL_DIR/lib.sh\"|" \
            "$SRC"
    } > "$TMPF"

    # 在 LIB_OVERRIDE 行后插入真正的 source 逻辑
    awk -v lib="$INSTALL_DIR/lib.sh" '
        /^LIB_OVERRIDE=/ {
            print "if [ -f \"" lib "\" ]; then"
            print "    . \"" lib "\""
            print "else"
            print "    echo \"[ERROR] 缺少 $lib ，请重新执行 install.sh\" >&2"
            print "    exit 1"
            print "fi"
            next
        }
        { print }
    ' "$TMPF" > "$TMPF.2"
    mv "$TMPF.2" "$TMPF"

    install -m 0755 "$TMPF" "/usr/local/bin/${APP_NAME}-${CMD_NAME}"
    rm -f "$TMPF"
done
# 函数库与配置一起放到安装目录，供已安装命令引用
install -m 0644 "$SCRIPT_DIR/scripts/lib.sh" "$INSTALL_DIR/lib.sh"

# 手动刷新命令：与盘后定时任务同一入口，用于排障/补跑（休市日会直接跳过）
REFRESH_CMD_TMP="$(mktemp)"
cat > "$REFRESH_CMD_TMP" <<EOF
#!/bin/bash
# 竞价观察名单 · 手动刷新（等价于 systemctl start ${APP_NAME}-refresh）
set -uo pipefail
APP="${APP_NAME}"
if ! systemctl list-unit-files "\${APP}-refresh.service" >/dev/null 2>&1; then
    echo "[ERROR] 未安装 \${APP}-refresh.service，请重新执行 sudo ./install.sh" >&2
    exit 1
fi
echo ">>> 触发竞价名单刷新（约 1-3 分钟；休市日会打印「今日休市」并直接结束）..."
systemctl start "\${APP}-refresh.service"
echo ">>> 完成。最近日志："
journalctl -u "\${APP}-refresh.service" -n 30 --no-pager
EOF
install -m 0755 "$REFRESH_CMD_TMP" "/usr/local/bin/${APP_NAME}-refresh"
rm -f "$REFRESH_CMD_TMP"

info "已安装命令: ${APP_NAME}-start / -stop / -restart / -status / -check / -port / -uninstall / -refresh"

# ---------- 7. 重载并启动 ----------
systemctl daemon-reload
info "已 daemon-reload"

systemctl enable "$APP_NAME" >/dev/null 2>&1 && info "已设置开机自启"
systemctl restart "$APP_NAME"
info "服务已启动，等待就绪..."

# 定时器：每交易日 15:10 生成次日名单（Persistent=true 会补跑错过的任务）
if [ -f "/etc/systemd/system/${APP_NAME}-refresh.timer" ]; then
    if systemctl enable --now "${APP_NAME}-refresh.timer" >/dev/null 2>&1; then
        NEXT_RUN="$(systemctl list-timers "${APP_NAME}-refresh.timer" --no-pager 2>/dev/null \
                    | awk 'NR==2 {print $1" "$2" "$3}')"
        info "已启用盘后自动刷新: ${APP_NAME}-refresh.timer  下次触发: ${NEXT_RUN:-待定}"
    else
        warn "定时器启用失败 —— 可手动执行 ${APP_NAME}-refresh 生成名单"
    fi
fi

# ---------- 8. 自检 ----------
sleep 2
if systemctl is-active --quiet "$APP_NAME"; then
    info "服务状态: active (running)"
else
    err "服务启动失败，最近日志:"
    journalctl -u "$APP_NAME" -n 30 --no-pager || true
    exit 1
fi

# ---------- 9. 首次生成竞价名单 ----------
# 否则从安装完成到「下一交易日 15:10」之间 /api/bid_watch 一直返回 502。
# --no-block: 不阻塞安装流程（抓取+计算约 1-3 分钟）；休市日脚本会自行跳过。
if [ -f "/etc/systemd/system/${APP_NAME}-refresh.service" ]; then
    if systemctl start --no-block "${APP_NAME}-refresh.service" >/dev/null 2>&1; then
        info "已触发首次名单生成（后台执行，约 1-3 分钟）"
        echo "        跟踪: journalctl -u ${APP_NAME}-refresh -f"
    else
        warn "首次名单生成触发失败，稍后可手动执行 ${APP_NAME}-refresh"
    fi
fi

# 探测本机可访问 IP
LOCAL_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)"
[ -z "$LOCAL_IP" ] && LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

echo ""
echo "============================================================"
echo -e "${GRN} 安装完成${NC}"
echo "============================================================"
echo "  验证地址:"
echo "    本机      http://127.0.0.1:${SERVE_PORT}"
if [ -n "$LOCAL_IP" ]; then
    echo "    局域网    http://${LOCAL_IP}:${SERVE_PORT}"
fi
echo ""
echo "  竞价推荐数据:"
echo "    名单文件  ${SCREEN_DIR}/tomorrow_watch.json"
echo "    自动生成  每交易日 15:10 (${APP_NAME}-refresh.timer)"
echo ""
echo "  常用命令:"
echo "    systemctl status  ${APP_NAME}"
echo "    systemctl restart ${APP_NAME}"
echo "    systemctl stop    ${APP_NAME}"
echo "    ${APP_NAME}-check                        # 一键健康检查"
echo "    ${APP_NAME}-refresh                      # 手动生成次日竞价名单"
echo "    journalctl -u ${APP_NAME} -f             # 跟踪主服务日志"
echo "    journalctl -u ${APP_NAME}-refresh -n 50  # 查看盘后刷新日志"
echo ""
echo "  防火墙放行（如需局域网访问）:"
echo "    firewall-cmd --permanent --add-port=${SERVE_PORT}/tcp && firewall-cmd --reload"
echo "============================================================"
