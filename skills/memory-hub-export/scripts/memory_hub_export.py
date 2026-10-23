#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory-hub-export：把本客户端的记忆、规则、工作流与会话导出到共享记忆中台。

调用约定：agent 只运行本脚本、检查退出码并转述输出的 JSON 摘要；不读取记忆或转录原文，
不将原文写入对话内容。

用法：
  # 单条正文（内容本身只有一份时）
  python3 memory_hub_export.py --client <id> --kind memory   --text-file <记忆文件路径> [--project <项目绝对路径>]
  python3 memory_hub_export.py --client <id> --kind rule     --text-file <规则文件路径> [--project <项目绝对路径>]
  # 聚合模式（每个项目每类内容的推荐用法）：把目录下的同类分片合并为一份后导出
  python3 memory_hub_export.py --client <id> --kind memory --from-dir <目录> [--project <项目绝对路径>] [--glob "memory-*"]
  python3 memory_hub_export.py --client <id> --kind workflow --items-file <清单JSON路径> [--project <项目绝对路径>]
  python3 memory_hub_export.py --client <id> --kind session  --session <会话id> --summary <不超过300字的摘要> [--project <项目绝对路径>] [--date YYYY-MM-DD]
  # 会话全文（format 2，需采集策略授权）：把逐轮 JSON 写为 format 2 会话文件
  python3 memory_hub_export.py --client <id> --kind session --session <会话id> --full --turns-file <轮次JSON> [--cursor N] [--title <会话名>] [--project <项目绝对路径>]

命名与存放位置（写入侧只使用以下规则，读取侧兼容历史命名）：
  <kind>-<scope>-<hash8>.md；会话为 YYYY-MM-DD-session-<sessionId>.md，存放于 clients/<id>/sessions/

退出码：0 成功；2 输入缺失（可静默跳过）；3 参数不合法；4 其他错误（原因输出到 stderr）。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

# 项目标识算法由 skills/_shared/project_id.py 统一提供，与管理端实现保持一致：
# 同一目录在中台只允许对应一个分区。缺少该文件时直接退出，避免向错误的分区写入数据。
_SHARED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared")
if not os.path.isfile(os.path.join(_SHARED, "project_id.py")):
    raise SystemExit("缺少 skills/_shared/project_id.py：请整包安装（skills/ 目录需保持完整）")
sys.path.insert(0, _SHARED)
from project_id import normalize_project_path, project_slug  # noqa: E402
from redact import redact  # noqa: E402

HOME = os.path.expanduser("~")
ROOT = os.environ.get("MEMORY_HUB_ROOT") or os.environ.get("DUTI_MEMORY_ROOT") or os.path.join(HOME, "DutiMemory")


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
    """判定该客户端在该项目下是否允许导出会话全文。项目级配置优先于客户端级，客户端级优先于全局开关；
    管理端在入库前会执行相同的检查。"""
    policy = read_capture_policy()
    if project:
        try:
            slug = project_slug(project)
        except Exception:
            slug = ""
        if slug and slug in policy["projects"]:
            return policy["projects"][slug] is not False
    if client in policy["clients"]:
        return policy["clients"][client] is not False
    return policy["enabled"] is not False


def fmt_turns(turns):
    """轮次 JSON → format 2 正文（与管理端及 memory_hub_hook.py 使用同一语法）。"""
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


def turns_stats(turns):
    n = len(turns or [])
    body_bytes = 0
    for t in turns or []:
        body_bytes += len((str(t.get("user") or "") + str(t.get("assistant") or "")).encode("utf-8"))
    return n, body_bytes


