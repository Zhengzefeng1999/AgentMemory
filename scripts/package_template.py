#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory 架构打包工具 — 生成可分发的模板包（只含架构，不含个人记忆）

用法:
  python scripts/package_template.py          # 生成 dist/AgentMemory-Template-vX.zip
  python scripts/package_template.py --out E:/share/  # 指定输出目录
"""
import argparse
import datetime
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "1.0.0"

# 分发包内文件清单（架构部分）
INCLUDE_FILES = [
    "README.md",
    "README_AI.md",
    "docs/architecture.html",
    ".gitignore",
    ".env.example",
    "config.json",
    "scripts/memory_tool.py",
    "scripts/build_index.py",
    "scripts/consolidate.py",
    "scripts/install.py",
    "tests/test_memory_tool.py",
    "tests/test_consolidate.py",
]

SAMPLE_BANK = {
    "user/_SAMPLE_profile.md": """---
title: 用户档案（示例）
tags: [profile, persona]
category: user
confidence: medium
verified_at: 1970-01-01
hits: 0
status: active
source: "模板示例"
conflicts: []
---

# 用户档案（示例）

> 本文件只含身份、角色与偏好。技术环境细节放 knowledge/agent-environment.md。

## 身份
- 姓名：（你的名字）
- 单位：（你的单位）
- 岗位：（你的岗位）

## 沟通偏好
- 中文 / 英文
- 简洁、结构化
""",
    "projects/_SAMPLE_project.md": """---
title: 项目上下文（示例）
tags: [project, sample]
category: projects
confidence: medium
verified_at: 1970-01-01
hits: 0
status: active
source: "模板示例"
conflicts: []
---

# 项目（示例）

## 概述
（项目目标、范围）

## 关键信息
- （依赖、地址、约束）
""",
    "knowledge/_SAMPLE_topic.md": """---
title: 知识主题（示例）
tags: [knowledge, sample]
category: knowledge
confidence: medium
verified_at: 1970-01-01
hits: 0
status: active
source: "模板示例"
conflicts: []
---

# 知识主题（示例）

## 内容
（方法论、工具链、领域知识）
""",
    "lessons/failures/_SAMPLE.md": """---
title: "踩坑示例：xxx 报错"
tags: [tool, sample]
category: failures
confidence: high
verified_at: 1970-01-01
hits: 0
status: active
source: "模板示例"
conflicts: []
---

# 踩坑记录

## 现象
（报错信息、失败表现）

## 根因
（为什么失败）

## 解决
（怎么修好的）
""",
    "lessons/corrections/_SAMPLE.md": """---
title: "纠正示例：错误理解 xxx"
tags: [sample]
category: corrections
confidence: high
verified_at: 1970-01-01
hits: 0
status: active
source: "模板示例"
conflicts: []
---

# 被纠正的教训

## 原错误理解
（我之前理解成什么）

## 正确理解
（实际应该是什么）
""",
    "lessons/patterns/.gitkeep": "",
    "CANDIDATES.md": """# Skill 候选清单（CANDIDATES）

> 由 consolidate.py 从 patterns.md 提炼，或人工登记。
> 满 5 条时提醒用户审核，走 skill_manage 升级流程。

| # | 模式 | 出现次数 | 建议 skill | 状态 |
|---|------|---------|-----------|------|
""",
}

INSTALL_MD = """# AgentMemory 模板包安装指南（部署者用）

本包是 AgentMemory 记忆银行的**架构模板**，不含任何个人记忆。
按以下步骤部署到你的机器（Windows / macOS / Linux 均可，需 Python 3.8+）。

## 1. 解压并放置

```bash
# 建议解压到非系统盘，例如 Windows: <盘符\\AgentMemory
# 或 Linux: ~/AgentMemory
unzip AgentMemory-Template-v{VERSION}.zip -d <盘符>:/AgentMemory
```

## 2. 配置路径（pi 用户可选）

如果你用 pi / Claude Code 等 agent，把 `SKILL.md.template` 复制为
agent 的 skill 目录下的 `memory-bank/SKILL.md`，并把文件里的
`<MEMORY_ROOT>` 替换为你的实际路径（如 `<盘符>:/AgentMemory`）。

## 3. 配置 LLM key（可选，仅每周提炼用）

```bash
cp .env.example .env
# 编辑 .env，填入 OpenAI 兼容的 LLM key（如 DeepSeek）：
#   DEEPSEEK_API_KEY=sk-xxx
# 检索不需要 key，只有 consolidate.py --mode llm 提炼时才用。
```

