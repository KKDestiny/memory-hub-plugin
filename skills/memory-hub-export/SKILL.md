---
name: memory-hub-export
description: Export this client's memories/rules/workflows/session digests to the shared memory hub (aggregate + redact + incremental). 把本客户端的记忆/规则/工作流/会话摘要导出到共享记忆中台（聚合 + 脱敏 + 增量）。
---

# memory-hub-export（记忆上行）

用户说「导出到记忆中台」「同步记忆到中台」或钩子触发时使用本技能。

## 硬性约束
- **会话全文（format 2）由钩子自动完成**：通过授权检查时（`.state/capture-policy.json`）钩子读取宿主转录
  或私有存储并组装逐轮全文；agent 不得在对话中粘贴会话原文，也不得手工拼装全文。
- **禁止把 transcript 或记忆原文粘贴进回复**：只运行脚本、检查退出码、转述脚本输出的 JSON 摘要。
- 只写自己的 `clients/<你的客户端id>/` 子目录；不得修改其他客户端的目录与 project.json。
- **记忆、规则、工作流必须聚合导出**：每个项目每类内容一份。**禁止逐条循环调用脚本**——
  中台的入库键不包含文件名哈希段，同一项目同一类的多条目会相互覆盖（曾实测 87 个记忆文件
  逐条导出后仅保留 5 条）。
- 会话不受聚合约束：一次会话一份（入库键为会话 id 维度）。

## 用法（把 <id> 替换为你的客户端标识，如 claude-code / codex / zcode / workbuddy）
```bash
# ① 聚合导出（推荐）：把该项目全部同类文件合并为一份
python3 skills/memory-hub-export/scripts/memory_hub_export.py --client <id> --kind memory --from-dir <你的记忆目录> --project <项目绝对路径>
python3 skills/memory-hub-export/scripts/memory_hub_export.py --client <id> --kind rule   --from-dir <你的规则目录> --project <项目绝对路径>
# 全局记忆/规则（不传 --project）
python3 skills/memory-hub-export/scripts/memory_hub_export.py --client <id> --kind memory --from-dir <目录>

# ② 单条正文（内容本身只有一份时）
python3 skills/memory-hub-export/scripts/memory_hub_export.py --client <id> --kind memory --text-file <文件> [--project <路径>]

# ③ 工作流清单（JSON：[{"name":"...","desc":"..."}]）
python3 skills/memory-hub-export/scripts/memory_hub_export.py --client <id> --kind workflow --items-file <清单>

# ④ 会话摘要（一次会话一份；回退路径：宿主钩子无法获取转录、或采集策略未授权时使用）
python3 skills/memory-hub-export/scripts/memory_hub_export.py --client <id> --kind session --session <会话id> --summary "<诉求…；结论…；工具…>" [--project <路径>]

# ⑤ 会话全文（format 2；钩子已自动处理，手工执行仅用于补录历史）：--turns-file 为逐轮 JSON
python3 skills/memory-hub-export/scripts/memory_hub_export.py --client <id> --kind session --session <会话id> --full --turns-file <轮次JSON> [--cursor N] [--title <会话名>] [--project <路径>]

# ⑥ 全量导入历史会话（**仅在用户明确提出时执行**；不限时间范围、增量幂等）
python3 skills/memory-hub-export/scripts/memory_hub_hook.py --client <id> --import-all [--sessions-dir <你自己的明文会话目录>] [--dry-run]
```

## 全量导入历史（触发条件与纪律）
- **触发**：用户说「把历史会话全部导入中台」「把以前的会话都同步过去」这类话时，执行 ⑥。
- **读取自己的数据**：zcode 读取自身的会话数据库；其他宿主通过 `--sessions-dir` 指定你自己的明文
  会话目录。不得让管理端读取客户端的数据。
- **先 dry-run 并报告数量**：先以 `--dry-run` 获取清点结果，向用户报告「将导入 N 场」，确认后再实际执行。
- **全量、不限时间范围**；重复执行为增量（只补写新增或变更的内容），不会重复入库。

脚本内置：脱敏（token、口令、长随机串 → [REDACTED]）、frontmatter 组装、内容哈希增量
（内容未变化时跳过）、**会话写入 `clients/<id>/sessions/`**、聚合文件携带 `itemCount`、
同类多分片时输出覆盖风险告警。

导出完成后向用户转述：写入了哪些文件、各包含多少条（聚合了几个来源）、脱敏了多少处。
