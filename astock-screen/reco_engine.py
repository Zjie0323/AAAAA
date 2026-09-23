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
import os as _os, re as _re, json as _json, glob as _glob, datetime as _dt, subprocess as _sp


# ============ 三套评分体系的职责边界（2026-09-18 补文档） ============
# 同一批涨停票会经过三个打分函数，它们的权重方向**刻意不同**，因为目标不同。
# 曾因缺此说明而被误读为「口径不一致的 bug」，故在此固定边界 —— 改动前先确认改的是哪一个：
#
#   1) _score_stock（server.py，累加制 ±3）—— 智能推荐的**选股**
#      目标：找「分歧低吸」标的 → 因此**偏爱 2~3 板**(+3)、惩罚高位(≥5板 -3)。
#      这是唯一的「选股」评分。
#   2) intensity_six（本模块，百分制，board .25 / main .20）—— **强度持续性**
#      目标：衡量「这波还能不能延续」→ 连板越高越强，但 ≥5 板已有折价(62)。
#      用途：作为竞价信号的上游准入门槛（评分 <45 则其竞价信号不被采信）。
#   3) win_score（本模块，百分制，seal .30 / ltsz .20）—— **赚钱效应排序**
#      目标：按实测收益边际排「盯哪几只」→ 弱化几乎无效的 main(.08)，强化封单/流通盘。
#
# 对「板块/主线」维度的权重差异最大，这是设计而非疏漏：
#   _score_stock 用同行业涨停家数(≥5家 +3)；intensity_six 给 is_main 权重 .20；
#   win_score 仅 .08 —— 实测 main 的赚钱边际只有 +0.26%，六维里却占 .20，故刻意压低。
#
# 关键：「选股」与「买点」是正交的两件事 ——
#   选哪几只 → win_score / _score_stock；  买不买 → buy_zone（竞价缺口分档）。
#   不得混用：实测「当日涨幅榜」与「竞价买入收益」方向相反（缺口越大涨幅越好，但开盘买越亏）。


# ============ 情绪周期五阶段 ============
# code: (name, desc, color, 竞价系数k)
# k 的门控语义(2026-09-18 起)：**k<=0 的阶段一律空仓**。auction_judge 据此判定，
#   不再硬编码阶段名 —— 这样 k 成为单一事实源，新增零 k 阶段自动获得空仓门控。
#   修复前门控写的是 `code in ("bingdian","tuichao")`，与 k 字段重复表达同一条规则。
# 档位口径（与 em.mood 的关系，两侧**刻意不同**，勿强行统一）：
#   stage 是「打板择时」的粗糙分档：<10 冰点 / 10~29 启动 / 30~119 发酵 / ≥120 高潮；
#   mood  是「盘面定性」的展示分档：<10 / 10~29 / 30~59 / 60~119 / ≥120 共 5 档。
#   故 60~119 区间：stage 仍归「发酵」(该区间打板胜率仍高)，mood 显示「情绪偏暖」。
#   ⚠️ 下列 desc 文案必须与 resolve_stage 的 tc 阈值逐字对应，否则会重演
#      「文案写 30~60 只、实现却是 tc>=30」的描述与实现不一致。
STAGES = {
    "bingdian": ("冰点", "涨停<10，打板期望为负，空仓等情绪修复",               "#1e8449", 0.0),
    "qidong":   ("启动", "涨停10~29只，试错期，只打最强主线/绝对核心",          "#2e86c1", 0.40),
    "fajiao":   ("发酵", "涨停30~119只，主线清晰，打板胜率最高",                "#b9770e", 0.85),
    "gaochao":  ("高潮", "涨停≥120只，普涨次日必分化，只打最高辨识度龙头",      "#c0392b", 0.60),
    "tuichao":  ("退潮", "高位爆量开板/连板断板，全面降仓回避",                  "#5f5e5a", 0.0),
}


