#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory-hub hook：由宿主钩子事件触发，把会话摘要或完整会话写入共享记忆中台。
纯文件通道，管理端未运行时同样可用。

各宿主的 stdin 契约（2026-09 逐宿主验证）：
  * transcript_path 指向完整转录：claude-code、workbuddy、qoder（JSONL，可解析完整会话）
  * transcript_path 指向临时文件：zcode（仅含最后一轮回答，钩子返回后即被删除，本脚本必须同步读取）
  * 无 transcript_path：trae、trae-cn、trae-work（回退为 last_assistant_message）、
    minimax-code（cwd 仅在 SessionStart 事件提供）
无可用输入时不静默跳过：把跳过原因追加到 .state/<client>-hook.jsonl 并输出到 stderr，
退出码保持 0（Claude Code 与 ZCode 系宿主将退出码 2 视为阻断，跳过场景不得使用该值）。
HTTP 上行与文件通道在发送前执行相同的脱敏（skills/_shared/redact.py）；脱敏模块缺失时
跳过 HTTP，直接走文件通道（导出脚本内置脱敏）。
"""
import json
import os
import re
import subprocess
import sys
import time

# 项目标识算法由 skills/_shared/project_id.py 统一提供。钩子不得因缺少该文件而失败
# （宿主将非 0 退出码视为异常、退出码 2 视为阻断）：缺少时退化为不带项目键的策略判定，
# 客户端与全局键仍然生效，管理端在入库前会再次执行判定。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared"))
try:
    from project_id import project_slug
except Exception:
    project_slug = None
try:
    from redact import redact, redact_turns
except Exception:
    # 脱敏模块缺失时不经 HTTP 发送内容，改走文件通道（导出脚本内置脱敏）
    redact = None
    redact_turns = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("MEMORY_HUB_ROOT") or os.path.join(os.path.expanduser("~"), "MemoryHub")


def clip(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + "…" if len(text) > limit else text


# ===== 会话全文采集：授权通过时写入逐轮全文（format 2），未授权时回退为摘要级 =====

def read_capture_policy():
    """读取会话全文采集策略（由管理端设置页写入；文件不存在时按已授权处理）。"""
    try:
        policy = json.load(open(os.path.join(ROOT, ".state", "capture-policy.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": True, "clients": {}, "projects": {}}
    if not isinstance(policy, dict):
        return {"enabled": True, "clients": {}, "projects": {}}
    return {"enabled": policy.get("enabled", True) is not False,
            "clients": policy.get("clients") if isinstance(policy.get("clients"), dict) else {},
            "projects": policy.get("projects") if isinstance(policy.get("projects"), dict) else {}}


def capture_full_allowed(client, project):
    """判定该客户端在该项目下是否允许采集全文。项目键使用与分区名一致的「目录名-hash6」
    规则（project_id.project_slug），否则按分区维度配置的例外将无法匹配。
    管理端在入库前会执行相同的检查。"""
    policy = read_capture_policy()
    if project:
        try:
            slug = project_slug(project) if project_slug else ""
        except Exception:
            slug = ""
        if slug and slug in policy["projects"]:
            return policy["projects"][slug] is not False
    if client in policy["clients"]:
        return policy["clients"][client] is not False
    return policy["enabled"] is not False


def fmt_turns(turns):
    """轮次 → format 2 正文（与 memory_hub_export.py 及管理端使用同一语法）。"""
    out = []
    for i, t in enumerate(turns or []):
        n = t.get("n") or (i + 1)
        head = "## 轮 %d" % n
        if t.get("time"):
            head += " · %s" % t["time"]
        out.append(head)
        if str(t.get("user") or "").strip():
            out.append("**用户**：%s" % str(t["user"]).strip())
        if str(t.get("assistant") or "").strip():
            out.append("**助手**：%s" % str(t["assistant"]).strip())
        if t.get("tools"):
            out.append("（工具：%s）" % " · ".join(str(x) for x in t["tools"]))
        out.append("")
    return "\n".join(out).strip() + "\n"


def turns_summary(turns):
    if not turns:
        return ""
    first = next((t for t in turns if str(t.get("user") or "").strip()), turns[0])
    last = next((t for t in reversed(turns) if str(t.get("assistant") or "").strip()), turns[-1])
    parts = ["诉求：%s" % clip(first.get("user"), 100)]
    if str(last.get("assistant") or "").strip():
        parts.append("结论：%s" % clip(last.get("assistant"), 100))
    parts.append("轮次：%d" % len(turns))
    return "；".join(parts)


def turns_bytes(turns):
    return sum(len((str(t.get("user") or "") + str(t.get("assistant") or "")).encode("utf-8")) for t in turns or [])


def build_turns_from_transcript(path_):
    """完整 JSONL 转录 → 逐轮全文（适用于 claude-code、workbuddy、qoder 的转录格式；不适用于 zcode 的临时文件）。"""
    events = read_lines(path_)
    if not events:
        return []
    turns = []
    current = None
    for ev in events:
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
        role = msg.get("role") or ev.get("role") or ev.get("type") or ""
        if role in ("system", "permission-mode", "file-history-snapshot", "workspace-directories", "runtime-config", "attachment", "active-leaf"):
            continue
        if role not in ("user", "human", "assistant"):
            continue
        content = flatten(msg.get("content") or msg.get("text") or "").strip()
        if role in ("user", "human"):
            if content:
                if current and (current.get("user") or current.get("assistant")):
                    turns.append(current)
                current = {"n": len(turns) + 1, "time": "", "user": content, "assistant": "", "tools": []}
        else:
            if current is None:
                current = {"n": len(turns) + 1, "time": "", "user": "", "assistant": "", "tools": []}
            if content:
                current["assistant"] = (current["assistant"] + "\n" + content).strip() if current["assistant"] else content
            for m in re.findall(r'"(?:name|tool_name)"\s*:\s*"([A-Za-z0-9_\-]+)"', flatten(msg.get("content") or ""))[:5]:
                if m not in current["tools"] and len(current["tools"]) < 20:
                    current["tools"].append(m)
    if current and (current.get("user") or current.get("assistant")):
        turns.append(current)
    return turns


def connect_ro(db_path, timeout=None):
    """以只读模式打开 SQLite 数据库。URI 由 pathlib 生成（file:///绝对路径），
    兼容 Windows 盘符与含特殊字符的路径；query 参数启用 SQLite 的只读模式。"""
    import pathlib
    import sqlite3
    uri = pathlib.Path(db_path).absolute().as_uri() + "?mode=ro"
    if timeout is not None:
        con = sqlite3.connect(uri, uri=True, timeout=timeout)
    else:
        con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def read_zcode_turns(session_id, db_path=None):
    """只读增量读取 ZCode 的私有会话数据库：message 与 part 两表 → 逐轮全文。返回 (轮次列表, 最大 rowid)。"""
    db_path = db_path or os.environ.get("MEMORY_HUB_ZCODE_DB") or os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
    if not os.path.exists(db_path):
        return [], 0
    try:
        con = connect_ro(db_path)
    except Exception:
        return [], 0
    try:
        parts = {}
        for r in con.execute("SELECT message_id, data FROM part WHERE session_id=? ORDER BY sequence", (session_id,)):
            try:
                data = json.loads(r["data"])
            except ValueError:
                continue
            parts.setdefault(r["message_id"], []).append(data)

        def part_text(message_id):
            texts, tools = [], []
            for data in parts.get(message_id, []):
                if data.get("type") == "text" and data.get("text"):
                    texts.append(str(data["text"]))
                elif data.get("type") in ("tool", "tool-invocation"):
                    name = data.get("tool") or data.get("toolName") or data.get("name") or ""
                    argument = ""
                    for key in ("args", "input"):
                        if isinstance(data.get(key), dict):
                            argument = data[key].get("file_path") or data[key].get("path") or data[key].get("command") or ""
                            if argument:
                                break
                    label = ("%s %s" % (name, clip(str(argument).split("\n")[0], 60))).strip() if argument else str(name)
                    if name and label not in tools:
                        tools.append(label)
            return "\n".join(t for t in texts if t).strip(), tools

        turns, current, max_rowid = [], None, 0
        for m in con.execute("SELECT rowid, id, data, time_created FROM message WHERE session_id=? ORDER BY sequence", (session_id,)):
            max_rowid = max(max_rowid, int(m["rowid"]) & 0x7fffffff)
            try:
                data = json.loads(m["data"])
            except ValueError:
                continue
            role = str(data.get("role") or "")
            text, tools = part_text(m["id"] or data.get("id"))
            ts = int(m["time_created"] or 0)
            hhmm = time.strftime("%H:%M", time.localtime(ts / 1000.0)) if ts else ""
            if role == "user":
                if text:
                    if current and (current.get("user") or current.get("assistant")):
                        turns.append(current)
                    current = {"n": len(turns) + 1, "time": hhmm, "user": text, "assistant": "", "tools": []}
            elif role == "assistant":
                if current is None:
                    current = {"n": len(turns) + 1, "time": hhmm, "user": "", "assistant": "", "tools": []}
                if text:
                    current["assistant"] = (current["assistant"] + "\n" + text).strip() if current["assistant"] else text
                for tool in tools:
                    if tool not in current["tools"] and len(current["tools"]) < 20:
                        current["tools"].append(tool)
        if current and (current.get("user") or current.get("assistant")):
            turns.append(current)
        return turns, max_rowid
    except Exception:
        return [], 0
    finally:
        try:
            con.close()
        except Exception:
            pass


# ===== 历史会话全量导入：由客户端读取自身存储后写入中台，全量、不限天数、增量幂等 =====
# 触发方式：用户明确要求时由 agent 执行：
#   python3 memory_hub_hook.py --client <id> --import-all [--sessions-dir <明文会话目录>] [--dry-run]
# 增量规则：zcode 使用 message 表 rowid，JSONL 使用文件字节数；重复执行只补写新增或变更的会话。

HOST_SESSION_ROOTS = {
    "claude-code": "~/.claude/projects",
    "codex": "~/.codex/sessions",
}


def zcode_db_path():
    return os.environ.get("MEMORY_HUB_ZCODE_DB") or os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")


def list_zcode_sessions():
    """枚举 ZCode 的全部顶层会话（只读打开其数据库，不限制时间范围）。"""
    db_path = zcode_db_path()
    if not os.path.exists(db_path):
        return []
    con = connect_ro(db_path)
    try:
        rows = con.execute("SELECT id, title, directory, time_updated FROM session WHERE parent_id IS NULL ORDER BY time_updated DESC").fetchall()
        return [{"sid": str(r["id"]), "title": str(r["title"] or ""), "cwd": str(r["directory"] or ""), "updated": int(r["time_updated"] or 0)} for r in rows]
    finally:
        con.close()


def list_jsonl_files(root):
    """遍历明文会话目录下的全部 .jsonl 文件（全量，不限制时间范围）。"""
    out = []
    base = os.path.expanduser(str(root or ""))
    if not base or not os.path.isdir(base):
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".jsonl"):
                fp = os.path.join(dirpath, fn)
                try:
                    out.append({"file": fp, "size": os.path.getsize(fp)})
                except OSError:
                    pass
    return out


_HTTP_DISABLED = False  # 批量导入时 HTTP 通道首次失败后置为 True，其余会话直接走文件通道，避免逐场等待超时


def emit_session(client, session_id, cwd, turns, summary="", title="", cursor=0):
    """把一场会话写入中台。优先使用 HTTP（管理端在线时）；失败时回退到文件通道
    （调用自身的导出脚本）。返回是否成功。

    HTTP 载荷在发送前执行与文件通道相同的脱敏；脱敏模块缺失时直接走文件通道。
    """
    global _HTTP_DISABLED
    if not turns:
        return False
    summary = summary or turns_summary(turns)
    body = fmt_turns(turns)
    cursor = int(cursor or 0) or len(turns)
    if redact is not None and not _HTTP_DISABLED:
        try:
            state_file = os.path.join(ROOT, ".state", "app.json")
            endpoint = json.load(open(state_file, encoding="utf-8"))
            import urllib.request
            http_summary, _ = redact(summary)
            http_body, _ = redact(body)
            http_turns, _ = redact_turns(turns)
            payload = {"client": client, "kind": "session", "scope": "project" if cwd else "global",
                       "project": cwd or "", "session": session_id, "summary": http_summary, "body": http_body,
                       "format": 2, "turns": http_turns, "turnCount": len(http_turns),
                       "bytes": turns_bytes(http_turns), "cursor": cursor}
            if title:
                payload["title"] = title
            req = urllib.request.Request(
                endpoint["endpoint"].rstrip("/") + "/xmem/report",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + endpoint.get("token", "")},
                method="POST", timeout=30)
            with urllib.request.urlopen(req) as resp:
                if 200 <= resp.status < 300:
                    return True
            _HTTP_DISABLED = True
        except Exception:
            _HTTP_DISABLED = True
    try:
        state_dir = os.path.join(ROOT, ".state")
        os.makedirs(state_dir, exist_ok=True)
        turns_file = os.path.join(state_dir, "%s-turns.json" % re.sub(r"[^A-Za-z0-9._-]", "_", "%s-%s" % (client, session_id))[:80])
        with open(turns_file, "w", encoding="utf-8") as f:
            json.dump(turns, f, ensure_ascii=False)
        args = [sys.executable, os.path.join(HERE, "memory_hub_export.py"),
                "--client", client, "--kind", "session", "--session", session_id,
                "--full", "--turns-file", turns_file, "--summary", summary, "--cursor", str(cursor)]
        if cwd:
            args += ["--project", cwd]
        if title:
            args += ["--title", title]
        result = subprocess.run(args, capture_output=True, text=True)
        try:
            os.remove(turns_file)
        except OSError:
            pass
        return result.returncode == 0
    except Exception:
        return False


def import_all(client, sessions_dir="", dry=False):
    """全量导入：枚举本宿主的全部历史会话，逐场组装逐轮全文后写入中台。输出 JSON 摘要。"""
    items = []
    if sessions_dir or client in HOST_SESSION_ROOTS:
        root = sessions_dir or HOST_SESSION_ROOTS.get(client) or ""
        for f in list_jsonl_files(root):
            items.append({"kind": "jsonl", "file": f["file"], "size": f["size"]})
    else:
        for row in list_zcode_sessions():
            items.append({"kind": "zcode", "sid": row["sid"], "cwd": row["cwd"], "title": row["title"]})
    if not items:
        print(json.dumps({"ok": False, "client": client, "reason": "没有找到可导入的会话（zcode 读取自身数据库；其他宿主通过 --sessions-dir 指定明文会话目录）"}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "phase": "scan", "client": client, "sessions": len(items), "dryRun": bool(dry)}, ensure_ascii=False), file=sys.stderr)
    if dry:
        print(json.dumps({"ok": True, "client": client, "sessions": len(items), "wrote": 0, "dryRun": True}, ensure_ascii=False))
        return 0
    wrote = 0
    skipped = 0
    failed = 0
    processed = 0
    for item in items:
        processed += 1
        try:
            if item["kind"] == "zcode":
                session_id = item["sid"]
                turns, cursor = read_zcode_turns(session_id)
                cwd = item.get("cwd") or ""
                title = item.get("title") or ""
            else:
                file_path = item["file"]
                try:
                    with open(file_path, encoding="utf-8") as f:
                        head_lines = f.read().split(chr(10))[:5]
                except OSError:
                    failed += 1
                    continue
                turns = build_turns_from_transcript(file_path)
                session_id = os.path.basename(file_path).replace(".jsonl", "")
                cwd = ""
                title = ""
                for line in head_lines:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not cwd:
                        cwd = str(event.get("cwd") or (event.get("payload") or {}).get("cwd") or "")
                    if not session_id:
                        session_id = str(event.get("sessionId") or event.get("session_id") or session_id)
                cursor = int(item.get("size") or 0)
            if not turns:
                skipped += 1
                continue
            if not capture_full_allowed(client, cwd):
                # 策略未授权全文：跳过该会话，不写入摘要级，避免向中台写入重复内容
                skipped += 1
                continue
            ok = emit_session(client, session_id, cwd, turns, title=title, cursor=cursor)
            if ok:
                wrote += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        if processed % 20 == 0:
            print(json.dumps({"phase": "progress", "done": processed, "total": len(items), "wrote": wrote}, ensure_ascii=False), file=sys.stderr)
    print(json.dumps({"ok": failed == 0, "client": client, "sessions": len(items), "wrote": wrote, "skipped": skipped, "failed": failed}, ensure_ascii=False))
    return 0


def note_skip(client, event, reason, session_id=""):
    """记录跳过事件：追加到 .state/<client>-hook.jsonl 并输出到 stderr，退出码保持 0。"""
    try:
        state_dir = os.path.join(ROOT, ".state")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "%s-hook.jsonl" % client), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event,
                                "session": session_id, "skip": reason}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    sys.stderr.write("memory-hub hook: skip (%s/%s): %s\n" % (client, event, reason))


def read_lines(path_):
    out = []
    try:
        with open(path_, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return None
    return out


def flatten(content):
    if isinstance(content, list):
        return " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in content)
    return str(content or "")


def summarize_from_transcript(path_):
    """从 JSONL 转录生成摘要（适用于 claude-code、workbuddy、qoder 的转录格式，zcode 的临时文件同样可解析）。"""
    events = read_lines(path_)
    if not events:
        return None
    user_first, assistant_last, tools, files, turns = "", "", [], [], 0
    for ev in events:
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
        role = msg.get("role") or ev.get("role") or ev.get("type") or ""
        if role in ("system", "permission-mode", "file-history-snapshot", "workspace-directories", "runtime-config", "attachment", "active-leaf"):
            continue
        content = flatten(msg.get("content") or msg.get("text") or "")
        if not content.strip():
            continue
        if role in ("user", "human"):
            if not user_first:
                user_first = content
            turns += 1
        elif role == "assistant":
            assistant_last = content
            turns += 1
        for m in re.findall(r'"(?:name|tool_name)"\s*:\s*"([A-Za-z0-9_\-]+)"', content)[:5]:
            if m not in tools and len(tools) < 20:
                tools.append(m)
        for m in re.findall(r"([/\w.\-]+\.[A-Za-z0-9]{1,6})", content):
            if m not in files and len(files) < 30:
                files.append(m)
    parts = []
    if user_first:
        parts.append("诉求：%s" % clip(user_first, 100))
    if assistant_last:
        parts.append("结论：%s" % clip(assistant_last, 200))
    if tools:
        parts.append("工具：" + "、".join(tools))
    if files:
        parts.append("涉及：" + "、".join(files))
    if turns:
        parts.append("轮次：%d" % turns)
    return "；".join(parts) if parts else None


def host_session_title(client, session_id):
    """读取宿主会话列表中显示的标题。目前仅支持 ZCode：只读其数据库 session 表的 title 列。
    数据库不存在、无此会话或无读取权限时返回空串，不推断标题。"""
    if client not in ("zcode",):
        return ""
    db = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
    if not os.path.exists(db):
        return ""
    try:
        con = connect_ro(db, timeout=2)
        try:
            row = con.execute("select title from session where id = ? limit 1", (session_id,)).fetchone()
        finally:
            con.close()
        return str(row[0]).strip() if row and row[0] else ""
    except Exception:
        return ""


def session_cwd_path(client):
    return os.path.join(ROOT, ".state", "%s-sessions.json" % client)


def session_cwd_put(client, session_id, cwd):
    """SessionStart 事件记录 会话→工作目录，供 SessionEnd 事件恢复作用域
    （minimax-code 仅在 SessionStart 事件提供 cwd）。最多保留 500 条。"""
    if not session_id or not cwd:
        return
    path_ = session_cwd_path(client)
    data = {}
    try:
        data = json.load(open(path_, encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[session_id] = {"cwd": cwd, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    items = sorted(data.items(), key=lambda kv: kv[1].get("at", ""))[-500:]
    try:
        os.makedirs(os.path.dirname(path_), exist_ok=True)
        json.dump(dict(items), open(path_, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except OSError:
        pass


def session_cwd_get(client, session_id):
    try:
        return (json.load(open(session_cwd_path(client), encoding="utf-8")).get(session_id) or {}).get("cwd") or ""
    except (OSError, ValueError):
        return ""


def main():
    if "--import-all" in sys.argv:
        client = sys.argv[sys.argv.index("--client") + 1] if "--client" in sys.argv else ""
        if not client:
            sys.stderr.write("memory-hub hook: missing --client")
            return 0
        sessions_dir = sys.argv[sys.argv.index("--sessions-dir") + 1] if "--sessions-dir" in sys.argv else ""
        return import_all(client, sessions_dir=sessions_dir, dry=("--dry-run" in sys.argv))
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    event = sys.argv[sys.argv.index("--event") + 1] if "--event" in sys.argv else ""
    client = sys.argv[sys.argv.index("--client") + 1] if "--client" in sys.argv else ""
    if not client:
        sys.stderr.write("memory-hub hook: missing --client\n")
        return 0
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        note_skip(client, event, "stdin 不是合法 JSON（宿主的输入契约可能已变更）")
        return 0
    if not isinstance(payload, dict):
        note_skip(client, event, "stdin JSON 不是对象")
        return 0
    session_id = str(payload.get("session_id") or payload.get("sessionId") or payload.get("sessionID") or "").strip()
    cwd = str(payload.get("cwd") or payload.get("workspaceDir") or payload.get("workspace_dir") or "").strip()
    event = event or str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    # SessionStart：仅记录 会话→工作目录（部分宿主只在启动事件提供 cwd）
    if event == "SessionStart":
        session_cwd_put(client, session_id, cwd)
        return 0
    if not cwd:
        cwd = session_cwd_get(client, session_id)
    if not session_id:
        note_skip(client, event, "stdin 中没有 session_id 或 sessionId（无法定位会话）")
        return 0
    transcript_path = str(payload.get("transcript_path") or payload.get("transcriptPath") or "").strip()
    last_message = payload.get("last_assistant_message") or payload.get("lastAssistantMessage") or ""
    # 优先构建逐轮全文（通过授权检查时使用 format 2）；未授权或无法获取时回退为摘要级
    full_turns = []
    zcode_cursor = 0
    try:
        if capture_full_allowed(client, cwd):
            if client == "zcode" or not (transcript_path and os.path.exists(transcript_path)):
                full_turns, zcode_cursor = read_zcode_turns(session_id)
            if not full_turns and transcript_path and os.path.exists(transcript_path):
                full_turns = build_turns_from_transcript(transcript_path)
    except Exception:
        full_turns = []
    summary = None
    if transcript_path and os.path.exists(transcript_path):
        summary = summarize_from_transcript(transcript_path)
    if not summary and last_message:
        summary = "结论：%s" % clip(flatten(last_message), 240)
        if not transcript_path:
            summary = "（该宿主的钩子不提供转录，摘要为最后一轮回答级）" + summary
    if not full_turns and not summary:
        note_skip(client, event, "既无 transcript_path 也无 last_assistant_message，无法生成摘要内容", session_id)
        return 0
    if full_turns and not summary:
        summary = turns_summary(full_turns)
    title = ""
    if "--with-title" in sys.argv:
        title = str(payload.get("session_title") or payload.get("title") or "").strip() or host_session_title(client, session_id)
    if full_turns:
        if not emit_session(client, session_id, cwd, full_turns, summary=summary or "", title=title, cursor=zcode_cursor):
            note_skip(client, event, "写入中台失败（管理端未运行且文件通道写入失败）", session_id)
        return 0
    # 无全文（未授权或无法获取转录）时写入摘要级
    args = [sys.executable, os.path.join(HERE, "memory_hub_export.py"),
            "--client", client, "--kind", "session", "--session", session_id, "--summary", summary or ""]
    if cwd:
        args += ["--project", cwd]
    if title:
        args += ["--title", title]
    if redact is not None:
        try:
            http_summary, _ = redact(summary or "")
            state_file = os.path.join(ROOT, ".state", "app.json")
            endpoint = json.load(open(state_file, encoding="utf-8"))
            import urllib.request
            payload_out = {"client": client, "kind": "session", "scope": "project" if cwd else "global",
                           "project": cwd or "", "session": session_id, "summary": http_summary, "body": http_summary}
            req = urllib.request.Request(
                endpoint["endpoint"].rstrip("/") + "/xmem/report",
                data=json.dumps(payload_out).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + endpoint.get("token", "")},
                method="POST", timeout=15)
            with urllib.request.urlopen(req) as resp:
                if 200 <= resp.status < 300:
                    return 0
        except Exception:
            pass
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        note_skip(client, event, "导出脚本执行失败：%s" % clip(result.stdout or result.stderr, 200), session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
