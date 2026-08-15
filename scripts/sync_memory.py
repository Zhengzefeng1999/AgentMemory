#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory 多设备安全同步脚本
================================
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
  python scripts/sync_memory.py status     # 查看 4 台设备的同步状态

前置条件:
  - bank/ 已 git init 并配置了远程仓库 (git remote add origin <私有仓库URL>)
  - git 已配置 user.name / user.email
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
        # 区分: 网络/认证失败 vs 真正的合并冲突
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
    rc, _ = run_git(["add", "-A"], check=False)
    # 检查是否有变更
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
