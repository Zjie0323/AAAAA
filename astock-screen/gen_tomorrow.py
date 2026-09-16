# -*- coding: utf-8 -*-
"""明日竞价观察名单：基于「今日真实涨停板（东财官方涨停池）」+ 历史涨停基因，
把标的分成 高位核心锚 / 连板龙头 / 首板精选 三类，
并给每只标注明日集合竞价(9:15-9:25)的观察信号与触发动作。

数据源说明(重要):
  live_raw.json 由 live_limit.py 生成, 来自东财官方涨停板专题池(getTopicZTPool),
  含官方连板数 lbc、封单额 fund、开板次数 zbc —— 是真实涨停名单。
  旧版曾用 clist 全市场按涨幅排序当涨停池(无过滤), 已废弃。

用法: python gen_tomorrow.py
输出: tomorrow_watch.html / .txt / .json
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from em import to_f, keep_history, mood
from reco_engine import intensity_six, resolve_stage  # 智能推荐引擎: 强度六维 + 情绪周期

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "live_raw.json")
_lu = os.path.join(HERE, "limit_up.json")
LU = json.load(open(_lu, encoding="utf-8")) if os.path.exists(_lu) else []
meta = {r["code"]: r for r in LU}


def grade(r):
    """历史股性评级; 基因池未覆盖则为「新面孔」。"""
    m = r.get("m")
    if not m:
        return ("新面孔", "#7f8c8d")
    one = m["one_ratio"]; rec = m["recent30"]
    if one <= 0.15 and rec >= 2:
        return ("优", "#1e8449")
    if one <= 0.30 and rec >= 1:
        return ("良", "#2e86c1")
    if one <= 0.50:
        return ("中", "#b9770e")
    return ("差", "#c0392b")


def fbt_str(v):
    if not v:
        return "—"
    s = str(int(v)).zfill(6)
    return "%s:%s" % (s[:2], s[2:4])


def classify(r, main_sectors):
    """C 高位核心锚(≥5板) / A 连板龙头(2~4板) / B 首板精选 / D 首板其他"""
    b = r["board"]
    if b >= 5:
        return ("C", "高位核心锚")
    if b >= 2:
        return ("A", "连板龙头")
    # 首板: 主线板块 或 封单坚决 或 换手回封 → 精选; 否则归 D(不进名单)
    if (r["ind"] in main_sectors) or (r["seal"] or 0) >= 0.5 or r["zbc"] >= 1:
        return ("B", "首板精选")
    return ("D", "首板其他")


def auction_note(r):
    """明日竞价观察信号与触发动作(结合官方封板质量)。"""
    b = r["board"]; zbc = r["zbc"]; g = grade(r)[0]
    early = r["fbt"] and int(r["fbt"]) <= 93500
    if zbc >= 3:
        return (f"今日反复开板{zbc}次，封板质量差：明日竞价大概率低开，"
                "<b>不参与</b>；若竞价仍高开属出货嫌疑，回避。")
    if b >= 5:
        return ("高位核心锚(≥5板)：盈亏比已差，普通账户<b>只看不做</b>。"
                "若竞价爆量开板=板块退潮信号，同板块其他票宜规避；仅作情绪高度参照。")
    if b == 4:
        return ("四板渡劫：竞价高开≥4%+缩量=弱转强可持；平开观察；"
                "低开=退潮竞价清；破昨收无条件走。")
    if b == 3:
        return ("三板定妖：竞价高开≥3%且放量(竞价额/昨成交≥3%)=弱转强可打回封；"
                "平开观察，低开即弱冲高走；破昨收清。")
    if b == 2:
        return ("二板定龙头：竞价高开≥2%且板块≥2只呼应=持有看三板；"
                "平开观察，低开弱走；换手回封可加仓。")
    # 首板
    tail = ""
    if early and zbc == 0:
        tail = "今日早盘秒板未开(封单强但难买)，明日若竞价大幅高开则不追、等回踩。"
    elif zbc >= 1:
        tail = f"今日开板{zbc}次后回封，换手已充分，明日竞价参与度较高。"
    if g in ("优", "良"):
        t1 = (r.get("m") or {}).get("t1_ret") or 0
        return (f"首板+历史基因{g}(次日溢价{t1*100:.1f}%)：竞价高开≥2%=超预期，"
                f"冲高不板即兑现；低开=不及预期开盘走。{tail}")
    if r["ind"] in MAIN_SECTOR_SET:
        return (f"首板+主线板块：明日看板块整体竞价，板块内≥2只高开才参与，"
                f"独自高开不追。{tail}")
    return f"首板普通股性：仅板块≥3只涨停共振时参与，竞价弱则放弃。{tail}"


# ---------- 读取真实涨停池 ----------
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
    m = meta.get(code)
    parsed.append(dict(
        code=code, name=r.get("f14"), ind=r.get("f100") or "其他",
        price=r.get("f2"), chg=r.get("f3"),
        board=r.get("lbc") or 1, zbc=r.get("zbc") or 0, fbt=r.get("fbt"),
        seal=r.get("seal_yi"), hs=r.get("hs"), ltsz=r.get("ltsz"),
        amount=r.get("amount") or 0,
        zt_days=r.get("zt_days"), zt_ct=r.get("zt_ct"),
        m=m,
        max_board=(m or {}).get("max_board"),
        recent30=(m or {}).get("recent30", 0),
        one_ratio=(m or {}).get("one_ratio"),
        t1_ret=(m or {}).get("t1_ret"),
        limit_cnt=(m or {}).get("limit_cnt", 0),
        # 明日竞价量比基准: 优先用今日成交额(专题池自带, 新面孔也有)
        yamt=r.get("amount") or 0,
    ))

# 板块统计(先算主线, 分类时要用)
ind_cnt = {}
for r in parsed:
    ind_cnt[r["ind"]] = ind_cnt.get(r["ind"], 0) + 1
top_ind = [(i, c) for i, c in sorted(ind_cnt.items(), key=lambda x: -x[1]) if c >= 2]
MAIN_SECTOR_SET = {i for i, c in ind_cnt.items() if c >= 3}

for r in parsed:
    r["cls"], r["cls_name"] = classify(r, MAIN_SECTOR_SET)

in_pool = sum(1 for r in parsed if r["m"])
print(f"收盘快照:{snap}  真实涨停:{tc}  炸板:{zb_tc}  基因池覆盖:{in_pool}")


# mood() 已上移到 em.py 作为单一事实源(涨停家数 × 炸板率)


mood_name, mood_color, mood_desc = mood(tc, zb_tc)   # 叠加炸板率修正
zb_ratio = (zb_tc / (zb_tc + tc) * 100) if (zb_tc and tc) else None

# 情绪周期阶段(智能推荐上游): 由涨停家数推导冰点/启动/发酵/高潮/退潮, 用于约束竞价信号
stage_code, (stage_name, stage_desc, stage_color, stage_k) = resolve_stage(tc)
print(f"情绪周期阶段: {stage_name}（{stage_desc}）竞价系数k={stage_k}")

A = sorted([r for r in parsed if r["cls"] == "A"],
           key=lambda x: (-x["board"], -(x["seal"] or 0)))
C = sorted([r for r in parsed if r["cls"] == "C"],
           key=lambda x: (-x["board"], -(x["seal"] or 0)))
B = sorted([r for r in parsed if r["cls"] == "B"],
           key=lambda x: (-(x["seal"] or 0), -x["recent30"]))
D = [r for r in parsed if r["cls"] == "D"]
print(f"A连板龙头:{len(A)}  B首板精选:{len(B)}  C高位锚:{len(C)}  D未入选首板:{len(D)}")

# 次日日期(跳过周末)
_t = time.localtime()
_wd = _t.tm_wday
_add = 3 if _wd == 4 else (2 if _wd == 5 else 1)
NEXT = time.strftime("%m/%d", time.localtime(time.time() + _add * 86400))
NEXT_WD = "周" + "一二三四五六日"[(_wd + _add) % 7]


def rows_html(rs):
    h = ""
    for r in rs:
        g, gc = grade(r)
        seal_s = f"{r['seal']:.2f}亿" if r["seal"] else "—"
        hs_s = f"{r['hs']:.1f}%" if r["hs"] else "—"
        amt_s = f"{r['amount']/1e8:.2f}亿" if r["amount"] else "—"
        cap_s = f"{r['ltsz']/1e8:.0f}亿" if r["ltsz"] else "—"
        ztj = f"{r['zt_days']}天{r['zt_ct']}板" if r["zt_days"] and r["zt_ct"] else "—"
        zbc_s = (f"<span style='color:#c0392b;font-weight:700'>{r['zbc']}</span>"
                 if r["zbc"] else "0")
        h += (f"<tr><td><b>{r['code']}</b></td><td>{r['name']}</td>"
              f"<td style='font-size:11px;color:#888'>{r['ind']}"
              f"{'<b style=color:#c0392b> ★</b>' if r['ind'] in MAIN_SECTOR_SET else ''}</td>"
              f"<td style='text-align:center;font-weight:700;color:#c0392b'>{r['board']}</td>"
              f"<td style='text-align:center;font-size:11px'>{ztj}</td>"
              f"<td style='text-align:center'>{fbt_str(r['fbt'])}</td>"
              f"<td style='text-align:center'>{zbc_s}</td>"
              f"<td style='text-align:center'>{seal_s}</td>"
              f"<td style='text-align:center'>{hs_s}</td>"
              f"<td style='text-align:center'>{amt_s}</td>"
              f"<td style='text-align:center'>{cap_s}</td>"
              f"<td style='text-align:center;color:{gc};font-weight:700'>{g}</td>"
              f"<td style='font-size:11px;color:#444;line-height:1.5'>{auction_note(r)}</td></tr>")
    return h


HEAD = ("<tr><th>代码</th><th>名称</th><th>行业</th><th>连板</th><th>涨停统计</th>"
        "<th>首封</th><th>开板次</th><th>封单额</th><th>换手</th><th>今日成交额</th>"
        "<th>流通市值</th><th>历史评级</th><th>明日竞价观察信号 / 触发动作</th></tr>")

ind_html = "".join(
    f"<tr><td>{i}{'<b style=color:#c0392b> ★主线</b>' if c >= 3 else ''}</td>"
    f"<td style='text-align:center;font-weight:700'>{c}</td>"
    f"<td><div style='background:#c0392b;height:10px;width:{min(c * 30, 200)}px'></div></td>"
    f"<td style='font-size:11px;color:#666'>{'主线' if c >= 5 else ('次主线' if c >= 3 else '双响/分散')}</td></tr>"
    for i, c in top_ind)

HTML = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>明日竞价观察名单</title>
<style>
*{{box-sizing:border-box}} body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#f4f6f8;color:#1f2937;margin:0;padding:18px}}
.wrap{{max-width:1320px;margin:0 auto}}
.card{{background:#fff;border-radius:10px;padding:16px 18px;margin-bottom:14px;box-shadow:0 1px 4px #0001}}
h1{{font-size:19px;margin:0 0 4px}} h2{{font-size:15px;margin:0 0 10px;color:#c0392b;border-left:4px solid #c0392b;padding-left:8px}}
.sub{{color:#888;font-size:12px}}
.kpis{{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}}
.kpi{{flex:1;min-width:110px;background:#f8fafc;border:1px solid #eef;border-radius:8px;padding:10px}}
.kpi b{{display:block;font-size:20px;color:#2c3e50}} .kpi span{{font-size:11px;color:#888}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 5px;border-bottom:1px solid #eee;text-align:left}}
th{{background:#fafafa;color:#666;font-weight:600;font-size:11px}}
tr:hover{{background:#fcfcfc}}
.note{{font-size:11px;color:#999;margin-top:6px;line-height:1.6}}
.warn{{background:#fff8f0;border:1px solid #ffd9a0;border-radius:8px;padding:12px;font-size:13px;line-height:1.7}}
.tag{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700;color:#fff;margin-right:4px}}
.tagA{{background:#c0392b}} .tagB{{background:#2e86c1}} .tagC{{background:#7f8c8d}}
</style></head><body><div class="wrap">
<div class="card">
  <h1>明日竞价观察名单 <span style="font-size:12px;color:#888">（{NEXT} {NEXT_WD}盘前）</span></h1>
  <div class="sub">数据源：<b>东方财富官方涨停板专题池</b>（权威涨停名单，官方连板数/封单额/开板次数） · 快照 {snap}</div>
  <div class="kpis">
    <div class="kpi"><b style="color:{mood_color}">{tc}</b><span>今日真实涨停家数</span></div>
    <div class="kpi"><b>{zb_tc if zb_tc is not None else '—'}</b><span>今日炸板数</span></div>
    <div class="kpi"><b>{len(A)}</b><span><span class="tag tagA">A</span>连板龙头(重点盯)</span></div>
    <div class="kpi"><b>{len(B)}</b><span><span class="tag tagB">B</span>首板精选</span></div>
    <div class="kpi"><b>{len(C)}</b><span><span class="tag tagC">C</span>高位锚(只看不做)</span></div>
  </div>
</div>

<div class="card">
  <h2>⚠️ 明日盘前总纲 · 今日盘面：<span style="color:{mood_color}">{mood_name}</span></h2>
  <div class="warn">
  今日真实涨停 <b>{tc}</b> 只{f"、炸板 <b>{zb_tc}</b> 只(炸板率 {zb_ratio:.0f}%)" if zb_ratio is not None else "、炸板 0 只(封板坚决)"}。
  <b>{mood_desc}</b><br>
  ① <b>先判板块</b>：下表带 ★ 的（≥3只涨停）才是有持续性的主线，优先盯这些板块的连板票；<br>
  ② <b>再判个股竞价</b>：连板票看「高开幅度 + 放量程度」，高开缩量=加速、低开=退潮；<br>
  ③ <b>看封板质量</b>：今日开板次数≥3 的票明日大概率低开，直接剔除；早盘秒板未开的难买，别追高；<br>
  ④ <b>纪律</b>：单票≤10%、破板3分钟不回封即走、T+1博次日溢价非隔日持有。<br>
  ⑤ <b>{'涨停家数偏少的分化行情，宁可空仓也不要打非主线票。' if tc < 60 else '涨停家数较多，次日分化是大概率，竞价定去留。'}</b>
  </div>
</div>

<div class="card">
  <h2>明日主线板块（★ = 同板块≥3只涨停 = 真主线）</h2>
  <table><thead><tr><th>行业</th><th>今日涨停家数</th><th>强度</th><th>定性</th></tr></thead>
  <tbody>{ind_html}</tbody></table>
  <div class="note">仅列≥2只涨停的板块。红条越长=板块内涨停越密集=明天延续性越强；独苗板块持续性差。</div>
</div>

<div class="card">
  <h2><span class="tag tagA">A</span> 连板龙头（{len(A)} 只，明日竞价重点盯：弱转强可打 / 强转弱即走）</h2>
  <table><thead>{HEAD}</thead><tbody>{rows_html(A)}</tbody></table>
  <div class="note">连板数为东财官方 lbc。「今日成交额」用于明早算竞价量比（竞价额÷今日成交额≥3%~5%为放量强势）。
  「开板次」0=全天未开板（封单强、难买），≥1=经过换手（更易参与），≥3=封板差（剔除）。</div>
</div>

<div class="card">
  <h2><span class="tag tagB">B</span> 首板精选（{len(B)} 只，主线板块 / 封单≥0.5亿 / 换手回封）</h2>
  <table><thead>{HEAD}</thead><tbody>{rows_html(B)}</tbody></table>
  <div class="note">从今日首板中筛出「属主线板块 或 封单坚决 或 开板后回封」的票；已剔除 {len(D)} 只非主线+封单弱的杂毛首板。</div>
</div>
{f'''
<div class="card">
  <h2><span class="tag tagC">C</span> 高位核心锚（≥5板，只看不做，作情绪高度参照）</h2>
  <table><thead>{HEAD}</thead><tbody>{rows_html(C)}</tbody></table>
  <div class="note">连板≥5的超高板，盈亏比已差、一旦爆量开板常预示板块退潮；普通账户仅作风险预警与高度参照，非买点。</div>
</div>''' if C else '''
<div class="card">
  <h2><span class="tag tagC">C</span> 高位核心锚（≥5板）</h2>
  <div class="note">今日无 ≥5 板个股 —— 市场情绪高度不高，无极端高位风险锚，也说明主线尚未形成明确领涨龙头。</div>
</div>'''}

<div class="card">
  <h2>明早 9:15-9:25 竞价操作 checklist</h2>
  <div class="warn">
  1. 先看「主线板块」：若 ★ 板块内≥2只核心票竞价高开，确认板块延续；若集体低开，全天降仓。<br>
  2. 逐一点开 A 类连板票：记录竞价价/量 → 算量比(竞价额÷今日成交额) → 对照上表「触发动作」。<br>
  3. 弱转强(高开+放量)才出手；平开观察、低开不买。<br>
  4. 今日开板≥3次的票直接跳过，不论竞价多强。<br>
  5. 所有单票≤10%仓，破板即走，不恋战。<br>
  6. {'今日仅 %d 只涨停属分化行情，若明早主线板块集体走弱，最优解是空仓。' % tc if tc < 60 else '普涨次日必分化，竞价定去留，不把涨停当持有信号。'}
  </div>
</div>

<div class="warn"><b>免责声明</b>：本名单为东财官方涨停池 + 历史股性的交叉筛选，仅供个人盘前复盘参考，不构成投资建议，盈亏自负。</div>
</div></body></html>"""