def resolve_stage(tc, override=None, zb_tc=None):
    """由涨停家数(+炸板率)推导当前情绪阶段；override 可手动覆盖（已知退潮/高潮时传入）。

    退潮自动推导（2026-09-18 新增）：
      修复前 STAGES 里的 tuichao **只能**由 override 返回，而生产链路上
      gen_tomorrow.py 调用的是 resolve_stage(tc) —— 从不传 override，
      于是 auction_judge 里「情绪退潮·空仓」这条门控形同虚设、永远不可能命中。
      现补上基于盘面数据的推导：涨停家数仍有 30+（不是冰点）但炸板率 ≥40%
      = 封板资金不坚决、高位一致转分歧，即退潮特征。
    阈值 40% 与 em.mood 的「炸板率 ≥40% 极高」保持同一口径（单一事实源）；
    且仅在 tc>=30 时判退潮 —— tc<30 本身已由 qidong/bingdian 覆盖，避免抢档。
    炸板率 = zb / (zb + tc)，与 mood() 一致。
    """
    ensure_strategy()                       # 热重载: 策略文件变更无需重启
    if override in STAGES:
        return override, STAGES[override]
    try:
        tc = int(tc)
    except Exception:
        tc = 0
    zb_ratio = None
    if zb_tc is not None:
        try:
            zb_tc = int(zb_tc)
            if zb_tc + tc > 0:
                zb_ratio = zb_tc / float(zb_tc + tc) * 100.0
        except Exception:
            zb_ratio = None
    if zb_ratio is not None and zb_ratio >= 40 and tc >= 30:
        return "tuichao", STAGES["tuichao"]
    if tc >= 120:
        code = "gaochao"
    elif tc >= 30:
        code = "fajiao"
    elif tc >= 10:
        code = "qidong"
    else:
        code = "bingdian"
    return code, STAGES[code]


def stage_k(stage_code):
    """阶段 code -> 竞价系数 k；未知阶段返回 None。

    门控统一入口（2026-09-18 新增）：所有「该阶段能不能参与」的判定都应当走
    `k <= 0`，而不是硬编码阶段名。这样 k 成为**单一事实源** —— 新增零 k 阶段
    自动获得空仓门控，不必在多处同步改（此前 auction_judge 与 win_pick 各写一遍
    阶段名，改一处漏一处）。
    """
    st = STAGES.get(stage_code)
    return None if st is None else st[3]


# ============ 强度持续性六维 ============
# 六维：连板高度 / 封单强度 / 主线板块 / 封板质量 / 流通盘适配 / 量能健康
# 每维 0-100，加权得总分(0-100)。这是「智能推荐」的量化核心。
W = {"board": 0.25, "seal": 0.15, "main": 0.20, "zbc": 0.15, "ltsz": 0.10, "amt": 0.15}


def _d_board(b):
    """连板高度维(0-100)。

    ≥5 板**刻意不给满分**（2026-09-18 修正）：修前返回 100，使高位票在
    「强度持续性」维度拿满分 —— 而 5 板以上在交易纪律里是 C 类「只看不做」
    （盈亏比已差，爆量开板即预示退潮）。满分会被前端的 S 级评级、以及
    auction_judge 的六维准入门槛（≥45 才采信竞价信号）一并放大成误导。
    现值 62 落在 2 板(46) 与 3 板(70) 之间略低：承认其「高」，但对
    「还能不能继续」的持续性打折价。
    影响面：本函数被 intensity_six 与 win_score 共用，故 ≥5 板票的 win_score
    同步降约 4.6 分（board 权重 0.12 × 38）。C 类在 win_pick 中本就返回
    ok=False、不进 TOP3 展示位，故不改变选股结果，仅修正分数本身的误导。
    """
    b = int(b or 1)
    return {1: 20, 2: 46, 3: 70, 4: 88}.get(b, 62 if b >= 5 else 20)


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
    ensure_strategy()                       # 热重载: 策略文件变更无需重启
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


