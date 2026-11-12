---
client: claude-code
scope: project
project: /home/demo/projects/demo
kind: rule
exported: 2026-10-20T09:00:00Z
summary: 本项目 ORM 统一使用 Drizzle；数据库迁移必须走 drizzle-kit，禁止手写 SQL 迁移
itemCount: 2
aggregated: true
---

# rule · claude-code

## rules-orm.md

_（来源摘要：ORM 选型结论）_

本项目 ORM 统一使用 Drizzle。所有数据访问一律经过 Drizzle schema 定义，
禁止绕过类型层直接拼接 SQL。

## rules-migration.md

_（来源摘要：迁移纪律）_

数据库结构变更必须通过 drizzle-kit 生成迁移文件并入库评审，禁止手写 SQL 迁移、
禁止在运行时改表。
