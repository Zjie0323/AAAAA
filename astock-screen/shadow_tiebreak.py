# -*- coding: utf-8 -*-
"""
影子对比: 智能推荐 score_pool 排序 tiebreaker 方案优劣.

背景
----
用户反馈「智能推荐一个晋级的都没有」. 定位到 score_pool 排序键为
    (score, camount)  # camount = 封单金额绝对值
而封单额绝对值是弱因子(实测 r≈0.21), 封单/流通比是更强的相对量纲因子(r≈0.32).
早期 parse_pool_item 漏解析 ltsz, 导致比值根本算不出来(已修复).

本脚本用 N 个复盘日, 让各候选排序键在同一批"当日涨停池"上打擂台:
    全池每只票 -> 打分 -> 各 tiebreaker 排序 -> 取 top5/top10
    -> 看次日「晋级率」(进次日涨停池) 与「触板率」(进次日涨停池 ∪ 炸板池)

数据源: 东财 push2ex 专题池 (zt/zb), 逐日一次请求, 结果落 _phist/ 缓存可断点续跑.
输出: _shadow_tiebreak_result.txt (运行时产物, 不入库)
"""
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "_phist")
OUT = os.path.join(HERE, "_shadow_tiebreak_result.txt")
REALTIME = os.path.join(HERE, "..", "astock-realtime")

sys.path.insert(0, HERE)
import em  # noqa: E402

# server.py 是以「文件路径」exec_module 载入的, 它内部的普通 import(如 em_changes)
# 要靠 sys.path 解析. 不把 astock-realtime 加进来, 这些 import 会静默降级为 None.
if REALTIME not in sys.path:
    sys.path.insert(0, REALTIME)

# ---- 载入 astock-realtime/server.py 的解析与打分逻辑(唯一真源, 不复制实现) ----
spec = importlib.util.spec_from_file_location("rt_server", os.path.join(REALTIME, "server.py"))
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)

DAYS = ["2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08", "2026-09-09",
        "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]
NEXT = {"2026-09-03": "2026-09-04", "2026-09-04": "2026-09-07",
        "2026-09-07": "2026-09-08", "2026-09-08": "2026-09-09",
        "2026-09-09": "2026-09-10", "2026-09-10": "2026-09-11",
        "2026-09-11": "2026-09-14", "2026-09-14": "2026-09-15",
        "2026-09-15": "2026-09-16"}


def ymd(d):
    return d.replace("-", "")


def cached_pool(kind, date):
    """取某日某类专题池, 命中 _phist/ 缓存则直接读, 否则抓一次并落盘."""
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, "pool_%s_%s.json" % (kind, date))
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    for attempt in (1, 2, 3):
        try:
            tc, rows = em.topic_pool(kind, date=ymd(date))
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
            time.sleep(1.3)
            return rows
        except Exception as e:
            sys.stderr.write("[warn] %s %s 第%d次失败: %s\n" % (kind, date, attempt, e))
            time.sleep(3.0 * attempt)
    return []


def load_all():
    """预取所有需要的池子: 每日涨停池 + 次日涨停/炸板池."""
    data = {}
    need = []
    for d in DAYS:
        need.append(("zt", d))
    for d in DAYS:
        n = NEXT[d]
        need.append(("zt", n))
        need.append(("zb", n))
    for kind, d in need:
        key = (kind, d)
        if key in data:
            continue
        rows = cached_pool(kind, d)
        data[key] = rows
        print("  [pool] %s %s -> %d 只" % (kind, d, len(rows)))
    return data


def codes(rows):
    return {str(r.get("c") or "") for r in rows if r.get("c")}


def sealed_at_auction(rows):
    """次日「9:25 竞价即封死」的代码集 -> 这些票当日买不进, 不具备可交易性.
    判据: 在次日涨停池 且 首封时间==9:25(92500 秒) 且 当日未炸板."""
    s = set()
    for r in rows:
        c = str(r.get("c") or "")
        if not c:
            continue
        fbt = r.get("fbt")
        zbc = r.get("zbc") or 0
        if fbt is not None and int(fbt) <= 92500 and zbc == 0:
            s.add(c)
    return s


# ---- 候选排序键 ----------------------------------------------------------
# 统一约定: 返回一个可比较的 tuple, 越大越优先
def k_old(s):
    """现行生产排序: (score, 封单额绝对值)"""
    return (s["_sc"], s.get("camount") or 0)


def k_ratio(s):
    """封单/流通比 替代封单额绝对值"""
    return (s["_sc"], s.get("seal_ratio") or 0, s.get("camount") or 0)


