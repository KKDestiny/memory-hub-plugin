#!/usr/bin/env node
// memory-hub 官方只读 MCP server（产品内置组件）
//
// 作用：把中台目录（~/MemoryHub）的读取能力通过 MCP 标准协议暴露给任何客户端——
// 不依赖对方安装本插件的 skill，只要宿主支持 MCP 即可检索中台。
// 与写入侧的分工：写入（脱敏闸、采集策略、幂等命名）全部在 export skill 的文件通道强制执行，
// 本 server 只读，不提供任何写入工具；配套管理端（执务Studio 桌面应用）的 server 额外提供
// 授权写与聚合索引，是升级选项而非依赖。
//
// 运行契约（与插件一致）：
//   env  MEMORY_HUB_ROOT      中台数据根目录（缺省 ~/MemoryHub）
//   env  MEMORY_HUB_CLIENT_ID 客户端标识（只读 server 不消费身份，仅用于展示）
//   args --client <id>        同上（参数形式）
// 启动方式：由宿主按 MCP stdio 传输拉起子进程（node 本文件），非常驻服务、不监听端口、不联网。
// 依赖：Node.js 18+，零第三方依赖。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";

const SERVER_NAME = "memory-hub";
const SERVER_VERSION = "0.2.0";
const MAX_READ_BYTES = 64 * 1024;
const MAX_EXCERPT = 1200;
const MAX_SEARCH_LIMIT = 50;

const clientArgIdx = process.argv.indexOf("--client");
const CLIENT_ID = String(
  process.env.MEMORY_HUB_CLIENT_ID
  || (clientArgIdx >= 0 ? process.argv[clientArgIdx + 1] : "")
  || "anonymous").trim();

const hubRoot = () =>
  path.resolve(process.env.MEMORY_HUB_ROOT || path.join(os.homedir(), "MemoryHub"));

// ===== 中台目录扫描（与 skills/memory-hub-consume/scripts/search.py 同口径；
//       两侧一致性由 tests/test_search_parity.py 的 golden 对比锁定） =====

const SESSION_RE = /^\d{4}-\d{2}-\d{2}-session-(.+)\.md$/i;
const HASH_RE = /^([a-z]+)-(global|project)-[0-9a-f]{6,8}\.md$/i;
const FIXED_KIND = {
  "project-rules": "rule", "global-rules": "rule", "rules": "rule",
  "project-memory": "memory", "global-memory": "memory", "memory": "memory",
  "workflows": "workflow", "project-workflows": "workflow",
};

function kindOf(filename, text) {
  // frontmatter 的 kind 优先；条目以 "---" 开头，必须用多行模式的 search 而非起始匹配
  const m = text.match(/^kind\s*:\s*(\w+)/im);
  if (m) return m[1].toLowerCase();
  if (SESSION_RE.test(filename)) return "session";
  const h = filename.match(HASH_RE);
  if (h) return h[1].toLowerCase();
  return FIXED_KIND[filename.replace(/\.md$/, "")] || "";
}

function parseFrontmatter(text) {
  if (!text.startsWith("---")) return { meta: {}, body: text };
  const end = text.indexOf("\n---", 3);
  if (end < 0) return { meta: {}, body: text };
  const meta = {};
  for (const line of text.slice(3, end).trim().split("\n")) {
    const m = line.match(/^([A-Za-z_][\w-]*)\s*:\s*(.*)$/);
    if (m) meta[m[1]] = m[2].trim().replace(/^["']|["']$/g, "");
  }
  return { meta, body: text.slice(end + 4).trim() };
}

function* walkMarkdown(dir) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
  for (const ent of entries) {
    if (ent.name.startsWith(".") || ent.name === "index" || ent.name === ".state") continue;
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) yield* walkMarkdown(full);
    else if (ent.isFile() && ent.name.endsWith(".md")) yield full;
  }
}