out = os.path.join(HERE, "tomorrow_watch.html")
open(out, "w", encoding="utf-8").write(HTML)
print("OK", out, len(HTML), "bytes")


# ---------- 极简 txt ----------
def brief(r):
    g = grade(r)[0]
    seal = f"{r['seal']:.2f}亿" if r["seal"] else "—"
    star = " ★主线" if r["ind"] in MAIN_SECTOR_SET else ""
    zb = f" 开板{r['zbc']}次" if r["zbc"] else ""
    import re
    note = re.sub(r"<[^>]+>", "", auction_note(r))
    return (f"{r['code']} {r['name']} [{r['cls_name']}] {r['board']}板 "
            f"{r['ind']}{star} 封单{seal}{zb} 评级{g}\n   竞价: {note}")


txt = [f"=== 明日竞价观察名单 ({NEXT} {NEXT_WD}盘前, 基准 {snap}) ===",
       f"今日真实涨停 {tc} 只 | 炸板 {zb_tc} 只 | 盘面: {mood_name}",
       f"A连板龙头 {len(A)} | B首板精选 {len(B)} | C高位锚 {len(C)}",
       "",
       "【主线板块(≥3只)】 " + ("  ".join(f"{i}({c})" for i, c in top_ind if c >= 3) or "无明确主线"),
       "【双响板块(2只)】 " + ("  ".join(f"{i}({c})" for i, c in top_ind if c == 2) or "无"),
       "",
       f"--- A 连板龙头 ({len(A)}) ---"]
