#!/bin/bash
# ============================================================
#  一键健康检查（核心验证工具）
#  校验 5 层: 进程 -> 端口 -> HTTP 主页 -> 后端接口 -> 竞价名单
#  用法: astock-realtime-check
#  退出码: 0 全部通过 / 1 存在失败项
# ============================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"   # 安装后由 install.sh 替换为内联配置

PASS=0; FAIL=0
ok()   { echo -e "  ${GRN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
bad()  { echo -e "  ${RED}[FAIL]${NC} $*"; FAIL=$((FAIL+1)); }
skip() { echo -e "  ${YLW}[SKIP]${NC} $*"; }

# 带超时的 HTTP 取内容，优先 curl 兜底 wget
#   --noproxy '*' 关键：若服务器设置了 http_proxy 环境变量，curl 会把
#   127.0.0.1 的请求也发给代理，导致误报 502/连接失败。强制直连避免误判。
http_get() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -sS --noproxy '*' -m "$HEALTH_TIMEOUT" -o /dev/null -w '%{http_code}' "$url" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T "$HEALTH_TIMEOUT" --no-proxy -O /dev/null "$url" 2>/dev/null && echo 200 || echo 000
    else
        echo "NOCLIENT"
    fi
}

# 取 HTTP 响应体（用于内容断言）
http_body() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -sS --noproxy '*' -m "$HEALTH_TIMEOUT" "$url" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T "$HEALTH_TIMEOUT" --no-proxy -O - "$url" 2>/dev/null
    fi
}

LOCAL_URL="http://127.0.0.1:${SERVE_PORT}"

title "============================================================"
title "  ${APP_NAME} 健康检查   $(date '+%Y-%m-%d %H:%M:%S')"
title "============================================================"
echo ""

# ---------- 1. 服务进程 ----------
title "[1/5] 服务进程"
if has_systemd; then
    STATE="$(systemctl is-active "$APP_NAME" 2>/dev/null || echo unknown)"
    PID="$(systemctl show -p MainPID --value "$APP_NAME" 2>/dev/null || echo 0)"
    if [ "$STATE" = "active" ] && [ "$PID" != "0" ]; then
        ok "systemd 状态 active, PID=$PID"
    elif [ "$STATE" = "active" ]; then
        warn "状态 active 但 PID 为 0，可能正在重启"
        FAIL=$((FAIL+1))
    else
        bad "systemd 状态: $STATE (期望 active)"
    fi
else
    skip "无 systemd，跳过"
fi

# ---------- 2. 端口监听 ----------
title "[2/5] 端口监听"
if command -v ss >/dev/null 2>&1; then
    if ss -lnt 2>/dev/null | grep -qE ":${SERVE_PORT}\s"; then
        LISTEN_ADDR="$(ss -lnt 2>/dev/null | awk -v p=":${SERVE_PORT}" '$4 ~ p {print $4}' | head -1)"
        ok "端口 ${SERVE_PORT} 正在监听 ($LISTEN_ADDR)"
    else
        bad "端口 ${SERVE_PORT} 未监听"
    fi
elif command -v netstat >/dev/null 2>&1; then
    if netstat -lnt 2>/dev/null | grep -qE ":${SERVE_PORT}\s"; then
        ok "端口 ${SERVE_PORT} 正在监听"
    else
        bad "端口 ${SERVE_PORT} 未监听"
    fi
else
    skip "未找到 ss/netstat"
fi

# ---------- 3. HTTP 主页 ----------
title "[3/5] HTTP 主页  $LOCAL_URL"
CODE="$(http_get "$LOCAL_URL/")"
case "$CODE" in
    200)
        BODY="$(http_body "$LOCAL_URL/")"
        if echo "$BODY" | grep -q "A股\|看板\|涨停"; then
            ok "HTTP 200，页面内容正常 ($(echo -n "$BODY" | wc -c) 字节)"
        else
            ok "HTTP 200（未匹配到预期关键字，可能已改版）"
        fi
        ;;
    NOCLIENT)
        skip "未安装 curl/wget，无法做 HTTP 检查"
        echo "        安装: yum install -y curl  或  apt-get install -y curl"
        ;;
    000)
        bad "无法连接 $LOCAL_URL （连接被拒或超时）"
        ;;
    *)
        bad "HTTP 状态码异常: $CODE"
        ;;
esac

