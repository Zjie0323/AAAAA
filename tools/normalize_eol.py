# -*- coding: utf-8 -*-
"""行尾规范化 —— 让「工作区 = git 索引 = 部署包」三者行尾一致。

为什么需要它
------------
本项目在 Windows 上开发、部署到 Linux。行尾错位的两类后果都**只在部署时才暴露**：

  .sh 带 CRLF  →  /bin/bash^M: bad interpreter: No such file or directory
  .bat 为 LF   →  cmd 解析多行块 / goto 标签时行为异常

`.gitattributes` 管住的是 git 入库后的行为；**已存在于工作区的文件**仍需一次性归正，
且日常新增脚本时也需要一个检查入口。

规则（与 .gitattributes 完全对应）
----------------------------------
    LF   : .sh .py .json .template .conf .js .md .txt .html .csv
    CRLF : .bat .cmd .ps1

范围
----
**只处理将进版本库的文件**（复用 tools/check_tracked.py 的忽略规则，保持同一事实源）。
被忽略的运行时数据（tomorrow_watch*.json / _review*.json / kdata/ 等）一律不动，
避免影响正在运行的服务。

用法
----
    python tools/normalize_eol.py            # 只检查并报告（默认，不改文件）
    python tools/normalize_eol.py --fix      # 执行规范化（原文件备份到 archive/eol-backup/）
    python tools/normalize_eol.py --fix --dry-run   # 预演将改哪些文件

退出码: 0 = 无需变更；1 = 存在行尾不符（--fix 时表示已修正）
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKUP = os.path.join(ROOT, "archive", "eol-backup")

sys.path.insert(0, HERE)
from check_tracked import Ignore, collect      # noqa: E402

NEED_LF = (".sh", ".py", ".json", ".template", ".conf",
           ".js", ".md", ".txt", ".html", ".csv")
NEED_CRLF = (".bat", ".cmd", ".ps1")


def to_lf(data):
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def to_crlf(data):
    return to_lf(data).replace(b"\n", b"\r\n")


def scan():
    """返回 [(相对路径, crlf数, lf数, 期望格式)]，仅含行尾不符者。"""
    ig = Ignore(os.path.join(ROOT, ".gitignore"))
    tracked, _skipped, _pruned = collect(ig)
    bad = []
    for rel in sorted(tracked):
        low = rel.lower()
        if low.endswith(NEED_LF):
            want = "LF"
        elif low.endswith(NEED_CRLF):
            want = "CRLF"
        else:
            continue
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        data = open(p, "rb").read()
        crlf = data.count(b"\r\n")
        lf = data.count(b"\n") - crlf
        if want == "LF" and crlf > 0:
            bad.append((rel, crlf, lf, want))
        elif want == "CRLF" and crlf == 0:
            bad.append((rel, crlf, lf, want))
    return bad


def main():
    ap = argparse.ArgumentParser(description="行尾检查与规范化")
    ap.add_argument("--fix", action="store_true", help="执行规范化（默认只检查）")
    ap.add_argument("--dry-run", action="store_true", help="配合 --fix，仅预演")
    args = ap.parse_args()

    bad = scan()
    if not bad:
        print("[ok] 所有将入库文件的行尾均符合 .gitattributes 规则")
        return 0

    print("发现 %d 个文件行尾不符：" % len(bad))
    for rel, crlf, lf, want in bad:
        print("   %-58s CRLF=%-6d LF=%-6d 期望=%s" % (rel, crlf, lf, want))

    if not args.fix:
        print("\n提示：加 --fix 执行规范化（原文件会备份到 archive/eol-backup/）")
        return 1

    if args.dry_run:
        print("\n[dry-run] 未改动任何文件")
        return 1

    print()
    for rel, _crlf, _lf, want in bad:
        p = os.path.join(ROOT, rel)
        data = open(p, "rb").read()
        new = to_lf(data) if want == "LF" else to_crlf(data)
        bak = os.path.join(BACKUP, rel)
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        if not os.path.exists(bak):
            shutil.copy2(p, bak)
        with open(p, "wb") as fh:
            fh.write(new)
        print("   [%s] %s" % (want, rel))

    print("\n已规范化 %d 个文件；原文件备份于 archive/eol-backup/" % len(bad))
    return 1


if __name__ == "__main__":
    sys.exit(main())
