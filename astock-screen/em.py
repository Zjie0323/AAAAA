# -*- coding: utf-8 -*-
"""东方财富行情接口公共层(已在本机实测可用的配置: 多节点轮换 + Connection: close)."""
import json
import time
import urllib.request
import urllib.parse

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Connection": "close",
}

QUOTE_HOSTS = [
    "push2delay.eastmoney.com",
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
]
# 实测结论(本机):
#   push2his + UT(fa5fd...)  -> 唯一稳定返回历史K线的组合, 数据新鲜
#   push2delay               -> 对 kline 路径静默返回 n=0 (不可用, 但可作最后兜底)
#   push2hisdelay            -> 返回 UTF-8 BOM, 需 utf-8-sig 解码
HIS_HOSTS = [
    "push2his.eastmoney.com",
    "push2hisdelay.eastmoney.com",
    "push2delay.eastmoney.com",
]
UT = "fa5fd080d2bcf6b6751843e8c9c826a4"


def _fetch(hosts, path, params, timeout=15, retries=2, nonempty=None):
    """依次尝试各节点; 任一成功即返回 dict. 全失败抛最后一次异常.

    nonempty: 可选谓词, 传入 (data_dict)->bool. 用于识别 '静默返回空数据' 的节点
    (如 push2delay 对 kline 返回 rc=0 但 klines=[]), 判空后自动切下一节点.
    """
    last_err = None
    for rnd in range(retries + 1):
        for host in hosts:
            url = "https://%s%s?%s" % (host, path, urllib.parse.urlencode(params))
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                # utf-8-sig: push2hisdelay 等节点返回带 BOM 的响应
                j = json.loads(raw.decode("utf-8-sig", "ignore"))
                if nonempty is not None:
                    if not nonempty(j.get("data") or {}):
                        raise RuntimeError("empty payload from %s" % host)
                return j
            except Exception as e:
                last_err = e
                # 指数退避: 第1轮几乎不等待, 后续逐轮加长, 规避上游限频
                time.sleep(0.4 * (2 ** rnd))
    raise last_err


def clist(fs, fields, fid="f6", pz=100, pn=1, po=1):
    """行情列表快照. fs 为市场筛选串, fields 为字段集."""
    params = {
        "pn": str(pn), "pz": str(pz), "po": str(po), "np": "1",
        "fltt": "2", "invt": "2", "fid": fid,
        "fs": fs, "fields": fields, "ut": UT,
        "_": str(int(time.time() * 1000)),
    }
    return _fetch(QUOTE_HOSTS, "/api/qt/clist/get", params)


def kline(secid, beg, end, fqt=1, klt=101):
    """历史K线. fqt: 0不复权 1前复权 2后复权. klt: 101日 102周 103月."""
    params = {
        "secid": secid,
        "klt": str(klt), "fqt": str(fqt),
        "beg": beg, "end": end,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": UT,
        "_": str(int(time.time() * 1000)),
    }
    return _fetch(HIS_HOSTS, "/api/qt/stock/kline/get", params,
                  nonempty=lambda d: bool(d.get("klines")))


def to_f(v, default=None):
    """东方财富在限频/停牌时部分字段退化为字符串或 '--', 统一安全转 float."""
    if v is None or v == "-" or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------- 涨停/跌停/炸板 专题池 ----------------
# 重要: clist(fs="m:0+t:2,m:1+t:2") 返回的是"沪深主板全部A股"列表, 配 fid=f3 只是按涨幅排序,
#       **不做任何涨停过滤**. 早期误把它当涨停池, 导致名单混入大量未涨停甚至下跌的票.
#       涨停池必须走下面的 push2ex 专题接口(权威), 它直接给出真实涨停家数 tc 与官方连板数 lbc.
EX_HOST = "push2ex.eastmoney.com"
UT_EX = "7eea3edcaed734bea9cbfc24409ed989"

# 专题池字段含义(实测):
#   c 代码 | m 市场(0深1沪) | n 名称 | p 现价(×1000) | zdp 涨跌幅%
#   amount 成交额(元) | ltsz 流通市值(元) | tshare 总市值(元) | hs 换手率%
#   lbc 连板数(官方权威) | fbt 首次封板时间(HHMMSS整数, 92500=09:25:00) | lbt 最后封板时间
#   fund 封单额(元)  <-- 单位是元, 不是亿
#   zbc 开板(炸板)次数 | hybk 所属行业 | zttj 涨停统计(如 {days:5, ct:3} = 5天3板)
# ⚠️⚠️ 关键坑(2026-09-10 修正): 三个专题池的 dpt 参数**必须都是 wz.ztzt**,
# 池子由 URL 路径区分。曾误按池子取 dpt(wz.zbgc / wz.dtzt), 实测返回 rc=206,
# 导致「炸板数/跌停数」恒为 0(连续 6 个交易日全部为 0 才被发现)。
# 正确写法参考 akshare stock_em_zt_pool_zbgc: dpt 恒为 wz.ztzt, 仅 path 不同。
# kind -> (path, sort, 中文名)
POOL_DPT = {
    "zt":  ("/getTopicZTPool",     "fbt:asc",  "涨停池"),
    "zb":  ("/getTopicZBPool",     "fbt:asc",  "炸板池"),   # 曾涨停后开板
    "dt":  ("/getTopicDTPool",     "fund:asc", "跌停池"),
    "yzt": ("/getYesterdayZTPool", "zs:desc",  "昨涨停池"),
}
POOL_COMMON_DPT = "wz.ztzt"   # 四个池共用, 不要按池子改