# ============ 竞价量比否决门控 (2026-09-22 实证) ============
# 生产服务器 5 个真实定盘快照 / 12 条正常推送批次实测（买入价 = 当日开盘价 = 9:25 撮合价）：
#   vol_ratio    n   当日收盘均值   胜率
#   < 2.5        4      +0.22%      50%
#   2.5 ~ 3.5    5      +1.39%      60%   <= 甜蜜点
#   >= 3.5       3      -2.28%      33%   <= 唯一坏档
#     被挡三只明细：太阳电缆 5.61 → -4.25% / 英联股份 8.57 → -3.39% / 电科思仪 4.30 → +0.80%
# 机制：「低开 + 显著放量」不是洗盘，是真出货 —— 与「低开是黄金买点」的假设直接冲突。
#   缺口门控 buy_zone 单独看会把这批票判成 gold 档，必须由量能端否决。
# 该三只的 vol_verdict 本就是「放量下跌·出」，但历史上只把它当展示文案、不参与门控。
# 现升级为**硬否决**（在排序中降位，而非全池剔除 —— 保「不空仓」）。
# 效果（同批样本，被挡后不补位的保守口径，即本表效果为下界）：
#   当日收盘 +0.08% -> +0.87%(n=9) / 次日开盘 +0.68% -> +1.69%(n=6) / 次日收盘 +1.99% -> +3.00%(n=6)
#   同期上证 +0.23% / +0.74% / +1.07% —— 调整后三口径全面跑赢基准。
# ⚠️ 必须记住的局限：n 极小，且**改善几乎全部来自 09-17 这一天**（挡掉那两只 -4.25%/-3.39%）；
#   只看 09-18/09-21/09-22 三天，本规则是 **-0.09pp 的轻微负贡献**。
#   故它的定位是「让执行与自身判词一致」的修复 + 有方向的经验规则，**不是已证实的 alpha**；
#   要升级为「全池剔除」须先由 astock-screen/shadow/ 的前向记录确认。
VOL_VETO_RATIO = 3.5
# 量比口径合理性上界（%）。竞价时段「集合竞价成交额 / 前一交易日全天成交额」实测落在
# 1%~9%（12 条样本），即使最热的小盘票也极少超过 20%。>30 视为**口径异常而非真放量**：
# 典型来源是盘中补定的名单 —— 其 yamt 与当日累计 amt 同基准，算出 amt/yamt≈1 →
# vol_ratio≈100（实测 09-16 批次为 99.9999999999 / 100.0025 / 100.00075）。
# 此类占位值若参与否决，会把整批票误标「放量下跌」，故设上界拦截。
VOL_VETO_MAX = 30.0


def vol_veto(gap, vol_ratio):
    """竞价「放量下跌」硬否决。返回 (vetoed, reason)。

    生效条件（全部满足才否决）：
      ① gap 与 vol_ratio 均为有效数 —— 缺失时**一律不否决**（缺失≠放量，宁放行不误杀）；
      ② VOL_VETO_RATIO <= vol_ratio <= VOL_VETO_MAX —— 上界挡口径异常的占位值；
      ③ gap < 0 —— 高开放量属「放量确认」，不否决。
    调用方（rt/bidwatch.py）只做**降位**，不做剔除，以保证「不空仓」。
    """
    if gap is None or vol_ratio is None:
        return False, ""
    try:
        vr = float(vol_ratio)
        gp = float(gap)
    except Exception:
        return False, ""
    if vr > VOL_VETO_MAX:
        return False, ""            # 口径异常（占位值），不裁决
    if vr >= VOL_VETO_RATIO and gp < 0:
        return True, ("竞价量比 %.2f ≥ %.1f 且低开 %+.2f%% —— 放量下跌·真出货"
                      "（实测该档 -2.28%%、胜率 33%%），本日降位" % (vr, VOL_VETO_RATIO, gp))
    return False, ""


