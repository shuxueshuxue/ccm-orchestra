# CCM Orchestra

[中文说明](./README.zh-CN.md)

`ccm-orchestra` is a control plane for running persistent, interactive Claude Code helpers in `tmux` and coordinating visible collaboration through `kitty`. Together they form a paired operating model: `tmux` keeps the helper alive and reusable, `kitty` makes the collaboration visible and relay-capable.

The core loop is simple: start a helper in a detached tmux pane, send it prompts, and read its transcript. Sessions are isolated by working directory, so the same helper name can coexist across repos. Together, the `tmux` layer and the `kitty` layer let Claude Code and Codex work in parallel, hand work back and forth, and stay observable when needed.

## The Two Layers

This system has two clearly different layers:

- `tmux` layer: the real session layer. It keeps interactive Claude Code alive, isolates it by worktree, and lets Codex reuse the same helper over time.
- `kitty` layer: the visible collaboration layer. It lets humans and other agents see selected sessions, list visible peers, and exchange messages with receipt-friendly envelopes.

If you remember only one thing, remember this:

- `tmux` and `kitty` are two distinct capabilities, and they can work independently or together
- the default helper loop is in the `tmux` layer: `start -> send -> read`
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
- the same interactive helper can be approached from both sides: humans can attach and inspect it, while programmatic tooling can still send, read, doctor, restart, and supervise it
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

python3 -m unittest tests/test_claude_coop_manager.py -v

ccm guide agent
ccm doctor --cwd "$PWD"
ccm start frontend-helper --cwd "$PWD"
ccm send frontend-helper "Review the current frontend flow and suggest 2-3 improvements." --cwd "$PWD"
ccm read frontend-helper --wait-seconds 20 --cwd "$PWD"
ccm kill frontend-helper --cwd "$PWD"
```

### 3. Install as a CLI

```bash
pip install -e .

ccm doctor
codex-heartbeat status
```

## Command Guide

### Canonical rule

Use the global `ccm` only. If a helper starts failing after a Claude or `ccm` upgrade:

1. Run `ccm doctor --cwd "$PWD"`.
2. Check for `@@@claude-path-mismatch` / `@@@claude-version-mismatch`.
3. Restart the helper. Existing helpers keep the binary and config root they started with.

If you are an agent or another LLM, run `ccm guide agent` before you improvise. That guide contains the longer operating rules, the tmux vs kitty split, and the wakeup model.

### Minimal everyday usage

```bash
ccm start frontend-helper --cwd "$PWD"
ccm send frontend-helper "Review the frontend in this branch and propose improvements." --cwd "$PWD"
ccm read frontend-helper --wait-seconds 30 --cwd "$PWD"
ccm kill frontend-helper --cwd "$PWD"
```

If a session crashed or `kill` was interrupted:

```bash
ccm cleanup --cwd "$PWD"
```

### Keep a long-lived Claude partner

- Each visible Codex tab should usually keep one dedicated, trusted Claude helper in tmux and reuse it over time. Do not kill the helper after every small task. The persistent session is the point.
- Name helpers by job and keep the names specific, for example `frontend-helper` or `docs-editor`. Avoid reusing a vague name that already exists in the current namespace.
- Claude is not just an advisor. It can directly edit the branch, commit, or push, especially for frontend and documentation work. Treat it as a collaborator with write access, not a read-only consultant.

### Start an interactive Claude session

Uses the current directory by default:

```bash
ccm start frontend-helper
```

Or target a specific directory:

```bash
ccm --cwd ~/Codebase/leonai/frontend start frontend-helper
```

### Reuse the same namespace later

```bash
ccm --cwd ~/Codebase/leonai/frontend list
ccm --cwd ~/Codebase/leonai/frontend send frontend-helper "Critique the new layout."
ccm --cwd ~/Codebase/leonai/frontend read frontend-helper --wait-seconds 30
```

### Check environment health

```bash
ccm doctor
```

### Open a helper only when you actually need to look

`open` is not part of the normal loop. Use it only when transcript output is not enough:

- debugging a stuck helper
- supervised live observation
- deliberate visible-tab collaboration

```bash
ccm open frontend-helper --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

### Use visible kitty tabs as peers

This is the optional `kitty` layer, not the core session path.

```bash
ccm tabs --listen-on unix:/tmp/mykitty
ccm relay "feat/main-thread-for-member" "Use Claude to review the UI and report back here." \
  --listen-on unix:/tmp/mykitty \
  --cwd "$PWD" \
  --task "frontend review" \
  --scene "untouched"
```

`ccm tabs` now shows the peer tab title together with its resolved worktree, git branch, and helper identity. `ccm relay` wraps the message with a default envelope and a `reply-via` hint so a newcomer can answer without learning extra ceremony.

This is also the wakeup-safe path for agents in visible tabs:

- use `ccm read` when waiting on Claude helper output from the `tmux` layer
- use `ccm relay` when another visible tab needs to wake up and reply

### Keep the supervising Codex tab alive

```bash
codex-heartbeat start --interval-seconds 1500
codex-heartbeat status
codex-heartbeat stop
```

## Architecture

`ccm-orchestra` has two main layers plus one small support tool:

- `tmux` session layer, handled by `claude_coop_manager.py`
  Starts and reuses interactive Claude helpers, isolates them by worktree, reads transcripts, and runs doctor checks.
- `kitty` collaboration layer, also handled by `claude_coop_manager.py`
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

### Helper keeps hitting 502 after Claude upgrades

If `ccm read ...` shows repeated `api_error status=502` and the helper looks "stuck", do not assume the current shell and the tmux helper are running the same Claude binary.

We hit a real case where:

- the current shell resolved `claude` to `~/.cac/bin/claude` (`Claude Code 2.1.86`)
- an older helper tmux pane had been started earlier with `/opt/homebrew/bin/claude` (`Claude Code 2.1.81`)
- the old helper kept failing until it was restarted

Why this happens:

- `ccm` now resolves and pins the Claude executable path when starting a helper
- but helpers that were already running before that fix keep whatever binary they originally launched
- tmux server environment can also keep an old `PATH`, so checking `which claude` in your current shell is not enough

What to do:

1. Check the current shell binary:
   `which claude && claude --version`
2. Run doctor in the target namespace:
   `ccm doctor --cwd "$PWD"`
   If it reports `@@@claude-path-mismatch` or `@@@claude-version-mismatch`, your tmux server would launch a different Claude than the current shell.
3. If the helper predates a Claude or `ccm` upgrade, restart that helper:
   `ccm kill frontend-helper`
   `ccm start frontend-helper --cwd "$PWD"`
4. Then read again:
   `ccm read frontend-helper --wait-seconds 30`

The key rule: if a helper was started before a Claude-path fix or binary upgrade, restart the helper itself. Do not trust the current shell's `which claude` as proof that the running tmux helper is current.

## Repository Layout

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
