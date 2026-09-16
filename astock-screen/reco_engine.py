# -*- coding: utf-8 -*-
"""智能推荐引擎：把《短线操作手法手册》的方法论落到「选股→竞价」的裁决关系上。

本次核心关系调整（相对旧版）：
  旧版：竞价 = 孤立的「高开幅度 gap」买入信号，只看 gap 是否达标就给 buy。
  新版：智能推荐（情绪周期阶段 + 强度持续性六维）为【上游筛选】，
        竞价（高开幅度 gap + 量能公理）为【下游确认】。
        → 只有「六维达标」的标的，其竞价信号才被采信；
          竞价再强，若「量能不确认」也不得出手（缩量高开=诱多，按公理回避）。
本模块被 gen_tomorrow.py（写入六维分/阶段）与 server.py（竞价裁决）共同引用，保证口径一致。
"""
import time


# ============ 情绪周期五阶段 ============
# code: (name, desc, color, 竞价系数k)
# k 仅用于展示/门槛说明：该阶段下竞价买入的整体可信度。
STAGES = {
    "bingdian": ("冰点", "涨停<10，打板期望为负，空仓等情绪修复",               "#1e8449", 0.0),
    "qidong":   ("启动", "10~30只，试错期，只打最强主线/绝对核心",              "#2e86c1", 0.40),
    "fajiao":   ("发酵", "30~60只，主线清晰，打板胜率最高",                     "#b9770e", 0.85),
    "gaochao":  ("高潮", "≥120只，普涨次日必分化，只打最高辨识度龙头",          "#c0392b", 0.60),
    "tuichao":  ("退潮", "高位爆量开板/连板断板，全面降仓回避",                  "#5f5e5a", 0.0),
}


def resolve_stage(tc, override=None):
    """由涨停家数推导当前情绪阶段；override 可手动覆盖（已知退潮/高潮时传入）。"""
    if override in STAGES:
        return override, STAGES[override]
    try:
        tc = int(tc)
    except Exception:
        tc = 0
    if tc >= 120:
        code = "gaochao"
    elif tc >= 30:
        code = "fajiao"
    elif tc >= 10:
        code = "qidong"
    else:
        code = "bingdian"
    return code, STAGES[code]


# ============ 强度持续性六维 ============
# 六维：连板高度 / 封单强度 / 主线板块 / 封板质量 / 流通盘适配 / 量能健康
# 每维 0-100，加权得总分(0-100)。这是「智能推荐」的量化核心。
W = {"board": 0.25, "seal": 0.15, "main": 0.20, "zbc": 0.15, "ltsz": 0.10, "amt": 0.15}


def _d_board(b):
    b = int(b or 1)
    return {1: 20, 2: 46, 3: 70, 4: 88}.get(b, 100 if b >= 5 else 20)


def _d_seal(s):
    s = float(s or 0)
    if s >= 3:
        return 100
    if s >= 1:
        return 85
    if s >= 0.5:
        return 65
    if s >= 0.2:
        return 45
    return 20


def _d_main(m):
    return 100 if m else 35


def _d_zbc(z):
    z = int(z or 0)
    # 0=全天封死(强) 1=换手回封(最佳参与) 2=一般 ≥3=封板差
    return {0: 100, 1: 90, 2: 55}.get(z, 10)


def _d_ltsz(lt):
    lt = float(lt or 0) / 1e8  # 转亿
    if lt < 20:
        return 100
    if lt < 40:
        return 90
    if lt < 80:
        return 75
    if lt < 150:
        return 50
    return 20


def _d_amt(a):
    a = float(a or 0) / 1e8
    if 1 <= a <= 8:
        return 100   # 适中，最利于连板
    if 8 < a <= 15:
        return 80
    if 15 < a <= 30:
        return 45   # 偏爆
    if a > 30:
        return 20   # 爆量一日游
    return 40


