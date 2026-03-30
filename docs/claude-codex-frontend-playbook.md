# Claude Codex Frontend Playbook

This playbook is for a Codex session that wants to use interactive Claude Code as a frontend copilot.

## Mental Model

Treat the system as two layers:

- `tmux` layer: the default path for persistent interactive Claude helpers
- `kitty` layer: optional visibility and peer-to-peer coordination

Most frontend work should stay in the `tmux` layer and use `ccm read` instead of staring at a live terminal.

The wakeup model matters:

- `ccm read` is poll-based waiting on the helper transcript
- `ccm relay` is push-based messaging into another visible tab

If you need another agent to wake up and reply later, do not sit on `read` and hope. Use `relay`.

## Tooling

- Claude session manager: `ccm`
- Manager repo docs: `~/Codebase/ccm-orchestra/README.md`
- Default ready timeout: `CCM_READY_TIMEOUT_SECONDS=300`

## Rules

- Use interactive Claude only. Do not use Claude `--print`.
- The main reason is operational: keep the workflow away from non-interactive automation patterns that may be more likely to trigger account risk controls. The tmux layer then gives us the process boundary and transcript flow we want.
- Keep Claude scoped to your current worktree and your current branch goal.
- Treat Claude as a frontend reviewer and alternative-implementation generator, not as the owner of the whole branch.
- Iterate. One prompt is not enough.

## Stale Helper Warning

If `ccm read frontend-helper --wait-seconds 30` keeps returning repeated `502` errors, the first thing to check is whether the helper tmux pane is still running an older Claude binary.

A real failure case looked like this:

- current shell resolved `claude` to `~/.cac/bin/claude`
- the already-running helper had been launched earlier with `/opt/homebrew/bin/claude`

In that situation, restarting the helper fixed the problem:

```bash
which claude && claude --version
ccm doctor --cwd "$PWD"
ccm kill frontend-helper
ccm start frontend-helper --cwd "$PWD"
```

Do not assume the current shell's `which claude` reflects what an already-running tmux helper is using.
If `ccm doctor` reports `@@@claude-path-mismatch` or `@@@claude-version-mismatch`, your tmux server would launch a different Claude than the current shell.

## Recommended loop

Assume your current worktree is the repo you are already editing.

### 1. Start a dedicated Claude helper

```bash
export CCM_READY_TIMEOUT_SECONDS=300
ccm start frontend-helper --cwd "$PWD"
```

### 2. Ask Claude for a focused frontend review

Use a prompt like this, but adapt it to your branch:

```bash
ccm send frontend-helper "You are helping on the current branch in $PWD. Review only the frontend/UI surfaces affected by this branch. First, inspect the relevant files, identify the current UX and visual weaknesses, and propose 2-3 better implementations. Stay minimal and branch-scoped."
```

### 3. Read only unread output

```bash
ccm read frontend-helper --wait-seconds 30
```

If you want to watch the live session in a visible terminal:

This is optional. `ccm open` is not part of the default loop.

```bash
ccm open frontend-helper --listen-on "${KITTY_LISTEN_ON:-unix:/tmp/mykitty}"
```

### 4. Iterate hard

Run several rounds, not one:

```bash
ccm send frontend-helper "Now pick the best proposal and refine it into a concrete implementation plan with file-level suggestions."
ccm read frontend-helper --wait-seconds 30

ccm send frontend-helper "Challenge your own proposal. Give me one bolder alternative and one safer alternative, with clear tradeoffs."
ccm read frontend-helper --wait-seconds 30

ccm send frontend-helper "Based on the current code, what exact frontend changes should be made first for the highest user-facing payoff?"
ccm read frontend-helper --wait-seconds 30
```

Good iteration targets:

- clarity of layout and hierarchy
- reduction of visual noise
- stronger empty/loading/error states
- better information density
- tighter action placement
- mobile behavior
- branch-specific UX polish

## What to do with Claude's output

1. Review Claude's suggestions yourself.
2. Keep only the branch-scoped frontend work that improves the user experience.
3. Implement or refine the best parts.
4. Ask Claude to critique the updated result again.
5. Repeat until the frontend direction is clearly better, not just different.

## Completion checklist

- You used Claude for multiple iterations, not a single pass.
- You can explain which suggestions you accepted and rejected.
- The branch frontend is better aligned, clearer, and more deliberate.
- You ran the relevant checks for your branch.
- You killed the helper when done:

```bash
ccm kill frontend-helper
```
