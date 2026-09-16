# -*- coding: utf-8 -*-
"""
东财盘口/个股异动 · 实时采集模块 (A股实时看板集成版)
======================================================================
数据源(与 quote.eastmoney.com/changes 同源):
  - push2ex.getAllStockChanges   个股异动明细流(时间/代码/名称/类型/量价)

实测约束(决定了本模块的采集策略):
  1. type 逗号多值只认第一个 type => 必须"逐类型"请求;
  2. 明细流为"最近 N 条滚动窗口" => pz 取 40~60 已足够覆盖 2s 节拍内的增量;
  3. 公开接口无鉴权但高频会被限频 => 固定节拍轮转 + (code,type,tm,info) 去重, 只保留增量。

与看板的对接方式(遵循看板既有架构: 后台线程采集 -> 内存缓冲 -> HTTP 只读缓存):
  - Collector 后台线程在交易时段(9:15-11:30 / 13:00-15:00)按 tick 节拍轮转采集;
    非交易时段完全静默(不发上游请求), 零成本;
  - 事件写入环形缓冲(deque), 每条带自增 seq, 前端以 seq 游标增量拉取 => 实时推送效果;
  - 「创业板(30开头)异动 + 同行业存在主板涨停股」联动判定复用看板已有的涨停池缓存
    (zt_provider 回调注入), 不重复拉取上游, 保证与「涨停个股」页同源一致;
  - 个股行业归属(f100)按需批量补查(ulist.np, 80只/次), 当日缓存 + 自动回填历史事件;
  - 非交易时段的启动/跨日会做一次 seed(逐类型拉一页)填充当日异动基线, 并按日期隔离,
    避免盘前把上一交易日的残留混入今日视图。

可独立运行(调试用):
  python em_changes.py --once           # 打一次快照(含联动标记)后退出
  python em_changes.py --loop           # 独立实时循环(与看板无关, 打印到控制台)
"""
import json
import os
import threading
import time
import datetime
import urllib.request
import urllib.parse

DIR = os.path.dirname(os.path.abspath(__file__))

EX_HOSTS = ["push2ex.eastmoney.com", "push2exdelay.eastmoney.com"]
QUOTE_HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com", "82.push2.eastmoney.com"]
EX_UT = "7eea3edcaed734bea9cbfc24409ed989"
EM_UT = "fa5fd080d2bcf6b6751843e8c9c826a4"

# 30 种盘口异动类型(由东财异动页 JS 字典逆向还原, 编号即接口 type)
TYPE_NAMES = {
    1: "顶级买单", 2: "顶级卖单", 4: "封涨停板", 8: "封跌停板",
    16: "打开涨停板", 32: "打开跌停板", 64: "有大买盘", 128: "有大卖盘",
    256: "机构买单", 512: "机构卖单", 8193: "大笔买入", 8194: "大笔卖出",
    8195: "拖拉机买", 8196: "拖拉机卖", 8201: "火箭发射", 8202: "快速反弹",
    8203: "高台跳水", 8204: "加速下跌", 8205: "买入撤单", 8206: "卖出撤单",
    8207: "竞价上涨", 8208: "竞价下跌", 8209: "高开5日线", 8210: "低开5日线",
    8211: "向上缺口", 8212: "向下缺口", 8213: "60日新高", 8214: "60日新低",
    8215: "60日大幅上涨", 8216: "60日大幅下跌",
}
# 正向(拉升/买盘)与反向(跳水/卖盘)类型, 用于方向配色
BULL = {1, 4, 16, 64, 256, 8193, 8195, 8201, 8202, 8207, 8209, 8211, 8213, 8215}
BEAR = {2, 8, 32, 128, 512, 8194, 8196, 8203, 8204, 8205, 8206, 8208, 8210, 8212, 8214, 8216}
# 页面默认关注(与东财异动页"拉升/买盘"标签一致)
DEFAULT_TYPES = [8201, 8193, 64, 4, 16, 8202, 8209, 8211, 8213, 8207]
# 高优先级: 每轮轮转多刷一次 => 刷新周期约为普通类型的一半
PRIORITY_TYPES = [8201, 8193, 64, 4, 16]

STOREFILE = os.path.join(DIR, "_changes_buffer.json")


# ---------- 通用工具 ----------
def parse_em_json(raw):
    """剥离 BOM / JSONP 外壳, 返回 dict."""
    if isinstance(raw, bytes):
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    elif "(" in text and text.endswith(")"):
        text = text[text.find("(") + 1:text.rfind(")")].strip()
    return json.loads(text)


