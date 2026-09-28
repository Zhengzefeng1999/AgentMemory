# ADR-0006: 溯源标签与更正流（v2.1）

- 日期：2026-09-28
- 状态：已接受

## 背景

借鉴 vectorize-io/hindsight（agent memory that learns）的设计评审后发现的两个真实缺口：

1. **无溯源维度**。条目只有内容标签（`[HTML]`、`[git]`），没有"哪个项目写的、哪个 agent 写的"。跨项目检索时无法按项目过滤，也无法区分"本机环境知识"与"某个项目的经验"。hindsight 用 `retainTags: ["project:{gitProject}"]` 解决，但它的实现依赖整套服务端。
2. **更正断链**。ADR-0001 定义了 belief 的演化路径（以最新为准 + superseded_by 保留沿革），原语已存在（`update --invalidate --supersede`），但需要三条命令。实践中 agent 只写一条新条目就走了——2026-09-27 实际发生：先写入错误结论"GitHub 被墙"，再写更正，两条同时 `status: active`，检索命中哪条全凭运气。检测到缺口（conflicts 字段）与闭环（CONFLICTS.md 决策卡）之间的那一环，对"agent 当场发现写错了"的场景太重。

另有工程问题：consolidate.py 一直存在，但只能单独跑（`python scripts/consolidate.py`），memory_tool 的提示文案说"语义冲突由 consolidate 复核"，而多数 agent 不知道去哪跑。实测 CONFLICTS.md 停在 2026-08-11——周整理六周没有执行。

## 决策

v2.1 引入三件事，全部遵守 ADR-0005 热路径红线（零 LLM、零网络、仅本地 stat/SQL）：

**1. 溯源标签（写入时自动附加）**
- `project:<git 根目录名>`：从 cwd 向上找 `.git`（最多 12 层，纯 stat 调用，µs 级），取根目录名。
- `agent:<pi|claude-code|codebuddy|codex>`：环境变量扫描（`PI_*` / `CLAUDECODE` / `CODEBUDDY_*` / `CODEX_*`）。
- **非 git 目录不打 project 标签**——`repositories`、`downloads` 这类杂目录名当项目是噪声。
- 用户显式 `--tags` 在前，溯源标签置尾去重。检索用 `search --tag project:xxx` 过滤（既有 LIKE 匹配，零改动）。

**2. 更正流 `--supersedes <旧条目 id|path>`（一步闭环）**
- `capture/add --supersedes X` = 写入新条目 + X 自动标 `invalidated` + `superseded_by` 指向新条目。
- 两条路径语义不同：`add`（手动路径）**先验后写**——X 不存在则拒绝且不写入，用户修正引用后重试；`capture`（自动路径）**宽容**——写入后警告，保证更正内容不丢（延续 Q2"自动捕获永不阻塞"立场）。
- `_resolve_entry_path` 先查 SQLite 索引，未命中退回文件系统直接解析（手写、未重建索引的条目也能被引用），带防越界检查（解析结果必须仍在 bank/ 内）。
- 检索（status='active' 过滤）不再返回被更正的旧条目；`get` 旧条目提示"已被 X 替代（沿革链，见 ADR-0001）"。

**3. consolidate 统一入口**
- `memory_tool.py consolidate --mode auto|llm [--timeout N]` 委托 `scripts/consolidate.py`：独立进程 + 强制超时（auto 默认 180s / llm 900s），超时返回码 3 并明确提示，**永不无限等待**。
- 不捕获子进程输出——直写终端，进度实时可见，避免"沉默等待像卡死"。
- AGENTS.md 同步加入更正流用法，让后续 agent 会话知道用 `--supersedes` 而非只写新条目。

## 备选方案

- **抄 hindsight 的 LLM 自动合并（observations consolidation）**：拒绝。与 ADR-0001 冲突——fact 类冲突须用户拍板，系统自动改写记忆会成为回声室；且 LLM 进热路径违反 ADR-0005。
- **向量检索做查重**：拒绝。capture 时有 agent 策展（人判断在高信号位置），FTS + 本地相似度已够；hindsight 需要向量是因为它全自动无人工判断。
- **每轮会话自动入库（Stop hook 全量写回）**：拒绝。token 焚烧，信噪比低于"被纠正/踩坑/完成要事才 capture"的既有纪律。
- **按仓库分多个 bank**：拒绝。用户是跨项目的（水利工具 + 本机环境 + 报告排版），全局库 + 溯源标签比多库管理简单，且保留既有检索习惯。
- **supersedes 双向记录（新条目加 supersedes 字段）**：暂缓。schema 无该列，单向 superseded_by 已可回溯，等真实需要再加。

## 后果

- 正面：检索获得项目维度；更正从三条命令变一条，agent 可执行性大幅提高；周整理回到统一入口；实测热路径 0.25–0.36s（含 Python 启动），红线未破。
- 代价：条目 tags 含机器生成标签，人工阅读时略多两个词；`find_local_conflicts` 会把 invalidated 条目仍算作冲突候选（同主题本就该关联，可接受）。
- 回归保障：tests/test_v3_context.py 20 项断言（溯源标签 5 + 更正流端到端 9 + 性能红线 1 + consolidate 有界性 3 + 无残留清理 1），全套 48/48。
- 遗留：sync_memory.py 在 pull --rebase 返回 "Already up to date" 时误报"拉取失败"（返回码判断过严），不影响功能，待修。
