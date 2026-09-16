# -*- coding: utf-8 -*-
"""
mcp_wx_push.py —— 通过「远程 MCP 服务」推送微信订阅消息

与 push_picks.py 的区别：
    push_picks.py  直接打自建后端的 REST 接口（/api/subscribe/push）
    mcp_wx_push.py 打 MCP 服务端点（https://test.yaohaiqing.top/mcp），
                   走 JSON-RPC 的 tools/call，是"用 MCP 推送"的正式路径。

内容渲染**完全复用** push_picks.py 的 build_messages()，保证两种通道
推送的文案字节级一致；本脚本只负责把渲染结果搬运到 MCP 上。

用法
----
    # 列模板 / 查额度 / 查流水（三个只读工具）
    python mcp_wx_push.py --tool list_templates
    python mcp_wx_push.py --tool list_subscriptions --args '{"openid":"of2DX..."}'
    python mcp_wx_push.py --tool list_push_logs --args '{"limit":5}'

    # 推送（按 PRESETS 预设自动选模式）
    python mcp_wx_push.py --openid of2DX... --template CvazWorV... --dry-run
    python mcp_wx_push.py --openid of2DX... --template CvazWorV...

    # 任意工具透传
    python mcp_wx_push.py --tool send_subscribe_message --args '{...}'

令牌来源优先级：--token > 环境变量 WX_MCP_TOKEN > ~/.workbuddy/mcp.json 的
                 mcpServers.wx-subscribe.headers.Authorization（自动剥 Bearer）
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MCP_JSON = Path.home() / ".workbuddy" / "mcp.json"
DEFAULT_URL = "https://test.yaohaiqing.top/mcp"
PROTO = "2024-11-05"

# 复用 push_picks 的渲染逻辑（同目录，可直接 import）
sys.path.insert(0, str(HERE))
try:
    import push_picks as pp
except Exception as e:  # pragma: no cover
    pp = None
    _PP_ERR = e


# ---------------------------------------------------------------------------
# MCP 传输层
# ---------------------------------------------------------------------------
def read_token(cli: str | None, url: str | None = None) -> str:
    """按优先级取 Bearer 令牌"""
    if cli:
        return cli.split()[-1] if cli.lower().startswith("bearer") else cli
    import os
    if os.environ.get("WX_MCP_TOKEN"):
        return os.environ["WX_MCP_TOKEN"].strip()
    if MCP_JSON.exists():
        try:
            cfg = json.loads(MCP_JSON.read_text(encoding="utf-8"))
            srv = (cfg.get("mcpServers") or {}).get("wx-subscribe") or {}
            auth = (srv.get("headers") or {}).get("Authorization", "")
            if auth:
                return auth.split()[-1]
            if srv.get("url"):
                return ""          # 无鉴权
        except Exception:
            pass
    return ""


def read_url(cli: str | None) -> str:
    if cli:
        return cli
    if MCP_JSON.exists():
        try:
            cfg = json.loads(MCP_JSON.read_text(encoding="utf-8"))
            srv = (cfg.get("mcpServers") or {}).get("wx-subscribe") or {}
            if srv.get("url"):
                return srv["url"]
        except Exception:
            pass
    return DEFAULT_URL


class McpError(RuntimeError):
    pass


class McpClient:
    """极简 MCP Streamable-HTTP 客户端（无第三方依赖，只用标准库）"""

    def __init__(self, url: str, token: str, timeout: int = 30, verbose: bool = False):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.verbose = verbose
        self._id = 0
        self.server = {}

    # -- 内部：单次 JSON-RPC POST --
    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        hdr = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTO,
        }
        if self.token:
            hdr["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url, data=body, headers=hdr, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise McpError(f"HTTP {e.code} {e.reason} :: {detail}") from None
        except Exception as e:
            raise McpError(f"连接失败：{e!r}") from None

        if self.verbose:
            print(f"    [raw] {raw[:400]}", file=sys.stderr)

        # SSE 或纯 JSON 两种形态
        txt = raw.strip()
        if txt.startswith("event:") or txt.startswith("data:"):
            for line in txt.splitlines():
                if line.startswith("data:"):
                    try:
                        return json.loads(line[5:].strip())
                    except Exception:
                        continue
            raise McpError("SSE 响应无法解析：" + txt[:200])
        if not txt:
            return {}
        try:
            return json.loads(txt)
        except Exception:
            raise McpError("响应非 JSON：" + txt[:200]) from None

    def handshake(self) -> dict:
        self._id += 1
        r = self._post({
            "jsonrpc": "2.0", "id": self._id, "method": "initialize",
            "params": {"protocolVersion": PROTO, "capabilities": {},
                       "clientInfo": {"name": "workbuddy-mcp-wx-push", "version": "1.0.0"}},
        })
        if "error" in r:
            raise McpError("initialize 失败：" + json.dumps(r["error"], ensure_ascii=False))
        self.server = r.get("result", {}).get("serverInfo", {})
        return self.server

    def call(self, tool: str, args: dict | None = None, notify: bool = True) -> dict:
        if notify:
            try:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            except Exception:
                pass
        self._id += 1
        r = self._post({
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        })
        if "error" in r:
            raise McpError(json.dumps(r["error"], ensure_ascii=False))
        return r.get("result", {})

    def call_json(self, tool: str, args: dict | None = None) -> object:
        """调用工具并把 content[0].text 反序列化成对象（后端返回的是 JSON 文本）"""
        res = self.call(tool, args)
        texts = [c.get("text", "") for c in (res.get("content") or []) if c.get("type") == "text"]
        blob = "\n".join(texts).strip()
        if res.get("isError"):
            raise McpError(f"工具 {tool} 返回错误：" + blob[:400])
        if not blob:
            return None
        try:
            return json.loads(blob)
        except Exception:
            return blob


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="通过远程 MCP 服务推送微信订阅消息（复用 push_picks 的渲染逻辑）")
    ap.add_argument("--openid", help="接收者 openid；填 all 表示所有有余额的用户")
    ap.add_argument("--template", help="微信订阅消息模板 ID（决定字段与模式）")
    ap.add_argument("--mode", choices=["single", "per-stock", "slots", "compact"],
                    help="覆盖模板预设的推送模式")
    ap.add_argument("--mapping", help="自定义字段映射 JSON")
    ap.add_argument("--limit", type=int, help="只推排名前 N 只（默认全部 TOP3）")
    ap.add_argument("--tip", help="slots 模式的「买点提示」覆盖")
    ap.add_argument("--time-fmt", choices=["date-hm", "hm"], default="date-hm",
                    help="时间字段格式：date-hm=『09-15 09:25』(默认) / hm=『09:25』")
    ap.add_argument("--page", help="点击消息跳转的小程序页面")
    ap.add_argument("--board", default=pp.LOCAL_BOARD if pp else None,
                    help="看板地址（取定盘快照）")
    ap.add_argument("--target", help="基准日 YYYY-MM-DD（默认最近交易日）")
    ap.add_argument("--dry-run", action="store_true", help="渲染 + 配额预检，不实际发送")
    ap.add_argument("--tool", help="直接调用某个 MCP 工具（只读工具可配合 --args）")
    ap.add_argument("--args", dest="tool_args", default="{}", help="--tool 的参数 JSON")
    ap.add_argument("--url", help="MCP 端点（默认读 mcp.json）")
    ap.add_argument("--token", help="Bearer 令牌（默认读 mcp.json / 环境变量）")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    if pp is None:
        print(f"[!] 无法导入 push_picks.py：{_PP_ERR!r}", file=sys.stderr)
        return 1

    url = read_url(args.url)
    token = read_token(args.token, url)
    cli = McpClient(url, token, timeout=args.timeout, verbose=args.verbose)

    try:
        srv = cli.handshake()
    except McpError as e:
        print(f"[!] MCP 握手失败：{e}", file=sys.stderr)
        return 1
    print(f"[*] MCP: {srv.get('name')} v{srv.get('version')} @ {url}")
    print(f"[*] 令牌: {(token[:12] + '…' + token[-6:]) if token else '(无)'}")

    # ---- 纯工具透传模式 ----
    if args.tool:
        try:
            payload = json.loads(args.tool_args)
        except Exception as e:
            print(f"[!] --args 不是合法 JSON：{e}", file=sys.stderr)
            return 1
        try:
            out = cli.call_json(args.tool, payload)
        except McpError as e:
            print(f"[!] {e}", file=sys.stderr)
            return 3
        print(f"\n=== {args.tool} ===")
        print(json.dumps(out, ensure_ascii=False, indent=2)
              if not isinstance(out, str) else out)
        return 0

    # ---- 推送模式 ----
    if not args.template:
        print("[!] 推送模式必须给 --template；或改用 --tool 调只读工具", file=sys.stderr)
        return 1

    # 1) 模板与字段
    try:
        tpls = cli.call_json("list_templates")
    except McpError as e:
        print(f"[!] list_templates 失败：{e}", file=sys.stderr)
        return 3
    tpl_list = tpls if isinstance(tpls, list) else (tpls or {}).get("templates", tpls)
    cur = None
    if isinstance(tpl_list, list):
        for t in tpl_list:
            tid = t.get("templateId") or t.get("template_id") or t.get("id")
            if tid == args.template:
                cur = t
                break
    print(f"\n[*] 模板登记：{json.dumps(cur, ensure_ascii=False) if cur else '（后端未登记该 ID）'}")

    # 2) 渲染
    picks, meta = pp.load_picks(
        __import__("datetime").date.fromisoformat(args.target) if args.target else None,
        args.board)
    meta["tip_override"] = args.tip
    meta["time_with_date"] = (args.time_fmt == "date-hm")
    if args.limit:
        picks = picks[:args.limit]
    if not picks:
        print("[!] 无候选标的（定盘快照为空或看板未响应）", file=sys.stderr)
        return 1
    mapping = json.loads(args.mapping) if args.mapping else None
    msgs, mode = pp.build_messages(picks, meta, args.template, args.mode, mapping)
    print(f"[*] 模式={mode}  标的数={len(picks)}  消息数={len(msgs)}")
    for m in msgs:
        print(f"    {m['stock']}")
        for k, v in m["data"].items():
            val = v.get("value", "")
            warn = " ⚠超20字符" if isinstance(val, str) and len(val) > pp.THING_MAX else ""
            print(f"      {k:<9} = {val}  ({len(val)}字符){warn}")

    # 3) 配额
    targets = []
    subs = cli.call_json("list_subscriptions", {})
    rows = subs if isinstance(subs, list) else (subs or {}).get("subscriptions", subs)
    # 配额是 (openid × template_id) 二元组 —— 必须按模板筛，否则会串档
    recs: dict[tuple[str, str], dict] = {}
    by_openid: dict[str, dict] = {}
    if isinstance(rows, list):
        for r in rows:
            oid, tid = r.get("openid"), r.get("template_id") or r.get("templateId")
            if oid is None:
                continue
            recs[(oid, tid)] = r
            # 同 openid 多条时，保留 remain 更大的那条做兜底展示
            if oid not in by_openid or (r.get("remain") or 0) > (by_openid[oid].get("remain") or 0):
                by_openid[oid] = r

    def quota_of(oid: str):
        r = recs.get((oid, args.template))
        return (r or by_openid.get(oid) or {}), (r or {}).get("remain",
                                                             (by_openid.get(oid) or {}).get("remain"))

    if args.openid and args.openid != "all":
        targets = [x.strip() for x in args.openid.split(",") if x.strip()]
    elif isinstance(rows, list):
        # all：只取「对本模板有余额」的订阅者
        targets = [r["openid"] for r in rows
                   if (r.get("template_id") or r.get("templateId")) == args.template
                   and (r.get("remain") or r.get("quota") or 0) > 0]
        targets = list(dict.fromkeys(targets))          # 去重保序
    print(f"\n[*] 目标订阅者 {len(targets)} 个（模板 {args.template[:14]}…）:")
    need = len(msgs)
    ok, blocked = [], []
    for oid in targets:
        _r, remain = quota_of(oid)
        matched = (oid, args.template) in recs
        good = (remain is None or remain >= need)
        (ok if good else blocked).append(oid)
        print(f"    {oid[:26]}…  remain={remain}"
              f"{'' if matched else ' (该模板无记录·按兜底值)'}"
              f"  {'✓' if good else '✗ 配额不足'}")
    if not targets:
        print("[!] 无目标订阅者", file=sys.stderr)
        return 1
    if blocked:
        print(f"[!] {len(blocked)} 个订阅者配额不足（需 {need}），将跳过")
    if not ok:
        print("[!] 所有目标配额均不足，终止", file=sys.stderr)
        return 2

    if args.dry_run:
        print("\n[*] --dry-run：未实际发送（零配额消耗）")
        return 0

    # 4) 发送
    print("\n=== 开始推送（经 MCP）===")
    sent = fail = 0
    for oid in ok:
        for i, m in enumerate(msgs, 1):
            try:
                res = cli.call("send_subscribe_message",
                               {"openid": oid, "templateId": args.template,
                                "data": m["data"], **({"page": args.page} if args.page else {})})
                texts = [c.get("text", "") for c in (res.get("content") or [])]
                blob = "\n".join(texts).strip()
                good = (not res.get("isError")) and ("NO_QUOTA" not in blob.upper())
                if good:
                    sent += 1
                else:
                    fail += 1
                tag = "OK " if good else "ERR"
                print(f"  [{tag}] {oid[:24]}… [{i}/{len(msgs)}] {m['stock']:<20} {blob[:150]}")
            except McpError as e:
                fail += 1
                print(f"  [ERR] {oid[:24]}… [{i}/{len(msgs)}] {m['stock']:<20} {e}")
    print(f"\n[*] 完成：成功 {sent} 条，失败 {fail} 条（经 MCP）")

    # 5) 回查剩余额度
    try:
        after = cli.call_json("list_subscriptions", {})
        rows2 = after if isinstance(after, list) else (after or {}).get("subscriptions", after)
        if isinstance(rows2, list):
            print("[*] 推送后余额:")
            for r in rows2:
                if r.get("openid") in ok:
                    print(f"    {r['openid'][:26]}…  remain={r.get('remain', r.get('quota'))}")
    except McpError:
        pass
    return 0 if fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
