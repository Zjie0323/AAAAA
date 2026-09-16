"""
MCP 服务 — 微信小程序订阅消息推送
==============================
暴露 send-subscribe-push 工具，自动处理 admin 鉴权（token 缓存 + 刷新）。

环境变量（任选一种方式）:
  方式一: ADMIN_TOKEN=<管理端 JWT token>
  方式二: ADMIN_USERNAME=admin + ADMIN_PASSWORD=<明文密码或sha256hex>
"""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

# 加载 .env 文件（同级目录）
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = os.environ.get("API_BASE_URL", "https://test.yaohaiqing.top/api")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# token 缓存
_token_cache: str = ""
_token_expires_at: float = 0.0  # Unix 秒


def _sha256_hex(plain: str) -> str:
    """明文 -> SHA-256 十六进制（已在环境变量传 hex 时不重复计算）"""
    return hashlib.sha256(plain.encode()).hexdigest()


async def _login(client: httpx.AsyncClient, base_url: str) -> tuple[str, int]:
    """调用 POST /auth/login 换取管理端 token"""
    pwd = ADMIN_PASSWORD
    # 若已经是 64 位 hex 则直传，否则先 sha256
    if len(pwd) != 64 or not all(c in "0123456789abcdef" for c in pwd.lower()):
        pwd = _sha256_hex(pwd)

    payload = {"username": ADMIN_USERNAME, "password": pwd}
    resp = await client.post(f"{base_url}/auth/login", json=payload)
    if resp.is_error:
        body = _try_json(resp)
        raise RuntimeError(f"登录失败 ({resp.status_code}): {body.get('error', '未知错误')}")

    data = resp.json()
    return data["token"], data.get("expiresIn", 7200)


async def _ensure_token(client: httpx.AsyncClient, base_url: str) -> str:
    """获取有效 token，缓存过期时自动刷新"""
    global _token_cache, _token_expires_at

    if ADMIN_TOKEN:
        return ADMIN_TOKEN

    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise RuntimeError(
            "未配置管理员凭证。请设置 ADMIN_TOKEN 环境变量，"
            "或设置 ADMIN_USERNAME + ADMIN_PASSWORD 自动登录"
        )

    now = datetime.now(timezone.utc).timestamp()
    if _token_cache and now < _token_expires_at - 60:  # 提前 60s 刷新
        return _token_cache

    token, expires_in = await _login(client, base_url)
    _token_cache = token
    _token_expires_at = now + expires_in
    return token


def _try_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


async def _push_subscribe(
    client: httpx.AsyncClient,
    openid: str,
    template_id: str,
    data: dict,
    page: str,
    base_url: str,
) -> str:
    """调用 POST /subscribe/push"""
    token = await _ensure_token(client, base_url)

    body: dict = {"openid": openid, "templateId": template_id, "data": data}
    if page:
        body["page"] = page

    resp = await client.post(
        f"{base_url}/subscribe/push",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    result = _try_json(resp)

    if resp.is_error:
        return json.dumps(
            {
                "status": resp.status_code,
                "error": result.get("error") or result.get("errmsg", "推送失败"),
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )

    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
server = Server("mcp-subscribe-push")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="send-subscribe-push",
            description="下发微信小程序订阅消息（调用后端 POST /subscribe/push 接口，自动处理 admin 鉴权）",
            inputSchema={
                "type": "object",
                "properties": {
                    "openid": {
                        "type": "string",
                        "description": "用户的 openid，例: of2DX5RasJfKxuzrITj9VUwUh9A8",
                    },
                    "templateId": {
                        "type": "string",
                        "description": "微信订阅消息模板 ID",
                    },
                    "page": {
                        "type": "string",
                        "description": "点击卡片后跳转的小程序页面路径，可空",
                    },
                    "data": {
                        "type": "object",
                        "description": (
                            "模板数据，键名与微信模板关键词一一对应。"
                            '例: {"thing1": {"value": "有新提醒了"}, "time2": {"value": "2026年9月14日"}}'
                        ),
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string"},
                            },
                            "required": ["value"],
                        },
                    },
                    "apiBaseUrl": {
                        "type": "string",
                        "description": f"后端 API Base URL，默认 {DEFAULT_BASE_URL}",
                        "default": DEFAULT_BASE_URL,
                    },
                },
                "required": ["openid", "templateId", "data"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "send-subscribe-push":
        raise ValueError(f"未知工具: {name}")

    openid = arguments.get("openid")
    template_id = arguments.get("templateId")
    data = arguments.get("data")
    page = arguments.get("page", "")
    base_url = arguments.get("apiBaseUrl", DEFAULT_BASE_URL)

    if not openid or not template_id or not data:
        raise ValueError("必填参数缺失: openid, templateId, data")

    async with httpx.AsyncClient(timeout=30) as client:
        result = await _push_subscribe(client, openid, template_id, data, page, base_url)

    return [TextContent(type="text", text=result)]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def main() -> None:
    if not ADMIN_TOKEN and (not ADMIN_USERNAME or not ADMIN_PASSWORD):
        import sys

        print(
            "⚠️  未配置管理员凭证。请在环境变量中设置：\n"
            "    方式一: ADMIN_TOKEN=<管理端 token>\n"
            "    方式二: ADMIN_USERNAME=admin + ADMIN_PASSWORD=<明文密码或sha256 hex>\n"
            "    服务会继续启动，但调用 send-subscribe-push 时会报错。",
            file=sys.stderr,
        )

    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="mcp-subscribe-push",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())