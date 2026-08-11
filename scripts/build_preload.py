#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory build_preload — PRELOAD 预热索引生成器 v2（Q5：两级预热）

规则:
  1. 所有 pinned 条目（常驻层，永不被挤出）
  2. 近 7 天更新的活跃条目（动态层）
  3. 高 confidence 且 hits>0 的活跃条目（常用层）
  预算: ≤200 行（参考 Claude Code MEMORY.md 上限），超出按 高confidence→新→旧 截断

输出: PRELOAD.md（ROOT 下，gitignore），agent 会话启动时读取。
"""
import argparse
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

from memory_tool import (  # noqa: E402
    BANK_DIR, PRELOAD_PATH, _utf8, all_entry_files, entry_rel_path,
    load_config, parse_frontmatter, rebuild_index,
)

MAX_LINES = 200
RECENT_DAYS = 7


def _entry_weight(meta, days_old):
    """排序权重：pinned 最高，其次高 confidence，其次更新时间新。"""
    pin = 3 if meta.get("pinned") else 0
    conf = {"high": 2, "medium": 1, "low": 0}.get(meta.get("confidence"), 1)
    fresh = max(0, RECENT_DAYS - days_old) * 0.01
    try:
        hits = min(int(meta.get("hits", 0)), 10) * 0.001
    except (TypeError, ValueError):
        hits = 0
    return pin + conf + fresh + hits


def build_preload(verbose=True):
    entries = []
    today = datetime.date.today()
    for fp in all_entry_files():
        try:
            with open(fp, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        meta, body = parse_frontmatter(text)
        if meta.get("status") != "active":
            continue
        if meta.get("secret"):
            continue  # secret 条目绝不进预热（ADR-0002）
        rel = entry_rel_path(fp)
        # 摘要（首行正文）
        summary = next((l.strip() for l in body.split("\n") if l.strip() and not l.startswith("#")), "")[:60]
        # 天数
        u = str(meta.get("updated_at", ""))[:10]
        days_old = 9999
        try:
            days_old = (today - datetime.date.fromisoformat(u)).days if u else 9999
        except ValueError:
            pass
        meta["_days_old"] = days_old
        entries.append({"rel": rel, "meta": meta, "summary": summary})

    pinned = [e for e in entries if e["meta"].get("pinned")]
    recent = [e for e in entries if not e["meta"].get("pinned") and e["meta"]["_days_old"] <= RECENT_DAYS]
    try:
        useful = [e for e in entries if not e["meta"].get("pinned") and e["meta"].get("confidence") == "high"
                  and int(e["meta"].get("hits", 0)) > 0]
    except (TypeError, ValueError):
        useful = []

    # 已选集合
    selected = {}
    for e in pinned:
        selected[e["rel"]] = e
    for e in sorted(recent + useful, key=lambda x: _entry_weight(x["meta"], x["meta"]["_days_old"]), reverse=True):
        if len(selected) >= MAX_LINES:
            break
        selected.setdefault(e["rel"], e)

    lines = [
        "# AgentMemory PRELOAD（自动生成，勿手改）",
        f"> 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 规则: pinned 常驻 + 近 {RECENT_DAYS} 天 + 高置信常用 | 全文用: memory_tool get <path>",
        "",
        "## 钉住常驻（pinned）",
    ]
    pin_lines = [f"- [{e['rel']}] ({e['meta'].get('type', 'belief')}) {e['meta'].get('title', '')}"
                 + (f" — {e['summary']}" if e['summary'] else "")
                 for e in pinned]
    if pin_lines:
        lines.extend(pin_lines)
    else:
        lines.append("_（无）_")
    lines.append("")
    lines.append(f"## 近期动态（近 {RECENT_DAYS} 天）")
    dyn = [e for e in selected.values() if e["rel"] not in {p["rel"] for p in pinned}]
    dyn.sort(key=lambda x: x["meta"]["_days_old"])
    if dyn:
        for e in dyn:
            lines.append(f"- [{e['rel']}] ({e['meta'].get('type', 'belief')}) {e['meta'].get('title', '')}"
                         + (f" — {e['summary']}" if e['summary'] else ""))
    else:
        lines.append("_（近 7 天无新增，可检索全库）_")
    lines.append("")
    lines.append(f"共 {len(selected)} 条（预算 {MAX_LINES} 行）。检索全库: memory_tool search \"关键词\" --synthesize")

    with open(PRELOAD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if verbose:
        print(f"PRELOAD 已生成: {PRELOAD_PATH} ({len(selected)} 条, {len(lines)} 行)")
    return len(lines)


def main():
    p = argparse.ArgumentParser(prog="build_preload")
    p.add_argument("--rebuild-index", action="store_true", help="先生成索引再预热")
    args = p.parse_args()
    _utf8("")
    if args.rebuild_index:
        rebuild_index(verbose=False)
    build_preload()


if __name__ == "__main__":
    sys.exit(main())
