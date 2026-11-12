#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory-hub-setup：按宿主已验证的配置契约，幂等地写入钩子与 MCP 配置。

用法：
  python3 setup.py --list                                    # 列出各宿主的验证状态与配置文件位置
  python3 setup.py --client <id> --dry-run                   # 打印将要写入的内容，不落盘
  python3 setup.py --client <id>                             # 幂等写入；写入前备份为 <文件>.hub-bak
  python3 setup.py --client <id> --hooks-only | --mcp-only
  python3 setup.py --client <id> --mcp-source plugin|desktop # MCP 条目来源，缺省 plugin

行为约束：
  * 不删除、不覆盖宿主配置中已有的条目，只追加本工具自身的声明；钩子条目以命令串中的
    "memory_hub_hook" 作为去重标记。
  * 写入前备份为 <文件>.hub-bak；配置文件无法解析时中止并保留原文件（退出码 4）。
  * 适配表中未确认钩子结构的宿主（hooks 为 null）会明确报告不支持自动安装及原因，不猜测路径。
  * MCP 来源（--mcp-source）：
      plugin（缺省）——写入本插件自带的只读 server（mcp/memory-hub-mcp.mjs）。只读 server 不消费
        客户端身份，条目只带 env MEMORY_HUB_ROOT，不带 --client / MEMORY_HUB_CLIENT_ID。
      desktop——写入配套管理端的完整 server（授权写、聚合索引）。此时必须注入 "--client <id>"
        参数与 MEMORY_HUB_CLIENT_ID 环境变量：宿主在 MCP 握手中自报的名称不受本工具控制，只有
        注入的标识才能保证导出目录 clients/<id>/ 与连接侧的授权记录指向同一客户端。
  * 来源冲突不静默改写：默认（plugin）模式下发现既有条目指向 desktop server 时，mcp.written
    返回 "conflict" 并说明改用 --mcp-source desktop 或先删除既有条目；显式指定 desktop 时允许
    把 plugin 条目改写为 desktop 条目（用户明确要求的来源切换）。
  * 客户端标识以中台登记为准：--client 传入的是宿主 id（适配表的键），实际写入配置的标识从中台
    .state/client-ids.json 的映射表解析；映射表不存在或未命中时使用宿主 id。归属本客户端的既有
    条目，其标识与映射值不一致时改写为映射值；其他客户端的条目保持原样并报告冲突。