def intensity_six(r):
    """输入 r: dict(含 board, seal, is_main, zbc, ltsz, amount)。返回(六维dict, 总分, 评级)。"""
    dims = {
        "board": _d_board(r.get("board")),
        "seal":  _d_seal(r.get("seal")),
        "main":  _d_main(r.get("is_main")),
        "zbc":   _d_zbc(r.get("zbc")),
        "ltsz":  _d_ltsz(r.get("ltsz")),
        "amt":   _d_amt(r.get("amount")),
    }
    total = round(sum(dims[k] * W[k] for k in W), 1)
    grade = "S" if total >= 75 else ("A" if total >= 60 else ("B" if total >= 45 else "C"))
    return dims, total, grade


# ============ 量能公理 ============
# 「量能是唯一开关」：放量=留/持有(确认)，缩量=走/卖(否决)。
# vol_ratio = 当前实时成交额 / 上一交易日成交额(yamt)。
#   竞价时段(9:15-9:25)即为「竞价量比」；盘中为累计成交额比。
def volume_axiom(gap, vol_ratio):
    """返回 (verdict, color, confirm)；confirm: True=量能端支持出手, False=量能端否决, None=无数据不裁决。"""
    if vol_ratio is None:
        return ("量能待确认", "#f59e0b", None)
    if gap is None:
        return ("量能中性", "#f59e0b", None)
    if gap >= 2 and vol_ratio >= 3:
        return ("放量确认·可出手", "#f23645", True)
    if gap >= 2 and vol_ratio < 1:
        return ("缩量诱多·观望", "#2bbf6a", False)
    if gap < 0 and vol_ratio >= 2:
        return ("放量下跌·出", "#2bbf6a", False)
    return ("量能中性·看方向", "#f59e0b", None)


# ============ 智能推荐 → 竞价 关系裁决 ============
def auction_judge(cls, board, gap, phase, stage_code, reco_score, is_main,
                  vol_ratio=None, override_stage=None):
    """上游(智能推荐: 情绪周期+六维) → 下游(竞价高开+量能) 的联合裁决。

    返回 (signal, action, color, buy)。
    """
    if phase in ("pre", "pre_open"):
        return ("待竞价", "名单已就绪；明早 9:15 集合竞价开始后，按实时高开+量能给提示",
                "#7f8c8d", False)
    if cls == "C":
        return ("锚·只看不做", "高位核心锚(≥5板)，盈亏比已差，仅作情绪高度参照；爆量开板=板块退潮信号",
                "#7f8c8d", False)

    # 情绪周期门控：冰点/退潮 直接空仓
    if override_stage in STAGES:
        code, (sname, sdesc, scolor, k) = override_stage, STAGES[override_stage]
    else:
        code, (sname, sdesc, scolor, k) = resolve_stage(None, stage_code)
    if code in ("bingdian", "tuichao"):
        return ("情绪%s·空仓" % sname, "%s：%s" % (sname, sdesc), "#2bbf6a", False)

    if gap is None:
        return ("无行情", "未取到实时价，请刷新或检查行情接口", "#7f8c8d", False)

    # 启动期：只打最强主线
    if code == "qidong" and not is_main:
        return ("启动期·非主线放弃", "启动期试错，仅参与最强主线；该票非主线，不参与", "#7f8c8d", False)
    # 高潮期：只打最高辨识度（3板以上 或 主线）
    if code == "gaochao" and not (int(board or 0) >= 3 or is_main):
        return ("高潮期·非核心放弃", "普涨次日必分化，只打最高辨识度龙头", "#7f8c8d", False)

    # 智能推荐门槛：六维不达标，竞价再强也不推荐
    if reco_score < 45:
        return ("六维不达标·放弃", "强度持续性不足（六维分<45），不参与", "#7f8c8d", False)

    b = int(board or 2)
    thr = {4: 4, 3: 3, 2: 2}.get(b, 2)
    vv, vc, vconfirm = volume_axiom(gap, vol_ratio)

    if gap >= thr:
        if vconfirm is True:
            return ("弱转强·量能确认·可买",
                    "高开%+.1f%%(≥%d%%)且放量，换手回封可打；破板3分钟不回封即走" % (gap, thr),
                    "#f23645", True)
        if vconfirm is False:
            return ("高开缩量·诱多观望",
                    "高开%+.1f%%但缩量（量能公理：缩量不追），等放量再决" % gap,
                    "#2bbf6a", False)
        return ("弱转强·待量能确认",
                "高开%+.1f%%达标，%s，量能确认即出手" % (gap, vv),
                "#f59e0b", False)
    if gap >= -1:
        return ("观察", "高开%+.1f%%平开附近，等量能方向确认" % gap, "#f59e0b", False)
    return ("弱/退潮", "低开%+.1f%%，强转弱竞价清；破昨收无条件走" % gap, "#2bbf6a", False)


