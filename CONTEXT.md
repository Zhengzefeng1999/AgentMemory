# AgentMemory · 领域词汇表（Glossary）

> 本文件是 AgentMemory 领域模型的权威词汇表，不是 spec、不是 scratch pad。
> 只收录已解决的术语。实现细节见 docs/adr/。

## 记忆条目（Entry）

- **条目（Entry）** - 记忆库的最小单位：一个带 frontmatter 元数据的 Markdown 文件，位于 `bank/` 下。有唯一相对路径（uid）。

## 条目类型（Type）— 冲突处理的依据

- **事实类（Fact）** - 外部可验证的内容：规范条文、API 行为、数据结论、工具用法。系统可主动附上外部依据供用户核对。
- **认知类（Belief）** - 用户的理解/判断/方法论，无绝对对错。系统不纠正，只保留演化沿革。
- **偏好类（Preference）** - 用户的喜好/习惯/风格。系统永不纠正、永不触发冲突检测。

## 冲突与沿革

- **冲突检测** - 发现两条条目对同一主题持不同看法的机制。分两级：写入时本地相似度检测（热路径），consolidate 时 LLM 语义检测（冷路径）。
- **冲突卡（Conflict Card）** - 冲突检测产出的决策单：旧观点 + 新观点 + 依据，由用户拍板。不静默覆盖。
- **沿革链（superseded_by）** - 认知类条目被新理解替代时，旧条目保留并通过 `superseded_by` 指向新条目，形成可回溯的认知演化轨迹。
- **失效（Invalidated）** - 用户显式标记条目为已推翻（区别于归档 archive）。失效条目不出现在检索结果，可 `--include-invalid` 查看。

## 生命周期

- **双信号（Dual Signal）** - 条目的冷热由"更新频率 + 读取频率"共同决定。长期未更新且未被读取的条目衰减沉底；被频繁读取的条目 confidence 回升。
- **衰减（Decay）** - consolidate 对双信号均为冷的条目自动降 confidence，低于阈值转归档。

## 捕获与写入

- **自动捕获（Auto Capture）** - agent 在会话中自主判断"值得记"并调用 capture 写入，无需用户确认（完全自动 + 安全网兜底）。
- **极简写入（Minimal Add）** - `add "一句话"` 零参数写入：title=首句、category/tags/type 自动推断。低置信时手动路径交互确认一次；自动捕获路径（--auto）永不交互。
- **本地守护进程（Daemon）** - 常驻 HTTP 服务（默认 :8123），消灭每次 spawn 的首次 SQLite 提交开销（1127ms → ~10ms）。同时承载 PRELOAD 生成与飞书桥。
- **PRELOAD 预热** - 两级预热：`pinned` 常驻条目 + 近 7 天动态条目，≤200 行，会话启动时注入。

## 安全

- **三道防线（Three Defenses）** - ①写前规则拦截（热路径，零 LLM）→ ②consolidate LLM 补扫（冷路径）→ ③pre-commit 全量扫描闸门（push 前强制）。
- **拦截（Block）** - 凭证类内容（API key、token、私钥）拒绝写入。
- **自动标记（Auto-secret）** - 敏感类内容（内网 IP、手机号、Cookie）不拒绝写入，但自动标记 `secret: true`，摘要隐藏、get 需 --force、不发给 LLM。

## 检索

- **综合检索（Synthesize）** - 基于命中条目生成自然语言回答的模式；每条结论必须带 `[来源:bank/...]`，无记录必须明说"记忆库无此记录"。冷路径（用户主动触发，LLM 调用）。
- **热路径（Hot Path）** - 用户每次操作触发的路径：零 LLM、零网络，仅本地正则/SQL/规则。

## Skill 升级

- **Skill 草稿（Skill Draft）** - consolidate 对命中阈值（pattern_threshold）的模式自动生成 SKILL.md 草稿（when_to_use/procedure_steps/pitfalls 骨架），存入 `candidates/`；人工审阅后启用。系统生成，人决策。