# ============ 卖点纪律 (2026-09-22 补 —— 策略此前只定义买点、从未定义卖点) ============
# 同一批样本、同一可分子集(n=6，已剔 vr>=3.5)三口径实测：
#   9:25 买 -> 当日收盘卖   +1.47%
#   9:25 买 -> 次日开盘卖   +1.69%
#   9:25 买 -> 次日收盘卖   +3.00%   <= 最优
# 同期上证同口径 +0.23% / +0.74% / +1.07%。
# 结论：**隔夜那段才有正期望，且是超额收益的主要来源**；当日平仓≈白干。
# ⚠️ 局限：样本 3 个交易日、n=6；且「次日收盘卖」承担隔夜跳空风险，勿误读为无风险最优。
#   n=9 口径下当日 +0.87% / 次日收 +3.00%，方向一致但样本更薄。
EXIT_RULE = "T+1"
EXIT_NOTE = "T+1收盘前卖"          # 推送 thing 字段用，≤15 字符
EXIT_DESC = ("次日(隔夜)收盘前卖出：实测三口径 当日 +1.47% / 次日开 +1.69% / "
             "次日收 +3.00%（同 n=6）；隔夜段是超额收益主要来源，当日平仓≈零期望")


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
    # 2026-09-22：阈值由 2 提到 VOL_VETO_RATIO(3.5) —— 与 vol_veto 共用同一常量，
    # 避免「展示说放量下跌、门控却不拦」的两套口径。实测 2.5~3.5 反为最优档(+1.39%)，
    # 原阈值 2 会把这个甜蜜点误标成「放量下跌·出」。
    if gap < 0 and vol_ratio >= VOL_VETO_RATIO:
        return ("放量下跌·出", "#2bbf6a", False)
    return ("量能中性·看方向", "#f59e0b", None)


# ============ 智能推荐 → 竞价 关系裁决 ============
def auction_judge(cls, board, gap, phase, stage_code, reco_score, is_main,
                  vol_ratio=None, override_stage=None):
    """上游(智能推荐: 情绪周期+六维) → 下游(竞价高开+量能) 的联合裁决。

    返回 (signal, action, color, buy)。

    门控顺序（逐级淘汰，任一不通过即不买）：
      阶段(k<=0 空仓) → C类只看不做 → 启动期非主线放弃 →
      高潮期非(≥3板∨主线)放弃 → 六维<45放弃 → gap 达阈 且 量能不否决。

    ⚠️ stage_code 由 gen_tomorrow.py 写入名单。2026-09-18 之前该字段**不可能**
      为 "tuichao"（退潮只能靠 override 传入，而生产链路无人传），故下方的
      「空仓」门控对退潮阶段完全失效。现已由 resolve_stage 的炸板率推导补齐；
      若观察到的名单里 stage_code 仍无 tuichao，先确认名单是否为旧版本生成
      （需重跑 gen_tomorrow.py 才会带上 zb_tc 推导）。
    """
    if phase in ("pre", "pre_open"):
        return ("待竞价", "名单已就绪；明早 9:15 集合竞价开始后，按实时高开+量能给提示",
                "#7f8c8d", False)
    if cls == "C":
        return ("锚·只看不做", "高位核心锚(≥5板)，盈亏比已差，仅作情绪高度参照；爆量开板=板块退潮信号",
                "#7f8c8d", False)
    # 高位板剔除（2026-09-23 老张：「4 板以上不考虑」）—— 放在 cls=="C" 之后，
    # 保证 4 板走本门控、≥5 板仍走上面的「锚」语义，两处不重复。
    if not board_allowed(board):
        return ("高位板·放弃",
                "%d 板已超 %d 板上限（高位盈亏比差，老张口径：4 板以上不考虑），不参与"
                % (int(board or 0), int(globals().get("MAX_BOARD", MAX_BOARD))),
                "#7f8c8d", False)

    # 情绪周期门控：k<=0（冰点/退潮）直接空仓
    if override_stage in STAGES:
        code, (sname, sdesc, scolor, k) = override_stage, STAGES[override_stage]
    else:
        code, (sname, sdesc, scolor, k) = resolve_stage(None, stage_code)
    if k <= 0:
        # 用 k 判定而非硬编码阶段名 —— k 是「该阶段竞价买入的整体可信度」，
        # 0 即不可参与。这样新增零k阶段自动获得空仓门控，无需两处同步改。
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
    # 2026-09-23 老张要求「高开」：把 0 ~ thr 这一段也判可买，否则会与 buy_zone
    # （高开模式的 HIGH_BUY_ZONES = flat+mid）互相矛盾 —— 那正是此前「门控互斥 93%」的老问题。
    if gap >= 0 and globals().get("GAP_MODE", "low") == "high":
        if vconfirm is False:
            return ("高开缩量·诱多观望",
                    "高开%+.1f%%但缩量（量能公理：缩量不追），等放量再决" % gap,
                    "#2bbf6a", False)
        return ("高开·可买", "高开%+.1f%%，%s；冲高不板即兑现" % (gap, vv),
                "#f23645", True)
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
#
# ⚠️ 可信度修正（2026-09-22，**改阈值前必读**）：
#   上表 gold 档「+2.50%、开买胜率 62%（n=39）」已被两轮独立样本削弱：
#     ① 服务器完整快照 12 条同档实测 **+0.08%、胜率 50%**（缩水约 1/30）；
#     ② 子档结论两轮**方向相反** —— 上轮 -1~-2% 最差；本轮 -2~-1.5% 最差(-1.79%)、
#        -1.5~-1% 最好(+0.78%)，n 仅 3~5。
#   → 结论：**本表不足以支撑改动 gap 阈值**，gold 档的「唯一正期望」表述应降级为
#     「历史样本强、近期样本不成立，待前向确认」。当前处理：阈值**刻意不动**，
#     只把「不空仓 + 量比否决（VOL_VETO_RATIO）」作为本次唯一的策略调整。
#     禁止再据单轮样本调档（已连续两轮被推翻）。
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

