# -*- coding: utf-8 -*-
"""智能推荐排序口径 · 前向影子记录器.

背景
----
2026-09-16 定位到「智能推荐」的 score_pool 存在排序结构性缺陷:
    scored.sort(key=lambda x: (score, camount))     # camount = 封单金额绝对值
封单额绝对值是**弱因子**(实测 r≈0.21)，且会把大盘股系统性顶到前面；更强的因子是
「封单/流通市值比」(r≈0.32)，因 parse_pool_item 漏解析 ltsz 而长期算不出来(已修复)。

影子回测 astock-screen/shadow_tiebreak.py 在 9 个复盘日上试了 10 个排序方案，
最优者 p=0.074 未过 0.05，且同批数据反复挑选会天然抬高"最优值"(多重比较)。
→ 结论: **不立即改生产排序**，改为前向记录，让候选方案在"还没被看见的未来"上
   跑够样本，再决定要不要切换。

本脚本做什么
------------
每个交易日盘后固化一份不可变快照，并在次日回填结局:
    record   当日涨停池全量 -> 打分 -> 记录每只票的完整特征 + 各方案 top5
    settle   用次日(实际交易日)的涨停池/炸板池回填 晋级 / 触板 / 竞价封死
    report   汇总累积样本 -> 对照报告(txt + html)

设计要点
--------
1. **同源**: 排序键与池缓存直接复用 shadow_tiebreak，不复制实现，避免两处口径漂移。
2. **可复算**: 记录的是「全池每只票的特征 + 次日结局」，因此日后任何新方案都能
   离线重算，不需要重新抓数，也不受 _score_stock 逻辑变更影响。
3. **口径分离**: in-sample(回填的历史, 已用于挑方案) 与 forward(前向盲测) 分组统计，
   **只有 forward 样本具备决策效力**。
4. **幂等 + 尽力而为**: 同日重复 record 默认跳过；本脚本失败绝不影响名单刷新主链路。

用法
----
    python shadow_log.py daily                    # 盘后: 结算上一交易日 + 记录今日
    python shadow_log.py record [--date D] [--force]
    python shadow_log.py settle [--date D]
    python shadow_log.py backfill [--from D] [--to D]     # 一次性回填历史(in-sample)
    python shadow_log.py report

产物
----
    shadow_log.json           累积记录 (入库, 长期证据链)
    _shadow_log_report.txt    汇总报告 (运行时产物, 不入库)
    _shadow_log_report.html   可视化报告 (运行时产物, 不入库)
"""
import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REALTIME = os.path.abspath(os.path.join(HERE, "..", "astock-realtime"))
for _p in (HERE, REALTIME):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import em                       # noqa: E402
import shadow_tiebreak as ST    # noqa: E402  复用排序键 + 池缓存(单一真源)

LOG = os.path.join(HERE, "shadow_log.json")
REPORT_TXT = os.path.join(HERE, "_shadow_log_report.txt")
REPORT_HTML = os.path.join(HERE, "_shadow_log_report.html")

# 跟踪哪几条排序线: A = 现行生产(基线, 必须有), I = 主候选, F = 极端对照(弃打分)
# 之所以敢多记两条: 记录的是全池特征, 任何方案都能事后重算, 这里只是"审计锚点".
TRACK = [
    ("A", "现行(score,封单额)", ST.k_old),
    ("I", "比值+剔真一字/高位", ST.k_ratio_filtered_fix),
    ("F", "仅封单/流比(弃打分)", ST.k_ratio_noscore),
]

# 收盘定格时间: 之前抓到的涨停池仍在变动(还有票会封/会炸), 记录会失真
CLOSE_HHMM = "1505"


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def ymd(d):
    return d.replace("-", "")


def add_days(d, n):
    dt = datetime.datetime.strptime(d, "%Y-%m-%d") + datetime.timedelta(days=n)
    return dt.strftime("%Y-%m-%d")


def now_str():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def after_close():
    return time.strftime("%H%M") >= CLOSE_HHMM


