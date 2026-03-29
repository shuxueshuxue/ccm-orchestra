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

### 2. Run directly from the repo

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

### 3. Install as a CLI

```bash
pip install -e .

ccm doctor
codex-heartbeat status
```

## Command Guide

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

## Caveats

- Upstream Claude API instability can still delay or block assistant output; the tool does not hide that.
- `read` depends on Claude transcript availability. `--wait-seconds` helps with lag, not upstream outages.
- `kitty` features require a valid `KITTY_LISTEN_ON`.
- This tool is intentionally simple. It is not trying to become a full agent platform.

## Repository Layout

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

## Status

This project is live enough to be useful and small enough to improve fast. That is exactly the point.