# ============ 买点模式（2026-09-23 老张要求：高开博弈涨停）============
# 切换：改 strategy/<YYYY>/strategy_<YYYYMM>.json 的 GAP_MODE（保存即热重载，无需重启）。
#   "low"  = 低开黄金档 gap ∈ [-4,-1)   ← 2026-09-23 之前的长期口径
#   "high" = 高开博弈  gap ∈ [0,+5)     ← 老张 2026-09-23 明确要求
#
# ⚠️⚠️ 数据反对本次切换，此处如实留档（详见 _astock_gap_eval_20260923.md）：
#   14 个买入日 / n=1114 实测：低开档 14 日累积 **+18.87%**（10/14 天赚）、盈亏比 1.56；
#                              高开档 14 日累积 **-13.05%**（仅 3/14 天赚）、盈亏比 0.94。
#   逐日配对：低开占优 **11/14 天**。高开≥+5% 档胜率 53%（比低开 49% 还高）但盈亏比仅 0.46 → 期望 -1.37%。
#   根因：**高开把涨停收益让渡给了前一日持有人** —— 同样赌中涨停，
#         高开档只赚 +4.17%、不涨停亏 -1.94%；低开档赚 +12.52%、不涨停仅亏 -0.25%。
#   → 本切换为**老张明确指令**，不是数据结论。回切一行：GAP_MODE 改回 "low"。
GAP_MODE = "high"
HIGH_BUY_ZONES = ("flat", "mid")     # 高开模式下 buy=True 的档：0~+1% 与 +1~+5%

# 高位板剔除（老张要求「4 板以上不考虑」）—— board > MAX_BOARD 直接放弃。
MAX_BOARD = 3
# 首板优先（老张要求「优先选择首板涨停后的」）—— 排序时 board=1 优先。
PREFER_FIRST_BOARD = True

# 高开模式的形态加减表：可买档必须给正分，否则排序会把可买档压到低开档之后
# （win_adj 是推送实际主排序键，见 rt/bidwatch.py::_pick_key）
_BUY_ADJ_HIGH = {"flat": 15, "mid": 20, "high": -20, "yz": -40,
                 "mic": -10, "gold": -5, "deep": -15}

# 高开模式下可买档的 note 覆盖 —— 原表说明是为低开口径写的（「无正期望」「最大敞口」），
# 直接沿用会与「可买」自相矛盾。数字为 2026-09-23 全池可买口径实测。
HIGH_MODE_NOTE = {
    "flat": "高开模式可买档(0~+1%)：⚠️ 14日实测 +0.61%/胜率50%(n=118)、盈亏比 1.46",
    "mid":  "高开模式可买档(+1~+5%)：⚠️ 14日实测 -0.41%/胜率43%(n=330)、盈亏比 1.17",
}


def cur_buy_adj():
    """按 GAP_MODE 返回生效的形态加减表（每次读模块全局，热重载安全）。"""
    if globals().get("GAP_MODE", "low") == "high":
        return globals().get("_BUY_ADJ_HIGH", _BUY_ADJ_HIGH)
    return globals().get("_BUY_ADJ", _BUY_ADJ)


