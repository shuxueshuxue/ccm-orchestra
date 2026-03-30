# Codex Claude Visible Collaboration Playbook

This playbook is for a visible Codex tab that should collaborate with interactive Claude Code while keeping the full live terminal process visible in `kitty`.

## Mental Model

Treat the system as two layers:

- `tmux` layer: the real Claude helper session. This is the default path.
- `kitty` layer: optional visible collaboration and observation.

Do not confuse "visible" with "canonical". Most work should stay in the `tmux` layer and use transcript reads.

## Tools

- `ccm`: manages interactive Claude Code sessions
- `ccm tabs`: lists visible `kitty` tabs with resolved worktree, branch, and helper identity
- `ccm tell`: low-level raw message injection into another visible `kitty` tab by title
- `ccm relay`: collaboration-first messaging with sender envelope and `reply-via` hint
- `codex-heartbeat`: keeps the supervising `main` tab awake

## Rules

- Use interactive Claude only. Do not use Claude `--print`.
- The reason is not style. We want persistent sessions, real transcript history, and a workflow that does not depend on non-interactive automation patterns.
- Keep Claude scoped to your current worktree and your current branch goal.
- Use Claude as a frontend reviewer, critic, and alternative-implementation generator.
- Iterate several times. One pass is not enough.
- Keep changes branch-scoped. Do not drift into unrelated cleanup.
- When finished, report exactly what changed, what Claude suggested, and what you accepted or rejected.

## Before You Blame `ccm`

If a helper suddenly starts producing repeated `502` errors after a Claude or `ccm` upgrade, check whether the helper was started earlier with a stale Claude binary inside tmux.

The failure mode we hit in practice:

- current shell: `~/.cac/bin/claude` on a newer version
- existing tmux helper pane: old `/opt/homebrew/bin/claude`

That helper must be restarted. `which claude` in the current shell does not prove the running helper is current.

Use:

```bash
which claude && claude --version
ccm doctor --cwd "$PWD"
ccm kill frontend-helper
ccm start frontend-helper --cwd "$PWD"
```

If `ccm doctor` reports `@@@claude-path-mismatch` or `@@@claude-version-mismatch`, your tmux server would launch a different Claude than the current shell.

## Startup

Assume you are already inside the correct worktree.

```bash
export CCM_READY_TIMEOUT_SECONDS=300
ccm start frontend-helper --cwd "$PWD"
```

Only open the helper into a visible `kitty` tab if transcript output is not enough:

```bash
ccm open frontend-helper --listen-on "${KITTY_LISTEN_ON:-unix:/tmp/mykitty}"
```

## Frontend loop

### 1. Ask Claude for a focused review

```bash
ccm send frontend-helper "You are helping on the current branch in $PWD. Review only the frontend/UI surfaces affected by this branch. Identify the current UX and visual weaknesses, then propose 2-3 better implementations. Stay minimal and branch-scoped."
ccm read frontend-helper --wait-seconds 30
```

### 2. Force iteration

```bash
ccm send frontend-helper "Pick the best proposal and turn it into a concrete implementation plan with file-level suggestions."
ccm read frontend-helper --wait-seconds 30

ccm send frontend-helper "Challenge your own proposal. Give me one bolder alternative and one safer alternative, with clear tradeoffs."
ccm read frontend-helper --wait-seconds 30

ccm send frontend-helper "Based on the current code, what exact frontend changes should be made first for the highest user-facing payoff?"
ccm read frontend-helper --wait-seconds 30
```

### 3. Implement, then ask Claude to critique again

After you make changes:

```bash
ccm send frontend-helper "I have updated the branch. Critique the result strictly. What still feels weak, confusing, or visually flat? Keep the feedback branch-scoped."
ccm read frontend-helper --wait-seconds 30
```

Repeat this loop until the frontend is clearly better, not merely different.

## What to optimize

- clearer hierarchy
- tighter copy and action labels
- better empty / loading / error states
- stronger visual distinction between related concepts
- better information density without clutter
- mobile behavior
- more intentional layout and spacing

## Reporting back

When you have a good result, write a final report in your current Codex tab that includes:

1. the frontend files you changed
2. what Claude suggested
3. which suggestions you accepted
4. which suggestions you rejected
5. what checks you ran

Then clean up:

```bash
ccm kill frontend-helper
```

## Visible tab messaging

If you need to message another visible Codex or Claude tab, prefer `relay`:

```bash
ccm tabs --listen-on "${KITTY_LISTEN_ON:-unix:/tmp/mykitty}"
ccm relay "scheduled-tasks" "Please summarize your current frontend direction." \
  --listen-on "${KITTY_LISTEN_ON:-unix:/tmp/mykitty}" \
  --cwd "$PWD" \
  --task "frontend sync" \
  --scene "untouched"
```

Use `ccm tell` only when you deliberately want raw text with no envelope. For newcomer onboarding, the canonical flow is:

```bash
ccm doctor --cwd "$PWD"
ccm tabs --listen-on "${KITTY_LISTEN_ON:-unix:/tmp/mykitty}"
ccm relay "main" "I am online and ready for tasking." --listen-on "${KITTY_LISTEN_ON:-unix:/tmp/mykitty}" --cwd "$PWD" --scene "untouched"
```
