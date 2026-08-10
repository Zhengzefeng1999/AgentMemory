#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory memory_tool — 记忆库读写检索工具（纯 Python 标准库）
用法:
  add      添加记忆条目
  search   检索（本地 SQLite FTS，零 LLM token）
  get      读取全文
  update   更新条目（hits+1 / 内容修改 / 状态）
  archive  归档条目
  list     列出分类下条目
  health   记忆库健康报告
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
BANK_DIR = os.path.join(ROOT, "bank")
INDEX_DB = os.path.join(BANK_DIR, "INDEX.db")

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
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def slugify(title, maxlen=24):
    s = re.sub(r"[^\w\u4e00-\u9fff-]", "-", title.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "untitled"

# ---- Frontmatter 解析 ----

def parse_frontmatter(text):
    """解析 --- yaml --- 头。返回 (meta dict, body str)。YAML 子集解析。"""
    meta, body = {}, ""
    if not text.startswith("---"):
        return meta, text
    lines = text.split("\n", 12)
    # 找结束的 ---
    end = None
    for i in range(1, len(lines)):
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

def all_entry_files():
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
    return conn

def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS entries(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE, path TEXT, title TEXT, tags TEXT, category TEXT,
        confidence TEXT, verified_at TEXT, hits INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active', source TEXT, updated_at TEXT, summary TEXT,
        secret INTEGER DEFAULT 0) """)
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
        "INSERT OR REPLACE INTO entries(uid, path, title, tags, category, confidence, verified_at, hits, status, source, updated_at, summary, secret) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, rel, meta.get("title", ""), tags, meta.get("category", ""),
         meta.get("confidence", "medium"), meta.get("verified_at", ""),
         int(meta.get("hits", 0)), meta.get("status", "active"),
         meta.get("source", ""), meta.get("updated_at", now_str()), summary, secret))
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
        conn.execute("DELETE FROM entries_fts")
    except sqlite3.OperationalError:
        pass
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

# ---- 命令实现 ----

def cmd_add(args):
    meta = {
        "title": args.title,
        "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
        "category": args.category,
        "confidence": args.confidence,
        "verified_at": datetime.date.today().isoformat(),
        "hits": 0,
        "status": "active",
        "source": args.source or f"session@{datetime.date.today().isoformat()}",
        "updated_at": now_str(),
        "conflicts": [],
    }
    if args.secret:
        meta["secret"] = True
    if args.secret and args.category == "knowledge":
        print("⚠️ 提醒：secret 条目不会出现在检索摘要，也不会发给 LLM 提炼")
    body = args.body if args.body else sys.stdin.read().strip()
    if not body:
        print("错误：正文为空")
        return 1
    sub = CATEGORY_DIRS.get(args.category, ("lessons/patterns", "模式"))[0]
    d = os.path.join(BANK_DIR, sub)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    fn = f"{stamp}-{slugify(args.title)}.md"
    fp = os.path.join(d, fn)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(build_frontmatter(meta, body))
    # 更新索引
    conn = get_conn()
    init_schema(conn)
    index_file(conn, fp, meta, body)
    conn.commit()
    conn.close()
    print(f"已添加: {entry_rel_path(fp)}")
    return 0

def cmd_search(args):
    conn = get_conn()
    init_schema(conn)
    conn = rebuild_if_empty(conn)
    q = args.query.strip()
    limit = args.limit
    # 元数据过滤（先小表筛，再用 FTS/ LIKE）
    sql = "SELECT id, path, title, tags, category, confidence, hits, status, summary, verified_at, secret FROM entries WHERE status='active'"
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
        # 先试 FTS5
        try:
            fts = conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH ? LIMIT ?",
                               (q.replace('"', ''), limit * 3)).fetchall()
            fts_ids = {r[0] for r in fts}
        except sqlite3.OperationalError:
            fts_ids = None
        for r in rows:
            # secret 条目摘要隐藏：标题保留，正文摘要不显示
            score = 0
            if fts_ids is not None and r[0] in fts_ids:
                score += 10
            if q.lower() in r[2].lower():
                score += 5
            if r[8] and q.lower() in r[8].lower():
                score += 2
            if score:
                scored.append((score + r[6] * 0.1, r))  # hits 加权
    else:
        scored = [(r[6] * 0.1, r) for r in rows]
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, r in scored[:limit]:
        out.append({
            "id": r[0], "path": r[1], "title": r[2], "tags": r[3],
            "category": r[4], "confidence": r[5], "hits": r[6],
            "status": r[7], "summary": r[8], "verified_at": r[9], "secret": bool(r[10]),
            "score": round(score, 1),
        })
    conn.close()
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if not out:
        print("(无匹配)")
        return 0
    for it in out:
        flag = {"high": "", "medium": "", "low": "⚠"}.get(it["confidence"], "")
        secret_flag = " 🔒secret" if it.get("secret") else ""
        print(f"[{it['score']:>5}] {it['category']}/{it['title']} {flag}{secret_flag}")
        print(f"       tags={it['tags']} hits={it['hits']} 验证={it['verified_at']}")
        if it.get("secret"):
            print(f"       (敏感条目，正文已隐藏；get 需 --force)")
        elif it["summary"]:
            print(f"       {it['summary'][:60]}")
        print(f"       path={it['path']}")
    return 0

def rebuild_if_empty(conn):
    n = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    if n == 0:
        conn.close()
        rebuild_index(verbose=False)
        return get_conn()
    return conn

def cmd_get(args):
    conn = get_conn()
    init_schema(conn)
    row = conn.execute("SELECT path, secret FROM entries WHERE uid=? OR path=? LIMIT 1", (args.id, args.id)).fetchone()
    if not row:
        print(f"未找到: {args.id}")
        return 1
    if row[1] and not args.force:
        print(f"🔒 敏感条目（secret），读取需确认: --force")
        print(f"   路径: {row[0]}")
        return 2
    fp = os.path.join(BANK_DIR, row[0])
    with open(fp, encoding="utf-8") as f:
        print(f.read())
    # 命中+1
    conn.execute("UPDATE entries SET hits=hits+1 WHERE path=?", (row[0],))
    conn.commit()
    conn.close()
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
    changed = False
    if args.hits:
        meta["hits"] = int(meta.get("hits", 0)) + 1
        changed = True
    if args.status:
        meta["status"] = args.status
        changed = True
    if args.confidence:
        meta["confidence"] = args.confidence
        changed = True
    meta["updated_at"] = now_str()
    with open(fp, "w", encoding="utf-8") as f:
        f.write(build_frontmatter(meta, body))
    index_file(conn, fp, meta, body)
    conn.commit()
    conn.close()
    print(f"已更新: {entry_rel_path(fp)} (hits={meta.get('hits')}, status={meta.get('status')})")
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
    sql = "SELECT path, title, category, hits, status, verified_at FROM entries WHERE 1=1"
    params = []
    if args.category:
        sql += " AND category=?"
        params.append(args.category)
    if args.status:
        sql += " AND status=?"
        params.append(args.status)
    else:
        sql += " AND status='active'"
    sql += " ORDER BY hits DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    print(f"共 {len(rows)} 条:")
    for r in rows:
        print(f"  [{r[4]:>7}] {r[0]}  hits={r[3]}  验证={r[5]}")
    return 0

def cmd_health(args):
    conn = get_conn()
    init_schema(conn)
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM entries WHERE status='active'").fetchone()[0]
    by_cat = conn.execute("SELECT category, COUNT(*) FROM entries GROUP BY category").fetchall()
    total_hits = conn.execute("SELECT COALESCE(SUM(hits),0) FROM entries").fetchone()[0]
    low_conf = conn.execute("SELECT COUNT(*) FROM entries WHERE confidence='low' AND status='active'").fetchone()[0]
    conn.close()
    files = len(all_entry_files())
    print("===== AgentMemory 健康报告 =====")
    print(f"  记忆条目总数 : {files} (索引 {total})")
    print(f"  活跃条目     : {active}")
    print(f"  累计命中次数 : {total_hits}")
    print(f"  低置信度活跃 : {low_conf}")
    print("  分类分布:")
    for c, n in by_cat:
        print(f"    {c:<12} {n}")
    return 0

def main():
    p = argparse.ArgumentParser(prog="memory_tool", description="AgentMemory 记忆库工具")
    sub = p.add_subparsers(dest="cmd")

    pa = sub.add_parser("add", help="添加记忆")
    pa.add_argument("--title", required=True)
    pa.add_argument("--tags", default="")
    pa.add_argument("--category", choices=list(CATEGORY_DIRS), default="patterns")
    pa.add_argument("--confidence", choices=["high", "medium", "low"], default="medium")
    pa.add_argument("--source")
    pa.add_argument("--body")
    pa.add_argument("--secret", action="store_true", help="标记为敏感条目（摘要隐藏/不发给LLM/get需--force）")
    pa.set_defaults(func=cmd_add)

    ps = sub.add_parser("search", help="检索记忆")
    ps.add_argument("query", nargs="?")
    ps.add_argument("--category")
    ps.add_argument("--tag")
    ps.add_argument("--limit", type=int, default=20)
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_search)

    pg = sub.add_parser("get", help="读取全文")
    pg.add_argument("id")
    pg.add_argument("--force", action="store_true", help="读取 secret 条目时确认")
    pg.set_defaults(func=cmd_get)

    pu = sub.add_parser("update", help="更新")
    pu.add_argument("id")
    pu.add_argument("--hits", action="store_true")
    pu.add_argument("--status", choices=["active", "archived", "obsolete"])
    pu.add_argument("--confidence", choices=["high", "medium", "low"])
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

    # archive 的 func 绑定
    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
        return 1
    return args.func(args)

if __name__ == "__main__":
    _utf8("")
    sys.exit(main())
