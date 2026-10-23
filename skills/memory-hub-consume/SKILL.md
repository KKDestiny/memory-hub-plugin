---
name: memory-hub-consume
description: Search the shared memory hub read-only for memories/rules/session digests contributed by other AI clients. 从共享记忆中台只读检索其他客户端沉淀的记忆/规则/会话摘要。
---

# memory-hub-consume（记忆下行）

用户的问题可能涉及「我在其他工具/项目里是怎么约定的、做过什么」时使用本技能。

## 步骤
1. 先读取中台根目录下的 `index/catalog.json`（不超过 2KB 的导航文件，由可选管理端聚合生成；
   不存在时跳过此步）——查看有哪些来源客户端与项目分区；也可读取 `index/digest.md` 查看总摘要。
2. 需要具体内容时运行检索脚本（直接扫描文件，无需管理端在线；`--json` 输出结构化结果，便于程序处理）：

   ```bash
   python3 skills/memory-hub-consume/scripts/search.py "<关键词>" [--scope project|global] [--client <id>] [--kind memory|rule|session] [--json]
   ```

3. 按需读取命中文件的全文（单文件过长时只读前 4000 字）。

## 命名与存放位置（两种位置都可能命中，检索时需同时覆盖）
- 记忆/规则/工作流：`<kind>-<scope>-<hash8>.md`（现行命名）或历史固定命名 `project-memory.md` /
  `rules.md`（历史数据，只读兼容）
- 会话：`clients/<id>/sessions/YYYY-MM-DD-session-<id>.md`（现行命名）或客户端根目录下的同名文件
  （历史数据）
- 聚合文件的正文包含多个 `## <来源文件名>` 小节；引用时应说明这是聚合条目，并给出小节名。

## 回答约定（硬性）
- 引用必须标注来源客户端与中台文件的相对路径（如
  `codex · projects/web-app-8d43b2/clients/codex/rule-project-1a2b3c4d.md`）。
- 检索未命中时应明确说明「中台里没有找到」，不得推断或虚构内容。
- 本技能只读：除读取外不得修改任何中台文件（index/ 由管理端生成，同样只读）。
