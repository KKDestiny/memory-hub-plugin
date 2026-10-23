#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory-hub-setup：按宿主已验证的配置契约，幂等地写入钩子与 MCP 配置。

用法：
  python3 setup.py --list                       # 列出各宿主的验证状态与配置文件位置
  python3 setup.py --client <id> --dry-run      # 打印将要写入的内容，不落盘
  python3 setup.py --client <id>                # 幂等写入；写入前备份为 <文件>.hub-bak
  python3 setup.py --client <id> --hooks-only | --mcp-only

行为约束：
  * 不删除、不覆盖宿主配置中已有的条目，只追加本工具自身的声明；钩子条目以命令串中的
    "memory_hub_hook" 作为去重标记。
  * 写入前备份为 <文件>.hub-bak；配置文件无法解析时中止并保留原文件（退出码 4）。
  * 适配表中未确认钩子结构的宿主（hooks 为 null）会明确报告不支持自动安装及原因，不猜测路径。
  * 客户端标识约束：写入 MCP 条目时必须同时注入 "--client <id>" 参数与 DUTI_CLIENT_ID 环境变量；
    对已存在的条目同样补齐或校正（written 为 "updated"）。宿主在 MCP 握手中自报的名称不受本工具
    控制，只有注入的标识才能保证导出目录 clients/<id>/ 与连接侧的授权记录指向同一客户端。
  * 客户端标识以中台登记为准：--client 传入的是宿主 id（适配表的键），实际写入配置的标识从中台
    .state/client-ids.json 的映射表解析；映射表不存在或未命中时使用宿主 id。配置中已有的标识与
    映射表不一致时，改写为映射表中的标识。

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

HOOK_MARKER = "memory_hub_hook"  # 钩子条目的去重标记：同一配置重复写入时据此识别自身条目
CLIENT_ARG = "--client"          # 注入 MCP 配置的客户端标识参数名（与 MCP server 的解析规则一致）
PY = sys.executable


def memory_root():
    """中台数据根目录。环境变量优先级：MEMORY_HUB_ROOT > DUTI_XMEM_ROOT > DUTI_MEMORY_ROOT，缺省 ~/DutiMemory。"""
    return (os.environ.get("MEMORY_HUB_ROOT")
            or os.environ.get("DUTI_XMEM_ROOT")
            or os.environ.get("DUTI_MEMORY_ROOT")
            or os.path.join(os.path.expanduser("~"), "DutiMemory"))


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


def fix_client_arg(obj, client):
    """将配置中已有的钩子命令的 --client 参数校正为中台登记的标识，返回是否有改动。

    钩子在宿主配置中以命令字符串形式存储。用户在中台侧修改标识后，若不在此处校正，
    钩子会继续按旧标识导出，中台会持续出现旧标识的目录。只修改包含 memory_hub_hook
    标记的命令字符串，不改动其他内容。
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
            changed = fix_client_arg(v, client) or changed
    return changed


def merge_hooks(cfg, adapter, client):
    """按适配表的结构写入钩子。返回 (配置, 已写入事件, 已存在事件, 冲突列表, 标识是否被校正)。"""
    spec = adapter["hooks"]
    events = spec["events"]
    written, skipped, conflicts = [], [], []
    id_fixed = fix_client_arg(cfg, client)
    if spec["shape"] == "zcode":
        hooks = cfg.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        hooks["enabled"] = True
        if spec["timeoutPlacement"] == "root-ms":
            hooks["timeoutMs"] = int(spec["timeout"])
        ev = hooks.get("events")
        if not isinstance(ev, dict):
            ev = {}
        for name in events:
            arr = ev.get(name)
            if not isinstance(arr, list):
                arr = []
            if any(has_hook(x) for x in arr):
                skipped.append(name)
                continue
            arr.append({"hooks": [{"type": "command", "command": hook_cmd(client, name), "enabled": True}]})
            ev[name] = arr
            written.append(name)
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
        owner = next((hook_client_of(x) for x in arr if has_hook(x)), "")
        if owner:
            if owner == client:
                skipped.append(name)
            else:
                conflicts.append("%s: 已存在属于客户端 %s 的钩子；该 hooks 文件由多个宿主共用，同一事件只能安装一个钩子" % (name, owner))
            continue
        handler = {"type": "command", "command": hook_cmd(client, name)}
        if spec["timeoutPlacement"] == "handler-seconds":
            handler["timeout"] = int(spec["timeout"])
        arr.append({"hooks": [handler]})
        hooks[name] = arr
        written.append(name)
    cfg["hooks"] = hooks
    return cfg, written, skipped, conflicts, id_fixed


def mcp_server(adapter, client):
    """构造 duti-mcp 的 server 条目。客户端标识为必填项，见模块头的约束说明。"""
    extra = adapter["mcp"].get("serverExtra") or {}
    server_path = ""
    try:
        endpoint = json.load(open(os.path.join(memory_root(), ".state", "duti-app.json"), encoding="utf-8"))
        server_path = endpoint.get("serverPath") or ""
    except (OSError, ValueError):
        server_path = ""
    root = memory_root()
    if adapter["mcp"].get("shape") == "opencode":
        cmd = ["node", server_path] if server_path and os.path.exists(server_path) else ["duti-mcp"]
        return {"type": "local", "command": cmd + [CLIENT_ARG, client],
                "environment": {"DUTI_MEMORY_ROOT": root, "DUTI_CLIENT_ID": client}}
    cmd = ["node", server_path] if server_path and os.path.exists(server_path) else ["duti-mcp"]
    out = dict(extra)
    out["command"] = cmd[0]
    out["args"] = cmd[1:] + [CLIENT_ARG, client]
    out["env"] = {"DUTI_MEMORY_ROOT": root, "DUTI_CLIENT_ID": client}
    return out


def inject_client_id(entry, client, shape="json"):
    """为已存在的 duti-mcp 条目补齐或校正客户端标识，返回是否有改动。

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
        if env.get("DUTI_CLIENT_ID") != client:
            env["DUTI_CLIENT_ID"] = client
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
    if env.get("DUTI_CLIENT_ID") != client:
        env["DUTI_CLIENT_ID"] = client
        changed = True
    entry["env"] = env
    return changed


