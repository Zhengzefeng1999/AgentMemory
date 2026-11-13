#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory build_preload — PRELOAD 预热索引生成器 v2（Q5：两级预热）

规则:
  1. 所有 pinned 条目（常驻层，永不被挤出）
  2. 近 7 天更新的活跃条目（动态层）
  3. 常用层：hits≥HITS_MIN 的活跃条目，按真实命中次数排序（信号取自 INDEX.db）
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
    BANK_DIR, PRELOAD_PATH, _utf8, all_entry_files, apply_usage_signals, entry_rel_path, is_inbox,
    get_device_name, load_config, parse_frontmatter, rebuild_index, usage_signals,
    _is_foreign_device,
)

MAX_LINES = 200
RECENT_DAYS = 7
# 生成侧字节预算：插件注入上限必须大于它（留余量），这样插件永远不需要截断
BUDGET_BYTES = int(os.environ.get("AGENTMEMORY_PRELOAD_BUDGET_BYTES", "14000"))
# footer 里的条数是最后才知道的，估算用固定占位；留 200B 余量保证真实输出不超预算。
FOOTER_MARGIN = 400
SUMMARY_PINNED = 90   # pinned 行摘要上限（字）；超预算自动逐级压到 60/30/0
SUMMARY_DYN = 45      # 动态行摘要上限（字）
HITS_MIN = 5          # 进「常用层」的最低命中次数
USEFUL_TOP = 25       # 常用层最多条数（预算优先给它）


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
    # 使用信号（hits/last_accessed）只在索引里，必须从这里读，不能信 frontmatter。
    signals = usage_signals(refresh=True)
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
        apply_usage_signals(meta, rel, signals)
        if is_inbox(rel):
            continue   # 收件箱条目是未消化的原始捕获，不进常驻注入（仍可 search）
        # 摘要（首行正文）
        summary = next((l.strip() for l in body.split("\n") if l.strip() and not l.startswith("#")), "")[:240]
        # 天数
        u = str(meta.get("updated_at", ""))[:10]
        days_old = 9999
        try:
            days_old = (today - datetime.date.fromisoformat(u)).days if u else 9999
        except ValueError:
            pass
        meta["_days_old"] = days_old
        # v2.2 设备防线：他机专属环境条目不进 PRELOAD（常驻注入污染代价远大于检索命中）
        tags_str = ",".join(meta.get("tags", []) or [])
        if _is_foreign_device(tags_str):
            continue
        entries.append({"rel": rel, "meta": meta, "summary": summary})

    def _hits(e):
        try:
            return int(e["meta"].get("hits") or 0)
        except (TypeError, ValueError):
            return 0

    pinned = [e for e in entries if e["meta"].get("pinned")]
    pin_rels = {e["rel"] for e in pinned}
    # 常用层：按真实 hits 排序（信号来自 INDEX.db，不是 frontmatter）。
    # 这一层是「老但一直在用」的知识——2026-11-13 修复前它恒为空，PRELOAD 只剩
    # 谁新谁上，高价值的旧经验反而被新噪声挤掉。
    useful = [e for e in entries
              if e["rel"] not in pin_rels and _hits(e) >= HITS_MIN]
    useful.sort(key=lambda e: (-_hits(e), e["meta"]["_days_old"]))
    useful = useful[:USEFUL_TOP]
    useful_rels = {e["rel"] for e in useful}
    recent = [e for e in entries
              if e["rel"] not in pin_rels and e["rel"] not in useful_rels
              and e["meta"]["_days_old"] <= RECENT_DAYS]
    selected = {e["rel"]: e for e in pinned + useful + recent}

    def _fmt(e, cap):
        s = e["summary"]
        if s and cap > 0:
            s = s[:cap] + ("…" if len(s) > cap else "")
            return f"- [{e['rel']}] ({e['meta'].get('type', 'belief')}) {e['meta'].get('title', '')} — {s}"
        return f"- [{e['rel']}] ({e['meta'].get('type', 'belief')}) {e['meta'].get('title', '')}"

    def _compose(pin_l, use_l, dyn_l, foot):
        return (head + (pin_l or ["_（无）_"])
                + ["", f"## 常用（命中≥{HITS_MIN}，按使用频次）"]
                + (use_l or ["_（暂无高频条目）_"])
                + ["", f"## 近期动态（近 {RECENT_DAYS} 天）"]
                + (dyn_l or ["_（近 7 天无新增，可检索全库）_"])
                + ["", foot])

    def _size(lines):
        return len(("\n".join(lines) + "\n").encode("utf-8"))

    head = [
        "# AgentMemory PRELOAD（自动生成，勿手改）",
        f"> 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 规则: pinned 常驻 + 近 {RECENT_DAYS} 天 + 高置信常用 | 全文用: memory_tool get <path>",
        f"> 预算: ≤{BUDGET_BYTES} B（生成侧压字节，插件上限留余量，不做字节级截断）",
        "",
        "## 钉住常驻（pinned）",
    ]
    dyn = [e for e in recent]
    dyn.sort(key=lambda x: (-_entry_weight(x["meta"], x["meta"]["_days_old"]), x["meta"]["_days_old"]))

    # footer 长度固定占位，避免预算估算偏差
    dummy_foot = (f"共 {len(selected)} 条（已展开 0000 条，000 条装不下请检索"
                  f" / 预算 {BUDGET_BYTES} B）。检索全库: memory_tool search \"关键词\" --synthesize")

    # pinned 永不挤出：先给足摘要，超预算就逐级压缩摘要长度，最后连摘要都不要
    pin_lines = []
    for cap in (SUMMARY_PINNED, 60, 30, 0):
        pin_lines = [_fmt(e, cap) for e in pinned]
        if _size(_compose(pin_lines, [], [], dummy_foot)) <= BUDGET_BYTES - FOOTER_MARGIN:
            break
    else:
        print(f"⚠️ 仅 pinned 就 {_size(_compose(pin_lines, [], [], dummy_foot))} B，已超预算 "
              f"{BUDGET_BYTES} B（pinned 不可牺牲，仍全量输出，请调大预算或减少 pinned）")

    # 优先装「常用层」（高价值，先到先得），再用剩余预算装「近期动态」。
    # dropped_use 记常用层被挤掉多少，便于发现预算不足。
    def _fill(items, use_l, dyn_l):
        out, dropped = [], 0
        for e in items:
            line = _fmt(e, SUMMARY_DYN)
            if _size(_compose(pin_lines, use_l + out, dyn_l, dummy_foot)) > BUDGET_BYTES - FOOTER_MARGIN:
                dropped = len(items) - len(out)
                break
            out = out + [line]
        return out, dropped

    use_lines, dropped_use = _fill(useful, [], [])
    dyn_lines, dropped_dyn = _fill(dyn, use_lines, [])

    expanded = len(pin_lines) + len(use_lines) + len(dyn_lines)
    dropped = dropped_use + dropped_dyn
    footer = (f"共 {len(selected)} 条（已展开 {expanded} 条"
              + (f"，{dropped} 条装不下请 search 检索" if dropped else "")
              + f" / 预算 {BUDGET_BYTES} B）。检索全库: memory_tool search \"关键词\" --synthesize")
    lines = _compose(pin_lines, use_lines, dyn_lines, footer)

    # newline="\n"：Windows 默认把 \n 翻成 \r\n，凭空多出 len(lines) 字节，
    # 让真实文件比 _size() 估算更大（预算失效）且 git diff 变脏。
    with open(PRELOAD_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    if verbose:
        print(f"PRELOAD 已生成: {PRELOAD_PATH} ({len(selected)} 条, {len(lines)} 行, {_size(lines)} B)")
        if dropped:
            print(f"  预算内挤掉 {dropped} 条最旧的动态条目（仍可 search 检索）")
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
