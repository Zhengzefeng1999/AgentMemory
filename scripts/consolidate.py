#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory consolidate — 记忆整理/淘汰/提炼工具

用法:
  python scripts/consolidate.py --mode auto    # 规则整理（零 LLM，每周跑）
  python scripts/consolidate.py --mode llm     # 规则 + DeepSeek 跨条目提炼
  python scripts/consolidate.py --report       # 仅输出健康报告
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANK_DIR = os.path.join(ROOT, "bank")
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

from memory_tool import (  # noqa: E402
    CATEGORY_DIRS, INDEX_DB, _utf8, all_entry_files, build_frontmatter,
    entry_rel_path, get_conn, init_schema, load_config, now_str, parse_frontmatter,
)

def _load_env():
    """读取 .env 中的 key"""
    env = {}
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

# ---- 规则整理 ----

def collect_entries():
    entries = []
    for fp in all_entry_files():
        with open(fp, encoding="utf-8") as f:
            text = f.read()
        meta, body = parse_frontmatter(text)
        entries.append({"path": fp, "rel": entry_rel_path(fp), "meta": meta, "body": body})
    return entries

def find_duplicates(entries):
    """按 title+tags 合并检测"""
    groups = {}
    for e in entries:
        title = e["meta"].get("title", "").strip().lower()
        tags = ",".join(sorted(e["meta"].get("tags", []))).lower()
        key = (title, tags)
        groups.setdefault(key, []).append(e)
    return {k: v for k, v in groups.items() if len(v) > 1}

def find_stale(entries, months=6):
    """超期无命中的低置信度条目"""
    cutoff = datetime.date.today() - datetime.timedelta(days=months * 30)
    stale = []
    for e in entries:
        meta = e["meta"]
        if meta.get("status") != "active":
            continue
        if int(meta.get("hits", 0)) > 0:
            continue
        v = meta.get("verified_at", "")
        if v:
            try:
                d = datetime.date.fromisoformat(v)
                if d < cutoff and meta.get("confidence") == "low":
                    stale.append(e)
            except ValueError:
                pass
    return stale

def count_patterns(entries):
    """统计 patterns 分类中的模式出现次数（按 tags 主标签）"""
    counter = {}
    for e in entries:
        if e["meta"].get("category") == "patterns":
            for t in e["meta"].get("tags", []):
                counter[t] = counter.get(t, 0) + 1
    return counter

def update_candidates(patterns_counter, threshold=3):
    cand_path = os.path.join(BANK_DIR, "CANDIDATES.md")
    lines = []
    existing = []
    if os.path.exists(cand_path):
        with open(cand_path, encoding="utf-8") as f:
            existing = f.readlines()
    header = ["# Skill 候选清单（CANDIDATES）\n\n",
              "> 由 consolidate.py 从 patterns.md 提炼，或人工登记。\n",
              "> 满 5 条时提醒用户审核，走 skill_manage 升级流程。\n\n",
              "| # | 模式 | 出现次数 | 建议 skill | 状态 |\n",
              "|---|------|---------|-----------|------|\n"]
    for i, (tag, cnt) in enumerate(sorted(patterns_counter.items(), key=lambda x: -x[1]), 1):
        if cnt >= threshold:
            # 检查是否已登记
            exists = any(tag in line for line in existing if "|" in line and "模式" not in line)
            if not exists:
                lines.append(f"| {i} | {tag} | {cnt} | (待定) | 待审核 |\n")
    if lines:
        with open(cand_path, "w", encoding="utf-8") as f:
            f.writelines(header + lines)
        print(f"  CANDIDATES.md 新增 {len(lines)} 条候选")
        return len(lines)
    return 0

def run_auto(verbose=True):
    entries = collect_entries()
    dups = find_duplicates(entries)
    stale = find_stale(entries)
    pats = count_patterns(entries)
    new_cands = update_candidates(pats, load_config()["consolidate"]["pattern_threshold"])

    # 处理 stale：自动归档（改 status）
    archived = 0
    for e in stale:
        meta, body = e["meta"], e["body"]
        meta["status"] = "archived"
        meta["updated_at"] = now_str()
        with open(e["path"], "w", encoding="utf-8") as f:
            f.write(build_frontmatter(meta, body))
        archived += 1

    # 重建索引
    from memory_tool import rebuild_index
    rebuild_index(verbose=False)

    if verbose:
        print("===== consolidate 规则整理结果 =====")
        print(f"  总条目      : {len(entries)}")
        print(f"  疑似重复    : {sum(len(v) for v in dups.values())} 条 / {len(dups)} 组")
        for k, v in dups.items():
            print(f"    [{k[0][:30]}] → {len(v)} 条重复")
        print(f"  过期归档    : {archived} 条")
        print(f"  模式统计    : {len(pats)} 个标签")
        print(f"  新增候选    : {new_cands} 条")
    return {"dups": len(dups), "archived": archived, "candidates": new_cands}

