#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股实时看板 - 后端代理服务 (生产级抗限频版)

数据源与功能:
  - 涨停/跌停个股:  东方财富 push2ex 涨停板专题池(getTopicZTPool / getTopicDTPool),
    直接给出 连板天数(lbc)/首次封板时间(fbt)/封单量(cnum)/封单金额(camount)/一字板(znh)/炸板(zs).
    该接口不可达时自动降级为 push2 全市场 clist + 代码前缀涨跌停判定(此时连板/封单字段为 null).
  - 板块资金流:     东方财富 push2 行业/概念板块主力净流入.
  - 个股当日分时:   东方财富 push2his stock/trends2, 按需拉取(点击触发)并短时缓存.

抗限频架构(核心):
  - 后台线程定时刷新行情到内存缓存, HTTP 请求只命中缓存, 用户刷新频率与上游频率解耦.
  - 指数退避: 刷新失败则下次间隔翻倍(15s->30s->...->120s 封顶), 成功即恢复.
  - 限频降级: 上游不可达仍返回最近成功缓存(stale=true), 页面不报错.
  - 多 host 兜底: push2 / push2ex / push2his 各自有多个边缘节点, 单 IP 限频通常只命中其一.

仅依赖 Python 标准库. 运行: python server.py [port]
"""
import json
import os
import sys
import time
import re
import codecs
import datetime
import threading
import subprocess
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DIR = os.path.dirname(os.path.abspath(__file__))
# 竞价观察名单(JSON): 由 astock-screen/gen_tomorrow.py 生成, 跨目录读取供 /api/bid_watch 使用
WATCH_JSON = os.path.join(DIR, "..", "astock-screen", "tomorrow_watch.json")
# 智能推荐引擎: 情绪周期 + 强度六维 + 量能公理(与 gen_tomorrow.py 共用同一套口径)
sys.path.insert(0, os.path.join(DIR, "..", "astock-screen"))
try:
    from reco_engine import (auction_judge, resolve_stage, volume_axiom, win_pick,
                             yz_risk, buy_zone, vol_veto, VOL_VETO_RATIO,
                             EXIT_RULE, EXIT_NOTE, EXIT_DESC)
except Exception:
    auction_judge = resolve_stage = volume_axiom = win_pick = yz_risk = buy_zone = None
    vol_veto = None
    VOL_VETO_RATIO = 3.5
    EXIT_RULE, EXIT_NOTE, EXIT_DESC = "T+1", "T+1收盘前卖", ""


def _re_attr(name, default):
    """动态读 reco_engine 的模块级常量。

    为什么不能直接用 `from reco_engine import VOL_VETO_RATIO` 的值：reco_engine 支持
    **策略文件热重载**（`ensure_strategy()` 会覆写模块全局），而 `from ... import` 只拿到
    导入那一刻的快照。`vol_veto()` 自身在调用时读模块全局（永远最新），但**展示用**的
    阈值若用导入快照，就会出现「门控按 4.0 判、界面写 3.5」的口径漂移。

    2026-09-23：MAX_BOARD / PREFER_FIRST_BOARD / GAP_MODE 同样走这里 ——
    改 strategy/*.json 即热生效，无需重启本服务。
    """
    m = sys.modules.get("reco_engine")
    return getattr(m, name, default) if m is not None else default

# 盘口/个股异动实时采集模块(与 quote.eastmoney.com/changes 同源)
#   职责: 交易时段后台按节拍采集异动明细流 -> 环形缓冲(seq 游标) -> /api/changes 增量输出
#   联动判定「创业板异动 + 同行业主板涨停」复用本服务已有的涨停池缓存(见 _zt_provider)
try:
    import em_changes
except Exception as _e:
    em_changes = None
    sys.stderr.write("[warn] em_changes 模块载入失败, 异动联动页将不可用: %s\n" % _e)

# 多 host 兜底: 东方财富各行情服务有多个边缘节点, 单 IP 限频通常只命中其一.
# (已实测 push2delay 在本机可用; push2ex/push2his 的 delay 节点通常也存在)
EM_HOSTS = [
    "push2.eastmoney.com",
    "push2delay.eastmoney.com",
    "82.push2.eastmoney.com",
]
POOL_HOSTS = [
    "push2ex.eastmoney.com",
    "push2exdelay.eastmoney.com",
]
HIS_HOSTS = [
    # 实测: push2delay 的 trends2 节点在本机可用且基本无限频, 必须置于最前才能修复分时 502
    "push2delay.eastmoney.com",
    "push2his.eastmoney.com",
    "push2hisdelay.eastmoney.com",
]
EM_PATH = "/api/qt/clist/get"
EM_UT = "fa5fd080d2bcf6b6751843e8c9c826a4"  # 东方财富行情接口公开令牌常量
POOL_UT = "7eea3edcaed734bea9cbfc24409ed989"  # 东方财富涨停板专题池专用令牌(akshare实测有效)

# 全市场 A股 fs (合并沪深主板+创业板+科创板), 一次拉全量即可覆盖涨跌停判定
#   深市主板 m:0+t:6 (00) / 创业板 m:0+t:80 (30) / 沪市主板 m:1+t:2 (60) / 科创板 m:1+t:23 (68)
#   注: 旧版 FS_SH_A="m:0+t:80" 实为创业板(30开头)而非沪市主板, 且遗漏深市主板与科创板,
#       深市探测 detect_sz_fs() 实际探测到的 m:1+t:2 才是沪市主板(60开头) —— 标签整体颠倒且覆盖不全。
#       现统一用 FS_ALL_A 合并 fs 一次拉全量, 不再分沪/深两次探测。
FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
# 板块: 行业 m:90+t:2, 概念 m:90+t:3 (f:!50 排除指数类)
FS_INDUSTRY = "m:90+t:2+f:!50"
FS_CONCEPT = "m:90+t:3+f:!50"

_STOCK_WARN = ""        # 最近一次个股抓取的部分缺失提示
_ALL_STOCKS_CACHE = {"ts": 0.0, "val": None}  # 全市场个股共享缓存(降级路径复用)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Connection": "close",
}

# ---------- 缓存层 (后台刷新 + 请求只读) ----------
_CACHE = {}                        # key -> (ts, payload)  个股/板块缓存
_CACHE_LOCK = threading.Lock()
_TRENDS_CACHE = {}                 # code -> (ts, payload) 分时短时缓存(点击触发)
_KLINE_CACHE = {}                  # (code, days) -> (ts, payload) 日K缓存(点击触发)
_REFRESH_NEXT = {"zt": 0.0, "dt": 0.0, "sectors": 0.0}   # 下次允许刷新时间戳
_REFRESH_INT = {"zt": 15, "dt": 15, "sectors": 30}       # 当前刷新间隔(失败翻倍)
_ZT_MAX_AGE = 60       # 涨停缓存超过此秒数视为 stale
_DT_MAX_AGE = 60       # 跌停缓存超过此秒数视为 stale
_SECTOR_MAX_AGE = 90   # 板块缓存超过此秒数视为 stale
_TRENDS_MAX_AGE = 60   # 分时缓存 TTL(点击触发, 盘中变化快但限频优先)
_KLINE_MAX_AGE = 300   # 日K缓存 TTL(盘中仅最后一根变动, 低频即可)


def get_cached(key, max_age):
    """返回 (ts, payload, fresh). 未命中返回 (None, None, False)."""
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return None, None, False
        ts, val = item
        return ts, val, (time.time() - ts) <= max_age


def set_cached(key, val):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), val)


def refresh_limit(kind):
    """刷新涨停(zt)/跌停(dt). push2ex 专题池优先, 失败降级 clist 筛选."""
    global _STOCK_WARN
    try:
        pool = fetch_pool(kind)
        stocks = [parse_pool_item(x) for x in pool]
        # rc=0 且 data 正常返回即可信; 空池表示当日确实无涨/跌停, 不再视为失败
        set_cached(kind, stocks)
        _STOCK_WARN = ""
        return True
    except Exception as e:
        sys.stderr.write("[warn] %s专题池抓取失败, 降级 clist: %s\n" % (kind, e))
        try:
            alls = get_all_stocks()
            lst = compute_limit_up(alls) if kind == "zt" else compute_limit_down(alls)
            set_cached(kind, lst)
            # 降级路径拿不到连板/封单等专题池专属字段, 明确告知前端显示 '--'
            _STOCK_WARN = ("上游涨停板专题池(push2ex)暂不可用, "
                           "连板天数/封单金额等字段显示为'-', 已用全市场涨跌停判定降级")
            return True
        except Exception as e2:
            sys.stderr.write("[warn] %s降级 clist 也失败: %s\n" % (kind, e2))
            return False


def refresh_sectors():
    try:
        sec = get_sectors()
        set_cached("sectors", sec)
        return True
    except Exception as e:
        sys.stderr.write("[warn] 板块刷新失败: %s\n" % e)
        return False


def _do_refresh(key):
    """执行一次刷新, 按成败更新退避间隔. 由后台线程串行调用, 不并发."""
    if key == "sectors":
        ok = refresh_sectors()
        base = 30
    else:
        ok = refresh_limit(key)
        base = 15
    now = time.time()
    if ok:
        _REFRESH_INT[key] = base
    else:
        _REFRESH_INT[key] = min(_REFRESH_INT[key] * 2, 120)
    _REFRESH_NEXT[key] = now + _REFRESH_INT[key]
    sys.stderr.write("[info] 刷新 %s %s, 下次间隔 %ds\n"
                     % (key, "成功" if ok else "失败", int(_REFRESH_INT[key])))


def background_refresh():
    """后台线程: 启动即预热, 之后每 5s 检查, 到点刷新(独立退避)."""
    for k in ("zt", "dt", "sectors"):
        _do_refresh(k)
    while True:
        time.sleep(5)
        now = time.time()
        for k in ("zt", "dt", "sectors"):
            if now >= _REFRESH_NEXT[k]:
                _do_refresh(k)


# ---------- 通用: 东方财富响应解析 (BOM + JSONP callback 剥离) ----------
def parse_em_json(raw):
    """东方财富部分接口返回 UTF-8 BOM 前缀的 JSONP(callback(...)).
    本函数去除 BOM、剥离外层 callback, 返回 Python dict. 失败抛异常."""
    if isinstance(raw, bytes):
        # 去 BOM: EF BB BF
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.strip()
    # JSONP: 可能是 "jQuery12345(...)" 或 "callback(...)"
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    elif "(" in text and text.endswith(")"):
        # 取第一个 ( 之后到最后一个 ) 之前的内容
        text = text[text.find("(") + 1:text.rfind(")")].strip()
    return json.loads(text)


# ---------- 抓取: push2 clist (全市场个股/板块, 多 host 兜底) ----------
def fetch_em(fs, fields, pz=6000, retries=0):
    """抓取东方财富 clist 接口, 返回 data.diff 列表; 失败抛异常.
    多 host 兜底: 依次尝试 EM_HOSTS, 命中即返回; rc!=0 或空 diff 也视为失败切下一节点."""
    params = {
        "pn": "1", "pz": str(pz), "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": fs, "fields": fields, "ut": EM_UT,
    }
    last_err = None
    for host in EM_HOSTS:
        url = "https://%s%s?%s" % (host, EM_PATH, urllib.parse.urlencode(params))
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            data = parse_em_json(raw)
            if data.get("rc") != 0:
                raise RuntimeError("东方财富返回异常 rc=%s" % data.get("rc"))
            diff = (data.get("data") or {}).get("diff") or []
            if not diff:
                raise RuntimeError("返回空列表(可能限频)")
            return diff
        except Exception as e:
            last_err = e
            continue
    raise last_err


# ---------- 交易日历: 所有「昨日 / 上一交易日」统计的唯一基准 ----------
# 用上证指数日K当作交易日历: 指数有日K的那天就是真实交易日,
# 周末与法定节假日会被自动剔除, 无需维护节假日表。
_TD_CACHE = {"ts": 0.0, "days": None}
_TD_TTL = 1800                      # 交易日历缓存 30min
_TD_REF = "sh000001"                # 上证指数 secid(腾讯格式)


def _td_norm(s):
    """'2026-09-11' / '20260911' -> '20260911'; 空值 -> ''."""
    s = str(s or "").strip()
    return s.replace("-", "").replace("/", "")[:8] if s else ""


def _td_label(day):
    """'20260911' -> '09-11'."""
    d = _td_norm(day)
    return ("%s-%s" % (d[4:6], d[6:8])) if len(d) == 8 else "—"


def fetch_trading_days(span=60):
    """拉最近 span 个真实交易日, 升序 ['20260810', ...]. 全部失败则抛异常.
    主源腾讯日K(实测稳定); 备源东财 push2his。
    注: 腾讯接口取 [beg,end] 区间内「最后 N 条」, 且 end 必须给今天(给明天会漏掉当天),
        故 N 取 span、beg 放宽到 2×span 天前。"""
    beg_d = datetime.date.today() - datetime.timedelta(days=span * 2)
    beg = beg_d.strftime("%Y-%m-%d")
    end = datetime.date.today().strftime("%Y-%m-%d")     # ★ 必须为今天, 否则当天日K被漏掉
    last_err = None
    try:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,%d,"
               % (_TD_REF, beg, end, max(20, span)))
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            j = json.loads(resp.read().decode("utf-8", "ignore"))
        node = ((j.get("data") or {}).get(_TD_REF) or {})
        rows = node.get("day") or node.get("qfqday") or []
        out = sorted({_td_norm(r[0]) for r in rows if r and r[0]})
        if out:
            return out
        last_err = RuntimeError("腾讯指数日K为空")
    except Exception as e:
        last_err = e
    for host in HIS_HOSTS:
        try:
            params = {"secid": "1.000001",
                      "fields1": "f1,f2,f3,f4,f5,f6",
                      "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                      "klt": "101", "fqt": "0",
                      "beg": _td_norm(beg), "end": _td_norm(end), "ut": EM_UT}
            url = "https://%s/api/qt/stock/kline/get?%s" % (host, urllib.parse.urlencode(params))
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                j = parse_em_json(resp.read())
            rows = ((j.get("data") or {}).get("klines") or [])
            out = sorted({_td_norm(str(r).split(",")[0]) for r in rows if r})
            if out:
                return out
            last_err = RuntimeError("东财指数日K为空")
        except Exception as e:
            last_err = e
            continue
    raise last_err


def trading_days():
    """交易日历(带缓存)。上游全失败时返回旧值/None, 由调用方降级。"""
    now = time.time()
    if _TD_CACHE["days"] and (now - _TD_CACHE["ts"]) <= _TD_TTL:
        return _TD_CACHE["days"]
    try:
        days = fetch_trading_days()
    except Exception as e:
        sys.stderr.write("[warn] 交易日历获取失败(降级为跳过周末): %s\n" % e)
        return _TD_CACHE["days"]
    _TD_CACHE["days"], _TD_CACHE["ts"] = days, now
    return days


def is_trading_day(day=None):
    """day: '2026-09-14'/'20260914'/date, 缺省今天。日历不可用时退化为「非周末」。
    注: 当天日K要等开盘后才生成, 因此「今天是否开市」请用 market_open_today()。"""
    d = _td_norm(day or time.strftime("%Y%m%d"))
    days = trading_days()
    if days:
        return d in set(days)
    try:
        return datetime.date(int(d[:4]), int(d[4:6]), int(d[6:8])).weekday() < 5
    except Exception:
        return False


_TODAY_OPEN_CACHE = {"day": None, "ok": None}


def market_open_today():
    """今日是否真的有盘口(权威判定, 按日缓存)。
    取上证指数当日分时: 其 data.date == 今天 即今日开市; 周末/节假日会停在上一个交易日。
    用于「定盘快照」护栏 —— 避免休市日把上一交易日的静态数据误定格成今日推荐。"""
    today = time.strftime("%Y%m%d")
    c = _TODAY_OPEN_CACHE
    if c["day"] == today and c["ok"] is not None:
        return c["ok"]
    ok = None
    try:
        url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=%s" % _TD_REF
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            j = json.loads(resp.read().decode("utf-8", "ignore"))
        node = ((j.get("data") or {}).get(_TD_REF) or {})
        dt = _td_norm((node.get("data") or {}).get("date"))
        if dt:
            ok = (dt == today)
    except Exception as e:
        sys.stderr.write("[warn] 今日盘口探测失败(降级工作日判断): %s\n" % e)
    if ok is None:
        try:
            ok = datetime.date.today().weekday() < 5
        except Exception:
            ok = False
    c["day"], c["ok"] = today, ok
    return ok


def last_trading_day(ref=None, include_ref=False):
    """最近一个交易日 'YYYYMMDD'.
    include_ref=False(默认): 严格早于 ref, 即「上一交易日」——周一/节后第一天
                             会自动回跳到上周五/节前最后一天。
    include_ref=True       : ref 本身是交易日则返回 ref, 否则向前找。"""
    r = _td_norm(ref or time.strftime("%Y%m%d"))
    days = trading_days()
    if days:
        cand = [x for x in days if (x <= r if include_ref else x < r)]
        if cand:
            return max(cand)
    try:
        base = datetime.date(int(r[:4]), int(r[4:6]), int(r[6:8]))
    except Exception:
        base = datetime.date.today()
    for i in range(0 if include_ref else 1, (0 if include_ref else 1) + 14):
        d = base - datetime.timedelta(days=i)
        if d.weekday() < 5:
            return d.strftime("%Y%m%d")
    return base.strftime("%Y%m%d")


def prev_trading_day():
    """「昨日」的实际日期 'YYYYMMDD' —— 全站昨日统计统一走这里。"""
    return last_trading_day()


# ---------- 抓取: push2ex 涨停/跌停专题池 ----------
_PREV_POOL_CACHE = {"ts": 0.0, "day": None, "zt": None, "dt": None}  # 昨日池缓存(按交易日键控)
_PREV_POOL_TTL = 300


def fetch_pool(kind, date=None):
    """抓取东方财富涨停板专题池. kind='zt' 涨停 / 'dt' 跌停. 返回 pool 列表.
    date 缺省为今天; 传 'YYYYMMDD' 可拉历史交易日(昨日涨停池用).
    参数对齐 akshare 实测有效组合: ut=POOL_UT, dpt=wz.ztzt, date=YYYYMMDD,
    pagesize(小写), sort=fbt:asc(涨停)/fund:asc(跌停)."""
    path = "/getTopicZTPool" if kind == "zt" else "/getTopicDTPool"
    today = date or time.strftime("%Y%m%d")
    params = {
        "ut": POOL_UT,
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "320" if kind == "zt" else "170",
        "sort": "fbt:asc" if kind == "zt" else "fund:asc",
        "date": today,
        "_": str(int(time.time() * 1000)),  # 缓存戳
    }
    last_err = None
    for host in POOL_HOSTS:
        url = "https://%s%s?%s" % (host, path, urllib.parse.urlencode(params))
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            j = parse_em_json(raw)
            if j.get("rc") != 0 or not j.get("data"):
                raise RuntimeError("专题池返回 rc=%s" % j.get("rc"))
            return j["data"].get("pool") or []
        except Exception as e:
            last_err = e
            continue
    raise last_err


def get_prev_pool(kind):
    """拉「上一交易日」涨停/跌停专题池(历史数据不变, 短缓存 TTL=300s). kind='zt'/'dt'.
    ★ 日期取真实交易日: 周一取上周五、节后取节前最后一天, 不再用「自然日昨天」。
    返回 parse_pool_item 归一化列表; 上一交易日无数据或上游失败时返回 []."""
    now = time.time()
    prev_day = prev_trading_day()
    if (_PREV_POOL_CACHE[kind] is not None
            and _PREV_POOL_CACHE.get("day") == prev_day
            and (now - _PREV_POOL_CACHE["ts"]) <= _PREV_POOL_TTL):
        return _PREV_POOL_CACHE[kind]
    try:
        pool = fetch_pool(kind, date=prev_day)
        stocks = [parse_pool_item(x) for x in pool]
    except Exception as e:
        sys.stderr.write("[warn] 上一交易日(%s)%s池抓取失败: %s\n" % (prev_day, kind, e))
        stocks = []
    _PREV_POOL_CACHE["ts"] = now
    _PREV_POOL_CACHE["day"] = prev_day
    _PREV_POOL_CACHE[kind] = stocks
    return stocks


def build_prev_limit_payload(kind):
    """上一交易日涨停/跌停池响应(结构同 build_stock_payload).
    ★ 日期为真实交易日(自动跳过周末/节假日), 通过 date/date_label 回传前端标注。"""
    key = "zt" if kind == "up" else "dt"
    stocks = get_prev_pool(key)
    main, gem = split_board(stocks)
    prev_day = prev_trading_day()
    return 200, {"ok": True,
                 "data": {"count": len(stocks), "main": main, "gemStar": gem,
                          "date": prev_day, "date_label": _td_label(prev_day)}}


# ---------- 抓取: 腾讯 smartbox 搜索 + 东方财富批量行情 (持仓模块用) ----------
def market_of(code):
    """代码 -> 东方财富 secid 市场位(1=沪 0=深). 6/9 开头沪市, 0/3 开头深市."""
    c = (code or '').strip()
    if not c:
        return 0
    return 1 if c[0] in ('6', '9') else 0


def fetch_search(q):
    """腾讯 smartbox 模糊搜索(代码/名称/拼音). 返回 [{code,name,market}].
    响应形如 v_hint="sz~000001~\\u5e73\\u5b89...~payh~GP-A^..." 非标准 JSON, 需解析."""
    q = (q or '').strip()
    if not q:
        return []
    url = "https://smartbox.gtimg.cn/s3/?t=all&q=%s" % urllib.parse.quote(q)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode('utf-8', 'ignore')
    m = re.search(r'v_hint="(.*)"', raw, re.S)
    if not m:
        return []
    out = []
    for seg in m.group(1).split('^'):
        p = seg.split('~')
        if len(p) < 5:
            continue
        market, code, name, _py, typ = p[0], p[1], p[2], p[3], p[4]
        if market not in ('sz', 'sh'):
            continue
        if not typ.upper().startswith('GP'):          # 仅 A股股票, 排除指数/基金/REIT
            continue
        try:
            name = codecs.decode(name, 'unicode_escape')
        except Exception:
            pass
        if code and name:
            out.append({"code": code, "name": name, "market": market})
        if len(out) >= 20:
            break
    return out


def fetch_quotes(codes):
    """批量实时行情(持仓盈亏 / 竞价裁决用). codes=['600519','000001'] -> 东方财富 ulist.np.
    返回 [{code,name,price,chg,change,amount,open}]. 盘前 f2=0 视为无实时价(price=None).
    open=f17 今日开盘价(9:25 集合竞价撮合价, 即当日定盘缺口基准), 未开盘时为 None。"""
    codes = [c.strip() for c in codes if c and c.strip()]
    if not codes:
        return []
    secids = ["%d.%s" % (market_of(c), c) for c in codes]
    params = {
        "fields": "f12,f13,f14,f2,f3,f4,f6,f17",
        "secids": ",".join(secids),
        "fltt": "2", "ut": EM_UT,
    }
    last_err = None
    for host in EM_HOSTS:
        url = "https://%s/api/qt/ulist.np/get?%s" % (host, urllib.parse.urlencode(params))
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            j = parse_em_json(raw)
            if j.get("rc") != 0 or not j.get("data"):
                raise RuntimeError("ulist rc=%s" % j.get("rc"))
            diff = j["data"].get("diff") or []
            res = []
            for x in diff:
                price = x.get("f2")
                price = price if isinstance(price, (int, float)) and price > 0 else None
                chg = x.get("f3")
                op = x.get("f17")
                res.append({
                    "code": str(x.get("f12") or ""),
                    "name": str(x.get("f14") or ""),
                    "price": price,
                    "chg": chg if isinstance(chg, (int, float)) else None,
                    "change": x.get("f4"),
                    "amount": x.get("f6"),
                    "open": op if isinstance(op, (int, float)) and op > 0 else None,
                })
            return res
        except Exception as e:
            last_err = e
            continue
    raise last_err


def _fbt_to_hhmm(fbt):
    """专题池 fbt/lbt 是整数 HHMMSS(如 92500=09:25:00), 转成距 00:00:00 的秒数,
    前端按秒数格式化为 HH:MM, 避免时区转换歧义."""
    try:
        v = int(fbt)
        if v <= 0:
            return None
        hrs = v // 10000
        mins = (v % 10000) // 100
        secs = v % 100
        return hrs * 3600 + mins * 60 + secs
    except Exception:
        return None


def parse_pool_item(it):
    """解析专题池单条 -> 统一结构(含连板/封板/封单/市值字段).
    新接口字段: c 代码, n 名称, p 最新价(毫), zdp 涨跌幅, amount 成交额(元),
    hs 换手率, lbc 连板数, fbt/lbt 首次/最后封板时间(秒), fund 封单金额(元),
    zbc 炸板次数, hybk 行业, zttj 涨停统计{days,ct}, ltsz 流通市值(元),
    tshare 总市值(元).
    ★ 市值(ltsz)必须透传: 封单额绝对值是弱因子(r≈0.21), 封单/流通比才是有效
      相对量纲因子(r≈0.32); 早期版本漏解析 ltsz 导致智能推荐排序只能用绝对值。"""
    code = str(it.get("c") or it.get("t") or it.get("f12") or "")
    name = str(it.get("n") or it.get("f14") or "")
    # 价格 p 接口返回毫(0.001元); amount/fund 已是元
    price_raw = it.get("p")
    price = price_raw / 1000.0 if isinstance(price_raw, (int, float)) else None
    chg = it.get("zdp")
    lbc = it.get("lbc")
    fbt_sec = it.get("fbt")
    lbt_sec = it.get("lbt")
    fbt_ts = _fbt_to_hhmm(fbt_sec)
    zbc = it.get("zbc")
    # 涨停统计 zttj = {"days": N, "ct": M}  ->  N天M板
    zttj = it.get("zttj")
    zt_days = zt_ct = None
    if isinstance(zttj, dict):
        zt_days = zttj.get("days")
        zt_ct = zttj.get("ct")
    # 流通市值/总市值(元): 封单/流通比 等相对量纲因子的分母
    ltsz = it.get("ltsz")
    ltsz = float(ltsz) if isinstance(ltsz, (int, float)) and ltsz > 0 else None
    tshare = it.get("tshare")
    tshare = float(tshare) if isinstance(tshare, (int, float)) and tshare > 0 else None
    # 封单强度 = 封单金额 / 流通市值(百分比); 无量纲, 可跨票跨日横向比较
    camount = it.get("fund") or it.get("camount")
    seal_ratio = None
    if isinstance(camount, (int, float)) and camount > 0 and ltsz:
        seal_ratio = camount / ltsz * 100.0
    # ★ 封板形态(两个口径必须分开, 早期把二者混为一谈):
    #   sealed_solid = 封板后全天未开板(fbt==lbt 且 zbc==0). 注意这包含「10:35 才封板
    #     然后死守到收盘」的票, 它并不是一字板, 开盘时完全买得进。
    #   yizi = 真一字板(fbt<=09:30:00 且 zbc==0), 开盘即以涨停价封死, 竞价买不进。
    # 实测 548 个样本: 旧口径判出 239 只「一字板」, 其中 201 只(84%)实为盘中封板死守,
    # 被错误标注「一字加速·买不进」并倒扣 3 分 -> 推荐页出现明显错标。
    sealed_solid = False
    if zbc == 0 and fbt_sec is not None and lbt_sec is not None and fbt_sec == lbt_sec:
        sealed_solid = True
    # znh 字段保留原口径仅供兼容, 新代码请用 yizi / sealed_solid
    znh = sealed_solid
    yizi = False
    try:
        if zbc == 0 and fbt_sec is not None and int(fbt_sec) <= 93000:
            yizi = True
    except Exception:
        yizi = False
    # 炸板: 炸板次数>0
    zs = False
    if isinstance(zbc, int) and zbc > 0:
        zs = True
    return {
        "code": code,
        "name": name,
        "price": price,
        "chg": chg,                       # 涨跌幅(%)
        "amount": it.get("amount") or it.get("f6"),      # 成交额(元)
        "turnover": it.get("hs") or it.get("wb"),        # 换手率(%)
        "main_net": it.get("f62") or it.get("main_net"),  # 主力净流入(元) 可能缺失
        "limit": limit_pct(code, name),
        "board": board_of(code, name),
        "lbc": lbc,                       # 连板天数(整数, 首板=1)
        "fbt": fbt_ts,                    # 首次封板时间戳(Unix秒, 形如 "09:25")
        "fbt_raw": fbt_sec,               # 首次封板原始秒数(排序用, 越小越早封)
        "lbt_raw": lbt_sec,               # 最后封板原始秒数(与 fbt_raw 对称; 等值=封后未开板)
        "camount": camount,               # 封单金额(元)
        "cnum": it.get("cnum"),           # 封单量(新接口无此字段, 保留兼容)
        "ltsz": ltsz,                     # 流通市值(元)
        "tshare": tshare,                 # 总市值(元)
        "seal_ratio": seal_ratio,         # 封单/流通市值(%)  无量纲相对因子
        "yizi": yizi,                     # 真一字板(09:30 前封死, 竞价买不进)
        "sealed_solid": sealed_solid,     # 封板后全天未开板(含盘中封板后死守)
        "zt_days": zt_days,               # 涨停统计: N天
        "zt_ct": zt_ct,                   # 涨停统计: M板
        "znh": znh,                       # 【兼容别名】== sealed_solid, 非真一字板
        "zs": zs,                         # 是否炸板
        "zbc": zbc,                       # 炸板次数(原始)
        "hybk": it.get("hybk") or "",     # 行业
    }


# ---------- 抓取: push2his 当日分时 ----------
def secid_of(code):
    """代码 -> 东方财富 secid(市场.代码). 沪市60/68->1, 其余->0."""
    code = str(code)
    if code.startswith("60") or code.startswith("68"):
        return "1." + code
    return "0." + code


def fetch_his(secid):
    """抓取当日分时 trends2. 多 host 兜底."""
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "iscr": "0", "ndays": "1", "forcect": "1", "ut": EM_UT,
    }
    last_err = None
    for host in HIS_HOSTS:
        url = "https://%s%s?%s" % (host, "/api/qt/stock/trends2/get", urllib.parse.urlencode(params))
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            return parse_em_json(raw)
        except Exception as e:
            last_err = e
            continue
    raise last_err


def _to_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _lookup_cached_stock(code):
    """从涨停/跌停缓存中查个股, 用于分时接口补充名称/昨收. 找不到返回 None."""
    code = str(code)
    for key in ("zt", "dt"):
        with _CACHE_LOCK:
            item = _CACHE.get(key)
        if not item:
            continue
        for s in item[1]:
            if s.get("code") == code:
                return s
    return None


def get_trends(code):
    """返回当日分时 {code,name,preClose,points:[{t,price,avg}]}. 短时缓存抗限频.
    注: push2delay 的 trends2 响应不含 name 与 preClose(preSettlement 恒为 0.0),
    故 name 取自涨停/跌停缓存, preClose 由 现价/涨幅 反推(仅供参考线)."""
    with _CACHE_LOCK:
        item = _TRENDS_CACHE.get(code)
        if item and (time.time() - item[0]) <= _TRENDS_MAX_AGE:
            return item[1]
    data = fetch_his(secid_of(code))
    d = (data.get("data") or {})
    pre = _to_float(d.get("preSettlement") or d.get("preClose"))
    info = _lookup_cached_stock(code)
    name = d.get("name") or (info.get("name") if info else None)
    # 昨收反推: 现价/(1+涨幅%), 作为分时图昨收参考线; 比上游恒为0的 preSettlement 更准确
    if (pre is None or pre == 0.0) and info and info.get("price") and info.get("chg") is not None:
        try:
            pre = info["price"] / (1.0 + info["chg"] / 100.0)
        except Exception:
            pre = None
    pts = []
    for row in (d.get("trends") or []):
        parts = str(row).split(",")
        if len(parts) < 8:
            continue
        price = _to_float(parts[2])
        avg = _to_float(parts[7])
        if price is None:
            continue
        # f56=成交量(手) 供分时量柱; f52=该分钟开盘, 用于量柱红绿着色。
        # 注: 09:30 首根含集合竞价撮合量。腾讯列序 [时间,开,收,高,低,量,额,均价]
        pts.append({"t": parts[0], "price": price, "avg": avg,
                    "o": _to_float(parts[1]), "vol": _to_float(parts[5])})
    result = {"ok": True, "code": code, "name": name,
              "preClose": pre, "points": pts}
    with _CACHE_LOCK:
        _TRENDS_CACHE[code] = (time.time(), result)
    return result


# ---------- 日K线 (点击触发, 腾讯主源 + 东财兜底) ----------
def tx_symbol(code):
    """代码 -> 腾讯行情 symbol. 60/68->sh, 8/4->bj, 其余->sz."""
    code = str(code)
    if code.startswith("60") or code.startswith("68"):
        return "sh" + code
    if code[:1] in ("8", "4"):
        return "bj" + code
    return "sz" + code


def fetch_daily_kline_tx(code, days):
    """腾讯日K(不复权) -> (name, [{d,o,c,h,l,v}] 升序).
    注: 接口 end 参数必须给「今天」——给明天会返回最后一个「已收盘」交易日而漏掉当日K线;
    不复权原始价更贴近真实成交/涨停价判定, 与 backfill_review.py 口径一致。"""
    sym = tx_symbol(code)
    today = datetime.date.today()
    beg = (today - datetime.timedelta(days=max(int(days) * 2, 60))).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    cnt = max(int(days) + 10, 30)
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,%d,"
           % (sym, beg, end, cnt))
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        j = json.loads(resp.read().decode("utf-8", "ignore"))
    node = ((j.get("data") or {}).get(sym) or {})
    rows = node.get("day") or node.get("qfqday") or []
    qtv = ((node.get("qt") or {}).get(sym) or [])
    name = qtv[1] if len(qtv) > 1 and qtv[1] else None
    bars = []
    for r in rows:
        if not r or len(r) < 6:
            continue
        o, c, h, l, v = (_to_float(r[1]), _to_float(r[2]), _to_float(r[3]),
                         _to_float(r[4]), _to_float(r[5]))
        if None in (o, c, h, l):
            continue
        # 腾讯列序: [日期, 开, 收, 高, 低, 成交量(手)]
        bars.append({"d": str(r[0])[:10], "o": o, "c": c, "h": h, "l": l, "v": v or 0.0,
                     "amt": None})
    bars.sort(key=lambda x: x["d"])
    return name, bars


def fetch_daily_kline_em(code, days):
    """东财 push2his 日K(兜底). 实测连续批量请求会被断连, 故仅作备源。"""
    params = {
        "secid": secid_of(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": "0",
        "beg": "0", "end": "20500101",
        "lmt": str(max(int(days) + 10, 30)), "ut": EM_UT,
    }
    last_err = None
    for host in HIS_HOSTS:
        try:
            url = "https://%s/api/qt/stock/kline/get?%s" % (host, urllib.parse.urlencode(params))
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                j = parse_em_json(resp.read())
            d = (j.get("data") or {})
            bars = []
            for row in (d.get("klines") or []):
                p = str(row).split(",")
                if len(p) < 6:
                    continue
                bars.append({"d": p[0], "o": _to_float(p[1]), "c": _to_float(p[2]),
                             "h": _to_float(p[3]), "l": _to_float(p[4]),
                             "v": _to_float(p[5]) or 0.0, "amt": _to_float(p[6])})
            bars = [b for b in bars if None not in (b["o"], b["c"], b["h"], b["l"])]
            bars.sort(key=lambda x: x["d"])
            if bars:
                return d.get("name"), bars
            last_err = RuntimeError("东财日K返回为空")
        except Exception as e:
            last_err = e
            continue
    raise last_err


def get_kline(code, days=60):
    """返回日K {ok,code,name,days,preClose,bars:[{d,o,c,h,l,v,amt,chg}]}.
    腾讯主源, 东财兜底; 末根为当日盘中实时(未收盘)。"""
    code = str(code)
    try:
        days = int(days)
    except Exception:
        days = 60
    days = max(5, min(days, 250))
    key = (code, days)
    with _CACHE_LOCK:
        item = _KLINE_CACHE.get(key)
        if item and (time.time() - item[0]) <= _KLINE_MAX_AGE:
            return item[1]
    name, bars = None, []
    try:
        name, bars = fetch_daily_kline_tx(code, days)
    except Exception as e:
        sys.stderr.write("[warn] 腾讯日K失败(%s): %s\n" % (code, e))
    if not bars:                                   # 主源为空才切备源, 避免无谓触发东财限频
        name2, bars = fetch_daily_kline_em(code, days)
        name = name or name2
    bars = bars[-days:]
    if not name:
        info = _lookup_cached_stock(code)
        name = info.get("name") if info else None
    for i, b in enumerate(bars):                   # 涨跌幅按前一根收盘计算
        prev = bars[i - 1]["c"] if i > 0 else None
        b["chg"] = round((b["c"] - prev) / prev * 100.0, 2) if prev else None
    result = {"ok": True, "code": code, "name": name, "days": len(bars),
              "preClose": (bars[-2]["c"] if len(bars) >= 2 else None),
              "bars": bars}
    with _CACHE_LOCK:
        _KLINE_CACHE[key] = (time.time(), result)
    return result


# ---------- 涨跌停判定 / 板块分类 ----------
def limit_pct(code, name):
    """根据代码/名称返回涨跌停幅度(百分数, 与 f3 单位一致)."""
    code = str(code)
    name_u = str(name).upper()
    if code[:1] in ("8", "4"):      # 北交所 30%
        return 30.0
    if code.startswith("30") or code.startswith("68"):  # 创业板/科创板 20%
        return 20.0
    if "ST" in name_u:              # ST 5%
        return 5.0
    return 10.0                     # 主板 10%


def board_of(code, name):
    """板块分类(用于分组展示), 与 ST 标记相互独立."""
    code = str(code)
    if code.startswith("60") or code.startswith("00"):
        return "主板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith("68"):
        return "科创板"
    if code[:1] in ("8", "4"):
        return "北交所"
    return "其他"


def split_board(lst):
    """按板块拆分: 沪深主板(main) / 创业板·科创板(+北交所, gemStar). 不遗漏任何个股."""
    main, gem = [], []
    for s in lst:
        if s.get("board") == "主板":
            main.append(s)
        else:
            gem.append(s)
    return main, gem


def to_f(v):
    """任意值 -> float, 失败返回 None.
    上游 clist 在部分节点(限频/降级)可能返回字符串格式(如 "2.35"), 必须强转."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_stock(it):
    """clist 降级路径: 解析全市场个股, 连板/封单等字段置 None."""
    code = str(it.get("f12", ""))
    name = str(it.get("f14", ""))
    return {
        "code": code,
        "name": name,
        "price": to_f(it.get("f2")),
        "chg": to_f(it.get("f3")),
        "amount": to_f(it.get("f6")),
        "turnover": to_f(it.get("f8")),
        "main_net": to_f(it.get("f62")),
        "limit": limit_pct(code, name),
        "board": board_of(code, name),
        "lbc": None, "fbt": None, "camount": None, "cnum": None,
        "znh": None, "zs": None,
    }


def get_all_stocks():
    """clist 降级路径: 用合并全市场 fs(FS_ALL_A)一次拉全量 A股并归一化.
    返回全部个股(含涨跌停), 由 compute_limit_up/down 过滤. 一次查询即覆盖沪深主板+创业板+科创板,
    不再分沪/深两次探测(旧方案 FS_SH_A 定义颠倒且覆盖不全).
    内置 15s 共享缓存: zt/dt 两次刷新在同一周期内复用同一份全量数据, 避免重复拉取."""
    fields = "f12,f14,f2,f3,f6,f8,f62"
    now = time.time()
    if _ALL_STOCKS_CACHE["val"] is not None and (now - _ALL_STOCKS_CACHE["ts"]) <= 15:
        return _ALL_STOCKS_CACHE["val"]
    try:
        diff = fetch_em(FS_ALL_A, fields, pz=6000)
    except Exception as e:
        raise RuntimeError("全市场个股抓取失败: %s" % e)
    stocks = [normalize_stock(x) for x in diff]
    if not stocks:
        raise RuntimeError("个股数据为空(接口返回异常)")
    _ALL_STOCKS_CACHE["ts"] = now
    _ALL_STOCKS_CACHE["val"] = stocks
    return stocks


def compute_limit_up(stocks):
    res = [s for s in stocks
           if isinstance(s["chg"], (int, float)) and s["chg"] >= s["limit"] - 0.1]
    res.sort(key=lambda x: (x["chg"] if isinstance(x["chg"], (int, float)) else 0), reverse=True)
    return res


def compute_limit_down(stocks):
    res = [s for s in stocks
           if isinstance(s["chg"], (int, float)) and s["chg"] <= -s["limit"] + 0.1]
    res.sort(key=lambda x: (x["chg"] if isinstance(x["chg"], (int, float)) else 0))
    return res


def sector_norm(it):
    return {
        "code": str(it.get("f12", "")),
        "name": str(it.get("f14", "")),
        "index": to_f(it.get("f2")),
        "chg": to_f(it.get("f3")),
        "main_net": to_f(it.get("f62")),
        "main_in": to_f(it.get("f66")),
        "main_out": to_f(it.get("f72")),
    }


def top_flow(lst, n=15):
    valid = [x for x in lst if x["main_net"] is not None]
    inflow = sorted(valid, key=lambda y: y["main_net"], reverse=True)[:n]
    outflow = sorted(valid, key=lambda y: y["main_net"])[:n]
    return inflow, outflow


def get_sectors():
    fields = "f12,f14,f2,f3,f62,f66,f72"
    indu, conc = [], []
    try:
        indu = [sector_norm(x) for x in fetch_em(FS_INDUSTRY, fields, pz=500)]
    except Exception as e:
        sys.stderr.write("[warn] 行业板抓取失败: %s\n" % e)
    try:
        conc = [sector_norm(x) for x in fetch_em(FS_CONCEPT, fields, pz=500)]
    except Exception as e:
        sys.stderr.write("[warn] 概念板抓取失败: %s\n" % e)
    if not indu and not conc:
        raise RuntimeError("板块数据抓取失败：东方财富接口不可达或被限频，请稍后重试")
    i_in, i_out = top_flow(indu)
    c_in, c_out = top_flow(conc)
    return {
        "industry": {"inflow": i_in, "outflow": i_out, "total": len(indu)},
        "concept": {"inflow": c_in, "outflow": c_out, "total": len(conc)},
    }


# ---------- HTTP 响应构建 (只读缓存, 不穿透) ----------
def _score_stock(s, hy_cnt):
    """按《交易逻辑》量化打分. 得分越高越符合「低位连板+充分换手+强板块+大封单」模式.
    核心规则映射:
      - 「板块分歧后补涨1进2板封死是买点, 2-3成功格局不动, 3-4分歧卖掉」→ 2-3 连板黄金位
      - 「高标死于一致, 充分分歧才能活」→ 一字/加速减分, 充分换手(5-20%)加分
      - 「看板块最大封单」「板块上8家涨停先上车」→ 大封单/同行业涨停数加分
      - 「不追高, 只低吸」「加速一律止盈」→ 高位连板(>=5)/一字板/炸板回避
    返回 (score, reasons)."""
    sc = 0
    reasons = []
    lbc = s.get("lbc")
    lbc = int(lbc) if isinstance(lbc, (int, float)) else 1
    hs = s.get("turnover")
    hs = float(hs) if isinstance(hs, (int, float)) else None
    fund = s.get("camount")
    fund = float(fund) if isinstance(fund, (int, float)) else None
    zbc = s.get("zbc")
    zbc = int(zbc) if isinstance(zbc, (int, float)) else 0
    znh = bool(s.get("znh"))
    zs = bool(s.get("zs"))
    hy = s.get("hybk") or ""
    hy_n = hy_cnt.get(hy, 0)

    # 1) 连板位置
    if lbc in (2, 3):
        sc += 3
        reasons.append("%d连板·分歧低吸位" % lbc)
    elif lbc == 1:
        sc += 2
        reasons.append("首板·低位启动")
    elif lbc == 4:
        sc += 1
        reasons.append("4连板·高位谨慎")
    else:
        sc -= 3
        reasons.append("%d连板·高位一致回避" % lbc)

    # 2) 封板形态扣分(合计仍是 -3, 与历史行为一致; 仅把「一字板」与「盘中封板后
    #    全天未开板」两个完全不同的形态分开标注, 避免出现「10:35 才封板却标注
    #    一字加速·买不进」的错标)
    yizi = bool(s.get("yizi"))
    sealed_solid = bool(s.get("sealed_solid")) or znh
    if yizi:
        sc -= 3
        reasons.append("一字板·竞价封死买不进")
    elif sealed_solid:
        sc -= 3
        reasons.append("封板后全天未开板·一致性过强")

    # 3) 炸板
    if zs and zbc >= 2:
        sc -= 2
        reasons.append("炸板%d次·分歧过大" % zbc)
    elif zs:
        sc -= 1
        reasons.append("炸板%d次" % zbc)

    # 4) 换手
    if hs is not None:
        if 5 <= hs <= 20:
            sc += 2
            reasons.append("换手%.1f%%·充分" % hs)
        elif hs > 30:
            sc -= 1
            reasons.append("换手%.1f%%·过热" % hs)
        elif hs < 2:
            sc -= 1
            reasons.append("换手%.1f%%·缩量" % hs)

    # 5) 封单 -- 绝对值分档(历史口径, 未改), 但把量纲可比的「封单/流通比」一并
    #    标注出来: 同样是 3 亿封单, 流通 40 亿的票(7.5%) 比流通 400 亿的票(0.75%)
    #    强度高一个数量级, 仅看绝对额会系统性偏向大盘股。
    sr = s.get("seal_ratio")
    if fund is not None and fund > 0:
        yi = fund / 1e8
        tail = "%.2f亿(占流通%.2f%%)" % (yi, sr) if sr else "%.1f亿" % yi
        if yi >= 3:
            sc += 2
            reasons.append("封单%s" % tail)
        elif yi >= 1:
            sc += 1
            reasons.append("封单%s" % tail)

    # 6) 板块效应
    if hy_n >= 5:
        sc += 3
        reasons.append("%s %d家涨停" % (hy, hy_n))
    elif hy_n >= 2:
        sc += 1
        reasons.append("%s %d家涨停" % (hy, hy_n))

    return sc, reasons


def _rank_key(rank):
    """排序键工厂. rank=None 为现行生产口径(行为与历史完全一致).
    影子口径用于「同一天同一批票, 只换 tiebreaker」的对照实验, 不改变默认行为.

    背景: 现行 tiebreaker = 封单额绝对值, 但它是弱因子(实测 r≈0.21); 封单/流通比
    是更强的相对量纲因子(r≈0.32). 9 个复盘日影子对比(见 astock-screen/
    _shadow_tiebreak.py)显示: 现行 top5「可买晋级率」16.3%, 低于全池基准 21.6%;
    改用封单/流通比可显著抬升, 但样本仅 45 只(p≈0.07, 未达显著), 故默认不切换,
    先以影子模式并行观察."""
    if rank == "ratio":
        # 保留打分, 仅把 tiebreaker 从「封单额」换成「封单/流通比」
        return lambda x: (x[0], x[1].get("seal_ratio") or 0, x[1].get("camount") or 0)
    if rank == "seal":
        # 弃用打分, 纯封单/流通比; 剔除真一字板(买不进)与 5 板以上高位
        def _seal(x):
            s = x[1]
            if s.get("yizi") or (s.get("lbc") or 1) >= 5:
                return (-1.0, 0.0)
            return (s.get("seal_ratio") or 0, 0.0)
        return _seal
    # 现行生产口径: (评分, 封单额绝对值)
    return lambda x: (x[0], x[1].get("camount") or 0)


def score_pool(stocks, n=5, rank=None):
    """对任意股票池(今日/昨日)统一评分排序, 返回 top n items(含 score/reasons).
    rank 为 None 时使用现行生产排序口径; 传入影子口径仅用于对照观察."""
    hy_cnt = {}
    for s in stocks:
        k = s.get("hybk") or "其他"
        hy_cnt[k] = hy_cnt.get(k, 0) + 1
    scored = []
    for s in stocks:
        sc, reasons = _score_stock(s, hy_cnt)
        scored.append((sc, s, reasons))
    scored.sort(key=_rank_key(rank), reverse=True)
    items = []
    for sc, s, reasons in scored[:n]:
        items.append({
            "code": s["code"], "name": s["name"], "price": s["price"],
            "chg": s["chg"], "lbc": s["lbc"], "fbt": s["fbt"],
            "camount": s["camount"], "turnover": s["turnover"],
            "hybk": s.get("hybk") or "", "board": s.get("board"),
            "score": sc, "reasons": reasons,
            # 以下为量纲可比因子, 供前端展示封单强度/市值(不参与默认排序)
            "ltsz": s.get("ltsz"), "seal_ratio": s.get("seal_ratio"),
            "yizi": bool(s.get("yizi")), "sealed_solid": bool(s.get("sealed_solid")),
        })
    return items


def build_recommend_payload(prev=False, rank=None):
    """按《交易逻辑》从涨停池选推荐 5 只(分歧低吸观察池, 非追涨).
    prev=True 时额外返回昨日推荐及其今日表现(对比用).
    rank: None=现行生产排序; "ratio"/"seal" 为影子对照口径(不改变默认行为)."""
    _NOTE = "按《交易逻辑》量化：低位连板(2-3板)+充分换手+强板块+大封单优先；一字加速/高位一致/炸板回避。候选为分歧低吸观察池，非追涨建议。"
    if prev:
        # ---- 对比模式: 今日推荐 + 昨日推荐 + 昨日推荐今日表现 ----
        ts, stocks, fresh = get_cached("zt", _ZT_MAX_AGE)
        today_items = score_pool(stocks or [], 5, rank) if stocks else []
        y_stocks = get_prev_pool("zt")
        y_items = score_pool(y_stocks, 5, rank)
        prev_day = prev_trading_day()
        perf = {}
        codes = [x["code"] for x in y_items]
        if codes:
            try:
                for q in fetch_quotes(codes):
                    perf[q["code"]] = {"price": q["price"], "chg": q["chg"]}
            except Exception as e:
                sys.stderr.write("[warn] 昨日推荐行情拉取失败: %s\n" % e)
        return 200, {"ok": True, "updated": int(ts), "stale": (not fresh),
                     "data": {
                         "today": {"count": len(today_items), "items": today_items,
                                   "total": len(stocks or [])},
                         "yesterday": {"count": len(y_items), "items": y_items,
                                       "total": len(y_stocks), "perf": perf,
                                       "date": prev_day, "date_label": _td_label(prev_day)},
                         "note": _NOTE, "rank": (rank or "camount")}}
    # ---- 默认: 今日推荐 ----
    ts, stocks, fresh = get_cached("zt", _ZT_MAX_AGE)
    if stocks is None:
        return 502, {"ok": False, "error": "数据尚未就绪（首次抓取中或接口持续不可达）"}
    items = score_pool(stocks, 5, rank)
    return 200, {"ok": True, "updated": int(ts), "stale": (not fresh),
                 "data": {"count": len(items), "items": items,
                          "total": len(stocks), "note": _NOTE,
                          "rank": (rank or "camount")}}


def build_stock_payload(kind):
    key = "zt" if kind == "up" else "dt"
    max_age = _ZT_MAX_AGE if kind == "up" else _DT_MAX_AGE
    ts, stocks, fresh = get_cached(key, max_age)
    if stocks is None:
        return 502, {"ok": False,
                     "error": "数据尚未就绪（首次抓取中或接口持续不可达），请稍后重试"}
    lst = stocks
    main, gem = split_board(lst)
    return 200, {"ok": True, "updated": int(ts), "stale": (not fresh),
                 "warn": _STOCK_WARN,
                 "data": {"count": len(lst), "main": main, "gemStar": gem}}


def build_sector_payload():
    ts, sec, fresh = get_cached("sectors", _SECTOR_MAX_AGE)
    if sec is None:
        return 502, {"ok": False, "error": "板块数据尚未就绪（首次抓取中或接口持续不可达）"}
    return 200, {"ok": True, "updated": int(ts), "stale": (not fresh), "data": sec}


# ---------- 盘口异动实时流 (em_changes 采集器) ----------
CHANGES = None          # em_changes.Collector 实例; 未启用/启动失败时为 None


def _zt_provider():
    """给异动采集器供料: 直接复用「涨停个股」页的涨停池缓存(含 board/hybk/lbc).
    好处: ① 联动判定与涨停页同源一致; ② 不额外请求上游, 零限频风险."""
    _ts, stocks, _fresh = get_cached("zt", _ZT_MAX_AGE)
    return stocks or []


def start_changes_collector():
    """启动异动采集后台线程(幂等). 采集/缓冲/落盘全部在模块内完成, 此处只注入数据源."""
    global CHANGES
    if em_changes is None or CHANGES is not None:
        return CHANGES
    try:
        CHANGES = em_changes.Collector(zt_provider=_zt_provider, tick=2.0,
                                       store_file=os.path.join(DIR, "_changes_buffer.json"))
        CHANGES.start()
        sys.stderr.write("[info] 异动采集器已启动: %s\n" % CHANGES.snapshot(0, 1)["meta"]["phase_desc"])
    except Exception as e:
        sys.stderr.write("[warn] 异动采集器启动失败: %s\n" % e)
        CHANGES = None
    return CHANGES


def build_changes_payload(query):
    """异动流增量响应. query: since(游标) / limit / date(YYYYMMDD, 默认最新有数据日期)."""
    if CHANGES is None:
        return 503, {"ok": False, "error": "异动采集器未启动（em_changes 模块不可用）"}
    since = 0
    limit = 200
    try:
        if query.get("since"):
            since = max(0, int(query["since"][0]))
        if query.get("limit"):
            limit = min(3000, max(10, int(query["limit"][0])))
    except (ValueError, TypeError):
        pass
    date = (query.get("date") or [""])[0] or None
    try:
        return 200, CHANGES.snapshot(since=since, limit=limit, date=date)
    except Exception as e:
        return 500, {"ok": False, "error": "异动流读取失败：%s" % e}


# ---------- 竞价观察名单 + 实时买入提示 ----------
def market_phase(base_date):
    """判定当前时段, 决定竞价信号是否有效.

    关键: 名单基准日==今天 时, 名单是为"下一交易日"准备的, 当天实时价就是名单里的
    基准价(涨停收盘价), 高开幅度恒为 0; 此时绝不能给买入提示。
    返回 (phase, desc)。
    """
    now = time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    hm = now.tm_hour * 100 + now.tm_min
    if base_date and base_date >= today:
        return ("pre", "名单基于今日收盘涨停, 待下一交易日 9:15 集合竞价后生效")
    if hm < 915:
        return ("pre_open", "开盘前, 9:15 集合竞价开始后显示实时高开幅度")
    if 915 <= hm < 925:
        return ("auction", "集合竞价进行中(9:15-9:25), 信号有效")
    if 925 <= hm <= 1500:
        return ("open", "盘中交易时段, 信号按实时价相对昨收计算")
    return ("closed", "已收盘, 信号仅作当日复盘参考")


def auction_signal(cls, board, gap, phase):
    """根据「相对名单基准价(上一交易日收盘)的高开幅度 gap」给出信号.

    gap: 实时价 vs 名单基准价 的偏离(%), 竞价时段即为竞价高开幅度。
         注意不能用实时涨跌幅 chg —— 那是相对昨收的, 涨停收盘后恒为 +10%,
         会把名单里所有票误标为"可买"。
    返回 (signal, action, color, buy)。
      color: 红(#f23645)=可买/持有, 绿(#2bbf6a)=回避/卖出, 黄(#f59e0b)=观察, 灰=未生效。
    """
    if phase in ("pre", "pre_open"):
        return ("待竞价", "名单已就绪; 明早 9:15 集合竞价开始后, 此处按实时高开幅度给买入提示",
                "#7f8c8d", False)
    if cls == "C":
        return ("锚·只看不做", "高位核心锚, 盈亏比已差, 仅作情绪高度参照; 爆量开板=板块退潮信号",
                "#7f8c8d", False)
    if gap is None:
        return ("无行情", "未取到实时价, 请刷新或检查行情接口", "#7f8c8d", False)
    try:
        b = int(board) if board else 2
    except Exception:
        b = 2
    if cls == "A":
        thr = {4: 4, 3: 3, 2: 2}.get(b, 2)
        if gap >= thr:
            return ("弱转强·可买", "高开%+.1f%%(≥%d%%阈值), 换手回封可打/持有看高; 破板3分钟不回封即走"
                    % (gap, thr), "#f23645", True)
        if gap >= -1:
            return ("观察", "高开%+.1f%%平开附近, 持有观察不追; 等放量方向确认" % gap, "#f59e0b", False)
        return ("弱/退潮", "低开%+.1f%%, 强转弱竞价减仓/清; 破昨收无条件走" % gap, "#2bbf6a", False)
    # B 首板精选
    if gap >= 2:
        return ("超预期·可买", "高开%+.1f%%(≥2%%)超预期, 可格局; 冲高不板兑现" % gap, "#f23645", True)
    if gap < 0:
        return ("不及预期", "低开%+.1f%%, 开盘走; 不恋战" % gap, "#2bbf6a", False)
    return ("观察", "高开%+.1f%%平开附近, 仅板块≥3只涨停共振时参与" % gap, "#f59e0b", False)


# ---------- 竞价定盘快照: 9:25-9:30 的推荐「定格」并全天保留 ----------
# 背景: 9:25 集合竞价定价确定的那一刻产生的 TOP3 是当日最有价值的决策依据。
#       它必须定格并全天留在界面上, 不能被盘中的实时刷新冲掉;
#       而 9:30 开盘后的实时精选另起一区, 每轮刷新。
# 规则: 每交易日只定格一次(幂等落盘 _bid_picks_<YYYYMMDD>.json);
#       9:25-9:30 为正式定盘窗口; 若页面当时未打开/服务重启, 则窗口内自动补定并标注实际时刻。
BID_FREEZE_KEEP = 15                       # 保留最近 N 个交易日的定盘快照
_FREEZE_LOCK = threading.Lock()
_FREEZE_MODES = {
    "auction":      "9:25 竞价定盘",
    "auction_late": "开盘后补定",
    "intraday":     "盘中补定",
}
_FREEZE_BASIS = {
    "open":  "今日开盘缺口(9:25 集合竞价撮合价)",
    "live":  "竞价实时价格",
    "mixed": "开盘缺口/实时价 混合",
}


def _freeze_file(day):
    return os.path.join(DIR, "_bid_picks_%s.json" % _td_norm(day))


def freeze_eligible(hm=None):
    """【后端落盘口径】当前是否允许定盘落盘; 返回 mode 或 None。
    auction      : 9:25-9:30 竞价定价已确定 -> 正式定盘
    auction_late : 9:30-9:40 开盘瞬间补定(页面当时未打开)
    intraday     : 9:40-15:05 盘中兜底补定(服务重启/迟开页面); 一律用今日开盘缺口口径
    * 这与前端「是否暂停实时刷新」的严格 9:25-9:30 窗口是两件事, 见 in_freeze_window()。"""
    if hm is None:
        n = time.localtime()
        hm = n.tm_hour * 100 + n.tm_min
    if 925 <= hm < 930:
        return "auction"
    if 930 <= hm < 940:
        return "auction_late"
    if 940 <= hm <= 1505:
        return "intraday"
    return None


def in_freeze_window(hm=None):
    """【前端口径】严格 9:25-9:30 竞价定盘窗口。
    此时定盘名单刚锁定, 界面应暂停刷新、只呈现这份名单。"""
    if hm is None:
        n = time.localtime()
        hm = n.tm_hour * 100 + n.tm_min
    return 925 <= hm < 930


def load_frozen_picks(day=None):
    """读取当日定盘快照; 不存在或损坏返回 None。"""
    p = _freeze_file(day or time.strftime("%Y%m%d"))
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        sys.stderr.write("[warn] 定盘快照读取失败 %s: %s\n" % (p, e))
        return None


def _save_frozen_picks(snap):
    with open(_freeze_file(snap["date"]), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    try:                                    # 只保留最近 N 个交易日
        files = sorted(f for f in os.listdir(DIR)
                       if f.startswith("_bid_picks_") and f.endswith(".json"))
        for old in files[:-BID_FREEZE_KEEP]:
            os.remove(os.path.join(DIR, old))
    except Exception:
        pass


def maybe_freeze_picks(picks, meta):
    """定盘: 把当前 TOP3 落盘为当日快照(每交易日仅一次, 幂等)。
    返回快照 dict; 不在窗口 / 已定格 / 今日无盘口则返回 None。"""
    if not picks:
        return None
    now = time.localtime()
    hm = now.tm_hour * 100 + now.tm_min
    mode = freeze_eligible(hm)
    if mode is None:
        return None
    day = time.strftime("%Y%m%d", now)
    if load_frozen_picks(day):
        return None
    if not market_open_today():             # 护栏: 休市日绝不定格
        return None
    with _FREEZE_LOCK:
        if load_frozen_picks(day):          # 双检: 防两个线程并发写
            return None
        bases = {p.get("gap_basis") for p in picks if p.get("gap_basis")}
        if mode != "auction" and bases == {"live"}:
            # 已过竞价时段却拿不到今日开盘价: 只剩盘中漂移价可依, 宁可不定格也不给错信号
            sys.stderr.write("[warn] 定盘延后且开盘价缺失, 跳过定格以免用盘中价误导\n")
            return None
        basis = (bases.pop() if len(bases) == 1 else ("mixed" if bases else None))
        snap = {"date": day, "date_label": _td_label(day),
                "frozen_at": time.strftime("%H:%M:%S", now), "frozen_hm": hm,
                "frozen_ts": int(time.time()),
                "mode": mode, "mode_name": _FREEZE_MODES.get(mode, mode),
                "basis": basis, "basis_name": _FREEZE_BASIS.get(basis, basis),
                "base": meta.get("base"), "phase": meta.get("phase"),
                "mood": meta.get("mood"), "mood_desc": meta.get("mood_desc"),
                "stage_name": meta.get("stage_name"),
                # 卖点纪律随快照落盘 —— 让快照自描述，push_picks 不必硬编码文案。
                # （2026-09-22 同步遗漏，2026-09-23 补齐 —— 缺这些字段时 push_picks 从
                #   快照读到的 exit_note / vol_veto_ratio 恒为 None。）
                "exit_rule": meta.get("exit_rule"), "exit_note": meta.get("exit_note"),
                "exit_desc": meta.get("exit_desc"),
                "pick_rule": meta.get("pick_rule"),
                "vol_veto_ratio": meta.get("vol_veto_ratio"),
                # 2026-09-23 买点模式随快照落盘 —— push_picks 据此决定「买/观/弃」标记与提示文案，
                # 避免像「只买低开-4~-1%」那样写死在 build_single/build_slots 里（改策略后文案不跟随）。
                "gap_mode": meta.get("gap_mode"),
                "max_board": meta.get("max_board"),
                "prefer_first_board": meta.get("prefer_first_board"),
                "picks": picks}
        try:
            _save_frozen_picks(snap)
        except Exception as e:
            sys.stderr.write("[warn] 定盘快照写入失败: %s\n" % e)
            return None
        sys.stderr.write("[info] 竞价定盘已定格(%s @%s) %d 只: %s\n"
                         % (snap["mode_name"], snap["frozen_at"], len(picks),
                            ", ".join("%s %s" % (p.get("code"), p.get("name") or "")
                                      for p in picks)))
        return snap


def start_freeze_scheduler():
    """后台线程: 定盘窗口内自动定格 TOP3(浏览器没打开也能定格)。
    幂等 —— 已有当日快照或不在窗口时为空转, 开销可忽略。"""
    def _loop():
        while True:
            try:
                if freeze_eligible() and not load_frozen_picks():
                    build_bid_watch_payload()
            except Exception as e:
                sys.stderr.write("[warn] 定盘线程异常: %s\n" % e)
            time.sleep(15)
    threading.Thread(target=_loop, daemon=True).start()


def _pick_key(r):
    """TOP3 精选排序键 —— 2026-09-22 改为「**不空仓** + 赚钱效应优先」三级降位。

    档位（数字越小越优先）：
      0  可参与(win_ok) 且无「放量下跌」  -> 正常推
      1  win_ok=False（一字买不进/深低开/退潮）但量能无异常 -> 仍可推（买不进收益=0 优于负期望）
      2  放量下跌（vol_veto 命中）         -> 最后才用，仅当同批不足 3 只时垫底
    旧键 `(not win_ok, -win_adj, …)` 让 win_ok 单独决定展示位，与下游 push_picks 的
    buy 标记叠加后，用户收到的是策略自己判「不可买」的票（服务器 5 快照 15/15 条
    buy=False 却全被推送）。改为分级降位后：任何单一门控都无法清空推送（不空仓），
    赚钱效应分 win_adj 成为实际主排序键。

    2026-09-23 追加两档（老张口径：优先首板、4 板以上不考虑）：
      第 3 位 `_over`  = 1 -> board > MAX_BOARD(3)，即 4 板及以上垫底
                        （「不考虑」但**不剔除**，保住「不空仓」原则：
                          极端情况下凑不出别的票时仍会推）
      第 4 位 `_first` = 0 -> board == 1（首板）优先，「优先选择首板涨停后的」
    两者都取自 reco_engine 模块全局（_re_attr 动态读），改 strategy/*.json 即热生效。

    ⚠️ 本文件是**扁平运行副本**（仓库 B），与仓库 A 的 `rt/bidwatch.py::_pick_key`
    必须保持同一口径 —— 改一处务必同步另一处。回切高开见 `GAP_MODE`。
    """
    _mb = _re_attr("MAX_BOARD", 3)
    _pf = _re_attr("PREFER_FIRST_BOARD", True)
    try:
        _lb = int(r.get("board") or 0)
    except Exception:
        _lb = 0
    try:
        _over = 1 if _lb > int(_mb) else 0
    except Exception:
        _over = 0
    _first = 0 if (_pf and _lb == 1) else 1
    return (1 if r.get("veto") else 0,
            1 if not r.get("win_ok") else 0,
            _over,
            _first,
            -(r.get("win_adj") or 0),
            -(r.get("reco_score") or 0),
            -(r.get("seal") or 0))


def build_bid_watch_payload():
    """竞价观察名单 + 实时行情 + 买入提示. 名单由 gen_tomorrow.py 生成的 JSON 提供."""
    if not os.path.exists(WATCH_JSON):
        return 502, {"ok": False, "error": "竞价观察名单未生成, 请先运行 astock-screen/gen_tomorrow.py"}
    try:
        watch = json.load(open(WATCH_JSON, encoding="utf-8"))
    except Exception as e:
        return 502, {"ok": False, "error": "名单读取失败：%s" % e}
    items = watch.get("list") or []
    phase, phase_desc = market_phase(watch.get("base_date"))
    base_meta = {"ok": True, "generated": watch.get("generated"),
                 "base": watch.get("base_date"),
                 "phase": phase, "phase_desc": phase_desc,
                 "zt_total": watch.get("zt_total"), "zb_total": watch.get("zb_total"),
                 "mood": watch.get("mood"), "mood_desc": watch.get("mood_desc"),
                 "sectors": watch.get("main_sectors", []),
                 # 情绪周期阶段(智能推荐上游入口): 优先用名单里已算好的 stage, 否则现场推
                 "stage_code": watch.get("stage_code"),
                 "stage_name": watch.get("stage_name"),
                 "stage_desc": watch.get("stage_desc"),
                 "stage_color": watch.get("stage_color"),
                 # 2026-09-22 调整：推送策略（不空仓 + 赚钱效应排序 + 量比否决）+ 卖点纪律
                 # 2026-09-23 追加：买点模式/高位板上限/首板优先（老张口径，均走 _re_attr 动态读）
                 "pick_rule": ("不空仓·按赚钱效应(win_adj)取 TOP3；买点模式=%s；"
                               "%s 板以上不考虑；首板优先=%s；"
                               "仅对「放量下跌」降位，不做剔除"
                               % (_re_attr("GAP_MODE", "low"),
                                  _re_attr("MAX_BOARD", 3),
                                  "是" if _re_attr("PREFER_FIRST_BOARD", False) else "否")),
                 "gap_mode": _re_attr("GAP_MODE", "low"),
                 "max_board": _re_attr("MAX_BOARD", 3),
                 "prefer_first_board": _re_attr("PREFER_FIRST_BOARD", False),
                 # 阈值/文案动态读 —— 策略文件热重载后不会显示过期值（见 _re_attr）
                 "vol_veto_ratio": _re_attr("VOL_VETO_RATIO", VOL_VETO_RATIO),
                 "exit_rule": _re_attr("EXIT_RULE", EXIT_RULE),
                 "exit_note": _re_attr("EXIT_NOTE", EXIT_NOTE),
                 "exit_desc": _re_attr("EXIT_DESC", EXIT_DESC)}
    if not items:
        return 200, dict(base_meta, data={"count": 0, "buy": 0, "items": []})
    # 情绪周期阶段 code(供竞价裁决门控)
    stage_code = watch.get("stage_code") or (resolve_stage(watch.get("zt_total"))[0]
                                             if resolve_stage else "fajiao")
    codes = [x["code"] for x in items]
    qmap = {}
    try:
        for q in fetch_quotes(codes):
            qmap[q["code"]] = q
    except Exception as e:
        return 502, {"ok": False, "error": "实时行情获取失败：%s" % e}
    out = []
    for x in items:
        q = qmap.get(x["code"]) or {}
        chg = q.get("chg")
        price = q.get("price")
        base_price = x.get("base_price")
        # gap = 相对名单基准价(上一交易日收盘/涨停价)的偏离 = 竞价高开幅度
        gap = None
        if price is not None and base_price:
            try:
                gap = (float(price) - float(base_price)) / float(base_price) * 100.0
            except Exception:
                gap = None
        # 开盘缺口 = 今日开盘价(9:25 集合竞价撮合价) vs 名单基准价。
        # 这是「9:25 定盘」的形态基准: 盘中现价会漂移, 开盘价不会。
        open_price = q.get("open")
        gap_open = None
        if open_price and base_price:
            try:
                gap_open = (float(open_price) - float(base_price)) / float(base_price) * 100.0
            except Exception:
                gap_open = None
        # 量能公理: 竞价量比 = 实时成交额 / 上一交易日成交额(yamt)
        vol_ratio = None
        yamt = x.get("yamt") or 0
        amt = q.get("amount") or 0
        if yamt and amt:
            try:
                vol_ratio = float(amt) / float(yamt) * 100.0  # 百分比(如 3% 即放量阈值线)
            except Exception:
                vol_ratio = None
        # 智能推荐(六维) → 竞价(高开+量能) 联合裁决
        if auction_judge:
            signal, action, color, buy = auction_judge(
                x["cls"], x.get("board"), gap, phase, stage_code,
                x.get("reco_score") or 0, x.get("is_main"),
                vol_ratio=vol_ratio)
        else:
            signal, action, color, buy = auction_signal(x["cls"], x.get("board"), gap, phase)
        # 量能公理结论(用于前端展示"放量确认/缩量诱多/待确认")
        vol_verdict = volume_axiom(gap, vol_ratio)[0] if volume_axiom else "—"
        # 仅竞价/盘中时段 gap 才是「竞价高开幅度」; pre / pre_open / closed 时 gap 恒为 0
        # 或等于当日涨跌幅, 若拿去判形态会把全部票误标「平开·最佳形态」, 故置 None。
        gap_eff = gap if phase in ("auction", "open") else None
        # 赚钱效应精选裁决(实证加权, 决定是否进入 TOP3 展示位)
        if win_pick:
            try:
                w_adj, w_label, w_color, w_ok, w_reason = win_pick(
                    x, gap=gap_eff, stage_code=stage_code, mood=watch.get("mood"))
            except Exception:
                w_adj = w_label = w_color = w_reason = None
                w_ok = False
        else:
            w_adj = w_label = w_color = w_reason = None
            w_ok = False
        # 买点门控(342 只实证): 与 win_score「选哪只」正交 —— 它决定「买不买」。
        # 口径 = 当日开盘缺口(gap_eff); 盘后 gap_eff 为 None -> 显示「待竞价确认」。
        if buy_zone:
            try:
                _bz = buy_zone(gap_eff)
            except Exception:
                _bz = {"code": None, "label": "待确认", "color": "#94a3b8",
                       "buy": None, "note": ""}
        else:
            _bz = {"code": None, "label": "待确认", "color": "#94a3b8",
                   "buy": None, "note": ""}
        # 一字风险预判(盘后即可用): 封单额/流通市值 比 -> 明早顶一字买不进概率
        if yz_risk:
            try:
                _yz = yz_risk(x)
            except Exception:
                _yz = {"ratio": None, "level": "未知", "color": "#94a3b8", "note": ""}
        else:
            _yz = {"ratio": None, "level": "未知", "color": "#94a3b8", "note": ""}
        # 竞价量比否决(2026-09-22 实证): 低开 + 量比≥VOL_VETO_RATIO = 放量下跌·真出货。
        # 只降位、不剔除 —— 见 _pick_key。gap_eff 为 None（盘后/未开盘）时一律不否决。
        if vol_veto:
            try:
                _vt, _vt_reason = vol_veto(gap_eff, vol_ratio)
            except Exception:
                _vt, _vt_reason = False, ""
        else:
            _vt, _vt_reason = False, ""
        out.append({
            "code": x["code"], "name": x["name"], "ind": x.get("ind"),
            "cls": x["cls"], "cls_name": x["cls_name"],
            "board": x.get("board"), "seal": x.get("seal"), "rating": x.get("rating"),
            "zbc": x.get("zbc"), "is_main": x.get("is_main"),
            "yamt": x.get("yamt"), "note": x.get("note"),
            "base_price": base_price, "price": price, "chg": chg, "gap": gap,
            "open": open_price, "gap_open": gap_open,
            # === 智能推荐引擎字段 ===
            "reco_score": x.get("reco_score"), "reco_grade": x.get("reco_grade"),
            "six_dims": x.get("six_dims"), "vol_ratio": vol_ratio, "vol_verdict": vol_verdict,
            "signal": signal, "action": action, "color": color, "buy": buy,
            # === 赚钱效应精选(TOP3)字段 ===
            "win_adj": (round(w_adj, 1) if w_adj is not None else None),
            "win_label": w_label, "win_color": w_color, "win_ok": w_ok, "win_reason": w_reason,
            # === 一字风险(封单/流通比) ===
            "yz_ratio": (round(_yz["ratio"], 2) if _yz.get("ratio") is not None else None),
            "yz_level": _yz.get("level"), "yz_color": _yz.get("color"), "yz_note": _yz.get("note"),
            # === 买点门控(竞价缺口 -> 可买/观望/放弃) ===
            "buy_code": _bz.get("code"), "buy_label": _bz.get("label"),
            "buy_color": _bz.get("color"), "buy_ok": _bz.get("buy"),
            "buy_note": _bz.get("note"),
            # === 量比否决(放量下跌, 只降位不剔除) ===
            "veto": _vt, "veto_reason": _vt_reason,
        })
    out.sort(key=lambda r: (not r["buy"], -(r["reco_score"] or 0), -(r["board"] or 0), -(r["seal"] or 0)))
    # TOP3 精选: 2026-09-22 起走 _pick_key 的「不空仓 + 赚钱效应优先」三级降位。
    ranked = sorted(out, key=_pick_key)
    picks = ranked[:3]
    # 定盘精选: 用「今日开盘缺口」重跑形态门控 —— 与 9:25 竞价定盘等价。
    # 因此即便快照在 9:40 后才补定(当时页面未打开/服务重启), 结论依然是 9:25 的口径,
    # 不会被盘中漂移的价格污染。开盘价缺失(刚过 9:25)时退回实时缺口。
    src_map = {x["code"]: x for x in items}
    fz_rank = []
    if win_pick:
        for r in ranked:
            x = src_map.get(r["code"])
            if not x:
                continue
            g, basis = r.get("gap_open"), "open"
            if g is None:
                g, basis = r.get("gap"), "live"
            try:
                adj, lab, col, ok, rea = win_pick(x, gap=g, stage_code=stage_code,
                                                  mood=watch.get("mood"))
            except Exception:
                continue
            rr = dict(r)
            rr.update({"win_adj": (round(adj, 1) if adj is not None else None),
                       "win_label": lab, "win_color": col, "win_ok": ok, "win_reason": rea,
                       "gap_used": g, "gap_basis": basis})
            # 买点门控用「开盘缺口」重算 —— 与 9:25 定盘同口径, 不受盘中价格漂移影响
            if buy_zone:
                try:
                    _bz2 = buy_zone(g)
                    rr.update({"buy_code": _bz2["code"], "buy_label": _bz2["label"],
                               "buy_color": _bz2["color"], "buy_ok": _bz2["buy"],
                               "buy_note": _bz2["note"]})
                except Exception:
                    pass
            # 量比否决同样用「开盘缺口」重算；但只在**实时竞价/盘中**生效
            # （与上面 gap_eff 同一道门）：pre/pre_open/closed 时该缺口不是竞价缺口，
            # 据此否决会误伤 —— 09-16 那批 vol_ratio≈100 的占位值即属此类。
            if vol_veto and phase in ("auction", "open"):
                try:
                    _vt2, _vt2r = vol_veto(g, r.get("vol_ratio"))
                    rr.update({"veto": _vt2, "veto_reason": _vt2r})
                except Exception:
                    pass
            else:
                rr.update({"veto": False, "veto_reason": ""})
            # 竞价裁决结果**回写 signal/action** —— 修 09-18 批的字段缺陷：
            # 那批 gap 有值(-1.43/-2.85/-1.10)，signal 却仍是「待竞价」、
            # action 仍是「名单已就绪；明早 9:15 集合竞价开始后…」。
            if auction_judge:
                try:
                    _sg, _ac, _cl, _by = auction_judge(
                        x["cls"], x.get("board"), g, phase, stage_code,
                        x.get("reco_score") or 0, x.get("is_main"),
                        vol_ratio=r.get("vol_ratio"))
                    rr.update({"signal": _sg, "action": _ac, "color": _cl, "buy": _by})
                except Exception:
                    pass
            fz_rank.append(rr)
        fz_rank.sort(key=_pick_key)
    freeze_picks = (fz_rank[:3] or picks)
    # 定盘: 9:25-9:30 首次拿到定盘 TOP3 即落盘(幂等), 全天保留
    maybe_freeze_picks(freeze_picks, base_meta)
    frozen = load_frozen_picks()
    if frozen and frozen.get("picks"):
        live_map = {r["code"]: r for r in out}
        frozen["mode_name"] = _FREEZE_MODES.get(frozen.get("mode"), frozen.get("mode"))
        for fp in frozen["picks"]:          # 附上「定格后走到哪了」, 快照本体不改写
            lr = live_map.get(fp.get("code")) or {}
            fp["now_price"] = lr.get("price")
            fp["now_chg"] = lr.get("chg")
            fp["now_gap"] = lr.get("gap")
            fp["now_buy"] = bool(lr.get("buy"))
            # 买点门控: 用快照自带的「定格缺口」现算 —— 兼容不带 buy_* 字段的历史快照
            if buy_zone and not fp.get("buy_code"):
                try:
                    _bzf = buy_zone(fp.get("gap_used"))
                    fp.update({"buy_code": _bzf["code"], "buy_label": _bzf["label"],
                               "buy_color": _bzf["color"], "buy_ok": _bzf["buy"],
                               "buy_note": _bzf["note"]})
                except Exception:
                    pass
    now_l = time.localtime()
    now_hm = now_l.tm_hour * 100 + now_l.tm_min
    return 200, dict(base_meta,
                     data={"count": len(out), "buy": sum(1 for r in out if r["buy"]),
                           "veto": sum(1 for r in out if r.get("veto")),
                           "items": out, "picks": picks, "picks_n": len(picks),
                           "frozen": frozen,
                           "freeze_window": in_freeze_window(now_hm),
                           "now_hm": now_hm,
                           "now": time.strftime("%H:%M:%S", now_l)})


# ---------- 竞价观察名单刷新 (后台线程重算, 供按钮/定时任务触发) ----------
REFRESH_WATCH = {"status": "idle", "ts": 0.0, "step": "", "log": ""}


def _run_refresh_watch():
    """依次重算: 历史基因池(缺则补) -> 拉今日涨停快照 -> 重算名单. 写入 REFRESH_WATCH 状态."""
    global REFRESH_WATCH
    screen_dir = os.path.join(DIR, "..", "astock-screen")
    py = sys.executable
    steps = []
    if not os.path.exists(os.path.join(screen_dir, "limit_up.json")):
        steps.append(("重算历史涨停基因池(limit_up)", [py, "limit_up.py"]))
    steps.append(("拉取今日收盘涨停快照(live_limit --fetch)", [py, "live_limit.py", "--fetch"]))
    steps.append(("生成明日竞价观察名单(gen_tomorrow)", [py, "gen_tomorrow.py"]))
    # 影子记录: 固化今日智能推荐各排序口径的选股 + 结算上一交易日表现(best-effort)
    steps.append(("影子记录(智能推荐排序口径)", [py, "shadow_log.py", "daily"]))
    REFRESH_WATCH["log"] = ""
    try:
        for label, cmd in steps:
            REFRESH_WATCH["step"] = label
            REFRESH_WATCH["log"] += "[%s] %s ...\n" % (time.strftime("%H:%M:%S"), label)
            subprocess.run(cmd, cwd=screen_dir, capture_output=True, text=True,
                           timeout=600, check=False)
        REFRESH_WATCH["status"] = "done"
        REFRESH_WATCH["step"] = ""
        REFRESH_WATCH["ts"] = time.time()
        REFRESH_WATCH["log"] += "[%s] 刷新完成\n" % time.strftime("%H:%M:%S")
    except Exception as e:
        REFRESH_WATCH["status"] = "error"
        REFRESH_WATCH["step"] = ""
        REFRESH_WATCH["ts"] = time.time()
        REFRESH_WATCH["log"] += "刷新失败: %s\n" % e


def trigger_refresh_watch():
    global REFRESH_WATCH
    if REFRESH_WATCH.get("status") == "running":
        return False
    REFRESH_WATCH = {"status": "running", "ts": time.time(), "step": "准备中",
                     "log": "启动刷新...\n"}
    threading.Thread(target=_run_refresh_watch, daemon=True).start()
    return True


# 历史涨停基因池重算 (纯本地, 依赖 raw_stats.json + kdata/ 缓存, 秒级)
GENEPOOL_REFRESH = {"status": "idle", "ts": 0.0, "step": "", "log": ""}


def _run_rebuild_genepool():
    """重算 limit_up.json(历史基因池), 并若已有今日快照则顺带重算名单. 写入 GENEPOOL_REFRESH 状态."""
    global GENEPOOL_REFRESH
    screen_dir = os.path.join(DIR, "..", "astock-screen")
    py = sys.executable
    steps = [("重算近1年涨停基因池(limit_up, 本地秒级)", [py, "limit_up.py"])]
    if os.path.exists(os.path.join(screen_dir, "live_raw.json")):
        steps.append(("用新基因池重算竞价名单(gen_tomorrow)", [py, "gen_tomorrow.py"]))
    GENEPOOL_REFRESH["log"] = ""
    try:
        for label, cmd in steps:
            GENEPOOL_REFRESH["step"] = label
            GENEPOOL_REFRESH["log"] += "[%s] %s ...\n" % (time.strftime("%H:%M:%S"), label)
            subprocess.run(cmd, cwd=screen_dir, capture_output=True, text=True,
                           timeout=600, check=False)
        GENEPOOL_REFRESH["status"] = "done"
        GENEPOOL_REFRESH["step"] = ""
        GENEPOOL_REFRESH["ts"] = time.time()
        GENEPOOL_REFRESH["log"] += "[%s] 完成\n" % time.strftime("%H:%M:%S")
    except Exception as e:
        GENEPOOL_REFRESH["status"] = "error"
        GENEPOOL_REFRESH["step"] = ""
        GENEPOOL_REFRESH["ts"] = time.time()
        GENEPOOL_REFRESH["log"] += "失败: %s\n" % e


def trigger_rebuild_genepool():
    global GENEPOOL_REFRESH
    if GENEPOOL_REFRESH.get("status") == "running":
        return False
    GENEPOOL_REFRESH = {"status": "running", "ts": time.time(), "step": "准备中",
                        "log": "启动重算...\n"}
    threading.Thread(target=_run_rebuild_genepool, daemon=True).start()
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        if isinstance(obj, (dict, list)):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        else:
            body = obj.encode("utf-8") if isinstance(obj, str) else obj
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        """HEAD 探活支持。

        部分客户端（浏览器预览面板的健康检查、外部监控探针）会先发 HEAD，
        若未实现则 BaseHTTPRequestHandler 返回 501 并在日志里刷错误。
        这里只回首部、不带 body，语义符合 RFC 9110。
        """
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            ctype = "text/html; charset=utf-8"
        elif path.startswith("/api/"):
            ctype = "application/json; charset=utf-8"
        else:
            ctype = "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            if path == "/api/limit_up":
                code, payload = build_stock_payload("up")
                self._send(code, payload)
                return
            if path == "/api/limit_down":
                code, payload = build_stock_payload("down")
                self._send(code, payload)
                return
            if path == "/api/recommend":
                q = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
                prev = (q.get("prev") or [""])[0] == "1"
                # rank 仅用于影子对照观察(同批票只换 tiebreaker), 缺省=现行口径
                rk = (q.get("rank") or [""])[0].strip().lower()
                rk = rk if rk in ("ratio", "seal") else None
                code, payload = build_recommend_payload(prev=prev, rank=rk)
                self._send(code, payload)
                return
            if path == "/api/limit_prev":
                q = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
                kind = (q.get("kind") or [""])[0]
                kind = "down" if kind == "down" else "up"
                code, payload = build_prev_limit_payload(kind)
                self._send(code, payload)
                return
            if path == "/api/bid_watch":
                code, payload = build_bid_watch_payload()
                self._send(code, payload)
                return
            if path == "/api/refresh_watch":
                started = trigger_refresh_watch()
                self._send(200, {"ok": True, "started": started,
                                 "msg": ("已启动后台刷新（约1-3分钟），完成后竞价观察页自动更新"
                                         if started else "刷新进行中，请稍候")})
                return
            if path == "/api/refresh_status":
                self._send(200, dict(REFRESH_WATCH))
                return
            if path == "/api/rebuild_genepool":
                started = trigger_rebuild_genepool()
                self._send(200, {"ok": True, "started": started,
                                 "msg": ("已启动后台重算基因池(秒级), 完成后竞价名单将用新基因池刷新"
                                         if started else "重算进行中, 请稍候")})
                return
            if path == "/api/genepool_status":
                self._send(200, dict(GENEPOOL_REFRESH))
                return
            if path == "/api/sectors":
                code, payload = build_sector_payload()
                self._send(code, payload)
                return
            if path == "/api/changes":
                q = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
                code, payload = build_changes_payload(q)
                self._send(code, payload)
                return
            if path == "/api/trends":
                q = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
                code = (q.get("code") or [""])[0]
                if not code:
                    self._send(400, {"ok": False, "error": "缺少 code 参数"})
                    return
                try:
                    res = get_trends(code)
                    self._send(200, res)
                except Exception as e:
                    self._send(502, {"ok": False, "error": "分时数据获取失败：%s" % e})
                return
            if path == "/api/kline":
                q = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
                code = (q.get("code") or [""])[0]
                if not code:
                    self._send(400, {"ok": False, "error": "缺少 code 参数"})
                    return
                days = (q.get("days") or ["60"])[0]
                try:
                    res = get_kline(code, days)
                    self._send(200, res)
                except Exception as e:
                    self._send(502, {"ok": False, "error": "日K数据获取失败：%s" % e})
                return
            if path == "/api/search":
                q = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
                kw = (q.get("q") or [""])[0]
                if not kw:
                    self._send(400, {"ok": False, "error": "缺少 q 参数"})
                    return
                try:
                    self._send(200, {"ok": True, "data": fetch_search(kw)})
                except Exception as e:
                    self._send(502, {"ok": False, "error": "搜索失败：%s" % e})
                return
            if path == "/api/quotes":
                q = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
                codes = (q.get("codes") or [""])[0]
                codes = [c for c in codes.split(",") if c.strip()]
                if not codes:
                    self._send(400, {"ok": False, "error": "缺少 codes 参数"})
                    return
                try:
                    self._send(200, {"ok": True, "data": fetch_quotes(codes)})
                except Exception as e:
                    self._send(502, {"ok": False, "error": "行情获取失败：%s" % e})
                return
            self._send(404, {"ok": False, "error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send(500, {"ok": False, "error": str(e)})
            except Exception:
                pass

    do_POST = do_GET


def main():
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    t = threading.Thread(target=background_refresh, daemon=True)
    t.start()
    start_changes_collector()
    start_freeze_scheduler()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write("A股实时看板已启动: http://localhost:%d\n" % port)
    sys.stderr.write("[info] 架构: 后台线程每15s刷新涨停/跌停(push2ex专题池, 失败降级clist全市场)/30s刷新板块; HTTP只读缓存; 限频指数退避+降级; 分时按需拉取(push2delay, 含name/昨收反推补充); 异动流2s节拍轮转采集(交易时段)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n已停止.\n")
        server.shutdown()


if __name__ == "__main__":
    main()
