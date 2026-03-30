# CCM Orchestra

[中文说明](./README.zh-CN.md)

`ccm-orchestra` turns interactive Claude Code sessions into infrastructure instead of ceremony.

It gives Codex a practical control plane for running real Claude sessions in the background, steering them through `tmux`, surfacing their transcript incrementally, and reopening them in `kitty` only when humans actually need to look. The result is simple: fewer dead-end terminal rituals, more usable parallel agent work.

## Why This Exists

Most "multi-agent" workflows fall apart in exactly the same places:

- the LLM session is not persistent
- state gets mixed across projects
- terminal automation is brittle
- the "helper" is just a wrapper around non-interactive mode
- supervision becomes theater instead of real operations

`ccm-orchestra` is built to avoid that trap. It keeps Claude interactive, keeps sessions isolated by working directory, and gives Codex enough leverage to supervise, reuse, and clean up those sessions like a real toolchain.

## Features

- Persistent interactive Claude Code sessions via detached `tmux`
- Namespace isolation by working directory, so same helper names can coexist across repos
- Incremental transcript reading from Claude's real JSONL session logs
- `kitty` reopen flow for live inspection when needed
- `kitty` tab listing and message injection for visible peer-to-peer coordination
- Heartbeat helper for keeping a supervising Codex tab alive
- `doctor` command for environment and namespace sanity checks
- `read --wait-seconds` for transcript lag without ad hoc sleep loops
- Minimal shell entrypoints, no heavy framework required

## Quickstart

### 1. Prerequisites

- `python3`
- `tmux`
- `claude`
- `kitty` for `open` or heartbeat features

### 2. Run from anywhere with the global CLI

```bash
git clone <repo-url>
cd ccm-orchestra

python3 -m unittest tests/test_claude_coop_manager.py -v

ccm doctor --cwd "$PWD"
ccm start frontend-helper --cwd "$PWD"
ccm send frontend-helper "Review the current frontend flow and suggest 2-3 improvements." --cwd "$PWD"
ccm read frontend-helper --wait-seconds 20 --cwd "$PWD"
ccm open frontend-helper --cwd "$PWD"
ccm tabs --listen-on unix:/tmp/mykitty
ccm tell "scheduled-tasks" "Please review the current frontend and reply in this tab." --listen-on unix:/tmp/mykitty
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

### Minimal everyday usage

```bash
ccm start frontend-helper --cwd "$PWD"
ccm send frontend-helper "Review the frontend in this branch and propose improvements." --cwd "$PWD"
ccm read frontend-helper --wait-seconds 30 --cwd "$PWD"
ccm open frontend-helper --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm kill frontend-helper --cwd "$PWD"
```

If a session crashed or `kill` was interrupted:

```bash
ccm cleanup --cwd "$PWD"
```

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

### Use visible kitty tabs as peers

```bash
ccm tabs --listen-on unix:/tmp/mykitty
ccm relay "feat/main-thread-for-member" "Use Claude to review the UI and report back here." \
  --listen-on unix:/tmp/mykitty \
  --cwd "$PWD" \
  --task "frontend review" \
  --scene "untouched"
```

`ccm tabs` now shows the peer tab title together with its resolved worktree, git branch, and helper identity. `ccm relay` wraps the message with a default envelope and a `reply-via` hint so a newcomer can answer without learning extra ceremony.

### Keep the supervising Codex tab alive

```bash
codex-heartbeat start --interval-seconds 1500
codex-heartbeat status
codex-heartbeat stop
```

## Architecture

`ccm-orchestra` has two moving parts:

- `claude_coop_manager.py`
  Handles session lifecycle, transcript reading, namespace isolation, and `kitty` reopening.
- `bin/codex-heartbeat`
  Sends periodic heartbeat prompts into a target `kitty` tab so long-running supervision does not die quietly.

The orchestration strategy is intentionally narrow:

- run normal interactive Claude, not `--print`
- isolate session state by project directory
- read Claude's real transcript files instead of scraping terminal text when possible
- fall back to terminal inspection only when the upstream session itself is misbehaving

## Sanity-Checked Behaviors

These have been explicitly verified in this repo:

- unit tests pass
- two different directories can run the same helper name in parallel
- `start --cwd <dir>` honors the requested working directory
- heartbeat injection into the `Main` `kitty` tab works in practice
- real interactive Claude sessions launch and receive prompts
- visible kitty tabs can be listed and can receive injected messages by title

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
│   └── claude-codex-frontend-playbook.md
├── tests/
│   └── test_claude_coop_manager.py
├── claude_coop_manager.py
├── pyproject.toml
├── README.md
└── README.zh-CN.md
```

## Status

This project is live enough to be useful and small enough to improve fast. That is exactly the point.
