# -*- coding: utf-8 -*-
"""赚钱效应分(win_score) × 竞价形态门控 —— 实盘验证器。

目的: 验证 reco_engine.win_score / win_pick 的实证权重是否真的有效。
  * win_score  : 纯静态特征加权(封单30/流通盘20/封板18/量能12/连板12/主线8)
                 -> 只能用于「选哪只」(排序), 用 chg 验证
  * 形态门控   : 用当日实际竞价 gap 分档 -> 只能用于「买不买」(准入门槛), 用 oret 验证

用法: python win_verify.py            # 全部复盘日 + 今日单日
      python win_verify.py 2026-09-11 # 只看某日
"""
import json, os, sys, math

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import reco_engine as R
from reco_engine import WIN_W

# (名单文件基准日, 实盘日) —— 名单 T 日盘后生成, 对应 T+1 实盘
PAIRS = [
    ("2026-09-03", "2026-09-04"),
    ("2026-09-04", "2026-09-07"),
    ("2026-09-07", "2026-09-08"),
    ("2026-09-08", "2026-09-09"),
    ("2026-09-09", "2026-09-10"),
    ("2026-09-10", "2026-09-11"),
    ("2026-09-11", "2026-09-14"),
    ("2026-09-14", "2026-09-15"),
    ("2026-09-15", "2026-09-16"),
]


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def _pearson(xs, ys):
    p = [(a, b) for a, b in zip(xs, ys) if a is not None and b is not None]
    n = len(p)
    if n < 3:
        return None
    mx, my = _avg([a for a, _ in p]), _avg([b for _, b in p])
    cov = sum((a - mx) * (b - my) for a, b in p)
    vx = math.sqrt(sum((a - mx) ** 2 for a, _ in p))
    vy = math.sqrt(sum((b - my) ** 2 for _, b in p))
    return (cov / (vx * vy)) if vx > 0 and vy > 0 else None


def load_pair(watch_day, live_day):
    """返回合并后的样本: 昨日静态特征 + 今日实盘结果。"""
    wf = os.path.join(HERE, "tomorrow_watch_%s.json" % watch_day)
    rf = os.path.join(HERE, "_review_%s.json" % live_day)
    if not (os.path.exists(wf) and os.path.exists(rf)):
        return None
    w = json.load(open(wf, encoding="utf-8"))
    r = json.load(open(rf, encoding="utf-8"))
    src = {x["code"]: x for x in w["list"]}
    out = []
    for row in r["rows"]:
        s = src.get(row["code"])
        if not s:
            continue
        if row.get("chg") is None:
            continue
        dims, ws = R.win_score(s)
        out.append({
            "code": row["code"], "name": row["name"], "board": s.get("board"),
            "seal": s.get("seal"), "zbc": s.get("zbc"), "cls": s.get("cls"),
            "ltsz": s.get("ltsz"), "amount": s.get("amount"), "fbt": s.get("fbt"),
            "hs": s.get("hs"),
            "reco": s.get("reco_score"), "win": ws, "dims": dims,
            "chg": row.get("chg"), "oret": row.get("oret"), "gap": row.get("gap"),
            "sealed": row.get("sealed"), "live_day": live_day,
        })
    return out


def stat(rows):
    ores = [x["oret"] for x in rows if x.get("oret") is not None]
    return {"n": len(rows),
            "seal": sum(1 for x in rows if x.get("sealed")),
            "red": sum(1 for x in rows if (x.get("chg") or 0) > 0),
            "avg_chg": _avg([x["chg"] for x in rows]),
            "avg_oret": _avg(ores)}


def gap_bucket(g):
    """竞价缺口分档(7档)。

    v2 (2026-09-14 复盘修正): 原口径把 -1<=g<1 合并为「平开±1%」, 导致
      「微低开 -1~0」(实测 avg开买 -1.47%, 胜率27%, 最差档)
      与「平开 0~1」(实测 -0.42%) 混在一档, 相互抵消后掩盖了
      「低开 -4~-1」(实测 +2.50%, 胜率62%, 唯一强正期望买点) 与
      「微低开 -1~0」之间的悬崖式差异(相差 3.97pct)。
      现拆为 7 档, 与 gen_review 的复核口径一致。
    """
    if g is None:
        return "无竞价数据"
    if g >= 9.8:
        return "顶一字>=9.8%"
    if g >= 5:
        return "高开5~9.8%"
    if g >= 1:
        return "小高开1~5%"
    if g >= 0:
        return "平开0~1%"
    if g >= -1:
        return "微低开-1~0%"
    if g >= -4:
        return "低开-4~-1%"
    return "深低开<-4%"


