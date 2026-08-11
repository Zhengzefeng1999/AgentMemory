#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory consolidate — 记忆整理/淘汰/提炼工具 v2（每周后台，冷路径）

v2 新增（ADR-0002/0003/0005）:
  - 双信号衰减:    >90天未更新 且 >60天未读取 → confidence 降级 → 归档
  - 敏感补扫②:    近7天新增条目 LLM/规则补扫，命中自动标 secret
  - 类型复查:      LLM 检查近7天条目的 type 推断是否合理（只提示不改）
  - 冲突卡:        LLM 语义冲突检测 → 写 bank/CONFLICTS.md 决策单
  - SKILL 草稿:    CANDIDATES 命中 → LLM 生成 candidates/<name>/SKILL.md 草稿（Q11）

用法:
  python scripts/consolidate.py --mode auto    # 规则整理（零 LLM，每周跑）
  python scripts/consolidate.py --mode llm     # 规则 + DeepSeek 跨条目提炼/补扫/复查/草稿
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
    rebuild_index,
)
from security_rules import scan_text  # noqa: E402

# 双信号参数（ADR-0003）
STALE_UPDATED_DAYS = 90    # 未更新阈值
STALE_READ_DAYS = 60       # 未读取阈值


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


# ---- 规则整理（零 LLM） ----

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


def find_cold(entries, updated_days=STALE_UPDATED_DAYS, read_days=STALE_READ_DAYS):
    """双信号冷条目（ADR-0003）：长期未更新 且 长期未读取。

    返回 [{entry, updated_ago, read_ago}]。
    """
    today = datetime.date.today()
    cold = []
    for e in entries:
        meta = e["meta"]
        if meta.get("status") != "active" or meta.get("pinned"):
            continue  # pinned 永不衰减
        u = str(meta.get("updated_at", ""))[:10]
        la = str(meta.get("last_accessed", ""))[:10]
        updated_ago = read_ago = None
        try:
            if u:
                updated_ago = (today - datetime.date.fromisoformat(u)).days
        except ValueError:
            pass
        try:
            if la:
                read_ago = (today - datetime.date.fromisoformat(la)).days
        except ValueError:
            pass
        # 无读取记录视为从未读取（read_ago 为 None → 认为很旧）
        if updated_ago is not None and updated_ago > updated_days and (read_ago is None or read_ago > read_days):
            cold.append({"entry": e, "updated_ago": updated_ago, "read_ago": read_ago})
    return cold


def apply_decay(cold, verbose=True):
    """双信号衰减：high→medium→low→归档。返回归档数。"""
    archived = 0
    order = {"high": 2, "medium": 1, "low": 0}
    for c in cold:
        e = c["entry"]
        meta, body = e["meta"], e["body"]
        cur = meta.get("confidence", "medium")
        lvl = order.get(cur, 1)
        if lvl == 0:
            meta["status"] = "archived"
            archived += 1
            if verbose:
                print(f"  [归档] {e['rel']} （{c['updated_ago']}天未更新 / 读取{c['read_ago']}）")
        else:
            nxt = {2: "medium", 1: "low"}[lvl]
            meta["confidence"] = nxt
            if verbose:
                print(f"  [降级] {e['rel']} {cur}→{nxt} （{c['updated_ago']}天未更新 / 读取{c['read_ago']}）")
        meta["updated_at"] = now_str()
        with open(e["path"], "w", encoding="utf-8") as f:
            f.write(build_frontmatter(meta, body))
    return archived


def find_stale(entries, months=6):
    """（保留 v1）超期无命中的低置信度条目"""
    cutoff = datetime.date.today() - datetime.timedelta(days=months * 30)
    stale = []
    for e in entries:
        meta = e["meta"]
        if meta.get("status") != "active" or meta.get("pinned"):
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
    """Q11: 命中阈值模式 → CANDIDATES.md 登记。返回新增候选 tag 列表。"""
    cand_path = os.path.join(BANK_DIR, "CANDIDATES.md")
    lines = []
    existing = []
    if os.path.exists(cand_path):
        with open(cand_path, encoding="utf-8") as f:
            existing = f.readlines()
    header = ["# Skill 候选清单（CANDIDATES）\n\n",
              "> 由 consolidate.py 从 patterns.md 提炼，或人工登记。\n",
              "> consolidate --mode llm 可自动生成 SKILL.md 草稿（candidates/ 目录），审阅后启用。\n\n",
              "| # | 模式 | 出现次数 | 建议 skill | 状态 |\n",
              "|---|------|---------|-----------|------|\n"]
    new_tags = []
    for i, (tag, cnt) in enumerate(sorted(patterns_counter.items(), key=lambda x: -x[1]), 1):
        if cnt >= threshold:
            exists = any(tag in line for line in existing if "|" in line and "模式" not in line)
            if not exists:
                lines.append(f"| {i} | {tag} | {cnt} | (待定) | 待审核 |\n")
                new_tags.append(tag)
    if lines:
        with open(cand_path, "w", encoding="utf-8") as f:
            f.writelines(header + lines)
        print(f"  CANDIDATES.md 新增 {len(lines)} 条候选: {new_tags}")
    return new_tags


