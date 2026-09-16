# -*- coding: utf-8 -*-
"""复盘报告生成器 (统一参数化版)

替代此前三份重复脚本: gen_review_html.py / gen_review_0908.py / gen_review_0910.py

用法:
  python gen_review.py <watch_json> <实盘日 YYYY-MM-DD> [--out review_xxx.html]
例:
  python gen_review.py tomorrow_watch_2026-09-09.json 2026-09-10

一次完成:
  1) 名单(昨日) × 今日收盘行情 全量对照          -> review_compare.build_rows
  2) 复盘数据集落盘 + 按日留存                    -> _review.json / _review_YYYY-MM-DD.json
  3) 晋级率/红盘率/跌停/竞价开买均值 + 竞价形态分档
  4) 六维分 reco_score × 实盘收益 相关性验证(累积全样本) -> reco_corr
  5) 生成暗色复盘报告 HTML (含明日数据化观察候选)
  6) 打印控制台摘要
"""
import json, os, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from review_compare import build_rows, save_review
import reco_corr
import win_verify
from em import mood
from reco_engine import resolve_stage


def band(g):
    """竞价形态分档(复盘核心口径)。"""
    if g is None:
        return "数据缺失"
    if g >= 9.8:
        return "顶一字区"
    if g >= 5:
        return "高开5%+"
    if g >= 0:
        return "平开~小高开"
    return "低开"


def avg(vs):
    vs = [v for v in vs if v is not None]
    return (sum(vs) / len(vs)) if vs else None


def group_stat(rows):
    ores = [r["oret"] for r in rows if r["oret"] is not None]
    return {"n": len(rows),
            "seal": sum(1 for r in rows if r["sealed"]),
            "red": sum(1 for r in rows if (r["chg"] or 0) > 0),
            "down": sum(1 for r in rows if (r["chg"] or 0) < 0),
            "dt": sum(1 for r in rows if (r["chg"] or 0) <= -9.5),
            "avg_oret": avg(ores)}


def auto_cmt(r):
    """按盘口数据自动生成点评(无需人工)。"""
    g, o, c = r.get("gap"), r.get("oret"), r.get("chg")
    b = band(g)
    if r["sealed"]:
        if b == "顶一字区":
            return "顶一字晋级 → <b>买不进</b>, 无实操意义"
        if b == "高开5%+":
            return "高开晋级但仅薄肉, 追入盈亏比差"
        return "**%s晋级 → 开买%+.1f%%**, 形态最优可实操" % (b, o or 0)
    if (c or 0) <= -9.5:
        return "**跌停闷杀**, 开买%+.1f%%" % (o or 0)
    if b == "高开5%+":
        return "高开%+.1f%% 冲高失败/诱多, 开买%+.1f%%" % (g or 0, o or 0)
    if b == "低开" and (c or 0) > 0:
        return "低开%+.1f%% 弱转强翻红, 开买%+.1f%%" % (g or 0, o or 0)
    return "%s, 收盘%+.1f%%, 开买%+.1f%%" % (b, c or 0, o or 0)


def build_candidates(lrows, topn=6):
    """从今日涨停池按机械规则提取次日观察候选(数据筛选, 非荐股)。"""
    if not lrows:
        return []
    ind_cnt = Counter((x.get("f100") or "") for x in lrows)
    out, used = [], set()

    def add(x, tag):
        if x and x.get("f12") not in used:
            used.add(x.get("f12"))
            out.append((tag, x))

    for x in sorted(lrows, key=lambda z: -(z.get("lbc") or 1))[:3]:
        add(x, "最高板")
    for x in sorted(lrows, key=lambda z: -(z.get("seal_yi") or 0))[:3]:
        add(x, "封单最强")
    for ind, c in ind_cnt.most_common():
        if c >= 3:
            cand = sorted([x for x in lrows if (x.get("f100") or "") == ind],
                          key=lambda z: (-(z.get("lbc") or 1), -(z.get("seal_yi") or 0)))
            if cand:
                add(cand[0], "抱团主线")
    return out[:topn]


