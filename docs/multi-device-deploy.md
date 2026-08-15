# AgentMemory 多设备记忆共享 · 部署手册

> 本文件供**主设备**及其他设备按步骤操作。
> 目标：4 台设备共享同一套 AgentMemory 记忆库（`bank/`），**绝不发生一台设备覆盖另一台设备的记忆**。
> 编写日期：2026-08-15 ｜ 适用：Jeff 的 4 台设备

---

## 一、先理解：为什么不会覆盖

`bank/` 是一个 **git 仓库**，git 本身就是为"多端协作不覆盖"设计的：

| 场景 | git 行为 | 结果 |
|---|---|---|
| 两台设备各自新增记忆 | 自动合并**双方文件** | ✅ 记忆都在 |
| 落后设备直接 push | git **拒绝**（non-fast-forward） | ✅ 提示先 pull |
| 两台设备改同一个文件 | 合并冲突，git **停下**等人工决策 | ✅ 不静默覆盖 |
| 有人 `push --force` | 会覆盖远程 | ❌ 唯一危险操作，**铁律禁用** |

安全基础：
- 记忆文件名带时间戳（如 `20260815-184115-093-xxx.md`）天然唯一，不同设备几乎不会写同一文件
- `INDEX.db` / `PRELOAD.md` 已 .gitignore，不入库、可重建，不会产生索引冲突
- **"覆盖"只会发生在整目录复制粘贴或 force push 时** —— 两者都被本方案禁止

---

## 二、需要的东西（一次准备，全部设备共用）

| 名称 | 地址 | 说明 |
|---|---|---|
| 公开模板仓库 | `https://github.com/Zhengzefeng1999/AgentMemory.git` | 含全部脚本/文档/示例骨架，**无个人记忆** |
| 私有记忆仓库 | `https://github.com/Zhengzefeng1999/AgentMemoryBank.git` | 含你的个人记忆，**必须私有** |
| GitHub Token | `D:\03-项目开发\WebForge\github_Token.txt` | 访问私有仓库用（本机已配置，其他设备各自准备） |

> ⚠️ 隐私提醒：个人记忆全部在 `AgentMemoryBank`（私有）里；`AgentMemory`（公开）只有模板骨架和脚本，不含你的任何记忆。

---

## 三、部署步骤（每台设备执行一遍）

### 第 1 步：安装 AgentMemory（含脚本）

```bash
# 从公开模板仓库克隆整套工具（脚本/文档/示例）
git clone https://github.com/Zhengzefeng1999/AgentMemory.git D:/AgentMemory
```

> 若已有一份 AgentMemory 目录，可跳过克隆，只需确保 `scripts/sync_memory.py` 存在（见文末附录）。

### 第 2 步：克隆私有记忆库到 bank/

```bash
# 关键：用 git clone，绝不整目录复制粘贴（那是"覆盖"的根源）
git clone https://github.com/Zhengzefeng1999/AgentMemoryBank.git D:/AgentMemory/bank
```

### 第 3 步：配置 Git 身份（每台设备一次）

```bash
git config --global user.name "JeffZheng"
git config --global user.email "1187254478@qq.com"
```

### 第 4 步：配置私有仓库访问凭据

**方式 A（推荐，Windows）：** 把 token 交给 Windows 凭据管理器，之后全程免密：

```bash
# 以你的实际 token 替换 <TOKEN>（或从 token 文件读取）
git credential approve <<< "protocol=https
host=github.com
username=Zhengzefeng1999
password=<TOKEN>"
```

**方式 B：** 用带 token 的 URL 先推一次，之后 Git 会记住（注意不要把它写进文档/代码）：

```bash
git -C D:/AgentMemory/bank remote set-url origin https://<TOKEN>@github.com/Zhengzefeng1999/AgentMemoryBank.git
git -C D:/AgentMemory/bank pull origin main
# 成功后立刻把 URL 还原为纯净形式
git -C D:/AgentMemory/bank remote set-url origin https://github.com/Zhengzefeng1999/AgentMemoryBank.git
```

### 第 5 步：安装同步脚本

脚本已随公开仓库克隆到 `D:/AgentMemory/scripts/sync_memory.py`。
若你的设备没有它，直接复制本文**附录 A** 的代码保存为 `scripts/sync_memory.py`。

### 第 6 步：验证

```bash
cd D:/AgentMemory
python scripts/sync_memory.py status
python scripts/sync_memory.py
```

