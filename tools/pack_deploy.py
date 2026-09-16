# -*- coding: utf-8 -*-
"""部署包打包器 —— 把 deploy/ 打成 dist/astock-realtime-deploy.tar.gz。

背景
----
本机 Windows 未安装 tar/bsdtar，无法用 `tar -czf` 打包。改用 Python tarfile，
并**显式修正 Unix 权限位** —— Windows 文件系统没有 Unix mode，若不修正，
解压后 install.sh / scripts/*.sh 会丢失可执行位，`sudo ./install.sh` 报
"Permission denied"。

前置条件
--------
先跑 `python tools/build_deploy.py` 生成 deploy/app/。本脚本会自检 app/ 是否就绪。

用法
----
    python tools/pack_deploy.py             # 打包 deploy/ -> dist/*.tar.gz
    python tools/pack_deploy.py --check     # 只列出将被打包的文件与权限，不写包

排除项: *.bak* / __pycache__ / *.pyc / .DS_Store / .git
权限:   *.sh -> 0755 ; 其余 -> 0644 ; 属主 root:root
"""
import argparse
import os
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(ROOT, "deploy")
OUT_DIR = os.path.join(ROOT, "dist")
OUT_TGZ = os.path.join(OUT_DIR, "astock-realtime-deploy.tar.gz")

# 解压后的顶层目录名 —— 必须与 README / install.sh 里的说明一致
ARC_NAME = "astock-realtime-deploy"

EXCLUDE_SUFFIX = (".bak", ".pyc")
EXCLUDE_PARTS = ("__pycache__", ".git", ".DS_Store")


def _excluded(path):
    name = os.path.basename(path)
    if name.endswith(EXCLUDE_SUFFIX) or ".bak-" in name or ".bak." in name:
        return True
    return any(part in path for part in EXCLUDE_PARTS)


def _filter(ti):
    """排除噪声文件, 并写入正确的 Unix 权限/属主。"""
    if _excluded(ti.name):
        return None
    if ti.isdir():
        ti.mode = 0o755
    elif ti.name.endswith(".sh"):
        ti.mode = 0o755          # install.sh 与 ctl-*.sh 必须可执行
    else:
        ti.mode = 0o644
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    return ti


def precheck():
    """打包前自检：app/ 是否已由 build_deploy.py 组装。"""
    errs = []
    if not os.path.isdir(SRC_DIR):
        errs.append("deploy/ 不存在")
        return errs
    for f in ["app/server.py", "app/index.html", "install.sh", "deploy.conf"]:
        if not os.path.isfile(os.path.join(SRC_DIR, f)):
            errs.append("deploy/%s 缺失 → 请先运行 python tools/build_deploy.py" % f)
    for f in ["em.py", "gen_tomorrow.py", "live_limit.py", "limit_up.py",
              "reco_engine.py", "refresh_watch.py"]:
        if not os.path.isfile(os.path.join(SRC_DIR, "app", "screen", f)):
            errs.append("deploy/app/screen/%s 缺失 → 请先运行 tools/build_deploy.py" % f)
    return errs


def main():
    ap = argparse.ArgumentParser(description="打包 deploy/ 为 dist/*.tar.gz")
    ap.add_argument("--check", action="store_true", help="只列清单，不写包")
    args = ap.parse_args()

    errs = precheck()
    if errs:
        for e in errs:
            print("  [!] " + e)
        print("\n!! 前置检查未通过")
        return 1

    if args.check:
        print("将打包 (源: deploy/):")
        n = 0
        for root, dirs, files in os.walk(SRC_DIR):
            dirs[:] = [d for d in dirs if not _excluded(d)]
            for f in sorted(files):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, SRC_DIR)
                if _excluded(rel):
                    print("  [跳过] %s" % rel)
                    continue
                if len(rel.split(os.sep)) > 3 and "kdata" in rel:
                    n += 1
                    continue          # kdata 折叠显示
                mode = "0755" if f.endswith(".sh") else "0644"
                print("  [%s] %s" % (mode, os.path.join(ARC_NAME, rel)))
        if n:
            print("  ... 另有 kdata/ 日K缓存 %d 个（0644）" % n)
        print("\n[check] 未写包")
        return 0

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    if os.path.exists(OUT_TGZ):
        os.remove(OUT_TGZ)

    n = 0
    with tarfile.open(OUT_TGZ, "w:gz") as tf:
        for root, dirs, files in os.walk(SRC_DIR):
            dirs[:] = [d for d in dirs if not _excluded(d)]
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, SRC_DIR)
                if _excluded(rel):
                    print("  [跳过] %s" % rel)
                    continue
                tf.add(full, arcname=os.path.join(ARC_NAME, rel), filter=_filter)
                n += 1

    size = os.path.getsize(OUT_TGZ)
    print("[OK] 已写出 %s" % os.path.relpath(OUT_TGZ, ROOT))
    print("     文件 %d 个 , %.1f MB" % (n, size / 1048576.0))

    # ---------------- 包内自检 ----------------
    # 三类问题都只在服务器上才暴露，因此在这里一次验完：
    #   1. 关键文件缺失
    #   2. 误带运行时名单（会让次日复盘退化成「自我对照」）
    #   3. 权限位/行尾错误（install.sh 无法执行 / bad interpreter）
    with tarfile.open(OUT_TGZ, "r:gz") as tf:
        members = {m.name: m for m in tf.getmembers()}

        must = ["%s/app/server.py" % ARC_NAME, "%s/app/index.html" % ARC_NAME,
                "%s/install.sh" % ARC_NAME, "%s/app/screen/reco_engine.py" % ARC_NAME,
                "%s/deploy.conf" % ARC_NAME]
        miss = [m for m in must if m not in members]

        bad_data = [x for x in members
                    if os.path.basename(x).startswith(("tomorrow_watch_", "live_raw_", "_review_"))
                    and os.path.basename(x) != "tomorrow_watch.html"]

        # 权限位：.sh 必须 0755（否则服务器上 install.sh 无法执行）
        bad_mode = [x for x, m in members.items()
                    if x.endswith(".sh") and m.isfile() and m.mode != 0o755]

        # 行尾：.sh 必须为 LF（否则报 /bin/bash^M: bad interpreter）
        bad_eol = []
        for x, m in members.items():
            if m.isfile() and x.endswith(".sh"):
                fh = tf.extractfile(x)
                if fh and b"\r\n" in fh.read():
                    bad_eol.append(x)

    print("\n=== 包内自检 ===")
    print("  关键文件   : %s" % ("齐全" if not miss else "缺失 %s" % miss))
    print("  运行时产物 : %s" % ("无" if not bad_data else "混入 %s" % bad_data))
    print("  脚本权限位 : %s" % ("全部 0755" if not bad_mode else "异常 %s" % bad_mode))
    print("  脚本行尾   : %s" % ("全部 LF" if not bad_eol else "含 CRLF %s" % bad_eol))

    problems = miss or bad_data or bad_mode or bad_eol
    print("  结论       : %s" % ("!! 存在问题，请修正后重打" if problems else "可安全部署 ✓"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
