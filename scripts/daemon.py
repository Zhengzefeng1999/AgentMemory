#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory daemon — 本地常驻服务 v2（ADR-0004：一进程三用）

  1. 快速写入/检索: 消灭每次 spawn 的首次 SQLite 提交开销（1306ms → ~10ms）
  2. PRELOAD 生成:   定时重建预热索引（pin 常驻 + 近 7 天，≤200 行）
  3. 飞书桥:         可选 --with-bridge 时接收飞书消息（记:/查:）

用法:
  python scripts/daemon.py                 # 默认 :8123
  python scripts/daemon.py --port 8124
  python scripts/daemon.py --with-bridge   # 同时启动飞书桥（需 config.json 配飞书凭据）

API:
  POST /memory     {"cmd":"add","body":"...","title":"...","auto":true}
  POST /memory     {"cmd":"search","query":"...","synthesize":true}
  POST /memory     {"cmd":"get","id":"bank/xxx.md","force":true}
  GET  /health
  GET  /preload    （重新生成 PRELOAD.md 并返回）
  GET  /feishu/status

热路径 = 零 LLM（ADR-0005）；synthesize 是冷路径（用户主动触发）。
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import memory_tool as mt  # noqa: E402
from build_preload import build_preload  # noqa: E402

CONFIG = mt.load_config()

# ---- 飞书桥（可选） ----

