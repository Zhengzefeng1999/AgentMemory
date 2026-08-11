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
  daemon        启动常驻本地服务（快速写入 + PRELOAD + 飞书桥）

v2 变更（见 docs/adr/）:
  - 条目类型 type: fact/belief/preference（Q4 自动推断，可覆盖）
  - 极简 add: 无参数时 title=首句、category/tags/type 自动推断（Q6）
  - capture --auto: 自动捕获，永不交互（Q2）
  - 写前敏感拦截: Block 拒绝 / Mark 自动标 secret（Q7 防线①）
  - 写入时本地相似度冲突检测（热路径零 LLM，Q3/Q5）
  - search --synthesize: LLM 综合回答 + 强制来源标注（Q8，冷路径）
  - get 刷新 last_accessed（Q9 双信号读取侧）
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
        pinned INTEGER DEFAULT 0, last_accessed TEXT DEFAULT '', superseded_by TEXT DEFAULT '') """)
    # v2: 兼容旧库补列
    for col, ddl in [
        ("type", "TEXT DEFAULT 'belief'"),
        ("pinned", "INTEGER DEFAULT 0"),
        ("last_accessed", "TEXT DEFAULT ''"),
        ("superseded_by", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass  # 列已存在
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
        "INSERT OR REPLACE INTO entries(uid, path, title, tags, category, confidence, verified_at, hits, status, source, updated_at, summary, secret, type, pinned, last_accessed, superseded_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, rel, meta.get("title", ""), tags, meta.get("category", ""),
         meta.get("confidence", "medium"), meta.get("verified_at", ""),
         int(meta.get("hits", 0)), meta.get("status", "active"),
         meta.get("source", ""), meta.get("updated_at", now_str()), summary, secret,
         meta.get("type", "belief"), 1 if meta.get("pinned") else 0,
         meta.get("last_accessed", ""), meta.get("superseded_by", "")))
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

# ---- 冲突检测（热路径本地版，Q3） ----

def _norm(s):
    return re.sub(r"[\s，。！？、,.;:：()（）\-—_\"'《》<>]+", "", str(s).lower())

def find_local_conflicts(title, body, conn, threshold=0.6):
    """本地相似度冲突检测：标题/正文关键词重叠。返回候选条目路径列表。
    零 LLM、零网络（ADR-0005）。语义级检测留给 consolidate。"""
    ntitle = _norm(title)
    if len(ntitle) < 4:
        return []
    rows = conn.execute(
        "SELECT path, title, type, status FROM entries WHERE status IN ('active','invalidated')"
    ).fetchall()
    cands = []
    for path, t, typ, status in rows:
        nt = _norm(t)
        if not nt:
            continue
        # 标题重叠率
        overlap = len(set(ntitle) & set(nt)) / max(len(set(ntitle) | set(nt)), 1)
        if overlap >= threshold and nt != ntitle:
            cands.append({"path": path, "title": t, "type": typ, "status": status, "overlap": round(overlap, 2)})
    cands.sort(key=lambda x: -x["overlap"])
    return cands[:3]

# ---- 命令实现 ----

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

    # 热路径冲突检测（本地相似度）
    conn = get_conn(); init_schema(conn)
    conflicts = find_local_conflicts(title, body, conn)
    conn.close()
    if conflicts:
        if not args.auto and sys.stdin.isatty():
            print("⚠️ 发现可能重复/冲突的已有条目:")
            for c in conflicts:
                print(f"   {c['overlap']:.0%}  {c['path']}  [{c['type']}] {c['title']}")
            try:
                r = input("仍要写入? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                r = "n"
            if r != "y":
                print("已取消写入")
                return 2
        else:
            print(f"ℹ️ 发现 {len(conflicts)} 条可能重复条目（如: {conflicts[0]['path']}），语义冲突由 consolidate 复核")

    rel = _write_entry(meta, body)
    print(f"已添加: {rel}  type={meta['type']} category={meta['category']}" + (" 🔒secret" if secret else ""))
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

    ok, mark_msg = pre_write_security_check(title, body)
    if not ok:
        print(f"capture 已拦截：{mark_msg}")
        return 1
    secret = args.secret or bool(mark_msg)

    meta = _build_meta(title, tags, category, args.confidence, args.source, secret, mtype, False)
    # 热路径冲突检测：--auto 下仅提示不阻塞（ADR-0005）
    conn = get_conn(); init_schema(conn)
    conflicts = find_local_conflicts(title, body, conn)
    conn.close()
    if conflicts:
        meta["conflicts"] = [c["path"] for c in conflicts]
        print(f"ℹ️ capture 发现 {len(conflicts)} 条可能重复（已记入 conflicts 字段，consolidate 复核）")

    rel = _write_entry(meta, body)
    print(f"已捕获: {rel}  type={meta['type']} category={meta['category']}" + (" 🔒secret" if secret else ""))
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
        try:
            fts = conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH ? LIMIT ?",
                               (q.replace('"', ''), limit * 3)).fetchall()
            fts_ids = {r[0] for r in fts}
        except sqlite3.OperationalError:
            fts_ids = None
        for r in rows:
            score = 0
            if fts_ids is not None and r[0] in fts_ids:
                score += 10
            if q.lower() in r[2].lower():
                score += 5
            if r[8] and q.lower() in r[8].lower():
                score += 2
            if score:
                scored.append((score + r[6] * 0.1, r))
    else:
        scored = [(r[6] * 0.1, r) for r in rows]
    scored.sort(key=lambda x: -x[0])
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
    conn.close()
    files = len(all_entry_files())
    print("===== AgentMemory 健康报告 (v2) =====")
    print(f"  记忆条目总数 : {files} (索引 {total})")
    print(f"  活跃条目     : {active} | 失效 {invalidated} | 归档(索引外)")
    print(f"  钉住常驻     : {pinned} | 敏感标记 {secret}")
    print(f"  累计命中次数 : {total_hits}")
    print(f"  低置信度活跃 : {low_conf}")
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
    pc.set_defaults(func=cmd_capture)

    ps = sub.add_parser("search", help="检索记忆")
    ps.add_argument("query", nargs="?")
    ps.add_argument("--category")
    ps.add_argument("--tag")
    ps.add_argument("--limit", type=int, default=20)
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--synthesize", action="store_true", help="LLM 综合回答（冷路径，强制来源标注）")
    ps.set_defaults(func=cmd_search)

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
