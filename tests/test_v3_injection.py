#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.3 注入层 / 使用信号 / 收件箱 回归测试（对应 ADR-0008）。

覆盖三块 2026-11-13 的改动：
  ① 使用信号（hits / last_accessed）必须从 INDEX.db 读，而不是 frontmatter（死值）
  ② 常驻摘要 PRELOAD_SUMMARY.md 的 schema / 预算 / 指针可解析
  ③ 收件箱 bank/inbox/：capture 落 inbox、不进注入层、consolidate 提级、近同名留观

所有测试都在**临时 bank** 里跑（history：v2 测试曾往真实 bank 写垃圾条目）。
"""
import datetime
import os
import shutil
import sys
import tempfile

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

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


def make_entry(bank, rel, title, body, hits=0, last_accessed="", status="active",
               extra=None, category="knowledge"):
    """直接写一个条目文件（不走 capture，避免推断干扰）。

    category 必须显式给：inbox 条目的「落点目录」是 inbox/，但它的 category 是采集时
    推断出来的（knowledge/lessons/...），提级时靠后者决定搬去哪个正式目录。
    """
    fp = os.path.join(bank, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    meta = [
        "---",
        f"title: {title}",
        "tags: [scope:global]",
        f"category: {category}",
        "confidence: medium",
        "verified_at: 2026-01-01",
        f"hits: {hits}",
        f"status: {status}",
        "source: test",
        f"updated_at: {datetime.date.today().isoformat()} 00:00:00",
        "conflicts: []",
        "type: fact",
        "pinned: false",
        "secret: false",
        f"last_accessed: {last_accessed}",
        "superseded_by: ",
    ]
    if extra:
        meta.extend(extra)
    meta.append("---")
    with open(fp, "w", encoding="utf-8") as f:
        f.write("\n".join(meta) + "\n" + body + "\n")
    return fp


def main():
    tmp = tempfile.mkdtemp(prefix="agentmem-inject-")
    bank = os.path.join(tmp, "bank")
    os.makedirs(bank)
    try:
        import memory_tool as mt
        import consolidate as C
        import build_preload as BP
        import build_summary as BS

        # 全部改道到临时 bank（模块全局，patch 后所有函数生效）
        mt.BANK_DIR = bank
        mt.INDEX_DB = os.path.join(bank, "INDEX.db")
        mt.INBOX_DIR = os.path.join(bank, mt.INBOX_SUB)
        C.BANK_DIR = bank
        BP.PRELOAD_PATH = os.path.join(tmp, "PRELOAD.md")
        BS.SUMMARY_PATH = os.path.join(tmp, "PRELOAD_SUMMARY.md")

        # 造数据：两条正式 + 一条 inbox
        make_entry(bank, "knowledge/20260101-000000-001-hot.md",
                   "高频老知识：Excel 打开 CSV 要用 utf-8-sig",
                   "Excel 打开 CSV 中文乱码时用 utf-8-sig 编码写入。")
        make_entry(bank, "knowledge/20260102-000000-002-cold.md",
                   "冷知识一条", "这条从未被检索过。")
        make_entry(bank, "inbox/20260103-000000-003-raw.md",
                   "收件箱里的原始捕获", "尚未提级的捕获内容。")

        # 索引 + 模拟「被检索过 7 次」：只更新索引，不回写 frontmatter
        mt.rebuild_index(verbose=False)
        conn = mt.get_conn()
        conn.execute("UPDATE entries SET hits=7, last_accessed=? WHERE path=?",
                     (datetime.date.today().isoformat() + " 10:00:00",
                      "knowledge/20260101-000000-001-hot.md"))
        conn.commit()
        conn.close()
        mt.usage_signals(refresh=True)

        print("① 使用信号从索引读")
        sig = mt.usage_signals(refresh=True)
        check("索引里有三条", len(sig) == 3, f"实际 {len(sig)}")
        check("hits 来自索引（7）", sig.get("knowledge/20260101-000000-001-hot.md", (0,))[0] == 7)
        meta, _b = mt.parse_frontmatter(open(os.path.join(
            bank, "knowledge", "20260101-000000-001-hot.md"), encoding="utf-8").read())
        check("frontmatter 里仍是死值（0）", int(meta.get("hits") or 0) == 0)
        mt.apply_usage_signals(meta, "knowledge/20260101-000000-001-hot.md", sig)
        check("apply_usage_signals 覆盖为 7", int(meta.get("hits")) == 7)
        check("last_accessed 一并带入", bool(meta.get("last_accessed")))

        print("② find_cold 用 config 阈值，且信号真的注入")
        es = C.collect_entries()
        hot = [e for e in es if e["rel"].endswith("001-hot.md")][0]
        check("collect_entries 注入 hits", int(hot["meta"].get("hits")) == 7)
        # 冷条目：从未读取 + 更新时间超阈值 → 应被判冷
        old = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        cold_fp = os.path.join(bank, "knowledge", "20260102-000000-002-cold.md")
        txt = open(cold_fp, encoding="utf-8").read().replace(
            str(datetime.date.today()), old)
        open(cold_fp, "w", encoding="utf-8").write(txt)
        mt.rebuild_index(verbose=False)
        mt.usage_signals(refresh=True)
        es = C.collect_entries()
        cold = C.find_cold(es)
        cold_rels = {c["entry"]["rel"] for c in cold}
        check("超期未读条目被判冷", any("002-cold" in r for r in cold_rels),
              f"cold={sorted(cold_rels)}")
        check("刚被读过的条目不判冷", not any("001-hot" in r for r in cold_rels))
        check("阈值来自 config", C.stale_thresholds() == (90, 60))

        print("③ 注入层：PRELOAD 有常用层、排除 inbox")
        BP.build_preload(verbose=False)
        pre = open(BP.PRELOAD_PATH, encoding="utf-8").read()
        check("含「常用」小节", "## 常用" in pre)
        check("hits≥5 的条目进常用层", "001-hot" in pre)
        check("inbox 条目不进 PRELOAD", "inbox/" not in pre)

        print("④ 注入层：摘要 schema / 预算 / 指针")
        text = BS.build_summary(verbose=False, offline=True)
        check("摘要非空", bool(text))
        check("首行是 v1", text.split("\n", 1)[0].strip() == "v1")
        for sec in ("## User Profile", "## User preferences",
                    "## General Tips", "## What's in Memory"):
            check(f"含小节 {sec}", sec in text)
        check("摘要不超预算", len(text.encode("utf-8")) <= BS.SUMMARY_BUDGET)
        check("inbox 条目不进摘要", "inbox/" not in text)

        print("⑤ 指针补全：半截路径补全、歧义不猜、不存在不动")
        full = "knowledge/20260101-000000-001-hot.md"
        got = BS._resolve_pointers("见（knowledge/20260101-000000-001）与（knowledge/nope）")
        check("半截路径被补全", full in got, got)
        check("不存在的前缀保持原样", "knowledge/nope" in got)
        make_entry(bank, "knowledge/20260101-000000-001-hot2.md", "另一个同前缀", "x")
        mt.rebuild_index(verbose=False)
        got2 = BS._resolve_pointers("见（knowledge/20260101-000000-001）")
        check("前缀歧义时不乱猜", "）" in got2 and got2.count(".md") == 0, got2)

        print("⑥ 收件箱：提级 / 近同名留观")
        check("is_inbox 判定", mt.is_inbox("inbox/a.md") and not mt.is_inbox("knowledge/b.md"))
        # 提级两条互不冲突的（前面还留了一条 003-raw 在 inbox）
        make_entry(bank, "inbox/20260104-000000-004-promote.md",
                   "完全独立的新知识 Alpha", "与库里任何标题都不同。")
        mt.rebuild_index(verbose=False)
        es = C.collect_entries()
        n_inbox = len([e for e in es if mt.is_inbox(e["rel"])])
        st = C.promote_inbox(es, verbose=False)
        check("互不冲突的 inbox 条目全部提级", st["promoted"] == n_inbox,
              f"{st} / inbox 原有 {n_inbox}")
        check("提级后落到 knowledge/", os.path.exists(os.path.join(
            bank, "knowledge", "20260104-000000-004-promote.md")))
        check("提级后 inbox 清空该条", not os.path.exists(os.path.join(
            bank, "inbox", "20260104-000000-004-promote.md")))
        # 近同名 → 留观不提级
        make_entry(bank, "inbox/20260105-000000-005-dup.md",
                   "完全独立的新知识 Alpha", "同名第二版。")
        mt.rebuild_index(verbose=False)
        es = C.collect_entries()
        st2 = C.promote_inbox(es, verbose=False)
        check("近同名被留观", st2["conflict"] == 1, st2)
        check("近同名仍留在 inbox", os.path.exists(os.path.join(bank, "inbox")))
        inbox_files = [f for f in os.listdir(os.path.join(bank, "inbox"))]
        check("留观条目没被移除", any("005-dup" in f for f in inbox_files), inbox_files)

        print("⑦ 摘要重建判定（定时任务 --if-stale）")
        import time as _time
        BS.write_summary(text)
        check("刚写完不重建", BS.is_stale() is False)
        _time.sleep(0.02)
        make_entry(bank, "knowledge/20260106-000000-006-newer.md", "更新一条", "新条目比摘要新。")
        check("有新条目 → 需要重建", BS.is_stale() is True)
        BS.write_summary(text)
        check("重建后回到不需要", BS.is_stale() is False)
        # 索引 mtime 变动不该触发重建（search 每次都改 INDEX.db）
        _time.sleep(0.02)
        mt.usage_signals(refresh=True)
        conn = mt.get_conn()
        conn.execute("UPDATE entries SET hits=hits+1 WHERE path=?",
                     ("knowledge/20260106-000000-006-newer.md",))
        conn.commit()
        conn.close()
        check("仅 hits 变动不触发重建", BS.is_stale() is False)
        # LLM 不可用时保留旧摘要，不用低质规则版覆盖
        orig = BS._call_llm
        BS._call_llm = lambda _p: None
        try:
            got = BS.build_summary(verbose=False, offline=False)
            check("LLM 不可用时返回 None（保留旧文件）", got is None, got)
            check("旧摘要仍然有效", BS._existing_summary_ok() is True)
        finally:
            BS._call_llm = orig

        print("⑧ stage 字段：curated 直写不落 inbox")
        m_cur = mt._build_meta("直进正式库", ["scope:global"], "knowledge",
                               "medium", "test", False, "fact", False)
        m_cur["stage"] = "curated"
        m_in = mt._build_meta("走收件箱", ["scope:global"], "knowledge",
                              "medium", "test", False, "fact", False)
        m_in["stage"] = mt.INBOX_SUB
        rel_cur = mt._write_entry(m_cur, "正文")
        rel_in = mt._write_entry(m_in, "正文")
        check("curated 落正式目录", rel_cur.startswith("knowledge/"), rel_cur)
        check("inbox 落收件箱", rel_in.startswith("inbox/"), rel_in)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
