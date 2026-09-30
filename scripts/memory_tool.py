#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory memory_tool — 记忆库读写检索工具 v2（纯 Python 标准库）

用法:
  add           添加记忆（支持极简降级: add "一句话"）
  capture       自动捕获（--auto 永不交互，agent 会话中调用）
  search        检索（本地 SQLite FTS，零 LLM token；--synthesize 综合回答）
  get           读取全文（secret 需 --force；同时刷新 last_accessed）
  update        更新条目（hits/status/confidence/type/pin/invalidate/supersede）
  archive       归档条目
  list          列出分类下条目
  health        记忆库健康报告
  consolidate   周整理入口（委托 scripts/consolidate.py，独立进程+超时防挂起）
  daemon        启动常驻本地服务（快速写入 + PRELOAD + 飞书桥）

v2 变更（见 docs/adr/）:
  - 条目类型 type: fact/belief/preference（Q4 自动推断，可覆盖）
  - 极简 add: 无参数时 title=首句、category/tags/type 自动推断（Q6）
  - capture --auto: 自动捕获，永不交互（Q2）
  - 写前敏感拦截: Block 拒绝 / Mark 自动标 secret（Q7 防线①）
  - 写入时本地相似度冲突检测（热路径零 LLM，Q3/Q5）
  - search --synthesize: LLM 综合回答 + 强制来源标注（Q8，冷路径）
  - get 刷新 last_accessed（Q9 双信号读取侧）

v2.1 变更:
  - 溯源标签: add/capture 自动附加 project:<git根目录> 与 agent:<来源>（纯本地 stat+env，零子进程，ADR-0005）
  - 更正流: capture/add --supersedes <旧条目id|path> 一步完成「写入新条目 + 旧条目标 invalidated+superseded_by」（ADR-0001 belief 演化路径）
  - consolidate 子命令: 从 memory_tool 同一入口跑周整理（auto=零LLM / llm=批量提炼），subprocess+timeout 永不挂起
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
BANK_DIR = os.path.join(ROOT, "bank")
INDEX_DB = os.path.join(BANK_DIR, "INDEX.db")
SCRIPTS = os.path.join(ROOT, "scripts")
PRELOAD_PATH = os.path.join(ROOT, "PRELOAD.md")

sys.path.insert(0, SCRIPTS)
from security_rules import scan_text  # noqa: E402
from infer import infer_type, infer_category, infer_tags  # noqa: E402

CATEGORY_DIRS = {
    "user": ("user", "档案"),
    "projects": ("projects", "项目"),
    "knowledge": ("knowledge", "知识"),
    "failures": ("lessons/failures", "踩坑"),
    "corrections": ("lessons/corrections", "纠正"),
    "patterns": ("lessons/patterns", "模式"),
}

# ---- 基础 ----

def _utf8(s):
    """Windows GBK 控制台兼容输出"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return s

def load_config():
    """读取 config.json；缺失/损坏时回退空配置（默认值生效），保证可移植性。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        print(f"⚠️ {CONFIG_PATH} 不是合法 JSON，已按默认配置运行")
        return {}

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def slugify(title, maxlen=24):
    s = re.sub(r"[^\w\u4e00-\u9fff-]", "-", title.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "untitled"

# ---- Frontmatter 解析 ----

def parse_frontmatter(text):
    """解析 --- yaml --- 头。返回 (meta dict, body str)。YAML 子集解析。

    注意：frontmatter 区域限制在文件前 200 行内寻找闭合 ---（v2 字段增多，
    不能再用 split(\n, 12) 截断——那会漏掉超长 frontmatter 的闭合行）。
    """
    meta, body = {}, ""
    if not text.startswith("---"):
        return meta, text
    lines = text.split("\n")
    end = None
    for i in range(1, min(len(lines), 200)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return meta, text
    fm = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:])
    for line in fm.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v.startswith("[") and v.endswith("]"):
                meta[k] = [x.strip().strip('"\'') for x in v[1:-1].split(",") if x.strip()]
            elif v in ("true", "false"):
                meta[k] = v == "true"
            else:
                meta[k] = v.strip('"\'')
    return meta, body

def build_frontmatter(meta, body):
    out = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            out.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        elif isinstance(v, bool):
            out.append(f"{k}: {'true' if v else 'false'}")
        else:
            out.append(f"{k}: {v}")
    out.append("---")
    out.append(body.lstrip("\n"))
    return "\n".join(out) + "\n"

# ---- 文件与索引 ----

def all_entry_files(include_invalid=False):
    files = []
    for sub in ("user", "projects", "knowledge", "lessons/failures", "lessons/corrections", "lessons/patterns"):
        d = os.path.join(BANK_DIR, sub)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".md"):
                    files.append(os.path.join(d, fn))
    return files

def entry_rel_path(fp):
    return os.path.relpath(fp, BANK_DIR).replace("\\", "/")