def k_ratio_fbt(s):
    """封单/流通比 + 早封优先"""
    fr = s.get("fbt_raw")
    fr = 999999 if fr is None else fr
    return (s["_sc"], s.get("seal_ratio") or 0, -fr)


def k_fbt(s):
    """仅早封优先(检验封板时间是否比封单更强)"""
    fr = s.get("fbt_raw")
    fr = 999999 if fr is None else fr
    return (s["_sc"], -fr)


def k_score_only(s):
    """无 tiebreaker(仅评分), 作为「tiebreaker 到底有没有用」的对照组"""
    return (s["_sc"],)


def k_ratio_noscore(s):
    """完全弃用打分, 只用封单/流通比 -- 检验打分本身是否有效"""
    return (s.get("seal_ratio") or 0,)


def k_small_cap(s):
    """仅小流通市值优先 -- 混淆检验: F 的高晋级率是否只是「小盘更强」在起作用"""
    v = s.get("ltsz")
    return (-(v if v else 9e18),)


def true_yizi(s):
    """真一字板: 开盘即以涨停价封住且全天未开板.
    判据 fbt<=09:30:00 且 zbc==0.
    ★ server.parse_pool_item 的 znh 只要 fbt==lbt 且 zbc==0 就判一字板, 会把
      「9:32 封板后死守到收盘」「10:00 封板死守」误判为一字板(实测误判率 84%),
      导致这类封板质量极高的强势票被倒扣 3 分. 此处用严格口径复核."""
    fr = s.get("fbt_raw")
    if fr is None:
        return False
    zbc = s.get("zbc")
    zbc = int(zbc) if isinstance(zbc, (int, float)) else 0
    return zbc == 0 and int(fr) <= 93000


def k_ratio_filtered(s):
    """比值 + 可交易性过滤(用现行 znh 口径) -- 原方案 H"""
    if s.get("znh"):
        return (-1, 0)
    lbc = s.get("lbc") or 1
    if lbc >= 5:
        return (-1, 0)
    return (s.get("seal_ratio") or 0,)


def k_ratio_filtered_fix(s):
    """比值 + 忽略高位/剔真一字板(严格口径) -- 修正 znh 误判后的方案"""
    if true_yizi(s):
        return (-1, 0)
    lbc = s.get("lbc") or 1
    if lbc >= 5:
        return (-1, 0)
    return (s.get("seal_ratio") or 0,)


def k_ratio_trueyizi(s):
    """比值 + 仅剔真一字板(不限制连板高度) -- 检验「高位回避」是否真有贡献"""
    if true_yizi(s):
        return (-1, 0)
    return (s.get("seal_ratio") or 0,)


VARIANTS = [
    ("A 现行(score,封单额)", k_old),
    ("B (score,封单/流比)", k_ratio),
    ("C (score,封单/流比,早封)", k_ratio_fbt),
    ("D (score,早封)", k_fbt),
    ("E 仅score(无tiebreak)", k_score_only),
    ("F 仅封单/流比(弃打分)", k_ratio_noscore),
    ("G 仅小流通市值(混淆检验)", k_small_cap),
    ("H 比值+剔znh误判/高位", k_ratio_filtered),
    ("I 比值+剔真一字/高位", k_ratio_filtered_fix),
    ("J 比值+仅剔真一字", k_ratio_trueyizi),
]


def fisher_p(a_hit, a_n, b_hit, b_n):
    """Fisher 精确检验(单侧, 越大越优): 返回 p 值. 不依赖 scipy."""
    from math import comb
    total = a_n + b_n
    hits = a_hit + b_hit
    # 固定边际下, 观测到 >= 当前 b_hit 的概率之和
    p = 0.0
    for x in range(b_hit, min(b_n, hits) + 1):
        y = hits - x
        if y < 0 or y > a_n:
            continue
        p += comb(b_n, x) * comb(a_n, y)
    p /= comb(total, hits)
    return p