# ---- LLM 提炼 ----

def call_llm(api_key, base_url, model, prompt, max_tokens=2000):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  [LLM 调用失败] {e}")
        return None

def collect_recent(entries, window_days=7):
    """筛选近 window_days 天内更新（updated_at）的非 secret 条目，按更新时间新→旧排序。

    无日期或日期无法解析的条目一律不参与提炼（宁缺毋滥），避免把不确定时效的内容发给 LLM。
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=window_days)
    recent = []
    for e in entries:
        if e["meta"].get("secret"):
            continue
        u = e["meta"].get("updated_at", "")
        try:
            d = datetime.date.fromisoformat(u[:10]) if u else None
        except ValueError:
            d = None
        if d is None or d < cutoff:
            continue  # 超出窗口：不发送给 LLM
        recent.append({"title": e["meta"].get("title", ""), "category": e["meta"].get("category", ""),
                       "body": e["body"][:500], "updated_at": u})
    # 新→旧排序：prompt 有 6000 字截断，优先保留最新条目
    return sorted(recent, key=lambda x: x["updated_at"], reverse=True)


def run_llm():
    cons_cfg = load_config()["consolidate"]
    cfg = cons_cfg["llm"]
    env = _load_env()
    api_key = env.get(cfg["env_key"], "")
    if not api_key or api_key.startswith("sk-在这里"):
        print("  ⚠️ 未配置 LLM key（.env 的 DEEPSEEK_API_KEY），跳过 LLM 提炼")
        return
    # 收集本周新增（updated_at 近 7 天，窗口天数可在 config.json 的 llm_window_days 调整）；
    # secret 条目绝不发送给 LLM
    entries = collect_entries()
    window_days = int(cons_cfg.get("llm_window_days", 7))
    recent = collect_recent(entries, window_days)
    skipped = sum(1 for e in entries if e["meta"].get("secret"))
    if skipped:
        print(f"  [安全] 已跳过 {skipped} 条 secret 条目（不发送给 LLM）")
    if len(recent) < 3:
        print("  条目太少，跳过 LLM 提炼")
        return
    prompt = (
        "你是一个记忆整理助手。以下是一批 Agent 会话记忆条目。请：\n"
        "1) 找出重复或高度重叠的条目；\n"
        "2) 找出相互矛盾的条目；\n"
        "3) 提炼出 1-3 条跨条目的新洞察（方法论级别）。\n"
        "输出格式：重复清单 / 矛盾清单 / 新洞察。用中文。\n\n"
        + json.dumps(recent, ensure_ascii=False, indent=1)[:6000]
    )
    print("  调用 DeepSeek 提炼中...")
    result = call_llm(api_key, cfg["base_url"], cfg["model"], prompt)
    if result:
        stamp = datetime.date.today().isoformat().replace("-", "")
        out = os.path.join(BANK_DIR, "knowledge", f"insights-{stamp}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"---\ntitle: 周度提炼 insights-{stamp}\ntags: [insights, weekly]\ncategory: knowledge\nconfidence: medium\nverified_at: {datetime.date.today().isoformat()}\nstatus: active\n---\n\n# 周度提炼 {stamp}\n\n" + result + "\n")
        print(f"  已写入: {entry_rel_path(out)}")
        from memory_tool import rebuild_index
        rebuild_index(verbose=False)

def run_report():
    entries = collect_entries()
    conn = get_conn()
    init_schema(conn)
    total_hits = conn.execute("SELECT COALESCE(SUM(hits),0) FROM entries").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM entries WHERE status='active'").fetchone()[0]
    conn.close()
    print("===== AgentMemory 记忆健康报告 =====")
    print(f"  总条目     : {len(entries)}")
    print(f"  活跃条目   : {active}")
    print(f"  累计命中   : {total_hits}")
    print(f"  平均命中/条: {total_hits / max(len(entries), 1):.1f}")
    if total_hits == 0:
        print("  ⚠️ 命中率为 0：检索可能未被触发，检查 pi 集成")

def main():
    p = argparse.ArgumentParser(prog="consolidate")
    p.add_argument("--mode", choices=["auto", "llm"], default="auto")
    p.add_argument("--report", action="store_true")
    args = p.parse_args()
    _utf8("")
    if args.report:
        run_report()
    else:
        run_auto()
        if args.mode == "llm":
            run_llm()
    return 0

if __name__ == "__main__":
    sys.exit(main())