def zone_buy(code, default):
    """该档是否可买 —— 唯一入口，避免 buy_zone / 展示 / 推送三处口径漂移。"""
    if globals().get("GAP_MODE", "low") == "high":
        return code in tuple(globals().get("HIGH_BUY_ZONES", HIGH_BUY_ZONES))
    return default


def board_allowed(board):
    """高位板过滤：board > MAX_BOARD 即超限（老张口径：4 板以上不考虑）。

    board 缺失/为 0 时返回 True（未知板数不误杀）。
    """
    try:
        b = int(board or 0)
    except Exception:
        return True
    if b <= 0:
        return True
    return b <= int(globals().get("MAX_BOARD", MAX_BOARD))


def buy_zone(gap):
    """买点门控 —— 由竞价缺口判定「可买 / 观望 / 放弃」。

    与 win_score 的「选哪只」职责正交: win_score 决定盯哪几只, buy_zone 决定买不买。
    返回 dict(code/label/color/buy/gap/note); gap 为 None 时 buy=None(待竞价确认)。
    """
    ensure_strategy()                       # 热重载: 策略文件变更无需重启
    if gap is None:
        return {"code": None, "label": "待竞价确认", "color": "#94a3b8",
                "buy": None, "gap": None, "note": "尚无竞价缺口数据, 无法判定买点"}
    for lo, hi, code, label, color, buy, note in BUY_ZONES:
        if lo <= gap < hi:
            b = zone_buy(code, buy)
            if globals().get("GAP_MODE", "low") == "high":
                # 高开模式：文案跟 buy 标志一致，避免出现「黄金买点」却 buy=False 的自相矛盾
                if b:
                    label = label.split("·")[0] + "·可买"
                    color = "#f23645"
                    note = globals().get("HIGH_MODE_NOTE", HIGH_MODE_NOTE).get(code, note)
                elif buy is True:
                    label = label.split("·")[0] + "·本轮不买"
                    color = "#7f8c8d"
            return {"code": code, "label": label, "color": color,
                    "buy": b, "gap": round(gap, 2), "note": note}
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
        adj += cur_buy_adj().get(bz["code"], 0)   # 按 GAP_MODE 选表（高开模式下可买档给正分）
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
    # 高位板剔除（2026-09-23 老张：「4 板以上不考虑」）。
    # 注意：这里只让 ok=False，**不剔除** —— 下游 _pick_key 把它降到第 3 档，
    # 仍遵守「任何单一门控都无法清空推送」的「不空仓」原则。
    if not board_allowed(r.get("board")):
        return (adj, "高位板·只看不做", "#7f8c8d", False,
                "；".join(reasons) + "；%d 板超 %d 板上限（4 板以上不考虑）"
                % (int(r.get("board") or 0), int(globals().get("MAX_BOARD", MAX_BOARD))))
    if r.get("cls") == "C":
        return (adj, "高位锚·只看不做", "#7f8c8d", False,
                "；".join(reasons) or "高位核心锚，盈亏比差")
    # 情绪门控走 k（单一事实源），不再硬编码阶段名 —— 与 auction_judge 同口径。
    # 未知阶段（None）不误判为空仓，仍由 mood 的「亏钱效应」兜底。
    _k = stage_k(stage_code)
    if (_k is not None and _k <= 0) or decline:
        tag = {"tuichao": "退潮期", "bingdian": "冰点"}.get(stage_code)
        if not tag:
            _st = STAGES.get(stage_code)
            tag = ("%s期" % _st[0]) if _st else "亏钱效应扩散"
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
STRATEGY_PARAM_KEYS = ["STAGES", "W", "WIN_W", "BUY_ZONES", "_BUY_ADJ",
                       "_BUY_ADJ_HIGH", "GAP_MODE", "HIGH_BUY_ZONES",
                       "MAX_BOARD", "PREFER_FIRST_BOARD",
                       "VOL_VETO_RATIO", "VOL_VETO_MAX", "EXIT_RULE", "EXIT_NOTE"]
