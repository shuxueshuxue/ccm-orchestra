# CCM Relay Reliability And Discoverability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ccm relay` submit reliably on plain kitty visible tabs first, keep tmux-backed visible-tab hardening, and expose the `read`/`relay`/`codex-heartbeat`/`wechat-watch` mental model more clearly.

**Architecture:** Keep the visible-tab relay envelope unchanged. Tighten the submit seam in two layers: after kitty injection, wait one short beat before visible Enter on all plain kitty delivery paths; if the target tab also resolves to a managed tmux-backed agent, keep the extra `tmux_send_enter(...)` beat. Update the shortest-path docs/help text so users can discover the wakeup model without reading the source.

**Tech Stack:** Python, unittest, argparse help text, Markdown docs

---

### Task 1: Lock the plain-kitty submit seam with tests

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `ccm_orchestra/cli.py`

- [ ] **Step 1: Write the failing plain title-targeted kitty test**

Add a unit test near the existing kitty messaging tests that mocks:

- `time.sleep(...)`
- `run_command(...)`
- `workspace_identity(...)`

Expected behavior:

- `send_message_to_kitty_tab(...)` still does `send-text`
- then waits `TMUX_PASTE_SUBMIT_DELAY_SECONDS`
- then does `send-key enter`

- [ ] **Step 2: Write the failing plain window-targeted kitty test**

Add one adjacent unit test that proves `send_message_to_kitty_window(...)` also waits `TMUX_PASTE_SUBMIT_DELAY_SECONDS` between visible paste and Enter.

- [ ] **Step 3: Run the targeted tests to verify red**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra && python3 -m unittest tests.test_cli.KittyMessagingTests.test_send_message_to_kitty_tab_injects_text_and_enter tests.test_cli.KittyMessagingTests.test_send_message_to_kitty_window_waits_a_beat_before_enter -v
```

Expected: both tests fail because the current plain kitty path submits immediately.

- [ ] **Step 4: Write the minimal implementation**

In `ccm_orchestra/cli.py`, keep the send path narrow:

- after visible kitty `send-text`, wait `TMUX_PASTE_SUBMIT_DELAY_SECONDS`
- then call `send-key enter`
- if the resolved visible target command is `codex`, wait one more short beat and send one extra Enter
- apply the same pacing to both title-targeted and direct window-targeted helpers
- keep the submit sequence in one shared helper so the two visible delivery paths cannot drift on timing behavior

- [ ] **Step 5: Run the targeted tests to verify green**

Run the same unittest command from Step 3.

Expected: both new tests pass.

### Task 2: Keep the tmux-backed visible-agent extra submit seam locked

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `ccm_orchestra/cli.py`

- [ ] **Step 1: Write the failing tmux-backed relay test**

Add a unit test near the existing relay tests that mocks:

- `resolve_current_sender_context(...)`
- `send_message_to_kitty_tab(...)`
- `time.sleep(...)`
- `tmux_send_enter(...)`

Expected behavior:

- if `send_message_to_kitty_tab(...)` returns `agent_tmux_session="ccm-target-1234"`, then `relay_message_to_kitty_tab(...)` calls `time.sleep(TMUX_PASTE_SUBMIT_DELAY_SECONDS)` and `tmux_send_enter("ccm-target-1234")`

- [ ] **Step 2: Write the failing plain-tab relay test**

Add one adjacent unit test that returns `agent_tmux_session=""` and proves `tmux_send_enter(...)` is not called.

- [ ] **Step 3: Run the targeted tests to verify red**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra && python3 -m unittest tests.test_cli.CliTests.test_relay_message_to_kitty_tab_submits_tmux_backed_visible_agent tests.test_cli.CliTests.test_relay_message_to_kitty_tab_does_not_submit_tmux_for_plain_visible_tab -v
```

Expected: at least the new tmux-backed relay test fails because `relay_message_to_kitty_tab(...)` does not currently call `tmux_send_enter(...)`.

- [ ] **Step 4: Write the minimal implementation**

In `ccm_orchestra/cli.py`, keep `relay_message_to_kitty_tab(...)` narrow:

- read `agent_tmux_session` from the payload returned by `send_message_to_kitty_tab(...)`
- if non-empty, wait `TMUX_PASTE_SUBMIT_DELAY_SECONDS`
- call `tmux_send_enter(...)`

- [ ] **Step 5: Run the targeted tests to verify green**

Run the same unittest command from Step 3.

Expected: both new tests pass.

### Task 3: Expose the wakeup model in help and docs

**Files:**
- Modify: `ccm_orchestra/cli.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update `ccm --help` epilog**

Add one short sentence that explicitly says:

- `read` polls tmux transcript output
- `relay` wakes visible tabs
- `codex-heartbeat` keeps one visible tab awake

- [ ] **Step 2: Update the operator docs**

Tighten the existing sections in:

- `README.md`
- `README.zh-CN.md`
- `AGENTS.md`

Add one short four-item mental model and one short relay reply-back example.

- [ ] **Step 3: Spot-check help output**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra && ccm --help
```

Expected: the epilog now names `codex-heartbeat` directly and distinguishes transcript polling from visible-tab wakeups.

### Task 4: Freeze the slice in checkpoint docs

**Files:**
- Modify: `docs/superpowers/specs/2026-04-08-ccm-relay-reliability-and-discoverability-design.md`
- Modify: `/Users/lexicalmathical/Codebase/algorithm-repos/mysale-cca/rebuild-agent-core/checkpoints/architecture/new_updates.md`

- [ ] **Step 1: Record the repo-local design**

Make sure the spec states:

- the relay submit seam
- the discoverability seam
- the chosen minimal fix
- the stopline

- [ ] **Step 2: Record the external checkpoint delta**

Add one new dated section to `new_updates.md` with:

- concrete field evidence about user confusion
- the relay linger seam
- the chosen narrow repair direction
- the corrected fact that plain kitty tabs are now the dominant peer shape

### Task 5: Verify the whole slice

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `ccm_orchestra/cli.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Run the focused CLI unit pack**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra && python3 -m unittest tests/test_cli.py -v
```

Expected: green.

- [ ] **Step 2: Run a live Codex-tab probe with proxy**

Run a disposable plain kitty tab that starts interactive Codex with the current proxy env:

```bash
kitty @ --to "${KITTY_LISTEN_ON}" launch --type=tab --title relay-codex-lab env \
  http_proxy="${http_proxy}" https_proxy="${https_proxy}" all_proxy="${all_proxy}" \
  zsh -lc 'cd /Users/lexicalmathical/Codebase/ccm-orchestra && codex --dangerously-bypass-approvals-and-sandbox'
```

Then use installed `ccm relay` to send a deterministic prompt and confirm the Codex tab returns the exact token.

Expected: plain interactive Codex accepts the relay envelope and produces the exact expected token.

- [ ] **Step 3: Spot-check no stray lab state remains**

Run:

```bash
ccm list --cwd /Users/lexicalmathical/Codebase/ccm-orchestra
ccm tabs --listen-on "${KITTY_LISTEN_ON}" --cwd /Users/lexicalmathical/worktrees/leonai--hub
```

Expected: no leftover temporary `relay-*` session/tab remains.

- [ ] **Step 4: Sync operator-facing docs**

Make sure the shortest-path docs all say the same thing:

- `ccm guide human`
- `README.md`
- `README.zh-CN.md`
- `AGENTS.md`
- `docs/codex-claude-visible-collab-playbook.md`

Expected: they all say that visible Codex delivery gets one extra Enter retry, and that this lowers submit misses rather than mathematically guaranteeing them away.
