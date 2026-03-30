# CCM Orchestra Agent Notes

Use `ccm` when you want the canonical interactive Claude Code path instead of building around `claude --print`.

Before you improvise, run `ccm guide agent`. That is the long-form operator guide for agents and LLMs.

This system has two layers:

- `tmux` layer: the default path for persistent interactive Claude helpers
- `kitty` layer: optional visible collaboration between tabs

## Default tmux Loop

```bash
ccm start frontend-helper --cwd "$PWD"
ccm send frontend-helper "Review the frontend in this branch and propose improvements." --cwd "$PWD"
ccm read frontend-helper --wait-seconds 30 --cwd "$PWD"
```

Keep the helper if this tab will keep working with it. Do not kill and recreate it after every small turn unless you are deliberately resetting the scene.

## Optional kitty Layer

```bash
ccm tabs --listen-on "${KITTY_LISTEN_ON}"
ccm relay "main" "I am online and ready for tasking." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD" --scene "untouched"
ccm tell "scheduled-tasks" "Please summarize your current frontend direction." --listen-on "${KITTY_LISTEN_ON}"
ccm open frontend-helper --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

## Wechat-Style Peer Layer

```bash
ccm wechat-register mycel --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm wechat-contacts
ccm wechat-send scheduled-tasks "Please summarize your current frontend direction." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm wechat-shift scheduled-tasks "Take over the next frontend simplify pass." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

For a headless Claude/tmux helper, register inside the helper session itself:

```bash
ccm wechat-register claude-handoff --runtime claude --tmux-session ccm-frontend-helper-abcd1234 --cwd "$PWD"
```

`ccm wechat-shift <alias> "..."` is the real handoff primitive. If you currently own the phone thread, shift also rebinds phone ownership to the target alias and emits a short handoff notice to the phone user.

## Phone WeChat Layer

```bash
ccm wechat-connect
ccm wechat-status
ccm wechat-register mycel --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
ccm wechat-bind mycel
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
ccm inspect frontend-helper --cwd "$PWD"
ccm read frontend-helper --raw --json --cwd "$PWD"
codex-heartbeat test --tab-title mycel
```

## Rules

- Always pass `--cwd "$PWD"` unless you intentionally want another namespace.
- Use `--state-path /abs/path/state.json` only when you intentionally want one explicit state file instead of the normal cwd-derived namespace.
- Pick helper names by job and keep them specific. Avoid colliding with helper names that already exist in the current namespace.
- Prefer `ccm read` over scraping terminal text.
- If `ccm read` is empty or transcript resolution lags, run `ccm inspect <helper> --cwd "$PWD"` before guessing. It shows transcript search roots and recent pane tail.
- Use `ccm read --raw --json` when you need unrendered transcript events for MCP/tool-trace debugging.
- Use `ccm list --all-scopes --json` when the session you want may live in another saved namespace.
- Prefer `ccm relay` over `ccm tell` when coordinating with another visible tab. `relay` auto-includes sender context and a reply hint.
- Remember the wakeup model: `ccm read` is poll-based tmux waiting; `ccm relay` is push-based kitty messaging. Polling Claude output will not wake another agent tab.
- `codex-heartbeat start/status/stop/test` can all target a custom visible tab title with `--tab-title ...`. Use `test` for a one-shot push before you start a long-running heartbeat loop.
- Use normal interactive Claude only. The main reason is to avoid drifting into non-interactive automation patterns that may be riskier for the account.
- `open` is not part of the everyday loop. Use it only for debugging, live observation, or deliberate visible-tab collaboration.
- If a session crashed or `kill` was interrupted, run `ccm cleanup --cwd "$PWD"`.
- After every new `ccm wechat-connect`, run `ccm wechat-bind <alias>` again. A new phone WeChat login is a new transport session.
- For ongoing phone delivery, use `ccm wechat-watch --detach`, not an ad-hoc shell background job.
