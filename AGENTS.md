# CCM Orchestra Agent Notes

Use `ccm` when you want a persistent interactive Claude Code session instead of `claude --print`.

This system has two layers:

- `tmux` layer: the default path for persistent interactive Claude helpers
- `kitty` layer: optional visible collaboration between tabs

## Default tmux Loop

```bash
ccm start frontend-helper --cwd "$PWD"
ccm send frontend-helper "Review the frontend in this branch and propose improvements." --cwd "$PWD"
ccm read frontend-helper --wait-seconds 30 --cwd "$PWD"
ccm kill frontend-helper --cwd "$PWD"
```

## Optional kitty Layer

```bash
ccm tabs --listen-on "${KITTY_LISTEN_ON}"
ccm relay "main" "I am online and ready for tasking." --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD" --scene "untouched"
ccm tell "scheduled-tasks" "Please summarize your current frontend direction." --listen-on "${KITTY_LISTEN_ON}"
ccm open frontend-helper --listen-on "${KITTY_LISTEN_ON}" --cwd "$PWD"
```

## Useful Commands

```bash
ccm list --cwd "$PWD"
ccm cleanup --cwd "$PWD"
ccm doctor --cwd "$PWD"
```

## Rules

- Always pass `--cwd "$PWD"` unless you intentionally want another namespace.
- Prefer `ccm read` over scraping terminal text.
- Prefer `ccm relay` over `ccm tell` when coordinating with another visible tab. `relay` auto-includes sender context and a reply hint.
- Use normal interactive Claude only. Do not switch back to `claude --print`.
- `open` is not part of the everyday loop. Use it only for debugging, live observation, or deliberate visible-tab collaboration.
- If a session crashed or `kill` was interrupted, run `ccm cleanup --cwd "$PWD"`.
