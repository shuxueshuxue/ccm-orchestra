# CCM Orchestra

[English](./README.md)

`ccm-orchestra` 的目标不是把 Claude Code 包成花哨玩具，而是把它变成 Codex 真能调度、监督、复用的基础设施。

它让 Codex 可以在后台启动并维持真实的交互式 Claude 会话，用 `tmux` 保持持续 Session，用 transcript 增量读取获取新消息，用 `kitty` 在需要时再把会话拉回前台。少一点表演式“多智能体”，多一点可运维、可复查、可复用的真实协作。

## 两层结构

这个系统本质上分成两层：

- `tmux` 层：真正的会话层。负责把交互式 Claude Code 持续跑着，按 worktree 隔离，并让 Codex 反复复用同一个 helper。
- `kitty` 层：可见协作层。负责把部分会话拉到前台可见，列出可见 peer，并在 tab 之间做带回执约定的通信。

如果只记一件事，就记这个：

- 日常工作发生在 `tmux` 层：`start -> send -> read`
- `kitty` 不是必需层，主要用于观察和可见协作

## 为什么做这个

很多所谓多智能体流程最后都死在同样的地方：

- Session 不持续
- 不同项目的状态互相污染
- 终端自动化非常脆弱
- 所谓 helper 其实只是非交互模式套壳
- 监督行为流于形式

`ccm-orchestra` 就是为了解决这些常见死法。它坚持使用正常交互式 Claude，会按工作目录隔离状态，并给 Codex 足够的控制能力去真正管理这些会话。

## 为什么用 `tmux` 跑交互式 Claude，而不是 `claude -p`

这个项目有意把“正常交互式 Claude Code”作为主路径，而不是把系统建立在非交互 print mode 上。

原因很直接：

- 交互式 session 是持续的，helper 可以跨多轮保留上下文
- 交互式 session 会产出真实 transcript，`ccm read` 才能增量读取
- `tmux` 给了我们稳定的进程边界，方便复用、检查、重启和清理
- 从运维角度看，我们不希望主流程建立在看起来像大规模脚本化调用的非交互模式上；这种模式更容易接近账号风控边界

所以规则很简单：

- canonical path 是在 `tmux` 里跑交互式 Claude
- 不要把主工作流建立在 `claude -p` 上

## 功能特性

- 明确的两层模型：后台 `tmux` 会话控制 + 可选的前台 `kitty` 协作
- 通过 detached `tmux` 运行持续的交互式 Claude Code Session
- 按工作目录隔离命名空间，不同项目可并行使用同名 helper
- 从 Claude 的真实 JSONL transcript 中增量读取新消息
- 在需要人工查看时，通过 `kitty` 重新打开会话
- 列出当前可见的 `kitty` tabs，并按 title 直接发消息
- 自带心跳工具，避免监督中的 Codex tab 静默死亡
- `doctor` 命令用于环境和命名空间自检
- `read --wait-seconds` 用于处理 transcript 落盘延迟
- 整体结构简单，没有沉重框架依赖

## 快速开始

### 1. 前置依赖

- `python3`
- `tmux`，用于会话层
- `claude`
- `kitty` 只在你需要可见协作层时才需要，比如 `open`、`tabs`、`tell`、`relay`、heartbeat

### 2. 在任意目录通过全局 CLI 运行

```bash
git clone <repo-url>
cd ccm-orchestra

python3 -m unittest tests/test_claude_coop_manager.py -v

ccm doctor --cwd "$PWD"
ccm start frontend-helper --cwd "$PWD"
ccm send frontend-helper "Review the current frontend flow and suggest 2-3 improvements." --cwd "$PWD"
ccm read frontend-helper --wait-seconds 20 --cwd "$PWD"
ccm kill frontend-helper --cwd "$PWD"
```

### 3. 安装成命令行工具

```bash
pip install -e .

ccm doctor
codex-heartbeat status
```

## 命令说明

### Canonical rule

只使用全局 `ccm`。如果 Claude 或 `ccm` 升级后 helper 开始异常：

1. 先跑 `ccm doctor --cwd "$PWD"`。
2. 看是否出现 `@@@claude-path-mismatch` / `@@@claude-version-mismatch`。
3. 重启 helper。已经在跑的 helper 会继续沿用自己启动时的 binary 和 config root。

### 最常用的一组命令

```bash
ccm start frontend-helper --cwd "$PWD"
ccm send frontend-helper "Review the frontend in this branch and propose improvements." --cwd "$PWD"
ccm read frontend-helper --wait-seconds 30 --cwd "$PWD"
ccm kill frontend-helper --cwd "$PWD"
```

如果 session 崩了，或者 `kill` 中断了：

```bash
ccm cleanup --cwd "$PWD"
```

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

### 只有真的需要看前台时才用 `open`

`open` 不是日常 loop 的一部分。只有在 transcript 不够的时候才该用：

- debug 卡住的 helper
- 做 live 观察
- 有意识地做 visible-tab 协作

```bash
ccm open frontend-helper --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

### 把可见 kitty tab 当成 peer 来通信

这是可选的 `kitty` 层，不是核心会话路径。

```bash
ccm tabs --listen-on unix:/tmp/mykitty
ccm relay "feat/main-thread-for-member" "Use Claude to review the UI and report back here." \
  --listen-on unix:/tmp/mykitty \
  --cwd "$PWD" \
  --task "frontend review" \
  --scene "untouched"
```

`ccm tabs` 现在会直接显示 peer 的 worktree、git branch 和 helper 身份。`ccm relay` 会自动包上一层默认 envelope 和 `reply-via` 提示，新人不需要自己记住上下文格式。

### 保持监督用的 Codex tab 存活

```bash
codex-heartbeat start --interval-seconds 1500
codex-heartbeat status
codex-heartbeat stop
```

## 架构

`ccm-orchestra` 主要由两层加一个辅助工具组成：

- `tmux` 会话层，由 `claude_coop_manager.py` 负责
  启动和复用交互式 Claude helper，按 worktree 隔离，读取 transcript，并运行 doctor 自检。
- `kitty` 协作层，也由 `claude_coop_manager.py` 负责
  列出可见 tab、注入消息，并支持带 reply hint 的 relay 通信。
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
- 可见的 kitty tab 可以被枚举出来，并且能按 title 收到注入消息

## 注意事项

- 如果 Claude 上游 API 自己不稳定，助手输出还是可能延迟或失败；工具不会掩盖这个事实。
- `read` 依赖 Claude transcript 落盘；`--wait-seconds` 只能缓解延迟，不能修复上游宕机。
- transcript 解析会优先跟随当前 Claude 的真实 config root，包括 `cac` 这种隔离环境；找不到时才回退到默认的 Claude projects 目录。
- `kitty` 相关能力依赖有效的 `KITTY_LISTEN_ON`。
- 这个工具故意保持简单，不打算发展成一个庞杂的 agent 平台。

## 仓库结构

```text
ccm-orchestra/
├── AGENTS.md
├── bin/
│   ├── ccm
│   └── codex-heartbeat
├── docs/
│   ├── claude-codex-frontend-playbook.md
│   └── codex-claude-visible-collab-playbook.md
├── tests/
│   └── test_claude_coop_manager.py
├── claude_coop_manager.py
├── pyproject.toml
├── README.md
└── README.zh-CN.md
```

## 当前状态

这个项目已经足够能打，可以投入使用；同时也还足够小，能很快继续进化。这正是它应有的状态。
