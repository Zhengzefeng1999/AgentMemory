# 多设备记忆共享方案（防覆盖）

> 适用：Jeff 的 4 台设备，各自有记忆系统，都未配置 GitHub 仓库。
> 目标：跨设备共享 AgentMemory 记忆，**绝不发生一台设备覆盖另一台设备**。

## 一、核心原理：为什么 git 能防覆盖

`bank/` 是 git 仓库，git 本身就是为"多端分布式协作不覆盖"设计的：

| 风险场景 | git 的行为 | 结果 |
|---|---|---|
| 设备 A 新增记忆，设备 B 也新增 | 各自 commit，merge 合并**双方文件** | ✅ 两边记忆都在 |
| 两台设备同时改**同一个文件** | 产生合并冲突，git **停下**报错 | ✅ 不静默覆盖，人工解决 |
| 设备 B 落后就 push | git **拒绝**（non-fast-forward） | ✅ 提示先 pull，不覆盖 |
| 有人用 `push --force` | 会覆盖远程 | ❌ 唯一危险操作，**铁律禁用** |
| 索引 INDEX.db / PRELOAD.md | 已 .gitignore，不入库、可重建 | ✅ 不会因索引冲突 |

**记忆文件名带时间戳**（`20260815-184115-093-...`），天然唯一，不同设备几乎不可能写同一个文件——冲突概率极低。

## 二、架构：一个私有仓库，四端围绕它

```
                     ┌─────────────────────┐
                     │  GitHub 私有仓库     │
                     │  (唯一真相, 中央备份) │
                     └──────────┬──────────┘
          ┌──────────────┬──────┴───────┬──────────────┐
          ▼              ▼              ▼              ▼
   设备1 bank/      设备2 bank/     设备3 bank/     设备4 bank/
   (本机 D:)        (pull/push)    (pull/push)    (pull/push)
```

- **每台设备**：`D:/AgentMemory/` 完整目录 + 同一个 `bank/` 仓库（指向同一远程）
- **同步节奏**：写记忆**前**先 `pull`，写记忆**后**立即 `push`
- **远程仓库必须私有**：记忆含隐私，禁止公开

## 三、一次性部署（每台设备执行一遍）

### 第 1 步：建私有远程仓库（只需一次）
在 GitHub 新建 **Private** 仓库，例如 `AgentMemory-Bank`（**不要**勾选初始化 README）。

### 第 2 步：本机 bank 指向远程
```bash
cd D:/AgentMemory/bank
git remote add origin https://github.com/<你的账号>/AgentMemory-Bank.git
git branch -M main
git push -u origin main
```

### 第 3 步：同步脚本（已提供 `scripts/sync_memory.py`）
```bash
# 首次拉取远端记忆（写记忆前）
python D:/AgentMemory/scripts/sync_memory.py pull

# 完整同步：拉取 → 提交本地 → 推送（写记忆后）
python D:/AgentMemory/scripts/sync_memory.py sync   # 或直接运行
```

### 第 4 步：其他 3 台设备
```bash
# 方式 A：整体复制 AgentMemory 目录（含脚本/配置），再 clone bank
git clone https://github.com/<你的账号>/AgentMemory-Bank.git D:/AgentMemory/bank

# 方式 B：已有 bank，直接改 remote
cd D:/AgentMemory/bank
git remote set-url origin https://github.com/<你的账号>/AgentMemory-Bank.git
git pull origin main
```

> 注意：**不要**整目录覆盖式复制 `bank/` 到其他设备（那才是"覆盖"的根源）。
> 用 git clone / pull 才能保留历史并安全合并。

## 四、日常使用铁律（防出错）

1. **永远先 pull，再写，再 push** —— 顺序不可颠倒
2. **绝不用 `git push --force` / `-f`** —— 这是唯一能覆盖远程的操作
3. **写记忆前先 `sync pull`**，避免基于过期副本修改
4. **冲突出现时停下来人工解决**，不要删对方的内容：
   - 打开冲突文件，保留两边有效内容
   - 删除 `<<<<<<<` / `=======` / `>>>>>>>` 标记
   - `git add <文件>` → `git commit` → `git push`
5. **每台设备只在一台机器上同时写**，避免同一主题被并发改
6. **定期 `git log` 检查**各设备提交是否都上来了

## 五、常见问题

| 问题 | 处理 |
|---|---|
| push 报 non-fast-forward | 正常：远端有新提交。运行 `sync`（先 pull 再 push） |
| pull 报 merge conflict | 人工解决冲突（见上），不覆盖对方内容 |
| 认证失败 | HTTPS 配 token / SSH 配密钥；仓库必须是私有且有写权限 |
| 某设备误 force push | 用 `git reflog` 找回，`git reset --hard` 到丢失前 commit 再 push |
| 换电脑恢复 | clone 私有仓库到 `bank/`，`python scripts/build_index.py` 重建索引 |

## 六、验证同步是否成功

```bash
python D:/AgentMemory/scripts/sync_memory.py status
# 应看到来自所有设备的提交记录 (commit message 含设备主机名)
```