预期输出：拉取 → （首次可能拉入全部记忆）→ 本地无变更 → 推送成功 → `✓ 完成 [你的主机名]`。

---

## 四、日常使用铁律（防出错，务必遵守）

1. **顺序不可颠倒**：先 `pull` → 再写记忆 → 再 `push`
2. **写记忆前**：`python D:/AgentMemory/scripts/sync_memory.py pull`
3. **写记忆后**：`python D:/AgentMemory/scripts/sync_memory.py`（完整同步）
4. **绝不用** `git push --force` / `-f` —— 这是唯一能覆盖远程的操作
5. **冲突出现时停下来人工解决**（见第六节），不要删对方内容
6. **同一主题不要在多台设备同时编辑**，避免无谓冲突
7. 定期 `python D:/AgentMemory/scripts/sync_memory.py status` 检查各设备提交是否都上来了

---

## 五、常见问题

| 问题 | 处理 |
|---|---|
| push 报 `non-fast-forward` | 正常：远端有新提交。运行完整 `sync`（先 pull 合并再推送） |
| pull 报 `CONFLICT` | 人工解决（见第六节），不覆盖对方内容 |
| 认证失败 / Permission denied | 检查 token 是否有效、仓库是否私有、是否有写权限；重新配置第 4 步 |
| 某设备误 force push | 别慌：用 `git reflog` 找回，`git reset --hard <丢失前commit>` 再 push |
| 换新电脑恢复 | 重新执行第三节即可（clone 私有仓库到 bank/） |
| Git SSL 证书报错 | 本机有 MITM 环境时：`git -C D:/AgentMemory/bank -c http.sslVerify=false pull/push` |

---

## 六、冲突处理（唯一需要人工的环节）

当两台设备同时修改同一个文件时，git 会停下并生成冲突标记：

```
<<<<<<< HEAD
（你本地的内容）
=======
（远端的内容）
>>>>>>> 7d4efa8 (其他设备)
```

处理步骤：

```bash
cd D:/AgentMemory/bank
git status                      # 看哪些文件冲突
# 用编辑器打开冲突文件：
#   - 保留两边都有价值的内容（一般是合并两段文字）
#   - 删除 <<<<<<< / ======= / >>>>>>> 三行标记
git add <冲突文件>              # 标记已解决
git commit -m "merge: 手工合并冲突"
git push
```

---

## 附录 A：同步脚本完整代码（sync_memory.py）

