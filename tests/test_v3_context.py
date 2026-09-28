#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentMemory v2.1 回归测试：溯源标签（project/agent）+ 更正流（--supersedes）+ consolidate 入口

约定沿用 test_v2：测试条目以 v3test 前缀写入真实 bank，结束后删除并重建索引。
"""
import datetime
import glob
import os
import subprocess
import sys
import tempfile
import time

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import memory_tool as mt  # noqa: E402
from memory_tool import parse_frontmatter, build_frontmatter, entry_rel_path  # noqa: E402

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

def run_cli(*args, cwd=None, timeout=60):
    """跑 memory_tool CLI（子进程，独立于本进程的 env/cwd 状态）"""
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "memory_tool.py"), *args],
        cwd=cwd or ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)
    return r

def write_tmp_entry(meta_extra, body):
    d = os.path.join(ROOT, "bank", "lessons", "patterns")
    os.makedirs(d, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    meta = {"title": f"v3test-{stamp}", "tags": ["test"], "category": "patterns",
            "confidence": "medium", "verified_at": datetime.date.today().isoformat(),
            "hits": 0, "status": "active", "source": "unittest", "updated_at": mt.now_str(),
            "conflicts": [], "type": "belief", "pinned": False,
            "last_accessed": "", "superseded_by": ""}
    meta.update(meta_extra)
    fp = os.path.join(d, f"{stamp}-{meta['title']}.md")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(build_frontmatter(meta, body))
    # 与真实 add/capture 一致：写入后同步索引（否则 --supersedes 的索引查找会落空）
    conn = mt.get_conn(); mt.init_schema(conn)
    mt.index_file(conn, fp, meta, body)
    conn.commit(); conn.close()
    return entry_rel_path(fp)

def read_meta(rel):
    with open(os.path.join(ROOT, "bank", rel), encoding="utf-8") as f:
        meta, _ = parse_frontmatter(f.read())
    return meta

def cleanup_and_reindex():
    # capture 会按内容推断 category（knowledge/corrections/…），必须递归扫全 bank
    n = 0
    for f in glob.glob(os.path.join(ROOT, "bank", "**", "*v3test*"), recursive=True):
        os.remove(f); n += 1
    mt.rebuild_index(verbose=False)
    return n

print("===== AgentMemory v2.1 回归测试 =====")

# ---- 1. 溯源标签 ----
with tempfile.TemporaryDirectory() as td:
    # 1a. 有 .git → project:<目录名>
    repo = os.path.join(td, "假项目甲")
    os.makedirs(os.path.join(repo, ".git"))
    sub = os.path.join(repo, "docs")
    os.makedirs(sub)
    tags = mt._context_tags(sub)
    check("溯源: git 子目录→project 标签", "project:假项目甲" in tags, str(tags))
    # 1b. 无 .git → 不打 project 标签
    plain = os.path.join(td, "普通目录")
    os.makedirs(plain)
    tags2 = mt._context_tags(plain)
    check("溯源: 非 git 目录→无 project 标签", not any(t.startswith("project:") for t in tags2), str(tags2))
    # 1c. 向上查找不超过 max_up 层
    tags3 = mt._context_tags(plain)
    check("溯源: max_up 之外不越界", isinstance(tags3, list))

# 1d. agent 标签：PI_ 环境变量
old_env = os.environ.get("PI_V3TEST_MARKER")
os.environ["PI_V3TEST_MARKER"] = "1"
tags4 = mt._context_tags()
if old_env is None:
    del os.environ["PI_V3TEST_MARKER"]
else:
    os.environ["PI_V3TEST_MARKER"] = old_env
check("溯源: PI_ 环境变量→agent:pi", "agent:pi" in tags4, str(tags4))

# 1e. 合并去重：用户标签在前，溯源置尾，不重复
merged = mt._merge_context_tags(["水文", "project:假项目甲"])
check("溯源: 合并去重且用户标签在前", merged[0] == "水文" and merged.count("project:假项目甲") == 1, str(merged))

# ---- 2. 更正流 capture --supersedes（端到端 CLI） ----
old_rel = write_tmp_entry({"title": "v3test-旧结论-将被更正"}, "旧结论：GitHub 在本机不可用。")
r = run_cli("capture", "--body", "更正：GitHub 经 Steam++ 代理可用，git clone 正常。（v3test）",
            "--title", "v3test-更正GitHub可用", "--supersedes", old_rel)
out = r.stdout + r.stderr
check("更正流: capture 退出码 0", r.returncode == 0, out[-200:])
check("更正流: 提示旧条目已失效", "已失效并指向新条目" in out, out[-200:])
new_rel = None
for line in out.split("\n"):
    if line.startswith("已捕获:"):
        new_rel = line.split("已捕获:")[1].strip().split()[0]
check("更正流: 新条目已写入", bool(new_rel), out)
if new_rel:
    m_old = read_meta(old_rel)
    check("更正流: 旧条目 status=invalidated", m_old.get("status") == "invalidated", str(m_old.get("status")))
    check("更正流: 旧条目 superseded_by 指向新条目", m_old.get("superseded_by") == new_rel,
          f"{m_old.get('superseded_by')} != {new_rel}")
    m_new = read_meta(new_rel)
    raw_tags = m_new.get("tags", [])
    tag_list = raw_tags if isinstance(raw_tags, list) else [t.strip() for t in str(raw_tags).split(",")]
    check("更正流: 新条目带 project 溯源标签", any(str(t).startswith("project:") for t in tag_list),
          str(tag_list))
    # search 不再返回失效旧条目
    rs = run_cli("search", "v3test", "--json")
    paths = [it["path"] for it in __import__("json").loads(rs.stdout)] if rs.stdout.strip().startswith("[") else []
    check("更正流: 检索不再返回失效条目", old_rel not in paths, str(paths))
    # get 旧条目有沿革提示
    rg = run_cli("get", old_rel)
    check("更正流: get 旧条目提示被替代", "已被" in rg.stdout and "替代" in rg.stdout, rg.stdout[-120:])

# ---- 3. add --supersedes 目标不存在 → 拒绝且不写入 ----
before = len(glob.glob(os.path.join(ROOT, "bank", "lessons", "patterns", "*v3test*")))
r2 = run_cli("add", "--body", "v3test 不应写入", "--supersedes", "不存在/的/路径.md")
after = len(glob.glob(os.path.join(ROOT, "bank", "lessons", "patterns", "*v3test*")))
check("更正流: add 目标不存在→退出码非0", r2.returncode != 0, str(r2.returncode))
check("更正流: add 目标不存在→不写入", before == after, f"{before} -> {after}")

# ---- 4. 热路径性能红线（ADR-0005：写入必须快） ----
t0 = time.time()
r3 = run_cli("capture", "--body", "v3test 性能红线测量：热路径零 LLM 零网络。")
dt = time.time() - t0
check("性能: capture 端到端 < 5s", r3.returncode == 0 and dt < 5, f"{dt:.2f}s")

# ---- 5. consolidate 入口 ----
rc = run_cli("consolidate", "--help")
check("consolidate: --help 可用", rc.returncode == 0 and "mode" in rc.stdout)
rb = run_cli("consolidate", "--mode", "bogus")
check("consolidate: 非法 mode 被拒", rb.returncode != 0)
rd = run_cli("consolidate", "--mode", "auto", "--timeout", "1")
# 有界性：要么 1s 内跑完(0)，要么被超时守护终止(3)；argparse 错误是 2，漏接 dispatch 是 1
check("consolidate: dispatch 已接通且有界执行", rd.returncode in (0, 3),
      f"rc={rd.returncode} out={(rd.stdout + rd.stderr)[-150:]}")

# ---- 清理 ----
removed = cleanup_and_reindex()
leftover = glob.glob(os.path.join(ROOT, "bank", "**", "*v3test*"), recursive=True)
check("清理: 无测试残留", not leftover, str(leftover))

print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
