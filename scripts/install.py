#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory 自动安装器 — 面向 AI Agent 的一键部署

用法（AI Agent 执行）:
  python install.py                       # 就地安装（解压目录即目标）
  python install.py --target D:/AgentMemory   # 安装到指定目录（自动迁移文件）
  python install.py --agent pi            # 指定 agent 类型（pi/claude/codebuddy）
  python install.py --check               # 只做环境检查，不安装

流程: 环境检查 → 就位 → git init → 钩子 → 索引 → 自测 → 注册 SKILL → 报告
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
MEMORY_ROOT = os.path.dirname(ROOT) if os.path.basename(ROOT) == "scripts" else ROOT

AGENT_SKILL_DIRS = {
    "pi": [os.path.expandvars(r"%USERPROFILE%\.pi\agent\skills"), os.path.expanduser("~/.pi/agent/skills")],
    "claude": [os.path.expandvars(r"%USERPROFILE%\.claude\skills"), os.path.expanduser("~/.claude/skills")],
    "codebuddy": [os.path.expandvars(r"%USERPROFILE%\.codebuddy\skills"), os.path.expanduser("~/.codebuddy/skills")],
    # DeepSeek Harness: agentsHome 默认 %USERPROFILE%\.agents（技能目录 <agentsHome>\skills）
    "dsh": [os.path.expandvars(r"%USERPROFILE%\.agents\skills"), os.path.expanduser("~/.agents/skills")],
}

REPORT = {"ok": [], "warn": [], "fail": []}


def step(name, fn):
    try:
        msg = fn()
        REPORT["ok"].append(name)
        print(f"  [OK] {name}{' - ' + msg if msg else ''}")
    except Exception as e:
        REPORT["fail"].append(f"{name}: {e}")
        print(f"  [FAIL] {name}: {e}")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def check_python():
    v = sys.version_info
    assert v >= (3, 8), f"Python {v.major}.{v.minor} < 3.8"
    return f"Python {v.major}.{v.minor}.{v.micro}"


def check_scripts():
    for f in ("memory_tool.py", "build_index.py", "consolidate.py",
              "daemon.py", "build_preload.py", "infer.py", "security_rules.py"):
        assert os.path.exists(os.path.join(ROOT, f)), f"缺少 {f}"
    return f"{7} 个脚本齐全"


def ensure_target(args):
    """如果指定了 target 且不是当前目录，把模板文件迁移过去"""
    if not args.target or os.path.abspath(args.target) == MEMORY_ROOT:
        return MEMORY_ROOT
    target = os.path.abspath(args.target)
    os.makedirs(target, exist_ok=True)
    moved = 0
    for item in os.listdir(MEMORY_ROOT):
        src = os.path.join(MEMORY_ROOT, item)
        dst = os.path.join(target, item)
        if item in (".git", "dist", "__pycache__"):
            continue
        if not os.path.exists(dst):
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            moved += 1
    return target


def git_init(target):
    if os.path.exists(os.path.join(target, ".git")):
        return "已存在 git"
    r = run(["git", "init"], cwd=target)
    assert r.returncode == 0, r.stderr[:200]
    # 初次提交
    run(["git", "add", "-A"], cwd=target)
    r = run(["git", "commit", "-m", "AgentMemory init"], cwd=target)
    if r.returncode != 0 and "nothing to commit" not in r.stderr:
        return f"提交警告: {r.stderr[:100]}"
    return "git init + 初次提交"


def install_hook(target):
    src = os.path.join(MEMORY_ROOT, "hooks", "pre-commit")
    if not os.path.exists(src):
        src = os.path.join(ROOT, "hooks", "pre-commit")
    hook_dir = os.path.join(target, ".git", "hooks")
    if not os.path.exists(src):
        return "跳过（无钩子模板）"
    os.makedirs(hook_dir, exist_ok=True)
    shutil.copy2(src, os.path.join(hook_dir, "pre-commit"))
    try:
        os.chmod(os.path.join(hook_dir, "pre-commit"), 0o755)
    except Exception:
        pass
    return "pre-commit 已装"