def today_open():
    """今日是否开市。复用 refresh_watch 的权威判定(与名单刷新同一口径)。"""
    try:
        import refresh_watch as RW
        return bool(RW.today_is_open())
    except Exception as e:
        log("复用 refresh_watch 判定失败(%s)，退化为工作日判断" % e)
        return datetime.date.today().weekday() < 5


# ---------------- 记录存取 ----------------
def load_log():
    if os.path.exists(LOG):
        try:
            with open(LOG, encoding="utf-8") as f:
                doc = json.load(f)
            doc.setdefault("days", {})
            return doc
        except Exception as e:
            log("!! %s 解析失败(%s)，改用空记录(原文件保留为 .bad)" % (LOG, e))
            try:
                os.replace(LOG, LOG + ".bad")
            except OSError:
                pass
    return {"version": 1, "updated": "", "track": {t: lbl for t, lbl, _ in TRACK},
            "days": {}}


def save_log(doc):
    doc["updated"] = now_str()
    doc["track"] = {t: lbl for t, lbl, _ in TRACK}
    tmp = LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, LOG)


# ---------------- 交易日序列 ----------------
def cached_days():
    """已缓存过涨停池的日期序列(= 本机已确认的交易日), 升序。"""
    out = []
    if os.path.isdir(ST.CACHE):
        for fn in os.listdir(ST.CACHE):
            if fn.startswith("pool_zt_") and fn.endswith(".json"):
                out.append(fn[len("pool_zt_"):-len(".json")])
    return sorted(out)


def resolve_next(doc, d, today=None):
    """求 d 的下一交易日。优先用已记录/已缓存的后续日, 其次用 today(运行日)。"""
    later = [x for x in doc.get("days", {}) if x > d]
    if later:
        return min(later)
    later = [x for x in cached_days() if x > d]
    if later:
        return min(later)
    if today and today > d:
        return today
    return None


# ---------------- record ----------------
def _score_all(rows):
    """原始池 rows -> 统一结构 + 打分(与 shadow_tiebreak 完全同源)。"""
    stocks = [ST.rt.parse_pool_item(x) for x in rows]
    hy = {}
    for s in stocks:
        k = s.get("hybk") or "其他"
        hy[k] = hy.get(k, 0) + 1
    for s in stocks:
        s["_sc"] = ST.rt._score_stock(s, hy)[0]
    return stocks


def _slim(s):
    """只留「参与排序 / 可复算」所需字段, 派生量保持最小。"""
    return {
        "c": s["code"], "n": s["name"], "hybk": s.get("hybk") or "",
        "lbc": s.get("lbc"), "zbc": s.get("zbc"),
        "fbt": s.get("fbt_raw"), "lbt": s.get("lbt_raw"),
        "fund": s.get("camount"), "ltsz": s.get("ltsz"),
        "score": s["_sc"], "seal_ratio": s.get("seal_ratio"),
        "n_zt": None, "n_zb": None, "n_sealed": None,
    }


def record(doc, date, sample, force=False):
    """记录 date 当日选股快照。返回 True=写入, False=跳过/失败。"""
    old = doc["days"].get(date)
    if old and old.get("recorded_at") and not force:
        log("  %s 已有记录, 跳过(需覆盖用 --force)" % date)
        return False
    rows = ST.cached_pool("zt", date)
    if not rows:
        log("  !! %s 涨停池为空, 拒绝记录(休市或数据源异常)" % date)
        return False
    stocks = _score_all(rows)
    picks = {}
    for tag, _label, kf in TRACK:
        rk = sorted(stocks, key=kf, reverse=True)[:5]
        picks[tag] = [[s["code"], s["name"]] for s in rk]
    # 炸板家数: 有缓存就带上(用于情绪记录), 取不到不阻塞
    zb = ST.cached_pool("zb", date) if os.path.exists(
        os.path.join(ST.CACHE, "pool_zb_%s.json" % date)) else []
    doc["days"][date] = {
        "sample": sample,
        "recorded_at": now_str(),
        "pool_n": len(stocks),
        "tc": len(stocks),
        "zb_tc": len(zb) if zb else None,
        "picks": picks,
        "rows": [_slim(s) for s in stocks],
        "next": resolve_next(doc, date),
        "settled_at": None,
        "next_zt_n": None,
        "next_zb_n": None,
    }
    log("  %s 已记录: 池 %d 只 | A=%s | I=%s"
        % (date, len(stocks),
           ",".join(p[1] for p in picks["A"]), ",".join(p[1] for p in picks["I"])))
    return True