for r in A:
    txt.append(brief(r))
txt += ["", f"--- B 首板精选 ({len(B)}) ---"]
for r in B:
    txt.append(brief(r))
if C:
    txt += ["", f"--- C 高位核心锚 ({len(C)}, 只看不做) ---"]
    for r in C:
        txt.append(brief(r))
txt += ["",
        "纪律: 单票≤10%仓 | 破板3分钟不回封即走 | T+1博次日溢价非隔日持有",
        f"提示: 今日涨停{tc}只({mood_name}), {mood_desc}"]

tout = os.path.join(HERE, "tomorrow_watch.txt")
open(tout, "w", encoding="utf-8").write("\n".join(txt))
print("OK", tout)

# ---------- 结构化名单(供 server.py /api/bid_watch) ----------
import re as _re
_w = {"generated": snap, "base_date": snap[:10],
      "zt_total": tc, "zb_total": zb_tc,
      "mood": mood_name, "mood_desc": mood_desc,
      "stage_code": stage_code, "stage_name": stage_name,
      "stage_desc": stage_desc, "stage_color": stage_color,
      "a_count": len(A), "b_count": len(B), "c_count": len(C),
      "main_sectors": [{"name": i, "count": c} for i, c in top_ind],
      "list": []}
for r in (A + B + C):
    g, gc = grade(r)
    # 强度持续性六维(智能推荐量化核心): 上游筛选, 决定该票是否值得被竞价信号采信
    is_main = r["ind"] in MAIN_SECTOR_SET
    six_dims, reco_score, reco_grade = intensity_six({
        "board": r["board"], "seal": r["seal"], "is_main": is_main,
        "zbc": r["zbc"], "ltsz": r["ltsz"], "amount": r["amount"]})
    _w["list"].append({
        "code": r["code"], "name": r["name"], "ind": r["ind"],
        "cls": r["cls"], "cls_name": r["cls_name"],
        # base_price: 今日收盘价(涨停价). 明日"竞价高开幅度"必须以此为基准计算,
        # 不能用实时涨跌幅(那是相对昨收的, 收盘后恒为 +10% 会把所有票误判为"可买")
        "base_price": r["price"], "base_chg": r["chg"],
        "board": r["board"], "seal": r["seal"], "rating": g,
        "zbc": r["zbc"], "fbt": r["fbt"], "hs": r["hs"],
        "amount": r["amount"], "ltsz": r["ltsz"],
        "is_main": is_main,
        "yamt": r["yamt"], "t1": r["t1_ret"], "recent30": r["recent30"],
        "limit_cnt": r["limit_cnt"],
        # === 智能推荐引擎输出 ===
        "reco_score": reco_score, "reco_grade": reco_grade, "six_dims": six_dims,
        "note": _re.sub(r"<[^>]+>", "", auction_note(r)),
    })
_wj = os.path.join(HERE, "tomorrow_watch.json")
json.dump(_w, open(_wj, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("OK", _wj, "共", len(_w["list"]), "只")
print("A:", [(r["code"], r["name"], f"{r['board']}板") for r in A[:8]])
# 按日留存历史副本(次日复盘用): tomorrow_watch.json -> tomorrow_watch_YYYY-MM-DD.json
_hist = keep_history(_wj, snap[:10])
print("历史副本:", _hist or "留存失败(忽略)")