def http_get(path, hosts, timeout=12):
    """多 host 兜底 GET. 失败抛最后一个异常."""
    last = None
    for h in hosts:
        try:
            req = urllib.request.Request("https://%s%s" % (h, path), headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://quote.eastmoney.com/changes/",
                "Connection": "close"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return parse_em_json(resp.read())
        except Exception as e:
            last = e
            continue
    raise RuntimeError("上游请求失败: %s" % last)


def fetch_stock_changes(etype, pz=60):
    """拉单个异动类型的明细流(最近 pz 条). 注意: 多类型逗号分隔只认第一个, 故逐类型调用."""
    d = http_get("/getAllStockChanges?" + urllib.parse.urlencode({
        "type": str(etype), "ut": EX_UT, "pageindex": "0",
        "pagesize": str(pz), "dpt": "wzchanges"}), EX_HOSTS)
    data = d.get("data") or {}
    return data.get("allstock") or []


def fmt_tm(tm):
    """接口 tm 为 HHMMSS 整数(如 145959) -> 'HH:MM:SS'."""
    try:
        t = str(int(tm))
        if len(t) == 5:
            t = "0" + t
        return "%s:%s:%s" % (t[0:2], t[2:4], t[4:6])
    except Exception:
        return str(tm)


def board_of(code):
    c = str(code)
    if c.startswith("60") or c.startswith("00"):
        return "主板"
    if c.startswith("30"):
        return "创业板"
    if c.startswith("68"):
        return "科创板"
    if c[:1] in ("8", "4"):
        return "北交所"
    return "其他"


def phase_now(now=None):
    """时段判定. 返回 (code, desc). 不含法定节假日(与看板其余模块口径一致)."""
    t = now or time.localtime()
    hm = t.tm_hour * 100 + t.tm_min
    wd = t.tm_wday  # 0=周一
    if wd >= 5:
        return ("weekend", "周末休市")
    if hm < 915:
        return ("pre_open", "盘前(9:15 集合竞价开始后推送竞价异动)")
    if hm < 925:
        return ("auction", "集合竞价(9:15-9:25)")
    if hm <= 1130:
        return ("open_am", "早盘交易中")
    if hm < 1300:
        return ("lunch", "午间休市")
    if hm <= 1500:
        return ("open_pm", "午盘交易中")
    return ("closed", "已收盘(展示当日异动基线)")


# 交易(需采集)时段
LIVE_PHASES = ("auction", "open_am", "open_pm")


def _prev_trading_day():
    """盘前(9:15 前)上游明细流仍是上一交易日的残留 => 事件日期归到上一交易日.
    仅跳过周末, 不含法定节假日(节假日当天无新事件, 影响可忽略)."""
    d = datetime.date.today()
    while True:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:
            return d.strftime("%Y%m%d")


def row_to_ev(r):
    """明细行 -> 归一化事件(未含联动字段, 由 Collector._resolve 补齐)."""
    i = str(r.get("i", ""))
    etype = int(r.get("t", 0) or 0)
    return {
        "tm": int(r.get("tm") or 0),
        "time": fmt_tm(r.get("tm")),
        "code": str(r.get("c", "")),
        "name": str(r.get("n", "")),
        "type": etype,
        "tname": TYPE_NAMES.get(etype, str(etype)),
        "info": i.split(",")[0] if i else "",
        "raw_i": i,
    }


def ev_key(ev):
    """去重键: 同代码+同类型+同时间+同量价 => 视为同一条事件."""
    return "%s|%s|%s|%s" % (ev["code"], ev["type"], ev["tm"], ev["info"])


def is_bull(etype):
    return etype in BULL


class Collector:
    """后台异动采集器: 轮转拉取 -> 去重 -> 环形缓冲(带 seq) -> 供 HTTP 增量读取."""

    def __init__(self, zt_provider=None, types=None, tick=2.0, buffer_size=4000,
                 store_file=STOREFILE, seed_pz=60, backfill_pz=60):
        self.zt_provider = zt_provider or (lambda: [])
        self.tick = float(tick)
        self.seed_pz = int(seed_pz)
        self.store_file = store_file
        self.types = [int(t) for t in (types or sorted(TYPE_NAMES.keys()))]
        # 轮转表: 优先类型(核心拉升类)每轮多刷一次, 刷新周期约为普通类型的一半
        self.rotation = self.types + [t for t in PRIORITY_TYPES if t in self.types]
        self._rot_idx = 0

        self.lock = threading.Lock()
        self.buffer = []                 # 事件列表(按 seq 升序)
        self.seq = 0
        self.keys = set()                # 去重键集合
        self.buffer_size = int(buffer_size)

        self.ind_of = {}                 # code -> 行业(当日缓存)
        self.ind_date = ""               # 行业缓存所属日期
        self._pending = []               # 待补查行业的代码(去重)
        self._pending_set = set()
        self._main_inds = set()          # 主板涨停所在行业集合
        self._main_by_ind = {}           # 行业 -> [(名称, 连板)]
        self._zt_fp = None               # 涨停池指纹(变化则回溯重算联动)

        self._seeded_date = ""           # 已做基线 seed 的日期
        self._last_ts = 0.0
        self._last_err = ""
        self._last_poll = 0.0
        self._req_count = 0
        self._lag = 0                    # 疑似漏采次数(单次返回满页)
        self._stop = threading.Event()
        self._dirty = False
        self._last_save = 0.0
        self.save_interval = 20.0      # 落盘节流(秒): 盘中 2s 节拍下避免空转写盘
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            if not os.path.exists(self.store_file):
                return
            with open(self.store_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            evs = d.get("events") or []
            self.buffer = evs[-self.buffer_size:]
            self.seq = int(d.get("seq") or (evs[-1]["seq"] if evs else 0))
            self.ind_of = dict(d.get("ind_of") or {})
            self.ind_date = str(d.get("ind_date") or "")
            self._seeded_date = str(d.get("seeded_date") or "")
            self.keys = {ev_key(e) for e in self.buffer}
            self._dirty = False
            # 冷启动补漏: 磁盘恢复的历史事件不会走 _insert, 其行业缺失需重新登记补查
            for e in self.buffer:
                if not e.get("ind"):
                    self._mark_pending(e.get("code"))
        except Exception as e:
            self._last_err = "载入历史缓冲失败: %s" % e

    def _save(self, force=False):
        """落盘缓冲与行业缓存(跨重启保留当日事件). 非 force 时节流, 避免每 2s 空写磁盘."""
        now = time.time()
        if not force:
            if not self._dirty or (now - self._last_save) < self.save_interval:
                return
        self._last_save = now
        try:
            tmp = self.store_file + ".tmp"
            with self.lock:
                payload = {"saved": time.strftime("%Y-%m-%d %H:%M:%S"), "seq": self.seq,
                           "seeded_date": self._seeded_date, "ind_date": self.ind_date,
                           "ind_of": self.ind_of, "events": self.buffer[-self.buffer_size:]}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.store_file)
            self._dirty = False
        except Exception as e:
            self._last_err = "落盘失败: %s" % e

    # ---------- 联动判定(复用看板涨停池缓存) ----------
    def _refresh_zt(self):
        """从 zt_provider(看板涨停池缓存)重建「主板涨停 -> 行业」索引. 零上游成本."""
        try:
            pool = self.zt_provider() or []
        except Exception:
            return
        inds, by_ind, fp = set(), {}, []
        for s in pool:
            if s.get("board") != "主板":
                continue
            ind = str(s.get("hybk") or "").strip()
            if not ind:
                continue
            inds.add(ind)
            by_ind.setdefault(ind, []).append((s.get("name") or "", s.get("lbc")))
            fp.append("%s:%s" % (s.get("code"), s.get("lbc")))
        fp = tuple(sorted(fp))
        if fp != self._zt_fp:
            self._zt_fp = fp
            self._main_inds, self._main_by_ind = inds, by_ind
            # 涨停池变化(新板块封板) -> 回溯重算近段事件的联动标记
            self._reresolve(tail=400)
            self._dirty = True

    def _resolve(self, ev):
        """补齐单个事件的 行业/板块/联动 字段."""
        code = ev["code"]
        ev["board"] = board_of(code)
        ev["cyb"] = code.startswith("30")
        ind = self.ind_of.get(code, "")
        ev["ind"] = ind
        peers = self._main_by_ind.get(ind) if (ev["cyb"] and ind) else None
        if peers:
            ev["link"] = True
            ev["peers"] = [{"name": n, "lbc": l} for n, l in peers[:4]]
        else:
            ev["link"] = False
            ev["peers"] = []
        return ev

    def _reresolve(self, tail=400):
        for ev in self.buffer[-tail:]:
            self._resolve(ev)

    def _mark_pending(self, code):
        if code and code not in self.ind_of and code not in self._pending_set:
            self._pending.append(code)
            self._pending_set.add(code)

    def _fill_ind(self, max_batch=2):
        """按需批量补查行业(ulist.np, 80只/批). 补到后回填缓冲中同代码的历史事件."""
        for _ in range(max_batch):
            batch = [c for c in self._pending[:80] if c not in self.ind_of]
            if not batch:
                self._pending = []
                self._pending_set = set()
                return
            self._pending = self._pending[len(batch):]
            self._pending_set.difference_update(batch)
            secids = ",".join("%s.%s" % ("1" if c.startswith(("6", "9")) else "0", c) for c in batch)
            try:
                d = http_get("/api/qt/ulist.np/get?" + urllib.parse.urlencode({
                    "fields": "f12,f100", "secids": secids, "fltt": "2", "ut": EM_UT}),
                    QUOTE_HOSTS)
                self._req_count += 1
                got = {}
                for it in ((d.get("data") or {}).get("diff") or []):
                    c = str(it.get("f12") or "")
                    if c:
                        self.ind_of[c] = str(it.get("f100") or "")
                        got[c] = self.ind_of[c]
            except Exception as e:
                self._last_err = "行业补查失败: %s" % e
                return
            if got:                     # 回填历史事件
                for ev in self.buffer:
                    if ev["code"] in got:
                        self._resolve(ev)
                self._dirty = True

    # ---------- 采集 ----------
    def _insert(self, ev, date_str):
        k = ev_key(ev)
        if k in self.keys:
            return False
        self.seq += 1
        ev = dict(ev)
        ev["seq"] = self.seq
        ev["d"] = date_str
        ev["ts"] = int(time.time())
        ev["bull"] = is_bull(ev["type"])
        self._resolve(ev)
        if not ev["ind"]:
            # 行业归属用于展示(全板块)与联动判定(创业板), 未缓存则登记批量补查
            self._mark_pending(ev["code"])
        self.buffer.append(ev)
        self.keys.add(k)
        if len(self.buffer) > self.buffer_size:
            drop = self.buffer[:len(self.buffer) - self.buffer_size]
            self.buffer = self.buffer[-self.buffer_size:]
            for x in drop:
                self.keys.discard(ev_key(x))
        self._dirty = True
        return True

    def _event_date(self, ph):
        """事件归属日期: 盘前采集到的是上一交易日残留."""
        if ph == "pre_open":
            return _prev_trading_day()
        return time.strftime("%Y%m%d")

    def seed(self, ph=None, pz=None):
        """基线填充: 逐类型拉一页明细. 用于启动/开盘前, 让页面首次打开即有内容."""
        ph = ph or phase_now()[0]
        date_str = self._event_date(ph)
        pz = int(pz or self.seed_pz)
        n = 0
        for t in self.types:
            try:
                rows = fetch_stock_changes(t, pz=pz)
                self._req_count += 1
                for r in rows:
                    if self._insert(row_to_ev(r), date_str):
                        n += 1
            except Exception as e:
                self._last_err = "基线 type %s 失败: %s" % (t, e)
            time.sleep(0.12)            # 错峰, 避免启动瞬间冲击上游
        self._last_ts = time.time()
        self._seeded_date = time.strftime("%Y%m%d")
        self._save(force=True)
        return n

    def _poll_once(self, etype, date_str):
        # 单页统一取 100(该接口实测上限, 见模块头注释): 请求次数不变, 页更大
        #   -> 节拍窗内事件数超过一页的概率下降, 减少漏采(突发时段尤其明显)
        pz = 100
        try:
            rows = fetch_stock_changes(etype, pz=pz)
            self._req_count += 1
            self._last_ts = time.time()
            if len(rows) >= pz:
                self._lag += 1
            new = 0
            for r in rows:
                if self._insert(row_to_ev(r), date_str):
                    new += 1
            self._fill_ind()        # 无条件尝试排空补查队列(无待查时立即返回)
            return new
        except Exception as e:
            self._last_err = "type %s 拉取失败: %s" % (etype, e)
            return 0

    def run(self):
        """采集主循环: 交易时段按时轮转; 非交易时段静默(不发请求)."""
        while not self._stop.is_set():
            try:
                ph, _desc = phase_now()
                today = time.strftime("%Y%m%d")
                # 行业缓存的日期隔离(跨日清空, 避免用昨日的行业归属)
                if self.ind_date != today:
                    self.ind_of = {}
                    self.ind_date = today
                self._refresh_zt()
                if ph in LIVE_PHASES:
                    # 新交易日首次进入交易时段 -> 做一次基线填充(补上隔夜缺口)
                    if self._seeded_date != today:
                        self.seed(ph)
                    date_str = self._event_date(ph)
                    etype = self.rotation[self._rot_idx % len(self.rotation)]
                    self._rot_idx += 1
                    self._poll_once(etype, date_str)
                    self._save()
                    self._stop.wait(self.tick)
                else:
                    # 非交易时段: 启动后补一次当日基线, 之后不发任何上游请求
                    if self._seeded_date != today:
                        self.seed(ph)
                    self._save()
                    self._stop.wait(20)
            except Exception as e:
                self._last_err = "采集循环异常: %s" % e
                self._stop.wait(5)

    def start(self):
        th = threading.Thread(target=self.run, name="em-changes-collector", daemon=True)
        th.start()
        return th

    def stop(self):
        self._stop.set()
        self._save(force=True)

    # ---------- 读取(HTTP 只读, 不触发上游) ----------
    def snapshot(self, since=0, limit=200, date=None):
        ph, desc = phase_now()
        with self.lock:
            buf = list(self.buffer)
            ind_of, seq = dict(self.ind_of), self.seq
            seeded, err, lag, reqs = self._seeded_date, self._last_err, self._lag, self._req_count
            last_ts = self._last_ts
        today = time.strftime("%Y%m%d")
        dates = {}
        for e in buf:
            dates[e["d"]] = dates.get(e["d"], 0) + 1
        if date:
            d = date
        elif dates.get(today):
            d = today
        elif dates:
            d = max(dates)              # 今日尚无事件 -> 回落到最近有事件的日期(盘前看上一交易日)
        else:
            d = today
        evs = [e for e in buf if e["d"] == d]
        truncated = False
        if since and since > 0:
            picked = [e for e in evs if e["seq"] > since]
            if len(picked) > limit:
                truncated = True
                picked = picked[:limit]
        else:
            truncated = len(evs) > limit
            picked = evs[-int(limit):]
        meta = {
            "phase": ph, "phase_desc": desc, "live": ph in LIVE_PHASES,
            "tick": self.tick, "types": len(self.types),
            "rotation": len(self.rotation), "priority": PRIORITY_TYPES,
            "buffered": len(evs), "buffered_all": len(buf), "seq": seq,
            "date": d, "is_today": (d == today), "dates": dates,
            "seeded_date": seeded, "last_ts": int(last_ts) if last_ts else 0,
            "last_error": err, "requests": reqs, "lag": lag,
            "ind_cached": len(ind_of),
        }
        if not since:
            # 首次加载才下发类型字典/默认关注集, 避免每轮 1KB 冗余
            meta["type_names"] = {str(k): v for k, v in TYPE_NAMES.items()}
            meta["default_types"] = DEFAULT_TYPES
            meta["bull"] = sorted(BULL)
            meta["bear"] = sorted(BEAR)
        return {"ok": True, "meta": meta, "events": picked,
                "max_seq": seq, "truncated": truncated}


# ---------- 独立运行(调试用) ----------
def _cli():
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="东财盘口异动采集器(独立调试模式)")
    ap.add_argument("--once", action="store_true", help="打一次快照后退出")
    ap.add_argument("--loop", action="store_true", help="独立实时循环(打印到控制台)")
    ap.add_argument("--link-only", action="store_true", help="只显示 创业板+主板涨停同行业 联动事件")
    ap.add_argument("--types", type=str, default=None, help="关注类型, 逗号分隔")
    ap.add_argument("--interval", type=float, default=2.0, help="轮转节拍秒")
    a = ap.parse_args()
    types = [int(x) for x in a.types.split(",")] if a.types else None
    c = Collector(zt_provider=None, types=types, tick=a.interval,
                  store_file=os.path.join(DIR, "_changes_cli.json"))
    n = c.seed()
    print("基线填充 %d 条 | 时段 %s" % (n, phase_now()[1]))
    snap = c.snapshot(since=0, limit=60)
    shown = 0
    for e in reversed(snap["events"]):
        if a.link_only and not e.get("link"):
            continue
        mark = "★联动" if e.get("link") else ("创业板" if e.get("cyb") else "")
        print("[%s] %s %s %s %s 价/量 %s %s [%s]%s" % (
            e["time"], "↑" if e.get("bull") else "↓", e["tname"], e["code"], e["name"],
            e["info"], mark, e["ind"], (" 主板同行业: " + ",".join(
                "%s×%s板" % (p["name"], p["lbc"]) for p in e.get("peers") or [])) if e.get("peers") else ""))
        shown += 1
        if shown >= 30:
            break
    if a.loop:
        print("\n独立循环(2s 节拍), Ctrl+C 退出...")
        c._stop.clear()
        try:
            c.run()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    _cli()