def feishu_send(chat_id, text, cfg):
    """通过飞书开放平台 API 发送消息（纯标准库）。"""
    import urllib.request
    base = cfg.get("base_url", "https://open.feishu.cn/open-apis")
    app_id, app_secret = cfg.get("app_id", ""), cfg.get("app_secret", "")
    if not app_id or not app_secret:
        return False, "未配置飞书 app_id/app_secret"
    # 1. tenant_access_token
    req = urllib.request.Request(base + "/auth/v3/tenant_access_token/internal",
                                 data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        token = json.loads(resp.read().decode())["tenant_access_token"]
    # 2. 发送消息
    req = urllib.request.Request(base + "/im/v1/messages?receive_id_type=chat_id",
                                 data=json.dumps({
                                     "receive_id": chat_id,
                                     "msg_type": "text",
                                     "content": json.dumps({"text": text}, ensure_ascii=False),
                                 }).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return data.get("code") == 0, data.get("msg", "")

def feishu_handle_message(msg_text, reply_to=None, cfg=None):
    """飞书消息路由：记:/查: 前缀。返回回复文本。"""
    text = (msg_text or "").strip()
    if not text:
        return "空消息。用法: 记：xxx 或 查：xxx"
    cfg = cfg or {}
    if text.startswith(("记", "记：", "记:")):
        body = text.split("：", 1)[-1] if "：" in text else (text.split(":", 1)[-1] if ":" in text else text[1:].strip())
        body = body or text
        if len(body) < 3:
            return "内容太短"
        try:
            rel = _daemon_add({"body": body, "auto": True})
            return f"✅ 已记入: {rel}"
        except SystemExit as e:
            return f"❌ {e}"
    if text.startswith(("查", "查：", "查:")):
        q = text.split("：", 1)[-1] if "：" in text else (text.split(":", 1)[-1] if ":" in text else text[1:].strip())
        q = q or text
        if len(q) < 2:
            return "查询词太短"
        hits = _daemon_search(q, limit=5)
        if not hits:
            return "记忆库无此记录。"
        lines = ["📚 记忆库命中："]
        for it in hits:
            lines.append(f"- [{it['score']}] {it['category']}/{it['title']} type={it.get('type','')}")
            lines.append(f"  path={it['path']}")
        return "\n".join(lines)
    return "用法：`记：xxx` 写入 / `查：xxx` 检索"

# ---- daemon 核心 ----

def _daemon_add(payload):
    """进程内写入（无 spawn，无交互）。返回相对路径。"""
    body = (payload.get("body") or "").strip()
    title = (payload.get("title") or "").strip()
    if not body and not title:
        raise SystemExit("正文为空")
    if not title:
        first = next((l.strip() for l in body.split("\n") if l.strip()), "")
        title = first[:30] or "untitled"
    ok, mark_msg = mt.pre_write_security_check(title, body)
    if not ok:
        raise SystemExit(mark_msg)
    mtype = payload.get("type") or mt.infer_type(title, body, payload.get("category"))[0]
    category = payload.get("category") or mt.infer_category(title, body)
    tags = (payload.get("tags") or "").split(",") if payload.get("tags") else mt.infer_tags(title, body)
    secret = bool(payload.get("secret")) or bool(mark_msg)
    meta = mt._build_meta(title, tags, category, payload.get("confidence", "medium"),
                          payload.get("source"), secret, mtype, False)
    # 冲突检测：记录但不阻塞（自动路径，ADR-0005）
    conn = mt.get_conn(); mt.init_schema(conn)
    conflicts = mt.find_local_conflicts(title, body, conn)
    conn.close()
    if conflicts:
        meta["conflicts"] = [c["path"] for c in conflicts]
    return mt._write_entry(meta, body)

def _daemon_search(q, limit=20, synthesize=False):
    """进程内检索。返回 dict 列表。"""
    conn = mt.get_conn(); mt.init_schema(conn)
    sql = "SELECT id, path, title, tags, category, confidence, hits, status, summary, verified_at, secret, type FROM entries WHERE status='active'"
    rows = conn.execute(sql).fetchall()
    scored = []
    try:
        fts = conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH ? LIMIT ?",
                           (q.replace('"', ''), limit * 3)).fetchall()
        fts_ids = {r[0] for r in fts}
    except sqlite3.OperationalError:
        fts_ids = None
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
    scored.sort(key=lambda x: -x[0])
    conn.close()
    out = []
    for score, r in scored[:limit]:
        out.append({"id": r[0], "path": r[1], "title": r[2], "tags": r[3],
                    "category": r[4], "confidence": r[5], "hits": r[6],
                    "status": r[7], "summary": r[8], "verified_at": r[9],
                    "secret": bool(r[10]), "type": r[11], "score": round(score, 1)})
    return out


def _daemon_get(entry_id, force=False):
    """进程内读取全文（不污染 stdout）。secret 需 force。双信号刷新读取时间。"""
    conn = mt.get_conn(); mt.init_schema(conn)
    row = conn.execute("SELECT path, secret, status FROM entries WHERE uid=? OR path=? LIMIT 1",
                       (entry_id, entry_id)).fetchone()
    if not row:
        raise SystemExit(f"未找到: {entry_id}")
    path, secret, status = row
    if secret and not force:
        raise SystemExit("敏感条目（secret），读取需确认: force=true")
    fp = os.path.join(mt.BANK_DIR, path)
    with open(fp, encoding="utf-8") as f:
        text = f.read()
    conn.execute("UPDATE entries SET hits=hits+1, last_accessed=? WHERE path=?", (mt.now_str(), path))
    conn.commit()
    conn.close()
    notes = []
    if status == "invalidated":
        notes.append("此条目已被标记失效（invalidated）")
    return {"path": path, "content": text, "notes": notes}


def _daemon_synthesize(q, hits):
    """冷路径：基于命中条目生成综合回答（结论强制 [来源:path]，无命中禁止脑补）。
    复用 memory_tool 的 LLM 配置与 .env key。返回回答字符串；未配置 LLM 时降级为命中列表。"""
    if not hits:
        return "记忆库无此记录。"
    provider = CONFIG.get("synthesize", {}).get("llm", {})
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
        lines = ["（未配置 LLM key，降级为命中列表）"]
        for it in hits:
            lines.append(f"[{it['score']}] {it['category']}/{it['title']}  path={it['path']}")
        return "\n".join(lines)
    import urllib.request
    brief = []
    for it in hits:
        fp = os.path.join(mt.BANK_DIR, it["path"])
        try:
            with open(fp, encoding="utf-8") as f:
                body_txt = mt.parse_frontmatter(f.read())[1]
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
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"synthesize 调用失败: {e}"

# ---- v2.2 每周维护线程（P3：冷路径不再依赖人记得跑） ----

MAINT_CHECK_INTERVAL = 3600      # 每小时检查一次到期
MAINT_WEEKLY_SECONDS = 7 * 24 * 3600
MAINT_STATE = os.path.join(ROOT, ".consolidate_state.json")

def _maint_load_state():
    try:
        with open(MAINT_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _maint_save_state(st):
    try:
        with open(MAINT_STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass

def run_weekly_consolidate():
    """subprocess 跑 consolidate auto（有界 300s），返回 (ok, 摘要)。"""
    import subprocess as sp
    try:
        r = sp.run([sys.executable, os.path.join(SCRIPTS, "memory_tool.py"), "consolidate", "--mode", "auto"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"疑似重复\s*: (\d+) 条 / (\d+) 组", out)
        dups = int(m.group(1)) if m else -1
        return r.returncode == 0, f"rc={r.returncode} dups={dups}\n{out.strip()[-400:]}"
    except Exception as e:
        return False, f"consolidate 执行异常: {e}"

def maintenance_loop():
    """每周自动 consolidate auto；发现重复组时经飞书桥推送（已配置时）。"""
    time.sleep(30)  # 避开启动尖峰
    while True:
        try:
            st = _maint_load_state()
            last = float(st.get("last_run_ts", 0))
            now = time.time()
            if now - last >= MAINT_WEEKLY_SECONDS:
                print("[maintenance] 周期到期，执行 consolidate auto …")
                ok, summary = run_weekly_consolidate()
                _maint_save_state({"last_run_ts": now, "last_ok": ok,
                                   "last_run_at": datetime.datetime.now().isoformat(timespec="seconds")})
                print(f"[maintenance] consolidate 完成 ok={ok}\n{summary}")
                m = re.search(r"dups=(\d+)", summary)
                if ok and m and int(m.group(1)) > 0:
                    cfg = CONFIG.get("feishu", {})
                    chat_id = cfg.get("chat_id")
                    if cfg.get("app_id") and chat_id:
                        try:
                            feishu_send(chat_id,
                                        f"[AgentMemory 周整理] 发现 {m.group(1)} 条疑似重复，详见 bank/CONFLICTS.md，"
                                        f"建议跑 consolidate llm 复核或手动 --supersedes 收编。", cfg)
                        except Exception:
                            pass
        except Exception as e:
            print(f"[maintenance] 异常（忽略继续）: {e}")
        time.sleep(MAINT_CHECK_INTERVAL)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "version": "2.0",
                             "preload": os.path.exists(mt.PRELOAD_PATH)})
        elif self.path.startswith("/preload"):
            try:
                lines = build_preload(verbose=False)
                self._send(200, {"status": "ok", "preload_lines": lines, "path": mt.PRELOAD_PATH})
            except Exception as e:
                self._send(500, {"status": "error", "msg": str(e)})
        elif self.path == "/feishu/status":
            cfg = CONFIG.get("feishu", {})
            self._send(200, {"enabled": bool(cfg.get("app_id")), "chat_id": cfg.get("chat_id", "")})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/memory":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        cmd = payload.get("cmd", "add")
        try:
            if cmd == "add":
                rel = _daemon_add(payload)
                self._send(200, {"ok": True, "path": rel})
            elif cmd == "search":
                q = payload.get("query", "")
                hits = _daemon_search(q, payload.get("limit", 20))
                if payload.get("synthesize"):
                    # 冷路径：LLM 综合回答（结论带来源，见 memory_tool._synthesize）
                    out = _daemon_synthesize(q, hits)
                    self._send(200, {"ok": True, "answer": out})
                else:
                    self._send(200, {"ok": True, "hits": hits})
            elif cmd == "get":
                result = _daemon_get(payload.get("id", ""), bool(payload.get("force", False)))
                self._send(200, {"ok": True, **result})
            else:
                self._send(400, {"error": f"unknown cmd {cmd}"})
        except SystemExit as e:
            self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})