STRATEGY_NOTES = {
    "STAGES":    "情绪周期五阶段",
    "W":         "强度持续性六维权重(reco_score)",
    "WIN_W":     "赚钱效应分实证权重(win_score)",
    "BUY_ZONES": "竞价缺口分档门控(buy_zone, 2026-09-14 复盘实证)",
    "_BUY_ADJ":  "形态加减分·低开模式(排序用)",
    # 2026-09-23 新增（老张要求：高开博弈涨停 + 首板优先 + 剔除4板以上）：
    "GAP_MODE":          '买点模式: "low"=低开黄金档[-4,-1) / "high"=高开博弈[0,+5)。'
                         '⚠️ 实测高开档 14日累积-13.05% vs 低开+18.87%（逐日低开占优11/14天），'
                         '本次切换是老张明确指令、非数据结论；回切改回 "low" 即可',
    "HIGH_BUY_ZONES":    "高开模式下 buy=True 的档位（默认 flat+mid = gap ∈ [0,+5)）",
    "_BUY_ADJ_HIGH":     "形态加减分·高开模式（可买档必须给正分，否则 win_adj 排序会把高开票压后）",
    "MAX_BOARD":         "连板上限：board > 此值即放弃（老张口径「4板以上不考虑」→ 3）",
    "PREFER_FIRST_BOARD": "首板优先（老张口径「优先选择首板涨停后的」）→ 排序时 board=1 优先",
    # 2026-09-22 新增（竞价推送策略调整）：
    "VOL_VETO_RATIO": "竞价量比否决阈值(低开+量比≥此值=放量下跌·真出货；实测 ≥3.5 档 -2.28%/胜率33%)",
    "VOL_VETO_MAX":   "量比口径合理性上界(超过视为占位值/口径异常, 不否决；仅调阈值不用改此值)",
    "EXIT_RULE":      "卖点纪律标识(T+1=持到次日；供推送/复盘引用)",
    "EXIT_NOTE":      "卖点提示文案(推送 thing 字段，须 ≤15 字符，超长会被 clip 截断)",
}


def _strategy_root():
    return _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "strategy")


def _yyyymm_of(path):
    m = _re.search(r"strategy_(\d{6})\.json$", _os.path.basename(path))
    return int(m.group(1)) if m else 0


def _git_head():
    try:
        return _sp.check_output(["git", "rev-parse", "HEAD"],
                                cwd=_os.path.dirname(_os.path.abspath(__file__)),
                                stderr=_sp.DEVNULL).decode().strip() or None
    except Exception:
        return None


def load_strategy(yyyymm=None):
    """载入并应用当期策略: 把文件 params 覆盖到模块级常量。"""
    if yyyymm:
        yyyymm = str(yyyymm)
        pp = _os.path.join(_strategy_root(), yyyymm[:4], "strategy_%s.json" % yyyymm)
        if not _os.path.exists(pp):
            raise FileNotFoundError(pp)
    else:
        files = _glob.glob(_os.path.join(_strategy_root(), "*", "strategy_*.json"))
        pp = max(files, key=_yyyymm_of) if files else None
    if not pp or not _os.path.exists(pp):
        return (None, None)
    d = _json.load(open(pp, encoding="utf-8"))
    for k in STRATEGY_PARAM_KEYS:
        if k in d.get("params", {}):
            globals()[k] = d["params"][k]
    return (yyyymm or ("%06d" % _yyyymm_of(pp)), pp)


_strategy_cache = {"sig": (None, None)}


def _latest_strategy_path():
    files = _glob.glob(_os.path.join(_strategy_root(), "*", "strategy_*.json"))
    return max(files, key=_yyyymm_of) if files else None


def ensure_strategy():
    """评分前调用: 当期策略文件较已加载更新(或首次)则热重载, 无需重启进程。"""
    pp = _latest_strategy_path()
    if not pp:
        return
    try:
        m = _os.path.getmtime(pp)
    except OSError:
        return
    if _strategy_cache["sig"] != (pp, m):
        try:
            d = _json.load(open(pp, encoding="utf-8"))
            for k in STRATEGY_PARAM_KEYS:
                if k in d.get("params", {}):
                    globals()[k] = d["params"][k]
            _strategy_cache["sig"] = (pp, m)
        except Exception as _e:
            import sys as _sys
            _sys.stderr.write("[warn] 策略热重载失败, 沿用旧策略: %s\n" % _e)