若设备上没有该脚本，复制以下全部内容，保存为 `D:/AgentMemory/scripts/sync_memory.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory 多设备安全同步脚本
用途: 在 4 台设备间共享 bank/ 记忆库, 防止一台设备的记忆覆盖其他设备。

安全铁律(本脚本强制):
  1. 先 pull --rebase, 再 commit, 再 push (顺序不可颠倒)
  2. 绝不使用 force push / push --force
  3. pull 若产生冲突: 立即中止并提示人工处理, 绝不自动覆盖
  4. 每个提交信息包含设备名+时间, 便于回溯
  5. 每台设备在写记忆前先同步一次, 写完后立即同步一次

用法:
  python scripts/sync_memory.py            # 拉取 + 提交本地 + 推送 (完整同步)
  python scripts/sync_memory.py pull       # 只拉取远端记忆 (写记忆前必做)
  python scripts/sync_memory.py push       # 只提交本地并推送 (写记忆后必做)
  python scripts/sync_memory.py status     # 查看各设备的同步状态
"""

import argparse
import datetime
import os
import socket
import subprocess
import sys

# Windows 控制台默认 GBK，强制 UTF-8 输出避免 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BANK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bank")


def run_git(args, check=True, capture=True):
    """执行 git 命令, 返回 (returncode, stdout)。"""
    cmd = ["git", "-C", BANK_DIR] + args
    result = subprocess.run(
        cmd, capture_output=capture, text=True, encoding="utf-8", errors="replace"
    )
    if check and result.returncode != 0:
        print(f"❌ git {' '.join(args)} 失败:\n{result.stderr or result.stdout}")
        sys.exit(1)
    return result.returncode, (result.stdout or "")


def device_name():
    """生成设备标识: 主机名, 用于提交信息区分。"""
    return socket.gethostname() or "device"


def sync_pull():
    print("① 拉取远端记忆 (pull --rebase)...")
    rc, out = run_git(["pull", "--rebase", "origin", "main"], check=False)
    if rc != 0:
        low = out.lower()
        if "conflict" in low or "merge conflict" in low or "<<<<<<<" in out:
            print("⚠️ 检测到合并冲突! 请人工解决后重试:")
            print("   cd D:/AgentMemory/bank")
            print("   git status          # 查看冲突文件")
            print("   # 手动编辑冲突文件, 保留两边内容, 删除冲突标记")
            print("   git add <文件> && git commit && git push")
            sys.exit(2)
        print(f"⚠️ 拉取失败(可能是网络/未配置远程/无提交)。详情:\n{out}")
        print("   - 首次使用且远程为空: 可忽略, 继续执行推送")
        print("   - 未配置远程: 先执行 git -C bank remote add origin <URL>")
        return False
    if out.strip():
        print(f"   拉取结果: {out.strip()[:200]}")
    else:
        print("   已是最新。")
    return True


def sync_commit(message=None):
    print("② 提交本地新增记忆...")
    run_git(["add", "-A"], check=False)
    rc2, status = run_git(["status", "--porcelain"], check=False)
    if not status.strip():
        print("   本地无变更, 无需提交。")
        return False
    if message is None:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        message = f"sync [{device_name()}] {ts}"
    rc, out = run_git(["commit", "-m", message], check=False)
    if rc != 0:
        if "nothing to commit" in out:
            print("   本地无变更, 无需提交。")
            return False
        if "user.name" in out or "user.email" in out:
            print("❌ 请先配置 git 身份: git config --global user.name / user.email")
            sys.exit(1)
        print(f"❌ 提交失败:\n{out}")
        sys.exit(1)
    print(f"   已提交: {message}")
    return True


def sync_push():
    print("③ 推送至远端 (普通 push, 绝不 force)...")
    rc, out = run_git(["push", "origin", "main"], check=False)
    if rc != 0:
        low = out.lower()
        if "non-fast-forward" in low or "fetch first" in low or "rejected" in low:
            print("⚠️ 远端有新提交, 本机落后。禁止 force push!")
            print("   请重新运行完整同步: python scripts/sync_memory.py")
            print("   (会先 pull 合并远端变更, 再推送)")
            sys.exit(2)
        if "could not read" in low or "authentication" in low or "permission denied" in low:
            print("❌ 认证失败。请检查:")
            print("   - GitHub 仓库是否为私有且你有写权限")
            print("   - HTTPS: 已配置 token / SSH: 已配置密钥")
            sys.exit(1)
        print(f"❌ 推送失败:\n{out}")
        sys.exit(1)
    print("   ✓ 推送成功, 记忆已同步到远端。")
    return True


def cmd_status():
    rc, out = run_git(["log", "--oneline", "-15"], check=False)
    print("=== 最近 15 条记忆提交 (来自所有设备) ===")
    print(out)
    rc2, rem = run_git(["remote", "-v"], check=False)
    print("=== 远程仓库 ===")
    print(rem.strip() or "(未配置远程 — 执行 remote add origin <URL>)")


def main():
    parser = argparse.ArgumentParser(description="AgentMemory 多设备安全同步")
    parser.add_argument("cmd", nargs="?", default="full", choices=["full", "pull", "push", "status"])
    args = parser.parse_args()

    if args.cmd == "status":
        cmd_status()
        return

    if args.cmd in ("full", "pull"):
        sync_pull()
    if args.cmd == "full":
        sync_commit()
        sync_push()
    elif args.cmd == "push":
        sync_commit()
        sync_push()
    print(f"\n✓ 完成 [{device_name()}]")


if __name__ == "__main__":
    main()
```

---

## 附录 B：AgentMemory 常用命令速查

```bash
# 写入记忆（自动推断类型/分类/标签）
python D:/AgentMemory/scripts/memory_tool.py add "一句话记忆"

# 会话中自动捕获
python D:/AgentMemory/scripts/memory_tool.py capture --body "今天学到的东西"

# 搜索（本地零 token）
python D:/AgentMemory/scripts/memory_tool.py search "关键词"

# 综合回答（LLM，结论带来源）
python D:/AgentMemory/scripts/memory_tool.py search "问题" --synthesize

# 健康检查
python D:/AgentMemory/scripts/memory_tool.py health

# 同步（写记忆前 pull / 写记忆后完整同步）
python D:/AgentMemory/scripts/sync_memory.py pull
python D:/AgentMemory/scripts/sync_memory.py
```

---

*文档结束。遇到任何问题，先在本文「常见问题」中查找；仍未解决再联系配置方。*