def get_conn():
    conn = sqlite3.connect(INDEX_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下安全，减少 fsync（ADR-0004 性能）
    return conn

def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS entries(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE, path TEXT, title TEXT, tags TEXT, category TEXT,
        confidence TEXT, verified_at TEXT, hits INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active', source TEXT, updated_at TEXT, summary TEXT,
        secret INTEGER DEFAULT 0, type TEXT DEFAULT 'belief',
        pinned INTEGER DEFAULT 0, last_accessed TEXT DEFAULT '', superseded_by TEXT DEFAULT '',
        bodyhash TEXT DEFAULT '') """)
    # v2: 兼容旧库补列
    for col, ddl in [
        ("type", "TEXT DEFAULT 'belief'"),
        ("pinned", "INTEGER DEFAULT 0"),
        ("last_accessed", "TEXT DEFAULT ''"),
        ("superseded_by", "TEXT DEFAULT ''"),
        ("bodyhash", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(title, body, tokenize='trigram')")
    except sqlite3.OperationalError:
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(title, body)")
        except sqlite3.OperationalError:
            pass  # FTS5 不可用时退化

def index_file(conn, fp, meta, body):
    rel = entry_rel_path(fp)
    uid = rel.replace("/", "__").replace(".md", "")
    summary = _make_summary(meta, body)
    tags = ",".join(meta.get("tags", []))
    secret = 1 if meta.get("secret") else 0
    cur = conn.execute(
        "INSERT OR REPLACE INTO entries(uid, path, title, tags, category, confidence, verified_at, hits, status, source, updated_at, summary, secret, type, pinned, last_accessed, superseded_by, bodyhash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, rel, meta.get("title", ""), tags, meta.get("category", ""),
         meta.get("confidence", "medium"), meta.get("verified_at", ""),
         int(meta.get("hits", 0)), meta.get("status", "active"),
         meta.get("source", ""), meta.get("updated_at", now_str()), summary, secret,
         meta.get("type", "belief"), 1 if meta.get("pinned") else 0,
         meta.get("last_accessed", ""), meta.get("superseded_by", ""), _body_hash(body)))
    rowid = cur.lastrowid
    try:
        conn.execute("DELETE FROM entries_fts WHERE rowid=?", (rowid,))
        conn.execute("INSERT INTO entries_fts(rowid, title, body) VALUES(?,?,?)",
                     (rowid, meta.get("title", ""), body))
    except sqlite3.OperationalError:
        pass
    return rowid

def _make_summary(meta, body):
    """首行非标题正文作为摘要"""
    for line in body.split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:80]
    return ""

def rebuild_index(verbose=True):
    conn = get_conn()
    init_schema(conn)
    conn.execute("DELETE FROM entries")
    try:
        # 旧版 unicode61 FTS 表不支持中文分词 → 重建为 trigram
        conn.execute("DROP TABLE IF EXISTS entries_fts")
    except sqlite3.OperationalError:
        pass
    init_schema(conn)  # 重新创建 trigram FTS 表
    n = 0
    for fp in all_entry_files():
        try:
            with open(fp, encoding="utf-8") as f:
                text = f.read()
            meta, body = parse_frontmatter(text)
            index_file(conn, fp, meta, body)
            n += 1
        except Exception as e:
            print(f"  [skip] {fp}: {e}")
    conn.commit()
    conn.close()
    if verbose:
        print(f"索引重建完成：{n} 条记忆")
    return n

# ---- 安全网①：写前拦截（Q7） ----

def pre_write_security_check(title, body):
    """写前敏感拦截。返回 (ok, msg)。
    Block 级 → 拒绝写入；Mark 级 → 提示调用方自动标 secret。"""
    text = f"{title}\n{body}"
    blocked, reason, marked, _ = scan_text(text)
    if blocked:
        return False, f"已拦截：{reason}。请移除凭证内容后重试。"
    return True, ("; ".join(marked) if marked else "")

# ---- 冲突检测（热路径本地版，Q3；v2.2 升级） ----

def _norm(s):
    return re.sub(r"[\s，。！？、,.;:：()（）\-—_\"'《》<>]+", "", str(s).lower())

def _bigrams(s):
    """2-gram 集合。中文短标题的区分度显著优于字符集合（v2.2）。"""
    t = _norm(s)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}

def title_similarity(a, b):
    """标题相似度：2-gram Jaccard；完全同名（normalize 后）直接 1.0。
    v2.2 修复：旧版把 `nt != ntitle`（完全同名）排除在冲突外，导致双轮批量导入的
    同名重复全部漏网——同名恰是最强重复信号，写入场景无自身可比，不应排除。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)

def title_containment(new, old):
    """宽松同主题度：共同 2-gram 占新标题 2-gram 总数（containment）。
    适用"同前缀不同后缀"的版本演进链——拦河坝2案例四版标题 Jaccard 仅 0.17~0.29，
    但共同主题前缀占新标题比例高，containment 可命中。仅作演进引导/聚类辅助，
    不单独作为重复判据（短标题易虚高）。"""
    g_new = _bigrams(new)
    if not g_new:
        return 0.0
    g_old = _bigrams(old)
    if not g_old:
        return 0.0
    return len(g_new & g_old) / len(g_new)

def _topic_peers(title, conn, threshold=0.3):
    """宽松同主题检索（containment）。返回同主题 active 条目，供演进引导。"""
    rows = conn.execute("SELECT path, title, status, tags FROM entries WHERE status='active'").fetchall()
    peers = []
    for path, t, status, tags in rows:
        c = title_containment(title, t)
        if c >= threshold:
            peers.append({"path": path, "title": t, "status": status, "tags": tags, "overlap": round(c, 2)})
    peers.sort(key=lambda x: -x["overlap"])
    return peers[:3]

def find_topic_peers_if_evolution(title, conn):
    """仅当标题含演进/更正触发词时才跑宽松同主题检索（v2.2）。
    避免普通写入付出 O(n) 额外扫描。"""
    if not _SUPERSEDE_HINT_RE.search(title or ""):
        return []
    return _topic_peers(title, conn)

def find_local_conflicts(title, body, conn, threshold=0.6):
    """本地相似度冲突检测：标题 2-gram Jaccard（v2.2）。返回候选条目列表。
    零 LLM、零网络（ADR-0005）。语义级检测留给 consolidate。"""
    ntitle = _norm(title)
    if len(ntitle) < 4:
        return []
    rows = conn.execute(
        "SELECT path, title, type, status, tags FROM entries WHERE status IN ('active','invalidated')"
    ).fetchall()
    cands = []
    for path, t, typ, status, tags in rows:
        sim = title_similarity(title, t)
        if sim >= threshold:
            cands.append({"path": path, "title": t, "type": typ, "status": status, "tags": tags, "overlap": round(sim, 2)})
    cands.sort(key=lambda x: -x["overlap"])
    return cands[:3]

# ---- v2.2 正文指纹（兜底批量导入跑两轮 / 完全重复写入） ----

def _body_hash(body):
    import hashlib
    return hashlib.sha256(str(body).encode("utf-8")).hexdigest()[:16]

def find_body_dup(body, conn):
    """正文 sha256 指纹精确查重（active）。完全相同正文 = 最强重复信号。"""
    h = _body_hash(body)
    rows = conn.execute(
        "SELECT path, title, status FROM entries WHERE bodyhash=? AND status='active'", (h,)
    ).fetchall()
    return [{"path": r[0], "title": r[1], "status": r[2]} for r in rows]

# ---- v2.2 设备标签解析（检索端标注 / PRELOAD 过滤 / supersedes 防跨设备共用） ----