def summarize_turns(turns_file):
    """轮次 JSON → 目录卡片摘要（诉求、结论、轮次；与管理端规则一致，不超过 200 字）。"""
    try:
        turns = json.load(open(turns_file, encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(turns, list) or not turns:
        return ""
    first = next((t for t in turns if str(t.get("user") or "").strip()), turns[0])
    last = next((t for t in reversed(turns) if str(t.get("assistant") or "").strip()), turns[-1])
    parts = ["诉求：%s" % clip(first.get("user"), 100)]
    if str(last.get("assistant") or "").strip():
        parts.append("结论：%s" % clip(last.get("assistant"), 100))
    parts.append("轮次：%d" % len(turns))
    return "；".join(parts)


def clip(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + "…" if len(text) > limit else text


def project_partition(project_path):
    """中台分区名。规则：realpath 解析符号链接 → 去除尾部分隔符 → Windows 盘符大写 → NFC 归一化
    → 目录名 + sha256(归一化路径)[:6]。不得在此处转小写或替换非 ASCII 目录名：否则同一目录会因
    大小写或中文目录名被计算为两个分区。"""
    return project_slug(project_path)


def write_if_changed(path_, content):
    """内容未变化时不写入（不更新 mtime）；变化时经临时文件原子替换。返回是否写入。"""
    if os.path.exists(path_):
        try:
            if open(path_, encoding="utf-8").read() == content:
                return False
        except OSError:
            pass
    os.makedirs(os.path.dirname(path_), exist_ok=True)
    tmp = path_ + ".tmp"
    open(tmp, "w", encoding="utf-8").write(content)
    os.replace(tmp, path_)
    return True


def scope_hint(project):
    return "project" if project else "global"


def split_frontmatter(text):
    """解析 YAML frontmatter。返回 (属性字典, 正文)；无 frontmatter 时属性字典为空。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    attrs = {}
    for line in text[3:end].strip().split("\n"):
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line.strip())
        if m:
            attrs[m.group(1)] = m.group(2).strip().strip("\"'")
    return attrs, text[end + 4:].strip()


def collect_dir(src_dir, kind, project, pattern):
    """聚合模式：把目录下的同类文件按路径排序合并为一份正文。
    排除对象：会话文件、已聚合的文件（带 aggregated 标记或 itemCount > 1）、其他类别的固定命名文件。
    返回 (正文, 来源数, 说明, 来源文件名列表)；无可聚合内容时正文为 None。"""
    if not os.path.isdir(src_dir):
        return None, 0, "not a directory: " + src_dir, []
    names = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".md") or fn.startswith("."):
                continue
            if re.match(r"^\d{4}-\d{2}-\d{2}-session-", fn):
                continue  # 会话文件不参与聚合
            names.append(os.path.join(dirpath, fn))
    names.sort()
    parts, used, skipped = [], [], []
    for fp in names:
        base = os.path.basename(fp)
        try:
            raw = open(fp, encoding="utf-8").read()
        except OSError:
            continue
        attrs, body = split_frontmatter(raw)
        file_kind = str(attrs.get("kind") or "")
        if file_kind and file_kind != kind:
            continue
        if not file_kind:
            # 无 frontmatter 的文件：按历史固定命名推断类别，仅做只读兼容
            stem = re.sub(r"\.md$", "", base)
            fixed = {"project-memory": "memory", "global-memory": "memory", "memory": "memory",
                     "project-rules": "rule", "workflows": "workflow"}.get(stem)
            if fixed != kind and not (fixed is None and pattern and re.search(pattern, base)):
                continue
        if pattern and not re.search(pattern, base):
            continue
        if attrs.get("aggregated") or (attrs.get("itemCount") and int(attrs.get("itemCount") or 1) > 1):
            skipped.append(base)  # 已聚合的文件不重复聚合
            continue
        if project and attrs.get("project") and os.path.abspath(attrs.get("project")) != os.path.abspath(project):
            continue
        title = str(attrs.get("summary") or "").strip() or base
        parts.append("## " + base + "\n\n_（来源摘要：%s）_\n\n%s" % (clip(title, 160), body.strip()))
        used.append(base)
    if not parts:
        return None, 0, "no matching files under " + src_dir, []
    body = "\n\n---\n\n".join(parts)
    note = ""
    if skipped:
        note = "skipped aggregated files: " + ", ".join(skipped[:5])
    return body, len(used), note, used


def main():
    parser = argparse.ArgumentParser(description="把本客户端的记忆、规则、工作流与会话导出到共享记忆中台。")
    parser.add_argument("--client", required=True)
    parser.add_argument("--kind", required=True, choices=["memory", "rule", "session", "workflow"])
    parser.add_argument("--text-file")
    parser.add_argument("--from-dir")
    parser.add_argument("--glob")
    parser.add_argument("--items-file")
    parser.add_argument("--session")
    parser.add_argument("--summary")
    parser.add_argument("--project")
    parser.add_argument("--date")
    parser.add_argument("--title")
    parser.add_argument("--full", action="store_true", help="会话全文模式（format 2；需配合 --turns-file）")
    parser.add_argument("--turns-file", help="逐轮 JSON：[{n,time,user,assistant,tools[]}]")
    parser.add_argument("--cursor", type=int, default=0, help="源端游标（行号或字节偏移；缺省取轮数）")
    args = parser.parse_args()

    warnings = []
    body = ""
    item_count = 1
    if args.from_dir:
        if args.kind == "session":
            print(json.dumps({"ok": False, "skip": True, "reason": "--from-dir 不支持 session（会话按会话 id 一份）"}, ensure_ascii=False))
            return 3
        body, item_count, note, used = collect_dir(args.from_dir, args.kind, args.project, args.glob)
        if body is None:
            print(json.dumps({"ok": False, "skip": True, "reason": note}, ensure_ascii=False))
            return 2
        if note:
            warnings.append(note)
        # 覆盖风险提示：目标目录下存在多个同类非聚合分片时，因入库键不含文件名哈希段，它们会相互覆盖
        siblings = [n for n in os.listdir(args.from_dir) if n.endswith(".md")] if os.path.isdir(args.from_dir) else []
        same = [n for n in siblings if re.match(r"^%s-%s-[0-9a-f]{6,8}\.md$" % (re.escape(args.kind), scope_hint(args.project)), n)]
        if len(same) >= 2:
            warnings.append("collapse-risk: 目标目录下存在 %d 个同类（%s）非聚合分片；中台入库键不含文件名哈希段，这些分片会相互覆盖。索引以聚合文件为准，分片仅作只读保留，建议后续只保留聚合文件" % (len(same), args.kind))
    else:
        if args.kind in ("memory", "rule") and not args.text_file:
            print(json.dumps({"ok": False, "skip": True, "reason": "missing --text-file or --from-dir"}))
            return 2
        if args.text_file:
            try:
                body = open(args.text_file, encoding="utf-8").read()
                item_count = int(os.environ.get("DUTI_ITEM_COUNT") or 1)
            except OSError as e:
                print(json.dumps({"ok": False, "skip": True, "reason": "read failed: %s" % e}))
                return 2
    if args.kind == "workflow":
        try:
            items = json.load(open(args.items_file, encoding="utf-8")) if args.items_file else []
        except (OSError, ValueError):
            items = []
        lines = ["- **%s**：%s" % (clip(it.get("name", ""), 60), clip(it.get("desc", ""), 80)) for it in items if isinstance(it, dict)]
        body = "\n".join(lines) or "（暂无条目）"
        item_count = max(1, len(lines))
    turns_list = []
    full_mode = False
    if args.kind == "session":
        if args.full and args.turns_file:
            try:
                turns_list = json.load(open(args.turns_file, encoding="utf-8"))
                if not isinstance(turns_list, list):
                    turns_list = []
            except (OSError, ValueError):
                turns_list = []
            # 采集策略检查：未授权时回退为摘要级（管理端在入库前会再次执行相同的检查）
            full_mode = bool(turns_list) and capture_full_allowed(args.client, args.project)
        if full_mode:
            body = fmt_turns(turns_list)
        else:
            body = str(args.summary or "").strip()
            if not body:
                print(json.dumps({"ok": False, "skip": True, "reason": "missing --summary"}, ensure_ascii=False))
                return 2
            if len(body) > 300:
                body = body[:300]

    body, hits = redact(body)
    scope = "project" if args.project else "global"
    date = args.date or time.strftime("%Y-%m-%d")
    if args.kind == "session":
        session_id = str(args.session or "").strip()
        if not session_id:
            print(json.dumps({"ok": False, "skip": True, "reason": "missing --session"}, ensure_ascii=False))
            return 3
        name = "%s-session-%s.md" % (date, session_id)
    else:
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
        name = "%s-%s-%s.md" % (args.kind, scope, digest)
    summary_raw = clip(args.summary or (summarize_turns(args.turns_file) if full_mode else "") or body.replace("\n", " ") or name, 200)
    # 摘要由轮次或正文派生，同样需要脱敏，避免凭据经摘要绕过正文的脱敏处理
    summary, summary_hits = redact(summary_raw)
    hits += summary_hits
    turns_n, turns_bytes = turns_stats(turns_list) if full_mode else (0, 0)
    cursor = int(args.cursor or 0) or turns_n
    fm_lines = [
        "---",
        "client: %s" % args.client,
        "scope: %s" % scope,
        ("project: %s" % os.path.abspath(args.project)) if args.project else None,
        "kind: %s" % args.kind,
        ("session: %s" % args.session) if (args.kind == "session" and args.session) else None,
        ("title: %s" % clip(args.title, 120)) if (args.title and str(args.title).strip()) else None,
        "exported: %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ("date: %s" % date) if args.kind == "session" else None,
        "summary: %s" % summary,
        "format: %d" % (2 if full_mode else 1),
        ("turns: %d" % turns_n) if full_mode else None,
        ("bytes: %d" % turns_bytes) if full_mode else None,
        ("cursor: %d" % cursor) if full_mode else None,
        ("itemCount: %d" % item_count) if (args.kind != "session" and item_count > 0) else None,
        "aggregated: true" if (args.kind != "session" and args.from_dir) else None,
        "---",
        "",
        "# %s · %s" % (args.kind, args.client),
        "",
        body,
        "",
    ]
    document = "\n".join(x for x in fm_lines if x is not None)
    if args.project:
        slug = project_partition(args.project)
        partition_dir = os.path.join(ROOT, "projects", slug)
        meta = os.path.join(partition_dir, "project.json")
        if not os.path.exists(meta):
            os.makedirs(partition_dir, exist_ok=True)
            # path 记录归一化后的路径：管理端据此将分区归属到对应项目，符号链接与相对路径指向同一目录时视为同一项目
            normalized = normalize_project_path(args.project)
            json.dump({"path": normalized, "name": os.path.basename(normalized) or os.path.basename(os.path.normpath(args.project)), "createdAt": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}, open(meta, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        if args.kind == "session":
            target = os.path.join(partition_dir, "clients", args.client, "sessions", name)
        else:
            target = os.path.join(partition_dir, "clients", args.client, name)
    else:
        target = os.path.join(ROOT, "global", "clients", args.client, name)
    # 同目录下存在旧的聚合文件（内容变更导致文件名变化，旧文件残留）时提示：中台索引以最新聚合条目为准
    if args.from_dir and args.kind != "session":
        target_dir = os.path.dirname(target)
        for fn in (os.listdir(target_dir) if os.path.isdir(target_dir) else []):
            if fn == name or not fn.endswith(".md"):
                continue
            try:
                old_attrs, _ = split_frontmatter(open(os.path.join(target_dir, fn), encoding="utf-8").read())
            except OSError:
                continue
            if str(old_attrs.get("aggregated", "")).lower() in ("true", "1"):
                warnings.append("stale-aggregate: %s 为内容变更前的旧聚合文件；中台索引以最新的聚合条目为准，该文件可以删除" % fn)
                break
    # 增量规则：记忆、规则、工作流的文件名由正文哈希决定，同名即同内容，直接跳过（不写盘、不更新 mtime）；
    # 会话文件名包含日期，改为比对正文
    if os.path.exists(target):
        try:
            old_text = open(target, encoding="utf-8").read()
            _, old_body = split_frontmatter(old_text)
        except OSError:
            old_body = None
        if args.kind != "session" or (old_body is not None and old_body.strip() == body.strip()):
            print(json.dumps({"ok": True, "file": target, "wrote": False, "unchanged": True,
                              "redacted": hits, "kind": args.kind, "scope": scope, "itemCount": item_count,
                              "items": item_count if args.from_dir else None, "warnings": warnings}, ensure_ascii=False))
            return 0
    wrote = write_if_changed(target, document)
    print(json.dumps({"ok": True, "file": target, "wrote": wrote, "unchanged": False,
                      "redacted": hits, "kind": args.kind, "scope": scope, "itemCount": item_count,
                      "items": item_count if args.from_dir else None, "warnings": warnings}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print("memory-hub-export error: %s" % e, file=sys.stderr)
        sys.exit(4)
