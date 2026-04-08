# CCM Orchestra Agent Notes

Use `ccm` when you want the canonical interactive Claude Code path instead of building around `claude --print`.

Before you improvise, run `ccm guide agent`. That is the long-form operator guide for agents and LLMs.

This system has two layers:

- `tmux` layer: the default path for persistent interactive Claude agents
- `kitty` layer: optional visible collaboration between tabs

## Default tmux Loop

```bash
ccm start frontend-agent --cwd "$PWD"
ccm send frontend-agent "Review the frontend in this branch and propose improvements." --cwd "$PWD"
ccm read frontend-agent --wait-seconds 30 --cwd "$PWD"
```

Keep the agent if this tab will keep working with it. Do not kill and recreate it after every small turn unless you are deliberately resetting the scene.

## Optional kitty Layer

```bash
ccm tabs --listen-on "${KITTY_LISTEN_ON}"
ccm relay "main" "I am online and ready for tasking." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD" --scene "untouched"
ccm tell "scheduled-tasks" "Please summarize your current frontend direction." --listen-on "${KITTY_LISTEN_ON}"
ccm open frontend-agent --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

Visible-tab communication rule:

- `ccm relay` is the primary path for tab-to-tab chat.
- `ccm tell` is legacy raw injection only.
- Reading raw tab text or pane tail is legacy debug-only evidence, not the normal collaboration path.

## Wechat-Style Peer Layer

```bash
ccm wechat-targets --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm wechat-send kitty:scheduled-tasks "Please summarize your current frontend direction." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm wechat-shift kitty:scheduled-tasks "Take over the next frontend simplify pass." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

Use one direct target language:
- `kitty:<tab-title>`
- `tmux:<session-name>`

For a headless Claude/tmux agent, target the tmux session directly:

```bash
ccm wechat-send tmux:ccm-frontend-agent-abcd1234 "Please take over the phone thread." --cwd "$PWD"
```

`ccm wechat-shift <target> "..."` is the real handoff primitive. If you currently own the phone thread, shift also rebinds phone ownership to that target and emits a short handoff notice to the phone user.

## Phone WeChat Layer

```bash
ccm wechat-connect
ccm wechat-status
ccm wechat-bind kitty:mycel
ccm wechat-watch --detach --listen-on "${KITTY_LISTEN_ON}"
ccm wechat-watch-status
```

If the user wants actual phone WeChat messaging, use the commands above. `ccm wechat-guide agent` explains the full split between the direct phone transport and the peer layer.

## Useful Commands

```bash
ccm list --cwd "$PWD"
ccm list --all-scopes --json
ccm cleanup --cwd "$PWD"
ccm doctor --cwd "$PWD"
ccm inspect frontend-agent --cwd "$PWD"
ccm read frontend-agent --raw --json --cwd "$PWD"
ccm heartbeat test --tab-title mycel
```

## Rules

- Always pass `--cwd "$PWD"` unless you intentionally want another namespace.
- Use `--state-path /abs/path/state.json` only when you intentionally want one explicit state file instead of the normal cwd-derived namespace.
- Pick agent names by job and keep them specific. Avoid colliding with agent names that already exist in the current namespace.
- Prefer `ccm read` over scraping terminal text.
- If `ccm read` is empty or transcript resolution lags, run `ccm inspect <agent> --cwd "$PWD"` before guessing. It shows transcript search roots and recent pane tail.
- Use `ccm read --raw --json` when you need unrendered transcript events for MCP/tool-trace debugging.
- Use `ccm list --all-scopes --json` when the session you want may live in another saved namespace.
- Treat `ccm relay` as the primary path when coordinating with another visible tab. `relay` auto-includes sender context and a reply hint.
- Treat `ccm tell` as legacy raw fire-and-forget injection only.
- Treat raw tab text and pane tail as legacy debug evidence only. Tabs should normally talk through `relay`, not by reading each other's raw terminal output.
- Remember the wakeup model: `ccm read` is poll-based tmux waiting; `ccm relay` is push-based kitty messaging. Polling Claude output will not wake another agent tab.
- Visible Codex tabs now get one extra Enter retry on relay delivery. Treat that as probability reduction for submit misses, not a mathematical guarantee.
- `ccm heartbeat start/status/stop/test` can all target a custom visible tab title with `--tab-title ...`. Use `test` for a one-shot push before you start a long-running heartbeat loop.
- `codex-heartbeat ...` remains the equivalent direct alias. Treat heartbeat as a visible-tab keepalive tool only.
- `ccm wechat-watch` is the phone transport watcher. Do not treat it as a general scheduler for agent transcript polling.
- Minimal reply-back loop: `ccm relay "<peer-tab>" "..." --cwd "$PWD"` and the peer replies with `ccm relay <your-tab> "..."`.
- Use normal interactive Claude only. The main reason is to avoid drifting into non-interactive automation patterns that may be riskier for the account.
- `open` is not part of the everyday loop. Use it only for debugging, live observation, or deliberate visible-tab collaboration.
- If a session crashed or `kill` was interrupted, run `ccm cleanup --cwd "$PWD"`.
- After every new `ccm wechat-connect`, run `ccm wechat-bind <target>` again. A new phone WeChat login is a new transport session.
- For ongoing phone delivery, use `ccm wechat-watch --detach`, not an ad-hoc shell background job.
