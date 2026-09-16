# -*- coding: utf-8 -*-
"""
push_once.py —— 单次真实推送 CLI（复用 main.py 的同一代码路径）
=================================================================
用途：在 MCP 连接器尚未「信任」、或需要脚本化推送时，直接触发一次真实推送。
注意：微信订阅消息为**一次授权一次推送**，配额耗尽（errcode/remain=0）后
      需用户在小程序内重新点击授权，否则会返回 43101。

用法
----
# 1) 查看后端在册模板及其字段（确定 data 键名的唯一可靠来源）
python push_once.py --templates

# 2) 推送（键值对写法，自动包装成 {"value": ...}）
python push_once.py \
    --openid of2DX5X4quEBHr0faqgq7Qo69g5k \
    --template CvazWorVb8j8bey5OrPgMg2L7uNNC_AJpsVnHXaDVIY \
    --kv thing4=联调测试提醒 \
    --kv thing1="2026年9月15日 15:30" \
    --kv thing2=订阅消息推送链路验证 \
    --kv thing5="WorkBuddy 工作台" \
    --kv thing3=收到即证明链路通畅

# 3) 或直接给 JSON（等价）
python push_once.py --openid <id> --template <tid> --data '{"thing1":{"value":"x"}}'
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import main as m  # noqa: E402  复用 .env 加载与鉴权/推送实现


async def _show_templates() -> int:
    async with httpx.AsyncClient(timeout=20) as c:
        token = await m._ensure_token(c, m.DEFAULT_BASE_URL)
        r = await c.get(f"{m.DEFAULT_BASE_URL}/templates",
                        headers={"Authorization": f"Bearer {token}"})
        if r.is_error:
            print(f"查询失败 HTTP {r.status_code}: {r.text[:300]}")
            return 1
        j = r.json()
    print(f"在册模板 {j.get('count')} 个：\n")
    for t in j.get("templates", []):
        print(f"  标题   : {t.get('title')}")
        print(f"  ID     : {t.get('template_id')}")
        print(f"  备注   : {t.get('remark') or '(无)'}")
        print("  字段   :")
        for f in t.get("fields") or []:
            print(f"      {f.get('key'):<14} {f.get('name')}")
        print()
    return 0


def _build_data(args) -> dict:
    if args.data:
        return json.loads(args.data)
    out = {}
    for item in args.kv or []:
        if "=" not in item:
            raise SystemExit(f"--kv 需要 key=value 形式: {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = {"value": v.strip()}
    return out


async def _push(args) -> int:
    data = _build_data(args)
    if not data:
        raise SystemExit("必须提供 --data 或至少一个 --kv")

    print("=== 请求 ===")
    print(f"  openid     : {args.openid}")
    print(f"  templateId : {args.template}")
    print(f"  page       : {args.page or '(空)'}")
    print(f"  data       : {json.dumps(data, ensure_ascii=False)}")

    async with httpx.AsyncClient(timeout=30) as c:
        res = await m._push_subscribe(c, args.openid, args.template,
                                      data, args.page, m.DEFAULT_BASE_URL)

    print("\n=== 响应 ===")
    print(res)

    try:
        j = json.loads(res)
    except Exception:
        return 1

    err = j.get("errcode")
    if err in (0, "0") and not j.get("error") and j.get("status") not in (400, 401, 403, 404, 500):
        print("\n>>> 成功：微信已受理（msgid=%s, remain=%s）" % (j.get("msgid"), j.get("remain")))
        if str(j.get("remain")) == "0":
            print("    提示：remain=0 表示一次性订阅配额已用尽，再推需用户重新授权。")
        return 0

    print("\n>>> 失败：errcode=%s errmsg=%s error=%s"
          % (err, j.get("errmsg"), j.get("error")))
    if err == 43101:
        print("    43101 = 用户未订阅或配额已用尽，需在小程序内重新授权。")
    return 2


def main_cli() -> int:
    ap = argparse.ArgumentParser(description="微信小程序订阅消息 —— 单次真实推送")
    ap.add_argument("--templates", action="store_true", help="列出后端在册模板及字段，然后退出")
    ap.add_argument("--openid", help="用户 openid")
    ap.add_argument("--template", help="模板 ID")
    ap.add_argument("--kv", action="append", metavar="KEY=VALUE",
                    help="模板字段，可重复；自动包装为 {\"value\": ...}")
    ap.add_argument("--data", help="模板数据 JSON（与 --kv 二选一）")
    ap.add_argument("--page", default="", help="点击卡片跳转的小程序页面路径，可空")
    args = ap.parse_args()

    if args.templates:
        return asyncio.run(_show_templates())
    if not args.openid or not args.template:
        ap.error("推送必须提供 --openid 与 --template（或改用 --templates 查看模板）")
    return asyncio.run(_push(args))


if __name__ == "__main__":
    raise SystemExit(main_cli())
