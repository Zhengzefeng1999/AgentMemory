#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.2 设备维度回归测试：device/scope 标签、检索标注、--device 过滤、
PRELOAD 过滤、supersedes 防跨设备误收编。

测试条目带 `设备防线测试` 标记，模拟"zhengzefeng 他机条目 vs 本机(lenovo)条目"，
结束后物理删除。
"""
import glob
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
MARK = "设备防线测试"

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
    for sub in ("lessons/patterns", "lessons/failures", "knowledge", "projects"):
        for f in glob.glob(os.path.join(ROOT, "bank", sub, f"*{MARK}*")):
            os.remove(f); n += 1
    return n

print("===== v2.2 设备维度回归测试 =====")
cleanup()

# ── ① 写入自动打 scope + device 标签 ──
rc, out, err = run("add", "--title", f"{MARK}通用经验", "--category", "knowledge",
                   "--body", f"{MARK} 水文排频公式 P=m/(n+1)，与设备无关。", "--auto")
rc2, out2, err2 = run("search", MARK, "--json")
ok = False
try:
    data = json.loads(out2)
    gen = next(d for d in data if "通用经验" in d["title"])
    ok = "scope:global" in gen["tags"] and f"device:lenovo" in gen["tags"]
except Exception:
    pass
check("① 通用经验 → scope:global + device:lenovo", ok, out2[:300])

rc, out, err = run("add", "--title", f"{MARK}环境经验本机Anaconda", "--category", "knowledge",
                   "--body", f"{MARK} 本机 F:\\Anaconda3 缺 JPEG 编码器。", "--auto")
rc2, out2, err2 = run("search", "Anaconda", "--json")
ok = False
try:
    data = json.loads(out2)
    env = next(d for d in data if MARK in d["title"])
    ok = "scope:device" in env["tags"]
except Exception:
    pass
check("① 环境特征正文 → scope:device 自动推断", ok, out2[:300])

# ── ② 他机条目：检索标注 + --device 过滤 ──
# 手工造一条 zhengzefeng 来源的他机条目（直接写文件，模拟同步来的）
fp = os.path.join(ROOT, "bank", "knowledge", "20260101-000000-000-" + MARK + "他机python坑.md")
with open(fp, "w", encoding="utf-8") as f:
    f.write("""---
title: """ + MARK + """他机python坑
tags: [scope:device, device:zhengzefeng]
category: knowledge
confidence: medium
verified_at: 2026-09-30
hits: 0
status: active
source: test
updated_at: 2026-09-30 00:00:00
type: fact
---
zhengzefeng 那台机的 python 环境在 C:\\Users\\32726 有 io 污染问题。
""")
rc, out, err = run("consolidate", "--mode", "auto")  # 索引刷新
rc, out, err = run("search", MARK, "--limit", "20")
check("② 他机条目标注 🖥️", "🖥️他机经验(zhengzengfeng".replace("zhengzengfeng","zhengzefeng") in out or "🖥️他机经验(zhengzefeng)" in out, out[:600])

rc, out, err = run("search", MARK, "--limit", "20", "--device", "local")
check("② --device local 隐藏他机专属", "他机python坑" not in out, out[:400])
rc, out, err = run("search", MARK, "--limit", "20", "--device", "zhengzefeng")
check("② --device zhengzefeng 只看他机", "他机python坑" in out and "通用经验" in out, out[:400])

# ── ③ supersedes 不引导收编他机专属条目 ──
rc, out, err = run("add", "--title", f"{MARK}他机python坑最终结论", "--category", "knowledge",
                   "--body", f"{MARK} 复核后结论不同（模拟跨设备伪冲突场景）。", "--auto")
hint_lines = "".join(l for l in out.splitlines() if "💡" in l)
check("③ supersedes 不建议收编他机条目", "20260101-000000-000" not in hint_lines, hint_lines)

# ── ④ PRELOAD 过滤他机条目 ──
from importlib import util as _ilu
spec = _ilu.spec_from_file_location("bp", os.path.join(ROOT, "scripts", "build_preload.py"))
bp = _ilu.module_from_spec(spec); spec.loader.exec_module(bp)
bp.build_preload(verbose=False)
pre = open(os.path.join(ROOT, "PRELOAD.md"), encoding="utf-8").read()
check("④ PRELOAD 不含他机专属条目", "20260101-000000-000" not in pre, "手写他机条目渗入了 PRELOAD")

# ── ⑤ health 设备指标 ──
rc, out, err = run("health")
check("⑤ health 含当前设备/他机条目数", "当前设备" in out and "他机环境条目" in out, out[:300])

# ── 收尾 ──
n = cleanup()
run("consolidate", "--mode", "auto")
print(f"\n清理测试条目 {n} 个，索引已刷新")
print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