def run_auto(verbose=True):
    entries = collect_entries()
    dups = find_duplicates(entries)
    cold = find_cold(entries)
    archived_decay = apply_decay(cold, verbose)
    stale = find_stale(entries)
    pats = count_patterns(entries)
    new_cands = update_candidates(pats, load_config()["consolidate"]["pattern_threshold"])

    # v1 stale 归档（保留）
    archived_old = 0
    for e in stale:
        meta, body = e["meta"], e["body"]
        meta["status"] = "archived"
        meta["updated_at"] = now_str()
        with open(e["path"], "w", encoding="utf-8") as f:
            f.write(build_frontmatter(meta, body))
        archived_old += 1

    rebuild_index(verbose=False)

    if verbose:
        print("===== consolidate 规则整理结果 (v2) =====")
        print(f"  总条目      : {len(entries)}")
        print(f"  疑似重复    : {sum(len(v) for v in dups.values())} 条 / {len(dups)} 组")
        for k, v in dups.items():
            print(f"    [{k[0][:30]}] → {len(v)} 条重复")
        print(f"  双信号冷条目: {len(cold)}（衰减: 降级 {len(cold) - archived_decay} / 归档 {archived_decay}）")
        print(f"  v1 过期归档  : {archived_old} 条")
        print(f"  模式统计    : {len(pats)} 个标签")
        print(f"  新增候选    : {new_cands}")
    return {"dups": len(dups), "decayed": len(cold), "archived": archived_decay + archived_old,
            "candidates": len(new_cands)}


# ---- 安全网②：敏感补扫（ADR-0002 防线 2） ----

def run_security_rescan(verbose=True):
    """近 7 天新增条目规则补扫：命中 Mark 级自动标 secret（Block 级标 secret + 提示人工处理）。"""
    entries = collect_entries()
    today = datetime.date.today()
    changed = 0
    for e in entries:
        meta = e["meta"]
        if meta.get("secret"):
            continue
        u = str(meta.get("updated_at", ""))[:10]
        try:
            if u and (today - datetime.date.fromisoformat(u)).days > 7:
                continue
        except ValueError:
            continue
        text = f"{meta.get('title', '')}\n{e['body']}"
        blocked, reason, marked, _ = scan_text(text)
        if blocked or marked:
            meta["secret"] = True
            meta["updated_at"] = now_str()
            with open(e["path"], "w", encoding="utf-8") as f:
                f.write(build_frontmatter(meta, e["body"]))
            changed += 1
            if verbose:
                print(f"  [补扫→secret] {e['rel']}  {reason or marked}")
    if changed:
        rebuild_index(verbose=False)
    if verbose:
        print(f"  敏感补扫：{changed} 条新标 secret")
    return changed


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
    """筛选近 window_days 天内更新（updated_at）的非 secret 条目，按更新时间新→旧排序。"""
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
            continue
        recent.append({"title": e["meta"].get("title", ""), "category": e["meta"].get("category", ""),
                       "type": e["meta"].get("type", ""), "body": e["body"][:500], "updated_at": u})
    return sorted(recent, key=lambda x: x["updated_at"], reverse=True)