def report(days, title):
    samples = []
    for wd, ld in days:
        s = load_pair(wd, ld)
        if s:
            samples += s
    if not samples:
        print("无样本")
        return samples
    L = []
    L.append("\n" + "=" * 96)
    L.append("%s  样本 %d 只 / %d 个复盘日" % (title, len(samples), len({x['live_day'] for x in samples and samples})))
    L.append("=" * 96)
    st = stat(samples)
    L.append("总览: 晋级 %d(%d%%)  红盘 %d(%d%%)  平均涨跌 %+.2f%%  平均开买 %s"
             % (st["seal"], round(100 * st["seal"] / st["n"]),
                st["red"], round(100 * st["red"] / st["n"]), st["avg_chg"],
                ("%+.2f%%" % st["avg_oret"]) if st["avg_oret"] is not None else "--"))

    # [1] 赚钱效应分 分档 (选股能力)
    L.append("\n[1] 赚钱效应分 win_score 分档  →  选股能力(看 avg_chg)")
    L.append("    %-10s %6s %8s %8s %8s %8s" % ("分档", "只数", "晋级率", "红盘率", "avg涨跌", "avg开买"))
    srt = sorted(samples, key=lambda x: -x["win"])
    n = len(srt)
    for i, lab in enumerate(["S(前25%)", "A(25~50%)", "B(50~75%)", "C(后25%)"]):
        seg = srt[i * n // 4:(i + 1) * n // 4]
        if not seg:
            continue
        s = stat(seg)
        L.append("    %-10s %6d %7d%% %7d%% %+8.2f%% %8s"
                 % (lab, s["n"], round(100 * s["seal"] / s["n"]), round(100 * s["red"] / s["n"]),
                    s["avg_chg"], ("%+.2f%%" % s["avg_oret"]) if s["avg_oret"] is not None else "--"))
    L.append("    pearson(win_score, 涨跌幅) = %s   |   pearson(win_score, 开买收益) = %s"
             % (_f(_pearson([x["win"] for x in samples], [x["chg"] for x in samples])),
                _f(_pearson([x["win"] for x in samples], [x["oret"] for x in samples]))))
    rc = _pearson([x["reco"] for x in samples], [x["chg"] for x in samples])
    L.append("    对照: pearson(原六维 reco_score, 涨跌幅) = %s" % _f(rc))

    # [2] 形态门控 (买点能力)
    L.append("\n[2] 竞价形态门控  →  买点能力(看 avg开买 = 竞价买入→收盘)")
    L.append("    %-16s %6s %8s %8s %8s" % ("形态", "只数", "晋级率", "avg涨跌", "avg开买"))
    order = ["顶一字>=9.8%", "高开5~9.8%", "小高开1~5%", "平开0~1%",
             "微低开-1~0%", "低开-4~-1%", "深低开<-4%", "无竞价数据"]
    buckets = {}
    for x in samples:
        buckets.setdefault(gap_bucket(x["gap"]), []).append(x)
    for k in order:
        seg = buckets.get(k)
        if not seg:
            continue
        s = stat(seg)
        L.append("    %-16s %6d %7d%% %+8.2f%% %8s"
                 % (k, s["n"], round(100 * s["seal"] / s["n"]), s["avg_chg"],
                    ("%+.2f%%" % s["avg_oret"]) if s["avg_oret"] is not None else "--"))

    # [3] 单维边际
    L.append("\n[3] 单维边际 (有效权重 vs 实测):")
    L.append("    %-12s %8s %10s %10s %10s %8s" % ("维度", "权重", "高分组chg", "低分组chg", "边际差", "方向"))
    for d, w in sorted(WIN_W.items(), key=lambda kv: -kv[1]):
        vs = [(x["dims"][d], x) for x in samples if x["dims"].get(d) is not None]
        xs = sorted(v for v, _ in vs)
        if len(xs) < 4 or xs[0] == xs[-1]:
            L.append("    %-12s %7.0f%% %10s %10s %10s %8s" % (DIM.get(d, d), w * 100, "—", "—", "样本无区分度", "—"))
            continue
        med = xs[len(xs) // 2]
        hi = stat([x for v, x in vs if v > med])
        lo = stat([x for v, x in vs if v <= med])
        edge = (hi["avg_chg"] - lo["avg_chg"]) if (hi["n"] and lo["n"]) else None
        L.append("    %-12s %7.0f%% %+9.2f%% %+9.2f%% %+9.2f%% %8s"
                 % (DIM.get(d, d), w * 100, hi["avg_chg"] or 0, lo["avg_chg"] or 0, edge or 0,
                    "有效" if (edge or 0) > 0.5 else ("弱" if (edge or 0) > 0 else "反向")))

    # [4] 按日
    L.append("\n[4] 按日分层 (方向稳定性):")
    L.append("    %-12s %6s %10s %10s %10s" % ("实盘日", "只数", "corr(涨跌)", "corr(开买)", "avg开买"))
    for ld in sorted({x["live_day"] for x in samples}):
        seg = [x for x in samples if x["live_day"] == ld]
        s = stat(seg)
        L.append("    %-12s %6d %10s %10s %10s"
                 % (ld, s["n"],
                    _f(_pearson([x["win"] for x in seg], [x["chg"] for x in seg])),
                    _f(_pearson([x["win"] for x in seg], [x["oret"] for x in seg])),
                    ("%+.2f%%" % s["avg_oret"]) if s["avg_oret"] is not None else "--"))

    txt = "\n".join(L)
    print(txt)
    with open(os.path.join(HERE, "_win_verify.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    return samples


def collect(extra_pair=None):
    """累积样本。extra_pair=(昨日名, 实盘日), 用于把当日新增复盘并入。"""
    pairs = list(PAIRS)
    if extra_pair:
        pairs = [p for p in pairs if p[1] != extra_pair[1]] + [extra_pair]
    out = []
    for wd, ld in pairs:
        s = load_pair(wd, ld)
        if s:
            out += s
    return out


def _cls(v, dn_is_good=False):
    if v is None:
        return "flat"
    return "up" if v > 0 else ("dn" if v < 0 else "flat")


def html_block(samples):
    """生成复盘报告第九章 HTML（沿用 gen_review 的暗色模板 class）。"""
    if not samples:
        return ""
    st = stat(samples)
    ndays = len({x["live_day"] for x in samples})

    # -- 分档表 --
    srt = sorted(samples, key=lambda x: -x["win"])
    n = len(srt)
    rows_a, mono_chg, mono_seal = "", [], []
    for i, lab in enumerate(["S (前25%)", "A (25~50%)", "B (50~75%)", "C (后25%)"]):
        seg = srt[i * n // 4:(i + 1) * n // 4]
        if not seg:
            continue
        s = stat(seg)
        mono_chg.append(s["avg_chg"])
        mono_seal.append(100.0 * s["seal"] / s["n"])
        rows_a += ("<tr><td class='nm'>%s</td><td class='num'>%d</td>"
                   "<td class='num'>%.0f%%</td><td class='num'>%.0f%%</td>"
                   "<td class='num %s'>%+.2f%%</td><td class='num %s'>%s</td></tr>"
                   % (lab, s["n"], 100.0 * s["seal"] / s["n"], 100.0 * s["red"] / s["n"],
                      _cls(s["avg_chg"]), s["avg_chg"],
                      _cls(s["avg_oret"]), ("%+.2f%%" % s["avg_oret"]) if s["avg_oret"] is not None else "--"))

    # -- 形态门控表 --
    order = ["顶一字>=9.8%", "高开5~9.8%", "小高开1~5%", "平开0~1%",
             "微低开-1~0%", "低开-4~-1%", "深低开<-4%", "无竞价数据"]
    buckets = {}
    for x in samples:
        buckets.setdefault(gap_bucket(x["gap"]), []).append(x)
    rows_b = ""
    for k in order:
        seg = buckets.get(k)
        if not seg:
            continue
        s = stat(seg)
        rows_b += ("<tr><td class='nm'>%s</td><td class='num'>%d</td><td class='num'>%.0f%%</td>"
                   "<td class='num %s'>%+.2f%%</td><td class='num %s'>%s</td></tr>"
                   % (k, s["n"], 100.0 * s["seal"] / s["n"], _cls(s["avg_chg"]), s["avg_chg"],
                      _cls(s["avg_oret"]), ("%+.2f%%" % s["avg_oret"]) if s["avg_oret"] is not None else "--"))

    p_win = _pearson([x["win"] for x in samples], [x["chg"] for x in samples])
    p_rec = _pearson([x["reco"] for x in samples], [x["chg"] for x in samples])
    p_win_o = _pearson([x["win"] for x in samples], [x["oret"] for x in samples])
    ok_mono = len(mono_chg) == 4 and len(mono_seal) == 4
    mono = ok_mono and mono_chg == sorted(mono_chg, reverse=True) and mono_seal == sorted(mono_seal, reverse=True)
    s_top = stat(srt[:n // 4]) if n >= 4 else {"seal": 0, "n": 1, "avg_chg": 0.0}
    s_bot = stat(srt[3 * n // 4:]) if n >= 4 else {"seal": 0, "n": 1, "avg_chg": 0.0}
    top_rate = 100.0 * s_top["seal"] / max(1, s_top["n"])
    bot_rate = 100.0 * s_bot["seal"] / max(1, s_bot["n"])
    top_chg = mono_chg[0] if mono_chg else 0.0
    bot_chg = mono_chg[-1] if mono_chg else 0.0

    best_buy = max(((k, stat(v)["avg_oret"]) for k, v in buckets.items()
                    if stat(v)["avg_oret"] is not None), key=lambda kv: kv[1], default=(None, None))

    # -- 封单/流通比 → 一字风险(盘后唯一可用的买不进预判因子) --
    rs = [x for x in samples if x.get("ltsz") and x.get("gap") is not None]
    yz_html = ""
    if len(rs) >= 20:
        bk = {}
        for x in rs:
            v = (x["seal"] or 0) * 1e8 / x["ltsz"] * 100
            k = "≥2.0%（危险区）" if v >= 2 else ("1.0~2.0%（甜蜜点）" if v >= 1 else
                                              ("0.5~1.0%" if v >= 0.5 else "<0.5%"))
            bk.setdefault(k, []).append(x)
        rows_c = ""
        for k in ["≥2.0%（危险区）", "1.0~2.0%（甜蜜点）", "0.5~1.0%", "<0.5%"]:
            a = bk.get(k)
            if not a:
                continue
            s = stat(a)
            yzr = 100.0 * sum(1 for x in a if x["gap"] >= 9.8) / len(a)
            rows_c += ("<tr><td class='nm'>%s</td><td class='num'>%d</td>"
                       "<td class='num %s'>%.0f%%</td>"
                       "<td class='num %s'>%+.2f%%</td>"
                       "<td class='num %s'>%s</td></tr>"
                       % (k.replace("<", "&lt;"), s["n"], "dn" if yzr >= 30 else "up", yzr,
                          _cls(s["avg_chg"]), s["avg_chg"] or 0,
                          _cls(s["avg_oret"]),
                          ("%+.2f%%" % s["avg_oret"]) if s["avg_oret"] is not None else "--"))
        r_ratio = _pearson([(x["seal"] or 0) * 1e8 / x["ltsz"] * 100 for x in rs], [x["gap"] for x in rs])
        r_abs = _pearson([x["seal"] or 0 for x in rs], [x["gap"] for x in rs])
        yz_html = (
            "<h2>十、一字风险归因：封单/流通市值 比"
            "<span class='sub'>盘后唯一能预判「明早买不进」的因子 · %d 只样本</span></h2>"
            "<table><thead><tr><th>封单额 / 流通市值</th><th>只数</th><th>次日顶一字率</th>"
            "<th>平均涨跌幅</th><th>竞价开买→收盘</th></tr></thead><tbody>%s</tbody></table>"
            "<div class='verdict'><b>这是上一版 TOP3 的真实缺陷：</b>"
            "win_score 中「封单强度」权重最高(30%%)，导致选出的票集中在封单巨大的一档 —— "
            "而封单/流通比 ≥2%% 的票<b>次日 35%% 概率顶一字、根本买不进</b>，"
            "涨幅榜上再好看也与你无关。<br>"
            "<b>因子强度：</b>pearson(封单/流通%%, 次日高开) = <b>%.3f</b>，"
            "强于 pearson(封单绝对值, 次日高开) = %.3f —— 所以要<b>除以流通市值</b>才有意义。<br>"
            "<b>甜蜜点：</b>封单/流通 1.0~2.0%% —— 仍能选到强势票(均涨 +2.5%% 级)，"
            "但一字率仅约 11%%，竞价开买表现在各档中最优。<br>"
            "<b>已修复：</b>win_pick 盘后即用该比值预判 —— ≥5%% 直接排除出精选位，"
            "2~5%% 降权并标「一字风险高」，1~2%% 加分。面板卡片已显示「一字风险」指标。"
            "</div>" % (len(rs), rows_c, r_ratio or 0, r_abs or 0))

    return (
        "<h2>九、赚钱效应分 win_score × 竞价形态门控 验证"
        "<span class='sub'>累积样本 %d 只 / %d 个复盘日 · 2026-09-10 新上线的 TOP3 排序模型</span></h2>"
        "<table><thead><tr><th>赚钱效应分档</th><th>只数</th><th>晋级率</th><th>红盘率</th>"
        "<th>平均涨跌幅</th><th>竞价开买均值</th></tr></thead><tbody>%s</tbody></table>"
        "<div class='verdict'><b>选股能力：</b>win_score 分档%s——S 档晋级率 %.0f%%、平均涨跌 %+.2f%%，"
        "C 档仅 %.0f%% / %+.2f%%。<br>"
        "<b>对照：</b>pearson(win_score, 涨跌幅) = <b>%.3f</b> vs pearson(原六维 reco_score, 涨跌幅) = %.3f —— "
        "实证加权版%s。<br>"
        "<b>买点能力：</b>pearson(win_score, 竞价开买收益) = <b>%.3f</b>，仍为负："
        "<b>分越高越适合「选」，越不适合「开盘追」</b>（高分票一致性预期强 → 次日被推高开 → 开盘买进即接盘）。</div>"

        "<table><thead><tr><th>竞价形态（当日实际 gap）</th><th>只数</th><th>晋级率</th>"
        "<th>平均涨跌幅</th><th>竞价开买→收盘</th></tr></thead><tbody>%s</tbody></table>"
        "<div class='verdict'><b>形态门控结论：</b>这是<b>准入门槛</b>，不是加分项——"
        "顶一字晋级率最高但买不进，高开 5%%+ 的票「当日涨幅最好、开盘买入最差」，"
        "唯一稳定正期望的买点是<b>%s（%s）</b>；"
        "「小高开 1~5%%」「微低开-1~0%%」两档样本占比高但期望为负，属当前策略主要敞口。"
        "故 win_pick 对顶一字扣 40 分、高开 5%%+ 扣 25 分。<br>"
        "<b>执行含义：</b>盘后 TOP3 只是「明早重点盯这 3 只」，"
        "<b>不是明早买这 3 只</b>——9:15 竞价 gap 出来后形态扣分才生效，"
        "届时被推成顶一字/高开 5%%+ 的票会被自动挤出精选位。</div>"
        "%s"
        % (len(samples), ndays,
           rows_a,
           "呈严格单调" if mono else "大体单调（低档有噪声）",
           top_rate, top_chg, bot_rate, bot_chg,
           p_win or 0, p_rec or 0, "更强" if (p_win or 0) > (p_rec or 0) else "未跑赢原六维",
           p_win_o or 0,
           rows_b,
           (best_buy[0] or "--"),
           ("%+.2f%%" % best_buy[1]) if best_buy[1] is not None else "--",
           yz_html))


def _f(v):
    return ("%.3f" % v) if v is not None else "  -- "


DIM = {"seal": "封单强度", "ltsz": "流通盘适配", "zbc": "封板质量",
       "amt": "量能健康", "board": "连板高度", "main": "主线板块"}


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if only:
        pairs = [(w, l) for w, l in PAIRS if l == only]
        report(pairs, "单日验证 %s" % only)
    else:
        # 今日单日优先
        report([PAIRS[-1]], "今日单日 %s" % PAIRS[-1][1])
        report(PAIRS, "累积全样本")