def _norm_params(p):
    """params 归一化(稳定序列化), 用于判断「内容是否真的变了」——
    忽略 dict 键顺序、tuple 与 list 的差异, 只比较实质取值。"""
    try:
        return _json.dumps(p, ensure_ascii=False, sort_keys=True)
    except Exception:
        return None


def new_strategy(yyyymm=None, force=False):
    """基于当期生效策略(或内置默认)生成一个新的月度策略文件。

    加固(2026-09-19):
      · 幂等 —— 目标文件已存在且 params 完全一致时不重写, 也不刷新 generated_at。
                避免每次执行都产生「只有时间戳变」的无意义 diff, 降低多机 pull 的冲突面。
      · 防误覆盖 —— 目标文件已存在且 params 有差异时默认拒绝写入(需 --force),
                    防止手滑用当期值冲掉当月已调好的权重。

    返回 (path, status):
      created   新文件已创建
      unchanged 已存在且参数一致 -> 未重写(幂等)
      updated   已存在且参数有差异 + 已 --force -> 已覆盖写入
      blocked   已存在且参数有差异但未 --force -> 拒绝覆盖
    """
    if yyyymm is None:
        yyyymm = _dt.datetime.now().strftime("%Y%m")
    yyyymm = str(yyyymm)
    out_dir = _os.path.join(_strategy_root(), yyyymm[:4])
    _os.makedirs(out_dir, exist_ok=True)
    pp = _os.path.join(out_dir, "strategy_%s.json" % yyyymm)
    existed = _os.path.exists(pp)
    params = {k: globals().get(k) for k in STRATEGY_PARAM_KEYS}

    if existed:
        try:
            old = _json.load(open(pp, encoding="utf-8"))
        except Exception:
            old = {}
        if _norm_params(old.get("params")) == _norm_params(params):
            return (pp, "unchanged")
        if not force:
            return (pp, "blocked")
    snap = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "engine_file": _os.path.basename(__file__),
        "git_head": _git_head(),
        "yyyymm": yyyymm,
        "params": params,
        "notes": STRATEGY_NOTES,
    }
    with open(pp, "w", encoding="utf-8") as f:
        _json.dump(snap, f, ensure_ascii=False, indent=2)
    return (pp, "updated" if existed else "created")


try:
    ACTIVE_STRATEGY = load_strategy()
except Exception as _e:
    import sys as _sys
    _sys.stderr.write("[warn] 加载策略文件失败, 回落内置默认值: %s\n" % _e)
    ACTIVE_STRATEGY = (None, None)

_ap = _latest_strategy_path()
try:
    _strategy_cache["sig"] = (_ap, _os.path.getmtime(_ap)) if _ap else (None, None)
except OSError:
    _strategy_cache["sig"] = (None, None)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="策略外置: 生成/查看 strategy/<YYYY>/ 下的月度策略文件")
    ap.add_argument("--new", nargs="?", const="", help="生成新策略文件(可选 YYYYMM, 默认当月)")
    ap.add_argument("--force", action="store_true",
                    help="配合 --new: 目标月份已存在且参数有差异时强制覆盖(默认拒绝)")
    ap.add_argument("--yyyymm", help="指定查看的月份(YYYYMM)")
    ap.add_argument("--show", action="store_true", help="打印当期生效策略")
    a = ap.parse_args()
    if a.new is not None:
        pp, st = new_strategy(a.new or None, force=a.force)
        if st == "unchanged":
            print("未重写(幂等): 该文件已存在且参数完全一致 ->", pp)
        elif st == "blocked":
            print("已拒绝覆盖(防误冲当月权重): 该文件已存在且参数有差异 ->", pp)
            print("  ① 确需重建 -> 加 --force;  ② 想回到代码内置默认值 -> 先移走该文件再 --new")
        else:
            print("已%s策略文件:" % ("更新" if st == "updated" else "生成"), pp,
                  "\n  -> 编辑其中 params 调整权重, 保存后评分函数会自动热加载, 无需重启服务")
    else:
        ym, pp = load_strategy(a.yyyymm)
        print("当期策略月份:", ym, "| 文件:", pp)
        if ym:
            print(_json.dumps({k: globals().get(k) for k in STRATEGY_PARAM_KEYS},
                              ensure_ascii=False, indent=2))
