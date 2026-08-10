# 🧠 AgentMemory · 记忆银行

> 可移植、零依赖、低成本、自进化的 Agent 会话记忆库。
> 突破 pi memory 5000 chars 限制，任何 Agent 客户端（pi / Claude Code / CodeBuddy）可读写同一套记忆。

## 架构总览

```
┌─────────────────────────────────────────────────────────┐
│ L0 历史会话（session_search，无限，自动）               │ ← Agent 自带
│ L1 热记忆（Agent 内置 memory，小容量）                  │ ← Agent 自带
│ L2 本记忆库（bank/，磁盘无限）                         │ ← 本系统 ★
│ L3 Skill 库（可执行方法论）                            │ ← 接 skill_manage
│ L4 外部知识（外部知识库 / 规范 / 数据源，按需）     │ ← 已有 skill
└─────────────────────────────────────────────────────────┘
```

## 目录结构

```
AgentMemory\
  README.md              ← 本文件
  config.json            ← 配置（路径、检索阈值、模式）
  .env                   ← LLM key（gitignore，勿提交）
  bank\                  ← ★ 记忆库（git 仓库，真相所在）
    user\ profile.md     ← 用户身份、偏好
    projects\ *.md       ← 项目上下文
    knowledge\ *.md      ← 领域知识、方法论
    lessons\             ← 经验层
      failures.md        ← 踩坑记录
      corrections.md     ← 被纠正的教训
      patterns.md        ← 重复模式（skill 候选）
    CANDIDATES.md        ← 待升级为 skill 的模式
    INDEX.db             ← SQLite 索引（gitignore，可重建）
  scripts\               ← 纯 Python 标准库工具
    memory_tool.py       ← add / search / get / update / archive / health / list
    build_index.py       ← 重建 SQLite 索引
    consolidate.py       ← 每周整理：合并/淘汰/提炼候选
  tests\                 ← 自测脚本
```

## 快速开始

```bash
# 查看健康状态
python scripts/memory_tool.py health

# 添加一条记忆（正文从 stdin 或 --body）
python scripts/memory_tool.py add --title "标题" --tags "a,b,c" --category lessons/failures

# 搜索（返回摘要列表，本地 FTS 零 token）
python scripts/memory_tool.py search "npm"

# 读取全文
python scripts/memory_tool.py get --id <id>

# 重建索引（文件被外部编辑后）
python scripts/build_index.py

# 每周整理
python scripts/consolidate.py --mode auto
```

## 配置

见 `config.json`。LLM key 放 `.env`（`DEEPSEEK_API_KEY=sk-...`），仅 `consolidate.py` 提炼时使用。

## 安全说明（重要）

1. **`.env` 已被三重防护**：① `.gitignore` 排除（不会提交 git）② Windows ACL 已收紧为仅当前用户可读 ③ 仅 `consolidate.py` 使用，代码不打印 key
2. **敏感记忆用 `--secret` 标记**：`add --secret` 的条目 → 检索摘要隐藏正文、`get` 需 `--force`、**绝不发送给 LLM 提炼**（consolidate 自动跳过）
3. **git pre-commit 钩子**：检测 `sk-`/`AKIA`/`ghp_` 等密钥模式，防误提交；误报时用 `git commit --no-verify` 绕过
4. **同步/备份时排除 `.env`**：复制 AgentMemory 目录到网盘/共享前，删除或忽略 `.env`（gitignore 只保护 git，不保护手动复制）
5. **consolidate --mode llm 会把非 secret 记忆发送给 DeepSeek**：敏感内容务必 `--secret` 标记

## 可移植性

- 纯 Python 标准库（sqlite3 / json / 自写 markdown 解析），任何装 Python 3.8+ 的机器可跑
- 脚本用自身位置推导根目录，无绝对路径依赖
- `bank/` 是 git 仓库：可回滚、可 clone、可局域网共享
- 迁移：整个 `AgentMemory` 目录复制到新机器即可，`python scripts/build_index.py` 重建索引

## 恢复指南

1. `git clone <备份位置> bank`（或直接复制）
2. `python scripts/build_index.py` 重建索引
3. `python scripts/memory_tool.py health` 验证