def merge_mcp_json(cfg, adapter, client):
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
    if "duti-mcp" in box:
        # 已存在的条目不做整条替换，只补齐客户端标识，保证导出侧与连接侧标识一致
        return cfg, ("updated" if inject_client_id(box["duti-mcp"], client, shape) else False)
    box["duti-mcp"] = mcp_server(adapter, client)
    return cfg, "created"


def patch_toml_client(text, client, table):
    """校正 TOML 配置中已存在的条目（Codex）：在 args 数组中补齐或校正 --client，
    在 env 表中补齐或校正 DUTI_CLIENT_ID。返回 (文本, 是否有改动)。

    这是基于文本行的最小实现：找不到本工具写入的标记或结构时原样返回，
    由调用方提示人工处理。
    """
    lines = text.split(chr(10))
    marker_i = next((i for i, l in enumerate(lines) if "duti-mcp (added by memory-hub-setup)" in l), -1)
    if marker_i < 0:
        return text, False
    changed = False
    args_i = -1
    for i in range(marker_i, len(lines)):
        s = lines[i].strip()
        if i > marker_i and s.startswith("["):
            break
        if s.startswith("args = ["):
            args_i = i
            break
    if args_i >= 0:
        cur = lines[args_i].strip()
        body = cur[cur.find("[") + 1: cur.rfind("]")].strip()
        vals = [v.strip() for v in body.split(",") if v.strip()] if body else []
        j = next((i for i, v in enumerate(vals) if v in ('"--client"', "'--client'")), -1)
        if j >= 0:
            if j + 1 < len(vals) and vals[j + 1] != json.dumps(client):
                vals[j + 1] = json.dumps(client)
                changed = True
        else:
            vals += ['"--client"', json.dumps(client)]
            changed = True
        if changed:
            lines[args_i] = "args = [%s]" % ", ".join(vals)
    env_head = "[%s.duti-mcp.env]" % table
    env_i = next((i for i in range(marker_i, len(lines)) if lines[i].strip() == env_head), -1)
    if env_i >= 0:
        end = env_i + 1
        has = False
        while end < len(lines) and not lines[end].strip().startswith("["):
            if lines[end].strip().startswith("DUTI_CLIENT_ID"):
                want = "DUTI_CLIENT_ID = %s" % json.dumps(client)
                if lines[end].strip() != want:
                    lines[end] = want
                    changed = True
                has = True
            end += 1
        if not has:
            lines.insert(env_i + 1, "DUTI_CLIENT_ID = %s" % json.dumps(client))
            changed = True
    return chr(10).join(lines), changed


def merge_mcp_toml(path_, adapter, client):
    """Codex 的 config.toml 写入：标记注释 + 幂等追加或补齐（Python 标准库不提供 TOML 写入）。"""
    marker = "# --- duti-mcp (added by memory-hub-setup) ---"
    server = mcp_server(adapter, client)
    table = adapter["mcp"]["table"]
    text = ""
    if os.path.exists(path_):
        text = open(path_, encoding="utf-8").read()
    if marker in text:
        new_text, changed = patch_toml_client(text, client, table)
        return new_text, ("updated" if changed else False)
    block = [marker, "[%s.duti-mcp]" % table, 'command = "%s"' % server["command"],
             "args = [%s]" % ", ".join(json.dumps(x) for x in server.get("args", [])),
             "[%s.duti-mcp.env]" % table]
    for k, v in (server.get("env") or {}).items():
        block.append("%s = %s" % (k, json.dumps(v)))
    block.append("")
    return (text.rstrip(chr(10)) + chr(10) + chr(10) + chr(10).join(block)), "created"


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--client")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--hooks-only", action="store_true")
    parser.add_argument("--mcp-only", action="store_true")
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
        else:
            mcp_file = expand(adapter["mcp"]["file"])
            if adapter["mcp"]["format"] == "toml":
                text, changed = merge_mcp_toml(mcp_file, adapter, client)
                if not args.dry_run and changed:
                    os.makedirs(os.path.dirname(mcp_file) or ".", exist_ok=True)
                    if os.path.exists(mcp_file):
                        shutil.copy2(mcp_file, mcp_file + ".hub-bak")
                    open(mcp_file, "w", encoding="utf-8").write(text)
                result["mcp"] = {"file": mcp_file, "written": changed, "format": "toml", "clientId": client,
                                 "content": text if args.dry_run else None, "note": adapter["mcp"].get("note")}
            else:
                cfg, ok = read_json(mcp_file)
                if not ok:
                    print(json.dumps({"ok": False, "reason": "配置文件无法解析，已中止写入（可从 %s.hub-bak 恢复）" % mcp_file}, ensure_ascii=False))
                    return 4
                cfg, changed = merge_mcp_json(cfg, adapter, client)
                text = dump_json(mcp_file, cfg, args.dry_run)
                result["mcp"] = {"file": mcp_file, "written": changed, "keys": ".".join(adapter["mcp"]["keys"]), "clientId": client,
                                 "content": text if args.dry_run else None, "note": adapter["mcp"].get("note")}

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