# ---------- 4. 后端数据接口 ----------
title "[4/5] 后端数据接口"
if [ "$CODE" = "200" ]; then
    for ep in "/api/limit_up" "/api/limit_down" "/api/sectors"; do
        EC="$(http_get "${LOCAL_URL}${ep}")"
        if [ "$EC" = "200" ]; then
            # 校验响应体为合法业务 JSON：期望 {"ok":true,...,"data":{...},"stale":bool}
            EB="$(http_body "${LOCAL_URL}${ep}")"
            if echo "$EB" | grep -q '"ok"'; then
                # 提取条数（data.count 字段）
                CNT="$(echo "$EB" | grep -o '"count": *[0-9]*' | head -1 | grep -o '[0-9]*')"
                [ -z "$CNT" ] && CNT="?"
                if echo "$EB" | grep -qE '"stale": *true'; then
                    ok "${ep}  HTTP 200 (上游限频降级，缓存快照 stale=true, 条数=$CNT)"
                else
                    ok "${ep}  HTTP 200 (数据新鲜, 条数=$CNT)"
                fi
                # 上游异常时 warn 字段会带提示
                WARNMSG="$(echo "$EB" | grep -o '"warn": *"[^"]*"' | head -1)"
                [ -n "$WARNMSG" ] && echo "        $WARNMSG"
            else
                bad "${ep}  HTTP 200 但响应体缺少 ok 字段（非预期 JSON）"
            fi
        else
            bad "${ep}  HTTP $EC"
        fi
    done

    # 上游连通性（服务需能访问东方财富，否则数据为空）
    echo ""
    title "  [附加] 上游行情接口连通性"
    for h in push2.eastmoney.com push2ex.eastmoney.com; do
        if command -v curl >/dev/null 2>&1; then
            UC="$(curl -sS -m "$HEALTH_TIMEOUT" -o /dev/null -w '%{http_code}' "https://${h}/" 2>/dev/null)"
            case "$UC" in
                200|301|302|403|404) ok "可达 $h (HTTP $UC)" ;;
                000) bad "不可达 $h —— 服务将无法取到行情数据" ;;
                *)   skip "$h 返回 HTTP $UC" ;;
            esac
        else
            skip "无 curl，跳过上游检测"
        fi
    done
else
    skip "主页不可用，跳过接口检查"
fi

# ---------- 5. 竞价观察名单 ----------
#  /api/bid_watch 的数据源，由盘后定时器生成；文件缺失时该接口返回 502
echo ""
title "[5/5] 竞价观察名单"
SCREEN_DIR="$(dirname "$INSTALL_DIR")/astock-screen"
WATCH_FILE="$SCREEN_DIR/tomorrow_watch.json"
if [ -f "$WATCH_FILE" ]; then
    BASE="$(grep -o '"base_date": *"[^"]*"' "$WATCH_FILE" 2>/dev/null \
            | head -1 | grep -o '[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}')"
    N="$(grep -c '"code"' "$WATCH_FILE" 2>/dev/null)"
    [ -z "$N" ] && N="?"
    ok "名单存在 (基准日 ${BASE:-?}，约 ${N} 只)"
    TODAY="$(date '+%Y-%m-%d')"
    if [ -n "$BASE" ] && [ "$BASE" != "$TODAY" ]; then
        echo "        说明: 基准日 ${BASE} 非今日 ${TODAY}。盘后生成、次日使用属正常；"
        echo "              若长期不更新，执行: sudo ${APP_NAME}-refresh"
    fi
else
    bad "名单不存在: $WATCH_FILE"
    echo "        生成: sudo ${APP_NAME}-refresh   （或等待盘后定时器 15:10）"
fi

if has_systemd && systemctl list-unit-files "${APP_NAME}-refresh.timer" >/dev/null 2>&1; then
    TSTATE="$(systemctl is-active "${APP_NAME}-refresh.timer" 2>/dev/null || echo unknown)"
    if [ "$TSTATE" = "active" ]; then
        NEXT="$(systemctl list-timers "${APP_NAME}-refresh.timer" --no-pager 2>/dev/null \
                | awk 'NR==2 {print $1" "$2" "$3}')"
        ok "刷新定时器 active，下次触发: ${NEXT:-待定}"
    else
        bad "刷新定时器状态 $TSTATE (期望 active)"
        echo "        启用: sudo systemctl enable --now ${APP_NAME}-refresh.timer"
    fi
else
    skip "未安装刷新定时器（无 systemd 环境请自建 cron，见 README 第七节）"
fi

# 接口层验证（仅在主页可用时做，避免重复报错）
if [ "$CODE" = "200" ]; then
    BC="$(http_get "${LOCAL_URL}/api/bid_watch")"
    if [ "$BC" = "200" ]; then
        ok "/api/bid_watch HTTP 200"
    else
        bad "/api/bid_watch HTTP $BC （名单未生成，或服务未重载新代码）"
    fi
fi

# ---------- 汇总 ----------
echo ""
title "============================================================"
if [ "$FAIL" -eq 0 ]; then
    echo -e "  ${GRN}检查通过${NC}   通过 $PASS 项，失败 0 项"
    echo ""
    title "  访问地址:"
    print_urls
    echo ""
    title "============================================================"
    exit 0
else
    echo -e "  ${RED}检查未通过${NC}  通过 $PASS 项，${RED}失败 $FAIL 项${NC}"
    echo ""
    echo "  排障建议:"
    echo "    1) 看日志:   journalctl -u ${APP_NAME} -n 50 --no-pager"
    echo "    2) 重启服务: systemctl restart ${APP_NAME}"
    echo "    3) 端口占用: ss -lntp | grep ${SERVE_PORT}"
    echo "    4) 刷新名单: sudo ${APP_NAME}-refresh"
    echo "    5) 刷新日志: journalctl -u ${APP_NAME}-refresh -n 30 --no-pager"
    echo ""
    title "============================================================"
    exit 1
fi
