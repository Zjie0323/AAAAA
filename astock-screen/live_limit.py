# -*- coding: utf-8 -*-
"""今日真实涨停板抓取 + 打板聚焦报告。

数据源: 东方财富官方「涨停板专题池」(push2ex/getTopicZTPool) —— 权威涨停名单。
  * 旧版曾用 clist(fs="m:0+t:2,m:1+t:2") + fid=f3, 那实际是「沪深主板全部A股按涨幅排序」,
    **不含任何涨停过滤**, 导致名单混入未涨停甚至下跌的票(1846条里仅27只真涨停)。已废弃。
  * 新版直接取专题池, 并使用官方连板数 lbc、封单额 fund(元)、开板次数 zbc。

用法: python live_limit.py            # 有缓存则用缓存
      python live_limit.py --fetch    # 强制重新抓取
"""
import sys, json, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from em import topic_pool, to_f, keep_history, mood   # mood 统一从 em 导入(单一事实源)

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "live_raw.json")

_lu_path = os.path.join(HERE, "limit_up.json")
LU = json.load(open(_lu_path, encoding="utf-8")) if os.path.exists(_lu_path) else []
meta = {r["code"]: r for r in LU}          # 历史涨停基因池(可选增强, 不再做硬过滤)


def normalize(r):
    """专题池原始行 -> 归一化行(保留 f12/f14/f2/f3/f100 兼容别名)。"""
    p = to_f(r.get("p"))
    zttj = r.get("zttj") or {}
    return {
        # 兼容别名(下游 gen_tomorrow.py 沿用)
        "f12": r.get("c"), "f14": (r.get("n") or "").replace(" ", ""),
        "f2": (p / 1000.0) if p else None, "f3": to_f(r.get("zdp")),
        "f100": r.get("hybk"),
        # 官方权威字段
        "mkt": r.get("m"),
        "lbc": r.get("lbc") or 1,               # 连板数(官方)
        "zbc": r.get("zbc") or 0,               # 今日开板(炸板)次数
        "fbt": r.get("fbt"),                    # 首次封板时间 HHMMSS
        "lbt": r.get("lbt"),                    # 最后封板时间
        "fund": to_f(r.get("fund")) or 0.0,     # 封单额(元)
        "seal_yi": (to_f(r.get("fund")) or 0.0) / 1e8,   # 封单额(亿元)
        "hs": to_f(r.get("hs")),                # 换手率%
        "ltsz": to_f(r.get("ltsz")),            # 流通市值(元)
        "amount": to_f(r.get("amount")),        # 成交额(元)
        "zt_days": zttj.get("days"), "zt_ct": zttj.get("ct"),   # N天M板
    }


def fetch_all(force=False, date=None):
    if (not force) and os.path.exists(RAW):
        print("使用缓存 live_raw.json")
        return
    d = date or time.strftime("%Y%m%d")
    print("抓取涨停池 date=%s ..." % d)
    tc, pool = topic_pool("zt", date=d)
    rows = [normalize(r) for r in pool if r.get("c")]
    # 炸板池: 曾涨停后开板, 是当日情绪强弱的重要温度计
    try:
        zb_tc, _ = topic_pool("zb", date=d)
    except Exception as e:
        print("  炸板池获取失败(忽略):", e)
        zb_tc = None
    _ts = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump({"tc": tc, "total": tc, "zb_tc": zb_tc, "date": d,
               "rows": rows, "ts": _ts},
              open(RAW, "w", encoding="utf-8"), ensure_ascii=False)
    print("真实涨停家数 tc=%s  实际入库=%s  炸板数=%s" % (tc, len(rows), zb_tc))
    # 按日留存历史副本(复盘用): live_raw.json -> live_raw_YYYY-MM-DD.json
    _hist = keep_history(RAW, _ts[:10])
    print("历史副本:", _hist or "留存失败(忽略)")
    if tc == 0:
        print("!! 涨停池为空: 可能是非交易日, 或当日数据尚未生成。")


