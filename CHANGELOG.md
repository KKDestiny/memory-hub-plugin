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
- GitHub Actions 流水线：Python 语法编译检查与 JSON 文件校验。