# ============ 赚钱效应分 (实证加权, 与 reco_score 并行) ============
# 权重来源: 2026-09-04 ~ 09-10 共 274 只名单 / 5 个复盘日的「单维高低组 平均收益差」实测:
#   封单强度 +2.47% | 流通盘适配 +1.41% | 封板质量 +0.98% | 量能健康 +0.83% | 连板高度 +0.76% | 主线板块 +0.26%
# 与 reco_score 的区别:
#   reco_score = 「强度持续性」通用评分(board 0.25 / main 0.20 权重偏高);
#   win_score  = 按实测赚钱边际重新加权, 弱化几乎无效的 main, 强化封单/流通盘。
#   实测: reco_score 与次日涨跌幅 pearson +0.129(选股有效), 但与「竞价开盘买入收益」-0.063(买点反向),
#         故 win_score 只用于「排序选哪只」, 买不买仍由竞价形态(win_pick)裁决。
WIN_W = {"seal": 0.30, "ltsz": 0.20, "zbc": 0.18, "amt": 0.12, "board": 0.12, "main": 0.08}


def win_score(r):
    """赚钱效应分(0-100)。r 需含 board/seal/is_main/zbc/ltsz/amount。"""
    dims = {
        "board": _d_board(r.get("board")),
        "seal":  _d_seal(r.get("seal")),
        "main":  _d_main(r.get("is_main")),
        "zbc":   _d_zbc(r.get("zbc")),
        "ltsz":  _d_ltsz(r.get("ltsz")),
        "amt":   _d_amt(r.get("amount")),
    }
    return dims, round(sum(dims[k] * WIN_W[k] for k in WIN_W), 1)


def yz_risk(r):
    """一字风险预判(盘后可用) —— 封单额/流通市值 比与次日竞价高开强相关。

    306 只样本实测(r=0.317, 强于封单绝对值 0.209):
      封单/流通 >=2.0% -> 一字率 35%, 次日均涨 +3.67% 但开买 -1.42%
      封单/流通 1.0~2.0% -> 一字率 11%, 均涨 +2.56%, 开买 -0.45%(各档最优) <= 甜蜜点
      封单/流通 0.5~1.0% -> 一字率  4%, 均涨 +0.62%, 开买 -1.01%
      封单/流通 <0.5%   -> 一字率  2%, 均涨 +0.12%, 开买 -1.20%
    """
    seal = r.get("seal") or 0        # 封单额(亿元)
    ltsz = r.get("ltsz") or 0        # 流通市值(元)
    if not ltsz:
        return {"ratio": None, "level": "未知", "color": "#94a3b8",
                "note": "缺流通市值, 无法预估一字风险"}
    ratio = seal * 1e8 / ltsz * 100
    if ratio >= 5:
        return {"ratio": ratio, "level": "极高", "color": "#2bbf6a",
                "note": "封单/流通 %.2f%% — 明早大概率一字, 买不进" % ratio}
    if ratio >= 2:
        return {"ratio": ratio, "level": "高", "color": "#f59e0b",
                "note": "封单/流通 %.2f%% — 实测 35%% 概率顶一字, 重点看明早是否给买点" % ratio}
    if ratio >= 1:
        return {"ratio": ratio, "level": "低", "color": "#f23645",
                "note": "封单/流通 %.2f%% — 甜蜜点: 一字率仅 11%%, 可买入性最优" % ratio}
    if ratio >= 0.5:
        return {"ratio": ratio, "level": "低", "color": "#f23645",
                "note": "封单/流通 %.2f%% — 一字率 4%%, 但承接偏弱需看竞价量能" % ratio}
    return {"ratio": ratio, "level": "低", "color": "#94a3b8",
            "note": "封单/流通 %.2f%% — 承接弱, 次日平均仅 +0.12%%" % ratio}


