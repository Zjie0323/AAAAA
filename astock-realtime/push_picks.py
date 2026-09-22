# -*- coding: utf-8 -*-
"""
push_picks.py —— 把「集合竞价精选 TOP3」推送到微信小程序订阅消息
=================================================================
数据源  : 定盘快照 _bid_picks_<YYYYMMDD>.json（9:25 竞价定盘，优先）
          回退 -> http://127.0.0.1:8000/api/bid_watch 的 picks
推送网关: 后端 POST /subscribe/push（与 MCP 工具 send-subscribe-push 同一接口）
依赖    : 仅 Python 标准库（不依赖 httpx / requests）

四种模式
--------
  --mode slots      3 只各占一个独立字段（消耗 1 次订阅授权）★ 推荐
     适用专用模板：s1/s2/s3=精选标的1~3（thing） / time=定盘时间（time） / tip=买点提示（thing）
     每只形如「通达股份002560 -3.7%买」，最坏 19 字符，不触 20 字符上限。

  --mode compact    3 只合并进一个 thing 字段（消耗 1 次订阅授权）
     适用字段少且无时间类型的模板；代价是丢掉 6 位代码，只留 2 字简称

  --mode single     3 只压进一条消息（消耗 1 次订阅授权）
     适用字段多但不便拆分的模板：名称/时间/内容/代码/提示

  --mode per-stock  每只股票一条消息（消耗 3 次订阅授权）
     适用「交易提醒模板」等字段少的模板：thing1=提醒内容 / time2=提醒时间 / thing13=预警类型

时间字段格式
------------
  --time-fmt date-hm  『09-15 09:25』（默认，带日期，便于次日回看时确认是哪一场竞价）
  --time-fmt hm       『09:25』（旧格式）

用法
----
# 0) 看每个 openid 的剩余授权次数（不推送）
python push_picks.py --list-sub

# 1) 预览将推送的内容（不消耗配额）
python push_picks.py --openid of2DX5... --template medVlG... --dry-run

# 2) 真实推送
python push_picks.py --openid of2DX5... --template medVlG... --mode per-stock

# 3) 推给所有有余额的用户
python push_picks.py --openid all --template medVlG... --dry-run

# 4) 时间字段不带日期（回到旧格式）
python push_picks.py --openid of2DX5... --template CvazWorV... --time-fmt hm
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ENV = HERE.parent / "python" / "mcp-subscribe-push" / ".env"
DEFAULT_API = "https://test.yaohaiqing.top/api"
LOCAL_BOARD = "http://127.0.0.1:8000"

# ---------------------------------------------------------------------------
# 模板预设：把「业务字段」映射到「微信模板 key」
#   fields 右侧必须是该模板真实存在的关键词，否则微信返回 47003
# ---------------------------------------------------------------------------
PRESETS: dict[str, dict] = {
    # ⚠️ 2026-09-15 实推实测结论（勿再踩）：
    #   该模板在【微信侧的真实字段】仍是旧形态：
    #       thing1=提醒内容 / time2=提醒时间 / thing13=预警类型
    #   后端 /api/templates 里登记的 5 字段（thing1/2/3 + time4 + thing5）**只是后端记录表**，
    #   改它不会改变微信侧模板定义。按 5 字段推送会被微信拒绝：
    #       errcode 47003, "data.time2.value is empty"
    #   → 微信模板【字段加入后不可修改】，要 5 字段必须**新建模板**（删不掉就新建一个）。
    #     新模板 ID 登记进本 PRESETS，或直接依赖 derive_mapping() 自动推导（零改代码）。
    #   默认模式取 compact：3 只合并成 1 条 → 1 次授权推 3 只（代价：丢 6 位代码）。
    #   想保留代码改成一条一只：加 --mode per-stock（字段映射共用，无需改映射）。
    "medVlGvUS4rGOtT5P6hARasm8PSpd3uiICORFtYmKuA": {
        "name": "交易提醒模板",
        "mode": "compact",
        "fields": {"content": "thing1", "time": "time2", "action": "thing13"},
    },
    # 活动日程提醒 —— 5 个 **thing** 字段（无 time 类型字段），
    #   后端登记：thing4=活动名称 / thing1=活动时间 / thing2=活动内容 /
    #             thing5=活动地点 / thing3=提示内容。
    #   借用为「竞价 3 只分列」→ slots 模式，**1 次授权推 3 只**。
    #   2026-09-15 收到真实卡片后校正映射 —— 卡片的字段显示顺序固定为
    #       thing4 → thing1 → thing2 → thing5 → thing3
    #   对应【微信侧】标签依次为：
    #       活动名称 / 活动时间 / 活动内容 / 活动地点 / 温馨提示
    #   （注意 `thing3` 微信侧叫「温馨提示」，与后台登记的「提示内容」不同，
    #     再次印证后端登记名称只是后台展示文案、与微信侧无关。）
    #   据此把语义能对上的两个槽位各归其位：
    #       thing1(活动时间) ← 定盘时间   ✓ 标签正确
    #       thing3(温馨提示) ← 买点提示   ✓ 标签正确
    #   剩余 thing4/thing2/thing5 放 3 只标的（标签为活动语义，无法避免）。
    #   ⚠️ 标签文字由微信侧模板固定，改不了；要 5 个标签全对只能新建模板。
    "CvazWorVb8j8bey5OrPgMg2L7uNNC_AJpsVnHXaDVIY": {
        "name": "活动日程提醒(借用为竞价5字段)",
        "mode": "slots",
        "fields": {"s1": "thing4", "s2": "thing2", "s3": "thing5",
                   "time": "thing1", "tip": "thing3"},
    },
}

def derive_mapping(fields: list) -> dict | None:
    """从后端登记的模板字段自动推导 slots 映射（注册新模板后免改代码）。

    参数形如 [{"key": "thing1", "name": "精选标的1"}, ...]。
    命中规则（按 name 关键字）：
      「标的/股票/个股」+ 末尾序号 1/2/3 → s1/s2/s3
      「时间」                          → time
      「提示/建议」                     → tip
    返回 None 表示字段形态不符合 5 字段专用模板，需手工 --mapping。
    """
    slots: dict = {}
    time_k: str | None = None
    tip_k: str | None = None
    for f in fields or []:
        k = (f or {}).get("key")
        nm = str((f or {}).get("name") or "")
        if not k or not nm:
            continue
        tail = nm.rstrip()[-1:]
        if any(t in nm for t in ("标的", "股票", "个股")) and tail in "123":
            slots[f"s{tail}"] = k
        elif "时间" in nm and time_k is None:
            time_k = k
        elif ("提示" in nm or "建议" in nm) and tip_k is None:
            tip_k = k
    if len(slots) == 3 and time_k and tip_k:
        return {**slots, "time": time_k, "tip": tip_k}
    return None


# thing 类型微信限制 20 个字符；留 1 字符余量防边界
THING_MAX = 20


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def clip(s: str, n: int = THING_MAX) -> str:
    """按字符数截断（thing 类型上限），超长时用省略号标记。"""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def read_env(path: Path) -> dict:
    """极简 .env 解析（KEY = VALUE，忽略 # 注释），避免引入 dotenv 依赖。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def http_json(url: str, method: str = "GET", body: dict | None = None,
              headers: dict | None = None, timeout: int = 20):
    """返回 (status, json_or_text)。不抛异常，便于调用方判定。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json; charset=utf-8"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 后端客户端
# ---------------------------------------------------------------------------
class Backend:
    def __init__(self, api: str, user: str, pwd: str):
        self.api = api.rstrip("/")
        self.user, self.pwd, self.token = user, pwd, ""

    def login(self) -> tuple[bool, str]:
        if len(self.pwd) == 64 and all(c in "0123456789abcdef" for c in self.pwd.lower()):
            pwd = self.pwd
        else:
            pwd = hashlib.sha256(self.pwd.encode()).hexdigest()
        st, j = http_json(f"{self.api}/auth/login", "POST",
                          {"username": self.user, "password": pwd})
        if st != 200 or not isinstance(j, dict) or "token" not in j:
            return False, f"登录失败 HTTP {st}: {str(j)[:200]}"
        self.token = j["token"]
        return True, f"登录成功 token len={len(self.token)}"

    def _h(self):
        return {"Authorization": f"Bearer {self.token}"}

    def subs(self):
        st, j = http_json(f"{self.api}/subscribe/all?limit=500", headers=self._h())
        if st != 200 or not isinstance(j, dict):
            return {}
        out = {}
        for s in j.get("subscriptions", []):
            out[(s.get("openid"), s.get("template_id"))] = s.get("remain", 0)
        return out

    def templates(self):
        st, j = http_json(f"{self.api}/templates", headers=self._h())
        return j.get("templates", []) if (st == 200 and isinstance(j, dict)) else []

    def push(self, openid: str, template_id: str, data: dict, page: str = ""):
        body = {"openid": openid, "templateId": template_id, "data": data}
        if page:
            body["page"] = page
        return http_json(f"{self.api}/subscribe/push", "POST", body, headers=self._h())


# ---------------------------------------------------------------------------
# 取 TOP3
# ---------------------------------------------------------------------------
def load_picks(target: date | None = None, board: str = LOCAL_BOARD) -> tuple[list, dict]:
    """返回 (picks, meta)。优先定盘快照，回退实时接口。"""
    d = target or date.today()
    ymd = d.strftime("%Y%m%d")
    snap = HERE / f"_bid_picks_{ymd}.json"
    if snap.exists():
        j = json.loads(snap.read_text(encoding="utf-8"))
        return j.get("picks", []), {"src": f"定盘快照 {snap.name}",
                                    "frozen_at": j.get("frozen_at"),
                                    "mode_name": j.get("mode_name"),
                                    "mood": j.get("mood"), "date_label": j.get("date_label"),
                                    "exit_note": j.get("exit_note"),
                                    "exit_rule": j.get("exit_rule")}
    st, j = http_json(f"{board}/api/bid_watch", timeout=25)
    if st == 200 and isinstance(j, dict):
        data = j.get("data") or {}
        picks = data.get("picks") or []
        if picks:
            return picks, {"src": f"实时接口 {board}/api/bid_watch",
                           "frozen_at": data.get("frozen_at"),
                           "mode_name": data.get("mode_name"),
                           "mood": data.get("mood"), "date_label": data.get("date_label"),
                           # 实时接口把 exit_* / pick_rule 放在**顶层**（由 base_meta 合并而来），
                           # 不在 data 里 —— 两个分支取法不同是接口形状决定的，勿统一。
                           "exit_note": data.get("exit_note") or j.get("exit_note"),
                           "exit_rule": j.get("exit_rule")}
    raise SystemExit(f"取不到 TOP3：{snap.name} 不存在，且 {board}/api/bid_watch 无 picks"
                     f"（HTTP {st}）。请确认看板已启动或换 --date。")


# ---------------------------------------------------------------------------
# 构造模板 data
# ---------------------------------------------------------------------------
def _gap_of(p: dict) -> float | None:
    g = p.get("gap_used", p.get("gap"))
    return g if isinstance(g, (int, float)) else None


def _hm(frozen_at: str | None, date_label: str | None = None) -> str:
    """'09:25:05' -> '09:25'；给了 date_label 则 -> '09-15 09:25'。

    取不到时刻则退回当前时间。日期取快照自带的 date_label（形如 '09-15'），
    不自行由 frozen_at 推算 —— frozen_at 只有时分，没有日期。
    """
    if frozen_at and re.match(r"^\d{2}:\d{2}", frozen_at):
        hm = frozen_at[:5]
    else:
        hm = datetime.now().strftime("%H:%M")
    dl = str(date_label or "").strip()
    return f"{dl} {hm}" if dl else hm


def _t(meta: dict) -> str:
    """时间字段的统一出口 —— 四种模式都从这里取值。

    是否带日期由 meta["time_with_date"] 决定（命令行 --time-fmt 控制）。
    带日期后最长 11 字符（'09-15 09:25'），远在 thing 的 20 字符上限内。
    """
    return _hm(meta.get("frozen_at"),
               meta.get("date_label") if meta.get("time_with_date") else None)


def build_per_stock(picks: list, meta: dict, fields: dict) -> list[dict]:
    """每只一条消息。fields 需含 content / time / action。"""
    hm = _t(meta)
    msgs = []
    for p in picks:
        g = _gap_of(p)
        gs = f"{g:+.1f}%" if g is not None else "--"
        content = clip(f"{p.get('name','')}{p.get('code','')} {gs}")
        # 预警类型：连板龙头·低开·黄金买点（兼顾「哪一类」与「能不能买」，且三只之间可区分）
        cls = p.get("cls_name") or ""
        lab = p.get("buy_label") or p.get("win_label") or "观望"
        action = clip(f"{cls}·{lab}" if cls else lab)
        data = {
            fields["content"]: {"value": content},
            fields["action"]: {"value": action},
        }
        if fields.get("time"):
            data[fields["time"]] = {"value": hm}
        msgs.append({"stock": f"{p.get('name')}({p.get('code')})", "data": data})
    return msgs


def _compact_join(picks: list, abbrev: bool, integer: bool = False) -> str:
    """把 3 只拼成 '名称+缺口' 串。

    参数为逐级降级的压缩档：
      abbrev=True  名称只取前 2 字
      integer=True 缺口去掉小数（-3.7 → -4），再省 2 字符/只
    缺省档 `通达股份-3.7 键邦股份-1.6 澳洋健康-2.4` 会超 20，
    故 3 只合并必须逐级压缩（见 build_compact）。
    """
    parts = []
    for p in picks[:3]:
        g = _gap_of(p)
        nm = str(p.get("name", ""))
        if abbrev:
            nm = nm[:2]
        if g is None:
            gs = "--"
        else:
            gs = f"{g:+.0f}" if integer else f"{g:+.1f}"
        parts.append(f"{nm}{gs}")
    return " ".join(parts)


def build_single(picks: list, meta: dict, fields: dict) -> list[dict]:
    """3 只压进一条消息。fields 需含 title / time / body / codes / tip。"""
    hm = _t(meta)
    codes = [str(p.get("code", "")) for p in picks[:3]]

    # thing 类型上限 20 字符：先试全名，超长则退化为前 2 字简称
    body = _compact_join(picks, abbrev=False)
    if len(body) > THING_MAX:
        body = _compact_join(picks, abbrev=True)

    data = {
        fields["title"]: {"value": clip("竞价精选3只")},
        fields["body"]: {"value": clip(body)},
        fields["tip"]: {"value": clip("只买低开-4~-1%，其余观望")},
    }
    if fields.get("time"):
        data[fields["time"]] = {"value": clip(f"{hm} 定盘")}
    if fields.get("codes"):
        # 代码串较长，超 20 字符时退化为空格分隔
        cs = "/".join(codes)
        data[fields["codes"]] = {"value": cs if len(cs) <= THING_MAX else clip(" ".join(codes))}
    return [{"stock": "3只合并", "data": data}]


def build_compact(picks: list, meta: dict, fields: dict) -> list[dict]:
    """3 只压进【一个】thing 字段 → 1 次授权推 3 只（没有 5 字段专用模板时的正解）。

    字段契约与 per-stock **共用**（content/time/action），好处是一套 PRESETS 映射两用，
    切换只改 --mode，不用改字段。

    ⚠️ 代价必须讲清楚：thing 上限 20 字符，装下 3 只就只能【舍弃 6 位代码】留 2 字简称。
       实测 `通达-3.7 键邦-1.6 澳洋-2.4` = 恰好 20 字符。
       要保留完整代码 → 一个字段仅够 1 只，只能走 per-stock（n 只 = n 条 = n 次授权）。

    三级压缩（防顶格触发 47003）：
       ① 全简称+1位小数 → ② 2字简称+1位小数 → ③ 2字简称+整数缺口
    """
    body = _compact_join(picks, abbrev=False)
    if len(body) > THING_MAX:
        body = _compact_join(picks, abbrev=True)
    if len(body) > THING_MAX:
        body = _compact_join(picks, abbrev=True, integer=True)

    data = {fields["content"]: {"value": clip(body)}}
    if fields.get("time"):
        data[fields["time"]] = {"value": _t(meta)}
    if fields.get("action"):
        data[fields["action"]] = {"value": clip(
            _tip_text(meta, picks, meta.get("tip_override")))}
    return [{"stock": f"3只合并({len(picks[:3])}只)", "data": data}]


# ---------------------------------------------------------------------------
# slots 模式 —— 3 只各占一个独立 thing 字段（专用模板最优形态，1 次授权/日）
#   实测每只 17 字符、最坏 19 字符，均在 thing 的 20 字符上限内。
# ---------------------------------------------------------------------------
_BUY_MARK = {
    "gold": "买",   # 低开 -4~-1%：历史最强档(+2.50%、胜率62%)，近期同档实测 +0.08%(缩水约1/30)
    "deep": "慎",   # 深低开 <-4%：需翻红确认
    "mid":  "观",   # 小高开 1~5%
    "flat": "观",   # 平开 0~1%
    "mic":  "观",   # 微低开 -1~0%（陷阱档，胜率仅27%）
    "high": "弃",   # 高开 5~9.8%
    "yz":   "弃",   # 顶一字 >=9.8%，买不进
}
_VETO_MARK = "避"    # 放量下跌(竞价量比≥3.5 且低开)：实测该档 -2.28%/胜率33%，已降位


def _slot_mark(p: dict) -> str:
    """买点标记（单字，避免挤占名称空间）。

    ⚠️ 它只表达「仓位/买不买」的参考提示，**不再决定推不推** —— 2026-09-22 起推送
    改为「不空仓 + 赚钱效应(win_adj) 排序 + 仅对放量下跌降位」，任何单一门控都不能
    把推送清空。此前 15/15 条票被判「不可买」却仍全部推出去，正是「文案与裁决打架」
    的根源，已在 rt/bidwatch.py 的 _pick_key 里改为分级降位。
    """
    if p.get("veto"):
        return _VETO_MARK
    code = p.get("buy_code")
    if code in _BUY_MARK:
        return _BUY_MARK[code]
    return "待" if _gap_of(p) is None else "观"


def _slot_text(p: dict) -> str:
    """「简称+代码+缺口+标记」，最坏 19 字符（thing 上限 20）。

    实测：缺口格式化后最长为 `-10.0%`(6 字符)，简称最长为 6 字符
    （如 `XD中国石油`），6+6+1+6+1 = 20 恰好顶格。故阈值取 >=20 即压缩，
    始终保留至少 1 字符余量，规避 47003。
    """
    g = _gap_of(p)
    gs = f"{g:+.1f}%" if g is not None else "--"
    s = f"{p.get('name','')}{p.get('code','')} {gs}{_slot_mark(p)}"
    if len(s) >= THING_MAX:                      # 兜底：简称压到 4 字
        s = f"{str(p.get('name',''))[:4]}{p.get('code','')} {gs}{_slot_mark(p)}"
    return clip(s)


def _tip_text(meta: dict, picks: list, override: str | None = None) -> str:
    """提示文案 = [情绪标签·]卖点纪律，最长 15 字符。

    2026-09-22 由「只买低开-4~-1%」改为**卖点纪律**，两条理由：
      ① 旧文案是「买点门槛」，而推送票定盘时 gap 恒为负（-1%~-4%），auction_judge
         一律判「强转弱·竞价清」—— 文案与推送内容自相矛盾；且该门槛由 buy_zone 单独
         表达、与 auction_judge 实测互斥 14/15 条（同时 buy=False 且 buy_ok=True），
         挂在提示位上只会误导。
      ② 策略此前**只定义买点、从未定义卖点**。同批样本同口径(n=6)实测：
         当日收盘卖 +1.47% / 次日开盘卖 +1.69% / 次日收盘卖 +3.00%
         → 隔夜段才是超额收益来源，卖点纪律比买点门槛更该占这个字段。
    文案优先级：命令行 --tip 覆盖 > 快照 meta.exit_note（由 reco_engine.EXIT_NOTE 下发）> 兜底。
    """
    if override:
        return clip(override)
    base = str(meta.get("exit_note") or "T+1收盘前卖")
    blob = str(meta.get("mood") or "") + "".join(
        str(p.get("win_label") or "") for p in picks[:3])
    tag = ""
    if "亏钱" in blob:
        tag = "亏钱效应·"
    elif "赚钱" in blob:
        tag = "赚钱效应·"
    return clip(tag + base)


def build_slots(picks: list, meta: dict, fields: dict) -> list[dict]:
    """3 只各占一个 thing 字段。fields 需含 s1 / s2 / s3 / time / tip。"""
    data: dict = {}
    for i in range(3):
        key = fields.get(f"s{i + 1}")
        if not key:
            continue
        p = picks[i] if i < len(picks) else None
        data[key] = {"value": _slot_text(p) if p else "无"}
    if fields.get("time"):
        data[fields["time"]] = {"value": _t(meta)}
    if fields.get("tip"):
        data[fields["tip"]] = {"value": _tip_text(meta, picks, meta.get("tip_override"))}
    return [{"stock": "3只分列", "data": data}]


def build_messages(picks: list, meta: dict, template_id: str,
                   mode: str | None, mapping: dict | None) -> tuple[list[dict], str]:
    preset = PRESETS.get(template_id)
    if mapping:
        fields, eff_mode = mapping, (mode or "single")
    elif preset:
        fields, eff_mode = preset["fields"], (mode or preset["mode"])
    else:
        raise SystemExit(
            f"模板 {template_id} 无内置预设。请用 --mapping 指定字段映射，例如：\n"
            f'  --mapping \'{{"content":"thing1","time":"time2","action":"thing13"}}\''
        )
    eff_mode = mode or eff_mode
    if eff_mode == "per-stock":
        need = {"content", "time", "action"}
        if not need.issubset(fields):
            raise SystemExit(f"per-stock 模式需要字段 {sorted(need)}，当前映射缺 {sorted(need - set(fields))}")
        return build_per_stock(picks, meta, fields), eff_mode
    if eff_mode == "compact":
        # 只用得着 content；time / action 有则填、无则跳过，映射复用 per-stock 那套
        need = {"content"}
        if not need.issubset(fields):
            raise SystemExit(f"compact 模式需要字段 {sorted(need)}，"
                             f"当前映射缺 {sorted(need - set(fields))}")
        return build_compact(picks, meta, fields), eff_mode
    if eff_mode == "slots":
        need = {"s1", "s2", "s3", "time", "tip"}
        if not need.issubset(fields):
            raise SystemExit(f"slots 模式需要字段 {sorted(need)}，当前映射缺 {sorted(need - set(fields))}")
        return build_slots(picks, meta, fields), eff_mode
    need = {"title", "time", "body", "codes", "tip"}
    if not need.issubset(fields):
        raise SystemExit(f"single 模式需要字段 {sorted(need)}，当前映射缺 {sorted(need - set(fields))}")
    return build_single(picks, meta, fields), eff_mode


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="把竞价精选 TOP3 推送到微信订阅消息")
    ap.add_argument("--openid", help="接收者 openid；多个用逗号分隔，或填 all（所有有余额的用户）")
    ap.add_argument("--template", help="微信订阅消息模板 ID")
    ap.add_argument("--mode", choices=["single", "per-stock", "slots", "compact"],
                    help="覆盖模板预设：compact=3只合并进1条(1次授权,丢代码) / "
                         "per-stock=每只1条(n次授权,留代码) / slots=专用5字段模板最优")
    ap.add_argument("--mapping", help='自定义字段映射 JSON，如 \'{"content":"thing1",...}\'')
    ap.add_argument("--date", help="取哪一天的定盘快照，YYYY-MM-DD，默认今天")
    ap.add_argument("--page", default="", help="点击卡片跳转的小程序页面路径，可空")
    ap.add_argument("--api", default=None, help=f"后端 API Base，默认 {DEFAULT_API}")
    ap.add_argument("--env", default=str(DEFAULT_ENV), help="凭证 .env 路径")
    ap.add_argument("--board", default=LOCAL_BOARD, help="本地看板地址（回退取数用）")
    ap.add_argument("--tip", help="slots 模式「买点提示」文案；默认按情绪自动生成")
    ap.add_argument("--time-fmt", choices=["date-hm", "hm"], default="date-hm",
                    help="时间字段格式：date-hm=『09-15 09:25』(默认) / hm=『09:25』")
    ap.add_argument("--limit", type=int, help="只推排名前 N 只（配额有限时用，如 --limit 1 只推 TOP1）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不推送、不消耗配额")
    ap.add_argument("--force", action="store_true", help="跳过配额预检（不建议）")
    ap.add_argument("--list-sub", action="store_true", help="列出订阅余额后退出")
    args = ap.parse_args()

    env = read_env(Path(args.env))
    api = args.api or env.get("API_BASE_URL") or DEFAULT_API
    user = env.get("ADMIN_USERNAME", "")
    pwd = env.get("ADMIN_PASSWORD", "")
    if not user or not pwd:
        print(f"[!] 凭证缺失：{args.env} 中需有 ADMIN_USERNAME / ADMIN_PASSWORD")
        return 1

    be = Backend(api, user, pwd)
    ok, msg = be.login()
    print(f"[*] {msg}")
    if not ok:
        return 1

    subs = be.subs()

    if args.list_sub:
        tpls = {t.get("template_id"): t.get("title") for t in be.templates()}
        print(f"\n=== 订阅余额（openid × 模板）===")
        print(f"{'openid':<34}{'模板':<16}{'remain':>7}  更新时间")
        for (oid, tid), rem in sorted(subs.items()):
            print(f"{oid:<34}{(tpls.get(tid) or tid[:12]):<16}{rem:>7}")
        return 0

    if not args.template:
        print("[!] 需要 --template（或用 --list-sub 查看可用模板）")
        return 1

    # 取数
    target = None
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    picks, meta = load_picks(target, args.board)
    if args.limit and args.limit > 0:
        picks = picks[: args.limit]          # 配额有限时只推 TOP-N
    meta["tip_override"] = args.tip          # slots 模式的「买点提示」覆盖
    meta["time_with_date"] = (args.time_fmt == "date-hm")   # 时间字段是否带日期
    print(f"[*] 数据源 {meta['src']}  定盘 {meta.get('frozen_at') or '-'}  "
          f"日期 {meta.get('date_label') or '-'}  情绪 {meta.get('mood') or '-'}")
    print(f"[*] TOP3 共 {len(picks)} 只："
          + "、".join(f"{p.get('name')}({p.get('code')})" for p in picks[:3]))

    # 后端登记的模板字段：用于「零改代码」自动推导 + 与预设做一致性自检
    tpl = next((t for t in be.templates() if t.get("template_id") == args.template), None)
    tpl_fields = (tpl or {}).get("fields") or []
    if tpl:
        print(f"[*] 模板「{tpl.get('title')}」登记字段："
              + " ".join(f"{f.get('key')}={f.get('name')}" for f in tpl_fields))
    else:
        print(f"[!] 后端未登记该模板 {args.template}（不影响推送，但无法自检字段）")

    mapping = json.loads(args.mapping) if args.mapping else None
    preset = PRESETS.get(args.template)
    if mapping is None and preset is None:
        mapping = derive_mapping(tpl_fields)
        if mapping:
            print("[*] 未登记预设 → 已按后端字段自动推导映射："
                  + json.dumps(mapping, ensure_ascii=False))
    # 字段核对：后端登记仅供参考 —— 实测它可能滞后于微信侧真实字段（2026-09-15 踩过），
    # 真正的裁判是微信推送接口的返回值，故此处仅提示差异，不阻断。
    if preset and tpl_fields:
        want = set(preset["fields"].values())
        have = {f.get("key") for f in tpl_fields}
        if want != have:
            print(f"[i] 字段核对：预设 {sorted(want)} ≠ 后端登记 {sorted(have)}"
                  f"（后端登记≠微信侧真实字段，一切以实推校验为准）")

    msgs, mode = build_messages(picks, meta, args.template, args.mode, mapping)

    # 决定接收者
    if not args.openid:
        print("[!] 需要 --openid（可填 all）")
        return 1
    if args.openid == "all":
        targets = sorted({o for (o, t) in subs if t == args.template and subs[(o, t)] > 0})
        if not targets:
            print("[!] 没有任何用户对该模板有剩余授权")
            return 1
        print(f"[*] --openid all → 命中 {len(targets)} 个有余额用户")
    else:
        targets = [o.strip() for o in args.openid.split(",") if o.strip()]

    print(f"\n[*] 模式 = {mode}（每人推送 {len(msgs)} 条，消耗 {len(msgs)} 次授权）")
    for i, m in enumerate(msgs, 1):
        print(f"    [{i}] {m['stock']}")
        print(f"        {json.dumps(m['data'], ensure_ascii=False)}")
        over = [k for k, v in m["data"].items() if len(v.get("value", "")) > THING_MAX]
        if over:
            print(f"        ⚠ 字段超 {THING_MAX} 字符: {over}")

    # 配额预检
    need = len(msgs)
    if args.dry_run:
        print("\n[*] --dry-run：未调用推送接口，未消耗配额")
        return 0

    shortfalls = []
    for oid in targets:
        rem = subs.get((oid, args.template), 0)
        if rem < need:
            shortfalls.append((oid, rem))
    if shortfalls and not args.force:
        print(f"\n[!] 配额不足，已中止（未推送任何消息）：")
        for oid, rem in shortfalls:
            print(f"    {oid}  remain={rem} < 需要 {need}")
        print("    → 用户需在小程序内重新授权订阅；或用 --force 强推（会失败）")
        return 2

    # 推送
    print(f"\n=== 开始推送 ===")
    fail = 0
    miss_fields: list[str] = []          # 微信侧报缺的字段（47003 诊断用）
    for oid in targets:
        for i, m in enumerate(msgs, 1):
            st, res = be.push(oid, args.template, m["data"], args.page)
            err = res.get("errcode") if isinstance(res, dict) else None
            tag = "OK " if (st == 200 and err in (0, "0", None)) else "ERR"
            if tag == "ERR":
                fail += 1
                if str(err) == "47003" and isinstance(res, dict):
                    # 微信会点名字段：argument invalid! data.time2.value is empty
                    miss_fields += re.findall(
                        r"data\.([A-Za-z0-9_]+)\.value", str(res.get("errmsg") or ""))
            print(f"  [{tag}] {oid[:24]}… [{i}/{len(msgs)}] {m['stock']:<22} "
                  f"HTTP {st}  {json.dumps(res, ensure_ascii=False) if isinstance(res, dict) else str(res)[:120]}")

    if miss_fields:
        print(f"\n[!] 微信侧报缺字段 {sorted(set(miss_fields))}"
              f" —— 该模板在微信侧的真实字段与当前预设不一致。")
        print("    微信模板字段『加入后不可修改』，两种处置：")
        print("      1) 新建模板（字段按需设计）→ 拿到新 ID 直接跑，代码会自动推导映射；")
        print("      2) 沿用现有模板 → 用 --mapping 指定正确字段，例如：")
        print("         --mapping '{\"content\":\"thing1\",\"time\":\"time2\",\"action\":\"thing13\"}'")

    print(f"\n[*] 完成：成功 {len(targets) * need - fail} 条，失败 {fail} 条")
    return 0 if fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
