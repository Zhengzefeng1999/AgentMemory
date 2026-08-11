#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory security_rules — 敏感内容规则库（v2 三道防线之①③共用）

规则分两级：
  Block 级（凭证）：拒绝写入/提交 —— API key、token、私钥、密码
  Mark  级（敏感）：自动标记 secret —— 内网 IP、手机号、身份证、Cookie、敏感路径

被 memory_tool.add/capture（写前拦截①）、consolidate（补扫②）、
pre-commit 钩子（闸门③）共用，保证三层规则一致。
"""
import re

# ---- Block 级：凭证类（命中 = 拒绝） ----
BLOCK_PATTERNS = [
    ("OpenAI/Anthropic sk- 密钥", r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ("AWS AKIA 访问密钥", r"\bAKIA[0-9A-Z]{16}\b"),
    ("GitHub PAT", r"\bghp_[A-Za-z0-9]{36}\b"),
    ("GitLab PAT", r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    ("Bearer token", r"\bBearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
    ("私钥块", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ("密码赋值", r"\b(?:password|passwd|pwd|secret|token)\b\s*[\"']?\s*[:=]\s*[\"'][^\"']{6,}[\"']", re.IGNORECASE),
    ("app_secret 赋值", r"\bapp[_-]?secret\b\s*[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_-]{12,}[\"']", re.IGNORECASE),
    ("key 变量赋值(含小写值)", r"\b(?:api[_-]?key|access[_-]?key|apikey)\b\s*[\"']?\s*[:=]\s*[\"'][^\"']*(?=[A-Za-z0-9_]*[a-z])[A-Za-z0-9_]{16,}[\"']", re.IGNORECASE),
    ("微信/平台 Cookie 长串", r"(?:Cookie|cookies?)\s*[:=]\s*['\"][A-Za-z0-9_=;.%+-]{40,}['\"]", re.IGNORECASE),
]

# ---- Mark 级：敏感类（命中 = 自动标 secret，不拒绝） ----
MARK_PATTERNS = [
    ("内网 IPv4", r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b|\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    ("手机号", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ("身份证号", r"(?<!\d)\d{17}[\dXx](?!\d)"),
    ("邮箱", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("Webhook URL", r"https?://[^\s'\"<>]*(?:hooks|webhook)[^\s'\"<>]*", re.IGNORECASE),
    ("UNC 网络路径", r"\\\\[A-Za-z0-9._-]+\\"),
    # 高熵 token 形长串：≥40 字符且含大写+小写+数字混合（git hash 全小写不误伤）
    ("高熵 token 形长串", r"(?=[A-Za-z0-9_-]{40,})(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9_-]{40,}"),
]


def scan_text(text):
    """扫描文本，返回 (blocked, blocked_reason, marked, marked_reasons)。

    blocked: 命中 Block 级规则（凭证），调用方必须拒绝写入/提交
    marked:  命中 Mark 级规则（敏感），调用方应自动标 secret
    """
    blocked, blocked_reason = None, None
    marked, marked_reasons = [], []
    for name, pat, *flags in BLOCK_PATTERNS:
        if re.search(pat, text, flags[0] if flags else 0):
            blocked = name
            blocked_reason = f"命中 Block 级规则: {name}"
            break
    for name, pat, *flags in MARK_PATTERNS:
        if re.search(pat, text, flags[0] if flags else 0):
            marked.append(name)
    # 去重保序
    seen, uniq = set(), []
    for m in marked:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return blocked, blocked_reason, uniq, [f"命中 Mark 级规则: {n}" for n in uniq]


def scan_file(path):
    """扫描文件内容，返回与 scan_text 相同的结构；文件不存在返回空结果。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return scan_text(f.read())
    except OSError:
        return None, None, [], []


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path:
        b, br, m, mr = scan_file(path)
    else:
        b, br, m, mr = scan_text(sys.stdin.read())
    print(f"Block: {b or '无'}  {br or ''}")
    print(f"Mark : {m or '无'}  {' '.join(mr)}")
    sys.exit(1 if b else 0)
