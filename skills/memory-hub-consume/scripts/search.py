#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory-hub-consume 检索：在记忆中台目录内做本地全文检索，不依赖模型与第三方库。

用法：python3 search.py "<关键词>" [--scope project|global] [--client <id>] [--limit 8]
      [--kind memory|rule|session|workflow] [--session <会话id>] [--json]
输出：默认为人读文本（命中文件的相对路径 + 片段，每条不超过 200 字）；--json 输出结构化
      结果，便于脚本与其他工具消费。退出码恒为 0，未命中以空结果表达。
只读，不修改任何中台文件。
兼容的命名：哈希命名 <kind>-<scope>-<hash8>.md、历史固定命名（rules.md、project-memory.md 等）、
会话 YYYY-MM-DD-session-<id>.md；会话的两种存放位置（clients/<id>/sessions/ 与历史数据所在的客户端根目录）均可检索。
"""
import argparse
import json
import os
import re
import sys

ROOT = os.environ.get("MEMORY_HUB_ROOT") or os.environ.get("DUTI_MEMORY_ROOT") or os.path.join(os.path.expanduser("~"), "DutiMemory")
SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-session-(.+)\.md$", re.I)
HASH_RE = re.compile(r"^([a-z]+)-(global|project)-[0-9a-f]{6,8}\.md$", re.I)
FIXED = {"project-rules": "rule", "global-rules": "rule", "rules": "rule",
         "project-memory": "memory", "global-memory": "memory", "memory": "memory",
         "workflows": "workflow", "project-workflows": "workflow"}


def kind_of(filename, text):
    """判定条目类别：优先取 frontmatter 的 kind，其次按文件名规则推断。"""
    m = re.match(r"^kind\s*:\s*(\w+)", text, re.I | re.M)
    if m:
        return m.group(1).lower()
    m = SESSION_RE.match(filename)
    if m:
        return "session"
    m = HASH_RE.match(filename)
    if m:
        return m.group(1).lower()
    return FIXED.get(re.sub(r"\.md$", "", filename), "")


def session_turns_text(text, term, max_sections=3, cap=1200):
    """format 2 会话按「## 轮」分节：返回命中轮及其下一轮的原文，而不是整篇截取前若干字。"""
    m = re.search(r"^##\s*轮\s*(\d+)", text, re.M)
    if not m:
        idx = text.lower().find(term)
        return re.sub(r"\s+", " ", text[max(0, idx - 60): idx + 260]).strip()
    sections = re.split(r"(?m)^(?=##\s*轮\s*\d+)", text)
    low_term = term.lower()
    hit_idx = [i for i, s in enumerate(sections) if low_term in s.lower()]
    if not hit_idx:
        hit_idx = [0]
    picked = []
    for i in hit_idx[:max_sections]:
        for j in (i, i + 1):
            if 0 <= j < len(sections) and sections[j] not in picked:
                picked.append(sections[j])
    out = "\n".join(s.strip() for s in picked)
    return out[:cap]


def search(base, args, terms):
    """遍历 base 目录执行检索。返回 (相对路径, 片段) 列表，数量不超过 args.limit。"""
    hits = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in (".state", "index")]
        for fn in sorted(filenames):
            if not fn.endswith(".md"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, ROOT)
            posix = "/" + rel.replace(os.sep, "/") + "/"
            if args.client and ("/clients/%s/" % args.client) not in posix:
                continue
            if args.session and ("-session-%s.md" % args.session) not in fn:
                continue
            try:
                text = open(fp, encoding="utf-8").read()
            except OSError:
                continue
            kind = kind_of(fn, text)  # 每个文件只计算一次，供类别过滤与分节输出共用
            if args.kind and kind != args.kind:
                continue
            low = text.lower()
            if all(t in low for t in terms):
                idx = low.find(terms[0])
                if args.session or kind == "session":
                    excerpt = session_turns_text(text, terms[0])  # 会话命中按轮分节返回原文
                else:
                    excerpt = re.sub(r"\s+", " ", text[max(0, idx - 40): idx + 160]).strip()
                hits.append((rel, excerpt))
            if len(hits) >= args.limit:
                break
        if len(hits) >= args.limit:
            break
    return hits


def main():
    parser = argparse.ArgumentParser(description="在记忆中台目录内做本地全文检索。")
    parser.add_argument("query")
    parser.add_argument("--scope", choices=["project", "global"])
    parser.add_argument("--client")
    parser.add_argument("--kind", choices=["memory", "rule", "session", "workflow"])
    parser.add_argument("--session", help="只检索指定会话（按轮分节返回原文）")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结构化结果")
    args = parser.parse_args()
    terms = [t.lower() for t in args.query.split() if t.strip()][:6] or [args.query.lower()]
    base = ROOT
    if args.scope == "global":
        base = os.path.join(ROOT, "global")
    elif args.scope == "project":
        base = os.path.join(ROOT, "projects")
    hits = search(base, args, terms)
    if args.json:
        print(json.dumps({"query": args.query, "count": len(hits),
                          "hits": [{"file": rel, "excerpt": excerpt} for rel, excerpt in hits]},
                         ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("（没有找到）")
        return 0
    for rel, excerpt in hits:
        print("- %s\n  %s" % (rel, excerpt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
