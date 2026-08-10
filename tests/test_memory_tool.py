#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentMemory 端到端冒烟测试"""
import os
import subprocess
import sys
import tempfile

# Windows GBK 控制台兼容
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "scripts", "memory_tool.py")

PASS = 0
FAIL = 0

def run(*args):
    r = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout, r.stderr

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")

print("===== AgentMemory 冒烟测试 =====")

# 1. health
rc, out, err = run("health")
check("health 命令", rc == 0 and "健康报告" in out, err)

# 2. 临时添加一条
rc, out, err = run("add", "--title", "冒烟测试条目", "--tags", "test,smoke", "--category", "patterns", "--body", "冒烟测试正文")
check("add 命令", rc == 0 and "已添加" in out, out + err)

# 3. 搜索它
rc, out, err = run("search", "冒烟")
check("search 命中", rc == 0 and "冒烟测试条目" in out, out + err)

# 4. get 全文（search --json 拿 path）
import json
rc, out, err = run("search", "冒烟", "--json")
path = None
if rc == 0 and out.strip():
    data = json.loads(out)
    if data:
        path = data[0]["path"]
check("search --json 返回 path", path is not None, out)
if path:
    rc, out, err = run("get", path)
    check("get 全文", rc == 0 and "冒烟测试正文" in out, out + err)

# 5. archive
if path:
    rc, out, err = run("archive", path)
    check("archive", rc == 0 and "已归档" in out, out + err)

# 6. 清理测试条目
import glob
for f in glob.glob(os.path.join(ROOT, "bank", "lessons", "patterns", "*冒烟*")):
    os.remove(f)

print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
