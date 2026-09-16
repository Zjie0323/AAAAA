# -*- coding: utf-8 -*-
"""
auto_push_bid.py —— 交易日 9:25:40 自动推送「集合竞价 TOP3」到微信订阅消息
==========================================================================
定位：调度执行器（单次运行、跑完即退），由 Windows 计划任务触发。
      推送逻辑不在本脚本内，本脚本只负责【时机 + 幂等 + 日志】。

推送通道（--channel，默认 mcp）
--------------------------------
  mcp   经远程 MCP 服务（https://test.yaohaiqing.top/mcp）的 tools/call 下发，
        调用 mcp_wx_push.py。相比 REST 多一层「推送流水」可观测性 ——
        事后可用 list_push_logs 回查某条消息究竟发没发出去，排障时是最硬的证据。
  rest  直连自建后端 REST 接口，调用 push_picks.py。作为回退通道保留。

  两条通道的渲染逻辑同源（mcp_wx_push.py 内部 import push_picks 的 build_messages），
  文案字节级一致；退出码语义也完全对齐。故切换只影响传输层，不影响内容与判定。

  刻意**不做**「MCP 失败自动回退 REST」：MCP 若已实际发出、只是响应超时，
  自动回退会造成同一内容重复推送并二次消耗订阅配额。失败即失败，人工介入。

时序（关键路径）
----------------
  09:25:00  集合竞价撮合定价
  09:25:0x  server.py 定盘线程（每 15s 轮询）把 TOP3 落盘
            -> _bid_picks_<YYYYMMDD>.json
  09:25:40  本脚本被计划任务唤醒（默认时刻）
            -> 等快照就绪 -> 调 push_picks.py 实推 -> 写幂等状态
  09:30:00  时效红线：超过即放弃（竞价买点已失效，推出去是误导）

四道护栏
--------
  1) 交易日护栏：周末直接跳过；节假日靠「等不到快照」自然兜底
  2) 时效护栏  ：now > --deadline(09:30:00) -> 跳过，绝不推过期内容
  3) 幂等护栏  ：当天已成功推送 -> 跳过，防止补跑重复消耗订阅配额
  4) 看板护栏  ：:8000 无响应时可自动拉起 server.py（--ensure-board，默认开）

退出码（0 = 正常，含「按设计跳过」；非 0 才需关注）
------
  0 正常：推送成功 / 周末跳过 / 已过时效跳过
  1 配置或环境错误：凭证缺失、看板不可用
  3 推送失败：配额不足、微信拒绝（47003 等）
  4 等待定盘快照超时：疑似休市，或看板定盘失败

用法
----
# 正常（计划任务调用，无需参数；默认走 mcp 通道）
python auto_push_bid.py

# 模拟 9:25:40 触发、只看不推（零配额消耗）
python auto_push_bid.py --now 09:25:40 --dry-run

# 忽略当天已推送状态，强制重推
python auto_push_bid.py --force

# 临时回退到 REST 通道（计划任务定义无需改动，只加参数）
python auto_push_bid.py --channel rest
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"

# 活动日程提醒（5 个 thing 字段）借用作「竞价 3 只分列」：1 次授权推 3 只
DEFAULT_TEMPLATE = "CvazWorVb8j8bey5OrPgMg2L7uNNC_AJpsVnHXaDVIY"
DEFAULT_OPENID = "all"          # all = 所有对该模板有余额的用户
DEFAULT_CHANNEL = "mcp"         # mcp = 经远程 MCP 服务；rest = 直连自建后端

# MCP 配置（读 Bearer 令牌用）
MCP_CONFIG = Path(os.environ.get("USERPROFILE") or Path.home()) / ".workbuddy" / "mcp.json"
MCP_SERVER = "wx-subscribe"

DEFAULT_WAIT = "09:25:40"       # 早于此时刻则先等待（计划任务触发时刻即此值）
                                # 说明：Task Scheduler 的 NextRunTime 计算可能比 StartBoundary
                                # 早若干秒，故把「精确时刻」放在脚本内自守，不依赖触发抖动。
DEFAULT_DEADLINE = "09:30:00"   # 晚于此时刻则放弃（开盘后买点失效）
DEFAULT_PROBE = 3               # 快照轮询间隔（秒）
BOARD_URL = "http://127.0.0.1:8000"
BOARD_WAIT = 60                 # 拉起看板后最多等多久（秒）

# 看板解释器候选（优先用当前跑着的那个，与 start-local.bat 一致）
BOARD_PY = [
    Path(r"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"),
    Path(sys.executable),
]

# 退出码语义：0 = 正常（含「按设计跳过」），非 0 才值得关注。
#   刻意让 SKIP 归 0 —— 周末与补跑超时效每天都会发生，若返回非 0，
#   任务计划程序的历史里会堆满假的「失败(0x2)」，真正的故障就被淹没。
EXIT_OK, EXIT_CFG, EXIT_SKIP, EXIT_PUSH_FAIL, EXIT_TIMEOUT = 0, 1, 0, 3, 4

_now_override: dtime | None = None
_log_path: Path | None = None


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def now() -> datetime:
    """当前时间；--now 覆盖时只替换时刻、日期仍取真实值（便于当天回放测试）。"""
    if _now_override is None:
        return datetime.now()
    return datetime.combine(date.today(), _now_override)


def log(msg: str, level: str = "INFO") -> None:
    """双写：控制台 + 当日日志文件（UTF-8，不依赖控制台代码页）。"""
    line = "%s [%-5s] %s" % (datetime.now().strftime("%H:%M:%S"), level, msg)
    if _log_path:
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def hms(s: str) -> dtime:
    p = [int(x) for x in str(s).split(":")]
    while len(p) < 3:
        p.append(0)
    return dtime(p[0], p[1], p[2])


def http_probe(url: str, method: str = "GET", body: dict | None = None,
               headers: dict | None = None, timeout: int = 5):
    """返回 (status, payload)。不抛异常，便于判定。仅用标准库。"""
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
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------------------
# 看板护栏
# ---------------------------------------------------------------------------
def board_alive() -> bool:
    st, j = http_probe(BOARD_URL + "/api/bid_watch", timeout=4)
    return st == 200


def ensure_board(allow_spawn: bool) -> bool:
    """看板不通时尝试拉起 server.py。返回最终是否可用。"""
    if board_alive():
        log("看板健康检查 %s -> OK" % BOARD_URL)
        return True
    if not allow_spawn:
        log("看板 %s 无响应，且 --no-ensure-board 已禁用自动拉起" % BOARD_URL, "ERROR")
        return False

    py = next((p for p in BOARD_PY if p.exists()), None)
    if py is None:
        log("看板无响应，且找不到可用解释器，无法拉起", "ERROR")
        return False

    slog = LOG_DIR / ("server_autostart_%s.log" % date.today().strftime("%Y%m%d"))
    log("看板无响应 -> 自动拉起 server.py（解释器 %s，日志 %s）" % (py.name, slog.name), "WARN")
    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        flags = 0x00000008 | 0x00000200 | 0x08000000
    try:
        with open(slog, "ab") as fh:
            subprocess.Popen([str(py), str(HERE / "server.py"), "8000"],
                             cwd=str(HERE), stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=flags,
                             close_fds=True)
    except Exception as e:
        log("拉起 server.py 失败：%s" % e, "ERROR")
        return False

    for i in range(BOARD_WAIT):
        if board_alive():
            log("看板已就绪（等待 %ds）" % (i + 1))
            return True
        time.sleep(1)
    log("看板拉起后 %ds 内仍未就绪" % BOARD_WAIT, "ERROR")
    return False


# ---------------------------------------------------------------------------
# 幂等状态
# ---------------------------------------------------------------------------
def state_file(d: date) -> Path:
    return HERE / ("_auto_push_state_%s.json" % d.strftime("%Y%m%d"))


def state_read(d: date) -> dict:
    p = state_file(d)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def state_write(d: date, payload: dict) -> None:
    try:
        state_file(d).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    except Exception as e:
        log("幂等状态写入失败：%s" % e, "WARN")


# ---------------------------------------------------------------------------
# 调用推送器（mcp / rest 双通道）
# ---------------------------------------------------------------------------
def mcp_env() -> dict:
    """把 mcp.json 里的 Bearer 令牌注入子进程的 WX_MCP_TOKEN。

    为何显式注入：计划任务以 S4U 主体运行，子进程读 mcp.json 要靠
    Path.home()（即 USERPROFILE）解析，通常没问题但不宜赌；而用命令行
    --token 传参又会让令牌出现在进程列表中。环境变量是两者之间最稳的落点。
    外部若已设 WX_MCP_TOKEN，则不覆盖。
    """
    if os.environ.get("WX_MCP_TOKEN"):
        return {}
    try:
        cfg = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}
    servers = cfg.get("mcpServers") or {}
    srv = servers.get(MCP_SERVER)
    if not isinstance(srv, dict):
        # 未按名字命中时，退而取第一个带 Authorization 的 http 服务
        for v in servers.values():
            if isinstance(v, dict) and (v.get("headers") or {}).get("Authorization"):
                srv = v
                break
    if not isinstance(srv, dict):
        return {}
    auth = (srv.get("headers") or {}).get("Authorization") or ""
    token = auth.split()[-1] if auth else ""
    return {"WX_MCP_TOKEN": token} if token else {}


# 通道 -> 推送器脚本
CHANNELS = {"mcp": "mcp_wx_push.py", "rest": "push_picks.py"}


def run_push(channel: str, template: str, openid: str, dry: bool, tip: str | None,
             time_fmt: str | None = None) -> tuple[int, str]:
    """调用所选通道的推送器，返回 (退出码, 合并输出)。

    超时放宽到 420s：MCP 通道需多次公网往返（握手 + 列模板 + 查余额 +
    发送 + 回查），明显慢于 REST 的单次 POST。
    """
    script = CHANNELS.get(channel) or CHANNELS["rest"]
    cmd = [sys.executable, str(HERE / script),
           "--openid", openid, "--template", template]
    if tip:
        cmd += ["--tip", tip]
    if time_fmt:
        cmd += ["--time-fmt", time_fmt]
    if dry:
        cmd.append("--dry-run")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.update(mcp_env())
    try:
        p = subprocess.run(cmd, cwd=str(HERE), env=env, timeout=420,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 99, "[auto_push] %s 超时（420s）" % script
    except Exception as e:
        return 98, "[auto_push] 调用 %s 失败：%s" % (script, e)


def cleanup_logs(keep_days: int = 30) -> None:
    try:
        files = sorted(LOG_DIR.glob("auto_push_*.log"))
        for f in files[:-keep_days] if len(files) > keep_days else []:
            f.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    global _now_override, _log_path

    ap = argparse.ArgumentParser(description="交易日 9:25:40 自动推送竞价 TOP3")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE, help="微信模板 ID")
    ap.add_argument("--openid", default=DEFAULT_OPENID, help="接收者 openid，或 all")
    ap.add_argument("--channel", choices=["mcp", "rest"], default=DEFAULT_CHANNEL,
                    help="推送通道：mcp=远程 MCP 服务(默认) / rest=自建后端 REST")
    ap.add_argument("--wait-until", default=DEFAULT_WAIT, help="早于此时刻则先等待，默认 09:25:00")
    ap.add_argument("--deadline", default=DEFAULT_DEADLINE, help="晚于此时刻则放弃，默认 09:30:00")
    ap.add_argument("--probe", type=int, default=DEFAULT_PROBE, help="快照轮询间隔秒，默认 3")
    ap.add_argument("--tip", default=None, help="买点提示文案覆盖（默认按情绪自动生成）")
    ap.add_argument("--time-fmt", dest="time_fmt", choices=["date-hm", "hm"], default="date-hm",
                    help="时间字段格式：date-hm=『09-15 09:25』(默认) / hm=『09:25』")
    ap.add_argument("--ensure-board", dest="ensure_board", action="store_true", default=True,
                    help="看板无响应时自动拉起 server.py（默认开）")
    ap.add_argument("--no-ensure-board", dest="ensure_board", action="store_false",
                    help="禁用自动拉起看板")
    ap.add_argument("--dry-run", action="store_true", help="只预览不推送，不消耗配额、不写幂等状态")
    ap.add_argument("--force", action="store_true", help="忽略当天已推送状态")
    ap.add_argument("--no-weekend-check", action="store_true", help="跳过周末判定（调试用）")
    ap.add_argument("--now", default=None, help="覆盖当前时刻 HH:MM:SS（回放测试用）")
    args = ap.parse_args()

    if args.now:
        _now_override = hms(args.now)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    _log_path = LOG_DIR / ("auto_push_%s.log" % today.strftime("%Y%m%d"))
    cleanup_logs()

    wait_until, deadline = hms(args.wait_until), hms(args.deadline)
    log("=" * 58)
    log("竞价推送调度启动  channel=%s  template=%s…  openid=%s"
        % (args.channel, args.template[:12], args.openid))
    log("窗口 %s ~ %s   probe=%ds   dry_run=%s   force=%s"
        % (args.wait_until, args.deadline, args.probe, args.dry_run, args.force))

    # ---- 护栏 1：交易日（周末）--------------------------------------------
    if not args.no_weekend_check and today.weekday() >= 5:
        log("今日 %s 为周末，非交易日 -> 跳过" % today.isoformat(), "SKIP")
        return EXIT_SKIP

    # ---- 护栏 2：时效窗口 -------------------------------------------------
    t = now()
    if t.time() > deadline:
        log("当前 %s 已过时效红线 %s（竞价买点失效）-> 跳过，不推过期内容"
            % (t.strftime("%H:%M:%S"), args.deadline), "SKIP")
        return EXIT_SKIP
    if t.time() < wait_until:
        gap = (datetime.combine(today, wait_until) - t).total_seconds()
        log("早于 %s，等待 %.0fs 后开始" % (args.wait_until, gap))
        time.sleep(max(0.0, gap))

    # ---- 护栏 3：幂等 -----------------------------------------------------
    st = state_read(today)
    if st.get("status") == "ok" and not args.force and not args.dry_run:
        log("今日已于 %s 成功推送（msgid=%s）-> 跳过，避免重复消耗配额"
            % (st.get("pushed_at"), st.get("msgid") or "-"), "SKIP")
        return EXIT_SKIP

    # ---- 护栏 4：看板 -----------------------------------------------------
    if not ensure_board(args.ensure_board):
        log("看板不可用，无法取得定盘快照 -> 放弃", "ERROR")
        return EXIT_CFG

    # ---- 等定盘快照 -------------------------------------------------------
    snap = HERE / ("_bid_picks_%s.json" % today.strftime("%Y%m%d"))
    log("等待定盘快照 %s …" % snap.name)
    t0 = time.time()
    picks_info = None
    while True:
        if snap.exists():
            try:
                j = json.loads(snap.read_text(encoding="utf-8"))
                picks = j.get("picks") or []
                if picks:
                    picks_info = (j, picks)
                    break
                log("快照已出现但 picks 为空，继续等待…", "WARN")
            except Exception as e:
                log("快照解析失败（可能正在写入）：%s" % e, "WARN")
        if now().time() > deadline:
            log("等待 %.0fs 后已达时效红线 %s，快照仍未就绪 -> 放弃本次推送"
                % (time.time() - t0, args.deadline), "TIMEOUT")
            return EXIT_TIMEOUT
        if int(time.time() - t0) % 30 == 0 and time.time() - t0 >= 30:
            log("仍在等待快照…（已 %.0fs）" % (time.time() - t0))
        time.sleep(max(1, args.probe))

    j, picks = picks_info
    log("快照就绪（等待 %.0fs）" % (time.time() - t0))
    log("  口径=%s  定格时刻=%s  情绪=%s  标的=%d 只"
        % (j.get("mode_name") or "-", j.get("frozen_at") or "-",
           j.get("mood") or "-", len(picks)))
    for i, p in enumerate(picks[:3], 1):
        log("  [%d] %s %s  gap=%s" % (i, p.get("name"), p.get("code"),
                                      p.get("gap_used", p.get("gap"))))

    # ---- 推送 -------------------------------------------------------------
    script = CHANNELS.get(args.channel) or CHANNELS["rest"]
    log("调用推送器 %s（channel=%s）…" % (script, args.channel))
    rc, out = run_push(args.channel, args.template, args.openid, args.dry_run,
                       args.tip, args.time_fmt)
    for ln in out.splitlines():
        log("  | " + ln)
    log("%s 退出码 = %d" % (script, rc))

    if args.dry_run:
        log("--dry-run：未真实推送、未消耗配额、未写幂等状态")
        return EXIT_OK

    if rc == 0:
        ok, status = True, "ok"
        # 从输出里抠 msgid（push_picks 会打印微信返回的 JSON）
        import re as _re
        m = _re.search(r'"msgid"\s*:\s*"?(\d+)"?', out)
        msgid = m.group(1) if m else None
        log("推送成功%s" % ("，msgid=%s" % msgid if msgid else ""))
        state_write(today, {"date": today.strftime("%Y%m%d"),
                            "pushed_at": now().strftime("%H:%M:%S"),
                            "channel": args.channel, "script": script,
                            "template": args.template, "openid": args.openid,
                            "status": status, "msgid": msgid, "picks": len(picks)})
        return EXIT_OK
    elif rc == 2:
        status = "quota_exhausted"
        log("配额不足 -> 需在小程序内重新授权订阅（配额按 openid × 模板单独计数）", "ERROR")
    else:
        status = "failed"
        log("推送失败（退出码 %d，通道 %s）" % (rc, args.channel), "ERROR")
        if args.channel == "mcp":
            # 仅通道级故障（网络不可达 / MCP 端点异常 / 超时）才值得提示回退；
            # 配额与字段类错误换通道同样失败，提示只会误导。
            log("若属通道故障，可手工切回 rest 补推（不改计划任务定义）：", "WARN")
            log("  \"%s\" --channel rest --force" % (HERE / "auto_push_bid.bat"), "WARN")

    state_write(today, {"date": today.strftime("%Y%m%d"),
                        "pushed_at": now().strftime("%H:%M:%S"),
                        "channel": args.channel, "script": script,
                        "template": args.template, "openid": args.openid,
                        "status": status, "exit_code": rc, "picks": len(picks)})
    return EXIT_PUSH_FAIL


if __name__ == "__main__":
    sys.exit(main())