def cond_text(x):
    lbc, zbc = (x.get("lbc") or 1), (x.get("zbc") or 0)
    # 买点口径 v2(2026-09-14 复盘修正, 342只/7日): 唯一强正期望买点是竞价缺口 -4%~-1%
    # (+2.50%, 开买胜率 62%); 「平开~小高开 1~5%」112 只为负(-1.18%), 「微低开-1~0%」41 只 -1.47%
    s = "只做竞价低开-4%~-1%(唯一正期望买点 +2.50%/胜率62%)；平开0~1%及小高开1~5% 观望；高开5%+、顶一字放弃"
    if lbc >= 4:
        s += "；高位标仓位减半、破板即走"
    if zbc >= 3:
        s += "；今日开板%d次, 封板质量差, 只低吸" % zbc
    return s


# ===================== HTML =====================
CSS = """
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--fg:#e2e8f0;--mut:#94a3b8;--up:#e53e3e;--dn:#16a34a;--gold:#f59e0b}
*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:14px/1.65 -apple-system,"Microsoft YaHei",sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;color:var(--gold);margin:22px 0 8px}
.sub{font-size:12px;color:var(--mut);font-weight:normal;margin-left:10px}
.meta{color:var(--mut);font-size:12px;margin-bottom:14px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 16px;min-width:110px}
.card b{font-size:20px;display:block}.card span{color:var(--mut);font-size:12px}
table{width:100%;border-collapse:collapse;background:var(--panel);border-radius:10px;overflow:hidden;margin:6px 0 14px;font-size:12.5px}
th{background:#263449;color:var(--mut);padding:7px 6px;text-align:center;font-weight:600;font-size:11.5px}
td{padding:6px 6px;text-align:center;border-top:1px solid #263449;font-variant-numeric:tabular-nums}
tr:hover td{background:#243349}.nm{text-align:left;font-weight:600}
.cmt{text-align:left;color:#cbd5e1;font-size:11.5px;max-width:340px}
.num{font-variant-numeric:tabular-nums}
.up{color:var(--up)!important}.dn{color:var(--dn)!important}.flat{color:var(--mut)}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;font-weight:600;white-space:nowrap}
.tag.up{background:rgba(229,62,62,.15);color:#f87171}.tag.down{background:rgba(22,163,74,.15);color:#4ade80}
.tag.up2{background:rgba(229,62,62,.07);color:#fca5a5}.tag.flat{background:#334155;color:#cbd5e1}
.verdict{background:var(--panel);border-left:4px solid var(--gold);border-radius:8px;padding:10px 16px;margin:10px 0}
.verdict li{margin:4px 0}.note{color:var(--mut);font-size:12px;margin-top:14px}
.tomorrow{background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.35);border-radius:10px;padding:12px 16px;margin:10px 0}
.plan{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 16px;margin:10px 0}
.plan th{background:#263449}.plan li{margin:4px 0;color:#cbd5e1}
"""


def tr_row(r, show_cmt=True):
    g, o, c, chg = r.get("gap"), r.get("oret"), r.get("close"), r.get("chg")
    gapc = "up" if (g or 0) >= 5 else ("dn" if (g or 0) < 0 else "")
    tag = "up" if r["sealed"] else ("down" if (chg or 0) <= -9.5 else
                                    ("up2" if (chg or 0) > 0 else "flat"))
    board = ("%s→%s板" % (r["board"], (r["board"] or 1) + 1)) if r["sealed"] else ("%s板" % (r["board"] or "-"))
    star = "★" if r.get("is_main") else ""
    cells = (
        "<tr><td>%s</td><td class='nm'>%s%s</td><td class='num'>%s</td><td class='num'>%s</td>"
        "<td class='num %s'>%s</td><td class='num'>%s</td><td class='num %s'>%s</td>"
        "<td><span class='tag %s'>%s</span></td><td class='num %s'>%s</td>"
        % (r["code"], r["name"], star, board,
           ("%.2f" % o) if o else "--",
           gapc, ("%+.1f%%" % g) if g is not None else "--",
           ("%.2f" % c) if c else "--",
           "up" if (chg or 0) > 0 else ("dn" if (chg or 0) < 0 else "flat"),
           ("%+.1f%%" % chg) if chg is not None else "--",
           tag, r["status"],
           "up" if (o or 0) > 0 else ("dn" if (o or 0) < 0 else "flat"),
           ("%+.1f%%" % o) if o is not None else "--"))
    if show_cmt:
        cells += "<td class='cmt'>%s</td>" % auto_cmt(r)
    return cells + "</tr>"