def main():
    lines = []

    def w(s=""):
        lines.append(s)
        print(s)

    w("=" * 78)
    w("智能推荐 score_pool 排序 tiebreaker 影子对比")
    w("口径: 每日取「当日涨停池」全量 -> _score_stock 打分 -> 各 tiebreaker 排序取 top5")
    w("     次日「晋级」= 进次日涨停池; 「触板」= 进次日涨停池 或 次日炸板池")
    w("=" * 78)

    data = load_all()

    agg = {name: {"p5": 0, "p5_hit": 0, "p5_touch": 0, "p10": 0, "p10_hit": 0,
                  "p10_touch": 0, "b5": 0, "bh5": 0, "b10": 0, "bh10": 0,
                  "picks": []} for name, _ in VARIANTS}
    base = {"n": 0, "hit": 0, "touch": 0, "sealed": 0}
    day_rows = []

    for d in DAYS:
        rows = data.get(("zt", d)) or []
        if not rows:
            w("[skip] %s 涨停池为空" % d)
            continue
        nxt = NEXT[d]
        n_zt = codes(data.get(("zt", nxt)) or [])
        n_zb = codes(data.get(("zb", nxt)) or [])
        n_touch = n_zt | n_zb
        n_sealed = sealed_at_auction(data.get(("zt", nxt)) or [])  # 次日竞价封死=买不进

        stocks = [rt.parse_pool_item(x) for x in rows]
        hy = {}
        for s in stocks:
            k = s.get("hybk") or "其他"
            hy[k] = hy.get(k, 0) + 1
        for s in stocks:
            s["_sc"] = rt._score_stock(s, hy)[0]

        pool_hit = sum(1 for s in stocks if s["code"] in n_zt)
        pool_touch = sum(1 for s in stocks if s["code"] in n_touch)
        base["n"] += len(stocks)
        base["hit"] += pool_hit
        base["touch"] += pool_touch
        base["sealed"] += sum(1 for s in stocks if s["code"] in n_sealed)

        line = []
        for name, keyfn in VARIANTS:
            rk = sorted(stocks, key=keyfn, reverse=True)
            t5, t10 = rk[:5], rk[:10]
            h5 = sum(1 for s in t5 if s["code"] in n_zt)
            c5 = sum(1 for s in t5 if s["code"] in n_touch)
            h10 = sum(1 for s in t10 if s["code"] in n_zt)
            c10 = sum(1 for s in t10 if s["code"] in n_touch)
            # 可买性: 次日 9:25 竞价封死 的票买不进, 不能计入可用战果
            b5 = sum(1 for s in t5 if s["code"] not in n_sealed)
            bh5 = sum(1 for s in t5 if s["code"] in n_zt and s["code"] not in n_sealed)
            b10 = sum(1 for s in t10 if s["code"] not in n_sealed)
            bh10 = sum(1 for s in t10 if s["code"] in n_zt and s["code"] not in n_sealed)
            a = agg[name]
            a["p5"] += len(t5); a["p5_hit"] += h5; a["p5_touch"] += c5
            a["p10"] += len(t10); a["p10_hit"] += h10; a["p10_touch"] += c10
            a["b5"] = a.get("b5", 0) + b5
            a["bh5"] = a.get("bh5", 0) + bh5
            a["b10"] = a.get("b10", 0) + b10
            a["bh10"] = a.get("bh10", 0) + bh10
            a["picks"].extend([(s, s["code"] in n_zt) for s in t5])
            line.append("%s %d/%d" % (name.split()[0], h5, len(t5)))
        day_rows.append((d, nxt, len(stocks), pool_hit, pool_touch, line))

    w("")
    w("-" * 78)
    w("逐日明细 (top5 晋级数/晋级率)")
    w("-" * 78)
    w("%-11s %-11s %4s %10s %10s   %s" % ("日", "次日", "池", "全池晋级", "全池触板", "各方案 top5"))
    for d, nxt, n, ph, pt, line in day_rows:
        w("%-11s %-11s %4d %6d/%2d%% %8d/%2d%%   %s"
          % (d, nxt, n, ph, round(ph * 100.0 / n), pt, round(pt * 100.0 / n),
             "  ".join(line)))

    w("")
    w("=" * 78)
    w("汇总 (%d 个交易日)" % len(day_rows))
    w("=" * 78)
    bn = base["n"] or 1
    w("全池基准: %d 只票 | 次日晋级 %d (%.1f%%) | 次日触板 %d (%.1f%%)"
      % (base["n"], base["hit"], base["hit"] * 100.0 / bn,
         base["touch"], base["touch"] * 100.0 / bn))
    w("")
    w("%-26s %14s %14s %10s" % ("方案", "top5 晋级", "top5 触板", "vs全池"))
    w("-" * 78)
    for name, _ in VARIANTS:
        a = agg[name]
        p5 = a["p5"] or 1
        r5 = a["p5_hit"] * 100.0 / p5
        t5 = a["p5_touch"] * 100.0 / p5
        lift = r5 - base["hit"] * 100.0 / bn
        w("%-26s %8d/%-3d %5.1f%% %8.1f%% %+9.1fpct"
          % (name, a["p5_hit"], p5, r5, t5, lift))

    w("")
    w("%-26s %14s %14s %10s" % ("方案", "top10 晋级", "top10 触板", "vs全池"))
    w("-" * 78)
    for name, _ in VARIANTS:
        a = agg[name]
        p10 = a["p10"] or 1
        r10 = a["p10_hit"] * 100.0 / p10
        t10 = a["p10_touch"] * 100.0 / p10
        lift = r10 - base["hit"] * 100.0 / bn
        w("%-26s %8d/%-3d %5.1f%% %8.1f%% %+9.1fpct"
          % (name, a["p10_hit"], p10, r10, t10, lift))

    # ---- 可交易性口径: 剔除次日 9:25 竞价即封死(买不进)的票 ----
    w("")
    w("=" * 78)
    w("可交易性口径 (剔除次日 9:25 竞价即封死、根本买不进的票)")
    w("=" * 78)
    w("全池中「次日竞价封死」= %d/%d (%.1f%%) -- 这类票即使晋级也无法在次日买入"
      % (base["sealed"], base["n"], base["sealed"] * 100.0 / bn))
    w("")
    w("%-26s %12s %12s %16s" % ("方案", "可买只数", "其中晋级", "可买晋级率"))
    w("-" * 78)
    for name, _ in VARIANTS:
        a = agg[name]
        b5 = a["b5"] or 1
        w("%-26s %6d/%-3d %10d      %10.1f%%"
          % (name, a["b5"], a["p5"], a["bh5"], a["bh5"] * 100.0 / b5))
    w("")
    w("%-26s %12s %12s %16s" % ("方案", "可买只数(t10)", "其中晋级", "可买晋级率"))
    w("-" * 78)
    for name, _ in VARIANTS:
        a = agg[name]
        b10 = a["b10"] or 1
        w("%-26s %6d/%-3d %10d      %10.1f%%"
          % (name, a["b10"], a["p10"], a["bh10"], a["bh10"] * 100.0 / b10))

    # ---- 显著性: 各方案 vs 现行 A ----
    w("")
    w("-" * 78)
    w("显著性检验 (Fisher 精确, 单侧; top5「可买晋级」口径, 对比现行 A)")
    w("-" * 78)
    a0 = agg[VARIANTS[0][0]]
    for name, _ in VARIANTS[1:]:
        a = agg[name]
        p = fisher_p(a0["bh5"], a0["b5"], a["bh5"], a["b5"])
        flag = "显著(p<0.05)" if p < 0.05 else ("趋势(p<0.15)" if p < 0.15 else "不显著")
        w("  %-26s  可买晋级 %2d/%d vs A %2d/%d   p=%.4f  %s"
          % (name, a["bh5"], a["b5"], a0["bh5"], a0["b5"], p, flag))

    # ---- top5 选中票画像: 可交易性与风格暴露 ----
    w("")
    w("-" * 78)
    w("top5 选中票画像 (9日 x 5 = 45 只; 用于识别混淆与可交易性)")
    w("-" * 78)
    w("%-24s %8s %8s %7s %9s %9s %9s %8s" %
      ("方案", "真一字板", "znh误判", "平均连板", "平均流通", "平均封单",
       "平均封单比", "晋级率"))
    w("-" * 78)
    for name, _ in VARIANTS:
        pk = [s for s, _h in agg[name]["picks"]]
        if not pk:
            continue
        n = len(pk)
        tyz = sum(1 for s in pk if true_yizi(s))
        faux = sum(1 for s in pk if s.get("znh") and not true_yizi(s))
        lbc = sum((s.get("lbc") or 1) for s in pk) / float(n)
        cap = sum((s.get("ltsz") or 0) for s in pk) / float(n) / 1e8
        cam = sum((s.get("camount") or 0) for s in pk) / float(n) / 1e8
        sr = sum((s.get("seal_ratio") or 0) for s in pk) / float(n)
        r = agg[name]["p5_hit"] * 100.0 / (agg[name]["p5"] or 1)
        w("%-24s %6d/%d %6d/%d %7.2f %7.1f亿 %7.2f亿 %7.2f%% %7.1f%%"
          % (name, tyz, n, faux, n, lbc, cap, cam, sr, r))

    w("")
    w("注: 「真一字板」= fbt<=09:30:00 且 zbc==0 (开盘即封死买不进);")
    w("    「znh误判」= parse_pool_item 现行 znh 口径判为真、实为盘中封板死守的票.")
    w("    若 F 与 G 晋级率接近, 说明 F 的收益主要来自「小盘」而非「封单强度」, 属混淆变量.")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n[OK] 结果已写入 %s" % OUT)


if __name__ == "__main__":
    main()
