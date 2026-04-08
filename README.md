> **Promotion:** This project is promoted through [linux.do](https://linux.do/).

# CCM Orchestra

[中文说明](./README.zh-CN.md)

`ccm-orchestra` is a control plane for running persistent, interactive Claude Code agents in `tmux` and coordinating visible collaboration between code agents through `kitty`. Together they form a paired operating model: `tmux` keeps an agent alive and reusable, `kitty` makes the collaboration visible and relay-capable.

The core loop is simple: start an agent in a detached tmux pane, send it prompts, and read its transcript. Sessions are isolated by working directory, so the same agent name can coexist across repos. Together, the `tmux` layer and the `kitty` layer let Claude Code, Codex, and other code agents work in parallel, hand work back and forth, and stay observable when needed.

## The Two Layers

This system has two clearly different layers:

- `tmux` layer: the real session layer. It keeps interactive Claude Code alive, isolates it by worktree, and lets Codex reuse the same agent over time.
- `kitty` layer: the visible collaboration layer. It lets humans and other agents see selected sessions, list visible peers, and exchange messages with receipt-friendly envelopes.

If you remember only one thing, remember this:

- `tmux` and `kitty` are two distinct capabilities, and they can work independently or together
- the default agent loop is in the `tmux` layer: `start -> send -> read`
- the `kitty` layer makes visible coordination, relay, and human observation possible

The wakeup model is different across the two layers:

- `tmux` layer is poll-based. `ccm read --wait-seconds ...` keeps checking Claude transcript output.
- `kitty` layer is push-based. `ccm relay` injects a message into another visible tab so that peer can wake up and answer later.

Do not mix those up. Waiting on `read` will not wake another agent tab for you.

## Why Interactive Sessions In `tmux` Instead of `claude -p`

The main reason is operational, not philosophical.

This project intentionally keeps its canonical path on normal interactive Claude Code sessions and avoids building the workflow around non-interactive print mode. The goal is to stay away from usage patterns that look like scripted non-interactive automation and may be more likely to trigger account risk controls.

That does not mean `claude -p` cannot carry context. That is not the claim here.

The practical rule is:

- the canonical path is interactive Claude in `tmux`
- `tmux` then gives us the process boundary we want for reuse, transcript reading, inspection, restart, and cleanup
- the same interactive agent can be approached from both sides: humans can attach and inspect it, while programmatic tooling can still send, read, doctor, restart, and supervise it
- do not build the main workflow around `claude -p`

## Quickstart

### 1. Prerequisites

- `python3`
- `tmux` for the session layer
- `claude`
- `kitty` only if you want the visible collaboration layer (`open`, `tabs`, `tell`, `relay`, heartbeat)

### 2. Run from anywhere with the global CLI

```bash
git clone <repo-url>
cd ccm-orchestra

python3 -m unittest tests/test_cli.py tests/test_heartbeat.py tests/test_smoke.py -v

ccm guide agent
ccm doctor --cwd "$PWD"
ccm start frontend-agent --cwd "$PWD"
ccm send frontend-agent "Review the current frontend flow and suggest 2-3 improvements." --cwd "$PWD"
ccm read frontend-agent --wait-seconds 20 --cwd "$PWD"
ccm inspect frontend-agent --cwd "$PWD"
ccm kill frontend-agent --cwd "$PWD"
```

### 3. Install as a CLI

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

Prefer a venv. On many modern systems, system Python is protected by PEP 668, so `pip install -e .` against the global interpreter may be blocked or may mutate a Python you did not intend to touch.

## Command Guide

### Canonical rule

Use the global `ccm` only. If an agent starts failing after a Claude or `ccm` upgrade:

1. Run `ccm doctor --cwd "$PWD"`.
2. Check for `@@@claude-path-mismatch` / `@@@claude-version-mismatch`.
3. Restart the agent. Existing agents keep the binary and config root they started with.

If you are a human operator and want the shortest entry path, run `ccm guide human`.
If you are an agent or another LLM, run `ccm guide agent` before you improvise. That guide contains the longer operating rules, the tmux vs kitty split, and the wakeup model.

Use `--state-path /abs/path/state.json` only when you intentionally want one explicit state file instead of the normal cwd-derived namespace. That is a debugging and surgery tool, not the everyday path.

### Minimal everyday usage

```bash
ccm start frontend-agent --cwd "$PWD"
ccm send frontend-agent "Review the frontend in this branch and propose improvements." --cwd "$PWD"
ccm read frontend-agent --wait-seconds 30 --cwd "$PWD"
ccm kill frontend-agent --cwd "$PWD"
```

If a session crashed or `kill` was interrupted:

```bash
ccm cleanup --cwd "$PWD"
```

If you want one command that exercises the basic live path end-to-end:

```bash
ccm-smoke --cwd "$PWD"
```

`ccm-smoke` runs a narrow live check: `doctor -> start -> list -> send -> read -> kill -> cleanup`, and also records the current `codex-heartbeat status`. It fails loudly if the agent path does not produce the probe token.

If `read` comes back empty or transcript resolution still looks suspicious, inspect the live session instead of guessing:

```bash
ccm inspect frontend-agent --cwd "$PWD"
```

That prints the state path, tmux session, resolved transcript path, transcript search roots, and a recent pane tail.

If you need the unrendered transcript stream for advanced Claude behavior, MCP traces, or tool debugging:

```bash
ccm read frontend-agent --raw --json --cwd "$PWD"
```

If you need to discover forgotten sessions across every saved namespace instead of only the current one:

```bash
ccm list --all-scopes --json
```

### Keep a long-lived Claude partner

- Each visible Codex tab should usually keep one dedicated, trusted Claude agent in tmux and reuse it over time. Do not kill the agent after every small task. The persistent session is the point.
- Name agents by job and keep the names specific, for example `frontend-agent` or `docs-editor`. Avoid reusing a vague name that already exists in the current namespace.
- Claude is not just an advisor. It can directly edit the branch, commit, or push, especially for frontend and documentation work. Treat it as a collaborator with write access, not a read-only consultant.

### Start an interactive Claude session

Uses the current directory by default:

```bash
ccm start frontend-agent
```

Or target a specific directory:

```bash
ccm --cwd ~/Codebase/leonai/frontend start frontend-agent
```

### Reuse the same namespace later

```bash
ccm --cwd ~/Codebase/leonai/frontend list
ccm --cwd ~/Codebase/leonai/frontend send frontend-agent "Critique the new layout."
ccm --cwd ~/Codebase/leonai/frontend read frontend-agent --wait-seconds 30
```

### Check environment health

```bash
ccm doctor
```

### Open an agent only when you actually need to look

`open` is not part of the normal loop. Use it only when transcript output is not enough:

- debugging a stuck agent
- supervised live observation
- deliberate visible-tab collaboration

```bash
ccm open frontend-agent --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

### Use visible kitty tabs as peers

`kitty` is the visible collaboration layer. It is not the agent runtime itself, but it is a first-class part of the system when humans, Codex, and Claude need to watch each other and exchange messages live.

```bash
ccm tabs --listen-on unix:/tmp/mykitty
ccm relay "feat/main-thread-for-member" "Use Claude to review the UI and report back here." \
  --listen-on unix:/tmp/mykitty \
  --cwd "$PWD" \
  --task "frontend review" \
  --scene "untouched"
```

`ccm tabs` now shows the peer tab title together with its resolved worktree, git branch, and agent identity. `ccm relay` wraps the message with a default envelope and a `reply-via` hint so a newcomer can answer without learning extra ceremony.

Visible-tab communication rule:

- `ccm relay` is the primary path for tab-to-tab chat.
- `ccm tell` is a legacy raw path for rare fire-and-forget text injection.
- reading raw tab text or pane tails is legacy debug-only evidence, not the normal way agents should talk to each other.

This is also the wakeup-safe path for agents in visible tabs:

- use `ccm read` when waiting on Claude agent output from the `tmux` layer
- use `ccm relay` when another visible tab needs to wake up and reply

Shortest-path mental model:

- `ccm read` polls unread transcript output from the tmux-managed agent
- `ccm relay` pushes a message into another visible tab and gives it a reply path
- `codex-heartbeat` is a separate visible-tab keepalive tool, not a `ccm` subcommand
- visible Codex tabs get one extra Enter retry on relay delivery; this lowers submit misses, not mathematically guarantees them away
- `ccm wechat-watch` is the phone transport watcher, not a general agent scheduler

Minimal relay round-trip:

```bash
ccm relay "code killer" "Please summarize your current blocker." --cwd "$PWD"
ccm relay hub "Current blocker is relay discoverability." --cwd "/Users/lexicalmathical/worktrees/leonai--code-killer"
```

### Use the wechat-style peer layer with direct targets

Use one address language everywhere:
- `kitty:<tab-title>`
- `tmux:<session-name>`

Visible tabs do not need a registry. Headless agents do not need fake aliases. Pick the right target directly:

```bash
ccm wechat-targets --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm wechat-send kitty:scheduled-tasks "Please summarize your current frontend direction." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm wechat-shift kitty:scheduled-tasks "Take ownership of the next frontend simplify pass." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

`wechat-send` and `wechat-shift` wrap the message with a system-style reminder that says how to reply, for example with `ccm wechat-send kitty:mycel "..."`.

For headless Claude/tmux peers, target the session directly instead of opening a visible kitty tab just for WeChat:

```bash
ccm wechat-send tmux:ccm-frontend-agent-abcd1234 "Please take over this phone thread." --cwd "$PWD"
```

`wechat-shift` is the real handoff primitive. If the sender currently owns the phone thread, `ccm wechat-shift <target> "..."` also moves phone ownership to that target and sends a handoff notice back to the phone user.

### Phone WeChat onboarding is a different path

If the user says "connect WeChat to you so I can message you from my phone", do not confuse that with the peer layer above.

Use the real CLI flow:

```bash
ccm wechat-connect
ccm wechat-status
ccm wechat-bind kitty:mycel
ccm wechat-watch --detach --listen-on "${KITTY_LISTEN_ON}"
ccm wechat-watch-status
```

What that does:

- `wechat-connect` talks directly to the WeChat iLink transport, requests a real QR code, renders it into a PNG, and can open it so the user can scan with their phone.
- `wechat-status` confirms whether the global phone-side WeChat transport is connected.
- `wechat-bind` chooses which direct target receives incoming phone messages.
- `wechat-watch --detach` starts the canonical background watcher inside `ccm` itself. Do not rely on ad-hoc shell background jobs for long-lived phone delivery.
- `wechat-watch-status` shows whether that watcher is still alive.
- Once the phone path is connected, `wechat-shift` can move both the peer conversation and the phone-thread owner in one step, as long as the sender currently owns that phone thread. That shift now also emits a short notice to the phone user so the transfer is visible on the phone side.

After that phone-side path is ready, `ccm`'s wechat-style peer layer can still be used for tab-to-tab coordination.

Operational rules that matter in practice:

- After every new `wechat-connect`, run `ccm wechat-bind <target>` again. A new login creates a new transport session; do not assume the old binding still points at the right live connection.
- If a watcher was started against an older WeChat session, stop it and start a fresh one after reconnecting. `ccm` now refuses to let a stale watcher overwrite a newer transport state.
- Use `ccm wechat-poll-once` for debugging or one-shot delivery. Use `ccm wechat-watch --detach` for the real ongoing path.

Use this when you need the longer script:

```bash
ccm wechat-guide agent
```

Useful cleanup:

```bash
ccm wechat-disconnect
ccm wechat-unbind
ccm wechat-users
ccm wechat-reply <user_id> "..."
ccm wechat-watch-stop
```

### Keep the supervising Codex tab alive

```bash
codex-heartbeat start --tab-title mycel --interval-seconds 1500
codex-heartbeat status --tab-title mycel
codex-heartbeat test --tab-title mycel
codex-heartbeat stop --tab-title mycel
```

`codex-heartbeat` is intentionally separate from `ccm`; it targets one visible tab by title and acts as a keepalive/wakeup helper.

## Architecture

`ccm-orchestra` has two main layers plus one small support tool:

- `tmux` session layer, handled by `ccm_orchestra/cli.py`
  Starts and reuses interactive Claude agents, isolates them by worktree, reads transcripts, and runs doctor checks.
- `kitty` collaboration layer, also handled by `ccm_orchestra/cli.py`
  Lists visible tabs, injects messages, and supports reply-friendly relay envelopes between tabs.
- `bin/codex-heartbeat`
  Sends periodic heartbeat prompts into a target `kitty` tab so long-running supervision does not die quietly.

The orchestration strategy is intentionally narrow:

- run normal interactive Claude, not `--print`
- isolate session state by project directory
- read Claude's real transcript files instead of scraping terminal text when possible
- fall back to terminal inspection only when the upstream session itself is misbehaving

## Caveats

- Upstream Claude API instability can still delay or block assistant output; the tool does not hide that.
- `read` depends on Claude transcript availability. `--wait-seconds` helps with lag, not upstream outages.
- transcript discovery follows the active Claude config root, including isolated `cac` environments, before falling back to the default Claude projects directory.
- `kitty` features require a valid `KITTY_LISTEN_ON`.
- This tool is intentionally simple. It is not trying to become a full agent platform.

## Troubleshooting

### Agent keeps hitting 502 after Claude upgrades

If `ccm read ...` shows repeated `api_error status=502` and the agent looks "stuck", do not assume the current shell and the tmux agent are running the same Claude binary.

We hit a real case where:

- the current shell resolved `claude` to `~/.cac/bin/claude` (`Claude Code 2.1.86`)
- an older agent tmux pane had been started earlier with `/opt/homebrew/bin/claude` (`Claude Code 2.1.81`)
- the old agent kept failing until it was restarted

Why this happens:

- `ccm` now resolves and pins the Claude executable path when starting an agent
- but agents that were already running before that fix keep whatever binary they originally launched
- tmux server environment can also keep an old `PATH`, so checking `which claude` in your current shell is not enough

What to do:

1. Check the current shell binary:
   `which claude && claude --version`
2. Run doctor in the target namespace:
   `ccm doctor --cwd "$PWD"`
   If it reports `@@@claude-path-mismatch` or `@@@claude-version-mismatch`, your tmux server would launch a different Claude than the current shell.
3. If the agent predates a Claude or `ccm` upgrade, restart that agent:
   `ccm kill frontend-agent`
   `ccm start frontend-agent --cwd "$PWD"`
4. Then read again:
   `ccm read frontend-agent --wait-seconds 30`

The key rule: if an agent was started before a Claude-path fix or binary upgrade, restart the agent itself. Do not trust the current shell's `which claude` as proof that the running tmux agent is current.

## Repository Layout

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
