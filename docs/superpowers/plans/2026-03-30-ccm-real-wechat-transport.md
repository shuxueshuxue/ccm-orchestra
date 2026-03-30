# CCM Real WeChat Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the leon-bound phone WeChat path with a real independent WeChat transport owned by `ccm`.

**Architecture:** Add one global transport state file plus a small set of direct iLink protocol helpers inside `claude_coop_manager.py`. Reuse the existing peer registry only as a delivery target, not as a transport dependency.

**Tech Stack:** Python standard library, Swift/CoreImage for QR rendering, existing `kitty` messaging helpers

---

### Task 1: Replace leon-bound phone commands with direct transport state

**Files:**
- Modify: `claude_coop_manager.py`
- Test: `tests/test_claude_coop_manager.py`

- [ ] Write failing tests for direct connect/state behavior and remove leon-login assumptions
- [ ] Run `python3 -m unittest tests/test_cli.py -v`
- [ ] Implement minimal direct transport state and direct QR/status helpers
- [ ] Run `python3 -m unittest tests/test_cli.py -v`

### Task 2: Bind incoming phone WeChat messages to a direct target

**Files:**
- Modify: `ccm_orchestra/cli.py`
- Test: `tests/test_cli.py`

- [ ] Write failing tests for `wechat-bind`, `wechat-unbind`, and inbound delivery formatting
- [ ] Run targeted tests
- [ ] Implement the minimal bind/unbind and delivery helpers
- [ ] Run targeted tests again

### Task 3: Add phone-user reply and polling commands

**Files:**
- Modify: `claude_coop_manager.py`
- Test: `tests/test_claude_coop_manager.py`

- [ ] Write failing tests for `wechat-users`, `wechat-reply`, `wechat-poll-once`, and `wechat-watch` entry behavior
- [ ] Run targeted tests
- [ ] Implement the minimal direct polling and reply path
- [ ] Run full test suite

### Task 4: Rewrite docs and help around the two WeChat layers

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `AGENTS.md`

- [ ] Remove leon-specific onboarding instructions from docs/help
- [ ] Add the new direct `ccm` transport flow and the bound-alias delivery model
- [ ] Run `git diff --check`

### Task 5: Run live smoke and sync global install

**Files:**
- Modify: `~/.local/share/ccm/claude_coop_manager.py`

- [ ] Run a live QR connect smoke without opening the QR window
- [ ] Verify QR file generation and transport state persistence
- [ ] Sync the updated manager into the global install