# mood() 已上移到 em.py 作为单一事实源(涨停家数 × 炸板率), 本文件按原语义调用


def grade(m):
    """历史股性可参与评级(依赖基因池, 缺失则未知)。"""
    if not m:
        return ("新面孔", "#7f8c8d")
    if m["one_ratio"] <= 0.15 and m["recent30"] >= 2:
        return ("优", "#1e8449")
    if m["one_ratio"] <= 0.30 and m["recent30"] >= 1:
        return ("良", "#2e86c1")
    if m["one_ratio"] <= 0.50:
        return ("中", "#b9770e")
    return ("差", "#c0392b")


def fbt_str(v):
    if not v:
        return "—"
    s = str(int(v)).zfill(6)
    return "%s:%s:%s" % (s[:2], s[2:4], s[4:6])


def advice(r):
    """基于官方封板质量给实战策略。"""
    if r["zbc"] >= 3:
        return "今日反复开板(%d次), 封板质量差, 明日易炸, 规避" % r["zbc"]
    if r["board"] >= 5:
        return "超高位板, 盈亏比差, 只看不做作情绪锚"
    if r["board"] >= 3:
        return "高位连板, 只打换手回封, 破板即走"
    if r["fbt"] and int(r["fbt"]) <= 93500 and r["zbc"] == 0:
        return "早盘一字/秒板且未开板, 封单强但难买, 只作情绪锚"
    if r["zbc"] >= 1:
        return "曾开板后回封, 换手充分, 明日可竞价参与"
    if (r["m"] or {}).get("recent30", 0) >= 4:
        return "近期极活, 打首/二板, 冲高不板即兑现"
    return "普通封板, 仅板块共振时参与"


def main():
    fetch_all(force="--fetch" in sys.argv)
    raw = json.load(open(RAW, encoding="utf-8"))
    rows = raw["rows"]
    snap = raw["ts"]
    tc = raw.get("tc", len(rows))
    zb_tc = raw.get("zb_tc")

    parsed = []
    for r in rows:
        code = r.get("f12")
        if not code:
            continue
        m = meta.get(code)               # 基因池增强(可缺失)
        parsed.append(dict(
            code=code, name=r.get("f14"), ind=r.get("f100"),
            price=r.get("f2"), chg=r.get("f3"),
            board=r.get("lbc") or 1, zbc=r.get("zbc") or 0,
            fbt=r.get("fbt"), seal=r.get("seal_yi"), hs=r.get("hs"),
            ltsz=r.get("ltsz"), amount=r.get("amount"),
            zt_days=r.get("zt_days"), zt_ct=r.get("zt_ct"),
            m=m,
            max_board=(m or {}).get("max_board"),
            recent30=(m or {}).get("recent30", 0),
            one_ratio=(m or {}).get("one_ratio"),
            t1=(m or {}).get("t1_ret"),
        ))

    in_pool = sum(1 for r in parsed if r["m"])
    print("涨停 %s 只 (基因池覆盖 %s, 新面孔 %s)  炸板 %s"
          % (len(parsed), in_pool, len(parsed) - in_pool, zb_tc))
    if not parsed:
        print("今日无涨停(非交易日或数据未生成), 跳过报告生成。")
        return

    parsed.sort(key=lambda x: (-(x["board"] or 0), -(x["seal"] or 0)))
    top = parsed[:40]
    multib = sorted([r for r in parsed if r["board"] >= 2],
                    key=lambda x: (-x["board"], -(x["seal"] or 0)))

    ind_cnt = {}
    for r in parsed:
        ind_cnt[r["ind"]] = ind_cnt.get(r["ind"], 0) + 1
    top_ind = sorted(ind_cnt.items(), key=lambda x: -x[1])[:12]

    mood_name, mood_color, mood_desc = mood(tc, zb_tc)
    zb_ratio = (zb_tc / (zb_tc + tc) * 100) if (zb_tc and tc) else None

    def rows_html(rs):
        h = ""
        for r in rs:
            g, gc = grade(r["m"])
            seal_s = f"{r['seal']:.2f}亿" if r["seal"] else "—"
            hs_s = f"{r['hs']:.1f}%" if r["hs"] else "—"
            cap_s = f"{r['ltsz']/1e8:.1f}亿" if r["ltsz"] else "—"
            ztj = (f"{r['zt_days']}天{r['zt_ct']}板"
                   if r["zt_days"] and r["zt_ct"] else "—")
            zbc_s = (f"<span style='color:#c0392b;font-weight:700'>{r['zbc']}</span>"
                     if r["zbc"] else "0")
            h += (f"<tr><td><b>{r['code']}</b></td><td>{r['name']}</td>"
                  f"<td style='font-size:11px;color:#888'>{r['ind']}</td>"
                  f"<td style='text-align:center;font-weight:700;color:#c0392b'>{r['board']}</td>"
                  f"<td style='text-align:center;font-size:11px'>{ztj}</td>"
                  f"<td style='text-align:center'>{fbt_str(r['fbt'])}</td>"
                  f"<td style='text-align:center'>{zbc_s}</td>"
                  f"<td style='text-align:center'>{seal_s}</td>"
                  f"<td style='text-align:center'>{hs_s}</td>"
                  f"<td style='text-align:center'>{cap_s}</td>"
                  f"<td style='text-align:center;color:{gc};font-weight:700'>{g}</td>"
                  f"<td style='font-size:11px;color:#555'>{advice(r)}</td></tr>")
        return h

    HEAD = ("<tr><th>代码</th><th>名称</th><th>行业</th><th>连板</th><th>涨停统计</th>"
            "<th>首封时间</th><th>开板次</th><th>封单额</th><th>换手</th>"
            "<th>流通市值</th><th>历史评级</th><th>实战策略</th></tr>")

    HTML = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>今日涨停板 · 打板聚焦</title>
