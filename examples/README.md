# examples（示例中台数据）

`hub/` 是一个可直接检索的迷你中台目录：1 个项目分区（demo）、2 个客户端
（claude-code / zcode）、规则 / 全局记忆 / format 2 会话各若干条。它同时是
`tests/test_search_parity.py` 的 golden 固定数据，**内容变更会使一致性测试重新锁定基线**，
请勿随意改动文件名（文件名参与类别判定）。

体验方式：

```bash
# 检索脚本
MEMORY_HUB_ROOT=examples/hub python3 skills/memory-hub-consume/scripts/search.py "Drizzle"

# MCP server（示例数据作为数据根）
MEMORY_HUB_ROOT=examples/hub node mcp/memory-hub-mcp.mjs
```