# ===================== 买点门控 buy_zone (2026-09-14 复盘实证) =====================
# 342 只 / 7 个复盘日, 按「当日竞价缺口」分档 —— 口径为 竞价开盘价买入 → 当日收盘:
#   档位            只数  晋级率  avg涨跌   avg开买   开买胜率
#   顶一字>=9.8%     34    82%   +8.07%   -1.75%     9%
#   高开5~9.8%      56    41%   +5.21%   -1.77%    52%
#   小高开1~5%     112    12%   +1.44%   -1.18%    39%
#   平开0~1%        46     7%   -0.18%   -0.42%    43%
#   微低开-1~0%     41     2%   -2.01%   -1.47%    27%
#   低开-4~-1%      39    13%   +0.21%   +2.50%    62%   <= 唯一强正期望
#   深低开<-4%      14     7%   -5.17%   +0.62%    36%
# 核心结论:
#   1) 「当日涨幅榜」与「竞价买入收益」方向相反 —— 缺口越大涨幅越好, 但开盘买越亏。
#      故选股(win_score)与买点(buy_zone)是两个正交问题, 不得混用。
#   2) 唯一可买窗口 = gap ∈ [-4%, -1%)。微低开(-1~0)看似安全实为陷阱(胜率27%)。
#   3) 旧 win_pick 对 0~+5% 加 8 分、对 -4~-2% 扣 5 分 —— 与本实证相反, 已同步修正。
BUY_ZONES = [
    (9.8,  1e9,  "yz",   "顶一字·买不进",  "#94a3b8", False, "实测82%晋级但仅9%开买胜率,基本买不进"),
    (5.0,  9.8,  "high", "高开过甚·放弃",  "#2bbf6a", False, "该档56只开买-1.77%,当日涨幅(+5.21%)全被缺口吃掉"),
    (1.0,  5.0,  "mid",  "小高开·观望",    "#f59e0b", False, "该档112只(占样本33%)开买-1.18%,当前最大敞口"),
    (0.0,  1.0,  "flat", "平开·观望",      "#f59e0b", False, "该档46只开买-0.42%,无正期望"),
    (-1.0, 0.0,  "mic",  "微低开·观望",    "#f59e0b", False, "陷阱档:41只开买-1.47%、胜率仅27%"),
    (-4.0, -1.0, "gold", "低开·黄金买点",  "#f23645", True,  "唯一强正期望:39只+2.50%、开买胜率62%"),
    (-1e9, -4.0, "deep", "深低开·高风险",  "#f59e0b", False, "该档14只 avg涨跌-5.17%,需翻红确认"),
]

# 形态加减分(排序用) —— 与 BUY_ZONES 同源, 改一处即可全链路生效
_BUY_ADJ = {"gold": 20, "deep": -8, "mic": -10, "flat": -5,
            "mid": -12, "high": -25, "yz": -40}


def buy_zone(gap):
    """买点门控 —— 由竞价缺口判定「可买 / 观望 / 放弃」。

    与 win_score 的「选哪只」职责正交: win_score 决定盯哪几只, buy_zone 决定买不买。
    返回 dict(code/label/color/buy/gap/note); gap 为 None 时 buy=None(待竞价确认)。
    """
    if gap is None:
        return {"code": None, "label": "待竞价确认", "color": "#94a3b8",
                "buy": None, "gap": None, "note": "尚无竞价缺口数据, 无法判定买点"}
    for lo, hi, code, label, color, buy, note in BUY_ZONES:
        if lo <= gap < hi:
            return {"code": code, "label": label, "color": color,
                    "buy": buy, "gap": round(gap, 2), "note": note}
    return {"code": None, "label": "待确认", "color": "#94a3b8",
            "buy": None, "gap": round(gap, 2), "note": ""}


