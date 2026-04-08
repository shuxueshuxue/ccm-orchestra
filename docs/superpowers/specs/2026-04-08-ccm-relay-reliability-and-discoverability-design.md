# CCM Relay Reliability And Discoverability Design

## Goal

Take one narrow CCM debt slice to close three user-facing seams without expanding product scope:

- `ccm relay` sometimes leaves text sitting in the target visible tab instead of submitting it
- new users collapse `read`, `relay`, `codex-heartbeat`, and `wechat-watch` into one fuzzy model
- `codex-heartbeat` exists but is not exposed clearly enough in the main CCM entry points

## Scope

This slice is intentionally small.

In scope:

- fix the visible-tab relay submit path for plain kitty tabs first, because that is now the dominant collaboration surface
- keep the extra tmux-backed visible-agent submit hardening that was already added
- improve the shortest-path docs/help exposure for the wakeup model
- freeze the evidence and stopline in checkpoint-style docs

Out of scope:

- a new generic `agent-watch` daemon
- changing the tmux transcript model
- redesigning WeChat watcher behavior
- broad CLI restructuring

## Current Facts

- `ccm read` is a tmux transcript poll loop, not a wakeup path.
- `ccm relay` is the visible-tab wakeup path.
- `codex-heartbeat` is a separate tool, not a `ccm` subcommand.
- `wechat-watch` is a transport-specific background watcher, not a general scheduler.
- The current plain-kitty submit path still depends on kitty-only injection: `send-text` then immediate `send-key enter`.
- A live local repro showed text visibly accumulating in a target tab without being submitted, which matches the user report that relay messages can linger.
- The repo already has a stronger submit pattern for other tmux-backed delivery surfaces: after injecting visible text, it also triggers `tmux_send_enter(...)`.
- Fresh operator correction matters more than the earlier assumption: the high-frequency peers are now mostly plain kitty tabs, not tmux-backed managed agent tabs. So plain kitty submit honesty is the primary seam, not a tail case.
- The operator also narrowed the symptom more sharply: pure command-line tabs are stable; the unstable surface is plain kitty tabs running interactive Codex. Live verification therefore has to use a proxy-enabled Codex tab rather than treating shell stability as enough evidence.

## Options

### Option A: Pace the kitty submit path itself

Keep the current model but add one short beat between `send-text` and `send-key enter` for plain visible tabs and visible-window delivery.

Pros:

- smallest code diff
- directly targets the current dominant peer shape

Cons:

- still depends on kitty rather than a deeper transport

### Option B: For tmux-backed visible tabs, keep the visible kitty injection but also submit through tmux

After `relay` injects the envelope into the visible tab, detect whether that tab maps to a managed tmux-backed agent. If yes, trigger `tmux_send_enter(...)` on the mapped session after a short beat.

Pros:

- matches the delivery pattern already used elsewhere in CCM
- directly targets the submit seam reported by users
- keeps visible collaboration behavior unchanged

Cons:

- does not fix the primary plain-kitty path by itself

### Option C: Replace `send-key enter` with a different kitty-only transport

For example, rework relay around one-shot `send-text` payload shaping or a different visible-tab delivery trick.

Pros:

- potentially removes two-step kitty timing issues

Cons:

- less aligned with current repo patterns
- more speculative than necessary for this slice

## Recommendation

Choose Option A plus the already-landed Option B hardening.

That is the honest minimal repair after the latest field correction. Plain kitty tabs are the dominant peer shape now, so the first submit beat has to live in the kitty path itself. Keep the tmux-backed extra submit because it still hardens managed visible agents, but do not pretend that solves the main operator path.

## Design

### Relay submit path

- keep the current relay envelope format
- keep the current kitty visible-tab text injection
- after visible kitty `send-text`, wait one short beat before `send-key enter` for both title-targeted and window-targeted visible delivery
- for visible delivery paths whose target command is `codex`, add one extra delayed Enter after the first Enter; this is a bounded Codex-only retry because kitty cannot expose Codex's live input buffer for direct stuck-input confirmation
- when the resolved target tab has `agent_tmux_session`, keep the extra tmux submit beat and `tmux_send_enter(...)`
- keep the visible kitty submit sequence in one shared helper so title-targeted relay and direct window delivery cannot drift apart again
- do not add fallback silence or swallow failures; if the tmux submit path is broken, fail loudly

### Discoverability and mental model

Clarify the shortest-path operator model in the main docs/help:

- `read` = tmux transcript polling
- `relay` = visible-tab push/wakeup
- `codex-heartbeat` = visible-tab keepalive tool
- `wechat-watch` = phone transport watcher

Also make the residual risk explicit in operator-facing entry points:

- visible Codex delivery gets an extra Enter retry
- that lowers submit misses
- it is not a mathematical guarantee that no rare miss remains

The top-level `ccm --help` epilog should mention the wakeup split and point to `codex-heartbeat` by name so users do not infer capability absence from the missing subcommand.

### Checkpointing

Freeze this slice in two places:

- repo-local spec/plan docs for implementation context
- external checkpoint notes for the broader cross-repo memory layer

## Files

- Modify: `ccm_orchestra/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `AGENTS.md`
- Modify: `docs/codex-claude-visible-collab-playbook.md`
- Add: `docs/superpowers/plans/2026-04-08-ccm-relay-reliability-and-discoverability.md`
- Modify: `/Users/lexicalmathical/Codebase/algorithm-repos/mysale-cca/rebuild-agent-core/checkpoints/architecture/new_updates.md`

## Testing

- unit test: plain kitty title-targeted send waits one short beat before Enter
- unit test: plain kitty title-targeted send retries Enter once for Codex tabs only
- unit test: plain kitty window-targeted send waits one short beat before Enter
- unit test: relay to a tmux-backed visible tab triggers `tmux_send_enter(...)`
- unit test: relay to a plain visible tab does not trigger `tmux_send_enter(...)`
- focused unit pack: `python3 -m unittest tests/test_cli.py -v`
- spot-check `ccm --help` output after the epilog change
- live probe: proxy-enabled plain Codex tab accepts installed `ccm relay` envelope and returns the exact expected token
- live probe: proxy-enabled plain Codex tab accepts installed runtime `send_message_to_kitty_window(...)` delivery and returns the exact expected token

## Stopline

This slice is done when:

- relay submission for plain kitty visible tabs is covered by red/green tests
- tmux-backed visible-tab extra submit remains covered by red/green tests
- docs/help expose the four-way mental model clearly enough that new users do not have to infer it from source
- no new watcher/scheduler feature is introduced in the same tranche