def main():
    p = argparse.ArgumentParser(prog="daemon", description="AgentMemory 本地常驻服务 v2")
    p.add_argument("--port", type=int, default=CONFIG.get("daemon", {}).get("port", 8123))
    p.add_argument("--with-bridge", action="store_true", help="同时启动飞书桥")
    p.add_argument("--once-preload", action="store_true", help="启动时先重建一次 PRELOAD")
    args = p.parse_args()

    if args.once_preload or not os.path.exists(mt.PRELOAD_PATH):
        try:
            build_preload(verbose=False)
            print(f"PRELOAD 已生成: {mt.PRELOAD_PATH}")
        except Exception as e:
            print(f"PRELOAD 生成失败: {e}")

    if args.with_bridge:
        cfg = CONFIG.get("feishu", {})
        if not cfg.get("app_id"):
            print("⚠️ --with-bridge 但 config.json 未配置 feishu.app_id/app_secret，桥未启用")
        else:
            print(f"飞书桥已启用（chat_id={cfg.get('chat_id','未配置')}）")

    # v2.2：每周维护线程（consolidate auto + 重复时飞书提醒）
    threading.Thread(target=maintenance_loop, daemon=True, name="am-maintenance").start()
    print("每周维护线程已启动（consolidate auto，重复时经飞书桥提醒）")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"AgentMemory daemon v2 运行于 http://127.0.0.1:{args.port}")
    print(f"  POST /memory  add|search|get   |   GET /preload /health")
    print("  热路径零 LLM（ADR-0005）；Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\ndaemon 已停止")

if __name__ == "__main__":
    mt._utf8("")
    main()
