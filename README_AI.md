# README_AI.md — AI Agent 部署引导

> **如果你是 AI Agent（Claude Code / pi / CodeBuddy / Cursor 等），本文件是你部署 AgentMemory 记忆系统的唯一入口。请严格按流程执行。**
> 本包是**架构模板**，不含任何人的个人记忆——部署者后续会往 `bank/` 里沉淀自己的记忆。

---

## 你的任务

把 AgentMemory 记忆系统安装到当前机器，验证可用，并注册到 agent 的 skills 目录。

## 执行流程（按顺序，不要跳过）

### 第 1 步：环境检查

```bash
python scripts/install.py --check
```
或手动确认：Python ≥ 3.8（`python --version`）。无需任何第三方库。

### 第 2 步：安装

```bash
# 就地安装（推荐：解压在哪就装在哪）
python scripts/install.py

# 或安装到指定目录（自动迁移文件）
python scripts/install.py --target D:/AgentMemory

# 指定 agent 类型（默认自动探测）
python scripts/install.py --agent pi        # pi / claude / codebuddy / none
```

安装器会自动完成：git init（+初次提交）→ pre-commit 密钥钩子 → SQLite 索引 → 自测（6 项）→ 注册 `memory-bank` SKILL 到 agent 的 skills 目录。

### 第 3 步：验证（必须全部通过）

```bash
python scripts/memory_tool.py health
# 期望：条目总数 ≥ 5（示例条目），活跃条目 ≥ 5，无 FAIL

python tests/test_memory_tool.py
# 期望：结果 = 6 通过 / 0 失败
```

### 第 4 步：询问用户是否配置 LLM key（可选）

```bash
cp .env.example .env
# 编辑 .env，填入 OpenAI 兼容 key（如 DeepSeek）：
#   DEEPSEEK_API_KEY=sk-xxx
```
> 说明：检索/沉淀零成本不需要 key；只有 `consolidate.py --mode llm`（每周提炼）才用。用户没有 key 也能完整使用系统，只是跳过 LLM 提炼。

### 第 5 步：向用户报告

```
✅ AgentMemory 记忆系统已部署
- 位置: <安装目录>
- 状态: 自测 6/6 通过，索引正常
- 使用: python scripts/memory_tool.py add/search/get ...
- SKILL: 已注册 memory-bank（下次会话生效）
- 三条底线: 失败驱动检索 / 摘要导航 / 冲突优先
```

---

## 使用约定（部署完成后，你作为 agent 要遵守）

1. **失败驱动检索**：工具报错/命令失败 → 先查 `bank/lessons/failures/`；用户纠正 → 写 `bank/lessons/corrections/`
2. **摘要导航**：检索只读摘要，不全文展开；低置信度条目只提示"可核实"
3. **冲突优先**：记忆与当前证据冲突时以当前证据为准
4. **敏感记忆**：用户要求保密的内容 → `add --secret`（摘要隐藏、不发给 LLM、get 需 --force）
5. **成本控制**：检索本地零 token；只有用户明确要"整理记忆"时才跑 consolidate（auto 零成本 / llm 用 key）

## 常见问题排查

| 症状 | 处理 |
|------|------|
| `python scripts/install.py` 报 Python 版本 | 需要 Python ≥ 3.8，安装后重试 |
| health 显示 0 条 | 跑 `python scripts/build_index.py` 重建索引 |
| 自测有 FAIL | 检查 `tests/` 输出，多数是编码/路径问题，反馈给分发者 |
| SKILL 未注册 | 手动把 `SKILL.md.template` 复制为 `<agent-skills>/memory-bank/SKILL.md`，并把 `<MEMORY_ROOT>` 替换为实际路径 |
| consolidate llm 报 key 失败 | 检查 `.env` 的 `DEEPSEEK_API_KEY`；没有 key 就用 `--mode auto` |

## 安全提醒（必须告知部署者）

- `.env` 含密钥：已被 gitignore 排除 + ACL 收紧 + pre-commit 钩子拦截，**同步/备份时排除 `.env`**
- `consolidate --mode llm` 会把**非 secret** 记忆发送给 LLM 服务商（如 DeepSeek）——敏感内容务必 `--secret`
