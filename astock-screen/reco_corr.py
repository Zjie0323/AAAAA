# -*- coding: utf-8 -*-
"""六维分 reco_score × 实盘收益 相关性验证器

用法:
  python reco_corr.py                      # 自动扫 _review_YYYY-MM-DD.json 全样本
  python reco_corr.py a.json b.json ...    # 指定复盘数据集

为什么要分三个收益口径(核心):
  * chg    = 当日涨跌幅(收盘 vs 昨收)  → 「选股 / 持有」能力, 不含竞价买点成本
  * oret   = 竞价开盘买入 → 收盘       → 「竞价买点」能力, 含追高成本
  * sealed = 是否晋级涨停              → 打板胜率
六维分如果真的有效, 至少要在 chg / sealed 上体现区分度;
若只在 oret 上无区分度而 chg 上有, 说明「选股没问题, 拖累来自买点」。

被 gen_review.py 引用, 作为复盘固定章节输出。
"""
import glob, json, math, os, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
PAT = "_review_2???-??-??.json"

DIMS = ["board", "seal", "main", "zbc", "ltsz", "amt"]
DIM_CN = {"board": "连板高度", "seal": "封单强度", "main": "主线板块",
          "zbc": "封板质量", "ltsz": "流通盘适配", "amt": "量能健康"}
GRADES = ["S", "A", "B", "C"]
GRADE_HINT = {"S": "≥75", "A": "60~75", "B": "45~60", "C": "<45"}


