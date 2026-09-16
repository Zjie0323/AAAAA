# -*- coding: utf-8 -*-
"""历史复盘样本回填器

背景:
  review_compare.py 用的是实时行情接口, 只能抓「当前」价格;
  一旦 _review_YYYY-MM-DD.json 没落盘, 事后就无法重算那一天的复盘数据。
  历史日K接口保留了每日 OHLC, 可离线回填。

用途:
  把早先没留存的复盘日补回来, 使「六维分 × 实盘收益」相关性分析立刻有足够样本,
  而不必从今天起重新累积。

数据源(双源容错):
  主: 腾讯 web.ifzq.gtimg.cn/appstock/app/fqkline/get  (不复权, 稳定)
  备: 东财 push2his.eastmoney.com/api/qt/stock/kline/get
  ⚠️ 东财 push2his 在连续批量请求后会 RemoteDisconnected(限流), 故默认走腾讯。

用法:
  python backfill_review.py                      # 自动扫全部 tomorrow_watch_YYYY-MM-DD.json
  python backfill_review.py tomorrow_watch_2026-09-03.json ...
  python backfill_review.py --force              # 覆盖已存在的 _review 文件

产出:
  _review_<实盘日>.json  (与 review_compare.build_rows 同结构, 供 reco_corr 累积)
"""
import glob, json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
RT = os.path.abspath(os.path.join(HERE, "..", "astock-realtime"))
if RT not in sys.path:
    sys.path.insert(0, RT)

import server as S
from review_compare import sealed_of

TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
EM_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_UT_K = "fa5fd1943c7b386f172d6893dbfba10b"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://quote.eastmoney.com/"}


def tx_code(code):
    """腾讯代码前缀。"""
    if code[:2] == "92" or code[:2] in ("43", "83", "87", "88"):
        return "bj" + code
    return ("sh" if S.market_of(code) == 1 else "sz") + code


def _dash(d):
    d = str(d).replace("-", "")
    return "%s-%s-%s" % (d[:4], d[4:6], d[6:8])


def kline_tx(code, beg, end):
    """腾讯日K(不复权) -> [{date,open,close,high,low,vol}] 升序。"""
    tx = tx_code(code)
    url = "%s?param=%s,day,%s,%s,60," % (TX_URL, tx, _dash(beg), _dash(end))
    j = json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers=HEADERS), timeout=20).read().decode("utf-8", "ignore"))
    d = (j.get("data") or {}).get(tx) or {}
    arr = d.get("day") or d.get("qfqday") or []
    out = []
    for it in arr:
        if len(it) < 6:
            continue
        try:
            out.append({"date": it[0], "open": float(it[1]), "close": float(it[2]),
                        "high": float(it[3]), "low": float(it[4]), "vol": float(it[5])})
        except (TypeError, ValueError):
            continue
    return out


def kline_em(code, beg, end):
    """东财日K(不复权, 备源) -> 同结构。"""
    params = {
        "secid": "%d.%s" % (S.market_of(code), code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": "0", "beg": str(beg).replace("-", ""),
        "end": str(end).replace("-", ""), "ut": EM_UT_K,
    }
    url = EM_URL + "?" + urllib.parse.urlencode(params)
    j = S.parse_em_json(urllib.request.urlopen(
        urllib.request.Request(url, headers=HEADERS), timeout=20).read())
    out = []
    for k in ((j.get("data") or {}).get("klines") or []):
        p = k.split(",")
        if len(p) < 11:
            continue
        out.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]), "vol": float(p[5])})
    return out


def kline(code, beg, end, retry=2):
    """先腾讯后东财, 带退避重试。"""
    last = None
    for src in (kline_tx, kline_em):
        for a in range(retry + 1):
            try:
                return src(code, beg, end)
            except Exception as e:
                last = e
                time.sleep(0.5 * (a + 1))
    raise last if last else RuntimeError("kline failed")


