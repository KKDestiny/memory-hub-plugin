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
  * MCP 条目唯一来源是本插件自带的只读 server（mcp/memory-hub-mcp.mjs，env MEMORY_HUB_ROOT）。
    只读 server 不消费客户端身份，条目不带 --client / MEMORY_HUB_CLIENT_ID；写入中台统一走
    export skill 的文件通道。执务Studio 的能力 server（duti-studio）与记忆中台接入无关，
    不由本工具写入。

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


def mcp_server(adapter):
    """构造 memory-hub 的 server 条目（唯一来源：本插件自带的只读 server，env 只带数据根）。"""
    extra = adapter["mcp"].get("serverExtra") or {}
    root = memory_root()
    if adapter["mcp"].get("shape") == "opencode":
        return {"type": "local", "command": ["node", PLUGIN_MCP],
                "environment": {"MEMORY_HUB_ROOT": root}}
    out = dict(extra)
    out["command"] = "node"
    out["args"] = [PLUGIN_MCP]
    out["env"] = {"MEMORY_HUB_ROOT": root}
    return out


def merge_mcp_json(cfg, adapter):
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
    if "memory-hub" in box:
        # 已存在的条目：只校正数据根（文件位置迁移、MEMORY_HUB_ROOT 变更后重跑可修复），其余不动
        want = mcp_server(adapter)
        if json.dumps(box["memory-hub"], sort_keys=True) != json.dumps(want, sort_keys=True):
            box["memory-hub"] = want
            return cfg, "updated"
        return cfg, False
    box["memory-hub"] = mcp_server(adapter)
    return cfg, "created"


def merge_mcp_toml(path_, adapter):
    """Codex 的 config.toml 写入：标记注释 + 幂等追加（Python 标准库不提供 TOML 写入）。
    已存在本工具写入的段时，只校正 env 中的数据根（MEMORY_HUB_ROOT 变更后重跑可修复）。"""
    marker = "# --- memory-hub (added by memory-hub-setup) ---"
    server = mcp_server(adapter)
    table = adapter["mcp"]["table"]
    text = ""
    if os.path.exists(path_):
        text = open(path_, encoding="utf-8").read()
    if marker in text:
        marker_i = text.find(marker)
        block_text = text[marker_i: marker_i + 2000]
        want_env = "MEMORY_HUB_ROOT = %s" % json.dumps(memory_root())
        if want_env in block_text:
            return text, False
        # 数据根变化：整段按新值重建（以重建代替行级补丁，避免文本拼接漂移）
        head = text[:marker_i].rstrip(chr(10))
        block = [marker, "[%s.memory-hub]" % table, 'command = "%s"' % server["command"],
                 "args = [%s]" % ", ".join(json.dumps(x) for x in server.get("args", [])),
                 "[%s.memory-hub.env]" % table]
        for k, v in (server.get("env") or {}).items():
            block.append("%s = %s" % (k, json.dumps(v)))
        block.append("")
        return head + chr(10) + chr(10) + chr(10).join(block), "updated"
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
        elif not os.path.exists(PLUGIN_MCP):
            result["mcp"] = {"skipped": True,
                             "reason": "缺少 mcp/memory-hub-mcp.mjs：请整包安装插件仓库"}
            result["skipped"].append("mcp: %s" % result["mcp"]["reason"])
        else:
            mcp_file = expand(adapter["mcp"]["file"])
            if adapter["mcp"]["format"] == "toml":
                text, changed = merge_mcp_toml(mcp_file, adapter)
                if changed and not args.dry_run:
                    os.makedirs(os.path.dirname(mcp_file) or ".", exist_ok=True)
                    if os.path.exists(mcp_file):
                        shutil.copy2(mcp_file, mcp_file + ".hub-bak")
                    open(mcp_file, "w", encoding="utf-8").write(text)
                mcp_result = {"file": mcp_file, "written": changed, "format": "toml",
                              "clientId": client,
                              "content": text if args.dry_run else None, "note": adapter["mcp"].get("note")}

                result["mcp"] = mcp_result
            else:
                cfg, ok = read_json(mcp_file)
                if not ok:
                    print(json.dumps({"ok": False, "reason": "配置文件无法解析，已中止写入（可从 %s.hub-bak 恢复）" % mcp_file}, ensure_ascii=False))
                    return 4
                cfg, changed = merge_mcp_json(cfg, adapter)
                text = dump_json(mcp_file, cfg, args.dry_run)
                mcp_result = {"file": mcp_file, "written": changed, "keys": ".".join(adapter["mcp"]["keys"]),
                              "clientId": client,
                              "content": text if args.dry_run else None, "note": adapter["mcp"].get("note")}

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