# ---------- 基础统计 ----------
def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _rank(vs):
    idx = sorted(range(len(vs)), key=lambda i: vs[i])
    r = [0.0] * len(vs)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and vs[idx[j + 1]] == vs[idx[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[idx[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    if len(xs) < 3:
        return None
    return pearson(_rank(xs), _rank(ys))


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _stat(rows):
    n = len(rows)
    if not n:
        return None
    chgs = [r["chg"] for r in rows]
    ors = [r["oret"] for r in rows if r["oret"] is not None]
    seal = sum(1 for r in rows if r["sealed"])
    red = sum(1 for r in rows if (r["chg"] or 0) > 0)
    return {"n": n, "seal": round(seal / n * 100, 1), "red": round(red / n * 100, 1),
            "chg": round(sum(chgs) / n, 2),
            "oret": (round(sum(ors) / len(ors), 2) if ors else None)}


# ---------- 样本装载 ----------
def load_samples(paths=None):
    if not paths:
        paths = sorted(glob.glob(os.path.join(HERE, PAT)))
    samples, days = [], []
    for p in paths:
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        day = d.get("label") or os.path.basename(p)[8:18]
        days.append(day)
        for r in d.get("rows", []):
            if r.get("reco") is None or r.get("chg") is None:
                continue
            r = dict(r)
            r["day"] = day
            samples.append(r)
    return samples, sorted(set(days))


# ---------- 主分析 ----------
def analyze(samples, days=None):
    days = days if days is not None else sorted({s["day"] for s in samples})
    res = {"n_total": len(samples), "days": days, "overall": _stat(samples),
           "grades": [], "dims": [], "by_day": [], "corr": {}}

    # 1) 按评级分档
    for g in GRADES:
        gs = [s for s in samples if (s.get("reco_grade") or "?") == g]
        st = _stat(gs)
        if st:
            st["grade"] = g
            st["hint"] = GRADE_HINT[g]
            res["grades"].append(st)

    # 2) 相关性(整体)
    recos = [s["reco"] for s in samples]
    chgs = [s["chg"] for s in samples]
    ors = [(s["reco"], s["oret"]) for s in samples if s["oret"] is not None]
    res["corr"] = {
        "n": len(samples),
        "pearson_chg": _r(pearson(recos, chgs), 3),
        "spearman_chg": _r(spearman(recos, chgs), 3),
        "pearson_oret": _r(pearson([a for a, _ in ors], [b for _, b in ors]), 3) if len(ors) >= 3 else None,
    }

    # 3) 六维单维拆解(中位数分组, 比较高低组的 chg / 晋级率)
    for d in DIMS:
        vs = [(s["six"][d], s) for s in samples
              if isinstance(s.get("six"), dict) and s["six"].get(d) is not None]
        if len(vs) < 6:
            continue
        xs = [a for a, _ in vs]
        med = sorted(xs)[len(xs) // 2]
        hi = [s for a, s in vs if a > med]
        lo = [s for a, s in vs if a <= med]
        mode_share = round(max(xs.count(v) for v in set(xs)) / len(xs) * 100, 1)
        if min(len(hi), len(lo)) < 3:          # 取值集中 → 退化为均值分组
            mean = sum(xs) / len(xs)
            hi = [s for a, s in vs if a >= mean]
            lo = [s for a, s in vs if a < mean]
        base = {"dim": d, "cn": DIM_CN[d], "n": len(vs), "hi_med": med,
                "pearson_chg": _r(pearson(xs, [s["chg"] for _, s in vs]), 3),
                "top_share": mode_share}
        if min(len(hi), len(lo)) < 3:          # 全为同一取值, 该维无区分度
            res["dims"].append(dict(base, flat_dim=True, hi=None, lo=None, edge=None))
            continue
        sh, sl = _stat(hi), _stat(lo)
        res["dims"].append(dict(base, flat_dim=False, hi=sh, lo=sl,
                                edge=(_r(sh["chg"] - sl["chg"]) if (sh and sl) else None)))

    # 4) 按日分层(检验稳定性)
    for day in days:
        ds = [s for s in samples if s["day"] == day]
        st = _stat(ds)
        if not st:
            continue
        st["day"] = day
        st["pearson_chg"] = _r(pearson([s["reco"] for s in ds], [s["chg"] for s in ds]), 3)
        res["by_day"].append(st)
    return res


# ---------- 文本输出 ----------
def format_text(res):
    L = []
    L.append("=" * 72)
    L.append("六维分 reco_score × 实盘收益 相关性验证   样本 %d 只 / %d 个复盘日"
             % (res["n_total"], len(res["days"])))
    L.append("=" * 72)
    o = res["overall"]
    if o:
        L.append("全样本: 晋级率 %.1f%% | 红盘率 %.1f%% | 平均chg %+.2f%% | 平均开买 %s"
                 % (o["seal"], o["red"], o["chg"],
                    ("%+.2f%%" % o["oret"]) if o["oret"] is not None else "--"))
    c = res["corr"]
    L.append("\n[1] 整体相关性 (样本 %d)" % c.get("n", 0))
    L.append("    pearson (六维分 vs 当日涨跌幅) = %s   <- 选股/持有能力" % c.get("pearson_chg"))
    L.append("    spearman(六维分 vs 当日涨跌幅) = %s   <- 秩相关, 抗极值" % c.get("spearman_chg"))
    L.append("    pearson (六维分 vs 竞价开买收益) = %s   <- 竞价买点能力" % c.get("pearson_oret"))

    L.append("\n[2] 按评级分档")
    L.append("    %-4s %-9s %5s %8s %8s %9s %8s" % ("评级", "分数区间", "样本", "晋级率", "红盘率", "平均chg", "平均开买"))
    for g in res["grades"]:
        L.append("    %-4s %-9s %5d %7.1f%% %7.1f%% %+8.2f%% %s"
                 % (g["grade"], g["hint"], g["n"], g["seal"], g["red"], g["chg"],
                    ("%+.2f%%" % g["oret"]) if g["oret"] is not None else "--"))

    L.append("\n[3] 六维单维拆解 (按中位数分高低组)")
    L.append("    %-10s %12s %11s %11s %9s %10s" % ("维度", "相关(pearson)", "高分组chg", "低分组chg", "差值", "取值集中度"))
    for d in res["dims"]:
        flag = "  <- 该维无区分度(样本集中)" if (d.get("flat_dim") or (d.get("top_share") or 0) >= 60) else ""
        L.append("    %-10s %12s %11s %11s %9s %8.1f%%%s"
                 % (d["cn"], d["pearson_chg"],
                    "%+.2f%%" % d["hi"]["chg"] if d["hi"] else "--",
                    "%+.2f%%" % d["lo"]["chg"] if d["lo"] else "--",
                    "%+.2f%%" % d["edge"] if d["edge"] is not None else "--",
                    d.get("top_share") or 0, flag))

    L.append("\n[4] 按日分层 (检验是否稳定)")
    L.append("    %-12s %5s %8s %9s %9s" % ("实盘日", "样本", "晋级率", "平均chg", "相关"))
    for d in res["by_day"]:
        L.append("    %-12s %5d %7.1f%% %+8.2f%% %9s"
                 % (d["day"], d["n"], d["seal"], d["chg"], d["pearson_chg"]))
    return "\n".join(L)


# ---------- HTML 片段(供复盘报告嵌入) ----------
def _cls(v, good_high=True):
    if v is None:
        return "flat"
    return "up" if v > 0 else ("dn" if v < 0 else "flat")


def html_block(res):
    o = res["overall"] or {}
    c = res["corr"]
    pc, sc, po = c.get("pearson_chg"), c.get("spearman_chg"), c.get("pearson_oret")

    def corr_tag(v):
        if v is None:
            return "<span class='flat'>样本不足</span>"
        if abs(v) < 0.1:
            return "<b class='flat'>%.3f · 无区分度</b>" % v
        return "<b class='%s'>%.3f · %s</b>" % (
            "up" if v > 0 else "dn", v, "正相关" if v > 0 else "负相关")

    rows_g = "".join(
        "<tr><td><b>%s</b></td><td class='num'>%s</td><td class='num'>%d</td>"
        "<td class='num up'>%.1f%%</td><td class='num up'>%.1f%%</td>"
        "<td class='num %s'>%+.2f%%</td><td class='num %s'>%s</td></tr>"
        % (g["grade"], g["hint"], g["n"], g["seal"], g["red"],
           _cls(g["chg"]), g["chg"], _cls(g["oret"]),
           ("%+.2f%%" % g["oret"]) if g["oret"] is not None else "--")
        for g in res["grades"])

    rows_d = ""
    for d in res["dims"]:
        ts = d.get("top_share") or 0
        tag = ("<span class='tag flat'>%.0f%% 取值集中</span>" % ts
               if (d.get("flat_dim") or ts >= 60) else "%.0f%%" % ts)
        rows_d += ("<tr><td class='nm'>%s</td><td class='num'>%s</td>"
                   "<td class='num %s'>%s</td><td class='num %s'>%s</td>"
                   "<td class='num %s'>%s</td><td class='num'>%s</td></tr>"
                   % (d["cn"], corr_tag(d["pearson_chg"]),
                      _cls(d["hi"]["chg"]) if d["hi"] else "flat",
                      ("%+.2f%%" % d["hi"]["chg"]) if d["hi"] else "--",
                      _cls(d["lo"]["chg"]) if d["lo"] else "flat",
                      ("%+.2f%%" % d["lo"]["chg"]) if d["lo"] else "--",
                      _cls(d["edge"]), ("%+.2f%%" % d["edge"]) if d["edge"] is not None else "--",
                      tag))

    rows_bd = "".join(
        "<tr><td>%s</td><td class='num'>%d</td><td class='num up'>%.1f%%</td>"
        "<td class='num'>%d</td><td class='num %s'>%+.2f%%</td><td class='num'>%s</td></tr>"
        % (d["day"], d["n"], d["seal"], d["red"] if "red" in d else 0,
           _cls(d["chg"]), d["chg"], d["pearson_chg"])
        for d in res["by_day"])

    return """
<h2>六、六维分 reco_score × 实盘收益 相关性验证<span class="sub">累积样本 %d 只 / %d 个复盘日 · 复盘固定分析项</span></h2>
<div class="cards">
<div class="card"><b class="%s">%s</b><span>六维分 × 当日涨跌幅<br>pearson(选股能力)</span></div>
<div class="card"><b class="%s">%s</b><span>六维分 × 当日涨跌幅<br>spearman(秩相关)</span></div>
<div class="card"><b class="%s">%s</b><span>六维分 × 竞价开买收益<br>pearson(买点能力)</span></div>
<div class="card"><b>%.1f%%</b><span>全样本晋级率<br>n=%d</span></div>
<div class="card"><b class="%s">%+.2f%%</b><span>全样本平均涨跌幅<br>平均开买 %s</span></div>
</div>
<table><thead><tr><th>评级</th><th>分数区间</th><th>样本</th><th>晋级率</th><th>红盘率</th><th>平均涨跌幅</th><th>平均开买收益</th></tr></thead>
<tbody>%s</tbody></table>
<div class="verdict"><b>评级分档解读：</b>若各档「平均涨跌幅」单调递减/递增，说明六维分有区分度；若各档数值接近甚至倒挂，说明六维分目前<b>不能预测次日收益</b>，只能作为描述性标签。</div>
<h2>七、六维单维有效性拆解<span class="sub">按各维中位数分高低组 · 看哪一维真正有边际</span></h2>
<table><thead><tr><th>维度</th><th>相关性(pearson vs 涨跌幅)</th><th>高分组平均涨跌幅</th><th>低分组平均涨跌幅</th><th>高低组差值</th><th>取值集中度</th></tr></thead>
<tbody>%s</tbody></table>
<div class="verdict"><b>用法：</b>把「差值」为正且相关为正的维度权重保留/放大，长期为负的维度应<b>降权或反向使用</b>（例如若「封单强度」高分组反而跌得更多，说明大封单=一致性过高=次日兑现压力大）。</div>
<h2>八、按日分层 · 相关性稳定性<span class="sub">单日样本小，需看方向是否长期一致</span></h2>
<table><thead><tr><th>实盘日</th><th>样本</th><th>晋级率</th><th>红盘数</th><th>平均涨跌幅</th><th>当日相关(六维 vs 涨跌幅)</th></tr></thead>
<tbody>%s</tbody></table>
<div class="note">⚠️ 相关性不等于因果：样本量小、且受当日情绪(退潮/高潮)强混淆，仅作权重调整参考，不构成投资建议。</div>
""" % (res["n_total"], len(res["days"]),
       *[_cls(pc) if pc is not None else "flat", corr_tag(pc)],
       *[_cls(sc) if sc is not None else "flat", corr_tag(sc)],
       *[_cls(po) if po is not None else "flat", corr_tag(po)],
       (o.get("seal") or 0), res["n_total"],
       _cls(o.get("chg")), (o.get("chg") or 0),
       ("%+.2f%%" % o["oret"]) if o.get("oret") is not None else "--",
       rows_g, rows_d, rows_bd)


if __name__ == "__main__":
    args = sys.argv[1:]
    smp, dys = load_samples(args or None)
    if not smp:
        print("!! 未找到任何 _review_YYYY-MM-DD.json 样本; 请先跑 review_compare.py / gen_review.py 生成。")
        sys.exit(0)
    r = analyze(smp, dys)
    print(format_text(r))
