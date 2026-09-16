# -*- coding: utf-8 -*-
"""竞价观察名单刷新入口 —— 服务器盘后自动任务与本地手动执行共用同一入口。

链路:
    limit_up.json  (历史涨停基因池; 缺失才重算, 输入 kdata/ + raw_stats.json)
        -> live_limit.py --fetch   今日收盘涨停池 -> live_raw.json
        -> gen_tomorrow.py         -> tomorrow_watch.json  (供 /api/bid_watch)

用法:
    python refresh_watch.py            交易日盘后执行（systemd timer 调用）
    python refresh_watch.py --force    忽略「今日休市」前置判定，强制跑一次

退出码: 0 = 成功，或今日休市无需执行; 1 = 执行失败 / 产物校验不通过

—— 两道护栏（都是踩过坑换来的）——
  1. 休市日必须跳过。否则会把上一交易日的静态快照重写成"今日名单"，基准日错位，
     次日复盘会退化成"自我对照"（晋级率/红盘率双双 100%，结论完全失效）。
  2. 生成前后各校验一次日期：live_raw.json 的 date 与 tomorrow_watch.json 的
     base_date 都必须等于今天。接口延迟或休市时宁可不出名单，也不出错名单。
"""
import os
import sys
import json
import time
import datetime
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REALTIME_DIR = os.path.abspath(os.path.join(HERE, "..", "astock-realtime"))
RAW = os.path.join(HERE, "live_raw.json")
GENEPOOL = os.path.join(HERE, "limit_up.json")
WATCH = os.path.join(HERE, "tomorrow_watch.json")

INDEX_REF = "sh000001"          # 上证指数：判断今日是否真有盘口
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def _open_by_index():
    """上证指数当日分时: data.date == 今天 即今日开市。
    返回 True/False；接口不可用(拿不到 date)返回 None 以请求上层降级。"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=%s" % INDEX_REF)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=10) as resp:
        j = json.loads(resp.read().decode("utf-8", "ignore"))
    node = ((j.get("data") or {}).get(INDEX_REF) or {})
    dt = str(((node.get("data") or {}).get("date")) or "").replace("-", "")
    if not dt:
        return None
    return dt == time.strftime("%Y%m%d")


def today_is_open():
    """今日是否开市。优先复用 server.py 的权威判定(与看板同一口径)，
    其次用本文件内置的指数探测，最后退化为「非周末」。"""
    try:
        if REALTIME_DIR not in sys.path:
            sys.path.insert(0, REALTIME_DIR)
        import server as S                      # noqa: E402  (模块级无副作用)
        return bool(S.market_open_today())
    except Exception as e:
        log("复用 server 判定失败(%s)，改用内置探测" % e)
    try:
        ok = _open_by_index()
        if ok is not None:
            return ok
        log("指数分时未返回 date，退化为工作日判断")
    except Exception as e:
        log("指数探测失败(%s)，退化为工作日判断" % e)
    return datetime.date.today().weekday() < 5


def run(cmd, label):
    """执行子脚本并回显末尾输出。返回是否成功。"""
    log("→ %s" % label)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900, env=env)
    except Exception as e:
        log("   !! 执行异常: %s" % e)
        return False
    for line in (r.stdout or "").strip().splitlines()[-8:]:
        log("   %s" % line)
    if r.returncode != 0:
        log("   !! 退出码 %d" % r.returncode)
        for line in (r.stderr or "").strip().splitlines()[-8:]:
            log("   ERR %s" % line)
        return False
    return True


def main():
    log("=== 竞价名单刷新开始 (cwd=%s)" % HERE)

    if "--force" not in sys.argv and not today_is_open():
        log("今日休市，跳过刷新（保留上一交易日的名单）")
        return 0

    py = sys.executable
    if not os.path.exists(GENEPOOL):
        log("基因池 limit_up.json 缺失，先重算（读 kdata/ + raw_stats.json）")
        run([py, "limit_up.py"], "重算历史涨停基因池")
    else:
        log("基因池 limit_up.json 已存在，跳过重算（需刷新时用 /api/rebuild_genepool）")

    if not run([py, "live_limit.py", "--fetch"], "拉取今日收盘涨停快照"):
        log("!! 涨停快照抓取失败，中止（不生成名单，避免覆盖可用名单）")
        return 1

    # 护栏 1: 快照必须是今天 —— 休市或接口延迟时会是旧日期
    today = time.strftime("%Y%m%d")
    try:
        rr = json.load(open(RAW, encoding="utf-8"))
    except Exception as e:
        log("!! 读取 %s 失败: %s" % (RAW, e))
        return 1
    if str(rr.get("date")) != today:
        log("!! 涨停快照日期 %s != 今日 %s，中止生成（避免名单基准日错位）"
            % (rr.get("date"), today))
        return 1
    if not (rr.get("rows") or []):
        log("!! 今日涨停池为空，中止生成")
        return 1
    log("涨停 %s 只 / 炸板 %s" % (rr.get("tc"), rr.get("zb_tc")))

    if not run([py, "gen_tomorrow.py"], "生成明日竞价观察名单"):
        return 1

    # 护栏 2: 名单基准日必须等于今天
    try:
        w = json.load(open(WATCH, encoding="utf-8"))
    except Exception as e:
        log("!! 读取 %s 失败: %s" % (WATCH, e))
        return 1
    base = w.get("base_date")
    n = len(w.get("list") or [])
    log("名单基准日=%s 只数=%d A类=%s B类=%s 情绪=%s"
        % (base, n, w.get("a_count"), w.get("b_count"), w.get("mood")))
    if base != time.strftime("%Y-%m-%d"):
        log("!! 名单基准日 %s != 今日，校验不通过" % base)
        return 1
    if n == 0:
        log("!! 名单为空，校验不通过")
        return 1

    log("=== 刷新完成，次日集合竞价生效 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