def win_pick(r, gap=None, stage_code=None, mood=None):
    """竞价「赚钱效应」精选裁决 —— 决定是否进入 TOP3 展示位。

    返回 (adj, label, color, ok, reason)
      adj    : 赚钱效应分 + 形态加减(排序用)
      ok     : True = 可进入精选位
      reason : 判定依据(实测口径)

    实证规则: 形态是准入门槛 —— 顶一字 46% 买不进; 高开5%+ 31% 仅薄肉;
              平开~小高开 / 低开 贡献全部大肉。故形态做硬扣分而非加分。
    """
    _, ws = win_score(r)
    adj, reasons = ws, []

    bz = buy_zone(gap)
    if gap is not None:
        # 形态加减分与 buy_zone 同源(342 只实证), 防止两处口径漂移。
        # 本次修正了旧口径的反向错误: 旧版对 0~+5%(实测开买 -1.18%) 加 8 分、
        # 对 -4~-2%(实测黄金区) 扣 5 分, 会导致看板推荐负期望档、惩罚正期望档。
        adj += _BUY_ADJ.get(bz["code"], 0)
        reasons.append("%s(%+.1f%%)·%s" % (bz["label"], gap, bz["note"]))

    if (r.get("seal") or 0) >= 1:
        reasons.append("封单%.2f亿(实测最强单维)" % r["seal"])
    if (r.get("zbc") or 0) >= 3:
        adj -= 12; reasons.append("开板%d次·封板质量差" % r["zbc"])
    decline = bool(mood and "亏钱效应" in mood)

    # 一字风险(盘后可用): 封单/流通比过高 -> 明早大概率买不进, 需降权(买不进的票收益=0)
    risk = yz_risk(r)
    ratio = risk["ratio"]
    blocked = False
    if ratio is not None:
        if ratio >= 5:
            adj -= 30; blocked = True
            reasons.append(risk["note"])
        elif ratio >= 2:
            adj -= 12; reasons.append(risk["note"])
        elif ratio >= 1:
            adj += 5; reasons.append(risk["note"])
        else:
            reasons.append(risk["note"])

    if blocked:
        return (adj, "一字风险极高·大概率买不进", "#2bbf6a", False,
                "；".join(reasons) + "；等竞价看是否给低吸点, 不给就放弃")
    if r.get("cls") == "C":
        return (adj, "高位锚·只看不做", "#7f8c8d", False,
                "；".join(reasons) or "高位核心锚，盈亏比差")
    if stage_code in ("bingdian", "tuichao") or decline:
        tag = {"tuichao": "退潮期", "bingdian": "冰点"}.get(stage_code, "亏钱效应扩散")
        return (adj - 20, "%s·只低吸轻仓" % tag, "#2bbf6a", False,
                "%s：降仓或空仓，仅做形态最优的低吸" % tag + ("；" + "；".join(reasons) if reasons else ""))
    if gap is not None and gap >= 5:
        return (adj, "形态不佳·放弃", "#2bbf6a", False, "；".join(reasons))
    if gap is not None and gap < -4:
        return (adj, "深低开·高风险", "#f59e0b", False, "；".join(reasons))
    if gap is not None:
        # -4%~+5% 区间: 保留精选展示位(「盯哪几只」), 但「买不买」由 buy_zone 单独裁决。
        # 旧版把 -4~-2% 黄金区误判为「深低开·待翻红」并排除, 已修正。
        return (adj, bz["label"], bz["color"], True, "；".join(reasons))
    return (adj, "次日首选·等竞价确认", "#f23645", True,
            "；".join(reasons) or "六维与形态均达标")