def build_index(target):
    r = run([sys.executable, os.path.join(target, "scripts", "build_index.py")], cwd=target)
    assert r.returncode == 0, r.stderr[:200]
    n = len([f for f in os.listdir(os.path.join(target, "bank")) if f.endswith(".md")]) or "?"
    return f"索引完成"


def run_tests(target):
    total = 0
    for t in ("test_memory_tool.py", "test_consolidate.py", "test_v2.py"):
        r = run([sys.executable, os.path.join(target, "tests", t)], cwd=target)
        assert r.returncode == 0, f"{t}: " + r.stdout[-300:] + r.stderr[:300]
        total += r.stdout.count("[PASS]")
    return f"{total} 项自测通过"


def register_skill(target, args):
    if args.agent == "none":
        return "跳过（未指定 agent）"
    skill_template = os.path.join(MEMORY_ROOT, "SKILL.md.template")
    if not os.path.exists(skill_template):
        return "跳过（无 SKILL 模板）"
    # 探测 agent 类型
    agent = args.agent
    if not agent:
        for name in AGENT_SKILL_DIRS:
            if any(os.path.exists(d) for d in AGENT_SKILL_DIRS[name]):
                agent = name
                break
    if not agent:
        return "跳过（未探测到 agent 环境，可手动复制 SKILL.md.template）"
    dest_dir = None
    for d in AGENT_SKILL_DIRS[agent]:
        if os.path.exists(d) or agent == "pi":
            dest_dir = os.path.join(d, "memory-bank")
            break
    if not dest_dir:
        return f"跳过（{agent} skills 目录不存在）"
    os.makedirs(dest_dir, exist_ok=True)
    dest_skill = os.path.join(dest_dir, "SKILL.md")
    if os.path.exists(dest_skill):
        # 已有配置：备份后覆盖（防误覆盖既有部署）
        bak = dest_skill + ".bak-" + __import__("datetime").datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(dest_skill, bak)
        print(f"  [INFO] 已备份原 SKILL.md -> {os.path.basename(bak)}")
    with open(skill_template, encoding="utf-8") as f:
        content = f.read()
    content = content.replace("<MEMORY_ROOT>", target.replace("\\", "/"))
    with open(dest_skill, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已注册到 {agent} skills（memory-bank）"


def check_env(args):
    print("=== AgentMemory 安装前环境检查 ===")
    print(f"  Python: {sys.version_info.major}.{sys.version_info.minor} (需 >=3.8)")
    for name in AGENT_SKILL_DIRS:
        found = [d for d in AGENT_SKILL_DIRS[name] if os.path.exists(d)]
        print(f"  {name}: {'/'.join(found) if found else '(未检测到)'}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="install", description="AgentMemory 自动安装器")
    p.add_argument("--target", default="", help="安装目标目录（默认：解压目录本身）")
    p.add_argument("--agent", choices=["pi", "claude", "codebuddy", "dsh", "none", ""], default="", help="agent 类型（默认自动探测）")
    p.add_argument("--check", action="store_true", help="仅环境检查")
    p.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = p.parse_args()

    if args.check:
        check_env(args)
        return 0

    print("=== AgentMemory 自动安装 ===")
    step("环境检查: Python", check_python)
    step("脚本完整性", check_scripts)
    target = ensure_target(args)
    print(f"  安装目标: {target}")
    step("git 初始化", lambda: git_init(target))
    step("pre-commit 钩子", lambda: install_hook(target))
    step("索引构建", lambda: build_index(target))
    step("自测", lambda: run_tests(target))
    step("SKILL 注册", lambda: register_skill(target, args))

    ok = len(REPORT["ok"])
    fail = len(REPORT["fail"])
    print(f"\n=== 安装完成: {ok} 成功 / {fail} 失败 ===")
    if fail == 0:
        print("""
下一步（AI Agent 执行）:
  1. 询问用户是否提供 LLM key：复制 .env.example 为 .env 并填入 DEEPSEEK_API_KEY（可选，仅每周提炼用）
  2. 验证: python scripts/memory_tool.py health
  3. 开始使用: python scripts/memory_tool.py add --title "第一条" --category knowledge --body "..."
""")
    if args.json:
        print(json.dumps({"target": target, "report": REPORT}, ensure_ascii=False, indent=1))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
