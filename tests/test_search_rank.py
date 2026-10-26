#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索排序回归测试（2026-10-19 重写检索打分后的防回退网）。

覆盖重写前实测到的问题：
  ① 多词不连续查询零结果（"同步 频率" 类）——旧实现 FTS 整串当 AND
  ② 2 字中文词零结果——trigram 分词对 2 字无效
  ③ 自然语言整句零结果
  ④ 覆盖率优先：命中更多查询词的条目应排在最前
  ⑤ 检索命中应计访问次数（此前只有 get 计数，排序用的 hits 恒为 0）

测试条目统一带 `排序回归` 标记，结束后物理删除 + 重建索引，不污染真实库。
"""
import json
import os
import subprocess
import sys

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "scripts", "memory_tool.py")
BANK = os.path.join(ROOT, "bank")
MARK = "排序回归"

PASS = 0
FAIL = 0


def run(*args):
    r = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout, r.stderr


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def search(q, limit=10):
    rc, out, err = run("search", q, "--json", "--limit", str(limit))
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def add(title, body):
    run("capture", "--title", title, "--body", body, "--category", "knowledge",
        "--type", "fact", "--confidence", "high")


def cleanup():
    import glob
    n = 0
    for fp in glob.glob(os.path.join(BANK, "**", "*.md"), recursive=True):
        try:
            with open(fp, encoding="utf-8") as f:
                head = f.read(400)
        except Exception:
            continue
        if MARK in head:
            os.remove(fp)
            n += 1
    run("search", "warm")     # 触发索引重建入口
    from importlib import util
    spec = util.spec_from_file_location("mt", TOOL)
    mt = util.module_from_spec(spec)
    spec.loader.exec_module(mt)
    mt.rebuild_index(verbose=False)
    return n


def main():
    print("===== 检索排序回归 =====")
    add(f"{MARK}河湖断面排频计算规程",
        f"{MARK}标签：本条同时包含『断面』与『排频』两个词，是双词查询的期望第一。")
    add(f"{MARK}断面观测记录整理",
        f"{MARK}标签：本条只含『断面』一个词，用于验证覆盖率排序。")
    add(f"{MARK}完全无关的条目",
        f"{MARK}标签：本条只讲 Python 脚本打包，不含任何水文词。")

    # ① 多词不连续（旧实现零结果）
    r = search("断面 排频")
    check("多词不连续查询有结果", len(r) > 0, f"got {len(r)}")
    check("多词查询：双词覆盖条目排第一",
          bool(r) and MARK in r[0]["title"] and "排频" in r[0]["title"], f"top={r[0]['title'] if r else None}")

    # ② 2 字中文词（trigram 失效，靠 LIKE 兜底）
    r = search("排频")
    check("2 字中文词有结果", any("排频" in it["title"] for it in r), f"got {len(r)}")

    # ③ 自然语言整句
    r = search("河湖断面排频怎么做")
    check("自然语言整句有结果", len(r) > 0, f"got {len(r)}")
    check("整句查询：相关条目进前 3",
          any("排频" in it["title"] for it in r[:3]), f"top3={[x['title'] for x in r[:3]]}")

    # ④ 覆盖率：只含单词的条目应排在双词条目之后
    r = search("断面 排频", limit=10)
    titles = [it["title"] for it in r]
    i_both = next((i for i, t in enumerate(titles) if "排频" in t), None)
    i_one = next((i for i, t in enumerate(titles) if "断面观测" in t), None)
    check("覆盖率排序：双词条目在单词条目之前",
          i_both is not None and (i_one is None or i_both < i_one),
          f"双词@{i_both} 单词@{i_one}")

    # ⑤ 检索命中计访问次数
    before = {it["path"]: it["hits"] for it in search("断面 排频")}
    after = {it["path"]: it["hits"] for it in search("断面 排频")}
    common = [p for p in before if p in after]
    check("检索命中会累加 hits", any(after[p] > before[p] for p in common),
          f"{[(p.split('/')[-1][:16], before[p], after[p]) for p in common[:2]]}")

    # ⑥ 分页与无命中
    check("limit 生效", len(search("断面", limit=1)) <= 1)
    # 词法检索无法证明"库中无此记忆"，因此不强求空结果，但必须判为弱命中
    r = search("zzzz不存在的词zzzz")
    check("无意义长句不会被当成强匹配", not r or r[0]["score"] < 55,
          f"top={r[0]['score'] if r else None}")

    n = cleanup()
    print(f"  清理测试条目 {n} 个，索引已重建")
    print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
