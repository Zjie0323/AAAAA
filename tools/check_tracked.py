# -*- coding: utf-8 -*-
"""版本库内容预检 —— 预演「哪些文件会被提交」，发现误带的数据/凭据/大文件。

为什么需要它
------------
本机（Windows 开发环境）可能未安装 git，无法用 `git status` / `git check-ignore`
验证 .gitignore 是否真正生效。本脚本实现 gitignore 的常用语义，在提交前给出
将入库的文件清单与体积，用于捕捉三类典型事故：

  1. 大文件误入库（kdata/ 22MB、构建产物、压缩包）
  2. 运行时数据误入库（每日行情名单、定盘快照、日志）
  3. 凭据误入库（.env）

用法
----
    python tools/check_tracked.py                    # 汇总 + 可疑项提示
    python tools/check_tracked.py --list             # 列出全部将入库文件
    python tools/check_tracked.py --dir astock-screen # 只看某目录

退出码: 0 = 干净；1 = 发现可疑项（凭据 / 大文件），提交前请确认
"""
import argparse
import fnmatch
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 体积告警阈值
WARN_SIZE = 200 * 1024
# 命中即告警的敏感文件名片段
SENSITIVE = [".env", "credential", "secret", "password", ".pem", ".key", "mcp.json"]
# 模板类文件不含真实值，不应按凭据告警
SAFE_SUFFIX = (".example", ".template", ".sample", ".dist", ".example.txt")
# 已知的合理大文件：慢变数据，有意入库（见 README「数据与凭据」）
ALLOW_BIG = {
    "astock-screen/limit_up.json",     # 涨停基因池 325 KB
    "astock-screen/raw_stats.json",    # code→name 映射 1.3 MB
}


def is_sensitive(name):
    low = name.lower()
    if low.endswith(SAFE_SUFFIX):
        return False
    return any(s in low for s in SENSITIVE)


def _to_regex(pat):
    """把 gitignore 通配符翻译为正则（* 不跨 /，** 跨 /）。"""
    i, n, out = 0, len(pat), []
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < n and pat[i] == "/":
                    i += 1
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c in ".+^${}()|[]\\":
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


class Ignore:
    """gitignore 的简化实现，覆盖本项目实际用到的语法。"""

    def __init__(self, path):
        self.rules = []          # (原始 pattern, negate, dir_only, 正则)
        self.patterns = []
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.rstrip("\n").rstrip("\r")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                negate = line.startswith("!")
                if negate:
                    line = line[1:]
                line = line.rstrip()
                if not line:
                    continue
                dir_only = line.endswith("/")
                pat = line[:-1] if dir_only else line
                if pat.startswith("/"):
                    pat = pat[1:]
                regex = _to_regex(pat)
                # 含斜杠的规则锚定到仓库根；否则匹配任意层级
                anchored = "/" in pat
                full = ("^" + regex + "$") if anchored else ("(^|.*/)" + regex + "$")
                self.rules.append((pat, negate, dir_only, re.compile(full)))

    def ignored(self, rel, is_dir=False):
        rel = rel.replace("\\", "/")
        parts = rel.split("/")
        state = False
        # 祖先目录：任一被忽略则整体忽略
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            for pat, negate, dir_only, rx in self.rules:
                if dir_only and rx.match(prefix):
                    state = not negate
        # 自身：后出现的规则优先
        for pat, negate, dir_only, rx in self.rules:
            if dir_only and not is_dir:
                continue
            if rx.match(rel):
                state = not negate
        return state


def collect(ig, only_dir=None):
    """返回 (将入库文件, 被忽略的文件, 被剪枝的目录)。"""
    tracked, skipped, pruned = [], [], []
    for root, dirs, files in os.walk(ROOT):
        rel_root = os.path.relpath(root, ROOT).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""
        keep = []
        for d in dirs:
            rel_d = (rel_root + "/" + d) if rel_root else d
            if ig.ignored(rel_d, True):
                pruned.append(rel_d)
            else:
                keep.append(d)
        dirs[:] = keep
        for f in files:
            rel = (rel_root + "/" + f) if rel_root else f
            if only_dir and not rel.replace("\\", "/").startswith(only_dir):
                continue
            if ig.ignored(rel, False):
                skipped.append(rel)
            else:
                tracked.append(rel)
    return tracked, skipped, pruned


def main():
    ap = argparse.ArgumentParser(description="预演将进版本库的文件")
    ap.add_argument("--list", action="store_true", help="列出全部将入库文件")
    ap.add_argument("--dir", default=None, help="只看某目录前缀")
    args = ap.parse_args()

    ig = Ignore(os.path.join(ROOT, ".gitignore"))
    if not ig.rules:
        print("!! 未找到 .gitignore 或规则为空: %s" % os.path.join(ROOT, ".gitignore"))
        return 1

    tracked, skipped, pruned = collect(ig, args.dir)
    tracked.sort()

    total = sum(os.path.getsize(os.path.join(ROOT, p)) for p in tracked)
    print("=== 将进版本库 ===")
    print("  文件 %d 个 , 合计 %.2f MB" % (len(tracked), total / 1048576.0))
    print("=== 已忽略 ===")
    print("  文件 %d 个；整体排除的目录 %d 个（%s）"
          % (len(skipped), len(pruned),
             ", ".join(sorted(pruned)) if pruned else "无"))

    # 按顶层目录/文件归类
    buckets = {}
    for p in tracked:
        top = p.split("/")[0] if "/" in p else "(根目录文件)"
        b = buckets.setdefault(top, [0, 0])
        b[0] += 1
        b[1] += os.path.getsize(os.path.join(ROOT, p))
    print("\n=== 分布 ===")
    for k in sorted(buckets, key=lambda x: -buckets[x][1]):
        n, sz = buckets[k]
        print("  %-26s %5d 个  %9s" % (k, n, fmt(sz)))

    if args.list:
        print("\n=== 文件清单 ===")
        for p in tracked:
            print("  %-64s %9s" % (p, fmt(os.path.getsize(os.path.join(ROOT, p)))))

    # 可疑项
    sens = [p for p in tracked if is_sensitive(os.path.basename(p))]
    big = [p for p in tracked
           if os.path.getsize(os.path.join(ROOT, p)) > WARN_SIZE
           and p.replace("\\", "/") not in ALLOW_BIG]

    print("\n=== 预检 ===")
    if sens:
        print("  [!] 敏感文件将被提交（请确认）:")
        for p in sens:
            print("      %s" % p)
    else:
        print("  [ok] 无凭据类文件被提交")

    if big:
        print("  [!] 超过 %d KB 且不在白名单内的大文件:" % (WARN_SIZE // 1024))
        for p in big:
            print("      %-58s %9s" % (p, fmt(os.path.getsize(os.path.join(ROOT, p)))))
    else:
        allowed = [p for p in tracked
                   if p.replace("\\", "/") in ALLOW_BIG]
        print("  [ok] 无超限大文件"
              + ("（已知白名单 %d 个：%s）" % (len(allowed), ", ".join(allowed))
                 if allowed else ""))

    return 1 if (sens or big) else 0


def fmt(n):
    if n >= 1048576:
        return "%.2f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


if __name__ == "__main__":
    sys.exit(main())