TBL_HEAD = ("<table><thead><tr><th>代码</th><th>名称</th><th>昨板→今</th><th>今开</th><th>高开</th>"
            "<th>收盘</th><th>涨跌</th><th>结果</th><th>竞价买入→收盘</th><th>盘口点评</th></tr></thead><tbody>")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    watch_json, label = sys.argv[1], sys.argv[2]
    out_html = os.path.join(HERE, "review_%s.html" % label.replace("-", ""))
    if "--out" in sys.argv:
        out_html = sys.argv[sys.argv.index("--out") + 1]
    if not os.path.isabs(watch_json):
        watch_json = os.path.join(HERE, watch_json)

    # 1) 对照 + 落盘留存
    print("[1/5] 拉取今日收盘行情并对照名单 ...")
    rep = build_rows(watch_json, label)
    hist = save_review(rep, os.path.join(HERE, "_review.json"))
    rows = rep["rows"]
    print("      名单 %d 只 | 基准日 %s | 留存副本 %s"
          % (rep["n"], rep.get("base_date"), os.path.basename(hist) if hist else "失败"))

    # 2) 今日盘面
    lr_path = os.path.join(HERE, "live_raw_%s.json" % label)
    lr = json.load(open(lr_path, encoding="utf-8")) if os.path.exists(lr_path) else {}
    lrows = lr.get("rows", [])
    tc, zbc = lr.get("tc"), lr.get("zb_tc")
    zb_ratio = (zbc / float(zbc + tc) * 100) if (tc and zbc is not None and (zbc + tc)) else None
    mname, mcolor, mdesc = mood(tc, zbc)
    scode, (sname, sdesc, scolor, k) = resolve_stage(tc)

    # 3) 统计
    print("[2/5] 统计与形态分档 ...")
    n = len(rows)
    ov = group_stat(rows)
    ores = [r["oret"] for r in rows if r["oret"] is not None]
    avg_oret = avg(ores) or 0
    seal_rows = sorted([r for r in rows if r["sealed"]], key=lambda z: -(z["oret"] or 0))
    lose_rows = sorted([r for r in rows if (r["chg"] or 0) <= -4], key=lambda z: (z["chg"] or 0))[:8]
    band_cnt = Counter(band(r["gap"]) for r in seal_rows)
    by_cls = {}
    for cl in ("A", "B", "C"):
        g = [r for r in rows if r["cls"] == cl]
        if g:
            by_cls[cl] = group_stat(g)
    main_g = [r for r in rows if r.get("is_main")]
    nonmain_g = [r for r in rows if not r.get("is_main")]

    # 4) 六维相关性(累积)
    print("[3/5] 六维分 × 实盘收益 相关性验证(累积样本) ...")
    smp, dys = reco_corr.load_samples()
    corr = reco_corr.analyze(smp, dys) if smp else None

    # 5) 明日候选
    cands = build_candidates(lrows)

    # ---- 拼 HTML ----
    print("[4/5] 生成 HTML ...")
    cards = [
        ("%s" % (tc if tc is not None else "--"), "今日涨停", "var(--up)"),
        ("%s" % (zbc if zbc is not None else "--"), "今日炸板", "var(--fg)"),
        ("%.0f%%" % zb_ratio if zb_ratio is not None else "--", "炸板率", "var(--dn)" if (zb_ratio or 0) >= 30 else "var(--fg)"),
        ("%s" % sname, "情绪阶段", scolor),
        ("%+.2f%%" % avg_oret, "昨日名单开买均值", "var(--dn)" if avg_oret < 0 else "var(--up)"),
    ]
    cards_html = "".join("<div class='card'><b style='color:%s'>%s</b><span>%s</span></div>" % (c, v, t)
                         for v, t, c in cards)

    band_lines = []
    for b in ("顶一字区", "高开5%+", "平开~小高开", "低开"):
        g = [r for r in seal_rows if band(r["gap"]) == b]
        if g:
            ao = avg([r["oret"] for r in g])
            band_lines.append("<li><b>%s %d 只</b>: 开买均值 <b class='%s'>%+.2f%%</b></li>"
                              % (b, len(g), "up" if (ao or 0) > 0 else "dn", ao or 0))
    if band_cnt.get("顶一字区"):
        band_lines.append("<li class='flat'>顶一字 %d 只实际<b>买不进</b>，占总晋级 %.0f%%</li>"
                          % (band_cnt["顶一字区"], band_cnt["顶一字区"] / max(1, len(seal_rows)) * 100))

    cls_lines = "".join(
        "<li><b>%s 类</b>: %d 只 · 晋级 %.0f%% · 跌停 %d · 开买均值 <b class='%s'>%+.2f%%</b></li>"
        % ({"A": "连板龙头", "B": "首板", "C": "高位锚"}.get(cl, cl), st["n"],
           st["seal"] / st["n"] * 100, st["dt"],
           "up" if (st["avg_oret"] or 0) > 0 else "dn", st["avg_oret"] or 0)
        for cl, st in by_cls.items())

    main_cmp = ""
    if main_g and nonmain_g:
        a, b = group_stat(main_g), group_stat(nonmain_g)
        main_cmp = ("<li><b>昨日主线★ %d 只 vs 非主线 %d 只</b>: 开买均值 <b class='%s'>%+.2f%%</b> vs "
                    "<b class='%s'>%+.2f%%</b>，晋级 %d vs %d</li>"
                    % (a["n"], b["n"],
                       "dn" if (a["avg_oret"] or 0) < 0 else "up", a["avg_oret"] or 0,
                       "dn" if (b["avg_oret"] or 0) < 0 else "up", b["avg_oret"] or 0,
                       a["seal"], b["seal"]))

    hist_rows = ""
    if corr:
        for d in corr["by_day"]:
            hist_rows += ("<tr><td>%s</td><td class='num'>%d</td><td class='num'>%.0f%%</td>"
                          "<td class='num'>%d</td><td class='num'>%d</td>"
                          "<td class='num %s'>%+.2f%%</td><td class='num'>%s</td></tr>"
                          % (d["day"], d["n"], d["seal"], d["red"], d["dt"] if "dt" in d else 0,
                             "dn" if d["chg"] < 0 else "up", d["chg"], d["oret"]))

    cand_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td class='nm'>%s</td><td>%s</td><td class='num'>%s</td>"
        "<td class='num'>%s</td><td class='num'>%s</td><td class='cmt'>%s</td></tr>"
        % (tag, x.get("f12"), x.get("f14"), x.get("f100") or "-",
           x.get("lbc") or 1, ("%.2f亿" % (x.get("seal_yi") or 0)), x.get("zbc") or 0,
           cond_text(x))
        for tag, x in cands)

    corr_html = reco_corr.html_block(corr) if corr else "<p>样本不足，无法做相关性分析。</p>"
    # 赚钱效应分 win_score × 竞价形态门控 验证(把当日复盘并入累积样本)
    try:
        win_html = win_verify.html_block(win_verify.collect(extra_pair=(rep.get("base_date"), label)))
    except Exception as e:
        win_html = "<h2>九、赚钱效应分 × 竞价形态门控 验证</h2><p>生成失败: %s</p>" % e

    html = ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            "<title>昨日(%s)名单 × %s 实盘复盘</title><style>%s</style></head><body>"
            "<h1>昨日(%s)竞价名单 × %s 实盘复盘</h1>"
            "<div class='meta'>生成 %s · 数据源: 东财官方涨停/炸板专题池 + 收盘行情 · "
            "全量追踪 %d 只 (%s) · 脚本 gen_review.py</div>"

            "<h2>一、今日盘面温度<span class='sub'>%s</span></h2>"
            "<div class='cards'>%s</div>"
            "<p style='color:var(--mut);font-size:13px'>%s</p>"

            "<h2>二、昨日名单全量复盘 (%d 只)</h2>"
            "<div class='cards'>"
            "<div class='card'><b style='color:var(--up)'>%d/%d</b><span>晋级涨停(%.0f%%)</span></div>"
            "<div class='card'><b>%d/%d</b><span>红盘收涨(%.0f%%)</span></div>"
            "<div class='card'><b style='color:var(--dn)'>%d / %d</b><span>下跌 / 跌停</span></div>"
            "<div class='card'><b style='color:%s'>%+.2f%%</b><span>竞价开盘买入→收盘均值</span></div>"
            "</div>"
            "<ul style='color:#cbd5e1;font-size:13px'>%s%s</ul>"

            "<h2>三、晋级 %d 只 · 按竞价形态分档</h2>%s%s</tbody></table>"
            "<div class='verdict'><b>形态分档结论：</b><ul>%s</ul></div>"

            "<h2>四、跌幅最深 / 跌停</h2>%s%s</tbody></table>"

            "<h2>五、跨日复盘累积统计</h2>"
            "<table><thead><tr><th>实盘日</th><th>样本</th><th>晋级率</th><th>红盘率</th><th>跌停</th>"
            "<th>平均涨跌幅</th><th>竞价开买均值</th></tr></thead><tbody>%s</tbody></table>"
            "<div class='verdict'><b>核心结论：</b>名单本身<b>不是买入信号</b>——只有叠加「平开/小高开/低开」形态筛选才有正收益；"
            "退潮期跟昨日主线做接力是最大亏损来源。</div>"

            "%s"

            "%s"

            "<h2>十一、下一交易日数据化观察候选<span class='sub'>机械筛选, 非荐股</span></h2>"
            "<div class='tomorrow'><table><thead><tr><th>入选理由</th><th>代码</th><th>名称</th><th>板块</th>"
            "<th>连板</th><th>封单</th><th>开板次数</th><th>竞价参与条件</th></tr></thead>"
            "<tbody>%s</tbody></table>"
            "<p style='margin:8px 0 0;color:var(--mut);font-size:12.5px'><b>执行纪律：</b>"
            "① 先看盘面: 涨停家数 &lt;30 或炸板率 &ge;35%% 直接休息不开仓；"
            "② 只买平开~小高开(0~4%%)或低开翻红的票，<b>高开5%%+、顶一字坚决不追</b>；"
            "③ 回避昨日主线的高位接力票；④ <b>绝不补仓摊平</b>，单票轻仓、破位即走。</p></div>"

            "<div class='note'>⚠️ 以上为规则化数据复盘与策略推演，非投资建议。"
            "打板为 T+1 博次日溢价的高风险博弈，务必单票轻仓、严格止损。</div>"
            "</body></html>"
            % (rep.get("base_date"), label, CSS,
               rep.get("base_date"), label,
               __import__("time").strftime("%Y-%m-%d %H:%M"), n,
               os.path.basename(watch_json),
               mname, cards_html, mdesc,
               n, ov["seal"], n, ov["seal"] / n * 100,
               ov["red"], n, ov["red"] / n * 100,
               ov["down"], ov["dt"],
               "dn" if avg_oret < 0 else "up", avg_oret,
               cls_lines, main_cmp,
               len(seal_rows), TBL_HEAD,
               "".join(tr_row(r) for r in seal_rows),
               "".join(band_lines),
               TBL_HEAD, "".join(tr_row(r) for r in lose_rows),
               hist_rows,
               corr_html,
               win_html,
               cand_rows))

    open(out_html, "w", encoding="utf-8").write(html)
    print("[5/5] 已写出 %s (%d bytes)" % (out_html, len(html)))

    # ---- 控制台摘要 ----
    print("\n" + "=" * 60)
    print("今日 %s | 涨停 %s 炸板 %s 炸板率 %s | 情绪 %s(%s)"
          % (label, tc, zbc, ("%.0f%%" % zb_ratio) if zb_ratio is not None else "--", mname, sname))
    print("名单 %d 只: 晋级 %d(%.0f%%) 红盘 %d(%.0f%%) 跌停 %d | 竞价开买均值 %+.2f%%"
          % (n, ov["seal"], ov["seal"] / n * 100, ov["red"], ov["red"] / n * 100, ov["dt"], avg_oret))
    print("形态分档(晋级): " + " | ".join("%s %d" % (b, c) for b, c in band_cnt.most_common()))
    if corr:
        c = corr["corr"]
        print("六维相关性(累积 %d 只): pearson(涨跌幅)=%s spearman=%s pearson(开买)=%s"
              % (corr["n_total"], c["pearson_chg"], c["spearman_chg"], c["pearson_oret"]))
    print("=" * 60)


if __name__ == "__main__":
    main()
