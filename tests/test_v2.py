#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentMemory v2 回归测试：极简 add / capture / 类型推断 / 安全网 / 生命周期 / PRELOAD"""
import datetime
import glob
import json
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
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

from memory_tool import all_entry_files, parse_frontmatter, entry_rel_path  # noqa: E402
from infer import infer_type, infer_category  # noqa: E402
from security_rules import scan_text  # noqa: E402
from build_preload import build_preload  # noqa: E402
from consolidate import find_cold, collect_entries  # noqa: E402

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

def write_tmp_entry(meta_extra, body):
    """写临时条目到 patterns 目录（测试后清理），返回 rel path"""
    from memory_tool import build_frontmatter, now_str
    d = os.path.join(ROOT, "bank", "lessons", "patterns")
    os.makedirs(d, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    meta = {"title": f"v2test-{stamp}", "tags": ["test"], "category": "patterns",
            "confidence": "medium", "verified_at": datetime.date.today().isoformat(),
            "hits": 0, "status": "active", "source": "unittest", "updated_at": now_str(),
            "conflicts": [], "type": "belief", "pinned": False,
            "last_accessed": "", "superseded_by": ""}
    meta.update(meta_extra)
    fp = os.path.join(d, f"{stamp}-{meta['title']}.md")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(build_frontmatter(meta, body))
    return entry_rel_path(fp)

def cleanup(pattern="*v2test*"):
    for f in glob.glob(os.path.join(ROOT, "bank", "lessons", "patterns", pattern)):
        os.remove(f)

print("===== AgentMemory v2 回归测试 =====")

# 1. infer_type
check("infer: 公式→fact", infer_type("河网密度=水系总长/流域面积，规范公式", "")[0] == "fact")
check("infer: 规范号→fact", infer_type("GB 50201-2014 防洪标准", "")[0] == "fact")
check("infer: 我习惯→preference", infer_type("以后报告统一用这个模板", "")[0] == "preference")
check("infer: 我认为→belief", infer_type("我认为这个断面设计不合理", "")[0] == "belief")
check("infer: 踩坑分类→belief", infer_type("踩坑记录", "Excel 乱码", "failures")[0] == "belief")
check("infer: category 推断", infer_category("水位基面换算", "根据规范做基面换算") == "knowledge")

# 2. security_rules
# 运行时拼接假密钥，避免自身命中 pre-commit 敏感扫描（真实测试数据形状）
FAKE_SK = "sk-" + "abc123456789012345678901234567890"
b, br, m, mr = scan_text(f"我的 key 是 {FAKE_SK}")
check("安全: Block sk- 密钥", b == "OpenAI/Anthropic sk- 密钥", br)
b2, _, m2, _ = scan_text("内网地址 192.168.1.1 用于连接数据库")
check("安全: Mark 内网IP", b2 is None and "内网 IPv4" in m2, str(m2))
b3, _, m3, _ = scan_text("正常记忆内容：水位基面换算方法")
check("安全: 正常内容不误伤", b3 is None and not m3)

# 3. find_cold 双信号（构造冷条目：90 天前更新、无读取）
old = (datetime.date.today() - datetime.timedelta(days=120)).isoformat()
rel = write_tmp_entry({"updated_at": old, "last_accessed": "", "hits": 0}, "旧条目正文")
entries = collect_entries()
cold = find_cold(entries)
check("双信号: 120天未更新未读取→冷", any(e["entry"]["rel"] == rel for e in cold), rel)
# 构造读取过的冷条目 → 不冷
rel2 = write_tmp_entry({"updated_at": old, "last_accessed": datetime.date.today().isoformat(), "hits": 5}, "被读取的旧条目")
cold2 = find_cold(collect_entries())
check("双信号: 有读取记录→不冷", not any(e["entry"]["rel"] == rel2 for e in cold2), rel2)

# 4. PRELOAD：pinned 条目必在
rel3 = write_tmp_entry({"pinned": True, "updated_at": old, "hits": 0}, "pinned 旧条目")
build_preload(verbose=False)
preload_txt = open(os.path.join(ROOT, "PRELOAD.md"), encoding="utf-8").read()
check("PRELOAD: pinned 常驻", rel3 in preload_txt, preload_txt[:200])
check("PRELOAD: 行数预算", len(preload_txt.splitlines()) <= 210)

# 5. frontmatter 往返（>12 行 frontmatter 不损坏 —— 回归 v2 关键 bug）
rel4 = write_tmp_entry({"type": "fact", "pinned": True, "last_accessed": "2026-08-11 10:00:00",
                        "superseded_by": "x.md", "secret": False, "conflicts": ["a.md"]}, "多字段正文")
with open(os.path.join(ROOT, "bank", rel4), encoding="utf-8") as f:
    meta, body = parse_frontmatter(f.read())
check("frontmatter: 15+ 字段完整往返", meta.get("title", "").startswith("v2test") and meta.get("type") == "fact"
      and meta.get("pinned") is True and meta.get("superseded_by") == "x.md", str(meta)[:200])

# 6. 幂等重建索引
from memory_tool import rebuild_index
n = rebuild_index(verbose=False)
check("索引重建", n >= 15, str(n))

cleanup()
rebuild_index(verbose=False)  # cleanup 只删文件；重建索引清除孤儿行，避免污染生产索引（health/pinned 计数）
print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