# ---------------- settle ----------------
def _settle_with(doc, date, nxt, zt_rows, zb_rows):
    day = doc["days"][date]
    zc = ST.codes(zt_rows)
    bc = ST.codes(zb_rows)
    sealed = ST.sealed_at_auction(zt_rows)
    for r in day["rows"]:
        c = r["c"]
        r["n_zt"] = c in zc
        r["n_zb"] = c in bc
        r["n_sealed"] = c in sealed
    day["next"] = nxt
    day["next_zt_n"] = len(zc)
    day["next_zb_n"] = len(bc)
    day["settled_at"] = now_str()
    log("  已结算 %s -> %s (次日池 %d 只 / 炸板 %d, 竞价封死 %d)"
        % (date, nxt, len(zc), len(bc), len(sealed)))
    return True


def settle(doc, date, nxt=None):
    """用 nxt(缺省自动解析)的池子结算 date。"""
    day = doc["days"].get(date)
    if not day:
        log("  %s 无记录, 跳过结算" % date)
        return False
    if day.get("settled_at"):
        log("  %s 已结算, 跳过" % date)
        return False
    nxt = nxt or day.get("next") or resolve_next(doc, date)
    if not nxt:
        log("  %s 尚无可用的次日(%s), 保持待结算" % (date, nxt))
        return False
    zt = ST.cached_pool("zt", nxt)
    zb = ST.cached_pool("zb", nxt)
    if not zt:
        log("  !! %s 次日 %s 涨停池取不到, 保持待结算" % (date, nxt))
        return False
    return _settle_with(doc, date, nxt, zt, zb)


def settle_pending(doc, today, allow_today=False):
    """结算所有可结算的待结算日。

    两道护栏:
      1. 只允许用「紧邻的下一交易日」结算(中间不得夹着其他记录日), 防止日期错配;
      2. 当次日就是 today 且 today 尚未收盘定格时, 拒绝结算 —— 否则会拿**盘中**
         仍在变动的涨停池去判定「次日是否晋级」, 记录直接失真。
    """
    pending = sorted(d for d in doc["days"] if not doc["days"][d].get("settled_at"))
    if not pending:
        return 0
    n = 0
    for d in pending:
        nxt = resolve_next(doc, d, today)
        if not nxt or nxt <= d:
            continue
        # 只有当 d 与 nxt 之间没有任何其他记录日时, 用 nxt 结算才是正确的
        between = [x for x in doc["days"] if d < x < nxt]
        if between:
            log("  !! %s 与 %s 之间存在未结算记录 %s, 跳过(需人工补结算)"
                % (d, nxt, ",".join(between)))
            continue
        if nxt == today and not allow_today:
            log("  %s 的次日就是今日(%s)，尚未收盘定格，本轮不结算" % (d, nxt))
            continue
        if settle(doc, d, nxt):
            n += 1
    return n


# ---------------- 统计 ----------------
def stats_of(rows, topn=5, tag=None):
    """对一组记录行统计: 全池基准 + 各方案 top5 可买晋级率。
    rows: [(day_date, row_dict)] 已按方案排序好的顺序, 或全池顺序(tag=None)。"""
    res = {}
    if tag is None:
        buy = [r for _, r in rows if not r.get("n_sealed")]
        hit = [r for r in buy if r.get("n_zt")]
        res["pool"] = {"buyable": len(buy), "hit": len(hit)}
        return res
    for t, _lbl, _kf in TRACK:
        buy = hit_num = seats = 0
        for _d, day in rows:               # rows 此处是 [(date, day_dict)]
            picks = (day.get("picks") or {}).get(t) or []
            idx = {r["c"]: r for r in day["rows"]}
            for code, _name in picks[:topn]:
                r = idx.get(code)
                if r is None:
                    continue
                seats += 1
                if r.get("n_sealed"):
                    continue
                buy += 1
                if r.get("n_zt"):
                    hit_num += 1
        res[t] = {"seats": seats, "buyable": buy, "hit": hit_num}
    return res