def _tags_of(tags_str):
    """索引 tags 字符串（逗号分隔）→ set。"""
    return {t.strip() for t in (tags_str or "").split(",") if t.strip()}

def _device_view(tags_str):
    """→ (scope, device)：从 tags 提取 scope:device/scope:global 与 device:<name>。"""
    ts = _tags_of(tags_str)
    scope = next((t for t in ts if t.startswith("scope:")), "")
    dev = next((t for t in ts if t.startswith("device:")), "")
    return scope, (dev.split(":", 1)[1] if dev else "")

def _is_foreign_device(tags_str, local=None):
    """他机专属：scope:device 且来源设备≠本机（含无 device 标签的存量条目）。"""
    local = local or get_device_name()
    scope, dev = _device_view(tags_str)
    return scope == "scope:device" and dev != local

# ---- v2.2 更正流引导：把 --supersedes 从"靠素养"变"靠提示" ----

_SUPERSEDE_HINT_RE = re.compile(r"最终|定稿|更正|纠正|推翻|替代|收编|结论|批复|修订|更新版|新版|v\d+")

def suggest_supersedes(title, conflicts, local_device=None):
    """标题含演进/更正语义触发词 且 同主题已有 active 条目 → 返回引导文本；否则 None。
    背景：拦河坝2案例 29 分钟内连写 4 版"最终"结论互不收编，检索时并存矛盾数值。
    v2.2 设备防线：他机专属条目不参与收编建议——防跨设备伪冲突互相覆写
    （A 机验证发现 B 机经验"不对"→ 把 B 机正确经验改成 A 机事实）。"""
    if not conflicts or not _SUPERSEDE_HINT_RE.search(title or ""):
        return None
    local = local_device or get_device_name()
    actives = [c for c in conflicts
               if c.get("status") == "active" and not _is_foreign_device(c.get("tags", ""), local)]
    if not actives:
        return None
    tops = "、".join(f"{c['title'][:24]}" for c in actives[:2])
    return (f"💡 标题含演进/更正语义，同主题已有 active 条目：{tops}"
            f"{' 等' if len(actives) > 2 else ''}。若本条为替代/推翻，建议加 "
            f"--supersedes \"{actives[0]['path']}\" 一步收编旧条，避免新旧矛盾并存。"
            + ("（已排除他机专属条目）" if len(actives) < len([c for c in conflicts if c.get('status')=='active']) else ""))

# ---- v2.2 同主题多版本检测（检索端可见性 + health 统计共用） ----

def cluster_by_title(items, threshold=0.6, title_key="title", path_key="path"):
    """按标题 2-gram 相似度对条目聚类（v2.2 P2）。
    相似度 = max(Jaccard, containment)——后者覆盖"同前缀不同后缀"的版本演进链。
    items: dict 列表（含 title/path/verified_at）；返回 [{items, latest}]，仅含 size>1 的组。"""
    def sim(a, b):
        return max(title_similarity(a, b), title_containment(a, b))
    groups = []
    for it in items:
        placed = False
        for g in groups:
            if sim(it[title_key], g["items"][0][title_key]) >= threshold:
                g["items"].append(it)
                placed = True
                break
        if not placed:
            groups.append({"items": [it]})
    multi = [g for g in groups if len(g["items"]) > 1]
    for g in multi:
        g["items"].sort(key=lambda x: str(x.get("verified_at", "")))  # 旧→新
        g["latest"] = g["items"][-1]
    return multi

# ---- 溯源标签（v2.1；纯本地：向上 stat 找 .git + 环境变量扫描，零子进程零网络，ADR-0005） ----

_AGENT_ENV = [
    ("agent:pi",          lambda e: any(k.startswith("PI_") for k in e)),
    ("agent:claude-code", lambda e: "CLAUDECODE" in e or "CLAUDE_CODE_ENTRYPOINT" in e),
    ("agent:codebuddy",   lambda e: any(k.startswith("CODEBUDDY_") for k in e)),
    ("agent:codex",       lambda e: "CODEX_SANDBOX" in e or any(k.startswith("CODEX_") for k in e)),
]

# ---- v2.2 设备维度（多设备共享库：设备经验互不适用/互相污染问题） ----

def get_device_name():
    """设备名：config.json device.name 优先（hostname 常为 ADMINISTRATOR 这类无意义默认值，
    必须在各设备显式配置）；缺省退化 COMPUTERNAME 小写。"""
    try:
        return str(load_config().get("device", {}).get("name", "")).strip().lower()
    except Exception:
        pass
    return os.environ.get("COMPUTERNAME", "unknown").strip().lower()

# 环境强相关特征（写入时自动判 scope；零 LLM）
_ENV_SCOPE_RE = re.compile(
    "本机|[A-Za-z]:[\\\\/]|C[:/\\\\]+Users|zhengzefeng|32726|主机名|hostname|"
    "ThinkBook|Thinkbook|285[Kk]|i5-?10400|工作机|Anaconda3|工号|电脑配置|这台|那台"
)

def infer_scope(title, body):
    """适用范围推断：命中环境特征 → scope:device，否则 scope:global。
    例：'F:\\Anaconda3 的坑'→device；'水文规范公式'→global。"""
    text = f"{title or ''}\n{body or ''}"
    return "scope:device" if _ENV_SCOPE_RE.search(text) else "scope:global"

def _git_root(start=None, max_up=12):
    """向上找 .git（目录或文件）返回仓库根；没有则 None。仅 stat 调用，µs 级。"""
    d = os.path.abspath(start or os.getcwd())
    for _ in range(max_up):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None

def _context_tags(cwd=None):
    """capture/add 自动附加的溯源标签：project:<git根目录名> + agent:<来源> + device:<设备名>。
    无 git 则不打 project 标签（避免把 repositories 这类杂目录名当项目）。"""
    tags = []
    root = _git_root(cwd)
    if root:
        name = os.path.basename(root).strip()
        if name:
            tags.append(f"project:{name}")
    env = os.environ
    for tag, test in _AGENT_ENV:
        try:
            if test(env):
                tags.append(tag)
                break
        except Exception:
            pass
    tags.append(f"device:{get_device_name()}")
    return tags

def _merge_context_tags(tags):
    """溯源标签并入 tags：去重、用户标签在前、溯源置尾。"""
    out = [t for t in (tags or []) if t and t.strip()]
    for ct in _context_tags():
        if ct not in out:
            out.append(ct)
    return out

