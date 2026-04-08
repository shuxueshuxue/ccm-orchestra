# CCM Heartbeat Exposure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the shipped heartbeat tool through `ccm heartbeat ...` without changing heartbeat behavior.

**Architecture:** Keep heartbeat logic in `ccm_orchestra/heartbeat.py` and add one forwarding subcommand in `ccm_orchestra/cli.py`. Update help and docs so operators can discover the alias from the main entry points.

**Tech Stack:** Python 3.11+, `argparse`, `unittest`

---

### Task 1: Lock The Alias Contract With Tests

**Files:**
- Modify: `tests/test_cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write a failing parser help test**

Assert that `build_parser()` exposes a `heartbeat` subcommand with help text that frames it as the visible-tab keepalive alias.

- [ ] **Step 2: Write a failing main dispatch test**

Patch the heartbeat module entrypoint and assert `ccm.main(["heartbeat", "test", "--tab-title", "mycel"])` delegates to it.

- [ ] **Step 3: Run the focused test selection and watch it fail**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra
python3 -m unittest \
  tests.test_cli.ParserHelpTests.test_heartbeat_help_mentions_visible_tab_keepalive_alias \
  tests.test_cli.MainEntryPointTests.test_main_heartbeat_delegates_to_heartbeat_module \
  -v
```

Expected: failing tests because `ccm` does not expose `heartbeat` yet.

### Task 2: Implement The Alias Minimally

**Files:**
- Modify: `ccm_orchestra/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Add the parser entry**

Add a `heartbeat` subparser that captures the remaining argv and explains that it is the `ccm` alias for the existing keepalive tool.

- [ ] **Step 2: Add one forwarding branch in `main(...)`**

Delegate `heartbeat` to `ccm_orchestra.heartbeat.main(...)` with the captured remainder argv. Do not reimplement heartbeat behavior in `cli.py`.

- [ ] **Step 3: Run the focused tests and verify green**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra
python3 -m unittest \
  tests.test_cli.ParserHelpTests.test_heartbeat_help_mentions_visible_tab_keepalive_alias \
  tests.test_cli.MainEntryPointTests.test_main_heartbeat_delegates_to_heartbeat_module \
  -v
```

Expected: both tests pass.

### Task 3: Update Operator Surfaces

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `AGENTS.md`
- Modify: `docs/codex-claude-visible-collab-playbook.md`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Update wording**

Replace wording that says heartbeat is only a separate tool with wording that says `ccm heartbeat ...` and `codex-heartbeat ...` are equivalent entry points.

- [ ] **Step 2: Lock one root help assertion**

Extend parser help assertions so the root help continues to mention heartbeat by the `ccm heartbeat` name as the primary discoverable path.

- [ ] **Step 3: Run the full targeted suites**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra
python3 -m unittest tests/test_cli.py tests/test_heartbeat.py tests/test_smoke.py -v
```

Expected: all relevant CLI and heartbeat tests pass.

## Execution Notes

- The first implementation cut exposed the real runtime seam quickly: repo tests passed, but the shipped `~/bin/ccm` wrapper executes `python3 ~/.local/share/ccm/ccm_orchestra/cli.py` directly, so a top-level `from ccm_orchestra import heartbeat` import can still fail in normal user entry with `ModuleNotFoundError: No module named 'ccm_orchestra'`.
- The minimal repair is not to rewrite the wrapper. Instead, `cli.py` now resolves heartbeat lazily through `load_heartbeat_module()`: try `ccm_orchestra.heartbeat` first, then fall back to sibling-script import `heartbeat` when running as a bare script from the package directory.
- Fresh focused repo proof is green: `python3 -m unittest tests/test_cli.py tests/test_heartbeat.py tests/test_smoke.py -v` passed with 128 tests, including the new heartbeat dispatch and fallback-import coverage.
- Fresh shipped-runtime proof is also green. `ccm --help` now lists `heartbeat`, `ccm heartbeat --help` renders the alias help text, and a disposable tab `heartbeat-alias-proof-20260408` accepted `ccm heartbeat start/status/stop` with a real delivered token `HB_ALIAS_20260408`.
- Stopline for this slice is explicit: `ccm heartbeat` is discoverable and working in the real installed runtime, `codex-heartbeat` still works as the equivalent alias, and no wrapper packaging rewrite is bundled into the same change.
