# Claude Codex Frontend Playbook

This playbook is for a Codex session that wants to use interactive Claude Code as a frontend copilot.

## Tooling

- Claude session manager: `/Users/lexicalmathical/worktrees/serverManagement--feat-claude-codex-coop/bin/ccm`
- Manager repo docs: `/Users/lexicalmathical/worktrees/serverManagement--feat-claude-codex-coop/README.md`
- Default ready timeout: `CCM_READY_TIMEOUT_SECONDS=300`

## Rules

- Use interactive Claude only. Do not use Claude `--print`.
- Keep Claude scoped to your current worktree and your current branch goal.
- Treat Claude as a frontend reviewer and alternative-implementation generator, not as the owner of the whole branch.
- Iterate. One prompt is not enough.

## Recommended loop

Assume your current worktree is the repo you are already editing.

### 1. Start a dedicated Claude helper

```bash
export CCM_READY_TIMEOUT_SECONDS=300
CCM=/Users/lexicalmathical/worktrees/serverManagement--feat-claude-codex-coop/bin/ccm
$CCM start frontend-helper --cwd "$PWD"
```

### 2. Ask Claude for a focused frontend review

Use a prompt like this, but adapt it to your branch:

```bash
$CCM send frontend-helper "You are helping on the current branch in $PWD. Review only the frontend/UI surfaces affected by this branch. First, inspect the relevant files, identify the current UX and visual weaknesses, and propose 2-3 better implementations. Stay minimal and branch-scoped."
```

### 3. Read only unread output

```bash
$CCM read frontend-helper
```

If you want to watch the live session in a visible terminal:

```bash
$CCM open frontend-helper
```

### 4. Iterate hard

Run several rounds, not one:

```bash
$CCM send frontend-helper "Now pick the best proposal and refine it into a concrete implementation plan with file-level suggestions."
$CCM read frontend-helper

$CCM send frontend-helper "Challenge your own proposal. Give me one bolder alternative and one safer alternative, with clear tradeoffs."
$CCM read frontend-helper

$CCM send frontend-helper "Based on the current code, what exact frontend changes should be made first for the highest user-facing payoff?"
$CCM read frontend-helper
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
$CCM kill frontend-helper
```