def _rate(hit, den):
    return (hit * 100.0 / den) if den else 0.0


def collect(doc, sample=None):
    """返回 [(date, day)] 已结算记录, 可按 sample 过滤。"""
    out = []
    for d in sorted(doc["days"]):
        day = doc["days"][d]
        if not day.get("settled_at"):
            continue
        if sample and day.get("sample") != sample:
            continue
        out.append((d, day))
    return out


def report(doc):
    lines = []

    def w(s=""):
        lines.append(s)
        print(s)

    groups = [("全部", None), ("前向盲测 forward", "forward"),
              ("样本内 in-sample", "in-sample")]
    all_days = collect(doc)
    fwd_days = collect(doc, "forward")

    w("=" * 96)
    w("智能推荐排序口径 · 并行影子记录报告")
    w("生成 %s | 记录 %d 个交易日(已结算 %d) | 其中前向样本 %d 日"
      % (now_str(), len(doc["days"]), len(all_days), len(fwd_days)))
    w("=" * 96)
    w("")
    w("方案: " + "  /  ".join("%s=%s" % (t, lbl) for t, lbl, _ in TRACK))
    w("口径: 每日取当日涨停池全量 -> 打分 -> 各方案排序取 top5 ->")
    w("      「可买」= 次日非 9:25 竞价封死(买得进);「晋级」= 次日进入涨停池.")
    w("      可买晋级率 = 晋级数 / 可买只数. 基准 = 全池同口径.")
    w("")

    summary = {}
    for gname, gsample in groups:
        days = collect(doc, gsample)
        if not days:
            continue
        pool_buy = sum(1 for _d, day in days
                       for r in day["rows"] if not r.get("n_sealed"))
        pool_hit = sum(1 for _d, day in days
                       for r in day["rows"] if not r.get("n_sealed") and r.get("n_zt"))
        base_rate = _rate(pool_hit, pool_buy)
        w("-" * 96)
        w("%s —— %d 日 / 全池 %d 只(可买 %d) | 全池可买晋级率 %.1f%%"
          % (gname, len(days),
             sum(len(d["rows"]) for _d, d in days), pool_buy, base_rate))
        w("-" * 96)
        w("%-4s %-24s %10s %10s %12s %12s" %
          ("线", "说明", "席位", "可买", "晋级", "可买晋级率"))
        row = {}
        for t, lbl, _kf in TRACK:
            seats = buy = hit = 0
            for _d, day in days:
                idx = {r["c"]: r for r in day["rows"]}
                for code, _n in ((day.get("picks") or {}).get(t) or [])[:5]:
                    r = idx.get(code)
                    if r is None:
                        continue
                    seats += 1
                    if r.get("n_sealed"):
                        continue
                    buy += 1
                    hit += 1 if r.get("n_zt") else 0
            rate = _rate(hit, buy)
            row[t] = {"seats": seats, "buyable": buy, "hit": hit,
                      "rate": rate, "base": base_rate}
            w("%-4s %-24s %10d %10d %12d %10.1f%%   %s"
              % (t, lbl, seats, buy, hit, rate,
                 ("基准%+.1fpct" % (rate - base_rate)) if buy else "-"))
        summary[gname] = row
        w("")

    # 显著性: 各候选 vs 现行 A (仅前向样本才有决策效力)
    for gname, gsample in groups:
        if gname not in summary or len(collect(doc, gsample)) < 2:
            continue
        s = summary[gname]
        if "A" not in s:
            continue
        w("-" * 96)
        w("显著性 (%s): 各候选 vs 现行 A —— Fisher 精确, 单侧" % gname)
        w("-" * 96)
        for t, lbl, _kf in TRACK:
            if t == "A" or t not in s:
                continue
            a, b = s["A"], s[t]
            if not a["buyable"] or not b["buyable"]:
                w("  %-4s %-24s 样本不足" % (t, lbl))
                continue
            p = ST.fisher_p(a["hit"], a["buyable"], b["hit"], b["buyable"])
            flag = ("显著(p<0.05)" if p < 0.05
                    else ("趋势(p<0.15)" if p < 0.15 else "不显著"))
            w("  %-4s %-24s 可买晋级 %2d/%-3d vs A %2d/%-3d  p=%.4f  %s"
              % (t, lbl, b["hit"], b["buyable"], a["hit"], a["buyable"], p, flag))
        w("")

    # 逐日明细
    w("=" * 96)
    w("逐日明细")
    w("=" * 96)
    for d, day in all_days:
        idx = {r["c"]: r for r in day["rows"]}
        w("%s -> %s  [%s] 池 %d 只 | 全池可买晋级 %d/%d"
          % (d, day.get("next"), day.get("sample"),
             len(day["rows"]),
             sum(1 for r in day["rows"]
                 if not r.get("n_sealed") and r.get("n_zt")),
             sum(1 for r in day["rows"] if not r.get("n_sealed"))))
        for t, lbl, _kf in TRACK:
            parts = []
            for code, name in ((day.get("picks") or {}).get(t) or [])[:5]:
                r = idx.get(code) or {}
                mark = "晋级" if r.get("n_zt") else ("触板" if r.get("n_zb") else "—")
                if r.get("n_sealed"):
                    mark = "竞价封死"
                sr = r.get("seal_ratio")
                parts.append("%s(%s%s)" % (name, mark,
                                           "" if sr is None else " 比%.2f%%" % sr))
            w("   %-2s %s" % (t, " | ".join(parts)))
        w("")

    w("-" * 96)
    w("注: in-sample = 回填的历史样本, 已参与「方案挑选」, 存在多重比较偏差;")
    w("    forward    = 记录当日之后才发生的行情, 未被任何挑选过程看过, 才可据以决策.")
    w("    前向样本 < 10 个交易日时, 任何差异都不足以支撑切换生产排序.")

    with open(REPORT_TXT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("\n[OK] 文本报告 -> %s" % REPORT_TXT)

    _write_html(doc, all_days, fwd_days, summary)
    return summary


def _write_html(doc, all_days, fwd_days, summary):
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def rate_cls(v, base):
        if v > base + 1:
            return "good"
        if v < base - 1:
            return "bad"
        return "flat"

    cards = []
    for gname, row in summary.items():
        base = next((r["base"] for r in row.values()), 0.0)
        cs = "".join(
            '<div class="card"><div class="cname">%s <span class="tag">%s</span></div>'
            '<div class="crate %s">%.1f%%</div>'
            '<div class="csub">%d/%d 可买席晋级 · 基准 %.1f%% · %s</div></div>'
            % (esc(lbl), t, rate_cls(d["rate"], base), d["rate"],
               d["hit"], d["buyable"], base,
               ("%+.1fpct" % (d["rate"] - base)))
            for t, lbl, _ in TRACK if (d := row.get(t)))
        cards.append('<section><h2>%s</h2><div class="cards">%s</div></section>'
                     % (esc(gname), cs))

    dayhtml = []
    for d, day in all_days:
        idx = {r["c"]: r for r in day["rows"]}
        tr = []
        for t, _lbl, _kf in TRACK:
            chips = []
            for code, name in ((day.get("picks") or {}).get(t) or [])[:5]:
                r = idx.get(code) or {}
                if r.get("n_zt"):
                    cls, mark = ("hit", "晋级")
                elif r.get("n_zb"):
                    cls, mark = ("touch", "触板")
                else:
                    cls, mark = ("miss", "未晋")
                seal = ' <span class="sealed">竞价封死</span>' if r.get("n_sealed") else ""
                sr = r.get("seal_ratio")
                chips.append('<span class="chip %s">%s<em>%s</em>%s%s</span>'
                             % (cls, esc(name), esc(mark), seal,
                                "" if sr is None else " 比%.2f%%" % sr))
            tr.append('<td class="vcell"><div class="vlabel">%s</div>%s</td>'
                      % (esc(t), "".join(chips)))
        dayhtml.append(
            '<tr><td class="d">%s<br><span class="mut">→ %s · %s · 池%d</span></td>%s</tr>'
            % (esc(d), esc(day.get("next")), esc(day.get("sample")),
               len(day["rows"]), "".join(tr)))

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>智能推荐排序口径 · 影子记录</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c1f23;--mut:#6b7280;--line:#e3e6ea;
      --good:#c0392b;--bad:#1e8449;--acc:#2c6cb0}
*{box-sizing:border-box}
body{margin:0;padding:28px 32px 60px;background:var(--bg);color:var(--ink);
     font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1{margin:0 0 6px;font-size:22px}
h2{font-size:15px;margin:26px 0 10px;color:var(--acc)}
.meta{color:var(--mut);font-size:12.5px;margin-bottom:8px}
.note{background:#fff8e6;border-left:3px solid #e0a800;padding:10px 14px;
      border-radius:3px;font-size:13px;color:#5a4a12;margin:14px 0 22px}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
      padding:12px 16px;min-width:210px;flex:1 1 210px}
.cname{font-size:12.5px;color:var(--mut)}
.tag{background:#eef2f7;border-radius:3px;padding:0 5px;color:var(--acc);font-weight:600}
.crate{font-size:26px;font-weight:700;margin:4px 0 2px;font-variant-numeric:tabular-nums}
.crate.good{color:var(--good)} .crate.bad{color:var(--bad)}
.csub{font-size:11.5px;color:var(--mut)}
table{width:100%;border-collapse:collapse;background:var(--card);
      border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{border-bottom:1px solid var(--line);padding:9px 11px;text-align:left;
      vertical-align:top;font-size:13px}
th{background:#f0f2f5;font-weight:600;font-size:12px;color:#495057}
td.d{white-space:nowrap;font-weight:600;font-size:12.5px}
.mut{color:var(--mut);font-weight:400;font-size:11.5px}
.vcell{padding-top:8px}
.vlabel{font-size:11px;color:var(--mut);margin-bottom:3px}
.chip{display:inline-block;margin:2px 4px 2px 0;padding:2px 7px;border-radius:4px;
      font-size:12px;border:1px solid var(--line);background:#fafbfc}
.chip em{font-style:normal;font-size:11px;margin-left:4px;color:var(--mut)}
.chip.hit{background:#fdecea;border-color:#f2b8b1;color:#9c2b1e}
.chip.hit em{color:#9c2b1e}
.chip.touch{background:#fff6e5;border-color:#f0d19a;color:#8a5a00}
.chip.miss{background:#f2f3f5;color:#7a8290}
.sealed{color:#b2b8c0;font-size:11px}
footer{margin-top:26px;color:var(--mut);font-size:12px;line-height:1.8}
</style></head><body>
<h1>智能推荐排序口径 · 并行影子记录</h1>
<div class="meta">生成 __GEN__ · 记录 __NDAYS__ 个交易日(已结算 __SETTLED__) ·
其中前向样本 __NFWD__ 日</div>
<div class="note"><b>口径</b>：每日取当日涨停池全量 → 打分 → 各排序方案取 top5；
「可买」= 次日非 9:25 竞价封死(买得进)，「晋级」= 次日进涨停池。
<b>in-sample</b> 是回填的历史(已参与挑选方案，存在多重比较偏差)，
<b>forward</b> 才是未被看过的前向盲测——前向样本不足 10 个交易日时，
任何差异都不足以支撑切换生产排序。</div>
__CARDS__
<section><h2>逐日明细</h2>
<table><thead><tr><th style="width:190px">日期</th><th>A 现行</th>
<th>I 比值+剔真一字/高位</th><th>F 仅封单/流比</th></tr></thead>
<tbody>__DAYS__</tbody></table></section>
<footer>说明：红=晋级，黄=盘中触板后开板，灰=未触板。A 为现行生产排序(基线)。<br>
影子的价值在于「不改变今天的结果，但把两边的账都记清楚」——
排序键与池缓存与 shadow_tiebreak.py 同源，记录的是全池每只票的特征+结局，
因此任何新方案都能离线复算。</footer>
</body></html>"""

    html = (html
            .replace("__GEN__", now_str())
            .replace("__NDAYS__", str(len(doc["days"])))
            .replace("__SETTLED__", str(len(all_days)))
            .replace("__NFWD__", str(len(fwd_days)))
            .replace("__CARDS__", "".join(cards))
            .replace("__DAYS__", "".join(dayhtml)))
    with open(REPORT_HTML, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print("[OK] HTML 报告 -> %s" % REPORT_HTML)


# ---------------- backfill ----------------
def backfill(doc, d_from=None, d_to=None, force=False):
    """用本机已缓存的池子回填历史(in-sample)。零网络请求。

    force=True 时重写已有记录(字段口径变更后需要重建时用)。
    """
    days = [d for d in cached_days() if (not d_from or d >= d_from)
            and (not d_to or d <= d_to)]
    if not days:
        log("!! 没有可回填的缓存日(先跑 shadow_tiebreak.py 生成 _phist 缓存)")
        return 0
    log("回填 %d 个交易日: %s ... %s" % (len(days), days[0], days[-1]))
    for d in days:
        record(doc, d, "in-sample", force=force)
    # 缓存里的日期序列本身就是交易日序列, 逐日用下一个缓存日结算
    seq = cached_days()
    n = 0
    for d in days:
        later = [x for x in seq if x > d]
        if not later:
            continue
        nxt = later[0]
        if nxt not in doc["days"]:        # 次日不在回填范围 -> 直接抓/读缓存结算
            if settle(doc, d, nxt):
                n += 1
            continue
        if settle(doc, d, nxt):
            n += 1
    save_log(doc)
    log("回填完成: 记录 %d 日, 结算 %d 日" % (len(days), n))
    return len(days)


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser(description="智能推荐排序口径 · 前向影子记录器")
    ap.add_argument("mode", nargs="?", default="daily",
                    choices=["daily", "record", "settle", "backfill", "report"])
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, 缺省为今日")
    ap.add_argument("--from", dest="d_from", default=None, help="backfill 起始日")
    ap.add_argument("--to", dest="d_to", default=None, help="backfill 结束日")
    ap.add_argument("--force", action="store_true", help="覆盖已有记录 / 忽略收盘时间护栏")
    args = ap.parse_args()

    doc = load_log()
    today = args.date or time.strftime("%Y-%m-%d")
    mode = args.mode

    if mode == "backfill":
        backfill(doc, args.d_from, args.d_to, force=args.force)
        report(doc)
        return 0

    if mode == "report":
        report(doc)
        return 0

    if mode == "record":
        if not args.force and args.date is None and not after_close():
            log("当前 %s 未到收盘定格时间(%s)，拒绝记录(盘中池子仍在变动)"
                % (time.strftime("%H:%M"), CLOSE_HHMM))
            return 0
        out = record(doc, today, "forward", force=args.force)
        if out:
            save_log(doc)
        return 0

    if mode == "settle":
        if settle(doc, today):
            save_log(doc)
        return 0

    # ---- daily: 结算待结算 + 记录今日 ----
    log("=== 影子记录 daily (today=%s) ===" % today)
    closed = args.force or after_close()
    is_open = today_open() if args.date is None else True
    if not is_open:
        log("今日休市: 只做结算, 不记录新快照")
    elif not closed:
        log("未到收盘定格时间(%s)，本轮不记录新快照(可稍后重跑或 --force)"
            % CLOSE_HHMM)
    else:
        # 先 record 使 today 进入交易日序列, 便于把上一日结算到 today
        record(doc, today, "forward", force=args.force)
    n = settle_pending(doc, today, allow_today=closed)
    save_log(doc)
    log("daily 完成: 结算 %d 日, 记录日总数 %d" % (n, len(doc["days"])))
    report(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
