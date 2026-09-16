# -*- coding: utf-8 -*-
"""打板(涨停板)股性筛选: 从近1年日K统计每只票的涨停/连板/次日溢价等指标。"""
import json, os, statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
KDIR = os.path.join(HERE, "kdata")
RAW = os.path.join(HERE, "raw_stats.json")

# code -> name 映射(来自已筛候选池, 顺带剔除 ST/退)
name_map = {}
if os.path.exists(RAW):
    for r in json.load(open(RAW, encoding="utf-8-sig")):
        nm = r.get("name", "")
        if "ST" in nm or "退" in nm:
            continue
        name_map[r["code"]] = nm

def mkt_limit(code):
    """涨停幅度(%). 主板10, 创业板/科创板20, 北交已剔除。"""
    if code.startswith(("688", "8", "4", "30", "301")):
        return 20
    return 10

def load(code):
    fn = os.path.join(KDIR, code + ".json")
    if not os.path.exists(fn):
        return None
    try:
        rows = json.load(open(fn, encoding="utf-8-sig"))
    except Exception:
        return None
    data = []
    for r in rows:
        p = r.split(",")
        data.append(dict(date=p[0], open=float(p[1]), close=float(p[2]),
                         high=float(p[3]), low=float(p[4]), vol=float(p[5]),
                         amt=float(p[6]), amp=float(p[7]), chg=float(p[8])))
    return data

results = []
codes = sorted(name_map.keys())
for code in codes:
    data = load(code)
    if not data or len(data) < 120:
        continue
    L = mkt_limit(code)
    n = len(data)
    # 判定每日是否封死涨停: 涨幅达标 且 收盘基本在最高(封板)
    lim = []  # 涨停日索引
    one_word = 0  # 一字板次数(开盘即封, 全程未开)
    for i in range(1, n):
        d = data[i]
        if d["chg"] >= L - 0.6 and d["close"] >= d["high"] * 0.99:
            lim.append(i)
            if d["open"] >= d["high"] * 0.999:  # 开盘即涨停
                one_word += 1
    if not lim:
        continue
    # 连板段
    boards, run = [], 1
    for k in range(1, len(lim)):
        if lim[k] == lim[k-1] + 1:
            run += 1
        else:
            boards.append(run); run = 1
    boards.append(run)
    max_board = max(boards)
    board_cnt = sum(1 for b in boards if b >= 2)  # ≥2连板发生次数
    limit_cnt = len(lim)
    # 次日溢价(打板族次日卖出收益)
    t1_ret, t1_max = [], []
    for i in lim:
        if i + 1 < n:
            nx = data[i+1]
            t1_ret.append(nx["close"] / data[i]["close"] - 1)
            t1_max.append(nx["high"] / data[i]["close"] - 1)
    t1_ret_m = st.mean(t1_ret) if t1_ret else 0
    t1_max_m = st.mean(t1_max) if t1_max else 0
    recent30 = sum(1 for i in lim if i >= n - 30)
    amt_yi = st.mean([d["amt"] for d in data[-60:]]) / 1e8
    turn = st.mean([d["amt"] for d in data[-60:]])  # placeholder
    one_ratio = one_word / limit_cnt
    lim_amp = st.mean([data[i]["amp"] for i in lim])  # 涨停日平均振幅(封板质量)

    # 评分(打板潜力): 连板高度与次数权重最高
    score = 0
    score += min(max_board, 6) * 8
    score += board_cnt * 6
    score += max(0, t1_ret_m * 100) * 12
    score += min(limit_cnt, 20) * 1.2
    score += min(recent30, 5) * 5
    score -= one_ratio * 12           # 一字板多=难参与
    if amt_yi < 3:     score -= 12    # 流动性不足
    elif amt_yi > 80:  score -= 18    # 大盘难拉板
    elif amt_yi > 40:  score -= 6

    # 实战可参与分(剔除一字板/僵尸/大盘): 重近期活跃+可参与性
    p_score = 0
    p_score += min(recent30, 6) * 10        # 近期激活最关键
    p_score += min(max_board, 6) * 4
    p_score += max(0, t1_ret_m * 100) * 10
    p_score += min(limit_cnt, 20) * 0.8
    if one_ratio <= 0.15:  p_score += 12
    elif one_ratio <= 0.30: p_score += 6
    elif one_ratio <= 0.50: p_score += 0
    else:                   p_score -= 15
    if amt_yi < 3:     p_score -= 14
    elif amt_yi > 60:  p_score -= 10
    if t1_ret_m <= 0:  p_score -= 20         # 次日负溢价直接出局

    results.append(dict(
        code=code, name=name_map[code], mkt=L, limit_cnt=limit_cnt,
        max_board=max_board, board_cnt=board_cnt, t1_ret=t1_ret_m,
        t1_max=t1_max_m, recent30=recent30, amt_yi=amt_yi,
        one_ratio=one_ratio, lim_amp=lim_amp, score=score, p_score=p_score,
    ))

results.sort(key=lambda x: x["score"], reverse=True)
json.dump(results, open(os.path.join(HERE, "limit_up.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

print(f"候选 {len(results)} 只 (含≥1次涨停)\n")
print(f"{'代码':<8}{'名称':<8}{'涨停次':>6}{'最高连板':>8}{'连板次':>6}"
      f"{'次日收':>8}{'次日高':>8}{'近30日':>7}{'成交额亿':>9}{'一字率':>7}{'评分':>7}")
for r in results[:30]:
    print(f"{r['code']:<8}{r['name']:<8}{r['limit_cnt']:>6}{r['max_board']:>8}"
          f"{r['board_cnt']:>6}{r['t1_ret']*100:>7.1f}{r['t1_max']*100:>7.1f}"
          f"{r['recent30']:>7}{r['amt_yi']:>9.1f}{r['one_ratio']*100:>6.0f}{r['score']:>7.1f}")
