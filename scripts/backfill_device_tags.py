#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory v2.2 存量迁移：为历史条目回填 device:/scope: 标签。

背景：v2.2 之前写入的条目没有设备维度（实测 52% active 条目携带环境特征，
0 条有 device: 标签）。本脚本按内容特征推断并回填，幂等可重跑。

推断规则（零 LLM，正则特征）：
  来源设备 device:  —— 按已知设备特征词：
    zhengzefeng / 32726 / C:\\Users\\32726   → device:zhengzefeng（后续可在该机
        配置 config device.name 后用 --rename 统一改名，如 workstation）
    C:\\Users\\Lenovo / F:\\Anaconda3 / 本机...Lenovo → device:lenovo（本机）
    其余无法判定 → 不填（检索端标 🖥️? 来源不明）
  适用范围 scope:  —— 环境强相关特征（盘符/本机/主机名/工作机/ThinkBook/285K/10400 等）
    命中 → scope:device；否则 → scope:global

用法:
  python scripts/backfill_device_tags.py --dry-run   # 预览
  python scripts/backfill_device_tags.py             # 执行 + 重建索引
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import memory_tool as mt  # noqa: E402

DEVICE_PATTERNS = [
    ("zhengzefeng", re.compile(r"zhengzefeng|32726|C:[/\\]+Users[/\\]+32726", re.I)),
    ("lenovo",      re.compile(r"C:[/\\]+Users[/\\]+Lenovo|F:[/\\]+Anaconda3|本机[^，。\n]{0,12}[Ll]enovo|[Ll]enovo[^，。\n]{0,8}(设备|笔记本|主机)")),
]

def infer_source_device(text):
    for name, pat in DEVICE_PATTERNS:
        if pat.search(text):
            return name
    return ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stats = {"device": 0, "scope": 0, "skipped": 0, "total": 0}
    for fp in mt.all_entry_files():
        with open(fp, encoding="utf-8") as f:
            text = f.read()
        meta, body = mt.parse_frontmatter(text)
        stats["total"] += 1
        tags = [t for t in (meta.get("tags") or []) if t]
        changed = False

        # scope 回填（幂等：已有 scope: 标签跳过）
        if not any(str(t).startswith("scope:") for t in tags):
            scope = mt.infer_scope(meta.get("title", ""), body)
            tags.append(scope)
            stats["scope"] += 1
            changed = True

        # device 回填（幂等：已有 device: 标签跳过；按内容特征推断来源）
        if not any(str(t).startswith("device:") for t in tags):
            probe = f"{meta.get('title','')}\n{body[:2000]}"
            dev = infer_source_device(probe)
            if dev:
                tags.append(f"device:{dev}")
                stats["device"] += 1
                changed = True

        if not changed:
            stats["skipped"] += 1
            continue
        if args.dry_run:
            continue
        meta["tags"] = tags
        meta["updated_at"] = mt.now_str()
        with open(fp, "w", encoding="utf-8") as f:
            f.write(mt.build_frontmatter(meta, body))

    mode = "（dry-run 预览）" if args.dry_run else ""
    print(f"存量迁移完成{mode}：共 {stats['total']} 条 | "
          f"补 scope {stats['scope']} | 补 device {stats['device']} | 无需变更 {stats['skipped']}")
    if not args.dry_run:
        mt.rebuild_index(verbose=False)
        print("索引已重建")


if __name__ == "__main__":
    mt._utf8("")
    main()
