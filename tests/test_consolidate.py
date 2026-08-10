#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""consolidate.py 单元测试：collect_recent 的 7 天窗口过滤逻辑"""
import datetime
import os
import sys
import tempfile

# Windows GBK 控制台兼容
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from consolidate import collect_recent  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def make_entry(title, updated_at, secret=False, category="patterns"):
    meta = {"title": title, "updated_at": updated_at, "category": category, "tags": []}
    if secret:
        meta["secret"] = True
    return {"path": title, "rel": title, "meta": meta, "body": "正文-" + title}


print("===== test_consolidate: collect_recent 窗口过滤 =====")

today = datetime.date.today()
td = datetime.timedelta(days=1)
entries = [
    make_entry("今天更新", today.isoformat()),
    make_entry("3天前", (today - 3 * td).isoformat()),
    make_entry("正好7天前", (today - 7 * td).isoformat()),
    make_entry("8天前", (today - 8 * td).isoformat()),
    make_entry("30天前", (today - 30 * td).isoformat()),
    make_entry("无日期", ""),
    make_entry("坏日期", "not-a-date"),
    make_entry("secret今天", today.isoformat(), secret=True),
    make_entry("secret8天前", (today - 8 * td).isoformat(), secret=True),
]

recent = collect_recent(entries, window_days=7)
titles = [r["title"] for r in recent]

check("窗口内条目全部入选", {"今天更新", "3天前", "正好7天前"} <= set(titles), str(titles))
check("窗口外条目被排除", "8天前" not in titles and "30天前" not in titles, str(titles))
check("无日期条目被排除", "无日期" not in titles, str(titles))
check("坏日期条目被排除", "坏日期" not in titles, str(titles))
check("secret 条目永远被排除", "secret今天" not in titles and "secret8天前" not in titles, str(titles))
check("按更新时间新→旧排序", [r["updated_at"] for r in recent] == sorted([r["updated_at"] for r in recent], reverse=True), str([r["updated_at"] for r in recent]))
check("自定义窗口天数生效", set(t["title"] for t in collect_recent(entries, window_days=30)) >= {"30天前"}, "")

print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
