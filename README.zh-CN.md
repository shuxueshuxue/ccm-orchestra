# CCM Orchestra

[English](./README.md)

本项目通过 [linux.do](https://linux.do/) 进行推广。

`ccm-orchestra` 是一个控制层，用来在 `tmux` 里运行持续的交互式 Claude Code helper，并通过 `kitty` 做可见协作。两者构成一个配对操作模型：`tmux` 让 helper 持续存活、可复用，`kitty` 让协作过程可见并支持 relay。

核心 loop 很简单：在 detached tmux pane 里启动 helper，给它发 prompt，再从 transcript 里读新输出。Session 按工作目录隔离，所以不同仓库里可以复用同一个 helper 名。`tmux` 层和 `kitty` 层合在一起，才让 Claude Code 和 Codex 能并行协作、互相交接、并在需要时保持可观察。

## 两层结构

这个系统本质上分成两层：

- `tmux` 层：真正的会话层。负责把交互式 Claude Code 持续跑着，按 worktree 隔离，并让 Codex 反复复用同一个 helper。
- `kitty` 层：可见协作层。负责把部分会话拉到前台可见，列出可见 peer，并在 tab 之间做带回执约定的通信。

如果只记一件事，就记这个：

- `tmux` 和 `kitty` 是两个并列能力，可以单独用，也可以联动用
- 默认 helper loop 在 `tmux` 层：`start -> send -> read`
- `kitty` 层负责可见协作、relay 和人工观察

这两层的唤醒模型也不一样：

- `tmux` 层是 poll 模式。`ccm read --wait-seconds ...` 会不断轮询 Claude transcript。
- `kitty` 层是 push 模式。`ccm relay` 会把消息主动注入另一个可见 tab，让对方之后醒来并回复。

不要把这两者混为一谈。等 `read` 不会替你唤醒另一个 agent tab。

## 为什么用 `tmux` 跑交互式 Claude，而不是 `claude -p`

主因是运维层面的，不是哲学层面的。

这个项目有意把“正常交互式 Claude Code”作为 canonical path，而不是把系统建立在非交互 print mode 上。目的就是尽量远离那种看起来像脚本化非交互调用的使用模式，因为那种模式更容易靠近账号风控边界。

这并不是在声称 `claude -p` 不能携带上下文。这里不做这个论断。

实际规则是：

- canonical path 是在 `tmux` 里跑交互式 Claude
- `tmux` 再给我们提供想要的进程边界，方便复用、读 transcript、检查、重启和清理
- 同一个交互式 helper 可以同时被两边接触：人可以 attach/观察，程序化工具也仍然可以 send、read、doctor、restart、supervise
- 不要把主工作流建立在 `claude -p` 上

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

python3 -m unittest tests/test_cli.py tests/test_heartbeat.py tests/test_smoke.py -v

ccm guide agent
ccm doctor --cwd "$PWD"
ccm start frontend-helper --cwd "$PWD"
ccm send frontend-helper "Review the current frontend flow and suggest 2-3 improvements." --cwd "$PWD"
ccm read frontend-helper --wait-seconds 20 --cwd "$PWD"
ccm inspect frontend-helper --cwd "$PWD"
ccm kill frontend-helper --cwd "$PWD"
```

### 3. 安装成命令行工具

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .

ccm doctor
ccm-smoke --cwd "$PWD"
codex-heartbeat status
codex-heartbeat test --tab-title mycel
```

优先使用 venv。现在很多系统的系统 Python 会受 PEP 668 保护，所以直接对全局解释器执行 `pip install -e .` 可能会被拒绝，或者把包装到你根本不想动的 Python 里。

## 命令说明

### Canonical rule

只使用全局 `ccm`。如果 Claude 或 `ccm` 升级后 helper 开始异常：

1. 先跑 `ccm doctor --cwd "$PWD"`。
2. 看是否出现 `@@@claude-path-mismatch` / `@@@claude-version-mismatch`。
3. 重启 helper。已经在跑的 helper 会继续沿用自己启动时的 binary 和 config root。

如果你是 agent 或其他 LLM，不要直接即兴发挥，先跑 `ccm guide agent`。那里面有完整的操作规则、tmux/kitty 分层，以及唤醒模型说明。

只有在你明确想绕开 cwd 推导出来的 namespace、直接操作某一个固定 state 文件时，才使用 `--state-path /abs/path/state.json`。这是排障/手术刀，不是日常路径。

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

如果你想用一条命令跑完最基础的 live 自检：

```bash
ccm-smoke --cwd "$PWD"
```

`ccm-smoke` 会按很窄的顺序跑一遍：`doctor -> start -> list -> send -> read -> kill -> cleanup`，同时记录当前的 `codex-heartbeat status`。如果 helper 没有读回预期的 probe token，它会直接失败，不会掩盖问题。

如果 `read` 结果是空的，或者 transcript 解析看起来不对，不要靠猜，直接看 live 现场：

```bash
ccm inspect frontend-helper --cwd "$PWD"
```

它会打印 state path、tmux session、已解析的 transcript path、transcript search roots，以及最近一段 pane tail。

如果你要看未经渲染的 transcript 原始事件，用来调试更底层的 Claude / MCP / tool trace：

```bash
ccm read frontend-helper --raw --json --cwd "$PWD"
```

如果你要一次看到所有已保存 namespace 里的 session，而不是只看当前目录：

```bash
ccm list --all-scopes --json
```

### 保持一个长期的 Claude 搭档

- 每个可见的 Codex tab 都应该在 tmux 里维护一个长期存活、值得信任的专属 Claude helper，反复复用。不要每做完一件小事就 kill 掉 helper。持久 session 本身就是核心价值。
- helper 名称按职责取，并尽量具体，比如 `frontend-helper`、`docs-editor`。不要在当前 namespace 里重复使用一个含糊又容易撞名的名字。
- Claude 不只是顾问。它可以直接编辑分支、commit 或 push，尤其适合前端和文档类工作。把它当作有写权限的协作者，而不是只读的建议机器。

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

`kitty` 是可见协作层。它不是 helper 真正运行的那一层，但当人类、Codex、Claude 需要互相看见并实时交换消息时，它就是系统里的第一等能力。

```bash
ccm tabs --listen-on unix:/tmp/mykitty
ccm relay "feat/main-thread-for-member" "Use Claude to review the UI and report back here." \
  --listen-on unix:/tmp/mykitty \
  --cwd "$PWD" \
  --task "frontend review" \
  --scene "untouched"
```

`ccm tabs` 现在会直接显示 peer 的 worktree、git branch 和 helper 身份。`ccm relay` 会自动包上一层默认 envelope 和 `reply-via` 提示，新人不需要自己记住上下文格式。

这也是 visible tab 里最安全的唤醒路径：

- 等 `tmux` helper 输出时，用 `ccm read`
- 需要另一个可见 tab 之后醒来并回复时，用 `ccm relay`

### 用 wechat 风格的 peer 层做直接寻址

统一只说一种寻址语言：
- `kitty:<tab-title>`
- `tmux:<session-name>`

可见 tab 不需要 registry，headless helper 也不需要假 alias。直接写目标：

```bash
ccm wechat-targets --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm wechat-send kitty:scheduled-tasks "Please summarize your current frontend direction." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm wechat-shift kitty:scheduled-tasks "Take ownership of the next frontend simplify pass." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

`wechat-send` 和 `wechat-shift` 会自动包上一层 system-style reminder，并明确写出怎么回，例如 `ccm wechat-send kitty:mycel "..."`。

如果是 headless 的 Claude/tmux helper，就直接写 tmux session，不要为了 WeChat 临时再开一个可见 kitty tab：

```bash
ccm wechat-send tmux:ccm-frontend-helper-abcd1234 "Please take over this phone thread." --cwd "$PWD"
```

`wechat-shift` 才是真正的转交原语。如果当前 sender 本来就是手机线程 owner，那么 `ccm wechat-shift <target> "..."` 会同时把 phone ownership 切到这个目标，并给手机侧发一条转交通知。

### 手机微信接入是另一条路径

如果用户说“把微信接到你这里，让我能用手机联系你”，不要把这件事和上面的 peer 层混为一谈。

直接走 CLI 真流程：

```bash
ccm wechat-connect
ccm wechat-status
ccm wechat-bind kitty:mycel
ccm wechat-watch --detach --listen-on "${KITTY_LISTEN_ON}"
ccm wechat-watch-status
```

这条链的含义是：

- `wechat-connect` 直接对接 WeChat iLink transport，去申请真正的微信二维码，把它渲染成 PNG，并且可以直接打开给用户扫码。
- `wechat-status` 用来确认全局手机侧 WeChat transport 是否已经连上。
- `wechat-bind` 决定手机侧新消息默认送到哪个直接目标。
- `wechat-watch --detach` 用 `ccm` 自己托管后台 watcher。不要再依赖临时 shell 后台脚本去跑长期手机消息投递。
- `wechat-watch-status` 用来看这个 watcher 现在是否还活着。
- 手机链路连好之后，只要当前 sender 正持有这个 phone thread，`wechat-shift` 就能把 peer 对话和 phone ownership 一起转给目标，并且会给手机用户补一条可见的转交提示。

等这条手机侧路径准备好之后，`ccm` 里的 wechat 风格 peer 层仍然可以继续负责 tab 之间的协作和转交。

现场规则：

- 每次重新 `wechat-connect` 之后，都要再跑一次 `ccm wechat-bind <target>`。新扫码会创建新的 transport session，不要假设旧绑定会自动跟过去。
- 如果 watcher 是在旧的 WeChat session 上启动的，重连后就要停掉旧 watcher 再起新的。`ccm` 现在会拒绝让 stale watcher 把更新后的 transport state 覆盖回旧状态。
- `ccm wechat-poll-once` 适合 debug 或单次拉取；真正长期运行要用 `ccm wechat-watch --detach`。

需要完整引导词时，用：

```bash
ccm wechat-guide agent
```

常用清理：

```bash
ccm wechat-disconnect
ccm wechat-unbind
ccm wechat-users
ccm wechat-reply <user_id> "..."
ccm wechat-watch-stop
```

### 保持监督用的 Codex tab 存活

```bash
codex-heartbeat start --tab-title mycel --interval-seconds 1500
codex-heartbeat status --tab-title mycel
codex-heartbeat test --tab-title mycel
codex-heartbeat stop --tab-title mycel
```

## 架构

`ccm-orchestra` 主要由两层加一个辅助工具组成：

- `tmux` 会话层，由 `ccm_orchestra/cli.py` 负责
  启动和复用交互式 Claude helper，按 worktree 隔离，读取 transcript，并运行 doctor 自检。
- `kitty` 协作层，也由 `ccm_orchestra/cli.py` 负责
  列出可见 tab、注入消息，并支持带 reply hint 的 relay 通信。
- `bin/codex-heartbeat`
  定时向指定 `kitty` tab 注入心跳消息，避免长时间监督时主 Codex 静默掉线。

设计原则非常克制：

- 跑正常交互式 Claude，不走 `--print`
- 按项目目录隔离状态
- 能读 Claude transcript 时就读真实 transcript，不依赖屏幕抓取
- 只有在上游 Session 自己出问题时，才退回终端 pane 检查

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
│   ├── ccm-smoke
│   └── codex-heartbeat
├── ccm_orchestra/
│   ├── __init__.py
│   ├── cli.py
│   ├── heartbeat.py
│   └── smoke.py
├── docs/
│   ├── claude-codex-frontend-playbook.md
│   └── codex-claude-visible-collab-playbook.md
├── tests/
│   ├── test_cli.py
│   ├── test_heartbeat.py
│   └── test_smoke.py
├── pyproject.toml
├── README.md
└── README.zh-CN.md
```
