# -*- coding: utf-8 -*-
"""昨日竞价名单 × 今日实盘 复盘对照工具 (通用, 每个交易日盘后复用)

用法(CLI):
  python review_compare.py <watch_json> <date_label> <out_json>
例:
  python review_compare.py tomorrow_watch_2026-09-09.json 2026-09-10 _review.json

也可作为模块被 gen_review.py 调用:
  from review_compare import build_rows
  rep = build_rows("tomorrow_watch_2026-09-09.json", "2026-09-10")

判定逻辑:
  * sealed  = 收盘价 >= 涨停价(昨收*(1.1 或 1.2)) - 0.005
  * gap     = (今开 - 昨收)/昨收    竞价高开幅度(相对名单基准价)
  * oret    = (收盘 - 今开)/今开    竞价开盘买入 → 收盘 盈亏
  * chg     = 当日涨跌幅(收盘 vs 昨收) —— 不受竞价买点影响的「选股/持有」口径
  * status  = 晋级涨停 / 跌停 / 红盘未板 / 平收 / 下跌

落盘字段除行情外, 同步带上名单侧信息(reco_score / six_dims / cls / board / is_main),
以便后续做「六维分 × 实盘收益」的相关性累积分析 (见 reco_corr.py)。
"""
import sys, json, urllib.request, urllib.parse, os

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
RT = os.path.abspath(os.path.join(HERE, "..", "astock-realtime"))
if RT not in sys.path:
    sys.path.insert(0, RT)

import server as S
from em import keep_history


def is_20(c):
    return c[:2] in ("30", "68", "92") or c[:3] == "920"


def sealed_of(prev, c, close):
    if not prev or not close:
        return False, None
    lp = round(prev * (1.2 if is_20(c) else 1.1), 2)
    return (close >= lp - 0.005), lp


def fetch_quotes(codes):
    """批量抓收盘/实时行情, 返回 {code: quote_dict}。多 host 轮询容错。"""
    secids = ",".join("%d.%s" % (S.market_of(c), c) for c in codes)
    params = {"fields": "f2,f3,f6,f8,f12,f14,f15,f16,f17,f18",
              "secids": secids, "fltt": "2", "ut": S.EM_UT}
    last, j = None, None
    for host in S.EM_HOSTS:
        try:
            url = "https://%s/api/qt/ulist.np/get?%s" % (host, urllib.parse.urlencode(params))
            req = urllib.request.Request(url, headers=S.HEADERS)
            j = S.parse_em_json(urllib.request.urlopen(req, timeout=15).read())
            if j.get("rc") == 0 and j.get("data"):
                break
        except Exception as e:
            last = e
            continue
    if not j or not j.get("data"):
        raise SystemExit("!! 行情获取全部失败: %s" % last)
    return {str(x["f12"]): x for x in j["data"]["diff"]}


def build_rows(watch_json, label):
    """名单 × 行情 → 复盘数据集 dict(label 为「实盘日」)。"""
    d = json.load(open(watch_json, encoding="utf-8"))
    items = d["list"]
    qmap = fetch_quotes([x["code"] for x in items])

    out = []
    for x in items:
        q = qmap.get(x["code"], {})
        prev = q.get("f18"); opn = q.get("f17"); close = q.get("f2")
        chg = q.get("f3"); hi = q.get("f15"); lo = q.get("f16")
        seal, lp = sealed_of(prev, x["code"], close)
        gap = round((opn - prev) / prev * 100, 2) if (prev and opn) else None
        oret = round((close - opn) / opn * 100, 2) if (opn and close) else None
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
        out.append(dict(
            code=x["code"], name=x["name"], cls=x.get("cls"), board=x.get("board"),
            ind=x.get("ind"), is_main=x.get("is_main"), rating=x.get("rating"),
            reco=x.get("reco_score"), reco_grade=x.get("reco_grade"),
            six=x.get("six_dims"), grp=x.get("cls_name"), note=x.get("note"),
            prev=prev, open=opn, close=close, hi=hi, lo=lo,
            chg=chg, lp=lp, sealed=seal, status=status, gap=gap, oret=oret,
            amt=(q.get("f6") or 0) / 1e8, hs=q.get("f8"),
        ))
    return {
        "label": label,                 # 实盘日
        "base_date": d.get("base_date"),  # 名单生成日(涨停数据基准日)
        "gen_stage": d.get("stage_name"),
        "zt_total": d.get("zt_total"),
        "zb_total": d.get("zb_total"),
        "mood": d.get("mood"),
        "n": len(out),
        "rows": out,
    }


def save_review(rep, out_json, keep_dated=True):
    """写复盘数据集; keep_dated=True 时同时按日留存副本(供跨日累积分析)。"""
    json.dump(rep, open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    hist = keep_history(out_json, rep["label"]) if keep_dated else None
    return hist


def _n(v, fmt=".2f", dash="--"):
    return format(v, fmt) if v is not None else dash


def print_table(rep):
    out = rep["rows"]
    print(f"\n{'代码':<7}{'名称':<8}{'类':<3}{'昨板':<5}{'今开':<8}{'高开%':<7}{'收盘':<8}{'涨跌%':<7}{'结果':<7}{'开买盈亏':<9}{'额亿'}")
    print("-" * 92)
    for r in out:
        print(f"{r['code']:<7}{r['name']:<8}{r['cls'] or '':<3}{str(r['board'] or ''):<5}"
              f"{_n(r['open'], '.2f'):<8}{_n(r['gap'], '+.1f'):<7}{_n(r['close'], '.2f'):<8}"
              f"{_n(r['chg'], '+.1f'):<7}{r['status']:<7}{_n(r['oret'], '+.1f'):<9}{r['amt']:.1f}")


def summarize(rep):
    out = rep["rows"]
    n = len(out)
    seal = [r for r in out if r["sealed"]]
    up = [r for r in out if (r["chg"] or 0) > 0 and not r["sealed"]]
    dn = [r for r in out if (r["chg"] or 0) < 0]
    dt = [r for r in out if (r["chg"] or 0) <= -9.5]
    ores = [r["oret"] for r in out if r["oret"] is not None]
    avg = sum(ores) / len(ores) if ores else 0
    return {"n": n, "seal": len(seal), "red": len(seal) + len(up),
            "down": len(dn), "dt": len(dt), "avg_oret": round(avg, 2)}


def main():
    watch_json, label, out_json = sys.argv[1], sys.argv[2], sys.argv[3]
    if not os.path.isabs(out_json):
        out_json = os.path.join(HERE, out_json)
    watch_json = watch_json if os.path.isabs(watch_json) else os.path.join(HERE, watch_json)
    rep = build_rows(watch_json, label)
    hist = save_review(rep, out_json)
    print("基准: %s  名单 %d 只 → 行情落盘 %s" % (rep.get("base_date"), rep["n"], out_json))
    if hist:
        print("历史副本:", hist)
    print_table(rep)
    s = summarize(rep)
    print(f"\n统计: 晋级 {s['seal']}/{s['n']} | 红盘(含晋级) {s['red']}/{s['n']} | "
          f"下跌 {s['down']} | 跌停 {s['dt']} | 竞价开盘买入→收盘均值 {s['avg_oret']:+.2f}%")


if __name__ == "__main__":
    main()