def resolve_next_day(codes, base):
    """名单基准日之后的下一个交易日(日K里第一条晚于 base 的记录)。"""
    end = (datetime.strptime(base, "%Y-%m-%d") + timedelta(days=20)).strftime("%Y%m%d")
    for c in codes[:20]:
        try:
            for k in kline(c, base, end):
                if k["date"] > base:
                    return k["date"]
        except Exception:
            continue
    return None


def build_row(x, day):
    ks = kline(x["code"], day, day)
    k = next((kk for kk in ks if kk["date"] == day), None)
    if not k:
        return None
    prev = x.get("base_price")
    close, opn = k["close"], k["open"]
    chg = round((close - prev) / prev * 100, 2) if prev else None
    seal, lp = sealed_of(prev, x["code"], close)
    gap = round((opn - prev) / prev * 100, 2) if prev else None
    oret = round((close - opn) / opn * 100, 2) if opn else None
    if seal:
        status = "晋级涨停"
    elif (chg or 0) <= -9.5:
        status = "跌停"
    elif (chg or 0) > 0:
        status = "红盘未板"
    elif chg == 0:
        status = "平收"
    else:
        status = "下跌"
    return dict(
        code=x["code"], name=x["name"], cls=x.get("cls"), board=x.get("board"),
        ind=x.get("ind"), is_main=x.get("is_main"), rating=x.get("rating"),
        reco=x.get("reco_score"), reco_grade=x.get("reco_grade"),
        six=x.get("six_dims"), grp=x.get("cls_name"), note=x.get("note"),
        prev=prev, open=opn, close=close, hi=k["high"], lo=k["low"],
        chg=chg, lp=lp, sealed=seal, status=status, gap=gap, oret=oret,
        amt=round(k["vol"] * 100 * close / 1e8, 2), hs=None, src="backfill",
    )


def backfill(watch_path, force=False, workers=6):
    d = json.load(open(watch_path, encoding="utf-8"))
    base, items = d.get("base_date"), d["list"]
    if not base:
        print("!! %s 无 base_date, 跳过" % os.path.basename(watch_path))
        return None
    day = resolve_next_day([x["code"] for x in items], base)
    if not day:
        print("!! %s (%s) 无法确定次一交易日, 跳过" % (os.path.basename(watch_path), base))
        return None
    out_path = os.path.join(HERE, "_review_%s.json" % day)
    if os.path.exists(out_path) and not force:
        print("== %s(%s) -> %s 已存在, 跳过(--force 覆盖)"
              % (os.path.basename(watch_path), base, os.path.basename(out_path)))
        return out_path

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(lambda x: build_row(x, day), items))
    ok = [r for r in rows if r]
    miss = [x["code"] for x, r in zip(items, rows) if not r]

    rep = {"label": day, "base_date": base, "gen_stage": d.get("stage_name"),
           "zt_total": d.get("zt_total"), "zb_total": d.get("zb_total"),
           "mood": d.get("mood"), "n": len(ok), "rows": ok,
           "source": "backfill(历史日K:腾讯)", "missing": miss}
    json.dump(rep, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    seal = sum(1 for r in ok if r["sealed"])
    ors = [r["oret"] for r in ok if r["oret"] is not None]
    print("%s(%s) -> %s: %d/%d 只 | 晋级 %d(%.0f%%) | 开买均值 %+.2f%%%s"
          % (os.path.basename(watch_path), base, day,
             len(ok), len(items), seal, seal / max(1, len(ok)) * 100,
             (sum(ors) / len(ors)) if ors else 0,
             (" | 无行情 %d 只: %s" % (len(miss), ",".join(miss[:6]))) if miss else ""))
    return out_path


def main():
    force = "--force" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--force"]
    files = args or sorted(glob.glob(os.path.join(HERE, "tomorrow_watch_2???-??-??.json")))
    if not files:
        print("!! 未找到 tomorrow_watch_YYYY-MM-DD.json")
        return
    done = [backfill(f if os.path.isabs(f) else os.path.join(HERE, f), force) for f in files]
    print("\n完成 %d/%d 个复盘日" % (len([x for x in done if x]), len(files)))


if __name__ == "__main__":
    main()
