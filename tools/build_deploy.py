# -*- coding: utf-8 -*-
"""部署包组装器 —— 把源码目录组装成 deploy/app/（install.sh 的唯一输入）。

为什么需要它
------------
此前 deploy/app/ 的内容是**手工从 astock-realtime/ 与 astock-screen/ 拷贝**的。
手工同步长期必然漂移：改了源码忘记拷 → 部署到服务器的还是旧代码，且不易察觉。
本脚本把这一步收敛为可重复执行的单一入口，并内建三道自检。

部署布局（install.sh 的硬假设，不可更改）
----------------------------------------
    deploy/app/server.py     ->  /opt/astock-realtime/server.py
    deploy/app/index.html    ->  /opt/astock-realtime/index.html
    deploy/app/screen/*      ->  /opt/astock-screen/*
    deploy/scripts/*.sh      ->  /usr/local/bin/<app>-<name>  与  $INSTALL_DIR/lib.sh
    deploy/systemd/*.template->  /etc/systemd/system（sed 渲染占位符后）

目录名约束（务必保持）
----------------------
    astock-realtime/server.py    以 ../astock-screen/ 为基准读名单并 import reco_engine
    astock-screen/refresh_watch.py 以 ../astock-realtime/ 为基准复用 market_open_today()

服务器上这两个目录名固定为 astock-realtime / astock-screen，因此**本地源码目录名
也必须保持**，否则跨目录引用在开发期就无法自测。

用法
----
    python tools/build_deploy.py               # 组装（覆盖 deploy/app/）
    python tools/build_deploy.py --check       # 只列出将复制的文件，不写盘
    python tools/build_deploy.py --skip-kdata  # 跳过 1194 个日K缓存（快速验证布局用）
    python tools/build_deploy.py --clean       # 先清空 deploy/app/ 再组装

退出码: 0 = 成功；1 = 自检未通过（缺文件 / 混入运行时产物）
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # 仓库根
RT_DIR = os.path.join(ROOT, "astock-realtime")     # 看板源码
SC_DIR = os.path.join(ROOT, "astock-screen")       # 选股引擎源码
DEPLOY_DIR = os.path.join(ROOT, "deploy")          # 部署物料
APP_DIR = os.path.join(DEPLOY_DIR, "app")          # 组装产物

# ---------------------------------------------------------------------------
# 清单：进包的文件（单一事实源）
# ---------------------------------------------------------------------------
# 看板服务（与 index.html 必须同目录 —— server.py 按 __file__ 定位静态页）
REALTIME_FILES = ["server.py", "index.html"]

# 选股工具链脚本：install.sh 第 99 行会逐一校验存在性
SCREEN_SCRIPTS = [
    "em.py",             # 东方财富接口封装（单一事实源）
    "gen_tomorrow.py",   # 生成次日竞价观察名单
    "live_limit.py",     # 抓取当日涨停 / 炸板专题池
    "limit_up.py",       # 重算历史涨停基因池（读 kdata/）
    "reco_engine.py",    # 智能推荐引擎（六维分 / 赚钱效应分 / 买点门控）
    "refresh_watch.py",  # 刷新入口（server.py 亦 import 本目录）
]

# 慢变数据：随包分发，免服务器首次重算
SCREEN_DATA = [
    "limit_up.json",     # 近1年涨停基因池（约 325 KB）
    "raw_stats.json",    # 候选池 code→name 映射（约 1.3 MB）
]

# 慢变数据目录：1194 只个股近1年日K缓存（约 22 MB），供基因池重算
SCREEN_DATA_DIRS = ["kdata"]

# ---------------------------------------------------------------------------
# 禁止进包的文件（运行时产物）—— 组装后自检，防止误带
# ---------------------------------------------------------------------------
# 名单是「每日变化的行情产物」，固定打包会过期误导（基准日错位会让次日复盘
# 退化成自我对照，结论完全失真）。故名单一律由服务器本地盘后生成。
FORBIDDEN_NAMES = [
    "tomorrow_watch.json", "tomorrow_watch.txt", "tomorrow_watch.html",
    "live_raw.json", "_review.json", "limit_live.html",
]
FORBIDDEN_PREFIXES = ["tomorrow_watch_", "live_raw_", "_review_", "_bid_picks_",
                      "_auto_push_state_"]

# systemd / scripts 的必需项（install.sh 会引用，缺则安装中断）
SYSTEMD_REQUIRED = [
    "astock-realtime.service.template",
    "astock-realtime-refresh.service.template",
    "astock-realtime-refresh.timer.template",
]
SCRIPTS_REQUIRED = [
    "lib.sh", "ctl-start.sh", "ctl-stop.sh", "ctl-restart.sh",
    "ctl-status.sh", "ctl-check.sh", "ctl-port.sh", "ctl-uninstall.sh",
]


def _is_forbidden(rel):
    base = os.path.basename(rel)
    if base in FORBIDDEN_NAMES:
        return True
    return any(base.startswith(p) for p in FORBIDDEN_PREFIXES)


def _plan(skip_kdata):
    """返回 [(源绝对路径, 目标相对 deploy/ 的路径), ...]。"""
    items = []

    for f in REALTIME_FILES:
        items.append((os.path.join(RT_DIR, f), os.path.join("app", f)))

    for f in SCREEN_SCRIPTS + SCREEN_DATA:
        items.append((os.path.join(SC_DIR, f), os.path.join("app", "screen", f)))

    if not skip_kdata:
        for d in SCREEN_DATA_DIRS:
            src = os.path.join(SC_DIR, d)
            if not os.path.isdir(src):
                continue
            for name in sorted(os.listdir(src)):
                fp = os.path.join(src, name)
                if os.path.isfile(fp):
                    items.append((fp, os.path.join("app", "screen", d, name)))

    # 部署物料本身不进 app/，由 install.sh 直接引用，此处仅纳入完整性自检
    return items


def verify_sources():
    """源侧自检：必需文件是否齐全。返回错误列表。"""
    errs = []
    for f in REALTIME_FILES:
        p = os.path.join(RT_DIR, f)
        if not os.path.isfile(p):
            errs.append("缺少看板源码: astock-realtime/%s" % f)
    for f in SCREEN_SCRIPTS + SCREEN_DATA:
        p = os.path.join(SC_DIR, f)
        if not os.path.isfile(p):
            errs.append("缺少工具链文件: astock-screen/%s" % f)
    if not os.path.isdir(os.path.join(SC_DIR, "kdata")):
        errs.append("缺少日K缓存目录: astock-screen/kdata/（首次重算基因池会很慢）")
    for f in SYSTEMD_REQUIRED:
        p = os.path.join(DEPLOY_DIR, "systemd", f)
        if not os.path.isfile(p):
            errs.append("缺少 systemd 模板: deploy/systemd/%s" % f)
    for f in SCRIPTS_REQUIRED:
        p = os.path.join(DEPLOY_DIR, "scripts", f)
        if not os.path.isfile(p):
            errs.append("缺少运维脚本: deploy/scripts/%s" % f)
    for f in ["install.sh", "deploy.conf"]:
        p = os.path.join(DEPLOY_DIR, f)
        if not os.path.isfile(p):
            errs.append("缺少部署物料: deploy/%s" % f)
    return errs


def verify_app():
    """产物侧自检：必需文件都在、且没有混入运行时产物。返回错误列表。"""
    errs = []
    for f in REALTIME_FILES:
        p = os.path.join(APP_DIR, f)
        if not os.path.isfile(p):
            errs.append("产物缺少: app/%s" % f)
    for f in SCREEN_SCRIPTS:
        p = os.path.join(APP_DIR, "screen", f)
        if not os.path.isfile(p):
            errs.append("产物缺少: app/screen/%s" % f)

    for root, _dirs, files in os.walk(APP_DIR):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), APP_DIR)
            if _is_forbidden(rel):
                errs.append("产物混入运行时数据: app/%s（名单不应打包）" % rel)
    return errs


def main():
    ap = argparse.ArgumentParser(description="组装 deploy/app/")
    ap.add_argument("--check", action="store_true", help="只列出将复制的文件，不写盘")
    ap.add_argument("--clean", action="store_true", help="先清空 deploy/app/ 再组装")
    ap.add_argument("--skip-kdata", action="store_true",
                    help="跳过 kdata 日K缓存（快速验证布局用）")
    args = ap.parse_args()

    print("=== 源侧自检 ===")
    errs = verify_sources()
    if errs:
        for e in errs:
            print("  [!] " + e)
        # kdata 缺失只影响首次重算速度，属警告；其余为致命
        fatal = [e for e in errs if "kdata" not in e]
        if fatal:
            print("\n!! 源侧自检未通过，中止")
            return 1
        print("  (kdata 缺失为警告，继续)")
    else:
        print("  全部必需文件就位")

    items = _plan(args.skip_kdata)
    total = sum(os.path.getsize(s) for s, _ in items if os.path.isfile(s))
    print("\n=== 组装计划 ===")
    print("  文件 %d 个 , 合计 %.1f MB" % (len(items), total / 1048576.0))
    if args.skip_kdata:
        print("  (已按 --skip-kdata 跳过日K缓存)")

    if args.check:
        print("\n=== 将复制（--check 模式，不写盘）===")
        for s, r in items:
            if len(items) > 40 and "kdata" in r:
                continue          # kdata 太多，折叠显示
            print("  %-58s %9d" % (r, os.path.getsize(s)))
        if len(items) > 40:
            print("  ... 另有 kdata/ 日K缓存 %d 个"
                  % sum(1 for _, r in items if "kdata" in r))
        print("\n[check] 未写盘")
        return 0

    if args.clean and os.path.isdir(APP_DIR):
        shutil.rmtree(APP_DIR)
        print("\n已清空 deploy/app/")

    n = 0
    for s, r in items:
        dst = os.path.join(DEPLOY_DIR, r)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(s, dst)
        n += 1

    print("\n=== 产物侧自检 ===")
    errs = verify_app()
    if errs:
        for e in errs:
            print("  [!] " + e)
        print("\n!! 产物自检未通过")
        return 1
    print("  必需文件齐全，未混入运行时产物")

    print("\n[OK] 已组装 %d 个文件到 deploy/app/" % n)
    print("     下一步: python tools/pack_deploy.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