# ---- 命令实现 ----

def _resolve_entry_path(ref):
    """<arg_value><b88a6f17>按 uid 或相对 path 解析条目文件绝对路径；找不到返回 None。零 LLM。"""
    conn = get_conn(); init_schema(conn)
    row = conn.execute("SELECT path FROM entries WHERE uid=? OR path=? LIMIT 1", (ref, ref)).fetchone()
    conn.close()
    if row:
        fp = os.path.join(BANK_DIR, row[0])
        return fp if os.path.exists(fp) else None
    # 文件系统兜底：手写/未重建索引的条目也能被 --supersedes 引用（防越界：必须仍在 bank 内）
    if isinstance(ref, str) and ref.strip() and not os.path.isabs(ref):
        fp = os.path.abspath(os.path.join(BANK_DIR, ref.replace("\\", "/")))
        if fp.startswith(os.path.abspath(BANK_DIR) + os.sep) and os.path.isfile(fp):
            return fp
    return None

def _apply_supersedes(old_ref, new_rel):
    """旧条目标记 invalidated + superseded_by=new_rel（ADR-0001 演化路径）。返回 (ok, msg)。"""
    fp = _resolve_entry_path(old_ref)
    if not fp:
        return False, f"未找到被替代条目: {old_ref}"
    with open(fp, encoding="utf-8") as f:
        text = f.read()
    meta, body = parse_frontmatter(text)
    meta["status"] = "invalidated"
    meta["superseded_by"] = new_rel
    meta["updated_at"] = now_str()
    with open(fp, "w", encoding="utf-8") as f:
        f.write(build_frontmatter(meta, body))
    conn = get_conn(); init_schema(conn)
    index_file(conn, fp, meta, body)
    conn.commit(); conn.close()
    return True, f"旧条目已失效并指向新条目: {entry_rel_path(fp)} → {new_rel}"

def _build_meta(title, tags, category, confidence, source, secret, mtype, pinned):
    return {
        "title": title,
        "tags": tags,
        "category": category,
        "confidence": confidence,
        "verified_at": datetime.date.today().isoformat(),
        "hits": 0,
        "status": "active",
        "source": source or f"session@{datetime.date.today().isoformat()}",
        "updated_at": now_str(),
        "conflicts": [],
        "type": mtype,
        "pinned": bool(pinned),
        "secret": bool(secret),
        "last_accessed": "",
        "superseded_by": "",
    }

def _write_entry(meta, body):
    """落盘 + 索引。返回 (rel_path, conn)。"""
    sub = CATEGORY_DIRS.get(meta["category"], ("lessons/patterns", "模式"))[0]
    d = os.path.join(BANK_DIR, sub)
    os.makedirs(d, exist_ok=True)
    # 毫秒级时间戳 + 短随机串，防止同秒多次写入互相覆盖（v2 自动捕获高频写入）
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    fn = f"{stamp}-{slugify(meta['title'])}.md"
    fp = os.path.join(d, fn)
    # 极端情况下仍冲突则追加随机后缀
    n = 0
    while os.path.exists(fp) and n < 10:
        fp = os.path.join(d, f"{stamp}-{n}-{slugify(meta['title'])}.md")
        n += 1
    with open(fp, "w", encoding="utf-8") as f:
        f.write(build_frontmatter(meta, body))
    conn = get_conn()
    init_schema(conn)
    index_file(conn, fp, meta, body)
    conn.commit()
    conn.close()
    return entry_rel_path(fp)

