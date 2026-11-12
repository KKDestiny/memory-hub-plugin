---
name: memory-hub-setup
description: Wire this AI coding client into the shared memory hub — idempotently install hooks/MCP per the host's verified contract, with dry-run, backup and post-check. 把本客户端接入共享记忆中台：按宿主已验证的配置契约幂等写入钩子/MCP 配置（干跑、备份、回读自检）。
---

# memory-hub-setup（接入装配）

用户说「接入记忆中台 / 接入共享记忆」时使用本技能。

## 步骤（全部由脚本完成，agent 只负责执行与汇报）
1. `python3 skills/memory-hub-setup/scripts/setup.py --list` → 查看各宿主的验证状态、配置文件位置与可安装的事件。
   验证状态为「未核验（unverified）」的宿主，不得照抄其他宿主的路径。
2. **干跑**：`setup.py --client <id> --dry-run` → 检查输出中的 `hooks.content` / `mcp.content`，确认：
   - 写入目标是宿主的真实配置文件（ZCode = `~/.zcode/cli/config.json`、WorkBuddy = `~/.workbuddy/settings.json` 等）
   - 事件在宿主支持的事件集合内（ZCode 仅支持 Stop；MiniMax 的事件白名单不含 Stop；TRAE 不含 SessionEnd）
   - **MCP 来源**（`--mcp-source`，缺省 `plugin`）：
     - `plugin`：写入本插件自带的只读 server（`node <仓库>/mcp/memory-hub-mcp.mjs` + env `MEMORY_HUB_ROOT`）。
       只读 server 不消费客户端身份，条目**不带** `--client` / `MEMORY_HUB_CLIENT_ID`。
     - `desktop`：写入配套管理端的完整 server。此时条目**必须携带客户端标识**：`args` 含
       `--client <ClientID>`、`env` 含 `MEMORY_HUB_CLIENT_ID=<ClientID>`，且与输出的 `clientId` 一致
       （中台登记的规范标识，由管理端在授权时确定；未登记时等于宿主 id）。用户要求更换标识时，
       应先在中台或管理端中修改，不得在宿主配置中手工改名。
3. 写入：去掉 `--dry-run` 重新执行（幂等：包含 `memory_hub_hook` 标记的条目不会重复追加；已存在的
   MCP 条目按来源处理——同源补齐校正，既有条目指向 desktop 而本次为默认 plugin 时报告冲突
   `mcp.written: "conflict"`、不静默改写；已存在钩子命令中的 `--client` 参数会校正为 `clientId`，
   见输出的 `hooks.idFixed`；写入前自动备份为 `*.hub-bak`）。
   `mcp.written` 的取值：`"created"` 新建 / `"updated"` 补齐、校正或按显式来源切换 / `false` 已一致 /
   `"conflict"` 既有条目来源与本次不符，未改写（按输出的 reason 处理）。
4. **回读自检**：再次执行 `--dry-run`，确认 `hooks.written` 为空、`hooks.already` 列出已安装的事件、
   `hooks.idFixed` 为 false、`mcp.written` 为 false（表示无重复写入、来源与内容已一致）。
5. 向用户转述结构化报告：MCP 来源、客户端标识（desktop 源时）、写入的文件、事件、跳过项及原因；
   提示重启宿主后生效。

## 回滚
- `cp <配置文件>.hub-bak <配置文件>`（脚本每次写入前都会保留一份备份）。
- 或手工删除包含 `memory_hub_hook` 标记的钩子条目 / `memory-hub` 这个 server 条目。

## 边界
- 脚本不修改记忆中台数据目录中的任何文件。
- 无钩子机制的宿主（opencode）或钩子结构未经验证的宿主（Codex）会被明确拒绝并给出原因，
  改走「手动导出」通道——不得静默跳过。
- 宿主的私有存储（SQLite、加密文件）不读取；只能导出用户可见的内容。
- **desktop 源的客户端标识不可协商**：导出使用哪个 `--client <id>`，desktop 源的 MCP 配置就必须注入
  同一个 id；不得为使流程先行通过而省略注入，也不得用宿主自报的名称（如 `wb`、`mavis-local-runtime-mcp`）
  替代。plugin 源（只读 server）不消费身份，无此约束。
- **标识以中台登记为准**：标识的唯一权威是中台 `.state/client-ids.json`（由可选管理端在首次授权时
  写入，用户可在管理端中修改）。本脚本只读取该映射表，并据此写入配置与钩子命令；映射表不存在时
  才回退为宿主 id。因此重新执行本脚本不会覆盖用户修改过的标识，也不需要 agent 手工同步。
