#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.2 一致性防线回归测试。

用 2026-09-30 治理时发现的三类真实案例做端到端验证：
  ① 同名双写（双轮批量导入漏网）→ find_local_conflicts 命中
  ② 版本演进链（拦河坝2 式 29 分钟 4 版"最终"）→ suggest_supersedes 引导
  ③ 正文完全重复 → bodyhash 命中
  ④ 检索端多版本标注 / health 治理指标 / consolidate 三路查重

测试条目统一带 `v22回归` 标记，结束后物理删除，不污染真实库。
"""
import glob
import json
import os
import re
import subprocess
import sys

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "scripts", "memory_tool.py")
MARK = "回归标记同查重"

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

def cleanup():
    n = 0
    for sub in ("lessons/patterns", "lessons/failures", "lessons/corrections", "knowledge", "projects"):
        for f in glob.glob(os.path.join(ROOT, "bank", sub, f"*{MARK}*")):
            os.remove(f); n += 1
    return n

print("===== v2.2 一致性防线回归测试 =====")
cleanup()  # 幂等

# ── ① 同名双写检测（v2.1 bug：完全同名被 nt != ntitle 排除） ──
rc, out, err = run("add", "--title", f"{MARK}同名条目甲", "--category", "patterns",
                   "--body", f"{MARK} 第一条正文，内容独立。", "--auto")
check("① 基线写入", rc == 0 and "已添加" in out, out + err)
rc, out, err = run("add", "--title", f"{MARK}同名条目甲", "--category", "patterns",
                   "--body", f"{MARK} 第二条正文，完全不同的内容但同名。", "--auto")
check("① 同名双写被检出", "可能重复" in out, out)

# ── ② 版本演进链引导（拦河坝2式） ──
rc, out, err = run("add", "--title", f"{MARK}某坝计算中间结论", "--category", "patterns",
                   "--body", f"{MARK} 中间版正文。", "--auto")
check("② 演进链基线", rc == 0)
rc, out, err = run("add", "--title", f"{MARK}某坝计算最终定稿规范迭代法", "--category", "patterns",
                   "--body", f"{MARK} 最终版正文，另一套内容。", "--auto")
check("② 演进链触发 supersedes 引导", "💡" in out and "--supersedes" in out, out)

# ── ③ 正文指纹完全重复 ──
BODY = f"{MARK} 完全相同的一段正文内容，用于指纹查重验证 1234567890。"
run("add", "--title", f"{MARK}指纹条目一", "--category", "knowledge", "--body", BODY, "--auto")
rc, out, err = run("add", "--title", f"{MARK}指纹条目二不同名", "--category", "knowledge", "--body", BODY, "--auto")
check("③ 正文指纹命中", "正文指纹与已有条目完全相同" in out, out)

# ── ④ 检索端多版本标注 ──
rc, out, err = run("search", MARK)
check("④ search 检出同主题多版本", "同主题" in out and ("✅最新" in out or "🕘旧版" in out), out[:500])
rc, out, err = run("search", MARK, "--json")
ok = False
try:
    data = json.loads(out)
    ok = any(d.get("cluster") for d in data)
except Exception:
    pass
check("④ search --json 带 cluster 字段", ok, out[:300])

# ── ⑤ health 治理指标 ──
rc, out, err = run("health")
check("⑤ health 含同主题多版本", "同主题多版本" in out, out[:300])
check("⑤ health 含待复核冲突", "待复核冲突" in out, out[:300])

# ── ⑥ consolidate 三路查重（子进程隔离） ──
rc, out, err = run("consolidate", "--mode", "auto")
check("⑥ consolidate auto 成功", rc == 0 and "规则整理结果" in out, (out + err)[:400])
cf = open(os.path.join(ROOT, "bank", "CONFLICTS.md"), encoding="utf-8").read()
check("⑥ CONFLICTS.md 已追加规则清单", MARK in cf, cf[-500:])

# ── 收尾清理 ──
n = cleanup()
rc, out, err = run("consolidate", "--mode", "auto")  # 重建索引移除已删条目
print(f"\n清理测试条目 {n} 个，索引已刷新")

print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