<style>
*{{box-sizing:border-box}} body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#f4f6f8;color:#1f2937;margin:0;padding:18px}}
.wrap{{max-width:1240px;margin:0 auto}}
.card{{background:#fff;border-radius:10px;padding:16px 18px;margin-bottom:14px;box-shadow:0 1px 4px #0001}}
h1{{font-size:19px;margin:0 0 4px}} h2{{font-size:15px;margin:0 0 10px;color:#c0392b;border-left:4px solid #c0392b;padding-left:8px}}
.sub{{color:#888;font-size:12px}}
.kpis{{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}}
.kpi{{flex:1;min-width:120px;background:#f8fafc;border:1px solid #eef;border-radius:8px;padding:10px}}
.kpi b{{display:block;font-size:20px;color:#2c3e50}} .kpi span{{font-size:11px;color:#888}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 5px;border-bottom:1px solid #eee;text-align:left}}
th{{background:#fafafa;color:#666;font-weight:600;font-size:11px}}
tr:hover{{background:#fcfcfc}}
.note{{font-size:11px;color:#999;margin-top:6px;line-height:1.6}}
.warn{{background:#fff8f0;border:1px solid #ffd9a0;border-radius:8px;padding:12px;font-size:13px;line-height:1.7}}
.ok{{background:#f0f9f4;border:1px solid #b7e4c7}}
</style></head><body><div class="wrap">
<div class="card">
  <h1>今日涨停板 · 打板聚焦</h1>
  <div class="sub">快照 {snap} · 数据源 <b>东方财富官方涨停板专题池</b>（权威涨停名单，含官方连板数/封单额/开板次数）</div>
  <div class="kpis">
    <div class="kpi"><b style="color:{mood_color}">{tc}</b><span>今日真实涨停家数</span></div>
    <div class="kpi"><b>{zb_tc if zb_tc is not None else '—'}</b><span>今日炸板数(曾涨停后开板)</span></div>
    <div class="kpi"><b>{f"{zb_ratio:.0f}%" if zb_ratio is not None else '—'}</b><span>炸板率(越高越弱)</span></div>
    <div class="kpi"><b>{len(multib)}</b><span>连板≥2(情绪高度)</span></div>
  </div>
</div>

<div class="card">
  <h2>今日盘面定性：<span style="color:{mood_color}">{mood_name}</span></h2>
  <div class="warn">真实涨停 <b>{tc}</b> 只{f"、炸板 <b>{zb_tc}</b> 只(炸板率 {zb_ratio:.0f}%)" if zb_ratio is not None else ""}。
  <b>{mood_desc}</b><br>
  判读标准：涨停&lt;30=情绪冷(打板期望差)，30~60=结构性分化(只打主线)，60~120=偏暖，&gt;120=普涨(次日必分化)。
  炸板率越高说明封板资金越不坚决，次日溢价越差。</div>
</div>

<div class="card">
  <h2>一、涨停全景（按连板高度 / 封单额排序，共 {len(parsed)} 只）</h2>
  <table><thead>{HEAD}</thead><tbody>{rows_html(top)}</tbody></table>
  <div class="note">连板数/封单额/开板次数均为<b>东财官方涨停池字段</b>（lbc / fund / zbc），非本地推算。
  「涨停统计」为 N天M板；首封时间越早（如 09:25 一字）越难买；开板次数&gt;0 说明经过换手，反而更易参与。
  历史评级来自近1年涨停基因池，「新面孔」= 不在成交额前1200的日K缓存内，无历史股性数据。</div>
</div>

<div class="card">
  <h2>二、连板高度榜（≥2 板，市场情绪高度锚）</h2>
  <table><thead>{HEAD}</thead><tbody>{rows_html(multib[:25])}</tbody></table>
</div>

<div class="card">
  <h2>三、涨停集中行业 Top（板块联动信号）</h2>
  <table><thead><tr><th>行业</th><th>涨停家数</th><th>强度</th><th>定性</th></tr></thead><tbody>
  {"".join(f"<tr><td>{i}</td><td style='text-align:center;font-weight:700'>{c}</td><td><div style='background:#c0392b;height:10px;width:{min(c/max(v for _,v in top_ind)*200,200)}px'></div></td><td style='font-size:11px;color:#666'>{'主线' if c>=5 else ('次主线' if c>=3 else '独苗/分散')}</td></tr>" for i, c in top_ind)}
  </tbody></table>
  <div class="note">同板块≥3只涨停才是真主线；独苗持续性差，次日易砸。</div>
</div>

<div class="card">
  <h2>四、盘中打板纪律（高风险）</h2>
  <div class="warn">
  • 只打换手板（开板次数≥1后回封），不赌早盘一字板（买不进，勉强排板易成隔夜炸板接盘）。<br>
  • 封单不足/反复开板(zbc≥3)不追；破板 3 分钟不回封立即走。<br>
  • 单票仓位≤10%；T+1，打板博次日情绪溢价，非隔日持有。<br>
  • 看板块≥3只涨停才是主线，独苗持续性差。<br>
  • 涨停家数&lt;30 的分化行情，只打板块龙头，其余一律不碰。
  </div>
</div>

<div class="warn"><b>免责声明</b>：本页为东财官方涨停池快照 + 历史股性统计的交叉呈现，仅供个人复盘参考，不构成投资建议，盈亏自负。</div>
</div></body></html>"""

    out = os.path.join(HERE, "limit_live.html")
    open(out, "w", encoding="utf-8").write(HTML)
    print("OK", out, len(HTML), "bytes")
    print("盘面:", mood_name, "|", mood_desc)
    print("Top8:", [(r["code"], r["name"], f"{r['board']}板",
                     f"{r['seal']:.2f}亿" if r["seal"] else "?") for r in top[:8]])


if __name__ == "__main__":
    main()