## 4. 初始化

```bash
python scripts/build_index.py        # 重建索引
python scripts/memory_tool.py health # 验证（应显示示例条目）
python tests/test_memory_tool.py     # 自测（6 项）
python tests/test_consolidate.py     # consolidate 窗口过滤回归测试（7 项）
git init && git add -A && git commit -m "init"   # 建立版本管理
```

## 5. 开始使用

```bash
python scripts/memory_tool.py add --title "第一条记忆" --tags "a,b" --category knowledge --body "内容"
python scripts/memory_tool.py search "关键词"
python scripts/memory_tool.py list --category knowledge
```

## 6. 日常运维

- 每周整理：`python scripts/consolidate.py --mode auto`（规则，零成本）
- 或带提炼：`python scripts/consolidate.py --mode llm`（用 .env 的 key）
- 敏感记忆：`add --secret`（摘要隐藏、不发给 LLM、get 需 --force）
- 安全：`.env` 已被 gitignore 排除 + pre-commit 钩子防 key 误提交（钩子随 git init 后自动生效；如需重新安装，把 `.git/hooks` 从模板的 `hooks/pre-commit` 复制）

## 文件说明

| 文件 | 说明 |
|------|------|
| `scripts/memory_tool.py` | 核心工具：add/search/get/update/archive/list/health |
| `scripts/build_index.py` | 重建 SQLite 索引（文件被外部编辑后跑） |
| `scripts/consolidate.py` | 整理：合并重复/淘汰过期/提炼 skill 候选 |
| `config.json` | 检索阈值、三档模式、LLM 配置 |
| `bank/` | 记忆库骨架（含 _SAMPLE_ 示例条目，可删） |
| `tests/` | 自测脚本 |

## 架构说明

- 纯 Python 标准库，零第三方依赖
- 脚本用自身位置推导根目录，**任何路径可部署**
- 检索本地 SQLite FTS，零 LLM token；记忆按需展开
- 三条底线：失败驱动检索 / 摘要导航 / 冲突优先
"""


def build_zip(out_dir):
    stamp = datetime.date.today().strftime("%Y%m%d")
    name = f"AgentMemory-Template-v{VERSION}-{stamp}.zip"
    zip_path = os.path.join(out_dir, name)
    os.makedirs(out_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        # 1. 架构文件
        for f in INCLUDE_FILES:
            src = os.path.join(ROOT, f)
            if os.path.exists(src):
                z.write(src, f)
        # 2. 安装指南
        z.writestr("INSTALL.md", INSTALL_MD.format(VERSION=VERSION))
        # 3. SKILL 模板（优先用根目录模板，其次 pi 已注册版本）
        skill_src = os.path.join(ROOT, "SKILL.md.template")
        if not os.path.exists(skill_src):
            skill_src = os.path.join(ROOT, "..", ".pi", "agent", "skills", "memory-bank", "SKILL.md")
        if os.path.exists(skill_src):
            with open(skill_src, encoding="utf-8") as f:
                skill = f.read().replace("D:\\AgentMemory", "<MEMORY_ROOT>").replace("D:/AgentMemory", "<MEMORY_ROOT>")
            z.writestr("SKILL.md.template", skill)
        else:
            z.writestr("SKILL.md.template", "# memory-bank SKILL 模板\n> 将 <MEMORY_ROOT> 替换为你的记忆库路径\n")
        # 4. pre-commit 钩子模板
        hook = os.path.join(ROOT, ".git", "hooks", "pre-commit")
        if os.path.exists(hook):
            z.write(hook, "hooks/pre-commit")
        # 5. 示例记忆骨架（不含个人记忆）
        for rel, content in SAMPLE_BANK.items():
            z.writestr(f"bank/{rel}", content)
    return zip_path


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="package_template")
    p.add_argument("--out", default=os.path.join(ROOT, "dist"))
    args = p.parse_args()
    zip_path = build_zip(args.out)
    size = os.path.getsize(zip_path) / 1024
    print(f"[OK] 模板包已生成: {zip_path}")
    print(f"   大小: {size:.0f} KB")
    print(f"   内容: 架构脚本 + 示例记忆骨架 + 安装指南 + SKILL模板")
    print(f"   不含: .env / INDEX.db / 个人记忆条目 / .git")
    return 0

if __name__ == "__main__":
    main()
