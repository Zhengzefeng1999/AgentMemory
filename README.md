# 🧠 AgentMemory · 记忆银行

> 可移植、零依赖、低成本、自进化的 Agent 会话记忆库。
> 突破 pi memory 5000 chars 限制，任何 Agent 客户端（pi / Claude Code / CodeBuddy）可读写同一套记忆。
> v2：自动捕获 + 类型体系 + 三层安全网 + 双信号生命周期 + 本地 daemon（架构决策见 `docs/adr/`）

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

## v2 新增能力

| 能力 | 说明 | 决策 |
|---|---|---|
| **自动捕获** | agent 会话中自主 `capture` 写入，无需确认；安全网兜底 | ADR-0001/0002 |
| **条目类型** | fact / belief / preference 自动推断 + 可覆盖；冲突分层处理 | ADR-0001 |
| **三层安全网** | 写前拦截（Block 拒绝 / Mark 自动 secret）→ 周扫补检 → pre-commit 闸门 | ADR-0002 |
| **双信号生命周期** | 更新 + 读取双信号决定冷热；衰减归档、使用复活 | ADR-0003 |
| **本地 daemon** | 常驻 HTTP :8123，写入 ~100ms（v1 的 1.3s）；同时承载 PRELOAD + 飞书桥 | ADR-0004 |
| **PRELOAD 预热** | pinned 常驻 + 近 7 天动态，≤200 行，会话启动注入 | ADR-0004 |
| **综合检索** | `search --synthesize`：LLM 回答强制带 `[来源:path]`，无记录明说 | ADR-0005 |
| **Skill 草稿** | CANDIDATES 命中 → 自动生成 SKILL.md 草稿（candidates/），人工启用 | ADR-0001 |

**性能红线**：热路径（add/search/get）零 LLM、零网络，仅本地正则/SQL；LLM 只出现在 consolidate 周整理与 synthesize 冷路径（ADR-0005）。

## 目录结构

```
AgentMemory\
  README.md              ← 本文件
  CONTEXT.md             ← 领域词汇表（canonical terms）
  config.json            ← 配置（daemon/synthesize/consolidate/feishu）
  .env                   ← LLM key（gitignore，勿提交）
  PRELOAD.md             ← 预热索引（自动生成，gitignore）
  bank\                  ← ★ 记忆库（git 仓库，真相所在）
    user\ profile.md     ← 用户身份、偏好
    projects\ *.md       ← 项目上下文
    knowledge\ *.md      ← 领域知识、方法论
    lessons\             ← 经验层（failures / corrections / patterns）
    candidates\          ← SKILL.md 草稿（consolidate 生成，人工审阅）
    CANDIDATES.md        ← 待升级为 skill 的模式
    CONFLICTS.md         ← 冲突卡（LLM 语义冲突，待你决策）
    INDEX.db             ← SQLite 索引（gitignore，可重建）
  scripts\               ← 纯 Python 标准库工具
    memory_tool.py       ← add/capture/search/get/update/archive/list/health
    daemon.py            ← 本地常驻服务（快速写入 + PRELOAD + 飞书桥）
    build_preload.py     ← PRELOAD 预热生成器（pinned + 近 7 天）
    infer.py             ← 类型/分类/tags 启发式推断（热路径零 LLM）
    security_rules.py    ← 敏感规则库（Block/Mark 两级，三层防线共用）
    consolidate.py       ← 周整理：双信号衰减/去重/补扫/冲突/草稿
    build_index.py       ← 重建 SQLite 索引
    install.py           ← 一键部署 + 自测 + 注册 SKILL
    package_template.py  ← 分发模板打包
  docs\adr\              ← 架构决策记录（0001~0005）
  hooks\pre-commit       ← 提交前敏感闸门
  tests\                 ← 自测（28 项）
```

## 快速开始

