# 更新日志

本文件记录本项目的值得注意的变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- 接入装配（memory-hub-setup）、上行导出（memory-hub-export）、下行检索（memory-hub-consume）
  三个 skill 的初始版本。
- 宿主适配表 `skills/memory-hub-setup/scripts/adapters.json`，覆盖 Claude Code、ZCode、Codex、
  TRAE（国际版 / CN / SOLO）、WorkBuddy、Qoder、opencode、MiniMax Code，并登记验证状态与证据来源。
- `MEMORY_HUB_ROOT` 环境变量，用于指定中台数据根目录（缺省为 `~/DutiMemory`）。
- agent-plugins.org 1.0.0 插件清单（plugin.json）。
- GitHub Actions 流水线：Python 语法编译检查、单元测试与 JSON 文件校验。
- `search.py --json` 参数，以 JSON 输出结构化检索结果。
- 单元测试套件（`tests/`）：项目标识口径、脱敏规则、钩子合并逻辑与 TOML 补齐。

### Fixed

- 钩子的 HTTP 上行载荷（摘要、正文与逐轮轮次）在发送前执行与文件通道相同的脱敏；
  脱敏规则统一收敛至 `skills/_shared/redact.py`。
- 共用的 hooks 文件（如 `~/.trae/hooks.json`）中，其他客户端已安装的钩子不再被标识
  校正改写，改为按归属报告冲突；仅校正归属本客户端条目的过期标识。
- ZCode 形态安装不再覆盖用户已显式设置的 `hooks.enabled` 与 `timeoutMs`。
- SQLite 只读连接改用 pathlib 生成的规范 `file:///` URI，兼容 Windows 盘符路径。
- TOML `args` 数组解析支持引号转义与含逗号的字符串值。