function sessionSections(body) {
  return body.split(/^(?=##\s*轮\s*\d+)/m);
}

function sessionExcerpt(text, term) {
  if (!/^##\s*轮\s*\d+/m.test(text)) {
    const idx = text.toLowerCase().indexOf(term);
    return text.slice(Math.max(0, idx - 60), idx + 260).replace(/\s+/g, " ").trim();
  }
  const sections = sessionSections(text);
  const hits = [];
  sections.forEach((s, i) => { if (s.toLowerCase().includes(term) && !hits.includes(i)) hits.push(i); });
  const picked = [];
  for (const i of (hits.length ? hits : [0]).slice(0, 3)) {
    for (const j of [i, i + 1]) if (sections[j] !== undefined && !picked.includes(j)) picked.push(j);
  }
  return picked.map((j) => sections[j].trim()).join("\n").slice(0, MAX_EXCERPT);
}

function searchHub({ query, scope, client, kind, limit }) {
  const root = hubRoot();
  const terms = String(query || "").toLowerCase().split(/\s+/).filter(Boolean).slice(0, 6);
  if (!terms.length) throw new Error("query 不能为空");
  const max = Math.max(1, Math.min(Number(limit) || 8, MAX_SEARCH_LIMIT));
  const base = scope === "global" ? path.join(root, "global")
    : scope === "project" ? path.join(root, "projects") : root;
  const hits = [];
  for (const file of walkMarkdown(base)) {
    const rel = path.relative(root, file).split(path.sep).join("/");
    const posix = "/" + rel + "/";
    if (client && !posix.includes(`/clients/${client}/`)) continue;
    let text;
    try { text = fs.readFileSync(file, "utf8"); } catch { continue; }
    const entryKind = kindOf(path.basename(file), text);
    if (kind && entryKind !== kind) continue;
    const low = text.toLowerCase();
    if (!terms.every((t) => low.includes(t))) continue;
    const idx = low.indexOf(terms[0]);
    const excerpt = entryKind === "session"
      ? sessionExcerpt(text, terms[0])
      : text.slice(Math.max(0, idx - 40), idx + 160).replace(/\s+/g, " ").trim();
    hits.push({ file: rel, kind: entryKind, excerpt });
    if (hits.length >= max) break;
  }
  return hits;
}

function resolveInsideRoot(rel) {
  const root = hubRoot();
  const relNorm = String(rel || "").replace(/^[/\\]+/, "");
  const full = path.resolve(root, relNorm);
  if (full !== root && !full.startsWith(root + path.sep)) {
    throw new Error("路径越界：只允许读取中台目录内的条目");
  }
  return full;
}

function readEntry({ file }) {
  const full = resolveInsideRoot(file);
  const rel = path.relative(hubRoot(), full).split(path.sep).join("/");
  if (!fs.existsSync(full) || !fs.statSync(full).isFile()) throw new Error(`条目不存在：${rel}`);
  const buf = fs.readFileSync(full);
  return { file: rel, bytes: buf.length, content: buf.subarray(0, MAX_READ_BYTES).toString("utf8") };
}

function getSession({ file }) {
  const full = resolveInsideRoot(file);
  if (!fs.existsSync(full) || !fs.statSync(full).isFile()) throw new Error(`条目不存在：${file}`);
  const text = fs.readFileSync(full, "utf8");
  const { meta, body } = parseFrontmatter(text);
  if (kindOf(path.basename(full), text) !== "session") {
    throw new Error("不是会话条目（kind != session）");
  }
  const turns = [];
  for (const section of sessionSections(body)) {
    const m = section.match(/^##\s*轮\s*(\d+)(?:\s*·\s*([0-9:]+))?/);
    if (!m) continue;
    turns.push({ n: Number(m[1]), time: m[2] || "", text: section.replace(/^##[^\n]*\n*/, "").trim() });
  }
  if (!turns.length) return { file: String(file), meta, body };
  return { file: String(file), meta: { ...meta, turnCount: turns.length }, turns };
}

function listPartitions() {
  const root = hubRoot();
  const partitions = [];
  let dirs = [];
  try { dirs = fs.readdirSync(path.join(root, "projects"), { withFileTypes: true }); } catch { /* 无 projects 目录 */ }
  for (const ent of dirs) {
    if (!ent.isDirectory() || ent.name.startsWith(".")) continue;
    const entry = { dir: ent.name, entries: 0 };
    const metaFile = path.join(root, "projects", ent.name, "project.json");
    if (fs.existsSync(metaFile)) {
      try { Object.assign(entry, JSON.parse(fs.readFileSync(metaFile, "utf8"))); } catch { /* 损坏的 project.json：仅跳过元信息 */ }
    }
    let count = 0;
    for (const f of walkMarkdown(path.join(root, "projects", ent.name))) count += 1;
    entry.entries = count;
    partitions.push(entry);
  }
  return { partitions };
}

function listClients({ partition } = {}) {
  const root = hubRoot();
  const rows = [];
  const scan = (clientsDir, scope, part) => {
    let dirs = [];
    try { dirs = fs.readdirSync(clientsDir, { withFileTypes: true }); } catch { return; }
    for (const ent of dirs) {
      if (!ent.isDirectory() || ent.name.startsWith(".")) continue;
      let count = 0;
      for (const f of walkMarkdown(path.join(clientsDir, ent.name))) count += 1;
      const row = { client: ent.name, scope, entries: count };
      if (part) row.partition = part;
      rows.push(row);
    }
  };
  if (partition) {
    scan(path.join(root, "projects", String(partition), "clients"), "project", String(partition));
  } else {
    scan(path.join(root, "global", "clients"), "global");
    let dirs = [];
    try { dirs = fs.readdirSync(path.join(root, "projects"), { withFileTypes: true }); } catch { /* 无 */ }
    for (const ent of dirs) {
      if (ent.isDirectory() && !ent.name.startsWith(".")) {
        scan(path.join(root, "projects", ent.name, "clients"), "project", ent.name);
      }
    }
  }
  rows.sort((a, b) => a.client.localeCompare(b.client) || (a.partition || "").localeCompare(b.partition || ""));
  return { clients: rows };
}

function readCatalog() {
  const file = path.join(hubRoot(), "index", "catalog.json");
  if (!fs.existsSync(file)) {
    throw new Error("索引不存在：index/catalog.json 由配套管理端聚合生成，当前中台没有该文件");
  }
  const buf = fs.readFileSync(file);
  return { catalog: JSON.parse(buf.subarray(0, MAX_READ_BYTES).toString("utf8")) };
}

function hubStats() {
  const root = hubRoot();
  const byKind = {};
  const clients = new Set();
  let partitions = 0;
  let entries = 0;
  try {
    for (const ent of fs.readdirSync(path.join(root, "projects"), { withFileTypes: true })) {
      if (ent.isDirectory() && !ent.name.startsWith(".")) partitions += 1;
    }
  } catch { /* 目录不存在 */ }
  for (const file of walkMarkdown(root)) {
    entries += 1;
    const rel = path.relative(root, file).split(path.sep).join("/");
    const m = rel.match(/clients\/([^/]+)\//);
    if (m) clients.add(m[1]);
    const text = fs.readFileSync(file, "utf8");
    const k = kindOf(path.basename(file), text) || "unknown";
    byKind[k] = (byKind[k] || 0) + 1;
  }
  return { root, partitions, entries, clients: [...clients].sort(), byKind };
}

// ===== MCP（stdio / JSON-RPC 2.0） =====

const TOOLS = [
  {
    name: "search_hub",
    description: "在中台目录内做本地全文检索（多关键词为 AND）。返回命中条目的相对路径、类别与摘录；会话命中按「轮」分节给出原文。",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "关键词，空格分隔多个" },
        scope: { type: "string", enum: ["project", "global"] },
        client: { type: "string", description: "只检索指定客户端的目录（如 claude-code）" },
        kind: { type: "string", enum: ["memory", "rule", "session", "workflow"] },
        limit: { type: "number", description: "命中条数上限（缺省 8，最大 50）" },
      },
      required: ["query"],
    },
  },
  {
    name: "read_entry",
    description: "读取中台内一个条目的完整内容（相对路径，限 64KB）。",
    inputSchema: {
      type: "object",
      properties: { file: { type: "string", description: "相对中台根目录的路径" } },
      required: ["file"],
    },
  },
  {
    name: "get_session",
    description: "按轮结构化读取一个会话条目：frontmatter 元信息 + 各轮（轮号/时间/原文）。",
    inputSchema: {
      type: "object",
      properties: { file: { type: "string", description: "会话条目的相对路径" } },
      required: ["file"],
    },
  },
  {
    name: "list_partitions",
    description: "列出中台的全部项目分区：目录名、project.json 元信息（路径/名称）与条目数。",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "list_clients",
    description: "列出已登记客户端及其条目数。可选按分区过滤；缺省返回全局 + 全部分区。",
    inputSchema: {
      type: "object",
      properties: { partition: { type: "string", description: "分区目录名（缺省为全部）" } },
    },
  },
  {
    name: "read_catalog",
    description: "读取管理端聚合生成的索引导航（index/catalog.json）；中台没有该文件时返回错误。",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "hub_stats",
    description: "中台统计：分区数、条目数（按类别）、已登记客户端。",
    inputSchema: { type: "object", properties: {} },
  },
];

function handleCall(name, args) {
  switch (name) {
    case "search_hub": {
      const hits = searchHub(args || {});
      return { hits, count: hits.length };
    }
    case "read_entry":
      return readEntry(args || {});
    case "get_session":
      return getSession(args || {});
    case "list_partitions":
      return listPartitions();
    case "list_clients":
      return listClients(args || {});
    case "read_catalog":
      return readCatalog();
    case "hub_stats":
      return hubStats();
    default:
      throw new Error(`未知工具：${name}`);
  }
}

const rl = readline.createInterface({ input: process.stdin });
function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

rl.on("line", (line) => {
  let req;
  try { req = JSON.parse(line); } catch { return; }
  if (req.id === undefined) return; // 通知（如 notifications/initialized）：无需响应
  let result, error;
  try {
    switch (req.method) {
      case "initialize":
        result = {
          protocolVersion: "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
          instructions: `只读记忆中台检索。数据根：${hubRoot()}；客户端标识：${CLIENT_ID}（只读，仅展示）。写入请使用 memory-hub-plugin 的 export skill。`,
        };
        break;
      case "tools/list":
        result = { tools: TOOLS };
        break;
      case "tools/call": {
        const out = handleCall(req.params?.name, req.params?.arguments);
        result = { content: [{ type: "text", text: typeof out === "string" ? out : JSON.stringify(out, null, 2) }] };
        break;
      }
      case "ping":
        result = {};
        break;
      default:
        error = { code: -32601, message: `method not found: ${req.method}` };
    }
  } catch (e) {
    if (req.method === "tools/call") {
      // 工具执行失败按 MCP 约定返回 isError，而不是协议错误
      result = { content: [{ type: "text", text: String(e.message || e) }], isError: true };
    } else {
      error = { code: -32000, message: String(e.message || e) };
    }
  }
  send(error ? { jsonrpc: "2.0", id: req.id, error } : { jsonrpc: "2.0", id: req.id, result });
});

process.stderr.write(`[${SERVER_NAME}] ready. hub=${hubRoot()} client=${CLIENT_ID} (read-only)\n`);