def cmd_add(args):
    """add — 支持极简降级: add "一句话"；低置信手动路径交互确认（Q6）。"""
    body = (args.body or args.text or "").strip()
    if not args.title and not body:
        print("错误：请提供正文（--body 或 stdin），或直接 add \"一句话\"")
        return 1

    title = args.title or ""
    if not title:
        # 极简降级：首句做 title
        first = next((l.strip() for l in body.split("\n") if l.strip()), "")
        title = first[:30] or "untitled"

    # 类型/分类/tags 各自独立推断（Q4/Q6）：显式指定优先，缺省自动
    if args.type:
        mtype = args.type
    else:
        mtype, tconf = infer_type(title, body, args.category)
        # Q6：低置信且手动路径（非 --auto）→ 交互确认一次
        if tconf == "low" and not args.auto and sys.stdin.isatty():
            try:
                r = input(f"推断类型 {mtype} 置信度低，确认? [fact/belief/preference/回车接受] ").strip()
                if r in ("fact", "belief", "preference"):
                    mtype = r
            except (EOFError, KeyboardInterrupt):
                pass
    category = args.category or infer_category(title, body)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else infer_tags(title, body)
    tags = _merge_context_tags(tags)  # v2.1 溯源标签（project/agent）+ v2.2 device
    scope = getattr(args, "scope", None) or infer_scope(title, body)  # v2.2 适用范围
    if scope not in tags:
        tags.append(scope)

    # 安全网①：写前拦截（Q7）
    ok, mark_msg = pre_write_security_check(title, body)
    if not ok:
        print(mark_msg)
        return 1
    secret = args.secret or bool(mark_msg)  # Mark 级自动标 secret
    if mark_msg and not args.secret:
        print(f"ℹ️ 检测到敏感内容({mark_msg})，已自动标记 secret（摘要隐藏/不发给LLM/get需--force）")

    meta = _build_meta(title, tags, category, args.confidence, args.source, secret, mtype, False)
    if args.secret and category == "knowledge":
        print("⚠️ 提醒：secret 条目不会出现在检索摘要，也不会发给 LLM 提炼")

    # v2.1 更正流：--supersedes 先验后写（目标不存在则拒绝，避免写完接不上链）
    sup_ref = getattr(args, "supersedes", None)
    if sup_ref and not _resolve_entry_path(sup_ref):
        print(f"错误：--supersedes 指向的条目不存在: {sup_ref}")
        return 1

    # 热路径冲突检测（v2.2：2-gram + 同名命中 + 正文指纹 + 演进引导）
    conn = get_conn(); init_schema(conn)
    body_dups = find_body_dup(body, conn)
    conflicts = find_local_conflicts(title, body, conn)
    evo_peers = conflicts or find_topic_peers_if_evolution(title, conn)  # 宽松同主题（仅触发词标题）
    conn.close()
    if body_dups:
        dup = body_dups[0]
        print(f"⚠️ 正文指纹与已有条目完全相同：{dup['title']} ({dup['path']})")
        if not args.auto and sys.stdin.isatty():
            try:
                r = input("疑似重复写入，仍要写入? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                r = "n"
            if r != "y":
                print("已取消写入（可用 update 补充旧条，或 --supersedes 走更正流）")
                return 2
    if conflicts:
        if not args.auto and sys.stdin.isatty():
            print("⚠️ 发现可能重复/冲突的已有条目:")
            for c in conflicts:
                print(f"   {c['overlap']:.0%}  {c['path']}  [{c['type']}] {c['title']}")
            hint = suggest_supersedes(title, conflicts)
            if hint:
                print(hint)
            try:
                r = input("仍要写入? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                r = "n"
            if r != "y":
                print("已取消写入")
                return 2
        else:
            n_foreign = sum(1 for c in conflicts if _is_foreign_device(c.get("tags", "")))
            foreign_note = f"（含 {n_foreign} 条他机专属，不适用本地 --supersedes/archive）" if n_foreign else ""
            print(f"ℹ️ 发现 {len(conflicts)} 条可能重复条目（如: {conflicts[0]['path']}），语义冲突由 consolidate 复核{foreign_note}")
    hint = suggest_supersedes(title, conflicts or evo_peers)
    if hint:
        print(hint)

    rel = _write_entry(meta, body)
    print(f"已添加: {rel}  type={meta['type']} category={meta['category']}" + (" 🔒secret" if secret else ""))
    if sup_ref:
        ok, msg = _apply_supersedes(sup_ref, rel)
        print(("✓ " + msg) if ok else ("⚠️ " + msg))
    return 0

def cmd_capture(args):
    """capture — 自动捕获（Q2）。--auto 永不交互；从参数/环境推断全部字段。"""
    body = (args.body or args.text or "").strip()
    if not body:
        print("错误：capture 需要正文（--body 或 stdin）")
        return 1
    title = args.title or next((l.strip() for l in body.split("\n") if l.strip()), "")[:30] or "capture"
    mtype, _ = infer_type(title, body, args.category)
    if args.type:
        mtype = args.type
    category = args.category or infer_category(title, body)
    tags = args.tags.split(",") if args.tags else infer_tags(title, body)
    tags = _merge_context_tags(tags)  # v2.1 溯源标签（project/agent）+ v2.2 device
    scope = getattr(args, "scope", None) or infer_scope(title, body)  # v2.2 适用范围
    if scope not in tags:
        tags.append(scope)

    ok, mark_msg = pre_write_security_check(title, body)
    if not ok:
        print(f"capture 已拦截：{mark_msg}")
        return 1
    secret = args.secret or bool(mark_msg)

    meta = _build_meta(title, tags, category, args.confidence, args.source, secret, mtype, False)
    # 热路径冲突检测：--auto 下仅提示不阻塞（ADR-0005）；v2.2 附正文指纹 + 演进引导
    conn = get_conn(); init_schema(conn)
    body_dups = find_body_dup(body, conn)
    conflicts = find_local_conflicts(title, body, conn)
    evo_peers = conflicts or find_topic_peers_if_evolution(title, conn)  # 宽松同主题（仅触发词标题）
    conn.close()
    if body_dups:
        print(f"⚠️ capture 正文指纹与已有条目完全相同：{body_dups[0]['title']} ({body_dups[0]['path']})——疑似重复导入，请复核")
    if conflicts:
        meta["conflicts"] = [c["path"] for c in conflicts]
        n_foreign = sum(1 for c in conflicts if _is_foreign_device(c.get("tags", "")))
        foreign_note = f"（含 {n_foreign} 条他机专属，不适用本地 --supersedes/archive）" if n_foreign else ""
        print(f"ℹ️ capture 发现 {len(conflicts)} 条可能重复（已记入 conflicts 字段，consolidate 复核）{foreign_note}")
    hint = suggest_supersedes(title, conflicts or evo_peers)
    if hint:
        print(hint)

    rel = _write_entry(meta, body)
    print(f"已捕获: {rel}  type={meta['type']} category={meta['category']}" + (" 🔒secret" if secret else ""))
    sup_ref = getattr(args, "supersedes", None)
    if sup_ref:
        ok, msg = _apply_supersedes(sup_ref, rel)
        print(("✓ " + msg) if ok else ("⚠️ " + msg))
    return 0

def cmd_search(args):
    conn = get_conn()
    init_schema(conn)
    conn = rebuild_if_empty(conn)
    q = (args.query or "").strip()
    limit = args.limit
    sql = "SELECT id, path, title, tags, category, confidence, hits, status, summary, verified_at, secret, type FROM entries WHERE status='active'"
    params = []
    if args.category:
        sql += " AND category=?"
        params.append(args.category)
    if args.tag:
        sql += " AND tags LIKE ?"
        params.append(f"%{args.tag}%")
    rows = conn.execute(sql, params).fetchall()
    scored = []
    if q:
        # 先试 FTS5（trigram 对 ≥3 字中文有效）
        try:
            fts = conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH ? LIMIT ?",
                               (q.replace('"', ''), limit * 3)).fetchall()
            fts_ids = {r[0] for r in fts}
        except sqlite3.OperationalError:
            fts_ids = None
        # LIKE 兜底（2 字中文查询 FTS 匹配不到；FTS5 表可直接 LIKE）
        try:
            like_rows = conn.execute(
                "SELECT rowid FROM entries_fts WHERE title LIKE ? OR body LIKE ? LIMIT ?",
                (f"%{q}%", f"%{q}%", limit * 3)).fetchall()
            like_ids = {r[0] for r in like_rows}
        except sqlite3.OperationalError:
            like_ids = None
        for r in rows:
            score = 0
            if fts_ids is not None and r[0] in fts_ids:
                score += 10
            if like_ids is not None and r[0] in like_ids:
                score += 4
            if q.lower() in r[2].lower():
                score += 5
            if r[8] and q.lower() in r[8].lower():
                score += 2
            if score:
                scored.append((score + r[6] * 0.1, r))
    else:
        scored = [(r[6] * 0.1, r) for r in rows]
    scored.sort(key=lambda x: -x[0])
    # v2.2 设备过滤：--device local 只看本机专属+global+未知来源；--device <name> 看指定设备
    local_dev = get_device_name()
    dev_filter = getattr(args, "device", None) or "all"
    if dev_filter != "all":
        want = local_dev if dev_filter == "local" else dev_filter
        kept = []
        for s, r in scored:
            scope, dv = _device_view(r[3])  # r[3]=tags
            if scope == "scope:device" and dv not in ("", want):
                continue  # 他机专属，过滤
            kept.append((s, r))
        scored = kept
    out = []
    for score, r in scored[:limit]:
        out.append({
            "id": r[0], "path": r[1], "title": r[2], "tags": r[3],
            "category": r[4], "confidence": r[5], "hits": r[6],
            "status": r[7], "summary": r[8], "verified_at": r[9], "secret": bool(r[10]),
            "type": r[11], "score": round(score, 1),
        })
    conn.close()

    # Q8: synthesize 综合检索（冷路径，LLM；强制来源标注）
    if args.synthesize:
        if not out:
            print("记忆库无此记录。（synthesize: 无命中，禁止脑补）")
            return 0
        return _synthesize(q, out)

    # v2.2 P2：同主题多版本标注（版本演进歧义在读取端可见；阈值0.45覆盖"同前缀演进链"）
    multi_groups = cluster_by_title(out, threshold=0.45)
    cluster_info = {}   # path -> {size, is_latest, latest_path}
    for g in multi_groups:
        for it in g["items"]:
            cluster_info[it["path"]] = {
                "size": len(g["items"]),
                "is_latest": it is g["latest"],
                "latest_path": g["latest"]["path"],
                "latest_title": g["latest"]["title"],
            }

    if args.json:
        for it in out:
            it["cluster"] = cluster_info.get(it["path"])
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if not out:
        print("(无匹配)")
        return 0
    if multi_groups:
        print(f"⚠️ 检出 {len(multi_groups)} 组同主题多版本并存（最新已标 ✅，旧版建议 archive 或 --supersedes 收编）：")
        for g in multi_groups:
            print(f"   • {g['latest']['title'][:36]} ← 共 {len(g['items'])} 版（{g['items'][0]['verified_at']} → {g['latest']['verified_at']}）")
        print()
    for it in out:
        flag = {"high": "", "medium": "", "low": "⚠"}.get(it["confidence"], "")
        secret_flag = " 🔒secret" if it.get("secret") else ""
        ci = cluster_info.get(it["path"])
        ver_flag = ""
        if ci:
            ver_flag = " ✅最新" if ci["is_latest"] else f" 🕘旧版(最新: {ci['latest_title'][:24]})"
        # v2.2 设备标注：他机专属经验提示验证；来源不明的环境条目提示
        dev_flag = ""
        scope, dv = _device_view(it.get("tags", ""))
        if scope == "scope:device" and dv and dv != local_dev:
            dev_flag = f" 🖥️他机经验({dv})，请验证适用性"
        elif scope == "scope:device" and not dv:
            dev_flag = " 🖥️?来源不明(环境相关)"
        print(f"[{it['score']:>5}] {it['category']}/{it['title']} {flag}{secret_flag}{ver_flag}{dev_flag}")
        print(f"       tags={it['tags']} hits={it['hits']} 验证={it['verified_at']} type={it['type']}")
        if it.get("secret"):
            print(f"       (敏感条目，正文已隐藏；get 需 --force)")
        elif it["summary"]:
            print(f"       {it['summary'][:60]}")
        print(f"       path={it['path']}")
    return 0

def _synthesize(q, hits):
    """LLM 综合回答。每条结论强制 [来源:path]；无命中在上层已拦截。"""
    cfg = load_config().get("synthesize", {})
    provider = cfg.get("llm", {})
    env = {}
    env_path = os.path.join(ROOT, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    api_key = env.get(provider.get("env_key", "DEEPSEEK_API_KEY"), "")
    if not api_key:
        print("⚠️ synthesize 需要 LLM 配置（config.json synthesize.llm + .env key）")
        print("降级为普通检索结果:")
        for it in hits:
            print(f"  [{it['score']:>5}] {it['category']}/{it['title']}  path={it['path']}")
        return 0
    brief = []
    for it in hits:
        fp = os.path.join(BANK_DIR, it["path"])
        try:
            with open(fp, encoding="utf-8") as f:
                body = f.read()
            _, body_txt = parse_frontmatter(body)
            brief.append({"path": it["path"], "title": it["title"], "body": body_txt[:500]})
        except OSError:
            brief.append({"path": it["path"], "title": it["title"], "body": "(读取失败)"})
    prompt = (
        "你是记忆库问答助手。基于以下记忆条目回答用户问题。\n"
        "硬性规则：\n"
        "1) 每条结论必须附 [来源:bank/xxx.md]；\n"
        "2) 记忆条目没有覆盖的部分，必须明确说'记忆库无此记录'，禁止脑补；\n"
        "3) 条目相互矛盾时，并列列出双方观点并标注类型(fact/belief)。\n"
        f"用户问题: {q}\n\n记忆条目:\n" + json.dumps(brief, ensure_ascii=False, indent=1)[:6000]
    )
    url = provider.get("base_url", "https://api.deepseek.com/v1").rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": provider.get("model", "deepseek-chat"),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": provider.get("max_tokens", 2000),
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(data["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"synthesize 调用失败: {e}")
        return 1
    return 0

def rebuild_if_empty(conn):
    n = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    if n == 0:
        conn.close()
        rebuild_index(verbose=False)
        return get_conn()
    return conn

def cmd_consolidate(args):
    """consolidate — 周整理入口（委托 scripts/consolidate.py）。独立进程 + 超时，永不挂起（ADR-0005 批处理路径）。"""
    import subprocess as sp
    script = os.path.join(SCRIPTS, "consolidate.py")
    if not os.path.exists(script):
        print(f"未找到 {script}")
        return 1
    timeout = args.timeout or (900 if args.mode == "llm" else 180)
    cmd = [sys.executable, script, "--mode", args.mode]
    if args.report:
        cmd.append("--report")
    print(f"→ consolidate --mode {args.mode}（超时 {timeout}s；auto=零LLM 周整理 / llm=批量提炼冷路径）", flush=True)
    try:
        # 不捕获输出：子进程直接写终端，进度实时可见，避免“沉默等待像卡死”
        r = sp.run(cmd, cwd=ROOT, timeout=timeout)
    except sp.TimeoutExpired:
        print(f"⚠️ consolidate 超时（>{timeout}s）已终止。可加大 --timeout 重试，或单独运行 scripts/consolidate.py")
        return 3
    except FileNotFoundError:
        print(f"错误：无法启动 Python: {sys.executable}")
        return 1
    if r.returncode != 0:
        print(f"consolidate 退出码 {r.returncode}")
    return r.returncode

def cmd_get(args):
    conn = get_conn()
    init_schema(conn)
    row = conn.execute("SELECT path, secret, type, pinned, superseded_by, status FROM entries WHERE uid=? OR path=? LIMIT 1",
                       (args.id, args.id)).fetchone()
    if not row:
        print(f"未找到: {args.id}")
        return 1
    path, secret, mtype, pinned, superseded_by, status = row
    if secret and not args.force:
        print(f"🔒 敏感条目（secret），读取需确认: --force")
        print(f"   路径: {path}")
        return 2
    fp = os.path.join(BANK_DIR, path)
    with open(fp, encoding="utf-8") as f:
        text = f.read()
    # Q9: 读取信号（双信号之一）
    conn.execute("UPDATE entries SET hits=hits+1, last_accessed=? WHERE path=?", (now_str(), path))
    conn.commit()
    conn.close()
    print(text)
    if status == "invalidated":
        print(f"\n⚠️ 此条目已被标记失效（invalidated）")
    if superseded_by:
        print(f"\nℹ️ 此认知已被 {superseded_by} 替代（沿革链，见 ADR-0001）")
    if mtype:
        print(f"ℹ️ type={mtype}" + (" 📌pinned" if pinned else ""))
    return 0

def cmd_update(args):
    conn = get_conn()
    init_schema(conn)
    row = conn.execute("SELECT path FROM entries WHERE uid=? OR path=? LIMIT 1", (args.id, args.id)).fetchone()
    if not row:
        print(f"未找到: {args.id}")
        return 1
    fp = os.path.join(BANK_DIR, row[0])
    with open(fp, encoding="utf-8") as f:
        text = f.read()
    meta, body = parse_frontmatter(text)
    if args.hits:
        meta["hits"] = int(meta.get("hits", 0)) + 1
    if args.status:
        meta["status"] = args.status
    if args.confidence:
        meta["confidence"] = args.confidence
    if args.type:
        meta["type"] = args.type
    if args.pin is not None:
        meta["pinned"] = args.pin
    if args.invalidate:
        meta["status"] = "invalidated"
    if args.supersede:
        meta["superseded_by"] = args.supersede
    meta["updated_at"] = now_str()
    with open(fp, "w", encoding="utf-8") as f:
        f.write(build_frontmatter(meta, body))
    index_file(conn, fp, meta, body)
    conn.commit()
    conn.close()
    print(f"已更新: {entry_rel_path(fp)} (type={meta.get('type')}, status={meta.get('status')}, hits={meta.get('hits')})")
    return 0

def cmd_archive(args):
    conn = get_conn()
    init_schema(conn)
    row = conn.execute("SELECT path FROM entries WHERE uid=? OR path=? LIMIT 1", (args.id, args.id)).fetchone()
    if not row:
        print(f"未找到: {args.id}")
        return 1
    fp = os.path.join(BANK_DIR, row[0])
    with open(fp, encoding="utf-8") as f:
        text = f.read()
    meta, body = parse_frontmatter(text)
    meta["status"] = "archived"
    meta["updated_at"] = now_str()
    with open(fp, "w", encoding="utf-8") as f:
        f.write(build_frontmatter(meta, body))
    index_file(conn, fp, meta, body)
    conn.commit()
    conn.close()
    print(f"已归档: {entry_rel_path(fp)}")
    return 0

def cmd_list(args):
    conn = get_conn()
    init_schema(conn)
    sql = "SELECT path, title, category, hits, status, verified_at, type, pinned FROM entries WHERE 1=1"
    params = []
    if args.category:
        sql += " AND category=?"
        params.append(args.category)
    if args.status:
        sql += " AND status=?"
        params.append(args.status)
    else:
        sql += " AND status IN ('active','invalidated')"
    sql += " ORDER BY hits DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    print(f"共 {len(rows)} 条:")
    for r in rows:
        pin = " 📌" if r[7] else ""
        print(f"  [{r[4]:>10}] {r[0]}  hits={r[3]}  验证={r[5]} type={r[6]}{pin}")
    return 0

def cmd_health(args):
    conn = get_conn()
    init_schema(conn)
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM entries WHERE status='active'").fetchone()[0]
    invalidated = conn.execute("SELECT COUNT(*) FROM entries WHERE status='invalidated'").fetchone()[0]
    by_cat = conn.execute("SELECT category, COUNT(*) FROM entries GROUP BY category").fetchall()
    by_type = conn.execute("SELECT type, COUNT(*) FROM entries GROUP BY type").fetchall()
    total_hits = conn.execute("SELECT COALESCE(SUM(hits),0) FROM entries").fetchone()[0]
    low_conf = conn.execute("SELECT COUNT(*) FROM entries WHERE confidence='low' AND status='active'").fetchone()[0]
    pinned = conn.execute("SELECT COUNT(*) FROM entries WHERE pinned=1").fetchone()[0]
    secret = conn.execute("SELECT COUNT(*) FROM entries WHERE secret=1").fetchone()[0]
    # v2.2 一致性治理指标：同主题多版本组 + 待复核 conflicts + 他机环境条目
    active_rows = conn.execute("SELECT path, title, verified_at, tags FROM entries WHERE status='active'").fetchall()
    multi_groups = cluster_by_title([{"path": r[0], "title": r[1], "verified_at": r[2]} for r in active_rows], threshold=0.75)
    local_dev = get_device_name()
    foreign = sum(1 for r in active_rows if _is_foreign_device(r[3], local_dev))
    conflicts_pending = 0
    for fp in all_entry_files():
        try:
            with open(fp, encoding="utf-8") as f:
                meta, _ = parse_frontmatter(f.read())
            cf = meta.get("conflicts")
            if cf and meta.get("status") == "active" and (isinstance(cf, list) and len(cf) or isinstance(cf, str) and cf):
                conflicts_pending += 1
        except Exception:
            pass
    conn.close()
    files = len(all_entry_files())
    print("===== AgentMemory 健康报告 (v2) =====")
    print(f"  记忆条目总数 : {files} (索引 {total})")
    print(f"  活跃条目     : {active} | 失效 {invalidated} | 归档(索引外)")
    print(f"  钉住常驻     : {pinned} | 敏感标记 {secret}")
    print(f"  累计命中次数 : {total_hits}")
    print(f"  低置信度活跃 : {low_conf}")
    print(f"  当前设备     : {local_dev}")
    print(f"  同主题多版本 : {len(multi_groups)} 组（建议 archive 旧版或 --supersedes 收编）")
    print(f"  待复核冲突   : {conflicts_pending} 条（frontmatter conflicts 非空，跑 consolidate auto/llm 复核）")
    print(f"  他机环境条目 : {foreign} 条（scope:device 且来源≠本机；检索标 🖥，PRELOAD 已过滤）")
    print("  分类分布:")
    for c, n in by_cat:
        print(f"    {c:<12} {n}")
    print("  类型分布:")
    for t, n in by_type:
        print(f"    {t:<12} {n}")
    return 0

def main():
    p = argparse.ArgumentParser(prog="memory_tool", description="AgentMemory 记忆库工具 v2")
    sub = p.add_subparsers(dest="cmd")

    pa = sub.add_parser("add", help="添加记忆（支持极简降级: add \"一句话\"）")
    pa.add_argument("text", nargs="?", help="极简模式：一句话（用作正文，首句做标题）")
    pa.add_argument("--title", help="标题（缺省取正文首句）")
    pa.add_argument("--tags", default="")
    pa.add_argument("--category", choices=list(CATEGORY_DIRS), help="缺省自动推断")
    pa.add_argument("--type", choices=["fact", "belief", "preference"], help="缺省自动推断")
    pa.add_argument("--confidence", choices=["high", "medium", "low"], default="medium")
    pa.add_argument("--source")
    pa.add_argument("--body")
    pa.add_argument("--secret", action="store_true", help="标记为敏感条目")
    pa.add_argument("--auto", action="store_true", help="自动模式：永不交互（自动捕获用）")
    pa.add_argument("--supersedes", help="v2.1 更正流：被本条替代的旧条目 id/path（写入后自动标 invalidated+superseded_by）")
    pa.add_argument("--scope", choices=["scope:device", "scope:global"], help="v2.2 适用范围（缺省按正文环境特征自动推断）")
    pa.set_defaults(func=cmd_add)

    pc = sub.add_parser("capture", help="自动捕获（agent 会话中调用，--auto 语义）")
    pc.add_argument("text", nargs="?", help="极简模式：一句话")
    pc.add_argument("--title")
    pc.add_argument("--tags", default="")
    pc.add_argument("--category", choices=list(CATEGORY_DIRS))
    pc.add_argument("--type", choices=["fact", "belief", "preference"])
    pc.add_argument("--confidence", choices=["high", "medium", "low"], default="medium")
    pc.add_argument("--source")
    pc.add_argument("--body")
    pc.add_argument("--secret", action="store_true")
    pc.add_argument("--auto", action="store_true", help="自动模式：永不交互（默认即此语义）")
    pc.add_argument("--supersedes", help="v2.1 更正流：被本条替代的旧条目 id/path（写入后自动标 invalidated+superseded_by）")
    pc.add_argument("--scope", choices=["scope:device", "scope:global"], help="v2.2 适用范围（缺省按正文环境特征自动推断）")
    pc.set_defaults(func=cmd_capture)

    ps = sub.add_parser("search", help="检索记忆")
    ps.add_argument("query", nargs="?")
    ps.add_argument("--category")
    ps.add_argument("--tag")
    ps.add_argument("--limit", type=int, default=20)
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--synthesize", action="store_true", help="LLM 综合回答（冷路径，强制来源标注）")
    ps.add_argument("--device", default="all", help="v2.2 设备过滤：local=本机专属+global+未知来源；all=全部（默认）；或指定设备名")
    ps.set_defaults(func=cmd_search)

    pcon = sub.add_parser("consolidate", help="周整理（auto=零LLM规则整理 / llm=批量提炼；独立进程+超时防挂起）")
    pcon.add_argument("--mode", choices=["auto", "llm"], default="auto")
    pcon.add_argument("--report", action="store_true")
    pcon.add_argument("--timeout", type=int, default=0, help="秒；0=按模式取默认（auto 180 / llm 900）")
    pcon.set_defaults(func=cmd_consolidate)

    pg = sub.add_parser("get", help="读取全文")
    pg.add_argument("id")
    pg.add_argument("--force", action="store_true", help="读取 secret 条目时确认")
    pg.set_defaults(func=cmd_get)

    pu = sub.add_parser("update", help="更新")
    pu.add_argument("id")
    pu.add_argument("--hits", action="store_true")
    pu.add_argument("--status", choices=["active", "archived", "obsolete", "invalidated"])
    pu.add_argument("--confidence", choices=["high", "medium", "low"])
    pu.add_argument("--type", choices=["fact", "belief", "preference"])
    pu.add_argument("--pin", type=lambda x: x.lower() in ("1", "true", "yes", "y"), help="钉住常驻（PRELOAD 必含）")
    pu.add_argument("--invalidate", action="store_true", help="标记失效（认知被推翻）")
    pu.add_argument("--supersede", metavar="PATH", help="沿革链：本条目被 PATH 替代")
    pu.set_defaults(func=cmd_update)

    par_arch = sub.add_parser("archive", help="归档")
    par_arch.add_argument("id")
    par_arch.set_defaults(func=cmd_archive)
    par = sub.add_parser("list", help="列出")
    par.add_argument("--category")
    par.add_argument("--status")
    par.set_defaults(func=cmd_list)

    ph = sub.add_parser("health", help="健康报告")
    ph.set_defaults(func=cmd_health)

    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
        return 1
    return args.func(args)

if __name__ == "__main__":
    _utf8("")
    sys.exit(main())
