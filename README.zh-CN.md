# CCM Orchestra

[English](./README.md)

`ccm-orchestra` 的目标不是把 Claude Code 包成花哨玩具，而是把它变成 Codex 真能调度、监督、复用的基础设施。

它让 Codex 可以在后台启动并维持真实的交互式 Claude 会话，用 `tmux` 保持持续 Session，用 transcript 增量读取获取新消息，用 `kitty` 在需要时再把会话拉回前台。少一点表演式“多智能体”，多一点可运维、可复查、可复用的真实协作。

## 为什么做这个

很多所谓多智能体流程最后都死在同样的地方：

- Session 不持续
- 不同项目的状态互相污染
- 终端自动化非常脆弱
- 所谓 helper 其实只是非交互模式套壳
- 监督行为流于形式

`ccm-orchestra` 就是为了解决这些常见死法。它坚持使用正常交互式 Claude，会按工作目录隔离状态，并给 Codex 足够的控制能力去真正管理这些会话。

## 功能特性

- 通过 detached `tmux` 运行持续的交互式 Claude Code Session
- 按工作目录隔离命名空间，不同项目可并行使用同名 helper
- 从 Claude 的真实 JSONL transcript 中增量读取新消息
- 在需要人工查看时，通过 `kitty` 重新打开会话
- 自带心跳工具，避免监督中的 Codex tab 静默死亡
- `doctor` 命令用于环境和命名空间自检
- `read --wait-seconds` 用于处理 transcript 落盘延迟
- 整体结构简单，没有沉重框架依赖

## 快速开始

### 1. 前置依赖

- `python3`
- `tmux`
- `claude`
- 如果要用 `open` 或 heartbeat，需要 `kitty`

### 2. 直接运行

```bash
git clone <repo-url>
cd ccm-orchestra

python3 -m unittest tests/test_claude_coop_manager.py -v

bin/ccm start frontend-helper
bin/ccm send frontend-helper "Review the current frontend flow and suggest 2-3 improvements."
bin/ccm read frontend-helper --wait-seconds 20
bin/ccm open frontend-helper
bin/ccm kill frontend-helper
```

### 3. 安装成命令行工具

```bash
pip install -e .

ccm doctor
codex-heartbeat status
```

## 命令说明

### 启动交互式 Claude Session

默认使用当前目录：

```bash
ccm start frontend-helper
```

也可以显式指定目标目录：

```bash
ccm --cwd ~/Codebase/leonai/frontend start frontend-helper
```

### 在同一个命名空间里继续使用

```bash
ccm --cwd ~/Codebase/leonai/frontend list
ccm --cwd ~/Codebase/leonai/frontend send frontend-helper "Critique the new layout."
ccm --cwd ~/Codebase/leonai/frontend read frontend-helper --wait-seconds 30
```

### 检查环境健康度

```bash
ccm doctor
```

### 保持监督用的 Codex tab 存活

```bash
codex-heartbeat start --interval-seconds 1500
codex-heartbeat status
codex-heartbeat stop
```

## 架构

`ccm-orchestra` 主要由两部分组成：

- `claude_coop_manager.py`
  负责会话生命周期、transcript 读取、命名空间隔离和 `kitty` 重新打开。
- `bin/codex-heartbeat`
  定时向指定 `kitty` tab 注入心跳消息，避免长时间监督时主 Codex 静默掉线。

设计原则非常克制：

- 跑正常交互式 Claude，不走 `--print`
- 按项目目录隔离状态
- 能读 Claude transcript 时就读真实 transcript，不依赖屏幕抓取
- 只有在上游 Session 自己出问题时，才退回终端 pane 检查

## 已验证的关键行为

这个 repo 里已经明确验证过：

- 单元测试通过
- 两个不同目录可以并行运行同名 helper
- `start --cwd <dir>` 会严格使用指定目录
- heartbeat 真的能把消息注入 `Main` `kitty` tab
- 真实的交互式 Claude Session 能正常启动并接收 prompt

## 注意事项

- 如果 Claude 上游 API 自己不稳定，助手输出还是可能延迟或失败；工具不会掩盖这个事实。
- `read` 依赖 Claude transcript 落盘；`--wait-seconds` 只能缓解延迟，不能修复上游宕机。
- `kitty` 相关能力依赖有效的 `KITTY_LISTEN_ON`。
- 这个工具故意保持简单，不打算发展成一个庞杂的 agent 平台。

## 仓库结构

```text
ccm-orchestra/
├── bin/
│   ├── ccm
│   └── codex-heartbeat
├── docs/
│   └── claude-codex-frontend-playbook.md
├── tests/
│   └── test_claude_coop_manager.py
├── claude_coop_manager.py
├── pyproject.toml
├── README.md
└── README.zh-CN.md
```

## 当前状态

这个项目已经足够能打，可以投入使用；同时也还足够小，能很快继续进化。这正是它应有的状态。