def topic_pool(kind="zt", date=None, pagesize=300):
    """抓取东财专题池(涨停/跌停/炸板)全量.

    返回 (tc, rows): tc=该池真实总家数, rows=池内个股原始 dict 列表.
    date: 'YYYYMMDD', 缺省取今日. 非交易日返回 (0, []).
    """
    if kind not in POOL_DPT:
        raise ValueError("kind must be one of %s" % list(POOL_DPT))
    path, sort, _label = POOL_DPT[kind]
    if not date:
        date = time.strftime("%Y%m%d")
    rows, tc, page = [], 0, 0
    while True:
        params = {
            "ut": UT_EX, "dpt": POOL_COMMON_DPT, "Pageindex": str(page),
            "pagesize": str(pagesize), "sort": sort, "date": date,
            "_": str(int(time.time() * 1000)),
        }
        j = _fetch([EX_HOST], path, params)
        data = j.get("data") or {}
        tc = data.get("tc", 0) or 0
        pool = data.get("pool") or []
        if not pool:
            break
        rows.extend(pool)
        if len(rows) >= tc or len(pool) < pagesize:
            break
        page += 1
        time.sleep(0.3)
    return tc, rows


def mood(tc, zb_tc=None):
    """盘面定性 = 涨停家数(打板稀缺性) × 炸板率(封板坚决度)。  <-- 单一事实源

    炸板率 = zb / (zb + tc)。实测(2026-09)炸板率 <25% 的交易日, 次日名单表现
    明显好于 >35% 的交易日; 涨停少 + 炸板率高 = 亏钱效应扩散, 应空仓。
    live_limit.py 与 gen_tomorrow.py 都从这里导入, 不要再各写一份(会漂移)。
    """
    if tc is None:
        return ("未知", "#7f8c8d", "无数据")
    ratio = None
    if zb_tc and (zb_tc + tc) > 0:
        ratio = zb_tc / float(zb_tc + tc) * 100.0
    if tc >= 120:
        name, color, base = ("强普涨/政策催化", "#c0392b",
                             "涨停极多, 打板稀缺性低、次日易分化, 只打主线龙头")
    elif tc >= 60:
        name, color, base = ("情绪偏暖", "#e67e22",
                             "涨停家数健康, 主线清晰时打板胜率较高")
    elif tc >= 30:
        name, color, base = ("结构性分化", "#b9770e",
                             "涨停有限, 资金只做少数主线, 严格只打板块龙头, 非主线一律不碰")
    elif tc >= 10:
        name, color, base = ("情绪偏冷", "#2e86c1",
                             "涨停稀少, 打板胜率低、炸板风险高, 建议轻仓或空仓观望")
    else:
        name, color, base = ("冰点/极弱", "#1e8449",
                             "涨停极少, 打板期望为负, 应空仓等情绪修复")
    if ratio is not None:
        if ratio >= 40:
            base += "；炸板率 %.0f%% 极高——封板资金不坚决, 次日溢价大概率差" % ratio
        elif ratio >= 30:
            base += "；炸板率 %.0f%% 偏高——只做换手回封, 不排一字" % ratio
        elif ratio < 20:
            base += "；炸板率 %.0f%% 极低——封板坚决、赚钱效应好, 可正常参与主线" % ratio
        else:
            base += "；炸板率 %.0f%% 中性" % ratio
        if tc < 60 and ratio >= 35:
            name += "·亏钱效应扩散"
            color = "#1e8449"
    return (name, color, base)


def keep_history(path, day, max_keep=30):
    """按日留存历史副本(供次日/后续复盘): 数据主文件写完后调用.
    复制一份 <主名>_<day><ext>(如 tomorrow_watch.json -> tomorrow_watch_2026-09-03.json),
    并自动清理本主文件派生出的旧副本, 只保留最近 max_keep 份。
    path   : 主文件绝对路径
    day    : 日期标签, 形如 YYYY-MM-DD
    注意: 只清理匹配 <主名>_YYYY-MM-DD.<ext> 的派生副本, 绝不动主文件本身。
    """
    import glob, os, shutil
    if not os.path.exists(path):
        return None
    base, ext = os.path.splitext(path)
    hist = "%s_%s%s" % (base, day, ext)
    try:
        shutil.copyfile(path, hist)
    except OSError:
        return None
    pat = os.path.join(os.path.dirname(path),
                       os.path.basename(base) + "_2???-??-??" + ext)
    for o in sorted(glob.glob(pat))[:-max_keep]:
        try:
            os.remove(o)
        except OSError:
            pass
    return hist
