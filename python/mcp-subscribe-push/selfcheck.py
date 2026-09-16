# -*- coding: utf-8 -*-
"""MCP 服务自检脚本 —— 不依赖任何客户端，直接走 stdio 协议验证服务可用性。

用法（必须用本服务的 venv 解释器，否则缺依赖）:
    C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\mcp-subscribe-push\\Scripts\\python.exe selfcheck.py

校验四层: initialize 握手 -> tools/list 工具定义 -> 缺参拒绝 -> stderr 诊断
注意: 本脚本不会发起真实推送。
"""
import json
import os
import queue
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "main.py")

out_q: "queue.Queue" = queue.Queue()
err_lines: list = []


def _pump(stream, q):
    try:
        for line in iter(stream.readline, b""):
            q.put(line.decode("utf-8", "replace"))
    except Exception:
        pass
    finally:
        q.put(None)


def main() -> int:
    py = sys.executable
    print("解释器 :", py)
    print("脚本   :", SCRIPT)
    print()

    if not os.path.exists(SCRIPT):
        print("!! main.py 不存在")
        return 2

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    p = subprocess.Popen(
        [py, SCRIPT], cwd=HERE,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )
    threading.Thread(target=_pump, args=(p.stdout, out_q), daemon=True).start()
    threading.Thread(target=lambda: err_lines.extend(
        l.decode("utf-8", "replace").rstrip() for l in iter(p.stderr.readline, b"")
    ), daemon=True).start()

    def send(obj) -> bool:
        try:
            p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            p.stdin.flush()
            return True
        except Exception as e:
            print("  !! 写入子进程失败（进程可能已退出）:", e)
            return False

    def recv(timeout=20):
        try:
            line = out_q.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except Exception:
            return {"_raw": line}

    def dump_err(tag: str) -> None:
        print("  -- 子进程 stderr --")
        shown = False
        for l in list(err_lines)[:20]:
            if l.strip():
                print("    |", l)
                shown = True
        if not shown:
            print("    | (stderr 为空)")
        print("  -- 退出码:", p.poll(), "--  [%s]" % tag)

    ok = True

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "selfcheck", "version": "1.0"}}})
    r1 = recv()
    if not r1 or "result" not in r1:
        print("[1/4] initialize      FAIL ->", r1)
        dump_err("initialize 失败，服务无法启动")
        try:
            p.kill()
        except Exception:
            pass
        return 1
    si = r1["result"].get("serverInfo", {})
    print("[1/4] initialize      OK   serverInfo=%s  protocol=%s"
          % (si, r1["result"].get("protocolVersion")))

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    r2 = recv()
    tools = (r2 or {}).get("result", {}).get("tools", []) if r2 else []
    if not tools:
        print("[2/4] tools/list      FAIL ->", r2)
        ok = False
    else:
        print("[2/4] tools/list      OK   共 %d 个工具" % len(tools))
        for t in tools:
            sch = t.get("inputSchema", {})
            req = sch.get("required", [])
            props = list(sch.get("properties", {}).keys())
            print("        - %s" % t["name"])
            print("          required : %s" % req)
            print("          optional : %s" % [x for x in props if x not in req])
            print("          default  : apiBaseUrl=%s"
                  % sch.get("properties", {}).get("apiBaseUrl", {}).get("default"))

    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "send-subscribe-push", "arguments": {"openid": "x"}}})
    r3 = recv(timeout=15)
    if r3 and ("error" in r3 or r3.get("result", {}).get("isError")):
        msg = r3.get("error", {}).get("message") or str(r3.get("result"))[:120]
        print("[3/4] 参数校验        OK   缺参被正确拒绝 -> %s" % str(msg)[:110])
    elif r3 and "result" in r3:
        print("[3/4] 参数校验        WARN 缺参未被拒绝 ->", str(r3)[:150])
    else:
        print("[3/4] 参数校验        FAIL 无响应/进程异常退出")
        ok = False

    # 4) 凭证配置检查（只看是否配置，不打印内容）
    cfg = os.path.join(HERE, ".env")
    if os.path.exists(cfg):
        txt = open(cfg, encoding="utf-8").read()
        has_tok = any(l.strip().startswith("ADMIN_TOKEN=") and l.split("=", 1)[1].strip()
                      for l in txt.splitlines())
        has_pwd = any(l.strip().startswith("ADMIN_PASSWORD=") and l.split("=", 1)[1].strip()
                      for l in txt.splitlines())
        if has_tok or has_pwd:
            print("[4/4] 凭证配置        OK   %s" % ("ADMIN_TOKEN" if has_tok else "用户名+密码"))
        else:
            print("[4/4] 凭证配置        WARN .env 存在但凭证为空，调用时会报错")
            ok = False
    else:
        print("[4/4] 凭证配置        WARN 未找到 .env，需改用环境变量注入")
        ok = False

    try:
        p.stdin.close()
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()

    if err_lines and any(l.strip() for l in err_lines):
        print()
        print("--- stderr ---")
        for l in err_lines[:15]:
            if l.strip():
                print(" ", l)

    print()
    print("结论:", "服务可正常运行" if ok else "存在问题，见上方 FAIL/WARN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