```bash
# 健康状态
python scripts/memory_tool.py health

# 极简写入（一句话降级：title=首句，type/category/tags 自动推断）
python scripts/memory_tool.py add "河网密度=水系总长/流域面积，规范公式"

# 自动捕获（agent 会话中用；永不交互，写前安全拦截）
python scripts/memory_tool.py capture --body "今天踩坑：Excel 打开大断面 CSV 时编码要用 utf-8-sig"

# 搜索（本地 FTS 零 token）/ 综合回答（LLM，结论带来源）
python scripts/memory_tool.py search "水位基面"
python scripts/memory_tool.py search "水位基面换算怎么做" --synthesize

# 读取全文（刷新 last_accessed；secret 需 --force）
python scripts/memory_tool.py get bank/knowledge/xxx.md

# 更新：改类型 / 钉住常驻 / 失效 / 沿革链
python scripts/memory_tool.py update <path> --type fact --pin true --invalidate --supersede bank/xxx.md

# 本地 daemon（推荐常驻：写入 ~100ms，替代每次 1.3s 的进程启动）
python scripts/daemon.py --port 8123
curl -X POST http://127.0.0.1:8123/memory -H "Content-Type: application/json" -d '{"cmd":"add","body":"...","auto":true}'

# PRELOAD 预热（会话启动时生成并读取）
python scripts/build_preload.py && type PRELOAD.md

# 每周整理（auto=规则零成本；llm=提炼/冲突/补扫/草稿）
python scripts/consolidate.py --mode auto
python scripts/consolidate.py --mode llm
```

## 配置

见 `config.json`：

- `consolidate.llm` — 周整理 LLM（DeepSeek，key 放 `.env` 的 `DEEPSEEK_API_KEY`）
- `synthesize.llm` — 综合检索 LLM（可复用同一 key）
- `daemon` — 端口 / PRELOAD 预算（默认 :8123 / 200 行 / 7 天）
- `feishu` — 飞书桥（app_id / app_secret / chat_id；配好后 `daemon --with-bridge` 启用，飞书里发 `记：xxx` / `查：xxx`）

## 安全说明（三道防线，ADR-0002）

1. **写前拦截**：凭证类（`sk-`/`AKIA`/`ghp_`/私钥/Bearer）→ **拒绝写入**；敏感类（内网 IP/手机号/邮箱/Cookie）→ **自动标记 secret**（摘要隐藏、get 需 `--force`、不发给 LLM、不进 PRELOAD）
2. **周扫补检**：`consolidate --mode llm` 对近 7 天条目规则补扫，漏网的自动标 secret
3. **提交闸门**：git pre-commit 钩子全量扫描，Block 级命中拒绝提交；误报时 `git commit --no-verify` 绕过
4. `.env` 已三重防护：gitignore + 权限收紧 + 代码不打印 key；同步/备份时排除 `.env`
5. **LLM 数据边界**：secret 条目绝不发送给 LLM；`--mode llm` 只发送近 7 天非 secret 条目

## 可移植性

- 纯 Python 标准库（sqlite3 / json / 自写 markdown 解析），Python 3.8+ 即可
- 脚本用自身位置推导根目录，无绝对路径依赖
- `bank/` 是 git 仓库：可回滚、可 clone、可局域网共享
- 迁移：整个目录复制到新机器 → `python scripts/build_index.py` 重建索引
- 分发：`python scripts/package_template.py` 生成不含个人记忆的模板 zip

## 恢复指南

1. `git clone <备份位置> bank`（或直接复制）
2. `python scripts/build_index.py` 重建索引
3. `python scripts/memory_tool.py health` 验证

## 设计立场（不是应声虫）

记忆系统**不讨好用户**：事实类冲突附外部依据、认知类保留演化沿革（superseded_by）、错误不被当真理反复注入。你检索时会撞见自己的认知历史，而不是一个自我重复的回声室。详见 `CONTEXT.md` 与 ADR-0001。
