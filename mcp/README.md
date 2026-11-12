# memory-hub MCP Server（内置只读组件）

本目录是 memory-hub-plugin 的产品组成部分：**官方只读 MCP server**。它把中台目录的读取能力
通过 MCP 标准协议暴露给任何客户端——不要求对方安装本插件的 skill，只要宿主支持 MCP，
就能检索/阅读中台里各客户端沉淀的记忆、规则与会话。

与写入侧的分工：写入（脱敏闸、采集策略、幂等命名、聚合纪律）全部在
`skills/memory-hub-export` 的文件通道强制执行，本 server **只读、无写入工具**；
配套管理端（执务Studio 桌面应用）的 server 额外提供授权写、记忆提升与聚合索引，
是升级选项而非依赖。

## 运行要求

Node.js 18+，零第三方依赖。由宿主按 MCP stdio 传输拉起（子进程、不监听端口、不联网）。

## 接入方式

### ① 支持 agent-plugins 标准的客户端

仓库根目录的 `mcp.json` 会被自动发现，安装插件即得，无需配置。

### ② 手动配置 / setup.py 接入

`python3 skills/memory-hub-setup/scripts/setup.py --client <id>` 默认（`--mcp-source plugin`）
写入以下条目（ZCode 示例；各宿主配置文件位置见适配表 `--list`）：

```json
{
  "mcpServers": {
    "memory-hub": {
      "type": "stdio",
      "command": "node",
      "args": ["/path/to/memory-hub-plugin/mcp/memory-hub-mcp.mjs"],
      "env": { "MEMORY_HUB_ROOT": "/Users/you/MemoryHub" }
    }
  }
}
```

与执务Studio 配套、需要授权写能力时：`--mcp-source desktop` 切换为其完整 server
（注入 `--client <id>` 与 `MEMORY_HUB_CLIENT_ID`，见 setup.py 说明）。

想先用示例数据体验：把 `MEMORY_HUB_ROOT` 指向本仓库 `examples/hub/`。

## 工具清单（7 个，全部只读）

| 工具 | 参数 | 说明 |
|---|---|---|
| `search_hub` | `query`（必填）；`scope` / `client` / `kind` / `limit` 可选 | 本地全文检索（多关键词 AND）；会话命中按「轮」分节给原文 |
| `read_entry` | `file` | 读单个条目全文（限 64KB，路径限定在中台内） |
| `get_session` | `file` | 按轮结构化读会话：元信息 + 各轮（轮号/时间/原文） |
| `list_partitions` | — | 项目分区列表（目录名、project.json 元信息、条目数） |
| `list_clients` | `partition` 可选 | 已登记客户端及条目数（全局 + 各分区） |
| `read_catalog` | — | 读管理端生成的 `index/catalog.json` 导航（存在时） |
| `hub_stats` | — | 分区数、条目数（按类别）、客户端清单 |

## 安全边界

- 只读：不提供任何写入/删除工具；
- 路径守卫：所有按路径读取的工具都限制在中台根目录内（拒绝 `../` 越界）；
- 单条目读取上限 64KB，检索摘录上限 1.2KB；
- 检索行为与 `skills/memory-hub-consume/scripts/search.py` 保持一致，
  由 CI 的 golden 对比测试（`tests/test_search_parity.py`）锁定，防止两套实现漂移。

## License

MIT，随仓库根 [LICENSE](../LICENSE)。