退出码：0 成功（含跳过）；3 未识别的客户端；4 配置损坏或写入失败。
"""
import argparse
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTERS_FILE = os.path.join(HERE, "adapters.json")
with open(ADAPTERS_FILE, encoding="utf-8") as _f:
    ADAPTERS = json.load(_f)
# adapters.json 顶层的 _meta 是适配表自身的说明元数据，不是宿主条目，加载时剥离
ADAPTERS_META = ADAPTERS.pop("_meta", {})

HOOK_MARKER = "memory_hub_hook"  # 钩子条目的去重标记：同一配置重复写入时据此识别自身条目
CLIENT_ARG = "--client"          # 注入 MCP 配置的客户端标识参数名（与 MCP server 的解析规则一致）
PY = sys.executable
# 本插件自带的只读 MCP server（plugin 源）：skills/memory-hub-setup/scripts → 仓库根 → mcp/
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PLUGIN_MCP = os.path.join(PLUGIN_ROOT, "mcp", "memory-hub-mcp.mjs")


def memory_root():
    """中台数据根目录。环境变量 MEMORY_HUB_ROOT 优先；缺省为 ~/MemoryHub。"""
    return os.environ.get("MEMORY_HUB_ROOT") or os.path.join(os.path.expanduser("~"), "MemoryHub")


def canonical_client(host):
    """宿主 id → 中台登记的规范客户端标识。映射表不存在或未命中时，视为未登记，返回宿主 id。"""
    try:
        data = json.load(open(os.path.join(memory_root(), ".state", "client-ids.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return host
    for key, entry in (data.get("clients") or {}).items():
        if not isinstance(entry, dict):
            continue
        ids = set([key, entry.get("id")])
        ids.update(entry.get("hosts") or [])
        ids.update(entry.get("aliases") or [])
        if host in ids:
            return entry.get("id") or key
    return host


HOOK_PY = os.path.join(os.path.dirname(os.path.dirname(HERE)), "memory-hub-export", "scripts", "memory_hub_hook.py")

# 会话标题来源：目前仅 ZCode 需要显式传入 --with-title 才会读取其自身会话列表中的标题
TITLE_HOSTS = ("zcode",)


def hook_cmd(client, event):
    extra = " --with-title" if client in TITLE_HOSTS else ""
    return "%s %s --client %s --event %s%s" % (PY, HOOK_PY, client, event, extra)


def expand(path_):
    return os.path.expanduser(path_)


def read_json(path_):
    """读取 JSON 配置。返回 (数据, 是否可解析)；文件不存在时返回 ({}, True)。"""
    if not os.path.exists(path_):
        return {}, True
    try:
        return json.load(open(path_, encoding="utf-8")), True
    except ValueError:
        return None, False


def dump_json(path_, data, dry):
    """序列化并写回 JSON；dry 为真时仅返回文本不落盘。写入前备份原文件。"""
    text = json.dumps(data, ensure_ascii=False, indent=2) + chr(10)
    if dry:
        return text
    os.makedirs(os.path.dirname(path_) or ".", exist_ok=True)
    if os.path.exists(path_):
        shutil.copy2(path_, path_ + ".hub-bak")
    open(path_, "w", encoding="utf-8").write(text)
    return text


def hook_client_of(entry):
    """识别条目中的钩子属于哪个客户端（无钩子时返回空串）。同一 hooks 文件可能被多个宿主共用，需据此识别归属。"""
    text = json.dumps(entry)
    if HOOK_MARKER not in text:
        return ""
    for part in text.split("--client")[1:]:
        rest = part.strip().split()[0] if part.strip() else ""
        if rest and not rest.startswith("-"):
            return rest
    return "unknown"


def has_hook(entry):
    return HOOK_MARKER in json.dumps(entry)


def fix_entry_client(obj, client):
    """将单个钩子条目命令中的 --client 参数校正为中台登记的标识，返回是否有改动。

    钩子在宿主配置中以命令字符串形式存储。用户在中台侧修改标识后，若不在此处校正，
    钩子会继续按旧标识导出。仅处理包含 memory_hub_hook 标记的命令字符串，且仅用于
    归属本客户端的条目；其他客户端的条目由调用方报告冲突，不做修改。
    """
    if isinstance(obj, dict):
        items = list(obj.items())
    elif isinstance(obj, list):
        items = list(enumerate(obj))
    else:
        return False
    changed = False
    for k, v in items:
        if isinstance(v, str):
            if HOOK_MARKER in v:
                new = re.sub(r"--client\s+\S+", "--client %s" % client, v)
                if new != v:
                    obj[k] = new
                    changed = True
        elif isinstance(v, (dict, list)):
            changed = fix_entry_client(v, client) or changed
    return changed


def merge_hooks(cfg, adapter, client, resolve=None):
    """按适配表的结构写入钩子。返回 (配置, 已写入事件, 已存在事件, 冲突列表, 标识是否被校正)。

    同一 hooks 文件可能被多个宿主共用（如 ~/.trae/hooks.json）：先判定已安装钩子的归属，
    仅对归属同一客户端的条目做标识校正；其他客户端的条目保持原样并报告冲突。
    resolve 用于将命令中的客户端标识映射到规范标识，缺省使用中台登记表 canonical_client；
    测试可注入恒等映射以隔离环境。
    """
    if resolve is None:
        resolve = canonical_client
    spec = adapter["hooks"]
    events = spec["events"]
    written, skipped, conflicts = [], [], []
    id_fixed = False

    def merge_event(name, arr, handler_extra):
        nonlocal id_fixed
        owner = next((hook_client_of(x) for x in arr if has_hook(x)), "")
        if owner and owner != "unknown" and resolve(owner) != client:
            conflicts.append("%s: 已存在属于客户端 %s 的钩子；该 hooks 文件由多个宿主共用，同一事件只能安装一个钩子" % (name, owner))
            return
        for entry in arr:
            if has_hook(entry):
                id_fixed = fix_entry_client(entry, client) or id_fixed
        if owner:
            skipped.append(name)
            return
        handler = {"type": "command", "command": hook_cmd(client, name)}
        handler.update(handler_extra)
        if spec["timeoutPlacement"] == "handler-seconds":
            handler["timeout"] = int(spec["timeout"])
        arr.append({"hooks": [handler]})
        written.append(name)

    if spec["shape"] == "zcode":
        hooks = cfg.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        # 仅在字段缺失时写默认值，不覆盖用户已显式设置的开关与超时
        hooks.setdefault("enabled", True)
        if spec["timeoutPlacement"] == "root-ms":
            hooks.setdefault("timeoutMs", int(spec["timeout"]))
        ev = hooks.get("events")
        if not isinstance(ev, dict):
            ev = {}
        for name in events:
            arr = ev.get(name)
            if not isinstance(arr, list):
                arr = []
            merge_event(name, arr, {"enabled": True})
            ev[name] = arr
        hooks["events"] = ev
        cfg["hooks"] = hooks
        return cfg, written, skipped, conflicts, id_fixed
    # flat 结构：Claude Code 系（WorkBuddy、Qoder、MiniMax Code、TRAE 系列）
    for k, v in (spec.get("rootExtra") or {}).items():
        cfg.setdefault(k, v)
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for name in events:
        arr = hooks.get(name)
        if not isinstance(arr, list):
            arr = []
        merge_event(name, arr, {})
        hooks[name] = arr
    cfg["hooks"] = hooks
    return cfg, written, skipped, conflicts, id_fixed


def mcp_entry_source(entry):
    """识别既有 memory-hub 条目指向哪个来源的 server：plugin（本插件只读 server）/ desktop。"""
    text = json.dumps(entry)
    if "memory-hub-mcp.mjs" in text:
        return "plugin"
    return "desktop"


def mcp_server(adapter, client, source="plugin"):
    """构造 memory-hub 的 server 条目。source 语义与模块头「MCP 来源」一致：
    plugin = 本插件只读 server（不注入客户端标识）；desktop = 管理端完整 server（必须注入标识）。"""
    extra = adapter["mcp"].get("serverExtra") or {}
    root = memory_root()
    if source == "plugin":
        if adapter["mcp"].get("shape") == "opencode":
            return {"type": "local", "command": ["node", PLUGIN_MCP],
                    "environment": {"MEMORY_HUB_ROOT": root}}
        out = dict(extra)
        out["command"] = "node"
        out["args"] = [PLUGIN_MCP]
        out["env"] = {"MEMORY_HUB_ROOT": root}
        return out
    server_path = ""
    try:
        endpoint = json.load(open(os.path.join(memory_root(), ".state", "app.json"), encoding="utf-8"))
        server_path = endpoint.get("serverPath") or ""
    except (OSError, ValueError):
        server_path = ""
    if adapter["mcp"].get("shape") == "opencode":
        cmd = ["node", server_path] if server_path and os.path.exists(server_path) else ["memory-hub"]
        return {"type": "local", "command": cmd + [CLIENT_ARG, client],
                "environment": {"MEMORY_HUB_ROOT": root, "MEMORY_HUB_CLIENT_ID": client}}
    cmd = ["node", server_path] if server_path and os.path.exists(server_path) else ["memory-hub"]
    out = dict(extra)
    out["command"] = cmd[0]
    out["args"] = cmd[1:] + [CLIENT_ARG, client]
    out["env"] = {"MEMORY_HUB_ROOT": root, "MEMORY_HUB_CLIENT_ID": client}
    return out


def inject_client_id(entry, client, shape="json"):
    """为已存在的 memory-hub 条目补齐或校正客户端标识，返回是否有改动。

    缺失时补上；取值与中台登记不一致时改为登记值。只修改标识相关字段，
    不改动条目中的其他字段。
    """
    if not isinstance(entry, dict) or not client:
        return False
    changed = False
    if shape == "opencode":
        cmd = entry.get("command")
        if isinstance(cmd, list):
            vals = [str(x) for x in cmd]
            if CLIENT_ARG in vals:
                i = vals.index(CLIENT_ARG)
                if i + 1 < len(cmd) and str(cmd[i + 1]) != client:
                    cmd[i + 1] = client
                    changed = True
            else:
                cmd.extend([CLIENT_ARG, client])
                changed = True
        env = entry.get("environment")
        if not isinstance(env, dict):
            env = {}
        if env.get("MEMORY_HUB_CLIENT_ID") != client:
            env["MEMORY_HUB_CLIENT_ID"] = client
            changed = True
        entry["environment"] = env
        return changed
    args = entry.get("args")
    if isinstance(args, list):
        vals = [str(x) for x in args]
        if CLIENT_ARG in vals:
            i = vals.index(CLIENT_ARG)
            if i + 1 < len(args) and str(args[i + 1]) != client:
                args[i + 1] = client
                changed = True
        else:
            args.extend([CLIENT_ARG, client])
            changed = True
    env = entry.get("env")
    if not isinstance(env, dict):
        env = {}
    if env.get("MEMORY_HUB_CLIENT_ID") != client:
        env["MEMORY_HUB_CLIENT_ID"] = client
        changed = True
    entry["env"] = env
    return changed


def merge_mcp_json(cfg, adapter, client, source="plugin"):
    keys = adapter["mcp"]["keys"]
    cur = cfg
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
        cur[k] = nxt
        cur = nxt
    box = cur.get(keys[-1])
    if not isinstance(box, dict):
        box = {}
    cur[keys[-1]] = box
    shape = adapter["mcp"].get("shape") or "json"
    if "memory-hub" in box:
        entry = box["memory-hub"]
        existing_source = mcp_entry_source(entry)
        if source == "plugin":
            if existing_source != "plugin":
                # 既有条目指向 desktop server：默认模式不改写，报告冲突交由用户选择来源
                return cfg, "conflict"
            # 同源的 plugin 条目：只校正数据根（文件路径迁移、MEMORY_HUB_ROOT 变更后重跑可修复）
            want = mcp_server(adapter, client, "plugin")
            if json.dumps(entry, sort_keys=True) != json.dumps(want, sort_keys=True):
                box["memory-hub"] = want
                return cfg, "updated"
            return cfg, False
        # desktop 源：plugin 条目按用户明确的来源切换整条改写；desktop 条目只补齐/校正标识
        if existing_source == "plugin":
            box["memory-hub"] = mcp_server(adapter, client, "desktop")
            return cfg, "updated"
        return cfg, ("updated" if inject_client_id(entry, client, shape) else False)
    box["memory-hub"] = mcp_server(adapter, client, source)
    return cfg, "created"


def parse_toml_string_array(body):
    """解析 TOML 字符串数组的元素值，支持双引号、单引号与反斜杠转义（含逗号的字符串不会被拆分）。
    body 为方括号内的文本；本工具写入的 args 元素均为字符串。"""
    values = []
    current = []
    quote = ""
    escaped = False

    def flush():
        text = "".join(current).strip()
        if text:
            values.append(text)
        del current[:]

    for ch in body:
        if quote:
            if escaped:
                current.append(ch)
                escaped = False
            elif ch == "\\":
                current.append(ch)
                escaped = True
            elif ch == quote:
                quote = ""
            else:
                current.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            flush()
        else:
            current.append(ch)
    flush()
    return values


def patch_toml_client(text, client, table):
    """校正 TOML 配置中已存在的条目（Codex）：在 args 数组中补齐或校正 --client，
    在 env 表中补齐或校正 MEMORY_HUB_CLIENT_ID。返回 (文本, 是否有改动)。

    这是基于文本行的最小实现：找不到本工具写入的标记或结构时原样返回，
    由调用方提示人工处理。
    """
    lines = text.split(chr(10))
    marker_i = next((i for i, l in enumerate(lines) if "memory-hub (added by memory-hub-setup)" in l), -1)
    if marker_i < 0:
        return text, False
    changed = False
    # args 行位于本工具写入的表头 [table.memory-hub] 之后、下一个表头之前；
    # 表头本身以 "[" 开头，扫描必须先定位表头再读取 args，不能见到 "[" 就终止
    table_head = "[%s.memory-hub]" % table
    args_i = -1
    seen_table = False
    for i in range(marker_i, len(lines)):
        s = lines[i].strip()
        if not seen_table:
            if s == table_head:
                seen_table = True
            continue
        if s.startswith("args = ["):
            args_i = i
            break
        if s.startswith("["):
            break
    if args_i >= 0:
        cur = lines[args_i].strip()
        body = cur[cur.find("[") + 1: cur.rfind("]")]
        vals = parse_toml_string_array(body)
        j = next((i for i, v in enumerate(vals) if v == CLIENT_ARG), -1)
        if j >= 0:
            if j + 1 < len(vals) and vals[j + 1] != client:
                vals[j + 1] = client
                changed = True
        else:
            vals += [CLIENT_ARG, client]
            changed = True
        if changed:
            lines[args_i] = "args = [%s]" % ", ".join(json.dumps(v) for v in vals)
    env_head = "[%s.memory-hub.env]" % table
    env_i = next((i for i in range(marker_i, len(lines)) if lines[i].strip() == env_head), -1)
    if env_i >= 0:
        end = env_i + 1
        has = False
        while end < len(lines) and not lines[end].strip().startswith("["):
            if lines[end].strip().startswith("MEMORY_HUB_CLIENT_ID"):
                want = "MEMORY_HUB_CLIENT_ID = %s" % json.dumps(client)
                if lines[end].strip() != want:
                    lines[end] = want
                    changed = True
                has = True
            end += 1
        if not has:
            lines.insert(env_i + 1, "MEMORY_HUB_CLIENT_ID = %s" % json.dumps(client))
            changed = True
    return chr(10).join(lines), changed


def merge_mcp_toml(path_, adapter, client, source="plugin"):
    """Codex 的 config.toml 写入：标记注释 + 幂等追加或补齐（Python 标准库不提供 TOML 写入）。
    来源语义与 merge_mcp_json 一致：plugin 条目不含标识；desktop 条目含 --client 与 env。"""
    marker = "# --- memory-hub (added by memory-hub-setup) ---"
    server = mcp_server(adapter, client, source)
    table = adapter["mcp"]["table"]
    text = ""
    if os.path.exists(path_):
        text = open(path_, encoding="utf-8").read()
    if marker in text:
        block_text = text[marker_i: marker_i + 2000] if (marker_i := text.find(marker)) >= 0 else ""
        existing_source = "plugin" if "memory-hub-mcp.mjs" in block_text else "desktop"
        if source == "plugin":
            if existing_source != "plugin":
                return text, "conflict"
            want_root = memory_root()
            want_env = "MEMORY_HUB_ROOT = %s" % json.dumps(want_root)
            if want_env not in block_text:
                # 数据根变化：按新来源整段重建（以重建代替行级补丁，避免文本拼接漂移）
                new_text = text[:text.find(marker)].rstrip(chr(10)) + chr(10) + chr(10) + chr(10).join(
                    [marker, "[%s.memory-hub]" % table, 'command = "%s"' % server["command"],
                     "args = [%s]" % ", ".join(json.dumps(x) for x in server.get("args", [])),
                     "[%s.memory-hub.env]" % table]
                    + ["%s = %s" % (k, json.dumps(v)) for k, v in (server.get("env") or {}).items()] + [""])
                return new_text, "updated"
            return text, False
        # desktop：plugin 条目整段重建为 desktop；desktop 条目走标识补齐/校正
        if existing_source == "plugin":
            new_text = text[:text.find(marker)].rstrip(chr(10)) + chr(10) + chr(10) + chr(10).join(
                [marker, "[%s.memory-hub]" % table, 'command = "%s"' % server["command"],
                 "args = [%s]" % ", ".join(json.dumps(x) for x in server.get("args", [])),
                 "[%s.memory-hub.env]" % table]
                + ["%s = %s" % (k, json.dumps(v)) for k, v in (server.get("env") or {}).items()] + [""])
            return new_text, "updated"
        new_text, changed = patch_toml_client(text, client, table)
        return new_text, ("updated" if changed else False)
    block = [marker, "[%s.memory-hub]" % table, 'command = "%s"' % server["command"],
             "args = [%s]" % ", ".join(json.dumps(x) for x in server.get("args", [])),
             "[%s.memory-hub.env]" % table]
    for k, v in (server.get("env") or {}).items():
        block.append("%s = %s" % (k, json.dumps(v)))
    block.append("")
    return (text.rstrip(chr(10)) + chr(10) + chr(10) + chr(10).join(block)), "created"


def main():
    parser = argparse.ArgumentParser(description="按宿主已验证的配置契约，幂等地写入钩子与 MCP 配置。")
    parser.add_argument("--client")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--hooks-only", action="store_true")
    parser.add_argument("--mcp-only", action="store_true")
    parser.add_argument("--mcp-source", choices=["plugin", "desktop"], default="plugin",
                        help="MCP 条目来源：plugin=本插件只读 server（缺省）；desktop=配套管理端完整 server（注入客户端标识）")
    args = parser.parse_args()

    if args.list or not args.client:
        rows = [{"client": k, "label": v["label"], "status": v["status"], "hooks": (v["hooks"] or {}).get("file"),
                 "hooksEvents": (v["hooks"] or {}).get("events"), "mcp": (v["mcp"] or {}).get("file"),
                 "evidence": v["evidence"]} for k, v in ADAPTERS.items()]
        print(json.dumps({"adapters": rows}, ensure_ascii=False, indent=2))
        return 0
    adapter = ADAPTERS.get(args.client)
    if not adapter:
        print(json.dumps({"ok": False, "reason": "unknown client; see --list", "adapters": sorted(ADAPTERS)}, ensure_ascii=False))
        return 3

    # 写入配置的标识为中台登记值（未登记时等于宿主 id）
    client = canonical_client(args.client)
    result = {"ok": True, "client": args.client, "clientId": client, "label": adapter["label"], "status": adapter["status"],
              "dryRun": bool(args.dry_run), "hooks": None, "mcp": None, "skipped": [], "evidence": adapter["evidence"]}

    if not args.mcp_only:
        if not adapter.get("hooks"):
            result["hooks"] = {"skipped": True, "reason": adapter.get("hooksNote") or "该宿主不支持自动安装钩子"}
            result["skipped"].append("hooks: %s" % result["hooks"]["reason"])
        else:
            spec = adapter["hooks"]
            config_file = expand(spec["file"])
            if not os.path.exists(config_file) and not spec.get("create"):
                result["hooks"] = {"skipped": True,
                                   "reason": "配置文件不存在，且适配表规定不自动创建（避免在未初始化的环境中猜测路径）",
                                   "file": config_file}
                result["skipped"].append("hooks: 配置文件不存在 %s" % config_file)
            else:
                cfg, ok = read_json(config_file)
                if not ok:
                    print(json.dumps({"ok": False, "reason": "配置文件无法解析，已中止写入（可从 %s.hub-bak 恢复）" % config_file}, ensure_ascii=False))
                    return 4
                cfg, written, skipped, conflicts, id_fixed = merge_hooks(cfg, adapter, client)
                text = dump_json(config_file, cfg, args.dry_run)
                result["hooks"] = {"file": config_file, "events": spec["events"], "written": written,
                                   "already": skipped, "conflicts": conflicts, "clientId": client,
                                   "idFixed": id_fixed, "note": spec.get("note"),
                                   "content": text if args.dry_run else None}

    if not args.hooks_only:
        if not adapter.get("mcp"):
            result["mcp"] = {"skipped": True, "reason": "该宿主的 MCP 配置位置未确认"}
        elif args.mcp_source == "plugin" and not os.path.exists(PLUGIN_MCP):
            result["mcp"] = {"skipped": True, "source": "plugin",
                             "reason": "缺少 mcp/memory-hub-mcp.mjs：请整包安装插件仓库（--mcp-source desktop 可改装管理端 server）"}
            result["skipped"].append("mcp: %s" % result["mcp"]["reason"])
        else:
            mcp_file = expand(adapter["mcp"]["file"])
            if adapter["mcp"]["format"] == "toml":
                text, changed = merge_mcp_toml(mcp_file, adapter, client, args.mcp_source)
                if changed not in (False, "conflict") and not args.dry_run:
                    os.makedirs(os.path.dirname(mcp_file) or ".", exist_ok=True)
                    if os.path.exists(mcp_file):
                        shutil.copy2(mcp_file, mcp_file + ".hub-bak")
                    open(mcp_file, "w", encoding="utf-8").write(text)
                mcp_result = {"file": mcp_file, "written": changed, "format": "toml",
                              "source": args.mcp_source, "clientId": client,
                              "content": text if args.dry_run else None, "note": adapter["mcp"].get("note")}
                if changed == "conflict":
                    mcp_result["reason"] = "既有 memory-hub 条目指向管理端 server（desktop 源）：默认不改写。确需切换请显式 --mcp-source desktop，或先删除既有条目"
                    result["skipped"].append("mcp: 来源冲突（desktop 条目已存在）")
                result["mcp"] = mcp_result
            else:
                cfg, ok = read_json(mcp_file)
                if not ok:
                    print(json.dumps({"ok": False, "reason": "配置文件无法解析，已中止写入（可从 %s.hub-bak 恢复）" % mcp_file}, ensure_ascii=False))
                    return 4
                cfg, changed = merge_mcp_json(cfg, adapter, client, args.mcp_source)
                text = dump_json(mcp_file, cfg, args.dry_run)
                mcp_result = {"file": mcp_file, "written": changed, "keys": ".".join(adapter["mcp"]["keys"]),
                              "source": args.mcp_source, "clientId": client,
                              "content": text if args.dry_run else None, "note": adapter["mcp"].get("note")}
                if changed == "conflict":
                    mcp_result["reason"] = "既有 memory-hub 条目指向管理端 server（desktop 源）：默认不改写。确需切换请显式 --mcp-source desktop，或先删除既有条目"
                    result["skipped"].append("mcp: 来源冲突（desktop 条目已存在）")
                result["mcp"] = mcp_result

    result["input"] = (adapter.get("hooks") or {}).get("input") or {}
    result["sessions"] = adapter.get("sessions")
    result["rules"] = adapter.get("rules")
    result["autoExport"] = adapter.get("autoExport")
    result["autoNote"] = adapter.get("autoNote")
    result["reload"] = "重启宿主后生效" + ("" if adapter.get("hooks") else "；该宿主无钩子机制，需手动导出")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
