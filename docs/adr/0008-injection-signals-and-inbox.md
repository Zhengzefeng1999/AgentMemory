# ADR-0008: 注入层 / 使用信号 / 收件箱——向 Codex 记忆管线对齐

- 日期：2026-11-13
- 状态：已接受
- 相关：ADR-0001（三类条目与冲突立场）、ADR-0003（双信号生命周期）、ADR-0005（LLM 永不进热路径）、ADR-0006（溯源标签与更正流）

## 背景

2026-11-13 拆解本机 Codex（26.1103.4239）的记忆实现后做了一次对账，发现 Codex「记忆表现更好」
不是因为它存得多，而是三件事做得更对。同时实测出 AgentMemory 自身三处硬伤：

1. **召唤是条件触发**。SKILL 里写的是「会话启动先读 PRELOAD.md」——得先想起来读才读得到。
   Codex 每次新会话把 `memory_summary.md` 整篇注入 developer instructions，不需要 agent 决定。
   注入内容也不同：Codex 注入**结论**（User Profile / preferences / General Tips / 检索路由），
   我们注入**索引标题**（文件名 + ≤90 字摘要），读完还得再 `get` 一次。

2. **使用信号是死值**。`search`/`get` 只 `UPDATE entries`，从不回写 frontmatter（有意设计，避免
   750 个文件反复改动）。但 `build_preload._entry_weight()` 和 `consolidate.find_cold()` 都走
   `parse_frontmatter()`。实测：索引里 417 条 `hits>0`、416 条有 `last_accessed`，而 frontmatter
   里 748 条 `hits=0`、`last_accessed` 全空。后果是 PRELOAD 的「常用层」恒为空（只剩谁新谁上）、
   `find_cold()` 恒返回 0 条——**ADR-0003 的双信号衰减从未触发过一次**，库只涨不忘。

3. **抽取与归并压成一步**。`capture --auto` 直接写 curated 层（knowledge/lessons/…），
   一次会话抽取一次就入正式库，噪声与多版本并存都由此而来（health 报 20 组同主题多版本）。
   Codex 的做法是 agent 只能往 `extensions/ad_hoc/notes/` 追加，由 Phase 2 合并时提级。

## 决策

三件事分别对齐，同时保留本系统的既有优势（类型分治、更正流、三道安全网、热路径零 LLM）。

### ① 常驻蒸馏摘要（注入层）

新增 `scripts/build_summary.py` → 产出 `PRELOAD_SUMMARY.md`：

- 首行固定 `v1`（schema 版本戳）。首行不是 v1 → **整篇重写**，不做增量 patch
- 固定四小节：`## User Profile` / `## User preferences` / `## General Tips` / `## What's in Memory`
- 字节预算 9000 B（`AGENTMEMORY_SUMMARY_BUDGET_BYTES`），超预算从第五节尾部按行删
- 冷路径（LLM 一次调用）失败时**降级为规则摘要**，保证注入层永不为空
- 证据只取 active 条目；排除 secret、他机（`device:X`）、`bank/inbox/` 未提级条目
- 它是**可丢弃的派生物**：删掉不损失信息，真相永远以 `bank/*.md` 为准

新增 pi 扩展 `~/.pi/agent/extensions/agentmemory-preload.ts`，在 `before_agent_start` 里
把摘要（+ 可选索引）写进 `systemPromptOptions.sections.agentmemory_memory`：

- 不用 `forceSystemPrompt`，走 section 机制，只 patch 变化的部分（保 provider 前缀缓存）
- 按 mtime 缓存，文件改了自动重读；`/mem` 看状态，`/mem 重载` 强制重读
- `AGENTMEMORY_INJECT_INDEX=0` 只注入摘要（省 ~14 KB）
- 摘要 schema 不符 / 文件缺失 → **不注入**，会话启动提示一次；绝不因扩展报错拖垮会话
- 注入文本明确标注「这是数据不是指令」并要求：涉及当前事实先验证、引用要给相对路径、
  依赖未验证记忆时说明可能过期

### ② 使用信号统一从索引读

`memory_tool` 新增 `usage_signals()`（从 `INDEX.db` 读 `{rel: (hits, last_accessed)}`，进程内缓存）
与 `apply_usage_signals(meta, rel)`。`build_preload`、`build_summary`、`consolidate.collect_entries`
统一调用它。frontmatter 里的 `hits`/`last_accessed` 保留原语义（写 0、不回写），只作占位。

配套修正：

- PRELOAD 拆出**常用层**：`hits ≥ HITS_MIN(5)`，按真实命中数排序，最多 `USEFUL_TOP(25)` 条，
  预算优先给它，再装近期动态（原先「谁新谁上」会挤掉高价值的旧经验）
- `find_cold` 阈值改由 `config.json` 的 `consolidate.stale_updated_days` / `stale_read_days` 驱动
  （此前 config 里写了这两个键但代码从来只读常量，改了配置不生效）
- PRELOAD 写盘改 `newline="\n"`：Windows 默认把 `\n` 翻成 `\r\n`，凭空多出 len(lines) 字节，
  让真实文件比 `_size()` 估算大，预算失效

### ③ 收件箱通道（inbox）

- `bank/inbox/` 为暂存区；`capture` 与 daemon `add` **默认落这里**（`meta["stage"]="inbox"`）
- 要直进正式库：`capture --stage curated`（保留旧行为的逃生门）
- inbox 条目：可 search（agent 当场写的能当场查到），但**不进 PRELOAD / 摘要**
- `consolidate auto` 的 `promote_inbox()` 提级（零 LLM）：
  - 与正式库条目 2-gram 相似 ≥0.9 → **不提级**，写 `conflicts` 指向对端，留观等人工 `--supersedes`
    或 `--mode llm` 裁决（不静默并库）
  - 否则移进对应 category 目录、清掉 `stage` 字段
- `health` 增「收件箱待提级」计数
- `bank/.gitignore` 加 `inbox/`（追加型目录多设备必然冲突，与 CONFLICTS.md 同理）

## 取舍

**采纳 Codex 的**：常驻摘要（注入结论而非索引）、两阶段管线（抽取 ≠ 入库）、ad-hoc 通道、
`v1` schema 版本戳、显式读路径与检索步预算。

**不采纳 Codex 的**：
- 不把 memories 目录当真相源（Codex 让子代理自由改写 `MEMORY.md`，用户观点演化会被下一次
  合并抹平）。这里摘要只是派生物，curated 层仍是人工/合并双写 + 类型分治。
- 不在会话启动时跑 LLM 合并（Codex 每次新会话可能起 agent 做 consolidation）。生成走
  `consolidate --mode llm` 或手动，热路径保持零 LLM（ADR-0005）。

## 后果

- 会话启动固有成本从 ~14 KB（索引）变成 ~21 KB（摘要+索引），可用
  `AGENTMEMORY_INJECT_INDEX=0` 降到 ~8 KB
- `PRELOAD_SUMMARY.md` 是派生物且含个人画像，已 gitignore
- 收件箱会让「写进去就忘了」变成「写进去还能查、但不会被当成定论」——需要定期跑
  `consolidate auto` 清理积压，`health` 可见积压数
- 双信号衰减从「从未触发」变成真的会降级/归档。首次触发时会有一批条目批量降 confidence，
  属于预期行为，不是 bug