def run_llm():
    cons_cfg = load_config()["consolidate"]
    cfg = cons_cfg["llm"]
    env = _load_env()
    api_key = env.get(cfg["env_key"], "")
    if not api_key or api_key.startswith("sk-在这里"):
        print("  ⚠️ 未配置 LLM key（.env 的 DEEPSEEK_API_KEY），跳过 LLM 相关步骤")
        return
    entries = collect_entries()
    window_days = int(cons_cfg.get("llm_window_days", 7))
    recent = collect_recent(entries, window_days)
    skipped = sum(1 for e in entries if e["meta"].get("secret"))
    if skipped:
        print(f"  [安全] 已跳过 {skipped} 条 secret 条目（不发送给 LLM）")
    if len(recent) < 3:
        print("  条目太少，跳过 LLM 提炼")
        return

    # 综合 prompt：提炼 + 语义冲突 + 类型复查（一次调用三合一，省钱省时）
    prompt = (
        "你是记忆库整理助手。以下是近一周的 Agent 会话记忆条目（含 title/category/type/body）。\n"
        "请完成三件事：\n"
        "1) 【提炼】找出重复或高度重叠的条目；提炼 1-3 条跨条目的新洞察（方法论级别）。\n"
        "2) 【语义冲突】找出对同一主题持不同看法的条目对（这是 Q3 冲突检测的冷路径部分）。\n"
        "3) 【类型复查】检查每条 type(fact/belief/preference) 推断是否合理，只列出明显错误的。\n"
        "输出格式：\n"
        "## 重复清单\n...\n## 冲突清单（条目对 + 双方观点）\n...\n## 类型复查\n...\n## 新洞察\n...\n用中文。\n\n"
        + json.dumps(recent, ensure_ascii=False, indent=1)[:6000]
    )
    print("  调用 LLM 提炼/冲突检测/类型复查中...")
    result = call_llm(api_key, cfg["base_url"], cfg["model"], prompt)
    if result:
        stamp = datetime.date.today().isoformat().replace("-", "")
        out = os.path.join(BANK_DIR, "knowledge", f"insights-{stamp}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"---\ntitle: 周度提炼 insights-{stamp}\ntags: [insights, weekly]\ncategory: knowledge\nconfidence: medium\nverified_at: {datetime.date.today().isoformat()}\nstatus: active\n---\n\n# 周度提炼 {stamp}\n\n" + result + "\n")
        print(f"  已写入: {entry_rel_path(out)}")
        # 冲突清单 → CONFLICTS.md 决策单（Q3：用户拍板）
        if "冲突" in result:
            conflicts_path = os.path.join(BANK_DIR, "CONFLICTS.md")
            with open(conflicts_path, "a", encoding="utf-8") as f:
                f.write(f"\n## {stamp} 冲突清单（待你决策）\n\n")
                # 只取冲突段
                seg = result.split("## 冲突清单")[-1].split("##")[0] if "## 冲突清单" in result else result[:800]
                f.write(seg.strip() + "\n")
            print(f"  冲突清单已追加: {entry_rel_path(conflicts_path)}（请审阅决策）")
        rebuild_index(verbose=False)

    # Q11: CANDIDATES → SKILL.md 草稿
    gen_drafts(api_key, cfg["base_url"], cfg["model"])


def gen_drafts(api_key, base_url, model, verbose=True):
    """Q11: CANDIDATES.md 中"待审核"的模式 → LLM 生成 candidates/<name>/SKILL.md 草稿。"""
    cand_path = os.path.join(BANK_DIR, "CANDIDATES.md")
    if not os.path.exists(cand_path):
        return
    # 收集 patterns 条目
    entries = collect_entries()
    patterns = [e for e in entries if e["meta"].get("category") == "patterns"]
    if not patterns:
        return
    candidates_dir = os.path.join(BANK_DIR, "candidates")
    os.makedirs(candidates_dir, exist_ok=True)
    # 从 CANDIDATES.md 提取已登记模式 tag
    tags = []
    with open(cand_path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\| \d+ \| ([^|]+) \|", line)
            if m:
                tags.append(m.group(1).strip())
    if not tags:
        return
    # 每个 tag 找相关 patterns 条目
    for tag in tags[:3]:  # 每轮最多 3 个，控制成本
        related = [e for e in patterns if tag in e["meta"].get("tags", [])]
        if not related:
            continue
        name = re.sub(r"[^\w\u4e00-\u9fff-]", "-", tag)
        draft_dir = os.path.join(candidates_dir, name)
        draft_path = os.path.join(draft_dir, "SKILL.md")
        if os.path.exists(draft_path):
            continue  # 已有草稿
        brief = [{"title": e["meta"].get("title", ""), "body": e["body"][:400]} for e in related[:5]]
        prompt = (
            f"基于以下 {len(related)} 条模式记忆，为模式「{tag}」生成一份 SKILL.md 草稿。\n"
            "结构要求：## When to Use（何时使用）、## Procedure（步骤，编号列表）、## Pitfalls（常见坑）、## Verification（验证方法）。\n"
            "用中文。只输出 SKILL.md 正文，不要额外解释。\n\n"
            + json.dumps(brief, ensure_ascii=False, indent=1)[:4000]
        )
        draft = call_llm(api_key, base_url, model, prompt, max_tokens=1500)
        if draft:
            os.makedirs(draft_dir, exist_ok=True)
            with open(draft_path, "w", encoding="utf-8") as f:
                f.write(f"---\nname: {name}\ndescription: {tag} 模式自动化\n---\n\n" + draft + "\n")
            if verbose:
                print(f"  [草稿] {entry_rel_path(draft_path)}（审阅后可启用）")


def run_report():
    entries = collect_entries()
    conn = get_conn()
    init_schema(conn)
    total_hits = conn.execute("SELECT COALESCE(SUM(hits),0) FROM entries").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM entries WHERE status='active'").fetchone()[0]
    pinned = conn.execute("SELECT COUNT(*) FROM entries WHERE pinned=1").fetchone()[0]
    secret = conn.execute("SELECT COUNT(*) FROM entries WHERE secret=1").fetchone()[0]
    cold = find_cold(entries)
    conn.close()
    print("===== AgentMemory 记忆健康报告 (v2) =====")
    print(f"  总条目     : {len(entries)}")
    print(f"  活跃条目   : {active}")
    print(f"  钉住常驻   : {pinned} | 敏感标记 {secret}")
    print(f"  累计命中   : {total_hits}")
    print(f"  平均命中/条: {total_hits / max(len(entries), 1):.1f}")
    print(f"  双信号冷条目: {len(cold)}（建议 consolidate --mode auto 衰减）")
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
            run_security_rescan()
            run_llm()
    return 0


if __name__ == "__main__":
    sys.exit(main())
